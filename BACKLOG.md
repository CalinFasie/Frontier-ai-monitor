# Backlog

This backlog is intentionally short. The project should not add new orchestration/framework layers until reliability and evaluation are stronger.

## P0 — v9 reliability

See `docs/exec-plans/active/v9-reliability.md`.

- [ ] Add real pytest CI for pull requests/pushes.
- [ ] Correct documentation/Makefile test command.
- [ ] Separate observation/editorial decision from successful publication state.
- [ ] Preserve partial stats and failed stage for failed runs.
- [ ] Add per-domain coverage health.
- [ ] Distinguish healthy empty brief from degraded/failed research.
- [ ] Start an editorial regression/evaluation corpus.
- [ ] Explicitly treat retrieved source text as untrusted prompt data.

## P1 — evidence and measurement

- [ ] Formalize independent publisher vs independent evidence where practical.
- [ ] Add deterministic/direct source adapters for high-value primary sources.
- [ ] Measure source/category coverage over time.
- [ ] Measure REPORT/WATCH/IGNORE distribution and editorial regressions.
- [ ] Add explicit schema migration mechanism before the first real Neon schema alteration.
- [ ] Review provider-specific request parameters instead of assuming one OpenAI-compatible payload is optimal for every provider.

## P2 — only after evaluation exists

- [ ] Small review/quality dashboard if it reduces operator effort.
- [ ] Human feedback labels for false positive / false negative / duplicate / watch.
- [ ] Embedding-assisted historical matching only if current matching is measurably insufficient.
- [ ] Additional provider/model fallbacks only when they solve a measured reliability problem.

## Explicitly deferred

Do not add merely for sophistication:

- LangChain/LangGraph;
- n8n;
- T3 Code integration;
- vector database for the full corpus;
- complex multi-agent orchestration;
- Kubernetes/permanent server infrastructure.

Any of these can be revisited if a concrete measured need appears.

