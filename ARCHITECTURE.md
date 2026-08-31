# Frontier AI Monitor — Architecture

Status: documents the supplied **v8 code snapshot**.

For intended product semantics, see `docs/PRODUCT.md`.
For editorial rules, see `docs/EDITORIAL_POLICY.md`.
For known reliability gaps and the next plan, see `docs/RELIABILITY.md` and `docs/exec-plans/active/v9-reliability.md`.

## System overview

The current implementation is a scheduled, stateful change-detection pipeline:

```text
Google News RSS + arXiv + optional RSS
                 |
                 v
       deterministic collection
                 |
                 v
            Postgres/Neon
        sources + prior state
                 |
                 v
              Scout
       one compact call/topic
                 |
                 v
      dedupe + balanced select
                 |
                 v
    active evidence acquisition
      |       |        |       |
      |       |        |       +-- Crossref
      |       |        +---------- arXiv
      |       +------------------- targeted official/entity/news queries
      +--------------------------- existing stored corpus
                 |
                 v
              Editor
        one candidate/call
                 |
                 v
 deterministic state/evidence gates
                 |
        +--------+--------+
        |        |        |
      REPORT    WATCH    IGNORE
        |
        v
 Postgres + Markdown + optional email
```

The pipeline is deliberately split between deterministic code and LLM judgment.

## Entry point

`frontier_monitor/main.py`

High-level run sequence in v8:

1. load environment/config;
2. create/connect to the database;
3. create a `runs` row;
4. collect and persist sources;
5. enforce discovery health checks;
6. run Scout calls for active topics;
7. enforce Scout coverage check;
8. deduplicate and balance candidates;
9. acquire additional evidence;
10. run Editor candidate-by-candidate;
11. apply deterministic state/evidence rules and persist decisions;
12. render/save the brief;
13. write per-run, daily, and `latest.md` Markdown files;
14. optionally send email;
15. mark the run successful.

Important: the active v9 reliability plan proposes changes to the persistence/publication ordering. Do not assume the v8 ordering is the desired final semantics.

## Discovery

Implementation: `frontier_monitor/collectors.py`

Current `collect_all()` uses:

- Google News RSS per configured topic;
- arXiv;
- optional configured RSS/Atom feeds.

`collect_gdelt()` still exists in the codebase, but the current `collect_all()` path is intentionally **not dependent on GDELT**.

Exact-source deduplication happens before semantic/event deduplication.

Configuration: `config/topics.yaml`

Current monitored areas:

1. AI research automation
2. Autonomous agents
3. AI for science and biology
4. Verification and scaffolding
5. Model behavior and interpretability
6. Cyber risk
7. Energy and infrastructure
8. Major AI legal or policy conflicts

## Scout

Implementation: `frontier_monitor/pipeline.py::run_scout`
Prompt: `prompts/scout.txt`

Purpose:

- high-recall discovery;
- one topic per call;
- at most 5 candidates/topic;
- use only source IDs supplied by deterministic retrieval;
- classify an evidence-stage hint;
- ignore routine product noise, ordinary funding, commentary, and benchmark-only noise.

v8 intentionally uses compact prompts and delays between calls because free-tier TPM limits are a design constraint.

One Scout topic failure does not automatically kill the full run. `main.py` enforces a minimum Scout coverage gate afterward.

## Candidate deduplication and balancing

Implementation:

- `dedupe_candidates()`
- `balanced_select()`

Deduplication uses:

- shared source IDs;
- canonical-title similarity.

Balancing selects at least one strong candidate from each active category when capacity permits, then fills remaining slots by score.

## Active evidence acquisition

Implementation: `frontier_monitor/evidence_acquisition.py`

Runs after Scout and before Editor.

It can enrich a candidate with evidence from:

- already-stored relevant sources;
- targeted Google News RSS;
- targeted official-domain searches;
- primary-entity searches;
- arXiv;
- Crossref/DOI metadata.

The purpose is not to relax the publication gate. It is to actively look for the evidence that a conservative gate needs.

Evidence-acquisition diagnostics are attached to candidates/decisions for auditability.

## Evidence classification and publication gate

Implementation: `frontier_monitor/evidence.py`
Configuration: `config/evidence.yaml`

Source roles include concepts such as:

- official primary;
- primary research;
- primary research index/metadata;
- primary claim by an involved organization;
- reputable independent secondary;
- other secondary/context.

A company/lab source is primary evidence for **what that organization claims or did**, not independent confirmation of its own claim.

The deterministic `publication_gate()` applies type-aware rules to REPORT proposals.

Examples:

- legal/regulatory action: official source or sufficiently strong independent secondary corroboration;
- paper: primary research requirements, with explicit distinction between a paper and independent replication;
- demo/deployment/confirmed incident: stronger corroboration requirements;
- announcement: not directly publishable as REPORT under the current gate;
- all REPORT items must satisfy minimum update materiality and evidence strength.

## Editor

Implementation: `frontier_monitor/pipeline.py::run_editor`
Prompt: `prompts/editor.txt`

The Editor is deliberately **candidate-level**, not one giant batch request.

It receives a compact packet containing:

- candidate data;
- strongest source records/excerpts;
- aggregated evidence profile;
- relevant prior-state matches from the database.

Historical matching searches up to roughly:

```text
180 days / 300 developments
```

and supplies only the most relevant matches for each candidate.

Core concepts:

- `materiality`
- `update_materiality`
- `evidence_strength`
- `state_delta`
- `state_delta_kind`
- `matched_development_id`

The Editor can propose REPORT/WATCH/IGNORE, but deterministic code can downgrade REPORT.

## State delta kinds

Current prompt vocabulary:

- `new_development`
- `status_progression`
- `evidence_strengthening`
- `deployment_scale_change`
- `scope_expansion`
- `policy_or_legal_action`
- `infrastructure_commitment`
- `contradiction_or_retraction`
- `none`

Repeated coverage with no substantive change should normally become:

```text
state_delta_kind = none
decision = IGNORE
```

## Database

Implementation: `frontier_monitor/db.py`
Reference schema: `sql/schema.sql`

Core tables:

### `sources`
Retrieved articles/documents/items.

### `developments`
Canonical longitudinal state for an underlying development.

### `development_sources`
Many-to-many association between developments and sources.

### `observations`
Per-run state observations/deltas.

### `candidate_decisions`
Audit record for Editor/gate decisions, including IGNORE.

### `briefs`
Rendered human-facing brief for a run.

### `runs`
Operational run state, providers/models, stats, and errors.

The database is the machine source of truth for longitudinal state.

Markdown briefs are the human-readable archive.

## Brief archive

v8 writes:

```text
briefs/
  YYYY-MM-DD.md
  latest.md
  YYYY-MM-DD/
    HHMMSS_<run-suffix>.md
```

This preserves multiple manual/test runs on the same day.

## Providers

Implementation: `frontier_monitor/providers.py`
Configuration: `config/models.yaml`

Current intended roles:

### Groq
Primary Scout and Editor.

### OpenRouter free router
Scout fallback only. The project intentionally avoids a random free router for final editorial judgment because the actual model can vary.

### Gemini
Optional fallback, gated by environment configuration.

Provider availability and free-tier limits are external operational dependencies and can change.

## Scheduling

Workflow:

`.github/workflows/frontier-monitor.yml`

Current v8 deployment:

- GitHub Actions;
- scheduled daily;
- `Europe/Bucharest` timezone;
- manual `workflow_dispatch`;
- Neon/Postgres required in GitHub Actions;
- generated brief files are committed back to the repository.

## Source of truth hierarchy

For future agents:

```text
Product/editorial intent:
  docs/PRODUCT.md
  docs/EDITORIAL_POLICY.md

Current implementation:
  code + tests + ARCHITECTURE.md

Operational history/state:
  Postgres/Neon

Human output history:
  briefs/

Rationale / why:
  docs/DECISIONS.md

Work not yet implemented:
  docs/exec-plans/active/
```

