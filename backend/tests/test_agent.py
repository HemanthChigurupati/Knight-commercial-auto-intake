"""Stage 4 -- agent recheck. The loop is tested with a scripted model, so each test knows
exactly which tools are called and checks how the loop runs them, caps them and records
the verdict. Stages 1-3 come from the recorded Bedrock run in fixtures/."""
from dataclasses import replace
from pathlib import Path

import pytest

from app.agent.recheck import MAX_TURNS, recheck_finding, recheck_findings
from app.agent.tools import AgentTools
from app.config import Settings
from app.pipeline.run import process
from app.providers.llm import LLMResult

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples" / "cobalt-mesa" / "Sample Submission - Cobalt Mesa Transport.zip"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not any((REPO / "fixtures" / "llm_cache").glob("*.json")):
        pytest.skip("no recorded run in fixtures/")
    data = tmp_path_factory.mktemp("data")
    sub = process([SAMPLE], replace(Settings(), mode="demo", data_dir=data), "agent")
    return sub, data / "submissions" / "agent"


class Scripted:
    """Returns the scripted turns in order and keeps every conversation it was sent."""
    name = "scripted"

    def __init__(self, turns):
        self.turns, self.seen = list(turns), []

    def converse(self, role, system, messages, tools, max_tokens=2000):
        self.seen.append([dict(m) for m in messages])
        turn = self.turns.pop(0) if self.turns else self.last
        self.last = turn
        return LLMResult(data={"content": turn, "stop_reason": "tool_use"}, model="scripted")


def call(i, name, **args):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}


def finding(sub, words):
    return next(f for f in sub.reconciliation.findings if words in f.title)


def test_misread_license_is_resolved_after_viewing_the_photo(run):
    sub, work = run
    f = finding(sub, "license number read differently")
    llm = Scripted([
        [{"type": "text", "text": "The card value came from a photo; view it."},
         call(1, "view_source_image", unit_id="f009:p10")],
        [call(2, "record_verdict", verdict="resolved", confidence=0.9,
              explanation="The card shows a letter O, matching the MVR's O7314062.")],
    ])
    review = recheck_finding(f, sub, AgentTools(sub, work), llm)
    assert review.verdict == "resolved"
    assert [s.tool for s in review.steps] == ["view_source_image", "record_verdict"]
    assert review.steps[0].thought.startswith("The card value came from a photo")
    # The second turn carried the image back to the model, identified by its source unit.
    result = llm.seen[1][-1]["content"][0]
    assert result["type"] == "tool_result"
    assert any(b.get("type") == "image" and b["ref"] == "f009:p10" for b in result["content"])


def test_text_pages_are_rendered_and_vins_decoded(run):
    sub, work = run
    tools = AgentTools(sub, work)
    img = tools.run("view_source_image", {"unit_id": "f001:p1"})       # a text-layer loss run page
    assert not img.is_error and Path(img.content[1]["path"]).exists()
    vin = tools.run("decode_vin", {"vin": "4V4NC9EH9LN248615"})
    assert "VOLVO" in vin.summary and "2020" in vin.summary
    assert tools.run("view_source_image", {"unit_id": "f014:s1"}).is_error  # spreadsheets have no image
    assert tools.run("read_source_text", {"unit_id": "nope"}).is_error       # bad ids are reported


def test_turn_cap_hands_over_to_a_human(run):
    sub, work = run
    f = finding(sub, "date of birth")
    llm = Scripted([[call(i, "read_source_text", unit_id="f005:s1")] for i in range(20)])
    review = recheck_finding(f, sub, AgentTools(sub, work), llm)
    assert review.verdict == "needs_human" and len(review.steps) == MAX_TURNS
    assert len(llm.seen) == MAX_TURNS


def test_verdicts_set_finding_status(run):
    sub, work = run
    verdicts = iter(["confirmed", "corrected", "needs_human", "confirmed", "confirmed", "confirmed",
                     "resolved"])

    class OneShot(Scripted):
        def converse(self, *a, **k):
            return LLMResult(data={"content": [call(0, "record_verdict", verdict=next(verdicts),
                                                    explanation="x", confidence=0.8)]}, model="m")

    recheck_findings(sub, OneShot([]), work)
    flagged = [f for f in sub.reconciliation.findings if f.needs_recheck]
    assert [f.status for f in flagged] == ["confirmed", "resolved", "needs_human", "confirmed",
                                           "confirmed", "confirmed", "resolved"]
    assert all(f.review is None for f in sub.reconciliation.findings if not f.needs_recheck)
