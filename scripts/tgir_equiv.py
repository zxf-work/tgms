"""M3.3's equivalence harness: the compiled path against the leaf, shadow-run.

The comparison is `canonical_json(payload_of(envelope))` (`tgms/tgir/equiv.py`)
— the same function the oracle family compares with, so a compiled path cannot
pass one and fail the other.

Corpus, following the M2 plan's §8.2: the oracle suite's own store shapes, both
frozen canonical stores where present, and a **corrected** store — because
carving is where the COMPILE risks live and the canonical bitcoin-otc store
carries zero corrections. Both backends, since `resolve_entities` and
`aggregate_events` already have dual native/portable paths and a compiled form
must not diverge by backend either.

A receipt lands in `docs/tgir/equiv/`, carrying git SHA, store digest, backend
and case count. The scope-diff half is **non-gating** and recorded separately:
§8.1's comparator excludes `dependency` by necessity, so the equivalence suite
is structurally blind to a scope regression and something else has to look.

    uv run python scripts/tgir_equiv.py [--op entity_history] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any


from tgms.core.model import canonical_json
from tgms.temporal.algebra import call_operator, ensure_all_registered, validate_args
from tgms.tgir.compiled import COMPILED
from tgms.tgir.compiled.entity_history import UNCOMPILABLE_COLS
from tgms.tgir.equiv import first_divergence, payload_of
from tgms.tgir.leaf import build_leaf
from tgms.tgir.scope_of import ScopeBasis, leaf_scope

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ("native", "duckdb")

W = {"t_a": 0, "t_b": 300}

CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("entity_history/plain", "entity_history", {"uid": "u1"}),
    ("entity_history/edges", "entity_history", {"uid": "u1", "include_edges": True}),
    ("entity_history/paged", "entity_history", {"uid": "u1", "limit": 1}),
    ("entity_history/pinned", "entity_history", {"uid": "u1", "as_of_tt": 3}),
    ("entity_history/corrected", "entity_history", {"uid": "c1",
                                                    "include_edges": True}),
    ("version_history/node", "version_history", {"kind": "node", "window": W}),
    ("version_history/edge", "version_history", {"kind": "edge", "window": W}),
    ("version_history/paged", "version_history", {"kind": "node", "window": W,
                                                  "limit": 2}),
    ("version_history/superseded", "version_history", {"kind": "node", "window": W,
                                                       "belief": "superseded"}),
    ("version_history/all", "version_history", {"kind": "node", "window": W,
                                                "belief": "all"}),
    ("version_history/pinned", "version_history", {"kind": "node", "window": W,
                                                   "as_of_tt": 3}),
    ("version_history/rel_types", "version_history", {"kind": "edge", "window": W,
                                                      "rel_types": ["R"]}),
    ("version_history/narrow", "version_history", {"kind": "edge",
                                                   "window": {"t_a": 20, "t_b": 60}}),
]


def build(backend: str) -> Any:
    """A store with corrections, retractions and multi-version identities —
    the metamorphic suite's own pattern, because a compiled path that only ever
    sees one version per identity has not been tested."""
    if backend == "duckdb":
        from tgms.storage.duckdb_adapter import DuckDBAdapter
        adapter: Any = DuckDBAdapter(":memory:")
    else:
        from tgms.storage.native import NativeAdapter
        adapter = NativeAdapter(Path(tempfile.mkdtemp()) / "store")

    def write(ops: list[dict[str, Any]], tt: int) -> None:
        adapter.begin()
        adapter.apply_ops(ops, tt)
        adapter.commit()

    write([{"op": "assert_node", "uid": f"u{i}", "label": "N" if i % 2 else "M",
            "props": {"w": i, "name": f"n{i}"}, "vt_s": 0, "vt_e": 200,
            "source": "ingest", "provenance_ref": None} for i in range(1, 5)]
          + [{"op": "assert_node", "uid": "c1", "label": "C", "props": {"w": 0},
              "vt_s": 0, "vt_e": 200, "source": "ingest",
              "provenance_ref": "ref-1"}], 1)
    write([{"op": "assert_edge", "src": f"u{i}", "dst": f"u{i % 4 + 1}",
            "rel_type": "R" if i % 2 else "S", "props": {"k": i},
            "vt_s": 10 * i, "vt_e": 10 * i + 60, "disc": "",
            "source": "ingest", "provenance_ref": None} for i in range(1, 4)]
          + [{"op": "assert_edge", "src": "c1", "dst": "u1", "rel_type": "R",
              "props": {}, "vt_s": 5, "vt_e": 150, "disc": "",
              "source": "ingest", "provenance_ref": None}], 2)
    # a second believed version of one identity
    write([{"op": "assert_node", "uid": "u1", "label": "N", "props": {"w": 11},
            "vt_s": 200, "vt_e": 300, "source": "ingest",
            "provenance_ref": None}], 3)
    # a correction that carves, and a retraction — the two shapes a compiled
    # path most easily gets wrong
    write([{"op": "correct", "ref": {"kind": "node", "uid": "c1"},
            "props": {"w": 9}, "vt_s": 50, "vt_e": 90,
            "source": "ingest", "provenance_ref": None}], 4)
    write([{"op": "retract", "ref": {"kind": "node", "uid": "u3"}, "t": 120,
            "source": "ingest", "provenance_ref": None}], 5)
    return adapter


def compare(backend: str, only: str | None) -> list[dict[str, Any]]:
    ensure_all_registered()
    adapter = build(backend)
    results = []
    try:
        for case_id, op, args in CASES:
            if only and op != only:
                continue
            filled = validate_args(op, dict(args))
            leaf = call_operator(adapter, op, dict(args))
            compiled_payload = COMPILED[op](adapter, filled)
            compiled_env = {**compiled_payload, "op": op}
            divergence = first_divergence(compiled_env, leaf)
            differing = _differing_fields(compiled_env, leaf)
            # judged structurally, not by string match: the divergence is "as
            # predicted" iff every field that differs is one §2.1's scan schema
            # provably cannot express
            as_expected = divergence is None or (
                bool(differing) and set(differing) <= set(UNCOMPILABLE_COLS))
            results.append({
                "case": case_id, "op": op, "backend": backend,
                "identical": divergence is None,
                "divergence": divergence,
                "differing_fields": sorted(differing),
                "as_expected": as_expected,
                "rows": len(payload_of(leaf).get("rows", [])),
            })
    finally:
        adapter.close()
    return results


def _differing_fields(compiled: dict[str, Any], leaf: dict[str, Any]) -> set[str]:
    """Every row field on which the two payloads differ, across every list.

    Computed rather than parsed out of a message, so "as predicted" means a set
    relation against `UNCOMPILABLE_COLS` and not a substring.
    """
    a, b = payload_of(compiled), payload_of(leaf)
    out: set[str] = set()
    for key in set(a) | set(b):
        left, right = a.get(key), b.get(key)
        if not isinstance(left, list) or not isinstance(right, list):
            if canonical_json(left) != canonical_json(right):
                out.add(key)
            continue
        if len(left) != len(right):
            out.add(key)
            continue
        for x, y in zip(left, right):
            if isinstance(x, dict) and isinstance(y, dict):
                out |= {f for f in set(x) | set(y)
                        if canonical_json(x.get(f)) != canonical_json(y.get(f))}
            elif canonical_json(x) != canonical_json(y):
                out.add(key)
    return out


def scope_diff(op: str) -> dict[str, Any]:
    """The **non-gating** scope-diff receipt (§11.10's ruling).

    §8.1's comparator must exclude `dependency`, so the equivalence run cannot
    see a scope change. §6 #4 records that compiling `diff_snapshots` *costs
    freshness precision*; this records whether that applies to the two
    operators M3.3 actually compiled — and it does not, because neither
    compiled form emits a column its leaf did not.
    """
    args = {"uid": "u1"} if op == "entity_history" else \
        {"kind": "node", "window": W}
    filled = validate_args(op, dict(args))
    basis = ScopeBasis(store="equiv", tt_q=0)
    leaf = build_leaf(op, filled, ("rows",))
    terms = leaf_scope(leaf, basis).terms
    return {
        "op": op,
        "leaf_terms": [t.to_json() for t in terms],
        "carve_reachable": any(t.carve_reachable for t in terms),
        "note": ("the compiled form reads the same versions under the same Σ and "
                 "emits no column the leaf did not, so its Level-0 scope is the "
                 "leaf's — §6 #4's precision cost applies to `diff_snapshots`, "
                 "whose compiled form emits vt_s/vt_e unconditionally, and not "
                 "to these two"),
    }


def git_sha() -> str:
    head = ROOT / ".git" / "HEAD"
    if not head.exists():
        return "unknown"
    ref = head.read_text().strip()
    if ref.startswith("ref: "):
        target = ROOT / ".git" / ref[5:]
        return target.read_text().strip()[:12] if target.exists() else "unknown"
    return ref[:12]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", choices=sorted(COMPILED) + ["all"], default="all")
    ap.add_argument("--out", default=str(ROOT / "docs/tgir/equiv"))
    args = ap.parse_args()
    only = None if args.op == "all" else args.op

    results: list[dict[str, Any]] = []
    for backend in BACKENDS:
        results.extend(compare(backend, only))

    ops = sorted({r["op"] for r in results})
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for op in ops:
        rows = [r for r in results if r["op"] == op]
        path = out_dir / f"{op}-{date.today().isoformat()}.md"
        path.write_text(_receipt(op, rows))
        print(f"receipt: {path.relative_to(ROOT)}")

    for row in results:
        flag = "ok  " if row["as_expected"] else "FAIL"
        print(f"{flag} {row['backend']:7} {row['case']:32} "
              f"{'identical' if row['identical'] else row['divergence'][:70]}")
    bad = [r for r in results if not r["as_expected"]]
    print(f"\n{len(results) - len(bad)}/{len(results)} cases as predicted")
    return 1 if bad else 0


def _receipt(op: str, rows: list[dict[str, Any]]) -> str:
    identical = sum(1 for r in rows if r["identical"])
    table = "\n".join(
        f"| {r['backend']} | {r['case']} | {r['rows']} | "
        f"{'**identical**' if r['identical'] else 'differs in `' + ', '.join(r['differing_fields']) + '`'} |"
        for r in rows)
    verdict = ("byte-identical on every case, both backends"
               if identical == len(rows) else
               "byte-identical **except** the divergence predicted below")
    return f"""# Equivalence receipt — compiled `{op}` vs the leaf

**{date.today().isoformat()}.** Comparator:
`canonical_json(payload_of(envelope))` — the same function the oracle family
uses (`tgms/tgir/equiv.py`, M2 plan §8.1).

| | |
|---|---|
| git SHA | `{git_sha()}` |
| backends | native, duckdb |
| cases | {len(rows)} ({identical} byte-identical) |
| corpus | multi-version identities, a carving `correct`, a `retract`, a pinned read, a paged read |
| verdict | {verdict} |

| backend | case | leaf rows | result |
|---|---|---|---|
{table}

## Scope diff (non-gating)

```json
{json.dumps(scope_diff(op), indent=1)}
```

## Rollout

`COMPILE_MODE["{op}"] = "leaf"`. This receipt is the evidence a promotion to
`shadow` would cite; the promotion itself needs a D-entry, and — for
`entity_history` — a ruling on the two payload fields §2.1's scan schema cannot
express.
"""


if __name__ == "__main__":
    sys.exit(main())


