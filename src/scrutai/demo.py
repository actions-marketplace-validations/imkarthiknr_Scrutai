"""The bundled demo: real issues plus planted noise, so the critic has something to kill.

Shared by `scrutai review --demo` and the web UI's "Run demo" button.
"""

from __future__ import annotations

import tempfile
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


def review_demo(config: ScrutaiConfig, tracer: Tracer | None = None) -> ReviewResult:
    # A throwaway repo so the agents' tools see the demo file, not your cwd.
    with tempfile.TemporaryDirectory(prefix="scrutai-demo-") as root:
        target = Path(root) / DEMO_FILE
        target.parent.mkdir(parents=True)
        target.write_text(DEMO_SOURCE)
        patch = "".join(f"+{line}\n" for line in DEMO_SOURCE.splitlines())
        diff = DiffContext(repo_root=root, files=[ChangedFile(path=DEMO_FILE, patch=patch)])
        return review_diff(diff, config, make_client(config.llm_mode), tracer)
