"""The independent-question instrument's own guards, run by CI (D-064).

The instrument is a research artifact — the coverage number comes out of it —
but it lives in `scripts/` and only ran when someone invoked it by hand. Two
defects got in that way, one session apart, and both were mine:

  * D-063 declared cm-Q32's grouping as `calendar_unit x calendar_unit`, which
    the operator rejects as a duplicate dimension. It shipped, and `pagecap`
    stayed broken until it was next run by hand.
  * cm-Q39 was published as expressible for nine sessions with a grouping that
    needs three dimensions against a two-dimension budget.

Both are **validation** failures rather than measurement failures, so they
need no store and no data — just the operator's own validator, asked whether
the declared grouping is one it would accept. That is cheap enough to run on
every commit, which is what this file is for. The cardinalities still need
stores and stay in `pagecap`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

independent_questions = pytest.importorskip("independent_questions")


def test_every_declared_grouping_is_one_the_operator_accepts():
    """The guard that would have caught both defects above."""
    problems = independent_questions.check_groupings()
    assert not problems, "\n".join(problems)


def test_the_guard_actually_catches_a_bad_grouping():
    """A guard nobody has seen fail is a guard nobody knows works. Both real
    defect shapes are reinstated here and must be reported."""
    iq = independent_questions
    saved = dict(iq.GROUPINGS)
    try:
        iq.GROUPINGS[("cm", 32)] = (("calendar_unit", "calendar_unit"),)
        assert iq.check_groupings(), "the D-063 duplicate-dimension bug passed"

        iq.GROUPINGS[("cm", 32)] = (("src", "dst", "time_bucket"),)
        assert iq.check_groupings(), "a three-dimension grouping passed"
    finally:
        iq.GROUPINGS.clear()
        iq.GROUPINGS.update(saved)
    assert not iq.check_groupings(), "the table was not restored"


def test_every_grouping_that_is_declared_has_been_measured():
    """The structural rule is the guard; the measurement is what can
    contradict it. A grouping with no recorded measurement means the two
    cannot disagree, which is how `cm-Q34` would have stayed 'at risk' while
    measuring 6,458 (D-064)."""
    declared = {g for gs in independent_questions.GROUPINGS.values() if gs
                for g in gs}
    assert not declared - set(independent_questions.MEASURED)
