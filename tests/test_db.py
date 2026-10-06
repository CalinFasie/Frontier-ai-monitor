import unittest

from frontier_monitor.db import Database
from frontier_monitor.utils import fingerprint, utcnow


class DatabaseTests(unittest.TestCase):
    def test_source_upsert_is_idempotent(self):
        db = Database("sqlite:///:memory:")
        item = {
            "fingerprint": fingerprint("https://example.com/a", "A"),
            "url": "https://example.com/a",
            "title": "A",
            "publisher": "Example",
            "published_at": utcnow(),
            "category_hint": "autonomous_agents",
            "source_type": "test",
            "snippet": "hello",
        }
        first = db.upsert_source(item)
        second = db.upsert_source(item)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()


def test_coverage_diagnostics_distinguish_uninitialized_state(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'coverage.sqlite'}")

    assert db.get_coverage_state() is None
    assert db.get_coverage_diagnostics() == {
        "scope": "global",
        "schema_ready": True,
        "initialized": False,
        "covered_through": None,
        "updated_at": None,
        "last_run_id": None,
    }

    with db.engine.connect() as cx:
        assert cx.exec_driver_sql("SELECT COUNT(*) FROM coverage_state").scalar_one() == 0
    db.engine.dispose()


def test_coverage_diagnostics_reads_existing_state_as_utc_iso8601(tmp_path):
    from sqlalchemy import text

    db = Database(f"sqlite:///{tmp_path / 'coverage.sqlite'}")
    db.start_run("run-coverage")
    with db.engine.begin() as cx:
        cx.execute(
            text(
                "INSERT INTO coverage_state "
                "(scope, covered_through, updated_at, last_run_id) "
                "VALUES (:scope, :covered_through, :updated_at, :last_run_id)"
            ),
            {
                "scope": "global",
                "covered_through": "2026-10-06T03:00:00+03:00",
                "updated_at": "2026-10-06 01:02:03",
                "last_run_id": "run-coverage",
            },
        )

    assert db.get_coverage_state() == {
        "scope": "global",
        "covered_through": "2026-10-06T00:00:00Z",
        "updated_at": "2026-10-06T01:02:03Z",
        "last_run_id": "run-coverage",
    }
    assert db.get_coverage_diagnostics() == {
        "scope": "global",
        "schema_ready": True,
        "initialized": True,
        "covered_through": "2026-10-06T00:00:00Z",
        "updated_at": "2026-10-06T01:02:03Z",
        "last_run_id": "run-coverage",
    }
    db.engine.dispose()
