# Product specification

## Mission

Frontier AI Monitor is a **frontier intelligence change-detection system**.

It should answer:

> Since the previous known state, what new development is important enough that a technically sophisticated observer should update their model of AI capabilities, timelines, economics, risk, infrastructure constraints, regulation, or competitive dynamics?

It is **not** intended to summarize everything new in AI.

The desired output is the delta to the current world model.

## Product principle

A useful empty result is better than a noisy brief.

The system is allowed to say:

```text
No material frontier developments since the previous review.
```

when retrieval/coverage was healthy and nothing crossed the materiality threshold.

## Scope

The monitor covers eight broad areas.

### 1. AI research automation

Examples:

- automated AI research;
- recursive research workflows;
- systems designing/training/evaluating other AI systems;
- automated theorem proving;
- algorithm discovery.

### 2. Autonomous agents

Examples:

- long-horizon agents;
- computer-use agents;
- autonomous coding/research;
- multi-agent systems;
- real-world autonomous deployments.

### 3. AI for science and biology

Examples:

- drug discovery;
- protein/molecular design;
- genomics;
- chemistry;
- materials science;
- scientific discovery systems.

### 4. Verification and scaffolding

Examples:

- verifiers;
- process supervision;
- test-time compute/search;
- RL environments;
- tool scaffolding;
- formal verification.

### 5. Model behavior and interpretability

Examples:

- mechanistic interpretability;
- deceptive behavior;
- situational awareness;
- alignment/control;
- model autonomy;
- materially new behavioral evidence.

### 6. Cyber risk

Examples:

- offensive cyber capability;
- vulnerability discovery;
- autonomous exploitation;
- defensive AI;
- model security;
- AI-enabled attacks.

### 7. Energy / compute / infrastructure

Examples:

- datacenters;
- chips/networking;
- power/grid constraints;
- nuclear/energy commitments;
- AI infrastructure capex;
- compute bottlenecks.

### 8. Major AI legal or policy conflicts

Examples:

- court decisions;
- copyright;
- binding regulation;
- export controls;
- antitrust;
- national-security policy;
- major government action;
- international AI-policy conflict.

## What counts as material

A development is potentially material when it substantially changes one or more of:

- what AI systems can actually do;
- ability to automate AI R&D;
- autonomous real-world operation;
- scientific feasibility;
- economics of AI deployment;
- compute availability or bottlenecks;
- cyber offense/defense balance;
- model safety/control assumptions;
- legal precedent;
- regulatory constraints;
- geopolitical/strategic AI competition.

The product is explicitly skeptical of novelty without consequence.

## Non-goals

Do not optimize the product for:

- routine product launches;
- minor model releases;
- UI features;
- ordinary funding rounds;
- superficial partnerships;
- incremental benchmark records;
- generic opinions;
- public warnings/forecasts with no new evidence;
- open letters with no binding action;
- a famous person's changed opinion;
- generic calls for more or less regulation.

These can matter only when coupled to a concrete capability, deployment, legal/policy, evidence, or capital/infrastructure change that affects the world model.

## Core object: a development, not an article

An article is a **source**.

Several articles may describe the same **development**.

```text
official filing -----\
company post ----------> DEVELOPMENT
Reuters article ------/
```

The monitor should reason over the underlying development and its state, not treat every headline as a new event.

## Longitudinal model

Each meaningful development has a history.

A simplified progression might be:

```text
announced
   ->
demonstrated
   ->
independently tested
   ->
deployed
   ->
deployed at meaningful scale
```

Policy/legal topics have analogous progressions such as:

```text
proposed
   ->
adopted/enacted
   ->
enforced
   ->
court interpreted
```

Not every story follows a clean progression, but the product should prefer state-change reasoning over repeated headline reasoning.

## Materiality vs update materiality

These are separate by design.

### `materiality`

How consequential is the underlying story if established?

### `update_materiality`

How consequential is the new evidence/state delta in this run?

Example:

```text
Known development:
  materiality = 9

Today's article:
  repeats the same facts
  no new evidence
  no state progression

Expected:
  update_materiality = 0-2
  decision = IGNORE
```

This distinction is central to the product.

## Evidence strength

Importance and evidence are separate axes.

Examples:

```text
high materiality + weak evidence
    -> WATCH

high update materiality + strong evidence
    -> potentially REPORT

low update materiality + strong evidence
    -> usually IGNORE
```

## Human-facing output

The final brief should:

- contain only REPORT items;
- prefer fewer items;
- include no filler to reach a quota;
- explain **what changed**;
- explain **why the delta matters**;
- link to stored source records;
- make evidence limitations explicit where relevant;
- provide a concise bottom line.

## Operational user

The human operator is expected to act primarily as:

- product owner;
- evaluator;
- reviewer of run results;
- reviewer of database state and logs.

The project should minimize the need for the operator to manually copy code patches between an AI assistant and GitHub.

Repository documentation should therefore be sufficient for a coding agent to understand the product and propose/implement changes without depending on chat history.

## Success criteria

The system is successful when it achieves all of the following:

1. **High recall for genuinely material developments.**
2. **Low false-REPORT rate.**
3. **Low repetition of already-known developments.**
4. **Clear distinction between announcement, evidence, demonstration, deployment, and binding action.**
5. **Correct handling of quiet days.**
6. **No silent conversion of retrieval failures into empty briefs.**
7. **Traceable decisions and evidence.**
8. **Historical state integrity.**
9. **Reproducible behavior that can be evaluated across versions.**

## Infrastructure constraint

The project has intentionally pursued a zero/near-zero infrastructure and inference-cost path using:

- GitHub Actions;
- free/public retrieval sources;
- Neon/Postgres free tier;
- free-tier model providers when available.

This is an engineering constraint, not the product mission.

Provider free tiers and model availability can change. Do not sacrifice correctness, auditability, or state integrity merely to preserve one provider.
