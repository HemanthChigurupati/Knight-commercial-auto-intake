"""Stage 5 -- rules engine. The expected decision for the Cobalt Mesa package (replayed from
the recorded Bedrock run), plus proof that the YAML settings -- not code -- drive outcomes."""
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.pipeline.run import process
from app.rules.checks import CHECKS
from app.rules.engine import evaluate, load_rules

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples" / "cobalt-mesa" / "Sample Submission - Cobalt Mesa Transport.zip"


@pytest.fixture(scope="module")
def sub(tmp_path_factory):
    if not any((REPO / "fixtures" / "llm_cache").glob("*.json")):
        pytest.skip("no recorded run in fixtures/")
    return process([SAMPLE], replace(Settings(), mode="demo", data_dir=tmp_path_factory.mktemp("d")), "rules")


def test_every_rule_names_a_real_check():
    config, _ = load_rules()
    assert {r["check"] for r in config["rules"]} <= set(CHECKS)
    assert len({r["id"] for r in config["rules"]}) == len(config["rules"])


def test_expected_decision(sub):
    d = sub.decision
    assert (d.outcome, d.route) == ("refer", "small_fleet")
    status = {r.rule_id: r.status for r in d.results}
    assert {k for k, v in status.items() if v == "fail"} == {"C1", "C3", "C5", "C8", "D1", "E5", "E6", "F1", "F2"}
    assert status["D2"] == "unknown"          # Whitfield's CDL issue date is blank on the MVR
    assert all(status[k] == "pass" for k in ("A1", "B1", "C4", "C6", "C7", "E1", "E2", "E3"))
    msg = {r.rule_id: r.message for r in d.results}
    assert "Sandhu" in msg["D1"] and "16 characters" in msg["C8"] and "308-day lapse" in msg["E5"]


def test_agent_resolved_findings_do_not_count_as_conflicts(sub):
    f1 = next(r for r in sub.decision.results if r.rule_id == "F1")
    resolved = [f.finding_id for f in sub.reconciliation.findings if f.status == "resolved"]
    assert resolved and not set(resolved) & set(f1.finding_ids)


def test_settings_change_the_outcome_without_code(sub):
    config, _ = load_rules()
    strict = copy.deepcopy(config)
    strict["settings"]["assumptions"]["non_listed_states_scope"] = "operations"
    d = evaluate(sub, strict)
    assert d.outcome == "decline" and "B2" in d.reasons

    strict = copy.deepcopy(config)
    strict["settings"]["assumptions"]["baseline_failure_outcome"] = "decline"
    d = evaluate(sub, strict)
    assert d.outcome == "decline" and "D1" in d.reasons      # Sandhu has no CDL

    big = copy.deepcopy(config)
    big["settings"]["routing"]["small_fleet_max_units"] = 3
    assert evaluate(sub, big).route == "fleet"
