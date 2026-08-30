from frontier_monitor.db import Database
from frontier_monitor.evidence import evidence_profile, publication_gate
from frontier_monitor.evidence_acquisition import acquire_candidate_evidence, collect_crossref_candidate
from frontier_monitor.pipeline import _benchmark_only_update, render_brief
from frontier_monitor.utils import fingerprint, utcnow


def _row(i, publisher, source_type, url="https://example.com/x"):
    return {"id": i, "publisher": publisher, "source_type": source_type, "url": url}


def _item(url, title, publisher, category, source_type="google_news", snippet=None):
    return {
        "fingerprint": fingerprint(url, title),
        "url": url,
        "title": title,
        "publisher": publisher,
        "published_at": utcnow(),
        "category_hint": category,
        "source_type": source_type,
        "snippet": snippet or title,
    }


def test_crossref_abstract_counts_as_primary_research():
    profile = evidence_profile([
        _row(1, "ACM", "crossref_research", "https://doi.org/10.1/x"),
    ])
    assert profile["primary_research"] == 1


def test_crossref_index_needs_independent_secondary_for_paper():
    profile = evidence_profile([
        _row(1, "ACM", "crossref_index", "https://doi.org/10.1/x"),
        _row(2, "Reuters", "google_news", "https://news.google.com/x"),
    ])
    ok, reason = publication_gate(
        {"status": "paper", "materiality": 8, "update_materiality": 8, "evidence_strength": 8},
        profile,
    )
    assert ok
    assert reason == "research_index_plus_independent_secondary"


def test_crossref_candidate_parses_matching_work(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "message": {
                    "items": [{
                        "DOI": "10.1234/levjepa",
                        "title": ["LeVJEPA: Efficient Video Pretraining"],
                        "publisher": "Example Proceedings",
                        "abstract": "<jats:p>LeVJEPA reduces compute for video pretraining while preserving downstream performance.</jats:p>" * 3,
                        "published-online": {"date-parts": [[2026, 8, 20]]},
                    }]
                }
            }
    monkeypatch.setattr("frontier_monitor.evidence_acquisition.requests.get", lambda *a, **k: Resp())
    items = collect_crossref_candidate({"canonical_title": "LeVJEPA method reduces video pretraining costs by 20x", "category": "ai_research_automation"})
    assert items
    assert items[0]["source_type"] == "crossref_research"
    assert items[0]["url"].startswith("https://doi.org/")


def test_announcement_upgrades_to_paper_when_primary_research_found(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path/'x.db'}")
    category = "ai_science_biology"
    original = db.upsert_source(_item(
        "https://news.google.com/a",
        "Anthropic autonomous protein design study",
        "Reuters",
        category,
    ))
    paper = _item(
        "https://doi.org/10.1234/protein",
        "Anthropic autonomous protein design study",
        "Example Journal",
        category,
        "crossref_research",
        "Primary abstract describing autonomous protein design with wet-lab validation." * 3,
    )
    monkeypatch.setattr("frontier_monitor.evidence_acquisition.collect_google_news", lambda *a, **k: [])
    monkeypatch.setattr("frontier_monitor.evidence_acquisition.collect_crossref_candidate", lambda *a, **k: [paper])
    candidate = {
        "canonical_title": "Anthropic autonomous protein design study",
        "category": category,
        "source_ids": [original],
        "what_happened": "A study reports autonomous protein design with lab validation.",
        "evidence_stage": "announcement",
    }
    stats = acquire_candidate_evidence(db, candidate, {}, 72)
    assert stats["profile_after"]["primary_research"] >= 1
    assert stats["stage_before"] == "announcement"
    assert stats["stage_after"] == "paper"
    assert candidate["evidence_stage"] == "paper"


def test_benchmark_only_update_is_suppressed():
    candidate = {
        "canonical_title": "GCSA Agent achieves 91.3% score on CyberGym benchmark",
        "what_happened": "The agent scored 91.3% on the CyberGym benchmark.",
        "evidence_stage": "demo",
    }
    decision = {
        "status": "demo",
        "what_changed": "The model reached a new benchmark score.",
        "state_delta": "New benchmark result.",
        "why_it_matters": "Could indicate improved cyber capability.",
    }
    assert _benchmark_only_update(candidate, decision)


def test_benchmark_with_real_world_deployment_not_suppressed():
    candidate = {
        "canonical_title": "Agent benchmark result followed by production deployment",
        "what_happened": "The system was deployed in production after the benchmark.",
        "evidence_stage": "deployed",
    }
    decision = {"status": "deployed", "what_changed": "Production deployment began."}
    assert not _benchmark_only_update(candidate, decision)


def test_brief_header_uses_ascii_dash(tmp_path):
    db = Database(f"sqlite:///{tmp_path/'x.db'}")
    text = render_brief({"decisions": [], "bottom_line": "Nothing material."}, [], db, 8)
    assert "Frontier AI Brief -" in text
    assert "Ã¢â‚¬â€" not in text
