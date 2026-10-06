"""Transport contract only; editorial thresholds remain in the deterministic gate."""
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


_PROPERTIES = {
    "candidate_index": {"type": "integer"},
    "decision": {"type": "string", "enum": ["REPORT", "WATCH", "IGNORE"]},
    "matched_development_id": {"type": ["string", "null"]},
    "canonical_title": {"type": "string"},
    "category": {"type": "string"},
    "status": {"type": "string", "enum": [
        "rumor", "announcement", "paper", "demo", "independent_confirmation", "deployed",
        "incident_confirmed", "infrastructure_commitment", "enacted", "court_ruling", "regulatory_order",
    ]},
    "what_changed": {"type": "string"},
    "state_delta": {"type": "string"},
    "state_delta_kind": {"type": "string", "enum": [
        "new_development", "status_progression", "evidence_strengthening", "deployment_scale_change",
        "scope_expansion", "policy_or_legal_action", "infrastructure_commitment",
        "contradiction_or_retraction", "none",
    ]},
    "why_it_matters": {"type": "string"},
    "materiality": {"type": "number"},
    "update_materiality": {"type": "number"},
    "evidence_strength": {"type": "number"},
    "novelty": {"type": "number"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
}

EDITOR_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array", "minItems": 1, "maxItems": 1,
            "items": {
                "type": "object", "properties": _PROPERTIES,
                "required": list(_PROPERTIES), "additionalProperties": False,
            },
        },
    },
    "required": ["decisions"],
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(EDITOR_SCHEMA)


def validate_editor_response(data: Any, candidate_index: int | None = None) -> None:
    error = next(_VALIDATOR.iter_errors(data), None)
    if error:
        # Do not include response values or reasoning in failure logs.
        raise ValueError(f"Editor response contract violation ({error.validator})")
    if candidate_index is not None and data["decisions"][0]["candidate_index"] != candidate_index:
        raise ValueError(f"Editor returned the wrong candidate_index; expected {candidate_index}")
