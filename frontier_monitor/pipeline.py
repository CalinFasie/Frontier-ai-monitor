from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from .collectors import collect_all
from .config import ROOT, Settings, load_yaml
from .db import Database
from .enrich import enrich_sources
from .evidence import evidence_profile, publication_gate
from .providers import ProviderPool, ProviderResult
from .utils import clip, read_text, title_similarity, utcnow

log = logging.getLogger(__name__)


def _date(value: Any) -> str:
    if not value:
        return "unknown"
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def collect_and_store(db: Database, cfg: dict[str, Any], settings: Settings) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    items, stats = collect_all(cfg, settings.lookback_hours)
    cutoff = utcnow() - timedelta(hours=settings.lookback_hours + 12)
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stored = 0
    for item in items:
        published = item.get("published_at")
        if published and published < cutoff:
            continue
        source_id = db.upsert_source(item)
        row = dict(item)
        row["id"] = source_id
        by_topic[item.get("category_hint") or "ai_research_automation"].append(row)
        stored += 1
    stats["stored_sources"] = stored
    stats["topic_counts"] = {k: len(v) for k, v in by_topic.items()}
    return by_topic, stats


def _scout_input(topic_key: str, topic_label: str, rows: list[dict[str, Any]], max_rows: int = 12) -> str:
    # Newest first; cap each topic to keep each free-tier request comfortably small.
    rows = sorted(rows, key=lambda r: r.get("published_at") or utcnow(), reverse=True)[:max_rows]
    lines = [f"TOPIC KEY: {topic_key}", f"TOPIC LABEL: {topic_label}", "", "SOURCE RECORDS:"]
    for r in rows:
        lines.append(
            f"\nSOURCE_ID={r['id']}\n"
            f"DATE={_date(r.get('published_at'))}\n"
            f"PUBLISHER={r.get('publisher') or 'unknown'}\n"
            f"TITLE={clip(r.get('title'), 260)}\n"
            f"SNIPPET={clip(r.get('snippet'), 420) or '[title only; be conservative]'}"
        )
    return "\n".join(lines)


def run_scout(
    pool: ProviderPool,
    topics_cfg: dict[str, Any],
    by_topic: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[ProviderResult], dict[str, str]]:
    system = read_text(ROOT / "prompts/scout.txt")
    all_candidates: list[dict[str, Any]] = []
    results: list[ProviderResult] = []
    failures: dict[str, str] = {}
    active_topics = [k for k in topics_cfg if by_topic.get(k)]

    for position, topic_key in enumerate(active_topics):
        topic = topics_cfg[topic_key]
        rows = by_topic.get(topic_key, [])
        allowed = {int(r["id"]) for r in rows}
        try:
            result = pool.call(
                "scout",
                system,
                _scout_input(topic_key, topic.get("label", topic_key), rows),
            )
        except Exception as exc:
            # One free-provider failure must not erase successful scouting in
            # every other category. We still enforce a minimum-coverage gate in
            # main.py, so this cannot silently become a false quiet day.
            failures[topic_key] = str(exc)[:1500]
            log.error("Scout failed for topic %s: %s", topic_key, exc)
            if position + 1 < len(active_topics):
                delay = float(os.getenv("SCOUT_CALL_DELAY_SECONDS", "30"))
                if delay > 0:
                    time.sleep(delay)
            continue

        results.append(result)
        for c in result.data.get("candidates", [])[:5]:
            try:
                ids = [int(x) for x in c.get("source_ids", []) if int(x) in allowed]
                if not ids:
                    continue
                c["source_ids"] = list(dict.fromkeys(ids))[:5]
                c["category"] = topic_key
                c["materiality"] = int(c.get("materiality", 0))
                c["novelty"] = int(c.get("novelty", 0))
                c["scout_provider"] = result.provider
                c["scout_model"] = result.actual_model
                all_candidates.append(c)
            except Exception:
                log.warning("Discarding malformed scout candidate: %r", c)

        if position + 1 < len(active_topics):
            # Eight seconds was too aggressive for ~3k-token requests against
            # an 8k TPM bucket. The v8 default is intentionally conservative.
            # The prompt is also ~40% smaller than v7.
            delay = float(os.getenv("SCOUT_CALL_DELAY_SECONDS", "30"))
            if delay > 0:
                time.sleep(delay)

    return dedupe_candidates(all_candidates), results, failures


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda c: (c.get("materiality", 0), c.get("novelty", 0)), reverse=True)
    kept: list[dict[str, Any]] = []
    for c in ordered:
        duplicate = None
        for k in kept:
            same_sources = bool(set(c.get("source_ids", [])) & set(k.get("source_ids", [])))
            similar = title_similarity(c.get("canonical_title", ""), k.get("canonical_title", "")) >= 0.84
            if same_sources or similar:
                duplicate = k
                break
        if duplicate:
            duplicate["source_ids"] = list(dict.fromkeys(duplicate.get("source_ids", []) + c.get("source_ids", [])))[:6]
            duplicate["materiality"] = max(duplicate.get("materiality", 0), c.get("materiality", 0))
            duplicate["novelty"] = max(duplicate.get("novelty", 0), c.get("novelty", 0))
        else:
            kept.append(c)
    return kept


def balanced_select(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(candidates) <= limit:
        return candidates
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        groups[c.get("category", "other")].append(c)
    for arr in groups.values():
        arr.sort(key=lambda x: (x.get("materiality", 0), x.get("novelty", 0)), reverse=True)
    selected: list[dict[str, Any]] = []
    # One top candidate per active category first.
    for category in sorted(groups):
        if groups[category] and len(selected) < limit:
            selected.append(groups[category].pop(0))
    remaining = [x for arr in groups.values() for x in arr]
    remaining.sort(key=lambda x: (x.get("materiality", 0), x.get("novelty", 0)), reverse=True)
    selected.extend(remaining[: max(0, limit - len(selected))])
    return selected[:limit]


def _candidate_packet(db: Database, candidate: dict[str, Any]) -> dict[str, Any]:
    """Build a compact Editor packet.

    The deterministic evidence gate still evaluates every linked source. The
    LLM only needs the strongest few excerpts plus a compact source-role map.
    This distinction is important on Groq's free TPM tier.
    """
    all_rows = db.get_sources(candidate.get("source_ids", [])[:10])

    # Fetch/extract at most three direct sources. Google News aggregator links
    # are skipped by enrich.py; primary/official/research pages get preference
    # through the acquisition stage and evidence-role metadata below.
    enriched_rows = enrich_sources(db, [int(r["id"]) for r in all_rows[:3]])
    enriched_by_id = {int(r["id"]): r for r in enriched_rows}

    profile = evidence_profile(all_rows, candidate=candidate)
    assessment_by_id = {
        int(a["id"]): a for a in profile.get("sources", [])
        if a.get("role") not in {"irrelevant", "context_only"}
    }

    # Give the Editor a maximum of five concrete source records. The gate still
    # sees the full profile/counts, so prompt compression does not weaken the
    # deterministic publication test.
    ranked_rows = sorted(
        all_rows,
        key=lambda r: (
            1 if int(r["id"]) in assessment_by_id else 0,
            float((assessment_by_id.get(int(r["id"])) or {}).get("relevance", 0)),
        ),
        reverse=True,
    )[:5]

    sources = []
    for original in ranked_rows:
        r = enriched_by_id.get(int(original["id"]), original)
        assessment = assessment_by_id.get(int(r["id"]), {})
        evidence = r.get("fetched_text") or r.get("snippet") or ""
        sources.append(
            {
                "id": int(r["id"]),
                "title": clip(r.get("title"), 180),
                "publisher": clip(r.get("publisher"), 80),
                "published_at": _date(r.get("published_at")),
                "url": r.get("url"),
                "role": assessment.get("role"),
                "relevance": assessment.get("relevance"),
                "evidence_text": (
                    clip(evidence, 340)
                    if int(r["id"]) in enriched_by_id
                    else clip(r.get("snippet"), 120)
                ),
            }
        )

    acq = candidate.get("evidence_acquisition") or {}
    compact_acq = {
        "stage_before": acq.get("stage_before"),
        "stage_after": acq.get("stage_after"),
        "targeted_attempted": acq.get("targeted_attempted", []),
        "targeted_added": acq.get("targeted_added", 0),
    }

    return {
        "canonical_title": clip(candidate.get("canonical_title"), 180),
        "category": candidate.get("category"),
        "what_happened_from_scout": clip(candidate.get("what_happened"), 280),
        "why_potentially_material": clip(candidate.get("why_potentially_material"), 180),
        "scout_materiality": candidate.get("materiality"),
        "scout_novelty": candidate.get("novelty"),
        "evidence_stage": candidate.get("evidence_stage"),
        "evidence_stage_upgraded_from": candidate.get("evidence_stage_upgraded_from"),
        "evidence_acquisition": compact_acq,
        "evidence_profile": {k: v for k, v in profile.items() if k != "sources"},
        "source_assessments": [
            {
                "id": int(a["id"]),
                "role": a.get("role"),
                "publisher": clip(a.get("publisher"), 70),
                "relevance": a.get("relevance"),
            }
            for a in profile.get("sources", [])
            if a.get("role") not in {"irrelevant", "context_only"}
        ][:6],
        "sources": sources,
    }

def _known_packet(known: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": d["id"],
            "title": clip(d.get("canonical_title"), 120),
            "category": d.get("category"),
            "status": d.get("status"),
            "current_state": clip(d.get("current_state"), 240),
            "last_seen_at": _date(d.get("last_seen_at")),
            "reported_at": _date(d.get("reported_at")),
            "materiality": d.get("materiality"),
            "evidence_strength": d.get("evidence_strength"),
        }
        for d in known
    ]


_STATE_STOP = {
    "about", "after", "again", "against", "among", "being", "could", "from", "have", "into",
    "more", "than", "that", "their", "this", "those", "through", "under", "using", "with",
    "without", "would", "will", "model", "models", "artificial", "intelligence", "system", "systems",
    "report", "reports", "study", "research", "new", "first", "announces", "announcement",
}


def _state_tokens(text: str) -> set[str]:
    import re
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+._/-]{2,}", text or "")
        if len(w) >= 4 and w.lower() not in _STATE_STOP
    }


def _development_match_score(candidate: dict[str, Any], known: dict[str, Any]) -> float:
    c_title = str(candidate.get("canonical_title") or "")
    k_title = str(known.get("canonical_title") or "")
    seq = title_similarity(c_title, k_title)
    c_tokens = _state_tokens(c_title + " " + str(candidate.get("what_happened") or ""))
    k_tokens = _state_tokens(k_title + " " + str(known.get("current_state") or ""))
    overlap = len(c_tokens & k_tokens) / max(1, min(len(c_tokens), len(k_tokens), 10))
    category_bonus = 0.10 if candidate.get("category") == known.get("category") else 0.0
    return min(1.0, (0.58 * seq) + (0.42 * overlap) + category_bonus)


def _known_matches_for_candidate(candidate: dict[str, Any], known: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    ranked = sorted(
        ((_development_match_score(candidate, d), d) for d in known),
        key=lambda x: x[0],
        reverse=True,
    )
    selected = []
    for score, d in ranked:
        if score < 0.30:
            continue
        row = _known_packet([d])[0]
        row["match_score"] = round(score, 3)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


_MEANINGFUL_DELTA_KINDS = {
    "new_development",
    "status_progression",
    "evidence_strengthening",
    "deployment_scale_change",
    "scope_expansion",
    "policy_or_legal_action",
    "infrastructure_commitment",
    "contradiction_or_retraction",
}


_BENCHMARK_MARKERS = {
    "benchmark", "leaderboard", "score", "accuracy", "pass@", "sota",
    "state of the art", "cybergym", "swe-bench", "gpqa", "mmlu",
}
_PRACTICAL_CAPABILITY_MARKERS = {
    "deployed", "deployment", "production", "real-world", "real world",
    "independent test", "independently tested", "exploited", "campaign",
    "end-to-end", "end to end", "autonomous operation", "used against",
    "field test", "customer workload", "scientific validation",
}


def _benchmark_only_update(candidate: dict[str, Any], decision: dict[str, Any]) -> bool:
    text = " ".join(
        str(x or "")
        for x in [
            candidate.get("canonical_title"), candidate.get("what_happened"),
            decision.get("canonical_title"), decision.get("what_changed"),
            decision.get("state_delta"), decision.get("why_it_matters"),
        ]
    ).lower()
    has_benchmark = any(marker in text for marker in _BENCHMARK_MARKERS)
    has_practical = any(marker in text for marker in _PRACTICAL_CAPABILITY_MARKERS)
    status = str(decision.get("status") or candidate.get("evidence_stage") or "").lower()
    return has_benchmark and not has_practical and status in {"paper", "demo", "announcement"}


def run_editor(
    pool: ProviderPool,
    db: Database,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[ProviderResult], list[dict[str, Any]]]:
    """Run the senior editor one candidate at a time.

    Groq's free GPT-OSS tier is constrained by TPM. A monolithic ten-candidate
    prompt can exceed that cap in a *single request*, in which case retries can
    never help. Candidate-level calls keep each request bounded and make the
    failure domain auditable.
    """
    packets = [_candidate_packet(db, c) for c in candidates]

    known_all = db.known_developments(days=180, limit=300)
    valid_known_by_idx: dict[int, set[str]] = {}
    for idx, (candidate, packet) in enumerate(zip(candidates, packets)):
        matches = _known_matches_for_candidate(candidate, known_all, limit=3)
        packet["candidate_index"] = idx
        packet["prior_state_matches"] = matches
        valid_known_by_idx[idx] = {str(m["id"]) for m in matches}

    system = read_text(ROOT / "prompts/editor.txt")
    results: list[ProviderResult] = []
    raw_decisions: list[dict[str, Any]] = []
    model_bottom_lines: list[str] = []

    delay = float(os.getenv("EDITOR_CALL_DELAY_SECONDS", "65"))
    for pos, packet in enumerate(packets):
        user = (
            "EVALUATE EXACTLY THIS ONE CANDIDATE. Preserve candidate_index in your JSON response.\n"
            + json.dumps(packet, ensure_ascii=False, default=str)
        )
        # One attempt per provider for the editor. Repeating an identical
        # over-size request only creates duplicate 429s; genuine provider
        # outages should fail closed rather than silently change the brief.
        result = pool.call("editor", system, user, attempts_per_provider=1)
        results.append(result)
        raw_decisions.extend(result.data.get("decisions", []))
        if result.data.get("bottom_line"):
            model_bottom_lines.append(str(result.data.get("bottom_line")))

        if pos + 1 < len(packets) and delay > 0:
            time.sleep(delay)

    decisions: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for d in raw_decisions:
        try:
            idx = int(d.get("candidate_index"))
            if idx < 0 or idx >= len(candidates) or idx in seen_indices:
                continue
            seen_indices.add(idx)
            d["candidate_index"] = idx
            if d.get("matched_development_id") not in valid_known_by_idx.get(idx, set()):
                d["matched_development_id"] = None
            d["decision"] = str(d.get("decision", "IGNORE")).upper()
            if d["decision"] not in {"REPORT", "WATCH", "IGNORE"}:
                d["decision"] = "IGNORE"
            d["materiality"] = float(d.get("materiality", 0))
            d["update_materiality"] = float(d.get("update_materiality", d.get("materiality", 0)))
            d["evidence_strength"] = float(d.get("evidence_strength", 0))
            d["novelty"] = float(d.get("novelty", 0))
            delta_kind = str(d.get("state_delta_kind") or "none").lower()
            if delta_kind not in (_MEANINGFUL_DELTA_KINDS | {"none"}):
                delta_kind = "none"
            d["state_delta_kind"] = delta_kind
            d["evidence_acquisition"] = candidates[idx].get("evidence_acquisition")

            if _benchmark_only_update(candidates[idx], d):
                d["decision"] = "IGNORE"
                d["update_materiality"] = min(d["update_materiality"], 4.0)
                d["state_gate_reason"] = "benchmark_only_without_practical_capability_shift"

            if d.get("matched_development_id") and delta_kind == "none":
                d["decision"] = "IGNORE"
                d["update_materiality"] = 0.0
                d["state_gate_reason"] = "matched_prior_state_no_material_delta"
            elif d.get("matched_development_id") and d["decision"] == "WATCH" and d["update_materiality"] < 4:
                d["decision"] = "IGNORE"
                d["state_gate_reason"] = "matched_prior_state_weak_delta"

            profile = packets[idx].get("evidence_profile", {})
            passed, reason = publication_gate(d, profile)
            d["verification_gate"] = {
                "passed": passed,
                "reason": reason,
                "profile": profile,
            }
            if d["decision"] == "REPORT" and not passed:
                d["decision"] = "WATCH" if d["update_materiality"] >= 5 else "IGNORE"
                d["gate_downgraded_from_report"] = True
            decisions.append(d)
        except Exception:
            log.warning("Discarding malformed editor decision: %r", d)

    # If a provider returned valid JSON but omitted a candidate decision, fail
    # closed instead of turning an incomplete editorial run into a false quiet
    # day. The next scheduled run can retry.
    missing = sorted(set(range(len(candidates))) - {int(d["candidate_index"]) for d in decisions})
    if missing:
        raise RuntimeError(f"Editor omitted candidate decisions for indices: {missing}")

    downgraded = sum(1 for d in decisions if d.get("gate_downgraded_from_report"))
    report_titles = [d.get("canonical_title") for d in decisions if d.get("decision") == "REPORT"]
    if downgraded:
        if report_titles:
            bottom_line = (
                f"The deterministic verification gate retained {len(report_titles)} development(s) for reporting: "
                + "; ".join(str(x) for x in report_titles[:4])
                + f". {downgraded} proposed REPORT item(s) were moved to WATCH pending stronger evidence."
            )
        else:
            bottom_line = f"No candidate passed the deterministic publication-evidence gate; {downgraded} proposed REPORT item(s) remain on WATCH."
    elif report_titles:
        bottom_line = f"{len(report_titles)} materially new development(s) crossed the publication threshold this cycle."
    else:
        bottom_line = "No materially new development crossed the publication threshold this cycle."

    return {"decisions": decisions, "bottom_line": bottom_line}, results, packets

def render_brief(editor: dict[str, Any], candidates: list[dict[str, Any]], db: Database, max_items: int) -> str:
    report_items = [d for d in editor.get("decisions", []) if d.get("decision") == "REPORT"]
    report_items.sort(key=lambda d: (d.get("materiality", 0), d.get("evidence_strength", 0)), reverse=True)
    report_items = report_items[:max_items]
    date = utcnow().date().isoformat()
    if not report_items:
        return f"# Frontier AI Brief - {date}\n\nNo material frontier developments since the previous review.\n\n## Bottom line\n\n{editor.get('bottom_line') or 'No evidence crossed the publication threshold.'}\n"

    parts = [f"# Frontier AI Brief - {date}"]
    for n, d in enumerate(report_items, 1):
        candidate = candidates[int(d["candidate_index"])]
        source_rows = db.get_sources(candidate.get("source_ids", []))
        parts.extend(
            [
                f"\n## {n}. {d.get('canonical_title', candidate.get('canonical_title', 'Untitled'))}",
                f"\n**Area:** {d.get('category', candidate.get('category'))}",
                f"\n**What changed:**  \n{d.get('what_changed', '')}",
                f"\n**Why it matters:**  \n{d.get('why_it_matters', '')}",
                f"\n**Evidence:** underlying materiality {d.get('materiality', 0):g}/10; update materiality {d.get('update_materiality', d.get('materiality', 0)):g}/10; evidence {d.get('evidence_strength', 0):g}/10; status `{d.get('status', 'unknown')}`",
                f"\n**Verification:** `{(d.get('verification_gate') or {}).get('reason', 'not_recorded')}`",
                (
                    "\n**Evidence note:** Primary research result; this brief is reporting what the paper demonstrates/reports, "
                    "not claiming independent replication."
                    if (d.get("verification_gate") or {}).get("reason") == "primary_research_material_claim_not_replication"
                    else (
                        "\n**Evidence note:** A DOI/indexed research record plus independent reputable coverage supports the existence and reported result; "
                        "this is not a claim of independent replication."
                        if (d.get("verification_gate") or {}).get("reason") == "research_index_plus_independent_secondary"
                        else ""
                    )
                ),
                "\n**Sources:**",
            ]
        )
        seen_urls = set()
        for s in source_rows[:7]:
            if s["url"] in seen_urls:
                continue
            seen_urls.add(s["url"])
            label = s.get("publisher") or s.get("title") or "Source"
            parts.append(f"- [{label}]({s['url']}) - {clip(s.get('title'), 150)}")
    parts.extend(["\n## Bottom line", "", editor.get("bottom_line") or "The items above crossed the materiality threshold."])
    return "\n".join(parts).strip() + "\n"


def persist_editor_results(db: Database, run_id: str, editor: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"REPORT": 0, "WATCH": 0, "IGNORE": 0}
    for decision in editor.get("decisions", []):
        idx = int(decision["candidate_index"])
        candidate = candidates[idx]
        source_ids = candidate.get("source_ids", [])
        db.log_candidate_decision(run_id, decision, source_ids)
        label = decision.get("decision", "IGNORE")
        counts[label] = counts.get(label, 0) + 1
        if label in {"REPORT", "WATCH"}:
            db.persist_decision(run_id, decision, source_ids)
    return counts
