from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compact_ws(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clip(text: str | None, n: int) -> str:
    value = compact_ws(text)
    if len(value) <= n:
        return value
    return value[: max(0, n - 1)].rstrip() + "…"


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        # Tracking query strings are deliberately dropped for dedupe stability.
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    except Exception:
        return url.strip()


def fingerprint(url: str, title: str = "") -> str:
    raw = f"{canonical_url(url)}\n{compact_ws(title).lower()}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()


def development_id(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:55]
    digest = hashlib.sha1(compact_ws(title).lower().encode()).hexdigest()[:10]
    return f"dev_{normalized or 'item'}_{digest}"


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, compact_ws(a).lower(), compact_ws(b).lower()).ratio()


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError("Model response did not contain a valid JSON object")


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")
