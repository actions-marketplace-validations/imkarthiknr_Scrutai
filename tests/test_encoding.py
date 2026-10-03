"""Text I/O is UTF-8 everywhere, whatever the OS locale.

On Windows, Python's default text encoding is the legacy code page (cp1252),
so any read, write or subprocess call without `encoding=` breaks on a diff
containing a curly quote or an emoji. These tests keep that from coming back.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "scrutai"
SCRUTAI = str(Path(sys.executable).with_name("scrutai"))

SUBPROCESS = {"run", "Popen", "check_output"}


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _binary_mode(call: ast.Call) -> bool:
    mode = _kw(call, "mode") or (call.args[0] if call.args else None)
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "b" in mode.value
    return False


def _offence(call: ast.Call) -> str | None:
    func = call.func
    has_encoding = _kw(call, "encoding") is not None
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
        return None if has_encoding else f"{func.attr}()"
    is_open = (isinstance(func, ast.Name) and func.id == "open") or (
        isinstance(func, ast.Attribute) and func.attr == "open"
    )
    if is_open and not _binary_mode(call) and not has_encoding:
        return "open()"
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
        and func.attr in SUBPROCESS
    ):
        text = _kw(call, "text") or _kw(call, "universal_newlines")
        if isinstance(text, ast.Constant) and text.value and not has_encoding:
            return f"subprocess.{func.attr}(text=True)"
    return None


def test_every_text_io_call_names_its_encoding() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{node.lineno} {offence}"
        for path in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and (offence := _offence(node))
    ]
    assert not offenders, "text I/O without encoding= (breaks on Windows):\n" + "\n".join(offenders)


def test_review_works_in_a_non_utf8_locale(git_repo: Callable[[dict[str, str]], Path]) -> None:
    """The Windows failure, reproduced on any OS: an ASCII locale with UTF-8 mode off."""
    repo = git_repo({"notes.py": 'MSG = "don’t panic 🚀"  # curly quote and emoji\nprint(MSG)\n'})
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [SCRUTAI, "review", "--base", "main", "--head", "feature", "-f", "markdown", "-o", "r.md"],
        cwd=repo,
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode in (0, 1), stderr  # 1 = findings at/above fail_on; 2 or a crash = bug
    assert "UnicodeDecodeError" not in stderr and "UnicodeEncodeError" not in stderr
    assert "notes.py" in (repo / "r.md").read_text(encoding="utf-8")
