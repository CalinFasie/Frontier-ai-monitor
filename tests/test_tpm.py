from frontier_monitor.providers import _rate_limit_is_request_too_large


def test_detects_groq_request_too_large_tpm_error():
    text = (
        "Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): "
        "Limit 8000, Requested 31991, please reduce your message size"
    )
    assert _rate_limit_is_request_too_large(text) is True


def test_normal_rate_limit_is_retryable_shape():
    text = "Rate limit reached. Limit 8000, Requested 4200. Please try again later."
    assert _rate_limit_is_request_too_large(text) is False
