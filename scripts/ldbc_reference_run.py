"""Run the vendored LDBC Cypher, **unmodified**, against a Neo4j reference
instance loaded per `docs/design/LDBC_REFERENCE_CORRECTNESS_DESIGN_2026-09-13.md`
§2, binding from the same `params.json` `scripts/ldbc_snb_params.py
export_bindings()` writes.

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing produced here is an LDBC Benchmark Result.**

The `neo4j` driver is imported lazily, inside `open_driver`, so this module —
and every test of it — loads and runs with no driver installed. A test
injects a fake object satisfying the small surface `run_query` actually
uses: `.run(query, parameters) -> Result`, where `Result` supports `.keys()`
and iteration over records that support `record[name]` — the same shape the
real driver's `Session.run` / `Result` / `Record` present, so a fake session
built for a test exercises the same code path a real one would.

No plan artifact and no query is edited here (§1: "an edit to a query is an
edit to the reference"). This runner reads a `.cypher` file's bytes, hands
them to the driver verbatim, and records the file's own sha256 alongside the
result so a later reader can confirm nothing was touched.

    uv run --extra eval python scripts/ldbc_reference_run.py \\
        --params benchmarks/ldbc-ref-v1/params.json \\
        --cypher-dir external_workloads/ldbc/bi/neo4j/queries \\
        --out benchmarks/ldbc-ref-v1
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

#: `docs/eval/REPRODUCE.md:36`: "conf: loopback only, auth off". The bolt
#: handshake still wants a non-null credential pair even with server-side
#: auth disabled — the same pair `scripts/neo4j_baseline.py:42` uses, kept
#: consistent rather than rediscovered.
DEFAULT_URI = "bolt://127.0.0.1:7687"
DEFAULT_AUTH = ("neo4j", "neo4j")


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def open_driver(uri: str = DEFAULT_URI, auth: tuple[str, str] = DEFAULT_AUTH) -> Any:
    """The only place `neo4j` is imported. A test never calls this — it
    injects a session directly into `run_one`/`run_all` — so the package
    need not be installed for the test suite."""
    import neo4j  # noqa: PLC0415 — deliberately lazy, see module docstring

    return neo4j.GraphDatabase.driver(uri, auth=auth)


def load_params(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def load_cypher(path: Path) -> str:
    return Path(path).read_text()


def canonicalize_value(value: Any) -> Any:
    """A driver record's value -> plain JSON.

    Temporal values (the driver's own `DateTime`/`Date`/`LocalDateTime`
    classes) are duck-typed via `.to_native()` — real py2neo/neo4j objects
    all expose it — and turned into **epoch milliseconds**, Neo4j's own unit
    (§1's normalization table): `ldbc_compare.py` is the one place that
    multiplies by 1,000, so the unit conversion happens in exactly one spot
    across both runners.
    """
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, list):
        return [canonicalize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: canonicalize_value(v) for k, v in value.items()}
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        native = to_native()
        if isinstance(native, datetime.datetime):
            if native.tzinfo is None:
                native = native.replace(tzinfo=datetime.timezone.utc)
            return int(native.timestamp() * 1000)
        if isinstance(native, datetime.date):
            epoch = datetime.date(1970, 1, 1)
            return (native - epoch).days * 86_400_000
    # a Node/Relationship or anything else unexpected: stringify rather than
    # silently drop it, so an unhandled type is visible in the output instead
    # of vanishing from a row.
    return str(value)


def run_query(session: Any, cypher_text: str,
             params: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """One query, one set of parameters, against an injected session.
    Returns `(columns, rows)`; `columns` is the result's own declared order
    (`Result.keys()`), never re-sorted — the same "envelope's declared
    column order" rule `--emit-rows` follows on the TGMS side."""
    result = session.run(cypher_text, params)
    columns = list(result.keys())
    rows = [{name: canonicalize_value(record[name]) for name in columns}
           for record in result]
    return columns, rows


def run_one(plan_id: str, cypher_path: Path, params: dict[str, Any],
           session: Any) -> dict[str, Any]:
    """One template, end to end: read the vendored query, run it verbatim,
    write the row-file format `ldbc_compare.py` reads."""
    text = load_cypher(cypher_path)
    t0 = time.time()
    columns, rows = run_query(session, text, params)
    wall_s = round(time.time() - t0, 3)
    payload_bytes = json.dumps(rows, sort_keys=True, default=str).encode()
    return {
        "plan_id": plan_id,
        "columns": columns,
        "rows": rows,
        "query_file": str(cypher_path),
        "query_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "params": params,
        "wall_s": wall_s,
        "result_digest": hashlib.sha256(payload_bytes).hexdigest(),
        "commit": _sha(),
        "host": platform.node(),
    }


def run_all(params_doc: dict[str, Any], cypher_dir: Path, session: Any,
           plan_ids: Iterable[str] | None = None,
           cypher_name: Any = None) -> dict[str, dict[str, Any]]:
    """Every plan `params_doc["rows"]` names (or `plan_ids`, if given) whose
    `.cypher` file is present. A missing file or a bind error for one plan is
    recorded under `"error"` rather than aborting the rest — the same
    partial-input tolerance `export_bindings` has, and for the same reason:
    a local run against a handful of vendored queries should not need every
    one of them present to produce results for the ones it has.

    `cypher_name(plan_id) -> filename` defaults to the LDBC convention
    (`bi-3.cypher`, `interactive-short-2.cypher`, ...); pass it explicitly
    where that convention does not apply (e.g. a test's own fixture names).
    """
    name_fn = cypher_name or _default_cypher_name
    rows = params_doc.get("rows", {})
    ids = list(plan_ids) if plan_ids is not None else sorted(rows)
    out: dict[str, dict[str, Any]] = {}
    for pid in ids:
        row = rows.get(pid)
        if row is None:
            out[pid] = {"error": f"{pid}: not in params.json"}
            continue
        if "error" in row:
            out[pid] = {"error": f"{pid}: params.json export failed: "
                                 f"{row['error']}"}
            continue
        cpath = cypher_dir / name_fn(pid)
        if not cpath.exists():
            out[pid] = {"error": f"{pid}: no cypher file at {cpath}"}
            continue
        try:
            out[pid] = run_one(pid, cpath, row["cypher"], session)
        except Exception as e:                             # noqa: BLE001
            out[pid] = {"error": f"{pid}: {type(e).__name__}: {e}"}
    return out


#: LDBC's own file-naming convention for the vendored queries, as
#: `bi/neo4j/scripts/import.sh` and the interactive impls repo lay them out.
#: BI ids map to `bi-<n>.cypher` (BI10 is the one exception carrying a
#: sub-letter in its *parameter* file, `bi-10a.csv`, but the query file
#: itself is plain `bi-10.cypher`); Interactive short reads map to
#: `interactive-short-<n>.cypher`.
#:
#: IS1/IS4/IS5 (RUNBOOK.md §3.1's documented gap, closed here, lane D1-fix
#: 2026-09-14): these three were added to the *parameter* binder
#: (`ldbc_snb_params.py::IV_SOURCES`, Lane D2, 2026-09-14) but never to this
#: table, so `run_all()` raised `KeyError` for them. Consistent with
#: `ldbc_snb_params.py::IV_SOURCES` and the plan artifacts'
#: `provenance.reference_cypher` fields (`benchmarks/tgir-v1/plans/IS1.json`,
#: `IS4.json`, `IS5.json`): IS1 -> `interactive-short-1.cypher`,
#: IS4 -> `interactive-short-4.cypher`, IS5 -> `interactive-short-5.cypher`.
_IS_NUM = {"IS1": 1, "IS2": 2, "IS3": 3, "IS4": 4, "IS5": 5, "IS6": 6,
          "IS7": 7}
_IC_NUM = {"IC2": 2, "IC5": 5, "IC6": 6, "IC8": 8, "IC9": 9, "IC11": 11,
          "IC12": 12}


def _default_cypher_name(plan_id: str) -> str:
    if plan_id.startswith("BI"):
        return f"bi-{plan_id[2:].lower()}.cypher"
    if plan_id in _IS_NUM:
        return f"interactive-short-{_IS_NUM[plan_id]}.cypher"
    if plan_id in _IC_NUM:
        return f"interactive-complex-{_IC_NUM[plan_id]}.cypher"
    raise KeyError(f"no known vendored filename convention for {plan_id!r}")


def write_ref_exports(out_dir: Path, results: dict[str, dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for pid, rec in results.items():
        (out_dir / f"ref-{pid}.json").write_text(
            json.dumps(rec, indent=1, sort_keys=True, default=str))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", required=True)
    ap.add_argument("--cypher-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--uri", default=DEFAULT_URI)
    ap.add_argument("--user", default=DEFAULT_AUTH[0])
    ap.add_argument("--password", default=DEFAULT_AUTH[1])
    ap.add_argument("--plan", action="append", default=[])
    args = ap.parse_args()

    params_doc = load_params(Path(args.params))
    driver = open_driver(args.uri, (args.user, args.password))
    try:
        with driver.session() as session:
            results = run_all(params_doc, Path(args.cypher_dir), session,
                              plan_ids=args.plan or None)
    finally:
        driver.close()

    write_ref_exports(Path(args.out), results)
    errors = {pid: r["error"] for pid, r in results.items() if "error" in r}
    for pid, rec in results.items():
        status = rec.get("error", f"{len(rec.get('rows', []))} rows")
        print(f"  {pid:6s} {status}")
    print(f"record: {args.out} ({len(results) - len(errors)} ok, "
         f"{len(errors)} error)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
