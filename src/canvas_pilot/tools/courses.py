"""Course tools: list, hide and unhide."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field


from ..server import LOCAL_SETTING, client, tool
from .common import CourseArg


@tool("List courses")
async def canvas_list_courses(
    include_grades: Annotated[bool, Field(description="Include the current course score/grade.")] = True,
) -> str:
    """List the student's active Canvas courses with IDs, term, and (optionally) current grade.
    Courses the student has hidden are listed separately at the end."""
    c = client()
    courses = await c.all_courses()
    visible = [x for x in courses if not c.is_hidden(x["id"])]
    if not courses:
        return "No active course enrollments found."
    lines = [f"{len(visible)} active courses:"]
    for x in visible:
        line = f"- **{x['name']}** — code {x.get('course_code', '?')}, course_id={x['id']}"
        if term := (x.get("term") or {}).get("name"):
            line += f", term {term}"
        if include_grades:
            enr = next((e for e in x.get("enrollments") or [] if e.get("type") == "student"), None)
            if enr and enr.get("computed_current_score") is not None:
                grade = enr.get("computed_current_grade")
                line += f" — current score {enr['computed_current_score']}%" + (f" ({grade})" if grade else "")
        lines.append(line)
    hidden = c.hidden.items()
    if hidden:
        active = {x["id"] for x in courses}
        lines.append(f"\n**Hidden ({len(hidden)})** — left out of summaries; still available by name:")
        for cid, name in sorted(hidden.items(), key=lambda kv: kv[1] or ""):
            note = "" if cid in active else " (no longer active)"
            lines.append(f"- {name or 'Unknown course'} — course_id={cid}{note}")
        lines.append("_To bring one back, unhide it (canvas_unhide_course)._")
    return "\n".join(lines)


@tool("Hide a course", LOCAL_SETTING)
async def canvas_hide_course(
    course: CourseArg,
) -> str:
    """Hide a course from course-wide summaries: to-do list, deadlines, announcements, calendar,
    grade overview, and the overview/today/week/news/due shortcuts.

    Use for courses the student doesn't need day to day (orientation, advising, clubs). The course
    stays fully available when asked about by name. Only changes a local settings file, never Canvas.
    """
    c = client()
    crs = await c.resolve_course(course)
    if not c.hidden.hide(crs["id"], crs.get("name") or ""):
        return f"**{crs.get('name')}** is already hidden."
    return (
        f"Hidden **{crs.get('name')}** (course_id={crs['id']}). It's now left out of the to-do list, deadlines, "
        "announcements, calendar and grade summaries. You can still ask about it by name, and unhide it any time."
    )


@tool("Unhide a course", LOCAL_SETTING)
async def canvas_unhide_course(
    course: Annotated[str, Field(min_length=1, description="A hidden course's name, code or ID, or 'all' to unhide everything.")],
) -> str:
    """Bring a hidden course back into course-wide summaries (or every hidden course, with 'all')."""
    c = client()
    hidden = c.hidden.items()
    if course.strip().lower() == "all":
        n = c.hidden.unhide_all()
        return f"Unhid {n} course{'s' if n != 1 else ''}." if n else "No courses were hidden."
    q = course.strip()
    if q.isdigit() and int(q) in hidden:
        hits = [int(q)]
    else:
        qn = q.lower().replace(" ", "")
        hits = [i for i, n in hidden.items() if qn in (n or "").lower().replace(" ", "")]
    if not hits:  # maybe a course code; let the normal lookup decide
        crs = await c.resolve_course(q)
        if crs["id"] not in hidden:
            return f"**{crs.get('name')}** isn't hidden."
        hits = [crs["id"]]
    if len(hits) > 1:
        names = ", ".join(f"{hidden[i] or 'Unknown'} (id {i})" for i in hits)
        raise ToolError(f"{course!r} matches several hidden courses: {names}. Pass the course ID instead.")
    c.hidden.unhide(hits[0])
    return f"Unhid **{hidden[hits[0]] or hits[0]}**. It's back in your to-do list, deadlines, announcements and summaries."
