from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from .config import ROOT, Settings, load_yaml
from .db import Database
from .emailer import send_if_configured
from .evidence_acquisition import acquire_evidence_for_candidates
from .pipeline import (
    balanced_select,
    collect_and_store,
    persist_editor_results,
    render_brief,
    run_editor,
    run_scout,
)
from .providers import ProviderPool
from .utils import utcnow


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Frontier AI change-detection monitor")
    p.add_argument("--collect-only", action="store_true", help="Collect and persist sources without calling an LLM")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    load_dotenv(ROOT / ".env")
    if os.getenv("GITHUB_ACTIONS", "").lower() == "true" and not os.getenv("DATABASE_URL", "").strip():
        raise RuntimeError("DATABASE_URL is required in GitHub Actions so state persists between runs")
    settings = Settings.from_env()
    topics_cfg = load_yaml("config/topics.yaml")
    model_cfg = load_yaml("config/models.yaml")
    db = Database(settings.database_url)
    run_id = utcnow().strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    db.start_run(run_id)
    stats = {"run_id": run_id}

    try:
        by_topic, discovery = collect_and_store(db, topics_cfg, settings)
        stats["discovery"] = discovery
        if len(discovery.get("collector_successes", [])) < settings.min_discovery_successes:
            raise RuntimeError(
                f"Discovery degraded: only {len(discovery.get('collector_successes', []))} collectors succeeded; "
                f"minimum is {settings.min_discovery_successes}. Failures={discovery.get('collector_failures')}"
            )
        if int(discovery.get("stored_sources", 0)) < settings.min_discovered_sources:
            raise RuntimeError(
                f"Discovery suspiciously sparse: {discovery.get('stored_sources', 0)} sources stored; "
                f"minimum is {settings.min_discovered_sources}. Refusing to interpret this as a quiet news day."
            )
        if args.collect_only:
            db.finish_run(run_id, "collected", stats)
            print(json.dumps(stats, indent=2, default=str))
            return 0

        pool = ProviderPool(model_cfg)
        candidates, scout_results, scout_failures = run_scout(pool, topics_cfg.get("topics", {}), by_topic)
        active_scout_topics = sum(1 for key in topics_cfg.get("topics", {}) if by_topic.get(key))
        min_scout_successes = min(
            active_scout_topics,
            int(os.getenv("MIN_SCOUT_SUCCESSES", "6")),
        )
        stats["scout_topic_successes"] = len(scout_results)
        stats["scout_topic_failures"] = scout_failures
        if len(scout_results) < min_scout_successes:
            raise RuntimeError(
                f"Scout coverage degraded: only {len(scout_results)}/{active_scout_topics} topic calls succeeded; "
                f"minimum is {min_scout_successes}. Failures={scout_failures}"
            )
        candidates = balanced_select(candidates, settings.max_editor_candidates)
        stats["scout_candidates"] = len(candidates)

        # Before the Editor decides, expand each candidate's evidence from the
        # already-collected corpus and, only when needed, targeted free RSS/arXiv
        # retrieval. This improves corroboration without a paid web-search API.
        evidence_acquisition = acquire_evidence_for_candidates(
            db, candidates, topics_cfg.get("topics", {}), settings.lookback_hours
        ) if candidates else []
        stats["evidence_acquisition"] = evidence_acquisition
        stats["scout_calls"] = [
            {"provider": r.provider, "requested_model": r.requested_model, "actual_model": r.actual_model}
            for r in scout_results
        ]

        if candidates:
            editor, editor_results, packets = run_editor(pool, db, candidates)
            editor_result = editor_results[-1] if editor_results else None
        else:
            # No candidate survived high-recall scouting. This is a legitimate quiet result
            # as long as discovery itself passed health checks.
            editor = {"decisions": [], "bottom_line": "No candidate from the monitored sources crossed the materiality threshold."}
            editor_results = []
            editor_result = None

        counts = persist_editor_results(db, run_id, editor, candidates)
        stats["editor_decisions"] = counts
        if editor_results:
            stats["editor_calls"] = [
                {
                    "provider": r.provider,
                    "requested_model": r.requested_model,
                    "actual_model": r.actual_model,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.total_tokens,
                    "request_chars": r.request_chars,
                }
                for r in editor_results
            ]
            # Backward-compatible summary of the final editor call.
            stats["editor_call"] = {
                "provider": editor_result.provider,
                "requested_model": editor_result.requested_model,
                "actual_model": editor_result.actual_model,
            }

        brief = render_brief(editor, candidates, db, settings.max_final_items)
        db.save_brief(run_id, brief)
        out_dir = ROOT / "briefs"
        out_dir.mkdir(exist_ok=True)
        now = utcnow()
        day = now.date().isoformat()
        archive_dir = out_dir / day
        archive_dir.mkdir(exist_ok=True)
        archive_path = archive_dir / f"{now.strftime('%H%M%S')}_{run_id[-8:]}.md"
        daily_path = out_dir / f"{day}.md"
        latest_path = out_dir / "latest.md"
        for path in (archive_path, daily_path, latest_path):
            path.write_text(brief, encoding="utf-8")

        sent = send_if_configured(f"Frontier AI Brief - {day}", brief)
        stats["email_sent"] = sent
        stats["brief_path"] = str(archive_path.relative_to(ROOT))
        stats["brief_daily_path"] = str(daily_path.relative_to(ROOT))
        stats["brief_latest_path"] = str(latest_path.relative_to(ROOT))

        scout_provider = scout_results[0].provider if scout_results else None
        scout_model = scout_results[0].actual_model if scout_results else None
        db.finish_run(
            run_id,
            "success",
            stats,
            scout_provider=scout_provider,
            scout_model=scout_model,
            editor_provider=editor_result.provider if editor_result else None,
            editor_model=editor_result.actual_model if editor_result else None,
        )
        print(brief)
        print("\n--- RUN STATS ---")
        print(json.dumps(stats, indent=2, default=str))
        return 0
    except Exception as exc:
        db.fail_run(run_id, repr(exc))
        logging.exception("Run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
