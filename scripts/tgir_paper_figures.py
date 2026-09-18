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
                         benchmarks/ldbc-ref-v1/timings-2026-09-18.json, the
                         revision of record (falls back to the superseded
                         timings-2026-09-17.json only if 09-18 is not present
                         in this checkout) — paired horizontal bars on a log
                         axis, rows grouped BI/IC/IS; the Neo4j side is the
                         median of that record's three timed runs (the
                         warm-up excluded) and the TGIR side its `ms` in
                         seconds, each TGIR value cross-checked against
                         tgms-campaign-ldbc-ref-v1.json; a template whose TGIR
                         side timed out is drawn at the ceiling the matching
                         manifest-*.json pre-registers, with its own hatch,
                         never as a measurement.  Still a placeholder PDF when
                         neither revision's record exists.

  The two sides' wall times are NOT a speed ratio and the figure never draws
  one: they are different rep counts against different systems, and TGIR's
  `ms` excludes a per-plan store open the warm Neo4j server has no analogue
  for (the timings record's own `protocol.note`).  Both are plotted per
  side, on a shared axis, and left to the caption to qualify.

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
import re
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
Patch = None  # ditto: matplotlib.patches.Patch, for hand-built legends


def _require_matplotlib():
    """Import matplotlib on first use and cache it in the module-level `plt`.
    Every plotting function below references that global, so calling this
    once at the top of `main()` — before any figure is built — is enough."""
    global plt, Patch
    if plt is not None:
        return plt
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as _plt
        from matplotlib.patches import Patch as _Patch
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
    Patch = _Patch
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

REF_TIMINGS_REL_1809 = Path("timings-2026-09-18.json")
REF_TIMINGS_REL_1709 = Path("timings-2026-09-17.json")
REF_MANIFEST_REL_1809 = Path("manifest-2026-09-18.json")
REF_MANIFEST_REL_1709 = Path("manifest-2026-09-17.json")
REF_CAMPAIGN_REL = Path("tgms-campaign-ldbc-ref-v1.json")


def resolve_ref_source(root: Path, rel_1809: Path, rel_1709: Path) -> Path:
    """The revision of record is -2026-09-18; -2026-09-17 is the superseded
    interim revision (7 of the 24 templates had an invalid reference side
    there — see benchmarks/ldbc-ref-v1/README.md §5), used only when 09-18 is
    not present in this checkout."""
    p1809 = root / LDBC_REF_REL / rel_1809
    if p1809.exists():
        return p1809
    return root / LDBC_REF_REL / rel_1709

# The three LDBC query families, in the order the paper presents them.  The
# figure groups its rows by family and orders each family by template number
# (BI3 before BI10), which neither a lexicographic nor an insertion order gives.
REF_GROUPS = ("BI", "IC", "IS")
_TEMPLATE_RE = re.compile(r"^([A-Z]+)(\d+)")


def ref_sort_key(template: str) -> tuple[int, int, str]:
    m = _TEMPLATE_RE.match(template)
    if not m:
        return (len(REF_GROUPS), 0, template)
    family, number = m.group(1), int(m.group(2))
    index = REF_GROUPS.index(family) if family in REF_GROUPS else len(REF_GROUPS)
    return (index, number, template)


def read_ref_timings(path: Path) -> dict | None:
    """The executed reference run's paired wall times,
    ``benchmarks/ldbc-ref-v1/timings-2026-09-18.json`` (or the superseded
    ``-2026-09-17.json``, see ``resolve_ref_source``).

    Each entry of ``templates`` carries the Neo4j side as a warm-up plus three
    timed executions (``neo4j.wall_s.{warmup,t1,t2,t3}``, seconds) and the TGIR
    side as ``tgir.{ms,outcome}``.  **The warm-up is excluded** and the three
    timed runs are reduced by their median, which is what the figure plots;
    TGIR's ``ms`` is converted to seconds so both sides share one axis.  A
    template whose TGIR side did not complete carries ``ms: null`` and is
    returned separately, by outcome, rather than silently dropped.

    Returns None when the record does not exist (the figure then falls back to
    its placeholder), so a checkout without the run still builds the paper.
    """
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    templates = doc.get("templates") if isinstance(doc, dict) else None
    if not isinstance(templates, list):
        return None
    neo4j: dict[str, float] = {}
    tgms: dict[str, float] = {}
    incomplete: dict[str, str] = {}
    for entry in templates:
        template = entry.get("template")
        if not template:
            continue
        wall = (entry.get("neo4j") or {}).get("wall_s") or {}
        timed = [wall[k] for k in ("t1", "t2", "t3") if wall.get(k) is not None]
        if timed:
            neo4j[template] = statistics.median(timed)
        tgir = entry.get("tgir") or {}
        if tgir.get("ms") is not None:
            tgms[template] = tgir["ms"] / 1000.0
        else:
            incomplete[template] = tgir.get("outcome") or "UNKNOWN"
    return {"neo4j": neo4j, "tgms": tgms, "incomplete": incomplete}


def read_ref_ceiling(path: Path) -> float | None:
    """The pre-registered TGIR ceiling a timed-out template is drawn at: the
    manifest's bypass ceiling plus its store-open allowance, in seconds
    (unchanged between the 09-17 and 09-18 revisions).  A timeout is not a
    measurement, so the bar is
    drawn *at* the ceiling and hatched differently rather than being given a
    number the run never produced."""
    if not path.exists():
        return None
    ceilings = (json.loads(path.read_text(encoding="utf-8"))
                .get("protocol", {}).get("ceilings", {}))
    if "tgir_bypass_ceiling_s" not in ceilings:
        return None
    return float(ceilings["tgir_bypass_ceiling_s"]
                 + ceilings.get("tgir_child_open_allowance_s", 0))


def cross_check_tgms(campaign_path: Path, tgms: dict[str, float],
                     timings_path: Path) -> None:
    """The TGIR side also exists as its own campaign record, in the same shape
    as ``ldbc-sf1-campaign.json``.  Plotting one and citing the other is the
    class of drift this script exists to prevent, so every plotted TGIR value
    is asserted equal to that record's own ``ms``."""
    if not campaign_path.exists():
        return
    doc = json.loads(campaign_path.read_text(encoding="utf-8"))
    records = {r["plan_id"]: r for r in doc.get("records", []) if r.get("plan_id")}
    timings = json.loads(timings_path.read_text(encoding="utf-8"))
    plan_of = {e["template"]: (e.get("tgir_plan") or "").removesuffix(".json")
               for e in timings.get("templates", [])}
    for template, seconds in tgms.items():
        plan_id = plan_of.get(template)
        rec = records.get(plan_id)
        require(rec is not None,
                f"{campaign_path.name}: no record for {template}'s plan {plan_id!r}")
        if rec is not None:
            eq(round(rec["ms"] / 1000.0, 9), round(seconds, 9),
               f"{template}: plotted TGIR seconds against the campaign record's ms")


def build_neo4j(root: Path) -> dict:
    timings_src = resolve_ref_source(root, REF_TIMINGS_REL_1809, REF_TIMINGS_REL_1709)
    manifest_src = resolve_ref_source(root, REF_MANIFEST_REL_1809, REF_MANIFEST_REL_1709)
    campaign_src = root / LDBC_REF_REL / REF_CAMPAIGN_REL
    timings = read_ref_timings(timings_src)
    ceiling = read_ref_ceiling(manifest_src)

    if timings is None:
        return {
            "timings_source": str(timings_src.relative_to(root)),
            "manifest_source": str(manifest_src.relative_to(root)),
            "campaign_source": str(campaign_src.relative_to(root)),
            "available": False,
            "ceiling_s": None,
            "tgms": {},
            "neo4j": {},
            "timed_out": [],
            "common_plan_ids": [],
        }

    neo4j, tgms = timings["neo4j"], timings["tgms"]
    timed_out = sorted((t for t, outcome in timings["incomplete"].items()
                        if outcome == "TIMEOUT" and t in neo4j), key=ref_sort_key)
    require(not timed_out or ceiling is not None,
            f"{manifest_src.name}: a timed-out template needs the ceiling to draw it at")
    cross_check_tgms(campaign_src, tgms, timings_src)

    plan_ids = sorted((t for t in neo4j if t in tgms or t in timed_out), key=ref_sort_key)
    require(bool(plan_ids), f"{timings_src.name}: no template has both sides")
    return {
        "timings_source": str(timings_src.relative_to(root)),
        "manifest_source": str(manifest_src.relative_to(root)),
        "campaign_source": str(campaign_src.relative_to(root)),
        "available": True,
        "ceiling_s": ceiling,
        "tgms": {t: tgms[t] for t in plan_ids if t in tgms},
        "neo4j": {t: neo4j[t] for t in plan_ids},
        "timed_out": timed_out,
        "common_plan_ids": plan_ids,
    }


def plot_neo4j(data: dict, out_dir: Path) -> str:
    if not data["available"]:
        placeholder_pdf(out_dir / "fig-neo4j.pdf",
                        "[NEED EXPERIMENTAL RESULT: ldbc-ref-v1]")
        return (f"fig-neo4j.pdf: PLACEHOLDER (no record at "
                f"{data['timings_source']})")

    plan_ids = data["common_plan_ids"]
    timed_out = set(data["timed_out"])
    ceiling = data["ceiling_s"]
    floor = min([v for v in data["neo4j"].values()] + list(data["tgms"].values())) / 2

    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(COLUMN_WIDTH_IN, MAX_HEIGHT_IN))
        y = list(range(len(plan_ids)))
        h = 0.38
        for i, template in enumerate(plan_ids):
            if template in timed_out:
                ax.barh(i + h / 2, ceiling - floor, left=floor, height=h,
                        color="none", edgecolor="black", hatch="xxx", linewidth=0.5)
            else:
                ax.barh(i + h / 2, data["tgms"][template] - floor, left=floor,
                        height=h, color="black", edgecolor="black", linewidth=0.5)
            ax.barh(i - h / 2, data["neo4j"][template] - floor, left=floor,
                    height=h, color="none", edgecolor="black", hatch="///",
                    linewidth=0.5)
        # a hairline between families, so BI / IC / IS read as three blocks
        for i in range(1, len(plan_ids)):
            if ref_sort_key(plan_ids[i])[0] != ref_sort_key(plan_ids[i - 1])[0]:
                ax.axhline(i - 0.5, color="black", linewidth=0.4, linestyle=":")
        if ceiling is not None:
            ax.axvline(ceiling, color="black", linewidth=0.5, linestyle="--")
        ax.set_xscale("log")
        ax.set_xlim(left=floor)
        ax.set_yticks(y)
        ax.set_yticklabels(plan_ids, fontsize=4)
        ax.set_xlabel("wall time (s, log scale)")
        ax.invert_yaxis()
        handles = [
            Patch(facecolor="black", edgecolor="black", label="TGIR"),
            Patch(facecolor="none", edgecolor="black", hatch="///", label="Neo4j"),
        ]
        if timed_out:
            handles.append(Patch(facecolor="none", edgecolor="black", hatch="xxx",
                                 label=f"TGIR timeout ({ceiling:.0f} s)"))
        # Above the axes, in one row, rather than inside them: every row of
        # this figure carries a bar from the left edge, so an in-axes legend
        # would sit on top of one and clip its length where the reader reads
        # its value.
        ax.legend(handles=handles, ncol=len(handles), frameon=False, fontsize=5,
                  loc="lower left", bbox_to_anchor=(0, 1.0, 1, 0.1), mode="expand",
                  borderaxespad=0.2, handlelength=1.4, handletextpad=0.4)
        fig.tight_layout(pad=0.3)
        savefig(fig, out_dir / "fig-neo4j.pdf")

    per_family = ", ".join(
        f"{g} {sum(1 for t in plan_ids if t.startswith(g))}" for g in REF_GROUPS)
    return (f"fig-neo4j.pdf: {len(plan_ids)} paired templates ({per_family}), "
            f"Neo4j median of 3 timed runs vs TGIR, "
            f"{len(timed_out)} TGIR timeout(s) drawn at the {ceiling:.0f} s ceiling")


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
            "timings_source": neo4j["timings_source"],
            "manifest_source": neo4j["manifest_source"],
            "campaign_source": neo4j["campaign_source"],
            "available": neo4j["available"],
            "ceiling_s": neo4j["ceiling_s"],
            "tgms": neo4j["tgms"],
            "neo4j": neo4j["neo4j"],
            "timed_out": neo4j["timed_out"],
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
