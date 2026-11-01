"""Stage 6 -- AI analysis. The brief must carry the facts an underwriter needs, and the
citation check must strip unsupported statements no matter what the model returns."""
from dataclasses import replace
from pathlib import Path

import pytest

from app.analysis.analyze import analyze, check_citations
from app.analysis.brief import build_brief
from app.config import Settings
from app.pipeline.run import process
from app.providers.llm import LLMResult

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples" / "cobalt-mesa" / "Sample Submission - Cobalt Mesa Transport.zip"


@pytest.fixture(scope="module")
def sub(tmp_path_factory):
    if not any((REPO / "fixtures" / "llm_cache").glob("*.json")):
        pytest.skip("no recorded run in fixtures/")
    return process([SAMPLE], replace(Settings(), mode="demo", data_dir=tmp_path_factory.mktemp("d")), "an")


def test_brief_carries_the_key_facts(sub):
    brief, ids = build_brief(sub)
    assert "Outcome: REFER" in brief and "do not change it" in brief
    for fact in ("GA1092764", "KEVIN BRANDT", "penalty 346.42", "COBALT MESA TRANSPORT LLC", "308-day lapse"):
        assert fact in brief
    assert {"F01", "F17", "C1", "E5", "d02", "GA1092764"} <= ids


def test_citation_check_keeps_only_supported_items():
    data = {
        "summary": "Refer to Small Fleet [F05].",
        "concerns": [
            {"title": "Unreported driver", "detail": "x", "severity": "high", "cites": ["F05", "Z99"]},
            {"title": "Invented claim", "detail": "y", "severity": "high", "cites": ["F42"]},
        ],
        "recommendations": [
            {"action": "Request the 2022-23 loss run", "owner": "agent", "reason": "r", "cites": ["[C5]", "E6"]},
            {"action": "Uncited advice", "owner": "underwriter", "reason": "r", "cites": []},
        ],
    }
    a = check_citations(data, {"F05", "C5", "E6"})
    assert [c.title for c in a.concerns] == ["Unreported driver"]
    assert a.concerns[0].cites == ["F05"]
    assert [r.action for r in a.recommendations] == ["Request the 2022-23 loss run"]
    assert a.recommendations[0].cites == ["C5", "E6"]
    assert len(a.warnings) == 4      # Z99 removed, F42 item dropped, uncited item dropped (x2 lines)


def test_analysis_never_changes_the_decision(sub):
    class Fake:
        name = "fake"

        def complete_json(self, role, system, blocks, schema_name, schema, max_tokens=2000):
            assert role == "analysis" and "Outcome: REFER" in blocks[0].text
            return LLMResult(model="fake", data={"summary": "This account should be DECLINED.",
                                                 "concerns": [], "recommendations": []})
    before = sub.decision.outcome
    a = analyze(sub, Fake())
    assert a.status == "ok" and sub.decision.outcome == before == "refer"
