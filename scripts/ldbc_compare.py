"""Compare a TGMS-side row export against a Neo4j reference-side row export
(LDBC reference correctness, design `LDBC_REFERENCE_CORRECTNESS_DESIGN_2026-09-13.md`).

**This is not an LDBC Benchmark result and produces no LDBC Benchmark Result.**

Inputs are two directories of canonical row files in the shape
`scripts/tgir_ldbc_sf1.py --emit-rows` and `scripts/ldbc_reference_run.py`
write: `{"plan_id", "schema"?, "columns", "rows", "result_digest", ...}`. The
TGMS side's `schema` (`[[name, tau], ...]`, from the TGIR relation's own
declared type) is the authority for how each column normalizes — the
reference side is expected to carry the same column names, typed however its
driver handed them back, and is normalized to match.

Normalization (design §4, and the M-rule table in §1), applied identically to
both sides by column *kind*, never by column name:

* `uid`   — the TGMS side already carries a plain LDBC id (the exporter's
  job); the reference side is already LDBC-native. Both sides: `int`.
* `ts`    — TGMS: already integer microseconds. Reference: integer epoch
  **milliseconds**, `* 1000`. (The silent factor-1,000 §3 warns about.)
* `str`   — NFC-normalized, untrimmed, compared as code points.
* `float` — equal iff `|a-b| <= 1e-9 * max(1, |a|, |b|)`.
* everything else (`int`, `bool`, `label`, `rel_type`, `json`) — exact.

**Set semantics is forbidden** (§4): this is multiset matching (a greedy
1-1 pairing, tolerant only in the float columns), not `set(rows_a) ==
set(rows_b)| — `duplicate_safe` is `None` on some of the 24, and a
bag-to-set collapse would hide exactly the duplication defects this exists
to catch.

Per-template contract (`tests/fixtures/ldbc_ref/contracts.json`,
`claim_full_contract`):

* `CURRENT_ECQR_FRAGMENT`   — unordered multiset equality.
* `REQUIRES_ORDERED_RESULT` — compared as a sequence; a positional mismatch
  is `ORDERING/TIE` if the multisets agree, else `UNTRIAGED`.
* `REQUIRES_TOP_K`          — the top-k **set**; a residual mismatch confined
  to the last (worst) row on each side, whose declared sort keys tie, is
  `TIE-AMBIGUOUS` rather than agreement or disagreement (§4's tie rule). Sort
  keys are supplied by the caller (`sort_keys=` on `compare_plan`, or a
  `"sort_keys"` entry on the contract row) — this repository does not vendor
  the `.cypher` files' `ORDER BY` clauses, so a plan with no known sort keys
  cannot have its boundary tie detected and any residual is reported
  `UNTRIAGED` rather than guessed at. **Known limitation, named rather than
  silently worked around** — see the D1 report.

Never a single ratio (§4): every plan's verdict carries
`attempted / bound / both_sides_completed / compared / agreeing /
tie_ambiguous / disagreeing`, plus one `{cause, detail}` per disagreeing row
from the closed set in `DISAGREEMENT_CAUSES`.

    uv run python scripts/ldbc_compare.py --tgms-dir <dir> --ref-dir <dir> \\
        --contracts tests/fixtures/ldbc_ref/contracts.json --out <path>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: §4's closed set. A disagreement outside it is `UNTRIAGED`, reported as such
#: rather than forced into one of these five.
DISAGREEMENT_CAUSES = (
    "MAPPING-RULE", "ORDERING/TIE", "TGIR-SEMANTIC-GAP", "ENGINE-DEFECT",
    "REFERENCE-SIDE-QUIRK",
)

FLOAT_REL_TOLERANCE = 1e-9


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def load_contracts(path: Path) -> dict[str, dict[str, Any]]:
    """`tests/fixtures/ldbc_ref/contracts.json` -> `{query_id: row}`."""
    doc = json.loads(Path(path).read_text())
    return doc["rows"] if "rows" in doc else doc


def load_sort_keys(path: Path) -> dict[str, list[str]]:
    """`benchmarks/ldbc-ref-v1/sort_keys.yaml` (`scripts/ldbc_sort_keys.py`'s
    output) -> `{plan_id: [column, ...]}`, in `ORDER BY` order, for
    `compare_topk`'s tie-break rule. Only the column names travel through —
    direction and `limit` are the generator's own documentation of the
    query, not inputs `compare_topk` needs (the tie rule compares boundary
    *values*, not sort order)."""
    import yaml  # noqa: PLC0415 — only this loader needs it

    doc = yaml.safe_load(Path(path).read_text())
    return {pid: [k["column"] for k in row.get("order_by", [])]
            for pid, row in doc.get("templates", {}).items()}


def lookup_column_map(column_map: dict[str, Any] | None,
                      plan_id: str) -> dict[str, Any] | None:
    """`column_map.yaml` is keyed by the 24 LDBC template ids (`BI6`, not
    `BI6.v2`): the correspondence is a property of the query's RETURN and the
    artifact answering it, and `BI6.v2` answers `BI6`."""
    if not column_map:
        return None
    if plan_id in column_map:
        return column_map[plan_id]
    return column_map.get(plan_id.rsplit(".", 1)[0])


def lookup_sort_keys(sort_keys: dict[str, list[str]] | None,
                     plan_id: str) -> list[str] | None:
    """`sort_keys.yaml` is keyed by the 24 LDBC template ids (`BI6`, not
    `BI6.v2`) — the sort key is a property of the *vendored query*, which is
    the same one either plan artifact answers (RUNBOOK.md §6). A `--plan`
    invocation naming `BI6.v2` still finds `BI6`'s row by stripping a
    trailing `.v2` before falling back to "no sort keys known"."""
    if not sort_keys:
        return None
    if plan_id in sort_keys:
        return sort_keys[plan_id]
    base = plan_id.removesuffix(".v2")
    return sort_keys.get(base)


# --------------------------------------------------------------------------
# normalization (§4)
# --------------------------------------------------------------------------

def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def floats_equal(a: float, b: float, rel: float = FLOAT_REL_TOLERANCE) -> bool:
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def _base_tau(tau: str) -> str:
    return tau.rstrip("?")


def normalize_value(value: Any, tau: str, side: str) -> Any:
    """One value, one column kind, one side. `side` is `"tgms"` or `"ref"` —
    the only place the two sides are treated differently (the unit rule)."""
    if value is None:
        return None
    base = _base_tau(tau)
    if base == "uid":
        return int(value)
    if base == "ts":
        # TGMS: already integer microseconds (the store's native unit).
        # Reference: integer epoch milliseconds, `ldbc_reference_run.py`'s
        # own canonical form for a Neo4j temporal value -- the µs/ms rule
        # from §1's normalization table, applied here rather than at capture
        # time so the rule is visible and tested in one place.
        return int(value) if side == "tgms" else int(value) * 1000
    if base == "str":
        return nfc(value) if isinstance(value, str) else value
    if base == "float":
        return float(value)
    return value


def values_equal(tgms_value: Any, ref_value: Any, tau: str) -> bool:
    """Two *raw, un-normalized* values, one from each side, for one column of
    the given kind. Normalizes each side by its own rule (`normalize_value`)
    before comparing — the unit rule is side-dependent, so this must not be
    handed two already-normalized values and asked to just diff them."""
    a = normalize_value(tgms_value, tau, "tgms")
    b = normalize_value(ref_value, tau, "ref")
    if a is None or b is None:
        return a is None and b is None
    if _base_tau(tau) == "float":
        return floats_equal(a, b)
    return a == b


def normalize_row(row: dict[str, Any], schema: list[list[str]],
                  side: str) -> dict[str, Any]:
    return {name: normalize_value(row.get(name), tau, side)
            for name, tau in schema}


def rows_match(tgms_row: dict[str, Any], ref_row: dict[str, Any],
               schema: list[list[str]]) -> bool:
    """`tgms_row` and `ref_row` are raw (un-normalized); every caller in this
    module passes them in that fixed order, which is what lets `values_equal`
    apply the right side's unit rule to each."""
    return all(values_equal(tgms_row.get(name), ref_row.get(name), tau)
              for name, tau in schema)


# --------------------------------------------------------------------------
# multiset / ordered / top-k matching
# --------------------------------------------------------------------------

def multiset_match(tgms_rows: list[dict[str, Any]],
                   ref_rows: list[dict[str, Any]],
                   schema: list[list[str]]
                   ) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedy 1-1 pairing (float columns tolerant, everything else exact).
    Returns `(agreeing, unmatched_tgms, unmatched_ref)`."""
    remaining_ref = list(ref_rows)
    unmatched_tgms: list[dict[str, Any]] = []
    agreeing = 0
    for tr in tgms_rows:
        match_at = next((i for i, rr in enumerate(remaining_ref)
                         if rows_match(tr, rr, schema)), None)
        if match_at is None:
            unmatched_tgms.append(tr)
        else:
            agreeing += 1
            remaining_ref.pop(match_at)
    return agreeing, unmatched_tgms, remaining_ref


def sort_key_tuple(row: dict[str, Any], sort_keys: list[str],
                   schema_by_name: dict[str, str], side: str) -> tuple[Any, ...]:
    return tuple(normalize_value(row.get(k), schema_by_name.get(k, "str"), side)
                for k in sort_keys)


def compare_topk(tgms_rows: list[dict[str, Any]], ref_rows: list[dict[str, Any]],
                 schema: list[list[str]], sort_keys: list[str] | None
                 ) -> dict[str, Any]:
    """The top-k **set** (§4): agree/disagree/tie-ambiguous.

    Tie rule: if every row that fails to match sits exactly at the worst
    (last) sort-key value on *both* sides — i.e. the disagreement is confined
    to whichever row each side's tiebreak happened to keep at the k-th slot —
    the row is `TIE-AMBIGUOUS`, not agreement and not disagreement. Without
    `sort_keys` (the vendored `.cypher`'s `ORDER BY`, not carried in this
    repository — see the module docstring) the boundary cannot be identified,
    so any residual is left `UNTRIAGED`.
    """
    agreeing, only_tgms, only_ref = multiset_match(tgms_rows, ref_rows, schema)
    if not only_tgms and not only_ref:
        return {"verdict": "agreeing", "agreeing": agreeing,
                "tie_ambiguous": 0, "disagreeing": 0, "causes": []}
    if sort_keys and tgms_rows and ref_rows:
        by_tau = {name: tau for name, tau in schema}
        boundary_t = sort_key_tuple(tgms_rows[-1], sort_keys, by_tau, "tgms")
        boundary_r = sort_key_tuple(ref_rows[-1], sort_keys, by_tau, "ref")
        rest_t = {json.dumps(normalize_row(r, schema, "tgms"), sort_keys=True,
                             default=str) for r in tgms_rows[:-1]}
        rest_r = {json.dumps(normalize_row(r, schema, "ref"), sort_keys=True,
                             default=str) for r in ref_rows[:-1]}
        if (boundary_t == boundary_r and rest_t == rest_r
                and len(only_tgms) <= 1 and len(only_ref) <= 1):
            return {"verdict": "tie-ambiguous", "agreeing": agreeing,
                    "tie_ambiguous": len(only_tgms) + len(only_ref),
                    "disagreeing": 0, "causes": []}
    causes = [{"cause": "UNTRIAGED",
              "detail": f"tgms-only row: {r}"} for r in only_tgms]
    causes += [{"cause": "UNTRIAGED",
               "detail": f"reference-only row: {r}"} for r in only_ref]
    return {"verdict": "disagreeing", "agreeing": agreeing, "tie_ambiguous": 0,
            "disagreeing": len(only_tgms) + len(only_ref), "causes": causes}


def compare_unordered(tgms_rows: list[dict[str, Any]],
                      ref_rows: list[dict[str, Any]],
                      schema: list[list[str]]) -> dict[str, Any]:
    """`CURRENT_ECQR_FRAGMENT`: exact multiset equality, no top-k allowance."""
    agreeing, only_tgms, only_ref = multiset_match(tgms_rows, ref_rows, schema)
    causes = [{"cause": "UNTRIAGED",
              "detail": f"tgms-only row: {r}"} for r in only_tgms]
    causes += [{"cause": "UNTRIAGED",
               "detail": f"reference-only row: {r}"} for r in only_ref]
    disagreeing = len(only_tgms) + len(only_ref)
    return {"verdict": "agreeing" if disagreeing == 0 else "disagreeing",
            "agreeing": agreeing, "tie_ambiguous": 0,
            "disagreeing": disagreeing, "causes": causes}


def compare_ordered(tgms_rows: list[dict[str, Any]],
                    ref_rows: list[dict[str, Any]],
                    schema: list[list[str]]) -> dict[str, Any]:
    """`REQUIRES_ORDERED_RESULT`: row order is part of the answer. A
    positional mismatch where the *multisets* still agree is `ORDERING/TIE`;
    otherwise the row pair is `UNTRIAGED` (it is not this module's job to
    guess whether a differing row is a mapping defect or an engine defect
    without more context than a row diff gives)."""
    _, only_tgms, only_ref = multiset_match(tgms_rows, ref_rows, schema)
    multisets_agree = not only_tgms and not only_ref
    agreeing = 0
    causes: list[dict[str, Any]] = []
    n = max(len(tgms_rows), len(ref_rows))
    for i in range(n):
        t = tgms_rows[i] if i < len(tgms_rows) else None
        r = ref_rows[i] if i < len(ref_rows) else None
        if t is not None and r is not None and rows_match(t, r, schema):
            agreeing += 1
            continue
        cause = "ORDERING/TIE" if multisets_agree else "UNTRIAGED"
        causes.append({"cause": cause,
                       "detail": f"row {i}: tgms={t!r} ref={r!r}"})
    return {"verdict": "agreeing" if not causes else "disagreeing",
            "agreeing": agreeing, "tie_ambiguous": 0,
            "disagreeing": len(causes), "causes": causes}


# --------------------------------------------------------------------------
# per-plan verdict
# --------------------------------------------------------------------------

def compare_plan(plan_id: str, tgms_doc: dict[str, Any] | None,
                 ref_doc: dict[str, Any] | None,
                 contract_row: dict[str, Any] | None,
                 sort_keys: list[str] | None = None,
                 column_kinds: dict[str, str] | None = None,
                 column_map: dict[str, Any] | None = None) -> dict[str, Any]:
    """One template's verdict record — never collapsed into a ratio."""
    rec: dict[str, Any] = {
        "plan_id": plan_id,
        "contract": (contract_row or {}).get("claim_full_contract"),
        "attempted": tgms_doc is not None,
        "bound": bool(tgms_doc and tgms_doc.get("params")),
        "both_sides_completed": bool(tgms_doc and ref_doc
                                     and tgms_doc.get("rows") is not None
                                     and ref_doc.get("rows") is not None),
        "compared": 0, "agreeing": 0, "tie_ambiguous": 0, "disagreeing": 0,
        "causes": [],
        "tgms_digest": (tgms_doc or {}).get("result_digest"),
        "ref_digest": (ref_doc or {}).get("result_digest"),
        "note": None,
    }
    if not rec["both_sides_completed"]:
        rec["note"] = ("no comparison: " +
                       ("missing TGMS-side export" if tgms_doc is None else
                        "missing reference-side export" if ref_doc is None else
                        "one side has no rows"))
        return rec

    schema = tgms_doc.get("schema")
    if not schema:
        # the reference-side runner and this fixture format both carry
        # "columns" without TGIR taus; fall back to comparing every column
        # as an opaque scalar (exact equality) rather than refusing.
        schema = [[c, "str"] for c in tgms_doc.get("columns", [])]
    # `--emit-rows`'s `<name>__hierarchy` columns are decode provenance, not
    # part of the LDBC projection (design §4's "projected columns") — comparing
    # them would fault every row on a column the reference side never has.
    schema = [pair for pair in schema if not pair[0].endswith("__hierarchy")]
    # `scripts/ldbc_column_kinds.py`'s table, derived from the plan artifacts'
    # own projections and the loader's frozen property kinds. `--emit-rows`
    # types a property read as `json?` whatever the property is, so without
    # this the `ts` kind -- the compare-time half of RUNBOOK.md §4.3's µs/ms
    # rule -- never fires and every row carrying a timestamp mismatches.
    if column_kinds:
        schema = [[name, column_kinds.get(name, tau)] for name, tau in schema]
    contract = (contract_row or {}).get("claim_full_contract",
                                        "CURRENT_ECQR_FRAGMENT")
    tgms_rows = tgms_doc["rows"]
    ref_rows = ref_doc["rows"]

    # `scripts/ldbc_column_map.py`'s table. The two sides name their columns
    # differently (the vendored query's RETURN aliases vs the plan's own output
    # schema), and this module used to look both up by the TGIR name, so every
    # reference lookup returned None and every row faulted while the values
    # were identical. Renaming the reference side once, here, leaves every
    # matcher below untouched.
    not_projected: list[str] = []
    if column_map:
        renames = column_map.get("map") or {}
        not_projected = list(column_map.get("unmatched") or [])
        rec["column_map_rule"] = column_map.get("rule")
        if renames:
            ref_rows = [{renames.get(k, k): v for k, v in row.items()}
                        for row in ref_rows]
    rec["reference_columns_not_projected"] = not_projected

    rec["compared"] = max(len(tgms_rows), len(ref_rows))

    if contract == "REQUIRES_ORDERED_RESULT":
        out = compare_ordered(tgms_rows, ref_rows, schema)
    elif contract == "REQUIRES_TOP_K":
        keys = sort_keys if sort_keys is not None else contract_row.get(
            "sort_keys") if contract_row else None
        out = compare_topk(tgms_rows, ref_rows, schema, keys)
    else:
        out = compare_unordered(tgms_rows, ref_rows, schema)

    rec["agreeing"] = out["agreeing"]
    rec["tie_ambiguous"] = out["tie_ambiguous"]
    rec["disagreeing"] = out["disagreeing"]
    rec["causes"] = out["causes"]
    rec["verdict"] = out["verdict"]

    # A reference column that no TGIR column maps to is a fault in its own
    # right, not an absence: the comparator only ever compares the columns the
    # TGIR schema declares, so such a column was previously invisible and the
    # template could be scored `agreeing` on a strict subset of the answer
    # (IC12's `tagNames`, three of BI4's five). Reported as its own class.
    if not_projected:
        rec["verdict"] = "reference-column-not-projected"
        rec["causes"] = list(rec["causes"]) + [{
            "cause": "REFERENCE-COLUMN-NOT-PROJECTED",
            "detail": ("the reference projects " + ", ".join(not_projected) +
                       "; the TGIR plan does not, so the rows above were "
                       "compared on a strict subset of the query's answer"),
        }]
    return rec


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def render_markdown(verdicts: list[dict[str, Any]]) -> str:
    lines = [
        "| plan | contract | attempted | bound | both-sides | compared | "
        "agreeing | tie-ambiguous | disagreeing | causes |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in verdicts:
        causes = ", ".join(sorted({c["cause"] for c in v["causes"]})) or "-"
        lines.append(
            f"| {v['plan_id']} | {v.get('contract') or '-'} | "
            f"{v['attempted']} | {v['bound']} | {v['both_sides_completed']} | "
            f"{v['compared']} | {v['agreeing']} | {v['tie_ambiguous']} | "
            f"{v['disagreeing']} | {causes} |")
    return "\n".join(lines) + "\n"


def compare_all(tgms_dir: Path, ref_dir: Path, contracts: dict[str, Any],
                plan_ids: list[str],
                sort_keys: dict[str, list[str]] | None = None,
                column_kinds: dict[str, dict[str, str]] | None = None,
                column_map: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    import platform
    import time

    verdicts = []
    for pid in plan_ids:
        tgms_path = tgms_dir / f"tgms-{pid}.json"
        ref_path = ref_dir / f"ref-{pid}.json"
        tgms_doc = (json.loads(tgms_path.read_text())
                   if tgms_path.exists() else None)
        ref_doc = json.loads(ref_path.read_text()) if ref_path.exists() else None
        keys = lookup_sort_keys(sort_keys, pid)
        verdicts.append(compare_plan(pid, tgms_doc, ref_doc,
                                     contracts.get(pid), keys,
                                     (column_kinds or {}).get(pid),
                                     lookup_column_map(column_map, pid)))
    return {
        "manifest": {"commit": _sha(), "host": platform.node(),
                    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "tgms_dir": str(tgms_dir), "ref_dir": str(ref_dir),
                    "plans": len(plan_ids),
                    "disagreement_causes": list(DISAGREEMENT_CAUSES)},
        "verdicts": verdicts,
        "markdown": render_markdown(verdicts),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tgms-dir", required=True)
    ap.add_argument("--ref-dir", required=True)
    ap.add_argument("--contracts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", action="append", default=[],
                    help="restrict to these plan ids; default is every id "
                         "the contracts file names")
    ap.add_argument("--sort-keys", default=None,
                    help="path to scripts/ldbc_sort_keys.py's output "
                         "(benchmarks/ldbc-ref-v1/sort_keys.yaml); supplies "
                         "the REQUIRES_TOP_K tie-break columns RUNBOOK.md §8 "
                         "otherwise requires building by hand")
    ap.add_argument("--column-kinds", default=None,
                    help="path to scripts/ldbc_column_kinds.py's output "
                         "(benchmarks/ldbc-ref-v1/column_kinds.json); supplies "
                         "the `ts`/`uid` column kinds the --emit-rows schema "
                         "cannot carry, without which RUNBOOK.md §4.3's µs/ms "
                         "rule never fires")
    ap.add_argument("--column-map", default=None,
                    help="path to scripts/ldbc_column_map.py's output "
                         "(benchmarks/ldbc-ref-v1/column_map.yaml); maps each "
                         "reference column to the TGIR column it corresponds "
                         "to, and names the reference columns no TGIR column "
                         "projects")
    args = ap.parse_args()

    contracts = load_contracts(Path(args.contracts))
    plan_ids = args.plan or sorted(contracts)
    sort_keys = load_sort_keys(Path(args.sort_keys)) if args.sort_keys else None
    column_kinds = None
    if args.column_kinds:
        column_kinds = json.loads(
            Path(args.column_kinds).read_text()).get("plans", {})
    column_map = None
    if args.column_map:
        import yaml  # noqa: PLC0415 — only needed when the flag is given
        column_map = yaml.safe_load(
            Path(args.column_map).read_text()).get("plans", {})
    result = compare_all(Path(args.tgms_dir), Path(args.ref_dir), contracts,
                         plan_ids, sort_keys=sort_keys,
                         column_kinds=column_kinds, column_map=column_map)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({k: v for k, v in result.items()
                               if k != "markdown"},
                              indent=1, sort_keys=True, default=str))
    (out.with_suffix(".md")).write_text(result["markdown"])
    print(result["markdown"])
    print(f"record: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
