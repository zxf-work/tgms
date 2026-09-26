#!/usr/bin/env python
"""Generate the OSDI paper's figures and tables for claims with landed data.

Companion to ``scripts/osdi_paper_macros.py`` (read that file's docstring
first -- the "assert, do not trust" discipline and the record inventory
are shared). This script covers the figure/table plan's landed-data
subset (``docs/design/OSDI27_PAPER_SKELETON_2026-09-15.md`` S4, gitignored
and not read by this script -- every deliverable below documents its own
record path):

  1. crash campaign per-boundary table            (C1)
  2. reader scaling: quiescent vs live writer,
     aggregate + per-query, with a VmHWM panel    (C5)
  3. manifest bytes, control vs treatment,
     log scale, with the commit-phase decile
     ratios                                       (C3)
  4. version_history 1M/10M, control vs treatment (C4)
  5. fault-matrix outcome counts, pre vs post
     D-160                                        (C8)
  6. M4/M5 false-fresh + precision +
     avoided-recomputation table                  (C6)
  7. correction-storm DAG phase v1/v2/v3:
     false-safe cells + extra visits per seed      (C7, partial)
  8. R-18 probe crossover, N=10,000 c1 seed 0:
     per-batch check/lookup/global cost + ttf p50;
     the N=1,000 point now reads its speedup from
     the landed 36/36 main correction-load grid    (C7, partial)
  9. corruption-detection matrix: class x mutation
     detection rate, pre vs post A10                (C2)
 10. overhead ladder: per rung, per plan/op medians
     with the freeze's predicted band shaded         (D4/D4b)
 11. B7 scale curve: per-operator query p50 (log
     scale) at 10M/30M/100M, refused operators at
     100M (and reach.window's admission at each
     scale) flagged rather than fabricated           (B7)
 12. B7 scale costs: build wall/VmHWM, query-ready
     floor VmHWM, and cadence-labelled recovery wall
     across 1M/10M/30M/100M -- points the source
     record does not carry (e.g. no query-ready-floor
     figure at 1M/10M, no recovery record at 10M) are
     gaps, never estimated                            (B7)

Every deliverable is emitted as a CSV (the underlying data table, always,
independent of matplotlib) and, when matplotlib is importable in the
running interpreter, as a PDF and a PNG. The CSV and the numbers going
into the figure come from the same data-building function, so a figure
can never show a number its CSV disagrees with.

Output goes to ``paper/osdi/generated/`` (gitignored, not committed --
see ``scripts/osdi_paper_macros.py``'s docstring for the convention).

Usage:  $HOME/.venvs/tgms/bin/python scripts/osdi_paper_figures.py [--check]

``--check`` regenerates the CSVs into memory and fails if they would
differ from what is on disk; it does not re-render PDFs/PNGs (matplotlib
rendering is not guaranteed byte-identical across matplotlib versions,
so the CSV -- not the raster/vector output -- is the checked artifact).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "osdi" / "generated"

CRASH_V1 = ROOT / "benchmarks" / "crash-v1" / "eval-crash-campaign-2026-09-13.json"
B1_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-ab-2026-09-raw.json"
B2_RAW = ROOT / "benchmarks" / "results-v1" / "b2-version-history-ab-2026-09-raw.json"
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
DAG_V2 = STORM_V1 / "storm-campaign-dag-v2-2026-09.json"
DAG_V3 = STORM_V1 / "storm-campaign-dag-v3-2026-09.json"
R18_PROBE_ROWS = STORM_V1 / "storm-r18-probe-2026-09-rows.jsonl"
# the now-complete 36/36 main correction-load grid (see
# scripts/osdi_paper_macros.py's compute_c7_storm_v1, osdiStormV1SpeedupN1kSeed0)
# -- read here for the N=1,000 point f8's title used to wait on.
STORM_V1_MAIN_GRID_ROWS = STORM_V1 / "storm-v1-main-grid-2026-09-15-rows.jsonl"

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

SCALE_V1 = ROOT / "benchmarks" / "scale-v1"
SCALE_10M = SCALE_V1 / "itiger-calib-10m.json"
SCALE_30M = SCALE_V1 / "scale-curve-30m.json"
SCALE_100M = SCALE_V1 / "scale-curve-100m.json"

SCALE_CALIB_1M = SCALE_V1 / "itiger-calib-1m.json"
SCALE_CALIB_10M = SCALE_V1 / "itiger-calib-10m.json"
SCALE_BUILD_30M = SCALE_V1 / "build-30m.json"
SCALE_BUILD_100M = SCALE_V1 / "build-100m.json"
SCALE_QUERYFLOOR_30M = SCALE_V1 / "queryfloor-30m.json"
SCALE_QUERYFLOOR_100M = SCALE_V1 / "queryfloor-100m.json"
SCALE_RECOVERY_30M_CE500 = SCALE_V1 / "recovery-30m.json"
SCALE_RECOVERY_30M_CE5000 = SCALE_V1 / "recovery-30m-ce5000.json"
SCALE_RECOVERY_100M_CE5000 = SCALE_V1 / "recovery-100m-ce5000.json"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MPL = True
except ModuleNotFoundError:
    HAVE_MPL = False

# A dark/light-agnostic print style: an explicit white figure/axes
# background (never transparent -- a transparent PNG would pick up
# whatever background the viewer composites it against) with black ink,
# so the same file reads correctly printed, embedded in a light-mode PDF
# viewer, or previewed in a dark-mode file browser. Two-tone (control vs
# treatment / pre vs post) uses a solid vs hatched fill rather than a
# color pair, so the figures survive grayscale printing.
STYLE = {
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "text.color": "black",
    "axes.edgecolor": "black",
    "axes.labelcolor": "black",
    "xtick.color": "black",
    "ytick.color": "black",
    "font.size": 9,
}
CONTROL_STYLE = {"color": "0.55", "hatch": ""}
TREATMENT_STYLE = {"color": "0.15", "hatch": "///"}

# No creation/modification timestamp in the emitted PDFs -- the only
# non-deterministic byte matplotlib's pdf backend embeds by default.
PDF_METADATA = {"CreationDate": None, "ModDate": None}


def _savefig(fig, stem: Path) -> None:
    fig.savefig(stem.with_suffix(".pdf"), metadata=PDF_METADATA)
    fig.savefig(stem.with_suffix(".png"), dpi=200, metadata={})
    plt.close(fig)


def _require_mpl() -> None:
    if not HAVE_MPL:
        raise SystemExit(
            "matplotlib is required to render figures and is not installed in this "
            "interpreter. Run with the project venv "
            "($HOME/.venvs/tgms/bin/python scripts/osdi_paper_figures.py); CSVs can "
            "still be generated without it via write_all_csv()."
        )


def write_csv(path: Path, header: list[str], rows: list[list]) -> str:
    """Write `rows` under `header` to `path` and return the text written."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    text = buf.getvalue()
    path.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------
# 1. crash campaign per-boundary table (C1)
# --------------------------------------------------------------------------

def build_crash_data() -> dict:
    d = json.loads(CRASH_V1.read_text(encoding="utf-8"))
    results = d["results"]
    per_boundary = d["per_boundary"]
    rows = []
    for name in sorted(per_boundary):
        trials = [r for r in results if r["boundary"] == name]
        assert len(trials) == per_boundary[name]["trials"]
        rows.append({
            "boundary": name,
            "trials": len(trials),
            "problems": sum(1 for r in trials if r["problems"]),
            "failed_q1": sum(1 for r in trials if not r["q1_acked_survive"]),
            "failed_q2": sum(1 for r in trials if not r["q2_deterministic"]),
            "failed_q3": sum(1 for r in trials if not r["q3_single_generation"]),
            "failed_q4": sum(1 for r in trials if not r["q4_orphans_reclaimed"]),
        })
    total = {
        "boundary": "total",
        "trials": sum(r["trials"] for r in rows),
        "problems": sum(r["problems"] for r in rows),
        "failed_q1": sum(r["failed_q1"] for r in rows),
        "failed_q2": sum(r["failed_q2"] for r in rows),
        "failed_q3": sum(r["failed_q3"] for r in rows),
        "failed_q4": sum(r["failed_q4"] for r in rows),
    }
    return {"rows": rows, "total": total, "commit": d["git_commit"]}


def write_crash_csv(data: dict) -> str:
    header = ["boundary", "trials", "problems", "failed_q1", "failed_q2", "failed_q3", "failed_q4"]
    rows = [[r[c] for c in header] for r in data["rows"]]
    rows.append([data["total"][c] for c in header])
    return write_csv(OUT_DIR / "t1_crash_per_boundary.csv", header, rows)


def plot_crash(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(6.5, 3.0))
        names = [r["boundary"] for r in data["rows"]]
        trials = [r["trials"] for r in data["rows"]]
        ax.bar(range(len(names)), trials, color="0.3", edgecolor="black")
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, rotation=45, ha="right", fontsize=6)
        ax.set_ylabel("trials (0 problems in every one)")
        ax.set_title(f"Crash campaign: {data['total']['trials']:,} trials, "
                      f"{data['total']['problems']} problems, commit {data['commit']}")
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "t1_crash_per_boundary")


# --------------------------------------------------------------------------
# 2. reader scaling: quiescent vs live writer, aggregate + per-query,
#    with a VmHWM panel (C5)
# --------------------------------------------------------------------------

def build_readers_data() -> dict:
    d = json.loads(READERS_10M.read_text(encoding="utf-8"))
    quiescent = {q["readers"]: q for q in d["quiescent"]}
    readers = sorted(quiescent)
    agg_qps = [quiescent[r]["aggregate_qps"] for r in readers]
    vmhwm_gib = [quiescent[r]["vmhwm_kb"]["median"] / 1024 / 1024 for r in readers]

    mixed = {(mm["readers"], mm["writer"]): mm for mm in d["mixed"]}
    mixed_readers = sorted({r for r, _ in mixed})
    query_kinds = sorted(next(iter(mixed.values()))["per_query_p50_ms"])
    per_query = {}
    for qk in query_kinds:
        no_writer = []
        with_writer = []
        for r in mixed_readers:
            no_writer.append(statistics.median(mixed[(r, False)]["per_query_p50_ms"][qk]))
            with_writer.append(statistics.median(mixed[(r, True)]["per_query_p50_ms"][qk]))
        per_query[qk] = {"no_writer": no_writer, "with_writer": with_writer}

    writer_cost = {w["readers"]: w["cost_pct"] for w in d["writer_cost"]}
    commit_p50 = {}
    for mm in d["mixed"]:
        if mm["writer"] and mm["commit_p50_ms_per_trial"]:
            commit_p50[mm["readers"]] = statistics.median(mm["commit_p50_ms_per_trial"])

    return {
        "readers": readers,
        "agg_qps": agg_qps,
        "vmhwm_gib": vmhwm_gib,
        "mixed_readers": mixed_readers,
        "query_kinds": query_kinds,
        "per_query": per_query,
        "writer_cost_pct": [writer_cost.get(r) for r in mixed_readers],
        "commit_p50_ms": [commit_p50.get(r) for r in mixed_readers],
        "commit": d["git_commit"],
    }


def write_readers_csv(data: dict) -> str:
    header = (["readers", "aggregate_qps_quiescent", "vmhwm_gib_median"]
               + [f"p50_{qk}_no_writer_ms" for qk in data["query_kinds"]]
               + [f"p50_{qk}_with_writer_ms" for qk in data["query_kinds"]]
               + ["writer_cost_pct_of_throughput", "writer_commit_p50_ms"])
    quiescent_by_readers = dict(zip(data["readers"], zip(data["agg_qps"], data["vmhwm_gib"])))
    rows = []
    for i, r in enumerate(data["mixed_readers"]):
        agg_qps, vmhwm_gib = quiescent_by_readers.get(r, ("", ""))
        row = [r, agg_qps, vmhwm_gib]
        row += [round(data["per_query"][qk]["no_writer"][i], 3) for qk in data["query_kinds"]]
        row += [round(data["per_query"][qk]["with_writer"][i], 3) for qk in data["query_kinds"]]
        row += [data["writer_cost_pct"][i], data["commit_p50_ms"][i]]
        rows.append(row)
    return write_csv(OUT_DIR / "f4_reader_scaling.csv", header, rows)


def plot_readers(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.2))

        ax = axes[0]
        ax.plot(data["readers"], data["agg_qps"], marker="o", color="black")
        ax.set_xlabel("concurrent readers")
        ax.set_ylabel("aggregate q/s (quiescent)")
        ax.set_xscale("log", base=2)

        ax = axes[1]
        ax.plot(data["readers"], data["vmhwm_gib"], marker="s", color="black")
        ax.set_xlabel("concurrent readers")
        ax.set_ylabel("per-reader VmHWM, GiB")
        ax.set_xscale("log", base=2)
        ax.set_ylim(0, max(data["vmhwm_gib"]) * 1.3)

        ax = axes[2]
        x = range(len(data["mixed_readers"]))
        width = 0.35
        no_w = data["per_query"]["series.count"]["no_writer"]
        w_w = data["per_query"]["series.count"]["with_writer"]
        ax.bar([i - width / 2 for i in x], no_w, width, label="quiescent", **CONTROL_STYLE,
               edgecolor="black")
        ax.bar([i + width / 2 for i in x], w_w, width, label="with writer", **TREATMENT_STYLE,
               edgecolor="black")
        ax.set_xticks(list(x))
        ax.set_xticklabels(data["mixed_readers"])
        ax.set_xlabel("concurrent readers")
        ax.set_ylabel("series.count p50, ms")
        ax.legend(fontsize=7)

        fig.suptitle(f"Reader scaling at 10M, quiescent vs live writer (commit {data['commit']})",
                     fontsize=9)
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f4_reader_scaling")


# --------------------------------------------------------------------------
# 3. manifest bytes, control vs treatment, log scale, with decile ratios (C3)
# --------------------------------------------------------------------------

def build_manifest_data() -> dict:
    raw = json.loads(B1_RAW.read_text(encoding="utf-8"))
    b1a = raw["b1a"]
    b1b = raw["b1b"]["summary"]
    return {
        "bytes_ctl": b1a["control"]["manifest_bytes_at_stop"],
        "bytes_trt": b1a["treatment"]["manifest_bytes_at_stop"],
        "manifest_us_ctl": b1b["control"]["manifest_us"],
        "manifest_us_trt": b1b["treatment"]["manifest_us"],
        "total_us_ctl": b1b["control"]["total_us"],
        "total_us_trt": b1b["treatment"]["total_us"],
    }


def write_manifest_csv(data: dict) -> str:
    header = ["arm", "manifest_bytes_at_2.5M_ops", "manifest_us_first_decile",
              "manifest_us_last_decile", "manifest_us_ratio", "total_us_first_decile",
              "total_us_last_decile", "total_us_ratio"]
    rows = []
    for arm, bytes_key, m_key, t_key in (
        ("control", "bytes_ctl", "manifest_us_ctl", "total_us_ctl"),
        ("treatment", "bytes_trt", "manifest_us_trt", "total_us_trt"),
    ):
        m, t = data[m_key], data[t_key]
        rows.append([arm, data[bytes_key], m["first_decile_us"], m["last_decile_us"],
                     m["ratio_last_over_first"], t["first_decile_us"], t["last_decile_us"],
                     t["ratio_last_over_first"]])
    return write_csv(OUT_DIR / "f3_manifest_bytes.csv", header, rows)


def plot_manifest(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.2))

        ax = axes[0]
        arms = ["control\n(format 1)", "treatment\n(format 2)"]
        vals = [data["bytes_ctl"], data["bytes_trt"]]
        bars = ax.bar(arms, vals, color=["0.55", "0.15"], edgecolor="black",
                       hatch=["", "///"])
        ax.set_yscale("log")
        ax.set_ylabel("manifest bytes at 2.5M ops (log scale)")
        ratio = data["bytes_ctl"] / data["bytes_trt"]
        ax.set_title(f"{ratio:.0f}x reduction")
        for bar, v in zip(bars, vals):
            ax.annotate(f"{v:,}", (bar.get_x() + bar.get_width() / 2, v),
                        ha="center", va="bottom", fontsize=6, rotation=0)

        ax = axes[1]
        labels = ["manifest-write\n(control)", "manifest-write\n(treatment)",
                  "engine-commit\n(control)", "engine-commit\n(treatment)"]
        ratios = [data["manifest_us_ctl"]["ratio_last_over_first"],
                  data["manifest_us_trt"]["ratio_last_over_first"],
                  data["total_us_ctl"]["ratio_last_over_first"],
                  data["total_us_trt"]["ratio_last_over_first"]]
        colors = ["0.55", "0.15", "0.55", "0.15"]
        hatches = ["", "///", "", "///"]
        ax.bar(labels, ratios, color=colors, edgecolor="black", hatch=hatches)
        ax.axhline(1.2, color="black", linestyle="--", linewidth=1)
        ax.annotate("F2 bar (<=1.2x)", (0, 1.2), fontsize=6, va="bottom")
        ax.set_ylabel("last/first decile ratio, 300 commits")
        ax.tick_params(axis="x", labelsize=6)

        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f3_manifest_bytes")


# --------------------------------------------------------------------------
# 4. version_history 1M/10M, control vs treatment (C4)
# --------------------------------------------------------------------------

def build_vh_data() -> dict:
    raw = json.loads(B2_RAW.read_text(encoding="utf-8"))
    vh = raw["version_history"]["raw"]

    def med(key, field):
        return statistics.median(r[field] for r in vh[key])

    rows = []
    for scale, ctl_key, trt_key in (("1M", "control_1m", "treatment_1m"),
                                     ("10M", "control_10m", "treatment_10m")):
        rows.append({
            "scale": scale,
            "wall_ms_ctl": med(ctl_key, "median_ms"),
            "wall_ms_trt": med(trt_key, "median_ms"),
            "vmhwm_kb_ctl": med(ctl_key, "vmhwm_kb"),
            "vmhwm_kb_trt": med(trt_key, "vmhwm_kb"),
        })
    return {"rows": rows}


def write_vh_csv(data: dict) -> str:
    header = ["scale", "wall_ms_control", "wall_ms_treatment", "vmhwm_kb_control",
              "vmhwm_kb_treatment"]
    rows = [[r["scale"], r["wall_ms_ctl"], r["wall_ms_trt"], r["vmhwm_kb_ctl"], r["vmhwm_kb_trt"]]
            for r in data["rows"]]
    return write_csv(OUT_DIR / "f_vh_control_treatment.csv", header, rows)


def plot_vh(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.2))
        scales = [r["scale"] for r in data["rows"]]
        x = range(len(scales))
        width = 0.35

        ax = axes[0]
        ax.bar([i - width / 2 for i in x], [r["wall_ms_ctl"] for r in data["rows"]], width,
               label="control", color="0.55", edgecolor="black")
        ax.bar([i + width / 2 for i in x], [r["wall_ms_trt"] for r in data["rows"]], width,
               label="treatment", color="0.15", edgecolor="black", hatch="///")
        ax.set_yscale("log")
        ax.set_xticks(list(x))
        ax.set_xticklabels(scales)
        ax.set_ylabel("version_history wall time, ms (log)")
        ax.legend(fontsize=7)

        ax = axes[1]
        ax.bar([i - width / 2 for i in x], [r["vmhwm_kb_ctl"] / 1024 / 1024 for r in data["rows"]],
               width, label="control", color="0.55", edgecolor="black")
        ax.bar([i + width / 2 for i in x], [r["vmhwm_kb_trt"] / 1024 / 1024 for r in data["rows"]],
               width, label="treatment", color="0.15", edgecolor="black", hatch="///")
        ax.set_yscale("log")
        ax.set_xticks(list(x))
        ax.set_xticklabels(scales)
        ax.set_ylabel("peak VmHWM, GiB (log)")
        ax.legend(fontsize=7)

        fig.suptitle("version_history: bounded vs unbounded, 1M/10M", fontsize=9)
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f_vh_control_treatment")


# --------------------------------------------------------------------------
# 5. fault-matrix outcome counts, pre vs post D-160 (C8)
# --------------------------------------------------------------------------

OUTCOME_KEYS = ["correct", "safe-refusal", "explicit-failure", "silent-violation"]


def _outcome_totals(cells, *, strict: bool | None = None) -> dict:
    tot = dict.fromkeys(OUTCOME_KEYS, 0)
    for c in cells:
        if strict is not None and c["strict_gate"] != strict:
            continue
        for k in OUTCOME_KEYS:
            tot[k] += c["counts"][k]
    return tot


def build_fault_matrix_data() -> dict:
    pre = json.loads(FAULTS_PRE.read_text(encoding="utf-8"))
    post = json.loads(FAULTS_POST.read_text(encoding="utf-8"))
    pre_primary = [c for c in pre["per_cell_gate_table"] if not c["strict_gate"]]
    return {
        "pre": _outcome_totals(pre_primary),
        "post": _outcome_totals(post["per_cell_gate_table"]),
        "trials": post["total_trials"],
    }


def write_fault_matrix_csv(data: dict) -> str:
    header = ["outcome", "pre_d160", "post_d160"]
    rows = [[k, data["pre"][k], data["post"][k]] for k in OUTCOME_KEYS]
    return write_csv(OUT_DIR / "t5_fault_matrix_pre_post.csv", header, rows)


def plot_fault_matrix(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(6.0, 3.2))
        x = range(len(OUTCOME_KEYS))
        width = 0.35
        ax.bar([i - width / 2 for i in x], [data["pre"][k] for k in OUTCOME_KEYS], width,
               label="pre-D-160", color="0.55", edgecolor="black")
        ax.bar([i + width / 2 for i in x], [data["post"][k] for k in OUTCOME_KEYS], width,
               label="post-D-160", color="0.15", edgecolor="black", hatch="///")
        ax.set_xticks(list(x))
        ax.set_xticklabels(OUTCOME_KEYS, rotation=20, ha="right", fontsize=7)
        ax.set_ylabel(f"trials (of {data['trials']:,})")
        ax.legend(fontsize=7)
        ax.set_title("Fault-matrix outcomes, pre vs post D-160")
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "t5_fault_matrix_pre_post")


# --------------------------------------------------------------------------
# 6. M4/M5 false-fresh + precision + avoided-recomputation table (C6)
# --------------------------------------------------------------------------

def build_freshness_table_data() -> dict:
    fresh_full = json.loads(FRESH_FULL.read_text(encoding="utf-8"))
    fresh_fixture = json.loads(FRESH_FIXTURE.read_text(encoding="utf-8"))
    m4_trials = fresh_full["trials"] + fresh_fixture["trials"]
    m4_changed = [t for t in m4_trials if t["changed"]]
    m4_false_fresh = sum(1 for t in m4_changed if t["verdict"] == "fresh")
    m4_rt_false_fresh = sum(1 for t in m4_changed if t.get("rowtouch_verdict") == "fresh")

    carve2 = json.loads(M5_CARVE_TWO.read_text(encoding="utf-8"))
    carve2_rows = carve2["rows"]
    carve2_false_fresh = sum(1 for r in carve2_rows if r["changed"] and r["verdict"] == "fresh")

    prop_total = 0
    prop_avoided = 0
    prop_false_safe = 0
    for path in M5_PROP_TWO:
        d = json.loads(path.read_text(encoding="utf-8"))
        rows = d["rows"]
        prop_total += len(rows)
        prop_avoided += sum(1 for r in rows if r["child_recomputed"] is False)
        prop_false_safe += sum(1 for r in rows if r["false_safe"])

    rows = [
        {"population": "M4 dependency-scope mechanism", "trials": len(m4_changed),
         "false_fresh_or_false_safe": m4_false_fresh, "denominator_note": "changed trials"},
        {"population": "M4 naive row-touch control", "trials": len(m4_changed),
         "false_fresh_or_false_safe": m4_rt_false_fresh, "denominator_note": "changed trials"},
        {"population": "M5 carve-2 (round 2)", "trials": len(carve2_rows),
         "false_fresh_or_false_safe": carve2_false_fresh, "denominator_note": "all trials"},
        {"population": "M5 propagation round 2 (false-safe)", "trials": prop_total,
         "false_fresh_or_false_safe": prop_false_safe, "denominator_note": "decisions"},
    ]
    return {
        "rows": rows,
        "avoided_recomputation": prop_avoided,
        "avoided_denominator": prop_total,
        "avoided_pct": round(1000 * prop_avoided / prop_total) / 10 if prop_total else None,
        "precision": fresh_full["summary"]["precision"],
        "precision_denominator": fresh_full["summary"]["precision_denominator"],
    }


def write_freshness_table_csv(data: dict) -> str:
    header = ["population", "trials", "false_fresh_or_false_safe", "rate_pct", "denominator_note"]
    rows = []
    for r in data["rows"]:
        rate = 100.0 * r["false_fresh_or_false_safe"] / r["trials"] if r["trials"] else None
        rows.append([r["population"], r["trials"], r["false_fresh_or_false_safe"],
                     round(rate, 1) if rate is not None else "", r["denominator_note"]])
    rows.append(["M5 avoided recomputation", data["avoided_denominator"],
                 data["avoided_recomputation"], data["avoided_pct"], "decisions avoided"])
    rows.append(["M4 precision (bitcoinotc+collegemsg)", data["precision_denominator"],
                 round(data["precision"] * data["precision_denominator"]),
                 round(data["precision"] * 100, 1), "true-stale / POSSIBLY_STALE"])
    return write_csv(OUT_DIR / "t3_freshness_summary.csv", header, rows)


def plot_freshness_table(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(7.5, 2.2))
        ax.axis("off")
        cell_text = []
        for r in data["rows"]:
            rate = 100.0 * r["false_fresh_or_false_safe"] / r["trials"] if r["trials"] else 0.0
            cell_text.append([r["population"], f"{r['trials']:,}",
                               f"{r['false_fresh_or_false_safe']}", f"{rate:.1f}%"])
        cell_text.append(["M5 avoided recomputation", f"{data['avoided_denominator']:,}",
                           f"{data['avoided_recomputation']}", f"{data['avoided_pct']:.1f}%"])
        table = ax.table(cellText=cell_text,
                          colLabels=["population", "n", "false-fresh/false-safe", "rate"],
                          loc="center", cellLoc="left")
        table.auto_set_font_size(False)
        table.set_fontsize(7)
        table.scale(1, 1.4)
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "t3_freshness_summary")


# --------------------------------------------------------------------------
# 7. correction-storm DAG phase, v1/v2/v3: false-safe cells + extra visits
#    per seed (C7, partial)
# --------------------------------------------------------------------------

def build_dag_versions_data() -> dict:
    v1 = json.loads(DAG_V1.read_text(encoding="utf-8"))
    v2 = json.loads(DAG_V2.read_text(encoding="utf-8"))
    v3 = json.loads(DAG_V3.read_text(encoding="utf-8"))

    def by_task(per_cell):
        return {c["task_id"]: c for c in per_cell}

    v1c = by_task(v1["per_cell"])

    def extra_visits(d, seed):
        other = by_task(d["per_cell"])
        diffs = {other[tid]["nodes_visited"] - v1c[tid]["nodes_visited"]
                 for tid in v1c if v1c[tid]["seed"] == seed}
        assert len(diffs) == 1, f"nodes_visited delta not constant at seed {seed}"
        return next(iter(diffs))

    rows = []
    for version, d in (("v1", v1), ("v2", v2), ("v3", v3)):
        false_safe_cells = sum(1 for c in d["per_cell"] if c["false_safe_count"] > 0)
        extra_s0 = 0 if version == "v1" else extra_visits(d, 0)
        extra_s1 = 0 if version == "v1" else extra_visits(d, 1)
        rows.append({
            "version": version,
            "cells": len(d["per_cell"]),
            "false_safe_cells": false_safe_cells,
            "extra_visits_seed0_vs_v1": extra_s0,
            "extra_visits_seed1_vs_v1": extra_s1,
            "commit": d["git_commit"],
        })
    return {"rows": rows}


def write_dag_versions_csv(data: dict) -> str:
    header = ["version", "cells", "false_safe_cells", "extra_visits_seed0_vs_v1",
              "extra_visits_seed1_vs_v1", "commit"]
    rows = [[r[c] for c in header] for r in data["rows"]]
    return write_csv(OUT_DIR / "f7_dag_versions.csv", header, rows)


def plot_dag_versions(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.2))
        versions = [r["version"] for r in data["rows"]]
        x = range(len(versions))

        ax = axes[0]
        ax.bar(x, [r["false_safe_cells"] for r in data["rows"]], color="0.3", edgecolor="black")
        ax.set_xticks(list(x))
        ax.set_xticklabels(versions)
        ax.set_ylabel(f"false-safe cells (of {data['rows'][0]['cells']})")
        ax.set_title("DAG phase: registry-wide false-safe, by version")

        ax = axes[1]
        width = 0.35
        ax.bar([i - width / 2 for i in x],
               [r["extra_visits_seed0_vs_v1"] for r in data["rows"]], width,
               label="seed 0", **CONTROL_STYLE, edgecolor="black")
        ax.bar([i + width / 2 for i in x],
               [r["extra_visits_seed1_vs_v1"] for r in data["rows"]], width,
               label="seed 1", **TREATMENT_STYLE, edgecolor="black")
        ax.set_xticks(list(x))
        ax.set_xticklabels(versions)
        ax.set_ylabel("nodes_visited delta vs v1 (uniform per seed)")
        ax.set_title("DAG phase: extra visits vs v1, by version and seed")
        ax.legend(fontsize=7)

        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f7_dag_versions")


# --------------------------------------------------------------------------
# 8. R-18 probe crossover, N=10,000 c1 seed 0 (C7, partial)
# --------------------------------------------------------------------------

def build_r18_crossover_data() -> dict:
    rows = load_jsonl(R18_PROBE_ROWS)
    rows.sort(key=lambda r: r["batch_index"])

    batches = [r["batch_index"] for r in rows]
    check_s_l1 = [r["arms"]["tgms-L1"]["check_wall_ms"] / 1000 for r in rows]
    lookup_ms = [r["lookup_wall_ms"] for r in rows]
    global_s = [r["global_recompute_wall_ms"] / 1000 for r in rows]
    ttf_l1_s = [r["arms"]["tgms-L1"]["ttf_ms"] / 1000 for r in rows]
    ttf_global_s = [r["arms"]["global-recompute"]["ttf_ms"] / 1000 for r in rows]

    # N=1,000's own c1 seed-0 point: the main correction-load grid is now
    # complete (36/36 cells, storm-v1-main-grid-2026-09-15{,-rows.jsonl}),
    # so this reads the same (store=synth-iv-60k, mix=c1, age=None, seed=0)
    # cell scripts/osdi_paper_macros.py's compute_c7_storm_v1 lands as
    # osdiStormV1SpeedupN1kSeed0, computed the same way here: the ratio of
    # that cell's own summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms.
    main_grid_rows = load_jsonl(STORM_V1_MAIN_GRID_ROWS)
    n1000_target = [r for r in main_grid_rows if r["config"]["store"] == "synth-iv-60k"
                    and r["config"]["mix"] == "c1" and r["config"]["age"] is None
                    and r["config"]["seed"] == 0]
    if len(n1000_target) == 1 and n1000_target[0]["config"]["n_artifacts"] == 1000:
        arms = n1000_target[0]["summary"]["arms"]
        n1000_ttf_global_s = arms["global-recompute"]["ttf_p50_ms"] / 1000
        n1000_ttf_l1_s = arms["tgms-L1"]["ttf_p50_ms"] / 1000
        n1000_speedup = n1000_ttf_global_s / n1000_ttf_l1_s
        n1000_status = "measured"
    else:
        # honest fallback: the record's own shape no longer matches what
        # this figure expects (e.g. more/fewer than one matching cell, or
        # a different n_artifacts) -- report that from the record's own
        # fields, never a typed placeholder string.
        n1000_ttf_global_s = n1000_ttf_l1_s = n1000_speedup = None
        n1000_status = (f"unresolved: {len(n1000_target)} matching cells in "
                         f"{STORM_V1_MAIN_GRID_ROWS.name} (expected 1 at n_artifacts=1000)")

    return {
        "batches": batches,
        "check_s_l1": check_s_l1,
        "lookup_ms": lookup_ms,
        "global_s": global_s,
        "ttf_l1_s": ttf_l1_s,
        "ttf_global_s": ttf_global_s,
        "ttf_l1_p50_s": statistics.median(ttf_l1_s),
        "ttf_global_p50_s": statistics.median(ttf_global_s),
        "n1000_ttf_global_s": n1000_ttf_global_s,
        "n1000_ttf_l1_s": n1000_ttf_l1_s,
        "n1000_speedup": n1000_speedup,
        "n1000_status": n1000_status,
    }


def write_r18_crossover_csv(data: dict) -> str:
    header = ["batch_index", "check_seconds_tgms_L1", "lookup_ms", "global_recompute_seconds",
              "ttf_tgms_L1_seconds", "ttf_global_recompute_seconds"]
    rows = [[b, round(c, 3), round(lk, 3), round(g, 3), round(tl, 3), round(tg, 3)]
            for b, c, lk, g, tl, tg in zip(data["batches"], data["check_s_l1"],
                                            data["lookup_ms"], data["global_s"],
                                            data["ttf_l1_s"], data["ttf_global_s"])]
    rows.append(["p50", "", "", "", round(data["ttf_l1_p50_s"], 3),
                 round(data["ttf_global_p50_s"], 3)])
    if data["n1000_speedup"] is not None:
        rows.append(["N=1000 c1 seed0", "", "", "",
                     round(data["n1000_ttf_l1_s"], 3), round(data["n1000_ttf_global_s"], 3)])
    else:
        rows.append(["N=1000 c1 seed0", data["n1000_status"], data["n1000_status"],
                     data["n1000_status"], data["n1000_status"], data["n1000_status"]])
    return write_csv(OUT_DIR / "f8_r18_crossover.csv", header, rows)


def plot_r18_crossover(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(8.5, 3.2))

        ax = axes[0]
        ax.plot(data["batches"], data["check_s_l1"], marker="o", color="0.15",
                label="tgms-L1 check, s")
        ax.plot(data["batches"], data["global_s"], marker="s", color="0.55",
                label="global-recompute, s")
        ax2 = ax.twinx()
        ax2.plot(data["batches"], data["lookup_ms"], marker="^", color="0.35",
                 linestyle="--", label="lookup, ms (right axis)")
        ax.set_xlabel("batch index")
        ax.set_ylabel("seconds")
        ax2.set_ylabel("lookup_wall_ms")
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, fontsize=6, loc="center left")
        ax.set_title("N=10,000 c1 seed 0: per-batch cost")

        ax = axes[1]
        x = [0, 1]
        ax.bar(x, [data["ttf_global_p50_s"], data["ttf_l1_p50_s"]],
               color=["0.55", "0.15"], edgecolor="black", hatch=["", "///"])
        ax.set_xticks(x)
        ax.set_xticklabels(["global-recompute", "tgms-L1"])
        ax.set_ylabel("time-to-fresh p50, s")
        ratio = data["ttf_global_p50_s"] / data["ttf_l1_p50_s"]
        if data["n1000_speedup"] is not None:
            n1000_line = f"N=1,000: speedup {data['n1000_speedup']:.2f}x (main grid 36/36)"
        else:
            n1000_line = f"N=1,000: {data['n1000_status']}"
        ax.set_title(f"N=10,000: speedup {ratio:.2f}x\n{n1000_line}")

        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f8_r18_crossover")


# --------------------------------------------------------------------------
# 9. corruption-detection matrix: class x mutation detection rate, pre vs
#    post A10 (C2)
# --------------------------------------------------------------------------

def build_corruption_matrix_data() -> dict:
    pre = json.loads(CORRUPTION_PRE.read_text(encoding="utf-8"))
    post = json.loads(CORRUPTION_POST.read_text(encoding="utf-8"))

    def matrix(d):
        dm: dict[tuple[str, str], list[int]] = {}
        for r in d["results"]:
            key = (r["class"], r["mutation"])
            cell = dm.setdefault(key, [0, 0])
            cell[0] += 1
            if r["verdict"] == "DETECTED":
                cell[1] += 1
        return dm

    pre_dm, post_dm = matrix(pre), matrix(post)
    classes = sorted({c for c, _ in pre_dm})
    mutations = sorted({mut for _, mut in pre_dm})

    # Not every (class, mutation) pair applies -- e.g. swap_same_class has no
    # meaning for classes with no same-class sibling file to swap with --
    # so a missing cell is a valid population gap, not a data error.
    rows = []
    for c in classes:
        for mut in mutations:
            if (c, mut) not in pre_dm:
                continue
            trials_pre, det_pre = pre_dm[(c, mut)]
            trials_post, det_post = post_dm[(c, mut)]
            rows.append({
                "class": c, "mutation": mut,
                "trials_pre": trials_pre, "detected_pre": det_pre,
                "rate_pre": det_pre / trials_pre,
                "trials_post": trials_post, "detected_post": det_post,
                "rate_post": det_post / trials_post,
            })
    return {"classes": classes, "mutations": mutations, "rows": rows,
            "commit_pre": pre["git_commit"], "commit_post": post["git_commit"]}


def write_corruption_matrix_csv(data: dict) -> str:
    header = ["class", "mutation", "trials_pre", "detected_pre", "detection_rate_pre",
              "trials_post", "detected_post", "detection_rate_post"]
    rows = [[r["class"], r["mutation"], r["trials_pre"], r["detected_pre"],
             round(r["rate_pre"], 4), r["trials_post"], r["detected_post"],
             round(r["rate_post"], 4)] for r in data["rows"]]
    return write_csv(OUT_DIR / "f_corruption_matrix.csv", header, rows)


def plot_corruption_matrix(data: dict) -> None:
    _require_mpl()
    classes, mutations = data["classes"], data["mutations"]
    by_key = {(r["class"], r["mutation"]): r for r in data["rows"]}
    grid_pre = [[by_key[(c, mut)]["rate_pre"] if (c, mut) in by_key else math.nan
                 for mut in mutations] for c in classes]
    grid_post = [[by_key[(c, mut)]["rate_post"] if (c, mut) in by_key else math.nan
                  for mut in mutations] for c in classes]

    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(9.5, 5.0), sharey=True)
        for ax, grid, label, commit in (
            (axes[0], grid_pre, "pre-A10", data["commit_pre"]),
            (axes[1], grid_post, "post-A10", data["commit_post"]),
        ):
            im = ax.imshow(grid, cmap="Greys", vmin=0, vmax=1, aspect="auto")
            ax.set_xticks(range(len(mutations)))
            ax.set_xticklabels(mutations, rotation=45, ha="right", fontsize=6)
            ax.set_title(f"{label} (commit {commit})", fontsize=8)
        axes[0].set_yticks(range(len(classes)))
        axes[0].set_yticklabels(classes, fontsize=6)
        fig.colorbar(im, ax=axes, fraction=0.03, pad=0.02, label="detection rate")
        fig.suptitle("Corruption-detection matrix: class x mutation, pre vs post A10", fontsize=9)
        _savefig(fig, OUT_DIR / "f_corruption_matrix")


# --------------------------------------------------------------------------
# 10. overhead ladder: per rung, per plan/op medians with the freeze's
#     predicted band shaded (D4/D4b)
# --------------------------------------------------------------------------

# The freeze's own numeric predicted/pass_if bands (benchmarks/ladder-v1/campaign.yaml
# predictions.*), reproduced here only as shading anchors for the figure --
# never as a source for any number scripts/osdi_paper_macros.py emits.
LADDER_BANDS = {
    1: (0.8, 1.3),        # rung1_leaf_overhead: predicted
    2: (1, 2000),         # rung2_compiled_vs_kernel: entity_history pass_if
    3: {"one_step": (1500, 30000), "three_step": (4000, 90000)},
    4: (1, 20),           # rung4_verify_ms: predicted
    5: {"one_step": (800, 8000), "three_step": (2000, 20000)},
}


def build_ladder_data() -> dict:
    merged = json.loads(LADDER_MERGED.read_text(encoding="utf-8"))
    s = merged["summary"]

    rung1 = sorted(s["rung1_leaf_overhead"].items(), key=lambda kv: kv[1]["leaf_over_direct_median"])
    rung2 = s["rung2_compiled_vs_kernel"]

    n_steps_by_plan = {plan: v["n_steps"] for plan, v in s["rung5_tokens_tool_calls"].items()}
    plans = sorted(n_steps_by_plan, key=lambda p: (n_steps_by_plan[p], p))

    rung3 = [(p, s["rung3_trace_bytes"][p]["bytes_median"]) for p in plans]
    rung4 = [(p, s["rung4_verify_ms"][p]["p50_ms_median"]) for p in plans]
    rung5 = [(p, s["rung5_tokens_tool_calls"][p]["tokens_total_median"]) for p in plans]

    return {
        "rung1": rung1,
        "rung2_entity": rung2["entity_history"]["compiled_over_kernel_median"],
        "rung2_version": rung2["version_history"]["compiled_over_kernel_median"],
        "plans": plans,
        "n_steps_by_plan": n_steps_by_plan,
        "rung3": rung3,
        "rung4": rung4,
        "rung5": rung5,
        "commit": merged["git_commit"],
    }


def write_ladder_csv(data: dict) -> str:
    header = ["rung", "series", "n_steps", "value", "band_low", "band_high"]
    rows = []
    for op, v in data["rung1"]:
        lo, hi = LADDER_BANDS[1]
        rows.append([1, op, "", round(v["leaf_over_direct_median"], 4), lo, hi])
    lo2, hi2 = LADDER_BANDS[2]
    rows.append([2, "entity_history", "", round(data["rung2_entity"], 4), lo2, hi2])
    rows.append([2, "version_history", "", round(data["rung2_version"], 4), "", ""])
    for plan, bytes_med in data["rung3"]:
        n = data["n_steps_by_plan"][plan]
        band = LADDER_BANDS[3]["one_step"] if n == 1 else LADDER_BANDS[3]["three_step"]
        rows.append([3, plan, n, bytes_med, band[0], band[1]])
    for plan, ms_med in data["rung4"]:
        n = data["n_steps_by_plan"][plan]
        lo4, hi4 = LADDER_BANDS[4]
        rows.append([4, plan, n, round(ms_med, 4), lo4, hi4])
    for plan, tok_med in data["rung5"]:
        n = data["n_steps_by_plan"][plan]
        band = LADDER_BANDS[5]["one_step"] if n == 1 else LADDER_BANDS[5]["three_step"]
        rows.append([5, plan, n, tok_med, band[0], band[1]])
    return write_csv(OUT_DIR / "f_overhead_ladder.csv", header, rows)


def _plot_banded_bars(ax, labels, values, band, *, log=False, rotation=45):
    x = range(len(labels))
    ax.bar(x, values, color="0.3", edgecolor="black")
    if band is not None:
        ax.axhspan(band[0], band[1], color="0.6", alpha=0.25, zorder=0,
                   label=f"predicted [{band[0]}, {band[1]}]")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=rotation, ha="right", fontsize=6)
    if log:
        ax.set_yscale("log")


def plot_ladder(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(2, 3, figsize=(12.0, 7.0))

        ax = axes[0][0]
        ops = [op for op, _ in data["rung1"]]
        vals = [v["leaf_over_direct_median"] for _, v in data["rung1"]]
        _plot_banded_bars(ax, ops, vals, LADDER_BANDS[1])
        ax.set_ylabel("leaf_over_direct (median)")
        ax.set_title("Rung 1: leaf_overhead, per op")
        ax.legend(fontsize=6)

        ax = axes[0][1]
        _plot_banded_bars(ax, ["entity_history", "version_history"],
                           [data["rung2_entity"], data["rung2_version"]], LADDER_BANDS[2],
                           log=True, rotation=20)
        ax.set_ylabel("compiled_over_kernel (median, log)")
        ax.set_title("Rung 2: compiled_vs_kernel")
        ax.legend(fontsize=6)

        ax = axes[0][2]
        plans = [p for p, _ in data["rung3"]]
        vals = [v for _, v in data["rung3"]]
        one_step_n = sum(1 for p in plans if data["n_steps_by_plan"][p] == 1)
        _plot_banded_bars(ax, plans, vals, None, log=True)
        lo1, hi1 = LADDER_BANDS[3]["one_step"]
        lo3, hi3 = LADDER_BANDS[3]["three_step"]
        ax.axhspan(lo1, hi1, xmin=0, xmax=one_step_n / len(plans), color="0.6", alpha=0.25)
        ax.axhspan(lo3, hi3, xmin=one_step_n / len(plans), xmax=1, color="0.4", alpha=0.25)
        ax.set_ylabel("trace bytes (median, log)")
        ax.set_title("Rung 3: trace_bytes, per plan")

        ax = axes[1][0]
        plans4 = [p for p, _ in data["rung4"]]
        vals4 = [v for _, v in data["rung4"]]
        _plot_banded_bars(ax, plans4, vals4, LADDER_BANDS[4])
        ax.set_ylabel("verify p50, ms")
        ax.set_title("Rung 4: verify_ms, per plan")
        ax.legend(fontsize=6)

        ax = axes[1][1]
        plans5 = [p for p, _ in data["rung5"]]
        vals5 = [v for _, v in data["rung5"]]
        one_step_n5 = sum(1 for p in plans5 if data["n_steps_by_plan"][p] == 1)
        _plot_banded_bars(ax, plans5, vals5, None)
        lo1, hi1 = LADDER_BANDS[5]["one_step"]
        lo3, hi3 = LADDER_BANDS[5]["three_step"]
        ax.axhspan(lo1, hi1, xmin=0, xmax=one_step_n5 / len(plans5), color="0.6", alpha=0.25)
        ax.axhspan(lo3, hi3, xmin=one_step_n5 / len(plans5), xmax=1, color="0.4", alpha=0.25)
        ax.set_ylabel("tokens.total (median)")
        ax.set_title("Rung 5: tokens_tool_calls, per plan")

        axes[1][2].axis("off")

        fig.suptitle(f"Overhead ladder, run of record (commit {data['commit']}), "
                      "shaded = freeze's predicted band", fontsize=9)
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f_overhead_ladder")


# --------------------------------------------------------------------------
# 11. B7 scale curve: per-operator query p50 at 10M/30M/100M, refused
#     operators at 100M flagged rather than fabricated (B7)
# --------------------------------------------------------------------------

def _scale_reach_admission(rec: dict) -> dict | None:
    """Pull a `reach.window` admission decision out of a scale-curve
    record's top-level ``reach_window_admission`` (present at 30M/100M;
    absent at 10M, where the same operator's story lives instead in
    ``scale_curve.reach_window_deviation`` -- a differently shaped record
    documenting that it unexpectedly *executed* there rather than being
    refused). 30M carries a clean/contended pair (both agree: admitted);
    100M carries a single ``run``. Either way, return one
    ``{"admitted": bool, "time_est_ms": int}`` from whichever sub-record
    is present.
    """
    admission = rec.get("reach_window_admission")
    if admission is None:
        return None
    run = admission.get("run") or admission.get("clean_213174") or admission.get("contended_213069")
    return {"admitted": run["admitted"], "time_est_ms": run["estimate"]["time_est_ms"]}


def build_scale_curve_data() -> dict:
    calib_10m = json.loads(SCALE_10M.read_text(encoding="utf-8"))
    sc_10m = calib_10m["scale_curve"]["per_operator_p50_ms"]
    rec_30m = json.loads(SCALE_30M.read_text(encoding="utf-8"))
    sc_30m = rec_30m["per_operator_p50_ms"]
    rec_100m = json.loads(SCALE_100M.read_text(encoding="utf-8"))
    sc_100m = rec_100m["per_operator_p50_ms"]

    operators = sorted(sc_10m)
    assert operators == sorted(sc_30m) == sorted(sc_100m), "operator registry differs across scales"

    reach_30m = _scale_reach_admission(rec_30m)
    reach_100m = _scale_reach_admission(rec_100m)

    rows = []
    for op in operators:
        # 10M (itiger-calib-10m.json): a flat record layout --
        # {"error", "ok", "p50_ms", "p95_ms", "rows"}. Every operator here
        # executed (ok=True); `reach.window` executing at all is itself a
        # deviation (scale_curve.reach_window_deviation), not a refusal,
        # so it is carried through like any other executed op at 10M.
        e10 = sc_10m[op]
        assert e10["ok"] and e10["p50_ms"] is not None, f"{op} unexpectedly refused at 10M"
        rows.append({"operator": op, "scale": "10M", "p50_ms": e10["p50_ms"],
                     "refused": False, "time_est_ms": None, "bar_ms": None})

        # 30M (scale-curve-30m.json, the clean rerun): a different record
        # layout again -- no bare "p50_ms"/"ok"; instead a clean/contended
        # pair plus the forecast's "bar_ms". Per this deliverable, use the
        # clean run's figure. No 30M operator refuses (reach.window is
        # admitted here too, confirmed below).
        e30 = sc_30m[op]
        assert "clean_213174_p50_ms" in e30, f"{op} missing the clean rerun figure at 30M"
        if op == "reach.window":
            assert reach_30m is not None and reach_30m["admitted"], "reach.window not admitted at 30M"
        rows.append({"operator": op, "scale": "30M", "p50_ms": e30["clean_213174_p50_ms"],
                     "refused": False, "time_est_ms": None, "bar_ms": e30["bar_ms"]})

        # 100M (scale-curve-100m.json): a third record layout --
        # "measured_p50_ms" (None when refused) plus "bar_ms" (None only
        # for reach.window, whose bar is undefined once its own admission
        # estimate exceeds the ceiling) and an "error" string for every
        # refused op. Never fabricate a p50 for a refused op: only
        # reach.window carries a numeric time_est_ms anywhere in the
        # record (reach_window_admission); the other six refused ops
        # carry only the CostError string, so their time_est_ms stays
        # empty here (per benchmarks/scale-v1/README.md: "the record
        # carries only a CostError string ... nothing is estimated in
        # its place").
        e100 = sc_100m[op]
        refused = e100["measured_p50_ms"] is None
        time_est_ms = None
        if op == "reach.window":
            assert refused and reach_100m is not None and not reach_100m["admitted"], (
                "reach.window expected refused (not admitted) at 100M")
            time_est_ms = reach_100m["time_est_ms"]
        rows.append({"operator": op, "scale": "100M", "p50_ms": e100["measured_p50_ms"],
                     "refused": refused, "time_est_ms": time_est_ms, "bar_ms": e100["bar_ms"]})

    refused_100m = sorted(r["operator"] for r in rows if r["scale"] == "100M" and r["refused"])
    return {"operators": operators, "rows": rows, "refused_100m": refused_100m,
            "commit_10m": calib_10m["git_commit"], "commit_30m": rec_30m["git_commit"],
            "commit_100m": rec_100m["git_commit"]}


def write_scale_curve_csv(data: dict) -> str:
    header = ["operator", "scale", "p50_ms", "refused", "time_est_ms", "bar_ms"]
    rows = []
    for r in data["rows"]:
        rows.append([
            r["operator"], r["scale"],
            "" if r["p50_ms"] is None else r["p50_ms"],
            r["refused"],
            "" if r["time_est_ms"] is None else r["time_est_ms"],
            "" if r["bar_ms"] is None else r["bar_ms"],
        ])
    return write_csv(OUT_DIR / "f_b7_scale_curve.csv", header, rows)


def plot_scale_curve(data: dict) -> None:
    _require_mpl()
    scales = ["10M", "30M", "100M"]
    x = [10, 30, 100]  # million entities -- a log-scale x axis
    by_op = {op: {r["scale"]: r for r in data["rows"] if r["operator"] == op}
             for op in data["operators"]}
    executed_p50s = [r["p50_ms"] for r in data["rows"] if r["p50_ms"] is not None]
    refused_y = max(executed_p50s) * 1.6  # a distinct marker pinned above the real data

    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(8.5, 5.0))
        cmap = plt.get_cmap("tab20", len(data["operators"]))

        for i, op in enumerate(data["operators"]):
            color = cmap(i)
            series = by_op[op]
            xs = [xv for scale, xv in zip(scales, x) if series[scale]["p50_ms"] is not None]
            ys = [series[scale]["p50_ms"] for scale in scales if series[scale]["p50_ms"] is not None]
            ax.plot(xs, ys, marker="o", markersize=4, linewidth=1, color=color, label=op)

            for scale, xv in zip(scales, x):
                r = series[scale]
                # Faint per-operator forecast bar, where the record carries
                # one, as a reference tick only -- never plotted as data.
                if r["bar_ms"] is not None:
                    ax.plot(xv, r["bar_ms"], marker="_", color=color, alpha=0.35,
                            markersize=14, markeredgewidth=2, zorder=1)
                # Refused operators pinned at the top of the axis, distinct
                # from any executed p50.
                if r["refused"]:
                    ax.plot(xv, refused_y, marker="x", color=color, markersize=8,
                            markeredgewidth=2, zorder=3)

        ax.axhline(refused_y, color="0.7", linestyle=":", linewidth=1, zorder=0)
        ax.set_xscale("log")
        ax.set_xticks(x)
        ax.set_xticklabels(scales)
        ax.set_yscale("log")
        ax.set_xlim(8, 130)
        ax.set_xlabel("scale (entities)")
        ax.set_ylabel("query p50, ms (log scale); x = refused")
        ax.set_title("B7 scale curve: per-operator p50 at 10M / 30M / 100M", fontsize=8)
        # Provenance (the three source records' commits) lives in the
        # caption, not the title -- a title-line commit-hash string used to
        # overprint the plot title above.
        fig.text(0.01, 0.01,
                  f"commits: 10M {data['commit_10m']} / 30M {data['commit_30m']} / "
                  f"100M {data['commit_100m']}",
                  fontsize=5, ha="left", va="bottom")

        refused_labels = []
        for op in data["operators"]:
            r100 = by_op[op]["100M"]
            if r100["refused"]:
                if r100["time_est_ms"] is not None:
                    refused_labels.append(f"{op}: refused (time_est_ms {r100['time_est_ms']})")
                else:
                    refused_labels.append(f"{op}: refused")
        ax.annotate("100M refusals:\n" + "\n".join(refused_labels), xy=(1.02, 0.5),
                    xycoords="axes fraction", fontsize=5.5, va="center", ha="left")

        ax.legend(fontsize=5.5, loc="upper left", bbox_to_anchor=(1.02, 1.0))
        fig.tight_layout()
        _savefig(fig, OUT_DIR / "f_b7_scale_curve")


# --------------------------------------------------------------------------
# 12. B7 scale costs: build wall/VmHWM, query-ready floor VmHWM, and
#     cadence-labelled recovery wall, across 1M/10M/30M/100M (B7)
# --------------------------------------------------------------------------

# campaign convention throughout benchmarks/scale-v1/README.md: "GB" for a
# VmHWM figure means vmhwm_kb / 1,000,000, not true decimal GB or GiB (see
# the README's own "Unit note on 'GB'" -- verified there against the 10M
# anchor). Reused here so this figure's GB column matches the README's own
# 6.81/19.67 prose exactly.
_KB_PER_CAMPAIGN_GB = 1_000_000


def build_scale_costs_data() -> dict:
    """One row per (scale, quantity) actually present in a landed record.
    A quantity a given scale's record does not carry (no query-ready-floor
    figure at 1M/10M in itiger-calib-*.json; no recovery record at all in
    itiger-calib-10m.json) is simply not emitted -- a gap in the panel, not
    an estimated or interpolated point.
    """
    calib_1m = json.loads(SCALE_CALIB_1M.read_text(encoding="utf-8"))
    calib_10m = json.loads(SCALE_CALIB_10M.read_text(encoding="utf-8"))
    build_30m = json.loads(SCALE_BUILD_30M.read_text(encoding="utf-8"))
    build_100m = json.loads(SCALE_BUILD_100M.read_text(encoding="utf-8"))
    qf_30m = json.loads(SCALE_QUERYFLOOR_30M.read_text(encoding="utf-8"))
    qf_100m = json.loads(SCALE_QUERYFLOOR_100M.read_text(encoding="utf-8"))
    rec_30m_ce500 = json.loads(SCALE_RECOVERY_30M_CE500.read_text(encoding="utf-8"))
    rec_30m_ce5000 = json.loads(SCALE_RECOVERY_30M_CE5000.read_text(encoding="utf-8"))
    rec_100m_ce5000 = json.loads(SCALE_RECOVERY_100M_CE5000.read_text(encoding="utf-8"))

    rows = []

    def build_wall(scale, rec, source):
        rows.append({"scale": scale, "quantity": "build_wall_s",
                     "value": rec["build_info"]["wall_s"], "unit": "s",
                     "source_file": source, "note": ""})

    def build_vmhwm(scale, rec, source):
        vmhwm_kb = rec["build_info"]["peak_rss"]["vmhwm"]
        rows.append({"scale": scale, "quantity": "build_vmhwm_gb",
                     "value": round(vmhwm_kb / _KB_PER_CAMPAIGN_GB, 6), "unit": "GB",
                     "source_file": source,
                     "note": "campaign convention: vmhwm_kb / 1e6, see README's unit note"})

    build_wall("1M", calib_1m, "itiger-calib-1m.json")
    build_vmhwm("1M", calib_1m, "itiger-calib-1m.json")
    # itiger-calib-1m.json's own "recovery" sub-record is the frozen
    # --compact-every 500 protocol (its own invocation string names the
    # cadence); no query-ready-floor figure exists in this record.
    rows.append({"scale": "1M", "quantity": "recovery_wall_s_ce500",
                 "value": calib_1m["recovery"]["wall_s"], "unit": "s",
                 "source_file": "itiger-calib-1m.json",
                 "note": "cadence 500 (frozen protocol), digest_equal="
                         f"{calib_1m['recovery']['digest_equal']}"})

    build_wall("10M", calib_10m, "itiger-calib-10m.json")
    build_vmhwm("10M", calib_10m, "itiger-calib-10m.json")
    # No query-ready-floor figure and no recovery sub-record at all in
    # itiger-calib-10m.json -- both left as gaps, not estimated.

    build_wall("30M", build_30m, "build-30m.json")
    build_vmhwm("30M", build_30m, "build-30m.json")
    rows.append({"scale": "30M", "quantity": "query_ready_floor_vmhwm_gb",
                 "value": round(qf_30m["vmhwm_kb"] / _KB_PER_CAMPAIGN_GB, 6), "unit": "GB",
                 "source_file": "queryfloor-30m.json",
                 "note": f"n_ok={qf_30m['n_ok']}/{qf_30m['n_queries']}, job 213173 (clean, "
                         "read_only=True fix)"})
    rows.append({"scale": "30M", "quantity": "recovery_wall_s_ce500",
                 "value": rec_30m_ce500["replay_wall_s"], "unit": "s",
                 "source_file": "recovery-30m.json",
                 "note": "cadence 500 (frozen protocol), digest_equal="
                         f"{rec_30m_ce500['digest_compare']['digest_equal']}"})
    rows.append({"scale": "30M", "quantity": "recovery_wall_s_ce5000",
                 "value": rec_30m_ce5000["replay_wall_s"], "unit": "s",
                 "source_file": "recovery-30m-ce5000.json",
                 "note": "cadence 5000 (Addendum 6), digest_equal="
                         f"{rec_30m_ce5000['digest_compare']['digest_equal']}"})

    build_wall("100M", build_100m, "build-100m.json")
    build_vmhwm("100M", build_100m, "build-100m.json")
    rows.append({"scale": "100M", "quantity": "query_ready_floor_vmhwm_gb",
                 "value": round(qf_100m["vmhwm_kb"] / _KB_PER_CAMPAIGN_GB, 6), "unit": "GB",
                 "source_file": "queryfloor-100m.json",
                 "note": f"n_ok={qf_100m['n_ok']}/{qf_100m['n_queries']}, job 213189"})
    # No 500-cadence 100M recovery run exists or was ever planned
    # (Addendum 6 ruled it infeasible, ~45h extrapolated) -- ce5000 is the
    # campaign's only 100M recovery data point.
    rows.append({"scale": "100M", "quantity": "recovery_wall_s_ce5000",
                 "value": rec_100m_ce5000["replay_wall_s"], "unit": "s",
                 "source_file": "recovery-100m-ce5000.json",
                 "note": "cadence 5000 (only 100M recovery point; no ce500 100M run exists), "
                         "digest_equal="
                         f"{rec_100m_ce5000['digest_compare']['digest_equal']}"})

    return {"rows": rows}


def write_scale_costs_csv(data: dict) -> str:
    header = ["scale", "quantity", "value", "unit", "source_file", "note"]
    rows = [[r["scale"], r["quantity"], r["value"], r["unit"], r["source_file"], r["note"]]
            for r in data["rows"]]
    return write_csv(OUT_DIR / "f11_b7_scale_costs.csv", header, rows)


_SCALE_COSTS_X = {"1M": 1, "10M": 10, "30M": 30, "100M": 100}


def plot_scale_costs(data: dict) -> None:
    _require_mpl()
    by_quantity: dict[str, list[tuple[int, float, dict]]] = {}
    for r in data["rows"]:
        by_quantity.setdefault(r["quantity"], []).append(
            (_SCALE_COSTS_X[r["scale"]], r["value"], r))

    # Short labels: at this panel's 0.58-0.64\textwidth placement (~2in per
    # subplot after the 2x2 split) a longer label like the former
    # "query-ready floor VmHWM, GB" runs off the canvas and "recovery wall,
    # s (cadence labelled)" overlaps its neighbor's "build VmHWM, GB" -- the
    # cadence detail lives in the legend instead (see below), so the axis
    # label does not need to carry it.
    panels = [
        ("build_wall_s", "build wall (s)"),
        ("build_vmhwm_gb", "VmHWM (GB)"),
        ("query_ready_floor_vmhwm_gb", "floor VmHWM (GB)"),
        ("recovery", "recovery (s)"),
    ]

    # This figure alone is placed at ~0.58-0.64\textwidth in the manuscript
    # (the other figures in DELIVERABLES run at or near full column width),
    # which shrank the default STYLE's 9pt tick labels down to an unreadable
    # ~5pt. Fix: halve the figure's own physical size (so the same target
    # width covers proportionally less of it, i.e. everything renders
    # bigger relative to that width), pin tick/axis-label sizes explicitly
    # (>=7pt at the rendered size) instead of inheriting STYLE's font.size,
    # and use constrained_layout instead of tight_layout so the 2x2 grid's
    # axis labels, tick labels, and suptitle are all accounted for together
    # rather than a single post-hoc padding pass -- this panel's own style,
    # not a change to the shared STYLE dict every other figure also uses.
    # No data change.
    scale_costs_style = {
        **STYLE,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "axes.labelsize": 7,
        "legend.fontsize": 6,
    }

    with plt.rc_context(scale_costs_style):
        fig, axes = plt.subplots(2, 2, figsize=(4.25, 3.25), constrained_layout=True)
        for ax, (key, ylabel) in zip(axes.flat, panels):
            if key == "recovery":
                # Two cadences share this panel; the legend's own entries
                # ("cadence 500" / "cadence 5000") carry that distinction,
                # so no per-point annotation is drawn -- at this panel's
                # size, per-point "ce500"/"ce5000" text (even with
                # annotation_clip=True) has nowhere to sit without
                # overlapping the marker next to it or the axes' edge.
                for quantity, marker in (("recovery_wall_s_ce500", "o"),
                                          ("recovery_wall_s_ce5000", "s")):
                    pts = sorted(by_quantity.get(quantity, []))
                    if not pts:
                        continue
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    cadence = "500" if quantity.endswith("ce500") else "5000"
                    ax.plot(xs, ys, marker=marker, color="black", linestyle="--",
                            label=f"cadence {cadence}")
                ax.legend(loc="best", frameon=False, handlelength=1.5,
                          borderaxespad=0.2)
            else:
                pts = sorted(by_quantity.get(key, []))
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                ax.plot(xs, ys, marker="o", color="black")
            ax.set_xscale("log")
            # Every quantity here spans at least one full decade (build
            # wall alone runs ~78s to ~28,000s across 1M-100M) -- a log
            # y-axis throughout, not just for the widest-spanning panel.
            ax.set_yscale("log")
            ax.set_xticks([1, 10, 30, 100])
            ax.set_xticklabels(["1M", "10M", "30M", "100M"])
            ax.set_xlabel("scale (entities)")
            ax.set_ylabel(ylabel)

        fig.suptitle("B7 scale costs: build / query-ready floor / recovery, 1M-100M", fontsize=8)
        _savefig(fig, OUT_DIR / "f11_b7_scale_costs")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

DELIVERABLES = [
    ("crash", build_crash_data, write_crash_csv, plot_crash, "t1_crash_per_boundary.csv"),
    ("readers", build_readers_data, write_readers_csv, plot_readers, "f4_reader_scaling.csv"),
    ("manifest", build_manifest_data, write_manifest_csv, plot_manifest, "f3_manifest_bytes.csv"),
    ("version_history", build_vh_data, write_vh_csv, plot_vh, "f_vh_control_treatment.csv"),
    ("fault_matrix", build_fault_matrix_data, write_fault_matrix_csv, plot_fault_matrix,
     "t5_fault_matrix_pre_post.csv"),
    ("freshness_table", build_freshness_table_data, write_freshness_table_csv,
     plot_freshness_table, "t3_freshness_summary.csv"),
    ("dag_versions", build_dag_versions_data, write_dag_versions_csv, plot_dag_versions,
     "f7_dag_versions.csv"),
    ("r18_crossover", build_r18_crossover_data, write_r18_crossover_csv, plot_r18_crossover,
     "f8_r18_crossover.csv"),
    ("corruption_matrix", build_corruption_matrix_data, write_corruption_matrix_csv,
     plot_corruption_matrix, "f_corruption_matrix.csv"),
    ("overhead_ladder", build_ladder_data, write_ladder_csv, plot_ladder,
     "f_overhead_ladder.csv"),
    ("scale_curve", build_scale_curve_data, write_scale_curve_csv, plot_scale_curve,
     "f_b7_scale_curve.csv"),
    ("scale_costs", build_scale_costs_data, write_scale_costs_csv, plot_scale_costs,
     "f11_b7_scale_costs.csv"),
]
# `csv_filename` (not a precomputed path) so every consumer -- main() below,
# and tests that monkeypatch module-level OUT_DIR -- resolves the path
# against whatever OUT_DIR currently is, rather than the value OUT_DIR held
# when this module was first imported.


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                     help="fail if any CSV would differ from what is on disk")
    ap.add_argument("--csv-only", action="store_true",
                     help="write only CSVs, skip PDF/PNG rendering (no matplotlib required)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    render_figures = not args.check and not args.csv_only and HAVE_MPL
    changed = []
    for name, build, write_csv_fn, plot_fn, csv_filename in DELIVERABLES:
        csv_path = OUT_DIR / csv_filename
        data = build()
        old_text = csv_path.read_text(encoding="utf-8") if csv_path.exists() else None
        new_text = write_csv_fn(data)
        if old_text != new_text:
            changed.append(csv_path.name)
        if render_figures:
            plot_fn(data)

    if args.check and changed:
        print("stale generated CSVs: " + ", ".join(changed), file=sys.stderr)
        return 1

    mode = "checked" if args.check else ("csv" if not render_figures else "csv+pdf+png")
    print(f"osdi_paper_figures: {len(DELIVERABLES)} deliverables ({mode}) -> {OUT_DIR}")
    if not HAVE_MPL and not args.check and not args.csv_only:
        print("  note: matplotlib not installed in this interpreter -- PDF/PNG rendering "
              "was skipped; only CSVs were written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
