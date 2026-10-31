"""Stage 1 (intake -> classify) against the Cobalt Mesa sample, in offline demo mode.

These run without any credentials: with no recorded LLM responses the classifier falls
back to content signals, which must still get every document type right.
"""
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.pipeline.run import process
from app.schemas import DocType

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "cobalt-mesa"


@pytest.fixture(scope="module")
def submission(tmp_path_factory):
    s = replace(Settings(), mode="demo", data_dir=tmp_path_factory.mktemp("data"),
                llm_cache_dir=tmp_path_factory.mktemp("empty_cache"))
    return process([SAMPLES / "Sample Submission - Cobalt Mesa Transport.zip"], s, "test")


def test_every_document_type_found(submission):
    counts = Counter(d.doc_type for d in submission.documents)
    assert counts == {
        DocType.LOSS_RUN: 5, DocType.NO_LOSS_LETTER: 1, DocType.APPLICATION: 1,
        DocType.QUOTE_FORM: 1, DocType.DRIVER_SCHEDULE: 1, DocType.VEHICLE_SCHEDULE: 1,
        DocType.MVR: 6, DocType.DRIVER_LICENSE: 2, DocType.IFTA_RETURN: 4}


def test_mixed_mvr_file_is_split(submission):
    mvr_file = next(f for f in submission.files if f.name.startswith("MVR REPORT"))
    docs = [d for d in submission.documents if mvr_file.file_id in d.file_ids]
    assert [d.doc_type for d in docs].count(DocType.DRIVER_LICENSE) == 2
    titles = [d.title for d in docs if d.doc_type == DocType.MVR]
    assert sum("WHITFIELD" in t for t in titles) == 2       # current + stale MVR kept apart


def test_scanned_ifta_pages_are_ocrd_and_grouped(submission):
    ifta = [d for d in submission.documents if d.doc_type == DocType.IFTA_RETURN]
    assert all(any("OCR" in o for o in d.observations) for d in ifta)
    assert sorted(len(d.unit_ids) for d in ifta) == [2, 3, 3, 3]


def test_misleading_file_name_is_flagged(submission):
    letter = next(d for d in submission.documents if d.doc_type == DocType.NO_LOSS_LETTER)
    assert any("suggests loss run" in o for o in letter.observations)


def test_duplicate_files_are_detected(tmp_path):
    s = replace(Settings(), mode="demo", data_dir=tmp_path, llm_cache_dir=tmp_path / "c")
    sub = process([SAMPLES / "candidate_pack.zip"], s, "dupes")
    assert sum(1 for f in sub.files if f.duplicate_of) == 15
    assert any(d.doc_type == DocType.UNDERWRITING_GUIDELINE for d in sub.documents)
