"""What the rules need, in one place: dates, scheduled drivers and vehicles, policy terms,
IFTA quarters, MVR records and open findings -- all built from stages 2-4."""
from __future__ import annotations

import re
from datetime import date

from ..reconcile.compare import state_code
from ..schemas import DocType, Entity, Extraction, Finding, FindingKind, Observation, Submission


def to_date(v: object) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


class Facts:
    def __init__(self, sub: Submission, anchor: str = "application_date"):
        rec = sub.reconciliation
        self.sub = sub
        self.entities = rec.entities if rec else []
        self.findings: list[Finding] = rec.findings if rec else []
        self.applicant = next((e for e in self.entities if e.kind == "applicant"),
                              Entity(entity_id="applicant", kind="applicant", label="Applicant"))
        self.extractions = {e.doc_id: e for e in sub.extractions if e.status == "ok"}
        self.superseded_docs = {o.doc_id for f in self.findings if f.kind == FindingKind.SUPERSEDED
                                for o in f.observations[1:]}
        self.anchor_name = anchor
        self.anchor = to_date(self.value(self.applicant, anchor)) or date.today()

    # ---------------------------------------------------------------- generic access
    @staticmethod
    def obs(e: Entity, field: str) -> list[Observation]:
        return e.fields.get(field, [])

    def value(self, e: Entity, field: str, prefer: str = "Application"):
        o = self.obs(e, field)
        if not o:
            return None
        first = next((x for x in o if x.source.startswith(prefer)), o[0])
        return first.value

    def of_kind(self, kind: str) -> list[Entity]:
        return [e for e in self.entities if e.kind == kind]

    def by_type(self, t: DocType) -> list[Extraction]:
        return [x for x in self.extractions.values() if x.doc_type == t]

    @staticmethod
    def roles(e: Entity) -> set:
        return {o.value for o in e.fields.get("appears_in", [])}

    # ---------------------------------------------------------------- domain views
    @property
    def scheduled_drivers(self) -> list[Entity]:
        return [d for d in self.of_kind("driver") if "schedule" in self.roles(d)]

    @property
    def scheduled_vehicles(self) -> list[Entity]:
        return [v for v in self.of_kind("vehicle") if "schedule" in self.roles(v)]

    @property
    def terms(self) -> list[Entity]:
        return self.of_kind("policy_term")

    @property
    def quarters(self) -> list[Entity]:
        return self.of_kind("ifta_quarter")

    def current_mvr(self, driver: Entity) -> Extraction | None:
        """The newest MVR for a driver (superseded copies excluded)."""
        docs = [o.doc_id for o in self.obs(driver, "mvr_order_date") if o.doc_id not in self.superseded_docs]
        mvrs = [self.extractions[d] for d in docs if d in self.extractions]
        return max(mvrs, key=lambda x: str(x.facts["order_date"].value or ""), default=None)

    def mvr_obs(self, driver: Entity, field: str) -> list[Observation]:
        """Observations of a field from the driver's current MVR only."""
        cur = self.current_mvr(driver)
        return [o for o in self.obs(driver, field) if cur and o.doc_id == cur.doc_id]

    def operating_states(self) -> tuple[set[str], list[Observation]]:
        states, evidence = set(), []
        for q in self.quarters:
            for o in self.obs(q, "jurisdictions"):
                evidence.append(o)
                for part in re.split(r"[,;\n]", str(o.value)):
                    code = state_code(part.strip())
                    if len(code) == 2:
                        states.add(code)
        return states, evidence

    def commodities(self) -> list[tuple[str, Observation]]:
        out = []
        for t in (DocType.APPLICATION, DocType.QUOTE_FORM):
            for x in self.by_type(t):
                for row in x.records.get("commodities", []):
                    f = row["commodity"]
                    if f.value:
                        out.append((str(f.value), Observation(value=f.value, doc_id=x.doc_id,
                                                              source=f"{t.value.replace('_', ' ').title()} commodities",
                                                              unit_id=f.unit_id, quote=f.quote,
                                                              confidence=f.confidence)))
        return out

    def open_findings(self, kind: FindingKind | None = None) -> list[Finding]:
        return [f for f in self.findings if f.status != "resolved" and (kind is None or f.kind == kind)]
