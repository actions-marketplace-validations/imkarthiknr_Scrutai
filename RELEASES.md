# Releases

Every version of Scrutai, what it delivers, and what changed. Versions follow
[Semantic Versioning](https://semver.org/). While Scrutai is `0.x`, a minor version may change
behaviour; such changes are listed under **Changed**, with upgrade notes.

| Version | Date | Theme | Status |
|---|---|---|---|
| [0.4.0](#040) | 2026-10-03 | Ask for a review from any AI assistant | **Current** |
| [0.3.0](#030) | 2026-10-02 | See it work, swap the framework | Superseded |
| [0.2.0](#020) | 2026-10-01 | Ready for real pull requests | Superseded |
| [0.1.0](#010) | 2026-10-01 | The reviewing engine | Superseded |
| [0.0.0](#000-skeleton) | 2026-10-01 | Initial skeleton | Historical |

Install a released version from [PyPI](https://pypi.org/project/scrutai/) (`pip install scrutai==0.4.0`;
0.4.0 is the first version published there), or any commit from GitHub, e.g.
`pip install "scrutai @ git+https://github.com/imkarthiknr/Scrutai.git@main"`.

## Feature matrix

| Feature | 0.1 | 0.2 | 0.3 | 0.4 |
|---|:-:|:-:|:-:|:-:|
| Diff parsing with real line numbers, include/exclude globs | ✅ | ✅ | ✅ | ✅ |
| Routing (docs-only changes wake nobody) and optional LLM router | ✅ | ✅ | ✅ | ✅ |
| ReAct specialists over sandboxed tools (`read_file`, `grep`, `git_blame`) | ✅ | ✅ | ✅ | ✅ |
| Security, Correctness and Tests specialists | ✅ | ✅ | ✅ | ✅ |
| Performance and Style specialists | | ✅ | ✅ | ✅ |
| Adversarial critic: uphold / downgrade / kill / challenge | ✅ | ✅ | ✅ | ✅ |
| Debate: specialists defend or withdraw challenged findings | ✅ | ✅ | ✅ | ✅ |
| Unjudged findings withheld, never shipped | | ✅ | ✅ | ✅ |
| Benchmark harness with critic ablation | ✅ (38 cases) | ✅ (50 cases) | ✅ (50 cases) | ✅ (50 cases) |
| Semgrep evidence, bundled offline ruleset | | ✅ | ✅ | ✅ |
| Concurrent fan-out (LangGraph `Send`) | | ✅ | ✅ | ✅ |
| Chunked review with per-chunk routing | | ✅ | ✅ | ✅ |
| Token and dollar budget guardrails | | ✅ | ✅ | ✅ |
| Output: table, JSON, Markdown, SARIF | table, JSON | ✅ | ✅ | ✅ |
| `review --pr`, `--diff FILE` / stdin | `--diff` | ✅ | ✅ | ✅ |
| GitHub Action with idempotent PR comments | | ✅ | ✅ | ✅ |
| Tracing: JSONL, OpenTelemetry, Langfuse | | ✅ | ✅ | ✅ |
| Live trace events (run ids, start/end spans, findings, decisions) | | | ✅ | ✅ |
| Agent theater web UI (`scrutai serve`), live and replay | | | ✅ | ✅ |
| CrewAI backend, `eval --compare` | | | ✅ | ✅ |
| `grep` works without ripgrep, outside git repositories | | | ✅ | ✅ |
| MCP server over stdio and authenticated Streamable HTTP | | | | ✅ |
| MCP tools (review, results, explain, benchmark), resources and prompts | | | | ✅ |
| Post to a PR from an assistant, confirmed by the user | | | | ✅ |
| Review cancellation (partial result, nothing unjudged) | | | | ✅ |

---

## Unreleased

Changes on `main` that are not in a tagged version yet. Nothing yet.

---

## 0.4.0

**Released 2026-10-03 · "Ask for a review from any AI assistant"**

v0.4 turns Scrutai into an MCP server. Claude Code, Claude Desktop, Cursor and any other MCP client
can review a change, dig into a finding and, with the user's approval, post the review to the
pull request. The engine is the same one the CLI, the Action and the web UI use.

### Highlights
- **`scrutai mcp`** speaks MCP over **stdio** (local clients launch it) or **Streamable HTTP**
  (remote clients). Off localhost, HTTP requires a bearer token, and it checks `Host` headers
  against DNS rebinding.
- **Review and result tools:**
  - review a git range, a patch or a GitHub pull request;
  - list, fetch and explain results, including the critic's challenge, the specialist's defense
    and the code around the line;
  - run the benchmark.
  Every tool returns structured output, and reviews report live progress and can be cancelled.
- **Posting is opt-in twice over:** a separate destructive `post_review` tool that asks the user
  first, and that `--no-post` removes entirely.
- **Works with MCP Python SDK 1.28+ and 2.x**, including the 2026-07-28 protocol's input-required
  round trips. CI runs the MCP tests on both.

### Added
- `scrutai mcp [--transport stdio|http] [--host] [--port] [--root]... [--allowed-host]...
  [--max-concurrent] [--no-post] [--benchmark] [--config]`. Extra: `scrutai[mcp]`.
- **Tools:**
  - `review_patch`, `review_git_range`, `review_pull_request` (all take `wait=false` to return a
    `review_id` at once);
  - `get_review`, `list_reviews`, `explain_finding`;
  - `post_review`;
  - `run_benchmark` (`compare_backend`, `limit`).
- **Resources:** `scrutai://reviews/{id}` (JSON) plus `/report.md`, `/sarif` and `/trace`;
  `scrutai://agents`; `scrutai://config` (secrets redacted).
- **Prompts:** `review-my-branch`, `fix-finding`, `security-audit`.
- **Security for MCP clients:**
  - repository paths are confined to `--root` (symlinks resolved);
  - patch, file-count and diff-size caps;
  - the HTTP token is read from `SCRUTAI_MCP_TOKEN` only, must be 16+ characters, and is
    compared in constant time;
  - in-progress reviews are capped.
- **Library hooks:**
  - `review_diff(..., cancel=threading.Event())` and `CancellableClient`;
  - `ReviewResult.cancelled`;
  - `scrutai.progress.ProgressListener` (trace events → progress);
  - `scrutai.inputs.prepare(Source)`: one input path for every front end;
  - `scrutai.runs.RunStore`, shared by the web and MCP servers;
  - `eval.harness.run_benchmark(..., on_case=, limit=)`.
- `docs/MCP.md` (reference) and `examples/mcp/` (Claude Code, Claude Desktop and Cursor configs).

### Changed
- **A partial review (out of budget or cancelled) is never an approval:** its verdict is at most
  `comment`.
- The trace's `result` event is emitted after the budget and cancellation flags are set, so it
  reports them correctly.

### Packaging
- **On PyPI:** `pip install scrutai`, the first version published there.
- The benchmark ships inside the package, so `scrutai eval` and the MCP `run_benchmark` tool work
  after a plain `pip install`, from any directory. The cases moved from `benchmark/cases.jsonl`
  to `src/scrutai/eval/cases.jsonl`; `--benchmark` now defaults to them.
- PyPI metadata: project links, classifiers and a `py.typed` marker.
- The source archive holds only what building and testing need (238 KB instead of 21.8 MB).
- Releases are automated: pushing a `v*` tag builds, checks and smoke-tests the package, then
  publishes it to TestPyPI and PyPI through trusted publishing, and creates the GitHub release.
  A new `package` CI job runs the built wheel outside the repository on every change.

### Documentation
- The README shows Scrutai at work: a screen recording of the agent theater (GIF and MP4) and
  screenshots of `review`, `eval`, the theater's trial board and an MCP client session, all
  captured from real runs by `scripts/capture_media.py`. Its links are absolute, so they also
  work on the PyPI project page.
- `examples/mcp/try_it.py`: a scripted MCP client to try `scrutai mcp` without an AI client.
- Bring-your-own-key, documented:
  - the README's requirements table;
  - "Running with a real model" with each provider's model string and environment variable,
    including Windows syntax;
  - where the key goes for the CLI, Action, MCP server and library;
  - a "who pays" FAQ entry.
  DEVELOPMENT.md covers optional live testing for contributors.
- A full README rewrite, `DEVELOPMENT.md` (the contributor guide) and this `RELEASES.md`, which
  replaces `CHANGELOG.md`. DEVELOPMENT.md gains an "Add an MCP tool" recipe.

### Upgrade notes
- No breaking changes for the CLI, the Action or configs.
- If you call `review_diff()` directly, `cancel` is a new optional keyword; results gain a
  `cancelled` field.

---

## 0.3.0

**Released 2026-10-02 · "See it work, swap the framework"**

v0.3 makes Scrutai's reasoning visible and proves its framework seam is real.

### Highlights
- **Agent theater.** `scrutai serve` opens a React UI where you start a review (demo, git range,
  pasted patch or PR) and watch it happen. The graph shows which agents run on which chunk, and the
  trial board follows each finding from *On trial* to *Upheld* or *Killed*, with the critic's
  challenge, the specialist's defense and the final ruling. Any finished run can be replayed or
  scrubbed event by event, and any `--trace` file plays back like a live run.
- **CrewAI backend.** Put any specialist on CrewAI with `backends: {security: crewai}` (or
  `{"*": crewai}`). CrewAI runs the agent loop; Scrutai's sandboxed tools and its own model client
  (budgets, tracing, mock mode) stay in place.
- **`scrutai eval --compare crewai`** runs the benchmark on both backends. Result in mock mode:
  identical precision and recall, 100% per-case agreement, about 150 ms of extra orchestration per
  agent run.

### Added
- `scrutai serve` (FastAPI + Server-Sent Events): start runs, stream events, resume with
  `Last-Event-ID`, replay traces (`POST /api/replay` or `--replay FILE`). Binds to `127.0.0.1` by
  default. Extra: `scrutai[web]`.
- React 19 + TypeScript UI, shipped prebuilt inside the Python package (no Node needed to run
  it): review graph, trial board, live stats, run history, trace upload, 1×/4×/16× playback,
  scrubbing, light and dark themes, phone-friendly layout.
- `Specialist._loop`: one overridable seam for the ReAct loop, shared by `review()` and `defend()`.
- `CrewAISpecialist`, `BridgeLLM` and CrewAI tool adapters. Extra: `scrutai[crewai]`.
- Richer traces: a run id and sequence number on every event, span **start** events, and new
  `plan`, `finding`, `defense` and `result` events.
- The `backends` config option.

### Fixed
- **Per-call token counts in traces were wrong under concurrency:** parallel calls
  double-counted each other. Clients now report exact per-thread usage.
- **Traces reported defended findings as "withdrawn".**
- **Git refs are validated** everywhere, so a ref such as `--output=/tmp/x` can never be read by
  `git diff` as an option. This mattered once the web server accepted refs from a browser.
- **`grep` went blind without ripgrep outside a git repository** (e.g. GitHub's runners reviewing a
  scratch directory), which caused false `missing_tests` findings. It now falls back to `git grep`,
  then to a pure-Python search.

### Upgrade notes
- No breaking changes. Existing configs keep working; unlisted agents run on the native backend.
- Custom specialists that copied the old loop code from `review()` should override `_loop()`
  instead.

---

## 0.2.0

**Released 2026-10-01 · "Ready for real pull requests"**

v0.2 takes Scrutai from a command-line engine to something a team can put on every pull request.

### Highlights
- **GitHub Action** with **idempotent** reviews: one summary comment edited in place, plus inline
  comments that carry fingerprints, so a finding is never posted twice, even when the code moves.
  SARIF output for GitHub code scanning.
- **Semgrep evidence:** a rule firing on the exact line is the strongest evidence a security
  finding can carry. A bundled ruleset works offline.
- **Built for real diffs:** concurrent agents, chunked review with per-chunk routing, and hard
  token and dollar budgets.

### Added
- `action.yml` (composite Action): inputs `config`, `llm-mode`, `post-comments`, `sarif-file`,
  `install-semgrep`, `fail-on-findings`, `python-version`; output `verdict`; a report in the job
  summary. Example workflow in `examples/scrutai-workflow.yml`.
- `scrutai review --pr N [--post] [--github-repo owner/name]`.
- Output formats: `--format table|json|markdown|sarif`, `--output FILE`, `--show-dropped`.
- **Performance** specialist (N+1 queries, regex compiled in a loop, string `+=` in a loop,
  sorting to take a min or max) and **Style** specialist (debug leftovers, wildcard imports,
  untracked TODOs).
- Semgrep integration: seeded evidence for the security agent plus a `semgrep` tool; offline
  ruleset verified against Semgrep 1.178. Config: `semgrep`, `semgrep_config`. Extra:
  `scrutai[semgrep]`.
- Concurrent fan-out via LangGraph `Send`; parallel critic and defense calls (`concurrency`).
- Chunked review (`chunk_lines`), with each chunk routed on its own.
- `token_budget` and `max_cost_usd` guardrails; `cost_usd` and `budget_exhausted` in results.
- Tracing: `--trace FILE` (JSONL), `tracing: otel`, `tracing: langfuse`. Extras: `scrutai[otel]`,
  `scrutai[langfuse]`.
- `grep` gains a path `glob` filter.
- The benchmark grows to 50 cases, adding performance and style cases and their traps.

### Changed
- **Findings the critic could not judge are now withheld** (marked `unjudged`) instead of shipping
  on the specialist's own confidence.
- **Default models** are now `anthropic/claude-haiku-4-5` (router),
  `anthropic/claude-sonnet-5-5` (specialists) and `anthropic/claude-opus-5-5` (critic).
- Live calls no longer send `temperature`, which current Claude models reject.
- All five specialists are enabled by default.
- Invalid config values (unknown agents, modes, backends) fail fast with exit code `2`.

### Fixed
- Found by running Scrutai on its own code:
  - protocol markers inside reviewed code could confuse the mock model;
  - code inside triple-quoted strings was flagged;
  - `except` blocks that re-raise were flagged as swallowing errors;
  - test lookups were truncated by unrelated grep hits.

### Upgrade notes
- Set `models:` explicitly if you relied on the old defaults (`gpt-4o-mini` and
  `claude-3-5-sonnet-latest`).
- To keep the v0.1 agent set, set `enabled_agents: [security, correctness, tests]`.

---

## 0.1.0

**Released 2026-10-01 · "The reviewing engine"**

v0.1 turns the skeleton into a working reviewer: grounded specialists, a critic with a real
debate, and a benchmark that measures it.

### Highlights
- **The critic and the debate.** Every finding is judged against the cited code. Challenged
  findings go back to their specialist to defend with fresh evidence or withdraw.
- **Specialists grounded in the repository** through a real ReAct loop over sandboxed tools.
- **A benchmark that keeps everyone honest:** precision and recall with and without the critic,
  deliberate traps, and labelled expected misses.

### Added
- Unified-diff parsing with true new-file line numbers, multi-file `git diff` output,
  added/modified/deleted/renamed/binary status, and `include`/`exclude` globs.
- `scrutai review --diff FILE|-`.
- Routing by file kind and added-line risk surface; docs, lockfile and asset-only changes wake
  nobody. Optional `routing: llm`, which can only narrow the heuristic selection.
- ReAct specialists (Security, Correctness, Tests) with `read_file`, `grep` and `git_blame`. File
  paths are confined to the repository, and patterns are passed as data.
- Findings carry a file, line, **category**, severity, confidence and evidence; deduplication by
  `(file, line, category)`.
- The critic's four decisions (uphold, downgrade, kill, challenge), the `defend` step, and
  `max_critic_rounds`. Killed findings are kept in `ReviewResult.dropped` with the reason.
- A deterministic offline mock model that speaks the full protocol (`llm_mode: mock`).
- `scrutai eval`: hermetic per-case repositories, category scoring, clean-diff false-positive
  rate, critic ablation, `--min-precision` / `--min-recall` gates, a Markdown report; 38 cases.
- CI: ruff, `mypy --strict`, pytest and the benchmark gate.

### Fixed
- A bad git ref used to yield an empty diff and a "clean" review; it is now an error (exit `2`).
- The summary named an arbitrary finding as "most severe".

---

## 0.0.0 (skeleton)

**2026-10-01 · Initial skeleton**

The starting point: the package layout, the LangGraph graph outline (route, specialists,
critic, verdict), single-shot specialist stubs, a mock LLM, a 4-case benchmark and the design
documents. Not usable for real reviews.
