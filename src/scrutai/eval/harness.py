"""The eval harness — Scrutai's credibility layer.

Runs the full pipeline over a labeled set of diffs and measures how well the
critic keeps signal while dropping noise: precision, recall, and false-positive
rate. This is the number that goes in the README, and the number no single-shot
reviewer publishes.

Each benchmark line is a JSON object:
    {"id": "...", "patch": "...", "labels": ["injection", ...]}
`labels` are the issue *categories* a correct reviewer should surface. An empty
list means the diff is intentionally clean (any finding is a false positive).
"""

from __future__ import annotations

import json

from ..config import ScrutaiConfig
from ..llm import make_client
from ..models import ChangedFile, DiffContext

# Maps a finding title to a benchmark label so we can score matches. Extend as
# the agents and benchmark grow.
_LABEL_HINTS = {
    "injection": ("injection", "eval", "command"),
    "broad_except": ("exception", "except"),
    "missing_tests": ("test",),
}


def _matches(title: str, label: str) -> bool:
    return any(h in title.lower() for h in _LABEL_HINTS.get(label, (label,)))


def run_benchmark(path: str, config: ScrutaiConfig) -> dict:
    tp = fp = fn = 0
    cases = 0
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        cases += 1
        diff = DiffContext(files=[ChangedFile(path=case.get("file", "case.py"), patch=case["patch"])])
        # Fresh client per case keeps token accounting per-run honest.
        from ..orchestrator import review_diff

        result = review_diff(diff, config, make_client(config.llm_mode))

        expected = set(case.get("labels", []))
        got_titles = [f.title for f in result.findings]

        matched_labels = {lbl for lbl in expected if any(_matches(t, lbl) for t in got_titles)}
        tp += len(matched_labels)
        fn += len(expected - matched_labels)
        # Findings that matched no expected label are false positives.
        for t in got_titles:
            if not any(_matches(t, lbl) for lbl in expected):
                fp += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "cases": cases,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "false_positive_rate": round(fp / (tp + fp), 3) if (tp + fp) else 0.0,
        "f1": round(f1, 3),
    }
