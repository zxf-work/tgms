"""`disc` semantics for the two correction generators D2.1 calls "a new
fact" — `_a1_events` and `_a2_disjoint`'s edge branch
(`docs/design/CORRECTION_DISC_SEMANTICS_REVIEW_2026-09-15.md`).

Before this file's own fix, `_a1_events` emitted no `disc` at all (falling
back to `Store`/`_ingest_events`'s position-derived default) and
`_a2_disjoint`'s edge branch hard-coded the literal `"a2-disjoint"`. Both
defaults are **position-independent**, so two corrections that happened to
land on the same `(src, dst, rel_type)` triple — or one that reused a
bulk-loaded triple's own default disc — silently merged into one edge
identity instead of two, and for `_a2_disjoint` the second write *carved*
the first rather than merely merging with it (turning a Class-A "adds
belief" correction into an unlabelled Class-B one).

These tests force both generators onto the *same* triple twice (by
shrinking the substrate/target down to exactly one realizable draw) and
check the fix at the only level that matters: distinct eids actually land
in the store.
"""

from __future__ import annotations

import random

import tgms
from tgms.core.model import edge_eid
from tgms.eval.corrections import Substrate, Target, _a1_events, _a2_disjoint, generate

BACKEND = "native"


def _seed_store(tmp_path):
    store = tgms.open(tmp_path / "store", backend=BACKEND)
    store.ingest_events(
        [{"src": "n0", "dst": "n1", "rel_type": "FOLLOWS", "vt_s": 0}],
        node_label="Person")
    return store


def _triple_eids(store, src, dst, rel_type):
    return {v.eid for v in store.adapter.all_edge_versions()
            if v.src == src and v.dst == dst and v.rel_type == rel_type}


# ---------------------------------------------------------------------------
# 1 — two a1_events corrections on the same triple: N+2 eids, not N+1
# ---------------------------------------------------------------------------

def test_two_a1_events_corrections_on_the_same_triple_get_distinct_eids(tmp_path):
    store = _seed_store(tmp_path)
    before = _triple_eids(store, "n0", "n1", "FOLLOWS")
    assert len(before) == 1                     # the bulk load's own "#0" edge

    # A substrate/target with exactly one realizable draw at every step
    # (one read uid, one "other" uid, one rel type) forces both corrections
    # onto the identical (src, dst, rel_type) regardless of RNG state.
    sub = Substrate(uids=("n0", "n1"), rel_types=("FOLLOWS",), vt_lo=0, vt_hi=1000)
    target = Target(read_uids=("n0",), window=(0, 100))
    rng = random.Random(0)
    c1 = _a1_events(store, sub, target, "in-window-read", rng)
    c2 = _a1_events(store, sub, target, "in-window-read", rng)
    assert c1 is not None and c2 is not None

    ev1 = c1.ops[0]["events"][0]
    ev2 = c2.ops[0]["events"][0]
    assert (ev1["src"], ev1["dst"], ev1["rel_type"]) == ("n0", "n1", "FOLLOWS")
    assert (ev2["src"], ev2["dst"], ev2["rel_type"]) == ("n0", "n1", "FOLLOWS")
    assert ev1["disc"] != ev2["disc"]
    assert ev1["disc"] not in ("", "#0")

    store.ingest_events([ev1], node_label="Person")
    store.ingest_events([ev2], node_label="Person")

    after = _triple_eids(store, "n0", "n1", "FOLLOWS")
    assert len(after) == len(before) + 2, (
        "two a1 corrections on the same triple must land two fresh "
        "identities, not merge into the bulk load's or each other's eid")


# ---------------------------------------------------------------------------
# 2 — two a2_disjoint (edge branch) corrections on the same triple: same
#     N+2 property, and neither carves the other (they are different eids)
# ---------------------------------------------------------------------------

def test_two_a2_disjoint_corrections_on_the_same_triple_get_distinct_eids(tmp_path):
    store = _seed_store(tmp_path)
    before = _triple_eids(store, "n0", "n1", "FOLLOWS")
    assert len(before) == 1

    sub = Substrate(uids=("n0", "n1"), rel_types=("FOLLOWS",), vt_lo=0, vt_hi=1000)
    # "n0" already has a believed (open-ended) node version from the bulk
    # load, so `_a2_disjoint` takes its edge branch (corrections.py:285-304),
    # not the node-assert fallback.
    target = Target(read_uids=("n0",), window=(0, 100))
    rng = random.Random(0)
    c1 = _a2_disjoint(store, sub, target, "outside-window-read", rng)
    c2 = _a2_disjoint(store, sub, target, "outside-window-read", rng)
    assert c1 is not None and c2 is not None
    assert c1.ops[0]["op"] == "assert_edge" and c2.ops[0]["op"] == "assert_edge"

    op1, op2 = c1.ops[0], c2.ops[0]
    assert (op1["src"], op1["dst"], op1["rel_type"]) == ("n0", "n1", "FOLLOWS")
    assert (op2["src"], op2["dst"], op2["rel_type"]) == ("n0", "n1", "FOLLOWS")
    assert op1["disc"] != op2["disc"]
    assert op1["disc"] != "a2-disjoint" and op2["disc"] != "a2-disjoint"

    store.assert_edge(op1["src"], op1["dst"], op1["rel_type"], op1["props"],
                      op1["vt_s"], op1["vt_e"], op1["disc"])
    eid1 = edge_eid(op1["src"], op1["dst"], op1["rel_type"], op1["disc"])
    [v1] = [v for v in store.adapter.believed_edge_versions(eid1)]
    vt_e1_before = v1.vt_e

    store.assert_edge(op2["src"], op2["dst"], op2["rel_type"], op2["props"],
                      op2["vt_s"], op2["vt_e"], op2["disc"])

    after = _triple_eids(store, "n0", "n1", "FOLLOWS")
    assert len(after) == len(before) + 2

    # neither write touched the other's eid, so the first version's own
    # interval is untouched — a distinct-eid write structurally cannot carve.
    [v1_after] = [v for v in store.adapter.believed_edge_versions(eid1)]
    assert v1_after.vt_e == vt_e1_before


# ---------------------------------------------------------------------------
# 3 — disc determinism: replaying the same seed reproduces the same discs
# ---------------------------------------------------------------------------

def test_disc_determinism_across_two_generator_runs_with_the_same_seed(tmp_path):
    store = _seed_store(tmp_path)
    sub = Substrate(uids=("n0", "n1"), rel_types=("FOLLOWS", "MSG"), vt_lo=0, vt_hi=1000)
    target = Target(read_uids=("n0", "n1"), window=(0, 500))

    def _discs(seed: int) -> list[str]:
        rng = random.Random(seed)
        out = []
        for c in generate(store, sub, target, rng=rng):
            for op in c.ops:
                if op.get("op") == "assert_edge":
                    out.append(op["disc"])
                elif op.get("op") == "ingest_events":
                    for ev in op.get("events") or []:
                        if "disc" in ev:
                            out.append(ev["disc"])
        return out

    discs_a = _discs(42)
    discs_b = _discs(42)
    assert discs_a, "the fixture should realize at least one disc-carrying cell"
    assert discs_a == discs_b

    # a different seed is not required to (and, in practice, will not)
    # reproduce the same sequence — sanity that the test isn't vacuous.
    discs_c = _discs(43)
    assert discs_c != discs_a
