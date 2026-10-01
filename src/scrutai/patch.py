"""Pure unified-diff parsing — no git, no I/O, no models.

Agents need to cite exact lines, so the raw patch text is parsed into the lines
it adds, each tagged with its line number in the *new* file. Patches without a
hunk header (hand-written snippets, benchmark cases) are treated as a single
hunk starting at line 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class DiffLine:
    line: int  # 1-based line number in the new file
    text: str  # content without the leading "+"


def added_lines(patch: str) -> list[DiffLine]:
    """Every line the patch adds, with its new-file line number."""
    out: list[DiffLine] = []
    lineno = 1
    in_header = False
    for raw in patch.splitlines():
        m = _HUNK.match(raw)
        if m:
            lineno, in_header = int(m.group(1)), False
            continue
        if raw.startswith("diff --git "):
            in_header = True
        # `+++`/`---` are file headers only outside a hunk; inside one, `+++i`
        # is an added line reading `++i`.
        if in_header or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            out.append(DiffLine(lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue  # removed lines don't exist in the new file
        else:
            lineno += 1  # context line
    return out


def new_file_lines(patch: str) -> dict[int, tuple[str, bool]]:
    """Every new-file line visible in the patch: line -> (text, is_added)."""
    out: dict[int, tuple[str, bool]] = {}
    lineno = 1
    in_header = False
    for raw in patch.splitlines():
        m = _HUNK.match(raw)
        if m:
            lineno, in_header = int(m.group(1)), False
            continue
        if raw.startswith("diff --git "):
            in_header = True
        if in_header or raw.startswith(("\\", "-")):
            continue
        out[lineno] = (raw[1:], raw.startswith("+"))
        lineno += 1
    return out


def window(patch: str, line: int, radius: int = 3) -> str:
    """The patch's new-file lines around `line`, marked `+` when added."""
    lines = new_file_lines(patch)
    return "\n".join(
        f"{'+' if added else ' '}L{n}: {text}"
        for n, (text, added) in sorted(lines.items())
        if abs(n - line) <= radius
    )


def slice_patch(patch: str, max_lines: int) -> list[str]:
    """Split a patch into patches of at most `max_lines` added lines each.

    Each slice keeps true new-file line numbers (one hunk per contiguous run of
    added lines). Context and removed lines are dropped: agents only ever see
    added lines, and the critic judges against the original, unsliced patch.
    """
    lines = added_lines(patch)
    if len(lines) <= max_lines:
        return [patch]
    out: list[str] = []
    for start in range(0, len(lines), max(max_lines, 1)):
        window = lines[start : start + max_lines]
        hunks: list[list[DiffLine]] = []
        for d in window:
            if hunks and d.line == hunks[-1][-1].line + 1:
                hunks[-1].append(d)
            else:
                hunks.append([d])
        out.append(
            "".join(
                f"@@ -0,0 +{h[0].line},{len(h)} @@\n" + "".join(f"+{d.text}\n" for d in h)
                for h in hunks
            )
        )
    return out


def numbered(patch: str) -> str:
    """Render added lines as `L<n>: <code>` so an LLM can cite line numbers."""
    return "\n".join(f"L{d.line}: {d.text}" for d in added_lines(patch))


def split_unified_diff(text: str) -> list[tuple[str, str, str]]:
    """Split `git diff` output into (path, status, per-file patch) triples.

    status is one of: added, modified, deleted, renamed, binary.
    """
    chunks: list[list[str]] = []
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            chunks.append([])
        if chunks:
            chunks[-1].append(raw)

    files: list[tuple[str, str, str]] = []
    for chunk in chunks:
        header = chunk[0]
        path = header.split(" b/", 1)[1] if " b/" in header else header.split()[-1]
        status = "modified"
        for raw in chunk[1:]:
            if raw.startswith("@@"):
                break
            if raw.startswith("new file mode"):
                status = "added"
            elif raw.startswith("deleted file mode"):
                status = "deleted"
            elif raw.startswith("rename to "):
                status, path = "renamed", raw[len("rename to ") :]
            elif raw.startswith("Binary files"):
                status = "binary"
            elif raw.startswith("+++ b/"):
                path = raw[len("+++ b/") :]
        files.append((path, status, "\n".join(chunk) + "\n"))
    return files


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-ish glob (`**/vendor/**`, `*.lock`) to a regex.

    `**/` matches zero or more directories, `**` matches anything, `*` and `?`
    never cross a `/`.
    """
    i, parts = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile(f"^{''.join(parts)}$")


def path_selected(path: str, include: list[str], exclude: list[str]) -> bool:
    if any(glob_to_regex(p).match(path) for p in exclude):
        return False
    return any(glob_to_regex(p).match(path) for p in include)
