"""The SQL evidence adapter — direct SQL executions become ECQRs.

The portability half of the Gate A layering: a SQL backend produces the
same capabilities through its own mechanisms. An unlimited `COUNT(*)`
over the same predicate certifies `@ExactCardinality` the way a TGMS
operator's pre-pagination `rows_total` does; a query executed without
`LIMIT` — or whose delivered page equals the certified count — is
delivery-complete. The generic verifier cannot tell which backend
produced a descriptor; that is the point (the M3 exit gate), and trust
assumption A2 (the adapter truthfully constructs what it emits) is
carried by the caller supplying honest inputs: `total_count` must come
from an unlimited count over the *same* predicate and basis, never from
a page.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from tgms.core.model import OPEN_END, canonical_json, sha256_hex
from tgms.evidence.ecqr import ECQR, Basis, Ranking, Scope, basis_identity


def build_sql_ecqr(*, rows: list[Any], sql: str,
                   params: list[Any] | None = None,
                   store_id: str, as_of_tt: int = OPEN_END,
                   engine: str = "duckdb", engine_version: str = "",
                   total_count: int | None = None,
                   limited: bool = False,
                   input_ecqrs: list[ECQR] | None = None) -> ECQR:
    """Descriptor for one completed SQL query execution."""
    inputs_complete = all(e.scope.delivery_complete
                          for e in (input_ecqrs or []))
    delivered = len(rows)
    delivery_complete = inputs_complete and (
        not limited or (total_count is not None and delivered == total_count))
    cardinality = total_count if (isinstance(total_count, int)
                                  and inputs_complete) else None
    return ECQR(
        result_id=sha256_hex(canonical_json(
            {"rows": rows, "sql": sql, "params": params or []})),
        basis=Basis(store=store_id, as_of_tt=as_of_tt,
                    pinned=as_of_tt != OPEN_END),
        scope=Scope(domain={"sql": " ".join(sql.split()),
                            "params": params or []},
                    execution_complete=True,  # a completed statement
                    delivery_complete=delivery_complete,
                    rows_returned=delivered,
                    exact_cardinality=cardinality),
        exactness="exact",
        provenance={"engine": engine,
                    "inputs": [e.result_id for e in (input_ecqrs or [])]},
        semantics={"engine": engine, "version": engine_version,
                   "canonicalization": "tgms-canonical-json-1"},
    )


# ------------------------------------------------------------------ top-k
#
# A ranked page: the statement's outer `LIMIT k` (zero OFFSET) is read as
# pagination of the candidate domain Q' (the statement without it), the
# reading the claim TopK needs, and distinct from the default reading
# above, where an agent LIMIT is part of the domain. The adapter records
# the ordering and establishes `order_total` statically from the schema:
# true iff the ORDER BY key list contains every column of a unique,
# non-null key of the single base table in FROM. Anything it cannot
# establish stays false (understatement is sound).

#: SQLite rowid aliases; usable as a unique key on rowid tables only
ROWID_NAMES = ("rowid", "_rowid_", "oid")


class TopKBlocked(Exception):
    """A ranking over an input that is not delivery-certified complete:
    top-k is non-row-local, so the step is blocked (Lemma 3.10 rule (a))
    and no descriptor is emitted."""


@dataclass
class TopKShape:
    """What the adapter establishes about one statement's outer ranking.

    `is_topk` holds when the outer statement carries an integer-literal
    LIMIT k >= 1 with zero OFFSET; `reason` then names why `order_total`
    holds or not, else why the statement is not a top-k page.
    """
    is_topk: bool
    reason: str
    limit: int | None = None
    order: list[list[str]] = field(default_factory=list)
    order_total: bool = False
    candidate_sql: str | None = None
    unique_key: list[str] | None = None


def sqlite_unique_keys(con: sqlite3.Connection) -> dict[str, list[frozenset[str]]]:
    """Unique, non-null column sets per table (names lowercased).

    Counted: the rowid and its aliases on rowid tables, and the PRIMARY
    KEY when it cannot hold NULL (an INTEGER PRIMARY KEY rowid alias, a
    WITHOUT ROWID table, or every key column declared NOT NULL). SQLite
    admits NULLs in other primary keys, and NULLs tie, so such a key is
    not counted. UNIQUE constraints are not consulted.
    """
    out: dict[str, list[frozenset[str]]] = {}
    tables = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    for name, ddl in tables:
        without_rowid = bool(ddl) and "WITHOUT ROWID" in " ".join(
            ddl.upper().split())
        info = con.execute(
            f"PRAGMA table_info({_quote_ident(name)})").fetchall()
        # (cid, name, type, notnull, dflt, pk)
        pk = sorted((r for r in info if r[5]), key=lambda r: r[5])
        keys: list[frozenset[str]] = []
        if not without_rowid:
            cols = {r[1].lower() for r in info}
            for alias in ROWID_NAMES:
                if alias not in cols:   # a real column shadows the alias
                    keys.append(frozenset([alias]))
        if pk:
            rowid_alias = (len(pk) == 1 and not without_rowid
                           and str(pk[0][2]).strip().upper() == "INTEGER")
            if rowid_alias or without_rowid or all(r[3] for r in pk):
                keys.append(frozenset(r[1].lower() for r in pk))
        out[name.lower()] = keys
    return out


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _int_literal(node: Any) -> int | None:
    from sqlglot import expressions as exp
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.this)
        except ValueError:
            return None
    return None


def parse_topk(sql: str, unique_keys: dict[str, list[frozenset[str]]],
               dialect: str = "sqlite") -> TopKShape:
    """Parse the outer ORDER BY / LIMIT of `sql` and decide `order_total`.

    Q' (`candidate_sql`) is the statement with its outer LIMIT removed,
    ORDER BY kept. The ORDER BY list is recorded as written (canonical
    key text, direction). Inner LIMITs are part of Q' and untouched.
    sqlglot is imported here, not at module level: it is an evaluation
    dependency, not a runtime one.
    """
    import sqlglot
    from sqlglot import expressions as exp
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - any parse failure is "not established"
        return TopKShape(False, "parse_error")
    lim = tree.args.get("limit")
    if lim is None:
        return TopKShape(False, "no_outer_limit")
    off = tree.args.get("offset") or lim.args.get("offset")
    if off is not None:
        off_n = _int_literal(off.args.get("expression") if isinstance(
            off, exp.Offset) else off)
        if off_n != 0:
            return TopKShape(False, "outer_offset")
    k = _int_literal(lim.args.get("expression"))
    if k is None or k < 1:
        return TopKShape(False, "non_literal_or_zero_limit")

    cand = tree.copy()
    cand.set("limit", None)
    cand.set("offset", None)
    order_node = tree.args.get("order")
    ordered = list(order_node.expressions) if order_node is not None else []
    order = [[o.this.sql(dialect=dialect),
              "desc" if o.args.get("desc") else "asc"] for o in ordered]
    shape = TopKShape(True, "", limit=k, order=order,
                      candidate_sql=cand.sql(dialect=dialect))
    reason, key = _order_total(tree, ordered, unique_keys)
    shape.reason, shape.unique_key = reason, key
    shape.order_total = key is not None
    return shape


def _order_total(tree: Any, ordered: list[Any],
                 unique_keys: dict[str, list[frozenset[str]]]
                 ) -> tuple[str, list[str] | None]:
    from sqlglot import expressions as exp
    if not ordered:
        return "no_order_by", None
    if not isinstance(tree, exp.Select):
        return "compound_query", None
    src = tree.args.get("from_") or tree.args.get("from")
    if src is None or tree.args.get("joins"):
        return "not_single_table", None
    table = src.this
    if not isinstance(table, exp.Table) or table.args.get("db"):
        return "derived_or_qualified_source", None
    tname = table.name.lower()
    with_ = tree.args.get("with_") or tree.args.get("with")
    if with_ is not None and any(
            c.alias_or_name.lower() == tname for c in with_.expressions):
        return "derived_or_qualified_source", None
    if tname not in unique_keys:
        return "unknown_table", None
    if tree.args.get("group"):
        return "grouped", None
    names = {tname, (table.alias or tname).lower()}
    aliases = {}
    for e in tree.expressions:
        if isinstance(e, exp.Alias):
            aliases[e.alias.lower()] = e.this
    projected = set()
    for e in tree.expressions:
        c = e.this if isinstance(e, exp.Alias) else e
        if isinstance(c, exp.Column) and (
                not c.table or c.table.lower() in names):
            projected.add(c.name.lower())
    ordered_cols = set()
    for o in ordered:
        node = o.this
        pos = _int_literal(node)
        if pos is not None and 1 <= pos <= len(tree.expressions):
            node = tree.expressions[pos - 1]
            node = node.this if isinstance(node, exp.Alias) else node
        elif (isinstance(node, exp.Column) and not node.table
              and node.name.lower() in aliases):
            node = aliases[node.name.lower()]   # SQLite: alias first
        if isinstance(node, exp.Column) and (
                not node.table or node.table.lower() in names):
            ordered_cols.add(node.name.lower())
    for key in unique_keys[tname]:
        if key <= ordered_cols:
            if tree.args.get("distinct") and not key <= projected:
                continue
            return "unique_key_in_order", sorted(key)
    if tree.args.get("distinct") and any(
            k <= ordered_cols for k in unique_keys[tname]):
        return "distinct_key_not_projected", None
    return "no_unique_key_in_order", None


def build_sql_topk_ecqr(*, rows: list[Any], sql: str, shape: TopKShape,
                        params: list[Any] | None = None,
                        store_id: str, as_of_tt: int = OPEN_END,
                        engine: str = "sqlite", engine_version: str = "",
                        candidate_count: int | None = None,
                        input_ecqrs: list[ECQR] | None = None,
                        execution_context: str | None = None,
                        boundary_strict: bool = False,
                        sequence_strict: bool = False,
                        boundary_basis: dict[str, Any] | None = None,
                        provenance: dict[str, Any] | None = None) -> ECQR:
    """Ranked-page descriptor for one completed `ORDER BY ... LIMIT k`
    statement. The domain is Q' (`shape.candidate_sql`): the page is the
    first min(k, |R*(Q')|) rows of Q'. Delivery over Q' is certified
    complete when the completed statement returned fewer than k rows
    (the candidates are exhausted) or when `candidate_count`, an
    unlimited count of Q' on the same basis, equals the page length;
    that count is also Q''s cardinality certificate.

    `boundary_strict` records rank-boundary strictness at rank k and
    `sequence_strict` pairwise strictness of the first k+1 rows; their
    basis defaults to this page's basis (the check ran in the page's own
    read transaction, as `ranked_page_in_snapshot` does) unless
    `boundary_basis` names another. `execution_context` names the read
    transaction of an unpinned basis.

    Raises TopKBlocked when an input is not delivery-certified complete.
    """
    if not shape.is_topk or shape.candidate_sql is None or shape.limit is None:
        raise ValueError(f"not a top-k statement ({shape.reason})")
    if not all(e.scope.delivery_complete for e in (input_ecqrs or [])):
        raise TopKBlocked("ranking over a delivery-uncertified input")
    delivered = len(rows)
    if delivered > shape.limit:
        raise ValueError("page longer than its LIMIT")
    exhausted = delivered < shape.limit
    delivery_complete = exhausted or (
        candidate_count is not None and candidate_count == delivered)
    cardinality = (candidate_count if isinstance(candidate_count, int)
                   else delivered if exhausted else None)
    q_prime = {"sql": " ".join(shape.candidate_sql.split()),
               "params": params or []}
    basis = Basis(store=store_id, as_of_tt=as_of_tt,
                  pinned=as_of_tt != OPEN_END,
                  execution_context=(None if as_of_tt != OPEN_END
                                     else execution_context))
    boundary_strict = bool(boundary_strict or sequence_strict)
    if boundary_strict and boundary_basis is None:
        boundary_basis = basis_identity(basis)
    return ECQR(
        result_id=sha256_hex(canonical_json(
            {"rows": rows, "sql": sql, "params": params or []})),
        basis=basis,
        scope=Scope(domain=dict(q_prime),
                    execution_complete=True,  # a completed statement
                    delivery_complete=delivery_complete,
                    rows_returned=delivered,
                    exact_cardinality=cardinality),
        exactness="exact",
        provenance={"engine": engine, "statement": " ".join(sql.split()),
                    "inputs": [e.result_id for e in (input_ecqrs or [])],
                    **(provenance or {})},
        semantics={"engine": engine, "version": engine_version,
                   "canonicalization": "tgms-canonical-json-1"},
        ranking=Ranking(candidate_domain=dict(q_prime), limit=shape.limit,
                        order=[list(p) for p in shape.order],
                        order_total=shape.order_total,
                        boundary_strict=boundary_strict,
                        boundary_basis=(boundary_basis if boundary_strict
                                        else None),
                        sequence_strict=bool(sequence_strict)),
    )


# ------------------------------------------------- rank-boundary strictness

def _resolved_order_keys(tree: Any, dialect: str) -> list[Any] | None:
    """The ORDER BY expressions of `tree`, with result-column aliases and
    ordinals replaced by the expressions they name (SQLite resolves an
    ORDER BY identifier to a result alias first)."""
    from sqlglot import expressions as exp
    order = tree.args.get("order")
    if order is None:
        return None
    sel = list(tree.expressions)
    aliases = {e.alias.lower(): e.this for e in sel
               if isinstance(e, exp.Alias)}
    out = []
    for o in order.expressions:
        node = o.this
        pos = _int_literal(node)
        if pos is not None and 1 <= pos <= len(sel):
            node = sel[pos - 1]
            node = node.this if isinstance(node, exp.Alias) else node
        elif (isinstance(node, exp.Column) and not node.table
              and node.name.lower() in aliases):
            node = aliases[node.name.lower()]
        out.append(exp.Ordered(this=node.copy(), desc=bool(o.args.get("desc")),
                               nulls_first=o.args.get("nulls_first")))
    return out


def boundary_probe_sql(candidate_sql: str, k: int,
                       dialect: str = "sqlite") -> str | None:
    """One statement over Q' deciding rank-boundary strictness at rank k.

    Q' keeps its FROM/WHERE/GROUP BY/HAVING and projection; two window
    columns over the recorded ORDER BY are added, ROW_NUMBER (a position)
    and RANK (1 + the number of rows sorting strictly before). The
    engine's own peer test decides ties, so NULL placement and collation
    are exactly those of its ORDER BY. The statement returns
    (n, rank_at_k_plus_1, n_peer) over the first n = min(|Q'|, k+1) rows,
    n_peer counting rows whose RANK differs from their ROW_NUMBER (a row
    tied with its predecessor). The boundary is strict iff n <= k or
    rank_at_k_plus_1 = k+1; the sequence is strict iff n_peer = 0. None when the probe cannot
    preserve Q''s rows (DISTINCT, compound queries) or there is no ORDER
    BY; an unavailable probe is not strict.
    """
    import sqlglot
    from sqlglot import expressions as exp
    try:
        t = sqlglot.parse_one(candidate_sql, read=dialect)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(t, exp.Select) or t.args.get("distinct"):
        return None
    keys = _resolved_order_keys(t, dialect)
    if not keys:
        return None

    def win(fn: str) -> Any:
        return exp.Window(this=exp.Anonymous(this=fn, expressions=[]),
                          order=exp.Order(expressions=[o.copy()
                                                       for o in keys]))
    inner = t.copy()
    inner.set("order", None)
    inner.set("limit", None)
    inner.set("offset", None)
    inner.set("expressions", [e.copy() for e in t.expressions] + [
        exp.alias_(win("ROW_NUMBER"), "_tgms_rn"),
        exp.alias_(win("RANK"), "_tgms_rk")])
    body = inner.sql(dialect=dialect)
    return (f"SELECT COUNT(*), MAX(CASE WHEN _tgms_rn = {k + 1} "
            f"THEN _tgms_rk END), SUM(CASE WHEN _tgms_rk <> _tgms_rn "
            f"THEN 1 ELSE 0 END) FROM ({body}) AS _tgms_b "
            f"WHERE _tgms_rn <= {k + 1}")


def boundary_is_strict(n: Any, rank_k1: Any, k: int) -> bool:
    """Decode the probe's (n, rank_at_k_plus_1)."""
    if not isinstance(n, int):
        return False
    if n <= k:
        return True
    return isinstance(rank_k1, int) and rank_k1 == k + 1


def sequence_is_strict(n: Any, n_peer: Any) -> bool:
    """Decode the probe's peer count: every adjacent pair among the first
    min(|Q'|, k+1) rows strictly ordered. An empty Q' is strict."""
    if not isinstance(n, int):
        return False
    if n == 0:
        return True
    return isinstance(n_peer, int) and n_peer == 0


@dataclass
class RankedRead:
    """A ranked page, its candidate count and its boundary check, all
    read in one transaction (one basis)."""
    rows: list[tuple]
    candidate_count: int
    boundary_strict: bool
    sequence_strict: bool
    boundary_status: str          # "checked" | "unavailable" | "error: ..."
    token: dict[str, Any]
    seconds: dict[str, float]     # page / boundary / count (+ commit)


def ranked_page_in_snapshot(conn: Any, sql: str, shape: TopKShape, *,
                            engine: str = "sqlite",
                            dialect: str | None = None) -> RankedRead:
    """Run the ranked statement, the rank-boundary probe and the
    unlimited count of Q' in ONE read transaction, through
    `sql_snapshot.consistent_page_and_count`: the probe runs in its
    `between` hook, after the page and before the count, on the same
    connection inside the same transaction, so all three are facts about
    one basis. A probe that cannot be built or fails is recorded as not
    strict; it never aborts the page.
    """
    from tgms.evidence import sql_snapshot as ss
    if not shape.is_topk or shape.candidate_sql is None:
        raise ValueError(f"not a top-k statement ({shape.reason})")
    dialect = dialect or {"postgres": "postgres"}.get(engine, engine)
    probe = boundary_probe_sql(shape.candidate_sql, shape.limit, dialect)
    state: dict[str, Any] = {"strict": False, "seq": False,
                             "status": "unavailable" if probe is None
                             else "checked"}
    marks: dict[str, float] = {}

    def between() -> None:
        marks["page_end"] = time.perf_counter()
        if probe is not None:
            try:
                (n, rk1, npeer), = ss._run(conn, ss._engine(engine), probe)
                state["strict"] = boundary_is_strict(n, rk1, shape.limit)
                state["seq"] = state["strict"] and sequence_is_strict(
                    n, npeer)
            except Exception as e:  # noqa: BLE001
                state["status"] = f"error: {type(e).__name__}: {e}"[:300]
        marks["boundary_end"] = time.perf_counter()

    t0 = time.perf_counter()
    rows, total, token = ss.consistent_page_and_count(
        conn, shape.candidate_sql, sql, engine=engine, between=between)
    t1 = time.perf_counter()
    return RankedRead(
        rows=rows, candidate_count=total,
        boundary_strict=bool(state["strict"]),
        sequence_strict=bool(state["seq"]),
        boundary_status=state["status"], token=token,
        seconds={"page": marks["page_end"] - t0,
                 "boundary": marks["boundary_end"] - marks["page_end"],
                 "count_and_commit": t1 - marks["boundary_end"]})


def snapshot_context(token: dict[str, Any]) -> str:
    """Execution-context token naming one read transaction."""
    ident = ",".join(f"{k}={token[k]}" for k in sorted(token)
                     if k not in ("consistent", "isolation"))
    return f"txn[{ident}]"
