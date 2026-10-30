"""Assignment tools: the prioritized to-do list, deadlines, listings and details."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field

from ..canvas import CanvasError
from ..priority import (
    SUBMITTED_ELSEWHERE,
    TIER_ORDER,
    WorkItem,
    assignment_kind,
    compute_impacts,
    score_item,
    submission_status,
    tier,
)
from ..server import client, tool
from ..textutil import fence, fmt_day, fmt_dt, html_to_text, parse_dt
from .common import (
    STATUS_LABEL,
    CourseArg,
    IncludeHiddenArg,
    OptionalCourseArg,
    _course_errors,
    _courses,
    _hidden_footer,
    _hidden_tag,
    _item_line,
    _label,
    _now,
    _or_default,
    _tz,
    _work_items,
)


@tool("Get daily to-do list")
async def canvas_get_daily_todo(
    days_ahead: Annotated[int, Field(ge=1, le=30, description="How far ahead to look for due work.")] = 7,
    max_items: Annotated[int, Field(ge=1, le=50)] = 20,
    include_undated: Annotated[bool, Field(description="Also include unsubmitted work with no due date.")] = False,
    include_hidden: IncludeHiddenArg = False,
) -> str:
    """Prioritized to-do list across all courses: unsubmitted work ranked by urgency and grade impact.

    Combines due-date urgency (halves every 36h) with each item's estimated share of the
    final grade (from assignment-group weights or points). Overdue work that Canvas still
    accepts ranks first. Items marked done in the Canvas planner and the student's own
    planner notes are included/excluded accordingly. Each item lists the reasons for its rank.
    """
    now = _now()
    courses = await _courses(None, include_hidden)
    items, results, planner = await _work_items(courses, now, days_ahead)
    horizon = now + timedelta(days=days_ahead)

    todo = [
        i
        for i in items
        if i.status == "overdue"
        or (i.status == "pending" and ((i.due_at and now <= i.due_at <= horizon) or (include_undated and not i.due_at)))
        # Recently past-due work handed in outside Canvas: a gentle "did you submit this?" reminder.
        or (i.status == "unverified" and i.due_at and i.due_at >= now - timedelta(days=3))
    ]
    for p in planner:
        if p.get("plannable_type") != "planner_note" or (p.get("planner_override") or {}).get("marked_complete"):
            continue
        if not include_hidden and client().is_hidden(p.get("course_id")):
            continue
        due = parse_dt(p.get("plannable_date") or (p.get("plannable") or {}).get("todo_date"))
        if due and (due > horizon or due < now - timedelta(days=7)):
            continue
        todo.append(
            WorkItem(
                course_id=p.get("course_id"),
                course_name=p.get("context_name") or "Personal",
                item_id=p.get("plannable_id"),
                name=(p.get("plannable") or {}).get("title", "Planner note"),
                kind="planner_note",
                due_at=due,
                status="note",
            )
        )
    for i in todo:
        score_item(i, now)
    todo.sort(key=lambda i: (-i.score, i.due_at or horizon))
    shown = todo[:max_items]

    header = f"# To-do — {fmt_dt(now, _tz())}\n{len(todo)} open items due in the next {days_ahead} days across {len(courses)} courses"
    if len(todo) > len(shown):
        header += f" (showing top {len(shown)})"
    if not shown:
        out = header + ".\n\nNothing due — you're clear for this window."
    else:
        out = [header + "."]
        n = 0
        for t in TIER_ORDER:
            group = [i for i in shown if tier(i, now) == t]
            if not group:
                continue
            out.append(f"\n## {t}")
            for i in group:
                n += 1
                tag = f" [{i.kind}]" if i.kind in {"quiz", "discussion"} else ""
                out.append(f"{n}. {_item_line(i, now)}{tag}\n   " + " · ".join([f"priority {i.score:.0f}/100", *i.reasons]))
        out = "\n".join(out)

    missed = [i for i in items if i.status == "missed"]
    if missed:
        out += f"\n\n_{len(missed)} past assignment(s) were missed and are now closed — see canvas_get_grades._"
    return out + _course_errors(results) + await _hidden_footer(None, include_hidden)


@tool("Get upcoming deadlines")
async def canvas_get_upcoming_deadlines(
    days_ahead: Annotated[int, Field(ge=1, le=120)] = 14,
    course: OptionalCourseArg = None,
    include_completed: Annotated[bool, Field(description="Also list work already submitted/graded.")] = False,
    include_hidden: IncludeHiddenArg = False,
) -> str:
    """Chronological list of assignment/quiz/discussion due dates, grouped by day, plus anything overdue."""
    now = _now()
    courses = await _courses(course, include_hidden)
    items, results, _ = await _work_items(courses, now, days_ahead)
    horizon = now + timedelta(days=days_ahead)
    upcoming = sorted(
        (i for i in items if i.due_at and now <= i.due_at <= horizon and (include_completed or not i.is_done)),
        key=lambda i: i.due_at,
    )
    overdue = [i for i in items if i.status == "overdue"]

    out = [f"# Deadlines — next {days_ahead} days" + (f" in {_label(courses[0])}{_hidden_tag(courses[0])}" if course else "")]
    if overdue:
        out.append("\n## Overdue (still accepting submissions)")
        out += [f"- {_item_line(i, now, show_course=not course)}" for i in sorted(overdue, key=lambda i: i.due_at)]
    day = None
    for i in upcoming:
        d = fmt_day(i.due_at.astimezone(_tz()), long=True)
        if d != day:
            out.append(f"\n## {d}")
            day = d
        out.append(f"- {_item_line(i, now, show_course=not course)}")
    if not upcoming and not overdue:
        out.append("\nNothing due in this window.")
    return "\n".join(out) + _course_errors(results) + await _hidden_footer(course, include_hidden)


@tool("List assignments in a course")
async def canvas_list_assignments(
    course: CourseArg,
    status: Annotated[
        Literal["upcoming", "unsubmitted", "overdue", "submitted", "graded", "past", "all"],
        Field(description="upcoming = due in the future; unsubmitted = open work not yet turned in; past = due date passed."),
    ] = "upcoming",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> str:
    """List assignments in one course filtered by status, with due dates, points, and submission state."""
    now = _now()
    courses = await _courses(course)
    items, results, _ = await _work_items(courses, now)
    if isinstance(results[0][1], CanvasError):
        raise results[0][1]
    filters = {
        "upcoming": lambda i: i.due_at and i.due_at >= now,
        "unsubmitted": lambda i: i.status in {"pending", "overdue", "unverified"},
        "overdue": lambda i: i.status in {"overdue", "missed", "unverified"},
        "submitted": lambda i: i.status in {"submitted", "graded"},
        "graded": lambda i: i.status == "graded",
        "past": lambda i: i.due_at and i.due_at < now,
        "all": lambda i: True,
    }
    sel = [i for i in items if filters[status](i)]
    far = now + timedelta(days=3650)
    sel.sort(key=lambda i: i.due_at or far, reverse=status in {"past", "graded"})
    if not sel:
        return f"No {status} assignments in {_label(courses[0])}."
    lines = [f"{len(sel)} {status} assignments in **{courses[0]['name']}**{_hidden_tag(courses[0])} (course_id={courses[0]['id']}):"]
    lines += [f"- {_item_line(i, now, show_course=False)}" for i in sel[:limit]]
    if len(sel) > limit:
        lines.append(f"… {len(sel) - limit} more (raise limit to see them)")
    return "\n".join(lines)


@tool("Get assignment details")
async def canvas_get_assignment(
    course: CourseArg,
    assignment_id: Annotated[int, Field(description="assignment_id from a listing tool.")],
    max_description_chars: Annotated[int, Field(ge=500, le=50000)] = 8000,
) -> str:
    """Full details of one assignment: instructions, dates, rubric, grade weight, and your submission/feedback."""
    c = client()
    crs = await c.resolve_course(course)
    cid = crs["id"]
    a, sub, groups, all_a = await asyncio.gather(
        c.get(f"/courses/{cid}/assignments/{assignment_id}", {"include[]": ["submission"]}),
        _or_default(
            c.get(f"/courses/{cid}/assignments/{assignment_id}/submissions/self", {"include[]": ["submission_comments", "rubric_assessment"]}),
            None,
        ),
        # For the grade-weight lines only; the details still show without them.
        _or_default(c.assignment_groups(cid), None),
        _or_default(c.assignments(cid), None),
    )
    sub = sub or a.get("submission") or {}
    now = _now()
    tz = _tz()

    out = [f"# {a['name']}", f"{crs['name']}{_hidden_tag(crs)} · course_id={cid} · assignment_id={a['id']}", a.get("html_url", "")]
    facts = [
        ("Due", fmt_dt(a.get("due_at"), tz, now)),
        ("Available from", fmt_dt(a.get("unlock_at"), tz) if a.get("unlock_at") else None),
        ("Closes", fmt_dt(a.get("lock_at"), tz, now) if a.get("lock_at") else None),
        ("Points", f"{a['points_possible']:g}" if a.get("points_possible") is not None else None),
        ("Type", assignment_kind(a)),
        ("Submit via", ", ".join(a.get("submission_types") or []) or None),
        ("Allowed file types", ", ".join(a.get("allowed_extensions") or []) or None),
        ("Attempts allowed", None if a.get("allowed_attempts") in (None, -1) else str(a["allowed_attempts"])),
        ("Group assignment", "yes" if a.get("group_category_id") else None),
    ]
    if groups is not None and all_a is not None:
        impact, gname, gweight = compute_impacts(all_a, groups, bool(crs.get("apply_assignment_group_weights"))).get(
            a["id"], (None, None, None)
        )
        if gname:
            facts.append(("Assignment group", gname + (f" ({gweight:g}% of grade)" if gweight else "")))
        if impact is not None:
            facts.append(("Est. share of final grade", f"~{impact:.1f}%"))
    out.append("\n".join(f"- **{k}:** {v}" for k, v in facts if v))

    status = submission_status({**a, "submission": sub}, now)
    label = STATUS_LABEL.get(status, status)
    if status in {"pending", "unverified"} and set(a.get("submission_types") or []) <= SUBMITTED_ELSEWHERE:
        label = "unknown: handed in outside Canvas (Gradescope, in class, ...), so Canvas can't see it"
    s = [f"\n## Your submission\n- **Status:** {label}"]
    if sub.get("submitted_at"):
        s.append(f"- **Submitted:** {fmt_dt(sub['submitted_at'], tz)}" + (" (late)" if sub.get("late") else ""))
    if sub.get("attempt"):
        s.append(f"- **Attempt:** {sub['attempt']}")
    if sub.get("score") is not None:
        s.append(f"- **Score:** {sub['score']:g}/{a.get('points_possible') or 0:g}" + (f" ({sub['grade']})" if sub.get("grade") else ""))
    for cm in sub.get("submission_comments") or []:
        author = cm.get("author_name") or "?"
        s.append(f"- **Comment from {author}** ({fmt_dt(cm.get('created_at'), tz)}):\n" + fence(cm.get("comment"), f"comment by {author}"))
    out += s

    if rubric := a.get("rubric"):
        assessed = sub.get("rubric_assessment") or {}
        out.append("\n## Rubric")
        for crit in rubric:
            line = f"- **{crit.get('description') or ''}** ({crit.get('points') or 0:g} pts)"
            if crit.get("long_description"):
                line += "\n" + fence(html_to_text(crit["long_description"], 400), "rubric criterion")
            got = assessed.get(crit.get("id"), {})
            if got.get("points") is not None:
                line += f"\n  You got {got['points']:g}."
                if got.get("comments"):
                    line += "\n" + fence(got["comments"], "grader's rubric comment")
            out.append(line)

    desc = html_to_text(a.get("description"), max_description_chars)
    out.append("\n## Instructions\n" + (fence(desc, "assignment instructions") or "_No description provided in Canvas._"))
    return "\n".join(x for x in out if x is not None)
