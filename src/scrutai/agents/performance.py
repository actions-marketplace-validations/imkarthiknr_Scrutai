from __future__ import annotations

from typing import ClassVar

from .base import Specialist


class PerformanceAgent(Specialist):
    name = "performance"
    role = (
        "You look for avoidable cost on hot paths: queries or HTTP calls inside loops "
        "(N+1), work that could be hoisted out of a loop, and quadratic patterns. Use "
        "read_file to see the enclosing loop before reporting."
    )
    categories: ClassVar[dict[str, str]] = {
        "n_plus_one": "a database query or network call issued once per loop iteration",
        "regex_in_loop": "a regex compiled on every iteration instead of once",
        "string_concat_in_loop": "string built with += in a loop (quadratic)",
        "sort_for_min_max": "sorting a whole sequence just to take its min or max",
    }
