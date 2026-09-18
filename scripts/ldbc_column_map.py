"""Generate the per-template **column correspondence** `ldbc_compare.py` uses.

`ldbc_compare.py`'s docstring says it compares "both sides by column *kind*,
never by column name", but `rows_match` looks columns up by name on *both*
sides. The reference side's names are the vendored Cypher's own `RETURN … AS`
aliases; the TGIR side's are the plan artifact's `Project` binding names. For
10 of the 24 templates they differ (`forum.id` vs `forumId`, `relatedTag.name`
vs `relatedTagName`, …), so every lookup on the reference row returned `None`
and every row faulted — while the values were identical.

Derived, never hand-written, from two frozen inputs (the discipline
`scripts/ldbc_sort_keys.py` already follows for `sort_keys.yaml`):

  * the plan artifact's `Project` binding names, **in order**;
  * the vendored `.cypher`'s final top-level `RETURN` aliases, **in order** —
    read with `ldbc_sort_keys`'s own splitter and alias regex, imported rather
    than re-spelled so the two generators cannot drift apart.

The rule, fixed before looking at any result:

  **A. by name.** If every reference column has a same-named TGIR column (and
  the counts match), map by name. This is the *only* correct rule when the two
  sides share names in a different order — `IC8` returns
  `…, commentId, commentCreationDate, …` against TGIR's
  `…, commentCreationDate, commentId, …`, where pairing positionally would
  silently compare a timestamp against an id.

  **B. by position.** Otherwise, if both sides have the same column count, pair
  positionally — but only where the two columns' derived *kinds* agree at that
  position. A kind comes from `column_kinds.json` on the TGIR side and from the
  loader's own property table on the reference side (its last dotted segment:
  `forum.creationDate` -> `creationDate` -> `ts`), with any `*.id`/`*Id` name
  being a `uid`. A position whose kinds disagree is **not** paired.

  **Otherwise `unmatched`.** A reference column that neither rule maps is
  recorded, and `ldbc_compare.py` reports the template
  `reference-column-not-projected` rather than agreement — the hole that let
  `IC12`'s `tagNames` and three of `BI4`'s five columns pass unnoticed.

    uv run python scripts/ldbc_column_map.py --out benchmarks/ldbc-ref-v1/column_map.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ldbc_reference_run import _default_cypher_name  # noqa: E402
from ldbc_snb_params import substitute  # noqa: E402
from ldbc_sort_keys import _AS_ALIAS_RE, _collapse_ws, _split_top_level  # noqa: E402

from tgms.data.snb_loader import EDGES, NODES  # noqa: E402
from tgms.tgir.loader import load  # noqa: E402

PLANS_DIR = ROOT / "benchmarks" / "tgir-v1" / "plans"
BI_DIR = ROOT / "external_workloads/ldbc/bi/neo4j/queries"
IV_DIR = ROOT / "external_workloads/ldbc/interactive_v1/cypher/queries"

TEMPLATES = ["BI3", "BI4", "BI6", "BI7", "BI9", "BI10", "BI11", "BI12",
             "BI17", "BI18", "IC2", "IC5", "IC6", "IC8", "IC9", "IC11",
             "IC12", "IS1", "IS2", "IS3", "IS4", "IS5", "IS6", "IS7"]
#: RUNBOOK §6: the BI6 template's row is answered by the repaired artifact.
PLAN_ARTIFACT = dict({t: t for t in TEMPLATES}, **{"BI6": "BI6.v2"})

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"//[^\n]*")
_RETURN = re.compile(r"\bRETURN\b", re.IGNORECASE)
_TEMPORAL = {"ts", "date"}


def _property_kinds() -> dict[str, str]:
    seen: dict[str, set[str]] = {}
    for spec in NODES:
        for name, (_c, kind) in spec.props.items():
            seen.setdefault(name, set()).add(kind)
    for spec in EDGES:
        for name, (_c, kind) in spec.props.items():
            seen.setdefault(name, set()).add(kind)
        seen.setdefault("creationDate", set()).add("ts")
    return {n: (k.pop() if len(k) == 1 else "ambiguous") for n, k in seen.items()}


def reference_columns(cypher_path: Path) -> list[str]:
    """The final top-level `RETURN`'s output names, in order.

    Comments are stripped first: a `//` line or a `/* :params … */` header can
    contain the word RETURN, and LDBC's files carry both.
    """
    text = _COMMENT_LINE.sub("", _COMMENT_BLOCK.sub("", cypher_path.read_text()))
    matches = list(_RETURN.finditer(text))
    if not matches:
        return []
    tail = text[matches[-1].end():]
    cut = re.search(r"\b(ORDER\s+BY|LIMIT|SKIP)\b", tail, re.IGNORECASE)
    if cut:
        tail = tail[:cut.start()]
    out: list[str] = []
    for col in _split_top_level(tail):
        col = _collapse_ws(col).strip()
        if not col:
            continue
        m = _AS_ALIAS_RE.match(col)
        out.append(m.group("alias") if m else col)
    return out


def tgir_columns(plan_path: Path) -> list[tuple[str, str]]:
    """The plan's own declared output schema, in order, as `(name, tau)`.

    Read from `tgms.tgir`'s `Plan.out_schema` — the planner's own static
    derivation, the same one the runner's `--emit-rows` envelope reports —
    rather than by walking for a `Project`. Walking is wrong: a plan may carry
    several `Project`s and its output columns may be named by an aggregate
    instead (BI3's `messageCount`, and IC6/IC12 have no output `Project` at
    all). Needs no store and no engine: the artifact is substituted with its
    **own frozen `params`**, so this depends on no run output. Column names do
    not depend on parameter values.
    """
    doc = json.loads(plan_path.read_text())
    body = substitute({"root": doc["root"], "sigma": doc.get("sigma")},
                      doc.get("params", {}))
    plan = load({"plan_format": doc["plan_format"],
                 "plan_id": doc.get("plan_id", plan_path.stem), **body})
    return [(c.name, str(c.tau)) for c in plan.out_schema]


def ref_kind(name: str, prop_kind: dict[str, str]) -> str:
    """A reference column's kind, from its own name.

    Matched on the camelCase *suffix* as well as the whole leaf, because the
    vendored queries alias a property onto a longer name
    (`postOrCommentCreationDate`, `friendshipCreationDate` — both the loader's
    `creationDate`).
    """
    leaf = name.rsplit(".", 1)[-1]
    temporal = [p for p, k in prop_kind.items() if k in _TEMPORAL]
    low = leaf.lower()
    if any(low == p.lower() or low.endswith(p.lower()) for p in temporal):
        return "ts"
    if leaf == "id" or (leaf.endswith("Id") and leaf != "Id"):
        return "uid"
    return "scalar"


def tgir_kind(name: str, tau: str, kinds: dict[str, str]) -> str:
    """A TGIR column's kind: its own declared tau first, then
    `column_kinds.json`'s temporal overlay.

    Both are needed. A column reading a *version's* clock is already typed
    `ts` by the planner (IS3's `friendshipCreationDate` binds `r.vt_s`, the
    KNOWS edge's valid-time start), but a column reading a temporal
    **property** is typed `json?` whatever it reads (IS1's `creationDate`),
    and only the overlay knows it is a clock.
    """
    base = tau.rstrip("?")
    if base == "uid":
        return "uid"
    if base == "ts" or kinds.get(name) == "ts":
        return "ts"
    return "scalar"


def build(template: str, kinds_table: dict, prop_kind: dict[str, str]) -> dict:
    pid = PLAN_ARTIFACT[template]
    plan = PLANS_DIR / f"{pid}.json"
    cy = (BI_DIR if template.startswith("BI") else IV_DIR) / _default_cypher_name(template)
    tschema = tgir_columns(plan)
    tcols = [n for n, _ in tschema]
    rcols = reference_columns(cy)
    kinds = kinds_table.get(pid, {})

    rec: dict = {"plan_artifact": f"{pid}.json", "cypher": cy.name,
                 "tgir_columns": tcols, "reference_columns": rcols}

    by_name = {r: r for r in rcols if r in tcols}
    if len(rcols) == len(tcols) and len(by_name) == len(rcols):
        rec["rule"] = "name"
        rec["map"] = by_name
        rec["unmatched"] = []
        return rec

    if len(rcols) == len(tcols):
        mapped, unmatched = {}, []
        for r, (t, tau) in zip(rcols, tschema):
            if ref_kind(r, prop_kind) == tgir_kind(t, tau, kinds):
                mapped[r] = t
            else:
                unmatched.append(r)
        rec["rule"] = "positional"
        rec["map"] = mapped
        rec["unmatched"] = unmatched
        return rec

    rec["rule"] = "name-partial"
    rec["map"] = by_name
    rec["unmatched"] = [r for r in rcols if r not in by_name]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--column-kinds",
                    default="benchmarks/ldbc-ref-v1/column_kinds.json")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    kinds_table = json.loads(Path(args.column_kinds).read_text())["plans"]
    prop_kind = _property_kinds()
    plans = {t: build(t, kinds_table, prop_kind) for t in TEMPLATES}

    lines = [
        "# GENERATED by scripts/ldbc_column_map.py -- do not hand-edit.",
        "#",
        "# reference column -> TGIR column, per template. Rule A (name) where",
        "# every reference column has a same-named TGIR column; rule B",
        "# (positional, kind-checked) otherwise when the counts match; anything",
        "# neither rule maps is `unmatched` and ldbc_compare.py reports the",
        "# template `reference-column-not-projected`.",
        "#",
        "# Inputs, both frozen: the plan artifacts' Project binding names in",
        "# order, and the vendored .cypher files' final RETURN aliases in order.",
        "_provenance:",
        "  generated_by: scripts/ldbc_column_map.py",
        "  inputs:",
        "    - benchmarks/tgir-v1/plans/*.json (Project binding names, in order)",
        "    - external_workloads/ldbc/**/*.cypher (final RETURN aliases, in order)",
        "    - benchmarks/ldbc-ref-v1/column_kinds.json (TGIR-side kinds)",
        "    - tgms/data/snb_loader.py (reference-side property kinds)",
        "plans:",
    ]
    for t in TEMPLATES:
        p = plans[t]
        lines.append(f"  {t}:")
        lines.append(f"    plan_artifact: {p['plan_artifact']}")
        lines.append(f"    cypher: {p['cypher']}")
        lines.append(f"    rule: {p['rule']}")
        lines.append(f"    tgir_columns: [{', '.join(p['tgir_columns'])}]")
        lines.append(f"    reference_columns: [{', '.join(p['reference_columns'])}]")
        lines.append("    map:")
        for r, tg in p["map"].items():
            lines.append(f"      {r}: {tg}")
        if not p["map"]:
            lines[-1] = "    map: {}"
        lines.append(f"    unmatched: [{', '.join(p['unmatched'])}]")
    Path(args.out).write_text("\n".join(lines) + "\n")

    print(f"wrote {args.out}")
    for t in TEMPLATES:
        p = plans[t]
        flag = "  <-- reference-column-not-projected" if p["unmatched"] else ""
        print(f"  {t:6s} rule={p['rule']:<13s} mapped={len(p['map']):2d}/"
              f"{len(p['reference_columns']):2d} unmatched={p['unmatched']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
