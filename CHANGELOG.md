# Changelog

## 0.2.0

### Added
- GitHub Action (`action.yml`): idempotent PR reviews: one summary comment edited in place and
  fingerprinted inline comments that are never re-posted across pushes; SARIF output for code
  scanning; `verdict` output; job-summary report.
- `scrutai review --pr N [--post]`, `--diff FILE|-`, `--format table|json|markdown|sarif`,
  `--output`, `--show-dropped`, `--trace`.
- Performance and Style specialists.
- Semgrep integration with a bundled offline ruleset (seed evidence + agent tool).
- Concurrent fan-out (LangGraph `Send`), chunked review with per-chunk routing.
- Token / dollar budget guardrail; cost reporting.
- Tracing: JSONL, OpenTelemetry, Langfuse.
- Config validation (unknown agents/modes fail fast with exit code 2).

### Changed
- Findings the critic could not judge are withheld instead of shipping on the specialist's word.
- Default models: `claude-haiku-4-5` (router), `claude-sonnet-5-5` (specialists),
  `claude-opus-5-5` (critic). Live calls no longer send `temperature`.

## 0.1.0

### Added
- Unified-diff parsing with new-file line numbers; `include`/`exclude` globs; a bad git ref is
  an error instead of a silently "clean" review.
- File- and content-aware routing (docs-only diffs wake nobody); optional narrowing LLM router.
- ReAct specialists (Security, Correctness, Tests) over sandboxed repo tools.
- Evidence-grounded critic with uphold / downgrade / kill / challenge and a bounded
  defend-and-re-judge debate.
- Hermetic eval harness: category scoring, clean-case FPR, critic ablation; 50-case benchmark;
  CI gate.
