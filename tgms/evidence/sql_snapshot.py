"""One read snapshot for a page and its certifying count.

`build_sql_ecqr` certifies `@ExactCardinality` from a caller-supplied
`total_count`, which must be an unlimited count over the same predicate
AND the same basis as the delivered page. Two autocommit statements do
not share a basis: a writer that commits between them makes the count
describe a different database state than the page. This module runs the
page statement and `SELECT COUNT(*) FROM (<semantic_sql>)` inside ONE
read transaction whose snapshot semantics the engine documents, and
returns a token that names that transaction, so the descriptor can bind
both results to one basis.

Snapshot semantics relied on, per engine
----------------------------------------

SQLite (`engine="sqlite"`, Python `sqlite3`). `BEGIN` (DEFERRED) opens
no read transaction by itself; the first statement that reads the
database does (https://www.sqlite.org/lang_transaction.html). The
module's first statement inside the transaction is `PRAGMA
data_version`, which reads the database header and so starts the read
transaction; the page and the count follow it.
- WAL mode: a read transaction sees the database as of the moment it
  started; commits by other connections after that point are invisible
  to it until it ends ("Isolation In SQLite",
  https://www.sqlite.org/isolation.html; https://www.sqlite.org/wal.html,
  "Concurrency"). Writers proceed concurrently.
- Rollback-journal modes (DELETE, TRUNCATE, PERSIST): the read
  transaction holds a SHARED lock until it ends, and no writer can
  commit while any SHARED lock is held
  (https://www.sqlite.org/lockingv3.html; isolation.html). The reads
  see one state because the state cannot change; a concurrent writer
  waits (busy timeout) or fails with SQLITE_BUSY.
The token carries the journal mode and `PRAGMA data_version`, which
changes when another connection commits
(https://www.sqlite.org/pragma.html#pragma_data_version). It is read
first and again after the count; equal values are asserted.

DuckDB (`engine="duckdb"`). DuckDB runs every transaction under
multi-version concurrency control with snapshot isolation: a
transaction reads the snapshot taken when it starts, and concurrent
commits are invisible to it
(https://duckdb.org/docs/stable/connect/concurrency;
https://duckdb.org/docs/stable/sql/statements/transactions). The
transaction is opened with `BEGIN TRANSACTION`; its first statement,
`SELECT current_transaction_id(), txid_current()`
(https://duckdb.org/docs/stable/sql/functions/utility), records its
identity and fixes the snapshot before the page and the count run. A
DuckDB `cursor()` is a separate connection with its own transaction, so
the caller passes the connection that should run the statements.

PostgreSQL (`engine="postgres"`, psycopg 3 or psycopg2).
`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`. Under REPEATABLE
READ "a query ... sees a snapshot as of the start of the first
non-transaction-control statement in the transaction, not as of the
start of the current statement within the transaction. Thus,
successive SELECT commands within a single transaction see the same
data" (https://www.postgresql.org/docs/current/transaction-iso.html,
13.2.2). The first statement is `SELECT pg_current_snapshot()`
(`txid_current_snapshot()` before PostgreSQL 13;
https://www.postgresql.org/docs/current/functions-info.html), whose
value (xmin:xmax:xip_list) is the transaction snapshot itself and is
the token. A read-only REPEATABLE READ transaction cannot fail with a
serialization error.

The guarantee covers the engine, not the caller: `page_sql` must still
be a page of `semantic_sql` (same predicate, same order). What this
module removes is the second half of trust assumption A2, the basis:
the count and the page are now facts about one database state.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Sequence
from typing import Any

ENGINES = ("sqlite", "duckdb", "postgres")
_ALIASES = {"pg": "postgres", "postgresql": "postgres",
            "sqlite3": "sqlite"}

_marker = itertools.count(1)
_marker_lock = threading.Lock()


def _next_marker() -> int:
    with _marker_lock:
        return next(_marker)


def _engine(name: str) -> str:
    e = _ALIASES.get(name.lower(), name.lower())
    if e not in ENGINES:
        raise ValueError(f"unknown engine {name!r}; expected one of "
                         f"{', '.join(ENGINES)}")
    return e


def count_sql(semantic_sql: str) -> str:
    """The unlimited count over the semantic query's own result."""
    body = semantic_sql.strip().rstrip(";").strip()
    return f"SELECT COUNT(*) FROM ({body}) AS _tgms_count"


# ------------------------------------------------------------- execution

def _pg_driver(conn: Any) -> str:
    mod = type(conn).__module__
    if mod.startswith("psycopg2"):
        return "psycopg2"
    if mod.startswith("psycopg"):
        return "psycopg"
    raise TypeError(f"unsupported PostgreSQL connection type "
                    f"{type(conn).__module__}.{type(conn).__name__}")


def _pg_idle(conn: Any) -> bool:
    if _pg_driver(conn) == "psycopg2":
        return conn.get_transaction_status() == 0  # TRANSACTION_STATUS_IDLE
    return int(conn.info.transaction_status) == 0  # pq.TransactionStatus.IDLE


def _run(conn: Any, engine: str, sql: str,
         params: Sequence[Any] | None = None) -> list[tuple]:
    if engine == "postgres":
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return ([tuple(r) for r in cur.fetchall()]
                    if cur.description is not None else [])
        finally:
            cur.close()
    if engine == "duckdb":
        res = conn.execute(sql, params) if params is not None \
            else conn.execute(sql)
        return [tuple(r) for r in res.fetchall()] \
            if res.description is not None else []
    cur = conn.execute(sql, params if params is not None else ())
    return [tuple(r) for r in cur.fetchall()]


def _require_idle(conn: Any, engine: str) -> None:
    """The caller must not hold an open transaction: its snapshot (or
    its absence) would not be the one this module documents."""
    if engine == "sqlite" and conn.in_transaction:
        raise ValueError("sqlite connection already has an open "
                         "transaction (PEP 249 autocommit=False mode, or "
                         "an uncommitted statement); pass a connection "
                         "with no open transaction")
    if engine == "postgres" and not _pg_idle(conn):
        raise ValueError("postgres connection is inside a transaction; "
                         "commit or roll back before calling")


class _PgAutocommit:
    """Explicit BEGIN/COMMIT need driver autocommit on; restore after."""

    def __init__(self, conn: Any, engine: str):
        self.conn, self.on = conn, engine == "postgres"

    def __enter__(self):
        if self.on:
            self.saved = self.conn.autocommit
            self.conn.autocommit = True
        return self

    def __exit__(self, *exc):
        if self.on:
            self.conn.autocommit = self.saved
        return False


def _snapshot_identity(conn: Any, engine: str,
                       pg_legacy: bool = False) -> dict[str, Any]:
    """First statement of the transaction: starts the read snapshot and
    returns what identifies it."""
    if engine == "sqlite":
        (dv,), = _run(conn, engine, "PRAGMA data_version")
        return {"data_version": int(dv)}
    if engine == "duckdb":
        (tid, xid), = _run(conn, engine,
                           "SELECT current_transaction_id(), txid_current()")
        return {"transaction_id": int(tid), "txid": int(xid)}
    fn = "txid_current_snapshot" if pg_legacy else "pg_current_snapshot"
    (snap,), = _run(conn, engine, f"SELECT {fn}()::text")
    return {"snapshot": str(snap)}


def _begin_sql(engine: str) -> str:
    return {"sqlite": "BEGIN",
            "duckdb": "BEGIN TRANSACTION",
            "postgres": "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
            }[engine]


def _isolation(engine: str) -> str:
    return {"sqlite": "sqlite deferred read transaction",
            "duckdb": "duckdb MVCC snapshot isolation",
            "postgres": "postgres repeatable read, read only",
            }[engine]


def _pg_server_version(conn: Any) -> int:
    """Server version as an integer, e.g. 160004 for 16.4."""
    if _pg_driver(conn) == "psycopg2":
        return int(conn.server_version)
    return int(conn.info.server_version)


# ------------------------------------------------------------ public API

def consistent_page_and_count(
        conn: Any, semantic_sql: str, page_sql: str, *, engine: str,
        params: Sequence[Any] | None = None,
        page_params: Sequence[Any] | None = None,
        between: Callable[[], None] | None = None,
) -> tuple[list[tuple], int, dict[str, Any]]:
    """Run `page_sql` and the unlimited count of `semantic_sql` inside
    one read transaction. Returns (page rows, total_count,
    snapshot_token).

    `params` bind `semantic_sql` (inside the count), `page_params` bind
    `page_sql`, both in the driver's own paramstyle. `between` is called
    after the page statement and before the count statement, inside the
    transaction; it exists so stress tests can widen the window. The
    transaction is committed on success and rolled back on any error.
    """
    engine = _engine(engine)
    _require_idle(conn, engine)
    journal = None
    if engine == "sqlite":
        (journal,), = _run(conn, engine, "PRAGMA journal_mode")
    pg_legacy = engine == "postgres" and _pg_server_version(conn) < 130000
    with _PgAutocommit(conn, engine):
        _run(conn, engine, _begin_sql(engine))
        try:
            ident = _snapshot_identity(conn, engine, pg_legacy)
            rows = _run(conn, engine, page_sql, page_params)
            if between is not None:
                between()
            (total,), = _run(conn, engine, count_sql(semantic_sql), params)
            if engine == "sqlite":
                (dv_end,), = _run(conn, engine, "PRAGMA data_version")
                if int(dv_end) != ident["data_version"]:
                    raise RuntimeError(
                        "sqlite data_version changed inside one read "
                        f"transaction ({ident['data_version']} -> {dv_end})")
            _run(conn, engine, "COMMIT")
        except BaseException:
            try:
                _run(conn, engine, "ROLLBACK")
            except Exception:
                pass
            raise
    token: dict[str, Any] = {"engine": engine, "consistent": True,
                             "isolation": _isolation(engine), **ident,
                             "marker": _next_marker()}
    if journal is not None:
        token["journal_mode"] = str(journal).lower()
    return rows, int(total), token


def unsafe_page_and_count(
        conn: Any, semantic_sql: str, page_sql: str, *, engine: str,
        params: Sequence[Any] | None = None,
        page_params: Sequence[Any] | None = None,
        between: Callable[[], None] | None = None,
) -> tuple[list[tuple], int, dict[str, Any]]:
    """The unsafe pattern, for stress tests only: the page and the count
    as two autocommit statements, each with its own snapshot. Same
    signature and return shape as `consistent_page_and_count`; the
    token says `consistent: False`."""
    engine = _engine(engine)
    _require_idle(conn, engine)
    with _PgAutocommit(conn, engine):
        rows = _run(conn, engine, page_sql, page_params)
        if between is not None:
            between()
        (total,), = _run(conn, engine, count_sql(semantic_sql), params)
    return rows, int(total), {"engine": engine, "consistent": False,
                              "isolation": "autocommit, two statements",
                              "marker": _next_marker()}
