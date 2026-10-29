"""Course content tools: announcements, syllabus, modules, pages, files and the inbox."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field

from ..canvas import CanvasError
from ..server import client, tool
from ..textutil import fence, fmt_dt, html_to_text
from .common import (
    CourseArg,
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
    window = {"start_date": (now - timedelta(days=days_back)).date().isoformat(), "end_date": (now + timedelta(days=1)).date().isoformat()}
    pages = await asyncio.gather(
        *(c.get_all("/announcements", {**window, "context_codes[]": chunk}) for chunk in _context_chunks(list(by_code)))
    )
    anns = [a for page in pages for a in page]
    anns.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
    if not anns:
        return f"No announcements in the last {days_back} days." + footer
    tz = _tz()
    out = [f"{len(anns)} announcements in the last {days_back} days" + (f" (showing {limit})" if len(anns) > limit else "") + ":"]
    for x in anns[:limit]:
        crs = by_code.get(x.get("context_code"), {})
        unread = " · **unread**" if x.get("read_state") == "unread" else ""
        author = (x.get("author") or {}).get("display_name") or x.get("user_name") or "?"
        # The title is inside the fence too: whoever posts the announcement writes it.
        text = f"Title: {x.get('title') or '(untitled)'}\n\n" + html_to_text(x.get("message"), None if full_text else 600)
        out.append(
            f"\n### {(_label(crs) + _hidden_tag(crs)) if crs else x.get('context_code')} · {author} · "
            f"{fmt_dt(x.get('posted_at'), tz, now)}{unread} · {x.get('html_url', '')}\n"
            + fence(text, f"announcement by {author}")
        )
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
    return f"# Syllabus — {crs['name']}{_hidden_tag(crs)}\n\n" + fence(text, "course syllabus")


@tool("List course modules")
async def canvas_list_modules(
    course: CourseArg,
    search: Annotated[str | None, Field(description="Only show items whose title contains this text.")] = None,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of module items to list.")] = 150,
) -> str:
    """Course modules and their items (readings, lectures, pages, files, assignments) with links and due dates."""
    c = client()
    crs = await c.resolve_course(course)
    modules, assignments = await asyncio.gather(
        c.get_all(f"/courses/{crs['id']}/modules", {"include[]": ["items", "content_details"]}),
        # Module Quiz items carry the quiz ID; map it to the assignment ID the other tools take.
        _or_default(c.assignments(crs["id"]), []),
    )
    quiz_to_assignment = {a["quiz_id"]: a["id"] for a in assignments if a.get("quiz_id")}
    # Canvas omits items for large modules; fetch those separately, all at once.
    missing = [m for m in modules if m.get("items") is None]
    fetched = await asyncio.gather(
        *(c.get_all(f"/courses/{crs['id']}/modules/{m['id']}/items", {"include[]": ["content_details"]}) for m in missing)
    )
    item_lists = {m["id"]: items for m, items in zip(missing, fetched)}
    tz = _tz()
    out = [f"# Modules — {crs['name']}{_hidden_tag(crs)}"]
    needle = (search or "").lower()
    shown = skipped = 0
    for m in modules:
        items = m.get("items") if m.get("items") is not None else item_lists[m["id"]]
        if needle:
            items = [i for i in items if needle in (i.get("title") or "").lower()]
            if not items:
                continue
        if shown >= limit:
            skipped += len(items)
            continue
        state = f" ({m['state']})" if m.get("state") and m["state"] != "unlocked" else ""
        out.append(f"\n## {m.get('name')}{state}")
        for n, i in enumerate(items):
            if shown >= limit:
                skipped += len(items) - n
                break
            shown += 1
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
    if skipped:
        out.append(f"\n… {skipped} more items (raise limit, or narrow with search=)")
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
    # Escaped so a crafted value ("../../users/self") can't turn this into a read of another endpoint.
    try:
        data = await c.get(f"{base}/{quote(page.strip(), safe='')}")
    except CanvasError as e:
        if e.status != 404:
            raise
        hits = await c.get_all(base, {"search_term": page.strip(), "sort": "title"})
        if not hits:
            raise CanvasError(f"No page matching {page!r} in {crs['name']}. Use canvas_list_modules to find page= values.", 404)
        if len(hits) > 1:
            names = "\n".join(f"- {h.get('title')}  `page={h.get('url')}`" for h in hits[:25])
            return f"{len(hits)} pages match {page!r} in {crs['name']}; pass one of these page= values:\n{names}"
        data = await c.get(f"{base}/{quote(hits[0]['url'], safe='')}")
    if data.get("locked_for_user"):
        return f"**{data.get('title')}** is locked: {data.get('lock_explanation') or 'not available yet'}"
    body = fence(html_to_text(data.get("body"), max_chars), "course page") or "_This page is empty._"
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
        # Anyone in a course can message the student, so the subject is fenced along with the message.
        text = f"Subject: {cv.get('subject') or '(no subject)'}\n\n{cv.get('last_message') or ''}"
        out.append(
            f"\n### {cv.get('context_name') or 'Message'}{hidden} · {who} · "
            f"{fmt_dt(cv.get('last_message_at'), tz, now)} · {cv.get('message_count', '?')} messages\n"
            + fence(text, "latest message")
        )
    return "\n".join(out)
