"""End-to-end CLI behaviour: formats, exit codes, real git repos."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scrutai.cli import app
from scrutai.models import Finding

runner = CliRunner()


def test_demo_table_exits_1_on_high_finding() -> None:
    res = runner.invoke(app, ["review", "--demo"])
    assert res.exit_code == 1
    assert "request_changes" in res.output and "dropped 1 finding" in res.output


def test_demo_json_is_valid_and_complete() -> None:
    res = runner.invoke(app, ["review", "--demo", "--json"])
    data = json.loads(res.output)
    assert data["verdict"] == "request_changes"
    assert {f["category"] for f in data["findings"]} >= {"injection", "broad_except"}
    assert data["dropped"][0]["critic_note"].startswith("Pattern only appears")


def test_sarif_shape(tmp_path: Path) -> None:
    out = tmp_path / "r.sarif"
    res = runner.invoke(app, ["review", "--demo", "-f", "sarif", "-o", str(out)])
    assert res.exit_code == 1
    sarif = json.loads(out.read_text())
    assert sarif["version"] == "2.1.0"
    (run,) = sarif["runs"]
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    for r in run["results"]:
        assert r["ruleId"] in rule_ids
        assert r["level"] in ("error", "warning", "note")
        region = r["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] >= 1
        assert len(r["partialFingerprints"]["scrutai/v1"]) == 16
    assert {"error", "warning", "note"} <= {r["level"] for r in run["results"]}


def test_markdown_with_dropped() -> None:
    res = runner.invoke(app, ["review", "--demo", "-f", "markdown", "--show-dropped"])
    assert "### Scrutai review: `request_changes`" in res.output
    assert "| 🔴 | high | `app/runner.py:7` |" in res.output
    assert "Dropped by the critic" in res.output


def test_fingerprint_survives_line_moves() -> None:
    a = Finding(
        agent="s",
        title="t",
        body="",
        file="a.py",
        line=3,
        category="injection",
        evidence=["L3:   os.system(cmd)"],
    )
    b = a.model_copy(update={"line": 40, "evidence": ["L40: os.system(cmd)"]})
    c = a.model_copy(update={"evidence": ["L3: os.system(other)"]})
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


def test_clean_branch_exits_0(git_repo: Callable[[dict[str, str]], Path]) -> None:
    repo = git_repo({"docs/guide.md": "# Guide\n", "app.py": "def keep():\n    return 1\n"})
    res = runner.invoke(app, ["review", "--repo", str(repo), "--base", "main"])
    assert res.exit_code == 0, res.output
    assert "Nothing to review" in res.output


def test_risky_branch_exits_1(git_repo: Callable[[dict[str, str]], Path]) -> None:
    repo = git_repo({"svc.py": "import pickle\n\n\ndef _load(b):\n    return pickle.loads(b)\n"})
    res = runner.invoke(app, ["review", "--repo", str(repo), "--base", "main", "--json"])
    assert res.exit_code == 1
    (f,) = [f for f in json.loads(res.output)["findings"] if f["agent"] == "security"]
    assert (f["file"], f["line"], f["category"]) == ("svc.py", 5, "unsafe_deserialization")


def test_fail_on_threshold_is_configurable(
    git_repo: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = git_repo({"svc.py": "import pickle\n\n\ndef _load(b):\n    return pickle.loads(b)\n"})
    cfg = tmp_path / "c.yml"
    cfg.write_text("fail_on: critical\n")
    res = runner.invoke(
        app, ["review", "--repo", str(repo), "--base", "main", "--config", str(cfg)]
    )
    assert res.exit_code == 0


def test_stdin_diff() -> None:
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+eval(data)\n"
    res = runner.invoke(app, ["review", "--diff", "-", "--json"], input=patch)
    assert res.exit_code == 1
    assert json.loads(res.output)["findings"][0]["category"] == "injection"


def test_bad_inputs_exit_2(tmp_path: Path) -> None:
    assert runner.invoke(app, ["review", "--diff", str(tmp_path / "nope.diff")]).exit_code == 2
    bad = tmp_path / "bad.yml"
    bad.write_text("enabled_agents: [security, telepathy]\n")
    res = runner.invoke(app, ["review", "--demo", "--config", str(bad)])
    assert res.exit_code == 2 and "telepathy" in res.output


def test_version_flag() -> None:
    from scrutai import __version__

    res = CliRunner().invoke(app, ["--version"])
    assert res.exit_code == 0 and res.output.strip() == f"scrutai {__version__}"


def test_a_named_config_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    """Falling back to defaults would silently run a 'live' benchmark in mock mode."""
    missing = tmp_path / "live.yml"
    for args in (["review", "--demo"], ["eval", "--limit", "1"]):
        res = CliRunner().invoke(app, [*args, "--config", str(missing)])
        assert res.exit_code == 2 and "Config file not found" in res.output
    (tmp_path / "live.yml.txt").write_text("llm_mode: live\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["review", "--demo", "--config", str(missing)])
    assert res.exit_code == 2 and "Notepad added .txt" in res.output


def test_the_default_config_may_be_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["review", "--demo"])
    assert res.exit_code == 1  # findings, run with the built-in defaults
