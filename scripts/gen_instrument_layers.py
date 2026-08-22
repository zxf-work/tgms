"""Emit the `L19` / `C27` diff-table source, derived from `measured.yaml`.

The addendum (§10.1 (i)) fixes that M3.5's re-run is *one append-only diff table
per instrument*, and that every entry is **derived mechanically** from
`benchmarks/tgir-v1/measured.yaml` rather than authored independently — so the
instruments and the row-level record cannot drift apart. This is that
derivation, and it prints Python source rather than editing in place, so the
diff a reviewer reads is the diff that was reviewed.

Two kinds of entry, both mechanical:

- **class 2** for a row TGIR expresses. Its `need_or_ops` chain is the plan's
  own node sequence, read off the artifact (`measured.yaml::chain`). Never
  class 1: a TGIR compilation is several nodes by construction, and
  `ldbc_fit.py` asserts class 1 is a single operator (addendum §10.1 (iii)).
- **class 3, re-tagged** for a blocked row whose *residual set narrowed*. The
  new tags come from the frozen forecast's own `missing_primitives_if_no`
  through the fixed map below — never from a fresh reading. A row whose tags do
  not change is **left out**, because a diff table records changes and
  `_check_diff` rejects a non-change.

    uv run python scripts/gen_instrument_layers.py [--which L19|C27]
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "benchmarks/tgir-v1/measured.yaml"
FORECAST = ROOT / "docs/design/tgir_b1/forecast.yaml"

#: The frozen forecast's residual families → each instrument's need tag. Fixed
#: here rather than decided per row, so no row gets a bespoke reading.
RESIDUAL_TAG_LDBC = {
    "path-shortest-length": "SP", "path-all-shortest": "SP",
    "path-weighted-shortest": "SP", "path-weight-aggregation": "SP",
    "path-derived-weight": "SP", "path-longest-chain": "SP",
    "path-temporal-ordering": "SP",
    "list-aggregation": "G", "conditional-aggregate": "G",
    "per-group-top-k": "G",
    "arithmetic-over-aggregates": "ROW",
    "calendar-unit-extraction": "CAL",
    "set-ops": "SET",
    "seq-consecutive-pair": "SEQ", "seq-sliding-window-aggregate": "SEQ",
}
RESIDUAL_TAG_INDEPENDENT = {
    **RESIDUAL_TAG_LDBC,
    "path-longest-chain": "CHAIN", "path-temporal-ordering": "CHAIN",
    "path-shortest-length": "CHAIN", "path-all-shortest": "CHAIN",
    "seq-consecutive-pair": "SEQPAIR",
}


def load() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    measured = yaml.safe_load(MEASURED.read_text())["rows"]
    forecast = {r["id"]: r for r in yaml.safe_load(FORECAST.read_text())["rows"]}
    return measured, forecast


def current_ldbc(qid: str) -> tuple[int, str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import ldbc_fit

    return ldbc_fit.verdict(qid)


def current_independent(key: tuple[str, int]) -> tuple[int, str]:
    """Resolve a question through the instrument's own diff chain — so "is this
    a change?" is asked against what the instrument currently says, not against
    the pre-registered table."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import independent_questions as iq

    for name in [f"C{n}" for n in range(26, 13, -1)]:
        table = getattr(iq, name, None)
        if table and key in table:
            return table[key][0], table[key][1]
    return iq.C[key][0], iq.C[key][1]


def residual_tags(row: dict[str, Any], mapping: dict[str, str]) -> str:
    tags = {mapping[r] for r in (row.get("missing_primitives_if_no") or [])
            if r in mapping}
    return ",".join(sorted(tags))


def wrap(text: str, indent: int) -> str:
    pad = " " * indent
    # `break_on_hyphens=False`: a residual name like `path-temporal-ordering`
    # is one token, and splitting it across lines corrupts the justification
    lines = textwrap.wrap(text, width=74 - indent, break_on_hyphens=False)
    return "\n".join(f'{pad}"{line} "' if i < len(lines) - 1 else f'{pad}"{line}"'
                     for i, line in enumerate(lines))


def ldbc_entries() -> str:
    measured, forecast = load()
    out = ["L19: dict[str, tuple[int, str, str]] = {"]
    out.append("    # ---- freed by TGIR-v1: class 3 -> class 2 ---- #")
    for row in measured:
        if not row["suite"].startswith("ldbc") or row["predicted"] == "no":
            continue
        why = (f"TGIR-v1 compiles this row; the chain is the plan's own node "
               f"sequence, read off benchmarks/tgir-v1/plans/{row['id']}.json "
               f"(evidence {row['evidence']}). Class 2 and never 1: a TGIR "
               f"compilation is several nodes by construction.")
        out.append(f'    "{row["id"]}": (2, "{row["chain"]}",')
        out.append(wrap(why, 12) + "),")
    out.append("    # ---- re-audited, still class 3: the residual narrowed ---- #")
    for row in measured:
        if not row["suite"].startswith("ldbc") or row["predicted"] != "no":
            continue
        tags = residual_tags(forecast[row["id"]], RESIDUAL_TAG_LDBC)
        # a diff table records CHANGES: a row whose tags the re-audit leaves
        # untouched is left out, and `_check` rejects a non-change anyway
        if not tags or (3, tags) == current_ldbc(row["id"]):
            continue
        residuals = ", ".join(forecast[row["id"]]["missing_primitives_if_no"])
        why = (f"re-read against TGIR-v1: the pattern, property, negation and "
               f"projection halves ship, and what survives is {residuals} — "
               f"the tags are the frozen forecast's own residual list, not a "
               f"fresh reading.")
        out.append(f'    "{row["id"]}": (3, "{tags}",')
        out.append(wrap(why, 12) + "),")
    out.append("}")
    return "\n".join(out)


def independent_entries() -> str:
    measured, forecast = load()
    out = ["C27: dict[tuple[str, int], tuple[int, str, str]] = {"]
    out.append("    # ---- freed by TGIR-v1: class 3 -> class 2 ---- #")
    for row in measured:
        if row["suite"].startswith("ldbc") or row["predicted"] == "no":
            continue
        suite, number = row["id"][:2], int(row["id"][2:])
        why = (f"TGIR-v1 compiles this question; the chain is the plan's own "
               f"node sequence, read off benchmarks/tgir-v1/plans/"
               f"{row['id']}.json (evidence {row['evidence']}).")
        if row.get("gold", {}).get("matches"):
            why += (f" Runs: the answer equals the double-keyed gold "
                    f"({row['gold']['expected']}).")
        out.append(f'    ("{suite}", {number}): (2, "{row["chain"]}",')
        out.append(wrap(why, 12) + "),")
    out.append("    # ---- re-audited, still class 3: the residual narrowed ---- #")
    for row in measured:
        if row["suite"].startswith("ldbc") or row["predicted"] != "no":
            continue
        suite, number = row["id"][:2], int(row["id"][2:])
        tags = residual_tags(forecast[row["id"]], RESIDUAL_TAG_INDEPENDENT)
        if (3, tags) == current_independent((suite, number)):
            tags = ""          # unchanged: left out, see the LDBC note above
        residuals = ", ".join(forecast[row["id"]]["missing_primitives_if_no"])
        why = (f"re-read against TGIR-v1: its pattern and property halves "
               f"ship, and what survives is {residuals} — the tags are the "
               f"frozen forecast's own residual list.")
        out.append(f'    # {row["id"]}: {tags or "unchanged"} '
                   f'({"emitted" if tags else "left out: not a change"})')
        if tags:
            out.append(f'    ("{suite}", {number}): (3, "{tags}",')
            out.append(wrap(why, 12) + "),")
    out.append("}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", choices=["L19", "C27", "both"], default="both")
    args = ap.parse_args()
    if args.which in ("L19", "both"):
        print(ldbc_entries())
        print()
    if args.which in ("C27", "both"):
        print(independent_entries())
    return 0


if __name__ == "__main__":
    sys.exit(main())
