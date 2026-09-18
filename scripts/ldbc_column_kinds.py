"""Generate the per-plan **column-kind** table `ldbc_compare.py` compares by.

RUNBOOK.md §4.3's mapping table lists the µs/ms unit divergence as acting at
"compare time only": "`ldbc_compare.py`'s `ts` column kind multiplies the
reference side by 1000". That kind exists and is tested — but nothing was
producing it. `tgir_ldbc_sf1.py --emit-rows` writes each dump's `schema` from
the plan artifact's own projection, where a property read carries no TGIR type,
so every temporal column arrives as `json?`. `normalize_value` passes `json?`
through untouched, so a TGMS microsecond integer was compared against a
reference millisecond integer and every row carrying a timestamp mismatched.

Measured (ldbc-ref-v1, 2026-09-17, IS1): the two sides returned the *same*
person — same firstName/lastName/gender/browserUsed/locationIP/cityId — and
disagreed only in `birthday` (383184000000000 vs 383184000000) and
`creationDate` (1273606356004000 vs 1273606356004), each exactly 1000x. The
five templates that agreed outright (BI18, IC6, IC12, IS5, IS6) are precisely
the five projecting no temporal column.

The table is **derived, never hand-written** (the same discipline
`scripts/ldbc_sort_keys.py` follows for `sort_keys.yaml`), from two frozen
inputs that both predate this run:

  * each plan artifact's `Project` bindings — `{"prop": ["x.props", "name"]}`
    names the property a column reads, `{"col": "x.uid"}` names a uid column;
  * `tgms.data.snb_loader`'s `NODES`/`EDGES` property tables, which declare
    each property's kind (`ts`, `date`, `int`, `str`) and are the same
    declaration the loader itself used to build the store.

So a column is `ts` because the loader says the property it reads is a clock,
not because its values happened to differ by 1000 — the inference never
consults a result.

    uv run python scripts/ldbc_column_kinds.py \\
        --out benchmarks/ldbc-ref-v1/column_kinds.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tgms.data.snb_loader import EDGES, NODES  # noqa: E402

PLANS_DIR = ROOT / "benchmarks" / "tgir-v1" / "plans"

#: snb_loader kinds that name a clock. `ts` is a microsecond instant and
#: `date` a midnight-aligned one; both are stored in microseconds and both
#: arrive from the reference side in epoch milliseconds, so both take the
#: same compare-time rule.
TEMPORAL_KINDS = {"ts", "date"}


def property_kinds() -> dict[str, str]:
    """property name -> snb_loader kind, over every node and edge spec.

    A name is only reported when every spec that declares it agrees, so a
    collision can never be silently resolved in one direction.
    """
    seen: dict[str, set[str]] = {}
    for spec in NODES:
        for name, (_col, kind) in spec.props.items():
            seen.setdefault(name, set()).add(kind)
    for spec in EDGES:
        for name, (_col, kind) in spec.props.items():
            seen.setdefault(name, set()).add(kind)
        # every standalone edge file carries its own creationDate (Edge's
        # docstring); it is not in `props`, it is the edge's valid time.
        seen.setdefault("creationDate", set()).add("ts")
    out = {}
    for name, kinds in seen.items():
        if len(kinds) == 1:
            out[name] = kinds.pop()
        else:
            out[name] = "AMBIGUOUS:" + "/".join(sorted(kinds))
    return out


def _projections(node: Any) -> list[list[Any]]:
    """The bindings of the plan's own `Project`, or [] if it has none."""
    if isinstance(node, dict):
        if node.get("op") == "Project":
            return node.get("bindings") or []
        for value in node.values():
            got = _projections(value)
            if got:
                return got
    elif isinstance(node, list):
        for value in node:
            got = _projections(value)
            if got:
                return got
    return []


def kinds_for_plan(plan_path: Path, prop_kind: dict[str, str]) -> dict[str, str]:
    doc = json.loads(plan_path.read_text())
    out: dict[str, str] = {}
    for binding in _projections(doc):
        if not (isinstance(binding, list) and len(binding) == 2):
            continue
        name, expr = binding
        if not isinstance(expr, dict):
            continue
        if "col" in expr and str(expr["col"]).endswith(".uid"):
            out[name] = "uid"
        elif "prop" in expr:
            prop = expr["prop"]
            if isinstance(prop, list) and len(prop) == 2:
                kind = prop_kind.get(prop[1], "")
                if kind in TEMPORAL_KINDS:
                    out[name] = "ts"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plans-dir", default=str(PLANS_DIR))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    prop_kind = property_kinds()
    ambiguous = {k: v for k, v in prop_kind.items() if v.startswith("AMBIGUOUS")}
    if ambiguous:
        print(f"WARNING ambiguous property kinds, left un-typed: {ambiguous}")

    table: dict[str, dict[str, str]] = {}
    for path in sorted(Path(args.plans_dir).glob("*.json")):
        kinds = kinds_for_plan(path, prop_kind)
        if kinds:
            table[path.stem] = kinds

    doc = {
        "_provenance": {
            "generated_by": "scripts/ldbc_column_kinds.py",
            "from": ["benchmarks/tgir-v1/plans/*.json (Project bindings)",
                     "tgms/data/snb_loader.py (NODES/EDGES property kinds)"],
            "rule": "a column is `ts` iff the property it projects is declared "
                    "`ts` or `date` by the loader; a column binding `<var>.uid` "
                    "is `uid`. Never inferred from any result value.",
            "why": "RUNBOOK.md §4.3: the µs/ms divergence acts at compare time, "
                   "via ldbc_compare.py's `ts` kind. The --emit-rows schema "
                   "types property reads as `json?`, so that kind never fired.",
        },
        "plans": table,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")

    n_ts = sum(1 for k in table.values() for v in k.values() if v == "ts")
    print(f"wrote {args.out}: {len(table)} plans, {n_ts} temporal columns")
    for pid in sorted(table):
        ts = sorted(c for c, k in table[pid].items() if k == "ts")
        if ts:
            print(f"  {pid:8s} ts: {', '.join(ts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
