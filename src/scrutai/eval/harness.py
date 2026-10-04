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
from collections.abc import Callable
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
    cost: float = 0.0  # dollars, live mode (0 for mock or unpriced models)
    partial: bool = False  # the per-review budget ran out on this case
    model_calls: int = 0
    model_errors: int = 0
    model_error: str | None = None

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
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
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
            target.write_text(content, encoding="utf-8")
        # The changed file exists in the repo too, as the patch leaves it.
        target = Path(root) / case.file
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(
                "\n".join(d.text for d in added_lines(case.patch)) + "\n", encoding="utf-8"
            )

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
        cost=result.cost_usd,
        partial=result.budget_exhausted,
        model_calls=result.model_calls,
        model_errors=result.model_errors,
        model_error=result.model_error,
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


# The labelled benchmark shipped inside the package (so `pip install scrutai` has it).
BUNDLED_CASES = Path(__file__).with_name("cases.jsonl")

# (cases done, cases in total, id of the case just finished); may raise to stop early.
CaseCallback = Callable[[int, int, str], None]


def run_benchmark(
    path: str | Path,
    config: ScrutaiConfig,
    details: list[CaseResult] | None = None,
    on_case: CaseCallback | None = None,
    limit: int | None = None,
    max_total_cost: float = 0.0,
) -> dict[str, Any]:
    """Run every case (or the first `limit`) and return aggregate metrics.

    Pass a list as `details` to also collect the per-case results. With
    `max_total_cost` (dollars, 0 = no cap) the run stops after the case that
    reaches it; the metrics then cover the cases that ran and say so.
    """
    cases = load_cases(path)[:limit] if limit else load_cases(path)
    # Filled as cases finish, so an on_case callback can read the running cost.
    results: list[CaseResult] = details if details is not None else []
    for case in cases:
        results.append(run_case(case, config))
        if on_case:
            on_case(len(results), len(cases), case.id)
        if max_total_cost and sum(r.cost for r in results) >= max_total_cost:
            break
    metrics = summarize(results)
    total_cost = sum(r.cost for r in results)
    metrics.update(
        {
            "llm_mode": config.llm_mode,
            "models": config.models.model_dump() if config.llm_mode != "mock" else {},
            "cases_planned": len(cases),
            "stopped_early": len(results) < len(cases),
            "partial_cases": sum(r.partial for r in results),
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_case": round(total_cost / len(results), 4) if results else 0.0,
            "model_calls": sum(r.model_calls for r in results),
            "model_errors": sum(r.model_errors for r in results),
            "first_model_error": next((r.model_error for r in results if r.model_error), None),
        }
    )
    return metrics


def _mode_lines(m: dict[str, Any]) -> list[str]:
    """Say which model produced the numbers: a mock run must never read as a model's quality."""
    mode = m.get("llm_mode", "mock")
    if mode == "mock":
        return [
            "> **Pipeline check (mock model).** These numbers come from the offline, rule-based "
            "stand-in model. They show the machinery works; they are not a measure of any LLM.",
            "",
        ]
    models = m.get("models") or {}
    used = ", ".join(f"{role} `{name}`" for role, name in models.items())
    lines = [
        f"> **Live model run.** Models: {used or 'see config'}. "
        f"Cost ${m.get('total_cost_usd', 0):.2f} (about ${m.get('avg_cost_per_case', 0):.3f} "
        "per case; $0 means the model has no price in LiteLLM's table).",
    ]
    if m.get("stopped_early"):
        lines.append(
            f"> Stopped at the cost cap after {m['cases']} of {m['cases_planned']} cases: "
            "the numbers cover those cases only."
        )
    if m.get("model_errors"):
        all_failed = m["model_errors"] == m.get("model_calls")
        lines.append(
            f"> **{'Every' if all_failed else m['model_errors']} model call"
            f"{'' if all_failed else ' of ' + str(m.get('model_calls'))} failed"
            f"{': these numbers are meaningless' if all_failed else ''}.** "
            f"First error: `{m.get('first_model_error')}`"
        )
    if m.get("partial_cases"):
        lines.append(f"> {m['partial_cases']} case(s) hit the per-review budget and are partial.")
    return [*lines, ""]


def markdown_report(metrics: dict[str, Any], results: list[CaseResult]) -> str:
    m = metrics
    lines = [
        "# Scrutai benchmark report",
        "",
        *_mode_lines(m),
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


def compare_backends(
    path: str | Path,
    config: ScrutaiConfig,
    backends: list[str],
    on_case: CaseCallback | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run the same benchmark with every specialist on each framework backend.

    The model, prompts, tools and critic are held constant, so differences are
    attributable to the orchestration framework alone.
    """
    import time

    runs: dict[str, dict[str, Any]] = {}
    reported: dict[str, list[list[str]]] = {}
    for i, backend in enumerate(backends):
        cfg = config.model_copy(update={"backends": {"*": backend}})
        details: list[CaseResult] = []
        start = time.perf_counter()

        def progress(done: int, total: int, case: str, i: int = i, backend: str = backend) -> None:
            if on_case:
                on_case(i * total + done, len(backends) * total, f"{backend}: {case}")

        metrics = run_benchmark(path, cfg, details, progress, limit)
        metrics["seconds"] = round(time.perf_counter() - start, 2)
        runs[backend] = metrics
        reported[backend] = [sorted(r.reported) for r in details]
    first = backends[0]
    agreement = {
        b: round(
            sum(x == y for x, y in zip(reported[first], reported[b], strict=True))
            / max(len(reported[first]), 1),
            3,
        )
        for b in backends[1:]
    }
    return {"backends": runs, "agreement_with_" + first: agreement}


def compare_markdown(comparison: dict[str, Any]) -> str:
    runs: dict[str, dict[str, Any]] = comparison["backends"]
    names = list(runs)
    rows = [
        ("precision", "precision"),
        ("recall", "recall"),
        ("F1", "f1"),
        ("false positives", "false_positives"),
        ("clean-case FPR", "clean_case_fpr"),
        ("critic precision lift", "critic_precision_lift"),
        ("avg tokens / case", "avg_tokens_per_case"),
        ("wall time (s)", "seconds"),
    ]
    lines = [
        "# Framework comparison",
        "",
        "Same model, prompts, tools and critic; only the agent-loop framework differs.",
        "",
        "| metric | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for label, key in rows:
        lines.append(f"| {label} | " + " | ".join(str(runs[n][key]) for n in names) + " |")
    agreement_key = next(k for k in comparison if k.startswith("agreement_with_"))
    for other, score in comparison[agreement_key].items():
        base = agreement_key.removeprefix("agreement_with_")
        lines += ["", f"Per-case agreement {base} vs {other}: **{score:.0%}** of cases identical."]
    return "\n".join(lines) + "\n"
