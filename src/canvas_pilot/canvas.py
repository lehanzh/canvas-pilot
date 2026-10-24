"""Thin async client for the Canvas LMS REST API (https://canvas.instructure.com/doc/api/)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

import httpx

from .config import Settings, config_dir, config_file
from .hidden import HiddenCourses

T = TypeVar("T")
R = TypeVar("R")

CACHE_TTL_SECONDS = 120
MAX_PAGES = 50
MAX_CONCURRENCY = 6


class CanvasError(Exception):
    """A Canvas request failed; the message is written for the calling agent."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CanvasClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {settings.token}", "Accept": "application/json"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            transport=transport,
        )
        self.hidden = HiddenCourses(
            settings.hidden_courses_file or config_dir() / "hidden_courses.json",
            legacy_ids=settings.exclude_course_ids,
            legacy_file=None if settings.hidden_courses_file else config_file(),
        )
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._generation = 0  # bumped on writes so in-flight GETs don't re-cache stale data
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- low level ---------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with self._sem:
            for attempt in range(3):
                try:
                    resp = await self._http.request(method, url, **kwargs)
                except httpx.TimeoutException as e:
                    if attempt == 2:
                        raise CanvasError("Canvas timed out. Try again, or narrow the request.") from e
                    continue
                except httpx.HTTPError as e:
                    raise CanvasError(f"Could not reach Canvas at {self.settings.base_url}: {e}") from e
                # Canvas signals throttling with 403 + this body, or 429.
                throttled = resp.status_code == 429 or (
                    resp.status_code == 403 and "Rate Limit Exceeded" in resp.text
                )
                if throttled and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
        if resp.is_success:
            return resp
        if throttled:
            raise CanvasError("Canvas is rate-limiting requests right now. Wait a minute and try again.", resp.status_code)
        raise self._error_for(resp)

    def _json(self, resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as e:
            raise CanvasError(
                f"Canvas returned a web page instead of API data ({resp.url.host}). Check that CANVAS_BASE_URL is "
                "your Canvas address (e.g. https://myschool.instructure.com), not a login or portal page."
            ) from e

    def _error_for(self, resp: httpx.Response) -> CanvasError:
        status = resp.status_code
        path = resp.request.url.path.removeprefix("/api/v1")
        if status == 401:
            msg = (
                "Canvas rejected the access token (401). It may be expired or revoked; generate a new one at "
                f"{self.settings.base_url}/profile/settings and run `canvas-pilot set-token`."
            )
        elif status == 403:
            msg = f"Canvas denied access to {path} (403). The instructor may have disabled this feature for students."
        elif status == 404:
            msg = f"Not found: {path} (404). Check the IDs; use canvas_list_courses to see valid course IDs."
        else:
            msg = f"Canvas returned HTTP {status} for {path}: {resp.text[:300]}"
        return CanvasError(msg, status)

    async def get(self, path: str, params: dict[str, Any] | None = None, *, cache: bool = True) -> Any:
        key = ("get", path, _freeze(params))
        if cache and (hit := self._cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        gen = self._generation
        data = self._json(await self._request("GET", path, params=params))
        if cache and gen == self._generation:
            self._cache[key] = (time.monotonic(), data)
        return data

    async def get_all(self, path: str, params: dict[str, Any] | None = None, *, cache: bool = True) -> list[Any]:
        """GET every page of a paginated list endpoint (Canvas uses RFC 5988 Link headers)."""
        params = {"per_page": 100, **(params or {})}
        key = ("get_all", path, _freeze(params))
        if cache and (hit := self._cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        gen = self._generation
        items: list[Any] = []
        resp = await self._request("GET", path, params=params)
        for _ in range(MAX_PAGES):
            page = self._json(resp)
            # A few endpoints wrap lists in an object; unwrap the first list value.
            if isinstance(page, dict):
                page = next((v for v in page.values() if isinstance(v, list)), [])
            items.extend(page)
            next_url = resp.links.get("next", {}).get("url")
            if not next_url:
                break
            resp = await self._request("GET", next_url)
        if cache and gen == self._generation:
            self._cache[key] = (time.monotonic(), items)
        return items

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        return await self._write("POST", path, data)

    async def put(self, path: str, data: dict[str, Any]) -> Any:
        return await self._write("PUT", path, data)

    async def _write(self, method: str, path: str, data: dict[str, Any]) -> Any:
        resp = await self._request(method, path, json=data)
        self._generation += 1
        self._cache.clear()
        return self._json(resp)

    # -- domain helpers ----------------------------------------------------

    async def all_courses(self) -> list[dict]:
        """Every active course the student can open, hidden ones included."""
        courses = await self.get_all(
            "/courses",
            {"enrollment_state": "active", "include[]": ["total_scores", "term"]},
        )
        # Courses the user can see but not open come back without a name.
        courses = [c for c in courses if c.get("name")]
        self.hidden.fill_names({c["id"]: c["name"] for c in courses})
        return courses

    async def courses(self, include_hidden: bool = False) -> list[dict]:
        """Courses for course-wide views: hidden courses are left out unless include_hidden."""
        courses = await self.all_courses()
        if include_hidden:
            return courses
        hidden = self.hidden.ids()
        return [c for c in courses if c["id"] not in hidden]

    def is_hidden(self, course_id: int | None) -> bool:
        return course_id in self.hidden

    async def resolve_course(self, query: str | int) -> dict:
        """Find a course by numeric ID, course code, or a fragment of its name (case-insensitive).

        Hidden courses are found too, but visible ones win a tie, so hiding a course never makes an
        everyday question ambiguous.
        """
        courses = await self.all_courses()
        hidden = self.hidden.ids()
        q = str(query).strip()
        if q.isdigit():
            for c in courses:
                if c["id"] == int(q):
                    return c
        ql = q.lower().replace(" ", "")
        matches = [
            c
            for c in courses
            if ql in c["name"].lower().replace(" ", "") or ql in (c.get("course_code") or "").lower().replace(" ", "")
        ]
        visible = [c for c in matches if c["id"] not in hidden]
        for group in (visible, [c for c in matches if c["id"] in hidden]):
            if len(group) == 1:
                return group[0]
            if len(group) > 1:
                names = ", ".join(f"{c['name']} (id {c['id']})" for c in group)
                raise CanvasError(f"{q!r} matches several courses: {names}. Pass the course ID instead.")
        if q.isdigit():
            # A number that isn't a course code: maybe the ID of a past course; fetch it directly.
            return await self.get(f"/courses/{q}", {"include[]": ["total_scores", "term"]})
        names = ", ".join(f"{c['name']} (id {c['id']})" + (" [hidden]" if c["id"] in hidden else "") for c in courses)
        raise CanvasError(f"No active course matches {q!r}. Active courses: {names}")

    async def assignments(self, course_id: int) -> list[dict]:
        return await self.get_all(
            f"/courses/{course_id}/assignments",
            {"include[]": ["submission"], "order_by": "due_at"},
        )

    async def assignment_groups(self, course_id: int) -> list[dict]:
        return await self.get_all(f"/courses/{course_id}/assignment_groups")

    async def planner_items(self, start_iso: str, end_iso: str) -> list[dict]:
        return await self.get_all("/planner/items", {"start_date": start_iso, "end_date": end_iso})

    async def planner_done_keys(self) -> set[tuple[str, int]]:
        """(plannable_type, id) of everything the student checked off in their planner, at any date."""
        overrides = await self.get_all("/planner/overrides")
        return {(o["plannable_type"], o["plannable_id"]) for o in overrides if o.get("marked_complete")}

    async def gather_per_course(
        self, courses: Iterable[dict], fn: Callable[[dict], Awaitable[R]]
    ) -> list[tuple[dict, R | CanvasError]]:
        """Run fn for each course concurrently; per-course failures are returned, not raised."""
        courses = list(courses)

        async def one(c: dict) -> R | CanvasError:
            try:
                return await fn(c)
            except CanvasError as e:
                return e

        results = await asyncio.gather(*(one(c) for c in courses))
        return list(zip(courses, results))


def _freeze(params: dict[str, Any] | None) -> tuple:
    if not params:
        return ()
    return tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in params.items()))
