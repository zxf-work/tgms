#!/usr/bin/env python
"""Generate the submission campaign's figures and tables for claims with landed data.

Companion to ``scripts/sys_paper_macros.py`` (read that file's docstring
first -- the "assert, do not trust" discipline and the record inventory
are shared). This script covers the figure/table plan's landed-data
subset (an internal design memo (paper skeleton, 2026-09-15) S4, gitignored
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

and the paper figure set (one STIX style block, sizes equal to the column
or text width so each PDF is included at natural size; see PAPER_RC):

 13. f_arcs: five arcs, each measured against a frozen bar, fixed and
     re-measured (double column)
 14. f_scale: memory linear in rows; ingest and recovery compaction-bound
 15. f_ldbc: LDBC SNB SF1, four axes and the reference run's verdicts
 16. f_open_cost (optional): engine open time by phase, format 3 vs 2
 17. f_correction_load: speedup vs N in both rollout states, and why
 18. f_durability: commit protocol with its crash boundaries, and the
     corruption outcome per class
 19. f_external: time per burst against Neo4j and a dataflow view, and
     the withheld-correction outcome (double column; external-v1 records
     read only after their SHA256SUMS check)

Every number in 13-19 comes from a landed macro (asserted, never typed)
or from a sha-gated record field; a pre-registered bar is either a record
field or an entry of FROZEN_BARS, which carries its freeze citation.

Every deliverable is emitted as a CSV (the underlying data table, always,
independent of matplotlib) and, when matplotlib is importable in the
running interpreter, as a PDF and a PNG. The CSV and the numbers going
into the figure come from the same data-building function, so a figure
can never show a number its CSV disagrees with.

Output goes to ``paper/sys/generated/`` (gitignored, not committed --
see ``scripts/sys_paper_macros.py``'s docstring for the convention).

Usage:  $HOME/.venvs/tgms/bin/python scripts/sys_paper_figures.py [--check]

``--check`` regenerates the CSVs into memory and fails if they would
differ from what is on disk; it does not re-render PDFs/PNGs (matplotlib
rendering is not guaranteed byte-identical across matplotlib versions,
so the CSV -- not the raster/vector output -- is the checked artifact).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "sys" / "generated"

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
# scripts/sys_paper_macros.py's own generated output (not a raw record): it
# already applies every sha gate to the records below before landing a
# macro, so reading a value back from here is reading those same
# sha-gated records at one remove, not a typed literal. Used only for the
# sum-mode arc figure (build_r18_crossover_data) below, which needs the
# corrected (sum-mode / fixed end-to-end) speedups that macro file's own
# compute_c7_storm_v1_sum_mode / compute_c7_storm_v2_sum_mode /
# compute_c7_storm_v2_probe_sum_mode / compute_c7_storm_v3_probe /
# compute_c7_storm_v3 already derive from:
#   benchmarks/storm-v1/storm-v1-records-36-tasks.tar.gz
#   benchmarks/storm-v1/storm-r18-probe-2026-09-rows.jsonl
#   benchmarks/storm-v1/storm-v2-records-36-tasks.tar.gz
#   benchmarks/storm-v1/storm-v2-r18-probe-2026-09-15-rows.jsonl
#   benchmarks/storm-v1/storm-v3-main-grid-2026-10-08-rows.jsonl
#   benchmarks/storm-v1/storm-v3-r18-probe-2026-10-08-rows.jsonl
SYS_PAPER_MACROS = OUT_DIR / "sys-paper-macros.tex"

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


_MACRO_HEAD_RE = re.compile(r"\\csname\s+([A-Za-z0-9]+)\\endcsname\{")
_PLAIN_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_SCI_NUMBER_RE = re.compile(r"([+-]?\d+(?:\.(\d+))?)\\times10\^\{?([+-]?\d+)\}?")


def macro_bodies(text: str) -> dict[str, str]:
    """Every ``\\csname <name>\\endcsname{<body>}`` definition in `text`,
    with its whole body: braces are balanced (``13{,}714`` and
    ``1.55\\times 10^{-3}`` keep their inner groups) and a backslash
    escapes the character after it."""
    out: dict[str, str] = {}
    for match in _MACRO_HEAD_RE.finditer(text):
        depth, i = 1, match.end()
        while depth:
            assert i < len(text), f"macro {match.group(1)}: unbalanced braces in its body"
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        out[match.group(1)] = text[match.end():i - 1]
    return out


def _normalize_number_body(body: str) -> str:
    return body.strip().replace("{,}", "").replace("\\,", "").replace(" ", "")


def macro_number(name: str, body: str) -> float:
    """The numeric value of one macro body: ``13{,}714`` -> 13714.0,
    ``1.55\\times 10^{-3}`` -> 0.00155, ``1\\,000`` -> 1000.0. Any other
    body (``not measured``, ``true``, ``D-090``, a PENDING ``\\errmessage``)
    is refused with an assertion that names the macro."""
    s = _normalize_number_body(body)
    sci = _SCI_NUMBER_RE.fullmatch(s)
    if sci:
        return float(f"{sci.group(1)}e{sci.group(3)}")
    assert _PLAIN_NUMBER_RE.fullmatch(s), (
        f"macro {name} is not numeric: {body!r} -- a figure may only plot a landed number")
    return float(s)


def macro_half_unit(name: str, body: str) -> float:
    """Half a unit in the last printed digit of a numeric macro body -- the
    largest gap a figure's own recomputation from the record may show
    against the printed value (``2.035`` -> 5e-4, ``8.51\\times 10^{-3}`` ->
    5e-5)."""
    s = _normalize_number_body(body)
    sci = _SCI_NUMBER_RE.fullmatch(s)
    if sci:
        decimals = len(sci.group(2) or "")
        return 0.5 * 10.0 ** (int(sci.group(3)) - decimals)
    macro_number(name, body)
    decimals = len(s.partition(".")[2])
    return 0.5 * 10.0 ** (-decimals)


def _read_macro_bodies(names: list[str], path: Path | None) -> dict[str, str]:
    path = SYS_PAPER_MACROS if path is None else path
    assert path.exists(), (
        f"{relpath(path)} does not exist -- run "
        "scripts/sys_paper_macros.py before scripts/sys_paper_figures.py"
    )
    defined = macro_bodies(path.read_text(encoding="utf-8"))
    missing = [n for n in names if n not in defined]
    assert not missing, (
        f"{relpath(path)} is missing macro(s) {missing} -- regenerate it "
        "with scripts/sys_paper_macros.py"
    )
    return {n: defined[n] for n in names}


def load_macro_values(names: list[str], path: Path | None = None) -> dict[str, float]:
    """Read the requested macros' own values out of SYS_PAPER_MACROS.

    scripts/sys_paper_macros.py is the only place that touches the raw
    storm-v1/v2/v3 records and their sha gates; this never re-derives a
    number from those records, it only reads back what that script already
    landed, so a figure here can never show a value the macro layer
    disagrees with.
    """
    bodies = _read_macro_bodies(names, path)
    return {n: macro_number(n, b) for n, b in bodies.items()}


def load_macro_texts(names: list[str], path: Path | None = None) -> dict[str, str]:
    """Text macros (identifiers such as ``D-090``, fractions such as
    ``89/710``) as plain strings: ``{,}`` -> ``,`` and ``\\_`` -> ``_``. A
    PENDING macro is refused."""
    bodies = _read_macro_bodies(names, path)
    out = {}
    for n, b in bodies.items():
        assert "\\errmessage" not in b, f"macro {n} is PENDING, not landed"
        out[n] = b.strip().replace("{,}", ",").replace("\\_", "_").replace("\\,", "\u2009")
    return out


def assert_matches_macro(name: str, value: float, path: Path | None = None, *,
                         from_macros: tuple[str, ...] = ()) -> None:
    """Assert, do not trust: a value a figure recomputed from a record
    equals the landed macro to within the macro's printed precision. When
    `value` was itself computed from other (rounded) macros, their printed
    precision is added to the tolerance."""
    body = _read_macro_bodies([name], path)[name]
    target = macro_number(name, body)
    inputs = _read_macro_bodies(list(from_macros), path) if from_macros else {}
    tol = (macro_half_unit(name, body) + sum(macro_half_unit(n, b) for n, b in inputs.items())
           ) * (1 + 1e-9) + 1e-12
    assert abs(value - target) <= tol, (
        f"{name}: figure recomputed {value!r} from the record, macro prints {body!r}")


def relpath(path: Path) -> str:
    """`path` relative to ROOT, or `path` itself when it is outside ROOT
    (a test's tampered copy under tmp_path)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.colors
    import matplotlib.lines
    import matplotlib.patches
    import matplotlib.pyplot as plt
    import matplotlib.text
    import matplotlib.ticker
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
            "($HOME/.venvs/tgms/bin/python scripts/sys_paper_figures.py); CSVs can "
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
# 8. the correction-load arc (skeleton F6a, \ref{fig:arc}): both ends
#    (N=1,000 and the N=10,000 probe) of both rollout states (pre-rollout
#    storm-v1, post-rollout storm-v2/storm-v3), all in sum mode (C7,
#    partial)
# --------------------------------------------------------------------------

# Cut from draft pass 15 (\S\ref{sec:eval:q4}): the committed N=1,000
# point was recStormV1SpeedupN1kSeed0, the check-only end-to-end timer --
# an instrument error (storm-e2e-l1-interval-after-l0-refresh,
# \S\ref{sec:wrong:instruments} row 19), not check_wall_ms + refresh_wall_ms.
# Every point plotted below is the sum-mode (or already-sum/fixed
# end-to-end) sibling instead, read back from scripts/sys_paper_macros.py's
# own macros via load_macro_values -- the generator is the only place that
# opens the raw storm-v1/v2/v3 tarballs/rows.jsonl and checks their sha
# gates, so this never re-derives (or types) a number independently of it.
R18_CROSSOVER_MACROS = [
    # pre-rollout (storm-v1, 3 of 14 operators narrowing): the refutation.
    "recStormV1CrossoverSpeedupSumN1k",
    "recStormV1CrossoverSpeedupSumN10k",
    "recStormV1SpeedupSumGridMin",
    "recStormV1SpeedupSumGridMax",
    # the superseded first-recorded N=1,000 figure, kept only as an
    # annotation (never plotted as a series) so the correction is legible.
    "recStormV1SpeedupN1kSeed0",
    # post-rollout (13 of 14 narrowing), reconstructed on the check-only
    # timer's own sum-mode sibling: storm-v2.
    "recStormV2SpeedupSumN1kSeed0",
    "recStormV2SpeedupSumProbe",
    "recStormV2SpeedupSumGridMin",
    "recStormV2SpeedupSumGridMax",
    # post-rollout, re-measured after the fifth arc's timer fix: storm-v3.
    "recStormV3SpeedupN1kSeed0",
    "recStormV3ProbeSpeedup",
    "recStormV3SpeedupGridMin",
    "recStormV3SpeedupGridMax",
]


def build_r18_crossover_data() -> dict:
    v = load_macro_values(R18_CROSSOVER_MACROS)

    return {
        "n_values": [1_000, 10_000],
        "pre_rollout": {
            "label": "storm-v1, pre-rollout (3/14 narrowing)",
            "speedup_n1k": v["recStormV1CrossoverSpeedupSumN1k"],
            "speedup_n10k": v["recStormV1CrossoverSpeedupSumN10k"],
            "grid_min": v["recStormV1SpeedupSumGridMin"],
            "grid_max": v["recStormV1SpeedupSumGridMax"],
            "source_n1k": "recStormV1CrossoverSpeedupSumN1k",
            "source_n10k": "recStormV1CrossoverSpeedupSumN10k",
        },
        "post_rollout_v2": {
            "label": "storm-v2, post-rollout (13/14, reconstructed)",
            "speedup_n1k": v["recStormV2SpeedupSumN1kSeed0"],
            "speedup_n10k": v["recStormV2SpeedupSumProbe"],
            "grid_min": v["recStormV2SpeedupSumGridMin"],
            "grid_max": v["recStormV2SpeedupSumGridMax"],
            "source_n1k": "recStormV2SpeedupSumN1kSeed0",
            "source_n10k": "recStormV2SpeedupSumProbe",
        },
        "post_rollout_v3": {
            "label": "storm-v3, post-rollout (13/14, fifth-arc re-measurement)",
            "speedup_n1k": v["recStormV3SpeedupN1kSeed0"],
            "speedup_n10k": v["recStormV3ProbeSpeedup"],
            "grid_min": v["recStormV3SpeedupGridMin"],
            "grid_max": v["recStormV3SpeedupGridMax"],
            "source_n1k": "recStormV3SpeedupN1kSeed0",
            "source_n10k": "recStormV3ProbeSpeedup",
        },
        "pre_rollout_first_recorded_n1k": v["recStormV1SpeedupN1kSeed0"],
        "pre_rollout_first_recorded_source": (
            "recStormV1SpeedupN1kSeed0 (superseded: check-only end-to-end timer, "
            "ledger storm-e2e-l1-interval-after-l0-refresh)"
        ),
    }


def write_r18_crossover_csv(data: dict) -> str:
    header = ["campaign", "mode", "n_artifacts", "speedup", "grid_min", "grid_max",
              "source_macro"]
    rows = []
    for key in ("pre_rollout", "post_rollout_v2", "post_rollout_v3"):
        d = data[key]
        n1k, n10k = data["n_values"]
        rows.append([d["label"], "sum", n1k, round(d["speedup_n1k"], 3),
                     round(d["grid_min"], 3), round(d["grid_max"], 3), d["source_n1k"]])
        rows.append([d["label"], "sum", n10k, round(d["speedup_n10k"], 3), "", "",
                     d["source_n10k"]])
    rows.append(["storm-v1, pre-rollout -- superseded", "end-to-end (check-only, bug)",
                 data["n_values"][0], round(data["pre_rollout_first_recorded_n1k"], 3), "", "",
                 data["pre_rollout_first_recorded_source"]])
    return write_csv(OUT_DIR / "f8_r18_crossover.csv", header, rows)


def plot_r18_crossover(data: dict) -> None:
    _require_mpl()
    n1k, n10k = data["n_values"]
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(8.5, 3.6))

        ax = axes[0]
        pre = data["pre_rollout"]
        ax.plot([n1k, n10k], [pre["speedup_n1k"], pre["speedup_n10k"]],
                marker="o", color="0.1", label=pre["label"])
        ax.errorbar([n1k], [pre["speedup_n1k"]],
                     yerr=[[pre["speedup_n1k"] - pre["grid_min"]],
                           [pre["grid_max"] - pre["speedup_n1k"]]],
                     fmt="none", ecolor="0.4", capsize=3,
                     label=f"grid {pre['grid_min']:.2f}-{pre['grid_max']:.2f}x (36 cells)")
        ax.axhline(1.0, linestyle=":", color="0.6", linewidth=1, label="breakeven")
        ax.annotate(f"first recorded {data['pre_rollout_first_recorded_n1k']:.2f}x\n"
                    "(check-only timer, superseded)",
                    xy=(n1k, pre["speedup_n1k"]), xycoords="data",
                    xytext=(n1k, 0.945), textcoords="data",
                    ha="left", fontsize=6, color="0.35",
                    arrowprops={"arrowstyle": "->", "color": "0.5", "linewidth": 0.7})
        ax.set_xscale("log")
        ax.set_xticks([n1k, n10k])
        ax.set_xticklabels([f"$10^{int(math.log10(n1k))}$", f"$10^{int(math.log10(n10k))}$"])
        ax.set_xlabel("N (artifacts)")
        ax.set_ylabel("speedup, global-recompute / tgms-L1 (sum mode)")
        ax.legend(fontsize=6, loc="upper right")
        ax.set_title("Pre-rollout (3/14 narrowing): predicted to grow;\n"
                     f"{pre['speedup_n1k']:.2f}x at $10^3$ to "
                     f"{pre['speedup_n10k']:.2f}x at $10^4$ -- inverted", fontsize=7.5)

        ax = axes[1]
        v2, v3 = data["post_rollout_v2"], data["post_rollout_v3"]
        for d, marker, color, ls in ((v2, "s", "0.5", "--"), (v3, "o", "0.1", "-")):
            ax.plot([n1k, n10k], [d["speedup_n1k"], d["speedup_n10k"]],
                    marker=marker, color=color, linestyle=ls, label=d["label"])
            ax.errorbar([n1k], [d["speedup_n1k"]],
                         yerr=[[d["speedup_n1k"] - d["grid_min"]],
                               [d["grid_max"] - d["speedup_n1k"]]],
                         fmt="none", ecolor=color, capsize=3)
        ax.axhline(1.0, linestyle=":", color="0.6", linewidth=1)
        ax.set_xscale("log")
        ax.set_xticks([n1k, n10k])
        ax.set_xticklabels([f"$10^{int(math.log10(n1k))}$", f"$10^{int(math.log10(n10k))}$"])
        ax.set_xlabel("N (artifacts)")
        ax.legend(fontsize=6, loc="upper right")
        ax.set_title("Post-rollout (13/14 narrowing): above 1 at both\n"
                     "scales, still shrinking with N", fontsize=7.5)

        fig.suptitle("The correction-load arc, both ends measured (sum mode throughout)",
                      fontsize=8.5)
        fig.tight_layout(rect=(0, 0, 1, 0.94))
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
# never as a source for any number scripts/sys_paper_macros.py emits.
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
# Paper figure set (data plots). One style block for every plot below:
# STIX fonts at the caption's 7 pt, figure sizes equal to the column /
# text width so the PDF is included at natural size (never scaled in
# LaTeX), the Okabe-Ito palette with one colour and one marker per
# configuration, and no titles beyond panel tags. When the page geometry
# changes, COL_W / TEXT_W / BASE_PT change here and every figure
# re-renders.
# --------------------------------------------------------------------------

COL_W, TEXT_W = 3.34, 6.94      # in; == \columnwidth, \textwidth of the paper's geometry
BASE_PT = 7.0                   # == caption size (\scriptsize at 10 pt)
SMALL_PT = 6.5                  # ticks, legends, annotations
PAPER_RC = {
    "font.family": "serif", "font.serif": ["STIXGeneral"], "mathtext.fontset": "stix",
    "font.size": BASE_PT, "axes.labelsize": BASE_PT, "axes.titlesize": BASE_PT,  # panel tags only
    "xtick.labelsize": 6.5, "ytick.labelsize": 6.5, "legend.fontsize": 6.5,
    "legend.frameon": False, "legend.handlelength": 1.4, "legend.handletextpad": 0.4,
    "legend.labelspacing": 0.25, "legend.borderaxespad": 0.3, "legend.columnspacing": 0.8,
    "axes.linewidth": 0.5, "axes.spines.top": False, "axes.spines.right": False,
    "axes.labelpad": 2.0, "axes.axisbelow": True, "axes.grid": False,
    "grid.color": "#E6E6E6", "grid.linewidth": 0.4,          # enable per axis: ax.grid(axis="y")
    "xtick.direction": "out", "ytick.direction": "out",
    "xtick.major.size": 2.5, "ytick.major.size": 2.5, "xtick.minor.size": 1.5, "ytick.minor.size": 1.5,
    "xtick.major.width": 0.5, "ytick.major.width": 0.5, "xtick.minor.width": 0.4, "ytick.minor.width": 0.4,
    "xtick.major.pad": 1.5, "ytick.major.pad": 1.5,
    "lines.linewidth": 1.0, "lines.markersize": 3.5, "lines.markeredgewidth": 0.6,
    "errorbar.capsize": 1.5, "patch.linewidth": 0.5, "hatch.linewidth": 0.4,
    "axes.unicode_minus": True, "text.color": "#000000", "axes.edgecolor": "#000000",
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "pdf.fonttype": 42, "ps.fonttype": 42,
    "figure.constrained_layout.use": True,
    "figure.constrained_layout.h_pad": 0.02, "figure.constrained_layout.w_pad": 0.02,
    "savefig.bbox": "standard", "savefig.pad_inches": 0.0, "savefig.dpi": 300,
}

# Okabe-Ito. Colour is reserved for configuration identity, predictions and
# refutations; single-system plots use the grey ramp.
C_INCR = "#0072B2"      # TGMS incremental refresh; "after the fix"; post-rollout
C_GLOBAL = "#56B4E9"    # TGMS global recompute
C_NEO = "#E69F00"       # Neo4j full recompute
C_DD = "#CC79A7"        # differential-dataflow view (watermarked variant: hatched)
C_BEFORE = "#8C8C8C"    # before a fix / pre-rollout / superseded (hollow marker)
C_PRED = "#009E73"      # pre-registered prediction: band, dashed bar, hollow diamond
C_REF = "#D55E00"       # refutation label, false-fresh, "silent", "incremental slower"
GREY_RAMP = ("#000000", "#4D4D4D", "#999999", "#D9D9D9")
C_ANNOT = "#555555"     # in-plot annotations that do not mark a verdict

MARKER_INCR, MARKER_GLOBAL, MARKER_NEO, MARKER_DD, MARKER_PRED = "o", "s", "^", "D", "D"
MS, MS_CELL = 3.5, 2.0              # marker sizes: medians/points, per-cell dots
CELL_ALPHA = 0.45
HOLLOW_MEW = 0.8
REF_LW, REF_DASH = 0.6, (0, (3, 2))  # reference lines: breakeven, frozen bars
ARROW_LW = 0.6
PRED_BAND_ALPHA = 0.15              # a prediction band
PASS_TINT_ALPHA = 0.07              # the passing side of a frozen bar
SLOWER_TINT_ALPHA = 0.06

FIG_SIZES = {               # W x H in inches, == the exact PDF page size
    "f_arcs": (TEXT_W, 1.45),
    "f_scale": (COL_W, 1.65),
    "f_ldbc": (COL_W, 1.35),
    "f_open_cost": (COL_W, 1.20),
    "f_correction_load": (COL_W, 1.75),
    "f_durability": (COL_W, 2.30),
    "f_external": (TEXT_W, 1.65),
}

# Text no figure may carry: host names, campaign codenames, paths, commit
# shas (the caption, not the figure, carries provenance).
_FORBIDDEN_FIGURE_TEXT = re.compile(
    r"xzgpu|itiger|gnomon|jetstream|storm-v\d|\bR-?18\b|\bB7\b|\bA10\b|tgms-L\d|ARC5|"
    r"/mnt|/project|benchmarks/|\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b",
    re.IGNORECASE)


def figure_texts(fig) -> list[str]:
    """Every string a rendered figure carries (titles, labels, tick labels,
    annotations, legend entries)."""
    texts = [t.get_text() for t in fig.findobj(matplotlib.text.Text)]
    return [s for s in texts if s.strip()]


def _assert_clean_figure(fig, stem: str) -> None:
    assert not fig._suptitle, f"{stem}: no suptitle in a paper figure"
    for s in figure_texts(fig):
        bad = _FORBIDDEN_FIGURE_TEXT.search(s)
        assert bad is None, f"{stem}: figure text {s!r} carries {bad.group(0)!r}"
    for ax in fig.axes:
        for title in (ax.get_title("left"), ax.get_title("center"), ax.get_title("right")):
            assert not title or title.startswith("$\\mathbf{("), (
                f"{stem}: axes titles are panel tags only, got {title!r}")


def _savefig_paper(fig, stem: Path) -> None:
    """A Type 42 PDF at exactly the figure's size (no tight bbox: it would
    change the width) plus a 300 dpi PNG preview."""
    _assert_clean_figure(fig, stem.name)
    fig.savefig(stem.with_suffix(".pdf"), metadata=PDF_METADATA)
    fig.savefig(stem.with_suffix(".png"), dpi=300, metadata={})
    plt.close(fig)


def _new_fig(stem: str, **subplot_kw):
    return plt.figure(figsize=FIG_SIZES[stem], **subplot_kw)


def _panel_tag(ax, tag: str, text: str = "") -> None:
    """A bold panel tag plus roman text, left-aligned: the only axes title
    a paper figure carries."""
    label = f"$\\mathbf{{({tag})}}$" + (f" {text}" if text else "")
    ax.set_title(label, loc="left", fontsize=BASE_PT, pad=2.0)


def _fmt_times(value: float, _pos=None) -> str:
    return f"{value:g}×"


def _log2_speedup_axis(ax, lo: float = 0.5, hi: float = 8.0) -> None:
    """Speedups on a log2 axis, ticks 0.5x 1x 2x 4x 8x through one formatter."""
    ax.set_yscale("log", base=2)
    ax.set_ylim(lo, hi)
    ticks = [v for v in (0.5, 1, 2, 4, 8) if lo <= v <= hi]
    ax.yaxis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_times))
    ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())


_HUMAN_TIME_LABELS = {1e-3: "1 ms", 1e-2: "10 ms", 0.1: "100 ms", 1.0: "1 s", 10.0: "10 s",
                      60.0: "1 min", 600.0: "10 min", 3600.0: "1 h", 36000.0: "10 h"}


def _human_time_axis(ax, which: str, ticks_s: list[float]) -> None:
    """A log seconds axis labelled in human time (1 ms ... 1 min, 1 h, 10 h)."""
    axis = ax.yaxis if which == "y" else ax.xaxis
    (ax.set_yscale if which == "y" else ax.set_xscale)("log")
    axis.set_major_locator(matplotlib.ticker.FixedLocator(ticks_s))
    axis.set_major_formatter(matplotlib.ticker.FixedFormatter(
        [_HUMAN_TIME_LABELS[t] for t in ticks_s]))
    axis.set_minor_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0,), numticks=30))
    axis.set_minor_formatter(matplotlib.ticker.NullFormatter())


def _decade_formatter(value: float, _pos=None) -> str:
    exp = int(round(math.log10(value)))
    return f"$10^{{{exp}}}$" if exp not in (0, 1) else f"{value:g}"


def _point(ax, x, y, color, marker, *, hollow=False, ms=MS, zorder=4, **kw):
    if hollow:
        return ax.plot([x], [y], marker=marker, linestyle="none", markersize=ms,
                       markerfacecolor="white", markeredgecolor=color,
                       markeredgewidth=HOLLOW_MEW, zorder=zorder, **kw)
    return ax.plot([x], [y], marker=marker, linestyle="none", markersize=ms,
                   markerfacecolor=color, markeredgecolor=color, zorder=zorder, **kw)


def _arrow(ax, xy_from, xy_to, *, color=C_BEFORE, shrink=3.0) -> None:
    ax.annotate("", xy=xy_to, xytext=xy_from, zorder=3,
                arrowprops={"arrowstyle": "-|>", "color": color, "lw": ARROW_LW,
                            "shrinkA": shrink, "shrinkB": shrink, "mutation_scale": 5})


def _before_after(ax, xb, yb, xa, ya, color, marker) -> None:
    """Before: hollow grey marker; after: the configuration's colour,
    filled; a thin grey arrow joins them."""
    _point(ax, xb, yb, C_BEFORE, marker, hollow=True)
    _point(ax, xa, ya, color, marker)
    _arrow(ax, (xb, yb), (xa, ya))


def _frozen_bar(ax, value, *, orient="h", pass_side, span=(0.0, 1.0), tint=True,
                color=C_PRED) -> None:
    """A frozen (pre-registered) bar: a dashed line, with its passing side
    tinted. `span` is in axes fraction along the other axis; axis limits
    must already be set."""
    if orient == "h":
        ax.axhline(value, span[0], span[1], color=color, lw=REF_LW, ls=REF_DASH, zorder=2)
        if tint:
            lo, hi = ax.get_ylim()
            far = hi if pass_side == "above" else lo
            ax.axhspan(min(value, far), max(value, far), span[0], span[1], color=color,
                       alpha=PASS_TINT_ALPHA, lw=0, zorder=0)
            ax.set_ylim(lo, hi)
    else:
        ax.axvline(value, span[0], span[1], color=color, lw=REF_LW, ls=REF_DASH, zorder=2)
        if tint:
            lo, hi = ax.get_xlim()
            far = hi if pass_side == "above" else lo
            ax.axvspan(min(value, far), max(value, far), span[0], span[1], color=color,
                       alpha=PASS_TINT_ALPHA, lw=0, zorder=0)
            ax.set_xlim(lo, hi)


def _note(ax, x, y, text, *, color=C_ANNOT, **kw):
    kw.setdefault("fontsize", SMALL_PT)
    return ax.text(x, y, text, color=color, **kw)


def _fix_label(ax, text: str) -> None:
    """The fix a before/after panel re-measures, named in italic under its x-axis."""
    ax.set_xlabel(text, fontsize=SMALL_PT, fontstyle="italic", labelpad=1.5)


def _thousands(value: float) -> str:
    return f"{value:,.0f}"


FIGURE_CSV_HEADER = ["panel", "series", "x", "value", "unit", "source"]


def _write_figure_csv(stem: str, rows: list[dict]) -> str:
    return write_csv(OUT_DIR / f"{stem}.csv", FIGURE_CSV_HEADER,
                     [[r[c] for c in FIGURE_CSV_HEADER] for r in rows])


def _row(panel, series, x, value, unit, source) -> dict:
    return {"panel": panel, "series": series, "x": x, "value": value, "unit": unit,
            "source": source}


# --------------------------------------------------------------------------
# Frozen (pre-registered) bars. Thresholds are typed by design: each one
# carries its freeze citation, and where the freeze is a public file the
# exact clause is asserted to be present in it, so a typed value cannot
# drift from what was frozen. Bars a record carries as a field (the soak-2
# writer bound, the scale campaign's bands) are read from the record
# instead and are not in this table; the LDBC gate is a macro.
# --------------------------------------------------------------------------

STORM_CAMPAIGN_YAML = ROOT / "benchmarks" / "storm-v1" / "campaign.yaml"
LONGEVITY_README = ROOT / "benchmarks" / "longevity-v1" / "README.md"

FROZEN_BARS = {
    "P5": {
        "value": 1.0, "unit": "x", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml predictions.P5 (frozen 2026-09-15): speedup > 1.0",
        "source": STORM_CAMPAIGN_YAML, "after": None,
        "clause": "P5: end-to-end time-to-fresh speedup of tgms-L1 over global-recompute > 1.0",
    },
    "P3": {
        "value": 0.95, "unit": "fraction", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml predictions.P3 (frozen 2026-09-15): avoided "
                    "recomputation >= 0.95 at c1",
        "source": STORM_CAMPAIGN_YAML, "after": None,
        "clause": "P3: tgms-L0/L1 avoided recomputation (decision count) >= 0.95 at c1",
    },
    "rollout_survivor_predicted": {
        "value": 0.22, "unit": "fraction", "pass_side": "below",
        "citation": "storm-v1 campaign.yaml addendum_3.predictions.prefilter_survivor_fraction "
                    "(frozen before the post-rollout grid ran)",
        "source": STORM_CAMPAIGN_YAML, "after": "addendum_3:", "clause": "predicted: 0.22 (0.15-0.32)",
    },
    "rollout_precision_predicted": {
        "value": 0.20, "unit": "fraction", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml addendum_3.predictions."
                    "precision_changed_over_survivors",
        "source": STORM_CAMPAIGN_YAML, "after": "addendum_3:", "clause": "predicted: 0.20 (0.14-0.30)",
    },
    "rollout_avoided_predicted": {
        "value": 0.73, "unit": "fraction", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml addendum_3.predictions.avoided_recompute_decision_c1",
        "source": STORM_CAMPAIGN_YAML, "after": "addendum_3:", "clause": "predicted: 0.73 (0.65-0.82)",
    },
    "rollout_avoided_bar": {
        "value": 0.60, "unit": "fraction", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml addendum_3.predictions.avoided_recompute_decision_c1 "
                    "pass_if",
        "source": STORM_CAMPAIGN_YAML, "after": "avoided_recompute_decision_c1:",
        "clause": 'pass_if: ">= 0.60"',
    },
    "rollout_speedup_n1k_predicted": {
        "value": 4.2, "unit": "x", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml addendum_3.predictions.speedup_n1000_seed0",
        "source": STORM_CAMPAIGN_YAML, "after": "addendum_3:", "clause": "predicted: 4.2x (3.0-5.0)",
    },
    "rollout_speedup_n10k_predicted": {
        "value": 2.0, "unit": "x", "pass_side": "above",
        "citation": "storm-v1 campaign.yaml addendum_3.predictions.speedup_n10000_probe",
        "source": STORM_CAMPAIGN_YAML, "after": "addendum_3:", "clause": "predicted: 2.0x (1.1-4.3)",
    },
    "soak2_overlaps_predicted": {
        "value": 0.0, "unit": "findings", "pass_side": "below",
        "citation": "longevity-v1 README, P-SOAK2 pre-registration: full-mode verify reports 0 "
                    "believed-version overlaps",
        "source": LONGEVITY_README, "after": None,
        "clause": "**0 `believed-versions-overlap` findings — the pre-registered prediction",
    },
    "soak4_reader_errors": {
        "value": 0.0, "unit": "errors", "pass_side": "below",
        "citation": "longevity-v1 README, P-SOAK4 prediction (a): 0 reader errors",
        "source": LONGEVITY_README, "after": None,
        "clause": "**(a) 0 ENOENT-class reader errors at any store size",
    },
    "arc5_refresh_ms": {
        "value": 20.0, "unit": "ms", "pass_side": "below",
        "citation": "pre-registration P-ARC5 prediction (2), frozen 2026-10-08T19:34:34Z before "
                    "any fix or re-measurement: refresh (execute + publish) <= 20 ms per artifact",
        "source": None, "after": None, "clause": None,
    },
    "arc5_check_ms": {
        "value": 25.0, "unit": "ms", "pass_side": "below",
        "citation": "pre-registration P-ARC5 prediction (2), frozen 2026-10-08T19:34:34Z: check "
                    "<= 25 ms per artifact",
        "source": None, "after": None, "clause": None,
    },
}


def check_frozen_bars() -> None:
    """Every typed bar whose freeze is a public file is present, verbatim,
    in that file (inside the named block when one is given)."""
    for name, bar in FROZEN_BARS.items():
        if bar["source"] is None:
            continue
        text = bar["source"].read_text(encoding="utf-8")
        if bar["after"] is not None:
            assert bar["after"] in text, f"frozen bar {name}: {bar['after']!r} not in source"
            text = text[text.index(bar["after"]):]
        assert bar["clause"] in text, (
            f"frozen bar {name}: clause {bar['clause']!r} not found in {relpath(bar['source'])}")


def _bar(name: str) -> float:
    return FROZEN_BARS[name]["value"]


def _readme_sha_gate(path: Path, readme: Path) -> None:
    """A record read directly by a figure is sha256-checked against its own
    directory README's sha256 table first."""
    table = dict(re.findall(r"\|\s*`([^`]+)`[^|]*\|\s*`([0-9a-f]{64})`\s*\|",
                            readme.read_text(encoding="utf-8")))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert table.get(path.name) == digest, (
        f"{relpath(path)}: sha256 {digest} does not match {relpath(readme)}'s table")


def _sha256sums_gate(paths: list[Path], sums_file: Path) -> None:
    """Every file is sha256-checked against a ``sha256sum``-style manifest."""
    table = {Path(p).name: d for d, p in re.findall(
        r"^([0-9a-f]{64})\s+(\S+)\s*$", sums_file.read_text(encoding="utf-8"), re.MULTILINE)}
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert table.get(path.name) == digest, (
            f"{relpath(path)}: sha256 {digest} does not match {relpath(sums_file)}")


# --------------------------------------------------------------------------
# Fig. f_arcs: five arcs, each measured against a frozen bar, fixed and
# re-measured (double column, five small panels)
# --------------------------------------------------------------------------

SOAK2_RSS_SLOPES = ROOT / "benchmarks" / "longevity-v1" / "rss_slopes-2.json"

ARCS_MACROS = [
    # (a) incremental refresh vs global recompute, before / after lineage narrowing
    "recStormV1CrossoverSpeedupSumN1k", "recStormV1CrossoverSpeedupSumN10k",
    "recStormV3SpeedupN1kSeed0", "recStormV3ProbeSpeedup",
    # (b) writer RSS growth per life
    "recSoakWriterWithinLifeSlopeMedianKBps", "recSoakWriterWithinLifeSlopeMinKBps",
    "recSoakWriterWithinLifeSlopeMaxKBps",
    "recSoakWriterWithinLifeSlopeMedianKBpsTwo", "recSoakWriterWithinLifeSlopeMinKBpsTwo",
    "recSoakWriterWithinLifeSlopeMaxKBpsTwo",
    "recSoakWriterWithinLifeSlopeMedianKBpsThree", "recSoakWriterWithinLifeSlopeMaxKBpsThree",
    "recSoakWriterWithinLifeSlopeMedianKBpsFour", "recSoakWriterWithinLifeSlopeMaxKBpsFour",
    # (c) overlapping believed versions, full-mode verify
    "recSoakVerifyFullOverlapCount", "recSoakFullVerifyOverlapCountTwo",
    "recSoakFullVerifyOverlapCountThree", "recSoakFullVerifyOverlapCountFour",
    # (d) reader errors
    "recSoakReaderErrorsTrueTwo", "recSoakReaderErrorsTrueThree", "recSoakReaderErrorsTrueFour",
    "recSoakReaderErrorEpisodesThree",
    # (e) per-artifact cost, before / after the fifth arc
    "recExt1ControlArcFivePreArcPublishMsCollegeMsg", "recExt1ControlArcFivePreArcPublishMsSynth",
    "recExt1ControlPreArcPerCheckMsCollegeMsg", "recExt1ControlPreArcPerCheckMsSynth",
    "recExt1ControlArcFivePerArtifactPublishMsCollegeMsg",
    "recExt1ControlArcFivePerArtifactPublishMsSynth",
    "recExt1ControlArcFivePerCheckMsCollegeMsg", "recExt1ControlArcFivePerCheckMsSynth",
]

_SOAKS = ("soak 1", "soak 2", "soak 3", "soak 4")


def build_arcs_data() -> dict:
    check_frozen_bars()
    v = load_macro_values(ARCS_MACROS)
    _readme_sha_gate(SOAK2_RSS_SLOPES, LONGEVITY_README)
    writer_bound = json.loads(SOAK2_RSS_SLOPES.read_text(encoding="utf-8"))[
        "frozen_bound_writer_kb_per_s"]
    rows = []

    def add(panel, series, x, name, unit):
        rows.append(_row(panel, series, x, v[name], unit, name))

    add("a", "N=10^3 before", "before", "recStormV1CrossoverSpeedupSumN1k", "x")
    add("a", "N=10^4 before", "before", "recStormV1CrossoverSpeedupSumN10k", "x")
    add("a", "N=10^3 after", "after", "recStormV3SpeedupN1kSeed0", "x")
    add("a", "N=10^4 after", "after", "recStormV3ProbeSpeedup", "x")
    rows.append(_row("a", "frozen bar", "", _bar("P5"), "x", FROZEN_BARS["P5"]["citation"]))

    for soak, suffix in zip(_SOAKS, ("", "Two", "Three", "Four")):
        add("b", "median", soak, f"recSoakWriterWithinLifeSlopeMedianKBps{suffix}", "kB/s")
        if suffix in ("", "Two"):
            add("b", "min", soak, f"recSoakWriterWithinLifeSlopeMinKBps{suffix}", "kB/s")
        add("b", "max", soak, f"recSoakWriterWithinLifeSlopeMaxKBps{suffix}", "kB/s")
    rows.append(_row("b", "frozen bar", "", writer_bound, "kB/s",
                     f"{relpath(SOAK2_RSS_SLOPES)}: frozen_bound_writer_kb_per_s (P-SOAK2)"))

    add("c", "overlaps", "soak 1", "recSoakVerifyFullOverlapCount", "findings")
    for soak, suffix in zip(_SOAKS[1:], ("Two", "Three", "Four")):
        add("c", "overlaps", soak, f"recSoakFullVerifyOverlapCount{suffix}", "findings")
    rows.append(_row("c", "frozen bar", "", _bar("soak2_overlaps_predicted"), "findings",
                     FROZEN_BARS["soak2_overlaps_predicted"]["citation"]))

    for soak, suffix in zip(_SOAKS[1:], ("Two", "Three", "Four")):
        add("d", "reader errors", soak, f"recSoakReaderErrorsTrue{suffix}", "errors")
    add("d", "error episodes", "soak 3", "recSoakReaderErrorEpisodesThree", "episodes")
    rows.append(_row("d", "frozen bar", "", _bar("soak4_reader_errors"), "errors",
                     FROZEN_BARS["soak4_reader_errors"]["citation"]))

    for store, tok in (("CollegeMsg", "CollegeMsg"), ("synth", "Synth")):
        add("e", f"{store} before", "refresh", f"recExt1ControlArcFivePreArcPublishMs{tok}", "ms")
        add("e", f"{store} before", "check", f"recExt1ControlPreArcPerCheckMs{tok}", "ms")
        add("e", f"{store} after", "refresh",
            f"recExt1ControlArcFivePerArtifactPublishMs{tok}", "ms")
        add("e", f"{store} after", "check", f"recExt1ControlArcFivePerCheckMs{tok}", "ms")
    rows.append(_row("e", "frozen bar", "refresh", _bar("arc5_refresh_ms"), "ms",
                     FROZEN_BARS["arc5_refresh_ms"]["citation"]))
    rows.append(_row("e", "frozen bar", "check", _bar("arc5_check_ms"), "ms",
                     FROZEN_BARS["arc5_check_ms"]["citation"]))
    return {"rows": rows, "v": v, "writer_bound": writer_bound}


def write_arcs_csv(data: dict) -> str:
    return _write_figure_csv("f_arcs", data["rows"])


def plot_arcs(data: dict) -> None:
    _require_mpl()
    v = data["v"]
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_arcs")
        axes = fig.subplots(1, 5, gridspec_kw={"width_ratios": [1.0, 1.15, 1.0, 1.0, 1.0]})

        # (a) incremental refresh vs global recompute
        ax = axes[0]
        _log2_speedup_axis(ax, 0.5, 8)
        ax.set_xlim(-0.55, 1.55)
        _frozen_bar(ax, _bar("P5"), pass_side="above")
        _note(ax, 1.52, _bar("P5") * 0.82, "P5", color=C_PRED, ha="right", va="top")
        pairs = (("recStormV1CrossoverSpeedupSumN1k", "recStormV3SpeedupN1kSeed0", -0.14,
                  "$10^3$", 1.32),
                 ("recStormV1CrossoverSpeedupSumN10k", "recStormV3ProbeSpeedup", 0.14,
                  "$10^4$", 1 / 1.4))
        for before, after, dx, label, yoff in pairs:
            _before_after(ax, 0 + dx, v[before], 1 + dx, v[after], C_INCR, MARKER_INCR)
            _note(ax, 1 + dx, v[after] * yoff, label, ha="center",
                  va="bottom" if yoff > 1 else "top")
        ax.set_xticks([0, 1], ["before", "after"])
        ax.set_ylabel("speedup (×)")
        _fix_label(ax, "lineage narrowing,\n3 $\\rightarrow$ 13 of 14 operators")
        _panel_tag(ax, "a", "refresh vs recompute")

        # (b) writer memory per life
        ax = axes[1]
        ax.set_yscale("log")
        ax.set_ylim(5, 1e4)
        ax.set_xlim(-0.5, 3.5)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_decade_formatter))
        _frozen_bar(ax, data["writer_bound"], pass_side="below")
        _note(ax, 3.45, data["writer_bound"] * 1.12, f"≤ {data['writer_bound']:g} (P-SOAK2)",
              color=C_PRED, ha="right", va="bottom")
        meds = [v["recSoakWriterWithinLifeSlopeMedianKBps" + s] for s in ("", "Two", "Three", "Four")]
        lows = [v["recSoakWriterWithinLifeSlopeMinKBps"], v["recSoakWriterWithinLifeSlopeMinKBpsTwo"],
                meds[2], meds[3]]
        highs = [v["recSoakWriterWithinLifeSlopeMaxKBps" + s] for s in ("", "Two", "Three", "Four")]
        for i, (med, lo, hi) in enumerate(zip(meds, lows, highs)):
            color = C_BEFORE if i == 0 else C_INCR
            ax.errorbar([i], [med], yerr=[[med - lo], [hi - med]], fmt="none", ecolor=color,
                        elinewidth=0.6, capsize=1.5, capthick=0.6, zorder=3)
            _point(ax, i, med, color, MARKER_INCR, hollow=(i == 0))
        _arrow(ax, (0, meds[0]), (1, meds[1]))
        ax.plot([1, 2, 3], meds[1:], color=C_INCR, lw=0.6, zorder=2)
        ax.set_xticks(range(4), ["soak 1", "2", "3", "4"])
        ax.set_ylabel("RSS growth (kB/s)")
        _fix_label(ax, "postings drop unreachable\nsegments (D-087)")
        _panel_tag(ax, "b", "writer memory per life")

        # (c) overlapping believed versions
        ax = axes[2]
        counts = [v["recSoakVerifyFullOverlapCount"]] + [
            v[f"recSoakFullVerifyOverlapCount{s}"] for s in ("Two", "Three", "Four")]
        top = counts[0] * 1.15
        ax.set_ylim(-0.07 * top, top)
        ax.set_xlim(-0.5, 3.5)
        _frozen_bar(ax, _bar("soak2_overlaps_predicted"), pass_side="below", tint=False)
        _note(ax, 3.45, top * 0.04, "predicted 0", color=C_PRED, ha="right", va="bottom")
        _point(ax, 0, counts[0], C_BEFORE, MARKER_INCR, hollow=True)
        for i in (1, 2, 3):
            _point(ax, i, counts[i], C_INCR, MARKER_INCR)
        _arrow(ax, (0, counts[0]), (1, counts[1]))
        _note(ax, 0.18, counts[0], _thousands(counts[0]), ha="left", va="center")
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3, integer=True))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
            lambda y, _p: _thousands(y)))
        ax.set_xticks(range(4), ["soak 1", "2", "3", "4"])
        ax.set_ylabel("full-verify findings")
        _fix_label(ax, "generator fix (D-163)")
        _panel_tag(ax, "c", "overlapping versions")

        # (d) reader errors: log axis with a broken-axis "0" slot
        ax = axes[3]
        zero_y = 10 ** -0.5
        ax.set_yscale("log")
        ax.set_ylim(10 ** -0.9, 10 ** 10.6)
        ax.set_xlim(-0.5, 2.5)
        ax.yaxis.set_major_locator(matplotlib.ticker.FixedLocator([zero_y, 1e1, 1e5, 1e9]))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FixedFormatter(
            ["0", "10", "$10^{5}$", "$10^{9}$"]))
        ax.yaxis.set_minor_locator(matplotlib.ticker.FixedLocator(
            [10.0 ** k for k in range(0, 11)]))
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        brk = {"transform": ax.get_yaxis_transform(), "color": "#000000", "lw": 0.5,
               "clip_on": False, "zorder": 5}
        for yb in (10 ** -0.15, 10 ** 0.05):
            ax.plot([-0.03, 0.03], [yb / 1.25, yb * 1.25], **brk)
        _frozen_bar(ax, zero_y, pass_side="below", tint=False)
        _note(ax, 2.45, zero_y * 2.2, "0 (P-SOAK4)", color=C_PRED, ha="right", va="bottom")
        errs = [v["recSoakReaderErrorsTrueTwo"], v["recSoakReaderErrorsTrueThree"],
                v["recSoakReaderErrorsTrueFour"]]
        ys = [e if e > 0 else zero_y for e in errs]
        _point(ax, 0, ys[0], C_BEFORE, MARKER_INCR, hollow=True)
        _point(ax, 1, ys[1], C_BEFORE, MARKER_INCR, hollow=True)
        _point(ax, 2, ys[2], C_INCR, MARKER_INCR)
        _arrow(ax, (1, ys[1]), (2, ys[2]))
        _note(ax, -0.4, 10 ** 5.3,
              f"soak 3: {v['recSoakReaderErrorEpisodesThree']:.0f}\nepisodes, each\nhealed at reopen",
              ha="left", va="top", linespacing=0.95)
        ax.set_xticks(range(3), ["soak 2", "3", "4"])
        ax.set_ylabel("reader errors")
        _fix_label(ax, "pin segments at open (D-088)")
        _panel_tag(ax, "d", "reader errors")

        # (e) per-artifact cost
        ax = axes[4]
        ax.set_yscale("log")
        ax.set_ylim(2, 600)
        ax.set_xlim(-0.5, 1.5)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_decade_formatter))
        for xi, bar in ((0, "arc5_refresh_ms"), (1, "arc5_check_ms")):
            span = ((xi + 0.5 - 0.42) / 2.0, (xi + 0.5 + 0.42) / 2.0)
            _frozen_bar(ax, _bar(bar), pass_side="below", span=span)
            _note(ax, xi, _bar(bar) * 1.08, f"≤{_bar(bar):g}", color=C_PRED, ha="center",
                  va="bottom")
        for tok, dx, marker in (("CollegeMsg", -0.24, "o"), ("Synth", 0.24, "^")):
            pairs = ((0, f"recExt1ControlArcFivePreArcPublishMs{tok}",
                      f"recExt1ControlArcFivePerArtifactPublishMs{tok}"),
                     (1, f"recExt1ControlPreArcPerCheckMs{tok}",
                      f"recExt1ControlArcFivePerCheckMs{tok}"))
            for xi, before, after in pairs:
                _before_after(ax, xi + dx, v[before], xi + dx, v[after], C_INCR, marker)
        handles = [matplotlib.lines.Line2D([], [], marker=m, ls="none", ms=MS, mfc="white",
                                           mec="#000000", mew=HOLLOW_MEW, label=lab)
                   for m, lab in (("o", "CollegeMsg"), ("^", "synth"))]
        ax.legend(handles=handles, loc="lower right", handletextpad=0.1, borderaxespad=0.1)
        ax.set_xticks([0, 1], ["refresh", "check"])
        ax.set_ylabel("time per artifact (ms)")
        _fix_label(ax, "store identity cached;\ncheck walk on chain cache")
        _panel_tag(ax, "e", "per-artifact cost")

        _savefig_paper(fig, OUT_DIR / "f_arcs")


# --------------------------------------------------------------------------
# Fig. f_scale: memory is linear in rows; ingest and recovery are
# compaction-bound (single column, two panels)
# --------------------------------------------------------------------------

SCALE_README = SCALE_V1 / "README.md"
SCALE_COMPACTION_WALLS_100M = SCALE_V1 / "build-100m-compaction-walls.json"
_SCALE_ROWS = {"1M": 1e6, "10M": 1e7, "30M": 3e7, "100M": 1e8}

# (scale, quantity in build_scale_costs_data) -> the macro that landed it
SCALE_MACROS = {
    ("1M", "build_vmhwm_gb"): "recB7Calib1MPeakRSS",
    ("10M", "build_vmhwm_gb"): "recB7Calib10MPeakRSS",
    ("30M", "build_vmhwm_gb"): "recB7PeakRSS30M",
    ("100M", "build_vmhwm_gb"): "recB7PeakRSS100M",
    ("30M", "query_ready_floor_vmhwm_gb"): "recB7QueryFloor30M",
    ("100M", "query_ready_floor_vmhwm_gb"): "recB7QueryFloor100M",
    ("1M", "build_wall_s"): "recB7Calib1MBuildWall",
    ("10M", "build_wall_s"): "recB7Calib10MBuildWall",
    ("30M", "build_wall_s"): "recB7BuildWall30M",
    ("100M", "build_wall_s"): "recB7BuildWall100M",
    ("1M", "recovery_wall_s_ce500"): "recB7Calib1MRecovery",
    ("30M", "recovery_wall_s_ce500"): "recB7Recovery30M",
    ("30M", "recovery_wall_s_ce5000"): "recB7RecoveryCe5000At30M",
    ("100M", "recovery_wall_s_ce5000"): "recB7RecoveryCe5000At100M",
}


def build_scale_data() -> dict:
    costs = build_scale_costs_data()
    by_key = {(r["scale"], r["quantity"]): r for r in costs["rows"]}
    assert set(by_key) == set(SCALE_MACROS), (
        f"f_scale: record rows {sorted(set(by_key) ^ set(SCALE_MACROS))} have no macro pairing")
    rows = []
    for (scale, quantity), name in SCALE_MACROS.items():
        value = by_key[(scale, quantity)]["value"]
        assert_matches_macro(name, value)
        rows.append(_row("b" if "wall" in quantity else "a", quantity, scale, value,
                         by_key[(scale, quantity)]["unit"],
                         f"{name} == {by_key[(scale, quantity)]['source_file']}"))

    # bars and annotations read from the records themselves, sha-gated
    # against the scale campaign README's own table
    for path in (SCALE_BUILD_100M, SCALE_RECOVERY_30M_CE500, SCALE_COMPACTION_WALLS_100M):
        _readme_sha_gate(path, SCALE_README)
    build_100m = json.loads(SCALE_BUILD_100M.read_text(encoding="utf-8"))
    wall_bar = build_100m["falsifiers"]["build_wall"]
    band_h = wall_bar["band_h"]
    forecast = re.search(r"~(\d+)-(\d+)h total", wall_bar["note"])
    assert forecast, "f_scale: build-100m.json's build_wall note no longer states its ~N-Mh forecast"
    forecast_h = (float(forecast.group(1)), float(forecast.group(2)))
    rec_30m = json.loads(SCALE_RECOVERY_30M_CE500.read_text(encoding="utf-8"))
    rec_band_min = rec_30m["band_min"]
    assert_matches_macro("recB7RecoveryBoundRatioAt30M",
                         rec_30m["replay_wall_s"] / (rec_band_min[1] * 60))
    share = load_macro_values(["recB7CompactionShare100M"])["recB7CompactionShare100M"]
    walls = json.loads(SCALE_COMPACTION_WALLS_100M.read_text(encoding="utf-8"))
    assert_matches_macro("recB7CompactionShare100M", walls["compaction_only_share_of_wall_pct"])

    rows += [
        _row("b", "build wall frozen bar (upper)", "100M", band_h[1] * 3600, "s",
             f"{relpath(SCALE_BUILD_100M)}: falsifiers.build_wall.band_h[1] (h)"),
        _row("b", "build wall forecast band low", "100M", forecast_h[0] * 3600, "s",
             f"{relpath(SCALE_BUILD_100M)}: falsifiers.build_wall.note '~N-Mh total' (h)"),
        _row("b", "build wall forecast band high", "100M", forecast_h[1] * 3600, "s",
             f"{relpath(SCALE_BUILD_100M)}: falsifiers.build_wall.note '~N-Mh total' (h)"),
        _row("b", "recovery cadence 500 frozen bar (upper)", "30M", rec_band_min[1] * 60, "s",
             f"{relpath(SCALE_RECOVERY_30M_CE500)}: band_min[1] (min); == "
             "recB7RecoveryBoundRatioAt30M's bar"),
        _row("b", "compaction share of build wall", "100M", share, "%",
             "recB7CompactionShare100M"),
    ]
    return {"rows": rows, "by_key": by_key, "band_h": band_h, "forecast_h": forecast_h,
            "rec_band_min": rec_band_min, "share": share}


def write_scale_csv(data: dict) -> str:
    return _write_figure_csv("f_scale", data["rows"])


def _series(by_key, quantity):
    pts = sorted((_SCALE_ROWS[s], r["value"]) for (s, q), r in by_key.items() if q == quantity)
    return [p[0] for p in pts], [p[1] for p in pts]


def _scale_x_axis(ax) -> None:
    ax.set_xscale("log")
    ax.set_xlim(6e5, 1.7e8)
    ax.xaxis.set_major_locator(matplotlib.ticker.FixedLocator(list(_SCALE_ROWS.values())))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FixedFormatter(list(_SCALE_ROWS)))
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("input size (rows)")


def plot_scale(data: dict) -> None:
    _require_mpl()
    by_key = data["by_key"]
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_scale")
        axm, axt = fig.subplots(1, 2, gridspec_kw={"width_ratios": [0.92, 1.08]})

        # (a) memory
        xs, ys = _series(by_key, "build_vmhwm_gb")
        axm.plot(xs, ys, color=GREY_RAMP[0], marker="o", ms=MS, lw=1.0, zorder=3)
        fx, fy = _series(by_key, "query_ready_floor_vmhwm_gb")
        axm.plot(fx, fy, color=GREY_RAMP[1], marker="s", ms=MS, lw=1.0, ls=(0, (3, 1.5)),
                 zorder=3)
        guide_x = [xs[0], 1.7e8]
        axm.plot(guide_x, [ys[0] * gx / xs[0] for gx in guide_x], color=GREY_RAMP[2], lw=0.6,
                 ls=(0, (1, 1.5)), zorder=1)
        axm.set_yscale("log")
        axm.set_ylim(1, 600)
        axm.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_decade_formatter))
        _scale_x_axis(axm)
        axm.grid(axis="y", which="major")
        _note(axm, 1.4e6, ys[0] * 1.4 * 3.2, "linear", color=GREY_RAMP[2], ha="left",
              va="bottom", rotation=0)
        _note(axm, xs[-1] / 1.25, ys[-1], "ingest\npeak", color=GREY_RAMP[0], ha="right",
              va="center", linespacing=0.9)
        _note(axm, fx[-1] / 1.2, fy[-1] / 1.6, "query-ready\nfloor", color=GREY_RAMP[1],
              ha="right", va="top", linespacing=0.9)
        axm.set_ylabel("peak memory (GB)")
        _panel_tag(axm, "a", "memory")

        # (b) time
        _human_time_axis(axt, "y", [60.0, 600.0, 3600.0, 36000.0])
        axt.set_ylim(30, 1.2e5)
        _scale_x_axis(axt)
        axt.grid(axis="y", which="major")
        lo_h, hi_h = data["forecast_h"]
        axt.fill_between([6.2e7, 1.6e8], lo_h * 3600, hi_h * 3600, color=C_PRED,
                         alpha=PRED_BAND_ALPHA, lw=0, zorder=0)
        axt.plot([6.2e7, 1.6e8], [data["band_h"][1] * 3600] * 2, color=C_PRED, lw=REF_LW,
                 ls=REF_DASH, zorder=2)
        axt.plot([1.9e7, 4.7e7], [data["rec_band_min"][1] * 60] * 2, color=C_PRED, lw=REF_LW,
                 ls=REF_DASH, zorder=2)
        xs, ys = _series(by_key, "build_wall_s")
        axt.plot(xs, ys, color=GREY_RAMP[0], marker="o", ms=MS, lw=1.0, zorder=3)
        rx, ry = _series(by_key, "recovery_wall_s_ce500")
        axt.plot(rx, ry, color=GREY_RAMP[1], marker="v", ms=MS, lw=1.0, ls=(0, (3, 1.5)),
                 zorder=3)
        cx, cy = _series(by_key, "recovery_wall_s_ce5000")
        axt.plot(cx, cy, color=GREY_RAMP[2], lw=0.6, ls=(0, (1, 1.5)), zorder=2)
        for x, y in zip(cx, cy):
            _point(axt, x, y, GREY_RAMP[1], "^", hollow=True)
        _note(axt, xs[-1] / 1.3, ys[-1] * 1.55, "refuted", color=C_REF, ha="right",
              va="bottom")
        _note(axt, xs[-1] / 1.3, ys[-1] / 1.1, f"{data['share']:g}% compaction",
              ha="right", va="center")
        _note(axt, rx[-1] / 1.25, ry[-1], "refuted", color=C_REF, ha="right", va="center")
        handles = [
            matplotlib.lines.Line2D([], [], color=GREY_RAMP[0], marker="o", ms=MS, lw=1.0,
                                    label="ingest"),
            matplotlib.lines.Line2D([], [], color=GREY_RAMP[1], marker="v", ms=MS, lw=1.0,
                                    ls=(0, (3, 1.5)), label="recovery, 500"),
            matplotlib.lines.Line2D([], [], color=GREY_RAMP[2], marker="^", ms=MS, lw=0.6,
                                    ls=(0, (1, 1.5)), mfc="white", mec=GREY_RAMP[1],
                                    mew=HOLLOW_MEW, label="recovery, 5000"),
        ]
        axt.legend(handles=handles, loc="lower right", handlelength=1.6, borderaxespad=0.1)
        axt.set_ylabel("wall time")
        _panel_tag(axt, "b", "time")

        _savefig_paper(fig, OUT_DIR / "f_scale")


# --------------------------------------------------------------------------
# Fig. f_ldbc: LDBC SNB SF1, the four axes and the reference run (single
# column; replaces the LDBC table)
# --------------------------------------------------------------------------

LDBC_REF_V1 = ROOT / "benchmarks" / "ldbc-ref-v1"
LDBC_REF_TGMS_CAMPAIGN = LDBC_REF_V1 / "tgms-campaign-ldbc-ref-v1.json"
LDBC_REF_SHA256SUMS = LDBC_REF_V1 / "SHA256SUMS.txt"

LDBC_MACROS = [
    "recLdbcSfOneTemplates", "recLdbcExpressible", "recLdbcExecuted", "recLdbcValidated",
    "recLdbcCertifiedOrdered", "recLdbcCertifiedUnordered",
    "recLdbcTemplates", "recLdbcAgree", "recLdbcNotProjected", "recLdbcDisagree",
    "recLdbcTimeout",
    "recLdbcRowsBIAgreeing", "recLdbcRowsBICompared", "recLdbcRowsICAgreeing",
    "recLdbcRowsICCompared", "recLdbcRowsISAgreeing", "recLdbcRowsISCompared",
    "recLdbcRowsAgreeing", "recLdbcRowsCompared",
    "recLdbcRowAgreementFraction", "recLdbcGate", "recLdbcTemplateAgreementFraction",
]
LDBC_TEXT_MACROS = ["recLdbcDefectId", "recLdbcDefectTemplates"]


def build_ldbc_data() -> dict:
    v = load_macro_values(LDBC_MACROS)
    t = load_macro_texts(LDBC_TEXT_MACROS)
    assert (v["recLdbcAgree"] + v["recLdbcNotProjected"] + v["recLdbcDisagree"]
            + v["recLdbcTimeout"]) == v["recLdbcTemplates"], "f_ldbc: verdicts do not sum"
    families = ("BI", "IC", "IS")
    assert sum(v[f"recLdbcRows{f}Agreeing"] for f in families) == v["recLdbcRowsAgreeing"]
    assert sum(v[f"recLdbcRows{f}Compared"] for f in families) == v["recLdbcRowsCompared"]
    assert_matches_macro("recLdbcRowAgreementFraction",
                         v["recLdbcRowsAgreeing"] / v["recLdbcRowsCompared"])
    assert_matches_macro("recLdbcTemplateAgreementFraction",
                         v["recLdbcAgree"] / v["recLdbcTemplates"])
    _sha256sums_gate([LDBC_REF_TGMS_CAMPAIGN], LDBC_REF_SHA256SUMS)
    campaign = json.loads(LDBC_REF_TGMS_CAMPAIGN.read_text(encoding="utf-8"))["manifest"]
    ceiling_s = campaign["bypass_ceiling_s"] + campaign["child_open_allowance_s"]

    rows = []
    for name in ("recLdbcSfOneTemplates", "recLdbcExpressible", "recLdbcExecuted",
                 "recLdbcValidated", "recLdbcCertifiedOrdered", "recLdbcCertifiedUnordered"):
        rows.append(_row("a", "axis", name, v[name], "templates", name))
    for name in ("recLdbcAgree", "recLdbcNotProjected", "recLdbcDisagree", "recLdbcTimeout"):
        rows.append(_row("a", "verdict", name, v[name], "templates", name))
    rows.append(_row("a", "execution ceiling", "", ceiling_s, "s",
                     f"{relpath(LDBC_REF_TGMS_CAMPAIGN)}: manifest.bypass_ceiling_s + "
                     "manifest.child_open_allowance_s"))
    for f in families:
        rows.append(_row("b", f, "agreeing", v[f"recLdbcRows{f}Agreeing"], "rows",
                         f"recLdbcRows{f}Agreeing"))
        rows.append(_row("b", f, "compared", v[f"recLdbcRows{f}Compared"], "rows",
                         f"recLdbcRows{f}Compared"))
    for name in ("recLdbcRowAgreementFraction", "recLdbcGate",
                 "recLdbcTemplateAgreementFraction"):
        rows.append(_row("b", name, "", v[name], "fraction", name))
    return {"rows": rows, "v": v, "t": t, "ceiling_s": ceiling_s}


def write_ldbc_csv(data: dict) -> str:
    return _write_figure_csv("f_ldbc", data["rows"])


def plot_ldbc(data: dict) -> None:
    _require_mpl()
    v, t = data["v"], data["t"]
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_ldbc")
        axa, axb = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1.5, 1.0]})

        # (a) the four axes, and the reference run's verdicts
        total = v["recLdbcSfOneTemplates"]
        labels = ["SF1 templates", "expressible", f"executed ≤ {data['ceiling_s']:,.0f} s",
                  "validated", "certified complete", "verdicts"]
        ypos = list(range(len(labels)))[::-1]
        values = [total, v["recLdbcExpressible"], v["recLdbcExecuted"], v["recLdbcValidated"]]
        for y, val in zip(ypos[:4], values):
            axa.barh(y, val, height=0.62, color=GREY_RAMP[1], lw=0)
            _note(axa, val + 0.6, y, f"{val:.0f}", color="#000000", va="center")
        yc = ypos[4]
        axa.barh(yc, v["recLdbcCertifiedUnordered"], height=0.62, color=GREY_RAMP[3], lw=0)
        axa.barh(yc, v["recLdbcCertifiedOrdered"], height=0.62, color=GREY_RAMP[1], lw=0)
        _note(axa, v["recLdbcCertifiedOrdered"] + 0.6, yc,
              f"{v['recLdbcCertifiedOrdered']:.0f} ordered", color="#000000", va="center")
        _note(axa, v["recLdbcCertifiedUnordered"] + 0.6, yc,
              f"{v['recLdbcCertifiedUnordered']:.0f}", va="center", color="#000000")
        _note(axa, v["recLdbcCertifiedUnordered"] - 0.6, yc, "unordered", ha="right",
              va="center")
        yv = ypos[5]
        left = 0.0
        segments = (("recLdbcAgree", GREY_RAMP[1], None, "agree"),
                    ("recLdbcNotProjected", GREY_RAMP[2], None, "not projected"),
                    ("recLdbcDisagree", C_REF, None, "disagree"),
                    ("recLdbcTimeout", GREY_RAMP[3], "/////", "timeout"))
        handles = []
        for name, color, hatch, label in segments:
            axa.barh(yv, v[name], left=left, height=0.62, color=color, hatch=hatch,
                     edgecolor="white" if hatch is None else GREY_RAMP[2], lw=0.4)
            handles.append(matplotlib.patches.Patch(facecolor=color, hatch=hatch,
                                                    edgecolor=GREY_RAMP[2] if hatch else color,
                                                    lw=0.4, label=f"{label} {v[name]:.0f}"))
            left += v[name]
        _note(axa, v["recLdbcAgree"] / 2, yv, f"{v['recLdbcAgree']:.0f}", color="#FFFFFF",
              ha="center", va="center")
        _note(axa, left + 0.6, yv,
              f"{t['recLdbcDefectId']}: {t['recLdbcDefectTemplates']}", color=C_REF,
              va="center")
        axa.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.42, 0.0), ncol=2,
                   handlelength=0.9, columnspacing=0.6, handletextpad=0.3, labelspacing=0.1,
                   borderaxespad=0.0)
        axa.set_yticks(ypos, labels)
        axa.tick_params(axis="y", length=0)
        axa.set_xlim(0, total * 1.12)
        axa.set_ylim(-0.5, len(labels) - 0.5)
        axa.set_xticks([])
        axa.spines["left"].set_visible(False)
        axa.spines["bottom"].set_visible(False)
        _panel_tag(axa, "a", "four axes")

        # (b) row agreement by family
        fams = ("BI", "IC", "IS")
        yb = [4, 3, 2]
        for y, f in zip(yb, fams):
            frac = v[f"recLdbcRows{f}Agreeing"] / v[f"recLdbcRows{f}Compared"]
            axb.barh(y, 1.0, height=0.62, color=GREY_RAMP[3], lw=0)
            axb.barh(y, frac, height=0.62, color=GREY_RAMP[1], lw=0)
        axb.plot([v["recLdbcRowAgreementFraction"]], [1], marker="|", ms=7, mew=1.2,
                 color="#000000", ls="none", zorder=4)
        _point(axb, v["recLdbcTemplateAgreementFraction"], 0, "#000000", "o", hollow=True)
        axb.set_xlim(0, 1.0)
        axb.set_ylim(-0.6, 4.6)
        _frozen_bar(axb, v["recLdbcGate"], orient="v", pass_side="above")
        _note(axb, v["recLdbcGate"] - 0.03, 1, f"gate {v['recLdbcGate']:.2f}", color=C_PRED,
              ha="right", va="center")
        ylabels = [f"{f} {v[f'recLdbcRows{f}Agreeing']:.0f}/{v[f'recLdbcRows{f}Compared']:.0f}"
                   for f in fams]
        ylabels += [f"all rows {v['recLdbcRowAgreementFraction']:.3f}",
                    f"templates {v['recLdbcTemplateAgreementFraction']:.2f}"]
        axb.set_yticks(yb + [1, 0], ylabels)
        axb.tick_params(axis="y", length=0)
        axb.spines["left"].set_visible(False)
        axb.set_xticks([0, 0.5, 1.0], ["0", "0.5", "1"])
        axb.set_xlabel("agreement", labelpad=1.0)
        _panel_tag(axb, "b", "row agreement")

        _savefig_paper(fig, OUT_DIR / "f_ldbc")


# --------------------------------------------------------------------------
# Fig. f_open_cost (optional): the Merkle chain is not the open cost
# --------------------------------------------------------------------------

OPEN_COST_MACROS = [
    "recB1v2eOpenCheckpointMs", "recB1v2eOpenMerkleVerifyMs", "recB1v2eOpenStateBuildMs",
    "recB1v2eOpenDeltaReplayMs", "recB1v2eOpenDictionaryMs", "recB1v2eOpenTotalMs",
    "recB1v2eOpenComponentMs",
    "recB1co7CheckpointReadParseMs", "recB1co7MerkleVerifyMs", "recB1co7StateBuildMs",
    "recB1co7DeltaReplayMs", "recB1co7DictionaryOpenMs", "recB1co7TotalMs", "recB1co7ComponentMs",
    "recB1co7DeltaCount", "recB1co7ControlOpenMs", "recB1co7ControlDeltaCount",
    "recB1co7ControlPerDeltaMs", "recB1WorstPhaseOpenTreatmentMs", "recB1WorstPhaseOpenControlS",
]
_OPEN_PHASES = ("checkpoint", "Merkle verify", "state build", "delta replay")


def build_open_cost_data() -> dict:
    v = load_macro_values(OPEN_COST_MACROS)
    before = dict(zip(_OPEN_PHASES, (v["recB1v2eOpenCheckpointMs"], v["recB1v2eOpenMerkleVerifyMs"],
                                     v["recB1v2eOpenStateBuildMs"], v["recB1v2eOpenDeltaReplayMs"])))
    after = dict(zip(_OPEN_PHASES, (v["recB1co7CheckpointReadParseMs"], v["recB1co7MerkleVerifyMs"],
                                    v["recB1co7StateBuildMs"], v["recB1co7DeltaReplayMs"])))
    sources_b = ("recB1v2eOpenCheckpointMs", "recB1v2eOpenMerkleVerifyMs",
                 "recB1v2eOpenStateBuildMs", "recB1v2eOpenDeltaReplayMs")
    sources_a = ("recB1co7CheckpointReadParseMs", "recB1co7MerkleVerifyMs",
                 "recB1co7StateBuildMs", "recB1co7DeltaReplayMs")
    assert_matches_macro("recB1v2eOpenComponentMs", sum(before.values()), from_macros=sources_b)
    assert_matches_macro("recB1co7ComponentMs", sum(after.values()), from_macros=sources_a)
    rows = []
    for label, phases, srcs, dict_name, total_name in (
            ("format 3, before", before, sources_b, "recB1v2eOpenDictionaryMs",
             "recB1v2eOpenTotalMs"),
            ("format 3, after", after, sources_a, "recB1co7DictionaryOpenMs", "recB1co7TotalMs")):
        for (phase, val), src in zip(phases.items(), srcs):
            rows.append(_row("", label, phase, val, "ms", src))
        rows.append(_row("", label, "dictionary", v[dict_name], "ms", dict_name))
        other = v[total_name] - sum(phases.values()) - v[dict_name]
        assert other >= 0, f"f_open_cost: {label}: phases exceed the total"
        rows.append(_row("", label, "other (total - phases)", round(other, 3), "ms",
                         f"{total_name} - phases - {dict_name}"))
        rows.append(_row("", label, "total", v[total_name], "ms", total_name))
    for name, unit in (("recB1co7ControlOpenMs", "ms"), ("recB1co7ControlDeltaCount", "deltas"),
                       ("recB1co7ControlPerDeltaMs", "ms/delta"), ("recB1co7DeltaCount", "deltas"),
                       ("recB1WorstPhaseOpenTreatmentMs", "ms"),
                       ("recB1WorstPhaseOpenControlS", "s")):
        rows.append(_row("", "format 2 control" if "Control" in name else "worst phase",
                         name, v[name], unit, name))
    return {"rows": rows, "v": v, "before": before, "after": after}


def write_open_cost_csv(data: dict) -> str:
    return _write_figure_csv("f_open_cost", data["rows"])


def plot_open_cost(data: dict) -> None:
    _require_mpl()
    v = data["v"]
    xmax = 1250.0
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_open_cost")
        ax = fig.subplots(1, 1)
        colors = {"checkpoint": GREY_RAMP[0], "Merkle verify": GREY_RAMP[1],
                  "state build": GREY_RAMP[2], "delta replay": GREY_RAMP[2],
                  "other": "#FFFFFF", "dictionary": GREY_RAMP[3]}
        rows = ((data["before"], "recB1v2eOpenDictionaryMs", "recB1v2eOpenTotalMs", 2),
                (data["after"], "recB1co7DictionaryOpenMs", "recB1co7TotalMs", 1))
        for phases, dict_name, total_name, y in rows:
            left = 0.0
            for phase, val in phases.items():
                ax.barh(y, val, left=left, height=0.6, color=colors[phase], edgecolor="white",
                        lw=0.3)
                left += val
            other = v[total_name] - left - v[dict_name]
            ax.barh(y, other, left=left, height=0.6, color=colors["other"],
                    edgecolor=GREY_RAMP[2], lw=0.3)
            left += other
            ax.barh(y, v[dict_name], left=left, height=0.6, color=colors["dictionary"],
                    edgecolor="white", lw=0.3)
            _note(ax, sum(phases.values()) + 12, y + 0.34,
                  f"chain {sum(phases.values()):.0f} ms", ha="left", va="bottom")
            _note(ax, v[total_name] + 15, y, f"{v[total_name]:,.0f} ms", ha="left",
                  va="center", color="#000000")
        axis_end = 1350.0
        ax.barh(0, axis_end * 0.97, height=0.6, color=GREY_RAMP[2], lw=0)
        ax.plot([axis_end * 0.945, axis_end * 0.965], [-0.38, 0.38], color="white", lw=2.0,
                zorder=4)
        _note(ax, 20, 0, f"{v['recB1co7ControlOpenMs']:,.0f} ms: "
              f"{v['recB1co7ControlDeltaCount']:.0f} deltas × "
              f"{v['recB1co7ControlPerDeltaMs']:g} ms/delta", color="#000000", ha="left",
              va="center")
        _note(ax, 430, 2.62,
              f"projected worst case: {v['recB1WorstPhaseOpenTreatmentMs']:g} ms vs "
              f"{v['recB1WorstPhaseOpenControlS']:g} s", ha="left", va="center")
        handles = [matplotlib.patches.Patch(facecolor=colors[k], edgecolor=GREY_RAMP[2], lw=0.3,
                                            label=lab)
                   for k, lab in (("checkpoint", "checkpoint"), ("Merkle verify", "Merkle"),
                                  ("state build", "state + deltas"), ("other", "other"),
                                  ("dictionary", "dictionary"))]
        ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.45, 1.0), ncol=5,
                  handlelength=0.9, columnspacing=0.6, handletextpad=0.3)
        ax.set_yticks([2, 1, 0], ["format 3, before", "format 3, after", "format 2"])
        ax.tick_params(axis="y", length=0)
        ax.spines["left"].set_visible(False)
        ax.set_xlim(0, axis_end)
        ax.set_xticks([0, 250, 500, 750, 1000, 1250])
        ax.spines["bottom"].set_bounds(0, xmax)
        ax.set_ylim(-0.5, 2.9)
        ax.set_xlabel("engine open time (ms)")
        _savefig_paper(fig, OUT_DIR / "f_open_cost")


# --------------------------------------------------------------------------
# Fig. f_correction_load: sound first, then fast (single column, two
# panels; the re-rendered correction-load arc is panel (a))
# --------------------------------------------------------------------------

_V3_CELLS = [f"recStormV3Speedup{store}{mix}{age}" for store in ("Synth", "CollegeMsg")
             for mix in ("C1", "C3", "C4") for age in ("None", "Deep")]
CORRECTION_LOAD_MACROS = R18_CROSSOVER_MACROS + _V3_CELLS + [
    "recStormV1SurvivorFractionC1Median", "recStormV1PrecisionC1Median",
    "recStormV1AvoidedDecisionC1Median",
    "recStormV2SurvivorFractionC1Median", "recStormV2PrecisionC1Median",
    "recStormV2AvoidedDecisionC1Median",
]
_WHY_ROWS = (("survivor fraction", "SurvivorFractionC1Median", "rollout_survivor_predicted"),
             ("precision", "PrecisionC1Median", "rollout_precision_predicted"),
             ("avoided recomputation", "AvoidedDecisionC1Median", "rollout_avoided_predicted"))


def build_correction_load_data() -> dict:
    check_frozen_bars()
    v = load_macro_values(CORRECTION_LOAD_MACROS)
    cells = [v[n] for n in _V3_CELLS]
    assert min(cells) >= v["recStormV3SpeedupGridMin"] - 5e-4
    assert max(cells) <= v["recStormV3SpeedupGridMax"] + 5e-4
    rows = []
    for n, (pre, v2, v3) in ((1_000, ("recStormV1CrossoverSpeedupSumN1k",
                                      "recStormV2SpeedupSumN1kSeed0", "recStormV3SpeedupN1kSeed0")),
                             (10_000, ("recStormV1CrossoverSpeedupSumN10k",
                                       "recStormV2SpeedupSumProbe", "recStormV3ProbeSpeedup"))):
        rows.append(_row("a", "pre-rollout (3 of 14)", n, v[pre], "x", pre))
        rows.append(_row("a", "post-rollout re-measured", n, v[v3], "x", v3))
        rows.append(_row("a", "post-rollout reconstructed", n, v[v2], "x", v2))
    for name in ("recStormV1SpeedupSumGridMin", "recStormV1SpeedupSumGridMax"):
        rows.append(_row("a", "pre-rollout grid whisker", 1_000, v[name], "x", name))
    for name in _V3_CELLS:
        rows.append(_row("a", "post-rollout cell", 1_000, v[name], "x", name))
    rows.append(_row("a", "first recorded (check-only timer)", 1_000,
                     v["recStormV1SpeedupN1kSeed0"], "x", "recStormV1SpeedupN1kSeed0"))
    for n, bar in ((1_000, "rollout_speedup_n1k_predicted"),
                   (10_000, "rollout_speedup_n10k_predicted")):
        rows.append(_row("a", "predicted", n, _bar(bar), "x", FROZEN_BARS[bar]["citation"]))
    for label, tail, pred in _WHY_ROWS:
        rows.append(_row("b", "pre-rollout", label, v[f"recStormV1{tail}"], "fraction",
                         f"recStormV1{tail}"))
        rows.append(_row("b", "post-rollout", label, v[f"recStormV2{tail}"], "fraction",
                         f"recStormV2{tail}"))
        rows.append(_row("b", "predicted", label, _bar(pred), "fraction",
                         FROZEN_BARS[pred]["citation"]))
    rows.append(_row("b", "frozen bar (met)", "avoided recomputation",
                     _bar("rollout_avoided_bar"), "fraction",
                     FROZEN_BARS["rollout_avoided_bar"]["citation"]))
    rows.append(_row("b", "frozen bar (superseded)", "avoided recomputation", _bar("P3"),
                     "fraction", FROZEN_BARS["P3"]["citation"]))
    return {"rows": rows, "v": v}


def write_correction_load_csv(data: dict) -> str:
    return _write_figure_csv("f_correction_load", data["rows"])


def plot_correction_load(data: dict) -> None:
    _require_mpl()
    v = data["v"]
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_correction_load")
        axa, axb = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1.0, 1.05]})

        # (a) speedup vs N
        _log2_speedup_axis(axa, 0.5, 8)
        axa.set_xlim(-0.45, 1.45)
        axa.axhspan(0.5, 1.0, color=C_REF, alpha=SLOWER_TINT_ALPHA, lw=0, zorder=0)
        axa.axhline(1.0, color=GREY_RAMP[2], lw=REF_LW, ls=REF_DASH, zorder=1)
        _note(axa, -0.42, 0.53, "incremental slower", color=C_REF, ha="left", va="bottom")
        pre = [v["recStormV1CrossoverSpeedupSumN1k"], v["recStormV1CrossoverSpeedupSumN10k"]]
        post = [v["recStormV3SpeedupN1kSeed0"], v["recStormV3ProbeSpeedup"]]
        recon = [v["recStormV2SpeedupSumN1kSeed0"], v["recStormV2SpeedupSumProbe"]]
        axa.plot([0, 1], pre, color=C_BEFORE, lw=1.0, ls=(0, (3, 1.5)), zorder=2)
        for x, y in zip((0, 1), pre):
            _point(axa, x, y, C_BEFORE, MARKER_INCR, hollow=True)
        lo, hi = v["recStormV1SpeedupSumGridMin"], v["recStormV1SpeedupSumGridMax"]
        axa.errorbar([0], [pre[0]], yerr=[[pre[0] - lo], [hi - pre[0]]], fmt="none",
                     ecolor=C_BEFORE, elinewidth=0.6, capsize=1.5, capthick=0.6, zorder=2)
        axa.plot([0, 1], post, color=C_INCR, lw=1.0, zorder=3)
        for x, y in zip((0, 1), post):
            _point(axa, x, y, C_INCR, MARKER_INCR)
        for i, name in enumerate(_V3_CELLS):
            jitter = -0.06 + 0.12 * i / (len(_V3_CELLS) - 1)
            axa.plot([0.17 + jitter], [v[name]], marker="o", ls="none", ms=MS_CELL,
                     mfc=C_INCR, mec="none", alpha=CELL_ALPHA, zorder=2)
        for x, y in zip((-0.12, 0.83), recon):
            _point(axa, x, y, C_INCR, "s", hollow=True, ms=2.6)
        for x, bar in ((0, "rollout_speedup_n1k_predicted"), (1, "rollout_speedup_n10k_predicted")):
            _point(axa, x, _bar(bar), C_PRED, MARKER_PRED, hollow=True)
        first = v["recStormV1SpeedupN1kSeed0"]
        axa.plot([-0.27], [first], marker="x", ls="none", ms=2.8, mew=0.7, color=C_BEFORE,
                 zorder=3)
        _note(axa, -0.27, first / 1.14, "first\nrecorded,\ncheck-only", ha="center", va="top",
              linespacing=0.9)
        _note(axa, 0.06, _bar("rollout_speedup_n1k_predicted"), "predicted", color=C_PRED,
              ha="left", va="center")
        _note(axa, 0.56, (post[0] * post[1]) ** 0.5 / 1.12, "P6: refuted", color=C_REF,
              ha="center", va="top", rotation=-13)
        _note(axa, 0.5, (pre[0] * pre[1]) ** 0.5 / 1.07, "3 of 14", color=C_BEFORE,
              ha="center", va="top")
        _note(axa, 0.62, (post[0] * post[1]) ** 0.5 * 1.18, "13 of 14", color=C_INCR,
              ha="center", va="bottom", rotation=-13)
        axa.set_xticks([0, 1], ["$N=10^3$", "$N=10^4$"])
        axa.set_ylabel("speedup over TGMS\nglobal recompute (×)", linespacing=0.95)
        _panel_tag(axa, "a", "speedup")

        # (b) why: survivor fraction, precision, avoided recomputation
        ys = {label: 2 - i for i, (label, _t, _p) in enumerate(_WHY_ROWS)}
        axb.set_xlim(0, 1.0)
        axb.set_ylim(-0.95, 2.55)
        for label, tail, pred in _WHY_ROWS:
            y = ys[label]
            _before_after(axb, v[f"recStormV1{tail}"], y, v[f"recStormV2{tail}"], y, C_INCR,
                          MARKER_INCR)
            _point(axb, _bar(pred), y + 0.28, C_PRED, MARKER_PRED, hollow=True, ms=3.0)
        ya = ys["avoided recomputation"]
        span = ((ya - 0.42 + 0.95) / 3.5, (ya + 0.42 + 0.95) / 3.5)
        _frozen_bar(axb, _bar("rollout_avoided_bar"), orient="v", pass_side="above", span=span)
        axb.axvline(_bar("P3"), span[0], span[1], color=C_BEFORE, lw=REF_LW, ls=REF_DASH)
        _note(axb, _bar("rollout_avoided_bar"), ya - 0.46, f"{_bar('rollout_avoided_bar'):.2f}",
              color=C_PRED, ha="center", va="top")
        _note(axb, _bar("P3") + 0.03, ya - 0.46, "P3", color=C_BEFORE, ha="center", va="top")
        _note(axb, 1.0, ya - 0.8, "(superseded)", color=C_BEFORE, ha="right", va="top")
        _note(axb, v["recStormV1AvoidedDecisionC1Median"], ya - 0.2, "refuted", color=C_REF,
              ha="center", va="top")
        axb.set_yticks([ys[label] for label, _t, _p in _WHY_ROWS],
                       ["survivors", "precision", "avoided"])
        axb.tick_params(axis="y", length=0)
        axb.spines["left"].set_visible(False)
        axb.set_xticks([0, 0.5, 1.0], ["0", "0.5", "1"])
        axb.set_xlabel("fraction (median, first class)", labelpad=1.0)
        _panel_tag(axb, "b", "why")

        _savefig_paper(fig, OUT_DIR / "f_correction_load")


# --------------------------------------------------------------------------
# Fig. f_durability: the durability floor (single column; commit protocol
# with its crash boundaries, and the corruption outcome per class)
# --------------------------------------------------------------------------

# The write path in protocol order (structural), each step with the crash
# boundaries that fall at or inside it; the side lanes run outside the
# commit. Every boundary the crash record names must appear exactly once.
_PROTOCOL_STEPS = (
    ("log append\n+ fsync", ("py_torn_wal_append", "py_after_wal_fsync")),
    ("seal\nsegments", ("py_before_engine_commit", "after_seal")),
    ("close\nruns", ("after_close_runs",)),
    ("dictionary", ("after_dict",)),
    ("manifest\n+ fsync", ("after_manifest",)),
    ("rename\nCURRENT", ("after_current",)),
)
# Where each boundary sits on its step's box: a fraction of the box width
# (mid-append for the torn append, the box's left edge before the engine's
# commit starts, its right edge "after" a step).
_BOUNDARY_AT = {"py_torn_wal_append": 0.5, "py_before_engine_commit": 0.0}
_SIDE_LANES = (("compaction install", "compact_before_install"), ("GC delete", "gc_mid_delete"))

# Corruption classes by storage layer: log, manifest, segments, dictionary,
# artifacts. Labels are the readable names of the record's class ids.
_CORRUPTION_CLASSES = (
    ("event_log_record", "log record"), ("event_log_tail", "log tail"),
    ("current", "CURRENT"), ("checkpoint_manifest", "checkpoint manifest"),
    ("delta_manifest", "delta manifest"),
    ("segment_header", "segment header"), ("segment_body", "segment body"),
    ("segment_footer", "segment footer"), ("close_run", "closed run"),
    ("tcsr_file", "CSR index"),
    ("dict_tail", "dictionary tail"),
    ("artifacts_jsonl", "artifact index"), ("artifact_blob", "artifact blob"),
)
_VERDICTS = (("DETECTED", "detected", GREY_RAMP[1]),
             ("TOLERATED-REBUILT", "rebuilt", GREY_RAMP[2]),
             ("BENIGN", "benign", GREY_RAMP[3]),
             ("SILENT", "silent", C_REF))

DURABILITY_MACROS = ["recCrashTrials", "recCrashProblems", "recCrashBoundaries",
                     "recCorruptionTrials", "recCorruptionClasses", "recCorruptionDetectedPost",
                     "recCorruptionSilentPost", "recCorruptionBlobTrials"]
DURABILITY_TEXT_MACROS = ["recCorruptionBlobDetectedAllPre", "recCorruptionBlobDetectedAllPost"]


def _verdict_counts(results: list[dict]) -> dict[str, dict[str, int]]:
    """Per corruption class, the trial count of each verdict (the extension
    of build_corruption_matrix_data's detection rate: "0 detected" is not
    "silent", and an outcome stack shows the difference)."""
    out: dict[str, dict[str, int]] = {}
    for r in results:
        cell = out.setdefault(r["class"], dict.fromkeys((k for k, _l, _c in _VERDICTS), 0))
        assert r["verdict"] in cell, f"unknown corruption verdict {r['verdict']!r}"
        cell[r["verdict"]] += 1
    return out


def build_durability_data() -> dict:
    v = load_macro_values(DURABILITY_MACROS)
    t = load_macro_texts(DURABILITY_TEXT_MACROS)
    crash = build_crash_data()
    crash_record = json.loads(CRASH_V1.read_text(encoding="utf-8"))
    order = crash_record["config"]["boundaries"]
    placed = [b for _s, bs in _PROTOCOL_STEPS for b in bs] + [b for _l, b in _SIDE_LANES]
    assert sorted(placed) == sorted(order) == sorted(r["boundary"] for r in crash["rows"]), (
        "f_durability: the protocol strip must place every crash boundary exactly once")
    assert crash["total"]["trials"] == v["recCrashTrials"]
    assert crash["total"]["problems"] == v["recCrashProblems"]
    assert len(order) == v["recCrashBoundaries"]

    pre = json.loads(CORRUPTION_PRE.read_text(encoding="utf-8"))["results"]
    post = json.loads(CORRUPTION_POST.read_text(encoding="utf-8"))["results"]
    pre_counts, post_counts = _verdict_counts(pre), _verdict_counts(post)
    classes = [c for c, _l in _CORRUPTION_CLASSES]
    assert sorted(classes) == sorted(post_counts) == sorted(pre_counts), (
        "f_durability: the layer ordering must name every corruption class exactly once")
    assert len(classes) == v["recCorruptionClasses"]
    assert len(post) == v["recCorruptionTrials"]
    assert sum(c["DETECTED"] for c in post_counts.values()) == v["recCorruptionDetectedPost"]
    assert sum(c["SILENT"] for c in post_counts.values()) == v["recCorruptionSilentPost"]
    blob_pre, blob_post = pre_counts["artifact_blob"], post_counts["artifact_blob"]
    blob_n = sum(blob_post.values())
    assert blob_n == v["recCorruptionBlobTrials"]
    assert t["recCorruptionBlobDetectedAllPre"] == f"{blob_pre['DETECTED']}/{blob_n}"
    assert t["recCorruptionBlobDetectedAllPost"] == f"{blob_post['DETECTED']}/{blob_n}"

    rows = []
    by_name = {r["boundary"]: r for r in crash["rows"]}
    for b in order:
        rows.append(_row("a", b, "trials", by_name[b]["trials"], "trials", relpath(CRASH_V1)))
        rows.append(_row("a", b, "problems", by_name[b]["problems"], "trials",
                         relpath(CRASH_V1)))
    for c in classes:
        for k, _l, _c in _VERDICTS:
            rows.append(_row("b", c, f"{k} post", post_counts[c][k], "trials",
                             f"{relpath(CORRUPTION_POST)}: results[].verdict"))
    for k, _l, _c in _VERDICTS:
        rows.append(_row("b", "artifact_blob", f"{k} pre", blob_pre[k], "trials",
                         f"{relpath(CORRUPTION_PRE)}: results[].verdict"))
    return {"rows": rows, "order": order, "by_name": by_name, "post": post_counts,
            "blob_pre": blob_pre, "t": t}


def write_durability_csv(data: dict) -> str:
    return _write_figure_csv("f_durability", data["rows"])


def plot_durability(data: dict) -> None:
    _require_mpl()
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_durability")
        # two subfigures, so the strip in (a) spans the full width instead of
        # inheriting (b)'s tick-label margin
        sub_a, sub_b = fig.subfigures(2, 1, height_ratios=[0.66, 1.64])
        axa, axb = sub_a.subplots(1, 1), sub_b.subplots(1, 1)

        # (a) commit protocol and crash boundaries: every boundary a tick
        # with its problem count (every boundary ran the same trial count,
        # asserted in build_durability_data and stated in the panel tag)
        n_steps = len(_PROTOCOL_STEPS)
        axa.set_xlim(-0.02, n_steps + 0.02)
        axa.set_ylim(0, 2.6)
        axa.axis("off")
        box_y, box_h, box_w = 0.78, 1.15, 0.86
        by_name = data["by_name"]
        for i, (label, bounds) in enumerate(_PROTOCOL_STEPS):
            last = i == n_steps - 1
            x0 = i + (1 - box_w) / 2
            axa.add_patch(matplotlib.patches.FancyBboxPatch(
                (x0, box_y), box_w, box_h, boxstyle="round,pad=0,rounding_size=0.05",
                fc=matplotlib.colors.to_rgba(C_INCR, 0.10) if last else "#FFFFFF",
                ec=C_INCR if last else GREY_RAMP[1], lw=0.6 if last else 0.5))
            _note(axa, i + 0.5, box_y + box_h / 2, label, color="#000000", ha="center",
                  va="center", fontsize=6.0, linespacing=0.85)
            if i:
                _arrow(axa, (x0 - (1 - box_w) + 0.005, box_y + box_h / 2),
                       (x0 - 0.005, box_y + box_h / 2), color=GREY_RAMP[1], shrink=0)
            for b in bounds:
                x = x0 + box_w * _BOUNDARY_AT.get(b, 1.0)
                axa.plot([x], [box_y + box_h + 0.12], marker="v", ms=2.4, color="#000000",
                         zorder=3)
                _note(axa, x, box_y + box_h + 0.28, f"{by_name[b]['problems']}", ha="center",
                      va="bottom", fontsize=6.0, color="#000000")
        for j, (label, b) in enumerate(_SIDE_LANES):
            x0 = 0.07 + 2.0 * j
            axa.plot([x0, x0 + 1.8], [0.3, 0.3], color=GREY_RAMP[2], lw=0.6, ls=(0, (2, 1)))
            xb = x0 + 1.55
            axa.plot([xb], [0.3], marker="v", ms=2.4, color="#000000", zorder=3)
            _note(axa, x0, 0.38, label, color="#000000", ha="left", va="bottom", fontsize=6.0)
            _note(axa, xb + 0.08, 0.38, f"{by_name[b]['problems']}", ha="left", va="bottom",
                  fontsize=6.0, color="#000000")
        _note(axa, n_steps - 0.5, 0.38, "publication point", color=C_INCR, ha="center",
              va="bottom", fontsize=6.0)
        trials = {by_name[b]["trials"] for b in data["order"]}
        assert len(trials) == 1, "f_durability: boundaries ran different trial counts"
        _panel_tag(axa, "a", f"commit protocol; crash problems per {trials.pop():,} trials")

        # (b) corruption outcome by class (post-fix), with the pre-fix
        # artifact_blob row as a ghost bar above its class
        post = data["post"]
        rows = [(c, lab, post[c], False) for c, lab in _CORRUPTION_CLASSES[:-1]]
        rows.append(("artifact_blob", "before fix", data["blob_pre"], True))
        rows.append((_CORRUPTION_CLASSES[-1][0], _CORRUPTION_CLASSES[-1][1],
                     post[_CORRUPTION_CLASSES[-1][0]], False))
        ypos = list(range(len(rows)))[::-1]
        for (c, _lab, counts, ghost), y in zip(rows, ypos):
            n = sum(counts.values())
            left = 0.0
            for key, _l, color in _VERDICTS:
                frac = counts[key] / n
                axb.barh(y, frac, left=left, height=0.5 if ghost else 0.72, color=color, lw=0,
                         alpha=0.55 if ghost else 1.0)
                left += frac
            _note(axb, 1.035, y, f"{counts['SILENT']}", color=C_REF, ha="left", va="center")
        ghost_y = ypos[-2]
        _note(axb, 0.16, ghost_y,
              f"{data['t']['recCorruptionBlobDetectedAllPre']} $\\rightarrow$ "
              f"{data['t']['recCorruptionBlobDetectedAllPost']} detected",
              color="#000000", ha="left", va="center", fontsize=6.0)
        _note(axb, 1.035, ypos[0] + 0.8, "silent", color=C_REF, ha="left", va="bottom")
        ticklabels = [lab for _c, lab, _n, _g in rows]
        axb.set_yticks(ypos, ticklabels)
        for tick, (_c, _lab, _n, ghost) in zip(axb.get_yticklabels(), rows):
            if ghost:
                tick.set_color(C_ANNOT)
                tick.set_fontstyle("italic")
        axb.tick_params(axis="y", length=0)
        axb.spines["left"].set_visible(False)
        axb.set_xlim(0, 1.0)
        axb.set_ylim(-0.55, len(rows) - 0.45)
        axb.set_xticks([0, 0.5, 1.0], ["0", "50%", "100%"])
        axb.set_xlabel("share of trials per class", labelpad=1.0)
        handles = [matplotlib.patches.Patch(facecolor=color, lw=0, label=label)
                   for _k, label, color in _VERDICTS[:3]]
        axb.legend(handles=handles, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=3,
                   handlelength=0.9, columnspacing=0.7, handletextpad=0.3, borderaxespad=0.1)
        _panel_tag(axb, "b", "outcome")

        _savefig_paper(fig, OUT_DIR / "f_durability")


# --------------------------------------------------------------------------
# Fig. f_external: cheaper than a full recompute, costlier than a view,
# and the only one that never serves stale silently (double column)
#
# Per-cell values are read from the external-v1 records only after every
# file is checked against that directory's SHA256SUMS.txt; the medians the
# figure draws are then asserted against the landed recExt1*/recExt2*
# macros ("assert, do not trust").
# --------------------------------------------------------------------------

EXTERNAL_V1 = ROOT / "benchmarks" / "external-v1"
EXTERNAL_SHA256SUMS = EXTERNAL_V1 / "SHA256SUMS.txt"
EXTERNAL_NEO4J = EXTERNAL_V1 / "neo4j-recompute-2026-10-07.json"
EXTERNAL_IVM = EXTERNAL_V1 / "ivm-differential-2026-10-07.json"
EXTERNAL_CONTROL = EXTERNAL_V1 / "tgms-control-2026-10-05.json"
EXTERNAL_CONTROL_BATCHES = EXTERNAL_V1 / "tgms-control-2026-10-05-batches.jsonl"
EXTERNAL_CONTROL_ARC5 = EXTERNAL_V1 / "tgms-control-2026-10-08-arc5.json"
EXTERNAL_WITHHELD_HOLD = EXTERNAL_V1 / "ivm-differential-2026-10-09-withheld-hold.json"
_EXT_PROBE = "synth-iv-60k-c1-none-n10000-s0"
_EXT_FACETS = (("collegemsg", 1_000, "CollegeMsg, $N=10^3$", "CollegeMsg"),
               ("synth-iv-60k", 1_000, "synth, $N=10^3$", "Synth"),
               ("synth-iv-60k", 10_000, "synth, $N=10^4$", "Probe"))
_EXT_CONFIGS = (("incr", "incremental refresh", C_INCR, MARKER_INCR),
                ("global", "global recompute", C_GLOBAL, MARKER_GLOBAL),
                ("neo", "Neo4j recompute", C_NEO, MARKER_NEO),
                ("dd", "dataflow view", C_DD, MARKER_DD))
EXTERNAL_MACROS = [
    "recExt1RecomputeMedianS", "recExt2RefreshMedianMs",
    "recExt1SpeedupLOneArcFiveMedianCollegeMsg", "recExt1SpeedupLOneArcFiveMedianSynth",
    "recExt1SpeedupLOneArcFiveProbe",
    "recExt2WithheldFalseFreshIvm", "recExt2WithheldFalseFreshWatermark",
    "recExt2WithheldFalseFreshTgms", "recExt2UnanswerableMs", "recExt2UnanswerableBursts",
]


def _nearest_rank_p50(values: list[float]) -> float:
    """The harness's own percentile (nearest rank, k = round(0.5 * (n - 1))),
    which the committed ttf_p50_ms fields were produced with."""
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(0.5 * (len(s) - 1)))))]


def build_external_data() -> dict:
    _sha256sums_gate([EXTERNAL_NEO4J, EXTERNAL_IVM, EXTERNAL_CONTROL, EXTERNAL_CONTROL_BATCHES,
                      EXTERNAL_CONTROL_ARC5, EXTERNAL_WITHHELD_HOLD], EXTERNAL_SHA256SUMS)
    v = load_macro_values(EXTERNAL_MACROS)

    def per_cell(path):
        return {c["cell_id"]: c for c in
                json.loads(path.read_text(encoding="utf-8"))["summary"]["per_cell"]}

    neo, ivm = per_cell(EXTERNAL_NEO4J), per_cell(EXTERNAL_IVM)
    pre, arc5 = per_cell(EXTERNAL_CONTROL), per_cell(EXTERNAL_CONTROL_ARC5)
    assert set(pre) == set(arc5) and len(arc5) == 19
    batches: dict[str, list[dict]] = {}
    for b in load_jsonl(EXTERNAL_CONTROL_BATCHES):
        batches.setdefault(b["cell_id"], []).append(b)
    assert set(batches) == set(pre)

    # incremental refresh before the fifth arc = sum mode (check + refresh
    # per batch, nearest-rank p50); the as-committed check-only value is
    # never plotted. The probe cell already ran in sum mode, so its sum
    # reproduces its committed ttf_p50_ms exactly.
    sum_mode_l1 = {}
    for cid, bs in batches.items():
        arms = [b["arms"]["tgms-L1"] for b in bs]
        sum_mode_l1[cid] = _nearest_rank_p50([a["check_wall_ms"] + a["refresh_wall_ms"]
                                              for a in arms])
    assert {b["ttf_mode"] for b in batches[_EXT_PROBE]} == {"sum"}
    assert abs(sum_mode_l1[_EXT_PROBE] - pre[_EXT_PROBE]["arms"]["tgms-L1"]["ttf_p50_ms"]) < 1e-6

    # gate: the record medians the macros landed
    age_cells = {f"{s}-c3-{a}-n1000-s0" for s in ("collegemsg", "synth-iv-60k")
                 for a in ("recent", "hours", "days")}
    main_grid = [c for c in neo if c not in age_cells and c != _EXT_PROBE]
    assert len(main_grid) == 36
    assert_matches_macro("recExt1RecomputeMedianS",
                         statistics.median(neo[c]["refresh_wall_ms"]["median"]
                                           for c in main_grid) / 1000)
    assert_matches_macro("recExt2RefreshMedianMs",
                         statistics.median(ivm[c]["refresh_wall_ms"]["median"] for c in main_grid))
    nonprobe = [c for c in arc5 if c != _EXT_PROBE]
    assert_matches_macro("recExt1SpeedupLOneSumMedian",
                         statistics.median(neo[c]["refresh_wall_ms"]["median"] / sum_mode_l1[c]
                                           for c in nonprobe))
    bands = {"Recent": "c3-recent", "Hours": "c3-hours", "Days": "c3-days"}
    band_cells = {k: [c for c in nonprobe if tail in c] for k, tail in bands.items()}
    band_cells["Deep"] = [c for c in nonprobe if c.split("-")[-3] == "deep"]
    band_cells["None"] = [c for c in nonprobe if c.split("-")[-3] == "none"]
    for band, cells in band_cells.items():
        assert_matches_macro(f"recExt2RatioArcFive{band}", statistics.median(
            ivm[c]["refresh_wall_ms"]["median"] / arc5[c]["arms"]["tgms-L1"]["ttf_p50_ms"]
            for c in cells))
    assert_matches_macro("recExt2RatioArcFiveProbe",
                         ivm[_EXT_PROBE]["refresh_wall_ms"]["median"]
                         / arc5[_EXT_PROBE]["arms"]["tgms-L1"]["ttf_p50_ms"])

    rows, facets = [], []
    for store, n, title, tok in _EXT_FACETS:
        cells = sorted(c for c in arc5 if arc5[c]["store"] == store and arc5[c]["n_artifacts"] == n)
        assert len(cells) == (1 if n == 10_000 else 9), f"f_external: {store} N={n} cells"
        series = {
            ("incr", "before"): [sum_mode_l1[c] / 1000 for c in cells],
            ("incr", "after"): [arc5[c]["arms"]["tgms-L1"]["ttf_p50_ms"] / 1000 for c in cells],
            ("global", "before"): [pre[c]["arms"]["global-recompute"]["ttf_p50_ms"] / 1000
                                   for c in cells],
            ("global", "after"): [arc5[c]["arms"]["global-recompute"]["ttf_p50_ms"] / 1000
                                  for c in cells],
            ("neo", "after"): [neo[c]["refresh_wall_ms"]["median"] / 1000 for c in cells],
            ("dd", "after"): [ivm[c]["refresh_wall_ms"]["median"] / 1000 for c in cells],
        }
        sources = {
            ("incr", "before"): f"{relpath(EXTERNAL_CONTROL_BATCHES)}: p50(check_wall_ms + "
                                "refresh_wall_ms), tgms-L1",
            ("incr", "after"): f"{relpath(EXTERNAL_CONTROL_ARC5)}: arms.tgms-L1.ttf_p50_ms",
            ("global", "before"): f"{relpath(EXTERNAL_CONTROL)}: arms.global-recompute.ttf_p50_ms",
            ("global", "after"): f"{relpath(EXTERNAL_CONTROL_ARC5)}: "
                                 "arms.global-recompute.ttf_p50_ms",
            ("neo", "after"): f"{relpath(EXTERNAL_NEO4J)}: refresh_wall_ms.median",
            ("dd", "after"): f"{relpath(EXTERNAL_IVM)}: refresh_wall_ms.median "
                             "(refresh plus publish)",
        }
        medians = {k: statistics.median(vals) for k, vals in series.items()}
        if tok in ("CollegeMsg", "Synth"):
            assert_matches_macro(f"recExt1ControlArcFiveGlobalP50S{tok}",
                                 medians[("global", "after")])
            assert_matches_macro(f"recExt1RatioGrArcFiveMedian{tok}", statistics.median(
                a / b for a, b in zip(series[("neo", "after")], series[("global", "after")])))
            ratio_name = f"recExt1SpeedupLOneArcFiveMedian{tok}"
            ratio = statistics.median(
                a / b for a, b in zip(series[("neo", "after")], series[("incr", "after")]))
        else:
            assert_matches_macro("recExt1RatioGrArcFiveProbe",
                                 medians[("neo", "after")] / medians[("global", "after")])
            ratio_name = "recExt1SpeedupLOneArcFiveProbe"
            ratio = medians[("neo", "after")] / medians[("incr", "after")]
        assert_matches_macro(ratio_name, ratio)
        for (cfg, state), vals in series.items():
            for c, val in zip(cells, vals):
                rows.append(_row("a", f"{title} | {cfg} {state}", c, round(val, 6), "s",
                                 sources[(cfg, state)]))
            rows.append(_row("a", f"{title} | {cfg} {state}", "median", round(medians[(cfg, state)], 6),
                             "s", f"median over the {len(cells)} cell(s)"))
        rows.append(_row("a", f"{title} | Neo4j / incremental", "ratio", v[ratio_name], "x",
                         ratio_name))
        facets.append({"title": title, "series": series, "medians": medians,
                       "ratio": v[ratio_name]})

    hold = json.loads(EXTERNAL_WITHHELD_HOLD.read_text(encoding="utf-8"))["summary"]["withheld"]
    assert_matches_macro("recExt2UnanswerableMs", hold["ivm_f_watermark_hold_ms_median"])
    assert_matches_macro("recExt2UnanswerableBursts", hold["ivm_f_watermark_hold_bursts_median"])
    assert hold["ivm_f_epoch_false_fresh"] == v["recExt2WithheldFalseFreshIvm"]
    assert hold["ivm_f_watermark_false_fresh"] == v["recExt2WithheldFalseFreshWatermark"]
    for name in ("recExt2WithheldFalseFreshIvm", "recExt2WithheldFalseFreshWatermark",
                 "recExt2WithheldFalseFreshTgms", "recExt2UnanswerableMs",
                 "recExt2UnanswerableBursts"):
        rows.append(_row("b", name, "", v[name], "answers" if "FalseFresh" in name else
                         ("ms" if name.endswith("Ms") else "bursts"), name))
    return {"rows": rows, "facets": facets, "v": v}


def write_external_csv(data: dict) -> str:
    return _write_figure_csv("f_external", data["rows"])


def plot_external(data: dict) -> None:
    _require_mpl()
    v = data["v"]
    with plt.rc_context(PAPER_RC):
        fig = _new_fig("f_external")
        outer = fig.add_gridspec(1, 2, width_ratios=[4.6, 2.1])
        inner = outer[0].subgridspec(1, 3, wspace=0.08)
        facet_axes = [fig.add_subplot(inner[0, i]) for i in range(3)]
        rows_y = {cfg: 3 - i for i, (cfg, _l, _c, _m) in enumerate(_EXT_CONFIGS)}
        for k, (ax, facet) in enumerate(zip(facet_axes, data["facets"])):
            _human_time_axis(ax, "x", [1e-3, 1.0, 60.0, 3600.0])
            ax.set_xlim(1e-4, 1.2e4)
            ax.set_ylim(-0.6, 4.1)
            for cfg, _label, color, marker in _EXT_CONFIGS:
                y = rows_y[cfg]
                for state in ("before", "after"):
                    vals = facet["series"].get((cfg, state))
                    if vals is None:
                        continue
                    dot = C_BEFORE if state == "before" else color
                    if len(vals) > 1:
                        offs = [-0.13 + 0.26 * i / (len(vals) - 1) for i in range(len(vals))]
                        ax.plot(vals, [y + o for o in offs], marker=marker, ls="none",
                                ms=MS_CELL, mfc=dot, mec="none", alpha=CELL_ALPHA, zorder=2)
                med = facet["medians"]
                if (cfg, "before") in med:
                    _before_after(ax, med[(cfg, "before")], y, med[(cfg, "after")], y, color,
                                  marker)
                else:
                    _point(ax, med[(cfg, "after")], y, color, marker)
            x_incr = facet["medians"][("incr", "after")]
            x_neo = facet["medians"][("neo", "after")]
            yb = 3.6
            ax.plot([x_incr, x_incr, x_neo, x_neo], [yb - 0.12, yb, yb, yb - 0.12],
                    color="#000000", lw=0.5, zorder=3)
            _note(ax, (x_incr * x_neo) ** 0.5, yb + 0.05,
                  f"Neo4j ÷ incremental {facet['ratio']:.2f}×", color="#000000",
                  ha="center", va="bottom")
            ax.set_yticks([rows_y[c] for c, *_r in _EXT_CONFIGS],
                          [lab for _c, lab, *_r in _EXT_CONFIGS] if k == 0 else [])
            ax.tick_params(axis="y", length=0)
            if k:
                ax.spines["left"].set_visible(False)
            title = facet["title"]
            if k == 0:
                _panel_tag(ax, "a", title)
            else:
                ax.text(0.0, 1.0, title, transform=ax.transAxes, fontsize=BASE_PT, ha="left",
                        va="bottom")
            if k == 1:
                ax.set_xlabel("time per burst")
            if k == 2:
                for cfg, *_r in _EXT_CONFIGS:
                    scope = "persists (fsync)" if cfg in ("incr", "global") else "not persisted"
                    _note(ax, 1.3e-4, rows_y[cfg] + 0.3, scope, ha="left", va="center",
                          fontsize=6.0)

        # (b) one correction withheld
        axb = fig.add_subplot(outer[1])
        rows = (("dataflow view", v["recExt2WithheldFalseFreshIvm"], C_REF, None,
                 "old value, no signal"),
                ("dataflow +\nwatermark", v["recExt2WithheldFalseFreshWatermark"], C_DD, "/////",
                 "no answer until the watermark\npasses: hold "
                 f"{v['recExt2UnanswerableMs']:.0f} ms = {v['recExt2UnanswerableBursts']:g} "
                 "burst"),
                ("TGMS", v["recExt2WithheldFalseFreshTgms"], C_INCR, None,
                 "last verified value,\nmarked possibly stale"))
        top = v["recExt2WithheldFalseFreshIvm"]
        for i, (label, val, color, hatch, what) in enumerate(rows):
            y = 2 - i
            axb.barh(y, val, height=0.55, color=color, hatch=hatch, lw=0,
                     edgecolor="white" if hatch else color)
            _note(axb, val + top * 0.04, y, f"{val:.0f}",
                  color=C_REF if val > 0 else "#000000", ha="left", va="center")
            _note(axb, top * 1.25, y, what, ha="left", va="center", linespacing=0.95)
        axb.set_xlim(0, top * 3.1)
        axb.set_ylim(-0.6, 2.6)
        axb.spines["bottom"].set_bounds(0, top)
        axb.set_xticks([0, top], ["0", f"{top:.0f}"])
        axb.set_yticks([2, 1, 0], [r[0] for r in rows])
        axb.tick_params(axis="y", length=0)
        axb.spines["left"].set_visible(False)
        axb.set_xlabel("stale answers served without a signal", loc="left", labelpad=1.0)
        _panel_tag(axb, "b", "one correction withheld")

        _savefig_paper(fig, OUT_DIR / "f_external")


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
    # the paper figure set (STIX, natural size; see PAPER_RC)
    ("arcs", build_arcs_data, write_arcs_csv, plot_arcs, "f_arcs.csv"),
    ("scale", build_scale_data, write_scale_csv, plot_scale, "f_scale.csv"),
    ("ldbc", build_ldbc_data, write_ldbc_csv, plot_ldbc, "f_ldbc.csv"),
    ("open_cost", build_open_cost_data, write_open_cost_csv, plot_open_cost, "f_open_cost.csv"),
    ("correction_load", build_correction_load_data, write_correction_load_csv,
     plot_correction_load, "f_correction_load.csv"),
    ("durability", build_durability_data, write_durability_csv, plot_durability,
     "f_durability.csv"),
    ("external", build_external_data, write_external_csv, plot_external, "f_external.csv"),
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
    print(f"sys_paper_figures: {len(DELIVERABLES)} deliverables ({mode}) -> {OUT_DIR}")
    if not HAVE_MPL and not args.check and not args.csv_only:
        print("  note: matplotlib not installed in this interpreter -- PDF/PNG rendering "
              "was skipped; only CSVs were written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
