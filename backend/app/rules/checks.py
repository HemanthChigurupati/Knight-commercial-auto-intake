"""One function per kind of check. rules.yaml decides which checks run and with what
parameters; these functions only decide pass / fail / unknown / info and gather evidence.

Every check returns a Check. 'unknown' means the documents do not let us verify the rule;
the engine treats it like a referral -- never like a pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from ..pipeline.verify import normalize_vin
from ..schemas import FindingKind, Observation
from .facts import Facts, to_date


@dataclass
class Check:
    status: str                                  # pass | fail | unknown | info
    message: str
    evidence: list[Observation] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    on_fail_override: str | None = None          # e.g. a setting turns 'info' into 'decline'


def ok(msg, ev=None):
    return Check("pass", msg, ev or [])


def fail(msg, ev=None, fids=None):
    return Check("fail", msg, ev or [], fids or [])


def unknown(msg, ev=None):
    return Check("unknown", msg, ev or [])


def _years(d1, d2) -> float:
    return (d2 - d1).days / 365.25


def _label(e) -> str:
    return e.label.split(" (")[0]


# ---------------------------------------------------------------- appetite

def vehicle_types_eligible(fx: Facts, p: dict, s: dict) -> Check:
    bad, ev = [], []
    for v in fx.scheduled_vehicles:
        for o in fx.obs(v, "model"):
            ev.append(o)
            if any(k.upper() in str(o.value).upper() for k in p["prohibited"]):
                bad.append(f"{_label(v)}: {o.value}")
    if not fx.scheduled_vehicles:
        return unknown("No vehicle schedule to check.")
    if bad:
        return fail("Ineligible vehicle types: " + "; ".join(bad), ev)
    kinds = sorted({str(o.value).title() for o in ev})
    return ok(f"{len(fx.scheduled_vehicles)} scheduled units, all {', '.join(kinds)}.", ev)


def new_venture(fx: Facts, p: dict, s: dict) -> Check:
    yrs = [o for o in fx.obs(fx.applicant, "years_in_business")]
    if not yrs:
        return unknown("Years in business not stated.")
    years = max(float(o.value) for o in yrs)
    if years >= p["min_years_in_business"]:
        return ok(f"{years:g} years in business; not a new venture.", yrs)
    return fail(f"Only {years:g} years in business: new-venture requirements apply "
                "(2 years documented CDL experience and a resume/letter of experience).", yrs)


def no_liability_deductible(fx: Facts, p: dict, s: dict) -> Check:
    o = fx.obs(fx.applicant, "liability_deductible")
    if not o:
        return ok("No auto liability deductible requested.")
    v = str(o[0].value)
    if re.search(r"[1-9]", v):
        return fail(f"Auto liability deductible requested ({v}); not allowed.", o)
    return ok(f"Auto liability deductible: '{v}'.", o)


def physical_damage_not_requested(fx: Facts, p: dict, s: dict) -> Check:
    o = [x for x in fx.obs(fx.applicant, "physical_damage_requested") if x.value is True]
    if o:
        return Check("info", "Physical damage requested on " + ", ".join(sorted({x.source for x in o}))
                     + ". Not available in this program: quote liability (and cargo) only.", o)
    return ok("Physical damage not requested.")


# ---------------------------------------------------------------- states

def domicile_state_available(fx: Facts, p: dict, s: dict) -> Check:
    src = fx.obs(fx.applicant, "garaging_address") or fx.obs(fx.applicant, "mailing_address")
    if not src:
        return unknown("No garaging or mailing address stated.")
    m = re.search(r"\b([A-Z]{2})\s+\d{5}", str(src[0].value).upper())
    if not m:
        return unknown(f"State not readable from address '{src[0].value}'.", src)
    st = m.group(1)
    basis = "garaging" if fx.obs(fx.applicant, "garaging_address") else "mailing (no garaging address given)"
    if st in p["states"]:
        return ok(f"Based in {st} ({basis} address), an available state.", src[:1])
    return fail(f"Based in {st} ({basis} address), not an available state.", src[:1])


def operating_states(fx: Facts, p: dict, s: dict) -> Check:
    states, ev = fx.operating_states()
    if not states:
        return unknown("No IFTA jurisdictions to check.")
    outside = sorted(states - set(p["states"]))
    if not outside:
        return ok("All operating jurisdictions are available states.", ev)
    scope = s["assumptions"]["non_listed_states_scope"]
    msg = (f"IFTA returns show miles in {len(states)} jurisdictions; {len(outside)} are not listed states: "
           f"{', '.join(outside)}. Setting non_listed_states_scope = {scope}.")
    if scope == "operations":
        return Check("fail", msg, ev, on_fail_override="decline")
    return Check("info", msg + " Reported for the underwriter; not prohibited under the domicile reading.", ev)


def mexico_border(fx: Facts, p: dict, s: dict) -> Check:
    states, ev = fx.operating_states()
    near = sorted(states & set(p["border_states"]))
    cb = fx.obs(fx.applicant, "cross_border")
    if any(o.value is True for o in cb):
        return fail("Application states cross-border operations.", cb)
    if near:
        return Check("info", f"Operates in border states {', '.join(near)}; application answers "
                     f"'cross-border operations' = No. Distance from the border cannot be verified from "
                     f"the documents; confirm with the agent.", cb + ev[:1])
    return ok("No operations in border states.", ev[:1])


# ---------------------------------------------------------------- submission requirements

def fact_present(fx: Facts, p: dict, s: dict) -> Check:
    o = fx.obs(fx.applicant, p["field"])
    if o:
        return ok(f"{p['label']}: {o[0].value}.", o[:1])
    return fail(f"{p['label']} is not stated on the application.")


def application_signed(fx: Facts, p: dict, s: dict) -> Check:
    o = fx.obs(fx.applicant, "signed")
    if not o:
        return unknown("Could not tell whether the application is signed.")
    return ok("Application is signed.", o) if o[0].value is True else fail(
        "Application signature and date are blank.", o)


def loss_runs_current(fx: Facts, p: dict, s: dict) -> Check:
    rows, ev, old = [], [], []
    for t in fx.terms:
        for o in fx.obs(t, "run_date"):
            d = to_date(o.value)
            if d:
                age = (fx.anchor - d).days
                rows.append(age)
                ev.append(o)
                if age > p["max_age_days"]:
                    old.append(f"{o.source} ({age} days)")
    if not rows:
        return unknown("No loss run valuation dates found.")
    if old:
        return fail(f"Loss runs older than {p['max_age_days']} days at the {fx.anchor_name}: " + "; ".join(old), ev)
    return ok(f"{len(rows)} loss runs valued {min(rows)}-{max(rows)} days before the {fx.anchor_name} "
              f"({fx.anchor}).", ev)


def loss_run_years(fx: Facts, p: dict, s: dict) -> Check:
    terms = sorted(fx.terms, key=lambda t: str(fx.value(t, "period_start")))
    starts = [(t, to_date(fx.value(t, "period_start"))) for t in terms]
    expiring = next((t for t, st in reversed(starts) if st and st <= fx.anchor), None)
    if not expiring:
        return unknown("Could not identify the expiring policy term.")
    exp_start = to_date(fx.value(expiring, "period_start"))
    missing, have = [], []
    for k in range(0, p["prior_years"] + 1):
        target = exp_start.replace(year=exp_start.year - k)
        t = next((t for t, st in starts if st and abs((st - target).days) <= 365 // 2), None)
        label = "expiring" if k == 0 else f"prior year {k}"
        if t and fx.obs(t, "loss_run_auto_liability"):
            have.append(f"{label}: {t.label.replace('Policy term ', '')}")
        else:
            missing.append(f"{label} (term starting about {target})" + (f": {t.label}" if t else ""))
    ev = [o for t in fx.terms for o in fx.obs(t, "loss_run_auto_liability")]
    if missing:
        return fail("Auto liability loss runs missing for " + "; ".join(missing)
                    + ". Present: " + "; ".join(have) + ".", ev)
    return ok("Loss runs cover the expiring term and 3 prior years.", ev)


def ifta_quarters(fx: Facts, p: dict, s: dict) -> Check:
    ends = sorted({to_date(fx.value(q, "period_end")) for q in fx.quarters} - {None})
    if len(ends) < p["quarters"]:
        return fail(f"Only {len(ends)} IFTA quarters provided; {p['quarters']} required.")
    last = ends[-p["quarters"]:]
    gaps = [(b - a).days for a, b in zip(last, last[1:])]
    if any(g > 100 for g in gaps):
        return fail("IFTA quarters are not consecutive: " + ", ".join(map(str, last)))
    note = ""
    due_next = last[-1] + timedelta(days=92 + 31)      # next quarter end + 1 month filing window
    if fx.anchor > due_next:
        return fail(f"Most recent IFTA quarter ends {last[-1]}; a later quarter was due before the "
                    f"{fx.anchor_name} ({fx.anchor}).")
    if fx.anchor > last[-1] + timedelta(days=92):
        note = (f" The next quarter (ending {last[-1] + timedelta(days=92):%Y-%m-%d}) had ended but was not yet "
                f"due for filing at the {fx.anchor_name}.")
    ev = [o for q in fx.quarters for o in fx.obs(q, "period_end")]
    return ok(f"{len(last)} consecutive quarters, {last[0]} to {last[-1]}." + note, ev)


def mvrs_current(fx: Facts, p: dict, s: dict) -> Check:
    bad, ev = [], []
    for d in fx.scheduled_drivers:
        mvr = fx.current_mvr(d)
        if not mvr:
            bad.append(f"{_label(d)}: no MVR")
            continue
        o = fx.mvr_obs(d, "mvr_order_date")
        ev += o
        age = (fx.anchor - to_date(o[0].value)).days if o and to_date(o[0].value) else None
        if age is None or age > p["max_age_days"]:
            bad.append(f"{_label(d)}: MVR {age} days old")
    if bad:
        return fail("; ".join(bad), ev)
    return ok(f"All {len(fx.scheduled_drivers)} scheduled drivers have an MVR within {p['max_age_days']} days.", ev)


def vehicle_schedule_complete(fx: Facts, p: dict, s: dict) -> Check:
    if not fx.scheduled_vehicles:
        return fail("No vehicle schedule provided.")
    bad, ev = [], []
    for v in fx.scheduled_vehicles:
        sched = lambda f: [o for o in fx.obs(v, f) if o.source.startswith("Vehicle schedule")]
        for f in ("year", "make", "value"):
            if not sched(f):
                bad.append(f"{_label(v)}: no {f}")
        for o in sched("vin"):
            ev.append(o)
            vin = normalize_vin(str(o.value))
            if len(vin) != 17:
                bad.append(f"{_label(v)}: VIN {vin} has {len(vin)} characters")
    if bad:
        return fail("; ".join(bad), ev)
    return ok(f"All {len(fx.scheduled_vehicles)} units have year, make, a 17-character VIN and a value "
              "(value given as 'Cost New').", ev)


# ---------------------------------------------------------------- safe-driver criteria

def drivers_have_cdl(fx: Facts, p: dict, s: dict) -> Check:
    bad, unk, ev = [], [], []
    for d in fx.scheduled_drivers:
        types = fx.mvr_obs(d, "license_type")
        status = fx.mvr_obs(d, "license_status") if "license_status" in d.fields else []
        ev += types
        if not types:
            unk.append(_label(d))
            continue
        t = str(types[0].value).upper()
        if "NON" in t or "COMMERCIAL" not in t:
            cls = fx.mvr_obs(d, "license_class")
            bad.append(f"{_label(d)}: {t.title()} license" + (f", class {cls[0].value}" if cls else ""))
    if bad:
        return fail("No CDL: " + "; ".join(bad) + ".", ev)
    if unk:
        return unknown("License type not shown for: " + ", ".join(unk), ev)
    return ok(f"All {len(fx.scheduled_drivers)} scheduled drivers hold a commercial license.", ev)


def cdl_experience(fx: Facts, p: dict, s: dict) -> Check:
    short, unk, lines, ev = [], [], [], []
    for d in fx.scheduled_drivers:
        types = [str(o.value).upper() for o in fx.mvr_obs(d, "license_type")]
        if types and ("NON" in types[0] or "COMMERCIAL" not in types[0]):
            continue                                   # no CDL at all: covered by D1
        dates = fx.obs(d, "cdl_first_issued") + fx.obs(d, "license_issue_date")
        dates = [o for o in dates if to_date(o.value)]
        if not dates:
            stated = fx.obs(d, "experience")
            unk.append(f"{_label(d)} (no CDL issue date on MVR" +
                       (f"; schedule states '{stated[0].value}'" if stated else "") + ")")
            continue
        first = min(dates, key=lambda o: to_date(o.value))
        ev.append(first)
        yrs = _years(to_date(first.value), fx.anchor)
        lines.append(f"{_label(d)} {yrs:.1f} yrs (since {first.value}, {first.source})")
        if yrs < p["min_years"]:
            short.append(lines[-1])
    if short:
        return fail(f"Under {p['min_years']} years of CDL experience: " + "; ".join(short), ev)
    if unk:
        return unknown("CDL experience cannot be verified for " + "; ".join(unk) + ". Verified: "
                       + "; ".join(lines), ev)
    return ok("; ".join(lines), ev)


def driver_ages(fx: Facts, p: dict, s: dict) -> Check:
    young, old, unk, ev = [], [], [], []
    for d in fx.scheduled_drivers:
        dobs = [o for o in fx.obs(d, "dob") if to_date(o.value)]
        if not dobs:
            unk.append(_label(d))
            continue
        ev += dobs
        ages = [_years(to_date(o.value), fx.anchor) for o in dobs]
        if min(ages) < p["min_age"]:
            young.append(f"{_label(d)} ({min(ages):.0f})")
        if max(ages) >= p["medical_age"]:
            old.append(f"{_label(d)} ({max(ages):.0f}, needs DOT medical report)")
    if young or old:
        parts = ([f"Under {p['min_age']}: " + ", ".join(young)] if young else []) + old
        return fail("; ".join(parts), ev)
    if unk:
        return unknown("No date of birth for " + ", ".join(unk), ev)
    return ok(f"All scheduled drivers are between {p['min_age']} and {p['medical_age'] - 1}.", ev)


# ---------------------------------------------------------------- decline / escalation flags

def _violations(fx: Facts, d):
    mvr = fx.current_mvr(d)
    return [] if not mvr else [(r, mvr) for r in mvr.records.get("violations", [])]


def driver_points(fx: Facts, p: dict, s: dict) -> Check:
    bad, lines = [], []
    for d in fx.scheduled_drivers:
        p3 = p12 = 0
        for r, _ in _violations(fx, d):
            dt, pts = to_date(r["date"].value), r["points"].value or 0
            if not dt:
                continue
            if dt >= fx.anchor - timedelta(days=3 * 365):
                p3 += int(pts)
            if dt >= fx.anchor - timedelta(days=365):
                p12 += int(pts)
        lines.append(f"{_label(d)}: {p3} in 3 yrs, {p12} in 12 mo")
        if p3 > p["max_points_3y"] or p12 > p["max_points_12m"]:
            bad.append(lines[-1])
    if bad:
        return fail("Over the point thresholds: " + "; ".join(bad))
    return ok("; ".join(lines) + ".")


def unacceptable_violations(fx: Facts, p: dict, s: dict) -> Check:
    hits, ev = [], []
    for d in fx.scheduled_drivers:
        for r, mvr in _violations(fx, d):
            desc = str(r["description"].value or "").upper()
            mph = [int(x) for x in re.findall(r"(\d+)\s*(?:\+|-\s*\d+)?\s*MPH OVER", desc)]
            over = re.findall(r"(\d+)-(\d+) MPH OVER", desc)
            top = max([int(b) for _, b in over] + mph, default=0)
            if any(k in desc for k in p["keywords"]) or top >= p["speeding_over_mph"]:
                hits.append(f"{_label(d)}: {desc} ({r['date'].value})")
                ev.append(Observation(value=desc, doc_id=mvr.doc_id, source=mvr.doc_id,
                                      unit_id=r["description"].unit_id, quote=r["description"].quote))
    if hits:
        return fail("Unacceptable violations: " + "; ".join(hits), ev)
    return ok("No DUI/DWI, reckless or negligent driving, 30+ mph speeding, hit-and-run, fleeing, "
              "vehicular felony or school-bus violations on current MVRs.")


def prohibited_exposures(fx: Facts, p: dict, s: dict) -> Check:
    hits, ev = [], []
    for field_name, label in (("hauls_hazmat", "hazardous materials"), ("intermodal", "intermodal containers")):
        for o in fx.obs(fx.applicant, field_name):
            ev.append(o)
            if o.value is True:
                hits.append(f"{label} ({o.source})")
    for name, o in fx.commodities():
        if any(k in name.upper() for k in p["keywords"]):
            hits.append(f"commodity '{name}' ({o.source})")
            ev.append(o)
    if hits:
        return fail("Prohibited exposure: " + "; ".join(hits), ev)
    return ok("No hazmat, lithium batteries, waste, towing/recovery or intermodal exposure stated.", ev)


def selective_exposures(fx: Facts, p: dict, s: dict) -> Check:
    hits = [(n, o) for n, o in fx.commodities() if any(k in n.upper() for k in p["keywords"])]
    hits += [(str(o.value), o) for v in fx.scheduled_vehicles for o in fx.obs(v, "model")
             if any(k in str(o.value).upper() for k in p["keywords"])]
    if hits:
        return Check("info", "Selective exposure present: " + ", ".join(n for n, _ in hits)
                     + ". Check minimum premium / unit-count conditions.", [o for _, o in hits])
    return ok("No auto haulers, box trucks or transit vans. (Premium-per-unit condition is checked at quoting.)")


def prior_insurance(fx: Facts, p: dict, s: dict) -> Check:
    rows = []
    for t in fx.terms:
        listed = [o for o in fx.obs(t, "period_start") if o.source.startswith("Quote form")]
        ends = [o for o in fx.obs(t, "period_end") if o.source.startswith("Quote form")]
        if listed and ends:
            rows.append((to_date(listed[0].value), to_date(ends[0].value), t))
    rows = sorted(r for r in rows if r[0] and r[1])
    if not rows:
        return unknown("No prior policy history listed.")
    lapses = []
    for (s1, e1, t1), (s2, e2, t2) in zip(rows, rows[1:]):
        gap = (s2 - e1).days
        if gap > p["max_lapse_days"]:
            c1 = fx.value(t1, "carrier", prefer="Quote form")
            c2 = fx.value(t2, "carrier", prefer="Quote form")
            lapses.append(f"{gap}-day lapse from {e1} ({c1}) to {s2} ({c2})")
    # Start of the most recent unbroken run of coverage (no gap over the lapse limit).
    continuous_since = rows[-1][0]
    for (s1, e1, _), (s2, _, _) in zip(reversed(rows[:-1]), reversed(rows[1:])):
        if (s2 - e1).days > p["max_lapse_days"]:
            break
        continuous_since = s1
    window_start = fx.anchor.replace(year=fx.anchor.year - p["years"])
    short = continuous_since > window_start
    ev = [o for t in fx.terms for o in fx.obs(t, "period_start") if o.source.startswith("Quote form")]
    if lapses or short:
        parts = lapses + ([f"continuous coverage only since {continuous_since}, short of {p['years']} "
                           f"years before the {fx.anchor_name} ({window_start})"] if short else [])
        msg = "; ".join(parts)
        return fail(msg[0].upper() + msg[1:] + ". Must be explained before quoting.", ev)
    return ok(f"Continuous coverage for {p['years']}+ years with no lapse over {p['max_lapse_days']} days.", ev)


def losses_for_every_term(fx: Facts, p: dict, s: dict) -> Check:
    missing = [t for t in fx.terms if fx.obs(t, "listed_on_application")
               and not fx.obs(t, "loss_run_auto_liability")]
    if missing:
        fids = [f.finding_id for f in fx.findings if f.entity_id in {t.entity_id for t in missing}
                and f.kind == FindingKind.GAP]
        return fail("No loss run for listed term(s): " + "; ".join(t.label for t in missing), fids=fids)
    return ok("Every listed prior term has a loss run.")


# ---------------------------------------------------------------- routing and referral

def open_conflicts(fx: Facts, p: dict, s: dict) -> Check:
    open_ = [f for f in fx.open_findings(FindingKind.CONFLICT) if f.severity in p["severities"]]
    resolved = [f for f in fx.findings if f.status == "resolved"]
    if open_:
        return fail(f"{len(open_)} unresolved conflicts: " + "; ".join(
            f"{f.finding_id} {f.title}" + (" (agent confirmed)" if f.status == "confirmed" else "")
            for f in open_) + (f". {len(resolved)} resolved by the agent recheck not counted." if resolved else ""),
            fids=[f.finding_id for f in open_])
    return ok("No unresolved high or medium conflicts.")


def unreported_exposures(fx: Facts, p: dict, s: dict) -> Check:
    gaps = [f for f in fx.open_findings(FindingKind.GAP) if "loss history" in f.title]
    if gaps:
        return fail("; ".join(f"{f.finding_id} {f.title}" for f in gaps), fids=[f.finding_id for f in gaps])
    return ok("Every driver and vehicle in the loss history is on the schedules.")


CHECKS = {name: fn for name, fn in globals().items() if callable(fn) and fn.__module__ == __name__
          and name not in ("ok", "fail", "unknown", "Check", "field", "dataclass", "timedelta",
                           "normalize_vin", "to_date") and not name.startswith("_")}
