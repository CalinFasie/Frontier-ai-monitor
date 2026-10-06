CREATE TABLE coverage_state (
    scope TEXT PRIMARY KEY NOT NULL,
    covered_through TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_run_id VARCHAR(64) NULL REFERENCES runs (id)
);
