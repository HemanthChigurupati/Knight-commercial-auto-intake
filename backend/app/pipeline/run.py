"""Pipeline orchestrator: intake -> preprocess -> OCR -> classify/group -> extract ->
reconcile -> agent recheck. Rules and AI analysis plug in after the recheck.

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
from .extract import all_facts, extract_documents, fact_counts
from ..agent.recheck import recheck_findings
from ..reconcile.checks import reconcile
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

    log.info("[1/7] intake")
    t0 = time.time()
    sub.files = ingest(inputs, work)
    _event(sub, "intake", t0, files=len(sub.files),
           duplicates=sum(1 for f in sub.files if f.duplicate_of))

    log.info("[2/7] preprocess: splitting files into pages/sheets/images")
    t0 = time.time()
    sub.units = build_units(sub.files, work, s)
    _event(sub, "preprocess", t0, units=len(sub.units),
           needs_ocr=sum(1 for u in sub.units if u.image_path and not u.text))

    log.info("[3/7] ocr")
    t0 = time.time()
    run_ocr(sub.units, ocr, sub.files)
    _event(sub, "ocr", t0, engines=sorted({u.text_source for u in sub.units}))

    log.info("[4/7] classify + group")
    t0 = time.time()
    sub.classified, subjects = classify_units(sub.units, llm)
    sub.documents = group_documents(sub.classified, subjects, sub.files)
    _event(sub, "classify", t0, llm=llm.name, documents=len(sub.documents),
           needs_review=sum(1 for c in sub.classified if c.classification.needs_review))

    log.info("[5/7] extract facts")
    t0 = time.time()
    sub.extractions = extract_documents(sub, llm)
    totals = {"found": 0, "missing": 0, "invalid": 0, "unverified": 0}
    for ex in sub.extractions:
        for k, v in fact_counts(ex).items():
            totals[k] += v
    _event(sub, "extract", t0, llm=llm.name,
           documents={st: sum(1 for e in sub.extractions if e.status == st)
                      for st in ("ok", "not_run", "skipped", "error")}, facts=totals)

    log.info("[6/7] reconcile across documents")
    t0 = time.time()
    sub.reconciliation = reconcile(sub)
    rec = sub.reconciliation
    _event(sub, "reconcile", t0, entities=len(rec.entities),
           findings={k: sum(1 for f in rec.findings if f.kind.value == k)
                     for k in ("conflict", "gap", "possible_misread", "outdated_document", "superseded")},
           needs_recheck=sum(1 for f in rec.findings if f.needs_recheck))

    log.info("[7/7] agent recheck of flagged findings")
    t0 = time.time()
    recheck_findings(sub, llm, work, s.render_dpi)
    reviewed = [f for f in rec.findings if f.review]
    _event(sub, "recheck", t0, llm=llm.name, reviewed=len(reviewed),
           verdicts={v: sum(1 for f in reviewed if f.review.verdict == v)
                     for v in ("confirmed", "resolved", "corrected", "needs_human", "not_run")},
           tool_calls=sum(len(f.review.steps) for f in reviewed))

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
    print("\nExtraction")
    titles = {d.doc_id: d.title for d in sub.documents}
    for ex in sub.extractions:
        c = fact_counts(ex)
        print(f"{ex.doc_id}  {titles[ex.doc_id][:40]:<40} {ex.status:<8}"
              + (f" found {c['found']}, missing {c['missing']}, "
                 f"flagged {c['invalid'] + c['unverified']}" if ex.status == "ok" else ex.error))
        for f in all_facts(ex):
            if f.checks:
                print(f"      ! {f.name} = {f.value!r}: {'; '.join(f.checks)}")
        missing = [f.name for f in ex.facts.values() if f.status.value == "missing"]
        if missing:
            print(f"      - not stated: {', '.join(missing)}")
    if sub.reconciliation:
        rec = sub.reconciliation
        kinds = {}
        for e in rec.entities:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        print(f"\nReconciliation: {', '.join(f'{v} {k}' for k, v in kinds.items())}")
        for f in rec.findings:
            mark = "  [recheck]" if f.needs_recheck else ""
            print(f"{f.finding_id} {f.severity:<6} {f.kind.value:<17} {f.title}{mark}")
            if f.detail:
                print(f"      {f.detail}")
            if f.review:
                r = f.review
                print(f"      AGENT: {r.verdict.upper()} -> status {f.status}"
                      + (f" (confidence {r.confidence:.2f})" if r.confidence else ""))
                for st in r.steps:
                    if st.tool != "record_verdict":
                        print(f"        - {st.tool}({', '.join(f'{k}={v}' for k, v in st.input.items())}): {st.result}")
                if r.explanation:
                    print(f"        {r.explanation}")
                if r.corrected_value:
                    print(f"        corrected value: {r.corrected_value} ({r.corrected_source})")
    print("\nEvents:", json.dumps(sub.events, indent=1))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    for noisy in ("botocore", "boto3", "urllib3", "httpx", "anthropic", "pdfminer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _print_summary(process([Path(a) for a in sys.argv[1:]]))
