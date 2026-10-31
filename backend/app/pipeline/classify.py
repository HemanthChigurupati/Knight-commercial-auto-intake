"""Content-based classification and regrouping into logical documents.

Two independent opinions per unit, then a combined confidence:

  1. Signals  -- deterministic keyword/layout evidence (cheap, explainable, works offline)
  2. LLM      -- Claude Haiku reads the unit (text, or the image when there is no text)

  agree     -> confidence = noisy-OR of both          (two independent witnesses)
  disagree  -> take the LLM's answer, discount it, and send to review if it falls below 0.70
  no LLM    -> signals alone, discounted

File names are never an input. They are compared against the result afterwards only to
record a "name says X, content says Y" observation for the reviewer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..schemas import (Classification, ClassifiedUnit, ContentUnit, DocType, LogicalDocument,
                       SourceFile, UnitKind)
from ..providers.llm import Block, LLMProvider, LLMUnavailable

REVIEW_THRESHOLD = 0.70

# (pattern, weight). Patterns are matched case-insensitively against the unit text.
SIGNALS: dict[DocType, list[tuple[str, float]]] = {
    DocType.APPLICATION: [
        (r"insurance application", 3), (r"date of application", 2),
        (r"effective date requested", 2), (r"fraud statement", 1.5), (r"fein", 1),
        (r"signature of insured", 1.5), (r"fleet list information", 1.5),
        (r"driver type \(company driver", 1.5)],
    DocType.QUOTE_FORM: [
        (r"quick quote form", 4), (r"operation type", 1.5), (r"radius info", 1.5),
        (r"commodities transported", 1.5), (r"prior policy dates", 1.5),
        (r"fleet supplemental", 1)],
    DocType.DRIVER_SCHEDULE: [
        (r"driver #|driver no", 2), (r"first name", 1), (r"last name", 1),
        (r"date of birth|\bdob\b", 1), (r"d\.o\.h|date of hire|hire date", 2),
        (r"license #|license number", 1.5), (r"exp\.? in yrs|years of exp", 1.5)],
    DocType.VEHICLE_SCHEDULE: [
        (r"vehicle #|unit #", 2), (r"\bvin\b", 1.5), (r"\bmake\b", 1), (r"\bmodel\b", 1),
        (r"cost new|stated value|\bvalue\b", 1), (r"total tiv|trailer interchange", 1),
        (r"\btractor\b", 0.5)],
    DocType.MVR: [
        (r"driver record", 3), (r"violations/convictions", 2), (r"suspensions/revocations", 2),
        (r"license and permit information", 2), (r"end of report for", 1.5),
        (r"mvr report", 1)],
    DocType.DRIVER_LICENSE: [
        (r"driver'?s? licen[cs]e", 1.5), (r"commercial driver licen[cs]e", 1.5),
        (r"\b(rstr|restr)\b", 2), (r"\bend(orsements?)? none\b", 2), (r"\bhgt\b", 2),
        (r"\biss\b", 1), (r"\bclass [abc]\b", 1), (r"specimen|sample data", 0.5)],
    DocType.LOSS_RUN: [
        (r"policy period", 1.5), (r"total claims", 2), (r"total incurred", 2),
        (r"claim number", 1.5), (r"date of loss", 1.5), (r"total reserved", 1.5),
        (r"run date", 1.5), (r"not been any claims", 1.5)],
    DocType.NO_LOSS_LETTER: [
        (r"to whom it may concern", 2), (r"no (paid )?losses", 3), (r"there have been no", 2)],
    DocType.IFTA_RETURN: [
        (r"\bifta\b", 3), (r"jurisdiction summary", 2.5), (r"taxable miles", 2),
        (r"tax paid gal", 2), (r"\bmpg\b", 1), (r"filing period|period begin", 1.5),
        (r"fuel type", 1)],
    DocType.UNDERWRITING_GUIDELINE: [
        (r"guideline", 2), (r"appetite", 2), (r"decline", 1), (r"routing", 1),
        (r"not eligible|eligib", 1), (r"submission requirements", 1.5)],
}

# A page containing one of these starts a new logical document even if the type repeats.
START_MARKERS: dict[DocType, str] = {
    DocType.MVR: r"driver record\s*-",
    DocType.LOSS_RUN: r"policy number:",
    DocType.IFTA_RETURN: r"online services filing record|taxpayer name",
}

# Pull a short subject (whose record / which term / which quarter) for titles and grouping.
SUBJECT_PATTERNS: dict[DocType, str] = {
    DocType.MVR: r"name:\s*([A-Z][A-Z ,.'-]+?)\s{2,}",
    DocType.LOSS_RUN: r"policy number:\s*(\S+)",
    DocType.IFTA_RETURN: r"period end:?\s*([A-Za-z]+ \d{1,2}, \d{4})",
}

# Only used to flag name/content mismatches -- never to classify.
FILENAME_HINTS: list[tuple[str, DocType]] = [
    (r"\bmvr\b", DocType.MVR), (r"driver", DocType.DRIVER_SCHEDULE),
    (r"vehicle", DocType.VEHICLE_SCHEDULE), (r"loss|^al |^mtc ", DocType.LOSS_RUN),
    (r"quote", DocType.QUOTE_FORM), (r"^q[1-4] \d{4}", DocType.IFTA_RETURN),
    (r"guideline", DocType.UNDERWRITING_GUIDELINE)]

DOC_TYPE_GUIDE = """\
application            Carrier's commercial auto insurance application (applicant, FEIN, DOT/MC, coverages, operations Q&A, signature)
quote_form             Agency quick-quote form summarizing the risk (operations, radius, commodities, prior carriers)
driver_schedule        List of drivers (names, DOB, license numbers, hire dates, experience)
vehicle_schedule       List of vehicles/power units (year, make, model, VIN, value)
mvr                    Motor vehicle record for ONE driver from a state/MVR vendor (violations, accidents, license status)
driver_license         A photo or scan of a physical driver's license card
loss_run               Insurer-issued claims history for one policy term (claims, paid, reserved, incurred)
no_loss_letter         A letter stating there were no losses, without a full claims report
ifta_return            Quarterly IFTA fuel-tax return or filing record (miles and gallons by jurisdiction)
underwriting_guideline The carrier's own underwriting rules (not part of an applicant submission)
other                  Anything else (cover email, lookup lists, blank pages)"""

CLASSIFY_SYSTEM = (
    "You classify pages from commercial auto insurance submissions for an underwriting "
    "intake system. Judge only by the content shown -- never by file names, which are "
    "often wrong. Pick exactly one type from this list:\n\n" + DOC_TYPE_GUIDE + "\n\n"
    "Confidence is your probability (0..1) that the type is correct. If the content is a "
    "continuation page (e.g. the 2nd page of an IFTA table), give the type of the "
    "document it continues. 'subject' is a short identifier of whose/which record this is "
    "(driver name, policy number, filing period), or empty.")

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": [t.value for t in DocType]},
        "confidence": {"type": "number"},
        "subject": {"type": "string"},
        "reason": {"type": "string", "description": "One sentence citing the decisive content."},
    },
    "required": ["doc_type", "confidence", "subject", "reason"],
}


@dataclass
class SignalResult:
    best: DocType | None
    score: float
    matched: list[str]


def signal_classify(text: str) -> SignalResult:
    t = text.lower()
    scores: dict[DocType, float] = {}
    hits: dict[DocType, list[str]] = {}
    for dt, rules in SIGNALS.items():
        total = sum(w for _, w in rules)
        got = [(p, w) for p, w in rules if re.search(p, t)]
        scores[dt] = sum(w for _, w in got) / total
        hits[dt] = [p for p, _ in got]
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, s1), (_, s2) = ranked[0], ranked[1]
    if s1 < 0.2:
        return SignalResult(None, 0.0, [])
    # Strength of evidence, discounted when a runner-up type is nearly as strong.
    score = min(1.0, s1 * 1.3) * (1 - 0.6 * (s2 / s1))
    return SignalResult(best, round(score, 3), hits[best])


def _llm_classify(llm: LLMProvider, unit: ContentUnit) -> dict:
    blocks = [Block(text=f"Content kind: {unit.kind.value}"
                         + (" (hidden worksheet)" if unit.hidden else ""))]
    if unit.text:
        blocks.append(Block(text="Content:\n" + unit.text[:6000]))
    if unit.image_path and (not unit.text or unit.kind != UnitKind.PDF_PAGE):
        blocks.append(Block(image_path=unit.image_path))
    return llm.complete_json("classify", CLASSIFY_SYSTEM, blocks, "page_classification",
                             CLASSIFY_SCHEMA, max_tokens=400).data


def classify_unit(unit: ContentUnit, llm: LLMProvider | None) -> tuple[Classification, str]:
    sig = signal_classify(unit.text) if unit.text else SignalResult(None, 0.0, [])
    llm_out: dict | None = None
    if llm is not None:
        try:
            llm_out = _llm_classify(llm, unit)
        except LLMUnavailable:
            llm_out = None

    if llm_out:
        lt, lc = DocType(llm_out["doc_type"]), float(llm_out["confidence"])
        if sig.best == lt:
            conf = 1 - (1 - lc) * (1 - sig.score)
            reason = f"Model and content signals agree. {llm_out['reason']}"
        elif sig.best is None:
            conf, reason = lc * 0.9, f"Model only (no strong keyword evidence). {llm_out['reason']}"
        else:
            conf = lc * (1 - 0.5 * sig.score)
            reason = (f"Model says {lt.value}; keyword signals suggest {sig.best.value}. "
                      f"{llm_out['reason']}")
        c = Classification(doc_type=lt, confidence=round(conf, 3), signal_type=sig.best,
                           signal_score=sig.score, llm_type=lt, llm_confidence=lc, reason=reason)
        subject = llm_out.get("subject", "")
    elif sig.best is not None:
        c = Classification(doc_type=sig.best, confidence=round(sig.score * 0.85, 3),
                           signal_type=sig.best, signal_score=sig.score,
                           reason="Keyword/layout signals only: matched "
                                  + ", ".join(sig.matched[:4]))
        subject = ""
    else:
        c = Classification(doc_type=DocType.OTHER, confidence=0.3,
                           reason="No recognizable content.")
        subject = ""
    if not subject and c.doc_type in SUBJECT_PATTERNS:
        m = re.search(SUBJECT_PATTERNS[c.doc_type], unit.text, re.I)
        subject = m.group(1).strip() if m else ""
    c.needs_review = c.confidence < REVIEW_THRESHOLD
    return c, subject


def classify_units(units: list[ContentUnit], llm: LLMProvider | None
                   ) -> tuple[list[ClassifiedUnit], dict[str, str]]:
    out, subjects = [], {}
    for u in units:
        c, subject = classify_unit(u, llm)
        out.append(ClassifiedUnit(unit=u, classification=c))
        subjects[u.unit_id] = subject
    return out, subjects


def _filename_hint(name: str) -> DocType | None:
    n = name.lower()
    for pat, dt in FILENAME_HINTS:
        if re.search(pat, n):
            return dt
    return None


def group_documents(classified: list[ClassifiedUnit], subjects: dict[str, str],
                    files: list[SourceFile]) -> list[LogicalDocument]:
    names = {f.file_id: f.name for f in files}
    docs: list[LogicalDocument] = []
    current: LogicalDocument | None = None
    texts: dict[str, list[str]] = {}
    for cu in classified:
        u, c = cu.unit, cu.classification
        subject = subjects.get(u.unit_id, "")
        same_file = current is not None and current.file_ids[0] == u.file_id
        # A weak page right after a document in the same PDF (e.g. the last rows of an IFTA
        # table) is a continuation page; a secondary worksheet of a workbook belongs to the
        # workbook's main document unless it is confidently something else.
        continuation = same_file and (
            (u.kind == UnitKind.PDF_PAGE and c.doc_type == DocType.OTHER)
            or (u.kind == UnitKind.SHEET and c.doc_type != current.doc_type and c.confidence < 0.8))
        starts_new = not continuation and (
            not same_file
            or current.doc_type != c.doc_type
            or (c.doc_type in START_MARKERS and re.search(START_MARKERS[c.doc_type], u.text, re.I))
            or u.kind in (UnitKind.EMBEDDED_IMAGE, UnitKind.IMAGE_FILE)
        )
        if starts_new:
            title = c.doc_type.value.replace("_", " ").title() + (f" - {subject}" if subject else "")
            current = LogicalDocument(
                doc_id=f"d{len(docs) + 1:02d}", doc_type=c.doc_type, confidence=c.confidence,
                unit_ids=[u.unit_id], file_ids=[u.file_id], title=title)
            docs.append(current)
            texts[current.doc_id] = [u.text]
        else:
            current.unit_ids.append(u.unit_id)
            texts[current.doc_id].append(u.text)
            # The document is as certain as its strongest page; continuation pages carry
            # little evidence of their own and should not drag it down.
            current.confidence = max(current.confidence, c.confidence)
            if continuation and c.doc_type != current.doc_type:
                current.observations.append(
                    f"{u.label} treated as a continuation of this document.")
        if u.hidden:
            current.observations.append(f"Includes hidden worksheet '{u.meta.get('sheet')}'.")
        if u.text_source.startswith("ocr:"):
            note = f"Text read by OCR ({u.text_source[4:]})."
            if note not in current.observations:
                current.observations.append(note)

    for d in docs:
        # Re-score signals on the whole document: evidence split across pages adds up.
        whole = signal_classify("\n".join(texts[d.doc_id]))
        if whole.best == d.doc_type:
            d.confidence = round(max(d.confidence, whole.score * 0.85), 3)
        fname = names[d.file_ids[0]]
        hint = _filename_hint(fname)
        if hint and hint != d.doc_type:
            d.observations.append(
                f"File name '{fname}' suggests {hint.value.replace('_', ' ')}, "
                f"but the content is {d.doc_type.value.replace('_', ' ')}.")
    return docs
