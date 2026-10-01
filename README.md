# Scrutai

> A panel of specialist AI agents reviews your PR; an adversarial critic kills the false positives.
> **Every finding survives scrutiny, or it doesn't ship.**

![status](https://img.shields.io/badge/status-alpha-orange)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

Scrutai is a **multi-agent code reviewer**. Instead of one model making one pass over a diff, it runs a
panel of specialist agents — each a ReAct agent grounded in the *real repository* through tools — and then
puts every finding on trial before an adversarial **critic** that challenges, downgrades, or kills it.
What reaches you is the small set of high-confidence issues that survived cross-examination, not a wall of noise.

## Why it's different

Most open-source AI reviewers are single-shot: `diff → model → comments`. Scrutai adds the three things
they skip:

- **Specialists grounded in the repo, not just the diff.** Each agent uses real tools — read file, ripgrep,
  `git blame`, run Semgrep/linters — so it reasons over the whole codebase.
- **An adversarial critic (self-reflection).** Every finding must be defended with evidence or it's dropped.
  Precision over volume, by design.
- **A published precision number.** A built-in eval harness measures precision / recall / false-positive rate
  on a labeled benchmark — run `scrutai eval` and see for yourself.

## Quickstart

```bash
uv sync                       # or: pip install -e ".[dev]"
scrutai review --demo         # run the pipeline on a bundled sample diff (no API key needed)
scrutai eval                  # run the benchmark, print precision/FPR
```

By default Scrutai runs in **mock mode** — the full graph executes offline with a stubbed LLM, so you can
see the architecture work before wiring up a provider. Set `llm_mode: live` in `.scrutai.yml` and export a
provider key (any LiteLLM-supported model) to run for real:

```bash
scrutai review --base main    # review your current branch against main
scrutai review --pr 128       # (v0.2) review a specific PR
```

## How it works

```
START → route → specialists → critic ──(stable)──→ verdict → END
                    │             ↑                    
              (fan-out to    (loops until stable
               ReAct agents)  or max rounds)
```

- **route** — inspects the diff and selects only relevant specialists (a docs-only change wakes nobody).
- **specialists** — Security, Correctness, Test-coverage (Performance & Style next), each a ReAct agent.
- **critic** — cross-examines each finding for truth, scope, severity, and evidence; sets confidence.
- **verdict** — assembles a typed `ReviewResult`: findings, a recommended verdict, cost, and rounds.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design rationale.

## Configuration (`.scrutai.yml`)

```yaml
enabled_agents: [security, correctness, tests]
min_severity: low
min_confidence: 0.6
max_critic_rounds: 2
fail_on: high          # exit non-zero (fail CI) at this severity
llm_mode: mock         # mock | live
models:
  router: gpt-4o-mini
  specialist: gpt-4o-mini
  critic: claude-3-5-sonnet-latest
```

## Roadmap

- **v0.1** — CLI + eval harness (mock + live). *You are here.*
- **v0.2** — GitHub Action (inline + idempotent comments, SARIF), Semgrep tool, Performance/Style agents,
  Langfuse/OTel tracing, cost budget guardrail.
- **v0.3** — web UI ("agent theater") visualizing the live graph, and a second framework adapter (CrewAI/ADK).

## License

Apache-2.0.
