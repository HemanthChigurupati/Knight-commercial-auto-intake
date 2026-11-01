"""Regroup extracted facts by real-world entity, keeping every value and its source.

  applicant      one per submission
  driver         matched by license number (lookalikes allowed), then by name
  vehicle        matched by VIN
  policy_term    matched by policy period (start dates within 45 days = same term)
  ifta_quarter   matched by period end

Nothing is decided or discarded here: if three documents give three DOBs, the driver
entity holds all three. checks.py decides what the differences mean.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..pipeline.verify import normalize_license, normalize_vin, vin_decode
from ..schemas import DocType, Entity, Extraction, Fact, FactStatus, LogicalDocument, Observation
from .compare import license_match, person_label, same_person

USABLE = (FactStatus.FOUND, FactStatus.INVALID, FactStatus.UNVERIFIED)


def obs(fact: Fact | None, doc_id: str | None, source: str, value=None) -> Observation | None:
    """An observation from a fact (or a derived value when `value` is given)."""
    if value is not None:
        return Observation(value=value, doc_id=doc_id, source=source, confidence=0.99)
    if fact is None or fact.status not in USABLE or fact.value in (None, ""):
        return None
    return Observation(value=fact.value, doc_id=doc_id, source=source, unit_id=fact.unit_id,
                       quote=fact.quote, confidence=fact.confidence)


@dataclass
class Index:
    """The extractions arranged for lookup."""
    by_type: dict[DocType, list[Extraction]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)

    def all(self, t: DocType) -> list[Extraction]:
        return self.by_type.get(t, [])

    def one(self, t: DocType) -> Extraction | None:
        xs = self.all(t)
        return xs[0] if xs else None


def index(extractions: list[Extraction], documents: list[LogicalDocument]) -> Index:
    ix = Index(titles={d.doc_id: d.title for d in documents})
    for ex in extractions:
        if ex.status == "ok":
            ix.by_type.setdefault(ex.doc_type, []).append(ex)
    return ix


def _f(ex: Extraction | None, name: str) -> Fact | None:
    return ex.facts.get(name) if ex else None


def _d(iso: object) -> date | None:
    try:
        return date.fromisoformat(str(iso))
    except ValueError:
        return None


# ---------------------------------------------------------------- applicant

def build_applicant(ix: Index) -> Entity:
    a = Entity(entity_id="applicant", kind="applicant", label="Applicant")
    app, qf = ix.one(DocType.APPLICATION), ix.one(DocType.QUOTE_FORM)
    for ex, src in ((app, "Application"), (qf, "Quote form")):
        if not ex:
            continue
        for name in ("applicant_name", "dot_number", "mc_number", "mailing_address", "agency_name",
                     "years_in_business", "years_prior_insurance", "radius_breakdown",
                     "physical_damage_requested"):
            a.add(name, obs(_f(ex, name), ex.doc_id, src))
    for name in ("entity_type", "fein", "application_date", "effective_date", "owner_is_driver",
                 "liability_deductible", "garaging_address", "signed", "new_venture",
                 "intermodal", "cross_border", "farthest_territory"):
        a.add(name, obs(_f(app, name), app.doc_id if app else None, "Application"))
    if qf:
        a.add("hauls_hazmat", obs(_f(qf, "hauls_hazmat"), qf.doc_id, "Quote form"))

    # The insured's name as every other document prints it.
    for t, field_name in ((DocType.LOSS_RUN, "insured_name"), (DocType.NO_LOSS_LETTER, "insured_name"),
                          (DocType.IFTA_RETURN, "taxpayer_name")):
        for ex in ix.all(t):
            a.add("applicant_name", obs(_f(ex, field_name), ex.doc_id, ix.titles[ex.doc_id]))

    # Power units: rows on the vehicle schedule vs units the quote form says are insured now.
    vs = ix.one(DocType.VEHICLE_SCHEDULE)
    if vs:
        a.add("power_units", obs(None, vs.doc_id, "Vehicle schedule (rows counted)",
                                 value=len(vs.records.get("vehicles", []))))
    if qf:
        current = _current_prior_policy(qf, _f(app, "application_date"))
        if current:
            a.add("power_units", obs(current["units_insured"], qf.doc_id,
                                     "Quote form, current policy row"))
    return a


def _current_prior_policy(qf: Extraction, app_date: Fact | None) -> dict[str, Fact] | None:
    rows = qf.records.get("prior_policies", [])
    ref = _d(app_date.value) if app_date and app_date.value else None
    best = None
    for r in rows:
        s, e = _d(r["start_date"].value), _d(r["end_date"].value)
        if s and e and ref and s <= ref <= e:
            return r
        if s and (best is None or s > _d(best["start_date"].value)):
            best = r
    return best


# ---------------------------------------------------------------- drivers

@dataclass
class DriverRecord:
    source: str
    doc_id: str
    facts: dict[str, Fact]
    name: str
    role: str                # schedule | application | quote | mvr | license | claim


def _driver_records(ix: Index) -> list[DriverRecord]:
    recs: list[DriverRecord] = []
    for t, src, role in ((DocType.DRIVER_SCHEDULE, "Driver schedule", "schedule"),
                         (DocType.APPLICATION, "Application", "application"),
                         (DocType.QUOTE_FORM, "Quote form", "quote")):
        for ex in ix.all(t):
            for i, row in enumerate(ex.records.get("drivers", []), start=1):
                name = " ".join(str(row[k].value or "") for k in ("first_name", "last_name")).strip()
                recs.append(DriverRecord(f"{src} row {i}", ex.doc_id, row, name, role))
    for ex in ix.all(DocType.MVR):
        f = dict(ex.facts)
        recs.append(DriverRecord(ix.titles[ex.doc_id], ex.doc_id, f, str(f["driver_name"].value or ""), "mvr"))
    for ex in ix.all(DocType.DRIVER_LICENSE):
        f = dict(ex.facts)
        recs.append(DriverRecord(ix.titles[ex.doc_id], ex.doc_id, f, str(f["holder_name"].value or ""), "license"))
    return recs


DRIVER_FIELDS = {
    # entity field: {role: source fact name}
    "name": None,
    "dob": {"*": "dob"},
    "license_number": {"*": "license_number"},
    "license_state": {"*": "license_state"},
    "license_class": {"mvr": "license_class", "license": "license_class"},
    "license_type": {"mvr": "license_type"},
    "commercial": {"license": "commercial"},
    "license_issue_date": {"mvr": "license_issue_date", "license": "issue_date"},
    "license_expiry_date": {"mvr": "license_expiry_date", "license": "expiry_date"},
    "cdl_first_issued": {"mvr": "cdl_first_issued"},
    "first_licensed_year": {"mvr": "first_licensed_year"},
    "mvr_order_date": {"mvr": "order_date"},
    "mvr_stale_marker": {"mvr": "stale_marker"},
    "experience": {"schedule": "experience", "application": "experience", "quote": "experience"},
    "hire_date": {"schedule": "hire_date", "application": "hire_date", "quote": "hire_date"},
}


def build_drivers(ix: Index) -> tuple[list[Entity], list[tuple[str, str, str]]]:
    """Returns driver entities and the license lookalike pairs seen while matching."""
    drivers: list[Entity] = []
    keys: dict[str, list[str]] = {}            # entity_id -> license numbers seen
    lookalikes: list[tuple[str, str, str]] = []

    def find(rec: DriverRecord) -> Entity | None:
        lic = rec.facts.get("license_number")
        num = normalize_license(str(lic.value)) if lic and lic.value else ""
        if num:
            for e in drivers:
                for seen in keys[e.entity_id]:
                    m = license_match(num, seen)
                    if m == "exact":
                        return e
                    if m == "lookalike":
                        lookalikes.append((e.entity_id, seen, num))
                        return e
        for e in drivers:                      # fall back to the name
            if rec.name and any(same_person(rec.name, str(o.value)) for o in e.fields.get("name", [])):
                return e
        return None

    for rec in _driver_records(ix):
        e = find(rec)
        if e is None:
            lic = rec.facts.get("license_number")
            key = normalize_license(str(lic.value)) if lic and lic.value else re.sub(r"\W", "", rec.name)
            e = Entity(entity_id=f"driver:{key}", kind="driver", label=person_label(rec.name))
            drivers.append(e)
            keys[e.entity_id] = []
        lic = rec.facts.get("license_number")
        if lic and lic.value:
            keys[e.entity_id].append(normalize_license(str(lic.value)))
        e.add("name", Observation(value=rec.name, doc_id=rec.doc_id, source=rec.source,
                                  confidence=0.99))
        e.add("appears_in", Observation(value=rec.role, doc_id=rec.doc_id, source=rec.source,
                                        confidence=0.99))
        for field_name, src in DRIVER_FIELDS.items():
            if not src:
                continue
            fact_name = src.get(rec.role) or src.get("*")
            if fact_name:
                e.add(field_name, obs(rec.facts.get(fact_name), rec.doc_id, rec.source))
        if rec.role == "license" and "commercial" in rec.facts:
            c = rec.facts["commercial"]
            if c.value is True:
                e.add("license_type", obs(None, rec.doc_id, rec.source, value="COMMERCIAL"))

    # Drivers named in loss-run claims: matched by name only (claims carry no license number).
    for ex in ix.all(DocType.LOSS_RUN):
        for row in ex.records.get("claims", []):
            name = str(row["driver_name"].value or "")
            if not name:
                continue
            claim = row["claim_number"].value
            src = f"{ix.titles[ex.doc_id]}, claim {claim}"
            match = next((e for e in drivers if any(same_person(name, str(o.value))
                                                    for o in e.fields.get("name", []))), None)
            if match is None:
                match = Entity(entity_id=f"driver:{clean_key(name)}", kind="driver",
                               label=person_label(name))
                drivers.append(match)
                keys[match.entity_id] = []
                match.add("name", obs(row["driver_name"], ex.doc_id, src))
            match.add("appears_in", Observation(value="claim", doc_id=ex.doc_id, source=src,
                                                confidence=0.99))
            match.add("claims", obs(row["claim_number"], ex.doc_id, src))
    return drivers, lookalikes


def clean_key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# ---------------------------------------------------------------- vehicles

def build_vehicles(ix: Index) -> list[Entity]:
    vehicles: dict[str, Entity] = {}

    def entity(vin: str) -> Entity:
        if vin not in vehicles:
            vehicles[vin] = Entity(entity_id=f"vehicle:{vin}", kind="vehicle", label=vin)
            dec = vin_decode(vin)
            if dec["make"]:
                vehicles[vin].add("make", obs(None, None, "VIN decode", value=dec["make"]))
            if dec["model_year"] and len(vin) == 17:
                vehicles[vin].add("year", obs(None, None, "VIN decode", value=dec["model_year"]))
        return vehicles[vin]

    for t, src, role in ((DocType.VEHICLE_SCHEDULE, "Vehicle schedule", "schedule"),
                         (DocType.APPLICATION, "Application", "application")):
        for ex in ix.all(t):
            for i, row in enumerate(ex.records.get("vehicles", []), start=1):
                if not row["vin"].value:
                    continue
                e = entity(normalize_vin(str(row["vin"].value)))
                label = f"{src} row {i}"
                for k in ("year", "make", "model", "value", "vin"):
                    e.add(k, obs(row[k], ex.doc_id, label))
                e.add("appears_in", Observation(value=role, doc_id=ex.doc_id, source=label,
                                                confidence=0.99))
                if row["year"].value and row["make"].value:
                    e.label = f"{row['year'].value} {str(row['make'].value).title()} ({e.label})"

    for ex in ix.all(DocType.LOSS_RUN):
        for row in ex.records.get("claims", []):
            if not row["vehicle_vin"].value:
                continue
            e = entity(normalize_vin(str(row["vehicle_vin"].value)))
            src = f"{ix.titles[ex.doc_id]}, claim {row['claim_number'].value}"
            desc = str(row["vehicle_description"].value or "")
            m = re.match(r"\s*(\d{4})\s*[-\s]\s*([A-Za-z][A-Za-z ]*?)\s*-?\s*$", desc)
            if m:
                d = row["vehicle_description"]
                e.add("year", Observation(value=int(m.group(1)), doc_id=ex.doc_id, source=src,
                                          unit_id=d.unit_id, quote=d.quote, confidence=d.confidence))
                e.add("make", Observation(value=m.group(2).strip().upper(), doc_id=ex.doc_id, source=src,
                                          unit_id=d.unit_id, quote=d.quote, confidence=d.confidence))
            e.add("vin", obs(row["vehicle_vin"], ex.doc_id, src))
            e.add("appears_in", Observation(value="claim", doc_id=ex.doc_id, source=src, confidence=0.99))
            e.add("claims", obs(row["claim_number"], ex.doc_id, src))
            if e.label == e.entity_id.split(":", 1)[1] and m:
                e.label = f"{m.group(1)} {m.group(2).strip().title()} ({e.label})"
    return list(vehicles.values())


# ---------------------------------------------------------------- policy terms

def build_terms(ix: Index) -> list[Entity]:
    terms: list[Entity] = []

    def term_for(start: object, end: object) -> Entity | None:
        s = _d(start)
        if not s:
            return None
        for t in terms:
            ts = _d(t.fields["period_start"][0].value)
            if ts and abs((ts - s).days) <= 45:
                return t
        t = Entity(entity_id=f"term:{s.isoformat()}", kind="policy_term",
                   label=f"Policy term {s.isoformat()} to {end or '?'}")
        terms.append(t)
        return t

    qf = ix.one(DocType.QUOTE_FORM)
    if qf:
        for i, row in enumerate(qf.records.get("prior_policies", []), start=1):
            t = term_for(row["start_date"].value, row["end_date"].value)
            if not t:
                continue
            src = f"Quote form prior policy row {i}"
            for k, name in (("start_date", "period_start"), ("end_date", "period_end"),
                            ("carrier", "carrier"), ("units_insured", "units_insured")):
                t.add(name, obs(row[k], qf.doc_id, src))
            t.add("listed_on_application", obs(row["coverage"], qf.doc_id, src))
    for ex in ix.all(DocType.LOSS_RUN):
        t = term_for(_f(ex, "period_start").value, _f(ex, "period_end").value)
        if not t:
            continue
        src = ix.titles[ex.doc_id]
        for name in ("period_start", "period_end", "carrier", "run_date", "total_incurred"):
            t.add(name, obs(_f(ex, name), ex.doc_id, src))
        cov = str(_f(ex, "coverage").value or "").upper()
        line = "cargo" if "CARGO" in cov else "auto_liability" if "LIAB" in cov else "other"
        t.add(f"loss_run_{line}", obs(_f(ex, "policy_number"), ex.doc_id, src))
    for ex in ix.all(DocType.NO_LOSS_LETTER):
        t = term_for(_f(ex, "period_start").value, _f(ex, "period_end").value)
        if not t:
            continue
        src = ix.titles[ex.doc_id]
        t.add("period_start", obs(_f(ex, "period_start"), ex.doc_id, src))
        t.add("carrier", obs(_f(ex, "issuer"), ex.doc_id, src))
        pol = str(_f(ex, "policy_number").value or "").upper()
        line = "cargo" if "MTC" in pol or "CARGO" in pol else "unknown"
        t.add(f"no_loss_letter_{line}", obs(_f(ex, "statement"), ex.doc_id, src))
    terms.sort(key=lambda t: str(t.fields["period_start"][0].value), reverse=True)
    return terms


# ---------------------------------------------------------------- IFTA quarters

def build_quarters(ix: Index) -> list[Entity]:
    out = []
    for ex in ix.all(DocType.IFTA_RETURN):
        end = _f(ex, "period_end").value
        q = Entity(entity_id=f"ifta:{end}", kind="ifta_quarter", label=ix.titles[ex.doc_id])
        for name in ("taxpayer_name", "period_start", "period_end", "total_miles", "penalty",
                     "interest", "jurisdictions"):
            q.add(name, obs(_f(ex, name), ex.doc_id, ix.titles[ex.doc_id]))
        out.append(q)
    out.sort(key=lambda q: str(q.fields.get("period_end", [Observation(value="", source="")])[0].value),
             reverse=True)
    return out
