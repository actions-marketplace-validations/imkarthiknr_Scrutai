"""The eval harness — Scrutai's credibility layer.

Runs the full pipeline over a labeled set of diffs and measures how well the
panel finds real issues and how well the critic drops noise. This is the number
that goes in the README, and the number no single-shot reviewer publishes.

Each benchmark line is a JSON object:

    {"id": "inj-01",
     "file": "runner.py",                 # path of the changed file
     "patch": "+def run(cmd): ...",       # lines it adds (hunk header optional)
     "labels": ["injection"],             # categories a correct reviewer reports
     "repo": {"tests/test_x.py": "..."},  # optional: other files in the repo
     "note": "why this case exists"}      # optional

`labels` is a multiset of finding categories; an empty list marks a clean diff
(any finding is a false positive). Every case runs in its own temporary repo
built from `repo` plus the changed file, so tool calls (grep, read_file) see a
hermetic world rather than whatever directory `scrutai eval` was started in.

Metrics:
    precision / recall / f1   over category matches, multiset-counted per case
    false_discovery_rate      share of reported findings that were wrong
    clean_case_fpr            share of clean cases that got >= 1 finding
    *_without_critic          the same, computed on everything the specialists
                              raised before the critic judged it (ablation)
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ScrutaiConfig
from ..llm import make_client
from ..models import ChangedFile, DiffContext, Finding
from ..patch import added_lines


@dataclass
class Case:
    id: str
    file: str
    patch: str
    labels: list[str]
    repo: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass
class CaseResult:
    case: Case
    reported: list[str]  # categories of surviving findings
    raw: list[str]  # categories before the critic
    tokens: int
    rounds: int

    def score(self, got: list[str]) -> tuple[int, int, int]:
        expected, found = Counter(self.case.labels), Counter(got)
        tp = sum((expected & found).values())
        return tp, sum((found - expected).values()), sum((expected - found).values())

    @property
    def missed(self) -> list[str]:
        return sorted((Counter(self.case.labels) - Counter(self.reported)).elements())

    @property
    def spurious(self) -> list[str]:
        return sorted((Counter(self.reported) - Counter(self.case.labels)).elements())


def load_cases(path: str | Path) -> list[Case]:
    cases: list[Case] = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            raw = json.loads(line)
            cases.append(
                Case(
                    id=str(raw.get("id", f"case-{n}")),
                    file=str(raw.get("file", "case.py")),
                    patch=str(raw["patch"]),
                    labels=[str(lbl) for lbl in raw.get("labels", [])],
                    repo={str(k): str(v) for k, v in raw.get("repo", {}).items()},
                    note=str(raw.get("note", "")),
                )
            )
        except (json.JSONDecodeError, KeyError, AttributeError) as exc:
            raise ValueError(f"{path}:{n}: invalid benchmark case ({exc})") from exc
    return cases


def run_case(case: Case, config: ScrutaiConfig) -> CaseResult:
    from ..orchestrator import review_diff  # local: orchestrator imports agents

    with tempfile.TemporaryDirectory(prefix="scrutai-eval-") as root:
        for rel, content in case.repo.items():
            target = Path(root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        # The changed file exists in the repo too, as the patch leaves it.
        target = Path(root) / case.file
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("\n".join(d.text for d in added_lines(case.patch)) + "\n")

        diff = DiffContext(repo_root=root, files=[ChangedFile(path=case.file, patch=case.patch)])
        # Fresh client per case keeps token accounting per-run honest.
        llm = make_client(config.llm_mode)
        result = review_diff(diff, config, llm)

    raw: list[Finding] = [*result.findings, *result.dropped]
    return CaseResult(
        case=case,
        reported=[f.category for f in result.findings],
        raw=[f.category for f in raw if f.severity.rank >= config.min_severity.rank],
        tokens=result.tokens_used,
        rounds=result.rounds,
    )


def _ratios(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    def totals(attr: str) -> tuple[int, int, int]:
        tp = fp = fn = 0
        for r in results:
            a, b, c = r.score(getattr(r, attr))
            tp, fp, fn = tp + a, fp + b, fn + c
        return tp, fp, fn

    tp, fp, fn = totals("reported")
    rtp, rfp, rfn = totals("raw")
    clean = [r for r in results if not r.case.labels]
    flagged_clean = sum(1 for r in clean if r.reported)
    flagged_clean_raw = sum(1 for r in clean if r.raw)
    final, raw = _ratios(tp, fp, fn), _ratios(rtp, rfp, rfn)

    per_category: dict[str, dict[str, Any]] = {}
    cats = sorted({c for r in results for c in [*r.case.labels, *r.reported]})
    for cat in cats:
        ctp = cfp = cfn = 0
        for r in results:
            exp, got = r.case.labels.count(cat), r.reported.count(cat)
            ctp, cfp, cfn = ctp + min(exp, got), cfp + max(got - exp, 0), cfn + max(exp - got, 0)
        per_category[cat] = {"tp": ctp, "fp": cfp, "fn": cfn, **_ratios(ctp, cfp, cfn)}

    return {
        "cases": len(results),
        "clean_cases": len(clean),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        **final,
        "false_discovery_rate": round(fp / (tp + fp), 3) if (tp + fp) else 0.0,
        "clean_case_fpr": round(flagged_clean / len(clean), 3) if clean else 0.0,
        "precision_without_critic": raw["precision"],
        "recall_without_critic": raw["recall"],
        "false_positives_without_critic": rfp,
        "clean_case_fpr_without_critic": (
            round(flagged_clean_raw / len(clean), 3) if clean else 0.0
        ),
        "critic_precision_lift": round(final["precision"] - raw["precision"], 3),
        "avg_tokens_per_case": round(sum(r.tokens for r in results) / len(results))
        if results
        else 0,
        "per_category": per_category,
    }


def run_benchmark(
    path: str | Path, config: ScrutaiConfig, details: list[CaseResult] | None = None
) -> dict[str, Any]:
    """Run every case and return aggregate metrics.

    Pass a list as `details` to also collect the per-case results.
    """
    results = [run_case(c, config) for c in load_cases(path)]
    if details is not None:
        details.extend(results)
    return summarize(results)


def markdown_report(metrics: dict[str, Any], results: list[CaseResult]) -> str:
    m = metrics
    lines = [
        "# Scrutai benchmark report",
        "",
        f"{m['cases']} cases ({m['clean_cases']} clean).",
        "",
        "| metric | with critic | specialists only |",
        "|---|---|---|",
        f"| precision | **{m['precision']}** | {m['precision_without_critic']} |",
        f"| recall | **{m['recall']}** | {m['recall_without_critic']} |",
        f"| false positives | **{m['false_positives']}** | {m['false_positives_without_critic']} |",
        f"| clean-case FPR | **{m['clean_case_fpr']}** | {m['clean_case_fpr_without_critic']} |",
        "",
        f"F1 {m['f1']} · false-discovery rate {m['false_discovery_rate']} · "
        f"~{m['avg_tokens_per_case']} tokens/case",
        "",
        "## Per category",
        "",
        "| category | tp | fp | fn | precision | recall |",
        "|---|---|---|---|---|---|",
    ]
    for cat, c in m["per_category"].items():
        lines.append(
            f"| {cat} | {c['tp']} | {c['fp']} | {c['fn']} | {c['precision']} | {c['recall']} |"
        )
    misses = [r for r in results if r.missed or r.spurious]
    if misses:
        lines += [
            "",
            "## Cases with errors",
            "",
            "| case | missed | false positives |",
            "|---|---|---|",
        ]
        for r in misses:
            lines.append(
                f"| {r.case.id} | {', '.join(r.missed) or '-'} | {', '.join(r.spurious) or '-'} |"
            )
    return "\n".join(lines) + "\n"
