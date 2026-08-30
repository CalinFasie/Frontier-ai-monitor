from frontier_monitor.evidence import evidence_profile, publication_gate


def row(i, publisher, source_type="google_news", url="https://news.google.com/x"):
    return {"id": i, "publisher": publisher, "source_type": source_type, "url": url}


def test_company_claim_plus_reuters_can_report_deployment():
    profile = evidence_profile([
        row(1, "OpenAI"),
        row(2, "Reuters"),
    ])
    ok, _ = publication_gate({"materiality": 8, "evidence_strength": 8, "status": "deployed"}, profile)
    assert ok


def test_company_claim_alone_cannot_report():
    profile = evidence_profile([row(1, "Anthropic")])
    ok, reason = publication_gate({"materiality": 9, "evidence_strength": 9, "status": "deployed"}, profile)
    assert not ok
    assert "corroboration" in reason


def test_official_law_source_can_report_enacted():
    profile = evidence_profile([row(1, "European Commission")])
    ok, _ = publication_gate({"materiality": 8, "evidence_strength": 9, "status": "enacted"}, profile)
    assert ok


def test_material_paper_alone_can_report_with_replication_caveat():
    profile = evidence_profile([row(1, "arXiv: A. Author", source_type="arxiv", url="https://arxiv.org/abs/1234")])
    ok, reason = publication_gate({"materiality": 9, "evidence_strength": 8, "status": "paper"}, profile)
    assert ok
    assert reason == "primary_research_material_claim_not_replication"


def test_two_reputable_independent_secondaries_can_confirm_incident():
    profile = evidence_profile([row(1, "Reuters"), row(2, "BleepingComputer")])
    ok, _ = publication_gate({"materiality": 8, "evidence_strength": 8, "status": "incident_confirmed"}, profile)
    assert ok


def test_evidence_below_threshold_never_reports():
    profile = evidence_profile([row(1, "European Commission")])
    ok, reason = publication_gate({"materiality": 9, "evidence_strength": 6, "status": "enacted"}, profile)
    assert not ok
    assert reason == "evidence_strength_below_7"


def test_material_primary_paper_can_pass_without_secondary():
    profile = {
        "primary_official": 0,
        "primary_research": 1,
        "primary_claim": 0,
        "independent_primary_orgs": 1,
        "independent_reputable_secondary_orgs": 0,
    }
    decision = {"status": "paper", "materiality": 8, "evidence_strength": 7}
    passed, reason = publication_gate(decision, profile)
    assert passed is True
    assert reason == "primary_research_material_claim_not_replication"


def test_low_materiality_primary_paper_stays_watch():
    profile = {
        "primary_official": 0,
        "primary_research": 1,
        "primary_claim": 0,
        "independent_primary_orgs": 1,
        "independent_reputable_secondary_orgs": 0,
    }
    decision = {"status": "paper", "materiality": 7, "evidence_strength": 7}
    passed, reason = publication_gate(decision, profile)
    assert passed is False
    assert reason == "paper_missing_primary_research_or_materiality"


def test_official_infrastructure_commitment_can_pass():
    profile = {
        "primary_official": 1,
        "primary_research": 0,
        "primary_claim": 0,
        "independent_primary_orgs": 1,
        "independent_reputable_secondary_orgs": 0,
    }
    decision = {"status": "infrastructure_commitment", "materiality": 8, "evidence_strength": 8}
    passed, reason = publication_gate(decision, profile)
    assert passed is True
    assert reason == "official_infrastructure_commitment"


def test_update_materiality_controls_publication_gate():
    profile = evidence_profile([row(1, "European Commission")])
    ok, reason = publication_gate(
        {"materiality": 9, "update_materiality": 2, "evidence_strength": 9, "status": "enacted"},
        profile,
    )
    assert not ok
    assert reason == "update_materiality_below_7"


def test_official_regulatory_order_can_report():
    profile = evidence_profile([row(1, "Federal Trade Commission")])
    ok, _ = publication_gate(
        {"materiality": 8, "update_materiality": 8, "evidence_strength": 8, "status": "regulatory_order"},
        profile,
    )
    assert ok
