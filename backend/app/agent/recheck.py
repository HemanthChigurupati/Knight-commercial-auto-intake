"""Stage 4 -- agentic recheck of findings that might be reading mistakes.

For each finding marked `needs_recheck`, Claude investigates with a small set of tools
(agent/tools.py) and records one of four verdicts. The loop is plain code:

    send conversation + tools -> model asks for a tool -> run it, send result -> repeat
    ... until record_verdict, or MAX_TURNS (then: needs_human)

Guardrails: tools are read-only, the turn count is capped, and the agent never decides the
underwriting outcome -- it only says whether a finding is real. Every call is kept in the
review's `steps` so a reviewer can see exactly how the verdict was reached.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..providers.llm import LLMProvider, LLMUnavailable
from ..schemas import AgentReview, AgentStep, Finding, Submission
from .tools import TOOLS, AgentTools

log = logging.getLogger("pipeline")

MAX_TURNS = 6

SYSTEM = """\
You re-check one finding produced by an insurance submission review system. The finding \
says two documents disagree (or that one document is out of date). Your job is to decide \
whether the difference is REALLY printed in the source documents, or whether it comes from \
a reading error (OCR, a photo, a formatting difference).

How to work:
- Look at the original sources with the tools. When a value came from OCR or a photo, view \
the image rather than trusting the text.
- Use decode_vin for any question about a vehicle's make or model year.
- Be brief: a few tool calls are usually enough. You have at most %d turns.
- Do not speculate about why a document is wrong; report only what the sources show.
- Finish with record_verdict:
    confirmed   the difference is really printed in the documents
    resolved    the values agree once read correctly (reading/formatting difference only)
    corrected   one source was misread; give the correct value and which source
    needs_human the evidence is not enough to decide
""" % MAX_TURNS


def _finding_prompt(f: Finding, sub: Submission) -> str:
    units = {u.unit_id: u for u in sub.units}
    ent = next((e for e in sub.reconciliation.entities if e.entity_id == f.entity_id), None)
    unit_ids = {o.unit_id for o in f.observations if o.unit_id}
    if ent:                                       # every page of every document behind the entity
        docs = {d.doc_id: d for d in sub.documents}
        for doc_id in ent.doc_ids:
            unit_ids.update(u for u in docs[doc_id].unit_ids if u not in docs[doc_id].excluded_unit_ids)
    lines = [f"Finding {f.finding_id} ({f.severity}, {f.kind.value}): {f.title}", f"Detail: {f.detail}", "",
             "Values and where they came from:"]
    for o in f.observations:
        lines.append(f"- {o.value!s} | {o.source} | unit {o.unit_id or 'n/a (derived)'}"
                     + (f' | quote "{o.quote}"' if o.quote else ""))
    lines += ["", "Source units you can read or view:"]
    for uid in sorted(unit_ids):
        u = units.get(uid)
        if u:
            lines.append(f"- {uid}: {u.label} (text from {u.text_source})")
    return "\n".join(lines)


def _summarize_input(args: dict) -> dict:
    return {k: (v if len(str(v)) < 200 else str(v)[:200] + "...") for k, v in args.items()}


def recheck_finding(f: Finding, sub: Submission, tools: AgentTools, llm: LLMProvider) -> AgentReview:
    messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": _finding_prompt(f, sub)}]}]
    steps: list[AgentStep] = []
    model = ""
    for _ in range(MAX_TURNS):
        try:
            res = llm.converse("agent", SYSTEM, messages, TOOLS, max_tokens=1500)
        except LLMUnavailable:
            return AgentReview(verdict="not_run", explanation="No model available (demo mode without a "
                                                             "recording of this review).", steps=steps)
        model = res.model
        content = res.data["content"]
        messages.append({"role": "assistant", "content": content})
        thought = " ".join(c["text"] for c in content if c["type"] == "text").strip()
        calls = [c for c in content if c["type"] == "tool_use"]
        if not calls:
            break
        results = []
        for call in calls:
            if call["name"] == "record_verdict":
                v = call["input"]
                steps.append(AgentStep(tool="record_verdict", input=_summarize_input(v), thought=thought))
                return AgentReview(verdict=v.get("verdict", "needs_human"),
                                   explanation=v.get("explanation", ""),
                                   corrected_value=v.get("corrected_value", "") or "",
                                   corrected_source=v.get("corrected_source", "") or "",
                                   confidence=float(v.get("confidence", 0) or 0), steps=steps, model=model)
            r = tools.run(call["name"], call["input"])
            steps.append(AgentStep(tool=call["name"], input=_summarize_input(call["input"]),
                                   result=r.summary, thought=thought))
            thought = ""
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": r.content,
                            "is_error": r.is_error})
        messages.append({"role": "user", "content": results})
    return AgentReview(verdict="needs_human", steps=steps, model=model,
                       explanation=f"No verdict within {MAX_TURNS} turns; left for a human reviewer.")


STATUS_BY_VERDICT = {"confirmed": "confirmed", "resolved": "resolved", "corrected": "resolved",
                     "needs_human": "needs_human", "not_run": "open"}


def recheck_findings(sub: Submission, llm: LLMProvider, work_dir: Path, render_dpi: int = 150) -> None:
    if not sub.reconciliation:
        return
    tools = AgentTools(sub, work_dir, render_dpi)
    todo = [f for f in sub.reconciliation.findings if f.needs_recheck]
    for n, f in enumerate(todo, start=1):
        f.review = recheck_finding(f, sub, tools, llm)
        f.status = STATUS_BY_VERDICT.get(f.review.verdict, "needs_human")
        log.info("  recheck %d/%d  %s %-55s -> %s (%d steps)", n, len(todo), f.finding_id,
                 f.title[:55], f.review.verdict, len(f.review.steps))


def review_json(f: Finding) -> str:
    return json.dumps(f.review.model_dump() if f.review else {}, indent=1)
