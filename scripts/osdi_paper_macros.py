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
  C3  benchmarks/results-v1/b1-manifest-ab-2026-09{,-raw}.json
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
      (`manifest-llm-direct-fix-2026-09-14.json`) has not landed, so
      `osdiD160LlmDirectCoverageFixed` is PENDING.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "osdi" / "generated"

CRASH_V1 = ROOT / "benchmarks" / "crash-v1" / "eval-crash-campaign-2026-09-13.json"

B1_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-ab-2026-09-raw.json"

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
SITE_FACTS = ROOT / "docs" / "site_facts.json"
STABILITY_MD = ROOT / "docs" / "STABILITY.md"


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


def relpath(p: Path) -> str:
    """`p` relative to ROOT for a provenance string, or `p` itself when it
    is not under ROOT (e.g. a tampered copy under a test's tmp_path --
    tests monkeypatch the module-level path constants to such a copy and
    still need a working provenance string, not a ValueError)."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


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
# pending stubs (records not yet landed)
# --------------------------------------------------------------------------

def add_pending_stubs(m: Macros) -> None:
    m.add_pending("osdiCorruptionClasses", "C2 (corruption detection)",
                  "code landed (A2+A6/A4+A5/A8/A3 merges) but no campaign record exists yet; "
                  "see OSDI27_PAPER_SKELETON_2026-09-15.md S3 row C2")
    m.add_pending("osdiCorruptionDetected", "C2 (corruption detection)",
                  "same as osdiCorruptionClasses -- no campaign record yet")

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

    m.add_pending("osdiD160LlmDirectCoverageFixed", "D160-collegemsg (llm_direct tokenizer fix)",
                  "manifest-llm-direct-fix-2026-09-14.json / rows-llm-direct-fix-2026-09-14.json "
                  "have not landed -- job 212231 (real-tokenizer-aware budget, "
                  "tgms/eval/baselines.py commits dab5c2a/8de040f) was running as of the "
                  "benchmarks/d160-collegemsg-v1 commit; this record's own llm_direct coverage "
                  "(osdiD160LlmDirectCarrying, 0/282) stands as first shipped and is not "
                  "silently overwritten by this stub")


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
    compute_c4(m)
    compute_c5(m)
    compute_c6(m)
    compute_c8(m)
    compute_c7_dag(m)
    compute_c7_r18(m)
    compute_d160(m)
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
