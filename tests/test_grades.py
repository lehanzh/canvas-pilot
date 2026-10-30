"""What-if projection math (pure functions) and the canvas_what_if_grade tool end to end."""

from __future__ import annotations

import pytest

from canvas_pilot.grades import DEFAULT_SCHEME, GradeItem, describe_need, parse_scheme, parse_target, project

GROUPS = [{"id": 1, "name": "HW", "group_weight": 30}, {"id": 2, "name": "Exams", "group_weight": 70}]


def test_weighted_with_unposted_group():
    # HW: 18/20 graded, 20 pts left; Exams: nothing posted (70% of the grade still open).
    items = [GradeItem(1, "HW1", 1, 20, 18), GradeItem(2, "HW2", 1, 20, None)]
    p = project(items, GROUPS, weighted=True)
    assert p.current == pytest.approx(90)
    assert p.base == pytest.approx(30 * 18 / 40)  # 13.5
    assert p.slope == pytest.approx(15 + 70)
    # Needed average x: 13.5 + 85x = 90  ->  x = 90%
    assert p.needed(90) == pytest.approx(100 * (90 - 13.5) / 85)


def test_fixed_empty_group_what_if():
    items = [GradeItem(1, "HW1", 1, 20, 20)]
    p = project(items, GROUPS, weighted=True, fixed_empty={2: 0.8})
    assert p.base == pytest.approx(30 + 56) and p.slope == pytest.approx(0)
    assert p.needed(90) is None and describe_need(None) == "nothing left to earn"


def test_unweighted_points():
    items = [GradeItem(1, "A1", 1, 100, 90), GradeItem(2, "A2", 1, 100, None), GradeItem(3, "Missed", 1, 50, 0, "missed")]
    p = project(items, GROUPS, weighted=False)
    assert p.current == pytest.approx(60)  # 90/150 graded (missed counts as 0)
    assert p.base == pytest.approx(36) and p.slope == pytest.approx(40)
    assert describe_need(p.needed(94)) == "not reachable (145% needed)"
    assert describe_need(p.needed(30)) == "already secured"


def test_scheme_and_target_parsing():
    scheme = parse_scheme([["A", 0.94], ["B", 0.84], ["F", 0.0]])
    assert scheme[0] == ("A", 94.0)
    assert parse_target("b", scheme) == ("B", 84.0)
    assert parse_target("91.5%", scheme) == ("91.5%", 91.5)
    assert parse_scheme(None) == DEFAULT_SCHEME
    with pytest.raises(ValueError):
        parse_target("Z", scheme)


# -- drop rules -------------------------------------------------------------------

from canvas_pilot.grades import DropRules, apply_drops  # noqa: E402


def test_drop_lowest_keeps_best_percentage_not_lowest_raw_score():
    # Dropping the 20% item leaves 60%; dropping the 30% item leaves 83.6%. Canvas keeps the best result.
    kept = apply_drops([(2, 10, 1), (30, 100, 2), (90, 100, 3)], DropRules(lowest=1))
    assert sorted(kept) == [(2, 10), (90, 100)]


def test_never_drop_highest_and_keep_at_least_one():
    scored = [(2, 10, 1), (5, 10, 2), (10, 10, 3)]
    assert sorted(apply_drops(scored, DropRules(lowest=1, never_drop=frozenset({1})))) == [(2, 10), (10, 10)]
    assert sorted(apply_drops(scored, DropRules(highest=1))) == [(2, 10), (5, 10)]
    assert apply_drops(scored, DropRules(lowest=5)) == [(10, 10)]  # never drops every score


def test_projection_with_drop_rule():
    groups = [
        {"id": 1, "name": "HW", "group_weight": 40, "rules": {"drop_lowest": 1}},
        {"id": 2, "name": "Exams", "group_weight": 60},
    ]
    items = [GradeItem(1, "HW1", 1, 10, 10), GradeItem(2, "HW2", 1, 10, 0), GradeItem(3, "HW3", 1, 10, None)]
    p = project(items, groups, weighted=True)
    assert p.current == pytest.approx(100)  # the 0 is dropped, as Canvas shows it
    # final(x) = 40 * (10 + 10x) / 20 + 60x = 20 + 80x
    assert p.base == pytest.approx(20) and p.ceiling == pytest.approx(100)
    assert p.needed(90) == pytest.approx(87.5, abs=1e-6)
    assert p.drop_notes == ["HW drops lowest 1"]

    no_drop = project(items, [{**groups[0], "rules": {}}, groups[1]], weighted=True)
    assert describe_need(no_drop.needed(90)).startswith("not reachable")  # the rule is what makes it possible
