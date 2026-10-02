import type { Activity, Stage, TheaterState } from "../reduce";
import { ALL_AGENTS } from "../reduce";

// Fixed layout in SVG user units; the viewBox scales it to any width.
const W = 720;
const ROW = 60;
const TOP = 16;
const COL = { route: 52, agents: 236, collect: 420, critic: 548, verdict: 662 };
const VERDICT_LABEL: Record<string, string> = {
  approve: "approve",
  comment: "comment",
  request_changes: "changes req.",
};

type EdgeState = "idle" | "active" | "done";

function tone(status: Activity): string {
  return `node node--${status}`;
}

function Box(props: {
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  sub?: string;
  status: Activity;
  title?: string;
}) {
  const { x, y, w, h, label, sub, status, title } = props;
  return (
    <g className={tone(status)} transform={`translate(${x - w / 2},${y - h / 2})`}>
      <title>{title ?? label}</title>
      <rect width={w} height={h} rx={10} />
      <text x={w / 2} y={sub ? h / 2 - 3 : h / 2 + 4} className="node__label">
        {label}
      </text>
      {sub && (
        <text x={w / 2} y={h / 2 + 13} className="node__sub">
          {sub}
        </text>
      )}
    </g>
  );
}

function edge(x1: number, y1: number, x2: number, y2: number, state: EdgeState, key: string) {
  const mx = (x1 + x2) / 2;
  return <path key={key} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} className={`edge edge--${state}`} />;
}

function flow(from: Activity, to: Activity): EdgeState {
  if (to === "active") return "active";
  if (to === "done" && from === "done") return "done";
  return "idle";
}

export function Graph({ state }: { state: TheaterState }) {
  const agents = ALL_AGENTS;
  const height = TOP * 2 + agents.length * ROW + 24;
  const mid = TOP + (agents.length * ROW) / 2;
  const st = (s: Stage) => state.stages[s].status;
  const agentStatus = (a: string): Activity => {
    const tasks = state.agents[a] ?? [];
    if (!state.selected.includes(a)) return state.selected.length || state.status === "done" ? "skipped" : "idle";
    if (tasks.some((t) => t.status === "active")) return "active";
    if (tasks.length && tasks.every((t) => t.status === "done")) return "done";
    return "idle";
  };

  return (
    <svg
      className="graph"
      viewBox={`0 0 ${W} ${height}`}
      role="img"
      aria-label="Review graph: route, specialists, collect, critic and defend, verdict"
    >
      {agents.map((a, i) => {
        const y = TOP + i * ROW + ROW / 2;
        const status = agentStatus(a);
        return [
          edge(COL.route + 45, mid, COL.agents - 75, y, flow(st("route"), status), `in-${a}`),
          edge(COL.agents + 75, y, COL.collect - 42, mid, flow(status, st("collect") === "idle" ? "idle" : status), `out-${a}`),
        ];
      })}
      {edge(COL.collect + 42, mid, COL.critic - 46, mid, flow(st("collect"), st("critic")), "c-critic")}
      {edge(COL.critic + 46, mid, COL.verdict - 55, mid, flow(st("critic"), st("verdict")), "c-verdict")}
      <path
        className={`edge edge--${st("defend") === "active" ? "active" : st("defend") === "done" ? "done" : "loop"}`}
        d={`M${COL.critic - 20},${mid + 28} C${COL.critic - 44},${mid + 86} ${COL.critic + 44},${mid + 86} ${COL.critic + 20},${mid + 28}`}
      />

      <Box x={COL.route} y={mid} w={90} h={46} label="route" status={st("route")} />
      {agents.map((a, i) => {
        const y = TOP + i * ROW + ROW / 2;
        const tasks = state.agents[a] ?? [];
        const found = tasks.reduce((n, t) => n + t.findings, 0);
        const status = agentStatus(a);
        return (
          <g key={a}>
            <Box
              x={COL.agents}
              y={y}
              w={150}
              h={46}
              label={a}
              sub={status === "skipped" ? "not needed" : tasks.length ? `${tasks.length} chunk${tasks.length > 1 ? "s" : ""} · ${found} raised` : undefined}
              status={status}
              title={`${a}: ${tasks.map((t) => `chunk ${t.chunk} (${t.files.join(", ")})`).join("; ") || "not routed"}`}
            />
            {tasks.slice(0, 12).map((t, j) => (
              <rect
                key={t.chunk}
                className={`chip chip--${t.status}`}
                x={COL.agents - 70 + j * 12}
                y={y + 25}
                width={10}
                height={5}
                rx={2}
              />
            ))}
          </g>
        );
      })}
      <Box x={COL.collect} y={mid} w={84} h={46} label="collect" status={st("collect")} />
      <Box
        x={COL.critic}
        y={mid}
        w={92}
        h={52}
        label="critic"
        sub={state.criticRound ? `round ${state.criticRound}` : undefined}
        status={st("critic")}
      />
      <text x={COL.critic} y={mid + 100} className={`loop-label loop-label--${st("defend")}`}>
        defend ↺
      </text>
      <Box
        x={COL.verdict}
        y={mid}
        w={110}
        h={46}
        label="verdict"
        sub={state.verdict ? VERDICT_LABEL[state.verdict] : undefined}
        status={st("verdict")}
      />
    </svg>
  );
}
