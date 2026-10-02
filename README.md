# Scrutai

> A panel of specialist AI agents reviews your PR; an adversarial critic kills the false positives.
> **Every finding survives scrutiny, or it doesn't ship.**

![status](https://img.shields.io/badge/status-beta-yellow)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

![The agent theater mid-review: the critic in round 1, the defend loop active, three findings on trial](docs/images/theater.png)

Scrutai is a **multi-agent code reviewer**. Instead of one model making one pass over a diff, it runs a
panel of specialist agents (each a ReAct agent grounded in the *real repository* through tools) and then
puts every finding on trial before an adversarial **critic** that challenges, downgrades, or kills it.
What reaches you is the small set of issues that survived cross-examination, not a wall of noise.

## Why it's different

Most open-source AI reviewers are single-shot: `diff → model → comments`. Scrutai adds the three things
they skip:

- **Specialists grounded in the repo, not just the diff.** Each agent runs a ReAct loop over real tools
  (`read_file`, `grep`, `git_blame`, Semgrep), so it reasons over the whole codebase.
- **An adversarial critic with a real debate.** The critic sees the cited code and decides *uphold*,
  *downgrade*, *kill*, or *challenge*. A challenged finding goes back to its specialist, who must defend it
  with fresh tool evidence or withdraw it. Anything the critic could not judge is withheld.
- **A published precision number.** A built-in eval harness measures precision / recall / false-positive
  rate on a labeled benchmark, *with and without the critic*, so the critic has to earn its tokens.

## Quickstart

```bash
uv sync --extra dev           # or: pip install -e ".[dev]"
scrutai review --demo         # full pipeline on a bundled sample, offline, no API key
scrutai review --demo --show-dropped   # ...and see what the critic killed, and why
scrutai eval                  # run the benchmark: precision, recall, critic lift
```

Scrutai runs in **mock mode** by default: the whole graph executes offline against a deterministic
stand-in model, so you can see the architecture work before wiring up a provider. To run for real,
set `llm_mode: live` in `.scrutai.yml` and export a key for any [LiteLLM](https://docs.litellm.ai)
provider (defaults use Claude):

```bash
export ANTHROPIC_API_KEY=...
scrutai review --base main                 # your branch vs main (merge-base, like a PR)
scrutai review --diff change.patch         # a patch file ('-' reads stdin)
scrutai review --pr 128 --post             # a GitHub PR; posts summary + inline comments
scrutai review --base main -f sarif -o scrutai.sarif   # for GitHub code scanning
scrutai review --base main --trace run.jsonl           # every node, LLM call, tool call, verdict
scrutai serve                              # the agent theater at http://127.0.0.1:8765
```

Exit codes: `0` clean, `1` a finding at or above `fail_on`, `2` usage / diff / API error.

## How it works

```
                  +-> specialist (security, chunk 1) -+
    START -> route -+-> specialist (tests,    chunk 1) -+-> collect -> critic --(challenged)--> defend
                    +-> specialist (...,      chunk n) -+                ^  |                     |
                                                                         |  +---------------------+
                                                       (settled or out of rounds) -> verdict -> END
```

- **route**: picks only relevant specialists (a docs-only change wakes nobody; security only wakes on
  risky code or sensitive paths). An optional LLM router can narrow the choice, never widen it.
- **specialists**: Security, Correctness, Test coverage, Performance, Style. Each is a ReAct agent
  run concurrently per diff **chunk** (about 250 added lines), with each chunk routed on its own.
- **collect**: merges and deduplicates findings (same file, line and category means one issue).
- **critic**: cross-examines each finding against the cited code. A *challenge* starts a debate round
  (`defend`); `max_critic_rounds` bounds it.
- **verdict**: a typed `ReviewResult`: findings, verdict, dropped findings with reasons, tokens, cost.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design rationale.

## Agent theater (`scrutai serve`)

```bash
pip install "scrutai[web]"
scrutai serve                       # http://127.0.0.1:8765
scrutai serve --replay run.jsonl    # open a recorded --trace as a run
```

A React UI over the live trace stream. Start a review from the browser (demo, git range, pasted
patch or PR) and watch it happen: the graph lights up as the router picks specialists and each
one works through its chunks. On the **trial board**, every finding moves from *On trial* to
*Upheld* or *Killed* with its full story: who raised it, the critic's challenge, the specialist's
defense or withdrawal, and the final ruling. Finished runs can be replayed at 1×/4×/16× or scrubbed
event by event, and any saved `--trace` file plays back exactly like a live run.

The server binds to `127.0.0.1` by default: it can read your repository and spend your API budget.
The built UI ships inside the Python package; Node is only needed to develop it (`web/`).

## Framework backends (CrewAI)

```yaml
backends: {security: crewai}   # one agent on CrewAI...
backends: {"*": crewai}        # ...or all of them
```

Every specialist implements one seam, `Specialist._loop` (its ReAct loop). The CrewAI backend
overrides only that: a CrewAI `Agent` + `Task` + `Crew` runs the loop, Scrutai's sandboxed tools
are exposed as CrewAI tools, and the model is reached through a bridge LLM that wraps Scrutai's own
client, so budgets, cost tracking, tracing and mock/live mode all still apply. Prompts, parsing and
the critic debate are shared, which makes the comparison fair:

```bash
pip install "scrutai[crewai]"
scrutai eval --compare crewai
```

| metric (50-case benchmark, mock mode) | native | crewai |
|---|---|---|
| precision / recall | 1.00 / 0.90 | 1.00 / 0.90 |
| per-case agreement | | **100%** |
| wall time | 0.6 s | 31.8 s |

Same answers on every case. CrewAI adds roughly 150 ms of orchestration overhead per agent run,
which is noise next to real model latency but visible against the instant mock model.

## Benchmark

`scrutai eval` on the bundled 50-case benchmark (`benchmark/cases.jsonl`: 22 clean diffs, many of
them deliberate traps such as sinks in comments, constant commands, placeholder secrets, `safe_load`
and bound SQL parameters):

| metric | with critic | specialists only |
|---|---|---|
| precision | **1.00** | 0.77 |
| recall | **0.90** | 0.90 |
| false positives | **0** | 8 |
| clean diffs flagged | **0%** | 36% |

> **Read this honestly.** These numbers come from **mock mode**, a deterministic rule-based stand-in
> built to be noisy so the critic has something to remove. They prove the *pipeline*: routing, the
> debate, dedup and the scoring are wired correctly, and the critic removes noise without costing recall.
> They are not a claim about any LLM. Run `llm_mode: live` with `scrutai eval` to measure a real model
> on the same cases. The three misses are cases labelled as expected misses (data-flow SQL injection,
> path traversal, an off-by-one), kept so the numbers aren't flattering.

CI runs the benchmark as a regression gate (`--min-precision 0.95 --min-recall 0.8`).

## GitHub Action

```yaml
# .github/workflows/scrutai.yml
on: pull_request
permissions: { contents: read, pull-requests: write, security-events: write }
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { ref: "${{ github.event.pull_request.head.sha }}" }
      - uses: imkarthiknr/Scrutai@main
        with: { llm-mode: live, install-semgrep: "true", sarif-file: scrutai.sarif }
        env: { ANTHROPIC_API_KEY: "${{ secrets.ANTHROPIC_API_KEY }}" }
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with: { sarif_file: scrutai.sarif }
```

The Action posts **one summary comment** (edited in place on every push) and **one inline comment per
finding** on the exact line. Each inline comment carries a fingerprint that ignores line numbers, so a
finding that survives a push (even if it moved) is never posted twice. Inputs: `config`, `llm-mode`,
`post-comments`, `sarif-file`, `install-semgrep`, `fail-on-findings`. Output: `verdict`. A full example
is in [`examples/scrutai-workflow.yml`](examples/scrutai-workflow.yml).

## Configuration (`.scrutai.yml`)

All keys are optional; these are the defaults.

```yaml
enabled_agents: [security, correctness, tests, performance, style]
min_severity: low          # info | low | medium | high | critical
min_confidence: 0.6        # the critic's bar
max_critic_rounds: 2       # 1 disables the debate
max_agent_steps: 4         # ReAct budget per specialist
chunk_lines: 250           # added lines per chunk; 0 = whole diff at once
concurrency: 4             # parallel critic / defense calls
backends: {}               # per-agent framework: {security: crewai} or {"*": crewai}
token_budget: 200000       # hard cap per review; partial results are flagged
max_cost_usd: 0.0          # optional dollar cap (live); 0 disables
fail_on: high              # exit 1 (fail CI) at this severity
llm_mode: mock             # mock | live
routing: heuristic         # or llm: the router model may narrow the selection
semgrep: auto              # auto | off | required
semgrep_config: bundled    # offline ruleset, or e.g. p/default
tracing: none              # none | otel | langfuse
include: ["**/*"]
exclude: ["**/vendor/**", "**/*.lock", "**/dist/**"]
models:                    # any LiteLLM model string
  router: anthropic/claude-haiku-4-5
  specialist: anthropic/claude-sonnet-5-5
  critic: anthropic/claude-opus-5-5
```

Optional extras: `pip install "scrutai[web]"`, `"scrutai[crewai]"`, `"scrutai[semgrep]"`,
`"scrutai[otel]"`, `"scrutai[langfuse]"`.

## Library use

The CLI and the Action are thin consumers of one function:

```python
from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.diff import diff_from_git
from scrutai.llm import make_client

config = ScrutaiConfig.load(".scrutai.yml")
result = review_diff(diff_from_git("main", "HEAD"), config, make_client(config.llm_mode))
for f in result.findings:
    print(f.severity, f"{f.file}:{f.line}", f.title, f.critic_note)
```

## Development

```bash
uv sync --extra dev
uv run pytest -q            # 150+ tests incl. browser E2E, fully offline
cd web && npm ci && npm test && npm run build   # UI: vitest + bundle into the package
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

## Roadmap

- **v0.1**: CLI, ReAct specialists, critic, eval harness. ✅
- **v0.2**: GitHub Action (idempotent inline comments, SARIF), Semgrep, Performance/Style agents,
  tracing (JSONL / OpenTelemetry / Langfuse), cost budget guardrail, chunked parallel review. ✅
- **v0.3**: agent theater web UI (live + replay), CrewAI backend behind the `Specialist` seam,
  `eval --compare`. ✅
- **Next**: published live-model benchmark numbers; a Google ADK backend; Semgrep taint rules.

## License

Apache-2.0.
