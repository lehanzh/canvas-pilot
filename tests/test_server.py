"""End-to-end tests: real MCP client -> server -> CanvasClient -> fake Canvas API (httpx.MockTransport)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp import Client

from canvas_pilot import server
from canvas_pilot.canvas import CanvasClient
from canvas_pilot.config import Settings
from canvas_pilot.priority import WorkItem, compute_impacts, score_item, tier
from canvas_pilot.textutil import html_to_text

NOW = datetime.now(UTC)


def iso(delta_hours: float) -> str:
    return (NOW + timedelta(hours=delta_hours)).isoformat().replace("+00:00", "Z")


COURSES = [
    {
        "id": 101, "name": "Object-Oriented Design", "course_code": "CS3500", "apply_assignment_group_weights": True,
        "term": {"name": "Fall 2026"},
        "enrollments": [{"type": "student", "computed_current_score": 91.5, "computed_current_grade": "A-"}],
    },
    {
        "id": 202, "name": "Algorithms and Data", "course_code": "CS3000", "apply_assignment_group_weights": False,
        "enrollments": [{"type": "student", "computed_current_score": None}],
    },
    # An orientation shell: the course the hiding tests hide.
    {
        "id": 404, "name": "Husky 101 Orientation", "course_code": "Husky101.F26",
        "enrollments": [{"type": "student", "computed_current_score": 100.0}],
    },
    {"id": 303, "name": None},  # restricted course: never listed
]

GROUPS = {
    101: [{"id": 1, "name": "Homework", "group_weight": 30}, {"id": 2, "name": "Exams", "group_weight": 70}],
    202: [{"id": 3, "name": "Assignments", "group_weight": 0}],
    404: [{"id": 9, "name": "Orientation", "group_weight": 0}],
}

ASSIGNMENTS = {
    404: [
        {"id": 41, "name": "Orientation quiz", "due_at": iso(30), "points_possible": 5, "assignment_group_id": 9,
         "submission_types": ["online_quiz"], "published": True, "submission": {}},
    ],
    101: [
        {"id": 11, "name": "HW 4", "due_at": iso(20), "points_possible": 10, "assignment_group_id": 1,
         "submission_types": ["online_upload"], "html_url": "https://x/11", "published": True,
         "submission": {"workflow_state": "unsubmitted"}},
        {"id": 12, "name": "Midterm Exam", "due_at": iso(24 * 5), "points_possible": 100, "assignment_group_id": 2,
         "submission_types": ["online_quiz"], "quiz_id": 900, "published": True,
         "submission": {"workflow_state": "unsubmitted"}},
        {"id": 13, "name": "HW 3", "due_at": iso(-30), "lock_at": iso(48), "points_possible": 10, "assignment_group_id": 1,
         "submission_types": ["online_upload"], "published": True,
         "submission": {"workflow_state": "unsubmitted", "missing": True}},
        {"id": 14, "name": "HW 2", "due_at": iso(-24 * 8), "points_possible": 10, "assignment_group_id": 1,
         "submission_types": ["online_upload"], "published": True,
         "submission": {"workflow_state": "graded", "score": 9, "submitted_at": iso(-24 * 9), "graded_at": iso(-24 * 2)}},
        {"id": 15, "name": "HW 1", "due_at": iso(-24 * 15), "lock_at": iso(-24 * 14), "points_possible": 10,
         "assignment_group_id": 1, "submission_types": ["online_upload"], "published": True,
         "submission": {"workflow_state": "unsubmitted", "missing": True}},
        # Gradescope work a day past due: Canvas flags it missing, but it may well be submitted.
        {"id": 16, "name": "Gradescope HW", "due_at": iso(-20), "points_possible": 10, "assignment_group_id": 1,
         "submission_types": ["external_tool"], "published": True,
         "submission": {"workflow_state": "unsubmitted", "missing": True}},
        # Old, still-open item the student checked off long ago (only visible via /planner/overrides).
        {"id": 17, "name": "Old reflection", "due_at": iso(-24 * 45), "lock_at": iso(24 * 30), "points_possible": 5,
         "assignment_group_id": 1, "submission_types": ["online_text_entry"], "published": True,
         "submission": {"workflow_state": "unsubmitted", "missing": True}},
        {"id": 18, "name": "Quiz A", "due_at": iso(24 * 9), "points_possible": 5, "assignment_group_id": 1,
         "submission_types": ["online_quiz"], "quiz_id": 4321, "published": True, "submission": {}},
    ],
    202: [
        {"id": 21, "name": "Problem Set 5", "due_at": iso(30), "points_possible": 50, "assignment_group_id": 3,
         "submission_types": ["online_text_entry"], "published": True, "submission": {"workflow_state": "unsubmitted"}},
        {"id": 22, "name": "Reading response", "due_at": iso(10), "points_possible": 5, "assignment_group_id": 3,
         "submission_types": ["online_text_entry"], "published": True, "submission": {"workflow_state": "unsubmitted"}},
        {"id": 23, "name": "Problem Set 4", "due_at": iso(-50), "points_possible": 45, "assignment_group_id": 3,
         "submission_types": ["online_text_entry"], "published": True,
         "submission": {"workflow_state": "submitted", "submitted_at": iso(-52)}},
    ],
}

PLANNER = [
    # The student checked "Reading response" off in the Canvas planner.
    {"plannable_type": "assignment", "plannable_id": 22, "planner_override": {"marked_complete": True}, "plannable": {}},
    # A deadline that exists only as a course-calendar event, plus one duplicating a real assignment.
    {"plannable_type": "calendar_event", "plannable_id": 501, "course_id": 101, "plannable_date": iso(60),
     "planner_override": None, "plannable": {"title": "HW 6 due"}},
    {"plannable_type": "calendar_event", "plannable_id": 502, "course_id": 101, "plannable_date": iso(20),
     "planner_override": None, "plannable": {"title": "HW 4 due"}},
    {"plannable_type": "calendar_event", "plannable_id": 503, "course_id": 101, "plannable_date": iso(30),
     "planner_override": None, "plannable": {"title": "Office Hours"}},
    {"plannable_type": "calendar_event", "plannable_id": 504, "course_id": 101, "plannable_date": iso(40),
     "planner_override": None, "plannable": {"title": "Midterm review session"}},
    {"plannable_type": "planner_note", "plannable_id": 78, "course_id": 404, "plannable_date": iso(10),
     "planner_override": None, "plannable": {"title": "Bring ID card"}},
    {"plannable_type": "planner_note", "plannable_id": 77, "plannable_date": iso(5), "context_name": None,
     "planner_override": None, "plannable": {"title": "Email TA about regrade"}},
]

ANNOUNCEMENTS = [
    {"id": 1, "title": "Midterm moved", "context_code": "course_101", "posted_at": iso(-5), "read_state": "unread",
     "author": {"display_name": "Prof. Lee"}, "message": "<p>The midterm is now <strong>Friday</strong>.</p>",
     "html_url": "https://x/a1"},
    {"id": 2, "title": "Office hours", "context_code": "course_202", "posted_at": iso(-30), "read_state": "read",
     "user_name": "TA Sam", "message": "<ul><li>Mon 2pm</li><li>Wed 4pm</li></ul>"},
    {"id": 3, "title": "Welcome week", "context_code": "course_404", "posted_at": iso(-2), "read_state": "unread",
     "user_name": "Orientation Team", "message": "<p>Events all week.</p>"},
]

EVENTS = [
    {"title": "Midterm review", "context_code": "course_101", "start_at": iso(48), "end_at": iso(49.5), "location_name": "WVH 210"},
    {"title": "Orientation social", "context_code": "course_404", "start_at": iso(26)},
]


class FakeCanvas:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.posted: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        path = request.url.path.removeprefix("/api/v1")
        q = request.url.params
        if request.method in {"POST", "PUT"}:
            body = json.loads(request.content)
            self.posted.append((path, body))
            return httpx.Response(200, json={"id": 555, **body})
        if path == "/courses":
            # Exercise Link-header pagination: two pages.
            if q.get("page") == "2":
                return httpx.Response(200, json=COURSES[1:])
            nxt = str(request.url.copy_merge_params({"page": "2"}))
            return httpx.Response(200, json=COURSES[:1], headers={"Link": f'<{nxt}>; rel="next"'})
        parts = path.strip("/").split("/")
        if parts[0] == "courses" and len(parts) == 3 and parts[2] == "assignments":
            return httpx.Response(200, json=ASSIGNMENTS[int(parts[1])])
        if parts[0] == "courses" and len(parts) == 3 and parts[2] == "assignment_groups":
            return httpx.Response(200, json=GROUPS[int(parts[1])])
        if parts[0] == "courses" and len(parts) == 4 and parts[2] == "assignments":
            a = next(a for a in ASSIGNMENTS[int(parts[1])] if a["id"] == int(parts[3]))
            return httpx.Response(200, json={**a, "description": "<h2>Task</h2><p>Implement the <a href='https://spec'>spec</a>.</p>",
                                             "rubric": [{"id": "r1", "description": "Correctness", "points": 7}]})
        if path.endswith("/submissions/self"):
            return httpx.Response(200, json={"workflow_state": "unsubmitted",
                                             "submission_comments": [{"author_name": "TA", "comment": "Start early!"}]})
        if path == "/planner/items":
            return httpx.Response(200, json=PLANNER)
        if path == "/planner/overrides":
            return httpx.Response(200, json=[{"id": 1, "plannable_type": "assignment", "plannable_id": 17, "marked_complete": True}])
        if path == "/courses/101/modules":
            return httpx.Response(200, json=[{"id": 1, "name": "Week 1", "items": [
                {"type": "Quiz", "title": "Quiz A", "content_id": 4321, "html_url": "https://x/m1"},
                {"type": "Page", "title": "Lab spec", "page_url": "lab-spec", "html_url": "https://x/m2"}]}])
        if path == "/courses/101/pages/lab-spec":
            return httpx.Response(200, json={"title": "Lab spec", "body": "<p>Build the <em>thing</em>.</p>", "html_url": "https://x/p"})
        if path.startswith("/courses/101/pages/"):
            return httpx.Response(404, json={})
        if path == "/courses/101/pages":
            hits = [{"title": "Lab spec", "url": "lab-spec"}] if "lab" in (q.get("search_term") or "").lower() else []
            return httpx.Response(200, json=hits)
        if path == "/courses/101/files":
            return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
        if path == "/announcements":
            codes = q.get_list("context_codes[]")
            return httpx.Response(200, json=[a for a in ANNOUNCEMENTS if a["context_code"] in codes])
        if path == "/calendar_events":
            codes = q.get_list("context_codes[]")
            if codes == ["user_self"]:
                return httpx.Response(403, json={"status": "unauthorized"})
            return httpx.Response(200, json=[e for e in EVENTS if e["context_code"] in codes])
        if path == "/conversations":
            return httpx.Response(200, json=[{"subject": "Welcome!", "context_code": "course_404", "context_name": "Husky 101",
                                              "participants": [{"name": "Orientation Team"}], "last_message": "Hi there",
                                              "last_message_at": iso(-1), "message_count": 1}])
        if path in {"/courses/101", "/courses/202"}:
            crs = next(c for c in COURSES if c["id"] == int(parts[1]))
            return httpx.Response(200, json={**crs, "grading_scheme": [["A", 0.94], ["A-", 0.9], ["B", 0.84], ["F", 0.0]]})
        if path == "/courses/999":
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})
        return httpx.Response(404, json={})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    fake = FakeCanvas()
    settings = Settings(base_url="https://canvas.test", token="test-token", timezone=ZoneInfo("America/New_York"),
                        exclude_course_ids=frozenset(), hidden_courses_file=tmp_path / "hidden_courses.json")
    monkeypatch.setattr(server, "_client", CanvasClient(settings, transport=httpx.MockTransport(fake)))
    return fake


async def call(name: str, args: dict | None = None) -> str:
    async with Client(server.mcp) as c:
        r = await c.call_tool(name, args or {})
        text = r.content[0].text
        if r.is_error:
            raise AssertionError(text)
        return text


async def test_list_courses_paginates_and_hides_restricted(fake):
    out = await call("canvas_list_courses")
    assert "Object-Oriented Design" in out and "Algorithms and Data" in out
    assert "91.5% (A-)" in out
    assert "3 active courses" in out


async def test_daily_todo_ranks_and_filters(fake):
    out = await call("canvas_get_daily_todo", {"days_ahead": 7})
    # Overdue-but-open HW 3 first; graded/submitted/marked-done/closed items excluded.
    assert out.index("## Overdue") < out.index("HW 3") < out.index("## Do today")
    for absent in ("HW 2", "Problem Set 4", "Reading response", "HW 1"):
        assert absent not in out.split("_1 past")[0], absent
    assert "Email TA about regrade" in out
    assert "1 past assignment(s) were missed" in out
    # Gradescope item is a gentle check, not OVERDUE; the long-ago checked-off item stays hidden.
    assert out.index("## Check you submitted") < out.index("Gradescope HW")
    assert "Old reflection" not in out and "Midterm review session" not in out
    # The heavily weighted midterm carries its grade share in the reasons.
    assert "Midterm Exam" in out and "% of final grade" in out
    # Calendar-only deadline included once; duplicate of HW 4 and office hours are not.
    assert "HW 6 due" in out and "event_id=501" in out
    assert "weight estimated" in out  # borrowed from the HW series' group weight
    assert "HW 4 due" not in out and "Office Hours" not in out


async def test_deadlines_grouped_by_day(fake):
    out = await call("canvas_get_upcoming_deadlines", {"days_ahead": 7})
    assert "## Overdue" in out
    assert out.index("HW 4") < out.index("Problem Set 5") < out.index("Midterm Exam")
    assert "Reading response" not in out  # marked done in planner


async def test_assignment_details(fake):
    out = await call("canvas_get_assignment", {"course": "3500", "assignment_id": 12})
    assert "# Midterm Exam" in out
    assert "Exams (70% of grade)" in out
    assert "~70.0%" in out  # only exam in a 70% group
    assert "Implement the spec (https://spec)" in out
    assert "Correctness" in out and "Start early!" in out


async def test_course_name_resolution_errors_are_helpful(fake):
    async with Client(server.mcp) as c:
        r = await c.call_tool("canvas_list_assignments", {"course": "CS"})
        assert r.is_error and "matches several courses" in r.content[0].text
        r = await c.call_tool("canvas_get_syllabus", {"course": "999"})
        assert r.is_error and "404" in r.content[0].text


async def test_announcements(fake):
    out = await call("canvas_list_announcements", {"days_back": 7})
    assert "Midterm moved" in out and "**unread**" in out and "The midterm is now **Friday**." in out
    assert "Office hours" in out and "- Mon 2pm" in out


async def test_grades_course_breakdown(fake):
    out = await call("canvas_get_grades", {"course": "Object"})
    assert "Current: 91.5% (A-)" in out
    assert "**Homework** · weight 30%: 9/10 (90.0%)" in out
    assert "### Missing" in out and "HW 3" in out and "HW 1" in out


async def test_calendar(fake):
    out = await call("canvas_get_calendar_events")
    assert "Midterm review" in out and "WVH 210" in out


async def test_mark_done_targets_quiz_plannable(fake):
    out = await call("canvas_mark_done", {"course": "101", "assignment_id": 12})
    assert "Nothing was submitted" in out
    assert fake.posted == [("/planner/overrides", {"plannable_type": "quiz", "plannable_id": 900, "marked_complete": True})]


async def test_add_planner_note(fake):
    out = await call("canvas_add_planner_note", {"title": "Start lab", "date": "2026-10-23", "course": "3000"})
    path, body = fake.posted[0]
    assert path == "/planner_notes" and body["course_id"] == 202 and body["todo_date"].startswith("2026-10-23T23:59")
    assert "Start lab" in out


async def test_modules_map_quiz_to_assignment_and_pages_readable(fake):
    out = await call("canvas_list_modules", {"course": "101"})
    assert "assignment_id=18" in out and "page=lab-spec" in out
    page = await call("canvas_get_page", {"course": "101", "page": "lab-spec"})
    assert "# Lab spec" in page and "Build the thing." in page
    assert "# Lab spec" in await call("canvas_get_page", {"course": "101", "page": "Lab"})  # title search fallback


async def test_html_instead_of_json_gives_actionable_error(fake):
    async with Client(server.mcp) as c:
        r = await c.call_tool("canvas_search_course_files", {"course": "101"})
        assert r.is_error and "CANVAS_BASE_URL" in r.content[0].text


async def test_bad_date_and_empty_course_rejected(fake):
    async with Client(server.mcp) as c:
        r = await c.call_tool("canvas_add_planner_note", {"title": "x", "date": "2026-02-30"})
        assert r.is_error and "not a real calendar date" in r.content[0].text
        r = await c.call_tool("canvas_list_assignments", {"course": ""})
        assert r.is_error


async def test_mark_done_calendar_event(fake):
    await call("canvas_mark_done", {"event_id": 501})
    assert fake.posted == [("/planner/overrides", {"plannable_type": "calendar_event", "plannable_id": 501, "marked_complete": True})]


async def test_what_if_grade_tool(fake):
    out = await call("canvas_what_if_grade", {"course": "3500", "target": "A-"})
    # Graded: HW 2 9/10; HW 1 closed+unsubmitted counts 0 -> HW 9/20. HW 6 comes from the calendar.
    assert "## To finish with A- (90%)" in out and "| A | 94% |" in out
    assert "HW 6 due (calendar)" in out and "closed work you never submitted is counted as 0" in out
    scenario = await call("canvas_what_if_grade", {"course": "3500", "what_if": {"Exams": 100, "HW 4": 100}})
    assert "everything left in Exams" in scenario and "HW 4: 100%" in scenario
    async with Client(server.mcp) as c:
        r = await c.call_tool("canvas_what_if_grade", {"course": "3500", "what_if": {"Nonexistent": 50}})
        assert r.is_error and "No assignment or category" in r.content[0].text


def test_compute_impacts_weighted_and_unweighted():
    a = [{"id": 1, "points_possible": 10, "assignment_group_id": 1}, {"id": 2, "points_possible": 30, "assignment_group_id": 1},
         {"id": 3, "points_possible": 100, "assignment_group_id": 2}]
    g = [{"id": 1, "name": "HW", "group_weight": 40}, {"id": 2, "name": "Final", "group_weight": 60}]
    w = compute_impacts(a, g + [{"id": 9, "name": "Exams (none posted yet)", "group_weight": 0}], weighted=True)
    assert w[1][0] == pytest.approx(10) and w[2][0] == pytest.approx(30) and w[3][0] == pytest.approx(60)
    u = compute_impacts(a, g, weighted=False)
    assert u[3][0] == pytest.approx(100 * 100 / 140)


def test_impact_counts_groups_with_nothing_posted():
    a = [{"id": 1, "points_possible": 30, "assignment_group_id": 1}]
    g = [{"id": 1, "name": "HW", "group_weight": 15}, {"id": 2, "name": "Exams", "group_weight": 85}]
    assert compute_impacts(a, g, weighted=True)[1][0] == pytest.approx(15)


def test_scoring_prefers_urgent_and_heavy():
    now = NOW
    soon_small = score_item(WorkItem(1, "C", 1, "quiz", "quiz", now + timedelta(hours=6), impact_pct=0.5), now)
    later_big = score_item(WorkItem(1, "C", 2, "exam", "quiz", now + timedelta(days=6), impact_pct=25), now)
    later_small = score_item(WorkItem(1, "C", 3, "hw", "assignment", now + timedelta(days=6), impact_pct=0.5), now)
    assert soon_small.score > later_small.score and later_big.score > later_small.score
    assert tier(soon_small, now) == "Do today"


def test_parse_dt_date_only_is_aware():
    from canvas_pilot.textutil import parse_dt

    assert parse_dt("2026-10-22").tzinfo is not None and parse_dt("garbage") is None


def test_html_to_text():
    assert html_to_text("<p>a&nbsp;b</p><script>x()</script><ul><li>one</li></ul>") == "a b\n\n- one"


async def test_short_command_prompts_reference_real_tools():
    import re

    async with Client(server.mcp) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        prompts = {p.name: p for p in (await c.list_prompts()).prompts}
        assert {"overview", "today", "week", "news", "due"} <= set(prompts)
        for name, args in [("overview", {}), ("overview", {"course": "3000"}), ("today", {}), ("week", {}),
                           ("news", {}), ("news", {"course": "3000", "days": "3"}), ("due", {}), ("due", {"course": "PHIL"})]:
            text = (await c.get_prompt(name, args)).messages[0].content.text
            used = set(re.findall(r"canvas_\w+", text))
            assert used and used <= tools, (name, used - tools)
            if args.get("course"):
                assert f'course="{args["course"]}"' in text
        assert "days_back=3" in (await c.get_prompt("news", {"days": "3"})).messages[0].content.text


# -- hidden courses -------------------------------------------------------------------


async def test_hidden_course_left_out_of_summaries_but_reachable_by_name(fake):
    assert "Hidden **Husky 101 Orientation**" in await call("canvas_hide_course", {"course": "Husky"})

    todo = await call("canvas_get_daily_todo", {"days_ahead": 7})
    assert "Orientation quiz" not in todo and "Bring ID card" not in todo
    assert "1 hidden course not included (Husky 101 Orientation)" in todo
    for tool, args, text in [
        ("canvas_get_upcoming_deadlines", {}, "Orientation quiz"),
        ("canvas_list_announcements", {"days_back": 7}, "Welcome week"),
        ("canvas_get_calendar_events", {}, "Orientation social"),
        ("canvas_get_grades", {}, "## Husky 101 Orientation"),
    ]:
        out = await call(tool, args)
        assert text not in out and "hidden course not included" in out, tool
        everything = await call(tool, {**args, "include_hidden": True})
        assert text in everything and "not included" not in everything, tool

    # Naming the course still works, and says it's hidden.
    listing = await call("canvas_list_assignments", {"course": "Husky", "status": "all"})
    assert "Orientation quiz" in listing and "(hidden course)" in listing
    news = await call("canvas_list_announcements", {"course": "orientation", "days_back": 7})
    assert "Welcome week" in news and "not included" not in news
    # Inbox messages are never dropped, just labelled.
    inbox = await call("canvas_list_inbox")
    assert "Welcome!" in inbox and "(hidden course)" in inbox


async def test_list_courses_hidden_section_and_unhide(fake):
    await call("canvas_hide_course", {"course": "404"})
    out = await call("canvas_list_courses")
    assert "2 active courses" in out and "**Hidden (1)**" in out and "Husky 101 Orientation — course_id=404" in out
    assert "already hidden" in await call("canvas_hide_course", {"course": "Husky"})
    assert "Unhid **Husky 101 Orientation**" in await call("canvas_unhide_course", {"course": "husky"})
    assert "isn't hidden" in await call("canvas_unhide_course", {"course": "Husky101"})
    await call("canvas_hide_course", {"course": "Husky"})
    await call("canvas_hide_course", {"course": "Algorithms"})
    assert "Unhid 2 courses" in await call("canvas_unhide_course", {"course": "all"})
    assert "Hidden (" not in await call("canvas_list_courses")


async def test_visible_course_wins_a_name_tie(fake):
    await call("canvas_hide_course", {"course": "Algorithms"})  # CS3000
    # "CS" matches CS3500 (visible) and CS3000 (hidden): the visible one wins instead of an error.
    assert "**Object-Oriented Design**" in await call("canvas_list_assignments", {"course": "CS", "status": "all"})
    # A name only the hidden course matches still finds it.
    assert "**Algorithms and Data** (hidden course)" in await call("canvas_list_assignments", {"course": "CS3000", "status": "all"})
