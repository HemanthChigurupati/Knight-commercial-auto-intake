"""Turn entities into findings: what the documents disagree on, and what they fail to cover.

Finding kinds (most to least serious, typically):
  conflict           sources disagree on a value (Morales DOB 1991-10-03 vs 1991-10-30)
  gap                one document mentions what another should cover (claim driver not scheduled)
  possible_misread   values differ only by characters OCR confuses (07314062 vs O7314062)
  outdated_document  an older document disagrees with a newer one (expired license card)
  superseded         a newer copy of the same record exists (two MVRs for one driver)

Plain code only. Stage 4 re-reads the sources for findings marked `needs_recheck`;
stage 5 decides what the guideline says to do about them.
"""
from __future__ import annotations

from datetime import date

from ..schemas import Entity, Finding, FindingKind, Observation, Reconciliation, Submission
from .compare import clean, company_difference, same_value
from .entities import build_applicant, build_drivers, build_quarters, build_terms, build_vehicles, index

# entity kind -> field -> (comparison, severity)
COMPARE: dict[str, dict[str, tuple[str, str]]] = {
    "applicant": {
        "applicant_name": ("company", "high"), "dot_number": ("text", "high"),
        "mc_number": ("text", "high"), "power_units": ("number", "high"),
        "mailing_address": ("address", "medium"), "agency_name": ("org", "medium"),
        "years_in_business": ("number", "medium"), "years_prior_insurance": ("text", "low"),
        "radius_breakdown": ("text", "medium"), "physical_damage_requested": ("exact", "low"),
    },
    "driver": {
        "name": ("person", "medium"), "dob": ("text", "high"), "license_state": ("state", "medium"),
        "license_class": ("text", "high"),
    },
    "vehicle": {
        "make": ("text", "high"), "year": ("number", "high"), "model": ("text", "low"),
        "value": ("number", "low"),
    },
    "policy_term": {"carrier": ("org", "medium")},   # dates: see term_checks
}

FIELD_LABELS = {"dob": "date of birth", "applicant_name": "applicant name", "power_units": "number of power units",
                "agency_name": "agency name", "radius_breakdown": "operating radius", "make": "make",
                "year": "model year", "carrier": "carrier", "period_start": "policy start date",
                "period_end": "policy end date", "license_state": "license state",
                "license_class": "license class", "mailing_address": "mailing address"}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _same(kind: str, a: object, b: object) -> bool:
    if kind == "org":
        # 'GRANITE PEAK SPECIALTY' is the same carrier as 'GRANITE PEAK SPECIALTY INSURANCE COMPANY'.
        ta, tb = set(clean(a).split()), set(clean(b).split())
        return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)
    return same_value(kind, a, b)


def _groups(values: list[Observation], kind: str) -> list[list[Observation]]:
    groups: list[list[Observation]] = []
    for o in values:
        for g in groups:
            if _same(kind, g[0].value, o.value):
                g.append(o)
                break
        else:
            groups.append([o])
    return groups


def _describe(groups: list[list[Observation]]) -> str:
    return "; ".join(f"{g[0].value!s} ({', '.join(o.source for o in g)})" for g in groups)


class Findings:
    def __init__(self, ocr_units: set[str]):
        self.items: list[Finding] = []
        self.ocr_units = ocr_units

    def add(self, kind: FindingKind, severity: str, entity: Entity, title: str, detail: str = "",
            field: str = "", observations: list[Observation] | None = None,
            recheck: bool | None = None) -> None:
        observations = observations or []
        if recheck is None:
            # Re-read when the evidence came from OCR or the stakes are high. A gap has
            # nothing to re-read: the covering document is simply absent.
            recheck = kind != FindingKind.GAP and (
                severity == "high" or any(o.unit_id in self.ocr_units for o in observations))
        self.items.append(Finding(finding_id="", kind=kind, severity=severity, entity_id=entity.entity_id,
                                  field=field, title=title, detail=detail, observations=observations,
                                  needs_recheck=recheck))


def _d(v: object) -> date | None:
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


# ---------------------------------------------------------------- generic field conflicts

def field_conflicts(e: Entity, out: Findings, skip_docs: set[str]) -> None:
    for name, (kind, severity) in COMPARE.get(e.kind, {}).items():
        values = [o for o in e.fields.get(name, []) if o.doc_id not in skip_docs]
        groups = _groups(values, kind)
        if len(groups) < 2:
            continue
        label = FIELD_LABELS.get(name, name.replace("_", " "))
        detail = _describe(groups)
        if name == "applicant_name":
            diffs = {company_difference(str(groups[0][0].value), str(g[0].value)) for g in groups[1:]}
            label = f"applicant {', '.join(sorted(diffs))}" if diffs else label
        if e.kind == "vehicle" and any(o.source == "VIN decode" for g in groups for o in g):
            decoded = next(o.value for g in groups for o in g if o.source == "VIN decode")
            detail += f". The VIN itself encodes {decoded}."
        out.add(FindingKind.CONFLICT, severity, e, f"{e.label}: {label} differs between documents",
                detail, name, [o for g in groups for o in g])


# ---------------------------------------------------------------- drivers

def driver_checks(e: Entity, out: Findings, applicant: Entity) -> set[str]:
    """Driver-specific findings. Returns doc ids of superseded MVRs to leave out of comparisons."""
    roles = {o.value for o in e.fields.get("appears_in", [])}
    superseded: set[str] = set()

    # Two MVRs for one driver: use the newest, set the others aside.
    mvr_dates = [(o, _d(o.value)) for o in e.fields.get("mvr_order_date", [])]
    if len(mvr_dates) > 1:
        mvr_dates.sort(key=lambda t: t[1] or date.min, reverse=True)
        newest = mvr_dates[0][0]
        for o, d in mvr_dates[1:]:
            superseded.add(o.doc_id)
            marker = next((m.value for m in e.fields.get("mvr_stale_marker", []) if m.doc_id == o.doc_id), "")
            out.add(FindingKind.SUPERSEDED, "low", e, f"{e.label}: older MVR set aside",
                    f"MVR ordered {o.value}{' (stamped ' + repr(marker) + ')' if marker else ''} is "
                    f"superseded by the MVR ordered {newest.value}; only the newer one is used.",
                    "mvr_order_date", [newest, o], recheck=False)

    # License numbers that differ only by OCR lookalikes.
    nums = e.fields.get("license_number", [])
    distinct = {clean(o.value).replace(" ", "") for o in nums}
    if len(distinct) > 1:
        out.add(FindingKind.POSSIBLE_MISREAD, "low", e,
                f"{e.label}: license number read differently",
                f"{_describe(_groups(nums, 'text'))}. The values differ only by characters that are "
                "easy to confuse (e.g. letter O and digit 0); likely the same license.",
                "license_number", nums, recheck=True)

    # License card vs MVR: an older card is outdated, not a conflict.
    exp = [o for o in e.fields.get("license_expiry_date", []) if o.doc_id not in superseded]
    if len({o.value for o in exp}) > 1:
        exp.sort(key=lambda o: str(o.value))
        oldest, newest = exp[0], exp[-1]
        issues = e.fields.get("license_issue_date", [])
        out.add(FindingKind.OUTDATED_DOCUMENT, "low", e,
                f"{e.label}: license card is older than the MVR",
                f"{oldest.source} shows the license expiring {oldest.value}; {newest.source} shows "
                f"{newest.value}. The newer record is used." +
                (f" Issue dates: {_describe(_groups(issues, 'text'))}." if issues else ""),
                "license_expiry_date", exp + issues)

    # Coverage between documents.
    if "schedule" not in roles and "claim" in roles:
        out.add(FindingKind.GAP, "high", e, f"{e.label}: driver in loss history is not on the driver schedule",
                "A claim lists this driver, but no driver schedule, MVR or license covers them.",
                "appears_in", e.fields.get("claims", []))
    elif "schedule" not in roles and roles & {"mvr", "license"}:
        out.add(FindingKind.GAP, "medium", e, f"{e.label}: MVR or license provided for a driver not on the schedule",
                observations=e.fields.get("appears_in", []), field="appears_in")
    if "schedule" in roles and "mvr" not in roles:
        out.add(FindingKind.GAP, "high", e, f"{e.label}: no MVR provided for a scheduled driver",
                field="appears_in", observations=e.fields.get("appears_in", []))

    # The application says the owner does not drive, but the owner is on the driver schedule.
    owner_rows = [o for o in e.fields.get("hire_date", []) if clean(o.value) == "OWNER"]
    owner_flag = [o for o in applicant.fields.get("owner_is_driver", []) if o.value is False]
    if owner_rows and owner_flag:
        out.add(FindingKind.CONFLICT, "medium", e,
                f"{e.label}: owner listed as a driver, but the application says the owner does not drive",
                f"Driver rows give hire date 'OWNER' ({', '.join(o.source for o in owner_rows)}); "
                f"the application answers 'Is Owner a driver?' with No.",
                "owner_is_driver", owner_rows + owner_flag)
    return superseded


# ---------------------------------------------------------------- vehicles and terms

def vehicle_checks(e: Entity, out: Findings) -> None:
    roles = {o.value for o in e.fields.get("appears_in", [])}
    if "claim" in roles and "schedule" not in roles:
        out.add(FindingKind.GAP, "high", e, f"{e.label}: vehicle in loss history is not on the vehicle schedule",
                "A claim involves this vehicle, but the vehicle schedule does not list it.",
                "appears_in", e.fields.get("claims", []) + e.fields.get("vin", []))


def term_checks(e: Entity, out: Findings) -> None:
    # Coverages written a day apart (liability 12/15, cargo 12/16) are one small finding,
    # not one per date; a gap of more than a week is a real disagreement.
    for edge in ("period_start", "period_end"):
        dates = [(o, _d(o.value)) for o in e.fields.get(edge, []) if _d(o.value)]
        if not dates:
            continue
        lo = min(d for _, d in dates)
        hi = max(d for _, d in dates)
        if lo == hi:
            continue
        days = (hi - lo).days
        if edge == "period_end" and days <= 7:
            continue                          # reported once, with the start dates
        groups = _groups([o for o, _ in dates], "text")
        if days <= 7:
            out.add(FindingKind.CONFLICT, "low", e,
                    f"{e.label}: coverages on this term are dated {days} day{'s' if days > 1 else ''} apart",
                    _describe(groups), edge, [o for g in groups for o in g], recheck=False)
        else:
            out.add(FindingKind.CONFLICT, "medium", e,
                    f"{e.label}: {FIELD_LABELS[edge]} differs by {days} days between documents",
                    _describe(groups), edge, [o for g in groups for o in g])

    listed = e.fields.get("listed_on_application", [])
    al = e.fields.get("loss_run_auto_liability", [])
    cargo = e.fields.get("loss_run_cargo", [])
    letter = e.fields.get("no_loss_letter_cargo", [])
    if listed and not al:
        out.add(FindingKind.GAP, "high", e, f"{e.label}: no loss run provided for a prior term",
                f"The quote form lists this term ({_describe(_groups(listed, 'text'))}) but no auto "
                "liability loss run covers it" + ("" if cargo or letter else ", and nothing covers cargo") + ".",
                "loss_runs", listed + e.fields.get("carrier", []))
    elif listed and "MTC" in clean(listed[0].value) and not cargo and letter:
        out.add(FindingKind.GAP, "low", e, f"{e.label}: cargo history covered only by a no-loss letter",
                f"Letter states: {letter[0].value!s}. A letter is weaker than a loss run "
                "(e.g. 'no paid losses' says nothing about open or reserved claims).",
                "loss_runs", letter, recheck=False)


# ---------------------------------------------------------------- entry point

def reconcile(sub: Submission) -> Reconciliation:
    ix = index(sub.extractions, sub.documents)
    ocr_units = {u.unit_id for u in sub.units if u.text_source.startswith("ocr:")}
    out = Findings(ocr_units)

    applicant = build_applicant(ix)
    drivers, _ = build_drivers(ix)
    vehicles = build_vehicles(ix)
    terms = build_terms(ix)
    quarters = build_quarters(ix)

    field_conflicts(applicant, out, set())
    for d in drivers:
        skip = driver_checks(d, out, applicant)
        field_conflicts(d, out, skip)
    for v in vehicles:
        field_conflicts(v, out, set())
        vehicle_checks(v, out)
    for t in terms:
        field_conflicts(t, out, set())
        term_checks(t, out)

    out.items.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.kind.value))
    for i, f in enumerate(out.items, start=1):
        f.finding_id = f"F{i:02d}"
    return Reconciliation(entities=[applicant, *drivers, *vehicles, *terms, *quarters],
                          findings=out.items)
