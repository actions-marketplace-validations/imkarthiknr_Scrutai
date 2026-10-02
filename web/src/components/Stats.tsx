import type { TheaterState } from "../reduce";
import { counts } from "../reduce";

function Stat({ label, value, testId }: { label: string; value: string | number; testId?: string }) {
  return (
    <div className="stat" data-testid={testId}>
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
    </div>
  );
}

export function Stats({ state }: { state: TheaterState }) {
  const c = counts(state);
  const raised = state.order.length;
  const dropped = c.killed + c.withdrawn + c.withheld;
  const secs = state.startTs !== null && state.lastTs !== null ? state.lastTs - state.startTs : null;
  const elapsed = secs === null ? "–" : secs < 1 ? `${Math.round(secs * 1000)}ms` : `${secs.toFixed(1)}s`;
  const cost = state.result ? `$${state.result.cost_usd.toFixed(4)}` : "–";
  return (
    <div className="stats" aria-label="Run statistics">
      <Stat label="LLM calls" value={state.llmCalls} />
      <Stat label="tool calls" value={state.toolCalls} />
      <Stat label="tokens" value={state.tokens.toLocaleString()} />
      <Stat label="cost" value={cost} />
      <Stat label="elapsed" value={elapsed} />
      <Stat label="raised" value={raised} />
      <Stat label="killed by critic" value={raised ? `${dropped} (${Math.round((dropped / raised) * 100)}%)` : 0} testId="stat-killed" />
      <div className={`verdict verdict--${state.verdict ?? state.status}`} data-testid="verdict">
        {state.verdict ? state.verdict.replace("_", " ") : state.status}
      </div>
    </div>
  );
}
