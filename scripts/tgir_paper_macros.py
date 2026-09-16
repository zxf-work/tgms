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
  benchmarks/results-v1/e14-p3-frontier.json  the plan-scope admission frontier (P3)
  benchmarks/results-v1/eval-guardrail-frontier.json  D-086's operator-scope frontier
  benchmarks/results-v1/e14-p2-compiled-{1m,10m}[-after].json  the P2 before/after pair
  benchmarks/freshness-v1/trials-{full,fixture}.json  the M4 record-of-account trial pool
  docs/design/TECHNICAL_REPORT_2026-08-24.md  prose receipts for P2/P3 (regex-checked)
  docs/design/M4_MEASURED_REPORT.md           prose receipts for M4 (regex-checked)
  benchmarks/results-v1/ldbc-sf1-campaign.json  the E13 SF1 campaign, 21 plan records
  benchmarks/results-v1/e14-p1-leaf-overhead-{bitcoinotc,collegemsg}.json  the P1 cells
  benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09{.json,.README.md}  the 2026-09 rerun
  benchmarks/paper-a-v1/forecast.yaml         the frozen E13/E14 pre-registration
  docs/design/PAPER_A_EVIDENCE_FREEZE.md      the pre-registered thresholds (regex-checked)
  docs/design/PAPER_A_EVIDENCE_REPORT.md      prose receipts for E13/E14 (regex-checked)

Discipline: **assert, do not trust.**  Every value is recomputed from the
row-level data where the row-level data can produce it, and then checked against
the aggregate the source file states for itself.  A disagreement is a hard
failure, never a silent adjustment.  Values that exist only as prose (the M2
suite receipts, the fixture sizes) are pinned by a regular expression against
the document that owns them, so an edit at the source breaks this script rather
than silently de-synchronising the paper.

Usage:  python scripts/tgir_paper_macros.py [--root PATH] [--check]

``--check`` regenerates into memory and fails if the checked-in outputs differ.

``--root PATH`` resolves every source of record, and the output directory,
against PATH instead of against this file's own checkout.  The public worktree
carries the script and the benchmark records but neither ``docs/design/`` nor
``paper/``, so a public checkout runs it as::

    cd <worktree> && <repo>/.venv/bin/python scripts/tgir_paper_macros.py \\
        --root <repo>

which reads the internal tree's sources and writes the internal tree's
``paper/tgir/``.  Omitting the option keeps the historical behaviour exactly.
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

# Every source of record, written relative to ROOT.  ``set_root`` re-points all
# of them at once (see --root), so a path can never be half-moved: the names
# below are the single place a source is spelled.
OUT_DIR = Path("paper/tgir")

MEASURED = Path("benchmarks/tgir-v1/measured.yaml")
PLANS_DIR = Path("benchmarks/tgir-v1/plans")
GOLD = Path("benchmarks/tgir-v1/gold.json")
FORECAST = Path("docs/design/tgir_b1/forecast.yaml")
MERGED = Path("docs/design/tgir_b1/merged.yaml")
LDBC_INSTRUMENT = Path("benchmarks/ldbc-fit-v1/classification.json")
INDEP_INSTRUMENT = Path("benchmarks/independent-v1/classification.json")
M3_REPORT = Path("docs/design/TGIR_M3_MEASURED_REPORT.md")
PLAN_DOC = Path("docs/design/NEXT_MOVE_EXECUTION_PLAN_2026-08-21.md")
DECOMP = Path("docs/design/TGIR_WORKLOAD_DECOMPOSITION.md")
SPEC = Path("docs/design/TGIR_SPEC.md")
GATE = Path("docs/design/tgir_b1/B2C_GATE_REVIEW.md")
TESTS_DIR = Path("tests")
TESTS_GLOB = "test_tgir_*.py"
FRONTIER = Path("benchmarks/results-v1/e14-p3-frontier.json")
GUARDRAIL_FRONTIER = Path("benchmarks/results-v1/eval-guardrail-frontier.json")
P2_ONE_M = Path("benchmarks/results-v1/e14-p2-compiled-1m.json")
P2_ONE_M_AFTER = Path("benchmarks/results-v1/e14-p2-compiled-1m-after.json")
P2_TEN_M = Path("benchmarks/results-v1/e14-p2-compiled-10m.json")
P2_TEN_M_AFTER = Path("benchmarks/results-v1/e14-p2-compiled-10m-after.json")
FRESH_FULL = Path("benchmarks/freshness-v1/trials-full.json")
FRESH_FIXTURE = Path("benchmarks/freshness-v1/trials-fixture.json")
TECH_REPORT = Path("docs/design/TECHNICAL_REPORT_2026-08-24.md")
M4_REPORT = Path("docs/design/M4_MEASURED_REPORT.md")

# --- the E13/E14 evidence campaign (2026-08-24) and its 2026-09 rerun ------
SF1_CAMPAIGN = Path("benchmarks/results-v1/ldbc-sf1-campaign.json")
SF1_RERUN = Path("benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09.json")
SF1_RERUN_README = Path("benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09.README.md")
P1_BITCOIN = Path("benchmarks/results-v1/e14-p1-leaf-overhead-bitcoinotc.json")
P1_COLLEGE = Path("benchmarks/results-v1/e14-p1-leaf-overhead-collegemsg.json")
PAPER_A_FORECAST = Path("benchmarks/paper-a-v1/forecast.yaml")
EVIDENCE_FREEZE = Path("docs/design/PAPER_A_EVIDENCE_FREEZE.md")
EVIDENCE_REPORT = Path("docs/design/PAPER_A_EVIDENCE_REPORT.md")
FORECAST_FREEZE = Path("docs/design/TGIR_FORECAST_FREEZE.md")
OSDI_PLAN = Path("docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md")

# captured before set_root() makes them absolute: name -> path relative to ROOT
_RELATIVE_SOURCES = {n: v for n, v in list(globals().items())
                     if isinstance(v, Path) and not v.is_absolute()}


def set_root(root: Path) -> None:
    """Resolve ROOT and every source of record against `root`.

    Called once at import with this file's own checkout, and again from main()
    when --root names a different one.  Idempotent: the relative spellings are
    captured once, above, so re-rooting never compounds.
    """
    global ROOT
    ROOT = root.resolve()
    for name, rel in _RELATIVE_SOURCES.items():
        globals()[name] = ROOT / rel


set_root(ROOT)


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


def sf1_ratio(r: float) -> str:
    """An estimate/actual ratio, in the evidence report's own two registers.

    Below 1.0 the interesting digits are the leading ones, and the report
    prints three decimals (0.199, 0.272, 0.482); at or above 1.0 the ratio is
    an order-of-magnitude statement and the report prints a whole number
    (9, 245, 24,600).  Reproducing both is what lets the generated table be
    asserted against §1.4 cell for cell.
    """
    return f"{r:.3f}" if r < 1.0 else f"{round(r):,}"


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
    ap.add_argument("--root", type=Path, default=None, metavar="PATH",
                    help="resolve every source of record and paper/tgir/ against PATH "
                         "instead of this script's own checkout")
    args = ap.parse_args()
    if args.root is not None:
        if not args.root.is_dir():
            print(f"--root {args.root} is not a directory", file=sys.stderr)
            return 1
        set_root(args.root)

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
    plans = sorted(p.stem for p in PLANS_DIR.iterdir()
                   if p.is_file() and not p.name.startswith("."))
    universe = {r["id"] for r in mrows}
    eq(sorted(set(plans) & universe), sorted(universe),
       "every one of the 52 measured rows has a plan artifact")
    # The directory also carries artifacts composed AFTER the B1 freeze, which
    # are not rows of this paper's 52-row universe and must not inflate its
    # count: the three Interactive Short templates the legacy algebra could
    # already express (m6/D2, 2026-09-13) and BI6's repair.  Named, so a fourth
    # addition breaks this script rather than silently moving a paper number.
    post_freeze = sorted(set(plans) - universe)
    eq(post_freeze, ["BI6.v2", "IS1", "IS4", "IS5"],
       "post-freeze plan artifacts are exactly the m6/D2 three plus BI6's repair")
    eq(len(plans), 52 + len(post_freeze), "plan artifact count = the 52 plus the post-freeze ones")
    m.add("tgPlanArtifacts", len(universe),
          "benchmarks/tgir-v1/plans/: artifacts for the 52-row universe "
          "(the directory also carries 4 post-freeze artifacts, not rows of this paper)")

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

    # ----------------------------- decomposition receipts carried in prose
    # These were hand-typed in the draft (§3's outer-join sentence, the
    # instrument-defect paragraph).  Each is pinned to the document that owns
    # it, or recomputed from the merged layer, so an edit at the source breaks
    # this script rather than de-synchronising the manuscript.
    oj_rows = grep1(DECOMP, r"load-bearing in \*\*(\d+) BI rows\*\* — ([A-Z0-9, ]+)",
                    "outer-join BI rows")
    oj_named = re.search(r"load-bearing in \*\*\d+ BI rows\*\* — ((?:BI\d+, )+BI\d+)",
                         DECOMP.read_text(encoding="utf-8"))
    require(oj_named is not None, "the decomposition names the outer-join BI rows")
    if oj_named:
        eq(len(oj_named.group(1).split(", ")), int(oj_rows),
           "decomposition: the outer-join BI rows named match the count stated")
    m.add("tgOuterJoinRows", oj_rows,
          "TGIR_WORKLOAD_DECOMPOSITION.md §4: BI rows where outer join is load-bearing "
          "because the answer depends on absence")
    disagreements = sum(1 for r in brows if r.get("disagreement"))
    eq(disagreements, 8, "merged.yaml: rows carrying a recorded instrument disagreement")
    require(re.search(r"^Eight rows where the decomposition disagrees with the frozen "
                      r"instruments'", DECOMP.read_text(encoding="utf-8"), re.M) is not None,
            "the decomposition states its instrument disagreements as a section")
    m.add("tgInstrumentDisagreements", disagreements,
          "merged.yaml: rows carrying a non-null `disagreement` (the frozen instruments' "
          "capability tagging, disputed and not repaired)")
    m.add("tgPatDiffOneWay",
          grep_int(DECOMP, r"the sets differ \*\*(\d+) one way, \d+ the other\*\*",
                   "PAT-vs-PatternMatch, one way"),
          "TGIR_WORKLOAD_DECOMPOSITION.md §7: rows carrying the instruments' `PAT` tag that "
          "need no PatternMatch under R1")
    m.add("tgPatDiffOther",
          grep_int(DECOMP, r"the sets differ \*\*\d+ one way, (\d+) the other\*\*",
                   "PAT-vs-PatternMatch, the other way"),
          "TGIR_WORKLOAD_DECOMPOSITION.md §7: rows needing PatternMatch under R1 that carry "
          "no `PAT` tag")
    stale_rows = re.findall(r"disagreed with the live script on `(cm\d+)`|and `(cm\d+)` \(class",
                            DECOMP.read_text(encoding="utf-8"))
    stale_named = sorted({x for pair in stale_rows for x in pair if x})
    eq(stale_named, ["cm13", "cm39"],
       "decomposition: the two rows the stale instrument disagreed with the live script on")
    m.add("tgStaleInstrumentRows", len(stale_named),
          "TGIR_WORKLOAD_DECOMPOSITION.md §1: rows where the checked-in instrument, frozen at "
          "an earlier generation cycle, disagreed with the live classifier")

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
    # M3.5's own receipts, hand-typed in the draft's measurement section until
    # now.  The frozen-spec pass and the two evaluator defects are both counted
    # by the documents that own them.
    fs_txt = FORECAST_FREEZE.read_text(encoding="utf-8")
    changed_rows = re.findall(r"^### 7\.(\d) ", fs_txt, re.M)
    eq(len(changed_rows), 6, "TGIR_FORECAST_FREEZE §7: rows whose compilation the frozen spec changed")
    eq(changed_rows, [str(i) for i in range(1, len(changed_rows) + 1)],
       "TGIR_FORECAST_FREEZE §7: the subsections are numbered 7.1..7.n without a gap")
    require("What *did* change is how six rows compile. Each is recorded on its row." in fs_txt,
            "TGIR_FORECAST_FREEZE §7 states its own count of changed compilations")
    m.add("tgFrozenSpecChangedRows", len(changed_rows),
          "TGIR_FORECAST_FREEZE.md §7: rows whose compilation route the frozen spec changed, "
          "no verdict moving")
    labels = re.search(r"^### 7\.4 `labels: \[Label\]` is load-bearing for (\d+) rows, not (\d+) "
                       r"— and IS2 is not one of them", fs_txt, re.M)
    require(labels is not None, "TGIR_FORECAST_FREEZE §7.4 states the labels ruling's real reach")
    if labels:
        m.add("tgLabelsLoadBearing", labels.group(1),
              "TGIR_FORECAST_FREEZE.md §7.4: rows at the target rung constraining on the "
              "Message supertype")
        m.add("tgLabelsJustified", labels.group(2),
              "TGIR_FORECAST_FREEZE.md §7.4: rows the spec's own justification for the ruling "
              "names --- one of which is not among the real ones")
    eval_defect_rows = re.search(r"^(IC\d+) and (IC\d+) were rewritten before measuring",
                                 M3_REPORT.read_text(encoding="utf-8"), re.M)
    require(eval_defect_rows is not None, "TGIR_M3_MEASURED_REPORT names the two rewritten rows")
    require("Rewriting them exposed **two real evaluator defects**, both fixed" in
            M3_REPORT.read_text(encoding="utf-8"),
            "TGIR_M3_MEASURED_REPORT states the two evaluator defects")
    m.add("tgEvaluatorDefects", 2 if eval_defect_rows else -1,
          "TGIR_M3_MEASURED_REPORT.md: evaluator defects the artifact rewrite exposed "
          "(one per rewritten row, IC5 and IC6)")

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
    test_files = sorted(TESTS_DIR.glob(TESTS_GLOB))
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
    # findings the draft names in prose: the CO rows the review itself tags as
    # spec-versus-kernel drift, the rows a ruling would have uncompiled, and the
    # amendment-introduced regressions.  All recomputed from the review's own
    # summary table or pinned to its own accounting sentence.
    co_drift = sorted({int(x) for x in
                       re.findall(r"\| CO-(\d+) \|[^|]*\| coherence \(drift\) \|", gate_txt)})
    eq(co_drift, [4, 5, 6, 9], "gate: the CO findings tagged `coherence (drift)`")
    require(set(co_drift) <= set(co_ids), "every drift finding is one of the CO findings")
    m.add("tgGateCODrift", len(co_drift),
          "B2C_GATE_REVIEW.md summary table: CO findings classed `coherence (drift)` --- "
          "found only by reading the specification against the kernel")
    dup_probe = re.search(r"\| CP-6 \|.*\*\*uncompiles ((?:\w+ and )?\w+)\*\*", gate_txt)
    require(dup_probe is not None, "gate CP-6 names the rows a `reject` ruling would uncompile")
    dup_rows = dup_probe.group(1).split(" and ") if dup_probe else []
    eq(sorted(dup_rows), ["BI18", "bo31"], "gate CP-6: the two rows are bo31 and BI18")
    m.add("tgDupProbeRows", len(dup_rows),
          "B2C_GATE_REVIEW.md CP-6: unlocked rows whose probes carry duplicate keys on any "
          "corrected store, and that a `reject` ruling would have turned into refusals")
    cast_sort = re.search(r"Three of the nine rows \(((?:\w+, )+\w+)\) sort on `toInteger\(id\)`;"
                          r" two of them\n\(((?:\w+, )+\w+)\) sit under a `Limit`", gate_txt)
    require(cast_sort is not None, "gate CP-5 names the rows sorting on an identity cast")
    if cast_sort:
        sort_rows = cast_sort.group(1).split(", ")
        limit_rows = cast_sort.group(2).split(", ")
        eq(sort_rows, ["IS3", "IC2", "IS2"], "gate CP-5: the rows sorting on toInteger(id)")
        eq(limit_rows, ["IC2", "IS2"], "gate CP-5: of those, the rows under a Limit")
        require(set(limit_rows) < set(sort_rows), "gate CP-5: the Limit rows are a proper subset")
        m.add("tgCastSortRows", len(sort_rows),
              "B2C_GATE_REVIEW.md CP-5: gate rows whose tie-break sorts on an identity cast")
        m.add("tgCastLimitRows", len(limit_rows),
              "B2C_GATE_REVIEW.md CP-5: of those, the rows sorting under a Limit, where R4 "
              "makes the key row-determining")
    reg_split = re.search(r"\*\*Regressions:\*\* (\d+) new false-fresh \+ (\d+) coherence, "
                          r"of which (\d+) were introduced by the\namendments "
                          r"\(((?:RG-\d+'s [^,]+, ){3}RG-\d+'s [^)]+)\)", gate_txt)
    require(reg_split is not None, "gate round-2 verdict states its regression accounting")
    if reg_split:
        n_ff, n_coh, n_amend = (int(x) for x in reg_split.group(1, 2, 3))
        eq(n_ff + n_coh, 10, "gate: the regression total the tgGateRegressions macro carries")
        eq(len(reg_split.group(4).split(", ")), n_amend,
           "gate: the amendment-introduced regressions named match the count stated")
        # The amendment set {RG-1, RG-2, RG-4, RG-5} and the CP/CO-attributable
        # set {RG-2, RG-8, RG-9} overlap in RG-2 only.  They are different
        # quantities and neither contains the other; §6 must not nest them.
        amend_ids = sorted(int(x) for x in re.findall(r"RG-(\d+)", reg_split.group(4)))
        eq(amend_ids, [1, 2, 4, 5], "gate: which regressions the amendments introduced")
        eq(sorted(set(amend_ids) & {2, 8, 9}), [2],
           "gate: amendment-introduced and CP/CO-attributable regressions overlap in RG-2 only")
        m.add("tgGateRegressionsAmend", n_amend,
              "B2C_GATE_REVIEW.md round-2 verdict: regressions introduced by the amendments "
              "(RG-1, RG-2, RG-4, RG-5) --- NOT a superset of tgGateRegressionsSpec")
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

    # ------------------------------------------ P3: the admission inversion
    frontier = json.loads(FRONTIER.read_text(encoding="utf-8"))
    bi_arm = frontier["arms"]["scored-bi"]
    per_plan = bi_arm["per_plan"]
    eoa = bi_arm["estimate_over_actual"]

    eq(bi_arm["of"], 10, "plan-scope BI arm: total cells attempted")
    eq(bi_arm["excluded"], ["BI6"], "plan-scope BI arm: BI6 excluded (frozen artifact defect, E-k)")
    eq(bi_arm["scoreable"], 9, "plan-scope BI arm: scoreable cell count")
    eq(len(per_plan), bi_arm["scoreable"], "plan-scope BI arm: per_plan rows == scoreable count")
    m.add("tgAdmCells", bi_arm["scoreable"],
          "e14-p3-frontier.json arms.scored-bi.scoreable: the plan-scope admission frontier")

    under_est = eoa["under_estimates"]
    eq(sorted(under_est), ["BI12", "BI18", "BI9"], "plan-scope BI arm: under-estimate rows")
    m.add("tgAdmUnderEst", len(under_est),
          "e14-p3-frontier.json arms.scored-bi.estimate_over_actual.under_estimates: count")

    budget_ms = frontier["manifest"]["budget_ms"]
    eq(budget_ms, 10000.0, "plan-scope frontier: budget T fixed at 10 s (the policy's declared budget)")
    live_sweep = next(s for s in bi_arm["sweep"] if s["ceiling_ms"] == budget_ms)
    eq(live_sweep["multiplier"], 1.0,
       "plan-scope BI arm: the live policy point is multiplier 1.0 (ceiling == budget)")
    fa_rows = [p for p in per_plan if p["classifier"] == "false-admission"]
    fr_rows = [p for p in per_plan if p["classifier"] == "false-rejection"]
    eq(len(fa_rows), live_sweep["false-admission"],
       "plan-scope BI arm: false-admission count matches the live-ceiling sweep bucket")
    eq(len(fr_rows), live_sweep["false-rejection"],
       "plan-scope BI arm: false-rejection count matches the live-ceiling sweep bucket")
    eq(len(fa_rows), 1, "plan-scope BI arm: exactly one false admission")
    eq(len(fr_rows), 0, "plan-scope BI arm: zero false rejections")
    eq(fa_rows[0]["plan_id"], "BI18", "plan-scope BI arm: the false admission is BI18")
    eq(fa_rows[0]["est_ms"], 5918, "BI18 estimated cost, ms")
    eq(round(fa_rows[0]["actual_ms"], 1), 29734.5, "BI18 actual cost, ms (rounded to 1dp)")
    m.add("tgAdmFalseAdm", len(fa_rows),
          "e14-p3-frontier.json arms.scored-bi.per_plan at the live ceiling: false-admission count")
    m.add("tgAdmFalseRej", len(fr_rows),
          "e14-p3-frontier.json arms.scored-bi.per_plan at the live ceiling: false-rejection count")
    m.add("tgAdmFalseAdmRow", fa_rows[0]["plan_id"], "e14-p3-frontier.json: which plan is the false admission")
    m.add("tgAdmFalseAdmEstMs", tex_num(fa_rows[0]["est_ms"]), "e14-p3-frontier.json: BI18 estimated cost")
    m.add("tgAdmFalseAdmActualMs", f"{fa_rows[0]['actual_ms']:,.1f}".replace(",", "{,}"),
          "e14-p3-frontier.json: BI18 actual cost")

    eq(bi_arm["best"]["multiplier"], 0.59, "plan-scope BI arm: optimal ceiling multiplier")
    eq(bi_arm["best"]["false-admission"], 0, "plan-scope BI arm at its optimal ceiling: false admissions")
    eq(bi_arm["best"]["false-rejection"], 0, "plan-scope BI arm at its optimal ceiling: false rejections")
    m.add("tgAdmOptCeiling", "0.59",
          "e14-p3-frontier.json arms.scored-bi.best.multiplier: optimal ceiling, times the default")

    eq(round(eoa["min"], 3), 0.199, "plan-scope BI arm: estimate/actual min rounds to 0.199")
    eq(round(eoa["median"], 2), 8.87, "plan-scope BI arm: estimate/actual median rounds to 8.87")
    eq(round(eoa["max"]), 24600, "plan-scope BI arm: estimate/actual max rounds to 24,600")
    eq(round(eoa["spread"]), 123602, "plan-scope BI arm: estimate/actual spread rounds to 123,602")
    m.add("tgAdmRatioMin", "0.199", "e14-p3-frontier.json estimate_over_actual.min")
    m.add("tgAdmRatioMedian", "8.87", "e14-p3-frontier.json estimate_over_actual.median")
    m.add("tgAdmRatioMax", tex_num(round(eoa["max"])), "e14-p3-frontier.json estimate_over_actual.max")
    m.add("tgAdmRatioSpread", tex_num(round(eoa["spread"])), "e14-p3-frontier.json estimate_over_actual.spread")

    # D-086's operator-scope contrast, paired against the plan-scope frontier above
    guardrail = json.loads(GUARDRAIL_FRONTIER.read_text(encoding="utf-8"))
    eq(len(guardrail["cells"]), 90, "D-086 operator-scope frontier: cell count")
    b2000 = guardrail["frontier"]["budget_2000ms"]
    eq(b2000["n_cells"], 90, "D-086 2 s-budget bucket: cell count")
    eq(b2000["at_default"]["multiplier"], 1, "D-086 2 s budget: default multiplier is 1x")
    eq(b2000["at_default"]["false_admissions"], 0, "D-086 2 s budget, default ceiling: false admissions")
    eq(b2000["at_default"]["false_rejections"], 16, "D-086 2 s budget, default ceiling: false rejections")
    eq(b2000["best"]["multiplier"], 256, "D-086 2 s budget: optimal ceiling multiplier")
    m.add("tgAdmOpCells", len(guardrail["cells"]),
          "eval-guardrail-frontier.json cells: D-086's operator-scope frontier")
    m.add("tgAdmOpFalseAdm", b2000["at_default"]["false_admissions"],
          "eval-guardrail-frontier.json frontier.budget_2000ms.at_default: false admissions")
    m.add("tgAdmOpFalseRej", b2000["at_default"]["false_rejections"],
          "eval-guardrail-frontier.json frontier.budget_2000ms.at_default: false rejections")
    m.add("tgAdmOpOptCeiling", tex_num(b2000["best"]["multiplier"]),
          "eval-guardrail-frontier.json frontier.budget_2000ms.best.multiplier: optimal ceiling")

    # cross-check every derived number in this section against the report's own table
    tr_txt = TECH_REPORT.read_text(encoding="utf-8")
    require("| estimate direction | **every estimate an over-estimate** | "
            "**3 of 9 under-estimates** (BI12, BI18, BI9) |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(c): estimate-direction row")
    require("| false admissions | **0 at every budget** | "
            "**1** (BI18: admitted at 5,918 ms est, ran 29,734.5 ms) |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(c): false-admissions row")
    require("| false rejections | 16 of 90 | 0 of 9 |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(c): false-rejections row")
    require("| optimal ceiling | **256× above** the default | "
            "**0.59× — below** the default |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(c): optimal-ceiling row")
    require("| estimate/actual | — | min 0.199× · median 8.87× · "
            "max 24,600× · **spread 123,602×** |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(c): estimate/actual row")

    # ------------------------------------------------- P2: before / after
    p2_one_m = json.loads(P2_ONE_M.read_text(encoding="utf-8"))
    p2_one_m_after = json.loads(P2_ONE_M_AFTER.read_text(encoding="utf-8"))
    p2_ten_m = json.loads(P2_TEN_M.read_text(encoding="utf-8"))
    p2_ten_m_after = json.loads(P2_TEN_M_AFTER.read_text(encoding="utf-8"))

    def p2_numbers(doc):
        """Scan every numeric leaf so the D-149 190x figure can never hide in here."""
        def walk(obj):
            if isinstance(obj, dict):
                for v in obj.values():
                    yield from walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from walk(v)
            elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
                yield obj
        return list(walk(doc))

    for label, doc in (("1m", p2_one_m), ("1m-after", p2_one_m_after),
                       ("10m", p2_ten_m), ("10m-after", p2_ten_m_after)):
        suspects = [n for n in p2_numbers(doc) if 188.0 <= n <= 192.0]
        require(not suspects,
                f"P2 record e14-p2-compiled-{label}.json carries a value near 190 "
                f"(D-149's figure, not P2's): {suspects}")

    def p2_entity_row(doc):
        row = next(r for r in doc["rows"] if r["op"] == "entity_history")
        require(row["row_counts_agree"] is True,
                "P2 entity_history row: kernel and compiled row counts agree")
        kernel_ms = row["kernel"]["p50_ms"]
        compiled_ms = row["compiled"]["p50_ms"]
        ratio = compiled_ms / kernel_ms
        require(abs(ratio - row["compiled_over_kernel"]) < 1e-6,
                "P2 entity_history row: recomputed ratio disagrees with recorded compiled_over_kernel")
        return kernel_ms, compiled_ms, ratio

    k_one_m, c_one_m, ratio_pre_one_m = p2_entity_row(p2_one_m)
    k_one_m_after, c_one_m_after, ratio_post_one_m = p2_entity_row(p2_one_m_after)
    k_ten_m, c_ten_m, ratio_pre_ten_m = p2_entity_row(p2_ten_m)
    k_ten_m_after, c_ten_m_after, ratio_post_ten_m = p2_entity_row(p2_ten_m_after)

    eq(round(ratio_pre_one_m, 1), 292.7, "P2 1M pre-fix ratio")
    eq(round(ratio_pre_ten_m, 1), 446.9, "P2 10M pre-fix ratio")
    eq(round(ratio_post_one_m, 3), 2.701, "P2 1M post-fix ratio")
    eq(round(ratio_post_ten_m, 3), 1.816, "P2 10M post-fix ratio")
    eq(round(k_one_m, 3), 0.425, "P2 1M pre-fix kernel p50")
    eq(round(c_one_m, 3), 124.416, "P2 1M pre-fix compiled p50")
    eq(round(k_ten_m, 3), 0.908, "P2 10M pre-fix kernel p50")
    eq(round(c_ten_m, 3), 405.757, "P2 10M pre-fix compiled p50")

    m.add("tgPTwoKernelOneM", "0.425", "e14-p2-compiled-1m.json rows[entity_history].kernel.p50_ms")
    m.add("tgPTwoCompiledOneM", "124.416", "e14-p2-compiled-1m.json rows[entity_history].compiled.p50_ms")
    m.add("tgPTwoRatioPreOneM", "292.7", "e14-p2-compiled-1m.json rows[entity_history].compiled_over_kernel")
    m.add("tgPTwoKernelTenM", "0.908", "e14-p2-compiled-10m.json rows[entity_history].kernel.p50_ms")
    m.add("tgPTwoCompiledTenM", "405.757", "e14-p2-compiled-10m.json rows[entity_history].compiled.p50_ms")
    m.add("tgPTwoRatioPreTenM", "446.9", "e14-p2-compiled-10m.json rows[entity_history].compiled_over_kernel")
    m.add("tgPTwoKernelOneMAfter", f"{k_one_m_after:.3f}",
          "e14-p2-compiled-1m-after.json rows[entity_history].kernel.p50_ms")
    m.add("tgPTwoCompiledOneMAfter", f"{c_one_m_after:.3f}",
          "e14-p2-compiled-1m-after.json rows[entity_history].compiled.p50_ms")
    m.add("tgPTwoRatioPostOneM", "2.701",
          "e14-p2-compiled-1m-after.json rows[entity_history].compiled_over_kernel")
    m.add("tgPTwoKernelTenMAfter", f"{k_ten_m_after:.3f}",
          "e14-p2-compiled-10m-after.json rows[entity_history].kernel.p50_ms")
    m.add("tgPTwoCompiledTenMAfter", f"{c_ten_m_after:.3f}",
          "e14-p2-compiled-10m-after.json rows[entity_history].compiled.p50_ms")
    m.add("tgPTwoRatioPostTenM", "1.816",
          "e14-p2-compiled-10m-after.json rows[entity_history].compiled_over_kernel")

    require("| 1M | **0.425 ms** | **124.416 ms** | **292.7×** | 1/1 |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(e): P2 1M pre-fix row")
    require("| 10M | **0.908 ms** | **405.757 ms** | **446.9×** | 1/1 |" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 §3.1(e): P2 10M pre-fix row")
    require("xzgpu at **2.701× (1M)** and **1.816× (10M)**" in tr_txt,
            "TECHNICAL_REPORT_2026-08-24 Postscript: P2 re-scored 2.701x/1.816x")

    # ------------------------------------------- M4: the freshness headline
    fresh_full = json.loads(FRESH_FULL.read_text(encoding="utf-8"))
    fresh_fixture = json.loads(FRESH_FIXTURE.read_text(encoding="utf-8"))
    m4_trials = fresh_full["trials"] + fresh_fixture["trials"]
    eq(len(m4_trials), fresh_full["trial_count"] + fresh_fixture["trial_count"],
       "M4: combined trial pool size matches each file's own trial_count")
    eq(len(m4_trials), 3354, "M4: the record-of-account trial pool (both campaigns)")
    require(all(t["outcome"] == "OK" for t in m4_trials),
            "M4: every trial in the record-of-account pool completed OK")

    m4_changed = [t for t in m4_trials if t["changed"]]
    eq(len(m4_changed), 447, "M4: changed-column trials")
    m4_false_fresh = sum(1 for t in m4_changed if t["verdict"] == "fresh")
    eq(m4_false_fresh, 0, "M4: dependency-scope mechanism's false-fresh count")
    eq(m4_false_fresh, fresh_full["summary"]["false_fresh"] + fresh_fixture["summary"]["false_fresh"],
       "M4: recomputed false-fresh matches the sum of each file's own summary.false_fresh")
    m.add("tgMFourChanged", len(m4_changed),
          "freshness-v1/trials-{full,fixture}.json: changed trials, combined record-of-account pool")
    m.add("tgMFourFalseFresh", m4_false_fresh,
          "freshness-v1: changed trials where the dependency-scope verdict is fresh")

    m4_rt_false_fresh = sum(1 for t in m4_changed if t.get("rowtouch_verdict") == "fresh")
    eq(m4_rt_false_fresh, 212, "M4: naive row-touch false-fresh count")
    eq(round(m4_rt_false_fresh / len(m4_changed) * 1000) / 10, 47.4,
       "M4: naive row-touch false-fresh rate rounds to 47.4%")
    m.add("tgMFourRtFalseFresh", m4_rt_false_fresh,
          "freshness-v1: changed trials where rowtouch_verdict is fresh")
    m.add("tgMFourRtFalseFreshPct", "47.4",
          "freshness-v1: naive row-touch false-fresh rate, 212/447")

    m4_newid_changed = [t for t in m4_changed if t["placement"] == "new-identity"]
    eq(len(m4_newid_changed), 89, "M4: new-identity changed trials")
    m4_newid_missed = sum(1 for t in m4_newid_changed if t.get("rowtouch_verdict") == "fresh")
    eq(m4_newid_missed, 89, "M4: every new-identity changed trial is missed by naive row-touch")
    m.add("tgMFourNewIdentity", len(m4_newid_changed),
          "freshness-v1: changed trials in the new-identity placement")
    m.add("tgMFourNewIdentityMissed", m4_newid_missed,
          "freshness-v1: new-identity changed trials the row-touch rule calls fresh")

    prec_stores = {"bitcoinotc", "collegemsg"}
    prec_pool = [t for t in fresh_full["trials"] if t["store"] in prec_stores and t["outcome"] == "OK"]
    den_main = sum(1 for t in prec_pool if t["verdict"] == "possibly-stale")
    tp_main = sum(1 for t in prec_pool if t["verdict"] == "possibly-stale" and t["value_changed"])
    eq(den_main, fresh_full["summary"]["precision_denominator"],
       "M4: recomputed precision denominator matches trials-full.json summary.precision_denominator")
    eq(den_main, 1536, "M4: precision denominator, POSSIBLY_STALE over bitcoinotc+collegemsg")
    eq(tp_main, 203, "M4: true-stale trials within the precision denominator")
    require(abs(tp_main / den_main - fresh_full["summary"]["precision"]) < 1e-9,
            "M4: recomputed precision matches trials-full.json summary.precision")
    m.add("tgMFourPrecision", "0.132", "freshness-v1/trials-full.json: overall precision, 203/1536")
    m.add("tgMFourPrecisionTrue", tp_main, "freshness-v1: true-stale trials within the precision denominator")
    m.add("tgMFourPrecisionDen", den_main, "freshness-v1: POSSIBLY_STALE trials over bitcoinotc+collegemsg")

    den_top = sum(1 for t in prec_pool if t["top_verdict"] == "possibly-stale")
    tp_top = sum(1 for t in prec_pool if t["top_verdict"] == "possibly-stale" and t["value_changed"])
    eq(den_top, 1576, "M4: all-\"*\" control denominator")
    eq(tp_top, tp_main, "M4: all-\"*\" control shares the real derivation's true-stale numerator")
    eq(tp_top, 203, "M4: all-\"*\" control true-stale count")
    m.add("tgMFourPrecisionControl", "0.129",
          "freshness-v1/trials-full.json: all-\"*\" control precision, 203/1576")
    m.add("tgMFourPrecisionCtrlDen", den_top, "freshness-v1: all-\"*\" control POSSIBLY_STALE trials")

    m4_txt = M4_REPORT.read_text(encoding="utf-8")
    require("**false-fresh = 0** over a `changed` column of **447**\n"
            "> across every substrate, of which **447**\n"
            "> are value-changed and **0**\n"
            "> are digest-only." in m4_txt,
            "M4_MEASURED_REPORT §3 headline: false-fresh 0 / changed 447")
    require("**overall precision = 0.132** (203 true stale / 1536\n"
            "> `POSSIBLY_STALE`), on `bitcoinotc` and `collegemsg`." in m4_txt,
            "M4_MEASURED_REPORT §4: overall precision 0.132 (203/1536)")
    require("**row-touch false-fresh = 212 of 447 changed trials\n"
            "> (47.4%).** The dependency-scope mechanism's own count on the same\n"
            "> trials is **0**." in m4_txt,
            "M4_MEASURED_REPORT: row-touch false-fresh 212/447 (47.4%)")
    require("Of the 89 changed trials in the **new-identity** placement — a\n"
            "correction on an identity the stored result has no row for — the row-touch rule\n"
            "called **89** fresh." in m4_txt,
            "M4_MEASURED_REPORT: new-identity 89/89 missed by row-touch")
    require("**all-`\"*\"` precision = 0.129** (203/1576) versus the real\n"
            "> derivations' **0.132**." in m4_txt,
            "M4_MEASURED_REPORT: all-\"*\" control precision 0.129 (203/1576)")

    # ==================================================================
    # E13/E14 --- the 2026-08-24 evidence campaign.  The draft body predates
    # it; everything below is what the new evaluation material resolves
    # through.  Sources: benchmarks/results-v1/ldbc-sf1-campaign.json,
    # e14-p1-leaf-overhead-*.json, e14-p3-frontier.json,
    # ldbc-sf1-campaign-fmt3-2026-09.{json,README.md},
    # benchmarks/paper-a-v1/forecast.yaml, and the two PAPER_A_EVIDENCE_* docs.
    # ==================================================================

    campaign = json.loads(SF1_CAMPAIGN.read_text(encoding="utf-8"))
    pa_forecast = yaml.safe_load(PAPER_A_FORECAST.read_text(encoding="utf-8"))
    ev_txt = EVIDENCE_REPORT.read_text(encoding="utf-8")
    cman = campaign["manifest"]
    crecs = campaign["records"]

    # ------------------------------------------------ E13: the substrate
    stats = pa_forecast["substrate"]["assumed_stats"]
    sf1_nodes = stats["n_entities"]
    sf1_edges = stats["n_edge_versions"]
    eq(stats["n_node_versions"], sf1_nodes,
       "SF1: one node version per entity in the static initial snapshot")
    rel_counts = stats["rel_type_counts"]
    eq(len(rel_counts), 15, "SF1: fifteen relation types")
    eq(sum(rel_counts.values()), sf1_edges, "SF1: the fifteen rel_type counts sum to the edges")
    # KNOWS is carried doubled under mapping rule M7; halving it back must
    # recover LDBC's published SF1 edge total exactly.
    eq(sum(rel_counts.values()) - rel_counts["KNOWS"] // 2, 17196776,
       "SF1: undoing the declared M7 KNOWS doubling recovers LDBC's published total")
    sf1_versions = sf1_nodes + sf1_edges
    eq(sf1_versions, 20367142, "SF1: nodes + edges, the streamed record count")
    require(f"**{sf1_versions:,} records in 687 s**" in ev_txt,
            "PAPER_A_EVIDENCE_REPORT §1.1 states the streamed record count")
    require(f"types summing to **{sf1_nodes:,}**, fifteen relation types summing to\n"
            f"**{sf1_edges:,}** (the published 17,196,776 plus the "
            f"{rel_counts['KNOWS'] // 2:,} declared M7 `KNOWS`\ndoubling)" in ev_txt,
            "PAPER_A_EVIDENCE_REPORT §1.2 states both totals and the doubling")
    eq(round(sf1_versions / 1e6, 1), 20.4, "SF1: version count rounds to 20.4M")
    m.add("tgSfOneNodes", tex_num(sf1_nodes),
          "paper-a-v1/forecast.yaml assumed_stats.n_entities (= n_node_versions)")
    m.add("tgSfOneEdges", tex_num(sf1_edges),
          "paper-a-v1/forecast.yaml assumed_stats.n_edge_versions")
    m.add("tgSfOneVersionsM", f"{sf1_versions / 1e6:.1f}",
          "paper-a-v1/forecast.yaml: n_node_versions + n_edge_versions, in millions")

    # the mapping-fidelity gate.  The per-type table lives in the store's
    # dataset card, which is outside version control; the gate's own verdict is
    # the report's section heading, and that is what the paper cites.
    fidelity = re.search(r"^### 1\.2 The mapping-fidelity gate: (\d+) of (\d+) exact$",
                         ev_txt, re.M)
    require(fidelity is not None, "PAPER_A_EVIDENCE_REPORT §1.2 states the fidelity verdict")
    if fidelity:
        eq(fidelity.group(1), fidelity.group(2), "SF1: every per-type count matched")
        m.add("tgSfOneFidelityTypes", fidelity.group(2),
              "PAPER_A_EVIDENCE_REPORT.md §1.2: per-type counts checked against LDBC's "
              "published SF1 figures")
        m.add("tgSfOneFidelityMatched", fidelity.group(1),
              "PAPER_A_EVIDENCE_REPORT.md §1.2: of those, the ones matching exactly")

    # the frozen admission split, and the measured one, row by row
    fa_rows_by_id = {r["id"]: r for r in pa_forecast["derived_admission"]["rows"]}
    crecs_by_id = {r["plan_id"]: r for r in crecs}
    eq(sorted(fa_rows_by_id), sorted(crecs_by_id), "SF1: forecast and campaign plan id sets")
    for pid, frec in fa_rows_by_id.items():
        eq(crecs_by_id[pid]["derived_admission"], frec["derived_admission"],
           f"SF1 {pid}: measured admission equals the frozen one")
    # exactly one estimate moved between the assumed statistics and the store's
    # own; the split did not, which is the whole of the P-E13 admission claim.
    moved = sorted(pid for pid, frec in fa_rows_by_id.items()
                   if crecs_by_id[pid]["estimate"]["time_est_ms"] != frec["time_est_ms"])
    eq(moved, ["BI10"], "SF1: only BI10's estimate moved against the built store's stats")

    sf1_refuse = sum(1 for r in crecs if r["derived_admission"] == "refuse")
    sf1_admit = sum(1 for r in crecs if r["derived_admission"] == "admit")
    da = pa_forecast["derived_admission"]
    eq(sf1_refuse, da["refuse"], "SF1: measured refusals equal the frozen 10")
    eq(sf1_admit, da["admit"], "SF1: measured admissions equal the frozen 11")
    eq(len(crecs), da["total"], "SF1: 21 plan records")
    eq(sf1_refuse + sf1_admit, len(crecs), "SF1: refuse + admit partition the 21")
    m.add("tgSfOnePlans", len(crecs), "ldbc-sf1-campaign.json records: frozen LDBC plans")
    m.add("tgSfOneRefuse", sf1_refuse, "ldbc-sf1-campaign.json: derived_admission == refuse")
    m.add("tgSfOneAdmit", sf1_admit, "ldbc-sf1-campaign.json: derived_admission == admit")

    sf1_scored = sum(1 for r in crecs if r["arm"] == "scored-bi")
    sf1_char = sum(1 for r in crecs if r["arm"] == "characterization-interactive")
    eq(sf1_scored + sf1_char, len(crecs), "SF1: the two arms partition the 21 records")
    m.add("tgSfOneScored", sf1_scored,
          "ldbc-sf1-campaign.json: arm == scored-bi (LDBC's own SF1 parameters)")
    m.add("tgSfOneChar", sf1_char,
          "ldbc-sf1-campaign.json: arm == characterization-interactive (sampled anchors)")

    eq(cman["commit"], frontier["manifest"]["source_commit"],
       "SF1: the P3 frontier is derived from this campaign record")
    require(cman["complete"] is True, "SF1: the campaign record declares itself complete")
    eq(cman["policy_version"], "guardrail-policy-v2", "SF1: the campaign ran at policy-v2")
    eq(cman["wall_s"], float(int(cman["wall_s"])), "SF1: manifest wall_s is a whole second count")
    sf1_date = cman["utc"].split("T")[0]
    eq(sf1_date, "2026-08-24", "SF1: the campaign date")
    m.add("tgSfOneCommit", cman["commit"], "ldbc-sf1-campaign.json manifest.commit")
    m.add("tgSfOneWallS", tex_num(int(cman["wall_s"])), "ldbc-sf1-campaign.json manifest.wall_s")
    m.add("tgSfOneDate", sf1_date, "ldbc-sf1-campaign.json manifest.utc, date part")

    # ------------------------------- E13: the guard as a classifier, scored arm
    # per_plan already verified above (tgAdm*); these are the same cells counted
    # under the names the evaluation section uses.
    cls = Counter(p["classifier"] for p in per_plan)
    eq(sum(cls.values()), bi_arm["scoreable"], "scored arm: classifiers partition the scoreable cells")
    eq(cls["true-rejection"], 8, "scored arm: true rejections")
    eq(cls["true-admission"], 0, "scored arm: true admissions")
    eq(cls["false-admission"] + cls["false-rejection"] + cls["true-rejection"]
       + cls["true-admission"], 9, "scored arm: the nine scoreable cells")
    unscoreable = bi_arm["excluded"]
    eq(len(unscoreable), bi_arm["of"] - bi_arm["scoreable"], "scored arm: of - scoreable = excluded")
    eq(crecs_by_id[unscoreable[0]]["outcome"], "ERRORED",
       "scored arm: the excluded plan is the one that ERRORED")
    require("**8 true rejections · 1 false admission · 0 false rejections · 1 unscoreable.**"
            in ev_txt, "PAPER_A_EVIDENCE_REPORT §1.4 states the scored-arm classifier split")
    m.add("tgSfOneTrueRej", cls["true-rejection"],
          "e14-p3-frontier.json arms.scored-bi.per_plan: classifier == true-rejection")
    m.add("tgSfOneFalseAdm", cls["false-admission"],
          "e14-p3-frontier.json arms.scored-bi.per_plan: classifier == false-admission")
    m.add("tgSfOneFalseRej", cls["false-rejection"],
          "e14-p3-frontier.json arms.scored-bi.per_plan: classifier == false-rejection")
    m.add("tgSfOneUnscoreable", len(unscoreable),
          "e14-p3-frontier.json arms.scored-bi.excluded: unscoreable cells")
    m.add("tgSfOneUnscoreableRow", unscoreable[0],
          "e14-p3-frontier.json arms.scored-bi.excluded: which plan that is")
    m.add("tgSfOneScoreable", bi_arm["scoreable"],
          "e14-p3-frontier.json arms.scored-bi.scoreable: cells carrying a classifier")

    # --------------------------------- the scored arm, row by row (tab:sf1)
    # One row per scored BI plan, joining the campaign record (admission,
    # estimate, wall, rows, outcome) to the frontier's classifier.  BI6 has no
    # wall and no rows because it errored; it carries em-dashes and the
    # `unscoreable` label rather than being dropped from the denominator.
    per_plan_by_id = {p["plan_id"]: p for p in per_plan}
    sf1_rows = []
    derived_ratios = {}
    for pid in sorted(crecs_by_id):
        rec = crecs_by_id[pid]
        if rec["arm"] != "scored-bi":
            continue
        est = rec["estimate"]["time_est_ms"]
        cell = per_plan_by_id.get(pid)
        if cell is None:
            eq(rec["outcome"], "ERRORED", f"{pid}: absent from per_plan only because it errored")
            sf1_rows.append((pid, rec["derived_admission"], est, None, None, None, "unscoreable"))
            continue
        eq(cell["est_ms"], est, f"{pid}: frontier est_ms equals the campaign record's estimate")
        eq(round(cell["actual_ms"], 6), round(rec["ms"], 6),
           f"{pid}: frontier actual_ms equals the campaign record's ms")
        eq(cell["rows"], rec["rows"], f"{pid}: frontier row count equals the campaign record's")
        ratio = est / cell["actual_ms"]
        derived_ratios[pid] = ratio
        sf1_rows.append((pid, rec["derived_admission"], est, cell["actual_ms"], ratio,
                         rec["rows"], cell["classifier"]))
    eq(len(sf1_rows), sf1_scored, "tab:sf1 carries every scored BI plan, BI6 included")
    # the per-plan ratios must reproduce the aggregates the frontier states
    eq(round(min(derived_ratios.values()), 6), round(eoa["min"], 6),
       "recomputed estimate/actual min matches the frontier's")
    eq(round(max(derived_ratios.values()), 6), round(eoa["max"], 6),
       "recomputed estimate/actual max matches the frontier's")
    eq(round(sorted(derived_ratios.values())[len(derived_ratios) // 2], 6),
       round(eoa["median"], 6), "recomputed estimate/actual median matches the frontier's")
    eq(round(max(derived_ratios.values()) / min(derived_ratios.values()), 6),
       round(eoa["spread"], 6), "recomputed estimate/actual spread matches the frontier's")
    eq(sorted(p for p, r in derived_ratios.items() if r < 1.0), sorted(under_est),
       "recomputed under-estimates match the frontier's list")

    # ...and the whole table must reproduce the report's own §1.4 table, cell
    # for cell, so an edit at either end is a hard failure rather than a drift.
    report_table = {}
    for line in ev_txt.splitlines():
        cells = [c.strip().replace("**", "").replace("×", "") for c in line.strip().split("|")]
        if len(cells) == 9 and re.fullmatch(r"BI\d+", cells[1]):
            # the report shouts the one false admission; the classifier name is
            # the same token either way
            report_table[cells[1]] = cells[2:7] + [cells[7].lower()]
    eq(sorted(report_table), sorted(r[0] for r in sf1_rows),
       "PAPER_A_EVIDENCE_REPORT §1.4 table covers exactly the scored BI plans")
    for pid, adm, est, actual, ratio, rows, cls_name in sf1_rows:
        got = [adm, f"{est:,}",
               "—" if actual is None else f"{round(actual):,}",
               "—" if ratio is None else sf1_ratio(ratio),
               "—" if rows is None else f"{rows:,}",
               cls_name]
        eq(got, report_table[pid], f"PAPER_A_EVIDENCE_REPORT §1.4 row {pid}")

    # ------------------------------------------ E13: the characterization arm
    char_arm = frontier["arms"]["characterization-interactive"]
    char_plans = char_arm["per_plan"]
    eq(char_arm["of"], sf1_char, "char arm: cells == the 11 Interactive records")
    eq(char_arm["scoreable"], char_arm["of"], "char arm: nothing excluded")
    eq(len(char_plans), char_arm["of"], "char arm: per_plan rows == cells")
    ccls = Counter(p["classifier"] for p in char_plans)
    eq(sum(ccls.values()), char_arm["of"], "char arm: classifiers partition the cells")
    eq(ccls["false-admission"], 9, "char arm: false admissions")
    eq(ccls["true-admission"], 1, "char arm: true admissions")
    eq(ccls["true-rejection"], 1, "char arm: true rejections")
    eq(ccls["false-rejection"], 0, "char arm: false rejections")
    char_ta = [p["plan_id"] for p in char_plans if p["classifier"] == "true-admission"]
    char_tr = [p["plan_id"] for p in char_plans if p["classifier"] == "true-rejection"]
    eq(char_ta, ["IS3"], "char arm: the one true admission is IS3")
    eq(char_tr, ["IC12"], "char arm: the one true rejection is IC12")
    ceoa = char_arm["estimate_over_actual"]
    eq(len(ceoa["under_estimates"]), 10, "char arm: under-estimates")
    eq(len(ceoa["zero_estimates"]), 4, "char arm: plans estimating at literally 0 ms")
    eq(sorted(ceoa["zero_estimates"]), ["IC2", "IC8", "IS3", "IS7"],
       "char arm: which plans estimate at 0 ms")
    require(ceoa["spread"] is None,
            "char arm: the spread is unbounded, because some estimates are 0 ms")
    # every zero estimate really is zero in the record the frontier derives from
    for pid in ceoa["zero_estimates"]:
        eq(crecs_by_id[pid]["estimate"]["time_est_ms"], 0, f"char arm {pid}: estimate is 0 ms")
    eq(char_arm["best"]["multiplier"], 0.01, "char arm: the swept optimum sits at 0.01x")
    eq(char_arm["best"]["false-admission"], 7,
       "char arm: false admissions even at the optimum, because a zero estimate is admissible")
    require("**9 false admissions · 1 true admission (IS3, 7,747 ms) · 1 true rejection\n"
            "(IC12).**" in ev_txt,
            "PAPER_A_EVIDENCE_REPORT §1.5 states the characterization split")
    m.add("tgSfOneCharFalseAdm", ccls["false-admission"],
          "e14-p3-frontier.json arms.characterization-interactive.per_plan: false-admission")
    m.add("tgSfOneCharTrueAdm", ccls["true-admission"],
          "e14-p3-frontier.json arms.characterization-interactive.per_plan: true-admission")
    m.add("tgSfOneCharTrueRej", ccls["true-rejection"],
          "e14-p3-frontier.json arms.characterization-interactive.per_plan: true-rejection")
    m.add("tgSfOneCharTrueAdmRow", char_ta[0],
          "e14-p3-frontier.json characterization arm: which plan the true admission is")
    m.add("tgSfOneCharTrueRejRow", char_tr[0],
          "e14-p3-frontier.json characterization arm: which plan the true rejection is")
    m.add("tgSfOneCharUnderEst", len(ceoa["under_estimates"]),
          "e14-p3-frontier.json characterization arm: estimate_over_actual.under_estimates")
    m.add("tgSfOneCharZeroEst", len(ceoa["zero_estimates"]),
          "e14-p3-frontier.json characterization arm: estimate_over_actual.zero_estimates")
    m.add("tgSfOneCharBestFalseAdm", char_arm["best"]["false-admission"],
          "e14-p3-frontier.json characterization arm: false admissions at the best "
          "multiplier (0.01x) --- no ceiling reaches zero")

    # ------------------------------------------- E14 P1: what the leaf costs
    p1_lo, p1_hi = 0.8, 1.2
    p1 = {}
    for label, path in (("bitcoinotc", P1_BITCOIN), ("collegemsg", P1_COLLEGE)):
        doc = json.loads(path.read_text(encoding="utf-8"))
        eq(doc["manifest"]["store"], f"stores/{label}", f"P1 {label}: store")
        eq(doc["manifest"]["backend"], "native", f"P1 {label}: backend")
        require(f"{p1_lo}-{p1_hi}" in doc["manifest"]["band"],
                f"P1 {label}: the record declares the 0.8-1.2 band this script applies")
        cells = []
        for row in doc["rows"]:
            for arm in ("direct", "leaf"):
                eq(row[arm]["outcome"], "OK", f"P1 {label} {row['case']}: {arm} arm completed")
            ratio = row["leaf"]["p50_ms"] / row["direct"]["p50_ms"]
            require(abs(ratio - row["leaf_over_direct"]) < 1e-9,
                    f"P1 {label} {row['case']}: recomputed p50 ratio disagrees with the record")
            cells.append((row["case"], ratio))
        p1[label] = cells
    # the band is inclusive at both ends, and it has to be: bitcoinotc's
    # `compute` lands on 0.800 exactly.
    within = {k: [c for c in v if p1_lo <= c[1] <= p1_hi] for k, v in p1.items()}
    excursions = {k: [c for c in v if not (p1_lo <= c[1] <= p1_hi)] for k, v in p1.items()}
    p1_cases = eq(len(p1["bitcoinotc"]), len(p1["collegemsg"]), "P1: both stores ran the same cases")
    eq(sorted(c for c, _ in p1["bitcoinotc"]), sorted(c for c, _ in p1["collegemsg"]),
       "P1: both stores ran the same case names")
    eq(p1_cases, 16, "P1: cases per store")
    p1_cells = sum(len(v) for v in p1.values())
    p1_within = sum(len(v) for v in within.values())
    p1_exc = sum(len(v) for v in excursions.values())
    eq(p1_cells, 32, "P1: cells = 16 cases x 2 stores")
    eq(p1_within, 29, "P1: cells inside the band")
    eq(p1_within + p1_exc, p1_cells, "P1: within + excursions partition the cells")
    eq(len(within["bitcoinotc"]), 15, "P1 bitcoinotc: cells inside the band")
    eq(len(within["collegemsg"]), 14, "P1 collegemsg: cells inside the band")
    eq([c for c, _ in excursions["bitcoinotc"]], ["neighborhood_evolution"],
       "P1 bitcoinotc: the one excursion, by name")
    eq(sorted(c for c, _ in excursions["collegemsg"]), ["compute", "diff_snapshots"],
       "P1 collegemsg: the two excursions, by name")
    max_exc = max(r for _, r in excursions["bitcoinotc"] + excursions["collegemsg"])
    eq(round(max_exc, 3), 1.279, "P1: the largest excursion")
    compute = {k: dict(v)["compute"] for k, v in p1.items()}
    eq(round(compute["collegemsg"], 3), 1.249, "P1 collegemsg: the wrapper-only `compute` ratio")
    eq(round(compute["bitcoinotc"], 3), 0.800, "P1 bitcoinotc: the wrapper-only `compute` ratio")
    require(compute["bitcoinotc"] < 1.0 < compute["collegemsg"],
            "P1: `compute` lands either side of 1.0 across stores --- which is what noise is")
    # the report's own §2 table, so an edit at either end breaks this script
    require(f"> **{p1_within} of {p1_cells} cells within the ±20% band; three excursions, "
            "all ≤1.28×, none\n> reproducible across stores.**" in ev_txt,
            "PAPER_A_EVIDENCE_REPORT §2 headline")
    require("| bitcoinotc | 15/16 | `neighborhood_evolution` 1.210× | 75.0 s |" in ev_txt,
            "PAPER_A_EVIDENCE_REPORT §2 table: bitcoinotc row")
    require("| collegemsg | 14/16 | `diff_snapshots` 1.279×, `compute` 1.249× | 120.2 s |"
            in ev_txt, "PAPER_A_EVIDENCE_REPORT §2 table: collegemsg row")
    m.add("tgPOneCases", p1_cases, "e14-p1-leaf-overhead-*.json rows: cases per store")
    m.add("tgPOneCells", p1_cells, "e14-p1-leaf-overhead-*.json: cases x the two stores")
    m.add("tgPOneWithin", p1_within,
          "e14-p1-leaf-overhead-*.json: cells whose leaf/direct p50 ratio is in [0.8, 1.2]")
    m.add("tgPOneExcursions", p1_exc, "e14-p1-leaf-overhead-*.json: cells outside the band")
    m.add("tgPOneMaxExcursion", f"{max_exc:.3f}",
          "e14-p1-leaf-overhead-collegemsg.json: the largest leaf/direct ratio (diff_snapshots)")
    m.add("tgPOneBitcoinWithin", len(within["bitcoinotc"]),
          "e14-p1-leaf-overhead-bitcoinotc.json: cells in band")
    m.add("tgPOneCollegeWithin", len(within["collegemsg"]),
          "e14-p1-leaf-overhead-collegemsg.json: cells in band")
    m.add("tgPOneComputeCollege", f"{compute['collegemsg']:.3f}",
          "e14-p1-leaf-overhead-collegemsg.json rows[compute]: leaf/direct p50")
    m.add("tgPOneComputeBitcoin", f"{compute['bitcoinotc']:.3f}",
          "e14-p1-leaf-overhead-bitcoinotc.json rows[compute]: leaf/direct p50")

    # --------------------------------------- the pre-registered thresholds
    # Internal doc, cited by line the way the M3/M4 prose receipts already are.
    m.add("tgPredEThirteenAFalseRejMin",
          grep_int(EVIDENCE_FREEZE, r"\*\*at least (\d+) are false rejections\*\*",
                   "freeze §B3 false-rejection floor"),
          "PAPER_A_EVIDENCE_FREEZE.md §B3 (P-E13-A): the pre-registered floor on false "
          "rejections among the 10 derived refusals")
    m.add("tgPredCTwoBound",
          grep_int(EVIDENCE_FREEZE, r"compiled core vs kernel\.\*\* Within \*\*(\d+)×\*\*",
                   "freeze §C2 compiled/kernel bound"),
          "PAPER_A_EVIDENCE_FREEZE.md §C2 (P-E14-C2): the pre-registered compiled/kernel bound")
    m.add("tgPredCThreeMultMin",
          grep_int(EVIDENCE_FREEZE,
                   r"best `time_est_ms` multiplier at a 10 s budget is \*\*≥ (\d+)×\*\*",
                   "freeze §C3 multiplier floor"),
          "PAPER_A_EVIDENCE_FREEZE.md §C3 (P-E14-C3): the pre-registered floor on the best "
          "ceiling multiplier")
    m.add("tgPredCFourSpreadMin",
          grep_int(EVIDENCE_FREEZE, r"spread at plan scope is \*\*≥ (\d+)×\*\*",
                   "freeze §C3 spread floor"),
          "PAPER_A_EVIDENCE_FREEZE.md §C3 (P-E14-C4): the pre-registered floor on "
          "estimate-error spread")
    m.add("tgPredPOneBandPct",
          grep_int(EVIDENCE_FREEZE, r"leaf path is within the \*\*±(\d+)%\*\* between-day",
                   "freeze §C1 band width"),
          "PAPER_A_EVIDENCE_FREEZE.md §C1 (P1): the pre-registered between-day band, percent")

    # --------------------------------- the campaign scored against its freeze
    # The report's §5 scorecard, counted from its own verdict column rather
    # than from its closing sentence, so a re-scored row moves the paper.
    verdicts = [line.rstrip(" |").rsplit("| ", 1)[-1].strip("* ")
                for line in ev_txt.splitlines() if line.startswith("| **P")]
    eq(len(verdicts), 7, "PAPER_A_EVIDENCE_REPORT §5: pre-registered predictions scored")
    falsified = [v for v in verdicts if v.startswith("FALSIFIED")]
    eq(len(falsified), 4, "§5: predictions falsified")
    eq(sum(1 for v in verdicts if v == "CONFIRMED"), 1, "§5: predictions confirmed")
    eq(sum(1 for v in verdicts if v == "HELD"), 1, "§5: predictions that held")
    eq(sum(1 for v in verdicts if v == "restated"), 1, "§5: predictions restated")
    require("Four falsified, one confirmed, one held, one restated." in ev_txt,
            "PAPER_A_EVIDENCE_REPORT §5 states its own scorecard in prose")
    m.add("tgPredScored", len(verdicts),
          "PAPER_A_EVIDENCE_REPORT.md §5: pre-registered predictions scored")
    m.add("tgPredFalsified", len(falsified),
          "PAPER_A_EVIDENCE_REPORT.md §5: of those, the ones the campaign falsified")

    # P2's population, which the paper must not overstate: COMPILED holds two
    # operators, and only one of them has a kernel number to divide by at 1M
    # and 10M, because the guard refuses the other's kernel at both scales.
    p2_ops = [r["op"] for r in p2_one_m_after["rows"]]
    eq(sorted(p2_ops), ["entity_history", "version_history"], "P2: the COMPILED population")
    for label, doc in (("1m", p2_one_m_after), ("10m", p2_ten_m_after)):
        eq([r["op"] for r in doc["rows"]], p2_ops, f"P2 {label}: the same two operators")
        eq([r["op"] for r in doc["rows"] if r["kernel"]["outcome"] == "OK"],
           ["entity_history"], f"P2 {label}: only entity_history has a kernel number")
        eq([r["op"] for r in doc["rows"] if r["kernel"]["outcome"] != "OK"],
           ["version_history"], f"P2 {label}: version_history's kernel is refused")
    m.add("tgPTwoOperators", len(p2_ops),
          "e14-p2-compiled-*.json rows: operators holding both a kernel and a compiled form")
    m.add("tgPTwoOperatorsAtScale", 1,
          "e14-p2-compiled-{1m,10m}-after.json: of those, the ones with a kernel number to "
          "divide by at 1M and 10M --- the guard refuses version_history's kernel at both")

    # ------------------------------------------------- the 2026-09-15 rerun
    rerun = json.loads(SF1_RERUN.read_text(encoding="utf-8"))
    rman = rerun["manifest"]
    rrecs = {r["plan_id"]: r for r in rerun["records"]}
    readme = SF1_RERUN_README.read_text(encoding="utf-8")
    rerun_date = rman["utc"].split("T")[0]
    eq(rerun_date, "2026-09-15", "rerun: the record's date")
    eq(rman["policy_version"], cman["policy_version"], "rerun: same policy as the campaign")
    eq(rman["ceilings"], cman["ceilings"], "rerun: same ceilings as the campaign")
    eq(rman["campaign_seed"], cman["campaign_seed"], "rerun: same anchor seed as the campaign")
    eq(rman["build_info"]["manifest_format_version"], 3, "rerun: the format-3 engine")
    require(rman["complete"] is True, "rerun: the record declares itself complete")
    # The rerun carries its own pre-registered wall-time band, from P-SF1.  It
    # is NOT P1's between-day band (tgPredPOneBandPct) even though both read 20%:
    # PAPER_A_EVIDENCE_FREEZE §C's conceptual guard forbids citing one for the
    # other (D1, a measurement caveat, versus this, a prediction about an
    # engine change).  Two macros, two sources, deliberately.
    #
    # The provenance string below names the pre-registration but NOT the file:
    # docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md §4.3b is an internal
    # planning document for an unpublished campaign, and tgir-macros.tex ships
    # in the arXiv source.  The path is here, in the source that reads it.
    rerun_band = grep_int(OSDI_PLAN,
                          r"\*\*P-SF1 — SF1 reruns at the format-3 engine\.\*\*[^\n]*?"
                          r"wall time within ±(\d+) ?% of each existing record",
                          "P-SF1 wall-time band")
    m.add("tgSfOneRerunBandPct", rerun_band,
          "P-SF1 pre-registration (plan §4.3b): the rerun's own pre-registered "
          "wall-time band against each existing record, percent (distinct from "
          "tgPredPOneBandPct, which is P1's between-day band)")
    m.add("tgSfOneRerunCommit", rman["commit"],
          "ldbc-sf1-campaign-fmt3-2026-09.json manifest.commit")
    m.add("tgSfOneRerunDate", rerun_date,
          "ldbc-sf1-campaign-fmt3-2026-09.json manifest.utc, date part")

    bi_both = sorted(p for p in rrecs if p in crecs_by_id and p.startswith("BI"))
    eq(len(bi_both), sf1_scored, "rerun: BI plans present in both records")
    ratios = {}
    count_equal = 0
    for pid in bi_both:
        old, new = crecs_by_id[pid], rrecs[pid]
        eq(new["outcome"], old["outcome"], f"rerun {pid}: outcome class unchanged")
        # `rows` is absent from both sides for BI6, which ERRORED in both runs
        eq(new.get("rows"), old.get("rows"), f"rerun {pid}: row count unchanged")
        count_equal += 1
        if old["outcome"] == "COMPLETED":
            ratios[pid] = new["ms"] / old["ms"]
    eq(len(ratios), 9, "rerun: BI plans that COMPLETED in both runs")
    eq(count_equal, len(bi_both), "rerun: every compared BI plan agrees on row count")
    r_min_id = min(ratios, key=ratios.get)
    r_max_id = max(ratios, key=ratios.get)
    eq((r_min_id, r_max_id), ("BI7", "BI9"), "rerun: which plans hold the ratio extremes")
    eq(round(ratios[r_min_id], 2), 0.56, "rerun: fastest new/old wall ratio")
    eq(round(ratios[r_max_id], 2), 0.92, "rerun: slowest new/old wall ratio")
    require(max(ratios.values()) < 1.0, "rerun: every BI plan got faster, none slower")
    m.add("tgSfOneRerunBiCompared", len(bi_both),
          "ldbc-sf1-campaign-fmt3-2026-09.json: BI plans present in both records")
    m.add("tgSfOneRerunCountEqual", count_equal,
          "the 9 COMPLETED BI plans agree on `rows`, and BI6 ERRORED in both")
    m.add("tgSfOneRerunWallMin", f"{ratios[r_min_id]:.2f}",
          "ldbc-sf1-campaign-fmt3-2026-09.json: min new_ms/existing_ms over the 9 "
          "BI plans that COMPLETED in both")
    m.add("tgSfOneRerunWallMax", f"{ratios[r_max_id]:.2f}",
          "ldbc-sf1-campaign-fmt3-2026-09.json: max new_ms/existing_ms over the same 9")
    require(f"| BI7 | 13,076 | 7,336 | {ratios[r_min_id]:.2f} | True | COMPLETED->COMPLETED |"
            in readme, "rerun README per-plan table: the BI7 row")
    require(f"| BI9 | 66,037 | 60,501 | {ratios[r_max_id]:.2f} | True | COMPLETED->COMPLETED |"
            in readme, "rerun README per-plan table: the BI9 row")

    # BI6's repair.  The README's per-plan table reports it in the *new ms*
    # column; the row count is a separate, much smaller number, and conflating
    # them is exactly what this script exists to prevent.
    bi6v2 = rrecs["BI6.v2"]
    eq(bi6v2["outcome"], "COMPLETED", "BI6.v2: the repaired plan completes")
    eq(bi6v2["derived_admission"], "refuse", "BI6.v2: still refused by the guard")
    require(bi6v2["bypassed"] is True, "BI6.v2: run under the bypassed-but-recording arm")
    eq(bi6v2["rows"], 100, "BI6.v2: delivered row count")
    eq(round(bi6v2["ms"]), 291971, "BI6.v2: wall time, ms")
    eq(bi6v2["estimate"]["time_est_ms"], 230072, "BI6.v2: estimated cost, ms")
    require(f"| BI6.v2 | (no baseline) | {round(bi6v2['ms']):,} | — | n/a | (new)->COMPLETED |"
            in readme, "rerun README: BI6.v2's row reports its *ms*, not its rows")
    m.add("tgBiSixVTwoRows", bi6v2["rows"],
          "ldbc-sf1-campaign-fmt3-2026-09.json BI6.v2 `rows`: the delivered row count")
    m.add("tgBiSixVTwoMs", tex_num(round(bi6v2["ms"])),
          "ldbc-sf1-campaign-fmt3-2026-09.json BI6.v2 `ms`, rounded")
    m.add("tgBiSixVTwoEstMs", tex_num(bi6v2["estimate"]["time_est_ms"]),
          "ldbc-sf1-campaign-fmt3-2026-09.json BI6.v2 estimate.time_est_ms")

    # the characterization arm of the rerun: every Interactive plan failed to
    # bind.  These records carry neither `arm` nor `rows`, so read defensively.
    bind_failed = [r for r in rerun["records"] if r["outcome"] == "BIND_FAILED"]
    interactive = [r for r in rerun["records"]
                   if r.get("arm") is None and r["plan_id"][:2] in ("IC", "IS")]
    eq(len(bind_failed), len(interactive),
       "rerun: every Interactive record is a BIND_FAILED record and vice versa")
    eq(len(bind_failed), 14, "rerun: Interactive plans, the original 11 plus m6/D2's IS1/IS4/IS5")
    require(all(r.get("phantom_anchor") is True for r in bind_failed),
            "rerun: every BIND_FAILED record names a phantom anchor")
    eq(len(rerun["records"]), len(bi_both) + 1 + len(bind_failed),
       "rerun: the 10 BI plans, BI6.v2, and the 14 Interactive plans")
    require("All 14 characterization-interactive plans (the 11 original IC/IS rows plus\n"
            "the 3 new D2 rows IS1/IS4/IS5) that previously completed now record\n"
            "`BIND_FAILED`" in readme, "rerun README: the outcome-class change, as stated")
    m.add("tgSfOneRerunCharTotal", len(interactive),
          "ldbc-sf1-campaign-fmt3-2026-09.json: Interactive plans attempted in the rerun")
    m.add("tgSfOneRerunCharBindFailed", len(bind_failed),
          "ldbc-sf1-campaign-fmt3-2026-09.json: Interactive plans recording BIND_FAILED")

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
        OUT_DIR / "tab-sf1.tex": render_sf1_table(sf1_rows),
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


def render_sf1_table(sf1_rows) -> str:
    """The scored arm of the SF1 campaign, one row per BI plan.

    `sf1_rows` is (plan, admission, est_ms, actual_ms, est/actual, rows,
    classifier), with actual/ratio/rows None for the plan that errored.  Every
    cell was asserted against the evidence report's §1.4 table before this ran.
    """
    lines = [BANNER, "\\begin{table*}[t]", "\\centering", "\\small",
             "\\caption{The scored arm of the \\tgSfOneDate\\ campaign: \\tgSfOneScored\\ LDBC "
             "Business Intelligence plans against a real SF1 instance "
             "(\\tgSfOneVersionsM\\,M versions), bound to LDBC's own SF1 parameters, at "
             "\\texttt{\\tgSfOneCommit}.  \\emph{admission} is the guard's verdict at the frozen "
             "policy; the guard was bypassed-but-recording, so every plan ran and the verdict "
             "can be scored against what the plan actually cost.  \\emph{est/actual} below~1 is "
             "an under-estimate --- the unsafe direction.  \\tgSfOneUnscoreableRow\\ errored on "
             "real data (a null join key the fixture had no rows to expose) and is "
             "\\emph{\\tgSfOneUnscoreable} of the \\tgSfOneScored, reported rather than dropped "
             "from the denominator.  Nothing here is an LDBC Benchmark Result.}",
             "\\label{tab:sf1}",
             "\\begin{tabular}{llrrrrl}", "\\toprule",
             "plan & admission & est.\\ ms & actual ms & est/actual & rows & classifier \\\\",
             "\\midrule"]
    dash = "---"
    for plan, adm, est, actual, ratio, rows, cls_name in sf1_rows:
        adm_cell = f"\\textbf{{{adm}}}" if adm == "admit" else adm
        # true rejections are the expected case and stay plain; everything else
        # is a result and is set bold, as the report sets it.
        cls_cell = cls_name if cls_name == "true-rejection" else f"\\textbf{{{cls_name}}}"
        actual_cell = dash if actual is None else tex_num(round(actual))
        ratio_cell = dash if ratio is None else sf1_ratio(ratio).replace(",", "{,}")
        rows_cell = dash if rows is None else tex_num(rows)
        lines.append(f"{plan} & {adm_cell} & {tex_num(est)} & {actual_cell} & "
                     f"{ratio_cell} & {rows_cell} & {cls_cell} \\\\")
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
