"""`mask_in` — bit-identical to `np.isin`, and it must stay O(n).

The bug this pins cost a measured LDBC IC2 execution 231.9 of its 263.4
seconds at SF1 (88%), across six calls. `np.isin` on **object** arrays cannot
sort, so it compares every row against every element of the needle — O(n*m) in
Python-level object comparisons. Every identity column here is an object array
(uids, labels, rel_types are strings), and `Expand` resolves the version of
every uid in its frontier, so the needle grows with the expansion. The cost is
therefore quadratic in exactly the place a plan fans out.

Two obligations, and the second is the one that keeps the fix fixed:

1. **Equivalence.** The mask must equal `np.isin`'s, bit for bit, for every
   input shape the evaluator produces. Asserted directly against `np.isin`
   rather than against a hand-written expectation, so the oracle is the thing
   being replaced.
2. **Scaling.** A bounded-time assertion at a realistic needle size. If someone
   reverts to `np.isin` on an object column, this test does not merely fail —
   it fails *slowly*, which is the signature of the defect.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from tgms.tgir.eval.masks import SMALL_NEEDLE, mask_in

#: Big enough that the O(n*m) form is unmistakable (measured ~39 s at a 2,000
#: needle over 3M rows) and small enough to stay a unit test. At these sizes
#: `np.isin` takes ~2.6 s and `mask_in` ~0.02 s, so the ceiling separates them
#: by two orders of magnitude with room for a slow CI box.
ROWS = 200_000
NEEDLE = 2_000
CEILING_S = 1.0


def _uid_column(n: int) -> np.ndarray:
    return np.array([f"uid-{i}" for i in range(n)], dtype=object)


# ---------------------------------------------------------------------------
# 1. equivalence, against np.isin itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_values,n_wanted", [
    (0, 0), (0, 5), (10, 0), (1, 1), (10, 1), (10, 3),
    (100, SMALL_NEEDLE), (100, SMALL_NEEDLE + 1), (500, 50), (5_000, 300),
])
def test_the_mask_equals_np_isin_bit_for_bit(n_values, n_wanted):
    values = _uid_column(n_values)
    wanted = np.array([f"uid-{i * 3}" for i in range(n_wanted)], dtype=object)
    expected = np.isin(values, wanted)
    got = mask_in(values, wanted)
    assert got.dtype == expected.dtype == np.bool_
    assert got.shape == expected.shape
    assert np.array_equal(got, expected)


def test_it_matches_on_labels_where_the_needle_is_tiny_and_rows_repeat():
    """The `TypeConstraint` / `labels` shape: a handful of distinct values over
    a long column."""
    labels = np.array(["Person", "Post", "Comment", "Forum"] * 5_000, dtype=object)
    for wanted in (["Person"], ["Post", "Comment"], ["Nope"], []):
        w = np.array(wanted, dtype=object)
        assert np.array_equal(mask_in(labels, w), np.isin(labels, w))


def test_it_matches_when_nothing_and_everything_is_wanted():
    values = _uid_column(1_000)
    none_ = np.array([], dtype=object)
    all_ = values.copy()
    assert not mask_in(values, none_).any()
    assert mask_in(values, all_).all()
    assert np.array_equal(mask_in(values, none_), np.isin(values, none_))
    assert np.array_equal(mask_in(values, all_), np.isin(values, all_))


def test_duplicates_on_either_side_do_not_change_the_mask():
    values = np.array(["a", "b", "a", "c", "b"], dtype=object)
    wanted = np.array(["a", "a", "c"], dtype=object)
    assert np.array_equal(mask_in(values, wanted), np.isin(values, wanted))


def test_numeric_columns_are_left_to_np_isin():
    """`src_id`/`dst_id` are int64 and `np.isin` already sorts and merges
    there — the helper must not interpose a slower Python path."""
    values = np.arange(10_000, dtype=np.int64)
    wanted = np.array([3, 99, 10_001], dtype=np.int64)
    assert np.array_equal(mask_in(values, wanted), np.isin(values, wanted))


def test_mixed_types_agree_with_np_isin():
    """A string column against an int needle must not match by coercion."""
    values = np.array(["1", "2", "3"], dtype=object)
    wanted = np.array([1, 2], dtype=object)
    assert np.array_equal(mask_in(values, wanted), np.isin(values, wanted))


def test_an_unhashable_needle_falls_back_rather_than_raising():
    """Totality: the helper accepts everything `np.isin` accepts."""
    values = np.array(["a", "b"], dtype=object)
    wanted = np.empty(2, dtype=object)
    wanted[0], wanted[1] = ["x"], ["y"]        # lists: unhashable
    assert np.array_equal(mask_in(values, wanted), np.isin(values, wanted))


# ---------------------------------------------------------------------------
# 2. the scaling pin — the reason this file exists
# ---------------------------------------------------------------------------

def test_a_realistic_needle_stays_fast_on_an_object_column():
    """**The regression pin.** `np.isin` over this shape is O(n*m); at
    200,000 rows and a 2,000 needle it takes ~2.6 s, and the real SF1 shape
    (3M rows, growing needle) took 39-196 s per call. `mask_in` is ~0.02 s and
    flat in the needle.

    A revert to `np.isin` on an object column fails here on *time*, which is
    the only way to catch a change that is otherwise semantically perfect.
    """
    values = _uid_column(ROWS)
    wanted = np.array([f"uid-{i * 7 % ROWS}" for i in range(NEEDLE)], dtype=object)

    start = time.perf_counter()
    got = mask_in(values, wanted)
    elapsed = time.perf_counter() - start

    assert got.sum() > 0, "the fixture selected nothing; the pin proves nothing"
    assert elapsed < CEILING_S, (
        f"mask_in took {elapsed:.2f}s over {ROWS:,} rows with a {NEEDLE:,} "
        f"needle (ceiling {CEILING_S}s). That is the O(n*m) object-array "
        f"signature — see tgms/tgir/eval/masks.py.")


def test_the_cost_does_not_grow_with_the_needle():
    """Flatness, not just speed: the defect's signature is that widening the
    needle widens the runtime, so that is what is asserted against."""
    values = _uid_column(ROWS)

    def timed(m: int) -> float:
        wanted = np.array([f"uid-{i * 7 % ROWS}" for i in range(m)], dtype=object)
        start = time.perf_counter()
        mask_in(values, wanted)
        return time.perf_counter() - start

    small, large = timed(50), timed(5_000)
    # a 100x wider needle must not cost anything like 100x more
    assert large < max(small * 10, 0.2) + CEILING_S, (
        f"needle 50 -> {small:.3f}s but needle 5,000 -> {large:.3f}s: the cost "
        f"is tracking the needle, which is the O(n*m) form returning")


def test_the_evaluator_uses_the_helper_on_every_object_column():
    """A grep-as-a-test: the four object-column membership sites must not drift
    back to `np.isin`. The one remaining `np.isin` in `scan.py` is int64 dense
    ids, where it is the right call and is commented as such."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "tgms" / "tgir" / "eval"
    scan = (root / "scan.py").read_text()
    select = (root / "select.py").read_text()

    assert scan.count("np.isin(") == 1, (
        "scan.py should hold exactly one np.isin — the int64 dense-id filter")
    assert "keep = np.isin(side, ids)" in scan
    assert "mask_in(cols[\"label\"]" in scan
    assert "mask_in(cols[\"uid\"]" in scan
    assert "mask_in(cols[\"rel_type\"]" in scan
    assert "mask_in(cols[\"src\"]" in scan and "mask_in(cols[\"dst\"]" in scan
    assert "np.isin(" not in select and "mask_in(values, wanted)" in select
