from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative: str) -> dict[str, Any]:
    with (ROOT / relative).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class Settings:
    database_url: str
    lookback_hours: int
    max_editor_candidates: int
    max_final_items: int
    min_discovery_successes: int
    min_discovered_sources: int
    timezone: str

    @classmethod
    def from_env(cls) -> "Settings":
        db = os.getenv("DATABASE_URL", "").strip() or f"sqlite:///{ROOT / 'frontier_monitor.db'}"
        # Neon often provides postgres://; SQLAlchemy/psycopg prefers postgresql+psycopg://
        if db.startswith("postgres://"):
            db = "postgresql+psycopg://" + db[len("postgres://") :]
        elif db.startswith("postgresql://"):
            db = "postgresql+psycopg://" + db[len("postgresql://") :]
        return cls(
            database_url=db,
            lookback_hours=int(os.getenv("LOOKBACK_HOURS", "72")),
            max_editor_candidates=int(os.getenv("MAX_EDITOR_CANDIDATES", "10")),
            max_final_items=int(os.getenv("MAX_FINAL_ITEMS", "8")),
            min_discovery_successes=int(os.getenv("MIN_DISCOVERY_SUCCESSES", "6")),
            min_discovered_sources=int(os.getenv("MIN_DISCOVERED_SOURCES", "25")),
            timezone=os.getenv("TZ", "Europe/Bucharest"),
        )
