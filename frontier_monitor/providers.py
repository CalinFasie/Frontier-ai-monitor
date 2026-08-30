from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

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


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


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
        request_chars = len(system) + len(user)
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code >= 400 and r.status_code in (400, 422):
            # Some OpenAI-compatible free providers do not support
            # response_format=json_object. Retry once without that optional
            # feature, but do not do this for 429/TPM failures.
            payload.pop("response_format", None)
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            text = r.text[:2000]
            if r.status_code == 429:
                too_large = _rate_limit_is_request_too_large(text)
                retry_after = None
                try:
                    retry_after = float(r.headers.get("Retry-After", "") or 0) or None
                except Exception:
                    retry_after = None
                raise ProviderError(
                    f"{self.name} HTTP 429: {text}",
                    retryable=not too_large,
                    retry_after=retry_after or (65.0 if not too_large else None),
                )
            if 500 <= r.status_code < 600:
                raise ProviderError(f"{self.name} HTTP {r.status_code}: {text}", retryable=True)
            raise ProviderError(f"{self.name} HTTP {r.status_code}: {text}", retryable=False)

        body = r.json()
        try:
            text = body["choices"][0]["message"]["content"]
        except Exception as exc:
            raise ProviderError(f"Unexpected {self.name} response: {str(body)[:1500]}", retryable=False) from exc

        usage = body.get("usage") or {}
        return ProviderResult(
            data=extract_json(text),
            provider=self.name,
            requested_model=model,
            actual_model=body.get("model") or model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            request_chars=request_chars,
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
