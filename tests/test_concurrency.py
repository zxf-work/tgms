"""Mixed writer + readers against one native store (D-043 item 3).

Every concurrent measurement this project had was readers-only (§14.4 of
`docs/eval_resources.md`), and readers-only cannot see the interesting
failure: a commit landing under a reader's feet. The design says it cannot
matter — segments are immutable, a reader pins a manifest generation, the
only mutation is an append-only close record — but `engine_lessons.md` §6
records that an earlier draft of exactly this design had a store-wide mutable
close set that would have let a reader observe visibility from a generation
it had not pinned. An argument is not a measurement, and neither is a
measurement a proof; these are the assertions.

Four properties, each expressed as "what would a violation look like":

1. **One generation per handle.** A reader handle answers every query from
   the generation it pinned at open, for its whole life. A violation looks
   like an answer changing while a writer commits.
2. **Batch atomicity across reopens.** A reader that reopens sees whole
   batches only, in order. A violation looks like a count that is not a
   whole number of batches (a torn read), or one that goes backwards.
3. **History is immutable.** An as-of-tt query about the past returns the
   same answer no matter how many corrections land after it.
4. **Opening is not writing.** A reader process must never publish a
   generation, must never mutate a byte, and must survive opening at any
   instant of the writer's commit -- including the window between the
   write-ahead log fsync and the manifest publish, where the store looks
   exactly like it crashed.

These run regardless of TGMS_TEST_BACKEND: they are about the native engine's
concurrency contract, not about adapter conformance.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from tgms.core.errors import TgmsError
from tgms.core.model import EntityRef

pytest.importorskip("tgms._engine", reason="native engine extension not built")

#: Rows per writer batch. Any count a reader sees must be a whole multiple of
#: this above the base, or it saw half a batch.
BATCH_ROWS = 8
BATCHES = 12

#: Spawn, so a child never inherits an open store handle from the parent —
#: which would make "a separate process" a lie on Linux's default fork.
MP = mp.get_context("spawn")


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def seed_store(root: Path, base_rows: int = 16) -> int:
    """A store with `base_rows` edges, committed and closed. Returns the tt."""
    import tgms

    s = tgms.open(root, backend="native")
    for i in range(base_rows):
        s.assert_edge(f"seed{i}", f"seed{i + 1}", "R", {"w": i},
                      vt_s=i, vt_e=i + 1, disc=f"s{i}")
    tt = s.stats() and s.clock.last_tt
    s.close()
    return tt


def write_batches(root: Path, batches: int = BATCHES, rows: int = BATCH_ROWS,
                  pace_s: float = 0.0) -> None:
    """Commit `batches` batches of `rows` edges each, one generation apiece."""
    import tgms

    s = tgms.open(root, backend="native")
    for b in range(batches):
        s.ingest_events(
            [{"src": f"w{b}_{i}", "dst": f"x{b}_{i}", "rel_type": "W",
              "vt_s": 1000 + b * rows + i} for i in range(rows)])
        if pace_s:
            time.sleep(pace_s)
    s.close()


def edge_count(store) -> int:
    return int(store.stats()["n_edge_versions"])


# --------------------------------------------------------------------------- #
# child entry points (module level: spawn must be able to import them)         #
# --------------------------------------------------------------------------- #


#: Answer keys that carry call metadata rather than the answer (harness
#: `VOLATILE`): comparing them would report agreement as disagreement.
VOLATILE = ("op", "args_echo", "dataset_extent", "result_digest", "cursor")


def _answer_hash(payload: dict) -> str:
    from tgms.core.model import canonical_json, sha256_hex

    return sha256_hex(canonical_json(
        {k: v for k, v in payload.items() if k not in VOLATILE}))[:32]


def _child_pinned_reader(root: str, out: str, stop, ready) -> None:
    """Hold one read-only handle and answer the same questions repeatedly."""
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    s = tgms.open(root, backend="native", read_only=True)
    a = s.adapter
    gen = a.generation

    def snapshot() -> dict:
        # four shapes on purpose: a manifest statistic, a columnar scan, a
        # point read through the identity postings, and a traversal over the
        # lazily built TCSR — different paths into the same generation
        return {
            "count": edge_count(s),
            "window": len(a.edges_columnar(vt_min=0, vt_max=10_000)["vt_s"]),
            "history": _answer_hash(call_operator(
                a, "entity_history", {"uid": "seed0", "include_edges": True})),
            "hop2": _answer_hash(call_operator(
                a, "snapshot_subgraph",
                {"seeds": ["seed0"], "hops": 2, "t_valid": 0})),
        }

    first = snapshot()
    ready.set()
    observations = [first]
    while not stop.is_set():
        observations.append(snapshot())
    Path(out).write_text(json.dumps({
        "generation_open": gen,
        "generation_close": a.generation,
        "observations": observations,
        "distinct": sorted({json.dumps(o, sort_keys=True) for o in observations}),
    }))
    s.close()


def _child_reopening_reader(root: str, out: str, stop, ready) -> None:
    """Reopen per query: this reader *does* advance across generations."""
    import tgms

    seen: list[tuple[int, int]] = []
    ready.set()
    while not stop.is_set():
        s = tgms.open(root, backend="native", read_only=True)
        seen.append((s.adapter.generation, edge_count(s)))
        s.close()
    Path(out).write_text(json.dumps({"seen": seen}))


def _child_history_reader(root: str, out: str, as_of_tt: int, stop, ready) -> None:
    """Ask one fixed past-belief question over and over."""
    import tgms

    s = tgms.open(root, backend="native", read_only=True)
    answers = []
    ready.set()
    while not stop.is_set():
        rows = s.adapter.edges_columnar(as_of_tt=as_of_tt)
        answers.append(int(len(rows["vt_s"])))
    Path(out).write_text(json.dumps({"answers": sorted(set(answers))}))
    s.close()


def _child_writer(root: str, batches: int, rows: int) -> None:
    write_batches(Path(root), batches=batches, rows=rows)


def _run_mixed(tmp_path: Path, children: list, batches: int = BATCHES):
    """Start `children`, run the writer to completion, then stop and collect.

    `stop` is set in a `finally`: a reader that outlives its test blocks
    interpreter shutdown, so a failed assertion would hang the suite instead
    of reporting the failure.
    """
    stop = MP.Event()
    procs, readies = [], []
    try:
        for fn, args in children:
            ready = MP.Event()
            readies.append(ready)
            p = MP.Process(target=fn, args=(*args, stop, ready))
            p.start()
            procs.append(p)
        for r in readies:
            assert r.wait(180), "a reader never opened the store"

        w = MP.Process(target=_child_writer,
                       args=(str(tmp_path), batches, BATCH_ROWS))
        w.start()
        w.join(300)
        assert w.exitcode == 0, f"the writer failed: exit {w.exitcode}"
    finally:
        stop.set()
        for p in procs:
            p.join(180)
            if p.is_alive():
                p.terminate()
                p.join(30)
    for p in procs:
        assert p.exitcode == 0, f"a reader failed: exit {p.exitcode}"


# --------------------------------------------------------------------------- #
# 1. one generation per handle                                                 #
# --------------------------------------------------------------------------- #


def test_a_pinned_reader_answers_from_exactly_one_generation(tmp_path: Path):
    """A commit must be invisible to a handle that opened before it.

    The reader answers three questions of different shapes — a manifest
    statistic, a windowed columnar scan, and a point history — in a loop
    while twelve generations are published underneath it. Every answer must
    be the one its own generation gives. A single differing observation is a
    torn read: it means some part of the reader's view advanced while the
    rest did not.
    """
    seed_store(tmp_path)
    out = tmp_path / "pinned.json"
    _run_mixed(tmp_path, [(_child_pinned_reader, (str(tmp_path), str(out)))])

    got = json.loads(out.read_text())
    assert len(got["observations"]) > 1, "the reader did not get to loop"
    assert len(got["distinct"]) == 1, (
        f"a pinned reader saw {len(got['distinct'])} distinct states while a "
        f"writer committed: {got['distinct']}")
    assert got["generation_open"] == got["generation_close"], (
        "a read-only handle advanced its generation")

    # and the store really did move: the pinned view is stale on purpose
    import tgms
    after = tgms.open(tmp_path, backend="native", read_only=True)
    assert after.adapter.generation > got["generation_open"], (
        "the writer published nothing, so this test proved nothing")
    assert edge_count(after) > json.loads(out.read_text())["observations"][0]["count"]
    after.close()


# --------------------------------------------------------------------------- #
# 2. batch atomicity across reopens                                            #
# --------------------------------------------------------------------------- #


def test_a_reopening_reader_sees_whole_batches_in_order(tmp_path: Path):
    """Across generations a reader must still never see half a batch.

    Each writer batch adds exactly BATCH_ROWS edges, so a count that is not
    `base + k * BATCH_ROWS` is a partially visible commit. Counts must also
    never decrease: generations are published, not rolled forward.
    """
    seed_store(tmp_path)
    import tgms

    base = edge_count(tgms.open(tmp_path, backend="native", read_only=True))
    out = tmp_path / "reopen.json"
    _run_mixed(tmp_path, [(_child_reopening_reader, (str(tmp_path), str(out)))])

    seen = [tuple(x) for x in json.loads(out.read_text())["seen"]]
    assert len(seen) > 1, "the reader did not get to reopen"
    for gen, count in seen:
        assert (count - base) % BATCH_ROWS == 0, (
            f"generation {gen} exposed {count - base} rows, not a whole "
            f"number of {BATCH_ROWS}-row batches — a partially visible commit")
    gens = [g for g, _ in seen]
    counts = [c for _, c in seen]
    assert gens == sorted(gens), f"generations went backwards: {gens}"
    assert counts == sorted(counts), f"row counts went backwards: {counts}"
    assert len(set(counts)) > 1, (
        "the reader never observed a commit, so atomicity was not exercised")


# --------------------------------------------------------------------------- #
# 3. history is immutable under concurrent correction                          #
# --------------------------------------------------------------------------- #


def _child_correcting_writer(root: str, n: int) -> None:
    import tgms

    s = tgms.open(root, backend="native")
    for i in range(n):
        s.correct(EntityRef(kind="edge", src="seed0", dst="seed1",
                            rel_type="R", disc="s0"), {"w": 100 + i},
                  vt_s=0, vt_e=1)
    s.close()


def test_past_belief_is_unmoved_by_concurrent_corrections(tmp_path: Path):
    """A correction changes what we believe now, never what we believed then.

    The bi-temporal invariant under concurrency: a reader asking about belief
    at a transaction time before any of these corrections must get one answer,
    however many corrections land while it asks.
    """
    seed_store(tmp_path)
    import tgms

    s = tgms.open(tmp_path, backend="native")
    frozen_tt = s.clock.last_tt
    s.close()

    out = tmp_path / "history.json"
    stop, ready = MP.Event(), MP.Event()
    r = MP.Process(target=_child_history_reader,
                   args=(str(tmp_path), str(out), frozen_tt, stop, ready))
    r.start()
    try:
        assert ready.wait(180), "the reader never opened the store"
        w = MP.Process(target=_child_correcting_writer, args=(str(tmp_path), 10))
        w.start()
        w.join(300)
        assert w.exitcode == 0, f"the correcting writer failed: {w.exitcode}"
    finally:
        stop.set()
        r.join(180)
        if r.is_alive():
            r.terminate()
            r.join(30)
    assert r.exitcode == 0

    answers = json.loads(out.read_text())["answers"]
    assert len(answers) == 1, (
        f"belief at tt={frozen_tt} took {len(answers)} different values while "
        f"corrections landed: {answers}")


# --------------------------------------------------------------------------- #
# 4. opening is not writing                                                    #
# --------------------------------------------------------------------------- #


def test_a_read_only_handle_refuses_the_write_api(tmp_path: Path):
    import tgms

    seed_store(tmp_path)
    r = tgms.open(tmp_path, backend="native", read_only=True)
    gen = r.adapter.generation
    for call in (
        lambda: r.assert_node("nope", "N"),
        lambda: r.assert_edge("a", "b", "R"),
        lambda: r.correct(EntityRef(kind="node", uid="seed0"), {"p": 1}),
        lambda: r.retract(EntityRef(kind="node", uid="seed0"), 0),
        lambda: r.ingest_events([{"src": "a", "dst": "b", "rel_type": "R", "t": 1}]),
    ):
        with pytest.raises(TgmsError):
            call()
    assert r.adapter.generation == gen, "a refused write still published"
    r.close()


def test_opening_read_only_mid_commit_publishes_nothing(tmp_path: Path):
    """The window between the write-ahead log fsync and the manifest publish.

    The writer appends the batch to the event log and fsyncs it *before*
    applying it, so for the whole duration of every commit the log is ahead
    of the store — byte-for-byte the state a crash leaves behind, and the
    state suffix recovery (D-042) exists to repair. A read-write handle
    opened there repairs it, which means it publishes a generation
    concurrently with the writer publishing the same one. A read-only handle
    must do nothing at all.
    """
    import tgms
    from tgms.storage.base import make_op

    seed_store(tmp_path)
    w = tgms.open(tmp_path, backend="native")
    gen = w.adapter.generation
    log_size = w.eventlog.size()

    # stop the writer exactly where every commit spends its time
    tt = w.clock.tick()
    ops = [make_op("assert_edge", src="mid", dst="commit", rel_type="R",
                   props={}, vt_s=99, vt_e=100, disc="",
                   source="ingest", provenance_ref=None)]
    w.eventlog.append(tt, ops)
    assert w.eventlog.size() > log_size

    dict_log = tmp_path / "native" / "dict.log"
    before = dict_log.read_bytes()

    r = tgms.open(tmp_path, backend="native", read_only=True)
    assert r.adapter.generation == gen, (
        "a reader published the writer's un-applied batch: two writers")
    r.close()
    assert dict_log.read_bytes() == before, "a reader mutated the store"

    # the writer's own commit still lands, and the store is healthy
    w.adapter.begin()
    w.adapter.apply_ops(ops, tt)
    w.adapter.commit()
    assert w.adapter.generation == gen + 1
    w.close()

    re = tgms.open(tmp_path, backend="native")
    assert re.adapter.verify()["problems"] == []
    re.close()


def _child_opener(root: str, out: str, stop, ready) -> None:
    """Open and close read-only as fast as possible, recording failures."""
    import tgms

    opens, failures = 0, []
    ready.set()
    while not stop.is_set():
        try:
            s = tgms.open(root, backend="native", read_only=True)
            s.close()
            opens += 1
        except Exception as e:  # noqa: BLE001 — the failure is the result
            failures.append(repr(e)[:200])
            break
    Path(out).write_text(json.dumps({"opens": opens, "failures": failures}))


def test_readers_opening_throughout_a_write_run_never_damage_the_store(tmp_path: Path):
    """The stochastic companion to the deterministic engine-level tests.

    Three processes open and close the store as fast as they can while the
    writer publishes twelve generations, so opens land at every phase of a
    commit rather than at the one phase a hand-built interleave chooses.
    Every open must succeed, and the store must verify and replay identically
    afterwards.
    """
    import tgms

    seed_store(tmp_path)
    outs = [tmp_path / f"open{i}.json" for i in range(3)]
    _run_mixed(tmp_path, [(_child_opener, (str(tmp_path), str(o))) for o in outs])

    total = 0
    for o in outs:
        got = json.loads(o.read_text())
        assert got["failures"] == [], f"opening the store failed: {got['failures']}"
        total += got["opens"]
    assert total > 10, f"only {total} opens — the window was not exercised"

    s = tgms.open(tmp_path, backend="native")
    assert s.adapter.verify()["problems"] == []
    digest = s.digest()
    s.close()

    # and the log still reproduces exactly this store
    from tgms.storage.eventlog import replay
    from tgms.storage.native import NativeAdapter

    fresh = NativeAdapter(tmp_path / "rebuilt")
    assert replay(tmp_path / "eventlog.jsonl", fresh) > 0
    assert fresh.store_digest() == digest, (
        "the store no longer matches its own event log")
    fresh.close()
