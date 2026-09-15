#!/usr/bin/env python
"""Generate the OSDI paper's number macros from committed receipts.

House rule (copied verbatim from ``scripts/tgir_paper_macros.py`` and
``scripts/paper_macros.py``): **assert, do not trust.** Every macro this
script emits is recomputed from the row-level fields of the record that
owns it, cross-checked against whatever aggregate the record states for
itself, and then checked against a frozen expected value in
``tests/test_osdi_paper_macros.py``. A disagreement anywhere in that chain
is a hard failure -- the script refuses to write output, never silently
adjusts a number, and never falls back to a record's own summary field
without first recomputing it from the rows underneath.

This is Lane W (task W2)'s receipts machinery for the OSDI paper. The
claims -> evidence map that assigns every macro name to a claim and a
record is ``docs/design/OSDI27_PAPER_SKELETON_2026-09-15.md`` S3 (gitignored,
internal; not shipped with this script, but every macro below documents
its own record path and field so the mapping is reconstructable without
it). Implemented here: every claim whose record has LANDED per that
skeleton --

  C1  benchmarks/crash-v1/eval-crash-campaign-2026-09-13.json
  C3  benchmarks/results-v1/b1-manifest-ab-2026-09{,-raw}.json (v1 B1 A/B) and
      benchmarks/results-v1/b1-manifest-v2-ab-2026-09{,-raw}.json (Lane W2f's
      B1-v2, manifest format 3 -- the reader torn-tail fix -- vs. the pinned
      format-2 soak engine; ``osdiB1v2*`` macros stand beside the v1-era
      ``osdiManifest*`` ones without overwriting them; no verdict macro --
      scoring is the coordinator's, per the internal freeze doc) and
      benchmarks/results-v1/b1-manifest-co7-chain-open-2026-09{,-raw}.json
      (Lane P-CO7's chain-open re-measurement on a confirmed-quiet host,
      treatment ``ebe1dc2``; ``osdiB1co7*`` macros stand beside the v2e-era
      ``osdiB1v2e*`` ones without overwriting them; the two
      ``osdiB1WorstPhaseOpen*`` macros are arithmetic on the co7 per-delta
      costs projected to K-1=511 deltas, labelled as such, not a
      measurement; no verdict macro here either)
  C4  benchmarks/results-v1/b2-version-history-ab-2026-09{,-raw}.json
  C5  benchmarks/results-v1/eval-readers-10m-2026-09.json
  C6  benchmarks/freshness-v1/trials-{full,fixture}.json (M4 record of
      account) + benchmarks/m5-v1/topup-{carve2,propagation2-*}.json,
      the campaign's closing round per
      docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md Addendum 8 (never read
      as a number source here -- the addendum is prose-only provenance in
      this file's comments; every number below is recomputed from the
      m5-v1 JSON files' own rows)
  C8  benchmarks/faults-v1/fault-matrix-campaign-2026-09-{13,15-d160}.json
  D160-collegemsg (Lane W2c, not in the OSDI27 skeleton's C-numbering --
      the coordinator's D-160 ruling deliverable) --
      benchmarks/d160-collegemsg-v1/{manifest,rows}-2026-09-14.json, the
      CollegeMsg coverage/conditional-accuracy/UCR re-measurement under the
      production claim gate that also drops `unverifiable` claims (see
      docs/STABILITY.md section 9). Its pre-D-160-gate counterparts
      (osdiOldGate*) are parsed out of docs/site_facts.json's
      `unsupported_claims` fact and cross-checked against that same
      STABILITY.md section, never hard-coded independently of both. The
      `llm_direct` follow-up re-run under the real-tokenizer budget fix
      (`manifest-llm-direct-fix-2026-09-14.json` /
      `rows-llm-direct-fix-2026-09-14.json`, job 212231) has now landed --
      `osdiD160LlmDirectCoverageFixed` and its siblings
      (`osdiD160LlmDirectErrorsFixed`/`RawEmFixed`/`TokenizerFixed`/
      `BudgetFixed`) are computed by `compute_d160_llm_direct_fix` below.
      The pre-fix record's own numbers (`osdiD160LlmDirectCarrying`,
      `osdiD160LlmDirectOverflowErrors`) are unchanged and still stand
      beside them, per the campaign's non-overwrite discipline.
  C7  (partial) benchmarks/storm-v1/storm-campaign-dag-{,v2-,v3-}2026-09.json
      (+ each one's -rows.jsonl) -- the DAG-phase v1/v2/v3 grids, all 40/40
      cells each -- and benchmarks/storm-v1/storm-r18-probe-2026-09.json
      (+ -rows.jsonl) -- the N=10,000 c1 seed-0 R-18 characterization probe,
      5/5 batches, not wall-capped. The main correction-storm cell grid
      (addendum-1's 36-cell grid, ``storm-campaign-2026-09.json``) that the
      rest of C7's claims (P5/P6 end-to-end speedup at N=1,000, the merged
      N=1,000 c1 seed-0 cell) depend on has **not** landed -- 12/36 cells
      complete, 24 blocked on an iTiger disk-quota incident (see
      benchmarks/storm-v1/README.md) -- so those quantities stay PENDING
      below (``osdiStormCells``/``osdiStormFalseFresh``/``osdiTtfSpeedup``/
      ``osdiStormSpeedupN1k``/``osdiStormAvoidedN1k``) even though the DAG
      phase and the R-18 probe are both fully landed and scored here.

  W2g (24h longevity soak, Lane B task B7a, Gate G1/Gate E) --
      benchmarks/longevity-v1/longevity-synth-1m-native-0.json (manifest) +
      recoveries.jsonl, reader_restarts.jsonl, longevity_ledger.jsonl,
      orchestrator.log (whole-file sha256-checked against README.md's own
      Files-here table), and writer_error_counts_by_life.json (a locally
      derived, un-hashed side-file whose per-life sum this script recomputes
      rather than trusting). No verdict macro -- Gate E's own PASS/FAIL/FLAG
      table is gate_e_report.md, not this script.

Claims C2 (corruption-detection campaign), C9 (LDBC generality, four axes
-- the Neo4j reference run is pending), and C10 (live OSV workload) have
no landed record yet; their macros, plus the still-unlanded slice of C7
above, are emitted as PENDING stubs (see ``Macros.add_pending``) that
raise a real LaTeX error (``\\errmessage``) if the paper ever expands one,
rather than silently emitting a placeholder number.

Usage:  $HOME/.venvs/tgms/bin/python scripts/osdi_paper_macros.py [--check]

``--check`` regenerates into memory and fails if the output on disk at
``paper/osdi/generated/osdi-macros.tex`` would differ. Nothing under
``paper/`` is committed (``paper/`` is gitignored publicly); this is the
local convention this script and ``scripts/osdi_paper_figures.py`` share
for that directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "osdi" / "generated"

CRASH_V1 = ROOT / "benchmarks" / "crash-v1" / "eval-crash-campaign-2026-09-13.json"

B1_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-ab-2026-09-raw.json"

B1_V2_MANIFEST = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2-ab-2026-09.json"
B1_V2_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2-ab-2026-09-raw.json"

B1_V2E_MANIFEST = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2e-remeasure-2026-09.json"
B1_V2E_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2e-remeasure-2026-09-raw.json"

B1_CO7_MANIFEST = ROOT / "benchmarks" / "results-v1" / "b1-manifest-co7-chain-open-2026-09.json"
B1_CO7_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-co7-chain-open-2026-09-raw.json"

B2_SUMMARY = ROOT / "benchmarks" / "results-v1" / "b2-version-history-ab-2026-09.json"
B2_RAW = ROOT / "benchmarks" / "results-v1" / "b2-version-history-ab-2026-09-raw.json"
VH_FORECAST = ROOT / "docs" / "design" / "BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md"

READERS_10M = ROOT / "benchmarks" / "results-v1" / "eval-readers-10m-2026-09.json"

FRESH_FULL = ROOT / "benchmarks" / "freshness-v1" / "trials-full.json"
FRESH_FIXTURE = ROOT / "benchmarks" / "freshness-v1" / "trials-fixture.json"
M5_CARVE_TWO = ROOT / "benchmarks" / "m5-v1" / "topup-carve2-synth-iv-60k.json"
M5_PROP_TWO = [
    ROOT / "benchmarks" / "m5-v1" / f"topup-propagation2-{store}.json"
    for store in ("bitcoinotc", "collegemsg", "sx-mathoverflow")
]

FAULTS_PRE = ROOT / "benchmarks" / "faults-v1" / "fault-matrix-campaign-2026-09-13.json"
FAULTS_POST = ROOT / "benchmarks" / "faults-v1" / "fault-matrix-campaign-2026-09-15-d160.json"

STORM_V1 = ROOT / "benchmarks" / "storm-v1"
DAG_V1 = STORM_V1 / "storm-campaign-dag-2026-09.json"
DAG_V1_ROWS = STORM_V1 / "storm-campaign-dag-2026-09-rows.jsonl"
DAG_V2 = STORM_V1 / "storm-campaign-dag-v2-2026-09.json"
DAG_V2_ROWS = STORM_V1 / "storm-campaign-dag-v2-2026-09-rows.jsonl"
DAG_V3 = STORM_V1 / "storm-campaign-dag-v3-2026-09.json"
DAG_V3_ROWS = STORM_V1 / "storm-campaign-dag-v3-2026-09-rows.jsonl"
R18_PROBE = STORM_V1 / "storm-r18-probe-2026-09.json"
R18_PROBE_ROWS = STORM_V1 / "storm-r18-probe-2026-09-rows.jsonl"

D160_DIR = ROOT / "benchmarks" / "d160-collegemsg-v1"
D160_MANIFEST = D160_DIR / "manifest-2026-09-14.json"
D160_ROWS = D160_DIR / "rows-2026-09-14.json"
D160_MANIFEST_FIX = D160_DIR / "manifest-llm-direct-fix-2026-09-14.json"
D160_ROWS_FIX = D160_DIR / "rows-llm-direct-fix-2026-09-14.json"
D160_SUITE = ROOT / "benchmarks" / "frozen-v1" / "suite-collegemsg.json"
SITE_FACTS = ROOT / "docs" / "site_facts.json"
STABILITY_MD = ROOT / "docs" / "STABILITY.md"

CORRUPTION_PRE = ROOT / "benchmarks" / "corruption-v1" / "eval-corruption-campaign-2026-09-14.json"
CORRUPTION_POST = (ROOT / "benchmarks" / "corruption-v1"
                    / "eval-corruption-campaign-2026-09-14-post-a10.json")

LADDER_DIR = ROOT / "benchmarks" / "ladder-v1"
LADDER_MERGED = LADDER_DIR / "ladder-2026-09-14.json"
LADDER_RAW = [
    LADDER_DIR / "raw" / "overhead-ladder-bitcoinotc-seed0-job212303.json",
    LADDER_DIR / "raw" / "overhead-ladder-bitcoinotc-seed1-job212304.json",
    LADDER_DIR / "raw" / "overhead-ladder-bitcoinotc-seed2-job212305.json",
]

LONGEVITY_DIR = ROOT / "benchmarks" / "longevity-v1"
LONGEVITY_MANIFEST = LONGEVITY_DIR / "longevity-synth-1m-native-0.json"
LONGEVITY_RECOVERIES = LONGEVITY_DIR / "recoveries.jsonl"
LONGEVITY_READER_RESTARTS = LONGEVITY_DIR / "reader_restarts.jsonl"
LONGEVITY_LEDGER = LONGEVITY_DIR / "longevity_ledger.jsonl"
LONGEVITY_ORCHESTRATOR_LOG = LONGEVITY_DIR / "orchestrator.log"
LONGEVITY_WRITER_ERRORS_BY_LIFE = LONGEVITY_DIR / "writer_error_counts_by_life.json"
LONGEVITY_GATE_E_REPORT = LONGEVITY_DIR / "gate_e_report.md"
# benchmarks/longevity-v1/README.md's own "Files here" table -- the five
# record files verified byte-identical (sha256) between xzgpu and this
# committed copy before commit. writer_error_counts_by_life.json and
# gate_e_report.md are the README's own "derived locally" pair and are
# deliberately absent from this table (and from the digest check below);
# their numbers are recomputed from the hash-checked files instead.
LONGEVITY_MANIFEST_SHA256 = "a94a0c2c3d343d84161c911f04c6a29b586a4b3ddfc113bffb38549d988b3500"
LONGEVITY_RECOVERIES_SHA256 = "a3ef427f47a4801ffcb4eab03bd05fd4d979b6d3ce507318b01c89783b3081da"
LONGEVITY_READER_RESTARTS_SHA256 = "ff7375c22a6c660ab565641d8ecce6a82de2de7a0628e20d50b7eda6f50170fe"
LONGEVITY_LEDGER_SHA256 = "edc13c40f50b849ee4fde1adfdad1ebbbed0e7be24e97a853bea4e0e414d7db4"
LONGEVITY_ORCHESTRATOR_LOG_SHA256 = "3c66d62554a1d19051a166510ec4f003af0f9b4e3ed36c4ceca7da5dce80f7a0"

FAILURE_LEDGER = ROOT / "ops" / "failure_ledger.jsonl"


# --------------------------------------------------------------------------
# verification helpers (copied from scripts/tgir_paper_macros.py)
# --------------------------------------------------------------------------

FAILURES: list[str] = []
CHECKS = 0


def require(cond: bool, what: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(what)


def eq(got, want, what: str):
    require(got == want, f"{what}: derived {got!r} != source {want!r}")
    return got


def close(got: float, want: float, tol: float, what: str):
    require(abs(got - want) <= tol,
            f"{what}: derived {got!r} not within {tol} of {want!r}")
    return got


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relpath(p: Path) -> str:
    """`p` relative to ROOT for a provenance string, or `p` itself when it
    is not under ROOT (e.g. a tampered copy under a test's tmp_path --
    tests monkeypatch the module-level path constants to such a copy and
    still need a working provenance string, not a ValueError)."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def us_to_ms_str(us: int, ndigits: int) -> str:
    """Format an integer-microsecond figure as milliseconds with exact
    decimal rounding. ``f"{us / 1000:.{ndigits}f}"`` looks equivalent but is
    not: a binary float can't represent an exact half like 5935/1000 ==
    5.935, so it may land a hair below the tie and round the wrong way
    (5.93 instead of 5.94). Decimal division on the exact integer input
    rounds the true half correctly."""
    ms = Decimal(int(us)) / Decimal(1000)
    quant = Decimal(1).scaleb(-ndigits)
    return str(ms.quantize(quant, rounding=ROUND_HALF_UP))


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

    def add_pending(self, name: str, lane: str, reason: str) -> None:
        """A macro for a claim whose record has not landed yet.

        Renders to a real LaTeX error via ``\\errmessage`` -- expanding
        the macro in the paper halts compilation with the lane name and
        the reason, rather than silently rendering a placeholder number.
        """
        assert name not in self.seen, f"duplicate macro {name}"
        self.seen.add(name)
        msg = f"osdi_paper_macros: {name} is PENDING ({lane}): {reason}"
        value = r"\errmessage{" + msg.replace("{", "(").replace("}", ")") + "}"
        self.items.append((name, value, f"PENDING -- {lane}: {reason}"))

    def render(self) -> str:
        lines = [
            "% osdi-macros.tex --- GENERATED by scripts/osdi_paper_macros.py.",
            "% Do not hand-edit; re-run the generator.",
            "%",
            "% Every landed-record number in the manuscript resolves through one",
            "% of these; each was recomputed from row-level data and checked",
            "% against a frozen expectation in tests/test_osdi_paper_macros.py.",
            f"% This run performed {CHECKS} such assertions and refused to write",
            "% on any failure. A macro whose provenance begins 'PENDING' raises",
            "% a LaTeX error if the manuscript expands it -- its record has not",
            "% landed and no placeholder number is emitted for it.",
            "",
        ]
        width = max(len(n) for n, _, _ in self.items)
        for name, value, prov in self.items:
            pad = " " * (width - len(name))
            lines.append(f"\\newcommand{{\\{name}}}{{{value}}}{pad}  % {prov}")
        lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# C1 --- crash campaign (10,000 seeded trials, 0 problems)
# --------------------------------------------------------------------------

def compute_c1(m: Macros) -> None:
    d = json.loads(CRASH_V1.read_text(encoding="utf-8"))
    results = d["results"]
    per_boundary = d["per_boundary"]

    eq(len(results), 10000, "C1: crash campaign result row count")
    eq(d["total_trials"], len(results), "C1: total_trials matches len(results)")

    recomputed_problems = sum(1 for r in results if r["problems"])
    eq(recomputed_problems, 0, "C1: recomputed problem count over every trial row")
    eq(recomputed_problems, d["total_problems"],
       "C1: recomputed problems matches record's own total_problems")

    for r in results:
        require(r["q1_acked_survive"] and r["q2_deterministic"]
                and r["q3_single_generation"] and r["q4_orphans_reclaimed"],
                f"C1: trial {r['boundary']}/{r['trial']} failed a Q1-Q4 check")

    boundaries = sorted(per_boundary)
    eq(len(boundaries), 10, "C1: boundary count")
    per_boundary_trials = sum(v["trials"] for v in per_boundary.values())
    eq(per_boundary_trials, 10000, "C1: per_boundary trial counts sum to 10,000")
    for name, v in per_boundary.items():
        actual = [r for r in results if r["boundary"] == name]
        eq(len(actual), v["trials"], f"C1: {name} trial count matches per_boundary")
        eq(sum(1 for r in actual if r["problems"]), v["problems_total"],
           f"C1: {name} recomputed problems matches per_boundary")

    recomputed_wall = round(sum(r["wall_s"] for r in results), 2)
    eq(recomputed_wall, d["wall_s"], "C1: summed per-trial wall_s matches record wall_s")

    eq(d["total_trials"], 10000, "C1 frozen: total_trials")
    eq(recomputed_problems, 0, "C1 frozen: total_problems")
    eq(len(boundaries), 10, "C1 frozen: boundary count")
    eq(recomputed_wall, 4912.85, "C1 frozen: summed wall_s")

    m.add("osdiCrashTrials", tex_num(d["total_trials"]),
          f"{relpath(CRASH_V1)}: len(results), == total_trials")
    m.add("osdiCrashProblems", recomputed_problems,
          f"{relpath(CRASH_V1)}: trials with a non-empty problems list")
    m.add("osdiCrashBoundaries", len(boundaries),
          f"{relpath(CRASH_V1)}: distinct per_boundary keys")
    m.add("osdiCrashWall", f"{recomputed_wall:,.2f}".replace(",", "{,}"),
          f"{relpath(CRASH_V1)}: sum of results[*].wall_s, seconds")


# --------------------------------------------------------------------------
# C3 --- incremental manifest bytes (B1 A/B)
# --------------------------------------------------------------------------

def compute_c3(m: Macros) -> None:
    raw = json.loads(B1_RAW.read_text(encoding="utf-8"))
    b1a = raw["b1a"]
    ctl, trt = b1a["control"], b1a["treatment"]

    eq(ctl["stopped_at_ops"], 2_500_000, "C3: control stopped at 2.5M ops")
    eq(trt["stopped_at_ops"], 2_500_000, "C3: treatment stopped at 2.5M ops")
    eq(ctl["ops_series"][-1][0], 2_500_000, "C3: control ops_series last point is 2.5M ops")
    eq(trt["ops_series"][-1][0], 2_500_000, "C3: treatment ops_series last point is 2.5M ops")

    bytes_ctl = ctl["manifest_bytes_at_stop"]
    bytes_trt = trt["manifest_bytes_at_stop"]
    eq(bytes_ctl, 25_970_987_762, "C3 frozen: control manifest bytes at 2.5M ops")
    eq(bytes_trt, 62_046_913, "C3 frozen: treatment manifest bytes at 2.5M ops")
    ratio_bytes = bytes_ctl / bytes_trt
    close(ratio_bytes, 418.57, 0.01, "C3 frozen: manifest-byte reduction ratio (README's "
          "own prose rounds this to \"~419x\")")

    b1b = raw["b1b"]["summary"]
    total_ctl = b1b["control"]["total_us"]
    total_trt = b1b["treatment"]["total_us"]
    ratio_ctl = total_ctl["last_decile_us"] / total_ctl["first_decile_us"]
    ratio_trt = total_trt["last_decile_us"] / total_trt["first_decile_us"]
    close(ratio_ctl, total_ctl["ratio_last_over_first"], 0.005,
          "C3: control engine-commit decile ratio matches recorded field")
    close(ratio_trt, total_trt["ratio_last_over_first"], 0.005,
          "C3: treatment engine-commit decile ratio matches recorded field")
    close(ratio_trt, 1.798, 0.01, "C3 frozen: treatment engine-commit total_us decile ratio")
    require(ratio_trt > 1.2, "C3: F2's <=1.2x falsifier bar is refuted by the treatment ratio")

    b1c = raw["b1c"]["authoritative"]
    ctl_open = b1c["control_open_ms"]
    trt_open = b1c["treatment_open_ms"]
    eq(len(ctl_open), 3, "C3: control cold-open reps")
    eq(len(trt_open), 3, "C3: treatment cold-open reps")
    ctl_open_med = statistics.median(ctl_open)
    trt_open_med = statistics.median(trt_open)
    close(ctl_open_med, 1070, 2, "C3 frozen: control cold-open median, ms")
    close(trt_open_med, 8589, 2, "C3 frozen: treatment cold-open median, ms")
    cold_open_ratio = trt_open_med / ctl_open_med
    close(cold_open_ratio, 8.03, 0.02, "C3 frozen: cold-open slowdown ratio")
    require(trt_open_med > 100, "C3: F3's <=100ms falsifier bar is refuted by treatment open")

    m.add("osdiManifestBytesCtl", f"{bytes_ctl / 1e9:.2f}",
          f"{relpath(B1_RAW)}: b1a.control.manifest_bytes_at_stop, decimal GB")
    m.add("osdiManifestBytesTrt", f"{bytes_trt / 1e6:.1f}",
          f"{relpath(B1_RAW)}: b1a.treatment.manifest_bytes_at_stop, decimal MB")
    m.add("osdiManifestCommitRatio", f"{ratio_trt:.3f}",
          f"{relpath(B1_RAW)}: b1b.summary.treatment.total_us last/first decile "
          "(the F2 falsifier: refuted, bar <=1.2x)")
    m.add("osdiManifestColdOpen", f"{cold_open_ratio:.2f}",
          f"{relpath(B1_RAW)}: b1c.authoritative median(treatment_open_ms) / "
          "median(control_open_ms) at G~10k (the F3 falsifier: refuted, bar <=100ms)")


# --------------------------------------------------------------------------
# B1-v2 --- manifest format 3 (reader torn-tail fix) A/B, Lane B / W2f.
#
# Same Addendum-3 (manifest forecast) protocol as v1's B1 A/B above (C3):
# SF1 manifest bytes at matched 2.5M ops, SS20 batch=1 commitcost phase
# deciles/p50 (300 commits, 100k-row seed store), chain-open at G~10k
# K=512, plus the off-default K=128/K=1024 sweep (1 rep each). These
# osdiB1v2* macros stand beside the osdiManifest* ones above as v2-era
# measurements -- v1's numbers are never overwritten. No verdict macro:
# the brief's thresholds and pass/fail scoring live in the internal
# freeze doc, not here.
# --------------------------------------------------------------------------

def compute_b1_v2(m: Macros) -> None:
    manifest = json.loads(B1_V2_MANIFEST.read_text(encoding="utf-8"))
    raw = json.loads(B1_V2_RAW.read_text(encoding="utf-8"))

    # digest-check: the manifest's own result_digest is sha256 of its
    # `measurements` block (canonical, sort_keys JSON) -- this is the
    # manifest-format-3 record's own tamper-evidence, distinct from (and
    # in addition to) recomputing every figure from the raw record below.
    recomputed_digest = hashlib.sha256(
        json.dumps(manifest["measurements"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    eq(recomputed_digest, manifest["result_digest"],
       f"{relpath(B1_V2_MANIFEST)}: sha256(measurements, sort_keys=True) matches "
       "the manifest's own result_digest")

    ctl_commit = manifest["config"]["control_commit"]
    trt_commit = manifest["config"]["treatment_commit"]
    eq(ctl_commit[:7], "886805f", "B1-v2 frozen: control_commit short sha")
    eq(trt_commit[:7], "7a5ff98", "B1-v2 frozen: treatment_commit short sha")

    # --- B1(a): manifest/segment bytes + build ops/s at matched 2.5M ops ---
    b1a = raw["b1a"]
    ctl, trt = b1a["control"], b1a["treatment"]
    eq(ctl["stopped_at_ops"], 2_500_000, "B1-v2: control stopped at 2.5M ops")
    eq(trt["stopped_at_ops"], 2_500_000, "B1-v2: treatment stopped at 2.5M ops")
    eq(ctl["ops_series"][-1][0], 2_500_000,
       "B1-v2: control ops_series last point is 2.5M ops")
    eq(trt["ops_series"][-1][0], 2_500_000,
       "B1-v2: treatment ops_series last point is 2.5M ops")

    bytes_ctl = ctl["manifest_bytes_at_stop"]
    bytes_trt = trt["manifest_bytes_at_stop"]
    seg_ctl = ctl["segment_bytes_at_stop"]
    seg_trt = trt["segment_bytes_at_stop"]
    eq(bytes_ctl, 73_790_679, "B1-v2 frozen: control manifest bytes at 2.5M ops")
    eq(bytes_trt, 74_403_447, "B1-v2 frozen: treatment manifest bytes at 2.5M ops")
    eq(seg_ctl, 158_966_639, "B1-v2 frozen: control segment bytes at 2.5M ops")
    eq(seg_trt, 163_447_175, "B1-v2 frozen: treatment segment bytes at 2.5M ops")

    meas_bytes = manifest["measurements"]["bytes_at_2_5m_ops"]
    eq(bytes_ctl, meas_bytes["control_manifest_bytes"],
       "B1-v2: recomputed control manifest bytes matches manifest's own field")
    eq(bytes_trt, meas_bytes["treatment_manifest_bytes"],
       "B1-v2: recomputed treatment manifest bytes matches manifest's own field")
    eq(seg_ctl, meas_bytes["control_segment_bytes"],
       "B1-v2: recomputed control segment bytes matches manifest's own field")
    eq(seg_trt, meas_bytes["treatment_segment_bytes"],
       "B1-v2: recomputed treatment segment bytes matches manifest's own field")

    bytes_paired = bytes_trt / bytes_ctl
    close(bytes_paired, 1.008, 0.001,
          "B1-v2 frozen: manifest-bytes paired ratio treatment/control")

    ops_ctl_2_5m = ctl["ops_series"][-1][1]
    ops_trt_2_5m = trt["ops_series"][-1][1]
    eq(ops_ctl_2_5m, 2543, "B1-v2 frozen: control build ops/s at the 2.5M-op checkpoint")
    eq(ops_trt_2_5m, 5335, "B1-v2 frozen: treatment build ops/s at the 2.5M-op checkpoint")
    ops_ratio = ops_trt_2_5m / ops_ctl_2_5m
    close(ops_ratio, 2.098, 0.001,
          "B1-v2 frozen: build ops/s ratio (treatment/control) at 2.5M ops -- "
          "observation only, no claim attached (per the record's own note)")

    # --- B1(b): commitcost phase deciles / p50, K=512 default + K-sweep ---
    b1b = raw["b1b"]["raw"]

    def decile_ratio(rep: dict, field: str) -> float:
        return rep["last_decile_us"][field] / rep["first_decile_us"][field]

    def median_decile(reps: list, field: str) -> float:
        return statistics.median(decile_ratio(r, field) for r in reps)

    eq(len(b1b["control"]), 3, "B1-v2: control commitcost has 3 reps")
    eq(len(b1b["treatment"]), 3, "B1-v2: treatment commitcost has 3 reps")
    eq(len(b1b["treatment_k128"]), 1, "B1-v2: K=128 sweep is 1 rep")
    eq(len(b1b["treatment_k1024"]), 1, "B1-v2: K=1024 sweep is 1 rep")

    manifest_decile_ctl = median_decile(b1b["control"], "manifest_us")
    manifest_decile_trt = median_decile(b1b["treatment"], "manifest_us")
    total_decile_ctl = median_decile(b1b["control"], "total_us")
    total_decile_trt = median_decile(b1b["treatment"], "total_us")

    close(manifest_decile_ctl, 1.115, 0.001,
          "B1-v2 frozen: control manifest_us decile ratio (median of 3 reps)")
    close(manifest_decile_trt, 1.079, 0.001,
          "B1-v2 frozen: treatment manifest_us decile ratio (median of 3 reps)")
    close(total_decile_ctl, 1.733, 0.001,
          "B1-v2 frozen: control engine-commit total_us decile ratio (median of 3 reps)")
    close(total_decile_trt, 1.674, 0.001,
          "B1-v2 frozen: treatment engine-commit total_us decile ratio (median of 3 reps)")

    meas_decile = manifest["measurements"]["commitcost_decile"]
    md = meas_decile["manifest_us_first_last_decile_ratio"]
    td = meas_decile["engine_commit_total_us_first_last_decile_ratio"]
    close(manifest_decile_ctl, md["control_median"], 0.001,
          "B1-v2: recomputed control manifest_us decile matches manifest's own median")
    close(manifest_decile_trt, md["treatment_median"], 0.001,
          "B1-v2: recomputed treatment manifest_us decile matches manifest's own median")
    close(total_decile_ctl, td["control_median"], 0.001,
          "B1-v2: recomputed control total_us decile matches manifest's own median")
    close(total_decile_trt, td["treatment_median"], 0.001,
          "B1-v2: recomputed treatment total_us decile matches manifest's own median")

    manifest_decile_k128 = decile_ratio(b1b["treatment_k128"][0], "manifest_us")
    manifest_decile_k1024 = decile_ratio(b1b["treatment_k1024"][0], "manifest_us")
    close(manifest_decile_k128, 1.068, 0.001,
          "B1-v2 frozen: treatment K=128 manifest_us decile ratio (1 rep)")
    close(manifest_decile_k1024, 1.150, 0.001,
          "B1-v2 frozen: treatment K=1024 manifest_us decile ratio (1 rep)")

    p50_ctl = statistics.median(r["phase_p50_us"]["total_us"] for r in b1b["control"])
    p50_trt = statistics.median(r["phase_p50_us"]["total_us"] for r in b1b["treatment"])
    eq(p50_ctl, 5430, "B1-v2 frozen: control engine-commit p50 total_us (median of 3 reps)")
    eq(p50_trt, 5478, "B1-v2 frozen: treatment engine-commit p50 total_us (median of 3 reps)")

    meas_p50 = manifest["measurements"]["commitcost_p50"]
    eq(p50_ctl, meas_p50["control_median_us"],
       "B1-v2: recomputed control p50 matches manifest's own field")
    eq(p50_trt, meas_p50["treatment_median_us"],
       "B1-v2: recomputed treatment p50 matches manifest's own field")

    p50_paired = p50_trt / p50_ctl
    close(p50_paired, 1.009, 0.001, "B1-v2 frozen: p50 paired ratio treatment/control")
    close(p50_paired, meas_p50["paired_ratio_treatment_over_control"], 0.001,
          "B1-v2: recomputed p50 paired ratio matches manifest's own field")

    # --- B1(c): cold/warm chain-open at G~10k ---
    b1c = raw["b1c"]["authoritative"]
    ctl_open = b1c["control_open_ms"]
    trt_open = b1c["treatment_open_ms"]
    eq(len(ctl_open), 3, "B1-v2: control cold-open reps")
    eq(len(trt_open), 3, "B1-v2: treatment cold-open reps")
    ctl_open_med = statistics.median(ctl_open)
    trt_open_med = statistics.median(trt_open)
    close(ctl_open_med, 2247.9, 0.05, "B1-v2 frozen: control cold-open median, ms")
    close(trt_open_med, 1705.3, 0.05, "B1-v2 frozen: treatment cold-open median, ms")

    meas_open = manifest["measurements"]["chain_open_cold_warm"]
    close(ctl_open_med, meas_open["control_median_ms"], 0.05,
          "B1-v2: recomputed control open median matches manifest's own field")
    close(trt_open_med, meas_open["treatment_median_ms"], 0.05,
          "B1-v2: recomputed treatment open median matches manifest's own field")

    open_paired = trt_open_med / ctl_open_med
    close(open_paired, 0.76, 0.005,
          "B1-v2 frozen: cold-open paired ratio treatment/control")

    ctl_generation = b1c["control_generation"]
    trt_generation = b1c["treatment_generation"]
    eq(ctl_generation, 10759, "B1-v2 frozen: control chain-open generation (G)")
    eq(trt_generation, 11029, "B1-v2 frozen: treatment chain-open generation (G)")

    # The checkpoint-load vs. delta-replay component split was not measured:
    # NativeAdapter() is a single opaque constructor with no internal phase
    # timer exposed to Python (unlike the commit path's phase_p50_us).
    # Assert the record says so honestly rather than silently treating a
    # missing split as zero.
    require(b1c["component_breakdown"] is None,
            "B1-v2: component_breakdown is genuinely absent (flagged), not fabricated")

    # --- emit macros ---
    m.add("osdiB1v2ControlCommit", ctl_commit[:7],
          f"{relpath(B1_V2_MANIFEST)}: config.control_commit, short sha")
    m.add("osdiB1v2TreatmentCommit", trt_commit[:7],
          f"{relpath(B1_V2_MANIFEST)}: config.treatment_commit, short sha")

    m.add("osdiB1v2BytesControlMB", f"{bytes_ctl / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.control.manifest_bytes_at_stop, decimal MB")
    m.add("osdiB1v2BytesTreatmentMB", f"{bytes_trt / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.manifest_bytes_at_stop, decimal MB")
    m.add("osdiB1v2BytesPaired", f"{bytes_paired:.3f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.manifest_bytes_at_stop / "
          "b1a.control.manifest_bytes_at_stop")
    m.add("osdiB1v2SegmentBytesControlMB", f"{seg_ctl / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.control.segment_bytes_at_stop, decimal MB")
    m.add("osdiB1v2SegmentBytesTreatmentMB", f"{seg_trt / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.segment_bytes_at_stop, decimal MB")

    m.add("osdiB1v2ManifestDecileControl", f"{manifest_decile_ctl:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].last_decile_us.manifest_us / "
          "[*].first_decile_us.manifest_us, median over 3 reps")
    m.add("osdiB1v2ManifestDecileTreatment", f"{manifest_decile_trt:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].last_decile_us.manifest_us / "
          "[*].first_decile_us.manifest_us, median over 3 reps")
    m.add("osdiB1v2ManifestDecileK128", f"{manifest_decile_k128:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment_k128[0].last_decile_us.manifest_us / "
          "first_decile_us.manifest_us (1 rep)")
    m.add("osdiB1v2ManifestDecileK1024", f"{manifest_decile_k1024:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment_k1024[0].last_decile_us.manifest_us / "
          "first_decile_us.manifest_us (1 rep)")

    m.add("osdiB1v2TotalDecileControl", f"{total_decile_ctl:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].last_decile_us.total_us / "
          "[*].first_decile_us.total_us, median over 3 reps")
    m.add("osdiB1v2TotalDecileTreatment", f"{total_decile_trt:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].last_decile_us.total_us / "
          "[*].first_decile_us.total_us, median over 3 reps")

    m.add("osdiB1v2P50ControlMs", f"{p50_ctl / 1000:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].phase_p50_us.total_us, "
          "median over 3 reps, /1000, ms")
    m.add("osdiB1v2P50TreatmentMs", f"{p50_trt / 1000:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].phase_p50_us.total_us, "
          "median over 3 reps, /1000, ms")
    m.add("osdiB1v2P50Paired", f"{p50_paired:.3f}",
          f"{relpath(B1_V2_RAW)}: median(treatment[*].phase_p50_us.total_us) / "
          "median(control[*].phase_p50_us.total_us)")

    m.add("osdiB1v2OpenControlMs", f"{ctl_open_med:.1f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.control_open_ms), 3 reps, ms")
    m.add("osdiB1v2OpenTreatmentMs", f"{trt_open_med:.1f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.treatment_open_ms), 3 reps, ms")
    m.add("osdiB1v2OpenPaired", f"{open_paired:.2f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.treatment_open_ms) / "
          "median(b1c.authoritative.control_open_ms)")
    m.add("osdiB1v2OpenControlGeneration", tex_num(ctl_generation),
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.control_generation")
    m.add("osdiB1v2OpenTreatmentGeneration", tex_num(trt_generation),
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.treatment_generation")

    m.add("osdiB1v2BuildOpsPerSecRatioAt2p5M", f"{ops_ratio:.2f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.ops_series[-1][1] (5335) / "
          "b1a.control.ops_series[-1][1] (2543), build ops/s at the 2.5M-op "
          "checkpoint -- observation only, no claim attached")

    m.add("osdiB1v2OpenComponentStatus", "not computed",
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.component_breakdown is null -- "
          "NativeAdapter() exposes no internal phase timer to split checkpoint-load "
          "from delta-replay (text macro, not a number -- see "
          "b1c.authoritative.component_breakdown_note)")


# --------------------------------------------------------------------------
# B1-v2e --- remeasure of the two B1-v2 cells that A/B could not score,
# under the fully-timed B1-v2d harness (`open_phase_us`, `build_info`,
# per-commit phase decile/residual fields).
#
# `osdiB1v2*ManifestDecile*`/`osdiB1v2TotalDecile*`/`osdiB1v2P50*` (the v2
# commit-cost treatment macros above) are LEFT UNCHANGED here -- values and
# all -- but the B1-v2 A/B's 2026-09-15 README correction found their
# provenance strings understate what they actually measured: the v2 A/B's
# commit-cost "treatment" reps wrote manifest records sized like the
# format-2 control (1,565-1,568 B) rather than format 3 (1,592-1,595 B),
# because `Manifest::digest()` (`crates/tgms-engine-core/src/manifest.rs`
# :615-636) picks the digest rule from the manifest's own `format` field --
# a format-2 chain pays the O(segments) `legacy_body_sha` fallback no
# matter which binary is timing it. Those macros' provenance is relabeled
# below (`_VOID_AS_FORMAT3` appended) without touching a single digit; this
# is prose correction, the same discipline as the README's own "Correction
# (2026-09-15)" paragraph, applied to the generated macros file.
#
# The `osdiB1v2e*` macros below are new: recomputed from
# `b1-manifest-v2e-remeasure-2026-09{,-raw}.json`, the re-measurement that
# supersedes v2's commit-cost and chain-open cells (treatment `e5d4171`,
# built and verified on a genuine format-3 chain -- `build_info()` and the
# 1,592-1,595 B manifest records both confirm it). No verdict macro here
# either, same convention as v1/v2.
# --------------------------------------------------------------------------

_VOID_AS_FORMAT3 = (
    " -- VOID AS A FORMAT-3 MEASUREMENT (2026-09-15 correction): this "
    "treatment rep's manifest records are 1,565-1,568 B, byte-identical in "
    "size to the format-2 control, not the 1,592-1,595 B a format-3 chain "
    "writes -- Manifest::digest() (manifest.rs:615-636) dispatches on the "
    "manifest's own `format` field, so this arm measured a format-2 chain "
    "(O(segments) legacy_body_sha) under the format-3 binary, not the "
    "format-3 path. Superseded for commit cost and chain-open by "
    "osdiB1v2e* below; values here are unchanged (frozen as measured)."
)


def _void_b1_v2_treatment_provenance(m: Macros) -> None:
    """Append `_VOID_AS_FORMAT3` to the v2 commit-cost treatment macros'
    provenance strings, in place, without touching their values.

    `Macros` stores `(name, value, provenance)` tuples in `m.items` and
    guards against duplicate names via `m.seen` -- there is no public
    "amend a provenance string" method, so this rewrites the tuple in
    `m.items` directly by name. Called right after `compute_b1_v2` so every
    other macro it emitted is already in `m.items` to relabel.
    """
    voided = {
        "osdiB1v2TotalDecileTreatment", "osdiB1v2ManifestDecileTreatment",
        "osdiB1v2ManifestDecileK128", "osdiB1v2ManifestDecileK1024",
        "osdiB1v2P50TreatmentMs", "osdiB1v2P50Paired",
    }
    found = set()
    for i, (name, value, provenance) in enumerate(m.items):
        if name in voided:
            m.items[i] = (name, value, provenance + _VOID_AS_FORMAT3)
            found.add(name)
    eq(found, voided, "osdi_paper_macros: every v2 commit-cost treatment "
       "macro named for relabeling was actually emitted by compute_b1_v2")


def compute_b1_v2e(m: Macros) -> None:
    manifest = json.loads(B1_V2E_MANIFEST.read_text(encoding="utf-8"))
    raw_bytes = B1_V2E_RAW.read_bytes()
    raw = json.loads(raw_bytes)

    # digest-check: unlike B1-v2's own manifest (sha256 of its *own*
    # `measurements` block), this record's `result_digest` is the sha256 of
    # the raw-records *file's bytes* -- matching both the raw file's own
    # committed sha256 in the README's "Records and transfer" section and
    # the summary manifest's `result_digest` field verbatim. Recomputed from
    # the file this module actually reads, not copied from either document.
    recomputed_digest = hashlib.sha256(raw_bytes).hexdigest()
    eq(recomputed_digest, manifest["result_digest"],
       f"{relpath(B1_V2E_RAW)}: sha256(raw file bytes) matches the summary "
       f"manifest's ({relpath(B1_V2E_MANIFEST)}) own result_digest")

    trt_commit = manifest["git_commit"]
    eq(trt_commit[:7], "e5d4171", "B1-v2e frozen: treatment_commit short sha")

    # --- cell (b): commit-cost phase attribution, K=512, 3 reps/arm ---
    cb = raw["cell_b_commitcost"]
    trt_reps, ctl_reps = cb["treatment_reps_full"], cb["control_reps_full"]
    eq(len(trt_reps), 3, "B1-v2e: treatment commitcost has 3 reps")
    eq(len(ctl_reps), 3, "B1-v2e: control commitcost has 3 reps")

    def decile_ratio(rep: dict) -> float:
        return rep["last_decile_us"]["total_us"] / rep["first_decile_us"]["total_us"]

    total_decile_trt = statistics.median(decile_ratio(r) for r in trt_reps)
    total_decile_ctl = statistics.median(decile_ratio(r) for r in ctl_reps)
    close(total_decile_trt, 1.017, 0.001,
          "B1-v2e frozen: treatment engine-commit total_us decile ratio (median of 3 reps)")
    close(total_decile_ctl, 1.696, 0.001,
          "B1-v2e frozen: control engine-commit total_us decile ratio (median of 3 reps)")

    meas_cb = manifest["measurements"]["cell_b_commitcost"]
    close(total_decile_trt, meas_cb["treatment_decile_ratio"], 0.001,
          "B1-v2e: recomputed treatment decile ratio matches manifest's own field")
    close(total_decile_ctl, meas_cb["control_decile_ratio"], 0.001,
          "B1-v2e: recomputed control decile ratio matches manifest's own field")

    # manifest_bytes -- the format evidence itself: format 3 is 1,592-1,595 B,
    # format 2 is 1,565-1,568 B (this is exactly what the B1-v2 README
    # correction checked to find the v2 A/B's treatment reps mislabeled).
    trt_first_bytes = {r["first_decile_us"]["manifest_bytes"] for r in trt_reps}
    ctl_first_bytes = {r["first_decile_us"]["manifest_bytes"] for r in ctl_reps}
    eq(trt_first_bytes, {1592}, "B1-v2e frozen: every treatment rep's "
       "first-decile manifest_bytes is 1,592 B (format 3)")
    eq(ctl_first_bytes, {1565}, "B1-v2e frozen: every control rep's "
       "first-decile manifest_bytes is 1,565 B (format 2, unchanged engine)")
    manifest_bytes_trt = next(iter(trt_first_bytes))
    manifest_bytes_ctl = next(iter(ctl_first_bytes))

    # residual_first_us/residual_last_us -- the B1V2_AB_DIAGNOSIS memo's own
    # Q1 metric, mean over reps (the record's own field name says so:
    # `*_mean_of_reps`; this is a mean, not a median, deliberately -- a mean
    # would show a fat-tailed residual a median could hide).
    residual_first = statistics.fmean(r["residual_first_us"] for r in trt_reps)
    residual_last = statistics.fmean(r["residual_last_us"] for r in trt_reps)
    close(residual_first, meas_cb["treatment_residual_first_us_mean_of_reps"], 0.01,
          "B1-v2e: recomputed treatment residual_first_us (mean of reps) matches "
          "manifest's own field")
    close(residual_last, meas_cb["treatment_residual_last_us_mean_of_reps"], 0.01,
          "B1-v2e: recomputed treatment residual_last_us (mean of reps) matches "
          "manifest's own field")

    # engine-commit p50 -- phase_p50_us.total_us (the commit's engine-side
    # total, excluding wal_us/apply_us), median of reps. This is the frozen
    # Addendum 3 quantity: v1's README reports it as "engine-commit p50
    # (total_us)", the 5.07 ms baseline and 5.58 ms bar are this quantity,
    # and osdiB1v2P50* (5.478/5.430) already tracks it -- osdiB1v2eP50*
    # below is the same quantity for this remeasurement, not a different
    # one. The manifest's own digested measurements block reports it too,
    # and the README's prose quotes it (paired ratio 0.715x).
    engine_p50_trt = statistics.median(r["phase_p50_us"]["total_us"] for r in trt_reps)
    engine_p50_ctl = statistics.median(r["phase_p50_us"]["total_us"] for r in ctl_reps)
    eq(engine_p50_trt, 3365,
       "B1-v2e frozen: treatment engine-internal p50 total_us (median of 3 reps)")
    eq(engine_p50_ctl, 4704,
       "B1-v2e frozen: control engine-internal p50 total_us (median of 3 reps)")
    eq(engine_p50_trt, meas_cb["treatment_p50_median_us"],
       "B1-v2e: recomputed treatment engine-internal p50 matches manifest's own field")
    eq(engine_p50_ctl, meas_cb["control_p50_median_us"],
       "B1-v2e: recomputed control engine-internal p50 matches manifest's own field")

    engine_p50_paired = engine_p50_trt / engine_p50_ctl
    close(engine_p50_paired, 0.715, 0.001,
          "B1-v2e frozen: engine-internal p50 paired ratio treatment/control")
    close(engine_p50_paired, meas_cb["paired_p50_ratio_treatment_over_control"], 0.001,
          "B1-v2e: recomputed engine-internal p50 paired ratio matches manifest's own field")

    # wall-clock p50 -- commit_ms.p50 (the full per-commit latency
    # `_timed_write` measures: wal fsync + Python-side eventlog append +
    # apply_ops + engine commit), median of reps. This is *not* the frozen
    # Addendum 3 quantity above -- it is reported separately, under its own
    # osdiB1v2eWallP50* names, so a future reader of this file never
    # mistakes it for the number the paper's threshold actually binds.
    wall_p50_trt = statistics.median(r["commit_ms"]["p50"] for r in trt_reps)
    wall_p50_ctl = statistics.median(r["commit_ms"]["p50"] for r in ctl_reps)
    close(wall_p50_trt, 4.274, 0.001,
          "B1-v2e frozen: treatment wall-clock commit_ms.p50 (median of 3 reps)")
    close(wall_p50_ctl, 5.303, 0.001,
          "B1-v2e frozen: control wall-clock commit_ms.p50 (median of 3 reps)")

    wall_p50_paired = wall_p50_trt / wall_p50_ctl
    close(wall_p50_paired, 0.806, 0.001,
          "B1-v2e frozen: wall-clock p50 paired ratio treatment/control")

    # --- cell (a): chain-open component split at G~10k, K=512 ---
    ca = raw["cell_a_chain_open"]
    trt_open, ctl_open = ca["treatment"], ca["control"]
    meas_ca = manifest["measurements"]["cell_a_chain_open"]

    trt_generation = trt_open["generation"]
    ctl_generation = ctl_open["generation"]
    eq(trt_generation, 10365, "B1-v2e frozen: treatment chain-open generation (G)")
    eq(ctl_generation, 10052, "B1-v2e frozen: control chain-open generation (G)")
    eq(trt_generation, meas_ca["treatment_generation"],
       "B1-v2e: recomputed treatment generation matches manifest's own field")
    eq(ctl_generation, meas_ca["control_generation"],
       "B1-v2e: recomputed control generation matches manifest's own field")

    phases = trt_open["open_phase_p50_us"]
    eq(phases["chain_format"], 3, "B1-v2e frozen: treatment open chain_format is 3")
    checkpoint_us = phases["checkpoint_read_parse_us"]
    merkle_us = phases["merkle_verify_us"]
    state_build_us = phases["state_build_us"]
    delta_replay_us = phases["delta_replay_us"]
    dictionary_us = phases["dictionary_open_us"]
    total_us = phases["total_us"]

    eq(checkpoint_us, 42124, "B1-v2e frozen: checkpoint_read_parse_us (median)")
    eq(merkle_us, 40204, "B1-v2e frozen: merkle_verify_us (median)")
    eq(state_build_us, 1828, "B1-v2e frozen: state_build_us (median)")
    eq(delta_replay_us, 2899, "B1-v2e frozen: delta_replay_us (median)")
    eq(dictionary_us, 1066460, "B1-v2e frozen: dictionary_open_us (median)")
    eq(total_us, 1191773, "B1-v2e frozen: open total_us (median)")

    component_us = checkpoint_us + merkle_us + state_build_us + delta_replay_us
    eq(component_us, 87055, "B1-v2e: recomputed manifest-chain component "
       "(checkpoint+merkle+state_build+delta_replay) sums to 87,055 us")
    eq(component_us, meas_ca["treatment_manifest_chain_component_us_median"],
       "B1-v2e: recomputed manifest-chain component matches manifest's own field")
    breakdown = meas_ca["treatment_manifest_chain_component_breakdown_us_median"]
    eq(checkpoint_us, breakdown["checkpoint_read_parse_us"],
       "B1-v2e: recomputed checkpoint_read_parse_us matches manifest's own field")
    eq(merkle_us, breakdown["merkle_verify_us"],
       "B1-v2e: recomputed merkle_verify_us matches manifest's own field")
    eq(state_build_us, breakdown["state_build_us"],
       "B1-v2e: recomputed state_build_us matches manifest's own field")
    eq(delta_replay_us, breakdown["delta_replay_us"],
       "B1-v2e: recomputed delta_replay_us matches manifest's own field")
    eq(dictionary_us, meas_ca["treatment_dictionary_open_us_median"],
       "B1-v2e: recomputed dictionary_open_us matches manifest's own field")
    eq(total_us, meas_ca["treatment_total_us_median"],
       "B1-v2e: recomputed open total_us matches manifest's own field")

    # control's chain-open is flagged confounded by the concurrent phase-2
    # backup transfer (raw record's own caveat) -- not a measurement of
    # relative open cost, so no control-side open macro is emitted, and no
    # paired ratio either.
    require(ctl_open["component_breakdown"] is not None
            and "not available" in ctl_open["component_breakdown"],
            "B1-v2e: control component_breakdown is genuinely absent (flagged), "
            "not fabricated")
    require("caveat" in meas_ca and "backup" in meas_ca["caveat"].lower(),
            "B1-v2e: manifest's own measurements record the concurrent-backup "
            "caveat for cell (a)")

    # --- emit macros ---
    m.add("osdiB1v2eTreatmentCommit", trt_commit[:7],
          f"{relpath(B1_V2E_MANIFEST)}: git_commit, short sha")

    m.add("osdiB1v2eTotalDecileTreatment", f"{total_decile_trt:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "last_decile_us.total_us / [*].first_decile_us.total_us, median over 3 reps "
          "-- supersedes osdiB1v2TotalDecileTreatment, which measured a format-2 chain")
    m.add("osdiB1v2eTotalDecileControl", f"{total_decile_ctl:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "last_decile_us.total_us / [*].first_decile_us.total_us, median over 3 reps")

    m.add("osdiB1v2eResidualFirstUs", f"{residual_first:.2f}",
          f"{relpath(B1_V2E_RAW)}: mean(cell_b_commitcost.treatment_reps_full[*]."
          "residual_first_us) over 3 reps -- the B1V2_AB_DIAGNOSIS memo's Q1 metric, "
          "now ~30us against a ~3,300-3,400us total_us (closed, not hidden)")
    m.add("osdiB1v2eResidualLastUs", f"{residual_last:.2f}",
          f"{relpath(B1_V2E_RAW)}: mean(cell_b_commitcost.treatment_reps_full[*]."
          "residual_last_us) over 3 reps")

    m.add("osdiB1v2eP50TreatmentMs", f"{engine_p50_trt / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "phase_p50_us.total_us, median over 3 reps, /1000, ms -- engine-commit p50 "
          "(total_us), the Addendum 3 quantity, same as osdiB1v2P50*")
    m.add("osdiB1v2eP50ControlMs", f"{engine_p50_ctl / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "phase_p50_us.total_us, median over 3 reps, /1000, ms -- engine-commit p50 "
          "(total_us), the Addendum 3 quantity, same as osdiB1v2P50*")
    m.add("osdiB1v2eP50Paired", f"{engine_p50_paired:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(treatment[*].phase_p50_us.total_us) / "
          "median(control[*].phase_p50_us.total_us) -- engine-commit p50 (total_us), "
          "the Addendum 3 quantity, same as osdiB1v2P50*")

    m.add("osdiB1v2eWallP50TreatmentMs", f"{wall_p50_trt:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(cell_b_commitcost.treatment_reps_full[*]."
          "commit_ms.p50) over 3 reps, ms -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")
    m.add("osdiB1v2eWallP50ControlMs", f"{wall_p50_ctl:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(cell_b_commitcost.control_reps_full[*]."
          "commit_ms.p50) over 3 reps, ms -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")
    m.add("osdiB1v2eWallPaired", f"{wall_p50_paired:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(treatment[*].commit_ms.p50) / "
          "median(control[*].commit_ms.p50) -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")

    m.add("osdiB1v2eOpenComponentMs", f"{component_us / 1000:.2f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us -- "
          "checkpoint_read_parse_us + merkle_verify_us + state_build_us + "
          "delta_replay_us, /1000, ms")
    m.add("osdiB1v2eOpenCheckpointMs", f"{checkpoint_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "checkpoint_read_parse_us, /1000, ms")
    m.add("osdiB1v2eOpenMerkleVerifyMs", f"{merkle_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "merkle_verify_us, /1000, ms")
    m.add("osdiB1v2eOpenStateBuildMs", f"{state_build_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "state_build_us, /1000, ms")
    m.add("osdiB1v2eOpenDeltaReplayMs", f"{delta_replay_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "delta_replay_us, /1000, ms")
    m.add("osdiB1v2eOpenDictionaryMs", f"{dictionary_us / 1000:.1f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "dictionary_open_us, /1000, ms -- dominates the open (89.5% of total_us)")
    m.add("osdiB1v2eOpenTotalMs", f"{total_us / 1000:.1f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "total_us, /1000, ms")
    m.add("osdiB1v2eOpenGeneration", tex_num(trt_generation),
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.generation")

    m.add("osdiB1v2eControlOpenStatus", "confounded (concurrent backup transfer)",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.control's open time (9.6-10.4s) "
          "was measured while a phase-2 tar backup ran concurrently on xzgpu (text "
          "macro, not a number -- see cell_a_chain_open.control and the manifest's "
          "own measurements.cell_a_chain_open.caveat)")

    m.add("osdiB1v2eManifestBytesTreatment", tex_num(manifest_bytes_trt),
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "first_decile_us.manifest_bytes, constant across 3 reps -- the format-3 "
          "evidence (versus the v2 A/B's mislabeled treatment, which matched "
          "osdiB1v2eManifestBytesControl instead)")
    m.add("osdiB1v2eManifestBytesControl", tex_num(manifest_bytes_ctl),
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "first_decile_us.manifest_bytes, constant across 3 reps -- the format-2 "
          "byte size the v2 A/B's treatment reps actually matched")


# --------------------------------------------------------------------------
# C3 (cont.) --- Lane P-CO7: chain-open re-measurement on a confirmed-quiet
# host (same v2e B1(c) cell, treatment ebe1dc2, re-run because the v2e lane's
# control open time was confounded by a concurrent tar backup). Every
# component figure below is a per-field median recomputed from the raw
# record's 3-rep ``open_phase_us`` array, cross-checked against that same
# record's own precomputed ``open_phase_p50_us`` aggregate and against the
# summary manifest's own quoted fields -- never read off either aggregate
# without first recomputing it from the reps underneath.
# --------------------------------------------------------------------------

def compute_b1_co7(m: Macros) -> None:
    manifest = json.loads(B1_CO7_MANIFEST.read_text(encoding="utf-8"))
    raw_bytes = B1_CO7_RAW.read_bytes()
    raw = json.loads(raw_bytes)

    # digest-check: same discipline as B1-v2e -- result_digest is sha256 of
    # the raw records file's own bytes, recomputed from the file this module
    # actually reads, not copied from either document.
    recomputed_digest = hashlib.sha256(raw_bytes).hexdigest()
    eq(recomputed_digest, manifest["result_digest"],
       f"{relpath(B1_CO7_RAW)}: sha256(raw file bytes) matches the summary "
       f"manifest's ({relpath(B1_CO7_MANIFEST)}) own result_digest")

    trt_commit = manifest["git_commit"]
    eq(trt_commit, raw["treatment_commit"],
       "P-CO7: manifest's git_commit matches the raw record's own treatment_commit")
    eq(trt_commit, "ebe1dc2", "P-CO7 frozen: treatment commit")

    ca = raw["cell_a_chain_open"]
    trt, ctl = ca["treatment"], ca["control"]
    meas_ca = manifest["measurements"]["cell_a_chain_open"]

    reps = trt["open_phase_us"]
    eq(len(reps), 3, "P-CO7: treatment chain-open has 3 reps")

    def med(field: str) -> float:
        return statistics.median(r[field] for r in reps)

    checkpoint_us = med("checkpoint_read_parse_us")
    merkle_us = med("merkle_verify_us")
    state_build_us = med("state_build_us")
    delta_replay_us = med("delta_replay_us")
    dictionary_us = med("dictionary_open_us")
    total_us = med("total_us")

    eq(checkpoint_us, 12670, "P-CO7 frozen: checkpoint_read_parse_us (median of 3 reps)")
    eq(merkle_us, 34858, "P-CO7 frozen: merkle_verify_us (median of 3 reps)")
    eq(state_build_us, 5935, "P-CO7 frozen: state_build_us (median of 3 reps)")
    eq(delta_replay_us, 6256, "P-CO7 frozen: delta_replay_us (median of 3 reps)")
    eq(dictionary_us, 1092729, "P-CO7 frozen: dictionary_open_us (median of 3 reps)")
    eq(total_us, 1155032, "P-CO7 frozen: open total_us (median of 3 reps)")

    # cross-check the recomputed per-field medians against the raw record's
    # own precomputed open_phase_p50_us aggregate
    p50 = trt["open_phase_p50_us"]
    eq(checkpoint_us, p50["checkpoint_read_parse_us"],
       "P-CO7: recomputed checkpoint_read_parse_us matches raw record's own open_phase_p50_us")
    eq(merkle_us, p50["merkle_verify_us"],
       "P-CO7: recomputed merkle_verify_us matches raw record's own open_phase_p50_us")
    eq(state_build_us, p50["state_build_us"],
       "P-CO7: recomputed state_build_us matches raw record's own open_phase_p50_us")
    eq(delta_replay_us, p50["delta_replay_us"],
       "P-CO7: recomputed delta_replay_us matches raw record's own open_phase_p50_us")
    eq(dictionary_us, p50["dictionary_open_us"],
       "P-CO7: recomputed dictionary_open_us matches raw record's own open_phase_p50_us")
    eq(total_us, p50["total_us"],
       "P-CO7: recomputed open total_us matches raw record's own open_phase_p50_us")

    # ... and against the summary manifest's own quoted fields
    eq(checkpoint_us, meas_ca["treatment_checkpoint_read_parse_us_median"],
       "P-CO7: recomputed checkpoint_read_parse_us matches manifest's own field")
    eq(merkle_us, meas_ca["treatment_merkle_verify_us_median"],
       "P-CO7: recomputed merkle_verify_us matches manifest's own field")
    eq(state_build_us, meas_ca["treatment_state_build_us_median"],
       "P-CO7: recomputed state_build_us matches manifest's own field")
    eq(delta_replay_us, meas_ca["treatment_delta_replay_us_median"],
       "P-CO7: recomputed delta_replay_us matches manifest's own field")
    eq(dictionary_us, meas_ca["treatment_dictionary_open_us_median"],
       "P-CO7: recomputed dictionary_open_us matches manifest's own field")
    eq(total_us, meas_ca["treatment_total_us_median"],
       "P-CO7: recomputed open total_us matches manifest's own field")

    component_us = checkpoint_us + merkle_us + state_build_us + delta_replay_us
    eq(component_us, 59719, "P-CO7: recomputed manifest-chain component "
       "(checkpoint+merkle+state_build+delta_replay) sums to 59,719 us")
    eq(component_us, ca["manifest_chain_component_us_treatment_median"]["sum_us"],
       "P-CO7: recomputed component sum matches raw record's own sum_us field")
    eq(component_us, meas_ca["treatment_manifest_chain_component_us_median_sum"],
       "P-CO7: recomputed component sum matches manifest's own field")

    # delta_count is constant across reps (K=512 checkpoint cadence)
    delta_counts = {r["delta_count"] for r in reps}
    eq(delta_counts, {388}, "P-CO7 frozen: every treatment rep's delta_count is 388")
    delta_count = next(iter(delta_counts))

    trt_generation = trt["generation"]
    eq(trt_generation, 10116, "P-CO7 frozen: treatment chain-open generation (G)")
    eq(trt_generation, meas_ca["treatment_generation"],
       "P-CO7: recomputed treatment generation matches manifest's own field")

    checkpoint_generation = trt_generation - delta_count
    eq(checkpoint_generation, 9728,
       "P-CO7: recomputed checkpoint generation (generation - delta_count)")
    require(checkpoint_generation % 512 == 0,
            "P-CO7: checkpoint generation is a multiple of K=512, consistent with the "
            "chain's checkpoint cadence")

    state_build_per_delta_us = state_build_us / delta_count
    delta_replay_per_delta_us = delta_replay_us / delta_count
    close(state_build_per_delta_us, 15.3, 0.05,
          "P-CO7 frozen: state_build_us / delta_count (us/delta)")
    close(delta_replay_per_delta_us, 16.1, 0.05,
          "P-CO7 frozen: delta_replay_us / delta_count (us/delta)")

    # --- control ---
    ctl_generation = ctl["generation"]
    eq(ctl_generation, 10042, "P-CO7 frozen: control chain-open generation (G)")
    eq(ctl_generation, meas_ca["control_generation"],
       "P-CO7: recomputed control generation matches manifest's own field")

    ctl_reps_ms = ctl["open_ms"]
    eq(len(ctl_reps_ms), 3, "P-CO7: control chain-open has 3 reps")
    ctl_open_ms = statistics.median(ctl_reps_ms)
    close(ctl_open_ms, 10100.238, 0.001,
          "P-CO7 frozen: control open_ms (median of 3 reps)")
    eq(ctl_open_ms, ctl["open_ms_median"],
       "P-CO7: recomputed control open_ms median matches raw record's own field")
    close(ctl_open_ms, meas_ca["control_open_ms_median_wallclock"], 0.001,
          "P-CO7: recomputed control open_ms matches manifest's own field")

    require(ctl["component_breakdown"] is not None
            and "not available" in ctl["component_breakdown"],
            "P-CO7: control component_breakdown is genuinely absent (flagged), "
            "not fabricated -- the pinned control engine predates open_phase_us()")

    ctl_delta_count = ctl_generation % 512
    eq(ctl_delta_count, 314, "P-CO7: recomputed control delta_count (control_generation mod K=512)")

    # control has no phase breakdown; the treatment's measured dictionary-open
    # time is used only as an *estimate* of the control's own dictionary-open
    # cost (both arms open the same dictionary format) to back out an
    # approximate per-delta manifest-chain cost for the control arm. This is
    # explicitly an estimate built from one measured term and one measured
    # total, not a second independent measurement.
    dictionary_ms = dictionary_us / 1000
    ctl_per_delta_ms = (ctl_open_ms - dictionary_ms) / ctl_delta_count
    close(ctl_per_delta_ms, 28.7, 0.05,
          "P-CO7 frozen: estimated control per-delta manifest-chain cost "
          "((control open_ms - treatment's measured dictionary_open_ms estimate) / "
          "control delta_count)")

    open_ratio = (total_us / 1000) / ctl_open_ms
    close(open_ratio, 0.114, 0.001,
          "P-CO7 frozen: treatment/control open ratio (treatment total_us/1000 / "
          "control open_ms)")
    close(open_ratio, meas_ca["total_open_ratio_treatment_over_control"], 0.001,
          "P-CO7: recomputed open ratio matches manifest's own field")

    # --- derived (not measurements): projected worst-phase open cost at
    # K-1=511 deltas, the last generation before the next checkpoint reset --
    # arithmetic on the measured per-delta costs above, clearly not itself a
    # measurement.
    worst_phase_deltas = 511
    worst_phase_open_trt_ms = (
        checkpoint_us / 1000 + merkle_us / 1000
        + (state_build_per_delta_us + delta_replay_per_delta_us) * worst_phase_deltas / 1000
    )
    close(worst_phase_open_trt_ms, 63.6, 0.1,
          "P-CO7 derived (arithmetic, not measured): projected treatment manifest-chain "
          "open at K-1=511 deltas (checkpoint_ms + merkle_ms + "
          "(state_build_per_delta_us + delta_replay_per_delta_us) * 511 / 1000)")

    worst_phase_open_ctl_s = ctl_per_delta_ms * worst_phase_deltas / 1000
    close(worst_phase_open_ctl_s, 14.7, 0.1,
          "P-CO7 derived (arithmetic, not measured): projected control manifest-chain "
          "open at K-1=511 deltas (estimated control per-delta cost * 511 / 1000)")

    # --- emit macros ---
    m.add("osdiB1co7TreatmentCommit", trt_commit,
          f"{relpath(B1_CO7_MANIFEST)}: git_commit")

    m.add("osdiB1co7CheckpointReadParseMs", us_to_ms_str(checkpoint_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "checkpoint_read_parse_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7MerkleVerifyMs", us_to_ms_str(merkle_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "merkle_verify_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7StateBuildMs", us_to_ms_str(state_build_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "state_build_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7DeltaReplayMs", us_to_ms_str(delta_replay_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "delta_replay_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7ComponentMs", us_to_ms_str(component_us, 2),
          f"{relpath(B1_CO7_RAW)}: checkpoint_read_parse_us + merkle_verify_us + "
          "state_build_us + delta_replay_us (each median of 3 reps), /1000, ms -- "
          "asserted equal to the raw record's own manifest_chain_component_us_treatment_"
          "median.sum_us")
    m.add("osdiB1co7DictionaryOpenMs", us_to_ms_str(dictionary_us, 1),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "dictionary_open_us) over 3 reps, /1000, ms -- dominates the open")
    m.add("osdiB1co7TotalMs", us_to_ms_str(total_us, 1),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "total_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7Generation", tex_num(trt_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.generation")
    m.add("osdiB1co7DeltaCount", tex_num(delta_count),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.open_phase_us[*]."
          "delta_count, constant across 3 reps")
    m.add("osdiB1co7CheckpointGeneration", tex_num(checkpoint_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.generation - delta_count "
          "-- asserted a multiple of the K=512 checkpoint cadence")
    m.add("osdiB1co7StateBuildPerDeltaUs", f"{state_build_per_delta_us:.1f}",
          f"{relpath(B1_CO7_RAW)}: treatment state_build_us (median) / delta_count, "
          "us/delta")
    m.add("osdiB1co7DeltaReplayPerDeltaUs", f"{delta_replay_per_delta_us:.1f}",
          f"{relpath(B1_CO7_RAW)}: treatment delta_replay_us (median) / delta_count, "
          "us/delta")

    m.add("osdiB1co7ControlOpenMs", f"{ctl_open_ms:.1f}",
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.control.open_ms) over 3 reps, ms")
    m.add("osdiB1co7ControlGeneration", tex_num(ctl_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.control.generation")
    m.add("osdiB1co7ControlDeltaCount", tex_num(ctl_delta_count),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.control.generation mod K=512 "
          "-- the control engine predates open_phase_us() and reports no delta_count "
          "of its own")
    m.add("osdiB1co7ControlPerDeltaMs", f"{ctl_per_delta_ms:.1f}",
          f"{relpath(B1_CO7_RAW)}: (control open_ms (median) - treatment's measured "
          "dictionary_open_us/1000, used as an estimate of the control's own "
          "dictionary-open cost) / control delta_count -- an estimate, not a second "
          "independent measurement")
    m.add("osdiB1co7OpenRatio", f"{open_ratio:.3f}",
          f"{relpath(B1_CO7_RAW)}: (treatment total_us (median) / 1000) / control "
          "open_ms (median)")

    m.add("osdiB1WorstPhaseOpenTreatmentMs", f"{worst_phase_open_trt_ms:.1f}",
          "derived, not measured: osdiB1co7CheckpointReadParseMs + osdiB1co7MerkleVerifyMs "
          "+ (osdiB1co7StateBuildPerDeltaUs + osdiB1co7DeltaReplayPerDeltaUs) * 511 / 1000 "
          "-- projected treatment manifest-chain open cost at K-1=511 deltas")
    m.add("osdiB1WorstPhaseOpenControlS", f"{worst_phase_open_ctl_s:.1f}",
          "derived, not measured: osdiB1co7ControlPerDeltaMs * 511 / 1000 -- projected "
          "control manifest-chain open cost at K-1=511 deltas")


# --------------------------------------------------------------------------
# C4 --- bounded version_history (B2 A/B)
# --------------------------------------------------------------------------

def compute_c4(m: Macros) -> None:
    raw = json.loads(B2_RAW.read_text(encoding="utf-8"))
    summary = json.loads(B2_SUMMARY.read_text(encoding="utf-8"))
    vh = raw["version_history"]["raw"]
    vh_summary = raw["version_history"]["summary"]

    def median_of(records, field):
        return statistics.median(r[field] for r in records)

    for key in ("control_1m", "treatment_1m", "control_10m", "treatment_10m"):
        recs = vh[key]
        eq(len(recs), 3, f"C4: {key} has 3 reps")
        require(all(r["reps"] == 1 for r in recs),
                f"C4: {key} rows are each a single rep (3 rows == 3 reps)")
        wall_med = median_of(recs, "median_ms")
        vmhwm_med = median_of(recs, "vmhwm_kb")
        close(wall_med, vh_summary[key]["wall_ms"]["median"], 0.5,
              f"C4: recomputed {key} wall_ms median matches raw summary")
        close(vmhwm_med, vh_summary[key]["vmhwm_kb"]["median"], 1,
              f"C4: recomputed {key} vmhwm_kb median matches raw summary")

    trt_10m = vh["treatment_10m"]
    trt_1m = vh["treatment_1m"]
    ctl_10m = vh["control_10m"]

    trt_10m_wall = median_of(trt_10m, "median_ms")
    trt_10m_vmhwm = median_of(trt_10m, "vmhwm_kb")
    trt_1m_vmhwm = median_of(trt_1m, "vmhwm_kb")
    ctl_10m_wall = median_of(ctl_10m, "median_ms")
    ctl_10m_vmhwm = median_of(ctl_10m, "vmhwm_kb")

    close(trt_10m_vmhwm, 1_229_724, 1, "C4 frozen: treatment 10M VmHWM median, KB")
    close(trt_10m_wall, 1866.8, 0.1, "C4 frozen: treatment 10M wall median, ms")
    close(trt_1m_vmhwm, 167_968, 1, "C4 frozen: treatment 1M VmHWM median, KB")
    close(ctl_10m_vmhwm, 13_414_332, 1, "C4 frozen: control 10M VmHWM median, KB")
    close(ctl_10m_wall, 179_250.1, 0.1, "C4 frozen: control 10M wall median, ms")

    rss_reduction = ctl_10m_vmhwm / trt_10m_vmhwm
    wall_speedup = ctl_10m_wall / trt_10m_wall
    close(rss_reduction, 10.9, 0.05, "C4 frozen: peak-RSS reduction ratio at 10M")
    # NOTE: b2-version-history-ab-2026-09.json's own falsifier prose says
    # "95.2x faster" for this ratio; recomputing it from the raw per-rep
    # median wall times (179,250.1 / 1,866.8, both already verified above
    # against the raw record) gives 96.02x, not 95.2x. The two other ratios
    # in this claim (RSS reduction, flatness) use the median consistently
    # and match the record's own summary fields exactly (checked above);
    # this is the one place recomputation disagrees with the record's own
    # hand-written prose -- almost certainly because that prose used the
    # mean of the three reps (177,262.2 / 1,862.2 = 95.19x) instead of the
    # median. Per this script's "assert, do not trust" discipline the
    # macro uses the recomputed (median-based) value, not the prose's.
    close(wall_speedup, 96.02, 0.05,
          "C4 frozen: wall speedup ratio at 10M (median-based; the record's own prose says "
          "95.2x, apparently computed from the mean of reps instead -- see comment)")

    flatness_ratio = trt_10m_vmhwm / trt_1m_vmhwm
    close(flatness_ratio, 7.32, 0.02,
          "C4 frozen: 1M->10M peak-RSS flatness ratio (refuted, bar <=2.5x)")
    require(flatness_ratio > 2.5, "C4: the near-flatness falsifier is refuted by the treatment ratio")

    falsifiers = summary["falsifiers"]
    eq(falsifiers["peak_rss_le_2.5GB_at_10M"]["verdict"], "PASS",
       "C4: F1 (peak RSS) verdict is PASS in the summary record")
    eq(falsifiers["1M_to_10M_peak_rss_ratio_le_2.5x"]["verdict"], "REFUTED",
       "C4: the flatness falsifier verdict is REFUTED in the summary record")

    # The 100M projection is the same linear (10x) extrapolation off the
    # measured 10M point that BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md
    # uses ("~12.6 GB at 100M by the same linear extrapolation"); computed
    # here from the record alone so this macro has no dependency on that
    # gitignored, coordinator-internal doc. When the doc happens to be
    # present on disk (it is not shipped with this checkout or any
    # worktree), cross-check against its own stated figure.
    rss_gb = trt_10m_vmhwm * 1024 / 1e9
    proj_gb_value = 10 * rss_gb
    proj_gb = f"{proj_gb_value:.1f}"
    eq(proj_gb, "12.6", "C4 frozen: 100M linear-extrapolation projection (10x the 10M point)")
    if VH_FORECAST.exists():
        vh_txt = VH_FORECAST.read_text(encoding="utf-8")
        doc_proj = re.search(r"~(\d+\.\d+) GB at 100M by the same linear extrapolation", vh_txt)
        require(doc_proj is not None,
                "C4: BOUNDED_VERSION_HISTORY_FORECAST states the 100M linear-extrapolation "
                "projection (cross-check only; not this macro's source)")
        if doc_proj:
            eq(doc_proj.group(1), proj_gb,
               "C4: recomputed 100M projection matches the forecast doc's own stated figure")

    m.add("osdiVhRss", f"{rss_gb:.3f}",
          f"{relpath(B2_RAW)}: median(treatment_10m[*].vmhwm_kb), decimal GB")
    m.add("osdiVhWall", f"{trt_10m_wall / 1000:.2f}",
          f"{relpath(B2_RAW)}: median(treatment_10m[*].wall_ms) / 1000, s")
    m.add("osdiVhRatio", f"{flatness_ratio:.2f}",
          f"{relpath(B2_RAW)}: treatment 10M/1M VmHWM median ratio "
          "(the near-flatness falsifier: refuted, bar <=2.5x)")
    m.add("osdiVhProjHundredM", proj_gb,
          f"{relpath(B2_RAW)}: 10x median(treatment_10m[*].vmhwm_kb) -- the same "
          "linear extrapolation BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md uses, cross-"
          "checked against that (gitignored) doc's own figure when it is present on disk")


# --------------------------------------------------------------------------
# C5 --- reader scaling at 10M
# --------------------------------------------------------------------------

def compute_c5(m: Macros) -> None:
    d = json.loads(READERS_10M.read_text(encoding="utf-8"))
    quiescent = {q["readers"]: q for q in d["quiescent"]}
    eq(sorted(quiescent), [1, 2, 4, 8, 16, 32], "C5: quiescent reader counts")

    readers_max = max(quiescent)
    eq(readers_max, 32, "C5 frozen: max concurrent readers")

    agg_one = quiescent[1]["aggregate_qps"]
    agg_max = quiescent[readers_max]["aggregate_qps"]
    eq(agg_one, 2.85, "C5 frozen: aggregate q/s at 1 reader")
    eq(agg_max, 27.72, "C5 frozen: aggregate q/s at 32 readers")

    vmhwm_medians_gib = [q["vmhwm_kb"]["median"] / 1024 / 1024 for q in quiescent.values()]
    vmhwm_lo, vmhwm_hi = min(vmhwm_medians_gib), max(vmhwm_medians_gib)
    close(vmhwm_lo, 1.19, 0.01, "C5 frozen: reader VmHWM flat-range low end, GiB")
    close(vmhwm_hi, 1.22, 0.01, "C5 frozen: reader VmHWM flat-range high end, GiB")

    mixed16 = {mm["writer"]: mm for mm in d["mixed"] if mm["readers"] == 16}
    require(False in mixed16 and True in mixed16, "C5: readers=16 has both writer arms")
    no_writer = mixed16[False]["per_query_p50_ms"]
    with_writer = mixed16[True]["per_query_p50_ms"]
    series_no = statistics.median(no_writer["series.count"])
    series_yes = statistics.median(with_writer["series.count"])
    coactive_no = statistics.median(no_writer["coactive.narrow"])
    coactive_yes = statistics.median(with_writer["coactive.narrow"])
    series_pct = (series_yes / series_no - 1) * 100
    coactive_pct = (coactive_yes / coactive_no - 1) * 100
    close(series_pct, 47.53, 0.01, "C5 frozen: series.count p50 writer tail cost at 16 readers")
    close(coactive_pct, 42.71, 0.01, "C5 frozen: coactive.narrow p50 writer tail cost at 16 readers")

    m.add("osdiReadersMax", readers_max,
          f"{relpath(READERS_10M)}: max(quiescent[*].readers)")
    m.add("osdiReaderVmhwm", f"{vmhwm_lo:.2f}--{vmhwm_hi:.2f}",
          f"{relpath(READERS_10M)}: min/max over readers of quiescent[*].vmhwm_kb.median, GiB")
    m.add("osdiAggQpsOne", f"{agg_one:.2f}",
          f"{relpath(READERS_10M)}: quiescent[readers=1].aggregate_qps")
    m.add("osdiAggQpsThirtyTwo", f"{agg_max:.2f}",
          f"{relpath(READERS_10M)}: quiescent[readers=32].aggregate_qps")
    m.add("osdiWriterTailCost",
          f"+{series_pct:.1f}\\%/+{coactive_pct:.1f}\\%",
          f"{relpath(READERS_10M)}: mixed[readers=16] per_query_p50_ms median, "
          "series.count/coactive.narrow, writer vs quiescent")


# --------------------------------------------------------------------------
# C6 --- selective refresh never reports a stale artifact fresh
# --------------------------------------------------------------------------

def compute_c6(m: Macros) -> None:
    # M4 record of account: trials-full.json + trials-fixture.json (never
    # trials-{full,fixture}-run1-superseded.json -- see M4_MEASURED_REPORT.md
    # "The floor is scored on run 2 alone").
    fresh_full = json.loads(FRESH_FULL.read_text(encoding="utf-8"))
    fresh_fixture = json.loads(FRESH_FIXTURE.read_text(encoding="utf-8"))
    m4_trials = fresh_full["trials"] + fresh_fixture["trials"]
    eq(len(m4_trials), fresh_full["trial_count"] + fresh_fixture["trial_count"],
       "C6: combined M4 trial pool matches each file's own trial_count")
    eq(len(m4_trials), 3354, "C6 frozen: M4 record-of-account trial pool")

    m4_changed = [t for t in m4_trials if t["changed"]]
    eq(len(m4_changed), 447, "C6 frozen: M4 changed-column trials")
    m4_false_fresh = sum(1 for t in m4_changed if t["verdict"] == "fresh")
    eq(m4_false_fresh, 0, "C6 frozen: M4 false-fresh count")
    eq(m4_false_fresh,
       fresh_full["summary"]["false_fresh"] + fresh_fixture["summary"]["false_fresh"],
       "C6: recomputed M4 false-fresh matches the sum of each file's own summary")

    m4_rt_false_fresh = sum(1 for t in m4_changed if t.get("rowtouch_verdict") == "fresh")
    eq(m4_rt_false_fresh, 212, "C6 frozen: M4 naive row-touch false-fresh count")
    rt_rate = round(m4_rt_false_fresh / len(m4_changed) * 1000) / 10
    eq(rt_rate, 47.4, "C6 frozen: M4 naive row-touch false-fresh rate")

    m4_newid = [t for t in m4_changed if t["placement"] == "new-identity"]
    eq(len(m4_newid), 89, "C6 frozen: M4 new-identity changed trials")
    m4_newid_missed = sum(1 for t in m4_newid if t.get("rowtouch_verdict") == "fresh")
    eq(m4_newid_missed, 89, "C6 frozen: every new-identity changed trial is missed by row-touch")

    # M5 round 2 (commit d830f13, Addendum 8): the carve-2 (topup-2) run and
    # the propagation-2 (topup-2) runs are the campaign's closing, scored
    # figures -- never the earlier run-1/topup-1 rounds, and never pooled
    # with them (Addendum 8 scores "round 2 ... alone per store"). Recomputed
    # here from the per-trial `rows`, never trusted from the record's own
    # `summary` block, though the two are cross-checked against each other.
    carve2 = json.loads(M5_CARVE_TWO.read_text(encoding="utf-8"))
    eq(carve2["git_sha"][:7], "d830f13", "C6: carve-2 record is at commit d830f13")
    carve2_rows = carve2["rows"]
    eq(len(carve2_rows), 28_044, "C6 frozen: carve-2 trial count")
    carve2_false_fresh = sum(1 for r in carve2_rows if r["changed"] and r["verdict"] == "fresh")
    eq(carve2_false_fresh, 0, "C6 frozen: carve-2 false-fresh count")
    eq(carve2_false_fresh, carve2["summary"]["control_invariant_violations"],
       "C6: recomputed carve-2 false-fresh matches the record's own control_invariant_violations")

    prop_rows_total = 0
    prop_avoided = 0
    prop_false_safe = 0
    prop_payload_changed = 0
    prop_false_safe_payload = 0
    for path in M5_PROP_TWO:
        d = json.loads(path.read_text(encoding="utf-8"))
        eq(d["git_sha"][:7], "d830f13", f"C6: {path.name} is at commit d830f13")
        rows = d["rows"]
        eq(len(rows), d["summary"]["decisions"],
           f"C6: {path.name} row count matches summary.decisions")
        prop_rows_total += len(rows)
        prop_avoided += sum(1 for r in rows if r["child_recomputed"] is False)
        prop_false_safe += sum(1 for r in rows if r["false_safe"])
        payload = [r for r in rows if r["parent_payload_changed"]]
        prop_payload_changed += len(payload)
        prop_false_safe_payload += sum(1 for r in payload if r["false_safe"])

    eq(prop_rows_total, 5867, "C6 frozen: M5 round-2 propagation decisions")
    eq(prop_payload_changed, 308, "C6 frozen: M5 round-2 payload-changing decisions")
    eq(prop_false_safe, 0, "C6 frozen: M5 round-2 false-safe count (total)")
    eq(prop_false_safe_payload, 0, "C6 frozen: M5 round-2 false-safe count (payload-changing)")
    eq(prop_avoided, 5808, "C6 frozen: M5 round-2 avoided-recomputation decisions")
    avoided_pct = round(1000 * prop_avoided / prop_rows_total) / 10
    eq(avoided_pct, 99.0, "C6 frozen: M5 round-2 avoided-recomputation rate")

    m.add("osdiFalseFreshCarveTwo", carve2_false_fresh,
          f"{relpath(M5_CARVE_TWO)}: rows with changed and verdict=='fresh', of 28,044")
    m.add("osdiRowTouchRate", f"{rt_rate:.1f}",
          f"{relpath(FRESH_FULL)}+{FRESH_FIXTURE.name}: naive row-touch false-fresh "
          "rate over M4's changed column, 212/447")
    m.add("osdiNewIdentityFF", m4_newid_missed,
          f"{relpath(FRESH_FULL)}+{FRESH_FIXTURE.name}: new-identity changed trials "
          "the row-touch rule calls fresh, of 89")
    m.add("osdiAvoidedPct", f"{avoided_pct:.1f}",
          "benchmarks/m5-v1/topup-propagation2-{bitcoinotc,collegemsg,sx-mathoverflow}.json: "
          "decisions with child_recomputed==False, 5,808/5,867")


# --------------------------------------------------------------------------
# C8 --- trust-boundary fault matrix, before and after D-160
# --------------------------------------------------------------------------

def outcome_totals(cells, *, strict: bool | None = None):
    tot = {"correct": 0, "safe-refusal": 0, "explicit-failure": 0, "silent-violation": 0}
    for c in cells:
        if strict is not None and c["strict_gate"] != strict:
            continue
        for k in tot:
            tot[k] += c["counts"][k]
    return tot


def compute_c8(m: Macros) -> None:
    pre = json.loads(FAULTS_PRE.read_text(encoding="utf-8"))
    post = json.loads(FAULTS_POST.read_text(encoding="utf-8"))

    # The post-fix (deployed-gate) record carries only the primary gate.
    post_cells = post["per_cell_gate_table"]
    require(all(not c["strict_gate"] for c in post_cells),
            "C8: the post-D-160 record carries only the primary (non-strict) gate")
    eq(post["total_trials"], sum(c["n_cases"] for c in post_cells),
       "C8: post-fix total_trials matches the sum of per-cell n_cases")
    eq(post["total_trials"], 3102, "C8 frozen: post-fix (deployed-gate) trial count")

    post_tot = outcome_totals(post_cells)
    eq(post_tot["explicit-failure"], 2236, "C8 frozen: post-fix explicit-failure count")
    eq(sum(post_tot.values()), post["total_trials"],
       "C8: post-fix outcome totals partition total_trials")

    post_headline = sum(sv["count"] for sv in post["silent_violations"] if sv["headline"])
    eq(post_headline, 0, "C8 frozen: post-fix headline silent-violation count")
    eq(post_headline, post["headline_silent_violation_count"],
       "C8: recomputed headline silent-violations matches the record's own field")

    f23_post = [c for c in post_cells if c["cell"] == "F2-3"]
    eq(len(f23_post), 1, "C8: exactly one F2-3 row in the post-fix primary-gate table")
    f23_post_count = f23_post[0]["counts"]["silent-violation"]
    eq(f23_post_count, 32, "C8 frozen: F2-3 (declared blind spot) silent-violation count, post-fix")

    # The pre-fix record carries both gate arms for the same underlying
    # trial set; the primary (non-strict) arm is the one the deployed
    # system actually used and is the one comparable to the post-fix
    # record (same 23 cells, same 3,102-trial population -- only F1-9's
    # outcome differs).
    pre_primary = [c for c in pre["per_cell_gate_table"] if not c["strict_gate"]]
    eq(sum(c["n_cases"] for c in pre_primary), 3102,
       "C8: pre-fix primary-gate trial count matches the post-fix population")

    f19_pre = [c for c in pre_primary if c["cell"] == "F1-9"]
    eq(len(f19_pre), 1, "C8: exactly one F1-9 row in the pre-fix primary-gate table")
    f19_pre_count = f19_pre[0]["counts"]["silent-violation"]
    eq(f19_pre_count, 271, "C8 frozen: F1-9 silent-violation count, pre-fix primary gate")

    f23_pre = [c for c in pre_primary if c["cell"] == "F2-3"]
    eq(len(f23_pre), 1, "C8: exactly one F2-3 row in the pre-fix primary-gate table")
    f23_pre_count = f23_pre[0]["counts"]["silent-violation"]
    eq(f23_pre_count, 32, "C8 frozen: F2-3 silent-violation count, pre-fix primary gate (unchanged)")
    eq(f23_pre_count, f23_post_count, "C8: F2-3's blind spot is unchanged by D-160")

    pre_primary_tot = outcome_totals(pre_primary)
    silent_pre = pre_primary_tot["silent-violation"]
    eq(silent_pre, f19_pre_count + f23_pre_count,
       "C8: pre-fix primary-gate silent-violation total is exactly F1-9 + F2-3 "
       "(no other cell produced one)")
    eq(silent_pre, 303, "C8 frozen: pre-fix (primary gate) total silent-violation count")

    # D-160's fix: every F1-9 trial that was silent-violation becomes
    # explicit-failure; nothing else about the population changes.
    eq(pre_primary_tot["correct"], post_tot["correct"], "C8: correct count unchanged by D-160")
    eq(pre_primary_tot["safe-refusal"], post_tot["safe-refusal"],
       "C8: safe-refusal count unchanged by D-160")
    eq(post_tot["explicit-failure"] - pre_primary_tot["explicit-failure"], f19_pre_count,
       "C8: the explicit-failure increase equals exactly F1-9's former silent-violation count")
    eq(pre_primary_tot["silent-violation"] - post_tot["silent-violation"], f19_pre_count,
       "C8: the silent-violation decrease equals exactly F1-9's former count")

    m.add("osdiFaultTrials", tex_num(post["total_trials"]),
          f"{relpath(FAULTS_POST)}: total_trials, the deployed (post-D-160) gate")
    m.add("osdiSilentPre", silent_pre,
          f"{relpath(FAULTS_PRE)}: primary-gate silent-violation total "
          "(F1-9 271 + F2-3 32), before D-160")
    m.add("osdiSilentPost", post_headline,
          f"{relpath(FAULTS_POST)}: headline_silent_violation_count (F2-3's 32 is a "
          "declared non-headline blind spot, see osdiFTwoThree)")
    m.add("osdiFOneNineBefore", f19_pre_count,
          f"{relpath(FAULTS_PRE)}: F1-9 (wrong_step_citation) silent-violation count, "
          "primary gate, before D-160 -- 0 after")
    m.add("osdiFTwoThree", f23_post_count,
          f"{relpath(FAULTS_POST)}: F2-3 silent-violation count, the pre-registered "
          "assumption-A2 blind spot, unchanged by D-160")


# --------------------------------------------------------------------------
# C7 (partial) --- correction-storm DAG phase, v1/v2/v3 (40 cells each)
# --------------------------------------------------------------------------

def compute_c7_dag(m: Macros) -> None:
    v1 = json.loads(DAG_V1.read_text(encoding="utf-8"))
    v2 = json.loads(DAG_V2.read_text(encoding="utf-8"))
    v3 = json.loads(DAG_V3.read_text(encoding="utf-8"))
    v1_rows = load_jsonl(DAG_V1_ROWS)
    v2_rows = load_jsonl(DAG_V2_ROWS)
    v3_rows = load_jsonl(DAG_V3_ROWS)

    for name, d, rows in (("v1", v1, v1_rows), ("v2", v2, v2_rows), ("v3", v3, v3_rows)):
        eq(d["total_tasks"], 40, f"DAG {name}: total_tasks")
        eq(len(d["per_cell"]), 40, f"DAG {name}: per_cell row count")
        eq(len(rows), 40, f"DAG {name}: rows.jsonl line count")

    eq(len({v1["total_tasks"], v2["total_tasks"], v3["total_tasks"]}), 1,
       "DAG: v1/v2/v3 all run the same 40-cell grid")
    dag_cells = 40

    def by_task(per_cell):
        return {c["task_id"]: c for c in per_cell}

    def rows_by_task(rows):
        return {r["_task_id"]: r for r in rows}

    v1c, v2c, v3c = by_task(v1["per_cell"]), by_task(v2["per_cell"]), by_task(v3["per_cell"])

    # Cross-check every per_cell summary field against the fuller per-row
    # dag.cascade block in the companion -rows.jsonl file -- two different
    # serializations of the same underlying measurement.
    for name, cells, rows in (("v1", v1c, v1_rows), ("v2", v2c, v2_rows), ("v3", v3c, v3_rows)):
        for r in rows:
            c = cells[r["_task_id"]]
            cascade = r["dag"]["cascade"]
            eq(cascade["false_safe_count"], c["false_safe_count"],
               f"DAG {name} task {c['task_id']}: rows.jsonl dag.cascade.false_safe_count "
               "matches per_cell")
            eq(cascade["nodes_visited"], c["nodes_visited"],
               f"DAG {name} task {c['task_id']}: rows.jsonl dag.cascade.nodes_visited "
               "matches per_cell")
            eq(cascade["quiescent"], c["quiescent"],
               f"DAG {name} task {c['task_id']}: rows.jsonl dag.cascade.quiescent matches per_cell")

    v1_fs_cells = [c for c in v1["per_cell"] if c["false_safe_count"] > 0]
    eq(len(v1_fs_cells), 20, "DAG v1 frozen: false-safe cell count")
    require(all(c["false_safe_count"] == 3 for c in v1_fs_cells),
            "DAG v1: every false-safe cell reports exactly 3 false-safes")
    v1_false_safe_per_cell = 3

    v2_fs_cells = sum(1 for c in v2["per_cell"] if c["false_safe_count"] > 0)
    eq(v2_fs_cells, 0, "DAG v2 frozen: false-safe cell count")
    v3_fs_cells = sum(1 for c in v3["per_cell"] if c["false_safe_count"] > 0)
    eq(v3_fs_cells, 0, "DAG v3 frozen: false-safe cell count")

    # G-S2 gate cross-check: v1 fails it (some cells false-safe), v2/v3 pass.
    eq(v1["gates"]["g_s2_false_safe_zero"], False, "DAG v1: G-S2 gate fails as expected")
    eq(len(v1["gates"]["g_s2_failing_cells"]), len(v1_fs_cells),
       "DAG v1: gate's failing-cell count matches recomputed false-safe cell count")
    for name, d in (("v2", v2), ("v3", v3)):
        eq(d["gates"]["g_s2_false_safe_zero"], True, f"DAG {name}: G-S2 gate passes")
        eq(d["gates"]["g_s2_failing_cells"], [], f"DAG {name}: no G-S2 failing cells")

    for seed in (0, 1):
        n = sum(1 for c in v1["per_cell"] if c["seed"] == seed)
        eq(n, 20, f"DAG: {n} cells at seed {seed} (expected 20 -- half the 40-cell grid)")

    def extra_visits(other_cells, seed):
        diffs = {other_cells[tid]["nodes_visited"] - v1c[tid]["nodes_visited"]
                 for tid in v1c if v1c[tid]["seed"] == seed}
        require(len(diffs) == 1,
                f"DAG: nodes_visited delta vs v1 is not constant across seed-{seed} cells "
                f"(got {sorted(diffs)})")
        return next(iter(diffs))

    v2_extra_s0 = extra_visits(v2c, 0)
    v2_extra_s1 = extra_visits(v2c, 1)
    v3_extra_s0 = extra_visits(v3c, 0)
    v3_extra_s1 = extra_visits(v3c, 1)

    eq(v2_extra_s0, 61, "DAG v2 frozen: nodes_visited delta vs v1, seed 0")
    eq(v2_extra_s1, 62, "DAG v2 frozen: nodes_visited delta vs v1, seed 1")
    eq(v3_extra_s0, 15, "DAG v3 frozen: nodes_visited delta vs v1, seed 0")
    eq(v3_extra_s1, 11, "DAG v3 frozen: nodes_visited delta vs v1, seed 1")

    # v3-only: the D-161 rollout's narrowing_coverage block (absent from v1/v2).
    require(all("narrowing_coverage" not in r["summary"] for r in v1_rows + v2_rows),
            "DAG v1/v2: narrowing_coverage is a v3-only (post-D-161-rollout) field")
    require(all("narrowing_coverage" in r["summary"] for r in v3_rows),
            "DAG v3: every cell carries a narrowing_coverage block")
    all_top_term_total = sum(r["summary"]["narrowing_coverage"]["n_all_top_term"] for r in v3_rows)
    eq(all_top_term_total, 0, "DAG v3 frozen: n_all_top_term summed over all 40 cells")

    def tgms_false_fresh(rows):
        return sum(r["summary"]["arms"][arm]["false_fresh"]
                   for r in rows for arm in ("tgms-L0", "tgms-L1"))

    ff_v1, ff_v2, ff_v3 = (tgms_false_fresh(v1_rows), tgms_false_fresh(v2_rows),
                           tgms_false_fresh(v3_rows))
    eq(ff_v1, 0, "DAG v1 frozen: tgms-L0+tgms-L1 false-fresh total")
    eq(ff_v2, 0, "DAG v2 frozen: tgms-L0+tgms-L1 false-fresh total")
    eq(ff_v3, 0, "DAG v3 frozen: tgms-L0+tgms-L1 false-fresh total")
    ff_total = ff_v1 + ff_v2 + ff_v3
    eq(ff_total, 0, "DAG v1+v2+v3 frozen: tgms-L0+tgms-L1 false-fresh total")

    m.add("osdiDagCells", dag_cells,
          f"{relpath(DAG_V1)}: total_tasks, == v2/v3's own total_tasks (40-cell grid, all three)")
    m.add("osdiDagV1FalseSafeCells", len(v1_fs_cells),
          f"{relpath(DAG_V1)}: per_cell entries with false_safe_count>0, of 40")
    m.add("osdiDagV2FalseSafeCells", v2_fs_cells,
          f"{relpath(DAG_V2)}: per_cell entries with false_safe_count>0, of 40")
    m.add("osdiDagV3FalseSafeCells", v3_fs_cells,
          f"{relpath(DAG_V3)}: per_cell entries with false_safe_count>0, of 40")
    m.add("osdiDagV1FalseSafePerCell", v1_false_safe_per_cell,
          f"{relpath(DAG_V1)}: false_safe_count in each of the 20 affected cells (uniform)")
    m.add("osdiDagV2ExtraVisitsSeedZero", v2_extra_s0,
          f"{relpath(DAG_V2)} vs {DAG_V1.name}: nodes_visited delta, seed-0 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV2ExtraVisitsSeedOne", v2_extra_s1,
          f"{relpath(DAG_V2)} vs {DAG_V1.name}: nodes_visited delta, seed-1 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV3ExtraVisitsSeedZero", v3_extra_s0,
          f"{relpath(DAG_V3)} vs {DAG_V1.name}: nodes_visited delta, seed-0 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV3ExtraVisitsSeedOne", v3_extra_s1,
          f"{relpath(DAG_V3)} vs {DAG_V1.name}: nodes_visited delta, seed-1 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV3AllTopTerm", all_top_term_total,
          f"{relpath(DAG_V3_ROWS)}: sum of summary.narrowing_coverage.n_all_top_term over "
          "all 40 cells")
    m.add("osdiDagFalseFreshTotal", ff_total,
          f"{relpath(DAG_V1_ROWS)}+{DAG_V2_ROWS.name}+{DAG_V3_ROWS.name}: sum of "
          "summary.arms.{tgms-L0,tgms-L1}.false_fresh over all 120 cells (v1+v2+v3)")


# --------------------------------------------------------------------------
# C7 (partial) --- R-18 probe, N=10,000 c1 seed 0, 5 batches
# --------------------------------------------------------------------------

def compute_c7_r18(m: Macros) -> None:
    d = json.loads(R18_PROBE.read_text(encoding="utf-8"))
    rows = load_jsonl(R18_PROBE_ROWS)
    rows.sort(key=lambda r: r["batch_index"])

    eq(d["config"]["n_artifacts"], 10000, "R18 frozen: probe artifact count")
    eq(d["config"]["batches"], 5, "R18 frozen: batch count")
    eq(len(rows), d["config"]["batches"], "R18: rows.jsonl line count matches config.batches")
    eq(d["config"]["wall_capped"], False, "R18: probe was not wall-capped")
    eq(d["config"]["mix"], "c1", "R18: this probe is the c1 mix (P7's R-18 trip criterion "
       "targets c4, not this cell)")

    n_registered_vals = {r["n_registered"] for r in rows}
    require(len(n_registered_vals) == 1, "R18: n_registered constant across batches")
    n_registered = next(iter(n_registered_vals))
    eq(n_registered, d["config"]["n_registered"], "R18: recomputed n_registered matches config")

    intersects = [r["intersects_calls"] for r in rows]
    lookup_ms = [r["lookup_wall_ms"] for r in rows]
    survivors = [r["candidate_survivors"] for r in rows]
    changed = [r["changed_count"] for r in rows]

    intersects_med = statistics.median(intersects)
    lookup_med = statistics.median(lookup_ms)
    survivors_med = statistics.median(survivors)
    eq(intersects_med, 13009, "R18 frozen: median intersects_calls/batch")
    require(intersects_med <= 50000,
            "R18: median intersects_calls/batch does not exceed the R-18 trip threshold "
            "(P7: trips in c4, not this c1 cell)")

    survivor_fraction = survivors_med / n_registered
    close(survivor_fraction, 0.7131, 0.001,
          "R18 frozen: median candidate_survivors / n_registered")

    precision_per_batch = [c / s for c, s in zip(changed, survivors)]
    precision_med = statistics.median(precision_per_batch)
    close(precision_med, 0.0851, 0.001,
          "R18 frozen: median(changed_count / candidate_survivors) over the 5 batches")

    l1_check_ms = [r["arms"]["tgms-L1"]["check_wall_ms"] for r in rows]
    l1_ttf_ms = [r["arms"]["tgms-L1"]["ttf_ms"] for r in rows]
    global_ttf_ms = [r["arms"]["global-recompute"]["ttf_ms"] for r in rows]
    l1_invalidated = [r["arms"]["tgms-L1"]["invalidated_count"] for r in rows]
    l1_false_fresh = [r["arms"]["tgms-L1"]["false_fresh_count"] for r in rows]

    eq(sum(l1_false_fresh), 0, "R18 frozen: tgms-L1 false-fresh count, summed over batches")
    eq(sum(l1_false_fresh), d["summary"]["arms"]["tgms-L1"]["false_fresh"],
       "R18: recomputed tgms-L1 false-fresh matches the record's own summary.arms field")

    l1_check_med = statistics.median(l1_check_ms)
    l1_ttf_med = statistics.median(l1_ttf_ms)
    global_ttf_med = statistics.median(global_ttf_ms)

    # Cross-check every recomputed per-batch median against the record's own
    # aggregate summary.arms block -- never trusted without this.
    eq(l1_ttf_med, d["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"],
       "R18: recomputed tgms-L1 ttf p50 (median of per-batch ttf_ms) matches "
       "the record's own summary.arms field")
    eq(global_ttf_med, d["summary"]["arms"]["global-recompute"]["ttf_p50_ms"],
       "R18: recomputed global-recompute ttf p50 matches the record's own summary.arms field")

    avoided_decision = 1 - sum(l1_invalidated) / (n_registered * len(rows))
    eq(avoided_decision, d["summary"]["arms"]["tgms-L1"]["avoided_recompute_decision"],
       "R18: recomputed avoided_recompute_decision (1 - sum(invalidated)/sum(n_registered)) "
       "matches the record's own summary.arms field")

    # "Speedup of L1 over global-recompute" == baseline/candidate == global/L1,
    # per campaign.yaml's P5/P6 convention (>1.0 means L1 is faster). This
    # probe measures speedup < 1.0 -- L1 pays more in end-to-end time-to-fresh
    # than global-recompute here even though R-18 itself did not trip (median
    # intersects_calls 13,009 << the 50,000 trip threshold) -- a genuine,
    # asserted result, not a falsifier-triggering trip.
    speedup = global_ttf_med / l1_ttf_med
    close(speedup, 0.8067, 0.001, "R18 frozen: speedup of tgms-L1 over global-recompute "
          "(global_ttf_median / l1_ttf_median)")
    require(0.80 <= speedup <= 0.82,
            "R18: speedup(L1 over global) falls within the documented [0.80, 0.82] range")

    check_seconds_med = l1_check_med / 1000
    ttf_l1_s = l1_ttf_med / 1000
    ttf_global_s = global_ttf_med / 1000

    m.add("osdiR18Artifacts", tex_num(d["config"]["n_artifacts"]),
          f"{relpath(R18_PROBE)}: config.n_artifacts")
    m.add("osdiR18IntersectsCallsMedian", tex_num(int(intersects_med)),
          f"{relpath(R18_PROBE_ROWS)}: median(intersects_calls) over the 5 batches")
    m.add("osdiR18LookupMsMedian", f"{lookup_med:.2f}",
          f"{relpath(R18_PROBE_ROWS)}: median(lookup_wall_ms) over the 5 batches, ms")
    m.add("osdiR18SurvivorFraction", f"{survivor_fraction * 100:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(candidate_survivors) / n_registered, percent")
    m.add("osdiR18CheckSecondsMedian", f"{check_seconds_med:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.tgms-L1.check_wall_ms) over the 5 "
          "batches, /1000, s")
    m.add("osdiR18TtfL1Seconds", f"{ttf_l1_s:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.tgms-L1.ttf_ms) over the 5 batches, /1000, s "
          "(== record's own summary.arms.tgms-L1.ttf_p50_ms)")
    m.add("osdiR18TtfGlobalSeconds", f"{ttf_global_s:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.global-recompute.ttf_ms) over the 5 "
          "batches, /1000, s (== record's own summary.arms.global-recompute.ttf_p50_ms)")
    m.add("osdiR18Speedup", f"{speedup:.3f}",
          f"{relpath(R18_PROBE_ROWS)}: median(global-recompute ttf_ms) / median(tgms-L1 "
          "ttf_ms) -- P5/P6's speedup convention, <1.0 means L1 is slower here")
    m.add("osdiR18Precision", f"{precision_med * 100:.2f}",
          f"{relpath(R18_PROBE_ROWS)}: median(changed_count / candidate_survivors) over the "
          "5 batches, percent")
    m.add("osdiR18AvoidedRecompute", f"{avoided_decision * 100:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: 1 - sum(arms.tgms-L1.invalidated_count) / "
          "sum(n_registered) over the 5 batches, percent (== record's own summary field)")


# --------------------------------------------------------------------------
# D-160 CollegeMsg re-measurement (Lane W2c): the production claim gate now
# drops `unverifiable` claims too (docs/STABILITY.md section 9), and this is
# the fresh coverage/conditional-accuracy/UCR record measured under it. The
# old-gate numbers already on public surfaces are never overwritten -- see
# docs/site_facts.json's `unsupported_claims` fact and the new
# `*_d160` facts beside it.
# --------------------------------------------------------------------------

def _carries_claim(row: dict) -> bool:
    ao = row.get("answer_object")
    return bool(ao and ao.get("claims"))


def compute_d160(m: Macros) -> None:
    manifest = json.loads(D160_MANIFEST.read_text(encoding="utf-8"))
    rows_bytes = D160_ROWS.read_bytes()
    digest = hashlib.sha256(rows_bytes).hexdigest()
    eq(digest, manifest["result_digest"],
       "D160: sha256(rows-2026-09-14.json) matches manifest.result_digest")

    rows = json.loads(rows_bytes)
    eq(len(rows), 1128, "D160 frozen: total row count")
    eq(len(rows), manifest["provenance"]["n_rows"], "D160: row count matches manifest n_rows")

    def by_system(name: str) -> list[dict]:
        return [r for r in rows if r["system"] == name]

    ours = by_system("ours")
    b6e = by_system("b6e")
    b5 = by_system("b5")
    llm_direct = by_system("llm_direct")

    eq(len(ours), 282, "D160 frozen: ours row count")
    eq(len(b6e), 282, "D160 frozen: b6e row count")
    eq(len(b5), 282, "D160 frozen: b5 row count")
    eq(len(llm_direct), 282, "D160 frozen: llm_direct row count")

    task_ids = {r["task_id"] for r in ours}
    eq(len(task_ids), 94, "D160 frozen: distinct CollegeMsg task count")
    seeds = sorted({r["seed"] for r in ours})
    eq(seeds, [0, 1, 2], "D160: three seeds, 0/1/2")

    # ---- ours: coverage, conditional accuracy, ucr, ucr_pre_gate ----
    ours_carry = [r for r in ours if _carries_claim(r)]
    eq(len(ours_carry), 112, "D160 frozen: ours claim-carrying row count, pooled")
    ours_coverage = len(ours_carry) / len(ours)
    close(ours_coverage, 0.397, 0.001, "D160 frozen: ours pooled coverage")

    ours_cond_acc = statistics.mean(r["em"] for r in ours_carry)
    close(ours_cond_acc, 0.509, 0.001, "D160 frozen: ours pooled conditional accuracy")

    ours_ucr_gated = statistics.mean(r["ucr"] for r in ours_carry)
    eq(ours_ucr_gated, 0.0, "D160 frozen: ours pooled post-gate UCR")

    # ucr_pre_gate is only meaningful for rows that reached claim proposal at
    # all -- a safe-refusal row with no plan never produced a raw AnswerObject
    # to score pre-gate, and the record correctly omits the field there.
    ours_pre = [r for r in ours if "ucr_pre_gate" in r]
    eq(len(ours_pre), 257, "D160 frozen: ours rows that reached claim proposal pre-gate")
    ours_ucr_pre = statistics.mean(r["ucr_pre_gate"] for r in ours_pre)
    close(ours_ucr_pre, 0.212, 0.001, "D160 frozen: ours pooled pre-gate UCR")

    # Constancy: a per-seed coverage outlier averaged away by pooling must
    # not pass silently.
    for s in seeds:
        rs = [r for r in ours if r["seed"] == s]
        c = [r for r in rs if _carries_claim(r)]
        seed_cov = len(c) / len(rs)
        close(seed_cov, ours_coverage, 0.02,
              f"D160: ours seed {s} coverage {seed_cov:.4f} not within 0.02 of pooled "
              f"{ours_coverage:.4f}")

    # ---- b6e: its own ECQR-based gate, coverage + conditional accuracy ----
    b6e_carry = [r for r in b6e if _carries_claim(r)]
    b6e_coverage = len(b6e_carry) / len(b6e)
    close(b6e_coverage, 0.830, 0.001, "D160 frozen: b6e pooled coverage")
    b6e_cond_acc = statistics.mean(r["em"] for r in b6e_carry)
    close(b6e_cond_acc, 0.333, 0.001, "D160 frozen: b6e pooled conditional accuracy")
    for s in seeds:
        rs = [r for r in b6e if r["seed"] == s]
        c = [r for r in rs if _carries_claim(r)]
        seed_cov = len(c) / len(rs)
        close(seed_cov, b6e_coverage, 0.02,
              f"D160: b6e seed {s} coverage {seed_cov:.4f} not within 0.02 of pooled "
              f"{b6e_coverage:.4f}")

    # ---- b5: ungated interface ablation, raw EM (deterministic, temp 0) ----
    b5_em = statistics.mean(r["em"] for r in b5)
    close(b5_em, 0.181, 0.001, "D160 frozen: b5 pooled raw EM")
    for s in seeds:
        rs = [r for r in b5 if r["seed"] == s]
        seed_em = statistics.mean(r["em"] for r in rs)
        eq(round(seed_em, 6), round(b5_em, 6),
           f"D160: b5 seed {s} EM must equal the pooled EM (deterministic, temperature 0)")

    # ---- llm_direct: 0 claim-carrying rows, 216 context-overflow errors ----
    llm_carry = [r for r in llm_direct if _carries_claim(r)]
    eq(len(llm_carry), 0, "D160 frozen: llm_direct pooled claim-carrying row count")
    llm_errors = [r for r in llm_direct if r.get("task_error")]
    eq(len(llm_errors), 216, "D160 frozen: llm_direct task_error row count")
    llm_overflow = [r for r in llm_errors if "ContextWindowExceededError" in str(r["task_error"])]
    eq(len(llm_overflow), len(llm_errors),
       "D160: every llm_direct task_error is the known context-overflow error -- no other "
       "failure mode is silently folded into this count")

    # ---- old-gate counterparts: parsed out of docs/site_facts.json's
    # unsupported_claims fact (the only place the pre-D-160 CollegeMsg
    # numbers live as structured data) and cross-checked against
    # docs/STABILITY.md section 9, which is the only place the pre-D-160
    # conditional accuracy (0.548) is stated at all -- site_facts.json has
    # no separate coverage/conditional-accuracy fact for the old gate.
    facts = json.loads(SITE_FACTS.read_text(encoding="utf-8"))["facts"]
    old_uc = facts["unsupported_claims"]
    eq(old_uc["value"], "0", "D160: old-gate unsupported_claims value is still 0")
    eq(old_uc.get("label_required"), "pre-D-160 gate",
       "D160: old-gate unsupported_claims must be labelled 'pre-D-160 gate' now that the "
       "D-160-gate numbers land beside it")

    m_prose = re.search(
        r"0 of (\d+) on the frozen CollegeMsg campaign; before gating it was \d+ of \d+",
        old_uc["prose"])
    require(m_prose is not None,
            "D160: could not parse the old-gate emitted-answer count out of "
            "site_facts.json's unsupported_claims prose")
    old_gate_ucr_denominator = int(m_prose.group(1))
    eq(old_gate_ucr_denominator, 199, "D160 frozen: old-gate UCR denominator (emitted answers)")

    m_scope = re.search(r"\((\d+) emitted, of (\d+) task runs\)", old_uc["scope_required"])
    require(m_scope is not None,
            "D160: could not parse the old-gate coverage numerator/denominator out of "
            "site_facts.json's unsupported_claims scope_required")
    old_gate_coverage_num = int(m_scope.group(1))
    old_gate_coverage_den = int(m_scope.group(2))
    eq(old_gate_coverage_num, old_gate_ucr_denominator,
       "D160: old-gate coverage numerator matches the UCR denominator (same 199 emitted answers)")
    eq(old_gate_coverage_den, 282, "D160 frozen: old-gate coverage denominator (task runs)")
    old_gate_coverage = old_gate_coverage_num / old_gate_coverage_den
    close(old_gate_coverage, 0.706, 0.001, "D160 frozen: old-gate pooled coverage")

    stab_text = STABILITY_MD.read_text(encoding="utf-8")
    m_stab = re.search(
        r"ucr_gated`\s+0/(\d+),\s+coverage\s+(0\.\d+)\s+at\s+conditional accuracy\s+(0\.\d+)",
        stab_text)
    require(m_stab is not None,
            "D160: could not find the old-gate coverage/conditional-accuracy sentence in "
            "docs/STABILITY.md section 9 (D-160)")
    eq(int(m_stab.group(1)), old_gate_ucr_denominator,
       "D160: STABILITY.md's old-gate UCR denominator matches site_facts.json's")
    eq(float(m_stab.group(2)), round(old_gate_coverage, 3),
       "D160: STABILITY.md's old-gate coverage matches the ratio recomputed from "
       "site_facts.json's unsupported_claims fact")
    old_gate_cond_acc = float(m_stab.group(3))
    eq(old_gate_cond_acc, 0.548, "D160 frozen: old-gate conditional accuracy")

    m.add("osdiD160Tasks", len(task_ids),
          f"{relpath(D160_ROWS)}: distinct task_id values among system==ours rows")
    m.add("osdiD160TaskRuns", len(ours),
          f"{relpath(D160_ROWS)}: row count for system==ours (94 tasks x 3 seeds)")
    m.add("osdiD160OursCarrying", len(ours_carry),
          f"{relpath(D160_ROWS)}: ours rows with answer_object.claims non-empty, pooled "
          "over 3 seeds")
    m.add("osdiD160OursCoverage", f"{ours_coverage:.3f}",
          f"{relpath(D160_ROWS)}: osdiD160OursCarrying / osdiD160TaskRuns")
    m.add("osdiD160OursCondAcc", f"{ours_cond_acc:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over ours claim-carrying rows")
    m.add("osdiD160OursUcrGated", int(ours_ucr_gated),
          f"{relpath(D160_ROWS)}: mean(ucr) over ours claim-carrying rows (post-gate)")
    m.add("osdiD160OursUcrPreGate", f"{ours_ucr_pre:.3f}",
          f"{relpath(D160_ROWS)}: mean(ucr_pre_gate) over the 257 ours rows that reached "
          "claim proposal pre-gate")
    m.add("osdiD160B6eCoverage", f"{b6e_coverage:.3f}",
          f"{relpath(D160_ROWS)}: b6e rows with answer_object.claims non-empty / 282, pooled")
    m.add("osdiD160B6eCondAcc", f"{b6e_cond_acc:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over b6e claim-carrying rows")
    m.add("osdiD160B5Em", f"{b5_em:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over all 282 b5 rows (ungated, deterministic)")
    m.add("osdiD160LlmDirectCarrying", len(llm_carry),
          f"{relpath(D160_ROWS)}: llm_direct rows with answer_object.claims non-empty, pooled")
    m.add("osdiD160LlmDirectOverflowErrors", len(llm_overflow),
          f"{relpath(D160_ROWS)}: llm_direct rows whose task_error is "
          "litellm.ContextWindowExceededError, of 282 (known pre-tokenizer-fix limitation, "
          "see benchmarks/d160-collegemsg-v1/README.md)")
    m.add("osdiOldGateCoverage", f"{old_gate_coverage:.3f}",
          f"{relpath(SITE_FACTS)}: unsupported_claims.scope_required 199/282, cross-checked "
          f"against {relpath(STABILITY_MD)} section 9's own stated 0.706 -- the pre-D-160-gate "
          "coverage, printed only beside osdiD160OursCoverage, never in its place")
    m.add("osdiOldGateUcr", int(float(old_uc["value"])),
          f"{relpath(SITE_FACTS)}: unsupported_claims.value, of {old_gate_ucr_denominator} "
          "emitted answers -- the pre-D-160-gate UCR")
    m.add("osdiOldGateCondAcc", f"{old_gate_cond_acc:.3f}",
          f"{relpath(STABILITY_MD)} section 9 (D-160): the pre-D-160-gate conditional accuracy "
          "among emitted answers -- not a separate site_facts.json field, so parsed from and "
          "cross-checked against that section's own prose")


# --------------------------------------------------------------------------
# D-160 llm_direct follow-up (Lane W2d): the whitespace-token budget
# approximation that produced 216/282 task_error rows in compute_d160's
# first-shipped llm_direct arm is fixed in tgms/eval/baselines.py (a real
# HF tokenizer, commits dab5c2a/8de040f) -- this re-runs llm_direct only,
# same task set/model/seeds, under that fix (job 212231). It does not
# touch or overwrite compute_d160's numbers or docs/site_facts.json; see
# benchmarks/d160-collegemsg-v1/README.md.
# --------------------------------------------------------------------------

def compute_d160_llm_direct_fix(m: Macros) -> None:
    manifest = json.loads(D160_MANIFEST_FIX.read_text(encoding="utf-8"))
    rows_bytes = D160_ROWS_FIX.read_bytes()
    digest = hashlib.sha256(rows_bytes).hexdigest()
    eq(digest, manifest["result_digest"],
       "D160 fix: sha256(rows-llm-direct-fix-2026-09-14.json) matches manifest.result_digest")

    rows = json.loads(rows_bytes)
    eq(len(rows), 282, "D160 fix frozen: total row count")
    eq(len(rows), manifest["provenance"]["n_rows"],
       "D160 fix: row count matches manifest n_rows")

    llm_direct = [r for r in rows if r["system"] == "llm_direct"]
    eq(len(llm_direct), len(rows), "D160 fix frozen: every row is system==llm_direct")

    task_ids = {r["task_id"] for r in llm_direct}
    eq(len(task_ids), 94, "D160 fix frozen: distinct CollegeMsg task count")
    seeds = sorted({r["seed"] for r in llm_direct})
    eq(seeds, [0, 1, 2], "D160 fix: three seeds, 0/1/2")

    # ---- 0 task_error rows: the fix's whole point ----
    errors = [r for r in llm_direct if r.get("task_error")]
    eq(len(errors), 0, "D160 fix frozen: llm_direct task_error row count")

    # ---- still 0 claim-carrying rows: the fix removes the context-overflow
    # crash, not the gate's verdict on this arm's claims ----
    carry = [r for r in llm_direct if _carries_claim(r)]
    eq(len(carry), 0, "D160 fix frozen: llm_direct pooled claim-carrying row count")
    coverage = len(carry) / len(llm_direct)
    eq(coverage, 0.0, "D160 fix frozen: llm_direct pooled coverage")

    # ---- meta.tokenizer_kind / meta.budget_effective_tokens: uniform
    # across all 282 rows, confirming the fix measured the real tokenizer
    # and the nominal budget in real tokens, not whitespace-approximated
    # ones ----
    tokenizer_kinds = {r["meta"]["tokenizer_kind"] for r in llm_direct}
    eq(tokenizer_kinds, {"hf_real"},
       "D160 fix: meta.tokenizer_kind must be uniformly 'hf_real' across all 282 rows")
    budgets = {r["meta"]["budget_effective_tokens"] for r in llm_direct}
    eq(budgets, {8000},
       "D160 fix: meta.budget_effective_tokens must be uniformly 8000 across all 282 rows")
    eq(manifest["protocol"]["ceilings"]["llm_direct_budget_tokens"], 8000,
       "D160 fix: manifest's nominal llm_direct_budget_tokens is 8000, matching every row's "
       "meta.budget_effective_tokens")

    # ---- raw pre-gate EM: score meta.pre_gate_answer (the answer before
    # GATED_VERDICTS dropped anything) against the frozen suite's gold
    # answers, via the same tgms.eval.metrics.score_answer/extract_pred the
    # harness itself uses to score every row's post-gate `em`. This is
    # exactly the README's "raw pre-gate exact-match 0.064" computation. ----
    from tgms.eval.metrics import extract_pred, score_answer
    suite = json.loads(D160_SUITE.read_text(encoding="utf-8"))
    tasks_by_id = {t["id"]: t for t in suite["test"]}
    eq(len(tasks_by_id), 94, "D160 fix: frozen suite has 94 distinct test tasks")

    pre_gate_ems = []
    for r in llm_direct:
        pga = r["meta"]["pre_gate_answer"]
        task = tasks_by_id[r["task_id"]]
        pred = extract_pred(task["answer_kind"], pga)
        pre_gate_ems.append(score_answer(task["answer_kind"], task["gold"], pred)["em"])
    eq(len(pre_gate_ems), 282,
       "D160 fix: every row scored pre-gate (0 errors, meta.pre_gate_answer present throughout)")
    raw_em = statistics.mean(pre_gate_ems)
    close(raw_em, 0.064, 0.001, "D160 fix frozen: llm_direct raw pre-gate EM")

    m.add("osdiD160LlmDirectCoverageFixed", f"{coverage:.3f}",
          f"{relpath(D160_ROWS_FIX)}: llm_direct rows with answer_object.claims non-empty / "
          "282, pooled over 3 seeds -- the fixed-budget re-run of osdiD160LlmDirectCarrying, "
          "still 0.000 (the fix removes the context-overflow crash, not the gate's verdict)")
    m.add("osdiD160LlmDirectErrorsFixed", len(errors),
          f"{relpath(D160_ROWS_FIX)}: llm_direct task_error row count, of 282 -- 0 after the "
          "real-tokenizer budget fix, vs osdiD160LlmDirectOverflowErrors (216) before it")
    m.add("osdiD160LlmDirectRawEmFixed", f"{raw_em:.3f}",
          f"{relpath(D160_ROWS_FIX)}: mean(em) of meta.pre_gate_answer scored via "
          "tgms.eval.metrics.score_answer/extract_pred against "
          "benchmarks/frozen-v1/suite-collegemsg.json's gold/answer_kind fields, over all 282 "
          "rows (complete, vs the pre-fix record's partial n=66 easy-task-only sample)")
    m.add("osdiD160LlmDirectTokenizerFixed", r"hf\_real",
          f"{relpath(D160_ROWS_FIX)}: meta.tokenizer_kind, asserted uniform across all 282 rows")
    m.add("osdiD160LlmDirectBudgetFixed", 8000,
          f"{relpath(D160_ROWS_FIX)}: meta.budget_effective_tokens, asserted uniform across all "
          "282 rows (== manifest protocol.ceilings.llm_direct_budget_tokens)")


# --------------------------------------------------------------------------
# C2 --- corruption-detection sweep, pre- and post-A10 (task A10 fixed the
# artifact_blob reader to content-address the whole file)
# --------------------------------------------------------------------------

def _corruption_result_digest(results: list[dict]) -> str:
    """Mirrors scripts/corruption_campaign_merge.py's result_digest(kind="corruption"):
    sha256 over the results sorted by (class, mutation, task_id, trial), canonical JSON."""
    key_fn = lambda r: (r["class"], r["mutation"], r.get("task_id", -1), r["trial"])  # noqa: E731
    canon = sorted(results, key=key_fn)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _corruption_detection_matrix(results: list[dict]) -> dict[tuple[str, str], list[int]]:
    dm: dict[tuple[str, str], list[int]] = {}
    for r in results:
        key = (r["class"], r["mutation"])
        cell = dm.setdefault(key, [0, 0])
        cell[0] += 1
        if r["verdict"] == "DETECTED":
            cell[1] += 1
    return dm


def compute_c2(m: Macros) -> None:
    pre = json.loads(CORRUPTION_PRE.read_text(encoding="utf-8"))
    post = json.loads(CORRUPTION_POST.read_text(encoding="utf-8"))

    for label, d, path in (("pre-A10", pre, CORRUPTION_PRE), ("post-A10", post, CORRUPTION_POST)):
        digest = _corruption_result_digest(d["results"])
        eq(digest, d["result_digest"],
           f"C2 {label}: sha256(sorted results, corruption_campaign_merge.py's own scheme) "
           f"matches {relpath(path)}'s own result_digest")
        eq(len(d["results"]), 10000, f"C2 {label} frozen: trial row count")
        eq(d["total_trials"], len(d["results"]), f"C2 {label}: total_trials matches len(results)")

    pre_dm = _corruption_detection_matrix(pre["results"])
    post_dm = _corruption_detection_matrix(post["results"])

    # Cross-check the recomputed per-cell (trials, detected) against each
    # record's own stats.detection_matrix -- never trusted without this.
    for label, d, dm in (("pre-A10", pre, pre_dm), ("post-A10", post, post_dm)):
        stat_dm = d["stats"]["detection_matrix"]
        eq(len(stat_dm), len(dm),
           f"C2 {label}: recomputed cell count matches stats.detection_matrix")
        for key, cell in stat_dm.items():
            c, mut = key.split("|")
            trials, detected = dm[(c, mut)]
            eq(trials, cell["trials"], f"C2 {label}: {key} trials matches recomputed")
            eq(detected, cell["detected"], f"C2 {label}: {key} detected matches recomputed")

    classes = sorted({c for c, _ in pre_dm})
    mutations = sorted({mut for _, mut in pre_dm})
    eq(len(classes), 13, "C2 frozen: distinct on-disk file classes")
    eq(len(mutations), 7, "C2 frozen: distinct mutation kinds")
    eq(sorted({c for c, _ in post_dm}), classes,
       "C2: post-A10 record's file classes match the pre-A10 record's")
    eq(sorted({mut for _, mut in post_dm}), mutations,
       "C2: post-A10 record's mutation kinds match the pre-A10 record's")

    recomputed_silent_pre = sum(1 for r in pre["results"] if r["verdict"] == "SILENT")
    recomputed_silent_post = sum(1 for r in post["results"] if r["verdict"] == "SILENT")
    eq(recomputed_silent_pre, 0, "C2 frozen: pre-A10 SILENT count")
    eq(recomputed_silent_post, 0, "C2 frozen: post-A10 SILENT count")
    eq(recomputed_silent_pre, pre["stats"]["verdict_counts"].get("SILENT", 0),
       "C2: recomputed pre-A10 SILENT matches the record's own verdict_counts")
    eq(recomputed_silent_post, post["stats"]["verdict_counts"].get("SILENT", 0),
       "C2: recomputed post-A10 SILENT matches the record's own verdict_counts")

    detected_pre = sum(1 for r in pre["results"] if r["verdict"] == "DETECTED")
    detected_post = sum(1 for r in post["results"] if r["verdict"] == "DETECTED")
    eq(detected_pre, 5966, "C2 frozen: pre-A10 total DETECTED count")
    eq(detected_post, 6587, "C2 frozen: post-A10 total DETECTED count")
    eq(detected_pre, pre["stats"]["verdict_counts"]["DETECTED"],
       "C2: recomputed pre-A10 DETECTED matches the record's own verdict_counts")
    eq(detected_post, post["stats"]["verdict_counts"]["DETECTED"],
       "C2: recomputed post-A10 DETECTED matches the record's own verdict_counts")

    # The six artifact_blob mutations task A10 fixed. delete_file is
    # deliberately excluded: it was already DETECTED 89/89 before A10 (a
    # different code path -- an outright-missing plan blob, not a doctored
    # one), and the README's own six-cell diff excludes it too.
    blob_fixed = ("append_garbage", "flip_bit", "flip_byte", "swap_same_class",
                  "truncate", "zero_span")
    require(set(blob_fixed).issubset(mutations),
            "C2: the six blob-fix mutations are a subset of the campaign's own mutation set")
    require("delete_file" in mutations and "delete_file" not in blob_fixed,
            "C2: delete_file (already DETECTED pre-A10) is deliberately excluded from the "
            "blob-fix set")

    n_blob_pre = sum(pre_dm[("artifact_blob", mut)][0] for mut in blob_fixed)
    det_blob_pre = sum(pre_dm[("artifact_blob", mut)][1] for mut in blob_fixed)
    n_blob_post = sum(post_dm[("artifact_blob", mut)][0] for mut in blob_fixed)
    det_blob_post = sum(post_dm[("artifact_blob", mut)][1] for mut in blob_fixed)
    eq(n_blob_pre, n_blob_post,
       "C2: the blob-fix trial population is identical pre/post-A10 (same seeds, same design)")
    eq(n_blob_pre, 621, "C2 frozen: blob-fix mutation trial count (N)")
    eq(det_blob_pre, 0, "C2 frozen: pre-A10 blob-fix mutations DETECTED count (0 of N)")
    eq(det_blob_post, n_blob_post, "C2 frozen: post-A10 blob-fix mutations DETECTED count (N of N)")

    # All seven artifact_blob mutations (the six A10 fixed, plus the one
    # that was already DETECTED before A10 via a different mechanism) --
    # a reader citing "blob detected" without qualification would otherwise
    # assume this population, not the six-mutation fix subset above.
    all_blob_mutations = sorted(mut for c, mut in pre_dm if c == "artifact_blob")
    eq(len(all_blob_mutations), 7, "C2 frozen: artifact_blob mutation count (all seven)")
    eq(set(all_blob_mutations), set(mutations),
       "C2: artifact_blob's own mutation set equals the campaign's full mutation set")

    n_blob_all_pre = sum(pre_dm[("artifact_blob", mut)][0] for mut in all_blob_mutations)
    det_blob_all_pre = sum(pre_dm[("artifact_blob", mut)][1] for mut in all_blob_mutations)
    n_blob_all_post = sum(post_dm[("artifact_blob", mut)][0] for mut in all_blob_mutations)
    det_blob_all_post = sum(post_dm[("artifact_blob", mut)][1] for mut in all_blob_mutations)
    eq(n_blob_all_pre, n_blob_all_post,
       "C2: artifact_blob's total trial population (all seven mutations) is identical "
       "pre/post-A10")
    eq(n_blob_all_pre, 710, "C2 frozen: artifact_blob total trial count, all seven mutations")
    eq(det_blob_all_post, n_blob_all_post,
       "C2 frozen: post-A10 artifact_blob DETECTED count over all seven mutations (N of N)")

    # The one artifact_blob mutation DETECTED pre-A10 -- recomputed, never
    # hard-coded, as "the mutation outside the six-mutation fix set with a
    # nonzero pre-A10 DETECTED count".
    already_detected = [mut for mut in all_blob_mutations
                        if mut not in blob_fixed and pre_dm[("artifact_blob", mut)][1] > 0]
    eq(len(already_detected), 1,
       "C2: exactly one artifact_blob mutation was already DETECTED pre-A10")
    already_detected_mutation = already_detected[0]
    eq(already_detected_mutation, "delete_file",
       "C2 frozen: the pre-A10-already-detected artifact_blob mutation")

    # Partition check: the six-mutation fix set and the already-detected
    # mutation must cover all seven blob mutations, disjointly, and their
    # trial counts must sum to the all-mutations total (710) -- not merely
    # assumed from the mutation-name partition alone.
    eq(set(blob_fixed) | set(already_detected), set(all_blob_mutations),
       "C2: the six-mutation fix set + the already-detected mutation cover all seven blob "
       "mutations")
    eq(set(blob_fixed) & set(already_detected), set(),
       "C2: the six-mutation fix set and the already-detected mutation are disjoint")
    n_already_pre, det_already_pre = pre_dm[("artifact_blob", already_detected_mutation)]
    eq(n_blob_pre + n_already_pre, n_blob_all_pre,
       "C2: the six-mutation trial count plus the already-detected mutation's trial count "
       "sums to the all-seven-mutations total -- a partition of the 710 blob trials")
    eq(det_already_pre, n_already_pre,
       "C2 frozen: the already-detected mutation was DETECTED in every one of its own "
       "pre-A10 trials")
    eq(det_blob_all_pre, det_already_pre,
       "C2: pre-A10 DETECTED over all seven blob mutations equals the already-detected "
       "mutation's own count alone (the six fixed mutations contributed 0)")

    # Every non-blob cell must be within +/-2 trials of the pre-A10 record
    # (the README's own pre-registered prediction #8); this is the
    # constancy assertion, never averaged away.
    moved = [key for key in pre["stats"]["detection_matrix"]
             if not key.startswith("artifact_blob|")
             and abs(pre_dm[tuple(key.split("|"))][1] - post_dm[tuple(key.split("|"))][1]) > 2]
    eq(len(moved), 0,
       f"C2 frozen: non-blob class x mutation cells whose DETECTED count moved by >2 trials "
       f"post-A10 (got {moved})")

    m.add("osdiCorruptionTrials", tex_num(pre["total_trials"]),
          f"{relpath(CORRUPTION_PRE)}: total_trials, == {relpath(CORRUPTION_POST)}'s own "
          "(both 10,000-trial sweeps)")
    m.add("osdiCorruptionClasses", len(classes),
          f"{relpath(CORRUPTION_PRE)}: distinct classes in stats.detection_matrix keys "
          "(the 13 on-disk file families), unchanged post-A10")
    m.add("osdiCorruptionMutations", len(mutations),
          f"{relpath(CORRUPTION_PRE)}: distinct mutations in stats.detection_matrix keys, "
          "unchanged post-A10")
    m.add("osdiCorruptionDetected", tex_num(detected_post),
          f"{relpath(CORRUPTION_POST)}: recomputed DETECTED total, of 10,000 -- the deployed "
          "(post-A10) system's headline count; see osdiCorruptionDetectedPre/Post for the "
          "before/after pair")
    m.add("osdiCorruptionDetectedPre", tex_num(detected_pre),
          f"{relpath(CORRUPTION_PRE)}: recomputed DETECTED total, of 10,000, before task A10")
    m.add("osdiCorruptionDetectedPost", tex_num(detected_post),
          f"{relpath(CORRUPTION_POST)}: recomputed DETECTED total, of 10,000, after task A10")
    m.add("osdiCorruptionSilentPre", recomputed_silent_pre,
          f"{relpath(CORRUPTION_PRE)}: recomputed SILENT total, of 10,000, before task A10")
    m.add("osdiCorruptionSilentPost", recomputed_silent_post,
          f"{relpath(CORRUPTION_POST)}: recomputed SILENT total, of 10,000, after task A10")
    m.add("osdiCorruptionBlobDetectedPre", f"{det_blob_pre}/{n_blob_pre}",
          f"{relpath(CORRUPTION_PRE)}: DETECTED/trials summed over the six artifact_blob "
          "mutations task A10 fixed (append_garbage/flip_bit/flip_byte/swap_same_class/"
          "truncate/zero_span), before the fix")
    m.add("osdiCorruptionBlobDetectedPost", f"{det_blob_post}/{n_blob_post}",
          f"{relpath(CORRUPTION_POST)}: DETECTED/trials summed over the same six artifact_blob "
          "mutations, after the fix")
    m.add("osdiCorruptionBlobTrials", tex_num(n_blob_all_pre),
          f"{relpath(CORRUPTION_PRE)}: trials summed over all seven artifact_blob mutations "
          "(== the six-mutation fix population 621 + the already-detected mutation's own "
          f"{n_already_pre}), unchanged post-A10")
    m.add("osdiCorruptionBlobDetectedAllPre", f"{det_blob_all_pre}/{n_blob_all_pre}",
          f"{relpath(CORRUPTION_PRE)}: DETECTED/trials summed over all seven artifact_blob "
          "mutations, before the fix -- distinct from osdiCorruptionBlobDetectedPre, which "
          "covers only the six mutations A10 fixed")
    m.add("osdiCorruptionBlobDetectedAllPost", f"{det_blob_all_post}/{n_blob_all_post}",
          f"{relpath(CORRUPTION_POST)}: DETECTED/trials summed over all seven artifact_blob "
          "mutations, after the fix")
    m.add("osdiCorruptionBlobMutationAlreadyDetected", already_detected_mutation.replace("_", r"\_"),
          f"{relpath(CORRUPTION_PRE)}: the one artifact_blob mutation (of all seven) with a "
          "nonzero pre-A10 DETECTED count, recomputed as the mutation outside the six-mutation "
          "fix set with detected>0 -- not hard-coded")
    m.add("osdiCorruptionCellsMovedPost", len(moved),
          f"{relpath(CORRUPTION_PRE)} vs {CORRUPTION_POST.name}: non-blob class x mutation "
          "cells whose DETECTED count moved by more than 2 trials (asserted 0 above)")


# --------------------------------------------------------------------------
# D4/D4b --- overhead ladder, five rungs, twelve plans, three seeds
# --------------------------------------------------------------------------

def compute_ladder(m: Macros) -> None:
    merged = json.loads(LADDER_MERGED.read_text(encoding="utf-8"))
    raws = [json.loads(p.read_text(encoding="utf-8")) for p in LADDER_RAW]

    for raw, path in zip(raws, LADDER_RAW):
        digest = hashlib.sha256(
            json.dumps(raw["rows"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        eq(digest, raw["result_digest"],
           f"ladder: sha256(rows) matches {relpath(path)}'s own result_digest")

    summary_digest = hashlib.sha256(
        json.dumps(merged["summary"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    eq(summary_digest, merged["result_digest"],
       f"ladder: sha256(summary) matches {relpath(LADDER_MERGED)}'s own result_digest")
    eq(merged["seeds"], [0, 1, 2], "ladder: pooled record covers seeds 0/1/2")

    def rows_of(raw, rung):
        return [r for r in raw["rows"] if r["rung"] == rung]

    # ---- rung 1: leaf_overhead, per operator, flattened over every
    # (plan-occurrence, seed) sample -- never trusted from the summary's own
    # median without this recomputation. ----
    r1_samples: dict[str, list[float]] = {}
    for raw in raws:
        for outer in rows_of(raw, 1):
            for inner in outer["rows"]:
                r1_samples.setdefault(inner["op"], []).append(inner["leaf_over_direct"])
    r1_summary = merged["summary"]["rung1_leaf_overhead"]
    eq(len(r1_samples), 14, "ladder frozen: rung-1 distinct operator count (13 LEAF_SCOPES + "
       "compute)")
    eq(set(r1_samples), set(r1_summary), "ladder: rung-1 recomputed op set matches summary's")
    r1_medians = {}
    for op, samples in r1_samples.items():
        med = statistics.median(samples)
        eq(sorted(samples), sorted(r1_summary[op]["per_seed"]),
           f"ladder rung1 {op}: recomputed per-sample values match summary's per_seed list")
        close(med, r1_summary[op]["leaf_over_direct_median"], 1e-9,
              f"ladder rung1 {op}: recomputed median matches summary")
        r1_medians[op] = med
    rung1_min_op = min(r1_medians, key=r1_medians.get)
    rung1_max_op = max(r1_medians, key=r1_medians.get)
    rung1_min, rung1_max = r1_medians[rung1_min_op], r1_medians[rung1_max_op]
    close(rung1_min, 0.9919982626082593, 1e-9, "ladder frozen: rung-1 minimum op median")
    close(rung1_max, 1.064464807738779, 1e-9, "ladder frozen: rung-1 maximum op median")
    require(0.7 <= rung1_min and rung1_max <= 1.5,
            "ladder: every rung-1 op median falls within the freeze's falsifier band [0.7, 1.5]")

    # ---- rung 2: compiled_vs_kernel, the two tgms.tgir.compiled.COMPILED
    # ops only. ----
    r2_samples: dict[str, list[float]] = {}
    for raw in raws:
        for outer in rows_of(raw, 2):
            for inner in outer["rows"]:
                r2_samples.setdefault(inner["op"], []).append(inner["compiled_over_kernel"])
    r2_summary = merged["summary"]["rung2_compiled_vs_kernel"]
    eq(set(r2_samples), {"entity_history", "version_history"},
       "ladder frozen: rung-2 population is exactly the two compiled ops")
    eq(len(r2_samples["entity_history"]), 6,
       "ladder frozen: entity_history rung-2 sample count (2 plan-occurrences x 3 seeds)")
    eq(len(r2_samples["version_history"]), 3,
       "ladder frozen: version_history rung-2 sample count (1 plan-occurrence x 3 seeds)")
    entity_ratio = statistics.median(r2_samples["entity_history"])
    version_ratio = statistics.median(r2_samples["version_history"])
    close(entity_ratio, r2_summary["entity_history"]["compiled_over_kernel_median"], 1e-9,
          "ladder rung2: recomputed entity_history median matches summary")
    close(version_ratio, r2_summary["version_history"]["compiled_over_kernel_median"], 1e-9,
          "ladder rung2: recomputed version_history median matches summary")
    close(entity_ratio, 1.1321759928956914, 1e-9, "ladder frozen: rung-2 entity_history median")
    close(version_ratio, 2.5403795318319973, 1e-9, "ladder frozen: rung-2 version_history median")
    require(1 <= entity_ratio <= 2000,
            "ladder: entity_history compiled_over_kernel falls within the freeze's pass_if "
            "band [1, 2000]")

    # ---- plan set: 12 plans, 14/14 operator coverage (union of every
    # rung's own per-plan ops_in_plan field, cross-checked against rung 1's
    # own 14-op population above). ----
    plan_ids = sorted({outer["plan_id"] for outer in rows_of(raws[0], 1)})
    eq(len(plan_ids), 12, "ladder frozen: plan count")
    all_ops = {op for outer in rows_of(raws[0], 1) for op in outer["ops_in_plan"]}
    eq(all_ops, set(r1_samples), "ladder: union of every plan's ops_in_plan matches rung-1's "
       "own 14-op population")

    # Step count per plan is only carried explicitly in the rung-5 rows
    # (n_steps); reuse that rather than hard-coding which plans are 1-step
    # vs 3-step.
    n_steps_by_plan = {outer["plan_id"]: outer["n_steps"] for outer in rows_of(raws[0], 5)}
    one_step_plans = {p for p, n in n_steps_by_plan.items() if n == 1}
    three_step_plans = {p for p, n in n_steps_by_plan.items() if n == 3}
    eq(len(one_step_plans), 9, "ladder frozen: one-step plan count")
    eq(len(three_step_plans), 3, "ladder frozen: three-step plan count")
    eq(one_step_plans | three_step_plans, set(plan_ids),
       "ladder: every plan is either 1-step or 3-step, no other shape")

    # ---- rung 3: trace_bytes, bit-identical across reps and across seeds
    # for every plan. ----
    r3_summary = merged["summary"]["rung3_trace_bytes"]
    plan_bytes: dict[str, list[int]] = {}
    for raw in raws:
        for outer in rows_of(raw, 3):
            require(outer["bytes_min"] == outer["bytes_max"] == outer["bytes_median"],
                    f"ladder rung3 {outer['plan_id']}: not bit-identical across its own 5 reps")
            require(len(set(outer["bytes_all"])) == 1,
                    f"ladder rung3 {outer['plan_id']}: bytes_all has more than one distinct value")
            plan_bytes.setdefault(outer["plan_id"], []).append(outer["bytes_median"])
    n_deterministic = 0
    for plan, vals in plan_bytes.items():
        require(len(set(vals)) == 1,
                f"ladder rung3 {plan}: bytes_median differs across seeds {vals}")
        n_deterministic += 1
        eq(vals[0], r3_summary[plan]["bytes_median"],
           f"ladder rung3 {plan}: recomputed bytes_median matches summary")
        require(r3_summary[plan]["reproducible_across_seeds"] is True,
                f"ladder rung3 {plan}: summary's own reproducible_across_seeds flag is not True")
    eq(n_deterministic, 12, "ladder frozen: plans confirmed bit-identical across reps and seeds")
    one_step_bytes_median = statistics.median(plan_bytes[p][0] for p in one_step_plans)
    three_step_bytes_median = statistics.median(plan_bytes[p][0] for p in three_step_plans)
    eq(one_step_bytes_median, 3309, "ladder frozen: 1-step trace_bytes median across plans")
    eq(three_step_bytes_median, 7549, "ladder frozen: 3-step trace_bytes median across plans")
    for plan in plan_bytes:
        band = (1500, 30000) if plan in one_step_plans else (4000, 90000)
        require(band[0] <= plan_bytes[plan][0] <= band[1],
                f"ladder rung3 {plan}: bytes_median {plan_bytes[plan][0]} outside the freeze's "
                f"predicted band {band}")

    # ---- rung 4: verify_ms, median p50 per plan, then median across plans. ----
    r4_summary = merged["summary"]["rung4_verify_ms"]
    plan_p50: dict[str, list[float]] = {}
    for raw in raws:
        for outer in rows_of(raw, 4):
            recomputed_p50 = statistics.median(outer["ms_all"])
            close(recomputed_p50, outer["p50_ms"], 1e-9,
                  f"ladder rung4 {outer['plan_id']}: median(ms_all) matches the raw row's own "
                  "p50_ms")
            plan_p50.setdefault(outer["plan_id"], []).append(outer["p50_ms"])
    plan_p50_median = {}
    for plan, vals in plan_p50.items():
        med = statistics.median(vals)
        close(med, r4_summary[plan]["p50_ms_median"], 1e-9,
              f"ladder rung4 {plan}: recomputed median matches summary")
        plan_p50_median[plan] = med
    verify_ms_median = statistics.median(plan_p50_median.values())
    close(verify_ms_median, 5.061005940660834, 1e-6, "ladder frozen: rung-4 median-of-plan-medians")
    require(0.5 <= min(plan_p50_median.values()) and max(plan_p50_median.values()) <= 50,
            "ladder: every rung-4 plan median falls within the freeze's pass_if band [0.5, 50]")

    # ---- rung 5: tokens_tool_calls -- tokens median, and the tool-calls ==
    # executed-steps invariant on every one of the 12 plans. ----
    r5_summary = merged["summary"]["rung5_tokens_tool_calls"]
    plan_tokens: dict[str, list[int]] = {}
    plan_tool_calls: dict[str, list[int]] = {}
    for raw in raws:
        for outer in rows_of(raw, 5):
            plan_tokens.setdefault(outer["plan_id"], []).append(outer["tokens"]["total"])
            plan_tool_calls.setdefault(outer["plan_id"], []).append(outer["tool_calls"])
    plan_tokens_median = {}
    truncated_plans = []
    n_tool_calls_eq_executed = 0
    for plan in plan_ids:
        med = statistics.median(plan_tokens[plan])
        eq(med, r5_summary[plan]["tokens_total_median"],
           f"ladder rung5 {plan}: recomputed tokens median matches summary")
        plan_tokens_median[plan] = med
        tc = plan_tool_calls[plan]
        require(len(set(tc)) == 1, f"ladder rung5 {plan}: tool_calls not constant across seeds")
        eq(sorted(tc), sorted(r5_summary[plan]["tool_calls_per_seed"]),
           f"ladder rung5 {plan}: recomputed tool_calls_per_seed matches summary")
        n_steps = n_steps_by_plan[plan]
        eq(n_steps, r5_summary[plan]["n_steps"], f"ladder rung5 {plan}: n_steps matches summary")
        # The merge's own "executed_steps" field, by this campaign's
        # construction (README: Executor.run's truncation guard stops a
        # plan before its trailing compute step ever reaches
        # ToolRouter.call), is exactly tool_calls -- confirmed here, not
        # assumed, since executed_steps is not itself a raw-row field.
        eq(r5_summary[plan]["executed_steps"], tc[0],
           f"ladder rung5 {plan}: summary's executed_steps equals raw tool_calls")
        eq(tc[0] == r5_summary[plan]["executed_steps"],
           r5_summary[plan]["tool_calls_eq_executed_steps"],
           f"ladder rung5 {plan}: tool_calls_eq_executed_steps recomputes correctly")
        require(r5_summary[plan]["tool_calls_eq_executed_steps"] is True,
                f"ladder rung5 {plan}: tool_calls != executed_steps -- falsifier (a) trips")
        n_tool_calls_eq_executed += 1
        if tc[0] < n_steps:
            truncated_plans.append(plan)
        band = (800, 8000) if plan in one_step_plans else (2000, 20000)
        require(band[0] <= med <= band[1],
                f"ladder rung5 {plan}: tokens median {med} outside the freeze's predicted "
                f"band {band}")
    eq(n_tool_calls_eq_executed, 12,
       "ladder frozen: plans asserted tool_calls == executed_steps")
    tokens_median_overall = statistics.median(plan_tokens_median.values())
    eq(tokens_median_overall, 1811.5, "ladder frozen: rung-5 median-of-plan-medians tokens.total")
    eq(sorted(truncated_plans), ["p02-compiled-entity-and-version", "p10-reachability-and-paths"],
       "ladder frozen: the exact two plans truncated at 2-of-3 executed steps")

    m.add("osdiLadderPlans", tex_num(len(plan_ids)),
          f"{relpath(LADDER_RAW[0])}: distinct plan_id values in the rung-1 rows")
    m.add("osdiLadderOperatorsCovered", tex_num(len(all_ops)),
          f"{relpath(LADDER_RAW[0])}: union of every plan's ops_in_plan, rung 1 (13 LEAF_SCOPES "
          "+ compute)")
    m.add("osdiLadderRung1Min", f"{rung1_min:.3f}",
          f"{relpath(LADDER_MERGED)}: min over 14 ops of rung1_leaf_overhead[op]."
          f"leaf_over_direct_median ({rung1_min_op})")
    m.add("osdiLadderRung1Max", f"{rung1_max:.3f}",
          f"{relpath(LADDER_MERGED)}: max over 14 ops of rung1_leaf_overhead[op]."
          f"leaf_over_direct_median ({rung1_max_op})")
    m.add("osdiLadderRung2EntityHistory", f"{entity_ratio:.2f}",
          f"{relpath(LADDER_MERGED)}: rung2_compiled_vs_kernel.entity_history."
          "compiled_over_kernel_median")
    m.add("osdiLadderRung2VersionHistory", f"{version_ratio:.2f}",
          f"{relpath(LADDER_MERGED)}: rung2_compiled_vs_kernel.version_history."
          "compiled_over_kernel_median")
    m.add("osdiLadderRung3BytesOneStepMedian", tex_num(one_step_bytes_median),
          f"{relpath(LADDER_MERGED)}: median over the 9 one-step plans of "
          "rung3_trace_bytes[plan].bytes_median")
    m.add("osdiLadderRung3BytesThreeStepMedian", tex_num(three_step_bytes_median),
          f"{relpath(LADDER_MERGED)}: median over the 3 three-step plans of "
          "rung3_trace_bytes[plan].bytes_median")
    m.add("osdiLadderRung3Deterministic", tex_num(n_deterministic),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans confirmed bit-identical across all 5 reps "
          "and all 3 seeds (asserted above), of 12")
    m.add("osdiLadderRung4VerifyMsMedian", f"{verify_ms_median:.2f}",
          f"{relpath(LADDER_MERGED)}: median over the 12 plans of rung4_verify_ms[plan]."
          "p50_ms_median, ms")
    m.add("osdiLadderRung5TokensMedian", f"{tokens_median_overall:.1f}",
          f"{relpath(LADDER_MERGED)}: median over the 12 plans of rung5_tokens_tool_calls[plan]."
          "tokens_total_median")
    m.add("osdiLadderRung5ToolCallsEqualExecutedSteps", tex_num(n_tool_calls_eq_executed),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans with tool_calls == executed steps "
          "(falsifier (a)'s own 'executed steps' wording, not a naive n_steps comparison), "
          "asserted on all 12")
    m.add("osdiLadderPlansTruncated", tex_num(len(truncated_plans)),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans where tool_calls < n_steps "
          "(p02, p10 -- Executor.run's own truncation guard on a downstream compute step, "
          "see README)")


# --------------------------------------------------------------------------
# Longevity -- 24h soak (Lane W2g / Lane B task B7a, Gate G1/Gate E)
#
# benchmarks/longevity-v1/: the real (non-dev-host) 24h soak,
# scripts/longevity_run.py at commit 886805f (pre-fix for
# D-086-reader-torn-tail-race) against stores/synth-1m-native on xzgpu.
# README.md documents three harness defects this generator must not paper
# over, and this function's own checks recompute past every one of them
# rather than trusting a summary field:
#   (1) manifest.summary.error_count (1) is the LAST writer life's counter
#       only -- counter_latest's unlabeled-key collision silently drops
#       every earlier life's counters. The true total (249) is summed here
#       from writer_error_counts_by_life.json's per-life rows and asserted
#       against that file's own true_total_errors_all_lives field, never
#       trusted from either without the recomputation. Both numbers are
#       emitted, side by side, per this script's non-overwrite discipline --
#       osdiSoakWriterErrorsManifest is explicitly labelled as the wrong,
#       harness-defect value.
#   (2) digest_equal is JSON `null` ("not computed") because the disk guard
#       skipped the mandatory final replay: 1,074,952 uncompacted batches
#       project (D-149's own O(batches^2) manifest-growth formula,
#       recomputed here, not just read) to ~280.8 TB, refused by
#       --max-disk-mb=20000. Never "computed and found False" --
#       gate_e_report.md's own "digest_equal=False" text is a display
#       artifact of scripts/longevity_report.py coercing None to False,
#       asserted below as a known, cited discrepancy, not this macro's
#       source of truth.
#   (3) reader_restarts.jsonl's two rows (readers 6 and 5) are both the same
#       D-086 reader-torn-tail race, cross-checked against
#       ops/failure_ledger.jsonl's own entry when that file is present on
#       disk (it is coordinator-maintained on main, not edited by this
#       measurement worktree).
# No verdict macro is emitted here, per the lane brief -- Gate E's own
# PASS/FAIL/FLAG table lives in gate_e_report.md, not in this script.
# --------------------------------------------------------------------------

def compute_longevity_soak(m: Macros) -> None:
    # Whole-file digest check against benchmarks/longevity-v1/README.md's
    # own "Files here" table (sha256, verified byte-identical to the xzgpu
    # originals before commit) -- catches an edited committed copy before
    # any field inside it is even parsed.
    eq(sha256_file(LONGEVITY_MANIFEST), LONGEVITY_MANIFEST_SHA256,
       f"{relpath(LONGEVITY_MANIFEST)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_RECOVERIES), LONGEVITY_RECOVERIES_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_READER_RESTARTS), LONGEVITY_READER_RESTARTS_SHA256,
       f"{relpath(LONGEVITY_READER_RESTARTS)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_LEDGER), LONGEVITY_LEDGER_SHA256,
       f"{relpath(LONGEVITY_LEDGER)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_ORCHESTRATOR_LOG), LONGEVITY_ORCHESTRATOR_LOG_SHA256,
       f"{relpath(LONGEVITY_ORCHESTRATOR_LOG)}: sha256 matches README.md's Files-here table")

    manifest = json.loads(LONGEVITY_MANIFEST.read_text(encoding="utf-8"))
    summary = manifest["summary"]

    # --- commit + duration ---
    eq(manifest["git_commit"], "886805f", "Longevity frozen: measured commit")
    duration_s = manifest["config"]["duration_s"]
    eq(duration_s, 86400.0, "Longevity frozen: configured soak duration_s")
    hours = duration_s / 3600.0
    eq(hours, 24.0, "Longevity: config.duration_s / 3600 is exactly 24 hours")

    # --- entity count start/end ---
    # The starting count (1,000,000) is not a counted field anywhere in the
    # manifest or any side-file -- it is the store's own name
    # ("synth-1m-native"), the same "<N>m" naming convention compute_c4
    # above already treats as meaningful (control_1m/control_10m). Asserted
    # here only as "the store name says 1m", not as a counted field; the end
    # count *is* a counted field (final_stats.n_entities) and is checked as
    # one, with a frozen expected value.
    store_name = manifest["config"]["store_name"]
    eq(store_name, "synth-1m-native", "Longevity: config.store_name")
    eq(manifest["dataset"]["name"], store_name,
       "Longevity: dataset.name matches config.store_name")
    entities_start = 1_000_000
    entities_end = summary["final_stats"]["n_entities"]
    eq(entities_end, 1_730_492, "Longevity frozen: summary.final_stats.n_entities")
    require(entities_end > entities_start,
            "Longevity: the store grew past its nominal 1M starting size over the soak")

    # --- batches ---
    total_batches = summary["total_batches"]
    eq(total_batches, 1_074_952, "Longevity frozen: summary.total_batches")

    # --- writer lives / the true-vs-manifest error count defect ---
    by_life = json.loads(LONGEVITY_WRITER_ERRORS_BY_LIFE.read_text(encoding="utf-8"))
    per_life_errors = by_life["per_life_errors"]
    eq(by_life["lives"], 42, "Longevity frozen: writer_error_counts_by_life.json lives")
    eq(len(per_life_errors), by_life["lives"],
       "Longevity: per_life_errors row count matches the file's own lives field")
    true_total_errors = sum(per_life_errors)
    eq(true_total_errors, 249,
       "Longevity frozen: true total writer errors, summed over 42 lives")
    eq(true_total_errors, by_life["true_total_errors_all_lives"],
       "Longevity: recomputed sum(per_life_errors) matches the file's own "
       "true_total_errors_all_lives field")

    manifest_error_count = summary["error_count"]
    eq(manifest_error_count, 1, "Longevity frozen: manifest's own (wrong) summary.error_count")
    eq(manifest_error_count, summary["writer_final"]["errors"],
       "Longevity: summary.error_count matches writer_final.errors (both life-41-only)")
    eq(manifest_error_count, by_life["manifest_reported_error_count"],
       "Longevity: manifest's error_count matches writer_error_counts_by_life.json's "
       "own record of that (wrong) figure")
    require(true_total_errors != manifest_error_count,
            "Longevity: the true per-life sum must differ from the manifest's single-life "
            "figure -- this is the harness defect the README documents, not a no-op check")

    # --- recoveries: designed restart cycle, by cause ---
    recoveries_rows = load_jsonl(LONGEVITY_RECOVERIES)
    eq(len(recoveries_rows), 41, "Longevity frozen: recoveries.jsonl row count")
    eq(len(recoveries_rows), summary["recoveries"],
       "Longevity: recoveries.jsonl row count matches summary.recoveries")
    require(all(r["kind"] == "designed" for r in recoveries_rows),
            "Longevity: every recovery row is the harness's own designed restart cycle")
    n_sigabrt = sum(1 for r in recoveries_rows if r["returncode"] == -6)
    n_exit137 = sum(1 for r in recoveries_rows if r["returncode"] == 137)
    eq(n_sigabrt, 23, "Longevity frozen: SIGABRT (-6) recovery count")
    eq(n_exit137, 18, "Longevity frozen: os._exit(137) recovery count")
    eq(n_sigabrt + n_exit137, len(recoveries_rows),
       "Longevity: every recovery row is one of exactly these two returncodes")
    unexpected = summary["unexpected_writer_deaths"]
    eq(unexpected, 0, "Longevity frozen: unexpected_writer_deaths")

    # --- reader deaths: both the D-086 torn-tail race ---
    reader_rows = load_jsonl(LONGEVITY_READER_RESTARTS)
    eq(len(reader_rows), 2, "Longevity frozen: reader_restarts.jsonl row count")
    eq(len(reader_rows), summary["reader_restarts"],
       "Longevity: reader_restarts.jsonl row count matches summary.reader_restarts")
    eq(sorted(r["idx"] for r in reader_rows), [5, 6],
       "Longevity frozen: reader indices that died (5 and 6)")
    require(all(r["returncode"] == 1 for r in reader_rows),
            "Longevity: both reader deaths are returncode 1 (uncaught StateError)")
    if FAILURE_LEDGER.exists():
        ledger_entries = [json.loads(line) for line in
                           FAILURE_LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
        d086 = [e for e in ledger_entries if e.get("id") == "D-086-reader-torn-tail-race"]
        require(len(d086) == 1,
                "Longevity: exactly one D-086-reader-torn-tail-race entry in "
                "ops/failure_ledger.jsonl (cross-check only, not this macro's source)")
        if d086:
            wild = d086[0].get("observed_in_the_wild", "")
            require("reader 6" in wild and "reader 5" in wild,
                    "Longevity: the D-086 ledger entry's observed_in_the_wild note names "
                    "both reader 6 and reader 5 (cross-check only)")

    # --- verify_healthy ---
    require(summary["verify_healthy"] is True,
            "Longevity: summary.verify_healthy is the JSON literal true")

    # --- throughput / p99 drift, first hour vs. last hour ---
    drift = summary["drift"]
    throughput_start = round(drift["throughput_first_hour_avg"], 2)
    throughput_end = round(drift["throughput_last_hour_avg"], 2)
    eq(throughput_start, 24.03, "Longevity frozen: first-hour throughput, commits/s")
    eq(throughput_end, 16.41, "Longevity frozen: last-hour throughput, commits/s")

    p99_start = round(drift["commit_p99_first_hour_max"], 1)
    p99_end = round(drift["commit_p99_last_hour_max"], 1)
    eq(p99_start, 485.6, "Longevity frozen: first-hour commit p99, ms")
    eq(p99_end, 3740.5, "Longevity frozen: last-hour commit p99, ms")

    # --- metadata growth slopes ---
    growth = summary["metadata_growth_slope_bytes_per_s"]
    manifest_growth = round(growth["manifests"], 1)
    segment_growth = round(growth["segments"], 1)
    eq(manifest_growth, -3.4, "Longevity frozen: manifest-bytes growth slope, B/s")
    eq(segment_growth, 1341.1, "Longevity frozen: segment-bytes growth slope, B/s")

    # --- compactions ---
    compactions = summary["compactions"]
    eq(compactions, 2_415, "Longevity frozen: summary.compactions")

    # --- digest_equal / replay skip, cross-checked across the log, the
    #     ledger, the manifest, and the harness's own projection formula ---
    require(summary["digest_equal"] is None,
            "Longevity: summary.digest_equal is JSON null (\"not computed\"), never False")
    eq(summary["replay_skipped_reason"], "projected_replay_exceeds_limit",
       "Longevity frozen: summary.replay_skipped_reason")
    report_text = LONGEVITY_GATE_E_REPORT.read_text(encoding="utf-8")
    require("digest_equal=False" in report_text,
            "Longevity: gate_e_report.md's own digest_equal=False text confirms scripts/"
            "longevity_report.py's None->False display coercion (a known reporting "
            "artifact -- the manifest's own null is this macro's source, not this report)")

    log_text = LONGEVITY_ORCHESTRATOR_LOG.read_text(encoding="utf-8")
    log_match = re.search(
        r"disk_guard_replay_skip: total_batches=(\d+), projected_mb=([\d.]+)", log_text)
    require(log_match is not None,
            "Longevity: orchestrator.log carries a disk_guard_replay_skip line")
    log_batches = int(log_match.group(1))
    log_projected_mb = float(log_match.group(2))
    eq(log_batches, total_batches,
       "Longevity: orchestrator.log's disk_guard_replay_skip total_batches matches "
       "summary.total_batches")

    ledger_rows = load_jsonl(LONGEVITY_LEDGER)
    eq(len(ledger_rows), 1, "Longevity frozen: longevity_ledger.jsonl row count")
    ledger_event = ledger_rows[0]
    eq(ledger_event["event"], "disk_guard_replay_skip",
       "Longevity: the ledger's one row is the disk_guard_replay_skip event")
    eq(ledger_event["total_batches"], total_batches,
       "Longevity: ledger's total_batches matches summary.total_batches")
    close(ledger_event["projected_mb"], log_projected_mb, 1.0,
          "Longevity: ledger's projected_mb matches the orchestrator log line")

    # Recompute the projection from the harness's own documented formula
    # (scripts/longevity_run.py::cmd_run, quoted in README.md: projected_mb
    # = 243.0 * total_batches**2 / 1e6) rather than trusting either copy of
    # the number verbatim.
    recomputed_projected_mb = 243.0 * (total_batches ** 2) / 1e6
    close(recomputed_projected_mb, log_projected_mb, 1.0,
          "Longevity: 243.0*total_batches**2/1e6 matches the orchestrator log's "
          "projected_mb (D-149's own O(batches^2) formula, quoted in README.md)")
    projected_tb = round(recomputed_projected_mb / 1e6, 1)
    eq(projected_tb, 280.8, "Longevity frozen: replay projection, TB")

    # --- emit macros ---
    m.add("osdiSoakHours", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST)}: config.duration_s / 3600")
    m.add("osdiSoakCommit", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST)}: git_commit")
    m.add("osdiSoakEntitiesStart", tex_num(entities_start),
          f"{relpath(LONGEVITY_MANIFEST)}: config.store_name / dataset.name "
          "(\"synth-1m-native\") -- the store's own \"1m\" label, not a counted field; "
          "final_stats.n_entities confirms growth past this nominal start")
    m.add("osdiSoakEntitiesEnd", tex_num(entities_end),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.final_stats.n_entities")
    m.add("osdiSoakBatches", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.total_batches")
    m.add("osdiSoakWriterLives", tex_num(by_life["lives"]),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: lives (== len(per_life_errors))")
    m.add("osdiSoakRecoveries", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES)}: row count, == summary.recoveries")
    m.add("osdiSoakRecoveriesSigabrt", tex_num(n_sigabrt),
          f"{relpath(LONGEVITY_RECOVERIES)}: rows with returncode == -6 (SIGABRT)")
    m.add("osdiSoakRecoveriesExit137", tex_num(n_exit137),
          f"{relpath(LONGEVITY_RECOVERIES)}: rows with returncode == 137 (os._exit(137))")
    m.add("osdiSoakUnexpectedRecoveries", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.unexpected_writer_deaths -- all 41 "
          "recoveries are the harness's own designed restart cycle (kind==\"designed\")")
    m.add("osdiSoakReaderDeaths", tex_num(len(reader_rows)),
          f"{relpath(LONGEVITY_READER_RESTARTS)}: row count, == summary.reader_restarts")
    m.add("osdiSoakReaderDeathCause", "reader torn-tail race, pre-fix engine",
          f"{relpath(LONGEVITY_READER_RESTARTS)}: both rows (readers 6, 5) are the "
          "D-086-reader-torn-tail-race StateError shape; ops/failure_ledger.jsonl's D-086 "
          "entry (cross-checked when present) confirms both instances against commit "
          "886805f, which predates the fix series (ef97d2d, 43f6ef4, 3a664a8)")
    m.add("osdiSoakVerifyHealthy", "true",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.verify_healthy")
    m.add("osdiSoakWriterErrorsManifest", tex_num(manifest_error_count),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.error_count -- the harness's own "
          "counter_latest label-collision defect (last writer life only, see README.md); "
          "not a run total")
    m.add("osdiSoakWriterErrorsTrue", tex_num(true_total_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: sum(per_life_errors), 42 lives")
    m.add("osdiSoakThroughputStart", f"{throughput_start:.2f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.throughput_first_hour_avg, commits/s")
    m.add("osdiSoakThroughputEnd", f"{throughput_end:.2f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.throughput_last_hour_avg, commits/s")
    m.add("osdiSoakP99StartMs", f"{p99_start:.1f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.commit_p99_first_hour_max, ms")
    m.add("osdiSoakP99EndMs", f"{p99_end:,.1f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.commit_p99_last_hour_max, ms")
    m.add("osdiSoakManifestGrowthBps", f"{manifest_growth:.1f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.metadata_growth_slope_bytes_per_s.manifests")
    m.add("osdiSoakSegmentGrowthBps", f"{segment_growth:,.1f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.metadata_growth_slope_bytes_per_s.segments")
    m.add("osdiSoakDigestStatus", "not computed",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.digest_equal is JSON null -- the final "
          "replay/digest-equivalence step never ran (disk guard skip), never \"computed "
          "and found False\" (text macro, not a number)")
    m.add("osdiSoakReplayProjectedTB", f"{projected_tb:.1f}",
          f"{relpath(LONGEVITY_ORCHESTRATOR_LOG)}: disk_guard_replay_skip line's "
          "projected_mb / 1e6, cross-checked against longevity_ledger.jsonl and "
          "recomputed from 243.0*total_batches**2/1e6 (scripts/longevity_run.py's own "
          "D-149 projection formula, quoted in README.md)")
    m.add("osdiSoakCompactions", tex_num(compactions),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.compactions")


# --------------------------------------------------------------------------
# pending stubs (records not yet landed)
# --------------------------------------------------------------------------

def add_pending_stubs(m: Macros) -> None:
    # The DAG-phase (v1/v2/v3, all 40/40 cells) and the R-18 probe (5/5
    # batches) are both fully landed and scored -- see compute_c7_dag and
    # compute_c7_r18 above. What remains pending is the main correction-
    # storm cell grid itself (addendum-1's 36-cell grid,
    # storm-campaign-2026-09.json): 12/36 cells complete, the other 24
    # blocked on an iTiger disk-quota incident (2026-09-14, see
    # benchmarks/storm-v1/README.md and SUBMISSION_NOTE.txt on iTiger) --
    # not yet scored or committed as a merged record.
    m.add_pending("osdiTtfSpeedup", "C7 (storm-v1 time-to-fresh)",
                  "main grid 12/36, blocked on cluster quota")
    m.add_pending("osdiStormCells", "C7 (storm-v1 time-to-fresh)",
                  "main grid 12/36, blocked on cluster quota")
    m.add_pending("osdiStormFalseFresh", "C7 (storm-v1 time-to-fresh)",
                  "main grid 12/36, blocked on cluster quota")
    m.add_pending("osdiStormSpeedupN1k", "C7 (storm-v1 time-to-fresh)",
                  "the N=1,000 c1 seed-0 cell (211319_0) is not a merged main-grid record on "
                  "main yet -- its numbers appear only in benchmarks/storm-v1/README.md's R-18 "
                  "section table, which this generator does not treat as a record source; "
                  "main grid 12/36, blocked on cluster quota")
    m.add_pending("osdiStormAvoidedN1k", "C7 (storm-v1 time-to-fresh)",
                  "same as osdiStormSpeedupN1k -- the N=1,000 c1 seed-0 cell has not landed as "
                  "a committed main-grid record; main grid 12/36, blocked on cluster quota")

    m.add_pending("osdiLdbcExpressible", "C9 (LDBC generality, four axes)",
                  "benchmarks/ldbc-fit-v1/classification.json exists but the independent-"
                  "validation axis (Neo4j reference run, Lane E3-ldbc) has not landed")
    m.add_pending("osdiLdbcExecuted", "C9 (LDBC generality, four axes)",
                  "same as osdiLdbcExpressible -- the Neo4j reference run is pending")
    m.add_pending("osdiLdbcValidated", "C9 (LDBC generality, four axes)",
                  "same as osdiLdbcExpressible -- 0/41 pending the Neo4j reference run")

    m.add_pending("osdiLiveDays", "C10 (live OSV workload)",
                  "live-osv/ is running on xzgpu but the days-of-operation count is not yet "
                  "in a committed record")
    m.add_pending("osdiLiveAdvisories", "C10 (live OSV workload)",
                  "only the bootstrap count (32,787) is documented in prose "
                  "(LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md); no committed record snapshot exists")
    m.add_pending("osdiLiveCorrections", "C10 (live OSV workload)",
                  "the live-correction count is not yet in a committed record")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if output would change")
    args = ap.parse_args()

    m = Macros()
    compute_c1(m)
    compute_c3(m)
    compute_b1_v2(m)
    _void_b1_v2_treatment_provenance(m)
    compute_b1_v2e(m)
    compute_b1_co7(m)
    compute_c4(m)
    compute_c5(m)
    compute_c6(m)
    compute_c8(m)
    compute_c7_dag(m)
    compute_c7_r18(m)
    compute_d160(m)
    compute_d160_llm_direct_fix(m)
    compute_c2(m)
    compute_ladder(m)
    compute_longevity_soak(m)
    add_pending_stubs(m)

    if FAILURES:
        print(f"VERIFICATION FAILED after {CHECKS} checks:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    out_path = OUT_DIR / "osdi-macros.tex"
    text = m.render()
    old = out_path.read_text(encoding="utf-8") if out_path.exists() else None
    changed = old != text
    if changed and not args.check:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    if args.check and changed:
        print(f"stale generated file: {out_path.name}", file=sys.stderr)
        return 1

    landed = sum(1 for _, v, _ in m.items if not v.startswith("\\errmessage"))
    pending = len(m.items) - landed
    print(f"osdi_paper_macros: {landed} landed macros, {pending} pending stubs, "
          f"{CHECKS} verifications, all passed.")
    print(f"  {'up to date' if args.check else 'wrote'}: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
