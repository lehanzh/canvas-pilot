"""Prompts: short commands that pin down which tools to call and how to answer."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .server import mcp


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
