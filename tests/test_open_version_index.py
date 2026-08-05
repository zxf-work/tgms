"""The open-version index changes speed, never answers (D-076).

`believed_*_versions(identity)` walks every version of that identity to find
the ~1 currently believed. D-075 measured that walk at 58% of a correction's
cost at batch 100 — the batch size D-074 tells users to choose — growing
linearly with per-identity history depth. D-076 replaces the walk, on the
current-belief path only, with an in-memory `identity -> open rows` index
maintained incrementally at commit.

**This file exists because that index is an optimization of the engine's most
invariant-dense code.** D-059 found a defect in exactly this path that was
invisible unless a second op in the same batch read belief the first had
already changed. Every test here therefore compares the fast path against
ground truth — `all_node_versions()` filtered in Python, which knows nothing
about the index — under the shapes that have historically hidden defects:
earlier-batch corrections, multiple updates inside one batch, rollback,
compaction, generation collection, and replay.

Depth is what makes these interesting: at depth 1 an index that is subtly
wrong still returns the only row there is.
"""

from __future__ import annotations

import pytest

from tgms.core.model import OPEN_END, clamp_tt

from .conftest import fresh_adapter

DEPTH = 40


def _believed_by_hand(adapter, uid: str, as_of_tt: int = OPEN_END) -> list:
    """Ground truth, computed without asking the engine what it believes.

    Deliberately materializes every version and filters in Python: the point
    is to be a different implementation from the one under test. `clamp_tt`
    is not optional — the default `as_of_tt` is OPEN_END and open rows carry
    `tt_e == OPEN_END`, so an unclamped `as_of < tt_e` matches nothing.
    """
    a = clamp_tt(as_of_tt)
    return sorted(
        (v for v in adapter.all_node_versions()
         if v.uid == uid and v.tt_s <= a < v.tt_e),
        key=lambda v: (v.vt_s, v.vid),
    )


def _agree(adapter, uid: str, as_of_tt: int = OPEN_END) -> list:
    """Assert the engine's answer equals ground truth, and return it."""
    fast = sorted(adapter.believed_node_versions(uid, as_of_tt),
                  key=lambda v: (v.vt_s, v.vid))
    slow = _believed_by_hand(adapter, uid, as_of_tt)
    assert [v.vid for v in fast] == [v.vid for v in slow], (
        f"believed_node_versions({uid!r}, as_of_tt={as_of_tt}) disagrees with a "
        f"full scan: fast={[v.vid for v in fast]} slow={[v.vid for v in slow]}"
    )
    return fast


def _deepen(adapter, uid: str, depth: int, start_tt: int = 1000) -> int:
    """`depth` whole-interval overwrites, one per batch — so each lands in its
    own transaction time and is retained rather than retired (D-059)."""
    tt = start_tt
    for d in range(depth):
        tt += 1
        adapter.begin()
        adapter.apply_ops(
            [{"op": "assert_node", "uid": uid, "label": "N",
              "props": {"v": d}, "vt_s": 0, "vt_e": OPEN_END}], tt)
        adapter.commit()
    return tt


def test_deep_identity_keeps_exactly_one_believed_version():
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    believed = _agree(adapter, "deep")
    assert len(believed) == 1
    assert believed[0].props["v"] == DEPTH - 1
    # every superseded version is retained: the store's whole purpose
    assert len([v for v in adapter.all_node_versions() if v.uid == "deep"]) == DEPTH
    assert tt


def test_correction_to_an_earlier_batch_row_at_depth():
    """The half D-059 found invisible: the target was committed earlier."""
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    tt += 1
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "deep", "label": "N",
          "props": {"v": "corrected"}, "vt_s": 0, "vt_e": OPEN_END}], tt)
    adapter.commit()
    believed = _agree(adapter, "deep")
    assert len(believed) == 1 and believed[0].props["v"] == "corrected"


def test_multiple_updates_inside_one_batch_at_depth():
    """The shape that hid the `tt_s == tt_e` defect: a second op in the same
    batch reads belief the first has already changed (D-059). A version
    created and closed at the same tt was believed at no transaction time at
    all, so it must be retired rather than retained."""
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    tt += 1
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "deep", "label": "N",
          "props": {"v": "first"}, "vt_s": 0, "vt_e": OPEN_END},
         {"op": "assert_node", "uid": "deep", "label": "N",
          "props": {"v": "second"}, "vt_s": 0, "vt_e": OPEN_END}], tt)
    adapter.commit()

    believed = _agree(adapter, "deep")
    assert len(believed) == 1 and believed[0].props["v"] == "second"
    # the intermediate version was never believed at any tt, so it is not a row
    all_v = [v for v in adapter.all_node_versions() if v.uid == "deep"]
    assert len(all_v) == DEPTH + 1, (
        f"expected the in-batch intermediate to be retired, not retained; "
        f"got {len(all_v)} versions for depth {DEPTH} plus one batch"
    )
    assert all(v.tt_e > v.tt_s for v in all_v), "a belief interval is non-empty"


def test_mid_batch_read_sees_its_own_writes_at_depth():
    """Read-your-own-writes: the index must be layered under staged rows, not
    consulted instead of them."""
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    tt += 1
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "deep", "label": "N",
          "props": {"v": "staged"}, "vt_s": 0, "vt_e": OPEN_END}], tt)
    mid = adapter.believed_node_versions("deep")
    assert len(mid) == 1 and mid[0].props["v"] == "staged", (
        "a read inside the batch must see the batch's own write"
    )
    adapter.commit()
    _agree(adapter, "deep")


def test_rollback_leaves_the_previous_belief_intact_at_depth():
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    before = [v.vid for v in _agree(adapter, "deep")]

    tt += 1
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "deep", "label": "N",
          "props": {"v": "doomed"}, "vt_s": 0, "vt_e": OPEN_END}], tt)
    adapter.rollback()

    after = [v.vid for v in _agree(adapter, "deep")]
    assert after == before, "a rolled-back batch must leave belief unchanged"
    assert all(v.props.get("v") != "doomed" for v in adapter.all_node_versions())


def test_historical_as_of_still_answers_at_depth():
    """The index serves current belief only; every historical tt must still
    resolve through the walk, and agree with ground truth."""
    adapter = fresh_adapter(paranoid=False)
    start = 1000
    _deepen(adapter, "deep", DEPTH, start_tt=start)
    for k in (1, DEPTH // 3, DEPTH // 2, DEPTH - 1):
        as_of = start + k
        believed = _agree(adapter, "deep", as_of)
        assert len(believed) == 1, f"exactly one belief at tt={as_of}"
        assert believed[0].props["v"] == k - 1


def test_two_identities_do_not_share_open_rows():
    """A per-identity index keyed wrongly would cross-contaminate; depth makes
    the collision likely rather than theoretical."""
    adapter = fresh_adapter(paranoid=False)
    tt = 1000
    for d in range(DEPTH):
        tt += 1
        adapter.begin()
        adapter.apply_ops(
            [{"op": "assert_node", "uid": u, "label": "N",
              "props": {"v": d, "who": u}, "vt_s": 0, "vt_e": OPEN_END}
             for u in ("alpha", "beta")], tt)
        adapter.commit()
    for u in ("alpha", "beta"):
        believed = _agree(adapter, u)
        assert len(believed) == 1
        assert believed[0].props["who"] == u


def test_retract_at_depth():
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    tt += 1
    adapter.begin()
    adapter.apply_ops(
        [{"op": "retract", "ref": {"kind": "node", "uid": "deep"}, "t": 500}], tt)
    adapter.commit()
    believed = _agree(adapter, "deep")
    assert len(believed) == 1
    assert believed[0].vt_e == 500, "the retraction truncates valid time at t"


# --- maintenance paths ---------------------------------------------------- #
#
# The index holds physical (segment, row) addresses. Compaction rewrites rows
# into fresh segments and gc deletes the old ones, so an index that survives
# either without being rebuilt or invalidated points at rows that have moved.


def test_compaction_preserves_belief_at_depth():
    adapter = fresh_adapter(paranoid=False)
    _deepen(adapter, "deep", DEPTH)
    before = [v.vid for v in _agree(adapter, "deep")]
    if not hasattr(adapter, "compact"):
        pytest.skip("backend has no compaction")
    adapter.compact()
    assert [v.vid for v in _agree(adapter, "deep")] == before


def test_gc_preserves_belief_at_depth():
    adapter = fresh_adapter(paranoid=False)
    _deepen(adapter, "deep", DEPTH)
    before = [v.vid for v in _agree(adapter, "deep")]
    if not (hasattr(adapter, "compact") and hasattr(adapter, "gc")):
        pytest.skip("backend has no compaction/gc")
    adapter.compact()
    adapter.gc(keep_last=0)
    assert [v.vid for v in _agree(adapter, "deep")] == before


def test_correction_after_compaction_at_depth():
    """Writing *through* a compaction boundary: the index must be rebuilt or
    invalidated, not carried across segments that no longer exist."""
    adapter = fresh_adapter(paranoid=False)
    tt = _deepen(adapter, "deep", DEPTH)
    if not hasattr(adapter, "compact"):
        pytest.skip("backend has no compaction")
    adapter.compact()

    tt += 1000  # compaction does not advance tt; stay safely ahead
    adapter.begin()
    adapter.apply_ops(
        [{"op": "assert_node", "uid": "deep", "label": "N",
          "props": {"v": "post-compact"}, "vt_s": 0, "vt_e": OPEN_END}], tt)
    adapter.commit()

    believed = _agree(adapter, "deep")
    assert len(believed) == 1 and believed[0].props["v"] == "post-compact"
    assert len([v for v in adapter.all_node_versions() if v.uid == "deep"]) == DEPTH + 1
