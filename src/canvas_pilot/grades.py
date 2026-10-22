"""What-if grade projection: the average needed on remaining work to reach a target grade.

The final grade is linear in x, the (uniform) fraction scored on everything not yet graded:

    final(x) = base + slope * x

Weighted courses:  final = sum_g  w_g * (earned_g + x * remaining_g) / (graded_g + remaining_g)  /  sum_g w_g
                   A group with nothing posted yet (e.g. exams) contributes w_g * x.
Unweighted:        final = (earned + x * remaining) / (graded + remaining)

so the average needed for a target T is x = (T - base) / slope.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canvas's built-in scheme, used when a course doesn't expose its own.
DEFAULT_SCHEME: list[tuple[str, float]] = [
    ("A", 94), ("A-", 90), ("B+", 87), ("B", 84), ("B-", 80), ("C+", 77),
    ("C", 74), ("C-", 70), ("D+", 67), ("D", 64), ("D-", 61), ("F", 0),
]


@dataclass
class GradeItem:
    item_id: int | None
    name: str
    group_id: int | None
    points: float
    score: float | None  # None = not graded yet
    source: str = "canvas"  # canvas | calendar | what-if | missed
    original_score: float | None = None


@dataclass
class GroupSummary:
    group_id: int | None
    name: str
    weight: float | None
    earned: float = 0.0
    graded: float = 0.0
    remaining: float = 0.0
    remaining_items: list[GradeItem] = field(default_factory=list)
    share_of_final_left: float = 0.0  # percentage points of the final grade still open in this group


@dataclass
class Projection:
    weighted: bool
    current: float | None  # graded work only, Canvas-style (renormalised)
    base: float  # final % if you score 0 on everything left
    slope: float  # extra % per unit of x
    groups: list[GroupSummary]

    @property
    def ceiling(self) -> float:
        return self.base + self.slope

    def needed(self, target: float) -> float | None:
        """Average (0-100+) needed on remaining work, or None if nothing is left to earn."""
        if self.slope <= 1e-9:
            return None
        return 100.0 * (target - self.base) / self.slope


def parse_scheme(raw) -> list[tuple[str, float]]:
    """Canvas grading_scheme ([["A", 0.94], ...] or [{"name": "A", "value": 0.94}, ...]) -> [("A", 94.0), ...]."""
    out = []
    for row in raw or []:
        name, value = (row.get("name"), row.get("value")) if isinstance(row, dict) else (row[0], row[1])
        if name is not None and value is not None:
            out.append((str(name), float(value) * 100 if float(value) <= 1 else float(value)))
    return sorted(out, key=lambda r: -r[1]) or DEFAULT_SCHEME


def parse_target(target: str, scheme: list[tuple[str, float]]) -> tuple[str, float]:
    t = target.strip().rstrip("%").strip()
    for name, cutoff in scheme:
        if name.lower() == t.lower():
            return name, cutoff
    try:
        value = float(t)
    except ValueError:
        letters = ", ".join(n for n, _ in scheme)
        raise ValueError(f"Target {target!r} is neither a percentage nor a grade in this course's scheme ({letters}).")
    return f"{value:g}%", value


def project(
    items: list[GradeItem], groups: list[dict], weighted: bool, fixed_empty: dict[int | None, float] | None = None
) -> Projection:
    """fixed_empty: hypothetical score fraction for weighted groups with nothing posted yet (e.g. {exams_id: 0.8})."""
    fixed_empty = fixed_empty or {}
    by_id = {g.get("id"): g for g in groups}
    summaries: dict[int | None, GroupSummary] = {
        g.get("id"): GroupSummary(g.get("id"), g.get("name") or "?", g.get("group_weight") or 0.0) for g in groups
    }
    for it in items:
        if it.points <= 0:
            continue
        s = summaries.setdefault(it.group_id, GroupSummary(it.group_id, (by_id.get(it.group_id) or {}).get("name", "Other"), 0.0))
        if it.score is None:
            s.remaining += it.points
            s.remaining_items.append(it)
        else:
            s.earned += it.score
            s.graded += it.points

    groups_out = list(summaries.values())
    total_weight = sum(s.weight or 0 for s in groups_out)
    if weighted and total_weight > 0:
        base = slope = 0.0
        for s in groups_out:
            if not s.weight:
                continue
            share = 100.0 * s.weight / total_weight
            posted = s.graded + s.remaining
            if posted:
                base += share * s.earned / posted
                s.share_of_final_left = share * s.remaining / posted
            elif s.group_id in fixed_empty:
                base += share * fixed_empty[s.group_id]  # what-if score for a category with nothing posted
            else:
                s.share_of_final_left = share  # nothing posted yet: all of it is still open
            slope += s.share_of_final_left
        graded_groups = [s for s in groups_out if s.weight and s.graded]
        gw = sum(s.weight for s in graded_groups)
        current = 100.0 * sum(s.weight * s.earned / s.graded for s in graded_groups) / gw if gw else None
    else:
        weighted = False
        earned = sum(s.earned for s in groups_out)
        graded = sum(s.graded for s in groups_out)
        remaining = sum(s.remaining for s in groups_out)
        total = graded + remaining
        base = 100.0 * earned / total if total else 0.0
        slope = 100.0 * remaining / total if total else 0.0
        for s in groups_out:
            s.share_of_final_left = 100.0 * s.remaining / total if total else 0.0
        current = 100.0 * earned / graded if graded else None
    return Projection(weighted=weighted, current=current, base=base, slope=slope, groups=groups_out)


def describe_need(needed: float | None) -> str:
    if needed is None:
        return "nothing left to earn"
    if needed <= 0:
        return "already secured"
    if needed > 100:
        return f"not reachable ({needed:.0f}% needed)"
    return f"{needed:.1f}%"
