"""Checks on extracted values, in plain code (no AI).

  1. Grounding     -- is the model's quote really on the page it cites?
  2. Normalization -- '09/14/79' -> '1979-09-14', ' 3akj glD59 ' -> '3AKJGLD59', '$1,000' -> 1000.0
  3. Validation    -- VIN length, characters and check digit; dates that parse
  4. Confidence    -- model's own score x grounding, discounted when the value cannot be parsed

Reading confidence (did we read the page correctly?) is kept separate from data problems
(is the value acceptable?). A 16-character VIN read perfectly is high-confidence AND invalid:
the document is wrong, not the reading. Rules (stage 5) act on `checks`; reviewers look at
low `confidence`.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from ..schemas import Fact, FactStatus

# ---------------------------------------------------------------- grounding

GROUND_EXACT, GROUND_PARTIAL, GROUND_NONE, GROUND_NO_QUOTE = 1.0, 0.85, 0.4, 0.5


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'")
    s = re.sub(r"[■□☐☑☒]", " ", s)      # checkbox glyphs
    return re.sub(r"\s+", " ", s).strip()


def grounding_score(quote: str, page_text: str) -> float:
    """1.0 if the quote appears verbatim (ignoring case/spacing); 0.85 if most of its words
    do (OCR noise, wrapped lines); 0.4 if not -- the model may have made it up."""
    q, t = _norm(quote), _norm(page_text)
    if not q:
        return GROUND_NO_QUOTE
    if q in t:
        return GROUND_EXACT
    words = [w for w in re.findall(r"[a-z0-9$.,/-]+", q) if len(w) > 1]
    if words:
        hits = sum(1 for w in words if w in t)
        if hits / len(words) >= 0.8:
            return GROUND_PARTIAL
    return GROUND_NONE


# ---------------------------------------------------------------- normalizers

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
                 "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y.", "%m/%d/%Y %H:%M:%S")


def parse_date(raw: str) -> date | None:
    s = raw.strip().rstrip(".").strip()
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        # Two-digit years: '79' is 1979, '24' is 2024 (pivot 30 years ahead of today).
        if fmt.endswith("%y") and d.year > date.today().year + 1:
            d = d.replace(year=d.year - 100)
        return d
    return None


def parse_money(raw: str) -> float | None:
    s = raw.replace(",", "").replace("$", "").strip()
    m = re.fullmatch(r"-?\d+(\.\d+)?", s)
    return float(s) if m else None


def parse_int(raw: str) -> int | None:
    m = re.search(r"-?\d[\d,]*", raw)
    return int(m.group(0).replace(",", "")) if m else None


def parse_percent(raw: str) -> float | None:
    m = re.search(r"\d+(\.\d+)?", raw)
    if not m:
        return None
    v = float(m.group(0))
    return v / 100 if v > 1 else v           # '30', '30%' and '0.3' all mean 0.30


def parse_bool(raw: str) -> bool | None:
    s = _norm(raw)
    if s in ("yes", "y", "true", "x", "checked", "marked"):
        return True
    if s in ("no", "n", "false", "unchecked", "not marked", "blank"):
        return False
    return None


# ---------------------------------------------------------------- VIN

_VIN_VALUES = {**{str(i): i for i in range(10)},
               **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))),
               "P": 7, "R": 9, **dict(zip("STUVWXYZ", range(2, 10)))}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
# Position 10 -> model year (30-year cycle; 2010-2039 window for current fleets).
_VIN_YEARS = dict(zip("ABCDEFGHJKLMNPRSTVWXY123456789", range(2010, 2040)))
# World manufacturer identifiers seen in trucking fleets (first three characters).
VIN_MAKES = {"4V4": "VOLVO", "4V5": "VOLVO", "3HS": "INTERNATIONAL", "1HS": "INTERNATIONAL",
             "3AK": "FREIGHTLINER", "1FU": "FREIGHTLINER", "1XP": "PETERBILT", "1XK": "KENWORTH",
             "1M1": "MACK", "1NP": "PETERBILT", "2NP": "PETERBILT", "3WK": "KENWORTH"}


def normalize_vin(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw).upper()


def vin_checks(vin: str) -> list[str]:
    """Data problems with a VIN. Empty list = structurally valid."""
    problems = []
    if len(vin) != 17:
        problems.append(f"VIN must be 17 characters (has {len(vin)})")
    bad = sorted(set(c for c in vin if c in "IOQ" or not c.isalnum()))
    if bad:
        problems.append(f"VIN contains characters never used in VINs: {''.join(bad)}")
    if len(vin) == 17 and not bad:
        total = sum(_VIN_VALUES.get(c, 0) * w for c, w in zip(vin, _VIN_WEIGHTS))
        expected = "X" if total % 11 == 10 else str(total % 11)
        if vin[8] != expected:
            problems.append(f"VIN check digit is {vin[8]}, expected {expected} (likely mistyped or misread)")
    return problems


def vin_decode(vin: str) -> dict:
    """Make and model year encoded in the VIN (used by reconciliation and the agent)."""
    return {"make": VIN_MAKES.get(vin[:3]),
            "model_year": _VIN_YEARS.get(vin[9]) if len(vin) >= 10 else None}


# ---------------------------------------------------------------- one fact

def finalize(fact: Fact, page_text: str) -> Fact:
    """Normalize, validate and score one fact in place."""
    raw = fact.raw.strip()
    if not raw:
        fact.status, fact.value = FactStatus.MISSING, None
        fact.confidence = round(min(fact.model_confidence, 0.99), 3)   # confidence it is absent
        return fact

    parsed: object = raw
    parse_failed = False
    k = fact.kind
    if k == "date" and not re.search(r"\d", raw):
        pass                      # a word written where a date goes (e.g. hire date "OWNER")
    elif k == "date":
        d = parse_date(raw)
        parsed, parse_failed = (d.isoformat() if d else raw), d is None
        if parse_failed:
            fact.checks.append(f"Date '{raw}' could not be read as a date")
    elif k == "money":
        parsed = parse_money(raw)
        parse_failed = parsed is None
    elif k == "int":
        parsed = parse_int(raw)
        parse_failed = parsed is None
    elif k == "percent":
        parsed = parse_percent(raw)
        parse_failed = parsed is None
    elif k == "bool":
        parsed = parse_bool(raw)
        parse_failed = parsed is None
    elif k == "vin":
        parsed = normalize_vin(raw)
        fact.checks += vin_checks(parsed)
    if parse_failed and k != "date":
        parsed = raw
        fact.checks.append(f"Expected a {k} but read '{raw}'")
    fact.value = parsed

    fact.grounding = grounding_score(fact.quote, page_text)
    conf = fact.model_confidence * fact.grounding * (0.6 if parse_failed else 1.0)
    fact.confidence = round(min(conf, 0.99), 3)

    if fact.grounding <= GROUND_NONE:
        fact.status = FactStatus.UNVERIFIED
        fact.checks.append("Quoted text not found on the cited page; value may be invented")
    elif fact.checks:
        fact.status = FactStatus.INVALID
    else:
        fact.status = FactStatus.FOUND
    return fact
