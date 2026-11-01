"""Stage 3 -- reconciliation.

Unit tests for the comparison rules, plus the EXPECTED-FINDINGS test: it replays the
recorded Bedrock run committed in fixtures/ (real Claude output, no credentials needed)
and checks that reconciliation surfaces the issues an underwriter would find by hand.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.pipeline.run import process
from app.reconcile.compare import company_difference, same_person, state_code

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples" / "cobalt-mesa" / "Sample Submission - Cobalt Mesa Transport.zip"


# ---------------------------------------------------------------- comparison rules

def test_people():
    assert same_person("MORALES, LUIS E", "LUIS E MORALES")
    assert same_person("VOLKOV, ALEKSANDR P", "ALEKSANDR PAVEL VOLKOV")     # initial fits full name
    assert not same_person("VOLKOV, ALEKSANDR P", "ALEKSANDR K VOLKOV")
    assert not same_person("KEVIN BRANDT", "MARCUS J WHITFIELD")


def test_companies_and_states():
    assert company_difference("COBALT MESA TRANSPORT INC", "Cobalt Mesa Transport, Inc.") == ""
    assert company_difference("COBALT MESA TRANSPORT INC", "COBALT MESA TRANSPORT LLC") == "legal form (INC vs LLC)"
    assert company_difference("BAYLINE INSURANCE AGENCY INC", "Bayline Insurance Services Inc.") == "name"
    assert state_code("CALIFORNIA") == state_code("California") == "CA"


# ---------------------------------------------------------------- expected findings

@pytest.fixture(scope="module")
def findings(tmp_path_factory):
    if not any((REPO / "fixtures" / "llm_cache").glob("*.json")):
        pytest.skip("no recorded run in fixtures/ (run once in aws mode and commit fixtures)")
    s = replace(Settings(), mode="demo", data_dir=tmp_path_factory.mktemp("data"))
    sub = process([SAMPLE], s, "golden")
    return sub.reconciliation.findings


def _has(findings, kind, *words):
    return any(f.kind.value == kind and all(w.lower() in (f.title + " " + f.detail).lower() for w in words)
               for f in findings)


EXPECTED = [
    ("conflict", "INC vs LLC"),                         # Q4 2025 IFTA return
    ("conflict", "power units", "4", "5"),              # vehicle schedule vs quote form
    ("conflict", "Morales", "date of birth", "1991-10-03", "1991-10-30"),
    ("conflict", "make", "VOLVO", "FREIGHTLINER"),      # same VIN, VIN decodes to Volvo
    ("gap", "Kevin Brandt", "not on the driver schedule"),
    ("gap", "Peterbilt", "not on the vehicle schedule"),
    ("gap", "2022-03-08", "no loss run"),               # Lakeshore term
    ("conflict", "agency name"),
    ("conflict", "Sandhu", "owner"),
    ("conflict", "carrier", "Midwest Freight"),         # cargo letter vs quote form carrier
    ("gap", "no-loss letter"),
    ("outdated_document", "Volkov", "2026-06-02"),
    ("outdated_document", "Osei", "2025-02-17"),
    ("possible_misread", "Osei", "license number"),
    ("superseded", "Whitfield", "OLD MVR"),
]


@pytest.mark.parametrize("expected", EXPECTED, ids=[" ".join(e[1:3]) for e in EXPECTED])
def test_expected_finding_is_reported(findings, expected):
    kind, *words = expected
    assert _has(findings, kind, *words)


def test_high_severity_items_come_first(findings):
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key={"high": 0, "medium": 1, "low": 2}.get)
