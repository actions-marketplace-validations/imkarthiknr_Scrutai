"""The shared input path every front end (CLI, web, MCP) uses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scrutai.config import ScrutaiConfig
from scrutai.diff import DiffError
from scrutai.inputs import Source, prepare
from scrutai.runs import Run, RunNotFound, RunStore


def test_demo_lives_only_inside_the_context() -> None:
    with prepare(Source(kind="demo"), ScrutaiConfig()) as p:
        root = Path(p.diff.repo_root)
        assert (root / "app/runner.py").is_file()
        assert p.diff.paths == ["app/runner.py"]
    assert not root.exists()  # the throwaway repo is cleaned up


def test_git_patch_and_file_sources(
    git_repo: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = git_repo({"svc.py": "x = 1\n", "vendor/lib.py": "y = 2\n"})
    cfg = ScrutaiConfig()
    with prepare(Source(kind="git", base="main"), cfg, str(repo)) as p:
        assert p.diff.paths == ["svc.py"]  # vendor/ excluded by default globs
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+eval(x)\n"
    with prepare(Source(kind="patch", patch=patch), cfg) as p:
        assert p.diff.paths == ["a.py"] and p.github is None
    f = tmp_path / "c.diff"
    f.write_text(patch)
    with prepare(Source(kind="file", path=str(f)), cfg) as p:
        assert p.diff.paths == ["a.py"]


@pytest.mark.parametrize(
    "source",
    [
        Source(kind="patch", patch="   "),
        Source(kind="pr", pr=0),
        Source(kind="git", base="--output=/tmp/x"),
        Source(kind="file", path="/does/not/exist.diff"),
    ],
)
def test_invalid_sources_raise_diff_error(source: Source) -> None:
    with pytest.raises(DiffError), prepare(source, ScrutaiConfig()):
        pass


def test_labels() -> None:
    assert Source(kind="git", base="main", head="feat").label() == "main...feat"
    assert Source(kind="pr", pr=7).label() == "PR #7"


def test_run_store_evicts_finished_runs_only() -> None:
    store = RunStore()
    running = store.add(Run(id="live", source="demo", label="x"))
    for i in range(60):
        r = Run(id=f"r{i}", source="demo", label="x")
        r.status = "done"
        store.add(r)
    assert store.get("live") is running  # never evicted while running
    assert len(store.all()) == 50
    with pytest.raises(RunNotFound):
        store.get("r0")
