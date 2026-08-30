from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from .utils import development_id, utcnow


metadata = MetaData()

runs = Table(
    "runs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("status", String(32), nullable=False),
    Column("scout_provider", String(64)),
    Column("scout_model", String(128)),
    Column("editor_provider", String(64)),
    Column("editor_model", String(128)),
    Column("stats", JSON),
    Column("error", Text),
)

sources = Table(
    "sources",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("fingerprint", String(64), nullable=False, unique=True),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("publisher", String(255)),
    Column("published_at", DateTime(timezone=True)),
    Column("category_hint", String(80)),
    Column("source_type", String(40)),
    Column("snippet", Text),
    Column("fetched_text", Text),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
)

developments = Table(
    "developments",
    metadata,
    Column("id", String(96), primary_key=True),
    Column("canonical_title", Text, nullable=False),
    Column("category", String(80), nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("status", String(64)),
    Column("previous_state", Text),
    Column("current_state", Text),
    Column("materiality", Float),
    Column("evidence_strength", Float),
    Column("novelty", Float),
    Column("confidence", String(16)),
    Column("watch", Boolean, nullable=False, default=False),
    Column("reported_at", DateTime(timezone=True)),
    Column("report_count", Integer, nullable=False, default=0),
    Column("latest_run_id", String(64), ForeignKey("runs.id")),
)

development_sources = Table(
    "development_sources",
    metadata,
    Column("development_id", String(96), ForeignKey("developments.id"), primary_key=True),
    Column("source_id", Integer, ForeignKey("sources.id"), primary_key=True),
    Column("first_linked_at", DateTime(timezone=True), nullable=False),
)

observations = Table(
    "observations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("development_id", String(96), ForeignKey("developments.id"), nullable=False),
    Column("run_id", String(64), ForeignKey("runs.id"), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("summary", Text),
    Column("state_delta", Text),
    Column("decision", String(16)),
    Column("materiality", Float),
    Column("evidence_strength", Float),
    Column("novelty", Float),
    Column("raw", JSON),
)

candidate_decisions = Table(
    "candidate_decisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(64), ForeignKey("runs.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("canonical_title", Text, nullable=False),
    Column("category", String(80)),
    Column("decision", String(16)),
    Column("source_ids", JSON),
    Column("raw", JSON),
)

briefs = Table(
    "briefs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(64), ForeignKey("runs.id"), unique=True, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("body", Text, nullable=False),
)


class Database:
    def __init__(self, url: str):
        self.engine: Engine = create_engine(url, future=True, pool_pre_ping=True)
        metadata.create_all(self.engine)

    def start_run(self, run_id: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(insert(runs).values(id=run_id, started_at=utcnow(), status="running", stats={}))

    def finish_run(self, run_id: str, status: str, stats: dict[str, Any], **models: Any) -> None:
        values = {"ended_at": utcnow(), "status": status, "stats": stats, **models}
        with self.engine.begin() as cx:
            cx.execute(update(runs).where(runs.c.id == run_id).values(**values))

    def fail_run(self, run_id: str, error: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(runs).where(runs.c.id == run_id).values(ended_at=utcnow(), status="failed", error=error[:12000]))

    def upsert_source(self, item: dict[str, Any]) -> int:
        now = utcnow()
        with self.engine.begin() as cx:
            existing = cx.execute(select(sources.c.id).where(sources.c.fingerprint == item["fingerprint"])).first()
            if existing:
                cx.execute(
                    update(sources)
                    .where(sources.c.id == existing.id)
                    .values(last_seen_at=now, snippet=item.get("snippet") or sources.c.snippet)
                )
                return int(existing.id)
            result = cx.execute(
                insert(sources).values(
                    fingerprint=item["fingerprint"],
                    url=item["url"],
                    title=item["title"],
                    publisher=item.get("publisher"),
                    published_at=item.get("published_at"),
                    category_hint=item.get("category_hint"),
                    source_type=item.get("source_type"),
                    snippet=item.get("snippet"),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            return int(result.inserted_primary_key[0])

    def update_source_text(self, source_id: int, text: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(update(sources).where(sources.c.id == source_id).values(fetched_text=text))

    def get_sources(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(int(x) for x in ids))
        if not ids:
            return []
        with self.engine.begin() as cx:
            rows = cx.execute(select(sources).where(sources.c.id.in_(ids))).mappings().all()
        by_id = {int(r["id"]): dict(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def recent_sources(self, days: int = 14, category: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        cutoff = utcnow() - timedelta(days=days)
        stmt = select(sources).where(sources.c.last_seen_at >= cutoff)
        if category:
            stmt = stmt.where(sources.c.category_hint == category)
        stmt = stmt.order_by(sources.c.last_seen_at.desc()).limit(limit)
        with self.engine.begin() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings().all()]

    def known_developments(self, days: int = 90, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = utcnow() - timedelta(days=days)
        stmt = (
            select(developments)
            .where(developments.c.last_seen_at >= cutoff)
            .order_by(developments.c.last_seen_at.desc())
            .limit(limit)
        )
        with self.engine.begin() as cx:
            return [dict(r) for r in cx.execute(stmt).mappings().all()]


    def log_candidate_decision(self, run_id: str, decision: dict[str, Any], source_ids: list[int]) -> None:
        with self.engine.begin() as cx:
            cx.execute(
                insert(candidate_decisions).values(
                    run_id=run_id,
                    created_at=utcnow(),
                    canonical_title=decision.get("canonical_title", "Untitled"),
                    category=decision.get("category"),
                    decision=decision.get("decision"),
                    source_ids=list(source_ids),
                    raw=decision,
                )
            )

    def persist_decision(self, run_id: str, decision: dict[str, Any], source_ids: list[int]) -> str:
        now = utcnow()
        requested = decision.get("matched_development_id")
        dev_id = requested or development_id(decision["canonical_title"])
        with self.engine.begin() as cx:
            existing = cx.execute(select(developments).where(developments.c.id == dev_id)).mappings().first()
            report = decision.get("decision") == "REPORT"
            watch = decision.get("decision") == "WATCH"
            values = dict(
                canonical_title=decision["canonical_title"],
                category=decision["category"],
                last_seen_at=now,
                status=decision.get("status"),
                previous_state=(existing or {}).get("current_state") if existing else None,
                current_state=decision.get("what_changed") or decision.get("state_delta"),
                materiality=float(decision.get("materiality", 0)),
                evidence_strength=float(decision.get("evidence_strength", 0)),
                novelty=float(decision.get("novelty", 0)),
                confidence=decision.get("confidence"),
                watch=watch,
                latest_run_id=run_id,
            )
            if existing:
                if report:
                    values["reported_at"] = now
                    values["report_count"] = int(existing.get("report_count") or 0) + 1
                cx.execute(update(developments).where(developments.c.id == dev_id).values(**values))
            else:
                values.update(first_seen_at=now, reported_at=now if report else None, report_count=1 if report else 0)
                cx.execute(insert(developments).values(id=dev_id, **values))

            cx.execute(
                insert(observations).values(
                    development_id=dev_id,
                    run_id=run_id,
                    observed_at=now,
                    summary=decision.get("what_changed"),
                    state_delta=decision.get("state_delta"),
                    decision=decision.get("decision"),
                    materiality=float(decision.get("materiality", 0)),
                    evidence_strength=float(decision.get("evidence_strength", 0)),
                    novelty=float(decision.get("novelty", 0)),
                    raw=decision,
                )
            )
            for source_id in set(source_ids):
                exists = cx.execute(
                    select(development_sources.c.source_id).where(
                        development_sources.c.development_id == dev_id,
                        development_sources.c.source_id == source_id,
                    )
                ).first()
                if not exists:
                    cx.execute(insert(development_sources).values(development_id=dev_id, source_id=source_id, first_linked_at=now))
        return dev_id

    def save_brief(self, run_id: str, body: str) -> None:
        with self.engine.begin() as cx:
            cx.execute(delete(briefs).where(briefs.c.run_id == run_id))
            cx.execute(insert(briefs).values(run_id=run_id, created_at=utcnow(), body=body))
