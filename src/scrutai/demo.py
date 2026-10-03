"""The bundled demo: real issues plus planted noise, so the critic has something to kill.

Shared by `scrutai review --demo`, the web UI's "Run demo" button and the MCP server.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .config import ScrutaiConfig
from .llm import make_client
from .models import ChangedFile, DiffContext, ReviewResult
from .orchestrator import review_diff
from .trace import Tracer

DEMO_FILE = "app/runner.py"
DEMO_SOURCE = """\
import os


def run(cmd, env={}):
    # never pass user input to os.system(...) unescaped
    try:
        return os.system(cmd)
    except Exception:
        print("failed", cmd)
        return -1
"""


@contextlib.contextmanager
def demo_diff() -> Iterator[DiffContext]:
    """The demo as a diff inside a throwaway repo, so tools see it and not your cwd."""
    with tempfile.TemporaryDirectory(prefix="scrutai-demo-") as root:
        target = Path(root) / DEMO_FILE
        target.parent.mkdir(parents=True)
        target.write_text(DEMO_SOURCE)
        patch = "".join(f"+{line}\n" for line in DEMO_SOURCE.splitlines())
        yield DiffContext(repo_root=root, files=[ChangedFile(path=DEMO_FILE, patch=patch)])


def review_demo(config: ScrutaiConfig, tracer: Tracer | None = None) -> ReviewResult:
    with demo_diff() as diff:
        return review_diff(diff, config, make_client(config.llm_mode), tracer)
