import pytest

from frontier_monitor.providers import ProviderError, _message_text, _retry_after_from_text
from frontier_monitor.pipeline import _scout_input


def test_retry_after_seconds_and_ms():
    assert _retry_after_from_text('Please try again in 7.29s.') == pytest.approx(7.29)
    assert _retry_after_from_text('Please try again in 435ms.') == pytest.approx(0.435)


def test_message_text_handles_reasoning_when_content_null():
    msg = {'content': None, 'reasoning': '{"candidates": []}'}
    assert _message_text(msg) == '{"candidates": []}'


def test_message_text_empty_is_retryable_provider_error():
    with pytest.raises(ProviderError) as exc:
        _message_text({'content': None})
    assert exc.value.retryable is True


def test_scout_input_caps_rows_and_snippets():
    rows = [
        {'id': i, 'published_at': None, 'publisher': 'x', 'title': f'title {i}', 'snippet': 'a' * 1000}
        for i in range(20)
    ]
    text = _scout_input('x', 'X', rows)
    assert text.count('SOURCE_ID=') == 12
    assert 'a' * 421 not in text
