from __future__ import annotations

from typing import ClassVar

from .base import Specialist


class StyleAgent(Specialist):
    name = "style"
    role = (
        "You flag leftovers that should not ship: debug prints and breakpoints, "
        "wildcard imports, and TODOs with no ticket. Never comment on formatting a "
        "formatter would fix. CLIs and scripts may legitimately print."
    )
    categories: ClassVar[dict[str, str]] = {
        "debug_leftover": "print/console.log/breakpoint/pdb left in library code",
        "wildcard_import": "from module import * hides where names come from",
        "untracked_todo": "TODO/FIXME without an issue reference",
    }
    tools: ClassVar[list[str]] = ["read_file", "grep"]
