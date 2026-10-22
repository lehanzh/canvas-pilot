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
