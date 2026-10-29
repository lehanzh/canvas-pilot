"""What-if grade projection: the average needed on remaining work to reach a target grade.

final(x) is the course grade if the student scores the fraction x on everything not yet graded.

Weighted courses:  final = sum_g  w_g * kept_earned_g(x) / kept_possible_g(x)  /  sum_g w_g
                   A group with nothing posted yet (e.g. exams) contributes w_g * x.
Unweighted:        final = sum_g kept_earned_g(x) / sum_g kept_possible_g(x)

"kept" applies each group's drop rules the way Canvas does: drop_lowest removes the scores whose
removal gives the best group percentage, drop_highest the worst, never_drop items always count, and
at least one score is kept. Without drop rules final(x) is a straight line, so the needed average is
(T - final(0)) / (final(1) - final(0)); with them it's found by bisection (final is non-decreasing).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .textutil import norm_title

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


@dataclass(frozen=True)
class DropRules:
    lowest: int = 0
    highest: int = 0
    never_drop: frozenset[int] = frozenset()

    @classmethod
    def from_group(cls, group: dict) -> DropRules:
        r = group.get("rules") or {}
        return cls(
            lowest=max(int(r.get("drop_lowest") or 0), 0),
            highest=max(int(r.get("drop_highest") or 0), 0),
            never_drop=frozenset(int(i) for i in r.get("never_drop") or [] if str(i).isdigit()),
        )

    def __bool__(self) -> bool:
        return bool(self.lowest or self.highest)

    def describe(self) -> str:
        parts = []
        if self.lowest:
            parts.append(f"drops lowest {self.lowest}")
        if self.highest:
            parts.append(f"drops highest {self.highest}")
        return ", ".join(parts)


def _ratio(pairs: list[tuple[float, float]]) -> float:
    possible = sum(p for _, p in pairs)
    return sum(e for e, _ in pairs) / possible if possible else 0.0


def _drop(cands: list[tuple[float, float]], n: int, fixed: list[tuple[float, float]], best: bool) -> list[tuple[float, float]]:
    """Keep len(cands) - n candidates so the kept ratio (with `fixed`) is maximal (best) or minimal.

    Dinkelbach iteration: rank by earned - q * possible at the current ratio q, keep the top (or
    bottom) ones, recompute q, repeat until stable. Exact for this problem and fast for course sizes.
    """
    if n <= 0 or not cands:
        return cands
    keep = len(cands) - n
    q = _ratio(cands + fixed)
    kept = cands
    for _ in range(100):
        kept = sorted(cands, key=lambda c: c[0] - q * c[1], reverse=best)[:keep]
        new_q = _ratio(kept + fixed)
        if abs(new_q - q) < 1e-12:
            break
        q = new_q
    return kept


def apply_drops(scored: list[tuple[float, float, int | None]], rules: DropRules) -> list[tuple[float, float]]:
    """(earned, possible, assignment_id) -> the (earned, possible) pairs Canvas would count."""
    fixed = [(e, p) for e, p, i in scored if i is not None and i in rules.never_drop]
    cands = [(e, p) for e, p, i in scored if i is None or i not in rules.never_drop]
    min_kept = 0 if fixed else 1  # Canvas never drops every score in a group
    lowest = min(rules.lowest, max(len(cands) - min_kept, 0))
    cands = _drop(cands, lowest, fixed, best=True)
    highest = min(rules.highest, max(len(cands) - min_kept, 0))
    cands = _drop(cands, highest, fixed, best=False)
    return cands + fixed


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
    rules: DropRules = field(default_factory=DropRules)
    items: list[GradeItem] = field(default_factory=list)


@dataclass
class Projection:
    weighted: bool
    current: float | None  # graded work only, Canvas-style (renormalised)
    base: float  # final % if you score 0 on everything left
    slope: float  # final(100%) - final(0%)
    groups: list[GroupSummary]
    final_at: Callable[[float], float] | None = None  # final % for a uniform fraction x on what's left
    has_drops: bool = False

    @property
    def ceiling(self) -> float:
        return self.base + self.slope

    @property
    def drop_notes(self) -> list[str]:
        return [f"{g.name} {g.rules.describe()}" for g in self.groups if g.rules]

    def needed(self, target: float) -> float | None:
        """Average (0-100+) needed on remaining work, or None if nothing is left to earn."""
        if self.slope <= 1e-9:
            return None
        if not self.has_drops or self.final_at is None or target <= self.base:
            return 100.0 * (target - self.base) / self.slope  # exact while final(x) is a straight line
        lo, hi = 0.0, 1.0
        while self.final_at(hi) < target and hi < 64:
            lo, hi = hi, hi * 2
        if self.final_at(hi) < target:
            return 100.0 * hi  # far out of reach; report it as such
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self.final_at(mid) < target else (lo, mid)
        return 100.0 * hi


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
        g.get("id"): GroupSummary(g.get("id"), g.get("name") or "?", g.get("group_weight") or 0.0, rules=DropRules.from_group(g))
        for g in groups
    }
    for it in items:
        if it.points <= 0:
            continue
        s = summaries.setdefault(it.group_id, GroupSummary(it.group_id, (by_id.get(it.group_id) or {}).get("name", "Other"), 0.0))
        s.items.append(it)
        if it.score is None:
            s.remaining += it.points
            s.remaining_items.append(it)
        else:
            s.earned += it.score
            s.graded += it.points

    groups_out = list(summaries.values())
    total_weight = sum(s.weight or 0 for s in groups_out)
    use_weights = weighted and total_weight > 0

    def kept(s: GroupSummary, x: float | None) -> tuple[float, float]:
        """(earned, possible) Canvas would count; x=None means graded work only (the current grade)."""
        scored = [
            (it.score if it.score is not None else x * it.points, it.points, it.item_id)
            for it in s.items
            if it.score is not None or x is not None
        ]
        pairs = apply_drops(scored, s.rules) if s.rules else [(e, p) for e, p, _ in scored]
        return sum(e for e, _ in pairs), sum(p for _, p in pairs)

    def final_at(x: float) -> float:
        if use_weights:
            total = 0.0
            for s in groups_out:
                if not s.weight:
                    continue
                share = 100.0 * s.weight / total_weight
                if s.items:
                    e, p = kept(s, x)
                    total += share * (e / p if p else 0.0)
                else:
                    total += share * fixed_empty.get(s.group_id, x)  # nothing posted yet
            return total
        sums = [kept(s, x) for s in groups_out if s.items]
        possible = sum(p for _, p in sums)
        return 100.0 * sum(e for e, _ in sums) / possible if possible else 0.0

    if use_weights:
        for s in groups_out:
            if not s.weight:
                continue
            share = 100.0 * s.weight / total_weight
            posted = s.graded + s.remaining
            if posted:
                s.share_of_final_left = share * s.remaining / posted
            elif s.group_id not in fixed_empty:
                s.share_of_final_left = share
        graded_groups = [(s, kept(s, None)) for s in groups_out if s.weight and s.graded]
        gw = sum(s.weight for s, _ in graded_groups)
        current = 100.0 * sum(s.weight * e / p for s, (e, p) in graded_groups if p) / gw if gw else None
    else:
        total = sum(s.graded + s.remaining for s in groups_out)
        for s in groups_out:
            s.share_of_final_left = 100.0 * s.remaining / total if total else 0.0
        graded_sums = [kept(s, None) for s in groups_out if s.graded]
        graded_possible = sum(p for _, p in graded_sums)
        current = 100.0 * sum(e for e, _ in graded_sums) / graded_possible if graded_possible else None

    base, ceiling = final_at(0.0), final_at(1.0)
    has_drops = any(s.rules and s.items for s in groups_out)
    return Projection(
        weighted=use_weights,
        current=current,
        base=base,
        slope=ceiling - base,
        groups=groups_out,
        final_at=final_at,
        has_drops=has_drops,
    )


# One "name=score" pair. Names may contain commas or colons; the score must be a number, which is what ends a name.
_WHAT_IF_PAIR = re.compile(r"\s*(?P<key>[^;\n]+?)\s*[=:]\s*(?P<pct>\d+(?:\.\d+)?)\s*%?\s*(?:[;,\n]|$)")


def parse_what_if(text: str | None) -> dict[str, float]:
    """Parse "Homework-3=85; Midterm Exams=75" into {name: percent}.

    A string instead of an object parameter: free-form maps are the JSON Schema shape that
    strict clients (Gemini, OpenAI strict mode) reject or drop.
    """
    pairs: dict[str, float] = {}
    text = (text or "").strip()
    pos = 0
    while pos < len(text):
        m = _WHAT_IF_PAIR.match(text, pos)
        if not m:
            raise ValueError(
                f"Couldn't read what_if near {text[pos:][:60]!r}. "
                'Use "name=score" pairs separated by semicolons, e.g. "Homework-3=85; Midterm Exams=75".'
            )
        pairs[m["key"]] = float(m["pct"])
        pos = m.end()
    return pairs


def _match_item(key: str, items: list[GradeItem]) -> GradeItem:
    k = key.strip()
    if k.isdigit():
        hits = [i for i in items if i.item_id == int(k)]
    else:
        kn = norm_title(k)
        hits = [i for i in items if kn and kn in norm_title(i.name)]
        exact = [i for i in hits if norm_title(i.name) == kn]
        hits = exact or hits
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(
            f"No assignment or category matches {key!r} for the what-if. "
            "Use canvas_list_assignments for assignment names or canvas_get_grades for categories."
        )
    raise ValueError(f"{key!r} matches several assignments: " + ", ".join(i.name for i in hits[:10]) + ". Be more specific.")


def _has_item_match(key: str, items: list[GradeItem]) -> bool:
    try:
        _match_item(key, items)
        return True
    except ValueError as e:
        return "several" in str(e)  # ambiguous item names still mean the user meant assignments


def _match_group(key: str, groups: list[dict]) -> dict | None:
    kn = norm_title(key)
    hits = [g for g in groups if kn and kn in norm_title(g.get("name") or "")]
    exact = [g for g in hits if norm_title(g.get("name") or "") == kn]
    hits = exact or hits
    return hits[0] if len(hits) == 1 else None


def apply_what_ifs(
    what_ifs: dict[str, float], items: list[GradeItem], groups: list[dict], weighted: bool
) -> tuple[list[str], dict[int | None, float]]:
    """Set hypothetical percentages on items, in place. A key names an assignment (or part of one), an
    assignment ID, or a whole category: everything left in it, or a weighted category with nothing posted.

    Returns a line describing each what-if, and the fixed_empty map for project(). Raises ValueError.
    """
    applied: list[str] = []
    fixed_empty: dict[int | None, float] = {}
    for key, pct in what_ifs.items():
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
                raise ValueError(f"{group['name']} has nothing left to score, so a what-if there changes nothing.")
            continue
        it = _match_item(key, items)
        it.original_score, it.score, it.source = it.score, it.points * pct / 100.0, "what-if"
        was = f" (actual {it.original_score:g}/{it.points:g})" if it.original_score is not None else ""
        applied.append(f"- {it.name}: {pct:g}% → {it.score:g}/{it.points:g}{was}")
    return applied, fixed_empty


def describe_need(needed: float | None) -> str:
    if needed is None:
        return "nothing left to earn"
    if needed <= 0:
        return "already secured"
    if needed > 100:
        return f"not reachable ({needed:.0f}% needed)"
    return f"{needed:.1f}%"
