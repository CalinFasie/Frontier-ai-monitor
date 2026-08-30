from frontier_monitor.db import Database
from frontier_monitor.pipeline import _known_matches_for_candidate


def _decision(title, decision="REPORT", matched=None, delta="new_development", materiality=8, evidence=9):
    return {
        "canonical_title": title,
        "category": "autonomous_agents",
        "decision": decision,
        "matched_development_id": matched,
        "status": "deployed",
        "what_changed": "Strong current state",
        "state_delta": "Meaningful update",
        "state_delta_kind": delta,
        "materiality": materiality,
        "update_materiality": materiality,
        "evidence_strength": evidence,
        "novelty": 8,
        "confidence": "high",
    }


def test_known_matching_is_candidate_specific_and_semantic_enough():
    candidate = {
        "canonical_title": "Long-horizon coding agent deployed in production",
        "what_happened": "A coding agent now operates autonomously for long tasks.",
        "category": "autonomous_agents",
    }
    known = [
        {
            "id": "dev-agent",
            "canonical_title": "Production deployment of long-horizon coding agents",
            "category": "autonomous_agents",
            "status": "deployed",
            "current_state": "Long-running coding agents deployed for production engineering tasks.",
            "last_seen_at": None,
            "reported_at": None,
            "materiality": 8,
            "evidence_strength": 8,
        },
        {
            "id": "dev-energy",
            "canonical_title": "New gas turbines for data centers",
            "category": "energy_infrastructure",
            "status": "infrastructure_commitment",
            "current_state": "Power project announced.",
            "last_seen_at": None,
            "reported_at": None,
            "materiality": 8,
            "evidence_strength": 8,
        },
    ]
    matches = _known_matches_for_candidate(candidate, known)
    assert matches
    assert matches[0]["id"] == "dev-agent"


def test_meaningful_watch_does_not_downgrade_stronger_existing_state():
    db = Database("sqlite+pysqlite:///:memory:")
    db.start_run("r1")
    first = _decision("Agent deployment")
    dev_id = db.persist_decision("r1", first, [])

    db.start_run("r2")
    weak_update = _decision(
        "Agent deployment",
        decision="WATCH",
        matched=dev_id,
        delta="evidence_strengthening",
        materiality=7,
        evidence=6,
    )
    weak_update["what_changed"] = "A weaker secondary reiteration appeared."
    db.persist_decision("r2", weak_update, [])

    state = db.known_developments(days=365, limit=10)[0]
    assert state["materiality"] == 8
    assert state["evidence_strength"] == 9
    assert state["current_state"] == "Strong current state"


def test_contradiction_can_reduce_evidence_strength():
    db = Database("sqlite+pysqlite:///:memory:")
    db.start_run("r1")
    first = _decision("Agent deployment")
    dev_id = db.persist_decision("r1", first, [])

    db.start_run("r2")
    correction = _decision(
        "Agent deployment correction",
        decision="WATCH",
        matched=dev_id,
        delta="contradiction_or_retraction",
        materiality=8,
        evidence=4,
    )
    correction["what_changed"] = "The original deployment claim was partially retracted."
    db.persist_decision("r2", correction, [])

    state = db.known_developments(days=365, limit=10)[0]
    assert state["evidence_strength"] == 4
    assert "retracted" in state["current_state"]
