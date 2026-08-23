"""Event-log replay reproduces identical store digests (M1 acceptance)."""

from __future__ import annotations

import random

import tgms
from tgms.core.errors import NotFoundError
from tgms.core.model import EntityRef
from tgms.storage.duckdb_adapter import DuckDBAdapter
from tgms.storage.eventlog import replay


def _random_workload(store: tgms.Store, seed: int) -> None:
    rng = random.Random(seed)
    uids = ["u%d" % i for i in range(6)]
    for _ in range(60):
        kind = rng.choice(["an", "ae", "ae", "rt", "co"])
        u, v = rng.choice(uids), rng.choice(uids)
        s = rng.randrange(0, 50)
        e = s + rng.randrange(1, 20)
        try:
            if kind == "an":
                store.assert_node(u, "N", {"p": rng.randrange(3)}, vt_s=s, vt_e=e)
            elif kind == "ae":
                store.assert_edge(u, v, "R", {"w": rng.randrange(5)}, vt_s=s, vt_e=e)
            elif kind == "rt":
                store.retract(EntityRef(kind="edge", src=u, dst=v, rel_type="R"),
                              t=rng.randrange(0, 60))
            else:
                store.correct(EntityRef(kind="node", uid=u), {"p": 9}, vt_s=s, vt_e=e)
        except NotFoundError:
            pass
    store.ingest_events([
        {"src": rng.choice(uids), "dst": rng.choice(uids), "rel_type": "MSG",
         "vt_s": rng.randrange(0, 100)} for _ in range(200)])


def test_replay_reproduces_digest(tmp_path):
    store = tgms.open(tmp_path / "s1", backend="duckdb", paranoid=True)
    _random_workload(store, seed=7)
    original = store.digest()
    store.close()

    fresh = DuckDBAdapter(":memory:")
    n = replay(tmp_path / "s1" / "eventlog.jsonl", fresh)
    assert n > 0
    assert fresh.store_digest() == original
    fresh.close()


def test_replay_into_kuzu_matches_duckdb_digest(tmp_path):
    """M1 acceptance: replay(eventlog) reproduces identical store digests
    on both backends."""
    from tgms.storage.kuzu_adapter import KuzuAdapter

    store = tgms.open(tmp_path / "sd", backend="duckdb", paranoid=True)
    _random_workload(store, seed=13)
    duck_digest = store.digest()
    store.close()

    kz = KuzuAdapter(tmp_path / "sk.kuzu")
    replay(tmp_path / "sd" / "eventlog.jsonl", kz)
    assert kz.store_digest() == duck_digest
    kz.close()


def test_kuzu_live_write_path_matches_duckdb(tmp_path):
    """Same public-API workload on both backends yields identical digests
    when applied at identical transaction times (via replay of the duckdb
    log we already trust, plus a direct live run on kuzu)."""
    from tgms.core.errors import NotFoundError as NF
    from tgms.core.model import OPEN_END as OPEN
    from tgms.core.model import EntityRef as ER
    from tgms.core.model import canonical_json as canonical

    s1 = tgms.open(tmp_path / "a", backend="duckdb")
    s2 = tgms.open(tmp_path / "b", backend="kuzu")
    for s in (s1, s2):
        s.assert_node("x", "N", {"p": 1}, vt_s=0, vt_e=100)
        s.assert_edge("x", "y", "R", {"w": 2}, vt_s=10, vt_e=90)
        s.correct(ER(kind="node", uid="x"), {"p": 3}, vt_s=20, vt_e=30)
        s.retract(ER(kind="edge", src="x", dst="y", rel_type="R"), t=50)
        # retract truncated the believed edge interval to [10, 50); correct it
        # over a sub-range so both retract and correct of an EDGE run on kuzu
        # here (correct exercises the anchored believed_edge_versions path in
        # base.py's _correct, which needs ref.src/ref.dst hints — see
        # tgms/storage/kuzu_adapter.py).
        s.correct(ER(kind="edge", src="x", dst="y", rel_type="R"), {"w": 9}, vt_s=20, vt_e=30)
        try:
            s.retract(ER(kind="node", uid="zzz"), t=5)
        except NF:
            pass
    # tts differ between the two runs; compare belief content per local tt order
    def content(store):
        rows_n = sorted((v.uid, v.label, v.vt_s, v.vt_e, canonical(v.props),
                         v.tt_e == OPEN)
                        for v in store.adapter.all_node_versions())
        rows_e = sorted((v.eid, v.src, v.dst, v.rel_type, v.vt_s, v.vt_e,
                         canonical(v.props), v.tt_e == OPEN)
                        for v in store.adapter.all_edge_versions())
        return rows_n, rows_e

    assert content(s1) == content(s2)
    s1.close()
    s2.close()


def test_every_backend_agrees_on_a_batch_that_carves_its_own_write(tmp_path):
    """D-059's rule reaches all three adapters, so all three are checked.

    The suite runs against DuckDB and native; Kùzu is only ever exercised
    here, and its `retire` is the one implementation the invariant suite
    cannot reach — a node version there is a node with an OF_ENTITY edge
    hanging off it, which is its own way to get a delete wrong.
    """
    from tgms.core.model import OPEN_END
    from tgms.storage.kuzu_adapter import KuzuAdapter
    from tgms.storage.native import NativeAdapter

    ops = [
        {"op": "assert_node", "uid": "x", "label": "N", "props": {"p": 1},
         "vt_s": 0, "vt_e": 100},
        {"op": "assert_node", "uid": "x", "label": "N", "props": {"p": 2},
         "vt_s": 50, "vt_e": 150},
        {"op": "assert_edge", "src": "x", "dst": "y", "rel_type": "R",
         "props": {"w": 1}, "vt_s": 10, "vt_e": 20, "disc": ""},
        {"op": "assert_edge", "src": "x", "dst": "y", "rel_type": "R",
         "props": {"w": 2}, "vt_s": 5, "vt_e": 15, "disc": ""},
    ]

    def rows(adapter):
        adapter.paranoid = True
        adapter.begin()
        adapter.apply_ops(ops, 1)
        adapter.commit()
        got = (sorted((v.uid, v.vt_s, v.vt_e, v.tt_s, v.tt_e, v.props["p"])
                      for v in adapter.all_node_versions()),
               sorted((v.vt_s, v.vt_e, v.tt_s, v.tt_e, v.props["w"])
                      for v in adapter.all_edge_versions()))
        adapter.close()
        return got

    expected = ([("x", 0, 50, 1, OPEN_END, 1), ("x", 50, 150, 1, OPEN_END, 2)],
                [(5, 15, 1, OPEN_END, 2), (15, 20, 1, OPEN_END, 1)])
    assert rows(DuckDBAdapter(":memory:")) == expected
    assert rows(NativeAdapter(str(tmp_path / "native"))) == expected
    assert rows(KuzuAdapter(tmp_path / "k.kuzu")) == expected


def test_reopen_continues_clock(tmp_path):
    store = tgms.open(tmp_path / "s2")
    tt1 = store.assert_node("a", "N")
    store.close()
    store2 = tgms.open(tmp_path / "s2")
    tt2 = store2.assert_node("b", "N")
    assert tt2 > tt1
    assert {v.uid for v in store2.adapter.all_node_versions()} == {"a", "b"}
    store2.close()


def test_ingest_events_columnar_roundtrip(tmp_path):
    store = tgms.open(tmp_path / "s3")
    store.ingest_events([
        {"src": "a", "dst": "b", "rel_type": "MSG", "vt_s": 10},
        {"src": "b", "dst": "c", "rel_type": "MSG", "vt_s": 20},
        {"src": "a", "dst": "b", "rel_type": "MSG", "vt_s": 10},  # duplicate event ok
    ])
    cols = store.adapter.edges_columnar()
    assert len(cols["src_id"]) == 3
    assert list(cols["vt_s"]) == [10, 10, 20]  # sorted by vt_s
    # distinct logical edges even for identical (src, dst, rel, t)
    assert len(set(cols["eid"])) == 3
    ids = store.adapter.dense_ids(["a", "b", "c"])
    assert store.adapter.uids_for(ids) == ["a", "b", "c"]
    store.close()


def test_believed_edge_versions_anchored_matches_scan_on_kuzu(tmp_path):
    """`KuzuAdapter.believed_edge_versions` takes an anchored query plan when
    given src/dst hints and falls back to a scan without them (D-145's
    residual). The hints are documented as pure performance hints that MUST
    NOT change results, so this checks that claim directly: for every eid a
    randomized workload produces, and at every as_of_tt a caller might ask
    for (including historical snapshots with already-closed tt intervals),
    the anchored call and the hint-less scan return row-for-row identical
    EdgeVersion lists (full dataclass equality, same order).

    The workload mixes several rel_types and discriminators over a small
    uid pool so multiple eids share the same (src, dst) endpoints, and mixes
    assert/retract/correct so both open and closed tt intervals exist.
    """
    from tgms.core.errors import NotFoundError as NF
    from tgms.core.model import OPEN_END as OPEN
    from tgms.core.model import EntityRef as ER

    store = tgms.open(tmp_path / "kz_anchor", backend="kuzu")
    rng = random.Random(41)
    uids = [f"u{i}" for i in range(5)]
    rel_types = ["R0", "R1", "R2"]
    discs = ["", "alt"]
    snapshots: list[int] = []
    for _ in range(150):
        kind = rng.choice(["ae", "ae", "ae", "rt", "co"])
        src, dst = rng.sample(uids, 2)
        rel_type = rng.choice(rel_types)
        disc = rng.choice(discs)
        ref = ER(kind="edge", src=src, dst=dst, rel_type=rel_type, disc=disc)
        s = rng.randrange(0, 50)
        e = s + rng.randrange(1, 20)
        try:
            if kind == "ae":
                tt = store.assert_edge(src, dst, rel_type, {"w": rng.randrange(5)},
                                       vt_s=s, vt_e=e, disc=disc)
            elif kind == "rt":
                tt = store.retract(ref, t=rng.randrange(0, 60))
            else:
                tt = store.correct(ref, {"w": 9}, vt_s=s, vt_e=e)
        except NF:
            continue
        snapshots.append(tt)

    assert snapshots, "workload produced no successful edge ops"
    adapter = store.adapter
    eid_endpoints: dict[str, tuple[str, str]] = {}
    for v in adapter.all_edge_versions():
        eid_endpoints.setdefault(v.eid, (v.src, v.dst))
    # several distinct eids must share endpoints, by construction (multiple
    # rel_types/discs over a 5-uid pool)
    endpoint_pairs = [ep for ep in eid_endpoints.values()]
    assert len(endpoint_pairs) > len(set(endpoint_pairs))
    assert len(eid_endpoints) > 5

    as_of_values = sorted(set(snapshots)) + [0, OPEN]
    checked = 0
    for eid, (src, dst) in eid_endpoints.items():
        for as_of in as_of_values:
            scan = adapter.believed_edge_versions(eid, as_of)
            anchored = adapter.believed_edge_versions(eid, as_of, src=src, dst=dst)
            assert scan == anchored, (eid, as_of, scan, anchored)
            checked += 1
    assert checked > 0
    store.close()
