# Security policy

Scrutai reads source code, runs tools over a repository, calls model providers with your
credentials, and can post to GitHub. Security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Report it privately through
[GitHub security advisories](https://github.com/imkarthiknr/Scrutai/security/advisories/new).

Include what you can of:

- the affected version (`scrutai --version`, or `pip show scrutai`) and how Scrutai was run (CLI,
  GitHub Action, `scrutai serve`, `scrutai mcp`, library);
- the steps or a minimal diff, config or request that reproduces it;
- what an attacker gains.

Never include real API keys, tokens or private code. Redact them.

**What to expect:** an acknowledgement within 7 days and an assessment within 14. Fixes for
confirmed issues ship in a patch release, with credit in [RELEASES.md](RELEASES.md) unless you
prefer otherwise. Please give us a reasonable time to release a fix before disclosing publicly.

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/scrutai/) receives security fixes. Scrutai
is `0.x` (beta), so upgrade to the newest version before reporting.

## In scope

Anything where untrusted input makes Scrutai do something its user did not intend. Untrusted input
includes the reviewed diff and repository, model output, PR data from GitHub, and requests to the
web or MCP server. For example:

- **Escaping the repository sandbox:** a model or a diff reads or lists files outside the reviewed
  repository through `read_file`, `grep`, `git_blame` or `semgrep`, or passes options into them.
- **Git ref or argument injection:** for example, a ref that `git` reads as an option.
- **MCP server:**
  - reaching a path outside `--root`;
  - bypassing the bearer token or the `Host` check;
  - `post_review` writing to GitHub without the user's confirmation, or while disabled with
    `--no-post`.
- **Leaking credentials:** provider keys, `GITHUB_TOKEN` or `SCRUTAI_MCP_TOKEN` appearing in
  output, traces, reports, resources or PR comments.
- **The web UI (`scrutai serve`):** cross-site or DNS-rebinding attacks that let a web page start
  reviews or read results.
- **The GitHub Action:** a pull request from a fork that makes it post as the repository or leak
  secrets.

## Out of scope

- **Findings Scrutai misses or gets wrong.** Wrong or missed findings are quality issues, not
  vulnerabilities: please open a
  [false-positive](https://github.com/imkarthiknr/Scrutai/issues/new?template=false_positive.yml)
  or [missed-issue](https://github.com/imkarthiknr/Scrutai/issues/new?template=missed_issue.yml)
  report.
- **Prompt injection that only changes the review's content,** for example code that persuades a
  model to call it safe. Scrutai treats the code as data and the critic checks findings against
  the code, but no reviewer built on a language model can rule this out. Do not rely on Scrutai as
  your only security control.
- **Running the web or MCP server on a public network** without a bearer token and a TLS proxy,
  against the documented setup.
- **Costs from a live model** within the configured `token_budget` and `max_cost_usd`.
- **Vulnerabilities in dependencies** (LiteLLM, LangGraph, the MCP SDK, …) that Scrutai does not
  make worse. Please report those upstream; tell us if Scrutai should pin around them.

## How Scrutai is designed to be safe

The design is described in the README's
[Security and privacy](README.md#security-and-privacy) section and in
[docs/MCP.md](docs/MCP.md#security-model).
