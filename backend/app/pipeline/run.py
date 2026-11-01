"""Pipeline orchestrator. Today: intake -> preprocess -> OCR -> classify -> group.
Later stages (extract, reconcile, agentic recheck, rules, analysis) plug in after `group`.

    python -m app.pipeline.run ../samples/cobalt-mesa/"Sample Submission - Cobalt Mesa Transport.zip"
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings, get_settings
from ..providers.llm import build_llm
from ..providers.ocr import build_ocr
from ..schemas import Submission
from .classify import classify_units, group_documents
from .intake import ingest
from .preprocess import build_units, run_ocr

log = logging.getLogger("pipeline")


def _event(sub: Submission, step: str, t0: float, **detail) -> None:
    sub.events.append({"step": step, "at": datetime.now(timezone.utc).isoformat(),
                       "ms": int((time.time() - t0) * 1000), **detail})


def process(inputs: list[Path], settings: Settings | None = None,
            submission_id: str | None = None) -> Submission:
    s = settings or get_settings()
    sub = Submission(submission_id=submission_id or uuid.uuid4().hex[:12],
                     received_name=", ".join(p.name for p in inputs))
    work = s.data_dir / "submissions" / sub.submission_id
    llm = build_llm(s)
    ocr = build_ocr(s, llm)
    log.info("mode=%s  llm=%s  ocr=%s", s.mode, llm.name, ocr.name)

    log.info("[1/4] intake")
    t0 = time.time()
    sub.files = ingest(inputs, work)
    _event(sub, "intake", t0, files=len(sub.files),
           duplicates=sum(1 for f in sub.files if f.duplicate_of))

    log.info("[2/4] preprocess: splitting files into pages/sheets/images")
    t0 = time.time()
    sub.units = build_units(sub.files, work, s)
    _event(sub, "preprocess", t0, units=len(sub.units),
           needs_ocr=sum(1 for u in sub.units if u.image_path and not u.text))

    log.info("[3/4] ocr")
    t0 = time.time()
    run_ocr(sub.units, ocr, sub.files)
    _event(sub, "ocr", t0, engines=sorted({u.text_source for u in sub.units}))

    log.info("[4/4] classify + group")
    t0 = time.time()
    sub.classified, subjects = classify_units(sub.units, llm)
    sub.documents = group_documents(sub.classified, subjects, sub.files)
    _event(sub, "classify", t0, llm=llm.name, documents=len(sub.documents),
           needs_review=sum(1 for c in sub.classified if c.classification.needs_review))

    (work / "submission.json").write_text(sub.model_dump_json(indent=2))
    return sub


def _print_summary(sub: Submission) -> None:
    print(f"\nSubmission {sub.submission_id}: {len(sub.files)} files "
          f"({sum(1 for f in sub.files if f.duplicate_of)} duplicates), "
          f"{len(sub.units)} units, {len(sub.documents)} documents\n")
    for d in sub.documents:
        flag = "  REVIEW" if d.confidence < 0.70 else ""
        print(f"{d.doc_id}  {d.doc_type.value:<22} {d.confidence:.2f}{flag}  {d.title}")
        print(f"      units: {', '.join(d.unit_ids)}")
        for o in d.observations:
            print(f"      - {o}")
    print("\nEvents:", json.dumps(sub.events, indent=1))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "anthropic", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _print_summary(process([Path(a) for a in sys.argv[1:]]))
