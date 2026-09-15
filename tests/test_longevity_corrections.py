"""`scripts/longevity_run.py::apply_correction_via_public_api`'s `a1_events`
disc stamping (`docs/design/CORRECTION_DISC_SEMANTICS_REVIEW_2026-09-15.md`
§4, ranked recommendation (i), and the B7a ledger entry's related finding).

`corrections.py::_a1_events` now stamps a fresh, unique `disc` per
correction (see `tests/test_corrections_disc.py`), but that disc is derived
from the *generating process's own RNG sequence* — and the soak's writer
restarts every "life" as a fresh process re-seeded with the *same* seed
(`spawn_writer` never varies it by life index), so two lives can walk an
identical RNG sequence and reproduce an identical disc for what are, in
wall-clock terms, two unrelated corrections.
`apply_correction_via_public_api` re-stamps a disc at the point it calls
the public `Store.ingest_events` API, independent of the RNG and of any
`Store` counter, specifically so that a within-process (this test) or
cross-process (the real soak) repeat cannot collide.

This test stays within one process (a real soak restart is out of scope for
a laptop unit test — `Experiments remote only`), and checks the property
that matters at this level: two consecutive corrections through the public
API land two distinct edge identities, never one.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import longevity_run as LR  # noqa: E402

import tgms  # noqa: E402
from tgms.eval.corrections import Substrate, Target, _a1_events  # noqa: E402
from tgms.write import GroupCommitWriter  # noqa: E402

BACKEND = "native"


def test_two_consecutive_apply_correction_calls_land_two_identities(tmp_path):
    store = tgms.open(tmp_path / "store", backend=BACKEND)
    store.ingest_events(
        [{"src": "n0", "dst": "n1", "rel_type": "FOLLOWS", "vt_s": 0}],
        node_label="Person")
    before = {v.eid for v in store.adapter.all_edge_versions()
             if v.src == "n0" and v.dst == "n1" and v.rel_type == "FOLLOWS"}
    assert len(before) == 1

    gc_writer = GroupCommitWriter(store, max_delay_s=0.0, max_batch=200).start()
    try:
        # A substrate/target with exactly one realizable draw at every step
        # forces both corrections onto the identical (src, dst, rel_type),
        # the scenario the RNG-restart risk is about.
        sub = Substrate(uids=("n0", "n1"), rel_types=("FOLLOWS",), vt_lo=0, vt_hi=1000)
        target = Target(read_uids=("n0",), window=(0, 100))
        rng = random.Random(0)
        c1 = _a1_events(store, sub, target, "in-window-read", rng)
        c2 = _a1_events(store, sub, target, "in-window-read", rng)
        assert c1 is not None and c2 is not None
        assert (c1.ops[0]["events"][0]["src"], c1.ops[0]["events"][0]["dst"],
               c1.ops[0]["events"][0]["rel_type"]) == ("n0", "n1", "FOLLOWS")
        assert (c2.ops[0]["events"][0]["src"], c2.ops[0]["events"][0]["dst"],
               c2.ops[0]["events"][0]["rel_type"]) == ("n0", "n1", "FOLLOWS")

        assert LR.apply_correction_via_public_api(store, gc_writer, c1) is True
        assert LR.apply_correction_via_public_api(store, gc_writer, c2) is True
    finally:
        gc_writer.close()

    after = {v.eid for v in store.adapter.all_edge_versions()
            if v.src == "n0" and v.dst == "n1" and v.rel_type == "FOLLOWS"}
    assert len(after) == len(before) + 2, (
        "two consecutive a1_events corrections through the public API must "
        "land two distinct identities, not merge into one")


def test_apply_correction_stamps_a_disc_independent_of_corrections_pys_own(tmp_path):
    """Even if `corrections.py` handed two corrections the *same* disc (the
    RNG-restart scenario this call site guards against), the public-API
    path must not simply forward it — it stamps its own."""
    store = tgms.open(tmp_path / "store", backend=BACKEND)
    store.ingest_events(
        [{"src": "n0", "dst": "n1", "rel_type": "FOLLOWS", "vt_s": 0}],
        node_label="Person")
    gc_writer = GroupCommitWriter(store, max_delay_s=0.0, max_batch=200).start()
    try:
        correction = _a1_events(
            store,
            Substrate(uids=("n0", "n1"), rel_types=("FOLLOWS",), vt_lo=0, vt_hi=1000),
            Target(read_uids=("n0",), window=(0, 100)),
            "in-window-read", random.Random(0))
        assert correction is not None
        ev = correction.ops[0]["events"][0]
        colliding_disc = ev["disc"]

        # simulate two "lives" whose RNG walked to the identical correction
        # (same object, applied twice) — the scenario a same-seed restart
        # produces.
        assert LR.apply_correction_via_public_api(store, gc_writer, correction) is True
        assert LR.apply_correction_via_public_api(store, gc_writer, correction) is True
    finally:
        gc_writer.close()

    eids = {v.eid for v in store.adapter.all_edge_versions()
           if v.src == "n0" and v.dst == "n1" and v.rel_type == "FOLLOWS"}
    # bulk-load edge + two re-applications of the *same* colliding correction
    # object: still two fresh identities, because the call site re-stamps.
    assert len(eids) == 3
    assert colliding_disc not in {"", "#0"}
