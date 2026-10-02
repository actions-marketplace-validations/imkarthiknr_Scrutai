from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scrutai.diff import DiffError, apply_filters, diff_from_file, diff_from_git
from scrutai.models import ChangedFile, DiffContext
from scrutai.patch import added_lines, path_selected, split_unified_diff

GIT_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@ def f():
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a
diff --git a/docs/new.md b/docs/new.md
new file mode 100644
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1,1 @@
+hello
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def test_added_lines_track_new_file_numbers() -> None:
    files = split_unified_diff(GIT_DIFF)
    lines = added_lines(files[0][2])
    assert [(d.line, d.text) for d in lines] == [(11, "    b = 3"), (12, "    c = 4")]


def test_headerless_snippet_starts_at_line_one() -> None:
    assert [d.line for d in added_lines("+a\n+b\n")] == [1, 2]


def test_plus_plus_inside_hunk_is_content() -> None:
    patch = "diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1,0 +1,2 @@\n+++i;\n+--j;\n"
    assert [d.text for d in added_lines(patch)] == ["++i;", "--j;"]


def test_split_detects_status() -> None:
    statuses = {p: s for p, s, _ in split_unified_diff(GIT_DIFF)}
    assert statuses == {"src/app.py": "modified", "docs/new.md": "added", "old.py": "deleted"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("vendor/lib.py", False),
        ("a/vendor/b/lib.py", False),
        ("uv.lock", False),
        ("dist/x.js", False),
        ("src/app.py", True),
    ],
)
def test_default_globs(path: str, expected: bool) -> None:
    assert path_selected(path, ["**/*"], ["**/vendor/**", "**/*.lock", "**/dist/**"]) is expected


def test_filters_drop_deleted_and_excluded(tmp_path: Path) -> None:
    patch_file = tmp_path / "c.diff"
    patch_file.write_text(GIT_DIFF)
    diff = apply_filters(diff_from_file(str(patch_file)), ["**/*"], ["docs/**"])
    assert diff.paths == ["src/app.py"]


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("src/app.py", "code"),
        ("tests/test_app.py", "test"),
        ("web/button.test.ts", "test"),
        ("README.md", "docs"),
        ("poetry.lock", "lock"),
        ("logo.png", "other"),
    ],
)
def test_file_kind(path: str, kind: str) -> None:
    assert ChangedFile(path=path).kind == kind


def test_diff_from_real_git_repo(git_repo: Callable[[dict[str, str]], Path]) -> None:
    repo = git_repo({"app.py": "def keep():\n    return 1\n\n\ndef new():\n    return 2\n"})
    diff = diff_from_git("main", "HEAD", str(repo))
    assert isinstance(diff, DiffContext)
    assert diff.paths == ["app.py"]
    assert [d.line for d in diff.files[0].added] == [3, 4, 5, 6]


def test_bad_ref_raises(git_repo: Callable[[dict[str, str]], Path]) -> None:
    repo = git_repo({"x.py": "x = 1\n"})
    with pytest.raises(DiffError):
        diff_from_git("does-not-exist", "HEAD", str(repo))
