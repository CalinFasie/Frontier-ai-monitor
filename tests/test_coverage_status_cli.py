from __future__ import annotations

import json
import sys

import pytest
from sqlalchemy import text

import frontier_monitor.main as monitor_main
from frontier_monitor.db import Database as RealDatabase


def test_coverage_status_cli_is_read_only_and_skips_research(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path / 'monitor.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(monitor_main, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["frontier_monitor.main", "--coverage-status"])

    databases = []

    def capture_database(url):
        db = RealDatabase(url)
        databases.append(db)
        return db

    def research_must_not_run(*_args, **_kwargs):
        pytest.fail("coverage-status must not enter the monitor research path")

    monkeypatch.setattr(monitor_main, "Database", capture_database)
    monkeypatch.setattr(monitor_main, "load_yaml", research_must_not_run)
    monkeypatch.setattr(monitor_main, "collect_and_store", research_must_not_run)
    monkeypatch.setattr(monitor_main, "ProviderPool", research_must_not_run)
    monkeypatch.setattr(monitor_main, "run_scout", research_must_not_run)
    monkeypatch.setattr(monitor_main, "acquire_evidence_for_candidates", research_must_not_run)
    monkeypatch.setattr(monitor_main, "run_editor", research_must_not_run)
    monkeypatch.setattr(monitor_main, "persist_editor_results", research_must_not_run)
    monkeypatch.setattr(monitor_main, "render_brief", research_must_not_run)
    monkeypatch.setattr(monitor_main, "send_if_configured", research_must_not_run)

    assert monitor_main.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "scope": "global",
        "schema_ready": True,
        "initialized": False,
        "covered_through": None,
        "updated_at": None,
        "last_run_id": None,
    }
    assert len(databases) == 1
    with databases[0].engine.connect() as cx:
        assert cx.execute(text("SELECT COUNT(*) FROM runs")).scalar_one() == 0
        assert cx.execute(text("SELECT COUNT(*) FROM sources")).scalar_one() == 0
    databases[0].engine.dispose()
