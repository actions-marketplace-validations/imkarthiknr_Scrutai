"""Scrutai — multi-agent code review with an adversarial critic."""

# Defined before the imports below so modules they load (e.g. report.py) can read it.
__version__ = "0.3.0"

from .models import DiffContext, Finding, ReviewResult, Severity, Verdict  # noqa: E402
from .orchestrator import review_diff  # noqa: E402

__all__ = ["review_diff", "DiffContext", "Finding", "ReviewResult", "Severity", "Verdict"]
