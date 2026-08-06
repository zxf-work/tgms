"""XTDB 2 as the first semantic competitor (D-083; plan docs/eval_xtdb.md).

Replays the reference event log into an XTDB 2 container **op-level** — XTDB
performs its own SQL:2011 portion supersession — then checks believed-state
agreement against the native store at probe points and times the six storage
operations of D-070. D-023 discipline: one log, both systems; content
compared, never derived ids.

The tt mapping is direct: one of our batches = one XTDB transaction opened
with `BEGIN READ WRITE WITH (SYSTEM_TIME = map(tt))`, sound because XTDB
requires non-decreasing system time and our log guarantees strictly
increasing tt (invariant I2).

    python scripts/xtdb_baseline.py --scale 10000 --density 5 --json out.json

Fairness (D-030): XTDB's recommended single-node image, its own idioms,
sync commits to match our fsync-per-commit durability, JVM warm-up before
timing, everything in the manifest. Wire time is included, as it was for
PostgreSQL and ClickHouse.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_harness as H  # noqa: E402
from eval_bitemporal import build_log  # noqa: E402

import tgms  # noqa: E402
from tgms.core.model import OPEN_END, canonical_json, edge_eid  # noqa: E402

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
IMAGE = "ghcr.io/xtdb/xtdb"
WARMUPS, REPS = 2, 5


def ts(t: int) -> datetime:
    """Our integer time -> timestamp; order- and gap-preserving (µs)."""
    return EPOCH + timedelta(microseconds=int(t))


def from_ts(d: datetime | None) -> int:
    """NULL `_valid_to` is XTDB's end-of-time and maps to our OPEN_END."""
    if d is None:
        return OPEN_END
    return int((d - EPOCH) / timedelta(microseconds=1))


# --- container lifecycle --------------------------------------------------- #


def start_container(port: int) -> str:
    name = f"tgms-xtdb-{port}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"{port}:5432", IMAGE],
        check=True, capture_output=True, text=True)
    return name


def wait_ready(port: int, timeout_s: float = 120.0):
    import psycopg

    from psycopg.types.string import StrDumper

    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg.connect(host="localhost", port=port, dbname="xtdb",
                                   autocommit=True)
            # psycopg sends str parameters untyped (OID 0) so servers can
            # infer; xtdb's pgwire refuses untyped DML parameters outright.
            # StrDumper declares them as text at the protocol level.
            conn.adapters.register_dumper(str, StrDumper)
            conn.execute("SELECT 1")
            return conn
        except Exception as e:  # noqa: BLE001 — retry until deadline
            last = e
            time.sleep(1.0)
    raise RuntimeError(f"xtdb not ready on :{port} after {timeout_s}s: {last}")


def image_digest() -> str:
    r = subprocess.run(["docker", "inspect", "--format", "{{.Id}}", IMAGE],
                       capture_output=True, text=True)
    return r.stdout.strip()


# --- op-level replay ------------------------------------------------------- #


def _vt_to_clause(vt_e: int) -> str:
    # omitted _valid_to means end-of-time; our OPEN_END means the same
    return "" if vt_e >= OPEN_END else ", _valid_to"


def replay(conn, log_path: Path) -> dict[str, Any]:
    """One XTDB transaction per batch, at SYSTEM_TIME = map(tt).

    Returns S5 measurements: wall, per-batch latencies, op counts. The
    first-seen node bookkeeping replicates `_ingest_events` exactly; the
    client-side `seen` set is sound because replay is the only writer.
    """
    cur = conn.cursor()
    seen_nodes: set[str] = set()
    n_ops = n_batches = 0
    latencies: list[float] = []
    t_start = time.perf_counter()

    with open(log_path) as f:
        for line in f:
            batch = json.loads(line)
            if "tt" not in batch:
                continue  # the format header line
            tt, ops = batch["tt"], batch["ops"]
            t0 = time.perf_counter()
            cur.execute(
                f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{ts(tt).isoformat()}')")
            for op in ops:
                _apply(cur, op, seen_nodes)
                n_ops += 1
            cur.execute("COMMIT")
            latencies.append((time.perf_counter() - t0) * 1000)
            n_batches += 1
    wall = time.perf_counter() - t_start
    return {"wall_s": round(wall, 2), "batches": n_batches, "ops": n_ops,
            "batch_ms_p50": round(statistics.median(latencies), 2),
            "batch_ms_p99": round(sorted(latencies)[max(0, int(len(latencies)*.99)-1)], 2)}


def _insert_node(cur, uid, label, props, source, prov, vt_s, vt_e, seen):
    # parameter types reach xtdb at the protocol level (StrDumper on the
    # connection); SQL-level casts are rejected by its grammar in portion
    # clauses, so none appear here
    cols = "_id, label, props, source, provenance_ref, _valid_from" + _vt_to_clause(vt_e)
    casts = ["%s"] * 6
    vals = [uid, label, canonical_json(props), source, prov, ts(vt_s)]
    if vt_e < OPEN_END:
        casts.append("%s")
        vals.append(ts(vt_e))
    cur.execute(f"INSERT INTO nodes ({cols}) VALUES ({', '.join(casts)})", vals)
    seen.add(uid)


def _apply(cur, op: dict[str, Any], seen_nodes: set[str]) -> None:
    kind = op["op"]
    if kind == "assert_node":
        _insert_node(cur, op["uid"], op["label"], op.get("props", {}),
                     op.get("source", "ingest"), op.get("provenance_ref"),
                     op["vt_s"], op.get("vt_e", OPEN_END), seen_nodes)
    elif kind == "assert_edge":
        eid = edge_eid(op["src"], op["dst"], op["rel_type"], op.get("disc", ""))
        vt_s, vt_e = op["vt_s"], op.get("vt_e", OPEN_END)
        cols = ("_id, src, dst, rel_type, disc, props, source, provenance_ref, "
                "_valid_from") + _vt_to_clause(vt_e)
        vals = [eid, op["src"], op["dst"], op["rel_type"], op.get("disc", ""),
                canonical_json(op.get("props", {})), op.get("source", "ingest"),
                op.get("provenance_ref"), ts(vt_s)]
        casts = ["%s"] * 9
        if vt_e < OPEN_END:
            vals.append(ts(vt_e))
            casts.append("%s")
        cur.execute(f"INSERT INTO edges ({cols}) VALUES ({', '.join(casts)})", vals)
        for u in (op["src"], op["dst"]):
            if u not in seen_nodes:
                _insert_node(cur, u, op.get(f"{'src' if u == op['src'] else 'dst'}_label", "") or "Node",
                             {}, "ingest", None, op["vt_s"], OPEN_END, seen_nodes)
    elif kind == "retract":
        table, ident = _table_ident(op)
        t = op["t"]
        # our retract truncates only versions valid AT t. Queries are
        # forbidden inside a DML transaction, which forced a better mapping
        # than a boundary read: the row filter `_valid_from <= t` keeps the
        # portion delete off later disjoint versions our retract leaves
        # believed, in one statement, correct even against same-batch writes.
        cur.execute(
            f"DELETE FROM {table} FOR PORTION OF VALID_TIME FROM %s "
            f"TO NULL WHERE _id = %s AND _valid_from <= %s",
            (ts(t), ident, ts(t)))
    elif kind == "correct":
        table, ident = _table_ident(op)
        vt_s, vt_e = op["vt_s"], op.get("vt_e", OPEN_END)
        # TO NULL is XTDB's end-of-time; a year-9999 literal provably is NOT
        # (probed: it splits the open interval and leaves a believed sliver)
        end = f"TIMESTAMP '{ts(vt_e).isoformat()}'" if vt_e < OPEN_END else "NULL"
        cur.execute(
            f"UPDATE {table} FOR PORTION OF VALID_TIME FROM %s TO {end} "
            f"SET props = %s, source = %s WHERE _id = %s",
            (ts(vt_s), canonical_json(op["props"]), op.get("source", "ingest"), ident))
    elif kind == "ingest_events":
        # bulk load through executemany, which psycopg pipelines: one
        # prepared per-row statement, many bindings, few round trips. The
        # obvious alternative — multi-row VALUES chunks — was measured 4.7x
        # SLOWER on xtdb (36.9 s -> 172.5 s at 20k/20%): its SQL layer pays
        # per parameter in the statement text, so the fat statements lose to
        # the prepared thin one. D-030 says idioms written to win; this one
        # was chosen by measurement, not taste.
        offset = op.get("offset", 0)
        label = op.get("node_label", "Node")
        source = op.get("source", "ingest")
        prov = op.get("provenance_ref")
        first_seen: dict[str, int] = {}
        rows: list[tuple] = []
        for i, ev in enumerate(op["events"]):
            disc = ev.get("disc", f"#{offset + i}")
            vt_s = ev["vt_s"]
            vt_e = ev.get("vt_e") or vt_s + 1
            eid = edge_eid(ev["src"], ev["dst"], ev["rel_type"], disc)
            rows.append((eid, ev["src"], ev["dst"], ev["rel_type"], disc,
                         canonical_json(ev.get("props", {})), source, prov,
                         ts(vt_s), ts(vt_e)))
            for u in (ev["src"], ev["dst"]):
                if u not in first_seen or vt_s < first_seen[u]:
                    first_seen[u] = vt_s
        cur.executemany(
            "INSERT INTO edges (_id, src, dst, rel_type, disc, props, "
            "source, provenance_ref, _valid_from, _valid_to) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        node_rows = [(u, label, canonical_json({}), source, prov,
                      ts(first_seen[u]))
                     for u in sorted(u for u in first_seen if u not in seen_nodes)]
        if node_rows:
            cur.executemany(
                "INSERT INTO nodes (_id, label, props, source, provenance_ref, "
                "_valid_from) VALUES (%s,%s,%s,%s,%s,%s)", node_rows)
        seen_nodes.update(u for u, *_ in node_rows)
    else:
        raise ValueError(f"unknown op kind {kind}")


def _table_ident(op: dict[str, Any]) -> tuple[str, str]:
    ref = op["ref"]
    if ref["kind"] == "node":
        return "nodes", ref["uid"]
    return "edges", edge_eid(ref["src"], ref["dst"], ref["rel_type"], ref.get("disc", ""))


# --- probes and the six operations ----------------------------------------- #


def multi_touch_uids(log_path: Path, limit: int) -> list[str]:
    """Identities written more than once inside a single batch.

    The D-059 shape — a second op reading belief the first has already
    changed, inside one transaction — is where our semantics and a competitor
    are likeliest to diverge (F1), and uniform sampling rarely lands on it.
    """
    hits: list[str] = []
    with open(log_path) as f:
        for line in f:
            batch = json.loads(line)
            if "tt" not in batch:
                continue
            touched: dict[str, int] = {}
            for op in batch["ops"]:
                uid = op.get("uid") or (op.get("ref", {}).get("uid"))
                if uid:
                    touched[uid] = touched.get(uid, 0) + 1
            hits.extend(u for u, n in touched.items() if n > 1)
            if len(hits) >= limit:
                break
    return sorted(set(hits))[:limit]


def probe_points(adapter, n_identities: int, final_tt: int,
                 must_include: list[str] | None = None) -> list[dict[str, Any]]:
    """Believed-state probes from the native store's own boundaries."""
    import random

    rnd = random.Random(83)
    uids = sorted({v.uid for v in adapter.all_node_versions()})
    picks = rnd.sample(uids, min(n_identities, len(uids)))
    for u in must_include or []:
        if u in set(uids) and u not in picks:
            picks.append(u)
    probes = []
    for uid in picks:
        versions = [v for v in adapter.all_node_versions() if v.uid == uid]
        tts = sorted({v.tt_s for v in versions}) + [final_tt]
        vts = sorted({v.vt_s for v in versions})
        for tt in tts[-4:]:
            for vt in vts[:3]:
                probes.append({"uid": uid, "vt": vt, "tt": tt})
    return probes


def native_believed(adapter, uid: str, vt: int, tt: int) -> str | None:
    hits = [v for v in adapter.believed_node_versions(uid, tt)
            if v.vt_s <= vt < v.vt_e]
    if not hits:
        return None
    return canonical_json(hits[0].props)


def xtdb_believed(cur, uid: str, vt: int, tt: int) -> str | None:
    cur.execute(
        "SELECT props FROM nodes FOR VALID_TIME AS OF %s "
        "FOR SYSTEM_TIME AS OF %s WHERE _id = %s",
        (ts(vt), ts(tt), uid))
    row = cur.fetchone()
    return row[0] if row else None


def timed(fn, *args) -> tuple[float, Any]:
    for _ in range(WARMUPS):
        fn(*args)
    times, out = [], None
    for _ in range(REPS):
        t0 = time.perf_counter()
        out = fn(*args)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), out


# --- main ------------------------------------------------------------------ #


def scenario_d059(port: int) -> int:
    """The in-batch supersession shape, crafted — F1's untested cell.

    The §13-style log contains no identity written twice inside one batch
    (`d059_targeted_identities: 0`), so the shape D-059 found — a second op
    reading belief the first op of the same batch already changed — goes
    unprobed by the record runs. This scenario builds it directly:

      tt1  assert  n1 [0,100)  v1
      tt2  assert  n1 [0,100)  v2  THEN  assert n1 [40,60) v3   (one batch —
           the v2 version is carved by an op in its own transaction, so its
           middle never existed as a belief, D-059)
      tt3  correct n1 [0,20)   v4  THEN  retract n1 at 50       (one batch —
           the retract must truncate [40,60) to [40,50) and leave [60,100)
           believed, which is exactly the `_valid_from <= t` filter's job)
      tt4  assert  n2 (control)

    Probes cover every fragment and every batch boundary on both systems.
    """
    from tgms.storage.eventlog import EventLog

    work = Path(tempfile.mkdtemp(prefix="tgms-xtdb-d059-"))
    log_path = work / "eventlog.jsonl"
    log = EventLog(log_path)
    t1, t2, t3, t4 = 1_000_000, 2_000_000, 3_000_000, 4_000_000
    log.append(t1, [{"op": "assert_node", "uid": "n1", "label": "N",
                     "props": {"v": 1}, "vt_s": 0, "vt_e": 100}])
    log.append(t2, [{"op": "assert_node", "uid": "n1", "label": "N",
                     "props": {"v": 2}, "vt_s": 0, "vt_e": 100},
                    {"op": "assert_node", "uid": "n1", "label": "N",
                     "props": {"v": 3}, "vt_s": 40, "vt_e": 60}])
    log.append(t3, [{"op": "correct", "ref": {"kind": "node", "uid": "n1"},
                     "props": {"v": 4}, "vt_s": 0, "vt_e": 20},
                    {"op": "retract", "ref": {"kind": "node", "uid": "n1"},
                     "t": 50}])
    log.append(t4, [{"op": "assert_node", "uid": "n2", "label": "N",
                     "props": {"v": 9}, "vt_s": 0, "vt_e": 100}])

    native_path = work / "store"
    H.load_store(native_path, "native", log_path)
    store = tgms.open(native_path, backend="native")
    adapter = store.adapter

    name = start_container(port)
    try:
        conn = wait_ready(port)
        cur = conn.cursor()
        replay(conn, log_path)
        bad = 0
        for uid in ("n1", "n2"):
            for vt in (0, 10, 19, 20, 39, 40, 45, 49, 50, 55, 59, 60, 70, 99):
                for tt in (t1, t2, t3, t4, t4 + 1):
                    ours = native_believed(adapter, uid, vt, tt)
                    theirs = xtdb_believed(cur, uid, vt, tt)
                    if ours != theirs:
                        bad += 1
                        print(f"  DISAGREE {uid} vt={vt} tt={tt}: "
                              f"native={ours} xtdb={theirs}", flush=True)
        total = 2 * 14 * 5
        print(f"d059 scenario: {total} probes, {bad} disagreements", flush=True)
        return 0 if bad == 0 else 1
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=10_000)
    ap.add_argument("--density", type=float, default=5.0, help="correction %%")
    ap.add_argument("--port", type=int, default=54321)
    ap.add_argument("--probes", type=int, default=25, help="identities probed")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--scenario", choices=["d059"],
                    help="run a crafted semantic scenario instead of the sweep")
    args = ap.parse_args()

    if args.scenario == "d059":
        return scenario_d059(args.port)

    print(f"reference log: {args.scale} events at {args.density}% corrections …",
          flush=True)
    data, counts = build_log(args.scale, args.density)

    print("native store: replaying reference log …", flush=True)
    native_path = Path(tempfile.mkdtemp(prefix="tgms-xtdb-native-")) / "store"
    t0 = time.perf_counter()
    H.load_store(native_path, "native", data.log)
    native_replay_s = time.perf_counter() - t0
    store = tgms.open(native_path, backend="native")
    adapter = store.adapter
    final_tt = max(v.tt_s for v in adapter.all_node_versions())

    print("xtdb: starting container …", flush=True)
    name = start_container(args.port)
    rec: dict[str, Any] = {"corrections": counts}
    try:
        conn = wait_ready(args.port)
        cur = conn.cursor()

        print("xtdb: op-level replay …", flush=True)
        rec["s5_ingest"] = {"xtdb": replay(conn, Path(data.log))}
        rec["s5_ingest"]["native_replay_s"] = round(native_replay_s, 2)
        print(f"  xtdb replay {rec['s5_ingest']['xtdb']['wall_s']}s "
              f"(native {native_replay_s:.2f}s)", flush=True)

        print("agreement: believed-state probes …", flush=True)
        d059_uids = multi_touch_uids(Path(data.log), limit=10)
        probes = probe_points(adapter, args.probes, final_tt,
                              must_include=d059_uids)
        rec["d059_targeted_identities"] = len(d059_uids)
        disagreements = []
        for p in probes:
            ours = native_believed(adapter, p["uid"], p["vt"], p["tt"])
            theirs = xtdb_believed(cur, p["uid"], p["vt"], p["tt"])
            if ours != theirs:
                disagreements.append({**p, "native": ours, "xtdb": theirs})
        rec["agreement"] = {"probes": len(probes),
                           "disagreements": len(disagreements),
                           "sample": disagreements[:10]}
        print(f"  {len(probes)} probes, {len(disagreements)} disagreements",
              flush=True)

        # the six operations, timed (S5 already measured during replay)
        sample_uid = probes[0]["uid"]
        tts = sorted({p["tt"] for p in probes})
        mid_tt = tts[min(len(tts) - 1, len(tts) // 2)] if tts else final_tt
        mid_vt = probes[0]["vt"]

        def s1(): return xtdb_believed(cur, sample_uid, mid_vt, final_tt + 1)
        def s2():
            cur.execute("SELECT props FROM nodes FOR VALID_TIME AS OF %s "
                        "WHERE _id=%s", (ts(mid_vt), sample_uid))
            return cur.fetchall()

        def s3():
            cur.execute("SELECT props FROM nodes FOR SYSTEM_TIME AS OF %s "
                        "WHERE _id=%s", (ts(mid_tt), sample_uid))
            return cur.fetchall()
        def s4():
            cur.execute("SELECT _valid_from,_valid_to,_system_from,props FROM nodes "
                        "FOR ALL VALID_TIME FOR ALL SYSTEM_TIME WHERE _id=%s "
                        "ORDER BY _system_from,_valid_from", (sample_uid,))
            return cur.fetchall()
        def s6():
            cur.execute(
                "SELECT a._id FROM (SELECT _id, props FROM nodes FOR SYSTEM_TIME AS OF %s) a "
                "JOIN (SELECT _id, props FROM nodes FOR SYSTEM_TIME AS OF %s) b "
                "ON a._id = b._id WHERE a.props <> b.props",
                (ts(mid_tt), ts(final_tt + 1)))
            return cur.fetchall()

        def n1(): return native_believed(adapter, sample_uid, mid_vt, OPEN_END)
        def n2(): return [v for v in adapter.believed_node_versions(sample_uid)
                          if v.vt_s <= mid_vt < v.vt_e]
        def n3(): return adapter.believed_node_versions(sample_uid, mid_tt)
        def n4(): return [v for v in adapter.all_node_versions() if v.uid == sample_uid]
        def n6():
            # the native diff idiom (diff.global's shape): columnar scans at
            # each tt, vid comparison first, props materialized only for
            # identities whose believed version changed. The earlier draft
            # full-scanned all_node_versions twice, which is not our idiom
            # and would have flattered XTDB.
            def believed_at(tt):
                c = adapter.nodes_columnar(as_of_tt=tt, vt_min=mid_vt,
                                           vt_max=mid_vt + 1)
                return dict(zip(c["uid"].tolist(), c["vid"].tolist()))
            old, new = believed_at(mid_tt), believed_at(OPEN_END)
            changed = [u for u in old.keys() & new.keys() if old[u] != new[u]]
            vids = [old[u] for u in changed] + [new[u] for u in changed]
            props = adapter.props_for_vids("node", vids)
            return [u for u in changed
                    if props.get(old[u]) != props.get(new[u])]

        rec["ops"] = {}
        for label, xf, nf in (("s1_current", s1, n1), ("s2_vt_asof", s2, n2),
                              ("s3_tt_asof", s3, n3), ("s4_history", s4, n4),
                              ("s6_diff", s6, n6)):
            xt, _ = timed(xf)
            nt, _ = timed(nf)
            rec["ops"][label] = {"xtdb_ms": round(xt, 3), "native_ms": round(nt, 3)}
            print(f"  {label}: xtdb {xt:.2f} ms  native {nt:.2f} ms", flush=True)

        # F7 — storage, as D-070's footprints allow at this stage: bytes on
        # disk for each system's store of the same log. XTDB's is read from
        # inside the container (its /var/lib/xtdb); ours is the store dir.
        xdu = subprocess.run(
            ["docker", "exec", name, "du", "-sb", "/var/lib/xtdb"],
            capture_output=True, text=True)
        ndu = subprocess.run(["du", "-sk", str(native_path)],
                             capture_output=True, text=True)
        rec["storage_bytes"] = {
            "xtdb": int(xdu.stdout.split()[0]) if xdu.returncode == 0 and xdu.stdout else None,
            "native": int(ndu.stdout.split()[0]) * 1024 if ndu.stdout else None,
        }

        rec["manifest"] = {
            "commit": subprocess.run(["git", "rev-parse", "HEAD"],
                                     capture_output=True, text=True).stdout.strip(),
            "host": platform.node(), "image": IMAGE, "image_id": image_digest(),
            "scale": args.scale, "density_pct": args.density,
            "commit_mode": "sync (default)", "wire": "pgwire+psycopg",
        }
    finally:
        if args.keep:
            print(f"container kept: {name}", flush=True)
        else:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        store.close()

    if args.json:
        args.json.write_text(json.dumps(rec, indent=1, default=str) + "\n")
        print(f"record → {args.json}", flush=True)
    return 0 if rec["agreement"]["disagreements"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
