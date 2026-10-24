"""The student's hidden courses: left out of course-wide summaries, still reachable by name.

Stored in hidden_courses.json next to config.env:

    {"version": 1, "hidden": {"58605": "Khoury Orientation"}, "imported_legacy_ids": [58605]}

The file is re-read whenever it changes on disk, so hiding a course in one AI app takes effect in
every other running copy of the server without a restart.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class HiddenCourses:
    def __init__(self, path: Path, legacy_ids: frozenset[int] = frozenset(), legacy_file: Path | None = None):
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._hidden: dict[int, str] = {}
        self._imported: set[int] = set()
        self._import_legacy(legacy_ids, legacy_file)

    # -- reading -------------------------------------------------------------------

    def _load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._stamp, self._hidden, self._imported = None, {}, set()
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            data = {}  # a corrupt file shouldn't take the whole server down; the next write repairs it
        self._hidden = {int(k): str(v or "") for k, v in (data.get("hidden") or {}).items() if str(k).isdigit()}
        self._imported = {int(i) for i in data.get("imported_legacy_ids") or [] if str(i).isdigit()}
        self._stamp = stamp

    def ids(self) -> set[int]:
        self._load()
        return set(self._hidden)

    def items(self) -> dict[int, str]:
        """{course_id: name}; name may be "" for IDs imported from the old setting until first seen."""
        self._load()
        return dict(self._hidden)

    def __contains__(self, course_id: object) -> bool:
        return course_id in self.ids()

    # -- writing -------------------------------------------------------------------

    def _save(self) -> None:
        data = {
            "version": 1,
            "hidden": {str(k): v for k, v in sorted(self._hidden.items())},
            "imported_legacy_ids": sorted(self._imported),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a reader in another process never sees a half-written file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".hidden_courses.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)
        self._stamp = None  # force a re-stat on the next read

    def hide(self, course_id: int, name: str) -> bool:
        """Returns False if it was already hidden."""
        self._load()
        if course_id in self._hidden:
            if name and self._hidden[course_id] != name:
                self._hidden[course_id] = name
                self._save()
            return False
        self._hidden[course_id] = name
        self._save()
        return True

    def unhide(self, course_id: int) -> bool:
        """Returns False if it wasn't hidden."""
        self._load()
        if self._hidden.pop(course_id, None) is None:
            return False
        self._save()
        return True

    def unhide_all(self) -> int:
        self._load()
        n = len(self._hidden)
        if n:
            self._hidden.clear()
            self._save()
        return n

    def fill_names(self, names: dict[int, str]) -> None:
        """Record names for hidden IDs that don't have one yet (e.g. imported from the old setting)."""
        self._load()
        missing = {i: names[i] for i, n in self._hidden.items() if not n and names.get(i)}
        if missing:
            self._hidden.update(missing)
            self._save()

    # -- one-time move from CANVAS_EXCLUDE_COURSE_IDS --------------------------------

    def _import_legacy(self, legacy_ids: frozenset[int], legacy_file: Path | None) -> None:
        """Hide IDs from the old CANVAS_EXCLUDE_COURSE_IDS setting, once each.

        Remembering which IDs were imported means unhiding one sticks, even if the old setting is
        still present (for example in an MCP client's "env" block, which we can't edit).
        """
        self._load()
        new = set(legacy_ids) - self._imported
        if new:
            for i in new:
                self._hidden.setdefault(i, "")
            self._imported |= new
            self._save()
        if legacy_file and legacy_ids:
            try:
                has_line = any(
                    line.strip().startswith("CANVAS_EXCLUDE_COURSE_IDS=") for line in legacy_file.read_text().splitlines()
                )
            except OSError:
                has_line = False
            if has_line:
                from .config import write_config_values

                write_config_values(legacy_file, {"CANVAS_EXCLUDE_COURSE_IDS": None})
