from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests

from .utils import extract_json


@dataclass
class ProviderResult:
    data: dict[str, Any]
    provider: str
    requested_model: str
    actual_model: str


class ProviderError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    def __init__(self, name: str, base_url: str, api_key: str, headers: dict[str, str] | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = headers or {}

    def chat_json(self, model: str, system: str, user: str, timeout: int = 120) -> ProviderResult:
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
            "response_format": {"type": "json_object"},
        }
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code >= 400 and r.status_code in (400, 422):
            # Some free routed models may not accept response_format. Retry once without it.
            payload.pop("response_format", None)
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {r.status_code}: {r.text[:1200]}")
        body = r.json()
        try:
            text = body["choices"][0]["message"]["content"]
        except Exception as exc:
            raise ProviderError(f"Unexpected {self.name} response: {str(body)[:1500]}") from exc
        return ProviderResult(
            data=extract_json(text),
            provider=self.name,
            requested_model=model,
            actual_model=body.get("model") or model,
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
                "No LLM provider configured. Set GROQ_API_KEY (recommended) or OPENROUTER_API_KEY."
            )

    def call(self, role: str, system: str, user: str, attempts_per_provider: int = 2) -> ProviderResult:
        errors: list[str] = []
        for name, provider, cfg in self.providers:
            model = cfg[f"{role}_model"]
            for attempt in range(attempts_per_provider):
                try:
                    return provider.chat_json(model=model, system=system, user=user)
                except Exception as exc:
                    errors.append(f"{name}/{model}: {exc}")
                    if attempt + 1 < attempts_per_provider:
                        time.sleep(2 ** attempt)
        raise ProviderError("All free providers failed:\n" + "\n".join(errors[-8:]))
