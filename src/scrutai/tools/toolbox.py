"""The toolbox a ReAct specialist acts through.

Agents name a tool and pass JSON args; the toolbox validates them, runs the
deterministic function from `repo.py`, and returns a bounded text observation.
Unknown tools and bad args come back as an observation (not an exception), so a
model that misuses a tool sees its mistake and can correct course.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .repo import git_blame, grep, read_file

_MAX_OBS_CHARS = 4000


@dataclass(frozen=True)
class Tool:
    name: str
    signature: str
    description: str
    run: Callable[[dict[str, Any], str], str]


def _read(args: dict[str, Any], root: str) -> str:
    path = str(args["path"])
    start = int(args["start"]) if "start" in args else None
    end = int(args["end"]) if "end" in args else None
    text = read_file(path, root, start, end)
    return text or f"(no such file in repo: {path})"


def _grep(args: dict[str, Any], root: str) -> str:
    hits = grep(str(args["pattern"]), root, regex=bool(args.get("regex", False)))
    return "\n".join(hits) if hits else "(no matches)"


def _blame(args: dict[str, Any], root: str) -> str:
    return git_blame(str(args["path"]), int(args["line"]), root) or "(no blame available)"


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "read_file",
            'read_file {"path": str, "start"?: int, "end"?: int}',
            "Read a repo file (optionally a line range) to see code the diff doesn't show.",
            _read,
        ),
        Tool(
            "grep",
            'grep {"pattern": str, "regex"?: bool}',
            "Search the whole repo; returns path:line:text hits (fixed string unless regex).",
            _grep,
        ),
        Tool(
            "git_blame",
            'git_blame {"path": str, "line": int}',
            "Who last changed a line, and when.",
            _blame,
        ),
    )
}


def register(tool: Tool) -> None:
    """Add a tool (e.g. Semgrep) to the shared registry."""
    TOOLS[tool.name] = tool


class Toolbox:
    def __init__(self, repo_root: str, allowed: list[str] | None = None) -> None:
        self.repo_root = repo_root
        self.allowed = [n for n in (allowed or list(TOOLS)) if n in TOOLS]
        self.calls: list[str] = []  # human-readable trace, becomes finding evidence

    def describe(self) -> str:
        return "\n".join(f"- {TOOLS[n].signature}: {TOOLS[n].description}" for n in self.allowed)

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.allowed:
            obs = f"(unknown tool {name!r}; available: {', '.join(self.allowed)})"
        else:
            try:
                obs = TOOLS[name].run(args, self.repo_root)
            except (KeyError, TypeError, ValueError) as exc:
                obs = f"(bad arguments for {name}: {exc!r}; usage: {TOOLS[name].signature})"
        if len(obs) > _MAX_OBS_CHARS:
            obs = obs[:_MAX_OBS_CHARS] + "\n...(truncated)"
        hits = obs.count("\n") + 1 if not obs.startswith("(") else 0
        self.calls.append(f"tool:{name}({_short(args)}) -> {hits} line(s)")
        return obs


def _short(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())[:120]
