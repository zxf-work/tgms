"""Unit tests for tgms.core: intervals, canonical JSON, clock."""

import pytest

from tgms.core.clock import HybridLogicalClock
from tgms.core.model import (
    OPEN_END,
    EntityRef,
    Interval,
    canonical_json,
    digest,
    edge_eid,
)


def test_interval_validity():
    Interval(0, 1)
    Interval(5, OPEN_END)
    with pytest.raises(ValueError):
        Interval(3, 3)
    with pytest.raises(ValueError):
        Interval(4, 3)


def test_interval_half_open_semantics():
    iv = Interval(10, 20)
    assert iv.contains(10) and iv.contains(19)
    assert not iv.contains(20) and not iv.contains(9)
    assert iv.overlaps(Interval(19, 30))
    assert not iv.overlaps(Interval(20, 30))  # meets, does not overlap
    assert iv.intersect(Interval(15, 25)) == Interval(15, 20)
    assert iv.intersect(Interval(20, 25)) is None


def test_canonical_json_is_deterministic():
    a = canonical_json({"b": 1, "a": [2, 1], "c": {"y": 0, "x": 1}})
    b = canonical_json({"c": {"x": 1, "y": 0}, "a": [2, 1], "b": 1})
    assert a == b == '{"a":[2,1],"b":1,"c":{"x":1,"y":0}}'
    assert digest({"b": 1, "a": 2}) == digest({"a": 2, "b": 1})


def test_edge_eid_stable_and_disc_sensitive():
    assert edge_eid("u", "v", "R") == edge_eid("u", "v", "R", "")
    assert edge_eid("u", "v", "R") != edge_eid("u", "v", "R", "x")
    assert edge_eid("u", "v", "R") != edge_eid("v", "u", "R")


def test_entity_ref_validation():
    EntityRef(kind="node", uid="a")
    EntityRef(kind="edge", src="a", dst="b", rel_type="R")
    with pytest.raises(ValueError):
        EntityRef(kind="node")
    with pytest.raises(ValueError):
        EntityRef(kind="edge", src="a")


def test_clock_strictly_monotonic():
    fake_now = [100]
    clk = HybridLogicalClock(last_tt=0, now_fn=lambda: fake_now[0])
    assert clk.tick() == 100
    assert clk.tick() == 101  # wall clock stalled -> logical bump
    fake_now[0] = 500
    assert clk.tick() == 500
    fake_now[0] = 400  # wall clock regression tolerated
    assert clk.tick() == 501
    clk.observe(1000)
    assert clk.tick() == 1001
