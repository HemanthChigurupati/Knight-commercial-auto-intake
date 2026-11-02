// The evidence drawer: open any citation, fact or finding and see where it came from,
// down to the source page and the quoted text.
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import {
  DOC_TYPES, KINDS, OUTCOMES, RULE_STATUS, STATUS, TOOLS, VERDICTS,
  findClaim, fmtTime, fmtValue, isDocId, isFindingId, isRuleId, pct,
} from "./format.js";

export const SubCtx = createContext(null);
export const useSub = () => useContext(SubCtx);

// ------------------------------------------------------------ small shared pieces

export function Cite({ id }) {
  const { open, sub } = useSub();
  const label = isRuleId(id) ? `Rule ${id}` : isDocId(id) ? docTitle(sub, id) : id;
  return (
    <button type="button" className="cite" onClick={() => open({ ref: id })} title={`Show ${id}`}>
      {label}
    </button>
  );
}

// License numbers, VINs, claim and policy numbers in a face where 0 and O differ.
const ID_TOKEN = /\b(?=[A-Z0-9]*\d)[A-Z0-9]{7,}\b/g;
export function Ids({ text }) {
  if (text === null || text === undefined) return null;
  const str = String(text);
  const out = [];
  let last = 0;
  for (const m of str.matchAll(ID_TOKEN)) {
    if (m.index > last) out.push(str.slice(last, m.index));
    out.push(<span key={m.index} className="id">{m[0]}</span>);
    last = m.index + m[0].length;
  }
  out.push(str.slice(last));
  return <>{out}</>;
}

export function Severity({ level }) {
  return <span className={`sev sev-${level}`}>{level}</span>;
}

function docTitle(sub, id) {
  return sub.documents.find((d) => d.doc_id === id)?.title || id;
}

// One observed value and where it was read.
export function Source({ obs, showValue = true }) {
  const { open } = useSub();
  return (
    <div className="source">
      {showValue && <div className="source-value"><Ids text={fmtValue(obs.value)} /></div>}
      <div className="source-where">
        {obs.doc_id ? (
          <button type="button" className="linkish" onClick={() => open({ ref: obs.doc_id })}>{obs.source}</button>
        ) : (
          <span>{obs.source}</span>
        )}
        {obs.unit_id && (
          <button type="button" className="linkish quiet" onClick={() => open({ unit: obs.unit_id, quote: obs.quote })}>
            Show on page
          </button>
        )}
      </div>
      {obs.quote && <blockquote className="quote"><Ids text={obs.quote} /></blockquote>}
    </div>
  );
}

// ------------------------------------------------------------ the drawer

export function Drawer({ stack, setStack }) {
  const ref = useRef(null);
  const current = stack[stack.length - 1];
  useEffect(() => {
    if (!current) return;
    ref.current?.focus();
    const onKey = (e) => e.key === "Escape" && setStack([]);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, setStack]);
  if (!current) return null;
  return (
    <>
      <div className="scrim" onClick={() => setStack([])} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Evidence" tabIndex={-1} ref={ref}>
        <div className="drawer-bar">
          {stack.length > 1 ? (
            <button type="button" className="btn-plain" onClick={() => setStack(stack.slice(0, -1))}>Back</button>
          ) : <span />}
          <button type="button" className="btn-plain" onClick={() => setStack([])} aria-label="Close">Close</button>
        </div>
        <div className="drawer-body" key={stack.length}>
          <Selection sel={current} />
        </div>
      </aside>
    </>
  );
}

function Selection({ sel }) {
  const { sub } = useSub();
  if (sel.unit) return <UnitView unitId={sel.unit} quote={sel.quote} />;
  const id = sel.ref;
  if (isFindingId(id)) {
    const f = sub.reconciliation?.findings.find((x) => x.finding_id === id);
    if (f) return <FindingView f={f} />;
  }
  if (isRuleId(id)) {
    const r = sub.decision?.results.find((x) => x.rule_id === id);
    if (r) return <RuleView r={r} />;
  }
  if (isDocId(id)) {
    const d = sub.documents.find((x) => x.doc_id === id);
    if (d) return <DocView d={d} />;
  }
  const claim = findClaim(sub, id);
  if (claim) return <ClaimView {...claim} />;
  return <p className="muted">Nothing in this submission has the id {id}.</p>;
}

// ------------------------------------------------------------ finding

function FindingView({ f }) {
  const r = f.review;
  return (
    <article>
      <p className="kicker">{f.finding_id} <Severity level={f.severity} /> {KINDS[f.kind]}</p>
      <h2><Ids text={f.title} /></h2>
      <p className="status-line">Status: <strong>{STATUS[f.status] || f.status}</strong>
        {f.human && <> after your review</>}</p>
      {f.detail && <p><Ids text={f.detail} /></p>}

      {f.observations.length > 0 && (
        <section>
          <h3>What each document says</h3>
          {f.observations.map((o, i) => <Source key={i} obs={o} />)}
        </section>
      )}

      {r && r.verdict !== "not_run" && (
        <section>
          <h3>{VERDICTS[r.verdict] || r.verdict}{r.confidence ? `, ${pct(r.confidence)} sure` : ""}</h3>
          <ol className="trace">
            {r.steps.filter((s) => s.tool !== "record_verdict").map((s, i) => <TraceStep key={i} step={s} />)}
          </ol>
          <p><Ids text={r.explanation} /></p>
          {r.corrected_value && (
            <p className="callout">Correct value: <strong className="id">{r.corrected_value}</strong> (from {r.corrected_source})</p>
          )}
        </section>
      )}

      <ReviewBox f={f} />
    </article>
  );
}

function TraceStep({ step }) {
  const { open } = useSub();
  const unit = step.input?.unit_id;
  return (
    <li>
      <span className="trace-tool">{TOOLS[step.tool] || step.tool}</span>{" "}
      {unit ? (
        <button type="button" className="linkish" onClick={() => open({ unit })}>{unit}</button>
      ) : (
        <span>{Object.values(step.input || {}).join(", ")}</span>
      )}
      <div className="trace-result">{step.result}</div>
    </li>
  );
}

function ReviewBox({ f }) {
  const { sid, refresh, sub } = useSub();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  async function act(action) {
    setBusy(true);
    setMsg(null);
    try {
      const res = await api.review(sid, f.finding_id, action, note);
      const before = OUTCOMES[res.outcome_before], after = OUTCOMES[res.outcome_after];
      setMsg({
        ok: true,
        text: before === after
          ? `Saved. The decision stays ${after}; ${res.reasons_after.length} rules still drive it.`
          : `Saved. The decision changed from ${before} to ${after}.`,
      });
      setNote("");
      await refresh();
    } catch (e) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="review">
      <h3>Your review</h3>
      {f.history.length > 0 && (
        <ul className="history">
          {f.history.map((h, i) => (
            <li key={i}>
              <strong>{h.action === "resolve" ? "Resolved" : h.action === "confirm" ? "Confirmed" : "Cleared"}</strong>
              {" "}{fmtTime(h.at)}{h.note && <>: {h.note}</>}
            </li>
          ))}
        </ul>
      )}
      <label className="field-label" htmlFor={`note-${f.finding_id}`}>Note</label>
      <textarea id={`note-${f.finding_id}`} rows={3} value={note} onChange={(e) => setNote(e.target.value)}
        placeholder="For example: agent confirmed by email that the LLC name on the Q4 return is a filing error." />
      <div className="actions">
        <button type="button" className="btn" disabled={busy} onClick={() => act("confirm")}>Confirm the issue</button>
        <button type="button" className="btn" disabled={busy || !note.trim()} onClick={() => act("resolve")}
          title={note.trim() ? "" : "Add a note first"}>Mark resolved</button>
        {f.human && <button type="button" className="btn-plain" disabled={busy} onClick={() => act("clear")}>Undo my review</button>}
      </div>
      <p className="hint">Resolving a finding re-runs the rules. {sub.decision && "Conflicts you resolve stop counting toward rule F1."}</p>
      {msg && <p className={msg.ok ? "notice" : "error"} role="status">{msg.text}</p>}
    </section>
  );
}

// ------------------------------------------------------------ rule

function RuleView({ r }) {
  return (
    <article>
      <p className="kicker">Rule {r.rule_id}, {r.section}</p>
      <h2>{r.title}</h2>
      <p className="status-line">
        <span className={`rule-status rs-${r.status}`}>{RULE_STATUS[r.status]}</span>
        {(r.effect === "refer" || r.effect === "decline") && <> This sends the submission to <strong>{OUTCOMES[r.effect]}</strong>.</>}
        {r.status === "fail" && r.effect === "none" && " No effect on the decision."}
      </p>
      {r.message && <p>{r.message}</p>}
      {r.finding_ids.length > 0 && (
        <section>
          <h3>Findings behind this result</h3>
          <div className="chips">{r.finding_ids.map((id) => <Cite key={id} id={id} />)}</div>
        </section>
      )}
      {r.evidence.length > 0 && (
        <section>
          <h3>Evidence</h3>
          {r.evidence.map((o, i) => <Source key={i} obs={o} />)}
        </section>
      )}
    </article>
  );
}

// ------------------------------------------------------------ document

function DocView({ d }) {
  const { sub, sid, open } = useSub();
  const ex = sub.extractions.find((e) => e.doc_id === d.doc_id);
  const files = sub.files.filter((f) => d.file_ids.includes(f.file_id));
  const units = sub.units.filter((u) => d.unit_ids.includes(u.unit_id));
  return (
    <article>
      <p className="kicker">{d.doc_id} {DOC_TYPES[d.doc_type]}, classified with {pct(d.confidence)} confidence</p>
      <h2>{d.title}</h2>
      {d.observations.length > 0 && <ul className="notes">{d.observations.map((o, i) => <li key={i}>{o}</li>)}</ul>}

      <section>
        <h3>Pages</h3>
        <div className="chips">
          {units.map((u) => (
            <button type="button" key={u.unit_id} className="cite" onClick={() => open({ unit: u.unit_id })}>
              {u.label.replace(/^.*?\.(pdf|xlsx)\s*/i, "") || u.label}
            </button>
          ))}
        </div>
        {files.map((f) => (
          <p key={f.file_id} className="small">
            From <a href={api.fileUrl(sid, f.file_id)} target="_blank" rel="noreferrer">{f.name.split("/").pop()}</a>
          </p>
        ))}
      </section>

      {ex && ex.status === "ok" && <Facts ex={ex} />}
      {ex && ex.status !== "ok" && <p className="muted">Extraction {ex.status.replace("_", " ")}. {ex.error}</p>}
    </article>
  );
}

function Facts({ ex }) {
  const { open } = useSub();
  const facts = Object.values(ex.facts);
  return (
    <section>
      <h3>Extracted facts</h3>
      <table className="facts">
        <tbody>
          {facts.map((f) => <FactRow key={f.name} f={f} />)}
        </tbody>
      </table>
      {Object.entries(ex.records).map(([name, rows]) => (
        <div key={name} className="records">
          <h3>{name.replace(/_/g, " ")} ({rows.length})</h3>
          <div className="table-scroll">
            <table className="grid">
              <thead><tr>{Object.keys(rows[0] || {}).map((k) => <th key={k}>{k.replace(/_/g, " ")}</th>)}</tr></thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    {Object.values(row).map((f) => (
                      <td key={f.name} className={f.checks.length ? "flagged" : ""} title={f.checks.join("; ") || f.quote}>
                        {f.unit_id ? (
                          <button type="button" className="cell-link" onClick={() => open({ unit: f.unit_id, quote: f.quote })}>
                            {fmtValue(f.value)}
                          </button>
                        ) : fmtValue(f.value)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </section>
  );
}

function FactRow({ f }) {
  const { open } = useSub();
  const missing = f.status === "missing";
  return (
    <tr className={f.checks.length ? "flagged" : missing ? "missing" : ""}>
      <th scope="row">{f.name.replace(/_/g, " ")}</th>
      <td>
        {missing ? <span className="muted">Not stated</span> : (
          <button type="button" className="cell-link" onClick={() => open({ unit: f.unit_id, quote: f.quote })}
            title={f.quote}>{fmtValue(f.value)}</button>
        )}
        {f.checks.map((c, i) => <div key={i} className="check">{c}</div>)}
      </td>
      <td className="conf">{missing ? "" : pct(f.confidence)}</td>
    </tr>
  );
}

// ------------------------------------------------------------ claim

function ClaimView({ claim, docId }) {
  const { open } = useSub();
  return (
    <article>
      <p className="kicker">Claim on <button type="button" className="linkish" onClick={() => open({ ref: docId })}>{docId}</button></p>
      <h2>Claim {claim.claim_number.value}</h2>
      <table className="facts"><tbody>{Object.values(claim).map((f) => <FactRow key={f.name} f={f} />)}</tbody></table>
    </article>
  );
}

// ------------------------------------------------------------ source page

function UnitView({ unitId, quote }) {
  const { sub, sid } = useSub();
  const u = sub.units.find((x) => x.unit_id === unitId);
  const markRef = useRef(null);
  const [imgOk, setImgOk] = useState(true);
  useEffect(() => { markRef.current?.scrollIntoView({ block: "center" }); }, [unitId, quote]);
  if (!u) return <p className="muted">Page {unitId} is not in this submission.</p>;
  const [before, match, after] = splitOnQuote(u.text, quote);
  return (
    <article>
      <p className="kicker">{unitId}, text from {u.text_source.replace("ocr:", "OCR ")}</p>
      <h2>{u.label}</h2>
      {quote && !match && <p className="hint">The quoted text could not be located exactly in this page's text.</p>}
      {u.has_image && imgOk && (
        <img className="page-image" src={api.imageUrl(sid, unitId)} alt={`Page image of ${u.label}`}
          onError={() => setImgOk(false)} />
      )}
      <pre className="page-text">
        {before}{match && <mark ref={markRef}>{match}</mark>}{after}
      </pre>
    </article>
  );
}

function splitOnQuote(text, quote) {
  if (!quote || !text) return [text || "", "", ""];
  const tokens = quote.trim().split(/\s+/).filter(Boolean).slice(0, 40);
  if (!tokens.length) return [text, "", ""];
  const esc = tokens.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const m = new RegExp(esc.join("\\s+"), "i").exec(text);
  if (!m) return [text, "", ""];
  return [text.slice(0, m.index), m[0], text.slice(m.index + m[0].length)];
}
