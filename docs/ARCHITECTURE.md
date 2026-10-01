# Architecture & design rationale

This document explains *why* Scrutai is built the way it is. (The "why" is what an
interviewer probes — keep it honest and specific.)

## Library-first

`scrutai` the package is the engine. The CLI, the (v0.2) GitHub Action, and the
(v0.3) web UI are all thin consumers of `review_diff()`. Nothing about GitHub or a
terminal leaks into the core. This is what lets the same reviewed logic power four
surfaces without duplication.

## Why hierarchical delegation over a flat swarm

A single generalist prompt that "finds all the issues" degrades as scope grows and
gives you no control over cost or focus. A hierarchy — an orchestrator that routes
to narrow specialists — means: (1) each agent has a tight, testable remit; (2) the
router can skip irrelevant specialists (a CSS-only change never wakes the security
agent), which is the main cost lever; (3) new specialists are additive.

## Why the ReAct specialists use real tools

Reasoning over the diff *text* alone misses everything the diff doesn't show —
the function being called three files away, who last touched a line, whether a
Semgrep rule fires. Grounding each specialist in the repo (read/grep/blame/SAST)
is the single biggest quality lever and the main thing that separates Scrutai from
single-shot reviewers.

## Why an adversarial critic (self-reflection)

LLM reviewers are noisy: they raise plausible-sounding issues that don't hold up.
The critic is a self-reflection loop that forces every finding to justify itself
with evidence, then sets a confidence the threshold filters on. The product
promise — three real issues, not thirty noisy ones — lives or dies here.

### Why the loop terminates where it does

The critic edge can route back to itself for another round, but v0.1 converges in
one pass (`_unstable()` returns False). A real multi-round debate (specialists
revise and resubmit after the critic pushes back) is a deliberate v0.2 item; the
termination condition (`max_critic_rounds`) is already in place so the loop can
never run away.

## Why everything is a Pydantic object

Findings are typed from the moment a specialist emits them. That makes the critic
able to reason over structured objects (not prose), makes dedup trivial, and makes
the whole non-deterministic system unit-testable against a mock LLM.

## Why an eval harness ships in v0.1

A reviewer you can't measure is a reviewer you can't trust. Precision / recall /
false-positive rate on a labeled benchmark turns "it seems good" into a number,
gives a CI gate against regressions, and is the credibility artifact no single-shot
tool publishes.

## The framework seam

Specialists implement one `Specialist` interface. v0.1 wires the graph with
LangGraph and runs LangGraph-native agents, but the interface is the seam that lets
a CrewAI- or ADK-backed specialist drop in later (v0.3) to demonstrate all three
frameworks without a rewrite.
