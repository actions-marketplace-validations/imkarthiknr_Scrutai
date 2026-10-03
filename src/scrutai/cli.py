"""`scrutai` command line.

scrutai review --base main                # review working branch vs main
scrutai review --demo                     # run on the bundled sample diff
scrutai review --diff pr.patch -f sarif   # review a patch file, emit SARIF
scrutai eval --min-precision 0.9          # run the benchmark as a CI gate
scrutai mcp --transport http --root ~/src  # serve reviews to MCP clients
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .config import ScrutaiConfig
from .diff import DiffError
from .github import GitHubError, publish
from .inputs import Source, prepare
from .llm import make_client
from .models import ReviewResult
from .orchestrator import review_diff
from .report import location, to_markdown, to_sarif_json
from .tools.semgrep import available as semgrep_available
from .trace import Tracer, enable_langfuse

app = typer.Typer(add_completion=False, help="Multi-agent code review with an adversarial critic.")
console = Console()


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


def _make_tracer(config: ScrutaiConfig, path: str | None) -> Tracer | None:
    if config.tracing == "langfuse" and config.llm_mode == "live":
        enable_langfuse()
    if path is None and config.tracing != "otel":
        return None
    try:
        return Tracer(jsonl=path, otel=config.tracing == "otel")
    except (RuntimeError, OSError) as exc:
        console.print(f"[red]Tracing:[/red] {exc}")
        raise typer.Exit(code=2) from exc


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
    trace: str | None = typer.Option(None, help="Write a JSONL trace of every step here."),
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
    tracer = _make_tracer(config, trace)
    if demo:
        source = Source(kind="demo")
    elif pr is not None:
        source = Source(kind="pr", pr=pr, github_repo=github_repo or "")
    elif diff_file:
        source = Source(kind="file", path=diff_file)
    else:
        source = Source(kind="git", base=base, head=head)
    try:
        with prepare(source, config, repo) as prepared:
            result = review_diff(prepared.diff, config, make_client(config.llm_mode), tracer)
            if post and prepared.github is not None and prepared.pull is not None:
                try:
                    rep = publish(prepared.github, prepared.pull, result, prepared.diff)
                except GitHubError as exc:
                    console.print(f"[red]Could not post review:[/red] {exc}")
                    raise typer.Exit(code=2) from exc
                console.print(
                    f"[dim]PR #{pr}: summary {rep.summary}, {rep.posted} new inline "
                    f"comment(s), {rep.skipped_duplicates} already posted, "
                    f"{rep.skipped_off_diff} off-diff.[/dim]"
                )
    except (DiffError, GitHubError) as exc:
        console.print(f"[red]Could not build diff:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if tracer is not None:
        tracer.close()
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


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind (keep it local)."),
    port: int = typer.Option(8765, help="Port."),
    repo: str = typer.Option(".", help="Repo root for git-range and PR runs."),
    replay: str | None = typer.Option(None, help="Preload a --trace JSONL file as a run."),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
) -> None:
    """Open the agent theater: start reviews and watch the panel work, live."""
    _load_config(config_path)  # fail fast on a bad config
    try:
        import uvicorn

        from .runs import Run, RunStore, load_trace
        from .web.server import create_app
    except ImportError as exc:
        console.print('[red]The web UI needs extras:[/red] pip install "scrutai[web]"')
        raise typer.Exit(code=2) from exc

    store = RunStore()
    if replay:
        try:
            events = load_trace(Path(replay).read_text())
        except (OSError, ValueError) as exc:
            console.print(f"[red]Cannot replay {replay}:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        run = Run(id=str(events[0].get("run", "replay")), source="replay", label=Path(replay).name)
        for e in events:
            run.append(e)
        run.status = "done"
        store.add(run)
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            "[yellow]Warning:[/yellow] binding beyond localhost exposes your repo and API budget."
        )
    console.print(f"Scrutai theater on [bold]http://{host}:{port}[/bold]  (Ctrl+C to stop)")
    uvicorn.run(create_app(config_path, repo, store), host=host, port=port, log_level="warning")


class Transport(StrEnum):
    stdio = "stdio"
    http = "http"


@app.command("mcp")
def mcp_cmd(
    transport: Transport = typer.Option(Transport.stdio, help="stdio (local) or http (remote)."),
    host: str = typer.Option("127.0.0.1", help="HTTP: interface to bind."),
    port: int = typer.Option(8000, help="HTTP: port. The endpoint is /mcp."),
    root: list[str] = typer.Option(
        [], "--root", help="Directory tools may read (repeatable; default: the current one)."
    ),
    allowed_host: list[str] = typer.Option(
        [], "--allowed-host", help="HTTP: extra Host header to accept, e.g. a proxy's name."
    ),
    max_concurrent: int = typer.Option(2, help="Reviews allowed to run at once."),
    no_post: bool = typer.Option(
        False, "--no-post", help="Do not offer post_review: the server never writes to GitHub."
    ),
    benchmark: str = typer.Option(
        "benchmark/cases.jsonl", help="Labelled cases for the run_benchmark tool."
    ),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
) -> None:
    """Serve Scrutai over the Model Context Protocol (Claude Code, Claude Desktop, Cursor...).

    Over HTTP, set SCRUTAI_MCP_TOKEN to require a bearer token; it is mandatory
    when binding beyond localhost.
    """
    _load_config(config_path)  # fail fast on a bad config
    try:
        from .mcp.security import SecurityError
        from .mcp.server import Settings, http_application, run_stdio
    except ImportError as exc:
        console.print('[red]The MCP server needs an extra:[/red] pip install "scrutai[mcp]"')
        raise typer.Exit(code=2) from exc

    err = Console(stderr=True)  # stdout carries the protocol on stdio
    settings = Settings(
        config_path=config_path,
        roots=root or ["."],
        max_concurrent=max_concurrent,
        allow_post=not no_post,
        benchmark=benchmark,
    )
    try:
        if transport is Transport.stdio:
            run_stdio(settings)
            return
        app_ = http_application(settings, host, port, allowed_host)
    except SecurityError as exc:
        err.print(f"[red]Refusing to start:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    import uvicorn

    if host in ("0.0.0.0", "::") and not allowed_host:
        err.print(
            "[yellow]Note:[/yellow] only localhost Host headers are accepted; add "
            "--allowed-host NAME for each name remote clients use to reach this server."
        )
    auth = "bearer token required" if app_.expected else "no auth (localhost only)"
    err.print(f"Scrutai MCP on [bold]http://{host}:{port}/mcp[/bold]  ({auth}; Ctrl+C to stop)")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


@app.command("eval")
def eval_cmd(
    benchmark: str = typer.Option("benchmark/cases.jsonl", help="Labeled cases (JSONL)."),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
    min_precision: float = typer.Option(0.0, help="Exit 1 if precision falls below this."),
    min_recall: float = typer.Option(0.0, help="Exit 1 if recall falls below this."),
    report: str | None = typer.Option(None, help="Also write a markdown report here."),
    output_json: bool = typer.Option(False, "--json", help="Emit metrics as JSON."),
    compare: str | None = typer.Option(
        None, help="Also run every agent on this backend (e.g. crewai) and compare."
    ),
) -> None:
    """Run the labeled benchmark and report precision / recall / critic lift."""
    from .eval.harness import (
        CaseResult,
        compare_backends,
        compare_markdown,
        markdown_report,
        run_benchmark,
    )

    if not Path(benchmark).exists():
        console.print(f"[red]No benchmark at {benchmark}[/red]")
        raise typer.Exit(code=2)
    if compare:
        try:
            comparison = compare_backends(benchmark, _load_config(config_path), ["native", compare])
        except (ValueError, RuntimeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        md = compare_markdown(comparison)
        if report:
            Path(report).write_text(md)
        if output_json:
            typer.echo(json.dumps(comparison, indent=2))
        else:
            console.print(Markdown(md))
        bad = [
            f"{b}: precision {m['precision']} recall {m['recall']}"
            for b, m in comparison["backends"].items()
            if m["precision"] < min_precision or m["recall"] < min_recall
        ]
        if bad:
            console.print(f"[red]Benchmark gate failed:[/red] {'; '.join(bad)}")
            raise typer.Exit(code=1)
        return
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
