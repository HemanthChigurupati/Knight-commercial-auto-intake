"""Stage 2 -- extraction: turn each logical document into facts with proof.

For every document that has a spec (extraction/specs.py):
  1. Build a tool schema from the spec: each field = {value, quote, unit, confidence}
  2. Send the document's pages to Claude Sonnet, each marked with its unit id
  3. Check every answer in plain code (verify.py): grounding, normalization, validation

Values are asked for exactly as printed; normalization happens in code, never in the
prompt, so the quote and the raw value can be compared with the page.
"""
from __future__ import annotations

import logging
import re

from ..extraction.specs import DocSpec, spec_for
from ..providers.llm import Block, LLMProvider, LLMUnavailable
from ..schemas import ContentUnit, Extraction, Fact, LogicalDocument, Submission
from .verify import _norm, finalize

log = logging.getLogger("pipeline")

EXTRACT_SYSTEM = """\
You extract facts from ONE document of a commercial auto insurance submission for an \
underwriting system. Accuracy matters more than completeness.

Rules:
- Use only what this document shows. Never infer, guess, or fill in from general knowledge \
or from what similar documents usually say.
- value: exactly as printed. Do not reformat dates, numbers or names.
- quote: the shortest span copied character-for-character from the page that shows the \
value, including its nearby label when there is one (e.g. "DOB: 09/14/1979").
- unit: the id from the [[unit ...]] marker of the page the quote is on.
- confidence: your probability (0..1) that the value is read correctly.
- If the document does not state a field: value "", quote "", and confidence = how sure \
you are that it is absent.
- Checkboxes: a filled box (■) or X next to an option means it is marked. For yes/no \
fields answer "yes" or "no" as marked, or "" if neither is marked.
- Lists: one item per row actually present (skip headers, totals and placeholder rows such \
as "LIST ATTACHED"); quote = that row's text.
"""

FACT_PROPS = {
    "value": {"type": "string"},
    "quote": {"type": "string"},
    "unit": {"type": "string"},
    "confidence": {"type": "number"},
}
FACT_PROPS_ROW = {
    "quote": {"type": "string", "description": "The row's text, verbatim"},
    "unit": {"type": "string"},
    "confidence": {"type": "number"},
}


def tool_schema(spec: DocSpec) -> dict:
    props: dict = {}
    for f in spec.fields:
        props[f.name] = {"type": "object", "description": f.description,
                         "properties": FACT_PROPS, "required": list(FACT_PROPS)}
    for ls in spec.lists:
        row = {f.name: {"type": "string", "description": f.description} for f in ls.fields}
        row |= FACT_PROPS_ROW
        props[ls.name] = {"type": "array", "description": ls.description,
                          "items": {"type": "object", "properties": row, "required": list(row)}}
    return {"type": "object", "properties": props, "required": list(props)}



def _compact(text: str) -> str:
    # Layout-preserving PDF text pads columns with long runs of spaces; two are enough.
    return re.sub(r" {3,}", "  ", text)


def _resolve_unit(claimed: str, quote: str, units: list[ContentUnit]) -> ContentUnit | None:
    ids = {u.unit_id: u for u in units}
    if claimed in ids:
        return ids[claimed]
    q = _norm(quote)
    if q:
        for u in units:
            if q in _norm(u.text):
                return u
    return units[0] if len(units) == 1 else None


def _fact(name: str, kind: str, raw: str, quote: str, claimed_unit: str, conf: float,
          units: list[ContentUnit]) -> Fact:
    unit = _resolve_unit(claimed_unit, quote, units)
    page_text = unit.text if unit else "\n".join(u.text for u in units)
    fact = Fact(name=name, kind=kind, raw=raw or "", quote=quote or "",
                unit_id=unit.unit_id if unit else None,
                model_confidence=max(0.0, min(float(conf or 0), 1.0)))
    return finalize(fact, page_text)


def extract_document(doc: LogicalDocument, units: list[ContentUnit], llm: LLMProvider) -> Extraction:
    spec = spec_for(doc.doc_type)
    ex = Extraction(doc_id=doc.doc_id, doc_type=doc.doc_type)
    if spec is None or not units:
        ex.status = "skipped"
        return ex

    pages = "\n\n".join(f"[[unit {u.unit_id}]]\n{_compact(u.text)}" for u in units)
    blocks = [Block(text=f"Document type: {spec.purpose}\nTitle: {doc.title}\n\n{pages}")]
    try:
        res = llm.complete_json("extract", EXTRACT_SYSTEM, blocks, f"extract_{doc.doc_type.value}",
                                tool_schema(spec), max_tokens=6000)
    except LLMUnavailable:
        ex.status = "not_run"
        return ex
    except Exception as e:                       # one bad document must not stop the run
        ex.status, ex.error = "error", f"{type(e).__name__}: {e}"
        return ex

    data, ex.model = res.data, res.model
    for f in spec.fields:
        a = data.get(f.name) or {}
        ex.facts[f.name] = _fact(f.name, f.kind, a.get("value", ""), a.get("quote", ""),
                                 a.get("unit", ""), a.get("confidence", 0), units)
    for ls in spec.lists:
        rows = []
        for item in data.get(ls.name) or []:
            rows.append({f.name: _fact(f.name, f.kind, item.get(f.name, ""), item.get("quote", ""),
                                       item.get("unit", ""), item.get("confidence", 0), units)
                         for f in ls.fields})
        ex.records[ls.name] = rows
    return ex


def extract_documents(sub: Submission, llm: LLMProvider) -> list[Extraction]:
    by_id = {u.unit_id: u for u in sub.units}
    out = []
    for n, doc in enumerate(sub.documents, start=1):
        units = [by_id[i] for i in doc.unit_ids if i not in doc.excluded_unit_ids]
        ex = extract_document(doc, units, llm)
        out.append(ex)
        c = fact_counts(ex)
        log.info("  extract %d/%d  %-34s %-8s found %d, missing %d, flagged %d", n,
                 len(sub.documents), doc.title[:34], ex.status, c["found"], c["missing"],
                 c["invalid"] + c["unverified"])
    return out


def all_facts(ex: Extraction) -> list[Fact]:
    facts = list(ex.facts.values())
    for rows in ex.records.values():
        for row in rows:
            facts += row.values()
    return facts


def fact_counts(ex: Extraction) -> dict[str, int]:
    counts = {"found": 0, "missing": 0, "invalid": 0, "unverified": 0}
    for f in all_facts(ex):
        counts[f.status.value] += 1
    return counts
