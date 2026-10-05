# Developing Scrutai

Thanks for wanting to make Scrutai better. This guide gets you from a fresh clone to a merged pull
request: setup, how the code fits together, step-by-step recipes for the most common changes, how
the tests work, and what reviewers look for.

New here? Read [README.md](README.md) first for *what* Scrutai does, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for *why* it is built the way it is.

## Contents

- [Ways to contribute](#ways-to-contribute)
- [Setup](#setup)
- [Running the checks](#running-the-checks)
- [The architecture in five minutes](#the-architecture-in-five-minutes)
- [Code map](#code-map)
- [Recipes](#recipes)
  - [Add a specialist agent](#add-a-specialist-agent)
  - [Add a repository tool](#add-a-repository-tool)
  - [Add a Semgrep rule](#add-a-semgrep-rule)
  - [Add benchmark cases](#add-benchmark-cases)
  - [Add a framework backend](#add-a-framework-backend)
  - [Add or change a trace event](#add-or-change-a-trace-event)
  - [Work on the web UI](#work-on-the-web-ui)
  - [Add an MCP tool](#add-an-mcp-tool)
  - [Update the README's screenshots and recording](#update-the-readmes-screenshots-and-recording)
- [Testing guide](#testing-guide)
- [Conventions](#conventions)
- [Pull requests](#pull-requests)
- [Releasing](#releasing)
- [Debugging tips](#debugging-tips)
- [Getting help](#getting-help)

## Ways to contribute

- **Benchmark cases:** the most valuable contribution. Especially *traps*: code that looks like a
  bug but isn't, so the critic is forced to prove itself. See
  [Add benchmark cases](#add-benchmark-cases).
- **Semgrep rules** for the bundled offline ruleset, ideally for languages beyond Python.
- **New specialists** (e.g. accessibility, i18n, infrastructure-as-code) or new categories for
  existing ones.
- **Framework backends** (e.g. Google ADK) behind the `Specialist._loop` seam.
- **UI improvements** to the agent theater.
- **Bug reports** with a failing diff attached: `scrutai review --diff bug.patch --trace bug.jsonl`
  gives us everything we need to reproduce it.

## Setup

**You need:** Python 3.12+, `git`, and Node.js 22+ (only for the web UI). Optional:
[`uv`](https://docs.astral.sh/uv/) (faster installs), ripgrep, Semgrep.

```bash
git clone https://github.com/imkarthiknr/Scrutai.git
cd Scrutai

# Python: with uv (recommended)...
uv venv --python 3.12
uv pip install -e ".[dev]"
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# ...or with plain pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Browser for the end-to-end UI tests (skipped automatically if missing)
playwright install chromium

# Optional: run lint and format checks on every commit
pre-commit install

# Web UI (only if you work on it)
cd web && npm ci && cd ..
```

The `dev` extra installs everything the test suite exercises: pytest, ruff, mypy, the web server,
Playwright, OpenTelemetry, CrewAI and the MCP SDK. CrewAI pins the MCP SDK to 1.28, so the
main environment tests MCP on 1.x; CI tests it on 2.x as well (see below).

Check that it works:

```bash
scrutai review --demo   # should report "5 issue(s) upheld" and exit with code 1
pytest -q               # should report 200+ passed
```

You never need an API key to develop. Everything, including the whole test suite and CI, runs
against the offline mock model, so no contributor or CI run spends money or needs a secret.

**Optional: trying a change against a real model.** Use your own provider key (see the README's
[Running with a real model](README.md#running-with-a-real-model-bring-your-own-key) for providers
and variable names):

```bash
export ANTHROPIC_API_KEY=...                   # your key, in your shell only
scrutai review --demo --config my-live.yml     # a copy of .scrutai.yml with llm_mode: live
```

- Keep live configs and keys out of commits: put keys only in the environment, and set a
  `max_cost_usd` cap in live configs.
- Never add a test that needs a real key. Use `MockLLMClient`, a scripted client, or LiteLLM's
  `mock_response` (see the [testing guide](#testing-guide)).
- When a PR changes prompts or the critic, say in its description whether you checked it live,
  with which model.

## Running the checks

CI runs exactly these. Run them before you push:

```bash
# Python job
ruff check .
ruff format --check .
mypy src                                            # strict mode
pytest -q                                           # incl. browser E2E if Chromium is installed
scrutai eval --min-precision 0.95 --min-recall 0.8  # the benchmark gate

# MCP job: the MCP tests again, on MCP SDK 2.x (a separate venv: CrewAI pins 1.x)
uv venv .venv-mcp2 --python 3.12
uv pip install --python .venv-mcp2 -e . "mcp>=2" pytest httpx pyyaml
.venv-mcp2/bin/pytest -q tests/test_mcp_*.py

# Web job (from web/)
npm run typecheck
npm test                                            # vitest
npm run build && git diff --exit-code -- ../src/scrutai/web/static   # bundle must be committed
```

Useful variations:

```bash
pytest -q tests/test_critic.py           # one file
pytest -q -k "debate or withdraw"        # by name
pytest -q --ignore=tests/test_ui_e2e.py    # skip browser tests for a fast loop
ruff check . --fix && ruff format .      # auto-fix lint and formatting
```

## The architecture in five minutes

Everything goes through one function, `review_diff(diff, config, llm)` in
`src/scrutai/orchestrator.py`. The CLI, the GitHub Action, the web server and the benchmark are
thin wrappers around it.

```text
DiffContext ──► route ──► specialist × (agent, chunk) ──► collect ──► critic ⇄ defend ──► verdict ──► ReviewResult
                 │               │                           │           │
             router.py      agents/*.py                  dedupe()    critic.py
                            (ReAct over tools/)
```

A review goes like this:

1. **Diff in.** `diff.py` turns a git range, a patch or a PR into a `DiffContext` (a list of
   `ChangedFile`s with raw patches). `patch.py` does the pure parsing: added lines with real
   line numbers, slicing, globs.
2. **Route.** `router.py` decides which specialists a change needs, using file kinds and the "risk
   surface" of added lines. The orchestrator splits the diff into chunks (`chunk_diff`) and routes
   each chunk on its own.
3. **Fan out.** LangGraph `Send` starts one branch per (agent, chunk). Each branch builds a
   specialist and calls `review()`.
4. **ReAct.** `Specialist.review()` (in `agents/base.py`) builds a prompt, then calls `_loop()`: the
   model answers with either a tool action (`{"action": {...}}`) or findings
   (`{"findings": [...]}`). Tools run through a sandboxed `Toolbox` (`tools/`).
5. **Collect.** Findings from all branches are sorted and deduplicated by `(file, line, category)`.
6. **Critic.** `critic.py` judges each finding against the cited code: uphold, downgrade, kill or
   challenge. Challenged findings go to `defend` (the specialist's `defend()`), then back to the
   critic. `max_critic_rounds` bounds the loop.
7. **Verdict.** Survivors become `ReviewResult.findings`, everything else goes to `dropped` with
   the reason.

Three cross-cutting pieces wrap every LLM call:

- **`BudgetedClient`** (`llm.py`) enforces `token_budget` and `max_cost_usd`.
- **`TracingClient`** (`trace.py`) records spans when a tracer is active.
- **`MockLLMClient`** (`mock.py`) speaks the exact same protocol offline, which is what makes the
  whole system testable without a model.

**The protocol is JSON, line-anchored.** Reviewed code always appears as `L<n>: <code>`, and protocol
markers (`--- STEP`, `--- FINAL`, `OBSERVATION:`) are only recognised at the start of a line. That
way, a diff that happens to *contain* those strings (Scrutai reviewing itself, for example) can't
spoof the protocol.

## Code map

| Path | Responsibility |
|---|---|
| `src/scrutai/cli.py` | `review`, `serve`, `mcp` and `eval` commands; output formats; exit codes. |
| `src/scrutai/config.py` | `ScrutaiConfig`: every option, defaults and validation. |
| `src/scrutai/models.py` | Pydantic models: `ChangedFile`, `DiffContext`, `Finding`, `ReviewResult`. |
| `src/scrutai/diff.py` | Git and patch input, ref validation, include/exclude filters, chunking. |
| `src/scrutai/patch.py` | Pure unified-diff parsing; no I/O. |
| `src/scrutai/router.py` | Heuristic and optional LLM routing. |
| `src/scrutai/orchestrator.py` | The LangGraph graph and `review_diff()`. |
| `src/scrutai/agents/base.py` | `Specialist`: prompts, the `_loop` seam, finding parsing, `defend()`. |
| `src/scrutai/agents/<name>.py` | One specialist each: role, categories, tools, file kinds. |
| `src/scrutai/agents/crewai_backend.py` | The CrewAI backend: tool adapters and the protocol bridge. |
| `src/scrutai/critic.py` | The critic prompt, decisions, and per-round judging. |
| `src/scrutai/tools/` | `repo.py` (the functions), `toolbox.py` (registry + sandbox), `semgrep.py`. |
| `src/scrutai/llm.py` | `LiteLLMClient`, `BudgetedClient`, `extract_json`, `make_client`. |
| `src/scrutai/mock.py` | The offline model: specialist rules, the mock critic, mock defenses. |
| `src/scrutai/report.py` | Markdown and SARIF rendering; finding fingerprints are in `models.py`. |
| `src/scrutai/github.py` | GitHub REST client; idempotent `publish()`. |
| `src/scrutai/trace.py` | Tracer, spans, events, OpenTelemetry and Langfuse hooks. |
| `src/scrutai/eval/harness.py` | Benchmark loading, hermetic case runs, metrics, `--compare`. |
| `src/scrutai/inputs.py` | `Source` + `prepare()`: the one input path (demo, git range, patch, file, PR) every front end uses. |
| `src/scrutai/runs.py` | `Run` / `RunStore`: reviews in flight and finished, shared by the web and MCP servers. |
| `src/scrutai/progress.py` | `ProgressListener`: trace events → "step N of M" progress. |
| `src/scrutai/mcp/server.py` | The MCP server: tools, resources, prompts; `Reviewer` runs and keeps reviews. |
| `src/scrutai/mcp/compat.py` | The only module that imports the MCP SDK; hides 1.x vs 2.x differences. |
| `src/scrutai/mcp/schemas.py` | Structured tool output (`ReviewSummary`, `FindingExplanation`, …). |
| `src/scrutai/mcp/security.py` | Root allowlist, input caps, HTTP bearer-token and `Host` guard. |
| `src/scrutai/web/server.py` | FastAPI app: runs, SSE event stream, replay. |
| `src/scrutai/web/static/` | **Built** UI bundle (generated; never edit by hand). |
| `web/src/` | The React UI source: `reduce.ts` (all UI state), `components/`, `api.ts`. |
| `action.yml` | The composite GitHub Action. |
| `src/scrutai/eval/cases.jsonl` | The labelled benchmark (shipped in the package). |

## Recipes

### Add a specialist agent

Example: an `accessibility` agent for front-end code.

1. **Create `src/scrutai/agents/accessibility.py`:**

   ```python
   from __future__ import annotations

   from typing import ClassVar

   from .base import Specialist


   class AccessibilityAgent(Specialist):
       name = "accessibility"
       role = "You find accessibility problems in UI code: missing labels, alt text, focus traps."
       categories: ClassVar[dict[str, str]] = {
           "missing_alt_text": "an <img> without meaningful alt text",
           "unlabelled_control": "an input or button with no accessible name",
       }
       tools: ClassVar[list[str]] = ["read_file", "grep"]
       kinds = ("code",)  # which ChangedFile.kind values this agent sees
   ```

2. **Register it** in `src/scrutai/agents/__init__.py`: add it to `REGISTRY` and `__all__`.
3. **Route it** in `src/scrutai/router.py`: add an entry to the `wants` dict in
   `heuristic_route()`. Without one, the agent runs on every reviewable change.
4. **Enable it by default** (optional): add it to `enabled_agents` in `config.py` **and**
   `.scrutai.yml`. `test_default_config_matches_shipped_yaml` keeps the two in sync.
5. **Teach the mock** in `src/scrutai/mock.py`: add `Rule(...)`s with your agent name, so the agent
   does something offline. If a rule needs the critic to kill a known false-positive pattern, add
   that check in `MockLLMClient._critic`.
6. **Add benchmark cases** for each category, including at least one trap (see below).
7. **Show it in the UI:** add the name to `ALL_AGENTS` in `web/src/reduce.ts`, then rebuild the UI.
8. **Test it:** a unit test in the style of `tests/test_perf_style.py`, and make sure
   `scrutai eval` still passes the gate.

### Add a repository tool

1. Write the function in `src/scrutai/tools/repo.py`. Treat **every argument as hostile**: confine
   paths with `_confine()`, pass patterns as data (`-e PATTERN`, never as a flag), bound the output.
2. Register it in `TOOLS` in `src/scrutai/tools/toolbox.py` with a `Tool(name, signature,
   description, run)`. The signature and description go into every prompt that offers the tool.
3. Allow it on the agents that should use it (their `tools` class variable).
4. Add an argument schema to `_SCHEMAS` in `src/scrutai/agents/crewai_backend.py`, so CrewAI can
   validate calls.
5. Test it directly, *and* test that a model can't escape the sandbox through it (see
   `test_read_file_is_confined_to_repo`).

### Add a Semgrep rule

1. Add the rule to `src/scrutai/rules/semgrep.yml`. Use the id `scrutai.<category>.<short-name>`:
   the category becomes the finding category automatically.
2. Use `pattern-not` to exclude the safe forms (constant arguments, safe loaders, explicit opt-outs).
3. Verify it against the real binary on positives *and* traps:

   ```bash
   pip install semgrep
   semgrep scan --json --metrics=off --config src/scrutai/rules/semgrep.yml path/to/samples.py
   ```

4. Tests use a fake `semgrep` binary (see the `fake_semgrep` fixture in `tests/test_semgrep.py`), so
   CI doesn't need Semgrep installed.

### Add benchmark cases

Append JSON lines to `src/scrutai/eval/cases.jsonl`:

```json
{"id": "trap-secret-env", "file": "settings.py", "patch": "+API_KEY = os.environ[\"API_KEY\"]\n", "labels": [], "note": "read from the environment"}
```

- `labels` is a **multiset of categories** a correct reviewer reports; `[]` means the change is clean.
- `repo` (optional) adds other files to the case's temporary repository, e.g. an existing test that
  should stop a `missing_tests` finding.
- Prefer **traps**: realistic code that a naive reviewer would flag.
- If the mock model can't catch something a good reviewer should, keep the case and label it
  honestly; it becomes a documented expected miss (ids starting with `miss-`). **Never tune the
  mock to the benchmark, and never delete a case to raise a number.**

Run `scrutai eval --report eval.md` and read the "Cases with errors" table.

### Add a framework backend

Specialists share everything except one method: `Specialist._loop(system, transcript, toolbox,
answer_keys, final_note)`, which runs the ReAct loop and returns the answer payload, or `None`.

1. Write a mixin that overrides `_loop` (see `CrewAIMixin` in `agents/crewai_backend.py`). Drive
   your framework's agent with the given `system` prompt and `transcript`, expose
   `toolbox.allowed` as the framework's tools (each calling `toolbox.run(name, args)`), and return
   a dict containing one of `answer_keys`.
2. Reach the model through `self.llm` (Scrutai's client) rather than letting the framework call a
   provider directly. That keeps budgets, cost tracking, tracing and mock mode working, and keeps
   `--compare` fair.
3. Add the backend name to `BACKENDS` and to `agent_class()` in `agents/__init__.py`, and add an
   optional extra in `pyproject.toml`.
4. Prove parity: `scrutai eval --compare <backend>` should report 100% per-case agreement in mock
   mode. Copy the structure of `tests/test_crewai_backend.py`.

### Add or change a trace event

Trace events feed `--trace` files, OpenTelemetry and the agent theater.

1. Emit it with `emit("kind", **fields)` (a point event) or `span("kind", name, ...)` (start/end
   pair) from `scrutai.trace`.
2. Add its shape to the `TraceEvent` union in `web/src/types.ts` and handle it in `apply()` in
   `web/src/reduce.ts`.
3. **Regenerate the recorded fixture** that the UI tests run on. The file is opened in append mode,
   so delete it first:

   ```bash
   rm web/src/__fixtures__/demo.jsonl
   scrutai review --demo --trace web/src/__fixtures__/demo.jsonl
   ```

4. Update the event-kind assertions in `tests/test_trace.py`.

### Work on the web UI

```bash
scrutai serve                 # terminal 1: the API on http://127.0.0.1:8765
cd web && npm run dev         # terminal 2: Vite dev server with hot reload; /api is proxied
```

- **All UI state comes from `reduce(events)`** in `web/src/reduce.ts`, a pure function. Put logic
  there, test it in `reduce.test.ts`, and keep components presentational.
- The production bundle is built into `src/scrutai/web/static/` and **committed**, so
  `pip install` users don't need Node. After any UI change, run `npm run build` and commit the
  result. CI fails if the committed bundle doesn't match the source; the build is deterministic.
- Support light and dark themes (CSS variables in `styles.css`), keyboard focus, and phone widths:
  the page must never scroll horizontally. `tests/test_ui_e2e.py` checks the last point.

### Add an MCP tool

The MCP server is a thin adapter, so a new tool is usually a few lines in `build_server()` in
`src/scrutai/mcp/server.py`.

1. **Register it with the typed helpers** from `compat.py`:
   `@tool(server, name=..., title=..., description=..., annotations=..., structured_output=True)`.
   Never import `mcp.server` anywhere else: `compat.py` keeps 1.x and 2.x working.
2. **Return a Pydantic model** from `schemas.py`, so clients get an output schema.
3. **Declare honest annotations** with `tool_annotations(...)`. Anything that writes outside the
   server is `destructive=True` and must ask the user with `ask_user()` (see `post_review`).
4. **Trust no argument.**
   - Resolve paths with `reviewer.roots.resolve()`.
   - Cap sizes.
   - Raise `ToolError` with a clear message for bad input.
   - Inside a resource or prompt, raise `ResourceError` or `invalid_params()` instead: mcp 2 hides
     the text of other exceptions.
5. **Keep blocking work off the event loop:** `await anyio.to_thread.run_sync(...,
   abandon_on_cancel=True)`, and honour cancellation (see `Reviewer.review`).
6. **Test it through a real client.** Use `in_memory()` from `tests/mcp_util.py`, which works on
   both SDK majors and can answer elicitation. Add a case to `tests/test_mcp_*.py`, and run the
   MCP job above so it also passes on mcp 2.
7. **Document it** in the tools table in `docs/MCP.md`.

### Update the README's screenshots and recording

The README's images and screen recording are captured from real runs in mock mode. After a change
to the CLI output or the UI, regenerate them:

```bash
python scripts/capture_media.py   # needs the dev extra, Chromium for Playwright, and ffmpeg
```

The script writes `docs/images/*.png`, `theater.gif` and `theater.mp4`. Check the result by eye
before committing; keep the GIF under about 5 MB.

## Testing guide

The test suite runs **fully offline** and never calls a real model. The main tools:

| Need | Use |
|---|---|
| A model that behaves realistically | `MockLLMClient` (`scrutai.llm`): deterministic, speaks the real protocol, deliberately noisy. |
| A model that says exactly what a test needs | A small scripted client with `complete()`, `tokens_used` and `cost_usd`; see `Scripted` in `tests/test_agents.py` and `Critic` in `tests/test_critic.py`. |
| A real git repository | The `git_repo` fixture in `tests/conftest.py`: pass `{path: content}`, get a repo with a `feature` branch over `main`. |
| Semgrep without installing it | The `fake_semgrep` fixture in `tests/test_semgrep.py`. |
| GitHub without the network | The `github` fixture (`FakeGitHub` in `tests/fake_github.py`): an in-process HTTP server; also runs the Action's real shell step. |
| An MCP client | `tests/mcp_util.py`: `in_memory(server, elicit=...)` on either SDK major, plus `over_http()` for a real `scrutai mcp` process; see `tests/test_mcp_transports.py`. |
| The live LiteLLM path | `tests/test_live_client.py`: LiteLLM's `mock_response` builds real response objects. |
| The UI in a browser | `tests/test_ui_e2e.py`: a real server and Chromium; skipped if no browser. Set `SCRUTAI_CHROMIUM` to use a specific binary. |
| UI state logic | `web/src/reduce.test.ts` (vitest), against a trace recorded from the real backend. |

Ground rules:

- **Every bug fix gets a regression test** that fails before the fix.
- **Never skip, disable or loosen a test to get green.** If a test is wrong, fix the test and say why
  in the commit message.
- **Keep tests deterministic.** Concurrency is real (agents run in parallel), so assert on sorted or
  set results, never on completion order.
- **Hermetic paths.** Use `tmp_path` or `git_repo`, never the working directory: the agents' tools
  search whatever repository they are pointed at.

## Conventions

- **Python 3.12+**, fully typed: `mypy --strict` must pass. Use modern syntax: `X | None`, PEP 695
  generics, `StrEnum`.
- **ruff** for lint and formatting (line length 100). Don't fight the formatter.
- **Comments explain *why*,** not what. Match the density of the code around you.
- **Model output is untrusted input.** Anything a model returns (tool arguments, file paths,
  JSON) is validated before use. Tool paths are confined to the repository; patterns are data.
- **Nothing leaves the machine** except calls to the configured model provider. No telemetry; turn
  off third-party telemetry when integrating a library.
- **Honest numbers.** README and benchmark claims must be reproducible with a command, and mock-mode
  results must be labelled as such.
- **Dependencies:** core dependencies stay small. Anything heavy goes behind an optional extra
  and is imported lazily.

## Pull requests

1. **Branch from `main`**, and keep each commit to one logical change. We use
   [Conventional Commits](https://www.conventionalcommits.org/): `feat(critic): ...`,
   `fix(tools): ...`, `docs: ...`, `test: ...`, `chore: ...`. The body says *why*.
2. **Run [the checks](#running-the-checks)** locally.
3. **Open a PR** describing what changed, why, and how you tested it. Include before/after output for
   behaviour changes, and a screenshot for UI changes.

Checklist:

- [ ] `ruff check`, `ruff format --check`, `mypy src` and `pytest` pass.
- [ ] `scrutai eval --min-precision 0.95 --min-recall 0.8` passes.
- [ ] New behaviour has tests; bug fixes have a regression test.
- [ ] UI changes: `npm run typecheck`, `npm test`, and the rebuilt bundle is committed.
- [ ] Trace event changes: types, reducer and fixture updated.
- [ ] Docs updated (README, config reference, `.scrutai.yml`) when user-facing behaviour changes.
- [ ] An entry under **Unreleased** in [RELEASES.md](RELEASES.md).

CI must be green before merge. Reviewers look hardest at the critic, the tool sandbox and anything
that changes benchmark numbers.

## Releasing

Versions follow [Semantic Versioning](https://semver.org/). While Scrutai is `0.x`, a minor version
may change behaviour; RELEASES.md calls those changes out.

1. Move the **Unreleased** notes in [RELEASES.md](RELEASES.md) under a new version heading
   (`## 0.X.0`; the release workflow copies that section into the GitHub release).
2. Bump the version in all three places: `pyproject.toml`, `src/scrutai/__init__.py` and
   `web/package.json` (`cd web && npm version 0.X.0 --no-git-tag-version`). The README's PyPI
   badge updates itself; update the Action example's `uses: imkarthiknr/Scrutai@v0.X.0` by hand.
3. Rebuild the UI (`cd web && npm run build`) and commit the bundle.
4. Merge to `main` with CI green (the `package` job builds the wheel and runs it outside the
   repository), then tag the release: `git tag v0.X.0 && git push origin v0.X.0`.

The tag starts `.github/workflows/release.yml`:

1. It checks that the tag matches `pyproject.toml`'s version.
2. It builds the wheel and sdist, runs `twine check`, and smoke-tests the wheel in a clean
   environment.
3. It publishes to **TestPyPI**, then to **PyPI**, through trusted publishing (no API tokens
   exist).
4. It creates the GitHub release with the built files attached.

Then list that release on the GitHub Marketplace (a manual step; the API cannot do it): open the
release, choose **Edit**, tick **Publish this Action to the GitHub Marketplace**, check the
categories (*Code review*, *Code quality*) and choose **Update release**. The first time, GitHub
asks the owner to accept the Marketplace Developer Agreement; two-factor authentication must be on.

To rehearse without publishing to PyPI, run the workflow manually (Actions → release → Run
workflow): that run stops after TestPyPI. A version number can be uploaded to PyPI only once,
even if it is deleted later.

One-time setup, done by the PyPI project owner: on pypi.org and test.pypi.org add a trusted
publisher with owner `imkarthiknr`, repository `Scrutai`, workflow `release.yml`, and
environment `pypi` or `testpypi` respectively.

## Debugging tips

- **See everything a review did:** `scrutai review ... --trace run.jsonl`, then open it with
  `scrutai serve --replay run.jsonl`, or read the JSONL directly (one event per line, with `seq`).
- **See what the critic killed:** `--show-dropped`, or the `dropped` array in `--json` output; each
  entry has `critic_note` and a per-round `history`.
- **A finding is missing:** check routing first. The `plan` event in the trace lists which agents ran
  on which chunk.
- **The mock model is behaving strangely on a real diff:** remember it is a rule-based stand-in. If
  reviewed code contains protocol-looking text, check that markers are matched line-anchored.
- **Reproduce CI without ripgrep:** CI runners don't have it, so `grep` falls back to `git grep` or
  pure Python. Run tests with a `PATH` that hides `rg` to check that fallback.

## Getting help

Open a [GitHub issue](https://github.com/imkarthiknr/Scrutai/issues) for bugs and proposals, or a
draft pull request early if you want feedback on an approach. For security issues, use
[private advisories](https://github.com/imkarthiknr/Scrutai/security/advisories/new) instead of a
public issue.
