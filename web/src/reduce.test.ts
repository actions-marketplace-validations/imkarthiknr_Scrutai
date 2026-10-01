import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { counts, reduce } from "./reduce";
import type { TraceEvent } from "./types";

// A real trace recorded from `scrutai review --demo --trace`.
const events: TraceEvent[] = readFileSync(
  new URL("./__fixtures__/demo.jsonl", import.meta.url),
  "utf8",
)
  .trim()
  .split("\n")
  .map((line) => JSON.parse(line));

describe("reduce", () => {
  it("ends where the Python result says it ends", () => {
    const s = reduce(events);
    expect(s.status).toBe("done");
    expect(s.verdict).toBe("request_changes");
    const c = counts(s);
    expect(c.upheld).toBe(s.result!.findings.length);
    expect(c.killed + c.withdrawn + c.withheld).toBe(s.result!.dropped.length);
    expect(c.raised + c.challenged).toBe(0); // nothing left undecided
    expect(s.tokens).toBe(s.result!.tokens_used);
  });

  it("tells each finding's trial in order", () => {
    const s = reduce(events);
    const killed = Object.values(s.findings).find((f) => f.status === "killed")!;
    expect(killed.timeline[0].label).toMatch(/^Raised by /);
    expect(killed.timeline.at(-1)!.tone).toBe("bad");
    const debated = Object.values(s.findings).find((f) => f.timeline.some((t) => /defended/.test(t.label)))!;
    expect(debated.timeline.map((t) => t.label)).toEqual([
      expect.stringMatching(/^Raised/),
      expect.stringMatching(/^Round 1: challenge/),
      expect.stringMatching(/defended it$/),
      expect.stringMatching(/^Round 2: uphold/),
    ]);
  });

  it("shows work in progress partway through", () => {
    const firstLlm = events.findIndex((e) => e.kind === "llm");
    const s = reduce(events.slice(0, firstLlm + 1));
    expect(s.status).toBe("running");
    expect(s.activeCalls).toBe(1);
    expect(Object.values(s.agents).flat().some((t) => t.status === "active")).toBe(true);
    expect(s.verdict).toBeNull();
  });

  it("marks stages that never ran as skipped once the review ends", () => {
    const docsOnly: TraceEvent[] = [
      { run: "r", seq: 1, ts: 1, kind: "review", phase: "start", name: "diff", id: "a" },
      { run: "r", seq: 2, ts: 1, kind: "node", phase: "start", name: "route", id: "b" },
      { run: "r", seq: 3, ts: 1, kind: "node", phase: "end", name: "route", id: "b", ms: 1 },
      { run: "r", seq: 4, ts: 2, kind: "review", phase: "end", name: "diff", id: "a", verdict: "approve" },
    ];
    const s = reduce(docsOnly);
    expect(s.stages.route.status).toBe("done");
    expect(s.stages.defend.status).toBe("skipped");
  });

  it("surfaces errors", () => {
    const s = reduce([{ run: "r", seq: 1, ts: 1, kind: "error", message: "invalid git ref '--x'" }]);
    expect(s.status).toBe("error");
    expect(s.error).toContain("invalid git ref");
  });
});
