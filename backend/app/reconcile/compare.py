"""How to decide whether two values from different documents mean the same thing.

Each kind of value needs its own notion of "same":
  people     'MORALES, LUIS E' == 'LUIS E MORALES';  'ALEKSANDR P' fits 'ALEKSANDR PAVEL'
  companies  punctuation and case ignored, but 'INC' vs 'LLC' is a real difference
  states     'CALIFORNIA' == 'CA'
  licenses   spaces ignored; O/0-style differences are lookalikes, not new licenses
  addresses  punctuation and spacing ignored
  dates, numbers: exact
"""
from __future__ import annotations

import re

from ..pipeline.verify import license_match  # noqa: F401  (re-exported for reconcile)

STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC"}

LEGAL_SUFFIXES = {"INC": "INC", "INCORPORATED": "INC", "LLC": "LLC", "L L C": "LLC",
                  "CORP": "CORP", "CORPORATION": "CORP", "CO": "CO", "COMPANY": "CO",
                  "LTD": "LTD", "LP": "LP", "LLP": "LLP"}


def clean(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())).strip()


def state_code(s: object) -> str:
    c = clean(s)
    return STATES.get(c, c)


# ---------------------------------------------------------------- people

def person_parts(name: str) -> tuple[str, list[str], str]:
    """(first, middles, last). Handles 'LAST, FIRST M' and 'FIRST M LAST'."""
    raw = str(name).upper().strip()
    if "," in raw:
        last, rest = raw.split(",", 1)
        tokens = clean(rest).split()
        return (tokens[0] if tokens else "", tokens[1:], clean(last))
    tokens = clean(raw).split()
    if not tokens:
        return "", [], ""
    if len(tokens) == 1:
        return "", [], tokens[0]
    return tokens[0], tokens[1:-1], tokens[-1]


def same_person(a: str, b: str) -> bool:
    fa, ma, la = person_parts(a)
    fb, mb, lb = person_parts(b)
    if not la or la != lb:
        return False
    if fa and fb and fa != fb:
        return False
    # Middle names may be written in full or as an initial: P fits PAVEL.
    for x, y in zip(ma, mb):
        if x[0] != y[0]:
            return False
    return True


def person_label(name: str) -> str:
    f, m, l = person_parts(name)
    return " ".join(p for p in [f, *m, l] if p).title()


# ---------------------------------------------------------------- companies

def company_parts(name: str) -> tuple[str, str]:
    """('COBALT MESA TRANSPORT', 'INC')."""
    words = clean(name).split()
    suffix = ""
    while words and words[-1] in LEGAL_SUFFIXES:
        suffix = LEGAL_SUFFIXES[words.pop()] if not suffix else suffix
    return " ".join(words), suffix


def company_difference(a: str, b: str) -> str:
    """'' if the same company name; otherwise what differs."""
    (ba, sa), (bb, sb) = company_parts(a), company_parts(b)
    if ba != bb:
        return "name"
    if sa and sb and sa != sb:
        return f"legal form ({sa} vs {sb})"
    return ""


# ---------------------------------------------------------------- generic

def same_value(kind: str, a: object, b: object) -> bool:
    if kind == "person":
        return same_person(str(a), str(b))
    if kind == "company":
        return company_difference(str(a), str(b)) == ""
    if kind == "state":
        return state_code(a) == state_code(b)
    if kind == "address":
        return clean(a).replace(" ", "") == clean(b).replace(" ", "")
    if kind == "number":
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return clean(a) == clean(b)
    if kind == "exact":
        return a == b
    return clean(a) == clean(b)          # text, dates (ISO strings), VINs
