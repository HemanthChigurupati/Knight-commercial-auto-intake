"""Background pipeline jobs.

A live run takes minutes, so an upload returns at once and the pipeline runs here. One
worker thread: jobs run one at a time, which also caps load (and spend) on the server.
Progress and failures are written to status.json, so they survive a page reload.
"""
from __future__ import annotations

import logging
import queue
import shutil
import threading
import uuid
from pathlib import Path

from ..config import Settings
from ..pipeline.run import process
from .store import Store, now

log = logging.getLogger("pipeline")

MAX_QUEUED = 3                       # refuse new uploads beyond this many waiting jobs


class QueueFull(RuntimeError):
    pass


class Jobs:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.queue: queue.Queue = queue.Queue()
        self.pending = 0
        # A daemon thread, so stopping the server (Ctrl+C) never waits for a running job.
        threading.Thread(target=self._worker, name="pipeline", daemon=True).start()

    def _worker(self) -> None:
        while True:
            sid, inputs = self.queue.get()
            self._run(sid, inputs)

    def submit(self, inputs: list[Path], name: str, sid: str | None = None) -> str:
        if self.pending >= MAX_QUEUED:
            raise QueueFull("The processing queue is full. Try again in a few minutes.")
        sid = sid or uuid.uuid4().hex[:12]
        self.store.set_status(sid, state="queued", name=name, received_at=now(), mode=self.settings.mode,
                              step=0, step_label="waiting to start", error=None)
        self.pending += 1
        self.queue.put((sid, inputs))
        return sid

    def _run(self, sid: str, inputs: list[Path]) -> None:
        def progress(n: int, label: str) -> None:
            self.store.set_status(sid, state="running", step=n, step_label=label)
        try:
            self.store.set_status(sid, state="running", step=0, step_label="starting", started_at=now())
            sub = process(inputs, self.settings, sid, progress=progress)
            self.store.save(sub)
            self.store.set_status(sid, state="done", step=9, step_label="complete", finished_at=now())
        except Exception as e:                        # report, never crash the worker
            log.exception("job %s failed", sid)
            self.store.set_status(sid, state="failed", error=f"{type(e).__name__}: {e}")
        finally:
            self.pending -= 1
            inbox = self.store.dir(sid) / "inbox"     # the uploaded copies; intake stored its own
            shutil.rmtree(inbox, ignore_errors=True)
