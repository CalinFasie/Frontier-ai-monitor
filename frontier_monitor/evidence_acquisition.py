from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any

import requests

from .collectors import UA, collect_arxiv, collect_google_news
from .evidence import evidence_profile
from .utils import compact_ws, fingerprint, title_similarity, utcnow

log = logging.getLogger(__name__)

_STOP = {
    "about", "after", "again", "against", "among", "being", "build", "building",
    "could", "from", "have", "into", "large", "model", "models", "more", "report",
    "reports", "study", "using", "with", "without", "their", "this", "that", "these",
    "those", "through", "under", "over", "than", "then", "they", "will", "would",
    "artificial", "intelligence", "machine", "learning", "system", "systems", "method",
    "new", "first", "research", "approach", "results", "result", "shows", "show",
}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+._/-]{2,}", text or "")
    out: list[str] = []
    for word in words:
        norm = word.lower().strip("._-/")
        if len(norm) < 4 or norm in _STOP or norm.isdigit():
            continue
        if norm not in out:
            out.append(norm)
    return out


def _query_terms(candidate: dict[str, Any], limit: int = 7) -> list[str]:
    title = str(candidate.get("canonical_title") or "")
    happened = str(candidate.get("what_happened") or "")
    title_words = _tokens(title)

    # Preserve distinctive acronyms/product/lab names first.
    original_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+._/-]{2,}", title)
    distinctive = [
        w.strip(".,:;()[]{}").lower()
        for w in original_words
        if (sum(ch.isupper() for ch in w) >= 2 or any(ch.isdigit() for ch in w) or len(w) >= 9)
    ]
    merged: list[str] = []
    for word in distinctive + title_words + _tokens(happened):
        if word and word not in merged and word not in _STOP:
            merged.append(word)
    return merged[:limit]


def _source_score(candidate: dict[str, Any], row: dict[str, Any]) -> float:
    candidate_title = str(candidate.get("canonical_title") or "")
    source_title = str(row.get("title") or "")
    sim = title_similarity(candidate_title, source_title)
    c_tokens = set(_tokens(candidate_title + " " + str(candidate.get("what_happened") or "")))
    s_tokens = set(_tokens(source_title + " " + str(row.get("snippet") or "")))
    overlap = len(c_tokens & s_tokens) / max(1, min(len(c_tokens), 10))
    category_bonus = 0.08 if row.get("category_hint") == candidate.get("category") else 0.0
    return (0.62 * sim) + (0.38 * overlap) + category_bonus


def _relevant(candidate: dict[str, Any], rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    ranked = sorted((( _source_score(candidate, r), r) for r in rows), key=lambda x: x[0], reverse=True)
    # A deliberately moderate threshold: source metadata/snippets are noisy,
    # while the Editor remains the semantic judge downstream.
    return [r for score, r in ranked if score >= 0.30][:limit]


def _targeted_news_query(candidate: dict[str, Any]) -> str:
    terms = _query_terms(candidate, 7)
    if not terms:
        return compact_ws(candidate.get("canonical_title"))
    # Quote the most distinctive term, keep the rest unquoted for recall.
    return '"' + terms[0] + '" ' + " ".join(terms[1:])


_OFFICIAL_DOMAINS = [
    "ftc.gov", "justice.gov", "cisa.gov", "nist.gov", "sec.gov",
    "federalregister.gov", "congress.gov", "europa.eu", "eur-lex.europa.eu",
    "gov.uk", "supremecourt.gov",
]

_PRIMARY_ENTITY_DOMAINS = {
    "openai": "openai.com",
    "anthropic": "anthropic.com",
    "deepmind": "deepmind.google",
    "google": "research.google",
    "meta": "ai.meta.com",
    "nvidia": "nvidia.com",
    "microsoft": "microsoft.com",
    "hugging face": "huggingface.co",
    "huggingface": "huggingface.co",
    "spacex": "spacex.com",
    "xai": "x.ai",
    "mistral": "mistral.ai",
}


def _site_query(candidate: dict[str, Any], domains: list[str], term_limit: int = 5) -> str:
    terms = _query_terms(candidate, term_limit)
    if not terms or not domains:
        return ""
    sites = " OR ".join(f"site:{d}" for d in domains)
    return f"({sites}) " + " ".join(terms)


def _targeted_official_query(candidate: dict[str, Any]) -> str:
    stage = str(candidate.get("evidence_stage") or "").lower()
    category = str(candidate.get("category") or "").lower()
    if stage not in {"enacted", "court_ruling", "regulatory_order", "incident_confirmed", "infrastructure_commitment"} and category != "legal_policy":
        return ""
    return _site_query(candidate, _OFFICIAL_DOMAINS, 5)


def _targeted_primary_entity_query(candidate: dict[str, Any]) -> str:
    text = (str(candidate.get("canonical_title") or "") + " " + str(candidate.get("what_happened") or "")).lower()
    domains: list[str] = []
    for name, domain in _PRIMARY_ENTITY_DOMAINS.items():
        if name in text and domain not in domains:
            domains.append(domain)
    return _site_query(candidate, domains[:2], 5)


def _targeted_arxiv_cfg(candidate: dict[str, Any]) -> dict[str, Any] | None:
    terms = _query_terms(candidate, 5)
    if not terms:
        return None
    # V5 used AND across the first three model-generated title terms. That was
    # too brittle: canonical titles often contain paraphrases ("reduces",
    # "costs") that are absent from the actual paper title. Search first by the
    # most distinctive token and let title/source relevance filter the results.
    return {
        "enabled": True,
        "max_results": 20,
        "query": f'all:"{terms[0]}"',
    }


def _strip_markup(value: str | None) -> str:
    if not value:
        return ""
    return compact_ws(unescape(re.sub(r"<[^>]+>", " ", value)))


def _crossref_date(item: dict[str, Any]) -> datetime | None:
    for key in ("published-online", "published-print", "published", "created"):
        block = item.get(key) or {}
        parts = block.get("date-parts") if isinstance(block, dict) else None
        if parts and parts[0]:
            vals = list(parts[0]) + [1, 1]
            try:
                return datetime(int(vals[0]), int(vals[1]), int(vals[2]), tzinfo=timezone.utc)
            except Exception:
                pass
        if key == "created" and isinstance(block, dict) and block.get("date-time"):
            try:
                return datetime.fromisoformat(str(block["date-time"]).replace("Z", "+00:00"))
            except Exception:
                pass
    return None


def collect_crossref_candidate(candidate: dict[str, Any], max_results: int = 6) -> list[dict[str, Any]]:
    """Resolve a likely scholarly work by title without requiring an API key.

    Crossref is used as a fallback when the paper is not on arXiv or when a
    model-generated canonical title makes arXiv keyword search brittle. Records
    with an abstract count as primary-research evidence; metadata-only records
    are kept as research-index evidence and cannot, by themselves, prove the
    paper's substantive claims.
    """
    title = compact_ws(candidate.get("canonical_title"))
    if not title:
        return []
    r = requests.get(
        "https://api.crossref.org/works",
        params={"query.title": title, "rows": max_results},
        timeout=(6, 20),
        headers={"User-Agent": UA},
    )
    r.raise_for_status()
    items = ((r.json() or {}).get("message") or {}).get("items") or []
    out: list[dict[str, Any]] = []
    for item in items:
        titles = item.get("title") or []
        work_title = compact_ws(titles[0] if titles else "")
        doi = compact_ws(item.get("DOI"))
        if not work_title or not doi:
            continue
        sim = title_similarity(title, work_title)
        candidate_tokens = set(_tokens(title))
        work_tokens = set(_tokens(work_title))
        overlap = len(candidate_tokens & work_tokens) / max(1, min(len(candidate_tokens), 8))
        if sim < 0.43 and overlap < 0.45:
            continue
        abstract = _strip_markup(item.get("abstract"))
        url = f"https://doi.org/{doi}"
        out.append({
            "fingerprint": fingerprint(url, work_title),
            "url": url,
            "title": work_title,
            "publisher": item.get("publisher") or "Crossref",
            "published_at": _crossref_date(item),
            "category_hint": candidate.get("category") or "ai_research_automation",
            "source_type": "crossref_research" if len(abstract) >= 120 else "crossref_index",
            "snippet": abstract[:1600] if abstract else f"DOI metadata record for {work_title}",
        })
    return out


def _need_targeted_search(candidate: dict[str, Any], profile: dict[str, Any]) -> bool:
    stage = str(candidate.get("evidence_stage") or "").lower()
    if stage == "paper" and int(profile.get("primary_research", 0)) < 1 and int(profile.get("primary_research_index", 0)) < 1:
        return True
    if stage in {"incident_confirmed", "deployed", "independent_confirmation", "demo"}:
        return int(profile.get("independent_reputable_secondary_orgs", 0)) < 2
    if stage in {"enacted", "court_ruling", "regulatory_order", "infrastructure_commitment"}:
        return int(profile.get("primary_official", 0)) < 1 and int(profile.get("independent_reputable_secondary_orgs", 0)) < 2
    return int(profile.get("source_count", 0)) < 3


def acquire_candidate_evidence(
    db: Any,
    candidate: dict[str, Any],
    topics_cfg: dict[str, Any],
    lookback_hours: int,
    local_days: int = 14,
) -> dict[str, Any]:
    """Expand a Scout candidate's source set without using a paid search API.

    Order of operations:
      1. retrieve semantically related sources already collected into Postgres;
      2. if evidence is still weak for the claimed stage, run one targeted
         Google News RSS query;
      3. for paper candidates, run a targeted arXiv query;
      4. persist newly discovered sources and attach only sources that score as
         relevant to the candidate.

    This function never upgrades REPORT/WATCH itself. It only produces a
    stronger evidence packet for the Editor and deterministic publication gate.
    """
    original_ids = [int(x) for x in candidate.get("source_ids", [])]
    all_ids = list(dict.fromkeys(original_ids))

    # Reuse the corpus already paid for in network/collector terms.
    local = db.recent_sources(days=local_days, category=candidate.get("category"), limit=350)
    for row in _relevant(candidate, local, limit=8):
        sid = int(row["id"])
        if sid not in all_ids:
            all_ids.append(sid)

    profile_before = evidence_profile(db.get_sources(all_ids))
    targeted_attempted: list[str] = []
    targeted_added = 0

    def _run_targeted(query: str, label: str, hours: int, max_records: int = 12) -> None:
        nonlocal targeted_added
        if not query:
            return
        try:
            targeted_attempted.append(label)
            items = collect_google_news(
                candidate.get("category") or "ai_research_automation",
                query,
                hours,
                max_records,
            )
            new_rows: list[dict[str, Any]] = []
            for item in items:
                sid = db.upsert_source(item)
                row = dict(item)
                row["id"] = sid
                new_rows.append(row)
            for row in _relevant(candidate, new_rows, limit=5):
                sid = int(row["id"])
                if sid not in all_ids:
                    all_ids.append(sid)
                    targeted_added += 1
        except Exception as exc:
            log.info("Targeted %s evidence search failed for %s: %s", label, candidate.get("canonical_title"), exc)

    # For legal/policy, cyber incidents, and concrete infrastructure claims,
    # actively try authoritative government/regulatory pages first. Google News
    # RSS is only the discovery transport; evidence.py classifies the actual
    # publisher as official when an official result is found.
    current_profile = evidence_profile(db.get_sources(all_ids))
    if int(current_profile.get("primary_official", 0)) < 1:
        _run_targeted(_targeted_official_query(candidate), "official_site_search", max(lookback_hours * 4, 336), 14)

    # For named labs/companies, try their own domain so an announcement can at
    # least be grounded in the primary claim rather than secondary retellings.
    current_profile = evidence_profile(db.get_sources(all_ids))
    if int(current_profile.get("primary_claim", 0)) < 1:
        _run_targeted(_targeted_primary_entity_query(candidate), "primary_entity_search", max(lookback_hours * 4, 336), 12)

    current_profile = evidence_profile(db.get_sources(all_ids))
    if _need_targeted_search(candidate, current_profile):
        try:
            query = _targeted_news_query(candidate)
            if query:
                targeted_attempted.append("google_news")
                items = collect_google_news(
                    candidate.get("category") or "ai_research_automation",
                    query,
                    min(max(lookback_hours * 2, 96), 336),
                    16,
                )
                new_rows: list[dict[str, Any]] = []
                for item in items:
                    sid = db.upsert_source(item)
                    row = dict(item)
                    row["id"] = sid
                    new_rows.append(row)
                for row in _relevant(candidate, new_rows, limit=6):
                    sid = int(row["id"])
                    if sid not in all_ids:
                        all_ids.append(sid)
                        targeted_added += 1
        except Exception as exc:
            log.info("Targeted Google News evidence search failed for %s: %s", candidate.get("canonical_title"), exc)

        time.sleep(0.35)

    # Papers deserve a direct attempt to locate the primary research object.
    stage = str(candidate.get("evidence_stage") or "").lower()
    profile_mid = evidence_profile(db.get_sources(all_ids))
    if stage == "paper" and int(profile_mid.get("primary_research", 0)) < 1:
        arxiv_cfg = _targeted_arxiv_cfg(candidate)
        if arxiv_cfg:
            try:
                targeted_attempted.append("arxiv")
                items = collect_arxiv(arxiv_cfg, topics_cfg)
                new_rows = []
                for item in items:
                    # Keep the Scout's category when a targeted paper search
                    # found the item; generic arXiv topic classification is less
                    # important here than candidate linkage.
                    item["category_hint"] = candidate.get("category") or item.get("category_hint")
                    sid = db.upsert_source(item)
                    row = dict(item)
                    row["id"] = sid
                    new_rows.append(row)
                for row in _relevant(candidate, new_rows, limit=5):
                    sid = int(row["id"])
                    if sid not in all_ids:
                        all_ids.append(sid)
                        targeted_added += 1
            except Exception as exc:
                log.info("Targeted arXiv evidence search failed for %s: %s", candidate.get("canonical_title"), exc)

    # If arXiv did not resolve the scholarly object, try a DOI/title index. This
    # catches conference/journal papers and canonical-title paraphrases.
    profile_after_arxiv = evidence_profile(db.get_sources(all_ids))
    if stage in {"paper", "announcement"} and int(profile_after_arxiv.get("primary_research", 0)) < 1:
        try:
            targeted_attempted.append("crossref")
            items = collect_crossref_candidate(candidate, 6)
            new_rows = []
            for item in items:
                sid = db.upsert_source(item)
                row = dict(item)
                row["id"] = sid
                new_rows.append(row)
            for row in _relevant(candidate, new_rows, limit=3):
                sid = int(row["id"])
                if sid not in all_ids:
                    all_ids.append(sid)
                    targeted_added += 1
        except Exception as exc:
            log.info("Targeted Crossref evidence search failed for %s: %s", candidate.get("canonical_title"), exc)

    # Keep packets bounded. Prefer original Scout evidence, then highest-scoring
    # acquired evidence.
    original_set = set(original_ids)
    rows = db.get_sources(all_ids)
    acquired_rows = [r for r in rows if int(r["id"]) not in original_set]
    acquired_rows = _relevant(candidate, acquired_rows, limit=8)
    final_ids = original_ids + [int(r["id"]) for r in acquired_rows]
    final_ids = list(dict.fromkeys(final_ids))[:10]
    candidate["source_ids"] = final_ids

    final_profile = evidence_profile(db.get_sources(final_ids))

    # A Scout may label a research result as an announcement because the news
    # story was discovered before the paper. If evidence acquisition resolves a
    # true primary research object, give the Editor the stronger stage hint.
    stage_after = stage
    if stage == "announcement" and int(final_profile.get("primary_research", 0)) >= 1:
        stage_after = "paper"
        candidate["evidence_stage_upgraded_from"] = "announcement"
        candidate["evidence_stage"] = "paper"

    stats = {
        "title": candidate.get("canonical_title"),
        "stage_before": stage,
        "stage_after": stage_after,
        "original_sources": len(original_ids),
        "final_sources": len(final_ids),
        "targeted_attempted": targeted_attempted,
        "targeted_added": targeted_added,
        "profile_before": {k: v for k, v in profile_before.items() if k != "sources"},
        "profile_after": {k: v for k, v in final_profile.items() if k != "sources"},
    }
    candidate["evidence_acquisition"] = stats
    return stats


def acquire_evidence_for_candidates(
    db: Any,
    candidates: list[dict[str, Any]],
    topics_cfg: dict[str, Any],
    lookback_hours: int,
) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    for candidate in candidates:
        stats.append(acquire_candidate_evidence(db, candidate, topics_cfg, lookback_hours))
    return stats
