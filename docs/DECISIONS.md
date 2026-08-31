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

