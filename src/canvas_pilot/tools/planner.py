"""Calendar and planner tools: events, marking work done, personal notes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..canvas import CanvasClient
from ..server import PLANNER_WRITE, client, tool
from ..textutil import fence, fmt_day, fmt_dt, fmt_time, html_to_text, parse_dt
from .common import (
    IncludeHiddenArg,
    OptionalCourseArg,
    _context_chunks,
    _courses,
    _hidden_footer,
    _hidden_tag,
    _label,
    _now,
    _or_default,
    _tz,
)


@tool("Get calendar events")
async def canvas_get_calendar_events(
    days_ahead: Annotated[int, Field(ge=1, le=120)] = 14,
    course: OptionalCourseArg = None,
    include_hidden: IncludeHiddenArg = False,
) -> str:
    """Non-assignment calendar events (exams, review sessions, office hours, class meetings) from Canvas."""
    c = client()
    now = _now()
    tz = _tz()
    courses = await _courses(course, include_hidden)
    footer = await _hidden_footer(course, include_hidden)
    codes = [f"course_{x['id']}" for x in courses]
    names = {f"course_{x['id']}": _label(x) + _hidden_tag(x) for x in courses}
    window = {"type": "event", "start_date": now.isoformat(), "end_date": (now + timedelta(days=days_ahead)).isoformat()}
    requests = [c.get_all("/calendar_events", {**window, "context_codes[]": chunk}) for chunk in _context_chunks(codes)]
    if not course:
        # Some schools block the personal calendar for API tokens (403); skip it quietly then.
        requests.append(_or_default(c.get_all("/calendar_events", {**window, "context_codes[]": ["user_self"]}), []))
    events = [e for page in await asyncio.gather(*requests) for e in page]
    events.sort(key=lambda e: e.get("start_at") or "")
    if not events:
        return f"No calendar events in the next {days_ahead} days." + footer
    out = [f"{len(events)} events in the next {days_ahead} days:"]
    for e in events:
        line = f"- **{e.get('title')}** — {names.get(e.get('context_code'), e.get('context_name') or 'Personal')} — {fmt_dt(e.get('start_at'), tz, now)}"
        if (end := parse_dt(e.get("end_at"))) and e.get("end_at") != e.get("start_at"):
            line += f" to {fmt_time(end.astimezone(tz))}"
        if e.get("location_name"):
            line += f" · {e['location_name']}"
        if desc := html_to_text(e.get("description"), 300):
            line += "\n" + fence(desc, "event description")
        out.append(line)
    return "\n".join(out) + footer


def _require_writes() -> None:
    if not client().settings.allow_writes:
        raise ToolError("Planner writes are disabled (CANVAS_ALLOW_PLANNER_WRITES=false in .env).")


@tool("Mark item done in Canvas planner", PLANNER_WRITE)
async def canvas_mark_done(
    course: OptionalCourseArg = None,
    assignment_id: Annotated[int | None, Field(description="assignment_id from a listing (requires course).")] = None,
    event_id: Annotated[int | None, Field(description="event_id of a course-calendar deadline, instead of an assignment.")] = None,
    done: Annotated[bool, Field(description="false un-marks it.")] = True,
) -> str:
    """Check an assignment or calendar deadline off (or back on) in the student's own Canvas planner.

    Use it for work submitted outside Canvas (Gradescope, in class) so it leaves the to-do list.
    Only affects the student's personal planner view — it does NOT submit anything to the instructor.
    """
    _require_writes()
    c = client()
    if event_id is not None:
        return await _set_planner_done(c, "calendar_event", event_id, done, f"calendar event {event_id}")
    if assignment_id is None or not course:
        raise ToolError("Pass course + assignment_id for an assignment, or event_id for a calendar deadline.")
    crs = await c.resolve_course(course)
    a = await c.get(f"/courses/{crs['id']}/assignments/{assignment_id}")
    if a.get("quiz_id"):
        ptype, pid = "quiz", a["quiz_id"]
    elif (a.get("discussion_topic") or {}).get("id"):
        ptype, pid = "discussion_topic", a["discussion_topic"]["id"]
    else:
        ptype, pid = "assignment", a["id"]
    return await _set_planner_done(c, ptype, pid, done, a["name"])


async def _set_planner_done(c: CanvasClient, ptype: str, pid: int, done: bool, name: str) -> str:
    overrides = await c.get_all("/planner/overrides", cache=False)
    existing = next((o for o in overrides if o.get("plannable_type") == ptype and o.get("plannable_id") == pid), None)
    if existing:
        await c.put(f"/planner/overrides/{existing['id']}", {"marked_complete": done})
    else:
        await c.post("/planner/overrides", {"plannable_type": ptype, "plannable_id": pid, "marked_complete": done})
    return f"{'Marked' if done else 'Un-marked'} **{name}** as done in your Canvas planner. (Nothing was submitted.)"


@tool("Add a personal planner note", PLANNER_WRITE)
async def canvas_add_planner_note(
    title: Annotated[str, Field(min_length=1, max_length=200)],
    date: Annotated[str, Field(description="Day it's for, YYYY-MM-DD (local time).", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    details: Annotated[str | None, Field(max_length=4000)] = None,
    course: OptionalCourseArg = None,
) -> str:
    """Create a personal to-do note in the student's Canvas planner (visible only to them)."""
    _require_writes()
    c = client()
    try:
        day = datetime.fromisoformat(date).replace(hour=23, minute=59, tzinfo=_tz())
    except ValueError as e:
        raise ToolError(f"{date!r} is not a real calendar date; use YYYY-MM-DD.") from e
    body: dict[str, Any] = {"title": title, "todo_date": day.isoformat()}
    if details:
        body["details"] = details
    if course:
        body["course_id"] = (await c.resolve_course(course))["id"]
    note = await c.post("/planner_notes", body)
    return f"Added planner note **{title}** for {fmt_day(day)} (note id {note.get('id')})."
