"""HTTP API and web app.

    uvicorn app.api.main:app --port 8000          (from backend/)

Routes are thin: they read and write through Store, start work through Jobs and record
reviews through review.apply_review. The built React app in frontend/dist is served at /.

Safety for a public demo:
  - In demo mode nothing calls a paid service, so the app is open.
  - In live modes (aws / local) every write needs the ACCESS_CODE header, because each
    run costs money. Uploads are capped in size, and jobs run one at a time.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent.tools import AgentTools
from ..config import REPO_ROOT, Settings, get_settings
from ..pipeline.intake import stored_path
from .jobs import Jobs, QueueFull
from .review import ReviewError, apply_review
from .store import Store

log = logging.getLogger("pipeline")

SAMPLE_ID = "cobalt-mesa"
SAMPLE = REPO_ROOT / "samples" / "cobalt-mesa" / "Sample Submission - Cobalt Mesa Transport.zip"
FRONTEND = REPO_ROOT / "frontend" / "dist"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
ALLOWED = {".zip", ".eml", ".pdf", ".xlsx", ".xlsm", ".docx", ".png", ".jpg", ".jpeg",
           ".tif", ".tiff", ".txt", ".csv"}

settings: Settings = get_settings()
store = Store(settings.data_dir)
jobs = Jobs(store, settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A fresh server shows the sample straight away (replayed from fixtures in demo mode).
    if SAMPLE.exists() and SAMPLE_ID not in store.ids() and os.environ.get("SEED_SAMPLE", "true") == "true":
        jobs.submit([SAMPLE], SAMPLE.name, SAMPLE_ID)
    yield


app = FastAPI(title="Knight Commercial Auto Intake", lifespan=lifespan)


# ---------------------------------------------------------------- access control

def live() -> bool:
    return settings.mode != "demo"


def require_code(x_access_code: str | None = Header(default=None)) -> None:
    """Writes in a live mode spend money, so they need the access code."""
    if not live():
        return
    code = os.environ.get("ACCESS_CODE", "")
    if not code:
        raise HTTPException(403, "Live mode is running without ACCESS_CODE set; writes are disabled.")
    if not x_access_code or not hmac.compare_digest(x_access_code, code):
        raise HTTPException(401, "This action needs the access code.")


def _load(sid: str):
    try:
        return store.load(sid)
    except KeyError:
        raise HTTPException(404, f"No submission {sid}.")


# ---------------------------------------------------------------- read routes

@app.get("/api/health")
def health():
    """For the load balancer / container health check."""
    return {"ok": True, "mode": settings.mode}


@app.get("/api/config")
def config():
    return {"mode": settings.mode, "code_required": live(), "max_upload_mb": MAX_UPLOAD_MB,
            "sample_id": SAMPLE_ID, "allowed": sorted(ALLOWED),
            "models": {r: settings.model_for(r) for r in ("classify", "extract", "agent", "analysis")}}


@app.get("/api/submissions")
def list_submissions():
    return store.list()


@app.get("/api/submissions/{sid}/status")
def status(sid: str):
    try:
        st = store.status(sid)
    except KeyError:
        raise HTTPException(404, f"No submission {sid}.")
    if st.get("state") == "unknown":
        raise HTTPException(404, f"No submission {sid}.")
    return st


@app.get("/api/submissions/{sid}")
def get_submission(sid: str):
    sub = _load(sid)
    data = sub.model_dump(mode="json")
    for u in data["units"]:                   # never expose server paths
        u["has_image"] = bool(u.pop("image_path", None)) or u["kind"] == "pdf_page"
    for c in data["classified"]:
        c["unit"] = {"unit_id": c["unit"]["unit_id"]}
    return data


@app.get("/api/submissions/{sid}/units/{unit_id}/image")
def unit_image(sid: str, unit_id: str):
    sub = _load(sid)
    res = AgentTools(sub, store.dir(sid), settings.render_dpi).view_source_image(unit_id)
    img = next((b for b in res.content if b.get("type") == "image"), None)
    if res.is_error or img is None:
        raise HTTPException(404, res.summary)
    return FileResponse(img["path"], headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/submissions/{sid}/files/{file_id}")
def original_file(sid: str, file_id: str):
    sub = _load(sid)
    f = next((x for x in sub.files if x.file_id == file_id), None)
    if f is None:
        raise HTTPException(404, f"No file {file_id}.")
    path = stored_path(store.dir(sid), f)
    if not path.exists():
        raise HTTPException(404, "The stored copy of this file is missing.")
    name = Path(f.name).name
    return FileResponse(path, media_type=f.media_type, filename=name, content_disposition_type="inline")


# ---------------------------------------------------------------- write routes

@app.post("/api/submissions", status_code=202, dependencies=[Depends(require_code)])
async def upload(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "Attach at least one file.")
    sid = uuid.uuid4().hex[:12]
    inbox = store.dir(sid) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        saved = await _save_uploads(files, inbox)
        jobs.submit(saved, ", ".join(p.name for p in saved), sid)
    except QueueFull as e:
        shutil.rmtree(store.dir(sid), ignore_errors=True)
        raise HTTPException(429, str(e))
    except HTTPException:
        shutil.rmtree(store.dir(sid), ignore_errors=True)   # leave no half-made submission behind
        raise
    return {"id": sid}


async def _save_uploads(files: list[UploadFile], inbox: Path) -> list[Path]:
    saved, total = [], 0
    for up in files:
        name = re.sub(r"[^\w.\- ()]", "_", Path(up.filename or "upload").name)[:120] or "upload"
        if Path(name).suffix.lower() not in ALLOWED:
            raise HTTPException(400, f"{name}: this file type is not supported. "
                                     f"Use {', '.join(sorted(ALLOWED))}.")
        dest = inbox / name
        with dest.open("wb") as out:
            while chunk := await up.read(1 << 20):
                total += len(chunk)
                if total > MAX_UPLOAD_MB << 20:
                    raise HTTPException(413, f"Uploads are limited to {MAX_UPLOAD_MB} MB in total.")
                out.write(chunk)
        saved.append(dest)
    return saved


@app.post("/api/samples/run", status_code=202, dependencies=[Depends(require_code)])
def run_sample():
    """Process the case-study sample again as a new submission (a clean copy to review)."""
    if not SAMPLE.exists():
        raise HTTPException(404, "The sample submission is not on this server.")
    try:
        return {"id": jobs.submit([SAMPLE], SAMPLE.name)}
    except QueueFull as e:
        raise HTTPException(429, str(e))


class ReviewIn(BaseModel):
    action: str
    note: str = ""
    reviewer: str = "underwriter"


@app.post("/api/submissions/{sid}/findings/{finding_id}/review", dependencies=[Depends(require_code)])
def review(sid: str, finding_id: str, body: ReviewIn):
    sub = _load(sid)
    try:
        before, after = apply_review(sub, finding_id, body.action, body.note, body.reviewer)
    except ReviewError as e:
        raise HTTPException(400, str(e))
    store.save(sub)
    return {"outcome_before": before.outcome if before else None, "outcome_after": after.outcome,
            "reasons_after": after.reasons}


@app.exception_handler(Exception)
async def unexpected(_req, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"detail": f"Server error: {type(exc).__name__}"}, status_code=500)


# ---------------------------------------------------------------- the web app

if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="web")
