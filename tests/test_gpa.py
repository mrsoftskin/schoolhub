"""GPA derivation: the 4.0 scale, and what it refuses to invent."""
import pytest

from brain.grades import DEFAULT_CREDITS, letter_for, gpa_summary


def course(code, pct, **extra):
    summary = {"current_pct": pct, **extra}
    return {"course": code, "items": [], "summary": summary}


# ------------------------------------------------------------ the scale

@pytest.mark.parametrize(
    "pct,letter,points",
    [
        (100, "A", 4.0),
        (93, "A", 4.0),        # boundary: inclusive floor
        (92.9, "A-", 3.7),
        (90, "A-", 3.7),
        (89.9, "B+", 3.3),
        (87, "B+", 3.3),
        (83, "B", 3.0),
        (80, "B-", 2.7),
        (79.9, "C+", 2.3),
        (77, "C+", 2.3),
        (73, "C", 2.0),
        (70, "C-", 1.7),
        (67, "D+", 1.3),
        (63, "D", 1.0),
        (61, "D-", 0.7),
        (60.9, "F", 0.0),      # below the lowest floor
        (0, "F", 0.0),
    ],
)
def test_letter_boundaries(pct, letter, points):
    assert letter_for(pct) == (letter, points)


def test_no_grade_yields_no_letter():
    """Nothing graded must not become an F - that would be a projection."""
    assert letter_for(None) == (None, None)


@pytest.mark.parametrize("junk", ["", "n/a", object()])
def test_unparseable_pct_is_not_a_letter(junk):
    assert letter_for(junk) == (None, None)


# -------------------------------------------------------------- the GPA

def test_gpa_excludes_ungraded_courses_rather_than_zeroing_them():
    """The shape that matters: some courses have no grade yet."""
    courses = [
        course("COURSE1", 95.0),   # A  4.0
        course("COURSE2", 80),     # B- 2.7
        course("COURSE3", 75.0),   # C  2.0
        course("COURSE4", None),   # excluded
        course("COURSE5", None),   # excluded
    ]
    s = gpa_summary(courses)
    assert s["courses_counted"] == 3
    assert s["courses_total"] == 5
    assert s["credits_counted"] == 3 * DEFAULT_CREDITS
    # (4.0 + 2.7 + 2.0) / 3 == 2.90, NOT (4.0+2.7+2.0+0+0)/5 == 1.74
    assert s["gpa"] == 2.9


def test_gpa_is_none_when_nothing_is_graded_anywhere():
    s = gpa_summary([course("A", None), course("B", None)])
    assert s["gpa"] is None
    assert s["courses_counted"] == 0
    assert s["courses_total"] == 2


def test_gpa_of_empty_course_list():
    s = gpa_summary([])
    assert s["gpa"] is None
    assert s["credits_counted"] == 0
    assert s["rows"] == []


def test_credit_weighting_moves_the_average():
    """Equal credits average plainly; unequal credits weight."""
    courses = [course("LANG", 100), course("FIN", 80)]   # 4.0 and 2.7
    assert gpa_summary(courses)["gpa"] == 3.35           # plain mean

    weighted = gpa_summary(courses, credits={"LANG": 4, "FIN": 3})
    # (4.0*4 + 2.7*3) / 7 == 3.4428...
    assert weighted["gpa"] == 3.44
    assert weighted["credits_counted"] == 7


def test_rows_carry_the_footing_for_each_course():
    s = gpa_summary([course("COURSE1", 95.0), course("COURSE4", None)])
    by = {r["course"]: r for r in s["rows"]}
    assert by["COURSE1"]["letter"] == "A"
    assert by["COURSE1"]["counted"] is True
    assert by["COURSE4"]["letter"] is None
    assert by["COURSE4"]["counted"] is False
    # every course still appears, so the UI can show what is missing
    assert len(s["rows"]) == 2


def test_summary_without_a_summary_key_is_survivable():
    """A course dict that failed to summarize must not crash the GPA."""
    s = gpa_summary([{"course": "BROKEN"}, course("OK", 90)])
    assert s["gpa"] == 3.7
    assert s["courses_counted"] == 1
