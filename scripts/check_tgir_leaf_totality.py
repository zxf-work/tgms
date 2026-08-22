"""M2.2's totality gate: every operator is a leaf, and none reaches execution
unwrapped.

Three properties, each of which would be a silent hole if it failed:

1. **Every `REGISTRY` operator has exactly one leaf classification.** An
   operator missing from the `∅` decision is not "probably store-reading" — it
   is unclassified, and §2.0 obligation 6 makes the classification part of the
   operator's definition.
2. **No operator reaches its kernel unwrapped.** Checked by observation, not by
   reading the code: every registry operator is called through
   `call_operator` and each call must pass through `evaluate_leaf` exactly
   once. The escape hatch is then verified to be the *only* bypass.
3. **The `∅`-classified kernel really is `∅`.** `compute` is the one, and every
   one of its seventeen `fn` values runs against a `NullAdapter` — any store
   access raises `StateError` by name. §2.0 asks for the classification to be
   checkable rather than asserted; this is the check.

    uv run python scripts/check_tgir_leaf_totality.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

from tgms.core.errors import StateError, TgmsError
from tgms.storage.duckdb_adapter import DuckDBAdapter
from tgms.temporal.algebra import (
    REGISTRY, call_operator, ensure_all_registered, validate_args,
)
from tgms.tgir import evaluate as tgir_evaluate
from tgms.tgir.leaf import LEAF_VT_MODE, build_leaf
from tgms.tgir.node import EMPTY_SCOPE_OPS
from tgms.tgir.rollout import PLAN_PATH_ENV, plan_path_enabled

FAILURES: list[str] = []

W = {"t_a": 0, "t_b": 100}

#: One call per operator, with arguments that reach the kernel on an empty
#: store. Reaching the kernel is the point — the payload is irrelevant here.
CALLS: dict[str, dict[str, Any]] = {
    "entity_history": {"uid": "u1"},
    "version_history": {"kind": "node", "window": W},
    "snapshot_subgraph": {"seeds": ["u1"], "t_valid": 10},
    "diff_snapshots": {"t1": 10, "t2": 20},
    "neighborhood_evolution": {"uid": "u1", "t1": 10, "t2": 20},
    "resolve_entities": {"query": "u1"},
    "aggregate_events": {"group_by": [], "aggregates": [{"agg": "count"}], "window": W},
    "graph_metric_timeseries": {"metric": "edge_event_count", "window": W, "stride": 10},
    "burst_detection": {"target": {"kind": "edge_event_rate"}, "window": W, "stride": 10},
    "count_temporal_motifs": {"motif": "M_2node_pingpong", "delta": 5, "window": W},
    "find_temporal_motif_instances": {"motif": "M_2node_pingpong", "delta": 5, "window": W},
    "temporal_reachability": {"src": "u1", "window": W},
    "temporal_paths": {"src": "u1", "dst": "u2", "window": W},
    "co_active": {"a_spec": {"src": "u1"}, "b_spec": {"src": "u2"},
                  "allen_relation": {"relation": "overlaps"}},
    "compute": {"fn": "count", "input": [{"x": 1}]},
}

#: Every `compute` function, so the `∅` classification is checked across the
#: whole operator rather than on one lucky branch.
ROWS = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
COMPUTE_CALLS: dict[str, dict[str, Any]] = {
    "count": {"input": ROWS},
    "sum": {"input": ROWS, "field": "x"},
    "min": {"input": ROWS, "field": "x"},
    "max": {"input": ROWS, "field": "x"},
    "mean": {"input": ROWS, "field": "x"},
    "median": {"input": ROWS, "field": "x"},
    "topk": {"input": ROWS, "field": "x", "k": 1},
    "filter": {"input": ROWS, "field": "x", "cmp": "gt", "value": 1},
    "ratio": {"x": 6, "y": 3},
    "diff": {"x": 6, "y": 3},
    "percent": {"x": 6, "y": 3},
    "intersect": {"input": ROWS, "other": ROWS, "field": "x", "other_field": "x"},
    "difference": {"input": ROWS, "other": ROWS, "field": "x", "other_field": "x"},
    "union": {"input": ROWS, "other": ROWS, "field": "x", "other_field": "x"},
    "derive": {"input": ROWS, "field": "x", "field2": "y", "op": "add", "into": "z"},
    "join": {"input": ROWS, "other": [{"x": 1, "z": 9}], "on": "x"},
    "interval_relation": {"a": {"start": 0, "end": 5}, "b": {"start": 1, "end": 6}},
}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        FAILURES.append(label)


def adapter() -> DuckDBAdapter:
    return DuckDBAdapter(":memory:")


def seeded_adapter() -> DuckDBAdapter:
    """A two-node, one-edge store, so the operators reach their kernels rather
    than refusing on an unknown uid before they get there."""
    a = adapter()
    a.apply_ops([
        {"op": "assert_node", "uid": "u1", "label": "N", "props": {},
         "vt_s": 0, "vt_e": 100, "source": "i", "provenance_ref": None},
        {"op": "assert_node", "uid": "u2", "label": "N", "props": {},
         "vt_s": 0, "vt_e": 100, "source": "i", "provenance_ref": None},
        {"op": "assert_edge", "src": "u1", "dst": "u2", "rel_type": "R",
         "props": {}, "vt_s": 0, "vt_e": 100, "disc": "",
         "source": "i", "provenance_ref": None},
    ], 1)
    return a


def check_classification() -> None:
    ops = set(REGISTRY)
    check("every registry operator has a Σ/vt_mode derivation",
          ops <= set(LEAF_VT_MODE), str(sorted(ops - set(LEAF_VT_MODE))))
    check("the ∅ set is a subset of the registry",
          EMPTY_SCOPE_OPS <= ops, str(sorted(EMPTY_SCOPE_OPS - ops)))
    unclassified = []
    for op in sorted(ops):
        # the same filled args the call path builds a leaf from — `sigma_for`
        # reads required keys, which validation guarantees are present
        filled = validate_args(op, dict(CALLS[op]))
        leaf = build_leaf(op, filled, REGISTRY[op].output_fields)
        # exactly one classification: withheld xor store-reading
        if leaf.withhold_adapter == leaf.reads_store:
            unclassified.append(op)
    check("every operator has exactly one leaf classification (∅ xor reads)",
          not unclassified, str(unclassified))
    check("compute is the only ∅ operator",
          EMPTY_SCOPE_OPS == {"compute"}, str(sorted(EMPTY_SCOPE_OPS)))


def check_wrapping() -> None:
    """No operator reaches its kernel unwrapped — observed, not read."""
    ensure_all_registered()
    seen: list[str] = []
    real = tgir_evaluate.evaluate_leaf

    def spy(leaf, adapter_, *a, **kw):
        seen.append(leaf.op)
        return real(leaf, adapter_, *a, **kw)

    tgir_evaluate.evaluate_leaf = spy
    try:
        a = seeded_adapter()
        unwrapped: list[str] = []
        for op, args in CALLS.items():
            before = len(seen)
            try:
                call_operator(a, op, dict(args))
            except TgmsError:
                # A kernel error is still an *execution*, and it happens inside
                # the leaf. Only a call that never constructed a leaf is a hole
                # — and a pre-execution refusal (cost, validation) is not one:
                # it is admission, which C5 keeps at its existing site.
                pass
            if len(seen) != before + 1:
                unwrapped.append(op)
        check("every registry operator was called",
              set(CALLS) == set(REGISTRY), str(sorted(set(REGISTRY) - set(CALLS))))
        check("every call was wrapped in exactly one leaf",
              not unwrapped, str(unwrapped))
        check("and the fifteen leaves are the fifteen operators",
              set(seen) == set(REGISTRY), str(sorted(set(REGISTRY) ^ set(seen))))

        # the escape hatch is the only bypass, and it is off by default
        check("the plan path is on unless the escape hatch says otherwise",
              plan_path_enabled())
        before = len(seen)
        os.environ[PLAN_PATH_ENV] = "off"
        try:
            check("TGIR_PLAN_PATH=off disables the plan path",
                  not plan_path_enabled())
            call_operator(a, "version_history", {"kind": "node", "window": W})
            check("and then no leaf is constructed at all", len(seen) == before)
        finally:
            del os.environ[PLAN_PATH_ENV]
        check("unsetting it restores the plan path", plan_path_enabled())
        call_operator(a, "version_history", {"kind": "node", "window": W})
        check("and the next call is wrapped again", len(seen) == before + 1)
    finally:
        tgir_evaluate.evaluate_leaf = real


def check_empty_scope_kernel() -> None:
    """`compute` is `∅`: its kernel never touches the adapter. Seventeen
    functions, one `NullAdapter`, and a `StateError` if any of them reads."""
    ensure_all_registered()
    a = adapter()
    touched, errors = [], []
    for fn, args in COMPUTE_CALLS.items():
        try:
            call_operator(a, "compute", {"fn": fn, **args})
        except StateError as e:
            touched.append((fn, str(e)))
        except TgmsError as e:
            errors.append((fn, e.code, str(e)[:60]))
    check("every compute fn is covered",
          set(COMPUTE_CALLS) == set(_compute_fns()),
          str(sorted(set(_compute_fns()) - set(COMPUTE_CALLS))))
    check("no compute fn touches the storage adapter", not touched, str(touched))
    check("and none failed for an unrelated reason", not errors, str(errors))


def _compute_fns() -> list[str]:
    return REGISTRY["compute"].args_schema["properties"]["fn"]["enum"]


def main() -> int:
    ensure_all_registered()
    check_classification()
    check_wrapping()
    check_empty_scope_kernel()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nleaf totality holds: 15 operators, 15 leaves, no unwrapped path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
