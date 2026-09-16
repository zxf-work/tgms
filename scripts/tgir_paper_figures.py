#!/usr/bin/env python
"""Generate the TGIR VLDB paper's evaluation figures from committed records.

Companion to ``scripts/tgir_paper_macros.py`` — read that file's docstring for
the "assert, do not trust" discipline this script follows for its own, much
smaller, set of claims. **Numbers discipline**: every plotted value is read
from a committed record under ``benchmarks/results-v1/`` (or, for the neo4j
comparison, ``benchmarks/ldbc-ref-v1/``); nothing here is hand-typed. A
handful of aggregates this script leans on for the figures (the admission
ceiling, the single false admission, the leaf/direct and compiled/kernel
ratios) are cross-checked against the source records' own precomputed fields
using the same ``eq``/``require`` pattern ``tgir_paper_macros.py`` uses — a
mismatch aborts generation rather than silently drawing a stale number.

Figures (each a single acmart column: 3.3in wide, <=2.2in tall, 8pt type, no
title — the caption lives in the surrounding LaTeX float — vector PDF,
grayscale-safe: series are told apart by marker shape/fill and hatching, not
color alone):

  1. fig-admission.pdf   estimated vs. measured cost, log-log, from
                         benchmarks/results-v1/e14-p3-frontier.json
  2. fig-cost.pdf        (a) leaf/direct overhead ratio per case, both
                         stores, from e14-p1-leaf-overhead-{bitcoinotc,
                         collegemsg}.json; (b) entity_history compiled/kernel
                         ratio at 1M/10M before/after the fix, from
                         e14-p2-compiled-{1m,10m}{,-after}.json
  3. fig-neo4j.pdf       per-template wall time, TGIR vs. Neo4j, from
                         benchmarks/ldbc-ref-v1/{tgms,neo4j}-campaign.json —
                         a placeholder PDF when those records do not exist yet

Also emits ``figures.json`` (the plotted values per figure, so a reviewer of
the source can trace every point back to its record) and prints a one-line
summary per figure.

Usage::

    <python-with-matplotlib> scripts/tgir_paper_figures.py \\
        --root /path/to/checkout --out /path/to/out/dir

``--root`` (default: this script's own repo root) resolves every source of
record. ``--out`` (default: ``ROOT/paper/tgir-vldb/figs``) is where the PDFs
and figures.json land.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

# matplotlib is imported lazily, inside `_require_matplotlib()` (called once
# from `main()`), rather than at module scope: this module is safe to import
# — e.g. for `import tgir_paper_figures` from a test, or a lint/collection
# pass — in an interpreter that never plots anything and does not carry
# matplotlib (the project .venv is exactly such an interpreter today). Only
# actually rendering a figure requires it.
plt = None  # populated by _require_matplotlib()


def _require_matplotlib():
    """Import matplotlib on first use and cache it in the module-level `plt`.
    Every plotting function below references that global, so calling this
    once at the top of `main()` — before any figure is built — is enough."""
    global plt
    if plt is not None:
        return plt
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as _plt
    except ModuleNotFoundError:
        sys.exit(
            "matplotlib is required to render the TGIR paper figures and is not "
            "importable in this interpreter.\n"
            "Do NOT `uv pip install matplotlib` into the project .venv.\n"
            "Instead, run this script with whatever interpreter already has "
            "matplotlib, or — only if /usr/bin/python3 itself lacks it — "
            "install it there with:\n"
            "    /usr/bin/python3 -m pip install --user matplotlib"
        )
    plt = _plt
    return plt


ROOT = Path(__file__).resolve().parent.parent
RESULTS_REL = Path("benchmarks/results-v1")
LDBC_REF_REL = Path("benchmarks/ldbc-ref-v1")
OUT_REL = Path("paper/tgir-vldb/figs")


# --------------------------------------------------------------------------
# verification helpers (same pattern as scripts/tgir_paper_macros.py)
# --------------------------------------------------------------------------

FAILURES: list[str] = []


def require(cond: bool, what: str) -> None:
    """Record a verification. A failed check aborts generation (see main())."""
    if not cond:
        FAILURES.append(what)


def eq(got, want, what: str):
    require(got == want, f"{what}: derived {got!r} != source {want!r}")
    return got


# --------------------------------------------------------------------------
# acmart single-column figure styling
# --------------------------------------------------------------------------

COLUMN_WIDTH_IN = 3.3
MAX_HEIGHT_IN = 2.2

STYLE = {
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "text.color": "black",
    "axes.edgecolor": "black",
    "axes.labelcolor": "black",
    "xtick.color": "black",
    "ytick.color": "black",
    "font.size": 8,
    "axes.titlesize": 8,
    "axes.labelsize": 8,
    "legend.fontsize": 6,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "axes.linewidth": 0.6,
    "pdf.fonttype": 42,   # embed real (vector) glyphs, not Type-3 bitmaps
    "ps.fonttype": 42,
}

# No creation/modification timestamp in the emitted PDFs — the only
# non-deterministic bytes matplotlib's pdf backend embeds by default.
PDF_METADATA = {"CreationDate": None, "ModDate": None}


def savefig(fig, path: Path) -> None:
    fig.savefig(path, metadata=PDF_METADATA)
    plt.close(fig)


def placeholder_pdf(path: Path, message: str) -> None:
    """A same-size PDF that just prints `message`, so the paper still builds
    when a figure's records are not landed yet."""
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(COLUMN_WIDTH_IN, MAX_HEIGHT_IN))
        ax.axis("off")
        ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=8,
                wrap=True, transform=ax.transAxes)
        fig.tight_layout(pad=0.3)
        savefig(fig, path)


# --------------------------------------------------------------------------
# fig-admission: estimated vs. measured cost (P3 frontier)
# --------------------------------------------------------------------------

def resolve_ceiling(manifest: dict) -> float:
    """The default admission ceiling. e14-p3-frontier.json's manifest carries
    it directly as `budget_ms`; ldbc-sf1-campaign.json's manifest instead
    nests it under `ceilings.time_est_ms` — support both spellings, in that
    order, and fall back to the frozen default (10,000 ms) only if neither is
    present."""
    if "budget_ms" in manifest:
        return float(manifest["budget_ms"])
    ceilings = manifest.get("ceilings", {})
    if "time_est_ms" in ceilings:
        return float(ceilings["time_est_ms"])
    return 10000.0


def build_admission(root: Path) -> dict:
    src = root / RESULTS_REL / "e14-p3-frontier.json"
    doc = json.loads(src.read_text(encoding="utf-8"))
    manifest = doc["manifest"]
    ceiling = resolve_ceiling(manifest)
    eq(ceiling, 10000.0, "e14-p3-frontier.json: default admission ceiling")

    scored = sorted(doc["arms"]["scored-bi"]["per_plan"], key=lambda r: r["plan_id"])
    char = sorted(doc["arms"]["characterization-interactive"]["per_plan"],
                  key=lambda r: r["plan_id"])
    eq(len(scored), 9, "scored-bi per_plan row count")
    eq(len(char), 11, "characterization-interactive per_plan row count")

    false_admissions = [r["plan_id"] for r in scored if r["classifier"] == "false-admission"]
    eq(false_admissions, ["BI18"], "scored-bi per_plan: the single false admission")

    zero_est = sorted(r["plan_id"] for r in (scored + char) if r["est_ms"] == 0)

    return {
        "source": str(src.relative_to(root)),
        "ceiling_ms": ceiling,
        "false_admission_plan": false_admissions[0],
        "scored": scored,
        "characterization": char,
        "zero_est_plans": zero_est,
    }


def plot_admission(data: dict, out_dir: Path) -> str:
    scored, char = data["scored"], data["characterization"]
    all_pts = scored + char
    positive = [v for r in all_pts for v in (r["est_ms"], r["actual_ms"]) if v > 0]
    floor = 10 ** (math.floor(math.log10(min(positive))) - 1)
    vmax = max(max(r["est_ms"], r["actual_ms"]) for r in all_pts) * 3

    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(COLUMN_WIDTH_IN, MAX_HEIGHT_IN))
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(floor * 0.5, vmax)
        ax.set_ylim(floor * 0.5, vmax)

        # identity line
        ax.plot([floor * 0.5, vmax], [floor * 0.5, vmax],
                color="0.5", linestyle="--", linewidth=0.8, zorder=1)

        # default admission ceiling: vertical dashed line
        ceiling = data["ceiling_ms"]
        ax.axvline(ceiling, color="0.2", linestyle=":", linewidth=0.9, zorder=1)
        ax.text(ceiling, vmax, " ceiling", fontsize=5.5, color="0.2",
                ha="left", va="top")

        # characterization set: hollow markers, unlabeled
        for r in char:
            x = r["est_ms"] if r["est_ms"] > 0 else floor
            ax.plot(x, r["actual_ms"], marker="o", markersize=3.2,
                    markerfacecolor="none", markeredgecolor="black",
                    markeredgewidth=0.7, linestyle="none", zorder=2)

        # scored set: filled markers, labeled by plan id; BI18 (the single
        # false admission) highlighted with a star and its own note
        for r in scored:
            x = r["est_ms"] if r["est_ms"] > 0 else floor
            is_fa = r["plan_id"] == data["false_admission_plan"]
            marker = "*" if is_fa else "s"
            size = 8 if is_fa else 4
            ax.plot(x, r["actual_ms"], marker=marker, markersize=size,
                    markerfacecolor="black", markeredgecolor="black",
                    linestyle="none", zorder=3)
            dx, dy = (-6, -9) if is_fa else (3, 2)
            ha = "right" if is_fa else "left"
            label = f"{r['plan_id']} (false admission)" if is_fa else r["plan_id"]
            ax.annotate(label, (x, r["actual_ms"]), fontsize=4.3,
                       xytext=(dx, dy), textcoords="offset points", ha=ha)

        ax.set_xlabel("estimated cost (ms)")
        ax.set_ylabel("measured cost (ms)")
        note = ("○ characterization   ■ scored   ★ false admission\n"
                f"est=0 shown at floor ({', '.join(data['zero_est_plans'])})")
        ax.text(0.02, 0.02, note, transform=ax.transAxes, fontsize=4.6,
                va="bottom", ha="left")
        fig.tight_layout(pad=0.3)
        savefig(fig, out_dir / "fig-admission.pdf")

    return (f"fig-admission.pdf: {len(scored)} scored + {len(char)} "
            f"characterization points; ceiling={ceiling:.0f}ms; false "
            f"admission={data['false_admission_plan']}; "
            f"zero-est plans={','.join(data['zero_est_plans'])}")


# --------------------------------------------------------------------------
# fig-cost: (a) leaf/direct overhead, (b) compiled/kernel before/after
# --------------------------------------------------------------------------

def build_cost(root: Path) -> dict:
    bitcoin_src = root / RESULTS_REL / "e14-p1-leaf-overhead-bitcoinotc.json"
    college_src = root / RESULTS_REL / "e14-p1-leaf-overhead-collegemsg.json"
    bitcoin = json.loads(bitcoin_src.read_text(encoding="utf-8"))
    college = json.loads(college_src.read_text(encoding="utf-8"))
    bcases = {r["case"]: r for r in bitcoin["rows"]}
    ccases = {r["case"]: r for r in college["rows"]}
    eq(sorted(bcases), sorted(ccases), "leaf-overhead: same case set for both stores")
    cases = sorted(bcases)

    leaf_ratio: dict[str, dict[str, float]] = {}
    for c in cases:
        b, g = bcases[c], ccases[c]
        rb = b["leaf"]["p50_ms"] / b["direct"]["p50_ms"]
        eq(round(rb, 9), round(b["leaf_over_direct"], 9),
           f"{c}: bitcoinotc leaf/direct p50 ratio recompute")
        rg = g["leaf"]["p50_ms"] / g["direct"]["p50_ms"]
        eq(round(rg, 9), round(g["leaf_over_direct"], 9),
           f"{c}: collegemsg leaf/direct p50 ratio recompute")
        leaf_ratio[c] = {"bitcoinotc": b["leaf_over_direct"],
                         "collegemsg": g["leaf_over_direct"]}

    def entity_history_ratio(path: Path) -> float:
        doc = json.loads(path.read_text(encoding="utf-8"))
        row = next(r for r in doc["rows"] if r["compiled"]["op"] == "entity_history")
        recompute = row["compiled"]["p50_ms"] / row["kernel"]["p50_ms"]
        eq(round(recompute, 6), round(row["compiled_over_kernel"], 6),
           f"{path.name}: entity_history compiled/kernel ratio recompute")
        return row["compiled_over_kernel"]

    compiled = {
        "1m_before": entity_history_ratio(root / RESULTS_REL / "e14-p2-compiled-1m.json"),
        "1m_after": entity_history_ratio(root / RESULTS_REL / "e14-p2-compiled-1m-after.json"),
        "10m_before": entity_history_ratio(root / RESULTS_REL / "e14-p2-compiled-10m.json"),
        "10m_after": entity_history_ratio(root / RESULTS_REL / "e14-p2-compiled-10m-after.json"),
    }

    return {
        "sources": {
            "bitcoinotc": str(bitcoin_src.relative_to(root)),
            "collegemsg": str(college_src.relative_to(root)),
            "compiled_1m": "benchmarks/results-v1/e14-p2-compiled-1m.json",
            "compiled_1m_after": "benchmarks/results-v1/e14-p2-compiled-1m-after.json",
            "compiled_10m": "benchmarks/results-v1/e14-p2-compiled-10m.json",
            "compiled_10m_after": "benchmarks/results-v1/e14-p2-compiled-10m-after.json",
        },
        "cases": cases,
        "leaf_ratio": leaf_ratio,
        "compiled_kernel": compiled,
    }


def plot_cost(data: dict, out_dir: Path) -> str:
    cases = data["cases"]
    with plt.rc_context(STYLE):
        fig, (axa, axb) = plt.subplots(
            1, 2, figsize=(COLUMN_WIDTH_IN, MAX_HEIGHT_IN),
            gridspec_kw={"width_ratios": [1.3, 1]})

        # (a) leaf/direct overhead ratio, both stores, cases sorted by name
        y = list(range(len(cases)))
        axa.axvspan(0.8, 1.2, color="0.85", zorder=0, linewidth=0)
        bvals = [data["leaf_ratio"][c]["bitcoinotc"] for c in cases]
        gvals = [data["leaf_ratio"][c]["collegemsg"] for c in cases]
        axa.plot(bvals, y, marker="o", markersize=3, linestyle="none",
                 markerfacecolor="black", markeredgecolor="black",
                 label="bitcoinotc")
        axa.plot(gvals, y, marker="^", markersize=3.4, linestyle="none",
                 markerfacecolor="none", markeredgecolor="black",
                 markeredgewidth=0.7, label="collegemsg")
        axa.set_yticks(y)
        axa.set_yticklabels(cases, fontsize=4.6)
        axa.set_xlabel("leaf/direct p50 ratio")
        axa.invert_yaxis()
        axa.legend(loc="upper right", frameon=False, fontsize=4.8,
                   handletextpad=0.3, borderaxespad=0.1)

        # (b) entity_history compiled/kernel ratio, 1M/10M, before/after
        labels = ["1M\npre", "1M\npost", "10M\npre", "10M\npost"]
        vals = [data["compiled_kernel"]["1m_before"], data["compiled_kernel"]["1m_after"],
                data["compiled_kernel"]["10m_before"], data["compiled_kernel"]["10m_after"]]
        xb = list(range(len(vals)))
        axb.bar(xb, vals, color="0.3", edgecolor="black", width=0.55, linewidth=0.6)
        axb.set_yscale("log")
        axb.axhline(3, color="black", linestyle="--", linewidth=0.8)
        axb.text(len(vals) - 1, 3, " 3× bound", fontsize=4.6, va="bottom", ha="right")
        axb.set_xlim(-0.6, len(vals) - 0.4)
        axb.set_xticks(xb)
        axb.set_xticklabels(labels, fontsize=4.8, linespacing=1.3)
        axb.set_ylabel("compiled/kernel ratio")

        fig.tight_layout(pad=0.3, w_pad=1.0)
        savefig(fig, out_dir / "fig-cost.pdf")

    ck = data["compiled_kernel"]
    return (f"fig-cost.pdf: panel (a) {len(cases)} cases x2 stores "
            f"(band 0.8-1.2 shaded); panel (b) entity_history compiled/kernel "
            f"1M={ck['1m_before']:.1f}->{ck['1m_after']:.2f}, "
            f"10M={ck['10m_before']:.1f}->{ck['10m_after']:.2f} (3x bound)")


# --------------------------------------------------------------------------
# fig-neo4j: per-template wall time, TGIR vs. Neo4j (LDBC reference)
# --------------------------------------------------------------------------

def _median_or_scalar(values, scalar):
    """Prefer the median of a `*_runs` list when present and non-empty;
    otherwise the scalar field. Both readers below share this rule so a
    campaign carrying either shape (or both) is read the same way."""
    if values:
        return statistics.median(values)
    return scalar


def read_tgms_campaign(path: Path) -> dict[str, float] | None:
    """The tgms side of the LDBC reference comparison, in the same record
    shape as benchmarks/results-v1/ldbc-sf1-campaign.json: a top-level
    `records` list, each carrying `plan_id`, `ms` (and, like that file,
    possibly `ms_all`, a list of individual timed runs), `outcome`. Returns
    plan_id -> wall time in seconds, or None if the file does not exist."""
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    records = doc.get("records") if isinstance(doc, dict) else doc
    if not isinstance(records, list):
        return None
    out: dict[str, float] = {}
    for r in records:
        pid = r.get("plan_id")
        if not pid:
            continue
        ms = _median_or_scalar(r.get("ms_all"), r.get("ms"))
        if ms is not None:
            out[pid] = ms / 1000.0
    return out


def read_neo4j_campaign(path: Path) -> dict[str, float] | None:
    """The neo4j side. scripts/ldbc_reference_run.py's own per-plan record
    (write_ref_exports' `ref-{pid}.json`) carries `plan_id` and `wall_s`; a
    campaign aggregating those into one file could reasonably do so either
    as a `records`/`rows` list of such dicts or as a plan_id -> dict mapping,
    and either might additionally carry `wall_s_runs` (a list of repeated
    timed runs) instead of, or alongside, a single `wall_s` scalar — this
    reader tolerates all of those shapes and takes the median of
    `wall_s_runs` when present. Returns None if the file does not exist."""
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    records = None
    if isinstance(doc, list):
        records = doc
    elif isinstance(doc, dict):
        for key in ("records", "rows"):
            if isinstance(doc.get(key), list):
                records = doc[key]
                break
        if records is None and all(isinstance(v, dict) for v in doc.values()):
            # a plan_id -> record mapping
            records = [dict(v, plan_id=v.get("plan_id", k)) for k, v in doc.items()]
    if records is None:
        return None
    out: dict[str, float] = {}
    for r in records:
        pid = r.get("plan_id")
        if not pid:
            continue
        wall = _median_or_scalar(r.get("wall_s_runs"), r.get("wall_s"))
        if wall is not None:
            out[pid] = wall
    return out


def build_neo4j(root: Path) -> dict:
    tgms_src = root / LDBC_REF_REL / "tgms-campaign.json"
    neo4j_src = root / LDBC_REF_REL / "neo4j-campaign.json"
    tgms = read_tgms_campaign(tgms_src)
    neo4j = read_neo4j_campaign(neo4j_src)
    common = sorted(set(tgms) & set(neo4j)) if (tgms and neo4j) else []
    return {
        "tgms_source": str(tgms_src.relative_to(root)),
        "neo4j_source": str(neo4j_src.relative_to(root)),
        "available": bool(common),
        "tgms": tgms or {},
        "neo4j": neo4j or {},
        "common_plan_ids": common,
    }


def plot_neo4j(data: dict, out_dir: Path) -> str:
    if not data["available"]:
        placeholder_pdf(out_dir / "fig-neo4j.pdf",
                        "[NEED EXPERIMENTAL RESULT: ldbc-ref-v1]")
        return (f"fig-neo4j.pdf: PLACEHOLDER (no records at "
                f"{data['tgms_source']} / {data['neo4j_source']})")

    plan_ids = data["common_plan_ids"]
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(COLUMN_WIDTH_IN, MAX_HEIGHT_IN))
        y = list(range(len(plan_ids)))
        h = 0.34
        tgms_vals = [data["tgms"][p] for p in plan_ids]
        neo4j_vals = [data["neo4j"][p] for p in plan_ids]
        ax.barh([i + h / 2 for i in y], tgms_vals, height=h, color="black",
               edgecolor="black", label="TGIR")
        ax.barh([i - h / 2 for i in y], neo4j_vals, height=h, color="none",
               edgecolor="black", hatch="///", label="Neo4j")
        ax.set_xscale("log")
        ax.set_yticks(y)
        ax.set_yticklabels(plan_ids, fontsize=5)
        ax.set_xlabel("wall time (s)")
        ax.invert_yaxis()
        ax.legend(loc="lower right", frameon=False, fontsize=6)
        fig.tight_layout(pad=0.3)
        savefig(fig, out_dir / "fig-neo4j.pdf")

    return f"fig-neo4j.pdf: {len(plan_ids)} paired templates, TGIR vs Neo4j"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    _require_matplotlib()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT, metavar="PATH",
                    help="repository root to resolve every source of record "
                         "against (default: this script's own checkout)")
    ap.add_argument("--out", type=Path, default=None, metavar="DIR",
                    help="output directory for the PDFs and figures.json "
                         "(default: ROOT/paper/tgir-vldb/figs)")
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"--root {root} is not a directory", file=sys.stderr)
        return 1
    out_dir = (args.out if args.out is not None else root / OUT_REL)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    admission = build_admission(root)
    cost = build_cost(root)
    neo4j = build_neo4j(root)

    if FAILURES:
        print("tgir_paper_figures: refusing to write, failed checks:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    summaries = [
        plot_admission(admission, out_dir),
        plot_cost(cost, out_dir),
        plot_neo4j(neo4j, out_dir),
    ]

    figures_json = {
        "fig-admission": {
            "source": admission["source"],
            "ceiling_ms": admission["ceiling_ms"],
            "false_admission_plan": admission["false_admission_plan"],
            "zero_est_plans": admission["zero_est_plans"],
            "scored": admission["scored"],
            "characterization": admission["characterization"],
        },
        "fig-cost": {
            "sources": cost["sources"],
            "cases": cost["cases"],
            "leaf_ratio": cost["leaf_ratio"],
            "compiled_kernel": cost["compiled_kernel"],
        },
        "fig-neo4j": {
            "tgms_source": neo4j["tgms_source"],
            "neo4j_source": neo4j["neo4j_source"],
            "available": neo4j["available"],
            "tgms": neo4j["tgms"],
            "neo4j": neo4j["neo4j"],
            "common_plan_ids": neo4j["common_plan_ids"],
        },
    }
    (out_dir / "figures.json").write_text(
        json.dumps(figures_json, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for line in summaries:
        print(line)
    print(f"tgir_paper_figures: wrote 3 PDFs + figures.json -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
