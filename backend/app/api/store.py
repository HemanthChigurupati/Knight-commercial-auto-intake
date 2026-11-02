"""Where submissions live. The only module that knows the on-disk layout:

    data/submissions/<id>/submission.json   the full pipeline result
    data/submissions/<id>/status.json       job progress (queued / running / done / failed)
    data/submissions/<id>/files/...         stored originals (written by intake)
    data/submissions/<id>/units/...         rendered pages and extracted images

Swapping this module for an S3-backed one is the only change needed to move storage.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..schemas import Submission

_ID = re.compile(r"^[a-z0-9_-]{1,40}$")
_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "submissions"
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, sid: str) -> Path:
        if not _ID.match(sid):                       # ids come from URLs: never let them be paths
            raise KeyError(sid)
        return self.root / sid

    # ------------------------------------------------------------ submission.json
    def exists(self, sid: str) -> bool:
        return (self.dir(sid) / "submission.json").exists()

    def load(self, sid: str) -> Submission:
        p = self.dir(sid) / "submission.json"
        if not p.exists():
            raise KeyError(sid)
        return Submission.model_validate_json(p.read_text())

    def save(self, sub: Submission) -> None:
        d = self.dir(sub.submission_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "submission.json.tmp"
        tmp.write_text(sub.model_dump_json(indent=1))
        os.replace(tmp, d / "submission.json")        # atomic: readers never see half a file

    # ------------------------------------------------------------ status.json
    def status(self, sid: str) -> dict:
        p = self.dir(sid) / "status.json"
        if p.exists():
            return json.loads(p.read_text())
        return {"state": "done"} if self.exists(sid) else {"state": "unknown"}

    def set_status(self, sid: str, **fields) -> dict:
        with _lock:
            d = self.dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            st = self.status(sid) if (d / "status.json").exists() else {}
            st.update(fields, updated_at=now())
            (d / "status.json").write_text(json.dumps(st))
            return st

    # ------------------------------------------------------------ listing
    def ids(self) -> list[str]:
        return [p.name for p in self.root.iterdir() if p.is_dir() and _ID.match(p.name)]

    def summary(self, sid: str) -> dict:
        """One row of the review queue. Cheap fields only."""
        st = self.status(sid)
        row = {"id": sid, "state": st.get("state", "done"), "step": st.get("step"),
               "step_label": st.get("step_label"), "error": st.get("error"),
               "received_at": st.get("received_at"), "name": st.get("name", sid),
               "mode": st.get("mode")}
        if row["state"] == "done" and self.exists(sid):
            sub = self.load(sid)
            row.update(_summarize(sub))
        return row

    def list(self) -> list[dict]:
        rows = []
        for sid in self.ids():
            try:
                rows.append(self.summary(sid))
            except Exception as e:                    # one bad folder must not break the queue
                rows.append({"id": sid, "state": "failed", "error": str(e)})
        return sorted(rows, key=lambda r: r.get("received_at") or "", reverse=True)


def applicant_name(sub: Submission) -> str | None:
    rec = sub.reconciliation
    app = next((e for e in rec.entities if e.kind == "applicant"), None) if rec else None
    obs = app.fields.get("applicant_name") if app else None
    return str(obs[0].value) if obs else None


def _summarize(sub: Submission) -> dict:
    rec, dec = sub.reconciliation, sub.decision
    findings = rec.findings if rec else []
    return {
        "applicant": applicant_name(sub),
        "documents": len(sub.documents),
        "outcome": dec.outcome if dec else None,
        "route": dec.route if dec else None,
        "reasons": dec.reasons if dec else [],
        "open_findings": sum(1 for f in findings if f.status in ("open", "confirmed", "needs_human")),
        "reviewed_findings": sum(1 for f in findings if f.human),
        "analysis": sub.analysis.status if sub.analysis else None,
    }
