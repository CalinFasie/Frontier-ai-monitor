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
    rows = enrich_sources(db, candidate.get("source_ids", [])[:2])
    sources = []
    for r in rows:
        evidence = r.get("fetched_text") or r.get("snippet") or ""
        sources.append(
            {
                "id": int(r["id"]),
                "title": r["title"],
                "publisher": r.get("publisher"),
                "published_at": _date(r.get("published_at")),
                "url": r["url"],
                "evidence_text": clip(evidence, 600),
            }
        )
    return {
        "canonical_title": candidate.get("canonical_title"),
        "category": candidate.get("category"),
        "what_happened_from_scout": clip(candidate.get("what_happened"), 350),
        "why_potentially_material": clip(candidate.get("why_potentially_material"), 250),
        "scout_materiality": candidate.get("materiality"),
        "scout_novelty": candidate.get("novelty"),
        "evidence_stage": candidate.get("evidence_stage"),
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


def run_editor(pool: ProviderPool, db: Database, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], ProviderResult, list[dict[str, Any]]]:
    packets = [_candidate_packet(db, c) for c in candidates]
    known = _known_packet(db.known_developments(days=90, limit=20))
    user = (
        "KNOWN DEVELOPMENTS FROM PRIOR STATE:\n"
        + json.dumps(known, ensure_ascii=False, default=str)
        + "\n\nNEW CANDIDATES WITH EVIDENCE:\n"
        + json.dumps(packets, ensure_ascii=False, default=str)
    )
    system = read_text(ROOT / "prompts/editor.txt")
    result = pool.call("editor", system, user)
    valid_known = {d["id"] for d in known}
    decisions = []
    for d in result.data.get("decisions", []):
        try:
            idx = int(d.get("candidate_index"))
            if idx < 0 or idx >= len(candidates):
                continue
            d["candidate_index"] = idx
            if d.get("matched_development_id") not in valid_known:
                d["matched_development_id"] = None
            d["decision"] = str(d.get("decision", "IGNORE")).upper()
            if d["decision"] not in {"REPORT", "WATCH", "IGNORE"}:
                d["decision"] = "IGNORE"
            d["materiality"] = float(d.get("materiality", 0))
            d["evidence_strength"] = float(d.get("evidence_strength", 0))
            d["novelty"] = float(d.get("novelty", 0))
            # Enforce threshold in code, not only in prompt.
            if d["decision"] == "REPORT" and d["materiality"] < 7:
                d["decision"] = "WATCH" if d["materiality"] >= 5 else "IGNORE"
            decisions.append(d)
        except Exception:
            log.warning("Discarding malformed editor decision: %r", d)
    return {"decisions": decisions, "bottom_line": result.data.get("bottom_line", "")}, result, packets


def render_brief(editor: dict[str, Any], candidates: list[dict[str, Any]], db: Database, max_items: int) -> str:
    report_items = [d for d in editor.get("decisions", []) if d.get("decision") == "REPORT"]
    report_items.sort(key=lambda d: (d.get("materiality", 0), d.get("evidence_strength", 0)), reverse=True)
    report_items = report_items[:max_items]
    date = utcnow().date().isoformat()
    if not report_items:
        return f"# Frontier AI Brief — {date}\n\nNo material frontier developments since the previous review.\n\n## Bottom line\n\n{editor.get('bottom_line') or 'No evidence crossed the publication threshold.'}\n"

    parts = [f"# Frontier AI Brief — {date}"]
    for n, d in enumerate(report_items, 1):
        candidate = candidates[int(d["candidate_index"])]
        source_rows = db.get_sources(candidate.get("source_ids", []))
        parts.extend(
            [
                f"\n## {n}. {d.get('canonical_title', candidate.get('canonical_title', 'Untitled'))}",
                f"\n**Area:** {d.get('category', candidate.get('category'))}",
                f"\n**What changed:**  \n{d.get('what_changed', '')}",
                f"\n**Why it matters:**  \n{d.get('why_it_matters', '')}",
                f"\n**Evidence:** materiality {d.get('materiality', 0):g}/10; evidence {d.get('evidence_strength', 0):g}/10; status `{d.get('status', 'unknown')}`",
                "\n**Sources:**",
            ]
        )
        seen_urls = set()
        for s in source_rows[:5]:
            if s["url"] in seen_urls:
                continue
            seen_urls.add(s["url"])
            label = s.get("publisher") or s.get("title") or "Source"
            parts.append(f"- [{label}]({s['url']}) — {clip(s.get('title'), 150)}")
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
