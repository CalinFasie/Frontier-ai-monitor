import copy
import json
import math

import pytest
from jsonschema import Draft202012Validator

from frontier_monitor import pipeline
from frontier_monitor.config import ROOT, load_yaml
from frontier_monitor.editor_schema import EDITOR_SCHEMA
from frontier_monitor.providers import (
    ProviderError, ProviderPool, _groq_editor_request_tokens,
)
from frontier_monitor.utils import read_text
from test_editor_provider import Response, completion, decision, mock_posts


class PacketDB:
    def known_developments(self, **kwargs):
        return []


def test_strict_schema_is_closed_required_and_nullable():
    Draft202012Validator.check_schema(EDITOR_SCHEMA)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(EDITOR_SCHEMA)
    props = EDITOR_SCHEMA["properties"]["decisions"]["items"]["properties"]
    assert props["matched_development_id"]["type"] == ["string", "null"]
    assert "bottom_line" not in EDITOR_SCHEMA["properties"]


def pool(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_GEMINI_FALLBACK", "0")
    return ProviderPool(load_yaml(ROOT / "config/models.yaml"))


def packet(monkeypatch):
    monkeypatch.setattr(pipeline, "_candidate_packet", lambda db, c: {
        "canonical_title": "New development", "evidence_profile": {},
    })


def test_editor_preserves_pacing_gate_and_deterministic_bottom_line(monkeypatch):
    packet(monkeypatch)
    monkeypatch.delenv("EDITOR_CALL_DELAY_SECONDS", raising=False)
    sleeps = []
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    first = dict(decision(0), decision="REPORT", evidence_strength=8)
    second = decision(1)
    calls = mock_posts(monkeypatch,
        completion(json.dumps({"decisions": [first]})),
        completion(json.dumps({"decisions": [second]})))
    editor, results, _ = pipeline.run_editor(pool(monkeypatch), PacketDB(), [{}, {}])
    assert len(calls) == 2
    assert sleeps == [65.0]
    # No corroboration: the unchanged evidence gate must still downgrade REPORT.
    assert editor["decisions"][0]["decision"] == "WATCH"
    assert editor["decisions"][0]["gate_downgraded_from_report"] is True
    assert "deterministic publication-evidence gate" in editor["bottom_line"]
    assert all(r.strict_editor for r in results)


def test_wrong_candidate_fails_before_next_editor_request(monkeypatch):
    packet(monkeypatch)
    calls = mock_posts(monkeypatch, completion(json.dumps({"decisions": [decision(1)]})))
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: pytest.fail("unexpected pacing after failure"))
    with pytest.raises(ValueError, match="wrong candidate_index"):
        pipeline.run_editor(pool(monkeypatch), PacketDB(), [{}, {}])
    assert len(calls) == 1


def maximum_packet(monkeypatch):
    """Saturate the existing source/title/excerpt/prior-state caps, without I/O."""
    prose = "Autonomous research systems change the capabilities of laboratory workflows. "
    rows = [{
        "id": i, "title": prose * 8, "publisher": "Research organization " * 8,
        "published_at": None, "url": f"https://example.org/research/{i}",
        "source_type": "arxiv", "snippet": prose * 12, "fetched_text": prose * 20,
    } for i in range(10)]

    class Sources:
        def get_sources(self, ids):
            return rows

    monkeypatch.setattr(pipeline, "enrich_sources", lambda db, ids: rows[:3])
    candidate = {
        "canonical_title": prose * 10, "category": "ai_research_automation",
        "source_ids": list(range(10)), "what_happened": prose * 10,
        "why_potentially_material": prose * 10, "materiality": 8, "novelty": 8,
        "evidence_stage": "paper", "evidence_acquisition": {
            "stage_before": "announcement", "stage_after": "paper",
            "targeted_attempted": ["official", "entity", "news", "arxiv", "crossref"],
            "targeted_added": 6,
        },
    }
    result = pipeline._candidate_packet(Sources(), candidate)
    result["candidate_index"] = 0
    result["prior_state_matches"] = pipeline._known_packet([{
        "id": str(i), "canonical_title": prose * 10, "category": candidate["category"],
        "current_state": prose * 10, "status": "paper", "materiality": 8, "evidence_strength": 8,
    } for i in range(3)])
    return result


def use_offline_test_tokenizer(monkeypatch):
    """Keep request-budget unit tests hermetic without downloading BPE data."""
    class OfflineEncoding:
        def encode(self, value, disallowed_special=()):
            return [0] * math.ceil(len(value) / 6)

    monkeypatch.setattr(
        "frontier_monitor.providers.tiktoken.get_encoding",
        lambda _name: OfflineEncoding(),
    )


def test_maximum_representative_packet_includes_schema_and_completion_accounting(monkeypatch):
    use_offline_test_tokenizer(monkeypatch)
    data = maximum_packet(monkeypatch)
    calls = mock_posts(monkeypatch, completion())
    user = "EVALUATE EXACTLY THIS ONE CANDIDATE. Preserve candidate_index in your JSON response.\n" + json.dumps(data, ensure_ascii=False)
    pool(monkeypatch).call("editor", read_text(ROOT / "prompts/editor.txt"), user)
    payload = calls[0]
    accounted = _groq_editor_request_tokens(payload)
    without_schema = copy.deepcopy(payload)
    without_schema["response_format"] = {"type": "json_object"}
    assert accounted > _groq_editor_request_tokens(without_schema)
    assert 4000 < accounted <= 7000
    assert len(data["sources"]) == 5
    assert len(data["source_assessments"]) == 6
    assert len(data["prior_state_matches"]) == 3


def test_oversized_packet_fails_before_any_generation(monkeypatch):
    use_offline_test_tokenizer(monkeypatch)
    data = maximum_packet(monkeypatch)
    # URLs are not clipped by the current packet builder; account for them too.
    data["sources"][0]["url"] = "https://example.org/" + "a1b2c3/" * 6000
    monkeypatch.setattr("frontier_monitor.providers.requests.post", lambda *a, **k: pytest.fail("oversized request sent"))
    with pytest.raises(ProviderError, match="request too large") as exc:
        pool(monkeypatch).call("editor", read_text(ROOT / "prompts/editor.txt"), json.dumps(data))
    assert exc.value.retryable is False


def test_scout_payload_and_reasoning_compatibility_remain_unchanged(monkeypatch):
    response = completion()
    response.body["choices"][0]["message"] = {"content": None, "reasoning": '{"candidates": []}'}
    calls = mock_posts(monkeypatch, response)
    assert pool(monkeypatch).call("scout", "s", "u").data == {"candidates": []}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 650
    assert calls[0]["temperature"] == 0.1
    assert "reasoning_effort" not in calls[0]


@pytest.mark.parametrize("failure_target", [
    "frontier_monitor.providers.tiktoken.get_encoding",
    "frontier_monitor.providers._groq_editor_request_tokens",
])
def test_accounting_failure_warns_and_allows_one_request(monkeypatch, caplog, failure_target):
    def unavailable(_):
        raise OSError("PRIVATE-SECRET tokenizer failure details")

    monkeypatch.setattr(failure_target, unavailable)
    calls = mock_posts(monkeypatch, completion())
    result = pool(monkeypatch).call("editor", "s", "u")
    assert result.strict_editor is True
    assert len(calls) == 1
    assert calls[0]["max_completion_tokens"] == 2048
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert any(r.levelname == "WARNING" and "accounting unavailable" in r.message for r in caplog.records)
    assert "PRIVATE-SECRET" not in caplog.text


@pytest.mark.parametrize("status,error", [
    (400, {"code": "json_validate_failed"}),
    (429, {"code": "rate_limit_exceeded", "message": "Rate limit reached. Limit 8000, Requested 6000."}),
    (429, {"code": "rate_limit_exceeded", "message": "Request too large. Limit 8000, Requested 9000."}),
])
def test_provider_rejection_after_accounting_failure_still_fails_closed(monkeypatch, status, error):
    def unavailable(_):
        raise OSError("tokenizer unavailable")

    monkeypatch.setattr("frontier_monitor.providers.tiktoken.get_encoding", unavailable)
    calls = mock_posts(monkeypatch, Response(status, {"error": error}))
    monkeypatch.setattr("frontier_monitor.providers.time.sleep", lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(ProviderError):
        pool(monkeypatch).call("editor", "s", "u")
    assert len(calls) == 1
