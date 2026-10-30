"""Grade tools: current grades and the what-if calculator."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..canvas import CanvasError
from ..grades import (
    DropRules,
    GradeItem,
    apply_what_ifs,
    describe_need,
    parse_scheme,
    parse_target,
    parse_what_if,
    project,
)
from ..priority import submission_status
from ..server import client, tool
from ..textutil import fmt_dt, parse_dt
from .common import (
    STATUS_LABEL,
    CourseArg,
    IncludeHiddenArg,
    OptionalCourseArg,
    _course_errors,
    _courses,
    _hidden_footer,
    _hidden_tag,
    _label,
    _now,
    _tz,
    _work_items,
)


@tool("Get grades")
async def canvas_get_grades(
    course: OptionalCourseArg = None,
    recent_days: Annotated[int, Field(ge=1, le=120, description="Window for 'recently graded' items.")] = 14,
    include_hidden: IncludeHiddenArg = False,
) -> str:
    """Current grades. Without a course: each course's score plus recently graded work.
    With a course: per-assignment-group breakdown, every graded item, and missing work."""
    c = client()
    now = _now()
    tz = _tz()
    courses = await _courses(course, include_hidden)
    results = await c.gather_per_course(courses, lambda crs: c.assignments(crs["id"]))
    out: list[str] = []
    recent: list[tuple[datetime, str]] = []
    ungraded: list[str] = []
    for crs, assignments in results:
        if isinstance(assignments, CanvasError):
            continue
        enr = next((e for e in crs.get("enrollments") or [] if e.get("type") == "student"), {})
        score = enr.get("computed_current_score")
        grade = enr.get("computed_current_grade")
        graded = [a for a in assignments if (a.get("submission") or {}).get("score") is not None]
        if not course and score is None and not graded:
            ungraded.append(_label(crs))
            continue
        out.append(
            f"\n## {crs['name']}{_hidden_tag(crs)} (course_id={crs['id']})\nCurrent: "
            + (f"{score}%" + (f" ({grade})" if grade else "") if score is not None else "not available (hidden by instructor?)")
        )
        for a in graded:
            sub = a["submission"]
            g_at = parse_dt(sub.get("graded_at"))
            if g_at and g_at >= now - timedelta(days=recent_days):
                recent.append((g_at, f"- {_label(crs)} · **{a['name']}**: {sub['score']:g}/{a.get('points_possible') or 0:g} (graded {fmt_dt(g_at, tz)})"))
        if course:
            groups = await c.assignment_groups(crs["id"])
            weighted = bool(crs.get("apply_assignment_group_weights"))
            out.append("\n### By category")
            for g in groups:
                ga = [a for a in graded if a.get("assignment_group_id") == g.get("id") and a.get("points_possible")]
                earned = sum(a["submission"]["score"] for a in ga)
                possible = sum(a["points_possible"] for a in ga)
                pct = f"{100 * earned / possible:.1f}%" if possible else "—"
                weight = f" · weight {g.get('group_weight') or 0:g}%" if weighted else ""
                if rules := DropRules.from_group(g):
                    weight += f" · {rules.describe()} (totals below are before drops)"
                out.append(f"- **{g['name']}**{weight}: {earned:g}/{possible:g} ({pct}) over {len(ga)} graded")
            out.append("\n### Graded items")
            for a in sorted(graded, key=lambda a: a.get("due_at") or "", reverse=True):
                sub = a["submission"]
                late = " · late" if sub.get("late") else ""
                out.append(f"- {a['name']}: {sub['score']:g}/{a.get('points_possible') or 0:g}{late}  `assignment_id={a['id']}`")
            missing = [a for a in assignments if submission_status(a, now) in {"overdue", "missed", "unverified"}]
            if missing:
                out.append("\n### Missing or not yet graded")
                out += [
                    f"- {a['name']} (due {fmt_dt(a.get('due_at'), tz)}) — {STATUS_LABEL[submission_status(a, now)]}  `assignment_id={a['id']}`"
                    for a in missing
                ]
    if ungraded:
        out.append(f"\n_No grades yet: {', '.join(ungraded)}_")
    if recent and not course:
        out.append(f"\n## Graded in the last {recent_days} days")
        out += [line for _, line in sorted(recent, reverse=True)]
    return ("# Grades" + "\n".join(out) + _course_errors(results)).strip() + await _hidden_footer(course, include_hidden)


@tool("What-if grade calculator")
async def canvas_what_if_grade(
    course: CourseArg,
    target: Annotated[
        str | None, Field(description="Grade to aim for: a letter from the course scheme ('A-') or a percentage ('90'). Omit to see every letter grade.")
    ] = None,
    what_if: Annotated[
        str | None,
        Field(
            description="Hypothetical scores in percent as name=score pairs separated by semicolons, e.g. "
            "\"Homework-3=85; Midterm Exams=75\". A name is an assignment name (or part of it), an assignment_id, or a "
            "whole category name for everything left in it. Can also override graded work."
        ),
    ] = None,
    unposted_points: Annotated[
        float | None,
        Field(ge=0, description="Unweighted courses only: points of future work the instructor hasn't posted yet (check the syllabus)."),
    ] = None,
) -> str:
    """What average you need on the remaining work in a course to reach a target grade, plus what-if scenarios.

    Uses graded scores, posted-but-ungraded work, deadlines that exist only on the course calendar,
    and weighted categories with nothing posted yet (e.g. a future final exam). Closed work you never
    submitted counts as 0. Shows the needed average for every letter grade, the best/worst possible
    final grade, and how much of the final grade each category still controls.
    """
    c = client()
    now = _now()
    crs = await c.resolve_course(course)
    cid = crs["id"]
    detail, assignments, groups = await asyncio.gather(
        c.get(f"/courses/{cid}", {"include[]": ["grading_scheme", "total_scores"]}), c.assignments(cid), c.assignment_groups(cid)
    )
    scheme = parse_scheme(detail.get("grading_scheme"))
    weighted = bool(detail.get("apply_assignment_group_weights", crs.get("apply_assignment_group_weights")))

    items: list[GradeItem] = []
    for a in assignments:
        sub = a.get("submission") or {}
        pts = a.get("points_possible") or 0
        if not a.get("published", True) or a.get("omit_from_final_grade") or pts <= 0 or sub.get("excused"):
            continue
        score = sub.get("score")
        source = "canvas"
        if score is None and submission_status(a, now) == "missed":
            score, source = 0.0, "missed"
        items.append(GradeItem(a["id"], a.get("name") or "(untitled)", a.get("assignment_group_id"), pts, score, source))

    # Deadlines posted only on the calendar ("Homework-3 due") are future work too.
    group_by_name = {g.get("name"): g.get("id") for g in groups}
    work, _, _ = await _work_items([crs], now, days_ahead=180)
    for w in work:
        if w.kind == "calendar_deadline" and w.status in {"pending", "marked_done"} and w.points and w.group_name in group_by_name:
            items.append(GradeItem(None, w.name, group_by_name[w.group_name], w.points, None, "calendar"))
    if unposted_points:
        items.append(GradeItem(None, "Work not posted yet (your estimate)", None, unposted_points, None, "estimate"))

    actual = project(items, groups, weighted)
    try:
        applied, fixed_empty = apply_what_ifs(parse_what_if(what_if), items, groups, weighted)
    except ValueError as e:
        raise ToolError(str(e)) from e
    proj = project(items, groups, weighted, fixed_empty) if applied else actual

    out = [f"# What-if grades — {crs['name']}{_hidden_tag(crs)}"]
    enr = next((e for e in detail.get("enrollments") or crs.get("enrollments") or [] if e.get("type") == "student"), {})
    canvas_now = enr.get("computed_current_score")
    if actual.current is not None:
        line = f"Current grade on graded work: **{actual.current:.2f}%**"
        if canvas_now is not None and abs(canvas_now - actual.current) > 0.5:
            line += f" (Canvas shows {canvas_now}%; the gap usually means hidden assignments or a late-policy deduction)"
        out.append(line)
    out.append(
        f"Still open: **{proj.slope:.1f}%** of the final grade. Final range: {proj.base:.1f}% (0% on the rest) "
        f"to {proj.ceiling:.1f}% (100% on the rest)."
    )
    if actual.current is not None and proj.slope > 0:
        keep = proj.base + proj.slope * actual.current / 100
        out.append(f"If you keep averaging {actual.current:.1f}% on everything left, you finish around **{keep:.1f}%**.")
    if applied:
        out.append("\n**What-if scores applied:**\n" + "\n".join(applied))

    if target:
        try:
            tname, tval = parse_target(target, scheme)
        except ValueError as e:
            raise ToolError(str(e)) from e
        need = proj.needed(tval)
        label = tname if tname.endswith("%") else f"{tname} ({tval:g}%)"
        out.append(f"\n## To finish with {label}: average **{describe_need(need)}** on the remaining work")

    out.append("\n## Average needed on remaining work, by grade")
    out.append("| Grade | Cutoff | Needed |\n|---|---|---|")
    for name, cutoff in scheme:
        if cutoff <= 0:
            continue
        out.append(f"| {name} | {cutoff:g}% | {describe_need(proj.needed(cutoff))} |")

    out.append("\n## Where the remaining points are")
    for g in sorted(proj.groups, key=lambda g: -g.share_of_final_left):
        if g.share_of_final_left < 0.05:
            continue
        w = f" ({g.weight:g}% weight)" if proj.weighted and g.weight else ""
        if not g.remaining_items and proj.weighted:
            out.append(f"- **{g.name}**{w}: nothing posted yet — {g.share_of_final_left:.1f}% of the final grade")
            continue
        names = ", ".join(i.name + (" (calendar)" if i.source == "calendar" else "") for i in g.remaining_items[:6])
        more = f" +{len(g.remaining_items) - 6} more" if len(g.remaining_items) > 6 else ""
        out.append(f"- **{g.name}**{w}: {g.remaining:g} pts left = {g.share_of_final_left:.1f}% of the final grade — {names}{more}")

    notes = []
    if any(i.source == "missed" for i in items):
        notes.append("closed work you never submitted is counted as 0")
    if proj.drop_notes:
        notes.append("drop rules applied the way Canvas does (" + "; ".join(proj.drop_notes) + ")")
    if not proj.weighted and not unposted_points:
        notes.append(
            "this course grades by points, so only work already posted is counted; pass unposted_points if more is coming"
        )
    notes.append("assumes the same average on every remaining item; extra credit and curves aren't modelled")
    out.append("\n_Notes: " + "; ".join(notes) + "._")
    return "\n".join(out)
