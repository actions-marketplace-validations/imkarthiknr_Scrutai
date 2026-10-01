import type { RunSummary, TraceEvent } from "./types";

export type RunSource =
  | { source: "demo" }
  | { source: "git"; base: string; head: string }
  | { source: "patch"; patch: string }
  | { source: "pr"; pr: number };

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail ? String(body.detail) : `${resp.status} ${resp.statusText}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => fetch("/api/health").then(json<{ ok: boolean; llm_mode: string; agents: string[] }>),
  runs: () => fetch("/api/runs").then(json<RunSummary[]>),
  start: (body: RunSource) =>
    fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<RunSummary>),
  replay: (trace: string) =>
    fetch("/api/replay", { method: "POST", body: trace }).then(json<RunSummary>),
};

/** Subscribe to a run's events. Returns an unsubscribe function. */
export function follow(
  runId: string,
  onEvent: (e: TraceEvent) => void,
  onEnd: (status: string) => void,
): () => void {
  const source = new EventSource(`/api/runs/${runId}/events`);
  source.onmessage = (msg) => onEvent(JSON.parse(msg.data) as TraceEvent);
  source.addEventListener("end", (msg) => {
    source.close();
    onEnd(JSON.parse((msg as MessageEvent).data).status);
  });
  source.onerror = () => {
    // EventSource reconnects on its own (resuming via Last-Event-ID); only give
    // up once the browser has closed the stream for good.
    if (source.readyState === EventSource.CLOSED) onEnd("error");
  };
  return () => source.close();
}
