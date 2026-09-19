"""D-088 fidelity check: pin-at-open across a real second process.

`docs/design/D088_READER_GENERATION_PINNING_DESIGN_2026-09-19.md`, "The
deterministic test" (§ under "Recommendation"): a same-process second handle
cannot reproduce the defect this fix targets, because `gc.rs`'s process-global
`PINS` table protects a live handle's generation for real -- which is exactly
why the original 6h diagnosis's same-process fd probe came back null. The
Rust regression test (`crates/tgms-engine-core/src/gc.rs`,
`a_cross_process_reader_survives_gc_because_pin_at_open_maps_its_whole_
generation`) emulates the cross-process case with a `#[cfg(test)]` unpin.
This test does not emulate anything: it spawns a real subprocess reader,
which has its own, separate `PINS` table (a fresh process loading its own
copy of `_engine`'s statics) that the writer's `gc()` -- run in this, the
parent, process -- never sees at all. That is the actual production shape
`ops/failure_ledger.jsonl`'s D-088 entry measured in the 72h soak: a
`read_only=True` reader reopening its handle across a writer that compacts
and collects generations underneath it.

Before the fix (`crates/tgms-engine-core/src/store.rs::pin_generation_files`,
called from `NativeStore::open` and every commit/compaction), the reader's
`open_segment` mapped a segment lazily, on its first query -- so a reader
that queries nothing between opening and the writer's `gc()` had *nothing*
protected, and its first query after gc failed with a detected
`OSError: io: ... No such file or directory ... [file=.../seg/....tgs]`. This
test's reader is built to hit exactly that worst case: it opens, announces
itself, and does nothing else until the writer has already compacted and
collected its original generation's segments out from under it.

**Why this test also checks `reopen_on_enoent_total() == 0`.** The adapter-
level net (`tgms/storage/native/adapter.py::NativeAdapter._read_op`, D-088's
"net", not its fix) will itself turn a lazily-opened, gc'd segment's ENOENT
into an apparently successful read -- by reopening to whatever generation is
`CURRENT` *now* and retrying the whole operator. Measured directly (disabling
the Rust fix and rerunning this file by hand): the read still returns
`ok: true`, but with every row from every generation the writer has since
published, because the net silently answered from a newer generation than
the one this reader pinned at open. That is a real recovery for the net's
own job -- the open-vs-gc race pin-at-open cannot itself close -- but it is
not what this test is checking, and it would otherwise hide a broken
pin-at-open behind an apparently-passing row count. So this test additionally
asserts the reader's generation never advanced and the net never fired: with
the actual fix, pinning prevents the ENOENT in the first place, so the net
has nothing to do.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

#: Spawn, so the reader never inherits an open store handle (or the parent's
#: `PINS` table) from this process -- which would make "a separate process"
#: a lie on Linux's default fork. Same discipline as tests/test_concurrency.py.
MP = mp.get_context("spawn")

ROWS_PER_BATCH = 16


def _events(tag: str, n: int) -> list[dict]:
    return [{"src": f"{tag}{i}", "dst": f"{tag}{i + 1}", "rel_type": "R",
             "vt_s": i, "vt_e": i + 1} for i in range(n)]


def _child_delayed_reader(root: str, out: str, opened, proceed) -> None:
    """Open read-only, announce, then wait -- querying nothing -- until the
    parent (the writer) has finished compacting and gc'ing this handle's own
    generation. See the module docstring for why the delay is the point.
    """
    import tgms

    s = tgms.open(root, backend="native", read_only=True)
    generation = s.adapter.generation
    opened.set()
    proceed.wait(180)
    result: dict = {"generation": generation}
    try:
        rows = s.adapter.all_edge_versions()
        result["ok"] = True
        result["count"] = len(list(rows))
    except Exception as e:  # noqa: BLE001 -- the failure itself is the result
        result["ok"] = False
        result["error_type"] = type(e).__name__
        result["errno"] = getattr(e, "errno", None)
        result["message"] = str(e)[:500]
    # The D-088 *net* (adapter.py's `_read_op`) would also turn a lazily-
    # opened, gc'd segment's ENOENT into an apparently successful read -- by
    # reopening to whatever generation is CURRENT *now* and retrying, which
    # silently answers from a newer generation than the one this handle
    # pinned at open. That is a real recovery for the net's own job (the
    # open-vs-gc race pin-at-open cannot close), but it is not this test's
    # job: this test is about pin-at-open specifically, so it also reports
    # whether the net had to fire, and the parent asserts it did not.
    result["reopen_on_enoent_total"] = s.adapter.reopen_on_enoent_total()
    result["generation_after"] = s.adapter.generation
    Path(out).write_text(json.dumps(result))
    try:
        s.close()
    except Exception:  # noqa: BLE001 -- best-effort; the result is already written
        pass


def test_a_cross_process_reader_queries_correctly_after_a_writer_compacts_and_gcs(
    tmp_path: Path,
) -> None:
    """The fix, end to end.

    A real subprocess reader opens at whatever generation the seed commit
    produced, does nothing, then answers correctly even though this (parent,
    writer) process's `compact()` + `compact()` + `gc(keep_last=1)` has since
    unlinked every segment file that generation's own manifest named.
    """
    import tgms

    root = tmp_path
    seed = tgms.open(root, backend="native")
    seed.ingest_events(_events("seed", ROWS_PER_BATCH))
    seed.close()
    # `NativeStore.close()` is presently a Rust no-op (D-088 design memo
    # §3 item 4, not adopted here -- see the ledger deviation note): the
    # pin this handle registered at open only releases when CPython drops
    # the underlying object. Without this, `seed`'s generation would stay
    # pinned in *this very process's* own `PINS` table for the rest of the
    # test, and the writer's own gc() below -- which consults that same
    # table -- would refuse to collect anything this test needs collected.
    del seed

    native_dir = root / "native"
    pre_reader_segs = sorted(p.name for p in (native_dir / "seg").glob("*.tgs"))
    assert pre_reader_segs, "the seed commit must have written at least one segment"

    out = tmp_path / "reader_result.json"
    opened, proceed = MP.Event(), MP.Event()
    reader = MP.Process(target=_child_delayed_reader,
                        args=(str(root), str(out), opened, proceed))
    reader.start()
    try:
        assert opened.wait(180), "the reader never opened the store"

        # Everything below runs in *this* process -- a real second process
        # relative to the reader, with its own separate `PINS` table, exactly
        # the shape `gc.rs`'s module doc describes as having no
        # cross-process registry at all.
        w = tgms.open(root, backend="native")
        w.ingest_events(_events("mid", ROWS_PER_BATCH))
        w.adapter.compact()  # supersedes the seed + mid segments
        w.ingest_events(_events("late", ROWS_PER_BATCH))
        w.adapter.compact()  # supersedes the compacted-1 + late segments
        w.adapter.gc(keep_last=1)  # only the live generation is retained
        w.close()
    finally:
        proceed.set()
        reader.join(180)
        if reader.is_alive():
            reader.terminate()
            reader.join(30)

    # Assert first: gc really did remove a file the reader's generation
    # named, on disk -- otherwise everything below passes vacuously. The
    # reader opened strictly after the seed commit, so its generation's
    # manifest names at least `pre_reader_segs`.
    removed = [f for f in pre_reader_segs if not (native_dir / "seg" / f).exists()]
    assert removed, (
        "gc must have collected at least one of the reader's own generation's "
        "segments for this test to mean anything; none were removed"
    )

    assert reader.exitcode == 0, f"the reader process crashed: exit {reader.exitcode}"
    got = json.loads(out.read_text())
    assert got["ok"], (
        f"the cross-process reader failed after gc: {got.get('error_type')} "
        f"{got.get('message')!r} (errno={got.get('errno')}) -- this is exactly "
        f"D-088's soak-3 failure mode (OSError, errno ENOENT, naming an "
        f"unlinked segment); pin-at-open should have prevented it"
    )
    assert got["count"] == ROWS_PER_BATCH, (
        f"the reader's own (pinned) generation must see exactly the "
        f"{ROWS_PER_BATCH} rows committed before it opened, not the writer's "
        f"later `mid`/`late` batches -- got {got['count']}"
    )
    assert got["generation_after"] == got["generation"], (
        "the reader's generation must never advance on its own"
    )
    assert got["reopen_on_enoent_total"] == 0, (
        "the D-088 *net* (adapter.py's reopen-on-ENOENT) fired, which means "
        "pin-at-open did not actually protect this segment -- the net papered "
        "over the miss by silently reopening to a newer generation (hence "
        "still 'ok', but answering a different question than the one asked); "
        "this test is specifically about pin-at-open, so the net must stay "
        "idle here"
    )
