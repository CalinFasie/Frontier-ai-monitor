# Project history

This records major production incidents, project transitions, and validation lessons. It is not a changelog of every commit.

## 2026-09-03 — Last successful run before the provider incident

The monitor completed successfully. This became the last successful publication before a prolonged sequence of failed runs.

## September–October 2026 — Groq Editor failure period

Subsequent runs repeatedly failed during the Editor stage. Observed failures included truncated or non-JSON output, responses cut near the old completion budget, and some Groq TPM/rate-limit failures. A successful discovery or Scout phase did not mean the full monitor run had succeeded.

Failed runs could still contain useful intermediate research and candidates, but the then-current persistence semantics did not retain complete `candidate_decision` records for those failed runs.

## 2026-10-06 — Groq Editor structured-output fix

PR #1, “Fix Groq Editor truncation with strict structured output,” merged as `05f2d3eb1118194640fb2a8696292a53764b00b2`. The Groq Editor moved to strict JSON Schema structured output, with its completion budget increased from 800 to 2048 tokens. The unsafe `response_format` fallback was removed from the strict Editor path, and malformed or incomplete output fails closed. The local suite passed 85 tests before merge.

An isolated live Groq smoke test also succeeded: HTTP 200 from `openai/gpt-oss-120b`, `finish_reason=stop`, valid JSON, and exactly one decision.

## 2026-10-06 — Production validation

Manual GitHub Actions `workflow_dispatch` run #31 succeeded on commit `05f2d3eb1118194640fb2a8696292a53764b00b2`. Production Editor calls completed, the GitHub Actions bot committed the generated brief as `da16fa59f0fd7ca30afdda9629ff0b731164a26a`, and Neon recorded the run as successful.

A green workflow is useful evidence, but production validation should also check persistent run state and expected artifacts.

## 2026-10-06 — Coverage-continuity defect discovered

Production validation exposed a separate reliability flaw. The workflow is scheduled weekly but still sets `LOOKBACK_HOURS=72`, so collection covers only roughly the latest three days. There is no persistent successfully-covered-through watermark or catch-up mechanism. After the failed-run period, the next successful run did not reconstruct the full interval since the last successful review, yet its brief said, “No material frontier developments since the previous review.” That conclusion is not justified while relevant coverage gaps remain unresolved.

The Groq provider incident was fixed, but it exposed a separate P0 coverage-continuity problem. Once a safe bounded catch-up mechanism exists, the missing interval from 2026-09-03 through 2026-10-06 should be backfilled.
