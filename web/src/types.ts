// Shapes of the trace events `scrutai serve` streams (see src/scrutai/trace.py).

export type Severity = "info" | "low" | "medium" | "high" | "critical";
export type Verdict = "approve" | "comment" | "request_changes";

export interface FindingRef {
  finding: string; // stable id: file:line:category
  agent: string;
  category: string;
  title: string;
  file: string;
  line: number | null;
  severity: Severity;
  confidence: number;
}

interface Base {
  run: string;
  seq: number;
  ts: number;
}

export type TraceEvent =
  | (Base & { kind: "review"; phase: "start" | "end"; name: string; id: string; files?: number; verdict?: Verdict; kept?: number; dropped?: number; tokens?: number; ms?: number; error?: string })
  | (Base & { kind: "node"; phase: "start" | "end"; name: string; id: string; agent?: string; chunk?: number; files?: string[]; findings?: number; ms?: number; error?: string })
  | (Base & { kind: "llm"; phase: "start" | "end"; name: string; id: string; model: string; tokens?: number; ms?: number; error?: string })
  | (Base & { kind: "tool"; phase: "start" | "end"; name: string; id: string; args: string; chars?: number; ms?: number })
  | (Base & { kind: "plan"; agents: string[]; tasks: { agent: string; chunk: number; files: string[] }[] })
  | (Base & FindingRef & { kind: "finding"; evidence: string[] })
  | (Base & FindingRef & { kind: "decision"; round: number; decision: "uphold" | "downgrade" | "kill" | "challenge"; note: string })
  | (Base & FindingRef & { kind: "defense"; outcome: "defended" | "withdrawn" | "silent"; argument: string | null })
  | (Base & { kind: "result"; result: ReviewResult })
  | (Base & { kind: "error"; message: string });

export interface ResultFinding {
  agent: string;
  title: string;
  body: string;
  file: string;
  line: number | null;
  category: string;
  severity: Severity;
  confidence: number;
  evidence: string[];
  critic_note: string | null;
  alive: boolean;
  unjudged: boolean;
  history: string[];
}

export interface ReviewResult {
  verdict: Verdict;
  findings: ResultFinding[];
  dropped: ResultFinding[];
  summary: string;
  tokens_used: number;
  cost_usd: number;
  rounds: number;
  agents: string[];
  budget_exhausted: boolean;
}

export interface RunSummary {
  id: string;
  source: string;
  label: string;
  created: number;
  status: "running" | "done" | "error";
  error: string | null;
  events: number;
  verdict: Verdict | null;
}
