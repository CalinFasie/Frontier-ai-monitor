from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests
import tiktoken

from .editor_schema import EDITOR_SCHEMA, validate_editor_response
from .utils import extract_json

log = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    data: dict[str, Any]
    provider: str
    requested_model: str
    actual_model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    request_chars: int = 0
    strict_editor: bool = False


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _unsupported_response_format(response: requests.Response) -> bool:
    """Only explicit unsupported-format errors justify a legacy compatibility call.

    A validation failure can already have consumed a full generation. Never
    classify it as an unsupported feature, even if its message mentions format.
    """
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "").lower()
        message = str(error.get("message") or "").lower()
        param = str(error.get("param") or "").lower()
        if "validat" in code or error.get("failed_generation") is not None or any(
            phrase in message for phrase in ("failed to generate", "validation failed", "invalid schema")
        ):
            return False
        mentions_format = param == "response_format" if param else bool(re.search(
            r"response_format.{0,40}(?:not supported|unsupported)|"
            r"(?:unsupported parameter|unsupported value|not supported).{0,40}response_format",
            message,
        ))
        explicitly_unsupported = code in {"unsupported_parameter", "unsupported_value"} or any(
            phrase in message for phrase in ("not supported", "unsupported parameter", "unsupported value")
        )
        return mentions_format and explicitly_unsupported
    except (ValueError, AttributeError):
        return False


def _groq_editor_request_tokens(payload: dict[str, Any]) -> int:
    """Conservative local estimate, NOT Groq's exact billing/template accounting.

    Count the complete serialized request (including schema/JSON overhead) with
    GPT-OSS's tokenizer, add 256 tokens for server-side formatting, and reserve
    the entire completion ceiling. Reject above 7k to leave a further 1k margin
    below the observed 8k TPM limit. Other quota traffic may still cause 429s.
    """
    encoding = tiktoken.get_encoding("o200k_harmony")
    text = json.dumps(payload, ensure_ascii=True)
    return len(encoding.encode(text, disallowed_special=())) + 256 + payload["max_completion_tokens"]


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _rate_limit_is_request_too_large(text: str) -> bool:
    lower = text.lower()
    if "request too large" in lower:
        return True
    m = re.search(r"limit\s+(\d+).*?requested\s+(\d+)", lower, re.S)
    if m:
        try:
            return int(m.group(2)) > int(m.group(1))
        except Exception:
            pass
    return False


def _retry_after_from_text(text: str) -> float | None:
    """Parse provider messages such as `Please try again in 7.29s` or `435ms`."""
    lower = text.lower()
    m = re.search(r"try again in\s+([0-9.]+)\s*(ms|s|sec|secs|seconds?)", lower)
    if not m:
        return None
    try:
        value = float(m.group(1))
        unit = m.group(2)
        return value / 1000.0 if unit == "ms" else value
    except Exception:
        return None


def _message_text(message: dict[str, Any]) -> str:
    """Extract usable text from heterogeneous OpenAI-compatible responses.

    Some free OpenRouter models emit `content=null` while putting text in a
    reasoning field, or return content as a list of text parts. Treat a truly
    empty message as a provider failure instead of crashing on `.strip()`.
    """
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    parts.append(value)
        joined = "\n".join(x for x in parts if x.strip())
        if joined.strip():
            return joined
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ProviderError("Provider returned an empty assistant message (content=null)", retryable=True)


class OpenAICompatibleProvider:
    def __init__(self, name: str, base_url: str, api_key: str, headers: dict[str, str] | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = headers or {}

    def chat_json(
        self,
        model: str,
        system: str,
        user: str,
        timeout: int = 120,
        max_tokens: int = 900,
        strict_editor: bool = False,
    ) -> ProviderResult:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.headers,
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            # Bound the completion budget explicitly. Free-tier TPM systems can
            # count reserved completion tokens toward the request budget.
            "max_tokens": int(max_tokens),
            "response_format": {"type": "json_object"},
        }
        if strict_editor:
            if self.name != "groq" or model != "openai/gpt-oss-120b":
                raise ProviderError("Strict Editor requires Groq openai/gpt-oss-120b", retryable=False)
            if int(max_tokens) != 2048:
                raise ProviderError("Groq Editor completion budget must be 2048", retryable=False)
            payload.pop("max_tokens")
            payload["max_completion_tokens"] = int(max_tokens)
            payload["reasoning_effort"] = "low"
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "editor_decision", "strict": True, "schema": EDITOR_SCHEMA},
            }
            try:
                estimated_tokens = _groq_editor_request_tokens(payload)
            except Exception as exc:
                raise ProviderError("Groq Editor request token accounting unavailable", retryable=False) from exc
            if estimated_tokens > 7000:
                raise ProviderError(
                    f"Groq Editor request too large: estimated reserved tokens {estimated_tokens} exceed "
                    "7000 safety budget (8000 TPM; completion budget 2048); reduce the input packet",
                    retryable=False,
                )
            log.info("Groq Editor estimated reserved request tokens=%s (safety budget=7000)", estimated_tokens)
        request_chars = len(system) + len(user)
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if not strict_editor and r.status_code in (400, 422) and _unsupported_response_format(r):
            # Only a pre-generation unsupported feature warrants this legacy
            # compatibility path. Validation/generation errors never do.
            payload.pop("response_format", None)
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            if strict_editor and r.status_code in (400, 422):
                try:
                    code = str((r.json().get("error") or {}).get("code") or "invalid_request")
                except (ValueError, AttributeError):
                    code = "invalid_request"
                code = re.sub(r"[^A-Za-z0-9_-]", "", code)[:80]
                raise ProviderError(f"{self.name} strict Editor HTTP {r.status_code}: {code}", retryable=False)
            text = r.text[:2000]
            if r.status_code == 429:
                too_large = _rate_limit_is_request_too_large(text)
                retry_after = None
                try:
                    retry_after = float(r.headers.get("Retry-After", "") or 0) or None
                except Exception:
                    retry_after = None
                retry_after = retry_after or _retry_after_from_text(text)
                if retry_after is not None and not too_large:
                    # Provider clocks/buckets are not perfectly aligned with our
                    # runner, so wait a little beyond the advertised reset.
                    retry_after += 1.5
                raise ProviderError(
                    f"{self.name} HTTP 429: {text}",
                    retryable=not too_large,
                    retry_after=retry_after or (65.0 if not too_large else None),
                )
            if 500 <= r.status_code < 600:
                raise ProviderError(f"{self.name} HTTP {r.status_code}: {text}", retryable=True)
            raise ProviderError(f"{self.name} HTTP {r.status_code}: {text}", retryable=False)

        if strict_editor:
            return self._strict_editor_result(r, model, request_chars)

        body = r.json()
        try:
            message = body["choices"][0]["message"]
            text = _message_text(message)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Unexpected {self.name} response: {str(body)[:1500]}", retryable=False) from exc

        try:
            parsed = extract_json(text)
        except Exception as exc:
            raise ProviderError(
                f"{self.name} returned non-JSON/empty model output: {text[:800]!r}",
                retryable=True,
            ) from exc

        usage = body.get("usage") or {}
        return ProviderResult(
            data=parsed,
            provider=self.name,
            requested_model=model,
            actual_model=body.get("model") or model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            request_chars=request_chars,
        )

    def _strict_editor_result(self, response: requests.Response, model: str, request_chars: int) -> ProviderResult:
        diagnostics = ""
        try:
            body = response.json()
            usage = body.get("usage") or {}
            choice = body["choices"][0]
            finish = choice.get("finish_reason")
            diagnostics = (
                f"finish_reason={finish!r}; prompt_tokens={usage.get('prompt_tokens')}; "
                f"completion_tokens={usage.get('completion_tokens')}; total_tokens={usage.get('total_tokens')}"
            )
            if finish != "stop":
                raise ValueError("incomplete completion")
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty final content")
            parsed = json.loads(content, parse_constant=_reject_json_constant, object_pairs_hook=_unique_json_object)
            validate_editor_response(parsed)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "malformed response"
            raise ProviderError(
                f"Groq strict Editor failed: {reason}; {diagnostics}", retryable=False,
            ) from exc
        return ProviderResult(
            data=parsed, provider=self.name, requested_model=model, actual_model=body.get("model") or model,
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"), request_chars=request_chars, strict_editor=True,
        )


class ProviderPool:
    def __init__(self, model_config: dict[str, Any]):
        self.config = model_config
        self.providers: list[tuple[str, OpenAICompatibleProvider, dict[str, Any]]] = []
        for name in model_config.get("provider_order", []):
            cfg = model_config.get("providers", {}).get(name, {})
            enabled_env = cfg.get("enabled_env")
            if enabled_env and os.getenv(enabled_env, "0").lower() not in ("1", "true", "yes"):
                continue
            key = os.getenv(cfg.get("api_key_env", ""), "").strip()
            if not key:
                continue
            extra_headers = {}
            if name == "openrouter":
                extra_headers = {"X-Title": "Frontier AI Monitor"}
            self.providers.append(
                (name, OpenAICompatibleProvider(name, cfg["base_url"], key, extra_headers), cfg)
            )
        if not self.providers:
            raise ProviderError(
                "No LLM provider configured. Set GROQ_API_KEY (recommended) or another configured free provider key.",
                retryable=False,
            )

    @staticmethod
    def _role_allowed(cfg: dict[str, Any], role: str) -> bool:
        roles = cfg.get("roles")
        if not roles:
            return True
        return role in {str(x).lower() for x in roles}

    def call(self, role: str, system: str, user: str, attempts_per_provider: int | None = None) -> ProviderResult:
        role = role.lower()
        errors: list[str] = []
        eligible = [(n, p, c) for n, p, c in self.providers if self._role_allowed(c, role)]
        if not eligible:
            raise ProviderError(f"No configured provider is allowed for role={role!r}.", retryable=False)

        if attempts_per_provider is None:
            attempts_per_provider = 2 if role == "scout" else 1
        if role == "editor":
            attempts_per_provider = 1

        for provider_index, (name, provider, cfg) in enumerate(eligible):
            model = cfg[f"{role}_model"]
            max_tokens = int(cfg.get(f"{role}_max_tokens", 900))
            for attempt in range(attempts_per_provider):
                try:
                    return provider.chat_json(
                        model=model,
                        system=system,
                        user=user,
                        max_tokens=max_tokens,
                        strict_editor=(name == "groq" and role == "editor"),
                    )
                except ProviderError as exc:
                    errors.append(f"{name}/{model}: {exc}")
                    if not exc.retryable:
                        # Retrying an identical 32k-token request against an 8k
                        # TPM cap can never succeed. Move on/fail immediately.
                        break
                    if attempt + 1 < attempts_per_provider:
                        delay = exc.retry_after if exc.retry_after is not None else float(2 ** attempt)
                        log.warning(
                            "%s provider %s retryable failure; retrying in %.1fs: %s",
                            role,
                            name,
                            delay,
                            exc,
                        )
                        time.sleep(delay)
                except Exception as exc:
                    errors.append(f"{name}/{model}: {exc}")
                    if attempt + 1 < attempts_per_provider:
                        time.sleep(float(2 ** attempt))
            if provider_index + 1 < len(eligible):
                next_name = eligible[provider_index + 1][0]
                log.warning(
                    "%s provider %s failed; falling back to %s. Last error: %s",
                    role,
                    name,
                    next_name,
                    errors[-1],
                )

        # Fail closed for the editor rather than silently changing editorial
        # standards via an arbitrary free-router model.
        raise ProviderError(
            f"All allowed providers failed for role={role}:\n" + "\n".join(errors[-8:]),
            retryable=False,
        )
