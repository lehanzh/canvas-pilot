"""Canvas academic-assistant MCP server: tool and prompt definitions."""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .canvas import CanvasClient, CanvasError
from .config import ConfigError, load_settings
from .grades import GradeItem, describe_need, parse_scheme, parse_target, project
from .priority import (
    SUBMITTED_ELSEWHERE,
    TIER_ORDER,
    WorkItem,
    assignment_kind,
    compute_impacts,
    score_item,
    submission_status,
    tier,
)
from .textutil import fmt_day, fmt_dt, fmt_time, html_to_text, parse_dt

INSTRUCTIONS = """\
Academic assistant backed by the student's Canvas LMS account (read-mostly).
Start with canvas_get_daily_todo for "what should I work on", canvas_get_upcoming_deadlines
for "what's due", canvas_list_announcements for course news, and canvas_what_if_grade for
"what do I need to get an A". Course arguments accept a
numeric course ID, a course code, or a fragment of the course name (e.g. "3500" or "algorithms").
IDs printed in results (course_id, assignment_id) can be passed to the detail tools.
The student can hide courses (canvas_hide_course / canvas_unhide_course): course-wide summaries
leave hidden courses out, but naming a hidden course still works. Pass include_hidden=true only
when the student asks to include hidden courses.
All times are shown in the student's local timezone."""

mcp = MCPServer("canvas_pilot", title="Canvas Pilot", instructions=INSTRUCTIONS, version="0.1.0")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
PLANNER_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
# Changes only the local hidden-courses file, never Canvas.
LOCAL_SETTING = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

_client: CanvasClient | None = None


def client() -> CanvasClient:
    global _client
    if _client is None:
        _client = CanvasClient(load_settings())
    return _client


def tool(title: str, annotations: ToolAnnotations = READ_ONLY) -> Callable:
    """Register a tool whose Canvas/config failures surface to the model as readable errors."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await fn(*args, **kwargs)
            except (CanvasError, ConfigError) as e:
                raise ToolError(str(e)) from e
            except (KeyError, TypeError, ValueError) as e:
                # Unexpected data shape from Canvas; give the model something actionable instead of a bare failure.
                raise ToolError(f"Canvas returned data this tool didn't expect ({type(e).__name__}: {e}). Try a narrower request.") from e

        mcp.tool(title=title, annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator


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

# Calendar events that are really deadlines (courses that post "Homework-3 due" instead of an assignment),
# minus the sessions *about* a deadline ("Midterm review", "Office hours before the quiz").
DEADLINE_EVENT_RE = re.compile(r"\b(due|deadline|quiz|exam|midterm|final|test)\b", re.IGNORECASE)
NOT_DEADLINE_RE = re.compile(
    r"\b(review|office hours?|oh|study|prep|practice|solutions?|recitation|session|help|tutoring|lecture)\b", re.IGNORECASE
)


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\bdue\b", "", title.lower()))


def _stem(title: str) -> str:
    """Series name without numbers: 'Homework-3 due' -> 'homework'."""
    return re.sub(r"[^a-z]", "", re.sub(r"\bdue\b", "", title.lower()))


async def _work_items(
    courses: list[dict], now: datetime, days_ahead: int = 60
) -> tuple[list[WorkItem], list[tuple[dict, Any]], list[dict]]:
    """Every assignment across the courses as a WorkItem, with grade impact and planner 'done' marks applied."""
    c = client()

    async def per_course(course: dict) -> list[WorkItem]:
        assignments = await c.assignments(course["id"])
        groups = await c.assignment_groups(course["id"])
        impacts = compute_impacts(assignments, groups, bool(course.get("apply_assignment_group_weights")))
        items = []
        for a in assignments:
            if not a.get("published", True):
                continue
            impact, gname, gweight = impacts.get(a["id"], (None, None, None))
            items.append(
                WorkItem(
                    course_id=course["id"],
                    course_name=_label(course),
                    item_id=a["id"],
                    name=a.get("name", "(untitled)"),
                    kind=assignment_kind(a),
                    due_at=parse_dt(a.get("due_at")),
                    lock_at=parse_dt(a.get("lock_at")),
                    points=a.get("points_possible"),
                    group_name=gname,
                    group_weight=gweight,
                    impact_pct=impact,
                    status=submission_status(a, now),
                    submission_types=a.get("submission_types") or [],
                    html_url=a.get("html_url"),
                    planner_keys=[
                        ("assignment", a["id"]),
                        *([("quiz", a["quiz_id"])] if a.get("quiz_id") else []),
                        *([("discussion_topic", a["discussion_topic"]["id"])] if (a.get("discussion_topic") or {}).get("id") else []),
                    ],
                )
            )
        return items

    results = await c.gather_per_course(courses, per_course)
    items = [i for _, r in results if not isinstance(r, CanvasError) for i in r]

    # Planner items carry calendar-only deadlines and planner notes; the window grows with the request.
    try:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)  # day-aligned so the request is cacheable
        span = max(days_ahead, 60)
        planner = await c.planner_items((today - timedelta(days=30)).isoformat(), (today + timedelta(days=span)).isoformat())
    except CanvasError:
        planner = []
    # "Mark as done" (Canvas planner checkbox or canvas_mark_done), at any date.
    try:
        done_keys = await c.planner_done_keys()
    except CanvasError:
        done_keys = set()
    for p in planner:
        if (p.get("planner_override") or {}).get("marked_complete"):
            done_keys.add((p.get("plannable_type"), p.get("plannable_id")))
    for i in items:
        if not i.is_done and any(k in done_keys for k in i.planner_keys):
            i.status = "marked_done"

    by_id = {crs["id"]: crs for crs in courses}
    known = {_norm_title(i.name) for i in items}
    for p in planner:
        title = ((p.get("plannable") or {}).get("title") or "").strip()
        if p.get("plannable_type") != "calendar_event" or p.get("course_id") not in by_id:
            continue
        if not DEADLINE_EVENT_RE.search(title) or NOT_DEADLINE_RE.search(title) or _norm_title(title) in known:
            continue
        due = parse_dt(p.get("plannable_date"))
        done = ("calendar_event", p.get("plannable_id")) in done_keys
        # Borrow weight from a sibling assignment in the same series ("Homework-3 due" ~ "Homework-2").
        stem = _stem(title)
        sib = next(
            (i for i in items if i.course_id == p["course_id"] and i.kind != "calendar_deadline" and i.impact_pct and _stem(i.name) == stem),
            None,
        )
        items.append(
            WorkItem(
                course_id=p["course_id"],
                course_name=_label(by_id[p["course_id"]]),
                item_id=p["plannable_id"],
                name=title,
                kind="calendar_deadline",
                due_at=due,
                points=sib.points if sib else None,
                group_name=sib.group_name if sib else None,
                group_weight=sib.group_weight if sib else None,
                impact_pct=sib.impact_pct if sib else None,
                # Canvas can't see the submission, so a past calendar deadline is neither pending nor overdue.
                status="marked_done" if done else ("pending" if due and due >= now else "past_event"),
                submission_types=["none"],
                html_url=p.get("html_url"),
            )
        )
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


# -- tools --------------------------------------------------------------------


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
    a = await c.get(f"/courses/{cid}/assignments/{assignment_id}", {"include[]": ["submission"]})
    try:
        sub = await c.get(
            f"/courses/{cid}/assignments/{assignment_id}/submissions/self",
            {"include[]": ["submission_comments", "rubric_assessment"]},
        )
    except CanvasError:
        sub = a.get("submission") or {}
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
    try:
        groups = await c.assignment_groups(cid)
        all_a = await c.assignments(cid)
        impact, gname, gweight = compute_impacts(all_a, groups, bool(crs.get("apply_assignment_group_weights"))).get(
            a["id"], (None, None, None)
        )
        if gname:
            facts.append(("Assignment group", gname + (f" ({gweight:g}% of grade)" if gweight else "")))
        if impact is not None:
            facts.append(("Est. share of final grade", f"~{impact:.1f}%"))
    except CanvasError:
        pass
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
        s.append(f"- **Comment from {cm.get('author_name', '?')}** ({fmt_dt(cm.get('created_at'), tz)}): {(cm.get('comment') or '').strip()}")
    out += s

    if rubric := a.get("rubric"):
        assessed = sub.get("rubric_assessment") or {}
        out.append("\n## Rubric")
        for crit in rubric:
            line = f"- **{crit.get('description') or ''}** ({crit.get('points') or 0:g} pts)"
            if crit.get("long_description"):
                line += f": {html_to_text(crit['long_description'], 400)}"
            got = assessed.get(crit.get("id"), {})
            if got.get("points") is not None:
                line += f" — you got {got['points']:g}" + (f" · {got['comments']}" if got.get("comments") else "")
            out.append(line)

    desc = html_to_text(a.get("description"), max_description_chars)
    out.append("\n## Instructions\n" + (desc or "_No description provided in Canvas._"))
    return "\n".join(x for x in out if x is not None)


@tool("List announcements")
async def canvas_list_announcements(
    course: OptionalCourseArg = None,
    days_back: Annotated[int, Field(ge=1, le=180)] = 14,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
    full_text: Annotated[bool, Field(description="Show full announcement bodies instead of a preview.")] = False,
    include_hidden: IncludeHiddenArg = False,
) -> str:
    """Recent course announcements, newest first, with author, date, unread flag, and message text."""
    c = client()
    courses = await _courses(course, include_hidden)
    footer = await _hidden_footer(course, include_hidden)
    now = _now()
    by_code = {f"course_{x['id']}": x for x in courses}
    anns: list[dict] = []
    codes = list(by_code)
    for chunk in (codes[i : i + 10] for i in range(0, len(codes), 10)):  # Canvas caps context_codes at 10
        anns += await c.get_all(
            "/announcements",
            {
                "context_codes[]": chunk,
                "start_date": (now - timedelta(days=days_back)).date().isoformat(),
                "end_date": (now + timedelta(days=1)).date().isoformat(),
            },
        )
    anns.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
    if not anns:
        return f"No announcements in the last {days_back} days." + footer
    tz = _tz()
    out = [f"{len(anns)} announcements in the last {days_back} days" + (f" (showing {limit})" if len(anns) > limit else "") + ":"]
    for x in anns[:limit]:
        crs = by_code.get(x.get("context_code"), {})
        unread = " · **unread**" if x.get("read_state") == "unread" else ""
        author = (x.get("author") or {}).get("display_name") or x.get("user_name") or "?"
        body = html_to_text(x.get("message"), None if full_text else 600)
        out.append(
            f"\n### {x.get('title')}\n{(_label(crs) + _hidden_tag(crs)) if crs else x.get('context_code')} · {author} · "
            f"{fmt_dt(x.get('posted_at'), tz, now)}{unread} · {x.get('html_url', '')}\n\n{body}"
        )
    return "\n".join(out) + footer


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


def _match_items(key: str, items: list[GradeItem]) -> GradeItem:
    k = key.strip()
    if k.isdigit():
        hits = [i for i in items if i.item_id == int(k)]
    else:
        kn = _norm_title(k)
        hits = [i for i in items if kn and kn in _norm_title(i.name)]
        exact = [i for i in hits if _norm_title(i.name) == kn]
        hits = exact or hits
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ToolError(
            f"No assignment or category matches {key!r} for the what-if. "
            "Use canvas_list_assignments for assignment names or canvas_get_grades for categories."
        )
    raise ToolError(f"{key!r} matches several assignments: " + ", ".join(i.name for i in hits[:10]) + ". Be more specific.")


def _has_item_match(key: str, items: list[GradeItem]) -> bool:
    try:
        _match_items(key, items)
        return True
    except ToolError as e:
        return "several" in str(e)  # ambiguous item names still mean the user meant assignments


def _match_group(key: str, groups: list[dict]) -> dict | None:
    kn = _norm_title(key)
    hits = [g for g in groups if kn and kn in _norm_title(g.get("name") or "")]
    exact = [g for g in hits if _norm_title(g.get("name") or "") == kn]
    hits = exact or hits
    return hits[0] if len(hits) == 1 else None


@tool("What-if grade calculator")
async def canvas_what_if_grade(
    course: CourseArg,
    target: Annotated[
        str | None, Field(description="Grade to aim for: a letter from the course scheme ('A-') or a percentage ('90'). Omit to see every letter grade.")
    ] = None,
    what_if: Annotated[
        dict[str, float] | None,
        Field(
            description="Hypothetical scores in percent, keyed by assignment name (or part of it), assignment_id, or a "
            "whole category name for everything left in it, e.g. {\"Homework-3\": 85, \"Midterm Exams\": 75}. "
            "Can also override graded work."
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
    detail = await c.get(f"/courses/{cid}", {"include[]": ["grading_scheme", "total_scores"]})
    scheme = parse_scheme(detail.get("grading_scheme"))
    weighted = bool(detail.get("apply_assignment_group_weights", crs.get("apply_assignment_group_weights")))
    assignments = await c.assignments(cid)
    groups = await c.assignment_groups(cid)

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
    applied: list[str] = []
    fixed_empty: dict[int | None, float] = {}
    for key, pct in (what_if or {}).items():
        group = _match_group(key, groups) if not _has_item_match(key, items) else None
        if group is not None:
            left = [i for i in items if i.group_id == group["id"] and i.score is None]
            if left:
                for it in left:
                    it.score, it.source = it.points * pct / 100.0, "what-if"
                applied.append(f"- everything left in {group['name']} ({len(left)} items): {pct:g}%")
            elif weighted and group.get("group_weight"):
                fixed_empty[group["id"]] = pct / 100.0
                applied.append(f"- {group['name']} (nothing posted yet, {group['group_weight']:g}% weight): {pct:g}%")
            else:
                raise ToolError(f"{group['name']} has nothing left to score, so a what-if there changes nothing.")
            continue
        it = _match_items(key, items)
        it.original_score, it.score, it.source = it.score, it.points * pct / 100.0, "what-if"
        was = f" (actual {it.original_score:g}/{it.points:g})" if it.original_score is not None else ""
        applied.append(f"- {it.name}: {pct:g}% → {it.score:g}/{it.points:g}{was}")
    proj = project(items, groups, weighted, fixed_empty) if applied else actual

    out = [f"# What-if grades — {crs['name']}{_hidden_tag(crs)}"]
    enr = next((e for e in detail.get("enrollments") or crs.get("enrollments") or [] if e.get("type") == "student"), {})
    canvas_now = enr.get("computed_current_score")
    if actual.current is not None:
        line = f"Current grade on graded work: **{actual.current:.2f}%**"
        if canvas_now is not None and abs(canvas_now - actual.current) > 0.5:
            line += f" (Canvas shows {canvas_now}%; the gap usually means drop rules or hidden assignments)"
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
    if any((g.get("rules") or {}) for g in groups):
        notes.append("drop-lowest rules are ignored, so your real result may be slightly better")
    if not proj.weighted and not unposted_points:
        notes.append(
            "this course grades by points, so only work already posted is counted; pass unposted_points if more is coming"
        )
    notes.append("assumes the same average on every remaining item; extra credit and curves aren't modelled")
    out.append("\n_Notes: " + "; ".join(notes) + "._")
    return "\n".join(out)


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
    events: list[dict] = []
    for chunk in (codes[i : i + 10] for i in range(0, len(codes), 10)):
        events += await c.get_all("/calendar_events", {**window, "context_codes[]": chunk})
    if not course:
        # Some schools block the personal calendar for API tokens (403); skip it quietly then.
        try:
            events += await c.get_all("/calendar_events", {**window, "context_codes[]": ["user_self"]})
        except CanvasError:
            pass
    events.sort(key=lambda e: e.get("start_at") or "")
    if not events:
        return f"No calendar events in the next {days_ahead} days." + footer
    out = [f"{len(events)} events in the next {days_ahead} days:"]
    for e in events:
        line = f"- **{e.get('title')}** — {names.get(e.get('context_code'), e.get('context_name') or 'Personal')} — {fmt_dt(e.get('start_at'), tz, now)}"
        if e.get("end_at") and e.get("end_at") != e.get("start_at"):
            line += f" to {fmt_time(parse_dt(e['end_at']).astimezone(tz))}"
        if e.get("location_name"):
            line += f" · {e['location_name']}"
        if desc := html_to_text(e.get("description"), 300):
            line += f"\n  {desc}"
        out.append(line)
    return "\n".join(out) + footer


@tool("Get course syllabus")
async def canvas_get_syllabus(
    course: CourseArg,
    max_chars: Annotated[int, Field(ge=1000, le=100000)] = 20000,
) -> str:
    """The course syllabus page text (policies, grading scheme, schedule) as plain text."""
    c = client()
    crs = await c.resolve_course(course)
    data = await c.get(f"/courses/{crs['id']}", {"include[]": ["syllabus_body"]})
    text = html_to_text(data.get("syllabus_body"), max_chars)
    if not text:
        return (
            f"{crs['name']} has no syllabus page in Canvas. It may be posted as a file or module item; "
            "try canvas_search_course_files with 'syllabus' or canvas_list_modules."
        )
    return f"# Syllabus — {crs['name']}{_hidden_tag(crs)}\n\n{text}"


@tool("List course modules")
async def canvas_list_modules(
    course: CourseArg,
    search: Annotated[str | None, Field(description="Only show items whose title contains this text.")] = None,
) -> str:
    """Course modules and their items (readings, lectures, pages, files, assignments) with links and due dates."""
    c = client()
    crs = await c.resolve_course(course)
    modules = await c.get_all(f"/courses/{crs['id']}/modules", {"include[]": ["items", "content_details"]})
    try:  # module Quiz items carry the quiz ID; map it to the assignment ID the other tools take
        quiz_to_assignment = {a["quiz_id"]: a["id"] for a in await c.assignments(crs["id"]) if a.get("quiz_id")}
    except CanvasError:
        quiz_to_assignment = {}
    tz = _tz()
    out = [f"# Modules — {crs['name']}{_hidden_tag(crs)}"]
    needle = (search or "").lower()
    for m in modules:
        items = m.get("items")
        if items is None:  # Canvas omits items for large modules
            items = await c.get_all(f"/courses/{crs['id']}/modules/{m['id']}/items", {"include[]": ["content_details"]})
        if needle:
            items = [i for i in items if needle in (i.get("title") or "").lower()]
            if not items:
                continue
        state = f" ({m['state']})" if m.get("state") and m["state"] != "unlocked" else ""
        out.append(f"\n## {m.get('name')}{state}")
        for i in items:
            if i.get("type") == "SubHeader":
                out.append(f"  _{i.get('title')}_")
                continue
            det = i.get("content_details") or {}
            extra = f" · due {fmt_dt(det['due_at'], tz)}" if det.get("due_at") else ""
            done = " ✓" if (i.get("completion_requirement") or {}).get("completed") else ""
            if i.get("type") == "Assignment" and i.get("content_id"):
                ref = f" `assignment_id={i['content_id']}`"
            elif i.get("type") == "Quiz" and i.get("content_id") in quiz_to_assignment:
                ref = f" `assignment_id={quiz_to_assignment[i['content_id']]}`"
            elif i.get("type") == "Page" and i.get("page_url"):
                ref = f" `page={i['page_url']}`"
            else:
                ref = ""
            out.append(f"- [{i.get('type')}] {i.get('title')}{extra}{done}{ref} {i.get('html_url', '')}")
    if len(out) == 1:
        return f"No modules{' matching ' + repr(search) if search else ''} in {crs['name']}."
    return "\n".join(out)


@tool("Read a course page")
async def canvas_get_page(
    course: CourseArg,
    page: Annotated[str, Field(min_length=1, description="The page= value from canvas_list_modules, or words from the page title.")],
    max_chars: Annotated[int, Field(ge=1000, le=100000)] = 20000,
) -> str:
    """Read a Canvas wiki page (lecture notes, assignment specs, course info) as plain text."""
    c = client()
    crs = await c.resolve_course(course)
    base = f"/courses/{crs['id']}/pages"
    try:
        data = await c.get(f"{base}/{page.strip()}")
    except CanvasError as e:
        if e.status != 404:
            raise
        hits = await c.get_all(base, {"search_term": page.strip(), "sort": "title"})
        if not hits:
            raise CanvasError(f"No page matching {page!r} in {crs['name']}. Use canvas_list_modules to find page= values.", 404)
        if len(hits) > 1:
            names = "\n".join(f"- {h.get('title')}  `page={h.get('url')}`" for h in hits[:25])
            return f"{len(hits)} pages match {page!r} in {crs['name']}; pass one of these page= values:\n{names}"
        data = await c.get(f"{base}/{hits[0]['url']}")
    if data.get("locked_for_user"):
        return f"**{data.get('title')}** is locked: {data.get('lock_explanation') or 'not available yet'}"
    body = html_to_text(data.get("body"), max_chars) or "_This page is empty._"
    updated = fmt_dt(data.get("updated_at"), _tz()) if data.get("updated_at") else "?"
    return f"# {data.get('title')}\n{crs['name']}{_hidden_tag(crs)} · updated {updated} · {data.get('html_url', '')}\n\n{body}"


@tool("Search course files")
async def canvas_search_course_files(
    course: CourseArg,
    search_term: Annotated[str | None, Field(min_length=2, description="Filename text to search for, e.g. 'lecture 5'.")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
) -> str:
    """Find files (slides, PDFs, handouts) in a course, newest first, with links to open them in Canvas."""
    c = client()
    crs = await c.resolve_course(course)
    params: dict[str, Any] = {"sort": "updated_at", "order": "desc"}
    if search_term:
        params["search_term"] = search_term
    files = await c.get_all(f"/courses/{crs['id']}/files", params)
    if not files:
        return f"No files{' matching ' + repr(search_term) if search_term else ''} in {crs['name']}."
    tz = _tz()
    base = c.settings.base_url
    out = [f"{len(files)} files in {crs['name']}{_hidden_tag(crs)}" + (f" (showing {limit})" if len(files) > limit else "") + ":"]
    for f in files[:limit]:
        size = f"{(f.get('size') or 0) / 1_000_000:.1f} MB"
        out.append(
            f"- **{f.get('display_name')}** · {size} · updated {fmt_dt(f.get('updated_at'), tz)} · "
            f"{base}/courses/{crs['id']}/files/{f['id']}"
        )
    return "\n".join(out)


@tool("Read Canvas inbox")
async def canvas_list_inbox(
    unread_only: Annotated[bool, Field()] = True,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> str:
    """Recent Canvas inbox conversations (messages from instructors, TAs, classmates).
    Messages from hidden courses are still shown, labelled as such."""
    c = client()
    params = {"scope": "unread"} if unread_only else {}
    convos = await c.get_all("/conversations", params, cache=False)
    if not convos:
        return "No unread Canvas messages." if unread_only else "Canvas inbox is empty."
    tz = _tz()
    now = _now()
    out = [f"{len(convos)} {'unread ' if unread_only else ''}conversations" + (f" (showing {limit})" if len(convos) > limit else "") + ":"]
    for cv in convos[:limit]:
        who = ", ".join(p.get("name", "?") for p in cv.get("participants") or [])
        ctx = cv.get("context_code") or ""
        hidden = " (hidden course)" if ctx.startswith("course_") and c.is_hidden(int(ctx.split("_")[1])) else ""
        out.append(
            f"\n### {cv.get('subject') or '(no subject)'}\n{cv.get('context_name') or ''}{hidden} · {who} · "
            f"{fmt_dt(cv.get('last_message_at'), tz, now)} · {cv.get('message_count', '?')} messages\n"
            f"{(cv.get('last_message') or '').strip()}"
        )
    return "\n".join(out)


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


# -- prompts --------------------------------------------------------------------


# Short commands. Clients show these as slash commands (Claude Code: /mcp__canvas__today) or in a
# prompt menu (Claude Desktop: "+" > canvas). Each pins down which tools to call and how to answer.

PromptCourse = Annotated[str, Field(description="Optional: one course (ID, code, or name fragment). Blank = all courses.")]


def _scope(course: str) -> str:
    return f"course {course.strip()!r}" if course.strip() else "all my courses"


def _course_arg(course: str) -> str:
    return f', course="{course.strip()}"' if course.strip() else ""


@mcp.prompt(title="Overview")
def overview(course: PromptCourse = "") -> str:
    """Snapshot of where things stand: grades, what's due, news, next exam."""
    if course.strip():
        return (
            f"Give me an overview of {_scope(course)}. Call canvas_get_grades(course=\"{course.strip()}\"), "
            f"canvas_get_upcoming_deadlines(days_ahead=14{_course_arg(course)}), "
            f"canvas_list_announcements(days_back=7{_course_arg(course)}) and "
            f"canvas_what_if_grade(course=\"{course.strip()}\"). Then write, briefly: current grade and the average I need "
            "on remaining work to keep (or reach the next) letter grade; deadlines in the next 2 weeks; anything in "
            "announcements that changes requirements or dates; anything missing. Skip empty sections."
        )
    return (
        "Give me an overview of all my courses. Call canvas_list_courses, canvas_get_daily_todo(days_ahead=7), "
        "canvas_list_announcements(days_back=7) and canvas_get_calendar_events(days_ahead=14). Then write: "
        "(1) one line per course: current grade, next deadline, new announcements; (2) my top 3 priorities this week "
        "and why; (3) anything overdue or under 'Check you submitted'; (4) the next exam or quiz. Keep it skimmable."
    )


@mcp.prompt(title="Today")
def today() -> str:
    """What to do today, in order."""
    return (
        "Plan my day. Call canvas_get_daily_todo(days_ahead=2) and canvas_get_calendar_events(days_ahead=1). "
        "Answer with at most 5 numbered items in the order I should do them, each with its course, due time, and a "
        "few words on why it's first. Then list today's events (class, office hours, recitation) with times and "
        "links, and anything under 'Check you submitted'. If nothing is due, say so in one line. No preamble."
    )


@mcp.prompt(title="This week")
def week() -> str:
    """Day-by-day plan for the next 7 days."""
    return (
        "Plan my next 7 days. Call canvas_get_upcoming_deadlines(days_ahead=7), canvas_get_calendar_events(days_ahead=7) "
        "and canvas_get_daily_todo(days_ahead=7). Write one short section per day (starting today, e.g. 'Thu Oct 22'): "
        "what's due that day, fixed events, and the work I should do that day so nothing piles up. Start bigger "
        "assignments 2-3 days before they're due, and point out the heaviest day. Skip days with nothing on them."
    )


@mcp.prompt(title="News")
def news(
    course: PromptCourse = "",
    days: Annotated[str, Field(description="Optional: how many days back to look (default 7).")] = "",
) -> str:
    """Recent announcements, with changes and required actions called out."""
    n = days.strip() if days.strip().isdigit() else "7"
    return (
        f"Summarize announcements from {_scope(course)} in the last {n} days. Call "
        f"canvas_list_announcements(days_back={n}, full_text=true{_course_arg(course)}). Use only what the announcements "
        "say. Group into: **Changes** (moved deadlines or exams, new requirements, room or format changes; quote the new "
        "date or detail), **Action needed** (things I must do, with the deadline), and **FYI** (one line each). Mark "
        "unread ones. If there are no announcements, say so in one line."
    )


@mcp.prompt(title="Due soon")
def due(course: PromptCourse = "") -> str:
    """Everything due in the next 14 days."""
    return (
        f"List everything due in the next 14 days for {_scope(course)}. Call "
        f"canvas_get_upcoming_deadlines(days_ahead=14{_course_arg(course)}). Show it as a table grouped by day with columns: "
        "due time, course, item, points, status. Put anything overdue first. Mark items submitted outside Canvas "
        "(Gradescope) or taken from the course calendar. No advice, just the list."
    )


@mcp.prompt(title="Plan an assignment")
def plan_assignment(course: str, assignment_id: str) -> str:
    """Break an assignment into a step-by-step work plan before its deadline."""
    return (
        f"Use canvas_get_assignment for course {course!r}, assignment {assignment_id}. Read the instructions and "
        "rubric, then make a plan: a checklist of concrete steps, an effort estimate for each, and dated "
        "milestones working back from the due date. Point out rubric criteria that are easy to miss."
    )
