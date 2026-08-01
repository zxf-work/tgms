"""Figure for docs/eval_bitemporal.md: the §13 density curve.

Reads the raw run record (benchmarks/results-v1/eval-1m-bitemporal.json)
and emits docs/fig_bitemporal_density.svg — so the figure regenerates from
the receipts, never from retyped numbers.

Two panels:
  A — current-query latency multiplier (full p50 / current-only p50) per
      correction density. The multiplier is the honest §13 metric: the
      stripped store is the 1.0x baseline by construction.
  B — storage overhead (full vs current-only store bytes, same density).

Densities are placed at ordered positions, not on a linear scale — the
sweep is exponential and the axis label says so.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    ROOT / "benchmarks" / "results-v1" / "eval-1m-bitemporal.json"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else \
    ROOT / "docs" / "fig_bitemporal_density.svg"

# Categorical slots in the palette's fixed order (validated set; identity
# follows the query, never its rank in a given chart).
SERIES = [
    ("nbr.evolution", "#2a78d6"),
    ("coactive.narrow", "#eb6834"),
    ("series.count", "#1baf7a"),
    ("burst.zscore", "#eda100"),
    ("motif.filtered", "#e87ba4"),
]
#: The remaining current-belief queries: multipliers stay ~1x; drawn muted
#: as one visual group so the flat floor is present without eight legends.
MUTED = ["snap.hop2", "diff.global", "hist.single", "paths.k",
         "reach.window", "resolve.substr"]

INK, INK2, GRID, MUTE = "#0b0b0b", "#52514e", "#eceae6", "#b5b4ad"
SURFACE = "#fcfcfb"


def p50s(results: list[dict]) -> dict[str, float]:
    return {r["query"]: r["p50_ms"] for r in results if r["ok"]}


def load() -> tuple[list[float], dict[str, list[float]], list[float]]:
    data = json.loads(SRC.read_text())
    pcts, ratios, storage = [], {q: [] for q, _ in SERIES + [(m, "") for m in MUTED]}, []
    for rec in data["densities"]:
        pcts.append(rec["pct"])
        full, curr = p50s(rec["full"]["results"]), p50s(rec["current_only"]["results"])
        for q in ratios:
            ratios[q].append(full[q] / curr[q] if q in full and q in curr else None)
        f = rec["full"]["storage"]["store_total"]
        c = rec["current_only"]["storage"]["store_total"]
        storage.append((f / c - 1.0) * 100.0)
    return pcts, ratios, storage


def fmt_pct(p: float) -> str:
    return f"{p:g}"


def polyline(xs, ys, color, width, dash="") -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linejoin="round" '
            f'stroke-linecap="round"{d}/>')


def main() -> int:
    pcts, ratios, storage = load()
    n = len(pcts)

    W, H = 780, 360
    ax = dict(x0=52, x1=490, y0=300, y1=64)            # panel A plot box
    bx = dict(x0=572, x1=762, y0=300, y1=64)           # panel B plot box

    def xa(i):  # ordered positions, panel A
        return ax["x0"] + (i + 0.5) * (ax["x1"] - ax["x0"]) / n

    ymax = max(v for vs in ratios.values() for v in vs if v) * 1.12
    def ya(v):
        return ax["y0"] - (v / ymax) * (ax["y0"] - ax["y1"])

    smax = max(max(storage) * 1.25, 1.0)
    def yb(v):
        return bx["y0"] - (v / smax) * (bx["y0"] - bx["y1"])

    e = []
    e.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'font-family="system-ui, -apple-system, Segoe UI, sans-serif">')
    e.append(f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>')

    # ---- panel A: grid, parity line, series ------------------------------ #
    e.append(f'<text x="{ax["x0"]}" y="24" font-size="13" fill="{INK}" '
             f'font-weight="600">Current-query latency multiplier, full ÷ current-only</text>')
    e.append(f'<text x="{ax["x0"]}" y="40" font-size="11" fill="{INK2}">'
             f'1M events, p50 over 30 reps — the stripped store is the 1× baseline</text>')

    step = 1 if ymax <= 9 else 2
    g = 0.0
    while g <= ymax:
        y = ya(g)
        e.append(f'<line x1="{ax["x0"]}" y1="{y:.1f}" x2="{ax["x1"]}" y2="{y:.1f}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        e.append(f'<text x="{ax["x0"] - 6}" y="{y + 3.5:.1f}" font-size="10.5" '
                 f'fill="{INK2}" text-anchor="end">{g:g}×</text>')
        g += step
    # parity: what the stripped store costs, by definition
    e.append(polyline([ax["x0"], ax["x1"]], [ya(1), ya(1)], INK2, 1, dash="4 4"))
    e.append(f'<text x="{ax["x1"] - 2}" y="{ya(1) - 5:.1f}" font-size="10.5" '
             f'fill="{INK2}" text-anchor="end">parity (current-only)</text>')

    # a None ratio is a one-sided guardrail refusal (recorded as `partial`
    # in the JSON): the line simply stops at its last measured point
    def present(q):
        return [(xa(i), ya(v)) for i, v in enumerate(ratios[q]) if v is not None]

    for q in MUTED:
        pts = present(q)
        e.append(polyline([x for x, _ in pts], [y for _, y in pts], MUTE, 1.2))
    for q, color in SERIES:
        pts = present(q)
        e.append(polyline([x for x, _ in pts], [y for _, y in pts], color, 2))
        for x, y in pts:
            e.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" '
                     f'stroke="{SURFACE}" stroke-width="2"/>')
    # direct labels on the two leaders (identity also in the legend)
    for q in ("nbr.evolution", "coactive.narrow"):
        e.append(f'<text x="{xa(n - 1) + 8:.1f}" y="{ya(ratios[q][-1]) + 3.5:.1f}" '
                 f'font-size="10.5" fill="{INK}">{q}</text>')

    for i, p in enumerate(pcts):
        e.append(f'<text x="{xa(i):.1f}" y="{ax["y0"] + 16}" font-size="10.5" '
                 f'fill="{INK2}" text-anchor="middle">{fmt_pct(p)}</text>')
    e.append(f'<text x="{(ax["x0"] + ax["x1"]) / 2:.0f}" y="{ax["y0"] + 34}" '
             f'font-size="11" fill="{INK2}" text-anchor="middle">'
             f'correction density, % of events (ordered positions, not linear)</text>')

    # legend row
    lx = ax["x0"]
    for q, color in SERIES + [("others (≈1×)", MUTE)]:
        e.append(f'<rect x="{lx}" y="48" width="9" height="9" rx="2" fill="{color}"/>')
        w = 6.2 * len(q) + 20
        e.append(f'<text x="{lx + 13}" y="56" font-size="10.5" fill="{INK2}">{q}</text>')
        lx += w

    # ---- panel B: storage overhead --------------------------------------- #
    e.append(f'<text x="{bx["x0"]}" y="24" font-size="13" fill="{INK}" '
             f'font-weight="600">Storage overhead</text>')
    e.append(f'<text x="{bx["x0"]}" y="40" font-size="11" fill="{INK2}">'
             f'full vs current-only, %</text>')
    g = 0.0
    gstep = max(1.0, round(smax / 5))
    while g <= smax:
        y = yb(g)
        e.append(f'<line x1="{bx["x0"]}" y1="{y:.1f}" x2="{bx["x1"]}" y2="{y:.1f}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        e.append(f'<text x="{bx["x0"] - 6}" y="{y + 3.5:.1f}" font-size="10.5" '
                 f'fill="{INK2}" text-anchor="end">{g:g}</text>')
        g += gstep
    bw = min(18.0, (bx["x1"] - bx["x0"]) / n - 8)
    for i, v in enumerate(storage):
        x = bx["x0"] + (i + 0.5) * (bx["x1"] - bx["x0"]) / n - bw / 2
        y = yb(v)
        h = bx["y0"] - y
        r = min(4.0, bw / 2, max(h, 0.01))
        e.append(f'<path d="M{x:.1f},{bx["y0"]:.1f} V{y + r:.1f} '
                 f'Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bw - r:.1f} '
                 f'Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} '
                 f'V{bx["y0"]:.1f} Z" fill="#2a78d6"/>')
        lbl = f"+{v:.1f}%" if v >= 0.05 else "±0%"
        e.append(f'<text x="{x + bw / 2:.1f}" y="{y - 5:.1f}" font-size="9.5" '
                 f'fill="{INK2}" text-anchor="middle">{lbl}</text>')
        e.append(f'<text x="{x + bw / 2:.1f}" y="{bx["y0"] + 16}" font-size="10.5" '
                 f'fill="{INK2}" text-anchor="middle">{fmt_pct(pcts[i])}</text>')
    e.append(f'<line x1="{bx["x0"]}" y1="{bx["y0"]}" x2="{bx["x1"]}" y2="{bx["y0"]}" '
             f'stroke="{INK2}" stroke-width="1"/>')
    e.append(f'<text x="{(bx["x0"] + bx["x1"]) / 2:.0f}" y="{bx["y0"] + 34}" '
             f'font-size="11" fill="{INK2}" text-anchor="middle">density, %</text>')

    e.append("</svg>")
    OUT.write_text("\n".join(e) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
