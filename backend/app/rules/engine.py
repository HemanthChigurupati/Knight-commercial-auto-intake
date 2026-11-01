"""Stage 5 -- apply the underwriting guideline (rules/rules.yaml) and decide.

    any failed decline rule            -> DECLINE
    else any failed or unknown refer   -> REFER
    else                               -> PROCEED
    routing: power units on the vehicle schedule (1-10 Small Fleet, 11+ Fleet)

The model never decides: rules are data, checks are plain code, and every result carries
its evidence and the rules-file version it was produced with.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..schemas import Decision, RuleResult, Submission
from .checks import CHECKS
from .facts import Facts

log = logging.getLogger("pipeline")

RULES_FILE = REPO_ROOT / "rules" / "rules.yaml"


def load_rules(path: Path = RULES_FILE) -> tuple[dict, str]:
    text = path.read_text()
    return yaml.safe_load(text), hashlib.sha256(text.encode()).hexdigest()[:12]


def evaluate(sub: Submission, config: dict | None = None, version: str = "") -> Decision:
    if config is None:
        config, version = load_rules()
    settings = config["settings"]
    fx = Facts(sub, settings["assumptions"].get("window_anchor", "application_date"))

    results: list[RuleResult] = []
    for rule in config["rules"]:
        check = CHECKS[rule["check"]]
        on_fail = rule["on_fail"]
        if on_fail == "baseline":
            on_fail = settings["assumptions"]["baseline_failure_outcome"]
        try:
            c = check(fx, rule.get("params") or {}, settings)
        except Exception as e:                       # a broken check must never pass silently
            from .checks import Check
            c = Check("unknown", f"Check could not run: {type(e).__name__}: {e}")
        on_fail = c.on_fail_override or on_fail
        if c.status == "fail":
            effect = on_fail
        elif c.status == "unknown":
            effect = "decline" if on_fail == "decline" else "refer"
        elif c.status == "info":
            effect = "info"
        else:
            effect = "none"
        if effect == "refer" and on_fail == "info":
            effect = "info"
        results.append(RuleResult(rule_id=rule["id"], section=rule["section"], title=rule["title"],
                                  on_fail=on_fail, status=c.status, effect=effect, message=c.message,
                                  evidence=c.evidence, finding_ids=c.finding_ids))

    declines = [r.rule_id for r in results if r.effect == "decline"]
    refers = [r.rule_id for r in results if r.effect == "refer"]
    outcome = "decline" if declines else "refer" if refers else "proceed"

    units = len(fx.scheduled_vehicles)
    other = [o.value for o in fx.obs(fx.applicant, "power_units") if o.value != units]
    max_small = settings["routing"]["small_fleet_max_units"]
    route = "small_fleet" if units <= max_small else "fleet"
    reason = f"{units} power units on the vehicle schedule"
    if other:
        alt = ["small_fleet" if int(v) <= max_small else "fleet" for v in other]
        reason += (f" (other documents say {', '.join(map(str, other))}; "
                   + ("same team either way" if all(a == route for a in alt) else "ROUTING DEPENDS ON THE CONFLICT")
                   + ")")
    return Decision(outcome=outcome, route=route, route_reason=reason, reasons=declines or refers,
                    results=results, assumptions=settings["assumptions"], rules_version=version)
