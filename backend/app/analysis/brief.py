"""Build the compact, ID-labelled brief the analysis model reads.

The model sees the structured results of stages 1-5 -- never the raw PDFs -- and every
item carries an id it must cite: documents (d01..), findings (F01..), rules (A1..F2),
and claim numbers as printed on the loss runs.
"""
from __future__ import annotations

from ..schemas import DocType, Submission


def _v(e, field):
    o = e.fields.get(field, []) if e else []
    return o[0].value if o else None


def _delay(claim) -> str:
    """Days from loss to report, computed here so the model never does date arithmetic."""
    from ..pipeline.verify import parse_date
    a, b = parse_date(str(claim["date_of_loss"].value or "")), parse_date(str(claim["date_reported"].value or ""))
    return f", {(b - a).days} days after the loss" if a and b else ""


def build_brief(sub: Submission) -> tuple[str, set[str]]:
    """Returns (brief text, the set of ids the model may cite)."""
    ids: set[str] = set()
    lines: list[str] = []
    rec, dec = sub.reconciliation, sub.decision
    ents = rec.entities if rec else []
    applicant = next((e for e in ents if e.kind == "applicant"), None)

    lines.append("## Applicant")
    for label, field in (("Name", "applicant_name"), ("Entity type", "entity_type"), ("DOT", "dot_number"),
                         ("MC", "mc_number"), ("Mailing address", "mailing_address"),
                         ("Years in business", "years_in_business"), ("Application date", "application_date"),
                         ("Effective date", "effective_date"), ("Agency", "agency_name")):
        lines.append(f"- {label}: {_v(applicant, field)}")

    if dec:
        lines += ["", "## Decision (fixed by the rules engine; do not change it)",
                  f"- Outcome: {dec.outcome.upper()}; route: {dec.route} ({dec.route_reason})",
                  "- Rule results that are not a pass:"]
        for r in dec.results:
            ids.add(r.rule_id)
            if r.status != "pass":
                lines.append(f"  - [{r.rule_id}] {r.status.upper()} ({r.effect}) {r.title}: {r.message}")

    if rec:
        lines += ["", "## Findings across documents (status after the agent recheck)"]
        for f in rec.findings:
            ids.add(f.finding_id)
            agent = ""
            if f.review and f.review.verdict != "not_run":
                agent = f" | agent: {f.review.verdict} -- {f.review.explanation[:220]}"
            lines.append(f"- [{f.finding_id}] {f.severity} {f.kind.value}, status {f.status}: {f.title}. "
                         f"{f.detail[:300]}{agent}")

    lines += ["", "## Documents"]
    for d in sub.documents:
        ids.add(d.doc_id)
        lines.append(f"- [{d.doc_id}] {d.doc_type.value}: {d.title}")

    exs = {e.doc_id: e for e in sub.extractions if e.status == "ok"}
    lines += ["", "## Loss history (from loss runs and letters)"]
    for e in exs.values():
        if e.doc_type == DocType.LOSS_RUN:
            f = e.facts
            lines.append(f"- [{e.doc_id}] {f['coverage'].value} policy {f['policy_number'].value}, "
                         f"{f['period_start'].value} to {f['period_end'].value}, valued {f['run_date'].value}: "
                         f"{f['total_claims'].value} claims, paid {f['total_paid'].value}, reserved "
                         f"{f['total_reserved'].value}, incurred {f['total_incurred'].value}")
            for c in e.records.get("claims", []):
                if c["claim_number"].value:          # claim numbers are citable evidence too
                    ids.add(str(c["claim_number"].value))
                lines.append(f"  - claim {c['claim_number'].value}: loss {c['date_of_loss'].value}, reported "
                             f"{c['date_reported'].value}{_delay(c)} (late: {c['late_reported'].value}), driver "
                             f"{c['driver_name'].value}, vehicle {c['vehicle_description'].value} "
                             f"{c['vehicle_vin'].value}, {c['status'].value}, incurred {c['incurred'].value}; "
                             f"{c['unreported_driver_or_unit'].value or ''}")
        elif e.doc_type == DocType.NO_LOSS_LETTER:
            f = e.facts
            lines.append(f"- [{e.doc_id}] letter from {f['issuer'].value}: {f['statement'].value}")

    lines += ["", "## IFTA quarters"]
    for e in exs.values():
        if e.doc_type == DocType.IFTA_RETURN:
            f = e.facts
            lines.append(f"- [{e.doc_id}] {f['period_start'].value} to {f['period_end'].value}: taxpayer "
                         f"{f['taxpayer_name'].value}, {f['total_miles'].value} miles, penalty "
                         f"{f['penalty'].value}, interest {f['interest'].value}, due {f['total_due'].value}")

    lines += ["", "## Drivers and vehicles"]
    for e in ents:
        if e.kind == "driver":
            roles = sorted({str(o.value) for o in e.fields.get("appears_in", [])})
            viol = []
            for o in e.fields.get("mvr_order_date", []):
                x = exs.get(o.doc_id)
                if x:
                    viol += [f"{r['date'].value} {r['description'].value} ({r['points'].value} pts)"
                             for r in x.records.get("violations", [])]
            lines.append(f"- driver {e.label}: appears in {', '.join(roles)}; license type "
                         f"{_v(e, 'license_type')}, class {_v(e, 'license_class')}; violations: "
                         f"{'; '.join(sorted(set(viol))) or 'none'}")
        elif e.kind == "vehicle":
            roles = sorted({str(o.value) for o in e.fields.get("appears_in", [])})
            lines.append(f"- vehicle {e.label}: appears in {', '.join(roles)}")
    return "\n".join(lines), ids
