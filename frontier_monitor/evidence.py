from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from .config import load_yaml
from .utils import title_similarity


_RESEARCH_CATEGORIES = {
    "ai_research_automation",
    "autonomous_agents",
    "ai_science_biology",
    "verification_scaffolding",
    "model_behavior_interpretability",
    "cyber_risk",
}
_RESEARCH_MARKERS = {
    "paper", "preprint", "study", "research", "method", "experiment", "trial",
    "benchmark", "evaluation", "dataset", "model", "algorithm", "framework",
    "mechanistic", "protein", "antibody", "molecule", "genomics", "theorem",
}
_RELEVANCE_STOP = {
    "about", "after", "again", "against", "among", "being", "could", "from", "have",
    "into", "more", "than", "that", "their", "this", "those", "through", "under",
    "using", "with", "without", "would", "will", "artificial", "intelligence", "new",
    "report", "reports", "reported", "says", "said", "announces", "announcement",
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _contains_any(text: str, values: list[str]) -> bool:
    text = _norm(text)
    return any(_norm(v) in text for v in values if _norm(v))


def _domain_matches(host: str, domains: list[str]) -> bool:
    host = (host or "").lower()
    for domain in domains:
        d = domain.lower().removeprefix("www.")
        if host == d or host.endswith("." + d):
            return True
    return False


def _publisher_key(row: dict[str, Any]) -> str:
    publisher = _norm(row.get("publisher"))
    if publisher:
        if publisher.startswith("arxiv"):
            return "arxiv"
        if str(row.get("source_type") or "").lower().startswith("crossref"):
            # Crossref is an index/transport, not an independent confirming org.
            # Use the scholarly publisher if present, but keep the DOI records
            # from masquerading as many independent primary organizations.
            return f"crossref:{publisher[:100]}"
        return publisher[:120]
    host = _host(row.get("url"))
    return host or f"source-{row.get('id')}"


def _candidate_stage(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    return str(candidate.get("evidence_stage") or candidate.get("status") or "").lower()


def candidate_looks_research(candidate: dict[str, Any] | None) -> bool:
    """Whether scholarly evidence can substantively support this candidate.

    This prevents arbitrary papers from counting as primary evidence for legal,
    infrastructure, opinion-letter, or incident claims merely because a few AI
    keywords overlap.
    """
    if not candidate:
        return True
    stage = _candidate_stage(candidate)
    category = str(candidate.get("category") or "").lower()
    if stage == "paper":
        return True
    if category not in _RESEARCH_CATEGORIES:
        return False
    text = _norm(
        " ".join(
            str(candidate.get(k) or "")
            for k in ("canonical_title", "what_happened", "why_potentially_material")
        )
    )
    return any(marker in text.split() for marker in _RESEARCH_MARKERS)


def _tokens(text: str) -> set[str]:
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+._/-]{2,}", text or "")
        if len(w) >= 4 and w.lower() not in _RELEVANCE_STOP
    }


def _candidate_relevance(candidate: dict[str, Any] | None, row: dict[str, Any]) -> float:
    if not candidate:
        return 1.0
    c_title = str(candidate.get("canonical_title") or "")
    s_title = str(row.get("title") or "")
    c_body = " ".join(
        str(candidate.get(k) or "")
        for k in ("canonical_title", "what_happened", "why_potentially_material")
    )
    s_body = " ".join(str(row.get(k) or "") for k in ("title", "snippet", "fetched_text"))
    seq = title_similarity(c_title, s_title)
    c_tokens = _tokens(c_body)
    s_tokens = _tokens(s_body)
    overlap = len(c_tokens & s_tokens) / max(1, min(len(c_tokens), 10))

    # Reward exact distinctive names/identifiers (LeVJEPA, Rytr, Claude3, etc.).
    distinctive = {
        t for t in c_tokens
        if any(ch.isdigit() for ch in t) or len(t) >= 9 or any(ch.isupper() for ch in t)
    }
    distinctive_overlap = 1.0 if distinctive & s_tokens else 0.0
    return min(1.0, (0.58 * seq) + (0.34 * overlap) + (0.08 * distinctive_overlap))


def classify_source(
    row: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_yaml("config/evidence.yaml")
    source_type = str(row.get("source_type") or "").lower()
    publisher = str(row.get("publisher") or "")
    host = _host(row.get("url"))

    if source_type in {"arxiv", "crossref_research"}:
        role = "primary_research"
    elif source_type == "crossref_index":
        role = "primary_research_index"
    elif source_type in {"rss_official", "official"}:
        role = "primary_official"
    elif source_type in {"rss_primary_claim", "primary_claim"}:
        role = "primary_claim"
    elif _contains_any(publisher, cfg.get("official_publishers", [])) or _domain_matches(host, cfg.get("official_domains", [])):
        role = "primary_official"
    elif _contains_any(publisher, cfg.get("primary_claim_publishers", [])) or _domain_matches(host, cfg.get("primary_claim_domains", [])):
        role = "primary_claim"
    elif _contains_any(publisher, cfg.get("reputable_secondary_publishers", [])) or _domain_matches(host, cfg.get("reputable_secondary_domains", [])):
        role = "secondary_reputable"
    else:
        role = "secondary_other"

    relevance = _candidate_relevance(candidate, row)

    if candidate:
        # Scholarly objects are primary evidence only when the candidate is in
        # fact a research claim. A random arXiv paper is not primary evidence
        # for an FTC order, an EU implementation action, or a power project.
        if role in {"primary_research", "primary_research_index"} and not candidate_looks_research(candidate):
            role = "context_only"
        else:
            min_rel = {
                "primary_research": 0.46,
                "primary_research_index": 0.52,
                "primary_official": 0.30,
                "primary_claim": 0.30,
                "secondary_reputable": 0.30,
                "secondary_other": 0.34,
            }.get(role, 0.30)
            if relevance < min_rel:
                role = "irrelevant"

    return {
        "id": int(row["id"]),
        "role": role,
        "publisher": publisher or None,
        "publisher_key": _publisher_key(row),
        "host": host or None,
        "relevance": round(relevance, 3),
    }


def evidence_profile(
    rows: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_yaml("config/evidence.yaml")
    assessed = [classify_source(row, cfg, candidate=candidate) for row in rows]
    counts = Counter(x["role"] for x in assessed)

    reputable_keys = {
        x["publisher_key"]
        for x in assessed
        if x["role"] == "secondary_reputable"
    }
    primary_keys = {
        x["publisher_key"]
        for x in assessed
        if x["role"] in {"primary_official", "primary_research", "primary_claim"}
    }
    eligible_roles = {
        "primary_official", "primary_research", "primary_research_index",
        "primary_claim", "secondary_reputable", "secondary_other",
    }

    return {
        "source_count": sum(1 for x in assessed if x["role"] in eligible_roles),
        "packet_source_count": len(assessed),
        "primary_official": counts["primary_official"],
        "primary_research": counts["primary_research"],
        "primary_research_index": counts["primary_research_index"],
        "primary_claim": counts["primary_claim"],
        "reputable_secondary": counts["secondary_reputable"],
        "other_secondary": counts["secondary_other"],
        "context_only": counts["context_only"],
        "irrelevant": counts["irrelevant"],
        "independent_primary_orgs": len(primary_keys),
        "independent_reputable_secondary_orgs": len(reputable_keys),
        "sources": assessed,
    }


def publication_gate(decision: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, str]:
    """Deterministic last-mile gate for REPORT decisions."""
    materiality = float(decision.get("update_materiality", decision.get("materiality", 0)))
    evidence_strength = float(decision.get("evidence_strength", 0))
    status = str(decision.get("status") or "").lower()

    if materiality < 7:
        return False, "update_materiality_below_7"
    if evidence_strength < 7:
        return False, "evidence_strength_below_7"

    official = int(profile.get("primary_official", 0))
    research = int(profile.get("primary_research", 0))
    research_index = int(profile.get("primary_research_index", 0))
    claim = int(profile.get("primary_claim", 0))
    secondary = int(profile.get("independent_reputable_secondary_orgs", 0))
    primary_any = official + research + claim

    if status == "rumor":
        return False, "rumor_not_publishable"
    if status == "announcement":
        return False, "announcement_requires_stronger_evidence"

    if status in {"enacted", "court_ruling", "regulatory_order"}:
        if official >= 1 or secondary >= 2:
            return True, "official_legal_source_or_two_independent_secondaries"
        return False, "legal_claim_lacks_official_or_two_independent_secondaries"

    if status == "paper":
        if research >= 1 and secondary >= 1:
            return True, "primary_research_plus_independent_secondary"
        if research >= 2 and int(profile.get("independent_primary_orgs", 0)) >= 2:
            return True, "two_independent_primary_research_sources"
        if research >= 1 and materiality >= 8 and evidence_strength >= 7:
            return True, "primary_research_material_claim_not_replication"
        if research_index >= 1 and secondary >= 1 and materiality >= 8 and evidence_strength >= 7:
            return True, "research_index_plus_independent_secondary"
        return False, "paper_missing_primary_research_or_materiality"

    if status == "demo":
        if evidence_strength >= 8 and ((primary_any >= 1 and secondary >= 1) or secondary >= 2):
            return True, "high_strength_demo_with_independent_corroboration"
        return False, "demo_lacks_high_strength_independent_corroboration"

    if status == "infrastructure_commitment":
        if official >= 1:
            return True, "official_infrastructure_commitment"
        if claim >= 1 and secondary >= 1:
            return True, "primary_claim_plus_independent_secondary"
        if secondary >= 2:
            return True, "two_independent_reputable_secondaries"
        return False, "infrastructure_commitment_lacks_documented_support"

    if status in {"independent_confirmation", "deployed", "incident_confirmed"}:
        if primary_any >= 1 and secondary >= 1:
            return True, "primary_plus_independent_secondary"
        if secondary >= 2:
            return True, "two_independent_reputable_secondaries"
        return False, "claim_lacks_independent_corroboration"

    if official >= 1:
        return True, "authoritative_official_primary"
    if primary_any >= 1 and secondary >= 1:
        return True, "primary_plus_independent_secondary"
    if secondary >= 2:
        return True, "two_independent_reputable_secondaries"
    return False, "insufficient_independent_evidence"
