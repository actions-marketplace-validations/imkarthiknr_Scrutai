// Fold a stream of trace events into everything the theater draws.
// Pure and deterministic: the same events always give the same picture, so a
// live run, a replay and a scrubbed position all render identically.

import type { ReviewResult, Severity, TraceEvent, Verdict } from "./types";

export const STAGES = ["route", "collect", "critic", "defend", "verdict"] as const;
export type Stage = (typeof STAGES)[number];
export const ALL_AGENTS = ["security", "correctness", "tests", "performance", "style"];

export type Activity = "idle" | "active" | "done" | "skipped";

export interface StageState {
  status: Activity;
  runs: number;
  ms: number;
}

export interface TaskState {
  chunk: number;
  files: string[];
  status: Activity;
  findings: number;
}

export type FindingStatus = "raised" | "challenged" | "upheld" | "killed" | "withdrawn" | "withheld";

export interface TimelineEntry {
  seq: number;
  label: string;
  detail: string;
  tone: "neutral" | "warn" | "good" | "bad";
}

export interface FindingCard {
  id: string;
  agent: string;
  category: string;
  title: string;
  file: string;
  line: number | null;
  severity: Severity;
  confidence: number;
  status: FindingStatus;
  evidence: string[];
  timeline: TimelineEntry[];
}

export interface TheaterState {
  runId: string | null;
  status: "idle" | "running" | "done" | "error";
  startTs: number | null;
  lastTs: number | null;
  stages: Record<Stage, StageState>;
  agents: Record<string, TaskState[]>;
  selected: string[];
  findings: Record<string, FindingCard>;
  order: string[];
  llmCalls: number;
  toolCalls: number;
  tokens: number;
  activeCalls: number;
  criticRound: number;
  verdict: Verdict | null;
  result: ReviewResult | null;
  error: string | null;
}

export function initialState(): TheaterState {
  const stages = Object.fromEntries(
    STAGES.map((s) => [s, { status: "idle", runs: 0, ms: 0 } satisfies StageState]),
  ) as Record<Stage, StageState>;
  return {
    runId: null,
    status: "idle",
    startTs: null,
    lastTs: null,
    stages,
    agents: {},
    selected: [],
    findings: {},
    order: [],
    llmCalls: 0,
    toolCalls: 0,
    tokens: 0,
    activeCalls: 0,
    criticRound: 0,
    verdict: null,
    result: null,
    error: null,
  };
}

const pct = (x: number) => `${Math.round(x * 100)}%`;

function decisionEntry(decision: string, note: string, confidence: number, round: number): TimelineEntry {
  const label = `Round ${round}: ${decision}`;
  const tone: TimelineEntry["tone"] =
    decision === "kill" ? "bad" : decision === "challenge" ? "warn" : "good";
  return { seq: 0, label, detail: `${note} (confidence ${pct(confidence)})`, tone };
}

function findingKey(f: { file: string; line: number | null; category: string }): string {
  return `${f.file}:${f.line}:${f.category}`;
}

/** Apply one event. Mutates `s` in place; callers own a fresh copy (see reduce). */
export function apply(s: TheaterState, e: TraceEvent): void {
  s.runId ??= e.run;
  s.startTs ??= e.ts;
  s.lastTs = e.ts;
  if (s.status === "idle") s.status = "running";

  switch (e.kind) {
    case "plan": {
      s.selected = e.agents;
      for (const t of e.tasks) {
        (s.agents[t.agent] ??= []).push({ chunk: t.chunk, files: t.files, status: "idle", findings: 0 });
      }
      for (const a of ALL_AGENTS) s.agents[a] ??= [];
      break;
    }
    case "node": {
      if (e.name === "specialist" && e.agent !== undefined) {
        const tasks = (s.agents[e.agent] ??= []);
        let task = tasks.find((t) => t.chunk === e.chunk);
        if (!task) {
          task = { chunk: e.chunk ?? 0, files: e.files ?? [], status: "idle", findings: 0 };
          tasks.push(task);
        }
        task.status = e.phase === "start" ? "active" : "done";
        if (e.phase === "end") task.findings = e.findings ?? 0;
        break;
      }
      const stage = s.stages[e.name as Stage];
      if (!stage) break;
      if (e.phase === "start") {
        stage.status = "active";
        stage.runs += 1;
        if (e.name === "critic") s.criticRound = stage.runs;
      } else {
        stage.status = "done";
        stage.ms += e.ms ?? 0;
      }
      break;
    }
    case "llm":
      if (e.phase === "start") {
        s.llmCalls += 1;
        s.activeCalls += 1;
      } else {
        s.activeCalls = Math.max(0, s.activeCalls - 1);
        s.tokens += e.tokens ?? 0;
      }
      break;
    case "tool":
      if (e.phase === "start") s.toolCalls += 1;
      break;
    case "finding": {
      if (!s.findings[e.finding]) s.order.push(e.finding);
      s.findings[e.finding] = {
        id: e.finding,
        agent: e.agent,
        category: e.category,
        title: e.title,
        file: e.file,
        line: e.line,
        severity: e.severity,
        confidence: e.confidence,
        status: "raised",
        evidence: e.evidence,
        timeline: [
          {
            seq: e.seq,
            label: `Raised by ${e.agent}`,
            detail: `${e.severity} · confidence ${pct(e.confidence)}`,
            tone: "neutral",
          },
        ],
      };
      break;
    }
    case "decision": {
      const card = s.findings[e.finding];
      if (!card) break;
      card.confidence = e.confidence;
      card.severity = e.severity;
      card.timeline.push({ ...decisionEntry(e.decision, e.note, e.confidence, e.round), seq: e.seq });
      card.status = e.decision === "kill" ? "killed" : e.decision === "challenge" ? "challenged" : "upheld";
      break;
    }
    case "defense": {
      const card = s.findings[e.finding];
      if (!card) break;
      const withdrawn = e.outcome === "withdrawn";
      card.timeline.push({
        seq: e.seq,
        label: withdrawn ? `${e.agent} withdrew it` : `${e.agent} defended it`,
        detail: e.argument ?? "",
        tone: withdrawn ? "bad" : "neutral",
      });
      if (withdrawn) card.status = "withdrawn";
      break;
    }
    case "result": {
      // The verdict is the source of truth for where every finding ended up.
      s.result = e.result;
      s.verdict = e.result.verdict;
      const kept = new Set(e.result.findings.map(findingKey));
      for (const f of e.result.dropped) {
        const card = s.findings[findingKey(f)];
        if (!card) continue;
        if (f.unjudged) card.status = "withheld";
        else if (card.status !== "withdrawn") card.status = "killed";
      }
      for (const id of kept) {
        const card = s.findings[id];
        if (card) card.status = "upheld";
      }
      break;
    }
    case "review":
      if (e.phase === "end") {
        s.status = e.error ? "error" : "done";
        if (e.error) s.error = e.error;
        for (const stage of Object.values(s.stages)) if (stage.status === "idle") stage.status = "skipped";
      }
      break;
    case "error":
      s.status = "error";
      s.error = e.message;
      break;
  }
}

export function reduce(events: readonly TraceEvent[]): TheaterState {
  const s = initialState();
  for (const e of events) apply(s, e);
  return s;
}

export const FINAL: FindingStatus[] = ["upheld", "killed", "withdrawn", "withheld"];

export function counts(s: TheaterState): Record<FindingStatus, number> {
  const out: Record<FindingStatus, number> = {
    raised: 0,
    challenged: 0,
    upheld: 0,
    killed: 0,
    withdrawn: 0,
    withheld: 0,
  };
  for (const id of s.order) out[s.findings[id].status] += 1;
  return out;
}
