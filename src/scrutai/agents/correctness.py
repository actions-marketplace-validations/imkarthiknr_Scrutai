from __future__ import annotations

from typing import ClassVar

from .base import Specialist


class CorrectnessAgent(Specialist):
    name = "correctness"
    role = (
        "You find logic bugs: swallowed exceptions, unhandled None, mutable defaults, "
        "wrong comparisons, and missed edge cases. Read surrounding code with read_file "
        "when the diff alone is ambiguous."
    )
    categories: ClassVar[dict[str, str]] = {
        "broad_except": "bare or overly broad except that swallows errors",
        "mutable_default": "mutable default argument shared across calls",
        "none_comparison": "== / != None instead of is / is not",
        "identity_literal": "`is` used to compare with a literal",
        "logic_error": "any other concrete bug with a reproducible failure",
    }
