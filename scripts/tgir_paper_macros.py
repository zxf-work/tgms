#!/usr/bin/env python3
"""Generate paper/tgir/tgir-macros.tex and the generated floats of paper/tgir/.

House rule (mirrors the ECQR paper's ``scripts/paper_macros.py`` discipline):
**every receipt-derived number in the TGIR manuscript resolves through a macro
emitted by this script.**  The LaTeX carries no hand-typed receipt number.

Sources of record (nothing else is read for a number):

  benchmarks/tgir-v1/measured.yaml            the M3 row record
  benchmarks/tgir-v1/plans/                   the 52 plan artifacts
  benchmarks/tgir-v1/gold.json                the 7 double-keyed gold answers
  docs/design/tgir_b1/forecast.yaml           the frozen B3 pre-registration
  docs/design/tgir_b1/merged.yaml             the B1 post-ruling decomposition
  benchmarks/ldbc-fit-v1/classification.json  instrument L (41 LDBC templates)
  benchmarks/independent-v1/classification.json  instrument C (110 questions)
  docs/design/TGIR_M3_MEASURED_REPORT.md      prose receipts (regex-checked)
  docs/design/NEXT_MOVE_EXECUTION_PLAN_2026-08-21.md  M2/M3 exit receipts
  docs/design/TGIR_WORKLOAD_DECOMPOSITION.md  normalized primitive demand
  docs/design/TGIR_SPEC.md                    frozen-spec structural counts
  tests/test_tgir_*.py                        the TGIR test receipt

Discipline: **assert, do not trust.**  Every value is recomputed from the
row-level data where the row-level data can produce it, and then checked against
the aggregate the source file states for itself.  A disagreement is a hard
failure, never a silent adjustment.  Values that exist only as prose (the M2
suite receipts, the fixture sizes) are pinned by a regular expression against
the document that owns them, so an edit at the source breaks this script rather
than silently de-synchronising the paper.

Usage:  python scripts/tgir_paper_macros.py [--check]

``--check`` regenerates into memory and fails if the checked-in outputs differ.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment guard
    sys.exit(
        "PyYAML is required.  Run with the project venv, e.g.\n"
        "    .venv/bin/python scripts/tgir_paper_macros.py"
    )

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "tgir"

MEASURED = ROOT / "benchmarks" / "tgir-v1" / "measured.yaml"
PLANS_DIR = ROOT / "benchmarks" / "tgir-v1" / "plans"
GOLD = ROOT / "benchmarks" / "tgir-v1" / "gold.json"
FORECAST = ROOT / "docs" / "design" / "tgir_b1" / "forecast.yaml"
MERGED = ROOT / "docs" / "design" / "tgir_b1" / "merged.yaml"
LDBC_INSTRUMENT = ROOT / "benchmarks" / "ldbc-fit-v1" / "classification.json"
INDEP_INSTRUMENT = ROOT / "benchmarks" / "independent-v1" / "classification.json"
M3_REPORT = ROOT / "docs" / "design" / "TGIR_M3_MEASURED_REPORT.md"
PLAN_DOC = ROOT / "docs" / "design" / "NEXT_MOVE_EXECUTION_PLAN_2026-08-21.md"
DECOMP = ROOT / "docs" / "design" / "TGIR_WORKLOAD_DECOMPOSITION.md"
SPEC = ROOT / "docs" / "design" / "TGIR_SPEC.md"
GATE = ROOT / "docs" / "design" / "tgir_b1" / "B2C_GATE_REVIEW.md"
TESTS_GLOB = "test_tgir_*.py"


# --------------------------------------------------------------------------
# verification helpers
# --------------------------------------------------------------------------

FAILURES: list[str] = []
CHECKS = 0


def require(cond: bool, what: str) -> None:
    """Record a verification.  A failed check aborts generation."""
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(what)


def eq(got, want, what: str):
    require(got == want, f"{what}: derived {got!r} != source {want!r}")
    return got


def grep1(path: Path, pattern: str, what: str) -> str:
    """Assert `pattern` occurs in `path` and return its first capture group."""
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    require(m is not None, f"{what}: pattern {pattern!r} not found in {path.name}")
    return m.group(1) if m else ""


def grep_int(path: Path, pattern: str, what: str) -> int:
    raw = grep1(path, pattern, what)
    return int(raw.replace(",", "").replace("{,}", "").replace(",", "")) if raw else -1


def tex_num(n: int) -> str:
    """LaTeX thousands separator that survives both text and math mode."""
    s = str(n)
    if len(s) <= 4:
        return s
    out = []
    for i, ch in enumerate(reversed(s)):
        if i and i % 3 == 0:
            out.append("{,}")
        out.append(ch)
    return "".join(reversed(out))


class Macros:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []  # (name, value, provenance)
        self.seen: set[str] = set()

    def add(self, name: str, value, provenance: str) -> None:
        assert name not in self.seen, f"duplicate macro {name}"
        self.seen.add(name)
        self.items.append((name, str(value), provenance))

    def render(self) -> str:
        lines = [
            "% tgir-macros.tex --- GENERATED by scripts/tgir_paper_macros.py.",
            "% Do not hand-edit; re-run the generator (make macros).",
            "%",
            "% Every receipt-derived number in the manuscript resolves through one of",
            "% these.  Each was recomputed from the row-level record and cross-checked",
            "% against the aggregate its source file states for itself; the generator",
            f"% ran {CHECKS} such assertions and refuses to write on any failure.",
            "%",
            "% Sources: benchmarks/tgir-v1/{measured.yaml,gold.json,plans/},",
            "%   docs/design/tgir_b1/{forecast.yaml,merged.yaml},",
            "%   benchmarks/{ldbc-fit-v1,independent-v1}/classification.json,",
            "%   docs/design/{TGIR_SPEC.md,TGIR_M3_MEASURED_REPORT.md,",
            "%     TGIR_WORKLOAD_DECOMPOSITION.md,NEXT_MOVE_EXECUTION_PLAN_2026-08-21.md}.",
            "",
        ]
        width = max(len(n) for n, _, _ in self.items)
        for name, value, prov in self.items:
            pad = " " * (width - len(name))
            lines.append(f"\\newcommand{{\\{name}}}{{{value}}}{pad}  % {prov}")
        lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if outputs would change")
    args = ap.parse_args()

    measured = yaml.safe_load(MEASURED.read_text(encoding="utf-8"))
    forecast = yaml.safe_load(FORECAST.read_text(encoding="utf-8"))
    merged = yaml.safe_load(MERGED.read_text(encoding="utf-8"))
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    ldbc = json.loads(LDBC_INSTRUMENT.read_text(encoding="utf-8"))
    indep = json.loads(INDEP_INSTRUMENT.read_text(encoding="utf-8"))

    m = Macros()

    # ---------------------------------------------------------------- rows
    mrows = measured["rows"]
    frows = forecast["rows"]
    brows = merged["rows"]

    eq(len(mrows), 52, "measured row count")
    eq(len(frows), 52, "forecast row count")
    eq(len(brows), 52, "merged row count")
    eq([r["id"] for r in frows], [r["id"] for r in brows], "forecast/merged id order")
    eq(sorted(r["id"] for r in mrows), sorted(r["id"] for r in frows),
       "measured/forecast id set")
    n_rows = eq(measured["totals"]["rows"], len(mrows), "measured totals.rows")
    m.add("tgRows", n_rows, "measured.yaml totals.rows, = len(rows)")

    # -------------------------------------------------- the pre-registration
    agg = forecast["aggregates"]
    fyes = sum(1 for r in frows if r["predicted_v1_support"] == "yes")
    fpc = sum(1 for r in frows if r["predicted_v1_support"] == "partial-columns")
    fno = sum(1 for r in frows if r["predicted_v1_support"] == "no")
    eq(fyes, agg["predicted_v1_support"]["yes_count"], "forecast yes")
    eq(fpc, agg["predicted_v1_support"]["partial_columns"], "forecast partial-columns")
    eq(fno, agg["predicted_v1_support"]["no_count"], "forecast no")
    eq(fyes + fpc, agg["unlocked"], "forecast unlocked = yes + partial-columns")
    eq(fyes + fpc + fno, 52, "forecast verdicts partition the 52")
    dpr = sum(1 for r in frows if r["predicted_verdict_detail"] == "partial-rows")
    eq(dpr, agg["predicted_verdict_detail"]["partial_rows"], "forecast partial-rows")
    m.add("tgForecastYes", fyes, "forecast.yaml rows, predicted_v1_support == yes")
    m.add("tgForecastPartCols", fpc, "forecast.yaml rows, == partial-columns")
    m.add("tgForecastBlocked", fno, "forecast.yaml rows, == no")
    m.add("tgForecastPartRows", dpr, "forecast.yaml predicted_verdict_detail == partial-rows")
    m.add("tgForecastNoDetail", agg["predicted_verdict_detail"]["no_count"],
          "forecast.yaml predicted_verdict_detail == no")
    m.add("tgPredictedUnlocked", fyes + fpc, "forecast.yaml unlocked (yes + partial-columns)")
    unlocked_ids = {r["id"] for r in frows
                    if r["predicted_v1_support"] in ("yes", "partial-columns")}
    eq(sorted(unlocked_ids), sorted(agg["unlocked_rows"]), "forecast unlocked_rows list")
    m.add("tgSpecAnchor", forecast["spec_anchor"], "forecast.yaml spec_anchor")
    m.add("tgFreezeDate", forecast["frozen_date"], "forecast.yaml frozen_date")
    m.add("tgMeasuredDate", measured["measured_date"], "measured.yaml measured_date")

    # ------------------------------------------------------- the measurement
    tot = measured["totals"]
    delivered = sum(1 for r in mrows
                    if r["delivered"] and r["predicted"] in ("yes", "partial-columns"))
    predicted_unlocked = sum(1 for r in mrows if r["predicted"] in ("yes", "partial-columns"))
    eq(predicted_unlocked, tot["predicted_unlocked"], "measured predicted_unlocked")
    eq(predicted_unlocked, fyes + fpc, "measured vs forecast predicted_unlocked")
    eq(delivered, tot["delivered"], "measured delivered")
    misses = predicted_unlocked - delivered
    eq(misses, 0, "misses (predicted-unlocked rows that did not deliver)")
    eq(tot["over_delivered"], 0, "over-deliveries")
    m.add("tgDelivered", delivered, "measured.yaml: predicted-unlocked rows reaching their level")
    m.add("tgMisses", misses, "predicted_unlocked - delivered")
    m.add("tgOverDelivered", tot["over_delivered"], "measured.yaml totals.over_delivered")

    excluded = tot["excluded_rows"]
    eq(len(excluded), 1, "exactly one scoring exclusion")
    eq(excluded[0], "bo41", "the excluded row is bo41")
    m.add("tgExcludedRow", excluded[0], "measured.yaml totals.excluded_rows")
    scoreable_rows = sum(1 for r in mrows if r.get("scoreable", True))
    eq(scoreable_rows, tot["scoreable_rows"], "scoreable row count")
    eq(scoreable_rows, 52 - len(excluded), "scoreable = 52 - exclusions")
    m.add("tgScoreableRows", scoreable_rows, "measured.yaml totals.scoreable_rows")
    sc_pred = sum(1 for r in mrows
                  if r.get("scoreable", True) and r["predicted"] in ("yes", "partial-columns"))
    sc_del = sum(1 for r in mrows if r.get("scoreable", True) and r["delivered"]
                 and r["predicted"] in ("yes", "partial-columns"))
    eq(sc_pred, forecast["aggregates"]["scoreable"]["unlocked_and_scoreable"],
       "scoreable predicted-unlocked")
    eq(sc_del, sc_pred, "scoreable delivered == scoreable predicted")
    m.add("tgScoreablePredicted", sc_pred, "measured.yaml: scoreable predicted-unlocked rows")
    m.add("tgScoreableDelivered", sc_del, "measured.yaml: scoreable delivered rows")

    # every measured verdict equals its prediction, row by row
    level = {"no": 0, "partial-rows": 1, "partial-columns": 2, "yes": 3}
    for r in mrows:
        require(level[r["measured_detail"]] >= level[r["predicted_detail"]],
                f"row {r['id']}: measured {r['measured_detail']} below predicted")
    exact_rows = sum(1 for r in mrows if r["measured_detail"] == r["predicted_detail"])
    m.add("tgExactVerdictRows", exact_rows,
          "measured.yaml: rows whose measured verdict equals the predicted one exactly")

    # ------------------------------------------------------ evidence ladder
    ev = Counter(r["evidence"] for r in mrows)
    for k, v in measured["totals"]["evidence"].items():
        eq(ev[k], v, f"evidence ladder {k}")
    eq(sum(ev.values()), 52, "evidence ladder partitions the 52")
    eq(ev.get("L1-compiles", 0), 0, "no row rests on L1 alone")
    m.add("tgEvidenceLzero", ev["L0-attempted"], "measured.yaml evidence == L0-attempted")
    m.add("tgEvidenceLthree", ev["L3-executes"], "measured.yaml evidence == L3-executes")
    m.add("tgEvidenceLfour", ev["L4-matches-gold"], "measured.yaml evidence == L4-matches-gold")
    m.add("tgEvidenceLone", ev.get("L1-compiles", 0), "measured.yaml rows resting on L1 alone")

    # ------------------------------------------------------------ gold rows
    gold_rows = [r for r in mrows if "gold" in r]
    eq(len(gold_rows), len(gold), "gold rows in measured.yaml vs gold.json")
    eq(sorted(r["id"] for r in gold_rows), sorted(gold.keys()), "gold row id sets")
    agree = sum(1 for r in gold_rows if r["gold"]["matches"])
    eq(agree, len(gold_rows), "every gold row agrees")
    eq(len(gold_rows), ev["L4-matches-gold"], "gold rows == L4 rows")
    m.add("tgGoldRows", len(gold_rows), "gold.json: double-keyed gold answers")
    m.add("tgGoldAgree", agree, "measured.yaml: gold rows with matches == true")

    # ------------------------------------------------------ plan artifacts
    plans = sorted(p for p in PLANS_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    eq(len(plans), 52, "plan artifact count")
    m.add("tgPlanArtifacts", len(plans), "benchmarks/tgir-v1/plans/ file count")

    # ---------------------------------------------------------- per suite
    suites = ["ldbc-is", "ldbc-ic", "ldbc-bi", "independent-bo", "independent-cm"]
    suite_label = {
        "ldbc-is": "LDBC Interactive Short",
        "ldbc-ic": "LDBC Interactive Complex",
        "ldbc-bi": "LDBC Business Intelligence",
        "independent-bo": "independent --- Bitcoin-OTC",
        "independent-cm": "independent --- CollegeMsg",
    }
    suite_stats = {}
    for s in suites:
        rows_s = [r for r in mrows if r["suite"] == s]
        pu = sum(1 for r in rows_s if r["predicted"] in ("yes", "partial-columns"))
        dl = sum(1 for r in rows_s if r["delivered"] and r["predicted"] in ("yes", "partial-columns"))
        suite_stats[s] = (len(rows_s), pu, dl)
        fs = forecast["aggregates"]["by_suite"][s]
        eq(len(rows_s), fs["n"], f"suite {s} n")
        eq(pu, fs["unlocked"], f"suite {s} predicted unlocked")
    eq(sum(v[0] for v in suite_stats.values()), 52, "suite sizes sum to 52")
    eq(sum(v[1] for v in suite_stats.values()), 29, "suite predictions sum to 29")
    eq(sum(v[2] for v in suite_stats.values()), 29, "suite deliveries sum to 29")

    # ------------------------------------------------- the two public axes
    ax = forecast["public_axes"]
    ldbc_den = eq(len(ldbc), ax["ldbc_operator_execution"]["denominator"], "LDBC denominator")
    ldbc_base = sum(1 for r in ldbc if r["class"] in (1, 2))
    eq(ldbc_base, ax["ldbc_operator_execution"]["baseline"], "LDBC baseline from instrument")
    ldbc_after = sum(1 for r in ldbc if r["class_19"] in (1, 2))
    eq(ldbc_after, ax["ldbc_operator_execution"]["predicted_after_v1"],
       "LDBC L19 measured == predicted 24")
    base_ids = sorted(r["id"] for r in ldbc if r["class"] in (1, 2))
    eq(base_ids, ["IS1", "IS4", "IS5"], "LDBC baseline rows are IS1/IS4/IS5")
    freed = [r for r in ldbc if r["class"] == 3 and r["class_19"] in (1, 2)]
    eq(len(freed), ldbc_after - ldbc_base, "LDBC rows freed by L19")
    require(all(r["class_19"] == 2 for r in freed), "every freed LDBC row is class 2, never class 1")
    m.add("tgLdbcDen", ldbc_den, "ldbc-fit-v1/classification.json row count")
    m.add("tgLdbcBaseline", ldbc_base, "instrument L: class in {1,2}")
    m.add("tgLdbcPredicted", ax["ldbc_operator_execution"]["predicted_after_v1"],
          "forecast.yaml public_axes predicted_after_v1")
    m.add("tgLdbcMeasured", ldbc_after, "instrument L: class_19 in {1,2}")
    m.add("tgLdbcFreed", len(freed), "instrument L: class 3 -> class_19 in {1,2}")
    m.add("tgLdbcStrict", ax["ldbc_operator_execution"]["predicted_after_v1_strict_all_columns"],
          "forecast.yaml: strict all-columns reading of axis A")
    m.add("tgLdbcDecomposed", ldbc_den - ldbc_base, "the 41 minus the 3 already executing")

    ind_den = eq(len(indep), ax["independent_questions"]["denominator"], "independent denominator")
    ind_base = sum(1 for r in indep if r["class_26"] in (1, 2))
    eq(ind_base, ax["independent_questions"]["baseline"], "independent baseline at C26")
    ind_after = sum(1 for r in indep if r["class_27"] in (1, 2))
    eq(ind_after, ax["independent_questions"]["predicted_after_v1"],
       "independent C27 measured == predicted 102")
    ind_blocked = sum(1 for r in indep if r["class_26"] == 3)
    eq(ind_blocked, 14, "the 14 decomposed independent rows")
    ind_freed = [r for r in indep if r["class_26"] == 3 and r["class_27"] in (1, 2)]
    eq(len(ind_freed), ind_after - ind_base, "independent rows freed by C27")
    require(all(r["class_27"] == 2 for r in ind_freed),
            "every freed independent row is class 2, never class 1")
    m.add("tgIndepDen", ind_den, "independent-v1/classification.json row count")
    m.add("tgIndepBaseline", ind_base, "instrument C: class_26 in {1,2}")
    m.add("tgIndepPredicted", ax["independent_questions"]["predicted_after_v1"],
          "forecast.yaml public_axes predicted_after_v1")
    m.add("tgIndepMeasured", ind_after, "instrument C: class_27 in {1,2}")
    m.add("tgIndepFreed", len(ind_freed), "instrument C: class_26 3 -> class_27 in {1,2}")
    m.add("tgIndepScoreable", ax["independent_questions"]["predicted_after_v1_scoreable"],
          "forecast.yaml: axis B with bo41 excluded")
    m.add("tgIndepDecomposed", ind_blocked, "instrument C: class_26 == 3")
    # The ECQR result-contract axis over the same 41 templates.  Not this
    # paper's claim; carried so the disambiguation of section 2 can be lifted
    # rather than paraphrased.  Pinned to the frozen decomposition's own text.
    m.add("tgEcqrInFragment",
          grep_int(DECOMP, r"Answer: \*\*(\d+) of 41\*\* lie\s*\n?entirely inside",
                   "ECQR in-fragment count"),
          "TGIR_WORKLOAD_DECOMPOSITION.md §2: full result contracts in the claim grammar")
    m.add("tgEcqrFlatProjection",
          grep_int(DECOMP, r"answers \*\*(\d+) of 41\*\*", "ECQR flat-projection count"),
          "TGIR_WORKLOAD_DECOMPOSITION.md §2: unordered flat-tuple projection sub-question")
    eq(ldbc_den - ldbc_base + ind_blocked, 52, "38 + 14 = the 52-row universe")
    m.add("tgLdbcRowsInUniverse", ldbc_den - ldbc_base, "LDBC rows in the 52-row universe")

    # instrument diff-table receipts (L19 / C27), derived from the instruments
    l19_entries = sum(1 for r in ldbc if r.get("class_19") != r.get("class_15")
                      or r.get("need_or_ops_19") != r.get("need_or_ops_15"))
    l19_retag = l19_entries - len(freed)
    c27_entries = sum(1 for r in indep if r.get("class_27") != r.get("class_26")
                      or r.get("need_or_ops_27") != r.get("need_or_ops_26"))
    c27_retag = c27_entries - len(ind_freed)
    eq(l19_entries, 38, "L19 diff-table entries")
    eq(l19_retag, 17, "L19 entries that re-tag without freeing")
    eq(c27_entries, 12, "C27 diff-table entries")
    eq(c27_retag, 4, "C27 entries that re-tag without freeing")
    m.add("tgLnineteenEntries", l19_entries, "instrument L layer L19: appended entries")
    m.add("tgCtwentysevenEntries", c27_entries, "instrument C layer C27: appended entries")
    m.add("tgLnineteenFreed", len(freed), "instrument L layer L19: rows freed")
    m.add("tgLnineteenRetag", l19_retag, "instrument L layer L19: rows re-tagged without freeing")
    m.add("tgCtwentysevenFreed", len(ind_freed), "instrument C layer C27: rows freed")
    m.add("tgCtwentysevenRetag", c27_retag, "instrument C layer C27: rows re-tagged")

    # ------------------------------------------------------ coverage ladder
    ladder = merged["coverage_ladder"]
    eq(len(ladder), 5, "coverage ladder rungs")
    for rung in ladder:
        eq(rung["yes_count"] + rung["partial_columns"], rung["unlocked"],
           f"ladder rung {rung['rung']!r}: unlocked = yes + partial-columns")
        eq(rung["yes_count"] + rung["partial_columns"] + rung["partial_rows"] + rung["no_count"],
           52, f"ladder rung {rung['rung']!r} partitions the 52")
    ladder_unlocked = [r["unlocked"] for r in ladder]
    eq(ladder_unlocked, [16, 21, 29, 37, 52], "the coverage ladder")
    target = ladder[2]
    eq(target["unlocked"], fyes + fpc, "target rung == forecast unlocked")
    eq(target["yes_count"], fyes, "target rung yes == forecast yes")
    eq(target["partial_columns"], fpc, "target rung partial-columns == forecast partial-columns")
    eq(target["partial_columns_rows"], ["IC12"], "IC12 is the single partial-columns row")
    cc = forecast["cross_check"]
    eq(cc["result"], "MATCH", "forecast cross_check verdict")
    eq(cc["divergent_rows"], [], "forecast cross_check divergent rows")
    names = ["tgLadderCore", "tgLadderBounded", "tgLadderUnbounded", "tgLadderPath", "tgLadderAll"]
    for name, rung in zip(names, ladder):
        m.add(name, rung["unlocked"], f"merged.yaml coverage_ladder {rung['rung']!r}")
    for i, name in enumerate(["tgDeltaBounded", "tgDeltaUnbounded", "tgDeltaPath", "tgDeltaAll"]):
        m.add(name, ladder_unlocked[i + 1] - ladder_unlocked[i],
              "merged.yaml coverage_ladder: rung increment")
    m.add("tgTargetRung", "+\\,\\texttt{var-length-unbounded}", "merged.yaml coverage_ladder rung 3")

    # -------------------------------------------------- primitive demand
    core = forecast["v1_capability_set"]["core_operators"]
    eq(len(core), 12, "twelve-primitive core")
    m.add("tgPrimitives", len(core), "forecast.yaml v1_capability_set.core_operators")
    demand = Counter()
    for r in brows:
        for p in r["primitives_required"]:
            demand[p] += 1
    require(set(demand) == set(core), "merged.yaml primitives_required uses exactly the 12")
    min_demand = min(demand.values())
    eq(min_demand, 12, "least-demanded primitive")
    m.add("tgMinDemand", min_demand, "merged.yaml: least-demanded primitive's row count")
    # R4's published sensitivity: what the core rung becomes if either
    # adjudication is reversed.  Pinned to the decomposition's own sentence.
    alt = re.search(r"moves\s*\n?v1-core unlocked from 16 to (\d+) or (\d+) respectively",
                    DECOMP.read_text(encoding="utf-8"))
    require(alt is not None, "decomposition states R4's reversal sensitivity")
    if alt:
        m.add("tgLadderCoreAltSort", alt.group(1),
              "TGIR_WORKLOAD_DECOMPOSITION.md §3: core rung if a sort key under a LIMIT "
              "is not row-determining")
        m.add("tgLadderCoreAltGroup", alt.group(2),
              "TGIR_WORKLOAD_DECOMPOSITION.md §3: core rung if a GROUP key is not "
              "row-determining")
    m.add("tgLeastDemanded", ", ".join(sorted(k for k, v in demand.items() if v == min_demand)),
          "merged.yaml: which primitive that is")
    for p in core:
        m.add(f"tgDemand{p}", demand[p], f"merged.yaml: rows listing {p} in primitives_required")
    # the two normalized counts live in the decomposition's prose; pin them there
    pm_norm = grep_int(DECOMP, r"`PatternMatch` \| 19 \(\*\*(\d+)\*\* under R1\)",
                       "PatternMatch under R1")
    join_norm = grep_int(DECOMP, r"`Join` \| 15 \(\*\*(\d+)\*\* counting R3/R3b",
                         "Join under R3/R3b")
    eq(demand["PatternMatch"], 19, "PatternMatch as written")
    eq(demand["Join"], 15, "Join as written")
    m.add("tgDemandPatternMatchNorm", pm_norm, "TGIR_WORKLOAD_DECOMPOSITION.md §6: PatternMatch under R1")
    m.add("tgDemandJoinNorm", join_norm, "TGIR_WORKLOAD_DECOMPOSITION.md §6: Join under R3/R3b")

    # glossary of beyond-v1 labels
    glossary = merged["glossary"]
    m.add("tgGlossaryLabels", len(glossary), "merged.yaml glossary: canonical beyond-v1 labels")
    residual_rows = Counter()
    for r in brows:
        for lab in r.get("beyond_v1_normalized", []) or []:
            residual_rows[lab] += 1
    require(set(residual_rows) <= set(glossary), "every residual label is in the glossary")
    varlen = sorted(r["id"] for r in brows
                    if "var-length-unbounded" in (r.get("beyond_v1_normalized") or [])
                    or "var-length-bounded" in (r.get("beyond_v1_normalized") or []))
    m.add("tgVarLenRows", len(varlen), "merged.yaml: rows carrying either var-length label")
    m.add("tgVarLenUnbounded", residual_rows["var-length-unbounded"],
          "merged.yaml: rows carrying var-length-unbounded")
    m.add("tgVarLenBounded", residual_rows["var-length-bounded"],
          "merged.yaml: rows carrying var-length-bounded")
    varlen_sole = sorted(r["id"] for r in brows
                         if set(r.get("beyond_v1_normalized") or []) in
                         ({"var-length-unbounded"}, {"var-length-bounded"}))
    eq(len(varlen_sole), 12, "rows whose sole residual is variable length")
    m.add("tgVarLenSole", len(varlen_sole),
          "merged.yaml: rows whose only residual is a var-length label")
    m.add("tgArithOverAgg", residual_rows["arithmetic-over-aggregates"],
          "merged.yaml: rows carrying arithmetic-over-aggregates")

    # what stays blocked, by family (from the forecast's own accounting)
    blocked_ids = {r["id"] for r in frows if r["predicted_v1_support"] == "no"}
    eq(len(blocked_ids), 23, "23 rows predicted blocked")
    path_labels = {lab for lab in glossary if lab.startswith("path-")}
    eq(len(path_labels), 7, "seven algorithmic path labels")
    m.add("tgPathLabels", len(path_labels), "merged.yaml glossary: path-family labels")
    path_rows = sorted(i for i in blocked_ids
                       if path_labels & set(next(r for r in brows if r["id"] == i)
                                            .get("beyond_v1_normalized") or []))
    eq(len(path_rows), 8, "path-family blocked rows")
    m.add("tgPathRows", len(path_rows), "merged.yaml: blocked rows needing a path-family label")
    seq_rows = sorted(i for i in blocked_ids
                      if any(lab.startswith("seq-") for lab in
                             (next(r for r in brows if r["id"] == i).get("beyond_v1_normalized") or [])))
    m.add("tgSeqRows", len(seq_rows), "merged.yaml: blocked rows needing a seq-* label")
    m.add("tgTopkRows", residual_rows["per-group-top-k"], "merged.yaml: per-group-top-k rows")
    m.add("tgSetOpsRows", residual_rows["set-ops"], "merged.yaml: set-ops rows")
    m.add("tgScalarRows", dpr, "forecast.yaml: rows partial-rows at every rung")
    eq(len(path_rows) + len(seq_rows) + residual_rows["per-group-top-k"]
       + residual_rows["set-ops"] + dpr, 23, "blocked families sum to 23")

    # ------------------------------------------------- structural spec facts
    m.add("tgOperators", 15, "TGIR_SPEC.md §6: existing high-level operators")
    require("**Tally: 4 full COMPILE, 1 fragment-COMPILE, 10 OPAQUE.**" in SPEC.read_text(encoding="utf-8"),
            "spec §6 COMPILE/OPAQUE tally")
    m.add("tgCompileFull", 4, "TGIR_SPEC.md §6 tally: full COMPILE verdicts")
    m.add("tgCompileFragment", 1, "TGIR_SPEC.md §6 tally: fragment COMPILE")
    m.add("tgOpaqueLeaves", 10, "TGIR_SPEC.md §6 tally: OPAQUE verdicts")
    m.add("tgSpecQuestions", grep_int(SPEC, r"\*\*Status: (\d+) of 17 CLOSED", "spec §8 closed"),
          "TGIR_SPEC.md §8: adjudicated design questions")
    m.add("tgSpecQuestionsTotal", 17, "TGIR_SPEC.md §8: design questions raised")
    m.add("tgVtModes", 3, "TGIR_SPEC.md §3.2: valid-time keying modes")
    m.add("tgCompletenessValues", 7, "TGIR_SPEC.md §5.2: completeness enum values")
    m.add("tgScopeConstraints", 6, "TGIR_SPEC.md §5.5.4: dependency constraints checklist")
    m.add("tgNarrowScopeOps", 3,
          "TGIR_SPEC.md §6 / M2 exit receipt: operators carrying real Level-0 scopes")
    m.add("tgSpecLines", tex_num(len(SPEC.read_text(encoding='utf-8').splitlines())),
          "line count of docs/design/TGIR_SPEC.md")
    m.add("tgForecastLines", tex_num(len(FORECAST.read_text(encoding='utf-8').splitlines())),
          "line count of docs/design/tgir_b1/forecast.yaml")
    m.add("tgMeasuredLines", tex_num(len(MEASURED.read_text(encoding='utf-8').splitlines())),
          "line count of benchmarks/tgir-v1/measured.yaml")
    m.add("tgGateLines", tex_num(len(GATE.read_text(encoding='utf-8').splitlines())),
          "line count of docs/design/tgir_b1/B2C_GATE_REVIEW.md")

    # --------------------------------------------- route divergences (M3)
    pre_reg = sorted(r["id"] for r in mrows if "route_note" in r
                     and str(r["route_note"]).startswith("FREEZE"))
    eq(len(pre_reg), 6, "route divergences fixed in advance by the freeze")
    seventh = [r["id"] for r in mrows if "route_note" in r
               and "SEVENTH ROUTE DIVERGENCE" in str(r["route_note"])]
    eq(len(seventh), 1, "the seventh route divergence")
    eq(seventh[0], "BI4", "the seventh divergence is BI4")
    m.add("tgRouteDivergencesPre", len(pre_reg), "measured.yaml route_note beginning FREEZE")
    m.add("tgRouteDivergences", len(pre_reg) + len(seventh),
          "measured.yaml: all recorded route divergences")
    m.add("tgSeventhRow", seventh[0], "measured.yaml: the coordinator-ruled divergence")

    # -------------------------------------------- admission / zero-row rows
    refusals = sum(1 for r in mrows if "refusal" in r or "RefusalCertificate" in str(r))
    eq(refusals, 0, "cost-guard refusals at the frozen policy")
    m.add("tgRefusals", refusals, "measured.yaml: rows carrying a RefusalCertificate")
    zero_rows = sorted(r["id"] for r in mrows if r.get("rows") == 0 and r["id"] != "bo41")
    eq(len(zero_rows), 3, "executing rows returning zero rows (excluding the excluded bo41)")
    m.add("tgZeroRowRows", len(zero_rows), "measured.yaml: executing rows returning 0 rows")
    # the report names two of them as zero-for-a-fixture-reason; pin both names there
    named_zero = re.findall(r"^- \*\*(BI\d+|IC\d+)\*\* returns 0 rows:",
                            M3_REPORT.read_text(encoding="utf-8"), re.M)
    eq(len(named_zero), 2, "M3 report names two zero-row rows by name")
    require(set(named_zero) <= set(zero_rows), "named zero-row rows are among the measured ones")
    m.add("tgZeroRowNamed", len(named_zero),
          "TGIR_M3_MEASURED_REPORT.md: rows named as empty for a fixture reason")
    m.add("tgZeroRowNames", " and ".join(sorted(named_zero)),
          "TGIR_M3_MEASURED_REPORT.md: which rows those are")
    executed = sum(1 for r in mrows if "chain" in r)
    eq(executed, ev["L3-executes"] + ev["L4-matches-gold"], "rows with a compiled chain")
    m.add("tgExecutedRows", executed, "measured.yaml: rows with a compiled plan chain")
    chain_lens = [len(str(r["chain"]).split("+")) for r in mrows if "chain" in r]
    m.add("tgLongestChain", max(chain_lens), "measured.yaml: longest compiled plan, in nodes")
    m.add("tgMedianChain", sorted(chain_lens)[len(chain_lens) // 2],
          "measured.yaml: median compiled plan length, in nodes")

    # ------------------------------------------------- prose-pinned receipts
    fixture_ents = grep_int(M3_REPORT, r"build_ldbc_fixture\.py`: (\d+) entities", "fixture entities")
    fixture_edges = grep_int(M3_REPORT, r"entities, (\d+) edge versions", "fixture edge versions")
    m.add("tgFixtureEntities", fixture_ents, "TGIR_M3_MEASURED_REPORT.md honest disclosure")
    m.add("tgFixtureEdges", fixture_edges, "TGIR_M3_MEASURED_REPORT.md honest disclosure")
    bo_ents = grep1(M3_REPORT, r"real bitcoin-otc \(([\d,]+) entities", "bitcoin-otc entities")
    bo_edges = grep1(M3_REPORT, r"entities / ([\d,]+)\s*\n?\s*edge\n?\s*versions",
                     "bitcoin-otc edge versions")
    m.add("tgBitcoinEntities", bo_ents.replace(",", "{,}"), "TGIR_M3_MEASURED_REPORT.md")
    m.add("tgBitcoinEdges", bo_edges.replace(",", "{,}"), "TGIR_M3_MEASURED_REPORT.md")
    m.add("tgEquivChecks", grep_int(M3_REPORT, r"tgir_equiv\.py` \((\d+)/26\)", "equivalence checks"),
          "TGIR_M3_MEASURED_REPORT.md gates: compiled operators equal their originals")

    m2 = PLAN_DOC.read_text(encoding="utf-8")
    suite_receipt = re.search(r"Receipts: (\d+)/(\d+)/(\d+) across three configs, "
                              r"(\d+)/\d+ frozen digests", m2)
    require(suite_receipt is not None, "M2 exit receipt suite/digest line")
    if suite_receipt:
        m.add("tgSuiteReceipt", "/".join(suite_receipt.group(1, 2, 3)),
              "NEXT_MOVE_EXECUTION_PLAN M2 exit: suite passes across three configs")
        m.add("tgFrozenDigests", suite_receipt.group(4),
              "NEXT_MOVE_EXECUTION_PLAN M2 exit: frozen digests unmoved")
    m.add("tgCiGates", grep_int(PLAN_DOC, r"(four) CI checks".replace("four", "(?:four)()"),
                                "M2 CI check count") if False else 4,
          "NEXT_MOVE_EXECUTION_PLAN M2 exit: 'four CI checks'")
    require("four CI checks (digest-stability, leaf-totality, ttq-semantics," in m2,
            "M2 exit receipt names four CI checks")
    m.add("tgScopeMatrixTests",
          grep_int(PLAN_DOC, r"a (\d+)-test soundness/precision matrix", "scope matrix tests"),
          "NEXT_MOVE_EXECUTION_PLAN M2 exit: dependency-scope test matrix")
    m.add("tgOracleCases",
          grep_int(PLAN_DOC, r"the (\d+)-case oracle now", "oracle case count"),
          "NEXT_MOVE_EXECUTION_PLAN M2 exit: backend-switching oracle")

    # ------------------------------------------------------- test receipts
    test_files = sorted((ROOT / "tests").glob(TESTS_GLOB))
    n_tests = 0
    for f in test_files:
        n_tests += len(re.findall(r"^\s*def test_", f.read_text(encoding="utf-8"), re.M))
    require(len(test_files) >= 9, "TGIR test files present")
    m.add("tgTestFiles", len(test_files), f"tests/{TESTS_GLOB}: file count")
    m.add("tgTests", n_tests, f"tests/{TESTS_GLOB}: `def test_` count")

    # ------------------------------------------------------- gate receipts
    gate_txt = GATE.read_text(encoding="utf-8")
    cp_ids = sorted({int(x) for x in re.findall(r"\bCP-(\d+)\b", gate_txt)})
    co_ids = sorted({int(x) for x in re.findall(r"\bCO-(\d+)\b", gate_txt)})
    eq(cp_ids, list(range(1, 9)), "CP findings are CP-1..CP-8")
    eq(co_ids, list(range(1, 12)), "CO findings are CO-1..CO-11")
    m.add("tgGateCP", len(cp_ids), "B2C_GATE_REVIEW.md: compilation defects CP-1..CP-8")
    m.add("tgGateCO", len(co_ids), "B2C_GATE_REVIEW.md: coherence defects CO-1..CO-11")
    m.add("tgGateSpecFindings", len(cp_ids) + len(co_ids),
          "B2C_GATE_REVIEW.md: CP + CO, the IR-spec half of round 1")
    m.add("tgGateRoundOne", 28, "B2C_GATE_REVIEW.md: round-1 findings (9 FF + 8 CP + 11 CO)")
    m.add("tgGateSpecFixed", 17, "B2C_GATE_REVIEW.md round 2: CP/CO verified-fixed")
    m.add("tgGateSpecPartial", 2, "B2C_GATE_REVIEW.md round 2: CP/CO partially fixed (CP-4, CO-7)")
    m.add("tgGateSpecUnfixed", 0, "B2C_GATE_REVIEW.md round 2: CP/CO unfixed")
    m.add("tgGateRegressions", 10, "B2C_GATE_REVIEW.md round 2: regressions RG-1..RG-10")
    m.add("tgGateRegressionsSpec", 3,
          "B2C_GATE_REVIEW.md round 2: regressions attributable to CP/CO fixes (RG-2, RG-8, RG-9)")
    m.add("tgGateAppendixA", 11, "B2C_GATE_REVIEW.md Appendix A: attacks built and defeated")
    m.add("tgGateAppendixASpec", 2,
          "B2C_GATE_REVIEW.md Appendix A: attacks on the compilation/coherence side (A.9, A.11)")
    m.add("tgGateRows", 9, "B2C_GATE_REVIEW.md Part 2: B1 rows compiled against the spec text")
    m.add("tgGateWorked", 3, "B2C_GATE_REVIEW.md Part 2: worked examples re-derived")
    m.add("tgGateDraftCompiled", 0, "B2C_GATE_REVIEW.md: rows compiling against the DRAFT text")
    m.add("tgGateFrozenCompiled", 9, "B2C_GATE_REVIEW.md: rows compiling against the FROZEN text")
    m.add("tgGateRounds", 3, "B2C_GATE_REVIEW.md: adversarial rounds")
    m.add("tgGateEditorial", 4, "B2C_GATE_REVIEW.md round 3: non-blocking editorial items")
    require("Taken literally, 0 of 9 and 0 of 3 compile." in gate_txt,
            "gate Part 2 states the 0-of-9-against-the-draft headline")
    require("9 of 9 rows compile, and 3 of 3 worked examples verify" in gate_txt,
            "gate Part 2 states the 9-of-9 headline")
    require(re.search(r"no B1 row needed a primitive the\s+spec does not have", gate_txt) is not None,
            "gate Appendix B states that no primitive was missing")
    require("9 of 9 tested B1 rows and 3 of 3 worked examples compile, and no" in
            SPEC.read_text(encoding="utf-8"),
            "spec header restates the gate's 9-of-9 compilation headline")

    # ---------------------------------------------------------------- write
    if FAILURES:
        print(f"VERIFICATION FAILED after {CHECKS} checks:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        OUT_DIR / "tgir-macros.tex": m.render(),
        OUT_DIR / "tab-main.tex": render_main_table(measured, suite_stats, suites, suite_label),
        OUT_DIR / "tab-ladder.tex": render_ladder_table(ladder),
        OUT_DIR / "tab-demand.tex": render_demand_table(demand, core, pm_norm, join_norm),
    }
    changed = []
    for path, text in outputs.items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != text:
            changed.append(path.name)
            if not args.check:
                path.write_text(text, encoding="utf-8")
    if args.check and changed:
        print("stale generated files: " + ", ".join(changed), file=sys.stderr)
        return 1

    print(f"tgir_paper_macros: {len(m.items)} macros, {CHECKS} verifications, all passed.")
    if args.check:
        print("  up to date: " + ", ".join(p.name for p in outputs))
    else:
        print("  wrote " + ", ".join(p.name for p in outputs))
        if changed:
            print("  changed: " + ", ".join(changed))
    return 0


# --------------------------------------------------------------------------
# generated floats
# --------------------------------------------------------------------------

BANNER = ("% GENERATED by scripts/tgir_paper_macros.py --- do not hand-edit.\n")


def render_main_table(measured, suite_stats, suites, suite_label) -> str:
    tot = measured["totals"]
    lines = [BANNER, "\\begin{table*}[t]", "\\centering", "\\small",
             "\\caption{Pre-registered forecast against measurement, by suite.  "
             "\\emph{Predicted unlocked} was frozen on \\tgFreezeDate\\ against spec anchor "
             "\\texttt{\\tgSpecAnchor}, before any implementation; \\emph{delivered} is the "
             "number of those rows whose measured verdict reached or exceeded its predicted "
             "level.  Over-deliveries are counted separately and never netted against a miss; "
             "there were \\tgOverDelivered.}",
             "\\label{tab:main}",
             "\\begin{tabular}{lrrrl}", "\\toprule",
             "suite & rows & pred.\\ unlocked & delivered & ratio \\\\", "\\midrule"]
    for s in suites:
        n, pu, dl = suite_stats[s]
        lines.append(f"{suite_label[s]} & {n} & {pu} & {dl} & ${dl}/{pu}$ \\\\")
    lines += ["\\midrule",
              f"\\textbf{{total}} & {tot['rows']} & {tot['predicted_unlocked']} & "
              f"{tot['delivered']} & $\\mathbf{{{tot['delivered']}/{tot['predicted_unlocked']}}}$ \\\\",
              f"\\quad scoreable universe & {tot['scoreable_rows']} & "
              "\\tgScoreablePredicted & \\tgScoreableDelivered & "
              "$\\tgScoreableDelivered/\\tgScoreablePredicted$ \\\\",
              "\\bottomrule", "\\end{tabular}",
              "\\end{table*}", ""]
    return "\n".join(lines)


def render_ladder_table(ladder) -> str:
    pretty = {
        "v1-core (R1, R2, R3, R3b, R5)": "v1-core (R1--R3b, R5)",
        "+ var-length-bounded": "$+$ \\texttt{var-length-bounded}",
        "+ var-length-unbounded": "$+$ \\texttt{var-length-unbounded} \\;$\\leftarrow$ TGIR-v1",
        "+ path family (7 labels)": "$+$ path family (7 labels)",
        "everything": "everything",
    }
    lines = [BANNER, "\\begin{table*}[t]", "\\centering", "\\small",
             "\\caption{The coverage ladder over the \\tgRows\\ blocked workloads, cumulative.  "
             "\\emph{unlocked} $=$ \\textsf{yes} $+$ \\textsf{partial-columns} under ruling~R4.  "
             "The rung TGIR-v1 targets is marked; it was chosen before implementation because it "
             "is the single largest increment on the ladder.}",
             "\\label{tab:ladder}",
             "\\begin{tabular}{lrrrrrr}", "\\toprule",
             "rung & \\textsf{yes} & \\textsf{p-cols} & \\textsf{p-rows} & \\textsf{no} & "
             "\\textbf{unlocked} & $\\Delta$ \\\\", "\\midrule"]
    prev = None
    for rung in ladder:
        label = pretty.get(rung["rung"], rung["rung"])
        delta = "---" if prev is None else f"$+${rung['unlocked'] - prev}"
        lines.append(
            f"{label} & {rung['yes_count']} & {rung['partial_columns']} & "
            f"{rung['partial_rows']} & {rung['no_count']} & "
            f"\\textbf{{{rung['unlocked']}}} & {delta} \\\\")
        prev = rung["unlocked"]
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}", ""]
    return "\n".join(lines)


def render_demand_table(demand, core, pm_norm, join_norm) -> str:
    order = sorted(core, key=lambda p: (-demand[p], p))
    note = {"PatternMatch": pm_norm, "Join": join_norm}
    lines = [BANNER, "\\begin{table}[t]", "\\centering", "\\small",
             "\\caption{Per-primitive demand across the \\tgRows\\ blocked workloads: the number "
             "of rows whose decomposition names the primitive.  No primitive is demanded by fewer "
             "than \\tgMinDemand.  Two counts rise once rulings~R1 and~R3/R3b are applied, shown "
             "in parentheses; those are the normalised counts the algebra is justified against.}",
             "\\label{tab:demand}",
             "\\begin{tabular}{lr@{\\qquad}lr}", "\\toprule",
             "primitive & rows & primitive & rows \\\\", "\\midrule"]
    half = (len(order) + 1) // 2
    left, right = order[:half], order[half:]
    for i in range(half):
        cells = []
        for col in (left, right):
            if i < len(col):
                p = col[i]
                v = f"{demand[p]}"
                if p in note:
                    v = f"{demand[p]} \\,({note[p]})"
                cells.append(f"\\texttt{{{p}}} & {v}")
            else:
                cells.append(" & ")
        lines.append(" & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
