"""`Store.ingest_events`' default-`disc` offset must not reset per top-level
call (B7a incident, `docs/STABILITY.md`).

`tgms/storage/base.py::_ingest_events` gives an event without an explicit
`disc` a default derived from its position in the bulk stream: `f"#{offset +
i}"`, where `offset` comes from the op and `i` is the event's index within
that one op's own `events` list. `Store.ingest_events` (`tgms/store.py`)
supplies `offset`, and used to start it at a local `offset = 0` on *every*
top-level call — correct only across the `INGEST_CHUNK` chunks *within* one
call, since those chunks share the loop's running `offset`. A caller that
splits one logical bulk load into several top-level `ingest_events` calls
(batching) instead got the *same* default `disc` values from each call, so
two edges at the same intra-call index collided into one edge identity
whenever they shared `(src, dst, rel_type)` — silently merging what should
have been distinct edges.

The fix keeps the offset counter on the `Store` instance
(`self._ingest_offset_base`), advancing across top-level calls and never
resetting, so this collision cannot happen for any two calls made against
the same open `Store` handle.
"""

from __future__ import annotations

import tgms


def _batch(n: int, *, vt_s0: int = 0) -> list[dict]:
    """`n` edges between the same fixed endpoints/rel_type, at the same
    intra-call indices `0..n-1` a second call of this shape would also use —
    exactly the case the bug collided."""
    return [{"src": "A", "dst": "B", "rel_type": "R", "vt_s": vt_s0 + i}
            for i in range(n)]


def test_two_ingest_events_calls_do_not_collide_discs(tmp_path):
    store = tgms.open(tmp_path / "s")
    n = 25

    store.ingest_events(_batch(n, vt_s0=0))
    store.ingest_events(_batch(n, vt_s0=1000))

    edges = list(store.adapter.all_edge_versions())
    eids = {e.eid for e in edges}
    discs = {e.disc for e in edges}

    assert len(edges) == 2 * n, (
        f"expected {2 * n} edge versions from two independent {n}-edge "
        f"calls, got {len(edges)} — a lower count means later edges "
        f"superseded/merged with earlier ones via a colliding default disc"
    )
    assert len(eids) == 2 * n, "each edge must get its own distinct identity"
    assert len(discs) == 2 * n, (
        f"expected {2 * n} distinct default discs across the two calls, "
        f"got {len(discs)} — the offset base must not reset per top-level "
        f"ingest_events call"
    )


def test_single_call_disc_offset_unaffected(tmp_path):
    """The fix must not change a single top-level call's own digest: this
    instance's running offset starts at 0, exactly the old per-call
    `offset`, so a store's first `ingest_events` call assigns identical
    `disc` values either way."""
    store = tgms.open(tmp_path / "s")
    n = 10

    store.ingest_events(_batch(n))

    discs = sorted(e.disc for e in store.adapter.all_edge_versions())
    assert discs == [f"#{i}" for i in range(n)]
