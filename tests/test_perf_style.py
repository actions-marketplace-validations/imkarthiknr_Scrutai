from __future__ import annotations

import pytest
from pydantic import ValidationError

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient
from scrutai.mock import SrcLine, enclosing_loop
from scrutai.models import ChangedFile, DiffContext


def _review(patch: str, path: str = "lib/mod.py", agents: list[str] | None = None) -> list[str]:
    diff = DiffContext(files=[ChangedFile(path=path, patch=patch)])
    cfg = ScrutaiConfig(enabled_agents=agents or ["performance", "style"])
    return [f.category for f in review_diff(diff, cfg, MockLLMClient()).findings]


def _lines(*texts: str) -> list[SrcLine]:
    return [SrcLine("a.py", "code", i + 1, t) for i, t in enumerate(texts)]


def test_enclosing_loop_walks_up_indentation() -> None:
    lines = _lines("for a in xs:", "    if a:", "        call(a)", "x = 1")
    loop = enclosing_loop(lines, 2)
    assert loop is not None and loop.line == 1
    assert enclosing_loop(lines, 3) is None


def test_function_boundary_stops_the_search() -> None:
    lines = _lines("for a in xs:", "    def inner():", "        call()")
    assert enclosing_loop(lines, 2) is None


def test_query_in_loop_vs_hoisted() -> None:
    in_loop = "+for uid in ids:\n+    cur.execute(Q, (uid,))\n"
    hoisted = "+rows = cur.execute(Q)\n+for r in rows:\n+    out.append(r)\n"
    assert _review(in_loop) == ["n_plus_one"]
    assert _review(hoisted) == []


def test_print_in_cli_is_fine_but_not_in_library() -> None:
    assert _review('+print("hi")\n', path="lib/pricing.py") == ["debug_leftover"]
    assert _review('+print("hi")\n', path="src/app/cli.py") == []
    assert _review('+print("hi")\n', path="scripts/backfill.py") == []


def test_todo_is_info_and_filtered_by_default() -> None:
    assert _review("+x = 1  # TODO make faster\n") == []
    diff = DiffContext(files=[ChangedFile(path="m.py", patch="+x = 1  # TODO make faster\n")])
    cfg = ScrutaiConfig(enabled_agents=["style"], min_severity="info")
    (f,) = review_diff(diff, cfg, MockLLMClient()).findings
    assert f.category == "untracked_todo"
    ticketed = DiffContext(files=[ChangedFile(path="m.py", patch="+x = 1  # TODO(#42) faster\n")])
    assert review_diff(ticketed, cfg, MockLLMClient()).findings == []


@pytest.mark.parametrize(
    "bad",
    [{"enabled_agents": ["security", "linting"]}, {"llm_mode": "turbo"}, {"routing": "vibes"}],
)
def test_config_rejects_unknown_values(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ScrutaiConfig.model_validate(bad)


def test_default_config_matches_shipped_yaml() -> None:
    assert ScrutaiConfig() == ScrutaiConfig.load(".scrutai.yml")
