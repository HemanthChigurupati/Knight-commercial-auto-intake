"""Stage 6 -- AI analysis: the underwriter-facing summary, concerns and next actions.

One Claude call over the structured brief (analysis/brief.py). Every concern and
recommendation must cite ids from the brief; the citation check below removes ids that
do not exist and drops any item left without a valid citation. The rules-engine decision
is passed in as fixed and is never taken from the model's text.
"""
from __future__ import annotations

import logging
import re

from ..providers.llm import Block, LLMProvider, LLMUnavailable
from ..schemas import Analysis, Concern, Recommendation, Submission
from .brief import build_brief

log = logging.getLogger("pipeline")

SYSTEM = """\
You are a senior commercial auto (trucking) underwriting assistant. You receive the \
structured results of an automated submission review: the applicant, the rules-engine \
decision, cross-document findings (with an AI agent's verification verdicts), loss \
history, IFTA filings, drivers and vehicles. Every item has an id in [brackets].

Write for an underwriter who has two minutes:
- summary: 3-5 sentences -- who the applicant is, the decision and routing, and the few \
issues that matter most.
- concerns: underwriting concerns, most serious first. Include risk signals no rule \
covers (loss severity and trends, open claims and legal expense, late reporting, filing \
behaviour, data quality). Do not restate every rule failure; group related ones.
- recommendations: concrete next actions, each with an owner: 'agent' (ask the \
submitting agent for something), 'underwriter' (a judgment or check), or 'operations'.

Rules:
- Cite the ids that support every concern and recommendation (e.g. ["F05","C5","d02"]). \
Cite only ids that appear in the brief. A statement you cannot support with an id must \
be left out.
- The decision is fixed by the rules engine; explain it, never change or second-guess it.
- Use only facts in the brief. Do not invent amounts, dates, names or causes.
- Findings with status 'resolved' were reading errors: do not raise them as concerns.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "cites": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "detail", "severity", "cites"]}},
        "recommendations": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string"},
            "owner": {"type": "string", "enum": ["agent", "underwriter", "operations"]},
            "reason": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}}},
            "required": ["action", "owner", "reason", "cites"]}},
    },
    "required": ["summary", "concerns", "recommendations"],
}


def _clean_cites(raw: list, valid: set[str]) -> tuple[list[str], list[str]]:
    keep, bad = [], []
    for c in raw or []:
        c = str(c).strip().strip("[]")
        (keep if c in valid else bad).append(c)
    return list(dict.fromkeys(keep)), bad


def check_citations(data: dict, valid: set[str]) -> Analysis:
    """Keep only supported items; record every citation problem as a warning."""
    a = Analysis(summary=data.get("summary", ""))
    for kind, items, make in (("concern", data.get("concerns", []), Concern),
                              ("recommendation", data.get("recommendations", []), Recommendation)):
        for item in items:
            cites, bad = _clean_cites(item.get("cites"), valid)
            name = item.get("title") or item.get("action") or "?"
            if bad:
                a.warnings.append(f"Removed unknown ids {bad} from {kind} '{name}'.")
            if not cites:
                a.warnings.append(f"Dropped {kind} '{name}': no valid citation.")
                continue
            target = a.concerns if kind == "concern" else a.recommendations
            target.append(make(**{**item, "cites": cites}))
    for ref in set(re.findall(r"\[([A-Za-z]?\d{1,2}|[A-F]\d)\]", a.summary)) - valid:
        a.warnings.append(f"Summary cites unknown id [{ref}].")
    return a


def analyze(sub: Submission, llm: LLMProvider) -> Analysis:
    brief, valid = build_brief(sub)
    try:
        res = llm.complete_json("analysis", SYSTEM, [Block(text=brief)], "underwriting_analysis",
                                SCHEMA, max_tokens=4000)
    except LLMUnavailable:
        return Analysis(status="not_run", summary="", warnings=["No model available (demo mode without a "
                                                               "recording of this analysis)."])
    except Exception as e:
        return Analysis(status="error", warnings=[f"{type(e).__name__}: {e}"])
    a = check_citations(res.data, valid)
    a.model = res.model
    log.info("  analysis: %d concerns, %d recommendations, %d citation warnings",
             len(a.concerns), len(a.recommendations), len(a.warnings))
    return a
