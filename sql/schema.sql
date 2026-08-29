-- Reference schema. The application creates these tables automatically via SQLAlchemy.
-- This file documents the logical state model used by the monitor.

CREATE TABLE runs (
  id text PRIMARY KEY,
  started_at timestamptz NOT NULL,
  ended_at timestamptz,
  status text NOT NULL,
  scout_provider text,
  scout_model text,
  editor_provider text,
  editor_model text,
  stats jsonb,
  error text
);

CREATE TABLE sources (
  id bigserial PRIMARY KEY,
  fingerprint text UNIQUE NOT NULL,
  url text NOT NULL,
  title text NOT NULL,
  publisher text,
  published_at timestamptz,
  category_hint text,
  source_type text,
  snippet text,
  fetched_text text,
  first_seen_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL
);

CREATE TABLE developments (
  id text PRIMARY KEY,
  canonical_title text NOT NULL,
  category text NOT NULL,
  first_seen_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  status text,
  previous_state text,
  current_state text,
  materiality double precision,
  evidence_strength double precision,
  novelty double precision,
  confidence text,
  watch boolean NOT NULL DEFAULT false,
  reported_at timestamptz,
  report_count integer NOT NULL DEFAULT 0,
  latest_run_id text REFERENCES runs(id)
);

CREATE TABLE development_sources (
  development_id text REFERENCES developments(id),
  source_id bigint REFERENCES sources(id),
  first_linked_at timestamptz NOT NULL,
  PRIMARY KEY (development_id, source_id)
);

CREATE TABLE observations (
  id bigserial PRIMARY KEY,
  development_id text REFERENCES developments(id),
  run_id text REFERENCES runs(id),
  observed_at timestamptz NOT NULL,
  summary text,
  state_delta text,
  decision text,
  materiality double precision,
  evidence_strength double precision,
  novelty double precision,
  raw jsonb
);

CREATE TABLE candidate_decisions (
  id bigserial PRIMARY KEY,
  run_id text REFERENCES runs(id),
  created_at timestamptz NOT NULL,
  canonical_title text NOT NULL,
  category text,
  decision text,
  source_ids jsonb,
  raw jsonb
);

CREATE TABLE briefs (
  id bigserial PRIMARY KEY,
  run_id text UNIQUE REFERENCES runs(id),
  created_at timestamptz NOT NULL,
  body text NOT NULL
);
