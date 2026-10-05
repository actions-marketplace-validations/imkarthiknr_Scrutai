"""Semgrep integration, exercised through a fake `semgrep` binary so CI needs no install."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.diff import diff_from_git
from scrutai.llm import MockLLMClient
from scrutai.tools import Toolbox
from scrutai.tools.semgrep import category_for, normalize_rule_id, parse_output


def _result(path: str, line: int, rule: str) -> dict[str, object]:
    return {
        "check_id": rule,
        "path": path,
        "start": {"line": line},
        "extra": {"severity": "ERROR", "message": "shell=True with input"},
    }


@pytest.fixture
def fake_semgrep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[object]], Path]:
    """Install a `semgrep` on PATH that prints the given results and logs its argv."""

    def install(results: list[object]) -> Path:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        log = tmp_path / "argv.log"
        exe = bindir / "semgrep"
        payload = json.dumps({"results": results, "errors": []})
        exe.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\ncat <<'JSON'\n{payload}\nJSON\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        return log

    return install


def test_parse_output_tolerates_junk() -> None:
    text = json.dumps({"results": [_result("./a.py", 3, "x.sql.y"), {"check_id": "broken"}]})
    (hit,) = parse_output(text)
    assert (hit.path, hit.line, hit.category) == ("a.py", 3, "sql_injection")
    assert parse_output("not json") == []


@pytest.mark.parametrize(
    ("rule", "category"),
    [
        ("home.me.pkg.scrutai.rules.scrutai.injection.os-system", "injection"),
        ("scrutai.weak_crypto.md5-sha1", "weak_crypto"),
        ("python.lang.security.audit.formatted-sql-query", "sql_injection"),
        ("python.django.security.something-else", "sast"),
    ],
)
def test_category_mapping(rule: str, category: str) -> None:
    assert category_for(rule) == category


def test_normalize_keeps_registry_ids() -> None:
    assert normalize_rule_id("python.lang.security.x") == "python.lang.security.x"


def test_seed_keeps_only_hits_on_added_lines(
    git_repo: Callable[[dict[str, str]], Path], fake_semgrep: Callable[[list[object]], Path]
) -> None:
    repo = git_repo(
        {"job.py": "import subprocess\n\nsubprocess.run(\n    cmd,\n    shell=True,\n)\n"}
    )
    rule = "scrutai.injection.subprocess-shell"
    fake_semgrep([_result("job.py", 3, rule), _result("app.py", 1, rule)])  # app.py: unchanged
    diff = diff_from_git("main", "HEAD", str(repo))
    cfg = ScrutaiConfig(enabled_agents=["security"])
    result = review_diff(diff, cfg, MockLLMClient())
    (f,) = result.findings
    assert (f.file, f.line, f.category, f.confidence) == ("job.py", 3, "injection", 0.9)
    assert any("[scrutai.injection.subprocess-shell]" in e for e in f.evidence)


def test_semgrep_off_never_runs(
    git_repo: Callable[[dict[str, str]], Path], fake_semgrep: Callable[[list[object]], Path]
) -> None:
    repo = git_repo({"job.py": "os.system(cmd)\n"})
    log = fake_semgrep([])
    diff = diff_from_git("main", "HEAD", str(repo))
    review_diff(diff, ScrutaiConfig(enabled_agents=["security"], semgrep="off"), MockLLMClient())
    assert not log.exists()


def test_tool_confines_paths_and_ignores_model_config(
    tmp_path: Path, fake_semgrep: Callable[[list[object]], Path]
) -> None:
    log = fake_semgrep([_result("a.py", 1, "scrutai.injection.eval")])
    box = Toolbox(str(tmp_path), ["semgrep"], semgrep_config="my-rules.yml")
    assert "no paths inside the repo" in box.run("semgrep", {"paths": ["../../etc"]})
    out = box.run("semgrep", {"paths": ["a.py"], "config": "https://evil.example/rules"})
    assert "a.py:1: [scrutai.injection.eval]" in out
    argv = log.read_text()
    assert "my-rules.yml" in argv and "evil" not in argv


def test_required_semgrep_missing_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from scrutai.cli import app

    monkeypatch.setenv("PATH", str(tmp_path))  # no semgrep (or anything) on PATH
    cfg = tmp_path / "c.yml"
    cfg.write_text("semgrep: required\n")
    res = CliRunner().invoke(app, ["review", "--demo", "--config", str(cfg)])
    assert res.exit_code == 2 and "required" in res.output


class KillHappyCritic:
    """Specialist reports the injection; the critic always kills, never with counter-evidence."""

    tokens_used = 0
    cost_usd = 0.0

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        if "you are the critic" in system.lower():
            return json.dumps({"decision": "kill", "confidence": 0.2, "note": "probably fine"})
        if "MODE: defend" in prompt:
            return json.dumps({"defense": "cmd comes from the caller", "evidence": ["L3"]})
        finding = {
            "title": "Shell injection",
            "file": "job.py",
            "line": 3,
            "category": "injection",
            "severity": "high",
            "confidence": 0.8,
            "evidence": ["L3: subprocess.run("],
        }
        return json.dumps({"findings": [finding]})


@pytest.mark.parametrize("rule_fired", [True, False])
def test_a_rule_backed_finding_survives_a_kill_without_counter_evidence(
    git_repo: Callable[[dict[str, str]], Path],
    fake_semgrep: Callable[[list[object]], Path],
    rule_fired: bool,
) -> None:
    repo = git_repo(
        {"job.py": "import subprocess\n\nsubprocess.run(\n    cmd,\n    shell=True,\n)\n"}
    )
    rule = "scrutai.injection.subprocess-shell"
    fake_semgrep([_result("job.py", 3, rule)] if rule_fired else [])
    diff = diff_from_git("main", "HEAD", str(repo))
    cfg = ScrutaiConfig(enabled_agents=["security"])
    result = review_diff(diff, cfg, KillHappyCritic())
    if rule_fired:
        (f,) = result.findings
        assert f.sast_rule == rule and f.defense == "cmd comes from the caller"
        assert [h.split(" (")[0] for h in f.history] == [
            "round 1: challenge",
            "defense: submitted",
            "round 2: uphold",
        ]
    else:
        assert result.findings == []
        (dropped,) = result.dropped
        assert dropped.sast_rule is None and dropped.history[0].startswith("round 1: kill")
