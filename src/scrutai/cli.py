"""`scrutai` command line.

scrutai review --base main                # review working branch vs main
scrutai review --demo                     # run on the bundled sample diff
scrutai review --diff pr.patch -f sarif   # review a patch file, emit SARIF
scrutai eval --min-precision 0.9          # run the benchmark as a CI gate
"""

from __future__ import annotations

import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import ScrutaiConfig
from .diff import DiffError, apply_filters, diff_from_file, diff_from_git, parse_diff
from .github import GitHubClient, GitHubError, PullRequest, detect_repo, publish
from .llm import make_client
from .models import ChangedFile, DiffContext, ReviewResult
from .orchestrator import review_diff
from .report import location, to_markdown, to_sarif_json
from .tools.semgrep import available as semgrep_available

app = typer.Typer(add_completion=False, help="Multi-agent code review with an adversarial critic.")
console = Console()

# The demo: real issues plus planted noise, so the critic has something to kill.
_DEMO_FILE = "app/runner.py"
_DEMO_SOURCE = """\
import os


def run(cmd, env={}):
    # never pass user input to os.system(...) unescaped
    try:
        return os.system(cmd)
    except Exception:
        print("failed", cmd)
        return -1
"""


class Format(StrEnum):
    table = "table"
    json = "json"
    markdown = "markdown"
    sarif = "sarif"


def _render_table(result: ReviewResult, show_dropped: bool) -> None:
    console.print(
        f"[bold]Verdict:[/bold] {result.verdict.value}   "
        f"[dim]agents={','.join(result.agents) or '-'} rounds={result.rounds} "
        f"tokens={result.tokens_used} cost=${result.cost_usd:.4f}[/dim]"
    )
    console.print(f"[italic]{result.summary}[/italic]")
    if result.budget_exhausted:
        console.print("[yellow]Budget exhausted: this review is partial.[/yellow]")
    if result.dropped and not show_dropped:
        console.print(
            f"[dim]The critic dropped {len(result.dropped)} finding(s); "
            "--show-dropped to see them.[/dim]"
        )
    console.print()
    if result.findings:
        table = Table(show_lines=False)
        for col in ("Severity", "Agent", "Conf", "File:Line", "Finding"):
            table.add_column(col)
        for f in result.findings:
            table.add_row(f.severity.value, f.agent, f"{f.confidence:.2f}", location(f), f.title)
        console.print(table)
    if show_dropped and result.dropped:
        table = Table(title="Dropped by the critic", show_lines=False)
        for col in ("Agent", "File:Line", "Finding", "Why"):
            table.add_column(col)
        for f in result.dropped:
            table.add_row(f.agent, location(f), f.title, f.critic_note or "")
        console.print(table)


def _emit(result: ReviewResult, fmt: Format, output: str | None, show_dropped: bool) -> None:
    if fmt == Format.table and not output:
        _render_table(result, show_dropped)
        return
    if fmt == Format.json:
        text = result.model_dump_json(indent=2)
    elif fmt == Format.sarif:
        text = to_sarif_json(result)
    else:  # markdown (also what --output gets when the format is "table")
        text = to_markdown(result, show_dropped)
    if output:
        Path(output).write_text(text + ("" if text.endswith("\n") else "\n"))
        console.print(f"[dim]Wrote {fmt.value} report to {output}[/dim]")
    else:
        typer.echo(text)


def _review_demo(config: ScrutaiConfig) -> ReviewResult:
    # A throwaway repo so the agents' tools see the demo file, not your cwd.
    with tempfile.TemporaryDirectory(prefix="scrutai-demo-") as root:
        target = Path(root) / _DEMO_FILE
        target.parent.mkdir(parents=True)
        target.write_text(_DEMO_SOURCE)
        patch = "".join(f"+{line}\n" for line in _DEMO_SOURCE.splitlines())
        diff = DiffContext(repo_root=root, files=[ChangedFile(path=_DEMO_FILE, patch=patch)])
        return review_diff(diff, config, make_client(config.llm_mode))


@app.command()
def review(
    base: str = typer.Option("main", help="Base ref to diff against."),
    head: str = typer.Option("HEAD", help="Head ref."),
    repo: str = typer.Option(".", help="Repo root."),
    demo: bool = typer.Option(False, help="Review a bundled sample diff instead of git."),
    diff_file: str | None = typer.Option(
        None, "--diff", help="Review a unified diff file instead of git ('-' reads stdin)."
    ),
    fmt: Format = typer.Option(Format.table, "--format", "-f", help="Output format."),
    output_json: bool = typer.Option(False, "--json", help="Shorthand for --format json."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write the report to a file."),
    show_dropped: bool = typer.Option(False, help="Also list findings the critic killed."),
    pr: int | None = typer.Option(None, "--pr", help="Review a GitHub pull request by number."),
    post: bool = typer.Option(False, help="With --pr: post the summary and inline comments."),
    github_repo: str | None = typer.Option(
        None, help="owner/name for --pr (default: GITHUB_REPOSITORY or the origin remote)."
    ),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
) -> None:
    """Review a git range, a patch file, a GitHub PR, or the bundled demo.

    Exit codes: 0 ok, 1 a finding at or above fail_on, 2 usage/diff/API error.
    """
    config = _load_config(config_path)
    if output_json:
        fmt = Format.json
    if post and pr is None:
        console.print("[red]--post needs --pr[/red]")
        raise typer.Exit(code=2)
    gh: tuple[GitHubClient, PullRequest] | None = None
    if demo:
        result = _review_demo(config)
    else:
        try:
            if pr is not None:
                client = GitHubClient(
                    os.environ.get("GITHUB_TOKEN", ""),
                    github_repo or detect_repo(repo),
                    os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                )
                pull = client.pull_request(pr)
                diff = parse_diff(client.pull_request_diff(pr), repo_root=repo, head=pull.head_sha)
                gh = (client, pull)
            elif diff_file:
                diff = diff_from_file(diff_file, repo_root=repo)
            else:
                diff = diff_from_git(base, head, repo)
        except (DiffError, GitHubError) as exc:
            console.print(f"[red]Could not build diff:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        diff = apply_filters(diff, config.include, config.exclude)
        result = review_diff(diff, config, make_client(config.llm_mode))
        if gh is not None and post:
            try:
                rep = publish(gh[0], gh[1], result, diff)
            except GitHubError as exc:
                console.print(f"[red]Could not post review:[/red] {exc}")
                raise typer.Exit(code=2) from exc
            console.print(
                f"[dim]PR #{pr}: summary {rep.summary}, {rep.posted} new inline comment(s), "
                f"{rep.skipped_duplicates} already posted, {rep.skipped_off_diff} off-diff.[/dim]"
            )

    _emit(result, fmt, output, show_dropped)
    if any(f.severity.rank >= config.fail_on.rank for f in result.findings):
        raise typer.Exit(code=1)


def _load_config(path: str) -> ScrutaiConfig:
    try:
        config = ScrutaiConfig.load(path)
    except (ValueError, OSError) as exc:  # pydantic ValidationError is a ValueError
        console.print(f"[red]Invalid config {path}:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if config.semgrep == "required" and not semgrep_available():
        console.print("[red]semgrep: required by config but not installed[/red]")
        raise typer.Exit(code=2)
    return config


@app.command("eval")
def eval_cmd(
    benchmark: str = typer.Option("benchmark/cases.jsonl", help="Labeled cases (JSONL)."),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
    min_precision: float = typer.Option(0.0, help="Exit 1 if precision falls below this."),
    min_recall: float = typer.Option(0.0, help="Exit 1 if recall falls below this."),
    report: str | None = typer.Option(None, help="Also write a markdown report here."),
    output_json: bool = typer.Option(False, "--json", help="Emit metrics as JSON."),
) -> None:
    """Run the labeled benchmark and report precision / recall / critic lift."""
    from .eval.harness import CaseResult, markdown_report, run_benchmark

    if not Path(benchmark).exists():
        console.print(f"[red]No benchmark at {benchmark}[/red]")
        raise typer.Exit(code=2)
    details: list[CaseResult] = []
    try:
        metrics = run_benchmark(benchmark, _load_config(config_path), details)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    md = markdown_report(metrics, details)
    if report:
        Path(report).write_text(md)
    if output_json:
        typer.echo(json.dumps(metrics, indent=2))
    else:
        console.print(Markdown(md))

    failed = []
    if metrics["precision"] < min_precision:
        failed.append(f"precision {metrics['precision']} < {min_precision}")
    if metrics["recall"] < min_recall:
        failed.append(f"recall {metrics['recall']} < {min_recall}")
    if failed:
        console.print(f"[red]Benchmark gate failed:[/red] {'; '.join(failed)}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
