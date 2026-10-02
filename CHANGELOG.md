# Changelog

## 0.3.0

### Added
- Agent theater: `scrutai serve` (FastAPI + Server-Sent Events) and a React UI with the live review
  graph, a trial board following each finding from raised to upheld/killed, run history, trace
  upload, playback and scrubbing (`pip install "scrutai[web]"`).
- CrewAI backend behind the `Specialist._loop` seam, per agent via `backends:`
  (`pip install "scrutai[crewai]"`), and `scrutai eval --compare crewai`.
- Richer traces: run ids, sequence numbers, span start events, plan / finding / defense / result
  events.

### Fixed
- Per-call token counts in traces double-counted concurrent calls.
- Defense outcomes in traces reported defended findings as withdrawn.
- Git refs are validated so a ref can never be read as a git option.

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
