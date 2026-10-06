import copy
import json

import pytest

from frontier_monitor.providers import OpenAICompatibleProvider, ProviderError, ProviderPool


def decision(index=0):
    return {
        "candidate_index": index, "decision": "WATCH", "matched_development_id": None,
        "canonical_title": "An observed development", "category": "cyber_risk",
        "status": "announcement", "what_changed": "An organization announced a change.",
        "state_delta": "A new announcement.", "state_delta_kind": "new_development",
        "why_it_matters": "It could change capabilities, but evidence remains preliminary.",
        "materiality": 8, "update_materiality": 8, "evidence_strength": 5,
        "novelty": 8, "confidence": "medium",
    }


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body
        self.text = json.dumps(body)
        self.headers = {}

    def json(self):
        return self.body


def completion(content=None, finish="stop", tokens=1200, reasoning=None):
    return Response(body={
        "choices": [{"finish_reason": finish, "message": {
            "content": content if content is not None else json.dumps({"decisions": [decision()]}),
            "reasoning": reasoning,
        }}],
        "usage": {"prompt_tokens": 3900, "completion_tokens": tokens, "total_tokens": 3900 + tokens},
    })


def mock_posts(monkeypatch, *responses):
    calls = []
    pending = iter(responses)

    def post(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs["json"]))
        return next(pending)

    monkeypatch.setattr("frontier_monitor.providers.requests.post", post)
    return calls


def strict_call(**kwargs):
    return OpenAICompatibleProvider("groq", "https://example.test", "test-key").chat_json(
        "openai/gpt-oss-120b", "Return JSON", "candidate", max_tokens=2048,
        strict_editor=True, **kwargs,
    )


@pytest.mark.parametrize("tokens", [790, 800, 1200, 2040])
def test_complete_response_above_old_budget_and_strict_payload(monkeypatch, tokens):
    text = json.dumps({"decisions": [dict(decision(), what_changed="Evidence sentence. " * 250)]})
    calls = mock_posts(monkeypatch, completion(text, tokens=tokens))
    result = strict_call()
    assert result.data["decisions"][0]["decision"] == "WATCH"
    assert result.completion_tokens == tokens
    assert result.strict_editor is True
    assert len(calls) == 1
    payload = calls[0]
    assert payload["max_completion_tokens"] == 2048
    assert "max_tokens" not in payload
    assert payload["reasoning_effort"] == "low"
    assert payload["temperature"] == 0.1
    assert payload["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("content,finish", [('{"decisions":[', "length"),
    (json.dumps({"decisions": [decision()]}), "length"), ('{"decisions":[', "stop"),
    ("```json\n" + json.dumps({"decisions": [decision()]}) + "\n```", "stop"),
    ("", "stop"), ("   ", "stop")])
def test_incomplete_or_non_json_output_never_retries(monkeypatch, content, finish):
    calls = mock_posts(monkeypatch, completion(content, finish))
    with pytest.raises(ProviderError) as exc:
        strict_call()
    assert exc.value.retryable is False
    assert len(calls) == 1
    assert "completion_tokens=1200" in str(exc.value)


def test_reasoning_only_is_not_editor_content(monkeypatch):
    response = completion(reasoning=json.dumps({"decisions": [decision()]}))
    response.body["choices"][0]["message"]["content"] = None
    calls = mock_posts(monkeypatch, response)
    with pytest.raises(ProviderError, match="empty final content"):
        strict_call()
    assert len(calls) == 1


@pytest.mark.parametrize("status,code,message,param", [
    (400, "json_validate_failed", "Failed to generate JSON", None),
    (422, "json_validate_failed", "response_format is not supported", "response_format"),
    (400, "unsupported_parameter", "response_format is not supported", "response_format"),
    (422, "invalid_request_error", "Invalid schema", "response_format"),
])
def test_strict_http_failures_never_strip_format(monkeypatch, status, code, message, param):
    calls = mock_posts(monkeypatch, Response(status, {"error": {
        "code": code, "message": message, "param": param, "failed_generation": "PRIVATE OUTPUT",
    }}))
    with pytest.raises(ProviderError) as exc:
        strict_call()
    assert exc.value.retryable is False
    assert "PRIVATE OUTPUT" not in str(exc.value)
    assert len(calls) == 1


def test_explicit_unsupported_format_allows_one_legacy_compatibility_retry(monkeypatch):
    calls = mock_posts(monkeypatch,
        Response(422, {"error": {"code": "unsupported_parameter", "param": "response_format",
            "message": "response_format is not supported"}}),
        completion('{"candidates": []}'))
    provider = OpenAICompatibleProvider("openrouter", "https://example.test", "test-key")
    assert provider.chat_json("openrouter/free", "s", "u").data == {"candidates": []}
    assert len(calls) == 2
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    assert "reasoning_effort" not in calls[0]
    assert calls[0]["max_tokens"] == 900


@pytest.mark.parametrize("status,code,message", [
    (400, "json_validate_failed", "Failed to generate JSON"),
    (422, "json_validate_failed", "Failed to generate JSON"),
    (422, "invalid_request_error", "Invalid model"),
    (400, "invalid_request_error", "Invalid response_format schema"),
])
def test_unrelated_legacy_http_errors_do_not_retry(monkeypatch, status, code, message):
    calls = mock_posts(monkeypatch, Response(status, {"error": {"code": code, "message": message}}))
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider("groq", "https://example.test", "test-key").chat_json("scout", "s", "u")
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", [
    lambda d: d.pop("confidence"), lambda d: d.update(materiality="8"),
    lambda d: d.update(decision="PUBLISH"), lambda d: d.update(status="invented"),
    lambda d: d.update(materiality=float("nan")), lambda d: d.update(extra="value"),
    lambda d: d.update(candidate_index=True),
])
def test_contract_invalid_response_fails(monkeypatch, mutation):
    d = decision()
    mutation(d)
    calls = mock_posts(monkeypatch, completion(json.dumps({"decisions": [d]})))
    with pytest.raises(ProviderError):
        strict_call()
    assert len(calls) == 1


@pytest.mark.parametrize("data", [{"decisions": []}, {"decisions": [decision(), decision()]},
    {"decisions": [decision()], "bottom_line": "unused"}])
def test_exactly_one_decision_without_bottom_line(monkeypatch, data):
    mock_posts(monkeypatch, completion(json.dumps(data)))
    with pytest.raises(ProviderError):
        strict_call()


def test_pool_does_not_retry_editor_or_route_to_openrouter(monkeypatch):
    monkeypatch.setenv("ENABLE_GEMINI_FALLBACK", "0")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from frontier_monitor.config import ROOT, load_yaml
    pool = ProviderPool(load_yaml(ROOT / "config/models.yaml"))
    calls = mock_posts(monkeypatch, Response(400, {"error": {"code": "json_validate_failed"}}))
    monkeypatch.setattr("frontier_monitor.providers.time.sleep", lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(ProviderError):
        pool.call("editor", "s", "u", attempts_per_provider=3)
    assert len(calls) == 1


def test_ambiguous_unsupported_error_does_not_strip_response_format(monkeypatch):
    calls = mock_posts(monkeypatch, Response(400, {"error": {
        "code": "unsupported_parameter", "param": "temperature",
        "message": "temperature is not supported with this response_format",
    }}))
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider("openrouter", "https://example.test", "test-key").chat_json("free", "s", "u")
    assert len(calls) == 1


def test_duplicate_json_properties_fail_closed(monkeypatch):
    text = json.dumps({"decisions": [decision()]}).replace('"decision": "WATCH"', '"decision": "REPORT", "decision": "WATCH"')
    mock_posts(monkeypatch, completion(text))
    with pytest.raises(ProviderError, match="Duplicate JSON property"):
        strict_call()


@pytest.mark.parametrize("message", [
    "Rate limit reached. Limit 8000, Used 4500, Requested 6000. Please try again in 10s.",
    "Request too large. Limit 8000, Requested 9000.",
])
def test_editor_429_does_not_add_generation_retries(monkeypatch, message):
    from frontier_monitor.config import ROOT, load_yaml
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_GEMINI_FALLBACK", "0")
    calls = mock_posts(monkeypatch, Response(429, {"error": {"message": message, "code": "rate_limit_exceeded"}}))
    monkeypatch.setattr("frontier_monitor.providers.time.sleep", lambda _: pytest.fail("automatic Editor retry"))
    with pytest.raises(ProviderError):
        ProviderPool(load_yaml(ROOT / "config/models.yaml")).call("editor", "s", "u")
    assert len(calls) == 1


@pytest.mark.parametrize("provider,model", [("groq", "qwen/qwen3.8-27b"), ("gemini", "gemini-3.7-flash"), ("openrouter", "openrouter/free")])
def test_low_effort_and_strict_schema_are_not_sent_to_other_paths(monkeypatch, provider, model):
    calls = mock_posts(monkeypatch, completion())
    OpenAICompatibleProvider(provider, "https://example.test", "test-key").chat_json(model, "s", "u", max_tokens=650)
    assert "reasoning_effort" not in calls[0]
    assert "max_completion_tokens" not in calls[0]
    assert calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("estimate", [7000, 7001])
def test_request_safety_budget_boundary(monkeypatch, estimate):
    monkeypatch.setattr("frontier_monitor.providers._groq_editor_request_tokens", lambda _: estimate)
    calls = mock_posts(monkeypatch, completion())
    if estimate == 7000:
        strict_call()
        assert len(calls) == 1
    else:
        with pytest.raises(ProviderError, match="request too large"):
            strict_call()
        assert not calls
