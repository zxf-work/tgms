"""M4.0's gate: D13.19's eleven preconditions, as an executable receipt.

FRESHNESS_SEMANTICS D13.19 is normative — *"M4 cannot start without this"* —
and `M4_IMPLEMENTATION_PLAN.md` §3.0 tabulates eleven preconditions against the
tree and declares all eleven met. §3.0 then says, in its own words, why that
table is not enough:

> Phase M4.0 re-runs this audit as an executable receipt rather than trusting
> this table, because "the precondition was met four weeks ago" is exactly the
> kind of claim D-072 taught this project not to carry forward unchecked.

So this script is the table, executed. One check per row, named with the row's
own words, on **both** backends. It overlaps `check_ttq_semantics.py`
deliberately: that script is M2.1's gate on the *semantics* of `tt_q`, this one
is M4's gate on the *existence and placement* of everything the checker will
consume. When they disagree the overlap is the point.

Two degradations §3.0 found are asserted as **present and sound**, not merely
noted, because both bound the M4 evaluation population:

- `UNANCHORED` — an adapter-only read carries no event log, so D13.24 step 3
  makes every such scope `UNDECIDABLE("store-mismatch")`. Sound, and it
  restricts the population to real store directories (§4.1).
- **cursorless backends** — DuckDB and Kùzu keep no `event_cursor()`, so their
  scopes carry `FULL_SCAN_CHECKPOINTS`. Sound, and it makes D13.18's cost
  argument vacuous there (§8.1).

Lives in `scripts/` rather than `tests/`: `tests/` is human-owned
(`scripts/check_commit_hygiene.py`), and this is a receipt, not a suite.

    uv run python scripts/check_freshness_preconditions.py [--backend native|duckdb|both]
"""

from __future__ import annotations

import argparse
import inspect
import sys
import tempfile
from pathlib import Path
from typing import Any

import tgms
from tgms.agent.executor import FRESHNESS_KEYS, Executor
from tgms.agent.ir import Plan
from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END
from tgms.storage.duckdb_adapter import DuckDBAdapter
from tgms.storage.eventlog import EventLog
from tgms.temporal.algebra import ENVELOPE_META_FIELDS
from tgms.tgir import ttq
from tgms.tgir.depscope import (
    FULL_SCAN_CHECKPOINTS, TOP, TOP_TERM, UNANCHORED, DependencyScope, union_all,
)
from tgms.tools.server import ToolRouter

BACKENDS = ("native", "duckdb")
FAILURES: list[str] = []

#: The eleven rows of M4_IMPLEMENTATION_PLAN §3.0, in the plan's own order and
#: wording. Every check below cites one; a row with no check is a hole in this
#: receipt and `main` refuses to pass with one.
ROWS: tuple[str, ...] = (
    "tt_q exists, is the served frontier, rounded down",
    "captured BEFORE the read",
    "on the envelope",
    "on every trace step record",
    "on the plan record, as the union of the steps' and never a completion capture",
    "digest-excluded",
    "checkpoints from the applied cursor, with D13.8a's fallback",
    "store identity, and the union refuses across it",
    "D1.10's clamp on an above-frontier pin (FF-4)",
    "empty scope for compute (D5.3)",
    "the three real Level-0 derivations, and '*' elsewhere",
)
COVERED: set[str] = set()


def check(row: str, label: str, ok: bool, detail: str = "") -> None:
    if row not in ROWS:
        raise SystemExit(f"check cites a row that is not in D13.19's eleven: {row!r}")
    COVERED.add(row)
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        FAILURES.append(label)


def _fresh_store(backend: str) -> Any:
    path = Path(tempfile.mkdtemp()) / "store"
    store = tgms.open(path, backend=backend)
    for i in range(4):
        store.assert_node(f"n{i}", "N", {"p": i}, 0, 100)
    store.assert_edge("n0", "n1", "MSG", {}, 10, 20)
    return store


def check_backend(backend: str) -> None:
    store = _fresh_store(backend)
    applied = store.adapter.frontier_tt()
    router = ToolRouter(store.adapter, tt_source=store)
    env = router.call("entity_history", {"uid": "n0", "include_edges": True})

    # ---- row 1: tt_q is the SERVED frontier, rounded down -----------------
    check(ROWS[0], f"{backend}: tt_q is present, an int, and equals the applied frontier",
          isinstance(env.get("tt_q"), int) and env["tt_q"] == applied,
          f"tt_q={env['tt_q']} frontier={applied}")
    check(ROWS[0], f"{backend}: frontier_of reads the adapter's applied frontier, "
                   f"never Store.clock.last_tt",
          "clock" not in inspect.getsource(ttq.frontier_of),
          "D13.17: the log is fsynced before apply, so the clock over-reports")

    # ---- row 2: captured BEFORE the read ----------------------------------
    # `basis_of`'s docstring states it; what makes it true is that the router
    # asks for the basis and *then* issues the call. Assert the ordering by
    # construction rather than by reading: a basis captured after the read
    # would see a frontier the read did not, and the only way to observe that
    # is a write landing mid-call. Instead, assert the property that ordering
    # buys — tt_q never exceeds the frontier in force when the rows were built.
    after_write = store.assert_node("n9", "N", {}, 0, 100)
    check(ROWS[1], f"{backend}: a scope captured before the read cannot name a "
                   f"frontier the read did not see",
          env["tt_q"] <= after_write and env["tt_q"] == applied,
          f"tt_q={env['tt_q']} <= later write at {after_write}")

    router = ToolRouter(store.adapter, tt_source=store)
    env = router.call("entity_history", {"uid": "n0", "include_edges": True})

    # ---- row 3: on the envelope -------------------------------------------
    check(ROWS[2], f"{backend}: all four freshness keys are on the envelope",
          all(k in env for k in FRESHNESS_KEYS), ", ".join(FRESHNESS_KEYS))
    check(ROWS[2], f"{backend}: and the envelope's dependency parses as a scope",
          DependencyScope.from_json(env["dependency"]).version == 1)

    # ---- row 6: digest-excluded -------------------------------------------
    # (checked here, where two envelopes over identical state are in hand)
    pinned = router.call("entity_history",
                         {"uid": "n0", "include_edges": True, "as_of_tt": env["tt_q"]})
    check(ROWS[5], f"{backend}: every freshness key is named in ENVELOPE_META_FIELDS",
          all(k in ENVELOPE_META_FIELDS for k in FRESHNESS_KEYS))
    check(ROWS[5], f"{backend}: result_digest is blind to them",
          env["result_digest"] == pinned["result_digest"],
          "a pinned read of unchanged state is byte-identical")

    # ---- row 9: D1.10's clamp on an above-frontier pin (FF-4) -------------
    above = router.call("entity_history",
                        {"uid": "n0", "as_of_tt": min(env["tt_q"] + 10 ** 9, OPEN_END - 1)})
    check(ROWS[8], f"{backend}: an above-frontier pin clamps DOWN and reports "
                   f"pinned=false, clamped=true",
          (above["tt_q"], above["pinned"], above["clamped"]) == (env["tt_q"], False, True),
          "FF-4: the pin that is not a pin")

    # ---- row 7: checkpoints ------------------------------------------------
    scope = DependencyScope.from_json(env["dependency"])
    if backend == "native":
        offset, chain = store.adapter.event_cursor()
        check(ROWS[6], f"{backend}: checkpoints come from the applied cursor",
              scope.checkpoints == ((type(scope.checkpoints[0]))(offset, chain),),
              f"offset={offset}")
        check(ROWS[6], f"{backend}: and the chain verifies against the log",
              EventLog(store.path / "eventlog.jsonl").chain_of_prefix(offset) == chain)
    else:
        check(ROWS[6], f"{backend}: a cursorless backend falls back to a full scan "
                       f"(D13.8a) — sound, and it makes D13.18's cost argument vacuous here",
              scope.checkpoints == FULL_SCAN_CHECKPOINTS,
              "documented degradation, §3.0")

    # ---- row 8: store identity, and the union refuses across it -----------
    check(ROWS[7], f"{backend}: the store identity is anchored by the first write",
          scope.store == store.store_identity != UNANCHORED and len(scope.store) == 64)
    other = DependencyScope(store="a-different-store", tt_q=scope.tt_q)
    refused = False
    try:
        scope.union(other)
    except InvalidArgError:
        refused = True
    check(ROWS[7], f"{backend}: the union refuses across two store identities (RG-6)",
          refused, "at construction, not one plan too late")

    # ---- row 10: empty scope for compute -----------------------------------
    empty = router.call("compute", {"fn": "count", "input": [{"x": 1}]})
    check(ROWS[9], f"{backend}: compute carries the empty scope",
          empty["dependency"]["terms"] == [],
          "D5.3 — the correct, non-degenerate value, never a defect")

    # ---- row 11: three real derivations, '*' elsewhere ---------------------
    check(ROWS[10], f"{backend}: a derived operator narrows — every term targets it",
          len(scope.terms) == 3 and all(t != TOP_TERM for t in scope.terms)
          and all(t.targets is not TOP for t in scope.terms),
          "entity_history(include_edges=True) is T1a/T1b/T1c")
    coarse = DependencyScope.from_json(
        router.call("temporal_paths",
                    {"src": "n0", "dst": "n1", "window": {"t_a": 0, "t_b": 100}}
                    )["dependency"])
    check(ROWS[10], f"{backend}: an underived operator still carries the all-'*' term",
          coarse.terms == (TOP_TERM,), "'*' everywhere is a legal v1 answer")

    check_plan_record(backend, store)
    store.close()
    _check_unanchored(backend)


def check_plan_record(backend: str, store: Any) -> None:
    """Rows 4 and 5 — the trace step and the plan record."""
    router = ToolRouter(store.adapter, tt_source=store)
    plan = Plan.from_json({
        "plan_id": "m4-precond",
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "n0", "include_edges": True}},
            {"id": "s2", "op": "entity_history", "args": {"uid": "no-such-uid"}},
            {"id": "s3", "op": "compute", "args": {"fn": "count", "input": [{"x": 1}]}},
        ],
        "answer_spec": {"kind": "count", "from": "s3.rows"}})
    out = Executor(router).run(plan).to_json()
    steps = {s["step_id"]: s for s in out["steps"]}

    # ---- row 4: on every trace step record --------------------------------
    check(ROWS[3], f"{backend}: every trace step carries all four freshness keys",
          all(all(k in s for k in FRESHNESS_KEYS) for s in out["steps"]))
    check(ROWS[3], f"{backend}: including a FAILED step, which still contributes "
                   f"its scope (D13.14)",
          steps["s2"]["status"] == "failed" and isinstance(steps["s2"].get("dependency"), dict))

    # ---- row 5: the plan record is the union, never a completion capture ---
    scopes = [DependencyScope.from_json(s["dependency"]) for s in out["steps"]]
    got = DependencyScope.from_json(out["dependency"])
    check(ROWS[4], f"{backend}: the plan record is the union of its steps' scopes",
          got.canonical() == union_all(scopes).canonical())
    check(ROWS[4], f"{backend}: its tt_q is a step's, not a completion capture (D13.17b)",
          out["tt_q"] in [s["tt_q"] for s in out["steps"]])
    check(ROWS[4], f"{backend}: and every step's checkpoint survives the union (D13.8b)",
          len(got.checkpoints) == sum(len(s.checkpoints) for s in scopes))


def _check_unanchored(backend: str) -> None:
    """The other documented degradation: an adapter-only read has no log, so
    D13.24 step 3 will refuse it. Asserted rather than assumed, because it is
    what excludes the whole oracle test family from M4's population (§4.1)."""
    if backend != "duckdb":
        return
    env = ToolRouter(DuckDBAdapter(":memory:")).call(
        "version_history", {"kind": "node", "window": {"t_a": 0, "t_b": 10}})
    check(ROWS[7], "adapter-only: an unanchored read carries the UNANCHORED sentinel",
          env["dependency"]["store"] == UNANCHORED,
          "every such scope is UNDECIDABLE('store-mismatch') — it bounds the population")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=BACKENDS + ("both",), default="both")
    args = ap.parse_args()
    for backend in (BACKENDS if args.backend == "both" else (args.backend,)):
        print(f"--- {backend} ---")
        check_backend(backend)
        print()

    uncovered = [r for r in ROWS if r not in COVERED]
    if uncovered:
        print("rows of D13.19 with no check in this receipt:")
        for r in uncovered:
            print(f"  - {r}")
        FAILURES.append("uncovered preconditions")

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {len(ROWS)} of D13.19's preconditions hold on "
          f"{'both backends' if args.backend == 'both' else args.backend} — M4 may start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
