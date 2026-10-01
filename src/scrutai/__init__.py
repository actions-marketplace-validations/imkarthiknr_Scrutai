"""Scrutai — multi-agent code review with an adversarial critic."""

from .models import DiffContext, Finding, ReviewResult, Severity, Verdict
from .orchestrator import review_diff

__version__ = "0.1.0"
__all__ = ["review_diff", "DiffContext", "Finding", "ReviewResult", "Severity", "Verdict"]
