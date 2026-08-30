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


def _scout_input(topic_key: str, topic_label: str, rows: list[dict[str, Any]], max_rows: int = 20) -> str:
    # Newest first; cap each topic to keep each free-tier request comfortably small.
    rows = sorted(rows, key=lambda r: r.get("published_at") or utcnow(), reverse=True)[:max_rows]
    lines = [f"TOPIC KEY: {topic_key}", f"TOPIC LABEL: {topic_label}", "", "SOURCE RECORDS:"]
    for r in rows:
        lines.append(
            f"\nSOURCE_ID={r['id']}\n"
            f"DATE={_date(r.get('published_at'))}\n"
            f"PUBLISHER={r.get('publisher') or 'unknown'}\n"
            f"TITLE={clip(r.get('title'), 260)}\n"
            f"SNIPPET={clip(r.get('snippet'), 650) or '[title only; be conservative]'}"
        )
    return "\n".join(lines)


def run_scout(pool: ProviderPool, topics_cfg: dict[str, Any], by_topic: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[ProviderResult]]:
    system = read_text(ROOT / "prompts/scout.txt")
    all_candidates: list[dict[str, Any]] = []
    results: list[ProviderResult] = []
    for topic_key, topic in topics_cfg.items():
        rows = by_topic.get(topic_key, [])
        if not rows:
            continue
        allowed = {int(r["id"]) for r in rows}
        result = pool.call("scout", system, _scout_input(topic_key, topic.get("label", topic_key), rows))
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
        # Groq free-tier TPM is intentionally respected; fallback providers remain available.
        delay = float(os.getenv("SCOUT_CALL_DELAY_SECONDS", "8"))
        if delay > 0:
            time.sleep(delay)
    return dedupe_candidates(all_candidates), results


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
    # Use up to three evidence excerpts for the Editor, but assess every source
    # linked by the Scout for the deterministic publication gate.
    all_rows = db.get_sources(candidate.get("source_ids", [])[:10])
    # Extract full text only for the first four sources to stay within free-tier
    # token limits; metadata for all linked sources still feeds the gate.
    enriched_rows = enrich_sources(db, [int(r["id"]) for r in all_rows[:4]])
    enriched_by_id = {int(r["id"]): r for r in enriched_rows}

    sources = []
    for original in all_rows:
        r = enriched_by_id.get(int(original["id"]), original)
        evidence = r.get("fetched_text") or r.get("snippet") or ""
        sources.append(
            {
                "id": int(r["id"]),
                "title": r["title"],
                "publisher": r.get("publisher"),
                "published_at": _date(r.get("published_at")),
                "url": r["url"],
                "source_type": r.get("source_type"),
                # Keep the editor prompt inside free-tier TPM. Sources after
                # the first three still contribute metadata to verification.
                "evidence_text": clip(evidence, 500) if int(r["id"]) in enriched_by_id else clip(r.get("snippet"), 180),
            }
        )

    profile = evidence_profile(all_rows)
    return {
        "canonical_title": candidate.get("canonical_title"),
        "category": candidate.get("category"),
        "what_happened_from_scout": clip(candidate.get("what_happened"), 350),
        "why_potentially_material": clip(candidate.get("why_potentially_material"), 250),
        "scout_materiality": candidate.get("materiality"),
        "scout_novelty": candidate.get("novelty"),
        "evidence_stage": candidate.get("evidence_stage"),
        "evidence_profile": {k: v for k, v in profile.items() if k != "sources"},
        "source_assessments": profile.get("sources", []),
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


def run_editor(pool: ProviderPool, db: Database, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], ProviderResult, list[dict[str, Any]]]:
    packets = [_candidate_packet(db, c) for c in candidates]

    # State matching must scale beyond the newest handful of developments.
    # Retrieve a bounded 180-day state window, then give each candidate only
    # its few most plausible prior matches. This keeps the prompt small while
    # avoiding the old "last 20 developments" blind spot.
    known_all = db.known_developments(days=180, limit=300)
    valid_known_by_idx: dict[int, set[str]] = {}
    for idx, (candidate, packet) in enumerate(zip(candidates, packets)):
        matches = _known_matches_for_candidate(candidate, known_all, limit=4)
        packet["prior_state_matches"] = matches
        valid_known_by_idx[idx] = {str(m["id"]) for m in matches}

    user = "NEW CANDIDATES WITH EVIDENCE AND RELEVANT PRIOR STATE:\n" + json.dumps(
        packets, ensure_ascii=False, default=str
    )
    system = read_text(ROOT / "prompts/editor.txt")
    result = pool.call("editor", system, user)
    decisions = []
    for d in result.data.get("decisions", []):
        try:
            idx = int(d.get("candidate_index"))
            if idx < 0 or idx >= len(candidates):
                continue
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

            # A matched prior development with no actual state delta is not a
            # WATCH item. Persisting it as WATCH would refresh/overwrite state
            # forever and create "story drift". Keep it in candidate_decisions
            # for audit, but do not let it mutate the development state.
            if d.get("matched_development_id") and delta_kind == "none":
                d["decision"] = "IGNORE"
                d["update_materiality"] = 0.0
                d["state_gate_reason"] = "matched_prior_state_no_material_delta"
            elif d.get("matched_development_id") and d["decision"] == "WATCH" and d["update_materiality"] < 4:
                d["decision"] = "IGNORE"
                d["state_gate_reason"] = "matched_prior_state_weak_delta"

            # Enforce thresholds + primary/independent-source publication gate
            # in code, using UPDATE materiality rather than the historical
            # importance of the underlying story.
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

    downgraded = sum(1 for d in decisions if d.get("gate_downgraded_from_report"))
    report_titles = [d.get("canonical_title") for d in decisions if d.get("decision") == "REPORT"]
    bottom_line = result.data.get("bottom_line", "")
    if downgraded:
        if report_titles:
            bottom_line = (
                f"The deterministic verification gate retained {len(report_titles)} development(s) for reporting: "
                + "; ".join(str(x) for x in report_titles[:4])
                + f". {downgraded} proposed REPORT item(s) were moved to WATCH pending stronger evidence."
            )
        else:
            bottom_line = f"No candidate passed the deterministic publication-evidence gate; {downgraded} proposed REPORT item(s) remain on WATCH."
    return {"decisions": decisions, "bottom_line": bottom_line}, result, packets


def render_brief(editor: dict[str, Any], candidates: list[dict[str, Any]], db: Database, max_items: int) -> str:
    report_items = [d for d in editor.get("decisions", []) if d.get("decision") == "REPORT"]
    report_items.sort(key=lambda d: (d.get("materiality", 0), d.get("evidence_strength", 0)), reverse=True)
    report_items = report_items[:max_items]
    date = utcnow().date().isoformat()
    if not report_items:
        return f"# Frontier AI Brief â€” {date}\n\nNo material frontier developments since the previous review.\n\n## Bottom line\n\n{editor.get('bottom_line') or 'No evidence crossed the publication threshold.'}\n"

    parts = [f"# Frontier AI Brief â€” {date}"]
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
                    else ""
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
            parts.append(f"- [{label}]({s['url']}) â€” {clip(s.get('title'), 150)}")
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
