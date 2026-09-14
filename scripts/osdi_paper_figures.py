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
