"""M3.0's external gates: the core evaluators against operators that already
have an oracle.

Without this, M3.0's only gate would be self-consistency — the evaluators
agreeing with themselves. Two independent checks, both on real stores and both
backends:

**(1) Scan equivalence.** A `NodeScan`/`EdgeScan` under a leaf-equivalent Σ,
followed by `Filter`/`Project`, must reproduce `entity_history` and
`version_history`'s row sets **exactly** — same rows, same order. Those two
operators are the ones `TGIR_SPEC.md` §6 #1/#2 compile to a scan plus a
projection, so the comparison is the spec's own claim, executed.

**(2) `Join{inner}` versus `compute join`.** `compute join` is an existing
operator with an oracle, restricted to unique keys on both sides. A core
`Join{inner}` over unique keys must agree with it row for row. This is the
gate that makes M3.0's output testable against something outside itself.

    uv run python scripts/check_core_equivalence.py [--backend native|duckdb|both]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from tgms.core.model import canonical_json
from tgms.temporal.algebra import call_operator, ensure_all_registered
from tgms.tgir.eval import evaluate_core
from tgms.tgir.expr import Col, Cmp, Lit
from tgms.tgir.node import EdgeScan, Filter, Join, NodeScan, Order, Project, SortKey
from tgms.tgir.types import Sigma

BACKENDS = ("native", "duckdb")
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        FAILURES.append(label)


def build(backend: str) -> Any:
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
            "props": {"name": f"n{i}", "w": i}, "vt_s": 0, "vt_e": 100,
            "source": "i", "provenance_ref": None} for i in range(1, 7)], 1)
    # a second version of one identity, so a version *list* is not a singleton
    write([{"op": "assert_node", "uid": "u1", "label": "N",
            "props": {"name": "n1b", "w": 11}, "vt_s": 100, "vt_e": 200,
            "source": "i", "provenance_ref": None}], 2)
    write([{"op": "assert_edge", "src": f"u{i}", "dst": f"u{i % 6 + 1}",
            "rel_type": "R" if i % 2 else "S", "props": {"k": i},
            "vt_s": 10 * i, "vt_e": 10 * i + 40, "disc": "",
            "source": "i", "provenance_ref": None} for i in range(1, 6)], 3)
    return adapter


# ---------------------------------------------------------------------------
# (1) scan equivalence
# ---------------------------------------------------------------------------

def check_entity_history(adapter: Any, backend: str) -> None:
    """§6 #1: `entity_history(uid)` is a `NodeScan(uids=[uid])` over the whole
    valid-time extent, projected to `to_json()`'s field list."""
    env = call_operator(adapter, "entity_history", {"uid": "u1", "limit": 1000})
    expected = [{"vid": r["vid"], "uid": r["uid"], "label": r["label"],
                 "vt_s": r["vt_s"], "vt_e": r["vt_e"]} for r in env["rows"]]

    plan = Project(NodeScan("p", uids=("u1",), sigma_=Sigma.default()),
                   (("vid", Col("p.vid")), ("uid", Col("p.uid")),
                    ("label", Col("p.label")), ("vt_s", Col("p.vt_s")),
                    ("vt_e", Col("p.vt_e"))))
    got = evaluate_core(plan, adapter).rows()
    check(f"{backend}: NodeScan(uids) reproduces entity_history's rows",
          canonical_json(got) == canonical_json(expected),
          f"{len(got)} vs {len(expected)} rows")


def check_version_history(adapter: Any, backend: str) -> None:
    """§6 #2: `version_history(kind, window)` is a scan under the window,
    projected to `VERSION_COLS` — which drops `props`, `source` and
    `provenance_ref`."""
    window = {"t_a": 0, "t_b": 250}
    env = call_operator(adapter, "version_history",
                        {"kind": "node", "window": window, "limit": 1000})
    fields = ("vid", "uid", "label", "vt_s", "vt_e", "tt_s", "tt_e")
    expected = [{f: r[f] for f in fields} for r in env["rows"]]

    plan = Project(_leaf_order(
        NodeScan("p", sigma_=Sigma.in_window(window["t_a"], window["t_b"])), "p"),
        tuple((f, Col(f"p.{f}")) for f in fields))
    got = evaluate_core(plan, adapter).rows()
    check(f"{backend}: NodeScan(window) reproduces version_history's node rows",
          canonical_json(got) == canonical_json(expected),
          f"{len(got)} vs {len(expected)} rows")

    env = call_operator(adapter, "version_history",
                        {"kind": "edge", "window": window, "limit": 1000})
    fields = ("vid", "eid", "src", "dst", "rel_type", "disc", "vt_s", "vt_e",
              "tt_s", "tt_e")
    expected = [{f: r[f] for f in fields} for r in env["rows"]]
    plan = Project(_leaf_order(
        EdgeScan("e", sigma_=Sigma.in_window(window["t_a"], window["t_b"])), "e"),
        tuple((f, Col(f"e.{f}")) for f in fields))
    got = evaluate_core(plan, adapter).rows()
    check(f"{backend}: EdgeScan(window) reproduces version_history's edge rows",
          canonical_json(got) == canonical_json(expected),
          f"{len(got)} vs {len(expected)} rows")

    # ... and the *unordered* scan carries the same rows, so the only
    # difference between the leaf and its §6 #2 compilation is the sort key
    bare = Project(EdgeScan("e", sigma_=Sigma.in_window(window["t_a"],
                                                        window["t_b"])),
                   tuple((f, Col(f"e.{f}")) for f in fields))
    check(f"{backend}: and the same row *set* without the explicit Order",
          canonical_json(sorted(evaluate_core(bare, adapter).rows(),
                                key=lambda r: r["vid"]))
          == canonical_json(sorted(expected, key=lambda r: r["vid"])))


def _leaf_order(scan: Any, var: str) -> Any:
    """`version_history` orders by **`(tt_s, vid)`**, always
    (`ops_versions.py:151`) — while §2.2 declares the core scan's canonical
    order as `(vt_s, vid)` under `belief = current`. §6 #2's compilation
    ("a scan under the window, projected to `VERSION_COLS`") is therefore
    incomplete: reproducing the leaf row for row needs an explicit `Order`,
    and result ordering is part of the answer (`eval_semantics.md` §4).

    Recorded here rather than worked around silently, because it constrains
    M3.3's compiled `version_history`: `tt_s` is not on the columnar route, so
    the ordered form also forces the `versions_columnar` fallback.
    """
    return Order(scan, (SortKey(Col(f"{var}.tt_s")), SortKey(Col(f"{var}.vid"))))


def check_filtered_scan(adapter: Any, backend: str) -> None:
    """The same comparison with a `Filter` in between, so the selection is in
    the equivalence too rather than only the scan."""
    env = call_operator(adapter, "version_history",
                        {"kind": "node", "window": {"t_a": 0, "t_b": 250},
                         "limit": 1000})
    expected = [{"uid": r["uid"], "vt_s": r["vt_s"]}
                for r in env["rows"] if r["label"] == "N"]
    plan = Project(
        _leaf_order(Filter(NodeScan("p", sigma_=Sigma.in_window(0, 250)),
                           Cmp("=", Col("p.label"), Lit("N"))), "p"),
        (("uid", Col("p.uid")), ("vt_s", Col("p.vt_s"))))
    got = evaluate_core(plan, adapter).rows()
    check(f"{backend}: scan+filter+project matches the operator's filtered rows",
          canonical_json(got) == canonical_json(expected),
          f"{len(got)} vs {len(expected)} rows")


# ---------------------------------------------------------------------------
# (2) Join{inner} versus compute join
# ---------------------------------------------------------------------------

def check_join(adapter: Any, backend: str) -> None:
    """`compute join` requires unique keys on both sides, so the comparison is
    over a shape both can express. Bag multiplication is `Join`'s alone and is
    tested in `tests/test_tgir_eval_core.py`, not here."""
    left_rows = [{"k": f"u{i}", "w": i} for i in range(1, 7)]
    right_rows = [{"k": f"u{i}", "lab": "N" if i % 2 else "M"}
                  for i in range(1, 5)]
    env = call_operator(adapter, "compute",
                        {"fn": "join", "input": left_rows, "other": right_rows,
                         "on": "k", "how": "inner"})
    expected = env["rows"]

    plan = Join(
        Project(NodeScan("p", sigma_=Sigma.default()),
                (("k", Col("p.uid")), ("w", Col("p.vt_e")))),
        Project(NodeScan("q", sigma_=Sigma.default()),
                (("k2", Col("q.uid")), ("lab", Col("q.label")))),
        (("k", "k2"),))
    got = evaluate_core(plan, adapter)
    check(f"{backend}: Join{{inner}} emits left-then-right column order",
          got.schema.names == ("k", "w", "k2", "lab"))

    # An independent nested-loop join over the *same evaluated inputs*: it
    # reproduces §2.8's order (left position, then right position) and its bag
    # semantics (multiplicities multiply) without sharing a line of code with
    # the hash join. Quadratic, and the inputs are tiny.
    left_rel = evaluate_core(plan.left, adapter).rows()
    right_rel = evaluate_core(plan.right, adapter).rows()
    reference = [{**left_row, **right_row}
                 for left_row in left_rel
                 for right_row in right_rel
                 if left_row["k"] == right_row["k2"]]
    check(f"{backend}: Join{{inner}} agrees with an independent nested-loop join",
          canonical_json(got.rows()) == canonical_json(reference),
          f"{len(got.rows())} vs {len(reference)} rows")

    # ... and against the existing operator, on the shape `compute join` can
    # express: unique keys on both sides.
    #
    # `compute join` iterates `sorted(left)` (`ops_compute.py:498`), so its
    # output is **key-ordered**, while §2.8 orders by **left row position** and
    # adds no prefix. The two agree byte for byte once the plan *says* what
    # `compute join` bakes in — an explicit `Order` on the join key — which is
    # the honest form of the comparison: same rows, same order, one of them
    # ordered by the algebra rather than by the kernel.
    left_plan = Order(
        Project(NodeScan("p", uids=tuple(f"u{i}" for i in range(2, 7)),
                         sigma_=Sigma.at_instant(0)),
                (("k", Col("p.uid")), ("w", Col("p.vt_e")))),
        (SortKey(Col("k")),))
    right_plan = Project(NodeScan("q", uids=tuple(f"u{i}" for i in range(2, 5)),
                                  sigma_=Sigma.at_instant(0)),
                         (("k2", Col("q.uid")), ("lab", Col("q.label"))))
    unique = Join(left_plan, right_plan, (("k", "k2"),))
    core_rows = evaluate_core(unique, adapter).rows()
    left_side = evaluate_core(left_plan, adapter).rows()
    right_side = evaluate_core(right_plan, adapter).rows()
    env2 = call_operator(adapter, "compute",
                         {"fn": "join", "input": left_side, "other": right_side,
                          "on": "k", "other_on": "k2", "how": "inner"})
    # `other_prefix` defaults to "r_": the operator renames the right side,
    # the algebra does not (§4.2 makes a collision a static plan error instead)
    operator_rows = [{"k": r["k"], "w": r["w"], "k2": r["r_k2"], "lab": r["r_lab"]}
                     for r in env2["rows"]]
    check(f"{backend}: Join{{inner}} is byte-identical to `compute join` on unique keys",
          canonical_json(core_rows) == canonical_json(operator_rows),
          f"core {len(core_rows)} rows, operator {len(operator_rows)} rows")
    check(f"{backend}: and the comparison ran over a non-trivial join",
          len(left_side) == 5 and len(right_side) == 3 and len(core_rows) == 3,
          f"{len(left_side)} × {len(right_side)} → {len(core_rows)}")
    _ = expected


def check_pruning_is_invisible(adapter: Any, backend: str) -> None:
    """Column pruning changes which arrays are built and nothing else — the
    property that makes it not a plan rewrite (§3.7)."""
    from tgms.tgir.eval import Execution

    plan = Project(Filter(NodeScan("p", sigma_=Sigma.default()),
                          Cmp("=", Col("p.label"), Lit("N"))),
                   (("uid", Col("p.uid")),))
    pruned = evaluate_core(plan, adapter).rows()
    unpruned = Execution(adapter, None).run(plan).rows()
    check(f"{backend}: pruned and unpruned executions agree row for row",
          canonical_json(pruned) == canonical_json(unpruned))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=BACKENDS + ("both",), default="both")
    args = ap.parse_args()
    ensure_all_registered()
    for backend in (BACKENDS if args.backend == "both" else (args.backend,)):
        adapter = build(backend)
        try:
            check_entity_history(adapter, backend)
            check_version_history(adapter, backend)
            check_filtered_scan(adapter, backend)
            check_join(adapter, backend)
            check_pruning_is_invisible(adapter, backend)
        finally:
            adapter.close()
    if FAILURES:
        print(f"\n{len(FAILURES)} equivalence check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nthe core evaluators reproduce the operators they compile from")
    return 0


if __name__ == "__main__":
    sys.exit(main())
