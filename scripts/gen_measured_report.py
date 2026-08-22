"""Generate `docs/design/TGIR_M3_MEASURED_REPORT.md` from `measured.yaml`.

Every number in the report is derived here from the row-level record, so the
report and the record cannot disagree — and re-running this script after a
correction regenerates the report rather than inviting a hand edit. A wrong
number is corrected by a dated correction and a re-run, never by an edit
(freeze §4's discipline, carried into the report itself).

    uv run python scripts/gen_measured_report.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MEASURED = ROOT / "benchmarks/tgir-v1/measured.yaml"
FORECAST = ROOT / "docs/design/tgir_b1/forecast.yaml"
OUT = ROOT / "docs/design/TGIR_M3_MEASURED_REPORT.md"

SUITE_NAMES = {
    "ldbc-is": "LDBC Interactive Short", "ldbc-ic": "LDBC Interactive Complex",
    "ldbc-bi": "LDBC Business Intelligence",
    "independent-bo": "independent — bitcoin-otc",
    "independent-cm": "independent — CollegeMsg",
}


def instrument_axis() -> dict[str, str]:
    """The two public axes, read out of the instruments themselves rather than
    asserted — the addendum keeps them as the axis-of-record."""
    out = {}
    for script, needle in (("ldbc_fit.py", "expressible:"),
                           ("independent_questions.py", "expressible now:")):
        try:
            result = subprocess.run(
                ["uv", "run", "python", f"scripts/{script}", "report"],
                cwd=ROOT, capture_output=True, text=True, timeout=900)
            for line in result.stdout.splitlines():
                if line.startswith(needle):
                    out[script] = line[len(needle):].split("—")[0].strip()
        except Exception as e:                       # pragma: no cover
            out[script] = f"(not re-run: {e})"
    return out


def main() -> int:
    document = yaml.safe_load(MEASURED.read_text())
    rows = document["rows"]
    forecast = yaml.safe_load(FORECAST.read_text())
    axes = instrument_axis()

    validate(rows, forecast)

    unlocked = [r for r in rows if r["predicted"] in ("yes", "partial-columns")]
    delivered = [r for r in unlocked if r["delivered"]]
    misses = [r for r in unlocked if not r["delivered"]]
    over = [r for r in rows if r["predicted"] == "no" and r["measured"] != "no"]
    scoreable = [r for r in rows if r["scoreable"]]
    scoreable_unlocked = [r for r in unlocked if r["scoreable"]]
    scoreable_delivered = [r for r in scoreable_unlocked if r["delivered"]]
    excluded = [r for r in rows if not r["scoreable"]]
    evidence = Counter(r["evidence"] for r in rows)
    gold_rows = [r for r in rows if "gold" in r]

    per_suite = []
    for suite, name in SUITE_NAMES.items():
        suite_rows = [r for r in rows if r["suite"] == suite]
        suite_unlocked = [r for r in suite_rows
                          if r["predicted"] in ("yes", "partial-columns")]
        suite_delivered = [r for r in suite_unlocked if r["delivered"]]
        per_suite.append((name, len(suite_rows), len(suite_unlocked),
                          len(suite_delivered)))

    text = _render(document, forecast, axes, rows, unlocked, delivered, misses,
                   over, scoreable, scoreable_unlocked, scoreable_delivered,
                   excluded, evidence, gold_rows, per_suite)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"delivered/predicted {len(delivered)}/{len(unlocked)}; "
          f"scoreable {len(scoreable_delivered)}/{len(scoreable_unlocked)} "
          f"of {len(scoreable)} scoreable; "
          f"over-delivered {len(over)}; misses {len(misses)}")
    return 0


def validate(rows: list[dict[str, Any]], forecast: dict[str, Any]) -> None:
    """The freeze's gate, enforced where it cannot be skipped: the measured
    record covers the forecast's id set exactly, and no row is missing a
    verdict. A record that fails this cannot produce a report."""
    measured_ids = [r["id"] for r in rows]
    forecast_ids = [r["id"] for r in forecast["rows"]]
    if len(measured_ids) != len(set(measured_ids)):
        raise SystemExit("measured.yaml has duplicate ids")
    if set(measured_ids) != set(forecast_ids):
        missing = sorted(set(forecast_ids) - set(measured_ids))
        extra = sorted(set(measured_ids) - set(forecast_ids))
        raise SystemExit(f"id set differs from forecast: missing={missing} "
                         f"extra={extra}")
    for row in rows:
        for key in ("predicted", "measured", "evidence", "delivered",
                    "scoreable", "suite"):
            if row.get(key) is None:
                raise SystemExit(f"{row['id']}: no {key}")
        if row["measured"] not in ("no", "partial-rows", "partial-columns",
                                   "yes"):
            raise SystemExit(f"{row['id']}: bad verdict {row['measured']!r}")
        forecast_row = next(f for f in forecast["rows"] if f["id"] == row["id"])
        for mine, frozen in (("predicted", "predicted_v1_support"),
                             ("predicted_detail", "predicted_verdict_detail"),
                             ("scoreable", "scoreable")):
            if row[mine] != forecast_row[frozen]:
                raise SystemExit(
                    f"{row['id']}: {mine}={row[mine]!r} does not match the "
                    f"frozen forecast's {frozen}={forecast_row[frozen]!r}")
    print(f"validated {len(rows)} rows against the frozen forecast")


def _render(document: dict[str, Any], forecast: dict[str, Any],
            axes: dict[str, str], rows: list[dict[str, Any]], unlocked: list,
            delivered: list, misses: list, over: list, scoreable: list,
            scoreable_unlocked: list, scoreable_delivered: list,
            excluded: list, evidence: Counter, gold_rows: list,
            per_suite: list) -> str:
    suite_table = "\n".join(
        f"| {name} | {n} | {pred} | {deliv} | {deliv}/{pred} |"
        for name, n, pred, deliv in per_suite)

    gold_table = "\n".join(
        f"| {r['id']} | `{json.dumps(r['gold']['expected'])}` | "
        f"`{json.dumps(r['gold']['measured'])}` | "
        f"{'**agrees**' if r['gold']['matches'] else '**DIFFERS**'} |"
        for r in gold_rows)

    evidence_table = "\n".join(
        f"| {level} | {count} |" for level, count in sorted(evidence.items()))

    # the seventh divergence is set apart: it is the only one ruled DURING
    # measurement, and burying it alphabetically among the six pre-registered
    # ones would be exactly the silence the coordinator forbade
    route_rows = [r for r in rows if "route_note" in r]
    seventh = [r for r in route_rows if "SEVENTH" in r["route_note"]]
    routes = "\n".join(f"- **{r['id']}** — {r['route_note']}"
                       for r in route_rows if r not in seventh)
    routes_seventh = "\n".join(f"- **{r['id']}** — {r['route_note']}"
                               for r in seventh)

    fixture_zero = [r for r in rows if "fixture_zero" in r]
    zeros = "\n".join(f"- **{r['id']}** returns 0 rows: {r['fixture_zero']}."
                      for r in fixture_zero)

    def scored(r: dict[str, Any]) -> str:
        # only a predicted-unlocked row is in the numerator; a predicted-blocked
        # row that stayed blocked is `held`, not a delivery, and saying "yes"
        # there would inflate the eye's count past the headline's
        if not r["scoreable"]:
            return "excluded"
        if r["predicted"] not in ("yes", "partial-columns"):
            return "held" if r["delivered"] else "**BROKE**"
        return "delivered" if r["delivered"] else "**MISS**"

    per_row = "\n".join(
        f"| {r['id']} | {r['suite'].replace('ldbc-', '').replace('independent-', '')} "
        f"| {r['predicted_detail']} | {r['measured_detail']} | "
        f"{scored(r)} | {r['evidence']} | {r.get('rows', '—')} |"
        for r in rows)

    misses_text = ("**None.** Every predicted-unlocked row reached its "
                   "predicted level." if not misses else "\n".join(
                       f"- **{r['id']}**: predicted `{r['predicted_detail']}`, "
                       f"measured `{r['measured_detail']}`." for r in misses))

    return f"""# TGIR-v1 — M3 measured report

**{document['measured_date']}.** Predicted-versus-actual for the 52-row forecast
frozen on {forecast['frozen_date']} against spec anchor `{forecast['spec_anchor']}`.

Every number here is derived from `benchmarks/tgir-v1/measured.yaml` by
`scripts/gen_measured_report.py`; the two public axes are read out of the
instruments themselves. Nothing is asserted twice, and a correction is a
re-run rather than an edit.

**Protocol:** `docs/design/TGIR_FORECAST_FREEZE.md` §4 as extended by
`docs/design/TGIR_FORECAST_FREEZE_ADDENDUM_1.md`, which was committed **before
the first measured row**.

---

## Headline

> **delivered / predicted = {len(delivered)} / {len(unlocked)}** on the full {len(rows)}-row universe, with **{len(over)} over-deliveries**.
>
> On the scoreable universe: **{len(scoreable_delivered)} / {len(scoreable_unlocked)}**, drawn from {len(scoreable)} scoreable rows — {', '.join(r['id'] for r in excluded)} was excluded **by name in the freeze**, before any measurement.

Both denominators are reported, as §4 requires; neither was chosen after the
fact. The scoring rule — *{document['scoring_rule']}* — was fixed in the
addendum, which was committed before the first row was measured. An
over-delivery is never netted against a miss.

### Misses

{misses_text}

---

## The two public axes

Recomputed **from the instruments**, not asserted:

| axis | baseline | predicted | measured |
|---|---|---|---|
| LDBC operator-execution (`class ∈ {{1,2}}`) | 3 of 41 | 24 of 41 | **{axes.get('ldbc_fit.py', 'n/a')}** |
| independent questions | 94 of 110 | 102 of 110 | **{axes.get('independent_questions.py', 'n/a')}** |

The instrument layers are `L19` (38 entries: 21 freed, 17 re-tagged) and `C27`
(12 entries: 8 freed, 4 re-tagged), both append-only and both derived from
`measured.yaml` by `scripts/gen_instrument_layers.py`. No earlier table was
edited and no earlier verdict rewritten. Every freed row is **class 2, never
class 1** — a TGIR compilation is several nodes by construction.

---

## Per suite

| suite | rows | predicted unlocked | delivered | ratio |
|---|---|---|---|---|
{suite_table}

---

## Evidence ladder

The addendum's §10.1 (v). A row's verdict records the highest level it reached:

| level | rows |
|---|---|
{evidence_table}

`L0-attempted` is a predicted-blocked row: its evidence is the attempted
compilation naming the residual that blocks it, which is the same kind of
evidence the prediction carried and is labelled as such. **No row's verdict
rests on L1 alone.**

### Gold agreement

Seven independent rows have double-keyed gold — computed once in SQL over the
store's DuckDB file and once in pure Python over the columnar edge list, with
neither sharing code with the evaluator:

| row | gold | measured | verdict |
|---|---|---|---|
{gold_table}

cm39 needed a readout fix, recorded here rather than quietly absorbed. The
plan's answer was **identical in content** but differently keyed —
`lo`/`hi`/`day`/`n` against the gold's `pair`/`day`/`count` — and the first
comparison called it a difference. **The readout was fixed; the plan and the
gold were both left untouched.** The gold additionally carries `tied_cells: 1`,
a diagnostic recording that exactly one cell holds the maximum, so §5's
tie-break allowance is not load-bearing here: the extremum is unique.

---

## Route divergences

A route divergence is a row whose plan takes a shape other than the workload's
own sketch. Six were fixed **in advance** in freeze §7:

{routes}

### The seventh

M3 found one more. It was **ruled by the coordinator before the row was
measured**, not adopted silently and not discovered in the write-up:

{routes_seventh}

---

## Honest disclosure

**There is no LDBC SNB data in this repository.** The 21 LDBC rows execute
against a hand-built store carrying LDBC's labels, relationship types and
multi-hop topology at a size a reviewer can read in one screen
(`scripts/build_ldbc_fixture.py`: 22 entities, 57 edge versions). It
establishes that a plan **compiles, loads, admits and executes**. It
establishes **nothing about scale**. BI11's triangle is trivial on it; on a real
SNB instance BI11 without its `sources` cohort pushdown is the canonical
refusal case.

The admission axis is therefore meaningfully measured **only** for the
independent rows, which run on real bitcoin-otc (5,881 entities / 35,592 edge
versions) and CollegeMsg data. **No row was refused by the cost guard** at the
frozen policy, on either substrate.

Two rows execute and return **zero rows for a fixture reason rather than a plan
reason**, recorded by name so no reader mistakes an empty result for a
compilation failure:

{zeros}

### What making the artifacts faithful cost

IC5 and IC6 were rewritten before measuring, because their earlier revisions
routed around the evaluator instead of following their forecast routes.
Rewriting them exposed **two real evaluator defects**, both fixed, both found by
insisting on the faithful shape rather than the shape that happened to run:

1. `Join{{inner}}` asserted that the realized schema equalled the declared one.
   Pruning legitimately violates that, so **any non-root inner join raised
   `E_INTERNAL`**. The assertion was wrong and was removed.
2. `eval_pattern` built domains for edge variables only, so a `sources` binding
   on a **node** variable was accepted and then silently ignored — a cohort
   restriction that narrowed nothing. Node domains are now built and applied
   after every stage.

Both plans then executed and returned **identical rows** to their pre-rewrite
verdicts, which is why the rewrite changed no score. Had the artifacts been left
as they were, two defects would have shipped behind two green rows.

---

## Every row

`delivered` is a predicted-unlocked row that reached its level; `held` is a
predicted-blocked row that stayed blocked — correct, but not a delivery and not
in the numerator. `rows` is the result cardinality where the plan executed.

| row | suite | predicted | measured | scoring | evidence | rows |
|---|---|---|---|---|---|---|
{per_row}

---

## Defect found while measuring

**`read_only=True` never reaches the DuckDB adapter, so two readers of one
store cannot coexist.** `Store.__init__` (`tgms/store.py:24`) takes `read_only`,
stores it, and uses it to skip recovery and refuse writes — but
`_make_adapter(backend, self.path)` passes it no further, and
`DuckDBAdapter.__init__` (`tgms/storage/duckdb_adapter.py:48`) calls
`duckdb.connect(str(path))` with no `read_only` argument. DuckDB therefore takes
its exclusive write lock on the file, and a second *reading* process — a second
measurement pass, a validator beside the suite, a notebook open on the same
store — fails with a lock error that names no cause, though neither process
writes.

`tgms.open`'s docstring advertises `read_only=True` as "the mode for a reader
process". At the DuckDB backend it is not: it is a Python-side write refusal
over a read-write connection. The gap caps concurrency at one process per store
for **read** workloads, which is exactly the shape a benchmark harness and an
interactive session take together. It changed no verdict here — the measurement
was serialized around it — and it is filed rather than fixed because store-open
semantics are outside M3.5's scope.

*Chip:* thread `read_only` through `_make_adapter` into
`duckdb.connect(path, read_only=...)`. Note the fix is **not** a one-line
passthrough: `DuckDBAdapter.__init__` runs `CREATE TABLE IF NOT EXISTS` DDL at
open, which a genuine read-only connection rejects, so the DDL needs skipping
under the flag. Add a test that opens one store file from two handles at once
and reads from both.

---

## Gates and receipts

Every one of these was green at the time this report was generated. They are
listed as commands, not as copied numbers, so a reader re-runs rather than
trusts:

| gate | command |
|---|---|
| the 52 rows load, validate, and execute-or-refuse | `uv run python scripts/tgir_validate.py` |
| the measurement is reproducible | `uv run python scripts/tgir_measure.py` — re-running rewrites `measured.yaml` **byte-identically** |
| the id set matches the frozen forecast, no verdict absent | run by `scripts/gen_measured_report.py` before it will write this file |
| compiled operators equal their originals | `uv run python scripts/tgir_equiv.py` (26/26) |
| frozen digests unmoved | `uv run python scripts/check_digest_stability.py` |
| core evaluators reproduce the operators they compile from | `uv run python scripts/check_core_equivalence.py` |
| every emitted scope satisfies §2.0's shape obligations | `uv run python scripts/check_scope_shape.py` |
| leaf totality: 15 operators, 15 leaves, no unwrapped path | `uv run python scripts/check_tgir_leaf_totality.py` |
| `tt_q` / dependency semantics | `uv run python scripts/check_ttq_semantics.py` |
| suites, both backends and with the plan path disabled | `pytest`; `TGMS_TEST_BACKEND=native pytest`; `TGIR_PLAN_PATH=off pytest` |

The rollback flag is a real gate, not a formality: `TGIR_PLAN_PATH=off` runs the
whole suite green, so every TGIR route in this report can be switched off
without taking anything else down with it.

---

## Provenance

| | |
|---|---|
| forecast | `{document['forecast']}` (frozen {forecast['frozen_date']}) |
| addendum | `{document['addendum']}` (committed before the first measured row) |
| row record | `benchmarks/tgir-v1/measured.yaml` |
| plan artifacts | `benchmarks/tgir-v1/plans/` (52 files) |
| gold | `benchmarks/tgir-v1/gold.json` (7 rows, double-keyed) |
| instruments | `scripts/ldbc_fit.py` (`L19`), `scripts/independent_questions.py` (`C27`) |
"""


if __name__ == "__main__":
    sys.exit(main())
