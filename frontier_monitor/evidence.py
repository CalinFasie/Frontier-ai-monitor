from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from .config import load_yaml


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
        # arXiv publisher strings contain author names; collapse to arxiv so
        # several mirrors/authors do not masquerade as independent outlets.
        if publisher.startswith("arxiv"):
            return "arxiv"
        return publisher[:120]
    host = _host(row.get("url"))
    return host or f"source-{row.get('id')}"


def classify_source(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
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

    return {
        "id": int(row["id"]),
        "role": role,
        "publisher": publisher or None,
        "publisher_key": _publisher_key(row),
        "host": host or None,
    }


def evidence_profile(rows: list[dict[str, Any]], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_yaml("config/evidence.yaml")
    assessed = [classify_source(row, cfg) for row in rows]
    counts = Counter(x["role"] for x in assessed)

    reputable_keys = {
        x["publisher_key"]
        for x in assessed
        if x["role"] == "secondary_reputable"
    }
    primary_keys = {
        x["publisher_key"]
        for x in assessed
        if x["role"].startswith("primary_")
    }

    return {
        "source_count": len(assessed),
        "primary_official": counts["primary_official"],
        "primary_research": counts["primary_research"],
        "primary_research_index": counts["primary_research_index"],
        "primary_claim": counts["primary_claim"],
        "reputable_secondary": counts["secondary_reputable"],
        "other_secondary": counts["secondary_other"],
        "independent_primary_orgs": len(primary_keys),
        "independent_reputable_secondary_orgs": len(reputable_keys),
        "sources": assessed,
    }


def publication_gate(decision: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, str]:
    """Deterministic last-mile gate for REPORT decisions.

    The gate intentionally errs toward WATCH. It never upgrades a model
    decision; it only decides whether a proposed REPORT is publishable.
    """
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

    # A law actually enacted or a court ruling can be established by the
    # authoritative legal/government source itself.
    if status in {"enacted", "court_ruling", "regulatory_order"}:
        if official >= 1 or secondary >= 2:
            return True, "official_legal_source_or_two_independent_secondaries"
        return False, "legal_claim_lacks_official_or_two_independent_secondaries"

    # A paper is primary evidence of the experiment/result it reports. It is
    # NOT evidence of independent replication. High-materiality research may
    # therefore be reportable before press coverage or replication exists, but
    # the brief must label it explicitly as a primary-research result.
    if status == "paper":
        if research >= 1 and secondary >= 1:
            return True, "primary_research_plus_independent_secondary"
        if research >= 2 and int(profile.get("independent_primary_orgs", 0)) >= 2:
            return True, "two_independent_primary_research_sources"
        if research >= 1 and materiality >= 8 and evidence_strength >= 7:
            return True, "primary_research_material_claim_not_replication"
        # A DOI/index record proves that the scholarly object exists, but not its
        # substantive claims. It is publishable only when paired with an
        # independent reputable source describing the result.
        if research_index >= 1 and secondary >= 1 and materiality >= 8 and evidence_strength >= 7:
            return True, "research_index_plus_independent_secondary"
        return False, "paper_missing_primary_research_or_materiality"

    # Demos are especially hype-prone. Only exceptional, strongly evidenced
    # demos pass, and they still need independent corroboration.
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

    # Unknown/other statuses: conservative generic rule.
    if official >= 1:
        return True, "authoritative_official_primary"
    if primary_any >= 1 and secondary >= 1:
        return True, "primary_plus_independent_secondary"
    if secondary >= 2:
        return True, "two_independent_reputable_secondaries"
    return False, "insufficient_independent_evidence"
