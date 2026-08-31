# Reliability and evaluation

Status: combines durable reliability principles with a snapshot of known issues identified against the supplied v8 code.

Do not treat the "known v8 gaps" section as already implemented work.

## Reliability goal

A plausible-looking brief is not enough.

The monitor must distinguish:

```text
A. healthy run, no material frontier delta
B. degraded/failed research that happened to produce no REPORT items
```

Confusing B with A is one of the most dangerous failure modes.

## Critical failure modes

The project design has identified these as especially important:

1. missing an important development;
2. poor materiality/update-materiality judgment;
3. bad temporal memory or semantic deduplication;
4. weak primary-source verification;
5. confusing claims/announcements with demonstrated reality;
6. false positives driven by hype;
7. false negatives from overly rigid gates;
8. source monoculture / category imbalance;
9. citation/source mismatch;
10. silent partial failure;
11. model/provider drift;
12. prompt complexity/drift;
13. loss or regression of historical state.

## Current v8 protections

The supplied v8 code already includes several useful safeguards.

### Persistent state

Neon/Postgres persists sources, developments, observations, decisions, briefs, and runs.

### Discovery health checks

`main.py` rejects runs when:

- too few collectors report success;
- too few sources are stored.

This protects against a simple false quiet day.

### Scout coverage gate

Individual topic Scout calls can fail without discarding successful topics.

A minimum-success gate is enforced afterward.

### Candidate balancing

Editor capacity is distributed across active categories before remaining slots are filled by score.

### Active evidence acquisition

The system does not simply reject weak evidence. It attempts to find stronger/primary/official evidence before editorial judgment.

### Type-aware deterministic publication gate

REPORT proposals are checked in code, with different rules for papers, legal actions, demos, deployments, etc.

### State integrity protections

The system is designed so a weaker repeated observation does not automatically replace a previously stronger evidence state.

Contradictions/retractions are allowed to reduce confidence when legitimate.

### Per-run archives

Multiple runs in the same day can be retained as separate Markdown artifacts.

## Known v8 gaps

These were identified by inspecting the supplied v8 snapshot.

### 1. Complete tests are not run by CI

The actual suite is pytest-based.

Current correct command:

```bash
python -m pytest -q
```

The existing README/Makefile use `unittest discover`, which does not collect the complete current suite.

The scheduled GitHub Actions workflow does not run the test suite before the production monitor job.

This should be corrected without coupling production scheduling unnecessarily to a redundant daily full test run; a separate PR/push CI workflow is preferable.

### 2. Publication-state ordering is not transactionally clean

In v8, Editor results are persisted before Markdown/email publication finishes.

That means a run can mutate development/report state and then fail during publication/delivery.

The active v9 plan should separate at least:

```text
observed / decided / published
```

A failure after editorial decision should not falsely imply that the human-facing report was successfully published/delivered.

### 3. Coverage health is too aggregate

A high total source count can hide missing monitored domains.

Example:

```text
agents: 30
research automation: 25
cyber: 20

biology: 0
policy: 0
energy: 0
interpretability: 0
verification: 0
```

A total-count gate can still look healthy.

The system should record per-domain coverage and define healthy/degraded/failed coverage semantics.

### 4. Failed runs lose useful partial stats

`fail_run()` currently records the error/status but does not preserve all in-memory stage statistics accumulated before failure.

Persist:

- failed stage;
- partial discovery/scout/evidence/editor stats;
- enough context to diagnose the run without reconstructing logs manually.

### 5. No editorial regression harness yet

The project has code tests, but not a durable corpus that answers:

> Did v9 make editorial decisions better than v8?

A future eval corpus should measure decisions over real historical cases.

### 6. Web content is untrusted

Retrieved titles/snippets/pages are model input.

Prompts should explicitly state that source content is data, not instructions, and prompt injection inside sources must be ignored.

## Desired run-health model

A future run should expose explicit stages such as:

```text
STARTED
DISCOVERY_OK
SCOUT_OK
EVIDENCE_OK
EDITOR_OK
GATE_OK
PUBLISHED
```

and failures/degradation such as:

```text
FAILED_DISCOVERY
FAILED_SCOUT
FAILED_EVIDENCE
FAILED_EDITOR
FAILED_PUBLISH
DEGRADED_COVERAGE
```

Exact schema is an implementation decision; the invariant is that failure location must be explicit and auditable.

## Coverage metrics

Recommended per-run metrics:

```json
{
  "sources_checked": 0,
  "items_retrieved": 0,
  "deduped_items": 0,
  "topic_counts": {
    "ai_research_automation": 0,
    "autonomous_agents": 0,
    "ai_science_biology": 0,
    "verification_scaffolding": 0,
    "model_behavior_interpretability": 0,
    "cyber_risk": 0,
    "energy_infrastructure": 0,
    "legal_policy": 0
  },
  "scout_candidates": 0,
  "evidence_enriched": 0,
  "report": 0,
  "watch": 0,
  "ignore": 0
}
```

Names can differ from this sketch. The important requirement is domain-level visibility.

## Testing strategy

### Unit/integration tests

Use for deterministic code:

- source classification;
- evidence gate;
- matching/state integrity;
- provider error handling;
- DB persistence;
- archive behavior;
- coverage/failure logic.

Run:

```bash
python -m pytest -q
```

### Editorial evaluation corpus

Create a separate eval corpus from real historical examples.

Suggested case groups:

```text
evals/
  should_report.jsonl
  should_watch.jsonl
  should_ignore.jsonl
  state_progressions.jsonl
```

Each case should contain enough prior state + new evidence to evaluate the decision.

Potential metrics:

- REPORT precision;
- false REPORT rate;
- false IGNORE rate;
- duplicate/reiteration rejection;
- state-progression accuracy;
- evidence-status classification;
- domain recall on a manually curated time window.

Do not pretend that a numeric LLM score alone is an objective ground truth. Human-labeled expected behavior and explanations are more useful.

## Model/provider drift

The provider/model layer is intentionally swappable.

Whenever a model or provider changes materially:

1. record the version/config;
2. run deterministic tests;
3. run the editorial eval corpus;
4. compare decision distributions and notable regressions;
5. do not judge quality only from one attractive brief.

## Database schema evolution

The v8 application uses SQLAlchemy `create_all()` for initialization.

`create_all()` is not a migration framework.

Before making a schema change to an existing Neon deployment, add an explicit migration strategy (for example Alembic or small versioned migrations) instead of assuming `create_all()` will alter existing columns/tables safely.
