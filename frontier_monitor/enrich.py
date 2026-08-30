from __future__ import annotations

import logging
from typing import Any

import requests

from .utils import clip

log = logging.getLogger(__name__)
# Trafilatura can emit many "discarding data: None" warnings for pages that
# contain no extractable article body. They are expected in a broad news crawl
# and obscure the monitor's real health signals.
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)

UA = "Mozilla/5.0 (compatible; frontier-ai-monitor/0.1; personal research)"


def fetch_public_text(url: str, max_download_bytes: int = 1_500_000) -> str:
    import trafilatura
    try:
        with requests.get(url, timeout=20, headers={"User-Agent": UA}, stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text" not in ctype and ctype:
                return ""
            chunks = []
            total = 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > max_download_bytes:
                    break
                chunks.append(chunk)
            html = b"".join(chunks).decode(r.encoding or "utf-8", errors="ignore")
        extracted = trafilatura.extract(
            html,
            include_links=False,
            include_images=False,
            include_comments=False,
            favor_precision=True,
        )
        return clip(extracted or "", 4500)
    except Exception as exc:
        log.info("Could not extract %s: %s", url, exc)
        return ""


def enrich_sources(db: Any, source_ids: list[int]) -> list[dict[str, Any]]:
    rows = db.get_sources(source_ids)
    for row in rows:
        if row.get("fetched_text"):
            continue
        source_type = str(row.get("source_type") or "").lower()
        # Google News RSS URLs point to the aggregator, not reliably to the
        # publisher article. Fetching them produces noisy/non-article HTML and
        # trafilatura warnings. The RSS title/summary remains valid discovery
        # metadata; direct RSS/official/research sources are enriched below.
        if source_type == "google_news":
            continue
        if source_type in {"arxiv", "crossref_research", "crossref_index"} and row.get("snippet"):
            continue
        text = fetch_public_text(row["url"])
        if text:
            db.update_source_text(int(row["id"]), text)
            row["fetched_text"] = text
    return rows
