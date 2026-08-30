from datetime import timedelta

from frontier_monitor.db import Database
from frontier_monitor.evidence_acquisition import acquire_candidate_evidence
from frontier_monitor.utils import fingerprint, utcnow


def _item(url, title, publisher, category, source_type="google_news"):
    return {
        "fingerprint": fingerprint(url, title),
        "url": url,
        "title": title,
        "publisher": publisher,
        "published_at": utcnow(),
        "category_hint": category,
        "source_type": source_type,
        "snippet": title,
    }


def test_local_corpus_adds_correlated_source(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path/'x.db'}")
    category = "cyber_risk"
    a = db.upsert_source(_item("https://example.com/a", "CLOP exploits Windchill vulnerability in campaign", "Reuters", category))
    b = db.upsert_source(_item("https://example.com/b", "Windchill flaw exploited by CLOP ransomware", "BleepingComputer", category))

    # Prevent network fallback; local corpus should already improve the packet.
    monkeypatch.setattr("frontier_monitor.evidence_acquisition._need_targeted_search", lambda c, p: False)
    candidate = {
        "canonical_title": "CLOP ransomware exploits Windchill vulnerability",
        "category": category,
        "source_ids": [a],
        "what_happened": "CLOP exploited a Windchill vulnerability.",
        "evidence_stage": "incident_confirmed",
    }
    stats = acquire_candidate_evidence(db, candidate, {}, 72)
    assert b in candidate["source_ids"]
    assert stats["final_sources"] >= 2
