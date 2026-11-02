"""The underwriter's decisions on findings -- the human in the loop.

A review changes a finding's status, keeps the full history, and re-runs the rules engine,
because rules such as F1 (unresolved conflicts) and F2 (unreported exposures) read those
statuses. The AI analysis is not re-run; it is marked stale so the reader knows it was
written before the review.
"""
from __future__ import annotations

from ..rules.engine import evaluate
from ..schemas import Decision, HumanReview, Submission
from .store import now

# action -> resulting finding status ("clear" restores what the pipeline decided)
ACTIONS = {"confirm": "confirmed", "resolve": "resolved", "clear": None}


class ReviewError(ValueError):
    pass


def apply_review(sub: Submission, finding_id: str, action: str, note: str = "",
                 reviewer: str = "underwriter") -> tuple[Decision, Decision]:
    """Record a review and re-evaluate. Returns (decision before, decision after)."""
    if action not in ACTIONS:
        raise ReviewError(f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
    if not sub.reconciliation:
        raise ReviewError("This submission has no findings to review.")
    f = next((x for x in sub.reconciliation.findings if x.finding_id == finding_id), None)
    if f is None:
        raise ReviewError(f"No finding {finding_id}.")
    if action == "resolve" and not note.strip():
        raise ReviewError("Add a note saying why this finding is resolved.")

    rev = HumanReview(action=action, note=note.strip()[:2000], reviewer=reviewer[:80] or "underwriter",
                      at=now(), previous_status=f.status)
    machine_status = f.history[0].previous_status if f.history else f.status
    f.history.append(rev)
    f.status = ACTIONS[action] or machine_status
    f.human = None if action == "clear" else rev

    before = sub.decision
    sub.decision = evaluate(sub)
    if sub.analysis and sub.analysis.status == "ok":
        sub.analysis.stale = True
    sub.events.append({"step": "review", "at": rev.at, "finding": finding_id, "action": action,
                       "status": f.status, "outcome_before": before.outcome if before else None,
                       "outcome_after": sub.decision.outcome, "reasons_after": sub.decision.reasons})
    return before, sub.decision
