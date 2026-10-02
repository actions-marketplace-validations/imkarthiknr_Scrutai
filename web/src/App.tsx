import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, follow, type RunSource } from "./api";
import { EventLog } from "./components/EventLog";
import { Graph } from "./components/Graph";
import { Stats } from "./components/Stats";
import { TrialBoard } from "./components/TrialBoard";
import { reduce } from "./reduce";
import type { RunSummary, TraceEvent } from "./types";

type Source = RunSource["source"];

function SourceForm({ onStart, busy }: { onStart: (s: RunSource) => void; busy: boolean }) {
  const [source, setSource] = useState<Source>("demo");
  const [base, setBase] = useState("main");
  const [head, setHead] = useState("HEAD");
  const [patch, setPatch] = useState("");
  const [pr, setPr] = useState("");

  const submit = (ev: React.FormEvent) => {
    ev.preventDefault();
    if (source === "demo") onStart({ source });
    else if (source === "git") onStart({ source, base, head });
    else if (source === "patch") onStart({ source, patch });
    else onStart({ source, pr: Number(pr) });
  };
  return (
    <form className="source" onSubmit={submit} aria-label="Start a review">
      <div className="tabs" role="tablist">
        {(["demo", "git", "patch", "pr"] as Source[]).map((s) => (
          <button
            type="button"
            role="tab"
            key={s}
            aria-selected={source === s}
            className={source === s ? "tab tab--on" : "tab"}
            onClick={() => setSource(s)}
          >
            {{ demo: "Demo", git: "Git range", patch: "Patch", pr: "Pull request" }[s]}
          </button>
        ))}
      </div>
      <div className="source__fields">
        {source === "demo" && <p className="muted">A small file with real bugs and planted traps.</p>}
        {source === "git" && (
          <>
            <label>
              base <input value={base} onChange={(e) => setBase(e.target.value)} size={14} />
            </label>
            <label>
              head <input value={head} onChange={(e) => setHead(e.target.value)} size={14} />
            </label>
          </>
        )}
        {source === "patch" && (
          <textarea
            value={patch}
            onChange={(e) => setPatch(e.target.value)}
            placeholder="Paste a unified diff (git diff output)"
            rows={4}
            aria-label="Unified diff"
          />
        )}
        {source === "pr" && (
          <label>
            PR # <input value={pr} onChange={(e) => setPr(e.target.value)} inputMode="numeric" size={6} />
          </label>
        )}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Reviewing…" : "Start review"}
        </button>
      </div>
    </form>
  );
}

export default function App() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [live, setLive] = useState(false);
  const [cursor, setCursor] = useState<number | null>(null); // null = show everything
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [mode, setMode] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const unsubscribe = useRef<(() => void) | null>(null);

  const refreshRuns = useCallback(() => {
    api.runs().then(setRuns).catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshRuns();
    api.health().then((h) => setMode(h.llm_mode)).catch(() => setMode("offline"));
  }, [refreshRuns]);

  const open = useCallback(
    (id: string) => {
      unsubscribe.current?.();
      setRunId(id);
      setEvents([]);
      setCursor(null);
      setPlaying(false);
      setLive(true);
      setError(null);
      const buffer: TraceEvent[] = [];
      let frame = 0;
      const flush = () => {
        frame = 0;
        setEvents((prev) => prev.concat(buffer.splice(0)));
      };
      unsubscribe.current = follow(
        id,
        (e) => {
          buffer.push(e);
          frame ||= requestAnimationFrame(flush); // batch bursts into one render
        },
        () => {
          if (frame) cancelAnimationFrame(frame);
          flush();
          setLive(false);
          refreshRuns();
        },
      );
    },
    [refreshRuns],
  );

  useEffect(() => () => unsubscribe.current?.(), []);

  const start = async (body: RunSource) => {
    try {
      const run = await api.start(body);
      refreshRuns();
      open(run.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const upload = async (file: File) => {
    try {
      const run = await api.replay(await file.text());
      refreshRuns();
      open(run.id);
      setCursor(0);
      setPlaying(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Playback: advance the cursor through recorded events.
  useEffect(() => {
    if (!playing || cursor === null) return;
    if (cursor >= events.length) {
      setPlaying(false);
      setCursor(null);
      return;
    }
    const t = setTimeout(() => setCursor(Math.min(events.length, cursor + speed)), 80);
    return () => clearTimeout(t);
  }, [playing, cursor, events.length, speed]);

  const visible = useMemo(() => (cursor === null ? events : events.slice(0, cursor)), [events, cursor]);
  const state = useMemo(() => reduce(visible), [visible]);
  const position = cursor ?? events.length;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo" aria-hidden>
            ✓
          </span>
          <div>
            <h1>Scrutai Theater</h1>
            <p className="muted">Specialists raise findings. The critic puts every one on trial.</p>
          </div>
        </div>
        <div className="top__right">
          <span className={`pill pill--${mode}`} title="llm_mode from .scrutai.yml">
            {mode || "…"} mode
          </span>
          <label className="runs">
            <span className="sr-only">Previous runs</span>
            <select value={runId ?? ""} onChange={(e) => e.target.value && open(e.target.value)}>
              <option value="">Runs ({runs.length})</option>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label} · {r.status === "done" ? (r.verdict ?? "done").replace("_", " ") : r.status}
                </option>
              ))}
            </select>
          </label>
          <label className="file">
            Replay trace…
            <input
              type="file"
              accept=".jsonl,application/x-ndjson,text/plain"
              onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
            />
          </label>
        </div>
      </header>

      <SourceForm onStart={start} busy={live} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {runId === null ? (
        <section className="welcome">
          <h2>Start a review to watch the panel work</h2>
          <p>
            Every finding a specialist raises goes on trial. The critic checks it against the cited
            code and upholds, downgrades, challenges or kills it. Challenged findings go back to
            their specialist to defend with fresh evidence, or withdraw.
          </p>
        </section>
      ) : (
        <>
          <Stats state={state} />
          {state.error && (
            <p className="error" role="alert">
              {state.error}
            </p>
          )}
          {state.result?.summary && <p className="summary">{state.result.summary}</p>}
          <section className="playback" aria-label="Playback">
            <button
              onClick={() => {
                if (cursor === null || cursor >= events.length) setCursor(0);
                setPlaying(!playing);
              }}
              disabled={live || events.length === 0}
            >
              {playing ? "Pause" : "Replay"}
            </button>
            <input
              type="range"
              min={0}
              max={events.length}
              value={position}
              disabled={live}
              aria-label="Scrub through events"
              onChange={(e) => {
                setPlaying(false);
                const v = Number(e.target.value);
                setCursor(v >= events.length ? null : v);
              }}
            />
            <span className="muted">
              {position}/{events.length}
            </span>
            <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} aria-label="Playback speed">
              <option value={1}>1×</option>
              <option value={4}>4×</option>
              <option value={16}>16×</option>
            </select>
            {live && <span className="live">● live</span>}
          </section>
          <div className="stage">
            <section className="panel panel--graph" aria-label="Review graph">
              <Graph state={state} />
            </section>
            <TrialBoard state={state} />
          </div>
          <EventLog events={visible} />
        </>
      )}
    </div>
  );
}
