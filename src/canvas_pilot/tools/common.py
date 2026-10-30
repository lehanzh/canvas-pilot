"""Argument types and helpers shared by the tool modules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypeVar

from pydantic import Field

from ..canvas import CanvasError
from ..priority import WorkItem, apply_done_marks, assignment_work_items, calendar_deadlines
from ..server import client
from ..textutil import fmt_dt


CourseArg = Annotated[str, Field(min_length=1, description="Course ID, course code, or part of the course name, e.g. '3500' or 'Algorithms'.")]
OptionalCourseArg = Annotated[
    str | None, Field(description="Limit to one course (ID, code, or name fragment). Omit for all active courses.")
]
IncludeHiddenArg = Annotated[
    bool, Field(description="Also include courses the student has hidden. Only when they ask to include hidden courses.")
]


# -- shared helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _tz():
    return client().settings.timezone


def _label(course: dict) -> str:
    code = course.get("course_code") or ""
    parts = code.split(".")
    if len(parts) >= 3:  # SIS-style "CS3000.13896.202710" -> "CS3000"
        return parts[0]
    return code if code and len(code) <= 25 else (course.get("name") or "?")[:40]


async def _courses(course: str | None, include_hidden: bool = False) -> list[dict]:
    """One named course (hidden or not), or every course for a course-wide view."""
    c = client()
    return [await c.resolve_course(course)] if course and course.strip() else await c.courses(include_hidden)


def _hidden_tag(course: dict) -> str:
    return " (hidden course)" if client().is_hidden(course.get("id")) else ""


async def _hidden_footer(course: str | None, include_hidden: bool) -> str:
    """Course-wide views say what they left out, so a hidden course never silently disappears."""
    if (course and course.strip()) or include_hidden:
        return ""
    c = client()
    hidden = [x for x in await c.all_courses() if c.is_hidden(x["id"])]
    if not hidden:
        return ""
    s = "s" if len(hidden) > 1 else ""
    # Names, not codes: codes like "Khoury.Orientation.NU" shorten to a cryptic "Khoury".
    names = ", ".join(x["name"] if len(x["name"]) <= 30 else x["name"][:29].rstrip() + "…" for x in hidden)
    return f"\n\n_{len(hidden)} hidden course{s} not included ({names}). Ask about one by name, or ask to include hidden courses._"


T = TypeVar("T")


async def _or_default(request: Awaitable[T], default: T) -> T:
    """Optional extra data: a Canvas failure means "none" rather than failing the whole tool."""
    try:
        return await request
    except CanvasError:
        return default


def _context_chunks(codes: list[str]) -> list[list[str]]:
    return [codes[i : i + 10] for i in range(0, len(codes), 10)]  # Canvas caps context_codes[] at 10


def _course_errors(results: list[tuple[dict, Any]]) -> str:
    errs = [f"- {_label(c)}: {r}" for c, r in results if isinstance(r, CanvasError)]
    return ("\n\n_Some courses could not be read:_\n" + "\n".join(errs)) if errs else ""


STATUS_LABEL = {
    "pending": "not submitted",
    "overdue": "OVERDUE (still open)",
    "missed": "missed (closed)",
    "submitted": "submitted",
    "graded": "graded",
    "excused": "excused",
    "marked_done": "marked done",
    "note": "personal note",
    "unverified": "past due — submitted outside Canvas?",
}

async def _work_items(
    courses: list[dict], now: datetime, days_ahead: int = 60
) -> tuple[list[WorkItem], list[tuple[dict, Any]], list[dict]]:
    """Every assignment across the courses as a WorkItem, with grade impact and planner 'done' marks applied,
    plus deadlines that exist only on a course calendar."""
    c = client()

    async def per_course(course: dict) -> list[WorkItem]:
        assignments, groups = await asyncio.gather(c.assignments(course["id"]), c.assignment_groups(course["id"]))
        weighted = bool(course.get("apply_assignment_group_weights"))
        return assignment_work_items(course["id"], _label(course), assignments, groups, weighted, now)

    # Planner items carry calendar-only deadlines and planner notes; the window grows with the request.
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)  # day-aligned so the request is cacheable
    span = max(days_ahead, 60)
    results, planner, done_keys = await asyncio.gather(
        c.gather_per_course(courses, per_course),
        _or_default(c.planner_items((today - timedelta(days=30)).isoformat(), (today + timedelta(days=span)).isoformat()), []),
        # "Mark as done" (Canvas planner checkbox or canvas_mark_done), at any date.
        _or_default(c.planner_done_keys(), set()),
    )
    items = [i for _, r in results if not isinstance(r, CanvasError) for i in r]
    done_keys = apply_done_marks(items, planner, done_keys)
    items += calendar_deadlines(planner, items, {crs["id"]: _label(crs) for crs in courses}, done_keys, now)
    return items, results, planner


def _item_line(i: WorkItem, now: datetime, *, show_course: bool = True) -> str:
    parts = [f"**{i.name}**"]
    if show_course:
        parts.append(i.course_name)
    parts.append(f"due {fmt_dt(i.due_at, _tz(), now)}")
    if i.points:
        parts.append(f"{i.points:g} pts")
    if i.kind == "calendar_deadline":
        parts.append("marked done" if i.status == "marked_done" else "from course calendar" + (" (weight estimated)" if i.impact_pct else ""))
    elif i.status == "pending" and not i.online:
        parts.append("not submitted (or submitted outside Canvas)")
    else:
        parts.append(STATUS_LABEL.get(i.status, i.status))
    if i.kind == "calendar_deadline":
        ids = f"event_id={i.item_id}"
    elif i.course_id and i.kind != "planner_note":
        ids = f"course_id={i.course_id}, assignment_id={i.item_id}"
    else:
        ids = ""
    return " — ".join(parts) + (f"  `{ids}`" if ids else "")
