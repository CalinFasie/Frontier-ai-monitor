from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests
from dateutil import parser as dateparser

from .utils import clip, compact_ws, fingerprint

log = logging.getLogger(__name__)
UA = "frontier-ai-monitor/0.1 (+personal research monitor)"


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        parsed = dateparser.parse(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def collect_gdelt(topic_key: str, query: str, lookback_hours: int, max_records: int) -> list[dict[str, Any]]:
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": min(max_records, 250),
        "timespan": f"{lookback_hours}h",
        "sort": "datedesc",
        "format": "json",
    }
    r = requests.get(
    url,
    params=params,
    timeout=(8, 20),
    headers={"User-Agent": UA},
    )
    r.raise_for_status()
    payload = r.json()
    out: list[dict[str, Any]] = []
    for a in payload.get("articles", []):
        article_url = a.get("url")
        title = compact_ws(a.get("title"))
        if not article_url or not title:
            continue
        out.append(
            {
                "fingerprint": fingerprint(article_url, title),
                "url": article_url,
                "title": title,
                "publisher": a.get("domain") or a.get("sourcecountry"),
                "published_at": _dt(a.get("seendate")),
                "category_hint": topic_key,
                "source_type": "gdelt",
                "snippet": "",
            }
        )
    return out


def _topic_for_arxiv(entry: Any, topics: dict[str, Any]) -> str:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    tags = {t.get("term", "") for t in entry.get("tags", []) if isinstance(t, dict)}
    if any(tag.startswith("q-bio") for tag in tags):
        return "ai_science_biology"
    if "cs.CR" in tags:
        return "cyber_risk"
    scored: list[tuple[int, str]] = []
    for key, cfg in topics.items():
        score = sum(1 for kw in cfg.get("keywords", []) if kw.lower() in text)
        scored.append((score, key))
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else "ai_research_automation"


def collect_arxiv(cfg: dict[str, Any], topics: dict[str, Any]) -> list[dict[str, Any]]:
    import feedparser
    if not cfg.get("enabled", True):
        return []
    params = {
        "search_query": cfg.get("query", "cat:cs.AI"),
        "start": 0,
        "max_results": int(cfg.get("max_results", 80)),
        "sortBy": "lastUpdatedDate",
        "sortOrder": "descending",
    }
    r = requests.get(
        "https://export.arxiv.org/api/query",
        params=params,
        timeout=40,
        headers={"User-Agent": UA},
    )
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError(f"arXiv feed parse failure: {parsed.bozo_exception}")
    out: list[dict[str, Any]] = []
    for e in parsed.entries:
        url = e.get("link")
        title = compact_ws(e.get("title"))
        if not url or not title:
            continue
        authors = ", ".join(a.get("name", "") for a in e.get("authors", [])[:4])
        out.append(
            {
                "fingerprint": fingerprint(url, title),
                "url": url,
                "title": title,
                "publisher": f"arXiv{': ' + authors if authors else ''}",
                "published_at": _dt(e.get("updated") or e.get("published")),
                "category_hint": _topic_for_arxiv(e, topics),
                "source_type": "arxiv",
                "snippet": clip(e.get("summary", ""), 1600),
            }
        )
    return out


def collect_rss(feed_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    import feedparser
    url = feed_cfg["url"]
    r = requests.get(url, timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise RuntimeError(f"RSS parse failure for {url}: {parsed.bozo_exception}")
    out: list[dict[str, Any]] = []
    for e in parsed.entries:
        link = e.get("link")
        title = compact_ws(e.get("title"))
        if not link or not title:
            continue
        published_at = None
        if e.get("published_parsed"):
            published_at = datetime.fromtimestamp(calendar.timegm(e.published_parsed), tz=timezone.utc)
        elif e.get("updated_parsed"):
            published_at = datetime.fromtimestamp(calendar.timegm(e.updated_parsed), tz=timezone.utc)
        out.append(
            {
                "fingerprint": fingerprint(link, title),
                "url": link,
                "title": title,
                "publisher": feed_cfg.get("name"),
                "published_at": published_at,
                "category_hint": feed_cfg.get("default_topic", "ai_research_automation"),
                "source_type": "rss",
                "snippet": clip(e.get("summary") or e.get("description") or "", 1600),
            }
        )
    return out


def collect_all(config: dict[str, Any], lookback_hours: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    topics = config.get("topics", {})
    max_records = int(config.get("max_records_per_topic", 35))
    all_items: list[dict[str, Any]] = []
    successes: list[str] = []
    failures: dict[str, str] = {}

    for key, topic in topics.items():
        try:
            items = collect_gdelt(key, topic["gdelt_query"], lookback_hours, max_records)
            all_items.extend(items)
            successes.append(f"gdelt:{key}")
        except Exception as exc:
            failures[f"gdelt:{key}"] = str(exc)
            log.exception("GDELT collection failed for %s", key)
        time.sleep(5.2)

    try:
        items = collect_arxiv(config.get("arxiv", {}), topics)
        all_items.extend(items)
        successes.append("arxiv")
    except Exception as exc:
        failures["arxiv"] = str(exc)
        log.exception("arXiv collection failed")

    for feed_cfg in config.get("rss_feeds", []):
        name = feed_cfg.get("name") or feed_cfg.get("url")
        try:
            all_items.extend(collect_rss(feed_cfg))
            successes.append(f"rss:{name}")
        except Exception as exc:
            failures[f"rss:{name}"] = str(exc)
            log.exception("RSS collection failed for %s", name)

    # Exact-source dedupe; semantic/event dedupe happens in the Scout.
    deduped: dict[str, dict[str, Any]] = {}
    for item in all_items:
        deduped[item["fingerprint"]] = item

    stats = {
        "collector_successes": successes,
        "collector_failures": failures,
        "raw_items": len(all_items),
        "deduped_items": len(deduped),
    }
    return list(deduped.values()), stats
