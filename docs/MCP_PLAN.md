# Scrutai MCP server: implementation plan

Status: **shipped in 0.4.0** (2026-10-03). This document records the design and the decisions behind
it; [`docs/MCP.md`](MCP.md) is the user-facing reference.

## Goal

Expose Scrutai as a [Model Context Protocol](https://modelcontextprotocol.io) server, so any MCP
client (Claude Code, Claude Desktop, Cursor, other agents) can ask for a review and get Scrutai's
critic-vetted findings back as structured data, then explain them, fix them, or post them to a PR.

```text
MCP client (Claude Code / Desktop / Cursor / agents)
        │  Streamable HTTP (remote, authenticated)   or   stdio (local)
        ▼
scrutai mcp  ── src/scrutai/mcp/ (thin adapter: tools · resources · prompts)
        │
        ▼
review_diff()   ← the same engine the CLI, GitHub Action, web UI and benchmark use
```

## Decisions

| Question | Decision |
|---|---|
| Which clients first? | **Remote access early.** Streamable HTTP with authentication ships in the same milestone as stdio and the first tools, not at the end. |
| May the server post to GitHub? | **Yes, only through a separate `post_review` tool**, marked destructive, confirmed by the user before posting, and switchable off with `--no-post`. Reviewing never writes anything. |
| Workflow | A feature branch, one commit per milestone, then a pull request into `main` with CI. |

## Constraints found by checking the real SDK

1. **The current MCP Python SDK is 2.3.0**, which renamed `FastMCP` to `MCPServer`. The decorator API
   (`@tool` with annotations and structured output, `Context.report_progress`, `Context.elicit`,
   Streamable HTTP) has the same shape in 1.x and 2.x.
2. **CrewAI pins `mcp~=1.28.1`**, so `mcp>=2` and `scrutai[crewai]` can't be installed together.
3. **Semgrep 1.178 pins `mcp==1.29.0`.** This already clashes with CrewAI's pin: with both extras
   installed, the resolver falls back to Semgrep 1.79. That is a separate, existing issue.

**Therefore:** code against the shared decorator API, behind a one-line compatibility import
(`MCPServer` when available, otherwise `FastMCP`), and declare `mcp = ["mcp>=1.28"]`. CI tests on
both majors.

## What the server exposes

### Tools

| Tool | Input | Output | Annotations |
|---|---|---|---|
| `review_patch` | unified diff text | structured `ReviewSummary` | read-only, open-world (model provider) |
| `review_git_range` | `base`, `head`, `repo` (inside an allowed root) | `ReviewSummary` | read-only, open-world |
| `review_pull_request` | `pr`, `repo_slug?` | `ReviewSummary`; never posts | read-only, open-world |
| `post_review` | `review_id` | comments created / updated / skipped | **destructive**, confirmed by the user, can be disabled |
| `explain_finding` | `review_id`, `finding_id` | evidence, critic history, code around the line | read-only |
| `get_review` / `list_reviews` | `review_id` / paging | full result / summaries | read-only |
| `run_benchmark` | `compare_backend?` | metrics, with progress | read-only |

Review tools also take `wait: bool = true`. With `wait=false` they return a `review_id` at once and
the client polls `get_review`, which avoids client-side timeouts on big diffs. Every tool returns
structured data plus a short text summary.

### Resources

| URI | Content |
|---|---|
| `scrutai://reviews/{id}` | the full result as JSON |
| `scrutai://reviews/{id}/report.md` | the Markdown report the GitHub Action posts |
| `scrutai://reviews/{id}/sarif` | SARIF 2.1.0 |
| `scrutai://reviews/{id}/trace` | the JSONL trace (replayable in the agent theater) |
| `scrutai://agents` | the specialists, their categories and tools |
| `scrutai://config` | the effective config, secrets redacted |

### Prompts

`review-my-branch`, `fix-finding`, `security-audit`.

### Progress and cancellation

A tracer listener turns Scrutai's existing `plan` and per-agent events into MCP progress
notifications. A client cancel request sets a flag that a `CancellableClient` (modelled on
`BudgetedClient`) honours, so a cancelled review ends as a *partial* result, never a half-trusted one.

## Security model

1. **Path allowlist:** `repo` must resolve inside a `--root`; git refs go through `validate_ref`.
2. **Posting is separate and confirmed:** `post_review` is destructive, asks the user to confirm,
   and can be disabled with `--no-post`. The GitHub token comes only from the server's environment.
3. **Locked-down HTTP:** bound to 127.0.0.1 by default. Binding anywhere else requires a bearer
   token (`SCRUTAI_MCP_TOKEN`). Tokens are compared in constant time, and `Host` headers are checked
   against DNS rebinding.
4. **Untrusted code stays data:** code from diffs appears only in structured fields, and tool
   descriptions say that finding content is data, not instructions.
5. **Bounded inputs and outputs:** caps on patch size and file count; truncated evidence; paging.
6. **Budgets on every call,** plus an optional total budget per server process. Mock mode remains the
   default.

## Repository edit plan

| File | Change |
|---|---|
| `src/scrutai/inputs.py` (new) | One `prepare(Source)` for demo / git / patch / PR, shared by the CLI, the web server and MCP. |
| `src/scrutai/runs.py` (new) | `Run` / `RunStore` moved out of `web/server.py`, shared by web and MCP. |
| `src/scrutai/llm.py` | `CancellableClient`. |
| `src/scrutai/orchestrator.py` | `review_diff(..., cancel=None)`. |
| `src/scrutai/mcp/` (new) | `compat.py`, `server.py`, `schemas.py`, `security.py`. |
| `src/scrutai/cli.py` | `scrutai mcp [--transport http\|stdio] [--host] [--port] [--root]... [--no-post] [--config]`. |
| `src/scrutai/web/server.py` | Uses the shared `inputs.py` / `runs.py`; no API change. |
| `pyproject.toml` | Extra `mcp = ["mcp>=1.28"]`, added to `dev`. |
| `tests/test_mcp_*.py` (new) | In-memory client tests, plus real stdio and HTTP end-to-end tests. |
| `.github/workflows/ci.yml` | An extra job that runs the MCP tests on `mcp>=2`. |
| `docs/MCP.md`, `examples/mcp/` (new) | Reference and ready-to-paste client configs. |
| `README.md`, `DEVELOPMENT.md`, `RELEASES.md` | A README section, an "Add an MCP tool" recipe, release notes; version 0.4.0. |

## Milestones (one commit each)

1. **Plan:** this document.
2. **Refactor:** shared `inputs.py` and `runs.py`; no behaviour change.
3. **Core hooks:** cancellation and a progress listener.
4. **Server + remote access:** `scrutai mcp` on stdio **and** Streamable HTTP with bearer auth and
   host checks; `review_patch`, `review_git_range`; structured output; root allowlist.
5. **Results:** `get_review`, `list_reviews`, `explain_finding`, and the resources.
6. **GitHub:** `review_pull_request`, plus `post_review` with confirmation and `--no-post`.
7. **Benchmark:** `run_benchmark` with progress.
8. **Prompts.**
9. **Release:** `docs/MCP.md`, client examples, the CI job on `mcp>=2`, version 0.4.0.

## Testing

- **Discovery:** the tool, resource and prompt lists, annotations and output schemas match this
  spec.
- **Reviewing:** each review tool returns the demo's known findings (mock mode, offline); progress
  arrives in order; a cancelled review is partial with nothing unjudged.
- **Security:**
  - a `repo` outside the roots, a ref like `--output=…`, or an oversized patch is rejected;
  - `post_review` writes nothing without confirmation or with `--no-post`;
  - the config resource contains no secrets;
  - HTTP rejects missing or wrong tokens and unexpected `Host` headers.
- **GitHub:** `post_review` against the in-process fake GitHub API stays idempotent.
- **Both SDK majors:** the suite runs on `mcp` 1.x and 2.x.

## What changed during implementation

- **mcp 2 dropped server-to-client requests** on protocol 2026-07-28, so `ctx.elicit()` can't
  confirm a post there. `compat.ask_user()` uses elicitation on older protocols and an
  *input-required* round trip on the new one. Clients that can't ask at all must pass
  `confirm=true`.
- **mcp 2 hides the text of unexpected exceptions** in resources and prompts. Missing reviews
  are raised as `ResourceError` or `MCPError` there, so the client sees the reason.
- **mcp 1.x silently ignores snake_case `ToolAnnotations` keywords.** Annotations are built from
  their wire names.
- **A cancelled review is kept as a partial result** (`cancelled: true`) rather than as an error,
  so `get_review` can still show what was found.
- **Extra guards found while building:**
  - `wait=false` reviews are capped (5 × `--max-concurrent` in progress);
  - `post_review` refuses when the PR has new commits since the review;
  - a live `run_benchmark` asks the user first;
  - the benchmark file is fixed by the operator (`--benchmark`).
