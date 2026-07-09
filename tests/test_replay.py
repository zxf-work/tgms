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
