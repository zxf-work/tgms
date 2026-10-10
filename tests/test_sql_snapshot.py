"""[tests] One read snapshot for a SQL page and its certifying count.

The page statement carries `COUNT(*) OVER ()`, the size of the result
it was cut from, evaluated in the page statement's own snapshot. A
(page, count) pair is consistent iff the separately executed count
equals that size. A writer on a second connection commits between the
two statements, deterministically (the `between` hook) or continuously
(a thread).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from tgms.evidence.adapter_sql import build_sql_ecqr
from tgms.evidence.sql_snapshot import (
    consistent_page_and_count,
    count_sql,
    unsafe_page_and_count,
)

duckdb = pytest.importorskip("duckdb")

N = 343
PAGE = 100
SEMANTIC = "SELECT id FROM items WHERE q = 1 ORDER BY id"
PAGE_SQL = (f"SELECT id, COUNT(*) OVER () AS n FROM items WHERE q = 1 "
            f"ORDER BY id LIMIT {PAGE}")


class DB:
    """A reader connection and a writer on a second connection."""

    def __init__(self, kind: str, tmp_path, writer_timeout: float = 30.0):
        self.kind = kind
        self.next_id = 10 * N
        if kind.startswith("sqlite"):
            self.engine = "sqlite"
            path = tmp_path / f"{kind}.sqlite"
            mode = "wal" if kind == "sqlite-wal" else "delete"
            setup = sqlite3.connect(path, isolation_level=None)
            assert setup.execute(
                f"PRAGMA journal_mode={mode}").fetchone()[0] == mode
            setup.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, "
                          "q INTEGER NOT NULL)")
            setup.executemany("INSERT INTO items VALUES (?, ?)",
                              [(10 * i, 1) for i in range(N)]
                              + [(10 * i + 5, 0) for i in range(N)])
            setup.close()
            self.reader = sqlite3.connect(path, isolation_level=None,
                                          timeout=30,
                                          check_same_thread=False)
            self.writer = sqlite3.connect(path, isolation_level=None,
                                          timeout=writer_timeout,
                                          check_same_thread=False)
        else:
            self.engine = "duckdb"
            self.db = duckdb.connect(":memory:")
            self.db.execute("CREATE TABLE items (id BIGINT PRIMARY KEY, "
                            "q INTEGER NOT NULL)")
            self.db.execute(f"INSERT INTO items SELECT range * 10, 1 "
                            f"FROM range({N})")
            self.db.execute(f"INSERT INTO items SELECT range * 10 + 5, 0 "
                            f"FROM range({N})")
            self.reader = self.db.cursor()
            self.writer = self.db.cursor()
        self.lock = threading.Lock()

    def insert(self) -> None:
        """Commit one new qualifying row (ids are never reused)."""
        with self.lock:
            self.next_id += 1
            ident = self.next_id
        self.writer.execute(f"INSERT INTO items VALUES ({ident}, 1)")

    def delete_first(self) -> None:
        self.writer.execute("DELETE FROM items WHERE id = (SELECT MIN(id) "
                            "FROM items WHERE q = 1)")

    def fresh_count(self) -> int:
        return self.reader.execute(count_sql(SEMANTIC)).fetchone()[0]

    def close(self) -> None:
        self.reader.close()
        self.writer.close()


ENGINES = ["sqlite-wal", "sqlite-rollback", "duckdb"]


@pytest.fixture(params=ENGINES)
def db(request, tmp_path):
    d = DB(request.param, tmp_path)
    yield d
    d.close()


def own_size(rows) -> int:
    return rows[0][1]


# ------------------------------------------------- deterministic interleave

def test_unsafe_pair_breaks_under_one_interleaved_commit(db):
    rows, total, token = unsafe_page_and_count(
        db.reader, SEMANTIC, PAGE_SQL, engine=db.engine, between=db.insert)
    assert own_size(rows) == N and len(rows) == PAGE
    assert total == N + 1          # the count describes another state
    assert token["consistent"] is False
    ecqr = build_sql_ecqr(rows=[list(r[:1]) for r in rows], sql=SEMANTIC,
                          store_id="t", total_count=total, limited=True)
    assert ecqr.scope.exact_cardinality != own_size(rows)


@pytest.mark.parametrize("kind", ["sqlite-wal", "duckdb"])
def test_consistent_pair_survives_one_interleaved_commit(kind, tmp_path):
    d = DB(kind, tmp_path)
    try:
        rows, total, token = consistent_page_and_count(
            d.reader, SEMANTIC, PAGE_SQL, engine=d.engine, between=d.insert)
        assert total == own_size(rows) == N
        assert token["consistent"] is True
        assert d.fresh_count() == N + 1   # the writer did commit
    finally:
        d.close()


def test_sqlite_rollback_mode_holds_the_writer_off(tmp_path):
    """Rollback journal: the read transaction's SHARED lock keeps any
    writer from committing until the transaction ends."""
    d = DB("sqlite-rollback", tmp_path, writer_timeout=0.05)
    errors = []

    def try_write():
        try:
            d.insert()
        except sqlite3.OperationalError as e:
            errors.append(str(e))
    try:
        rows, total, token = consistent_page_and_count(
            d.reader, SEMANTIC, PAGE_SQL, engine="sqlite", between=try_write)
        assert errors and "locked" in errors[0]
        assert total == own_size(rows) == N
        assert token["journal_mode"] == "delete"
        d.insert()                       # free once the read txn ended
        assert d.fresh_count() == N + 1
    finally:
        d.close()


# ---------------------------------------------------- concurrent writer

class WriterThread(threading.Thread):
    def __init__(self, d: DB):
        super().__init__(daemon=True)
        self.d, self.commits, self.stop = d, 0, threading.Event()
        self.error = None

    def run(self):
        step = 0
        try:
            while not self.stop.is_set():
                # net drift of +1 per two commits, so sizes keep moving
                (self.d.insert if step % 3 else self.d.delete_first)()
                step += 1
                self.commits += 1
                time.sleep(0.001)
        except BaseException as e:
            self.error = e


def _run_trials(d: DB, fn, trials: int, stop_on_first_bad: bool):
    w = WriterThread(d)
    w.start()
    bad = hits = 0
    try:
        for _ in range(trials):
            c0 = w.commits
            rows, total, _tok = fn(d.reader, SEMANTIC, PAGE_SQL,
                                   engine=d.engine,
                                   between=lambda: time.sleep(0.005))
            hits += w.commits > c0
            if total != own_size(rows):
                bad += 1
                if stop_on_first_bad:
                    break
    finally:
        w.stop.set()
        w.join(timeout=30)
    assert w.error is None, w.error
    return bad, hits, w.commits


def test_consistent_variant_never_inconsistent_under_writer_thread(db):
    bad, hits, commits = _run_trials(db, consistent_page_and_count, 60,
                                     stop_on_first_bad=False)
    assert bad == 0
    assert commits > 0
    if db.kind != "sqlite-rollback":   # rollback mode blocks the writer
        assert hits > 0                 # the writer did land in trials


def test_unsafe_variant_is_caught_by_the_same_check(db):
    bad, _hits, _c = _run_trials(db, unsafe_page_and_count, 300,
                                 stop_on_first_bad=True)
    assert bad >= 1


# ----------------------------------------------------------- the token

def test_tokens_name_the_transaction(db):
    _r, _t, a = consistent_page_and_count(db.reader, SEMANTIC, PAGE_SQL,
                                          engine=db.engine)
    db.insert()
    _r, _t, b = consistent_page_and_count(db.reader, SEMANTIC, PAGE_SQL,
                                          engine=db.engine)
    assert a["engine"] == b["engine"] == db.engine
    assert a["marker"] != b["marker"]
    if db.engine == "sqlite":
        assert a["journal_mode"] == ("wal" if db.kind == "sqlite-wal"
                                     else "delete")
        assert b["data_version"] != a["data_version"]
    else:
        assert b["transaction_id"] != a["transaction_id"]


# ------------------------------------------------------- guard rails

def test_refuses_a_connection_with_an_open_transaction(tmp_path):
    d = DB("sqlite-wal", tmp_path)
    try:
        d.reader.execute("BEGIN")
        with pytest.raises(ValueError, match="open transaction"):
            consistent_page_and_count(d.reader, SEMANTIC, PAGE_SQL,
                                      engine="sqlite")
        d.reader.execute("COMMIT")
    finally:
        d.close()
    d = DB("duckdb", tmp_path)
    try:
        d.reader.execute("BEGIN TRANSACTION")
        with pytest.raises(duckdb.TransactionException):
            consistent_page_and_count(d.reader, SEMANTIC, PAGE_SQL,
                                      engine="duckdb")
    finally:
        d.close()


def test_rolls_back_when_the_window_raises(db):
    def boom():
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        consistent_page_and_count(db.reader, SEMANTIC, PAGE_SQL,
                                  engine=db.engine, between=boom)
    # the connection is usable for a new transaction
    rows, total, _ = consistent_page_and_count(db.reader, SEMANTIC,
                                               PAGE_SQL, engine=db.engine)
    assert total == own_size(rows) == N


def test_unknown_engine_and_count_sql():
    with pytest.raises(ValueError, match="unknown engine"):
        consistent_page_and_count(None, SEMANTIC, PAGE_SQL, engine="oracle")
    assert count_sql("SELECT 1;  ") == \
        "SELECT COUNT(*) FROM (SELECT 1) AS _tgms_count"


# ------------------------------------------------------------ postgres

@pytest.mark.skipif(os.environ.get("TGMS_PG_TESTS") != "1",
                    reason="set TGMS_PG_TESTS=1 to start an embedded "
                           "postgres (pgserver)")
def test_postgres_repeatable_read(tmp_path):
    pgserver = pytest.importorskip("pgserver")
    try:
        import psycopg as drv
    except ImportError:
        drv = pytest.importorskip("psycopg2")
    srv = pgserver.get_server(tmp_path / "pg", cleanup_mode="stop")
    setup = drv.connect(srv.get_uri())
    setup.autocommit = True
    cur = setup.cursor()
    cur.execute("CREATE TABLE items (id BIGINT PRIMARY KEY, "
                "q INTEGER NOT NULL)")
    cur.execute(f"INSERT INTO items SELECT g * 10, 1 FROM "
                f"generate_series(0, {N - 1}) g")
    reader = drv.connect(srv.get_uri())
    nxt = iter(range(10 * N, 20 * N))

    def insert():
        cur.execute(f"INSERT INTO items VALUES ({next(nxt)}, 1)")
    try:
        rows, total, tok = unsafe_page_and_count(
            reader, SEMANTIC, PAGE_SQL, engine="postgres", between=insert)
        assert (own_size(rows), total) == (N, N + 1)
        rows, total, tok = consistent_page_and_count(
            reader, SEMANTIC, PAGE_SQL, engine="pg", between=insert)
        assert total == own_size(rows) == N + 1
        assert tok["isolation"].startswith("postgres repeatable read")
        assert ":" in tok["snapshot"]
        cur.execute(count_sql(SEMANTIC))
        assert cur.fetchone()[0] == N + 2
    finally:
        reader.close()
        setup.close()
