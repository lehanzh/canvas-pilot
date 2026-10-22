"""Work-item model and the priority scoring behind the daily to-do list.

score = URGENCY_WEIGHT * urgency + IMPACT_WEIGHT * impact      (each 0-100)

urgency  100 when overdue-but-submittable, otherwise halves every
         URGENCY_HALF_LIFE_HOURS until the due date (due in 36h -> 50).
impact   share of the final grade, compressed with a square root so a 15%
         exam scores 100, a 5% project ~58 and a 1% quiz ~26. Items whose
         weight is unknown (0 points, no groups) get UNKNOWN_IMPACT.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .textutil import parse_dt

URGENCY_WEIGHT = 0.6
IMPACT_WEIGHT = 0.4
URGENCY_HALF_LIFE_HOURS = 36
IMPACT_FULL_SCALE_PCT = 15.0
UNKNOWN_IMPACT = 15.0

DONE_STATES = {"submitted", "graded", "excused", "marked_done"}
NO_ONLINE_SUBMISSION = {"none", "on_paper", "not_graded"}
# Work Canvas usually can't see being submitted: Gradescope and other LTI tools, paper, in class.
SUBMITTED_ELSEWHERE = NO_ONLINE_SUBMISSION | {"external_tool"}


@dataclass
class WorkItem:
    course_id: int | None
    course_name: str
    item_id: int
    name: str
    kind: str  # assignment | quiz | discussion | planner_note | calendar_deadline
    due_at: datetime | None
    lock_at: datetime | None = None
    points: float | None = None
    group_name: str | None = None
    group_weight: float | None = None
    impact_pct: float | None = None
    # pending | overdue | missed | unverified | submitted | graded | excused | marked_done | note | past_event
    status: str = "pending"
    submission_types: list[str] = field(default_factory=list)
    html_url: str | None = None
    # (plannable_type, id) pairs a Canvas planner "done" mark may be filed under.
    planner_keys: list[tuple[str, int]] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_done(self) -> bool:
        return self.status in DONE_STATES

    @property
    def online(self) -> bool:
        return not set(self.submission_types) <= SUBMITTED_ELSEWHERE if self.submission_types else True


def assignment_kind(a: dict) -> str:
    if a.get("quiz_id") or "online_quiz" in (a.get("submission_types") or []):
        return "quiz"
    if a.get("discussion_topic") or "discussion_topic" in (a.get("submission_types") or []):
        return "discussion"
    return "assignment"


def submission_status(a: dict, now: datetime) -> str:
    sub = a.get("submission") or {}
    if sub.get("excused"):
        return "excused"
    state = sub.get("workflow_state")
    if state == "graded" and (sub.get("score") is not None or sub.get("grade")):
        return "graded"
    if sub.get("submitted_at") or state in {"submitted", "pending_review"}:
        return "submitted"
    due = parse_dt(a.get("due_at"))
    if due and due < now:
        types = set(a.get("submission_types") or [])
        if types and types <= SUBMITTED_ELSEWHERE:
            # Gradescope / paper / in class: Canvas can't tell whether it was turned in.
            return "unverified"
        lock = parse_dt(a.get("lock_at"))
        return "missed" if lock and lock < now else "overdue"
    return "pending"


def compute_impacts(assignments: list[dict], groups: list[dict], weighted: bool) -> dict[int, tuple[float | None, str | None, float | None]]:
    """Map assignment id -> (percent of final grade, group name, group weight)."""
    group_by_id = {g["id"]: g for g in groups}
    counted = [a for a in assignments if (a.get("points_possible") or 0) > 0 and not a.get("omit_from_final_grade")]
    group_points: dict[int, float] = {}
    for a in counted:
        gid = a.get("assignment_group_id")
        group_points[gid] = group_points.get(gid, 0.0) + a["points_possible"]
    # Share of the *final* grade: divide by every group's weight, including groups (e.g. exams)
    # with nothing posted yet. Canvas's current score renormalises instead, which overstates early work.
    total_weight = sum(g.get("group_weight") or 0 for g in groups)
    total_points = sum(group_points.values())

    out: dict[int, tuple[float | None, str | None, float | None]] = {}
    for a in assignments:
        g = group_by_id.get(a.get("assignment_group_id")) or {}
        gname, gweight = g.get("name"), g.get("group_weight")
        pts = a.get("points_possible") or 0
        impact: float | None = None
        if pts > 0 and not a.get("omit_from_final_grade"):
            gid = a.get("assignment_group_id")
            if weighted and total_weight and gweight:
                impact = 100.0 * (gweight / total_weight) * pts / group_points[gid]
            elif not weighted and total_points:
                impact = 100.0 * pts / total_points
        out[a["id"]] = (impact, gname, gweight if weighted else None)
    return out


def score_item(item: WorkItem, now: datetime) -> WorkItem:
    reasons: list[str] = []
    if item.status == "overdue":
        urgency = 100.0
        reasons.append("OVERDUE — still accepting submissions")
    elif item.status == "unverified":
        urgency = 10.0
        reasons.append("past due; Canvas can't see outside submissions — mark done if you turned it in")
    elif item.due_at is None:
        urgency = 5.0
        reasons.append("no due date")
    else:
        hours = max((item.due_at - now).total_seconds() / 3600, 0)
        urgency = 100.0 * 0.5 ** (hours / URGENCY_HALF_LIFE_HOURS)

    if item.impact_pct is None:
        impact = UNKNOWN_IMPACT
    else:
        impact = min(100.0, math.sqrt(item.impact_pct / IMPACT_FULL_SCALE_PCT) * 100.0)
        reasons.append(f"~{item.impact_pct:.1f}% of final grade")
    if item.group_name:
        reasons.append(f"{item.group_name}" + (f" ({item.group_weight:g}% group)" if item.group_weight else ""))
    elif item.points:
        reasons.append(f"{item.points:g} pts")
    if item.kind == "calendar_deadline":
        reasons.append("posted on the course calendar; mark done once submitted")
    elif not item.online and item.kind != "planner_note":
        reasons.append("submitted outside Canvas; mark done once finished")

    item.score = round(URGENCY_WEIGHT * urgency + IMPACT_WEIGHT * impact, 1)
    item.reasons = reasons
    return item


def tier(item: WorkItem, now: datetime) -> str:
    if item.status == "overdue":
        return "Overdue"
    if item.status == "unverified":
        return "Check you submitted"
    hours = (item.due_at - now).total_seconds() / 3600 if item.due_at else math.inf
    if hours <= 24 or item.score >= 60:
        return "Do today"
    if hours <= 72 or item.score >= 35:
        return "Next up"
    return "On the radar"


TIER_ORDER = ["Overdue", "Do today", "Next up", "On the radar", "Check you submitted"]
