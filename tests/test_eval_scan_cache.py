"""`ScanCache` — one full node read per execution, not one per binding node.

A plan reads the whole node table once per `NodeScan`, and again once per
`Expand`/`PatternMatch` that binds `into`, because version resolution goes back
through `scan_nodes` deliberately: `into`'s columns must come through the same
Σ predicate, the same censoring rule and the same routing as any other scan.
That design is right and is kept; what it should not also mean is
re-materializing the table. Measured on LDBC IC2 at SF1: three
`nodes_columnar` calls, 8,992,056 rows, ~24 s of one execution.

The cache is per-`Execution`, keyed on exactly the arguments `nodes_columnar`
is called with. The properties that make it safe are the ones tested here:
identical results, no cross-execution leakage, and no mutation of a shared
array.
"""

from __future__ import annotations

import numpy as np
import pytest

import tgms
from tgms.core.model import OPEN_END
from tgms.tgir.eval import Execution, ScanCache, evaluate_core
from tgms.tgir.expr import Col
from tgms.tgir.node import Exact, Expand, NodeScan, Project
from tgms.tgir.types import Interval, Sigma


@pytest.fixture
def store(tmp_path):
    s = tgms.open(tmp_path / "s", backend="duckdb")
    s.ingest_events(
        [{"src": f"p{i}", "dst": f"p{(i + 1) % 6}", "rel_type": "KNOWS",
          "vt_s": 100 + i} for i in range(6)],
        nodes=[{"uid": f"p{i}", "label": "Person",
                "props": {"name": f"n{i}"}, "vt_s": 10} for i in range(6)])
    yield s
    s.close()


class _Counting:
    """Counts the reads the evaluator issues, without changing any of them."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "n", 0)

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name != "nodes_columnar":
            return attr

        def wrapped(*a, **k):
            object.__setattr__(self, "n",
                               object.__getattribute__(self, "n") + 1)
            return attr(*a, **k)
        return wrapped


SIGMA = Sigma(t_v=(Interval(0, OPEN_END),), t_b=OPEN_END)


def _plan():
    """`NodeScan -> Expand(into=) -> Project`: one scan plus one version
    resolution, which is two full node reads before the cache and one after.

    The `Project` is not decoration. Without it the root demands its whole
    declared schema — `tt_s`/`tt_e` included — and both scans take the
    `versions_columnar` fallback instead of the columnar route, so the plan
    would exercise a path this cache does not sit on. Every real plan carries a
    projection above its expansions, which is why the fast route is the one
    that matters.
    """
    expand = Expand(input=NodeScan("p", labels=("Person",), belief="current",
                                   vt_mode="overlap", sigma_=SIGMA),
                    from_="p", into="q", rel_type="KNOWS", hops=Exact(1),
                    dir="out")
    return Project(input=expand,
                   bindings=(("who", Col("p.uid")), ("friend", Col("q.uid"))),
                   keep="listed", sigma_=SIGMA)


def test_the_whole_node_table_is_read_once_per_execution(store):
    counting = _Counting(store.adapter)
    evaluate_core(_plan(), counting)
    assert object.__getattribute__(counting, "n") == 1, (
        "the node table was materialized more than once in one execution")


def test_the_cached_execution_returns_exactly_the_uncached_rows(store):
    """Equivalence, by construction and by check: the cache changes which reads
    happen, never what they say."""
    plan = _plan()
    with_cache = evaluate_core(plan, store.adapter)

    execution = Execution(store.adapter, live=None)
    execution.scans = None                      # the pre-cache route
    without = execution.run(plan)

    assert with_cache.n == without.n
    assert with_cache.schema.names == without.schema.names
    for name in with_cache.schema.names:
        a, b = with_cache.column(name), without.column(name)
        assert np.array_equal(np.asarray(a, dtype=object),
                              np.asarray(b, dtype=object)), name


def test_a_cache_is_not_shared_between_executions(store):
    """Lifetime is one execution. A store written between two runs must not be
    served a stale table — the same rule `AdjacencyCache` lives under."""
    first = Execution(store.adapter, live=None)
    second = Execution(store.adapter, live=None)
    assert first.scans is not second.scans
    first.run(_plan())
    assert second.scans.hits == 0 and second.scans.misses == 0


def test_a_write_between_executions_is_visible_to_the_next_one(store):
    """The property the lifetime rule exists to protect."""
    before = evaluate_core(_plan(), store.adapter)
    store.ingest_events([], nodes=[{"uid": "p99", "label": "Person",
                                    "props": {}, "vt_s": 10}])
    store.assert_edge("p0", "p99", "KNOWS", {}, 100, 200)
    after = evaluate_core(_plan(), store.adapter)
    assert after.n > before.n, "a later execution did not see the new edge"


def test_the_cache_hands_out_arrays_nobody_mutates(store):
    """Callers rebuild a filtered copy before touching anything; boolean
    indexing always copies. Checked by reading the same entry twice and
    confirming the second read is untouched by the first scan."""
    scans = ScanCache()
    scan = NodeScan("p", labels=("Person",), belief="current",
                    vt_mode="overlap", sigma_=SIGMA)
    first = scans.nodes(store.adapter, scan.sigma)
    snapshot = {k: v.copy() for k, v in first.items()}
    from tgms.tgir.eval.scan import scan_nodes
    scan_nodes(scan, store.adapter, None, scans)
    again = scans.nodes(store.adapter, scan.sigma)
    for k, v in snapshot.items():
        assert np.array_equal(np.asarray(again[k], dtype=object),
                              np.asarray(v, dtype=object)), k
    assert scans.hits >= 1


def test_two_different_sigmas_do_not_share_an_entry(store):
    """The key is the read's own arguments, so a narrower Σ is a different
    entry rather than a wrong hit."""
    scans = ScanCache()
    wide = Sigma(t_v=(Interval(0, OPEN_END),), t_b=OPEN_END)
    narrow = Sigma(t_v=(Interval(0, 50),), t_b=OPEN_END)
    a = scans.nodes(store.adapter, wide)
    b = scans.nodes(store.adapter, narrow)
    assert scans.misses == 2
    assert len(a["uid"]) >= len(b["uid"])
