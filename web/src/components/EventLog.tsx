import { useMemo, useState } from "react";

import type { TraceEvent } from "../types";

const KINDS = ["node", "llm", "tool", "finding", "decision", "defense", "plan", "error"] as const;

function describe(e: TraceEvent): string {
  switch (e.kind) {
    case "node":
      return `${e.phase} ${e.name}${e.agent ? ` · ${e.agent} chunk ${e.chunk}` : ""}${e.ms !== undefined ? ` · ${e.ms}ms` : ""}`;
    case "llm":
      return `${e.phase} ${e.name} → ${e.model}${e.tokens !== undefined ? ` · ${e.tokens} tok` : ""}`;
    case "tool":
      return `${e.phase} ${e.name}(${e.args})`;
    case "finding":
      return `${e.agent} raised ${e.category} at ${e.file}:${e.line}`;
    case "decision":
      return `round ${e.round}: ${e.decision} ${e.category} at ${e.file}:${e.line} · ${e.note}`;
    case "defense":
      return `${e.agent} ${e.outcome} ${e.category} at ${e.file}:${e.line}`;
    case "plan":
      return `plan: ${e.tasks.length} task(s) for ${e.agents.join(", ") || "nobody"}`;
    case "review":
      return `${e.phase} review${e.verdict ? ` · ${e.verdict}` : ""}`;
    case "result":
      return `result: ${e.result.verdict}`;
    case "error":
      return `error: ${e.message}`;
  }
}

export function EventLog({ events }: { events: readonly TraceEvent[] }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set(["llm", "tool"]));
  const shown = useMemo(
    () => events.filter((e) => !hidden.has(e.kind)).slice(-400),
    [events, hidden],
  );
  const toggle = (k: string) => {
    const next = new Set(hidden);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setHidden(next);
  };
  return (
    <details className="log">
      <summary>Event log ({events.length})</summary>
      <div className="log__filters" role="group" aria-label="Event kinds">
        {KINDS.map((k) => (
          <label key={k}>
            <input type="checkbox" checked={!hidden.has(k)} onChange={() => toggle(k)} /> {k}
          </label>
        ))}
      </div>
      <ol className="log__list">
        {shown.map((e) => (
          <li key={e.seq} className={`log__item log__item--${e.kind}`}>
            <span className="log__seq">#{e.seq}</span> {describe(e)}
          </li>
        ))}
      </ol>
    </details>
  );
}
