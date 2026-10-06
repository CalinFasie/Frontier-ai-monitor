# Architectural decisions and rationale

This is a concise decision log distilled from the project's design history and the supplied v8 code.

It exists so future agents do not "simplify" away decisions that solved known failure modes.

## D001 — Build a change detector, not a news summarizer

**Decision**

The system reports only deltas that materially change the frontier-AI world model.

**Reason**

A daily "important AI news" prompt tends to repeat stories, reward hype, and confuse novelty with significance.

**Consequence**

An empty brief is a valid successful outcome.

---

## D002 — Separate discovery from editorial judgment

**Decision**

Use a high-recall Scout stage followed by a stricter Editor stage.

**Reason**

Discovery and materiality judgment have different objectives.

A single prompt tends either to miss too much or publish too much.

**Consequence**

Scout candidates are intentionally noisy; Editor/gates are intentionally conservative.

---

## D003 — Retrieval should be as deterministic as practical

**Decision**

Collect sources in Python from public/free retrieval channels and give models source IDs/snippets rather than asking an LLM to freely browse for everything.

**Reason**

This improves coverage observability, cost control, source fidelity, and repeatability.

**Consequence**

The system can measure how many records/categories were actually checked.

---

## D004 — Store state in a real database

**Decision**

Use Postgres/Neon as the longitudinal machine source of truth.

**Reason**

Comparing only against the previous digest is too weak for semantic deduplication and multi-week story evolution.

**Consequence**

The data model distinguishes sources, developments, observations, decisions, briefs, and runs.

---

## D005 — Treat the underlying development as the canonical object

**Decision**

Multiple source records can map to one development.

**Reason**

Reuters, an official filing, and a company post may all describe one real-world event.

**Consequence**

Deduplication/change detection should operate at the development/state level, not only URL/title level.

---

## D006 — Keep materiality and update materiality separate

**Decision**

Track:

- `materiality` for the underlying development;
- `update_materiality` for the new delta.

**Reason**

A story can remain globally important while today's article adds nothing.

**Consequence**

A known material story can correctly become IGNORE with `update_materiality` near zero.

---

## D007 — Evidence strength is separate from materiality

**Decision**

High importance does not compensate for weak evidence.

**Reason**

Frontier AI produces many high-impact claims before independent confirmation.

**Consequence**

High-materiality/weak-evidence items belong in WATCH rather than REPORT.

---

## D008 — Add active evidence acquisition

**Decision**

After Scout, actively search for stronger/primary/official evidence before Editor/gating.

**Reason**

A strict gate without active acquisition produces false negatives simply because discovery found a weak secondary article first.

**Consequence**

Candidates carry an evidence-acquisition audit trail.

---

## D009 — Use a deterministic type-aware publication gate

**Decision**

The LLM cannot unilaterally publish a REPORT.

**Reason**

Prompts alone are too soft for evidence requirements and can drift between models.

**Consequence**

`publication_gate()` enforces minimum scores and status-specific evidence requirements.

---

## D010 — Company/lab claims are not independent confirmation

**Decision**

An involved organization's own page is primary evidence for its claim/action, not independent corroboration.

**Reason**

"Primary source" and "independent evidence" are different concepts.

**Consequence**

Capability claims often require external corroboration before REPORT.

---

## D011 — Papers and replication are different states

**Decision**

A paper may be primary evidence of a reported experiment without being independently replicated.

**Reason**

Requiring replication for every material paper is too conservative, while calling a paper "independent confirmation" is too strong.

**Consequence**

A material paper may be REPORT under paper-specific rules, with careful wording.

---

## D012 — Protect historical state from weak reiterations

**Decision**

A weaker repeated source should not downgrade a stronger existing state.

**Reason**

News cycles frequently resurface an old story with lower-quality coverage.

**Consequence**

Historical evidence/state is monotonic by default, except for genuine contradiction/retraction.

---

## D013 — Repeated commentary with no state change is IGNORE

**Decision**

Do not leave every known story in WATCH forever.

**Reason**

WATCH can otherwise become a graveyard of repeated headlines.

**Consequence**

Known development + no material delta -> deterministic/expected IGNORE.

---

## D014 — Benchmark-only noise should be filtered deterministically

**Decision**

A benchmark gain without evidence of practical capability change is not a frontier state change.

**Reason**

Benchmark news is abundant and can dominate the brief without changing real-world feasibility.

**Consequence**

The code has a benchmark-noise gate in addition to prompt rules.

---

## D015 — Fail closed on editorial uncertainty/provider failure

**Decision**

Random free-model routing may be acceptable for high-recall Scout fallback, but not for final editorial judgment.

**Reason**

The Editor determines a persistent world-state update.

**Consequence**

The configured OpenRouter free router is Scout-only; Editor failure should fail closed unless a known explicit fallback is enabled.

---

## D016 — Preserve per-run audit artifacts

**Decision**

Keep per-run briefs as well as daily/latest files.

**Reason**

Multiple test/manual runs in one day should not destroy the prior human-readable artifact.

**Consequence**

`briefs/YYYY-MM-DD/HHMMSS_<run>.md` is retained.

---

## D017 — Optimize prompts/calls for free-tier TPM without weakening deterministic gates

**Decision**

Use compact per-topic Scout and per-candidate Editor calls with deliberate pacing.

**Reason**

Free-tier TPM limits caused request-size/rate-limit failures.

**Consequence**

Prompt compression affects what the LLM sees, while deterministic evidence checks can still inspect the full stored evidence set.

---

## D018 — Reliability/evaluation is the next phase, not more features

**Decision**

The next priority after v8 should be reliability instrumentation and an editorial evaluation harness.

**Reason**

The system is already feature-rich enough that unmeasured changes can easily make quality worse while looking more sophisticated.

**Consequence**

See `docs/exec-plans/active/v9-reliability.md`.

---

## D019 — Provider/editor contract changes fail closed

**Decision**

Final editorial provider integrations must define an explicit output contract and fail closed on malformed, truncated, or schema-invalid output. Provider compatibility changes that affect final editorial judgment require deterministic/local tests and, when practical, a live provider smoke test before or around deployment.

**Reason**

OpenAI-compatible APIs are not behaviorally identical. The September–October 2026 Editor incident showed that transport success does not guarantee usable structured editorial output.

**Consequence**

Do not silently remove structured-output constraints in response to generic HTTP 400/422 errors, and do not infer production compatibility from mocks or unit tests alone.

**Rejected alternatives**

- Retry final editorial generation without its contract after a generic client error.
- Treat successful HTTP transport or mocked tests as sufficient proof of provider compatibility.

**Revisit condition**

Revisit the contract only when provider behavior or the editorial response shape changes, with evidence and tests that preserve fail-closed handling.

---

## D020 — Coverage continuity follows successfully covered time

**Decision**

The monitor must eventually track a persistent coverage watermark, or equivalent semantic state, for the interval successfully covered.

**Reason**

Schedule cadence and lookback duration can diverge, and provider or research failures can leave gaps.

**Consequence**

A failed or degraded run must not advance the point through which coverage is considered complete. The next healthy processing must account for unresolved time since the last confirmed coverage point. This behavior is planned, not implemented.

**Rejected alternatives**

- Treat a scheduled run as proof that its time interval was researched.
- Advance coverage on a failed or partial run.

**Revisit condition**

Revisit the stored representation during implementation if another durable state model preserves the same successfully-covered-through semantics.

---

## D021 — Unresolved coverage gaps cannot produce a healthy empty conclusion

**Decision**

“No material frontier developments since the previous review” is valid only when the relevant interval has healthy, sufficiently complete coverage.

**Reason**

A quiet result and an unresearched interval are different states.

**Consequence**

An unresolved gap must produce an explicit degraded/incomplete state, a catch-up requirement, or an equivalent representation; it must not be presented as a healthy empty brief.

**Rejected alternatives**

- Reuse healthy-empty wording when retrieval only covers a recent subset of the interval.

**Revisit condition**

Revisit the user-facing representation if a different status still makes the unresolved coverage gap explicit.

---

## D022 — Catch-up uses bounded resumable windows

**Decision**

Large coverage gaps should be processed in bounded time windows, with coverage advanced only after successful window processing.

**Reason**

Retrieval has per-topic caps and Scout sees only a bounded newest subset. A single large lookback can bury older events under newer records and still provide false coverage.

**Consequence**

The future v9 implementation must define resumable bounded catch-up semantics. Increasing `LOOKBACK_HOURS` alone is not a recovery mechanism.

**Rejected alternatives**

- Change `LOOKBACK_HOURS` from 72 to 168 and assume one week is enough.
- Set `LOOKBACK_HOURS` to roughly 800 for a month-long gap.
- Assume the next scheduled run can recover every outage in one retrieval pass.

**Revisit condition**

Window sizing may change based on measured retrieval limits, but catch-up must remain bounded, resumable, and evidence-backed.

---

## D023 — Production validation checks semantic side effects

**Decision**

For changes affecting production inference, persistence, or publication, validation should inspect relevant observable side effects as well as workflow status.

**Reason**

A green workflow can coexist with incorrect semantic output, missing persistent state, or absent expected artifacts.

**Consequence**

As appropriate to the change, validate workflow/job success, the expected model/provider path, persistent run state, generated brief/artifact, and publication/commit side effects. Keep validation proportional; documentation-only PRs do not require production validation.

**Rejected alternatives**

- Treat a successful Actions conclusion as complete evidence for every production behavior.

**Revisit condition**

Choose the evidence proportionally to the production behavior a change can affect.


---

## D024 — Coverage uses explicit temporal windows

**Decision**

Coverage is represented by explicit half-open [from, through) intervals and a persistent covered-through watermark. Only a COMPLETE research window advances that watermark. The durable coverage state is operational truth; runs.stats is a per-run audit record.

**Reason**

Relative lookbacks and successful workflow invocations cannot prove which contiguous time interval was researched, especially after failure or when retrieval is capped.

**Consequence**

Collectors must establish coverage for the requested interval before it can advance. Use one global scope initially; per-domain watermarks remain deferred. See Task 0 in the active v9 reliability plan.

---

## D025 — Research completion is distinct from publication completion

**Decision**

A research interval can be COMPLETE after its decisions and research state are durably persisted, even if downstream Markdown, Git, or email publication fails.

**Reason**

Delivery failure should not make the system research the same completed interval again. Current Editor persistence occurs before human-facing publication and includes fields with publication-like semantics.

**Consequence**

Live watermark advancement depends on a minimal Task 2 prerequisite: research completion must be independently and retry-safely committed without falsely finalizing publication state. This decision does not implement the broader Task 2 data model.

---

## D026 — Historical replay must not corrupt present state

**Decision**

Backfills over already-passed historical periods must not run naively through the normal state-mutating pipeline when newer development state already exists. The Sep 3–Oct 6 gap is initially processed as a retrospective audit.

**Reason**

Applying older observations after the successful Oct 6 run can introduce temporal leakage and mutate current development state or publication counters out of order.

**Consequence**

The initial audit uses bounded chronological windows, does not advance the live watermark, mutate current development state, increment report counters, or automatically republish historical REPORTs. Findings go to human review; reconciliation is a later explicit action/design. A time-travel-aware replay system is deferred unless needed.

**Rejected alternative**

Replay Sep 3–Oct 6 through the normal state-mutating path against the current database.
