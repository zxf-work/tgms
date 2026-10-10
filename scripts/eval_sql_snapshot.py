#!/usr/bin/env python
"""Stress test: page/count pairs under a concurrent writer.

Protocol: external_workloads/probe/SNAPSHOT_FREEZE.md (frozen before
any run; its sha256 is recorded in the receipt).

For each engine, input size N and variant (unsafe = two autocommit
statements, consistent = tgms.evidence.sql_snapshot), a fresh table
holds N qualifying rows (q = 1) and N non-qualifying rows (q = 0). A
writer thread commits single-row inserts and deletes of qualifying rows
at a fixed interval; every commit also sets meta.gen to a new
generation, and the writer records, BEFORE committing, the size and the
first `page` ids of the qualifying set at that generation. The reader
runs T trials of

    page:  SELECT s.id, m.gen, COUNT(*) OVER ()
           FROM (SELECT id FROM items WHERE q = 1) s CROSS JOIN meta m
           ORDER BY s.id LIMIT page
    sleep  delay   (between the page and the count)
    count: SELECT COUNT(*) FROM (SELECT id FROM items WHERE q = 1
                                 ORDER BY id)

The page statement reads its own generation, so the result the page
came from is known exactly: size_p = |qualifying set at gen_p|. A pair
is consistent iff count == size_p.

    PYTHONPATH=. python scripts/eval_sql_snapshot.py \
        --workdir /tmp/snap --receipt benchmarks/results-v1/eval-sql-snapshot.json
"""

from __future__ import annotations

import argparse
import bisect
import datetime
import hashlib
import json
import math
import os
import platform
import random
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from tgms.evidence.adapter_sql import build_sql_ecqr
from tgms.evidence.sql_snapshot import (consistent_page_and_count,
                                        unsafe_page_and_count)

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "external_workloads" / "probe" / "SNAPSHOT_FREEZE.md"

SPACING, HALF = 1000, 500  # qualifying ids SPACING*i, others +HALF
SEMANTIC_SQL = "SELECT id FROM items WHERE q = 1 ORDER BY id"
RECORD_FIELDS = ["trial", "page_len", "count", "size_p", "gen_p",
                 "writer_commits_in_trial", "writer_commits_in_window",
                 "consistent_pair",
                 "page_prefix_ok", "stmt_count_ok", "certificate_wrong",
                 "latency_ms"]
VARIANTS = {"unsafe": unsafe_page_and_count,
            "consistent": consistent_page_and_count}


def page_sql(page: int) -> str:
    return ("SELECT s.id, m.gen, COUNT(*) OVER () AS n_stmt "
            "FROM (SELECT id FROM items WHERE q = 1) s CROSS JOIN meta m "
            f"ORDER BY s.id LIMIT {int(page)}")


def wilson_upper(k: int, n: int, z: float = 1.959964) -> float:
    if n == 0:
        return 1.0
    p = k / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c + r) / den


# --------------------------------------------------------------- backends

class Backend:
    """One engine instance: a reader connection, a writer connection,
    and the API engine name."""

    api_engine = ""

    def __init__(self, n: int, workdir: Path):
        self.n = n
        self.workdir = workdir

    # reader-side
    reader = None

    def close(self) -> None:
        pass

    # writer-side: apply one op and set meta.gen, atomically
    def write(self, op: str, ident: int, gen: int) -> None:
        raise NotImplementedError


class SQLiteBackend(Backend):
    api_engine = "sqlite"

    def __init__(self, n, workdir, journal_mode):
        super().__init__(n, workdir)
        self.path = workdir / f"stress_{journal_mode}_{n}.sqlite"
        for suf in ("", "-wal", "-shm", "-journal"):
            p = Path(str(self.path) + suf)
            if p.exists():
                p.unlink()
        con = sqlite3.connect(self.path, isolation_level=None)
        mode = con.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
        if mode.lower() != journal_mode.lower():
            raise RuntimeError(f"journal_mode {journal_mode} not set: {mode}")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, "
                    "q INTEGER NOT NULL)")
        con.execute("CREATE TABLE meta (k INTEGER PRIMARY KEY, "
                    "gen INTEGER NOT NULL)")
        con.execute("BEGIN")
        con.executemany("INSERT INTO items VALUES (?, ?)",
                        [(SPACING * i, 1) for i in range(n)]
                        + [(SPACING * i + HALF, 0) for i in range(n)])
        con.execute("INSERT INTO meta VALUES (0, 0)")
        con.execute("COMMIT")
        con.close()
        self.journal_mode = journal_mode
        self.reader = sqlite3.connect(self.path, isolation_level=None,
                                      timeout=60, check_same_thread=False)
        self.writer = sqlite3.connect(self.path, isolation_level=None,
                                      timeout=60, check_same_thread=False)

    def write(self, op, ident, gen):
        w = self.writer
        try:
            w.execute("BEGIN IMMEDIATE")
            if op == "insert":
                w.execute("INSERT INTO items VALUES (?, 1)", (ident,))
            else:
                w.execute("DELETE FROM items WHERE id = ?", (ident,))
            w.execute("UPDATE meta SET gen = ? WHERE k = 0", (gen,))
            w.execute("COMMIT")
        except BaseException:
            if w.in_transaction:
                w.execute("ROLLBACK")
            raise

    def close(self):
        self.reader.close()
        self.writer.close()
        for suf in ("", "-wal", "-shm", "-journal"):
            p = Path(str(self.path) + suf)
            if p.exists():
                p.unlink()


class DuckDBBackend(Backend):
    api_engine = "duckdb"

    def __init__(self, n, workdir):
        super().__init__(n, workdir)
        import duckdb
        tmp = workdir / "duckdb_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(":memory:", config={
            "memory_limit": "2GB", "temp_directory": str(tmp)})
        self.db.execute("CREATE TABLE items (id BIGINT PRIMARY KEY, "
                        "q INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE meta (k INTEGER PRIMARY KEY, "
                        "gen BIGINT NOT NULL)")
        self.db.execute(f"INSERT INTO items SELECT range * {SPACING}, 1 "
                        f"FROM range({int(n)})")
        self.db.execute(f"INSERT INTO items SELECT range * {SPACING} + {HALF}, 0 "
                        f"FROM range({int(n)})")
        self.db.execute("INSERT INTO meta VALUES (0, 0)")
        self.reader = self.db.cursor()
        self.writer = self.db.cursor()
        self.version = duckdb.__version__

    def write(self, op, ident, gen):
        w = self.writer
        w.execute("BEGIN TRANSACTION")
        try:
            if op == "insert":
                w.execute("INSERT INTO items VALUES (?, 1)", [ident])
            else:
                w.execute("DELETE FROM items WHERE id = ?", [ident])
            w.execute("UPDATE meta SET gen = ? WHERE k = 0", [gen])
            w.execute("COMMIT")
        except BaseException:
            w.execute("ROLLBACK")
            raise

    def close(self):
        self.reader.close()
        self.writer.close()
        self.db.close()


def pg_connect(uri: str):
    try:
        import psycopg
        return psycopg.connect(uri), "psycopg " + psycopg.__version__
    except ImportError:
        import psycopg2
        return psycopg2.connect(uri), "psycopg2 " + psycopg2.__version__


class PostgresBackend(Backend):
    api_engine = "postgres"
    _server = None

    @classmethod
    def server(cls, pgdata: Path):
        if cls._server is None:
            import pgserver
            pgdata.mkdir(parents=True, exist_ok=True)
            cls._server = pgserver.get_server(pgdata, cleanup_mode="stop")
        return cls._server

    def __init__(self, n, workdir):
        super().__init__(n, workdir)
        uri = self.server(workdir / "pgdata").get_uri()
        setup, self.driver = pg_connect(uri)
        setup.autocommit = True
        cur = setup.cursor()
        cur.execute("DROP TABLE IF EXISTS items")
        cur.execute("DROP TABLE IF EXISTS meta")
        cur.execute("CREATE TABLE items (id BIGINT PRIMARY KEY, "
                    "q INTEGER NOT NULL)")
        cur.execute("CREATE TABLE meta (k INTEGER PRIMARY KEY, "
                    "gen BIGINT NOT NULL)")
        cur.execute(f"INSERT INTO items SELECT g * {SPACING}, 1 FROM "
                    "generate_series(0, %s) g", (n - 1,))
        cur.execute(f"INSERT INTO items SELECT g * {SPACING} + {HALF}, 0 "
                    "FROM generate_series(0, %s) g", (n - 1,))
        cur.execute("INSERT INTO meta VALUES (0, 0)")
        cur.execute("ANALYZE")
        cur.execute("SHOW server_version")
        self.version = cur.fetchone()[0]
        cur.close()
        setup.close()
        self.version = f"{self.version} ({self.driver})"
        self.reader, _ = pg_connect(uri)
        self.reader.autocommit = True
        self.writer, _ = pg_connect(uri)
        self.writer.autocommit = False

    def write(self, op, ident, gen):
        w = self.writer
        cur = w.cursor()
        try:
            if op == "insert":
                cur.execute("INSERT INTO items VALUES (%s, 1)", (ident,))
            else:
                cur.execute("DELETE FROM items WHERE id = %s", (ident,))
            cur.execute("UPDATE meta SET gen = %s WHERE k = 0", (gen,))
            w.commit()
        except BaseException:
            w.rollback()
            raise
        finally:
            cur.close()

    def close(self):
        self.reader.close()
        self.writer.close()


def make_backend(engine: str, n: int, workdir: Path) -> Backend:
    if engine == "sqlite-wal":
        return SQLiteBackend(n, workdir, "wal")
    if engine == "sqlite-rollback":
        return SQLiteBackend(n, workdir, "delete")
    if engine == "duckdb":
        return DuckDBBackend(n, workdir)
    if engine == "postgres":
        return PostgresBackend(n, workdir)
    raise ValueError(engine)


# ----------------------------------------------------------------- writer

class Writer(threading.Thread):
    """Single mutator. Its in-memory qualifying set is the oracle: the
    state recorded under generation g is exactly what a statement that
    reads meta.gen = g sees."""

    def __init__(self, backend: Backend, n: int, page: int, delta: int,
                 interval_s: float, seed: int):
        super().__init__(daemon=True)
        self.b, self.n, self.page = backend, n, page
        self.delta, self.interval = delta, interval_s
        self.rng = random.Random(seed)
        self.qual = [SPACING * i for i in range(n)]
        # ids are never reused: a reinserted key that an older snapshot
        # still sees deleted is a different test
        self.used = set(self.qual) | {SPACING * i + HALF for i in range(n)}
        self.gen = 0
        self.states = {0: (n, tuple(self.qual[:page]))}
        self.direction = 1
        self.commits = 0
        self.failures = 0
        self.stop_event = threading.Event()
        self.error: BaseException | None = None

    def _next_op(self) -> tuple[str, int]:
        size = len(self.qual)
        if size >= self.n + self.delta:
            self.direction = -1
        elif size <= self.n - self.delta:
            self.direction = 1
        if self.direction > 0:
            while True:
                ident = self.rng.randrange(0, SPACING * self.n)
                if ident not in self.used:
                    return "insert", ident
        return "delete", self.rng.choice(self.qual)

    def run(self):
        try:
            while not self.stop_event.is_set():
                op, ident = self._next_op()
                g = self.gen + 1
                after = list(self.qual)
                if op == "insert":
                    bisect.insort(after, ident)
                else:
                    after.remove(ident)
                # recorded before the commit can become visible
                self.states[g] = (len(after), tuple(after[:self.page]))
                while True:
                    try:
                        self.b.write(op, ident, g)
                        break
                    except Exception:
                        self.failures += 1
                        if self.stop_event.is_set():
                            return
                        time.sleep(0.001)
                self.gen, self.qual = g, after
                self.used.add(ident)
                self.commits += 1
                self.stop_event.wait(self.interval)
        except BaseException as e:  # surfaced by the reader
            self.error = e


# ------------------------------------------------------------------ block

def run_block(engine: str, n: int, variant: str, args, seed: int) -> dict:
    backend = make_backend(engine, n, args.workdir)
    fn = VARIANTS[variant]
    psql = page_sql(args.page)
    writer = Writer(backend, n, args.page, args.delta,
                    args.writer_interval_ms / 1000.0, seed)
    delay = args.delay_ms / 1000.0
    records = []
    snapshot_ids = set()
    token_example = None
    t_block = time.monotonic()
    writer.start()
    time.sleep(0.2)  # writer at steady rate before the first trial
    try:
        for t in range(args.trials):
            if writer.error is not None:
                raise RuntimeError(f"writer died: {writer.error!r}")
            c0 = writer.commits
            win = []

            def window():
                win.append(writer.commits)
                time.sleep(delay)
                win.append(writer.commits)
            t0 = time.monotonic()
            rows, count, token = fn(backend.reader, SEMANTIC_SQL, psql,
                                    engine=backend.api_engine,
                                    between=window)
            lat = (time.monotonic() - t0) * 1000.0
            c1 = writer.commits
            ids = [int(r[0]) for r in rows]
            gen_p = int(rows[0][1])
            n_stmt = int(rows[0][2])
            size_p, first_p = writer.states[gen_p]
            prefix_ok = (ids == list(first_p[:len(ids)])
                         and len(ids) == min(args.page, size_p))
            ecqr = build_sql_ecqr(rows=[[i] for i in ids], sql=SEMANTIC_SQL,
                                  store_id=f"stress-{engine}-{n}",
                                  engine=backend.api_engine,
                                  total_count=count, limited=True)
            cert_wrong = (ecqr.scope.exact_cardinality != size_p
                          or (ecqr.scope.delivery_complete
                              and len(ids) != size_p))
            if token_example is None:
                token_example = token
            snapshot_ids.add(json.dumps(
                {k: v for k, v in token.items() if k != "marker"},
                sort_keys=True))
            records.append([t, len(ids), count, size_p, gen_p, c1 - c0,
                            win[1] - win[0], count == size_p, prefix_ok,
                            n_stmt == size_p, cert_wrong, round(lat, 2)])
    finally:
        writer.stop_event.set()
        writer.join(timeout=60)
        wall = time.monotonic() - t_block
        backend.close()
    k_incons = sum(1 for r in records if not r[7])
    k_cert = sum(1 for r in records if r[10])
    hit = sum(1 for r in records if r[5] > 0)
    hit_w = sum(1 for r in records if r[6] > 0)
    lats = sorted(r[11] for r in records)
    return {
        "engine": engine, "N": n, "variant": variant, "seed": seed,
        "trials": len(records),
        "inconsistent_pairs": k_incons,
        "inconsistent_fraction": k_incons / len(records),
        "inconsistent_fraction_wilson95_upper":
            wilson_upper(k_incons, len(records)),
        "certificates_wrong": k_cert,
        "certificates_wrong_fraction": k_cert / len(records),
        "page_prefix_failures": sum(1 for r in records if not r[8]),
        "statement_count_mismatches": sum(1 for r in records if not r[9]),
        "trials_with_writer_commit": hit,
        "trials_with_writer_commit_fraction": hit / len(records),
        "writer_commits_per_trial_mean":
            sum(r[5] for r in records) / len(records),
        "trials_with_writer_commit_in_window": hit_w,
        "trials_with_writer_commit_in_window_fraction": hit_w / len(records),
        "writer_commits_per_window_mean":
            sum(r[6] for r in records) / len(records),
        "writer_commits_total": writer.commits,
        "writer_retries": writer.failures,
        "count_minus_size_p_hist": _hist(r[2] - r[3] for r in records),
        "latency_ms_p50": lats[len(lats) // 2],
        "latency_ms_p99": lats[min(len(lats) - 1, int(0.99 * len(lats)))],
        "block_wall_s": round(wall, 2),
        "engine_version": getattr(backend, "version", sqlite3.sqlite_version),
        "token_example": token_example,
        "distinct_snapshot_identities": len(snapshot_ids),
        "records": records,
    }


def _hist(vals) -> dict[str, int]:
    h: dict[str, int] = {}
    for v in vals:
        h[str(v)] = h.get(str(v), 0) + 1
    return dict(sorted(h.items(), key=lambda kv: int(kv[0])))


def _git(*a: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), *a],
                                       text=True).strip()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines",
                    default="sqlite-wal,sqlite-rollback,duckdb,postgres")
    ap.add_argument("--ns", default="343,34300")
    ap.add_argument("--page", type=int, default=100)
    ap.add_argument("--trials", type=int, default=1000)
    ap.add_argument("--delay-ms", type=float, default=50.0)
    ap.add_argument("--writer-interval-ms", type=float, default=5.0)
    ap.add_argument("--delta", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    ap.add_argument("--not-run", action="append", default=[],
                    metavar="ENGINE=REASON",
                    help="record an engine that was not run, with why")
    args = ap.parse_args()
    if args.receipt.exists():
        raise SystemExit(f"receipt {args.receipt} exists; receipts are "
                         f"records and are never overwritten")
    args.workdir.mkdir(parents=True, exist_ok=True)

    engines = [e for e in args.engines.split(",") if e]
    ns = [int(x) for x in args.ns.split(",") if x]
    blocks = []
    idx = 0
    for engine in engines:
        for n in ns:
            for variant in ("unsafe", "consistent"):
                seed = args.seed + idx
                idx += 1
                t0 = time.monotonic()
                b = run_block(engine, n, variant, args, seed)
                blocks.append(b)
                print(f"{engine:16s} N={n:<6d} {variant:10s} "
                      f"inconsistent {b['inconsistent_pairs']}/{b['trials']}"
                      f" certs_wrong {b['certificates_wrong']}"
                      f" window_hit "
                      f"{b['trials_with_writer_commit_in_window_fraction']:.3f}"
                      f" ({time.monotonic() - t0:.1f}s)", flush=True)
    versions = {b["engine"]: b["engine_version"] for b in blocks}

    table = [{k: b[k] for k in (
        "engine", "N", "variant", "trials", "inconsistent_pairs",
        "inconsistent_fraction", "inconsistent_fraction_wilson95_upper",
        "certificates_wrong", "trials_with_writer_commit_in_window_fraction",
        "writer_commits_per_window_mean",
        "trials_with_writer_commit_fraction", "page_prefix_failures",
        "statement_count_mismatches")} for b in blocks]
    consistent_ok = all(b["inconsistent_pairs"] == 0
                        and b["certificates_wrong"] == 0
                        for b in blocks if b["variant"] == "consistent")
    oracle_ok = all(b["page_prefix_failures"] == 0
                    and b["statement_count_mismatches"] == 0
                    for b in blocks)
    receipt = {
        "experiment": "SQL page/count snapshot consistency under a "
                      "concurrent writer",
        "freeze": str(FREEZE.relative_to(ROOT)),
        "freeze_sha256": hashlib.sha256(FREEZE.read_bytes()).hexdigest(),
        "commit": _git("rev-parse", "HEAD"),
        "worktree_dirty": bool(_git("status", "--porcelain", "--",
                                    "tgms", "scripts")),
        "host": socket.gethostname(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "date_utc": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(),
        "versions": versions,
        "protocol": {
            "engines": engines, "N": ns, "page": args.page,
            "trials_per_block": args.trials, "delay_ms": args.delay_ms,
            "writer_interval_ms": args.writer_interval_ms,
            "writer_size_band": [f"N-{args.delta}", f"N+{args.delta}"],
            "base_seed": args.seed,
            "semantic_sql": SEMANTIC_SQL,
            "page_sql": page_sql(args.page),
            "variants": {
                "unsafe": "page and count as two autocommit statements",
                "consistent": "tgms.evidence.sql_snapshot."
                              "consistent_page_and_count"},
            "sqlite_rollback_journal_mode": "delete",
        },
        "not_run": dict(x.split("=", 1) for x in args.not_run),
        "pass_criterion": "every consistent block has 0 inconsistent "
                          "pairs and 0 wrong certificates",
        "consistent_variant_passes": consistent_ok,
        "oracle_checks_pass": oracle_ok,
        "record_fields": RECORD_FIELDS,
        "table": table,
        "blocks": blocks,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps({"consistent_variant_passes": consistent_ok,
                      "oracle_checks_pass": oracle_ok}, indent=1))
    return 0 if (consistent_ok and oracle_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
