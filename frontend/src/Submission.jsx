// One submission: the decision first, then the evidence behind it.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import { Cite, Drawer, Ids, Severity, SubCtx, useSub } from "./Evidence.jsx";
import {
  DOC_TYPES, KINDS, OUTCOMES, OWNERS, ROUTES, RULE_STATUS, STATUS, VERDICTS,
  fmtTime, needsAttention, pct, titleCase,
} from "./format.js";
import { Progress } from "./Queue.jsx";

export default function Submission({ id }) {
  const [sub, setSub] = useState(null);
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [stack, setStack] = useState([]);
  const [tab, setTab] = useState("summary");

  const refresh = useCallback(async () => {
    try {
      const st = await api.status(id);
      setStatus(st);
      if (st.state === "done") setSub(await api.get(id));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, [id]);

  useEffect(() => { setSub(null); setStack([]); refresh(); }, [id, refresh]);
  useEffect(() => {
    if (!status || status.state === "done" || status.state === "failed") return;
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, [status, refresh]);

  const open = useCallback((sel) => setStack((s) => [...s, sel]), []);
  const ctx = useMemo(() => ({ sub, sid: id, open, refresh }), [sub, id, open, refresh]);

  if (error) return <Page><p className="error">{error}</p><a href="#/">Back to submissions</a></Page>;
  if (!status) return <Page><p className="muted">Loading…</p></Page>;
  if (status.state === "failed") {
    return <Page><h1>{status.name}</h1><p className="error">Processing failed: {status.error}</p></Page>;
  }
  if (!sub) {
    return (
      <Page>
        <h1>{status.name}</h1>
        <p>Processing this submission. Each step is listed as it starts.</p>
        <Progress step={status.step} label={status.step_label} />
        <Steps current={status.step} />
      </Page>
    );
  }

  const findings = sub.reconciliation?.findings || [];
  const openCount = findings.filter(needsAttention).length;
  const tabs = [
    ["summary", "Summary"],
    ["findings", `Findings ${openCount}`],
    ["rules", "Rules"],
    ["documents", `Documents ${sub.documents.length}`],
    ["audit", "Audit"],
  ];

  return (
    <SubCtx.Provider value={ctx}>
      <main className="desk">
        <a href="#/" className="back">All submissions</a>
        <nav className="folder-tabs" role="tablist">
          {tabs.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "ftab on" : "ftab"}
              onClick={() => setTab(k)}>{label}</button>
          ))}
        </nav>
        <div className="folder">
          <div className="paper" role="tabpanel">
            <FileHead sub={sub} status={status} />
            {tab === "summary" && <Summary sub={sub} setTab={setTab} />}
            {tab === "findings" && <Findings findings={findings} />}
            {tab === "rules" && <Rules decision={sub.decision} />}
            {tab === "documents" && <Documents sub={sub} />}
            {tab === "audit" && <Audit sub={sub} status={status} />}
          </div>
        </div>
      </main>
      <Drawer stack={stack} setStack={setStack} />
    </SubCtx.Provider>
  );
}

function Page({ children }) {
  return <main className="desk"><div className="folder"><div className="paper">{children}</div></div></main>;
}

const STEP_NAMES = ["Receive files", "Split into pages", "Read scans (OCR)", "Classify documents",
  "Extract facts", "Compare documents", "Agent recheck", "Apply Knight's rules", "Write the analysis"];

function Steps({ current }) {
  return (
    <ol className="steps">
      {STEP_NAMES.map((s, i) => (
        <li key={s} className={i + 1 < current ? "done" : i + 1 === current ? "now" : ""}>{s}</li>
      ))}
    </ol>
  );
}

// ------------------------------------------------------------ header and decision

function FileHead({ sub, status }) {
  const app = sub.reconciliation?.entities.find((e) => e.kind === "applicant");
  const v = (f) => app?.fields[f]?.[0]?.value;
  const d = sub.decision;
  const name = v("applicant_name") || status.name;
  return (
    <header className="file-head">
      <div>
        <h1>{titleCase(name)}</h1>
        <p className="meta">
          {v("dot_number") && <span>DOT {v("dot_number")}</span>}
          {v("mc_number") && <span>MC {v("mc_number")}</span>}
          {v("effective_date") && <span>Effective {v("effective_date")}</span>}
          {v("agency_name") && <span>{titleCase(String(v("agency_name")))}</span>}
          <span title={sub.received_name}>{sub.files.length} files received {fmtTime(status.received_at) || ""}</span>
        </p>
      </div>
      {d && (
        <section className={`disposition disp-${d.outcome}`} aria-label="Decision">
          <div className="disp-label">Disposition</div>
          <div className="disp-outcome">{OUTCOMES[d.outcome]}</div>
          <div className="disp-route">to {ROUTES[d.route]}</div>
          <div className="disp-reason" title={d.route_reason}>{shortReason(d.route_reason)}</div>
        </section>
      )}
    </header>
  );
}


function shortReason(r) {
  const m = /^(\d+) power units/.exec(r || "");
  return m ? `${m[1]} power units on schedule` : r;
}

const PAST = { refer: "Referred", decline: "Declined", proceed: "Checked" };

function RuleTags({ decision, setTab }) {
  const { open } = useSub();
  const drivers = decision.results.filter((r) => decision.reasons.includes(r.rule_id));
  const n = (k) => decision.results.filter((r) => r.status === k).length;
  return (
    <section className="rules-block">
      <div className="label-row">
        <h2 className="label">{drivers.length ? `${PAST[decision.outcome]} under ${drivers.length} rules` : "Every rule passed"}</h2>
        <span className="legend"><i className="dot dot-fail" />failed <i className="dot dot-unknown" />can't verify</span>
        <span className="legend-right">{n("pass")} passed · {n("info")} notes · <button type="button" className="linkish" onClick={() => setTab("rules")}>See all {decision.results.length} rules</button></span>
      </div>
      <div className="tags">
        {drivers.map((r) => (
          <button key={r.rule_id} type="button" className="rtag" onClick={() => open({ ref: r.rule_id })} title={r.message}>
            <i className={`dot dot-${r.status}`} /><b>{r.rule_id}</b> {r.problem || r.title}
          </button>
        ))}
      </div>
    </section>
  );
}

function Pipeline({ sub, setTab }) {
  const rec = sub.reconciliation;
  const facts = sub.extractions.reduce((n, ex) => n + [...Object.values(ex.facts),
    ...Object.values(ex.records || {}).flatMap((rows) => rows.flatMap((r) => Object.values(r)))]
    .filter((f) => f.status === "found").length, 0);
  const rechecked = (rec?.findings || []).filter((f) => f.review && f.review.verdict !== "not_run").length;
  const steps = [
    [sub.files.length, "files", "code", "documents"],
    [sub.units.length, "pages", "code", "documents"],
    [sub.documents.length, "documents", "AI", "documents"],
    [facts, "facts", "AI", "documents"],
    [rec?.findings.length || 0, "findings", "code", "findings"],
    [rechecked, "rechecked", "AI", "findings"],
    [sub.decision?.results.length || 0, "rules", "code", "rules"],
  ];
  return (
    <section>
      <h2 className="label">How this file was read</h2>
      <ol className="pipeline">
        {steps.map(([n, label, kind, tab]) => (
          <li key={label}>
            <button type="button" onClick={() => setTab(tab)}>
              <span className="pn">{n}</span>
              <span className="pl">{label}</span>
              <span className={`pk pk-${kind.toLowerCase()}`}>{kind}</span>
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

// ------------------------------------------------------------ summary (AI analysis)

function Summary({ sub, setTab }) {
  const a = sub.analysis;
  const findings = (sub.reconciliation?.findings || []).filter(needsAttention);
  const byOwner = {};
  for (const r of a?.recommendations || []) (byOwner[r.owner] ||= []).push(r);
  return (
    <>
      {sub.decision && <RuleTags decision={sub.decision} setTab={setTab} />}
      <Pipeline sub={sub} setTab={setTab} />
      <div className="columns">
        <section>
          <h2 className="label">Analysis</h2>
          {(!a || a.status !== "ok") ? (
            <p className="muted">The AI analysis did not run for this submission{sub.decision ? "; the disposition comes from the rules and does not depend on it." : "."}</p>
          ) : (
            <>
              {a.stale && <p className="notice">Findings were reviewed after this analysis was written. The disposition is current.</p>}
              <p className="summary-text">{a.summary}</p>
              <ol className="concerns">
                {a.concerns.map((c, i) => (
                  <li key={i} className={`concern c-${c.severity}`}>
                    <h3>{c.title} <Severity level={c.severity} /></h3>
                    <p>{c.detail}</p>
                    <div className="cites">{c.cites.map((id) => <Cite key={id} id={id} />)}</div>
                  </li>
                ))}
              </ol>
              <h2 className="label spaced">Next actions</h2>
              {["agent", "underwriter", "operations"].filter((o) => byOwner[o]).map((owner) => (
                <div key={owner} className="owner-group">
                  <div className="owner-head">
                    <h3>{OWNERS[owner]}</h3>
                    {owner === "agent" && <CopyRequest items={byOwner.agent} />}
                  </div>
                  <ul className="actions-list">
                    {byOwner[owner].map((r, i) => (
                      <li key={i}>
                        <p>{r.action}</p>
                        <div className="cites">{r.cites.map((id) => <Cite key={id} id={id} />)}</div>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              <p className="fine">Written by {a.model.replace(/^us\.anthropic\./, "")} from the structured results. Every item cites its evidence; uncited items are removed in code.</p>
            </>
          )}
        </section>
        <section>
          <h2 className="label">Findings needing a decision ({findings.length})</h2>
          <ul className="rows">{findings.slice(0, 8).map((f) => <FindingRow key={f.finding_id} f={f} />)}</ul>
          {findings.length > 8 && <button type="button" className="linkish more" onClick={() => setTab("findings")}>All {findings.length} findings</button>}
        </section>
      </div>
    </>
  );
}

function CopyRequest({ items }) {
  const [done, setDone] = useState(false);
  const text = "Hello,\n\nTo continue reviewing this submission we need the following:\n\n" +
    items.map((r, i) => `${i + 1}. ${r.action}`).join("\n") + "\n\nThank you.";
  return (
    <button type="button" className="btn small" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 2000); } catch { /* clipboard blocked */ }
    }}>{done ? "Copied" : "Copy as an email to the agent"}</button>
  );
}

// ------------------------------------------------------------ findings

function Findings({ findings }) {
  const groups = [
    ["Needs a decision", findings.filter(needsAttention)],
    ["Resolved", findings.filter((f) => f.status === "resolved")],
  ];
  return (
    <div>
      <p className="intro">Where the documents disagree or something is missing. The agent rechecked the ones that could be reading mistakes; open any finding to see its sources and record your decision.</p>
      {groups.map(([title, list]) => list.length > 0 && (
        <section key={title}>
          <h2 className="label spaced">{title} ({list.length})</h2>
          <ul className="rows">{list.map((f) => <FindingRow key={f.finding_id} f={f} />)}</ul>
        </section>
      ))}
    </div>
  );
}

function FindingRow({ f }) {
  const { open } = useSub();
  return (
    <li>
      <button type="button" className={`row row-${f.severity}`} onClick={() => open({ ref: f.finding_id })}>
        <span className="row-id">{f.finding_id}</span>
        <span className="row-main">
          <span className="row-title"><Ids text={f.title} /></span>
          <span className="row-meta">
            {KINDS[f.kind]}
            {f.review && f.review.verdict !== "not_run" && <span className="tag-agent">{VERDICTS[f.review.verdict]}</span>}
            {f.human && <span className="tag-human">{STATUS[f.status]} by you</span>}
          </span>
        </span>
        <Severity level={f.severity} />
      </button>
    </li>
  );
}

// ------------------------------------------------------------ rules

function Rules({ decision }) {
  const { open } = useSub();
  if (!decision) return null;
  const sections = [];
  for (const r of decision.results) {
    let s = sections.find((x) => x.name === r.section);
    if (!s) sections.push((s = { name: r.section, rules: [] }));
    s.rules.push(r);
  }
  const counts = (k) => decision.results.filter((r) => r.status === k).length;
  return (
    <div>
      <p className="intro">Knight's guideline as {decision.results.length} rules, checked in code: {counts("pass")} pass, {counts("fail")} fail, {counts("unknown")} can't be verified, {counts("info")} notes. Any failure or unverifiable item means Refer. Rules version {decision.rules_version}.</p>
      {sections.map((s) => (
        <section key={s.name}>
          <h2 className="label spaced">{s.name}</h2>
          <ul className="rows">
            {s.rules.map((r) => (
              <li key={r.rule_id}>
                <button type="button" className="row" onClick={() => open({ ref: r.rule_id })}>
                  <span className="row-id"><i className={`dot dot-${r.status}`} />{r.rule_id}</span>
                  <span className="row-main">
                    <span className="row-title">{r.title}</span>
                    {r.status !== "pass" && <span className="row-meta">{r.message}</span>}
                  </span>
                  <span className={`rule-status rs-${r.status}`}>{RULE_STATUS[r.status]}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

// ------------------------------------------------------------ documents

function Documents({ sub }) {
  const { open } = useSub();
  const exs = Object.fromEntries(sub.extractions.map((e) => [e.doc_id, e]));
  const all = (ex) => [...Object.values(ex?.facts || {}),
    ...Object.values(ex?.records || {}).flatMap((rows) => rows.flatMap((r) => Object.values(r)))];
  const count = (ex, st) => all(ex).filter((f) => f.status === st).length;
  const dups = sub.files.filter((f) => f.duplicate_of).length;
  return (
    <div>
      <p className="intro">{sub.files.length} files{dups ? ` (${dups} duplicates set aside)` : ""} became {sub.units.length} pages and sheets, regrouped by content into {sub.documents.length} documents. File names are never trusted for the type.</p>
      <div className="table-scroll">
        <table className="grid docs">
          <thead><tr><th>Document</th><th>Type</th><th>Confidence</th><th>Pages</th><th>Facts</th><th>Notes</th></tr></thead>
          <tbody>
            {sub.documents.map((d) => {
              const ex = exs[d.doc_id];
              return (
                <tr key={d.doc_id}>
                  <td><button type="button" className="cell-link" onClick={() => open({ ref: d.doc_id })}>{d.title}</button></td>
                  <td>{DOC_TYPES[d.doc_type]}</td>
                  <td className={d.confidence < 0.7 ? "flagged" : ""}>{pct(d.confidence)}</td>
                  <td>{d.unit_ids.length}</td>
                  <td>{ex?.status === "ok" ? `${count(ex, "found")} found${count(ex, "missing") ? `, ${count(ex, "missing")} not stated` : ""}` : ex?.status.replace("_", " ")}</td>
                  <td className="small">{d.observations.filter((o) => /suggests|Hidden/.test(o)).join(" ")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ------------------------------------------------------------ audit trail

const EVENT_NAMES = { intake: "Receive files", preprocess: "Split into pages", ocr: "Read scans (OCR)",
  classify: "Classify documents", extract: "Extract facts", reconcile: "Compare documents",
  recheck: "Agent recheck", rules: "Apply Knight's rules", analysis: "Write the analysis", review: "Underwriter review" };

function Audit({ sub, status }) {
  return (
    <div>
      <p className="intro">Every step, when it ran and what it produced. Mode: {status.mode || "demo"}.</p>
      <ol className="audit">
        {sub.events.map((e, i) => {
          const { step, at, ms, ...rest } = e;
          return (
            <li key={i}>
              <div className="audit-head">
                <strong>{EVENT_NAMES[step] || step}</strong>
                <span className="muted small">{fmtTime(at)}{ms !== undefined ? `, ${(ms / 1000).toFixed(1)} s` : ""}</span>
              </div>
              <code className="audit-detail">{summarize(rest)}</code>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function summarize(obj) {
  return Object.entries(obj).map(([k, v]) =>
    `${k.replace(/_/g, " ")}: ${typeof v === "object" && v !== null
      ? Array.isArray(v) ? v.join(", ") : Object.entries(v).filter(([, x]) => x).map(([a, b]) => `${a} ${b}`).join(", ")
      : v}`).join("   ");
}
