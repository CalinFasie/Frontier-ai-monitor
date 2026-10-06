# Production blocker fix: Groq Editor structured output

Base: main `313b3aacacf4ec0646032daa9747734cae2a0788`.
Scope: approved focused blocker fix, not implementation of v9 reliability.

## Failure and intended behavior

Editor output was capped at 800 tokens, with successful September 3 completions
already at 670-800 tokens. Later Actions executions failed with non-JSON/empty
Editor responses; September 7 and 9 instead ended in TPM errors. Previously every
400/422 immediately retried without response_format, including JSON generation
failures. Failure logs discarded finish reason and token usage. Reasoning text
could also be mistaken for the final answer.

Groq openai/gpt-oss-120b Editor now uses strict JSON Schema, exactly one decision,
max_completion_tokens=2048, and reasoning_effort=low. All object properties are
required, objects are closed, and matched_development_id is string-or-null.
The response is validated locally and bound to the current candidate index.
Only complete final content is parsed directly as JSON: no brace extraction,
reasoning recovery, unconstrained retries, or automatic Editor generation retries.
Length termination, empty content, malformed JSON, duplicate keys, non-finite
numbers, and contract violations fail closed. Available finish/usage metadata is
retained in failure messages, without full response/reasoning text.

Other paths retain legacy JSON Object Mode. A compatibility retry without
response_format is allowed only on explicit unsupported-format errors, never
json_validate_failed, invalid schema, or unrelated 400/422 errors.

## Request budget and dependencies

The local estimator uses tiktoken's o200k_harmony encoding over the full serialized
request (including schema), plus 256 tokens of formatting allowance and the full
2048 completion reservation. Requests estimated above 7000 fail before HTTP, with
no silent clipping or retry. This leaves 1000 additional tokens below the observed
8000 TPM cap; it is NOT an exact bound on Groq's internal templates/accounting or
protection from other organization traffic. Existing 65-second pacing is unchanged.

The maximum-size synthetic fixture fills existing title/excerpt/source/prior-state
caps and estimates 5464 reserved tokens. Unbounded URL input is also tested to fail
before generation. No claim is made that all future packets fit.

Runtime additions: jsonschema (local contract validation) and tiktoken (local token
accounting). On first use tiktoken may download public encoding data. If tokenizer
initialization/accounting fails, log a warning without exception details and proceed
with the unchanged strict request; provider rejection still fails closed without
retries. Successful accounting still enforces the 7000-token preflight cap.
Deployments may prewarm the o200k_harmony cache and set TIKTOKEN_CACHE_DIR if needed.

## Verification and unchanged behavior

Regression tests cover truncation, HTTP error classification, exactly-one output,
candidate-index binding, reasoning-only responses, unchanged Scout compatibility,
unchanged evidence-gate downgrades/bottom line, request accounting, and request
counts/pacing. Full verification command: `python -m pytest -q`.

The unused model-generated bottom_line field is removed; the deterministic
pipeline-generated bottom line remains authoritative. Temperature, provider order,
Scout budgets, editorial thresholds, evidence policy, persistence, and production
workflow are unchanged. OpenRouter remains Scout-only; explicitly enabled Gemini
fallback remains as configured. No database schema change or migration is needed.
Neon authentication is outside this fix; no live DB or inference was used in tests.

## Rollback and residual risks

Revert this PR as a unit; no database rollback is necessary. Reverting restores
the known 800-token production failure risk. Before production rollout, an isolated
non-publishing Groq smoke check should confirm the deployed model accepts the
schema and assess low-effort decision quality. Unit tests do not establish live
provider acceptance or editorial quality equivalence. The 2048 cap remains bounded:
unusually verbose/reasoning-heavy outputs can still fail, intentionally fail-closed.

References: Groq Structured Outputs, Reasoning, and Rate Limits documentation;
OpenAI Harmony documentation for GPT-OSS tokenizer compatibility.
