"""`scrutai` command line.

scrutai review --base main            # review working branch vs main
scrutai review --demo                 # run on the bundled sample diff
scrutai eval                          # run the benchmark, print precision/FPR
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import ScrutaiConfig
from .diff import DiffError, apply_filters, diff_from_file, diff_from_git
from .llm import make_client
from .models import ChangedFile, DiffContext, ReviewResult
from .orchestrator import review_diff

app = typer.Typer(add_completion=False, help="Multi-agent code review with an adversarial critic.")
console = Console()

_DEMO_DIFF = DiffContext(
    files=[
        ChangedFile(
            path="app/runner.py",
            patch=(
                "+def run(cmd):\n"
                "+    import os\n"
                "+    try:\n"
                "+        return os.system(cmd)\n"
                "+    except Exception:\n"
                "+        return -1\n"
            ),
        )
    ]
)


def _render(result: ReviewResult) -> None:
    console.print(
        f"[bold]Verdict:[/bold] {result.verdict.value}   "
        f"[dim]rounds={result.rounds} tokens={result.tokens_used}[/dim]"
    )
    console.print(f"[italic]{result.summary}[/italic]\n")
    if not result.findings:
        return
    table = Table(show_lines=False)
    for col in ("Severity", "Agent", "Conf", "File:Line", "Finding"):
        table.add_column(col)
    for f in result.findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        table.add_row(f.severity.value, f.agent, f"{f.confidence:.2f}", loc, f.title)
    console.print(table)


@app.command()
def review(
    base: str = typer.Option("main", help="Base ref to diff against."),
    head: str = typer.Option("HEAD", help="Head ref."),
    repo: str = typer.Option(".", help="Repo root."),
    demo: bool = typer.Option(False, help="Review a bundled sample diff instead of git."),
    diff_file: str | None = typer.Option(
        None, "--diff", help="Review a unified diff file instead of git ('-' reads stdin)."
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
) -> None:
    config = ScrutaiConfig.load(config_path)
    llm = make_client(config.llm_mode)
    try:
        if demo:
            diff = _DEMO_DIFF
        elif diff_file:
            diff = diff_from_file(diff_file, repo_root=repo)
        else:
            diff = diff_from_git(base, head, repo)
    except DiffError as exc:
        console.print(f"[red]Could not build diff:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    diff = apply_filters(diff, config.include, config.exclude)
    result = review_diff(diff, config, llm)

    if output_json:
        console.print_json(result.model_dump_json())
    else:
        _render(result)

    if any(f.severity.rank >= config.fail_on.rank for f in result.findings):
        raise typer.Exit(code=1)


@app.command("eval")
def eval_cmd(
    benchmark: str = typer.Option("benchmark/cases.jsonl", help="Labeled cases."),
    config_path: str = typer.Option(".scrutai.yml", "--config"),
) -> None:
    from .eval.harness import run_benchmark

    if not Path(benchmark).exists():
        console.print(f"[red]No benchmark at {benchmark}[/red]")
        raise typer.Exit(code=1)
    metrics = run_benchmark(benchmark, ScrutaiConfig.load(config_path))
    console.print_json(json.dumps(metrics))


if __name__ == "__main__":
    app()
