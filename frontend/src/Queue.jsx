// The review queue: every submission, its decision, and where new ones come in.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, getCode, setCode } from "./api.js";
import { OUTCOMES, ROUTES, fmtTime, titleCase } from "./format.js";

export function Progress({ step, label }) {
  return (
    <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={9} aria-valuenow={step || 0}>
      <div className="progress-bar" style={{ width: `${((step || 0) / 9) * 100}%` }} />
      <span className="progress-label">Step {step || 0} of 9{label ? `: ${label}` : ""}</span>
    </div>
  );
}

export default function Queue({ config }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try { setRows(await api.list()); setError(null); } catch (e) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!rows?.some((r) => r.state === "queued" || r.state === "running")) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [rows, load]);

  return (
    <main className="desk">
      <div className="queue-head">
        <div>
          <h1>Submissions</h1>
          <p className="lede">Emailed Commercial Auto submissions, read, checked against Knight's guideline and ready for an underwriter.</p>
        </div>
      </div>
      <div className="queue-layout">
        <section className="panel" aria-label="Submissions">
          {error && <p className="error">{error}</p>}
          {!rows && !error && <p className="muted">Loading…</p>}
          {rows && rows.length === 0 && <p className="muted">No submissions yet. Upload one, or run the sample.</p>}
          {rows && rows.length > 0 && (
            <ul className="queue">
              {rows.map((r) => <QueueRow key={r.id} r={r} />)}
            </ul>
          )}
        </section>
        <Intake config={config} onStarted={load} />
      </div>
    </main>
  );
}

function QueueRow({ r }) {
  const busy = r.state === "queued" || r.state === "running";
  return (
    <li>
      <a className="queue-row" href={`#/s/${r.id}`}>
        <span className="queue-main">
          <span className="queue-title">{r.applicant ? titleCase(r.applicant) : r.name}</span>
          <span className="queue-meta">
            {r.received_at ? `Received ${fmtTime(r.received_at)}` : "Sample submission"}
            {r.documents ? `, ${r.documents} documents` : ""}
            {r.mode && r.mode !== "demo" ? `, live ${r.mode} run` : ""}
          </span>
          {busy && <Progress step={r.step} label={r.step_label} />}
          {r.state === "failed" && <span className="error small">Failed: {r.error}</span>}
        </span>
        {r.state === "done" && r.outcome && (
          <span className="queue-decision">
            <span className={`badge badge-${r.outcome}`}>{OUTCOMES[r.outcome]}</span>
            <span className="small">{ROUTES[r.route]}</span>
            <span className="small muted">
              {r.open_findings} open findings{r.reviewed_findings ? `, ${r.reviewed_findings} reviewed` : ""}
            </span>
          </span>
        )}
      </a>
    </li>
  );
}

function Intake({ config, onStarted }) {
  const input = useRef(null);
  const [files, setFiles] = useState([]);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [code, setCodeState] = useState(getCode());
  const demo = config?.mode === "demo";

  async function start(fn) {
    setBusy(true);
    setMsg(null);
    try {
      if (config?.code_required) setCode(code);
      const { id } = await fn();
      setFiles([]);
      await onStarted();
      window.location.hash = `#/s/${id}`;
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="panel intake" aria-label="New submission">
      <h2>New submission</h2>
      <div
        className={`drop ${drag ? "drag" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); setFiles([...e.dataTransfer.files]); }}
      >
        <p>Drop the email (.eml), a .zip, or the PDFs and spreadsheets themselves.</p>
        <button type="button" className="btn" onClick={() => input.current?.click()}>Choose files</button>
        <input ref={input} type="file" multiple hidden accept={config?.allowed?.join(",")}
          onChange={(e) => setFiles([...e.target.files])} />
        {files.length > 0 && (
          <ul className="picked">{files.map((f) => <li key={f.name}>{f.name}</li>)}</ul>
        )}
      </div>
      {config?.code_required && (
        <>
          <label className="field-label" htmlFor="code">Access code</label>
          <input id="code" type="password" value={code} onChange={(e) => setCodeState(e.target.value)}
            autoComplete="off" />
        </>
      )}
      <button type="button" className="btn primary" disabled={!files.length || busy}
        onClick={() => start(() => api.upload(files))}>Process submission</button>
      <button type="button" className="btn-plain" disabled={busy} onClick={() => start(api.runSample)}>
        Process the Cobalt Mesa sample again
      </button>
      {msg && <p className="error" role="alert">{msg}</p>}
      <p className="hint">
        {demo
          ? "Demo mode: the sample replays a recorded live Bedrock run at no cost. A new upload runs every step that doesn't need AI; the AI steps show as not run."
          : `Live mode (${config?.mode}): uploads call Claude on Amazon Bedrock and Textract. Up to ${config?.max_upload_mb} MB.`}
      </p>
    </aside>
  );
}
