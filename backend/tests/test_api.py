"""Stage 7 -- the web API and the underwriter's review.

The review logic is plain Python and always runs. The HTTP tests need FastAPI's test
client (`pip install -r requirements.txt`) and skip without it.
"""
import importlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.api.review import ReviewError, apply_review
from app.api.store import Store
from app.config import Settings
from app.pipeline.run import process

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples" / "cobalt-mesa" / "Sample Submission - Cobalt Mesa Transport.zip"


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    if not any((REPO / "fixtures" / "llm_cache").glob("*.json")):
        pytest.skip("no recorded run in fixtures/")
    data = tmp_path_factory.mktemp("data")
    s = replace(Settings(), mode="demo", data_dir=data)
    store = Store(data)
    steps = []
    store.save(process([SAMPLE], s, "cobalt-mesa", progress=lambda n, label: steps.append(n)))
    store.set_status("cobalt-mesa", state="done", name=SAMPLE.name)
    return store, steps


def test_progress_reports_every_step(stored):
    _, steps = stored
    assert steps == list(range(1, 10))


def test_queue_summary(stored):
    store, _ = stored
    row = store.summary("cobalt-mesa")
    assert (row["applicant"], row["outcome"], row["route"]) == ("COBALT MESA TRANSPORT INC", "refer", "small_fleet")
    assert row["open_findings"] == 17 and row["documents"] == 22


def test_command_line_runs_are_not_in_the_queue(stored):
    store, _ = stored
    (store.root / "cli0run").mkdir()
    (store.root / "cli0run" / "submission.json").write_text("{}")
    assert store.ids() == ["cobalt-mesa"]


def test_ids_from_urls_cannot_escape_the_data_folder(stored):
    store, _ = stored
    for bad in ("../etc", "a/b", "..", "x" * 50):
        with pytest.raises(KeyError):
            store.dir(bad)


def test_resolving_conflicts_changes_the_rules_result(stored):
    store, _ = stored
    sub = store.load("cobalt-mesa")
    conflicts = [f.finding_id for f in sub.reconciliation.findings
                 if f.kind.value == "conflict" and f.severity in ("high", "medium") and f.status != "resolved"]
    assert "F1" in sub.decision.reasons
    for fid in conflicts:
        apply_review(sub, fid, "resolve", "Explained by the agent in writing.")
    assert "F1" not in sub.decision.reasons                    # no unresolved conflicts left
    assert sub.decision.outcome == "refer"                     # other rules still refer
    assert sub.analysis.stale
    assert sub.events[-1]["step"] == "review"

    apply_review(sub, conflicts[0], "clear")                   # undo restores the pipeline status
    f = next(x for x in sub.reconciliation.findings if x.finding_id == conflicts[0])
    assert f.status == "confirmed" and f.human is None and len(f.history) == 2
    assert "F1" in sub.decision.reasons


def test_review_needs_a_note_to_resolve(stored):
    store, _ = stored
    sub = store.load("cobalt-mesa")
    with pytest.raises(ReviewError):
        apply_review(sub, "F05", "resolve", "  ")
    with pytest.raises(ReviewError):
        apply_review(sub, "F99", "confirm")
    with pytest.raises(ReviewError):
        apply_review(sub, "F05", "approve")


# ---------------------------------------------------------------- HTTP

def _client(tmp_path, monkeypatch, mode="demo", code=""):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setenv("APP_MODE", mode)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEED_SAMPLE", "false")
    monkeypatch.setenv("ACCESS_CODE", code)
    import app.api.main as main
    main = importlib.reload(main)
    return TestClient(main.app), main


def test_http_read_and_review(tmp_path, monkeypatch, stored):
    client, main = _client(tmp_path, monkeypatch)
    store, _ = stored
    main.store.save(store.load("cobalt-mesa"))
    main.store.set_status("cobalt-mesa", state="done", name="sample")

    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/config").json()["mode"] == "demo"
    assert [r["id"] for r in client.get("/api/submissions").json()] == ["cobalt-mesa"]
    sub = client.get("/api/submissions/cobalt-mesa").json()
    assert sub["decision"]["outcome"] == "refer"
    assert all("image_path" not in u for u in sub["units"])     # no server paths leak out
    assert client.get("/api/submissions/nope").status_code == 404

    r = client.post("/api/submissions/cobalt-mesa/findings/F05/review",
                    json={"action": "resolve", "note": "Driver left in 2025."})
    assert r.status_code == 200 and r.json()["outcome_after"] == "refer"
    bad = client.post("/api/submissions/cobalt-mesa/findings/F05/review", json={"action": "resolve"})
    assert bad.status_code == 400


def test_http_live_mode_needs_the_access_code(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, mode="aws", code="s3cret")
    files = {"files": ("note.txt", b"hello", "text/plain")}
    assert client.post("/api/submissions", files=files).status_code == 401
    assert client.post("/api/submissions", files=files, headers={"X-Access-Code": "wrong"}).status_code == 401
    assert client.post("/api/samples/run").status_code == 401


def test_http_rejects_unsupported_files(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.post("/api/submissions", files={"files": ("run.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


@pytest.fixture(autouse=True)
def _restore_env():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
