import { useState } from "react";

import type { FindingCard, FindingStatus, TheaterState } from "../reduce";

const COLUMNS: { title: string; statuses: FindingStatus[]; hint: string }[] = [
  { title: "On trial", statuses: ["raised", "challenged"], hint: "Waiting for the critic" },
  { title: "Upheld", statuses: ["upheld"], hint: "Survived cross-examination" },
  { title: "Killed", statuses: ["killed", "withdrawn", "withheld"], hint: "Will not ship" },
];

const STATUS_LABEL: Record<FindingStatus, string> = {
  raised: "raised",
  challenged: "challenged",
  upheld: "upheld",
  killed: "killed",
  withdrawn: "withdrawn",
  withheld: "withheld (unjudged)",
};

function Card({ f }: { f: FindingCard }) {
  const [open, setOpen] = useState(false);
  const where = f.line ? `${f.file}:${f.line}` : f.file;
  return (
    <article className={`card card--${f.status}`} data-testid="finding-card" data-status={f.status}>
      <button className="card__head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`sev sev--${f.severity}`}>{f.severity}</span>
        <span className="card__title">{f.title}</span>
        <span className="card__chev" aria-hidden>
          {open ? "▾" : "▸"}
        </span>
      </button>
      <div className="card__meta">
        <code>{where}</code> · {f.agent} · <span className="badge">{STATUS_LABEL[f.status]}</span>
      </div>
      <ol className="timeline">
        {f.timeline.map((t, i) => (
          <li key={i} className={`timeline__item timeline__item--${t.tone}`}>
            <strong>{t.label}</strong>
            {open && t.detail && <span className="timeline__detail">{t.detail}</span>}
          </li>
        ))}
      </ol>
      {open && f.evidence.length > 0 && (
        <details className="evidence" open>
          <summary>Evidence</summary>
          <ul>
            {f.evidence.map((e, i) => (
              <li key={i}>
                <code>{e}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

export function TrialBoard({ state }: { state: TheaterState }) {
  const cards = state.order.map((id) => state.findings[id]);
  return (
    <section className="board" aria-label="Findings on trial">
      {COLUMNS.map((col) => {
        const items = cards.filter((c) => col.statuses.includes(c.status));
        return (
          <div className="board__col" key={col.title} data-testid={`col-${col.title}`}>
            <header className="board__head">
              <h2>{col.title}</h2>
              <span className="count">{items.length}</span>
            </header>
            <p className="board__hint">{col.hint}</p>
            {items.length === 0 ? (
              <p className="empty">Nothing here yet.</p>
            ) : (
              items.map((f) => <Card key={f.id} f={f} />)
            )}
          </div>
        );
      })}
    </section>
  );
}
