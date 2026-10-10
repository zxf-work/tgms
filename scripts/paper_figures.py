#!/usr/bin/env python
"""Emit the paper's data figures as pgfplots from receipts.

The no-hand-transcription rule covers figure data: this script reads
paper_numbers.json and the results-v1 receipts and writes complete
figure floats, one fig-<name>.tex each, into the paper directory.
Re-running regenerates them byte-for-byte. Every number a caption
prints is a pn macro, checked against the receipt value it stands for
(pn-macros.tex is read from the output directory).

    python scripts/paper_figures.py --outdir paper/ecqr [--only probe rq1]

D-169 figures: fig-probe (eval-trunc-probe.json), fig-rq1
(eval-baseline-checkers.json + eval-fault-matrix.json), fig-bird-census
(eval-bird-agent.json), fig-ldbc (eval-ldbc-coverage.json +
external_workloads/ldbc/coverage_annotation.jsonl); fig-main restyled.
fig-cost (evidence-overhead-itiger.json + eval-verifier-scaling.json +
eval-unsupported-composition.json) absorbs the former fig-efficiency,
now an empty-safe stub; fig-conformance restyled to the shared palette
and type sizes.

Layout round (ECQR campaign F3): fig-cost is one figure* row of three
panels; fig-ldbc is a side-by-side pair in one column; fig-rq1, fig-probe
and fig-main are tightened. The BIRD census is now the right half of the
figure* that scripts/paper_bird_funnel.py emits (bird_census_panel
below); fig-bird-census.tex is an empty-safe stub.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RES = Path("benchmarks/results-v1")
LDBC_ANN = Path("external_workloads/ldbc/coverage_annotation.jsonl")
DS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]
DS_SHORT = {"sx-mathoverflow": "MathOverflow", "sx-superuser": "SuperUser",
            "wiki-talk": "wiki-talk"}
# expressibility per level from the oracle-plan-ops check (D-107); the
# only figure input not in paper_numbers.json — sourced from the suites
LEVEL_EXPR = {"a1": 0.59, "a2": 0.83, "a3": 1.00, "a4": 1.00}
LEVEL_OPS = {"a1": 5, "a2": 11, "a3": 13, "a4": 15}


def fig5(pn: dict, uc: dict) -> str:
    """Merged RQ2 figure: row 1 is the three need/coverage/utility ybar
    panels (former fig-main); row 2 is the unsupported-claim
    composition stacked xbar (former fig-reasons), placed beneath via
    a plain node-anchor offset from panel a. One shared caption; the
    figure carries BOTH \\label{fig:frozen} and \\label{fig:reasons}
    so existing \\ref{fig:frozen} and \\ref{fig:reasons} uses in
    hand-written prose (owned by other agents, not edited here) keep
    resolving to this merged figure. fig-reasons.tex becomes an
    empty-safe stub (see fig_reasons_stub below)."""
    fz = pn["frozen_2x2"]
    pre_t = fz["ucr_pre_gate_tgms"]
    pre_s = fz["ucr_pre_gate_sql"]
    carry = fz["claim_carrying_rate"]
    emc = fz["em_given_claims"]
    bars_t = " ".join(f"({DS_SHORT[d]},{v})" for d, v in zip(DS, pre_t))
    bars_s = " ".join(f"({DS_SHORT[d]},{v})" for d, v in zip(DS, pre_s))
    cov_o = " ".join(f"({DS_SHORT[d]},{carry[f'{d}|ours']})" for d in DS)
    cov_s = " ".join(f"({DS_SHORT[d]},{carry[f'{d}|b6e']})" for d in DS)
    ccc_o = " ".join(
        f"({DS_SHORT[d]},{round(carry[f'{d}|ours']*emc[f'{d}|ours'],4)})"
        for d in DS)
    ccc_s = " ".join(
        f"({DS_SHORT[d]},{round(carry[f'{d}|b6e']*emc[f'{d}|b6e'],4)})"
        for d in DS)
    ax = ("symbolic x coords={MathOverflow,SuperUser,wiki-talk}, "
          "xtick=data, x tick label style={inner sep=1pt}, "
          "ybar, bar width=5pt, width=0.355\\linewidth, "
          "height=3.2cm, ymin=0, enlarge x limits=0.22, "
          "axis x line*=bottom, axis y line*=left, ymajorgrids, "
          "y tick label style={/pgf/number format/fixed}, " + SQ)

    # row 2: unsupported-claim composition (former fig-reasons body)
    comp = uc["composition"]
    dsrow = {"sx-mathoverflow": "MathOverflow",
             "sx-superuser": "SuperUser", "wiki-talk": "wiki-talk"}
    rows = []
    for d in DS:
        c = comp[f"{d}|operators"]
        k = c["unsupported_claims_by_kind"]
        rows.append((f"{dsrow[d]} / Ops", k.get("count", 0),
                     k.get("entity", 0), k.get("value", 0),
                     c.get("unsupported_claims_undetermined_kind", 0), 0))
    for d in DS:
        v = comp[f"{d}|sql"]["claim_verdicts"]
        nw = sum(x for kk, x in v.items() if kk != "SUPPORTED")
        # every SQL-path failure is UNSUPPORTED_NO_WITNESS, so the SQL
        # rows share the witness series with the operator entity
        # witnesses (rows are disjoint by interface)
        assert set(v) <= {"SUPPORTED", "UNSUPPORTED_NO_WITNESS"}, v
        rows.append((f"{dsrow[d]} / SQL", 0, nw, 0, 0))
    sym = ",".join(r[0] for r in reversed(rows))
    series = [("exact count", 1, "figAccent"),
              ("witness", 2, "figDark"),
              ("cited value", 3, "figMid"),
              ("undetermined", 4, "figLight")]
    plots = "\n".join(
        rf"\addplot[fill={col}, draw=white, line width=0.4pt] "
        r"coordinates {"
        + " ".join(f"({r[si]},{r[0]})" for r in rows) + "};"
        for _, si, col in series)
    legend = ", ".join(lab for lab, _, _ in series)
    ops = "fill=figDark, draw=none"
    sql = "fill=figMid, draw=none"

    return rf"""{GEN}
% F-main: need/coverage/utility (row 1) and unsupported-claim composition
% (row 2). Sources: paper_numbers.json (frozen_2x2) and
% eval-unsupported-composition.json.
\begin{{figure*}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[name=a, {ax}, ymax=0.3, ytick={{0,0.1,0.2,0.3}},
  ylabel={{pre-gate UCR}},
  legend style={{at={{(0.5,1.03)}}, anchor=south, legend columns=-1,
    /tikz/every even column/.append style={{column sep=4pt}}}}]
\addplot[{ops}] coordinates {{{bars_t}}};
\addplot[{sql}] coordinates {{{bars_s}}};
\legend{{Operators, SQL}}
\end{{axis}}
\begin{{axis}}[name=b, at={{(a.outer east)}}, anchor=outer west,
  xshift=3mm, {ax}, ymax=1.0,
  ylabel={{certified-output coverage}},
  legend style={{at={{(0.5,1.03)}}, anchor=south, legend columns=-1,
    /tikz/every even column/.append style={{column sep=4pt}}}}]
\addplot[{ops}] coordinates {{{cov_o}}};
\addplot[{sql}] coordinates {{{cov_s}}};
\legend{{Operators+ECQR, SQL+ECQR}}
\end{{axis}}
\begin{{axis}}[name=c, at={{(b.outer east)}}, anchor=outer west,
  xshift=3mm, {ax}, ymax=0.6, ytick={{0,0.2,0.4,0.6}},
  ylabel={{correct-certified coverage}},
  legend style={{at={{(0.5,1.03)}}, anchor=south, legend columns=-1,
    /tikz/every even column/.append style={{column sep=4pt}}}}]
\addplot[{ops}] coordinates {{{ccc_o}}};
\addplot[{sql}] coordinates {{{ccc_s}}};
\legend{{Operators+ECQR, SQL+ECQR}}
\end{{axis}}
\begin{{axis}}[name=d, at={{(a.outer south west)}}, anchor=outer north west,
  yshift=-2.5mm, xbar stacked, {SQ}, width=0.975\linewidth,
  height=3.3cm, symbolic y coords={{{sym}}}, ytick=data, xmin=0,
  enlarge y limits=0.1, axis x line*=bottom, axis y line*=left,
  xmajorgrids, xlabel={{unsupported proposed claims (count)}},
  bar width=6pt,
  legend style={{at={{(0.99,0.04)}}, anchor=south east}},
  legend cell align=left]
{plots}
\legend{{{legend}}}
\end{{axis}}
\end{{tikzpicture}}
\caption{{Evidence need and enforcement effect per dataset and
interface. Top: pre-gate UCR, certified-output coverage, and
correct-certified coverage. Bottom: unsupported proposed claims by the
obligation they fail (exact count, witness, cited value, or
undetermined). Unsupported claims occur under both interfaces, and
operator failures span three distinct obligations, so certified output
trades answer availability for support.}}
\label{{fig:frozen}}
\label{{fig:reasons}}
\end{{figure*}}
"""


def fig_reasons_stub() -> str:
    """fig-reasons.tex is merged into fig-main.tex (label fig:frozen);
    this file is now an empty-safe stub so a stale \\input{{fig-reasons}}
    is harmless."""
    return ("% fig-reasons merged into fig-main.tex (see fig:frozen).\n"
            "% This file is intentionally empty -- a stale "
            "\\input{fig-reasons} is a no-op.\n")


def fig6(pn: dict) -> str:
    m6 = pn["m6_frontier"]
    if isinstance(m6, str):
        raise SystemExit("m6_frontier missing from paper_numbers.json — "
                         "regenerate it on the eval host first")
    lv = ["a1", "a2", "a3", "a4"]
    expr = " ".join(f"({LEVEL_OPS[k]},{LEVEL_EXPR[k]})" for k in lv)
    execok = " ".join(f"({LEVEL_OPS[k]},{m6[k]['exec_ok']})" for k in lv)
    em = " ".join(f"({LEVEL_OPS[k]},{m6[k]['em']})" for k in lv)
    first = " ".join(f"({LEVEL_OPS[k]},{m6[k]['first_plan_valid']})"
                     for k in lv)
    return rf"""% F6 — generated from paper_numbers.json; do not edit
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[width=0.9\linewidth, height=4.6cm,
  xlabel={{\scriptsize operators in surface}},
  xtick={{5,11,13,15}}, ymin=0, ymax=1.05,
  legend style={{font=\tiny, at={{(0.98,0.5)}}, anchor=east}},
  every axis plot/.append style={{mark size=1.6pt}}]
\addplot+[mark=*] coordinates {{{expr}}};
\addplot+[mark=square*] coordinates {{{execok}}};
\addplot+[mark=triangle*] coordinates {{{first}}};
\addplot+[mark=diamond*] coordinates {{{em}}};
\legend{{expressibility, execution success, first-plan validity,
  exact match}}
\end{{axis}}
\end{{tikzpicture}}
\caption{{The interface frontier over nested surfaces at identical
runtime, from a one-seed exploratory study. Expressibility and
execution success rise monotonically with surface size. Accuracy does
not, and the dip at the full surface coincides with the one operator
pair whose capabilities structurally overlap.}}
\label{{fig:frontier}}
\end{{figure}}
"""


CLAIM_AB = {"membership": "mem", "scalar": "scl", "exact_count": "cnt",
            "complete_set": "set", "existence": "ext",
            "nonexistence": "nex", "historical_basis": "bas"}
FAULT_AB = {"clean": "clean", "page_truncation": "trunc",
            "execution_incomplete": "exec", "wrong_count": "wrongN",
            "wrong_scalar": "wrongV", "omitted_member": "omit",
            "fabricated_member": "fabr", "false_membership": "falseM",
            "false_existence": "falseE", "false_nonexistence": "falseN",
            "wrong_snapshot": "snap", "unpinned_snapshot": "unpin",
            "uncited_value": "uncit", "digest_mismatch": "digest"}


def fig_conformance(bc: dict, fm: dict, mac: dict, palette: dict) -> str:
    """Per-cell decision strip for the companion appendix: the 27
    EvidenceBench cells (columns) x three checkers (rows), columns grouped
    by the fault families of fig-rq1 (scripts/paper_fault_families.py).
    Each cell is coloured by whether the checker's decision matches the
    expected verdict. The appendix does not load preamble-shared.tex, so
    the palette is re-stated with \\providecolor (values read from that
    preamble) and every node names its preamble size explicitly."""
    from paper_fault_families import FAMILY, ORDER
    gshort = {"controls (must certify)": "controls (must certify)",
              "delivery incompleteness": "delivery\\\\incompleteness",
              "execution incompleteness": "execution\\\\incompleteness",
              "value / witness": "value / witness",
              "basis": "basis", "integrity": "integrity"}
    assert set(gshort) == set(ORDER), "family names changed"

    def classify(c):
        if c["ok"]:
            return "figLight"     # decision matches the expected verdict
        if c["expectation"] == "must_not_certify":
            return "figAccent"    # certified an injected fault
        return "figMid"           # rejected a clean control

    b1 = bc["b1_value_only"]["cells"]
    b2 = bc["b2_taint_all"]["cells"]
    ecqr = fm["cells"]
    n = len(ecqr)
    assert len(b1) == n and len(b2) == n
    for cb1, cb2, ce in zip(b1, b2, ecqr):
        assert (cb1["claim"], cb1["fault"]) == (ce["claim"], ce["fault"])
        assert (cb2["claim"], cb2["fault"]) == (ce["claim"], ce["fault"])
    check_macro(mac, "pnCells", n)
    assert all(c["ok"] for c in ecqr), "ECQR must match every cell"

    labs, seen = [], {}
    for c in ecqr:
        key = (c["claim"], c["fault"])
        seen[key] = seen.get(key, 0) + 1
        labs.append(CLAIM_AB[c["claim"]] + ("*" if c["fault"] == "clean"
                                            and seen[key] == 2 else ""))
    # column order: by family (fig-rq1 order), cell order within a family
    order = sorted(range(n), key=lambda i: ORDER.index(FAMILY[ecqr[i]["fault"]]))
    rows = [("value-only", b1), ("incompleteness taint", b2),
            ("ECQR", ecqr)]
    cw, ch, gap = 0.52, 0.40, 0.16        # cm: column, row, family gap
    xs, x, prev = [], 0.0, None
    for i in order:
        fam = FAMILY[ecqr[i]["fault"]]
        if prev is not None and fam != prev:
            x += gap
        xs.append((i, fam, x))
        x += cw
        prev = fam

    cells = []
    for ri, (_, cl) in enumerate(rows):
        y = -ri * ch
        for i, _f, x0 in xs:
            cells.append(rf"\fill[{classify(cl[i])}] ({x0:.2f},{y:.2f}) "
                         rf"rectangle ({x0 + cw - 0.06:.2f},"
                         rf"{y + ch - 0.06:.2f});")
    ybot = -(len(rows) - 1) * ch
    cols = [rf"\node[anchor=north, font=\scriptsize, inner sep=1pt, "
            rf"text height=1.6ex, text depth=0.3ex] at "
            rf"({x0 + (cw - 0.06) / 2:.2f},{ybot - 0.06:.2f}) "
            rf"{{{labs[i]}}};" for i, _f, x0 in xs]
    names = [rf"\node[anchor=east, font=\scriptsize, inner sep=1pt] at "
             rf"(-0.12,{-ri * ch + (ch - 0.06) / 2:.2f}) {{{rname}}};"
             for ri, (rname, _) in enumerate(rows)]
    groups, ytop = [], ch - 0.06
    for fam in ORDER:
        span = [x0 for _i, f, x0 in xs if f == fam]
        if not span:
            continue
        x0, x1 = span[0], span[-1] + cw - 0.06
        groups.append(
            rf"\draw[black!60] ({x0:.2f},{ytop + 0.10:.2f}) -- "
            rf"({x0:.2f},{ytop + 0.17:.2f}) -- ({x1:.2f},{ytop + 0.17:.2f})"
            rf" -- ({x1:.2f},{ytop + 0.10:.2f});")
        groups.append(
            rf"\node[anchor=south, font=\scriptsize, align=center, "
            rf"inner sep=1pt, text depth=0.3ex] at "
            rf"({(x0 + x1) / 2:.2f},{ytop + 0.20:.2f}) "
            rf"{{{gshort[fam]}}};")

    def swatch(col):
        return rf"\tikz\fill[{col}] (0,0) rectangle (0.2,0.2);"
    legend = (rf"\node[anchor=north west, font=\scriptsize] at "
              rf"(0,{ybot - 0.55:.2f}) {{{swatch('figLight')}~matches the "
              rf"expected verdict\quad {swatch('figAccent')}~false accept "
              rf"(certifies a fault)\quad {swatch('figMid')}~false reject "
              rf"(rejects a valid control)}};")
    provide = "\n".join(
        rf"\providecolor{{{k}}}{{RGB}}{{{v}}}"
        for k, v in sorted(palette.items()))
    body = "\n".join(groups + cells + names + cols + [legend])
    return rf"""{GEN}
% Sources: benchmarks/results-v1/eval-baseline-checkers.json (b1_value_only,
% b2_taint_all) and eval-fault-matrix.json (ECQR); families as in
% scripts/paper_fault_families.py. Palette from preamble-shared.tex.
{provide}
\begin{{figure*}}[t]
\centering
\begin{{tikzpicture}}
{body}
\end{{tikzpicture}}
\caption{{Conformance decision for each of the \pnCells\ EvidenceBench
cells and each checker, with cells grouped by fault family; column
labels give the claim form (mem, scl, cnt, set, ext, nex, bas), and *
marks the two controls whose evidence is truncated yet sufficient. The
simple checkers fail in opposite directions, and the verifier matches
every expected verdict.}}
\label{{fig:conformance}}
\end{{figure*}}
"""


# ---------------------------------------------------------------------------
# D-169 figure round: probe, RQ1 checkers, BIRD census, LDBC coverage.
# Typography comes from preamble-shared.tex (picture text \footnotesize,
# ticks/legends/data labels \scriptsize); colours are the shared palette
# figDark/figMid/figLight/figAccent/figAccentLight only. No figure here
# sets a font size or family. Every number a caption prints is a pn
# macro, and each is checked against the receipt value it stands for.
# ---------------------------------------------------------------------------

GEN = "% GENERATED by scripts/paper_figures.py -- do not edit."
DL = r"font=\scriptsize, inner sep=1pt"     # data-label node style
# nodes near coords on horizontal bars: label just past the bar end
DLX = DL + ", anchor=west"
# square legend swatches for bars (xbar/ybar default to a two-bar glyph)
SQ = (r"legend image code/.code={\fill[#1] (0cm,-0.08cm) "
      r"rectangle (0.2cm,0.1cm);}")
# the ECQR dot series keeps a dot in the legend (a plot option cannot
# carry the #1 parameter, so the colour is fixed)
MK = (r"legend image code/.code={\fill[figAccent] (0.1cm,0.01cm) "
      r"circle (1.6pt);}")


def labels(items: list, tag: str) -> tuple:
    """Data labels drawn ON TOP of the bars: pgfplots draws its plots
    after any \\node given inside the axis, so each label position is
    recorded as a coordinate inside the axis and the node is placed after
    \\end{axis}. items: (x, y, node options, text). Returns the
    (inside-axis, after-axis) TeX fragments."""
    inside, after = [], []
    for i, (x, y, opts, text) in enumerate(items):
        inside.append(rf"\coordinate ({tag}{i}) at (axis cs:{x},{y});")
        after.append(rf"\node[{DL}, {opts}] at ({tag}{i}) {{{text}}};")
    return "\n".join(inside), "\n".join(after)


def load_macros(path: Path) -> dict:
    """\\newcommand{\\pnX}{body} -> {"pnX": "body"} (flat bodies only)."""
    out = {}
    for m in re.finditer(r"\\newcommand\{\\(pn[A-Za-z]+)\}\{([^{}]*)\}",
                         path.read_text()):
        out[m.group(1)] = m.group(2)
    return out


def load_palette(path: Path) -> dict:
    """\\definecolor{figX}{RGB}{r,g,b} lines of preamble-shared.tex ->
    {"figX": "r,g,b"}. A figure that is also input by a document which
    does not load the shared preamble (the companion appendix) re-states
    these with \\providecolor, a no-op wherever the preamble ran."""
    out = {}
    for m in re.finditer(r"\\definecolor\{(fig[A-Za-z]+)\}\{RGB\}"
                         r"\{([0-9, ]+)\}", path.read_text()):
        out[m.group(1)] = m.group(2).replace(" ", "")
    need = {"figDark", "figMid", "figLight", "figAccent", "figAccentLight"}
    if not need <= set(out):
        raise SystemExit(f"palette incomplete in {path}: {sorted(out)}")
    return out


def check_macro(mac: dict, name: str, value) -> None:
    """Refuse to emit a figure whose receipt disagrees with a macro the
    paper prints next to it."""
    if name not in mac:
        raise SystemExit(f"\\{name} missing from pn-macros.tex")
    if mac[name] != str(value):
        raise SystemExit(f"\\{name} prints {mac[name]!r} but the receipt "
                         f"gives {value!r}")


def fig_probe(tp: dict, mac: dict) -> str:
    """Truncation probe: page-derived and correct fractions per endpoint
    condition, with the exact-total condition split by question family
    (receipt: eval-trunc-probe.json)."""
    C = tp["conditions"]

    def pct(d, k):
        return f"{100 * d[k] / d['n']:.1f}"

    rows = [  # (y, tick label, record, page-derived macro, correct macro)
        (4.8, "bare page", C["C0"]["all"],
         "pnProbeBarePD", "pnProbeBareCorr"),
        (3.8, "+ truncation flag", C["C1"]["all"],
         "pnProbeFlagPD", "pnProbeFlagCorr"),
        (2.8, "+ exact total", C["C2"]["all"],
         "pnProbeTotalPD", "pnProbeTotalCorr"),
        (1.3, "+ exact total, count", C["C2"]["by_family"]["COUNT"],
         "pnProbeTotalCountPD", "pnProbeTotalCountCorr"),
        (0.3, "+ exact total, set", C["C2"]["by_family"]["SET"],
         "pnProbeTotalSetPD", "pnProbeTotalSetCorr"),
    ]
    check_macro(mac, "pnProbeN", C["C0"]["all"]["n"])
    check_macro(mac, "pnProbeCountN", C["C2"]["by_family"]["COUNT"]["n"])
    check_macro(mac, "pnProbeSetN", C["C2"]["by_family"]["SET"]["n"])
    for _y, _l, r, mpd, mco in rows:
        check_macro(mac, mpd, pct(r, "page_derived"))
        check_macro(mac, mco, pct(r, "correct"))
    # explicit offsets instead of pgfplots' automatic bar shift, so each
    # data label sits on its own bar
    pd = " ".join(f"({pct(r, 'page_derived')},{y + 0.19:.2f}) "
                  f"[{pct(r, 'page_derived')}]" for y, _l, r, _a, _b in rows)
    co = " ".join(f"({pct(r, 'correct')},{y - 0.19:.2f}) "
                  f"[{pct(r, 'correct')}]" for y, _l, r, _a, _b in rows)
    yt = ",".join(str(y) for y, *_ in rows)
    ytl = ",".join(f"{{{lab}}}" for _y, lab, *_ in rows)
    return rf"""{GEN}
% Source: benchmarks/results-v1/eval-trunc-probe.json (conditions C0, C1,
% C2; C2 split by question family).
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[xbar, bar shift=0pt, {SQ}, width=0.92\linewidth, y=0.5cm,
  bar width=4.5pt, xmin=0, xmax=100, xtick={{0,20,40,60,80,100}},
  ytick={{{yt}}}, yticklabels={{{ytl}}},
  ymin=-0.2, ymax=5.3, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left, xmajorgrids,
  xlabel={{answers (\%)}},
  point meta=explicit symbolic, nodes near coords,
  every node near coord/.append style={{{DLX}}},
  legend style={{at={{(0.98,0.98)}}, anchor=north east}},
  legend cell align=left]
\addplot[fill=figAccent, draw=none] coordinates {{{pd}}};
\addplot[fill=figDark, draw=none] coordinates {{{co}}};
\legend{{page-derived, correct}}
\end{{axis}}
\end{{tikzpicture}}
\caption{{Truncation probe: share of the \pnProbeN\ answers that are
page-derived or correct under each endpoint condition; the last two
rows split the exact-total condition into its \pnProbeCountN\ count
and \pnProbeSetN\ complete-set questions. Only the exact total moves
the answers, and it repairs the counts while leaving the sets
page-derived.}}
\label{{fig:probe}}
\end{{figure}}
"""


def fig_rq1(bc: dict, fm: dict, mac: dict) -> str:
    """Errors of five deterministic checkers per EvidenceBench family,
    over the single-step cells and the composition cells (receipt:
    eval-baseline-checkers-v3.json; the single-step rows are checked
    against eval-baseline-checkers.json and eval-fault-matrix.json). A
    control row can only hold false rejects and a fault row only false
    accepts, so each row carries one error kind. Drawn as a matrix of
    counts: exact values matter and five bars per family would not fit
    one column."""
    from paper_macros import BL_CHECKERS, baseline_rows
    v3 = json.loads((RES / "eval-baseline-checkers-v3.json").read_text())
    rows = baseline_rows(v3)
    single = [r for r in rows if r[0] == "single_step"]
    comp = [r for r in rows if r[0] == "composition"]
    vo, ta = BL_CHECKERS.index("value_only"), BL_CHECKERS.index("taint_all")
    check_macro(mac, "pnCells", sum(r[2] for r in single))
    check_macro(mac, "pnCells", len(fm["cells"]))
    check_macro(mac, "pnValueOnlyFail", sum(r[4][vo] for r in single))
    check_macro(mac, "pnTaintFail", sum(r[4][ta] for r in single))
    check_macro(mac, "pnControlCellsN",
                sum(r[2] for r in single if r[3] == "FR"))
    check_macro(mac, "pnFaultCellsN",
                sum(r[2] for r in single if r[3] == "FA"))
    check_macro(mac, "pnBaseCompN", sum(r[2] for r in comp))
    assert sum(f["ok"] is False for f in fm["cells"]) == 0
    assert all(r[4][BL_CHECKERS.index("ecqr")] == 0 for r in rows)
    short = {"delivery incompleteness": "delivery incomplete",
             "execution incompleteness": "execution incomplete"}
    heads = [r"value-\\only", r"incompl.\\taint", r"metadata\\rules",
             r"ECQR, no\\propagation", r"\textbf{ECQR}"]
    dx, dy, x0 = 1.02, 0.33, 0.62
    out, y = [], 0.0
    for i, h in enumerate(heads):
        out.append(rf"\node[align=center, anchor=south, inner sep=1pt, "
                   rf"font=\scriptsize] at ({x0 + i * dx:.2f},"
                   rf"{y + 0.12:.2f}) {{{h}}};")
    out.append(rf"\draw[figLight] (-2.55,{y + 0.08:.2f}) -- "
               rf"({x0 + 4 * dx + 0.45:.2f},{y + 0.08:.2f});")
    for title, group in (("single-step cells", single),
                         ("composition cells", comp)):
        y -= dy
        out.append(rf"\node[anchor=west, inner sep=0pt, font=\footnotesize"
                   rf"\itshape] at (-2.55,{y:.2f}) {{{title}}};")
        for _scope, fam, n, kind, errs in group:
            y -= dy
            out.append(rf"\node[anchor=east, inner sep=1pt, font="
                       rf"\scriptsize] at (0.05,{y:.2f}) "
                       rf"{{{short.get(fam, fam)} ({n})}};")
            for i, e in enumerate(errs):
                x = x0 + i * dx
                if e:
                    out.append(rf"\node[fill=figAccentLight, minimum "
                               rf"width=0.62cm, minimum height=0.28cm, "
                               rf"inner sep=0pt, rounded corners=1pt, "
                               rf"font=\scriptsize] at "
                               rf"({x:.2f},{y:.2f}) {{\textbf{{{e}}}}};")
                else:
                    out.append(rf"\node[text=figMid, inner sep=0pt, "
                               rf"font=\scriptsize] at "
                               rf"({x:.2f},{y:.2f}) {{0}};")
    body = "\n".join(out)
    return rf"""{GEN}
% Source: benchmarks/results-v1/eval-baseline-checkers-v3.json (five
% checkers, single-step and composition cells); single-step rows checked
% against eval-baseline-checkers.json and eval-fault-matrix.json;
% families as in scripts/paper_macros.py (BL_FAMILY).
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
{body}
\end{{tikzpicture}}
\caption{{EvidenceBench decisions that disagree with the expected
verdict, per family and checker, over the \pnCells\ single-step and
\pnBaseCompN\ composition cells (cells per family in parentheses; a
control row counts false rejects, every other row false accepts). The
metadata-rules checker and ECQR without propagation match ECQR on every
single-step cell but accept every composition fault; only ECQR with
propagation makes no error.}}
\label{{fig:rq1}}
\end{{figure}}
"""


def fig_topk(mac: dict) -> str:
    """TopK routes on the ranked BIRD pages, agent and gold SQL: each page
    under its first certifying route (total order, sequence-strict,
    boundary-strict set) or the reason none certifies (receipt:
    eval-bird-topk-v3.json). No-ORDER-BY and unavailable-probe pages share
    one segment; the appendix table separates them."""
    tk = json.loads((RES / "eval-bird-topk-v3.json").read_text())
    sides = [("agent SQL", tk["agent"]), ("gold SQL", tk["gold"])]
    a, g = tk["agent"], tk["gold"]
    for name, val in (("pnTopkAgentN", a["n_topk_shaped"]),
                      ("pnTopkAgentTotal", a["certified_total_order"]),
                      ("pnTopkAgentSeq", a["certified_sequence_strict"]),
                      ("pnTopkAgentSet", a["certified_boundary_strict_set"]),
                      ("pnTopkAgentTie",
                       a["not_certified_by_reason"]["boundary_tie"]),
                      ("pnTopkGoldN", g["n_topk_shaped"]),
                      ("pnTopkGoldSeq", g["certified_sequence_strict"]),
                      ("pnTopkGoldTie",
                       g["not_certified_by_reason"]["boundary_tie"])):
        check_macro(mac, name, val)
    assert a["certified_total_order"] == g["certified_total_order"] == 0, \
        "a nonzero total-order route needs its own segment"

    def other(s):
        return sum(v for k, v in s["not_certified_by_reason"].items()
                   if k != "boundary_tie")
    segs = [(lambda s: s["certified_sequence_strict"], "sequence-strict",
             "figDark", "white"),
            (lambda s: s["certified_boundary_strict_set"],
             "boundary-strict (set only)", "figMid", "white"),
            (lambda s: s["not_certified_by_reason"]["boundary_tie"],
             r"tie at rank $k$/$k{+}1$", "figAccent", "white"),
            (other, "no ORDER BY or no probe", "figLight", "black")]
    ys = [1, 0]
    plots, notes = [], []
    cum = {lbl: 0 for lbl, _ in sides}
    for fn, _lab, fill, txt in segs:
        pts = []
        for (lbl, s), y in zip(sides, ys):
            v = fn(s)
            pts.append(f"({v},{y})")
            if v >= 8:
                notes.append((cum[lbl] + v / 2, y, f"text={txt}", str(v)))
            cum[lbl] += v
        plots.append(rf"\addplot[fill={fill}, draw=white, line width=0.4pt] "
                     rf"coordinates {{{' '.join(pts)}}};")
    for lbl, s in sides:
        assert cum[lbl] == s["n_topk_shaped"], (lbl, cum[lbl])
    lin, lout = labels(notes, "tk")
    ytl = ",".join(f"{{{lbl} ({s['n_topk_shaped']})}}" for lbl, s in sides)
    leg = ", ".join(c[1] for c in segs)
    xmax = max(s["n_topk_shaped"] for _l, s in sides)
    return rf"""{GEN}
% Source: benchmarks/results-v1/eval-bird-topk-v3.json (agent, gold;
% each ranked page once, under its first certifying route).
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[xbar stacked, {SQ}, width=0.80\linewidth, y=0.42cm,
  bar width=7pt, xmin=0, xmax={xmax}, xtick={{0,20,40,60,80}},
  xlabel={{ranked BIRD pages}},
  ytick={{{",".join(map(str, ys))}}}, yticklabels={{{ytl}}},
  ymin=-0.55, ymax=1.55, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left,
  legend style={{at={{(0.5,1.0)}}, anchor=south, legend columns=2,
    yshift=0pt, row sep=-2pt, /tikz/every even column/.append style={{column sep=6pt}}}},
  legend cell align=left]
{chr(10).join(plots)}
\legend{{{leg}}}
{lin}
\end{{axis}}
{lout}
\end{{tikzpicture}}
\caption{{Ranked BIRD pages by the TopK route that certifies them, or
the reason none does. No page has a static total order, and ties at
rank $k$/$k{{+}}1$ leave \pnTopkAgentTie\ of the \pnTopkAgentN\ agent
pages without a unique top-$k$ answer.}}
\label{{fig:topk}}
\end{{figure}}
"""


def bird_census_panel(ba: dict, mac: dict, place: str) -> tuple:
    """BIRD questions per intent contract, split by certification outcome
    (receipt: eval-bird-agent.json, by_form; the census that
    scripts/paper_bird_census.py tabulates), as one axis for the
    combined BIRD figure that scripts/paper_bird_funnel.py emits. place:
    extra axis options (position). Returns (axis TeX, after-axis TeX)."""
    from paper_bird_census import LABEL, ORDER
    lab = dict(LABEL, SET_AND_COUNT="Multipart (set + count)")
    bf = ba["by_form"]
    rows = []
    for k in ORDER:
        if k not in bf:
            continue
        n, cert = bf[k]["n"], bf[k]["certified"]
        full = bf[k].get("certified_full_contract", 0)
        rows.append((lab[k], n, full, cert - full, n - cert))
    assert sum(r[1] for r in rows) == ba["n"], "questions must sum to n"
    check_macro(mac, "pnBirdN", ba["n"])
    check_macro(mac, "pnBirdCert", sum(r[2] + r[3] for r in rows))
    check_macro(mac, "pnBirdCertFull", sum(r[2] for r in rows))
    check_macro(mac, "pnBirdCertPartial", sum(r[3] for r in rows))
    assert sum(1 for r in rows if r[3]) == 1, "partial gap in one row"
    ys = list(range(len(rows)))[::-1]
    xmax = 350
    ytl = ",".join(f"{{{r[0]}}}" for r in rows)

    def series(i):
        return " ".join(f"({r[i]},{y})" for r, y in zip(rows, ys))
    items = []
    for r, y in zip(rows, ys):
        items.append((r[1], y, "anchor=west", rf"\,{r[2]}/{r[1]}"))
        if r[3]:
            items.append((r[2] + r[3] / 2, y, "text=white", str(r[3])))
    lin, lout = labels(items, "bc")
    axis = rf"""\begin{{axis}}[name=cen, {place}xbar stacked, {SQ},
  y=0.42cm, bar width=7pt, xmin=0, xmax={xmax}, xtick={{0,100,200,300}},
  ytick={{{",".join(map(str, ys))}}}, yticklabels={{{ytl}}},
  ymin=-0.6, ymax={len(rows) - 0.4}, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left, xmajorgrids,
  xlabel={{questions}},
  legend style={{at={{(0.5,1.02)}}, anchor=south, legend columns=-1,
    /tikz/every even column/.append style={{column sep=4pt}}}}]
\addplot[fill=figDark, draw=white, line width=0.4pt]
  coordinates {{{series(2)}}};
\addplot[fill=figAccent, draw=white, line width=0.4pt]
  coordinates {{{series(3)}}};
\addplot[fill=figLight, draw=white, line width=0.4pt]
  coordinates {{{series(4)}}};
\legend{{strict full contract, any claim only, no certified claim}}
{lin}
\end{{axis}}
"""
    return axis, lout


def fig_bird_census_stub() -> str:
    """fig-bird-census.tex is merged into fig-bird-funnel.tex (one
    figure*, census on the right; scripts/paper_bird_funnel.py); this
    file is now an empty-safe stub so a stale \\input{fig-bird-census}
    is harmless."""
    return ("% fig-bird-census merged into fig-bird-funnel.tex "
            "(see fig:birdcensus).\n"
            "% This file is intentionally empty -- a stale "
            "\\input{fig-bird-census} is a no-op.\n")


def fig_ldbc(ann: list, rec: dict, mac: dict) -> str:
    """LDBC SNB read templates per group (IC/IS/BI): execution coverage
    and full-contract claim coverage as two side-by-side panels (sources:
    external_workloads/ldbc/coverage_annotation.jsonl, receipt
    eval-ldbc-coverage.json). The claim partition is the annotation's
    claim_full_contract label, assigned by the first missing feature in
    the receipt's precedence path > groupwise extremum > top-k > ordered,
    the same rule scripts/paper_ldbc_table.py tabulates; groupwise
    extremum and path (one template each) share a segment."""
    assert len(ann) == rec["n_templates"]
    assert rec["claim_partition_precedence"] == [
        "REQUIRES_PATH_CERTIFICATE", "REQUIRES_GROUPWISE_EXTREMUM",
        "REQUIRES_TOP_K", "REQUIRES_ORDERED_RESULT",
        "CURRENT_ECQR_FRAGMENT"], "precedence changed; revisit the figure"
    groups = [("IC", "interactive complex"), ("IS", "interactive short"),
              ("BI", "business intelligence")]
    exec_cls = [("DIRECT_TGMS", "one operator", "figDark", "white"),
                ("DECOMPOSABLE_TGMS", "operator DAG", "figMid", "white"),
                ("SQL_ONLY", "SQL only", "figLight", "black"),
                ("UNSUPPORTED_EXECUTION", "unsupported", "figAccent",
                 "white")]
    claim_cls = [(("CURRENT_ECQR_FRAGMENT",), "in fragment", "figDark",
                  "white"),
                 (("REQUIRES_ORDERED_RESULT",), "ordered", "figMid",
                  "white"),
                 (("REQUIRES_TOP_K",), "top-$k$", "figAccent", "white"),
                 (("REQUIRES_GROUPWISE_EXTREMUM",
                   "REQUIRES_PATH_CERTIFICATE"),
                  "group extremum or path", "figLight", "black")]
    for k, v in rec["exec_coverage_counts"].items():
        assert sum(1 for r in ann if r["exec_coverage"] == k) == v, k
    for k, v in rec["claim_full_contract_counts"].items():
        assert sum(1 for r in ann if r["claim_full_contract"] == k) == v, k
    ec = rec["exec_coverage_counts"]
    cc = rec["claim_full_contract_counts"]
    check_macro(mac, "pnLdbcN", rec["n_templates"])
    check_macro(mac, "pnLdbcExecDirect", ec["DIRECT_TGMS"])
    check_macro(mac, "pnLdbcExecDecomp", ec["DECOMPOSABLE_TGMS"])
    check_macro(mac, "pnLdbcExecSql", ec["SQL_ONLY"])
    check_macro(mac, "pnLdbcExecUnsup", ec["UNSUPPORTED_EXECUTION"])
    check_macro(mac, "pnLdbcClaimFrag", cc["CURRENT_ECQR_FRAGMENT"])
    check_macro(mac, "pnLdbcClaimOrdered", cc["REQUIRES_ORDERED_RESULT"])
    check_macro(mac, "pnLdbcClaimTopK", cc["REQUIRES_TOP_K"])
    check_macro(mac, "pnLdbcClaimGext", cc["REQUIRES_GROUPWISE_EXTREMUM"])
    check_macro(mac, "pnLdbcClaimPath", cc["REQUIRES_PATH_CERTIFICATE"])
    assert all(r["query_id"][:2] in {g for g, _ in groups} for r in ann)
    sizes = {g: sum(1 for r in ann if r["query_id"].startswith(g))
             for g, _ in groups}
    ys = list(range(len(groups)))[::-1]
    ytl = ",".join(f"{{{g} ({sizes[g]})}}" for g, _ in groups)
    xmax = max(sizes.values())

    def panel(name, key, classes, extra, title, ticklabels):
        plots, notes = [], []
        cum = {g: 0 for g, _ in groups}
        for vals, _lab, fill, txt in classes:
            vals = (vals,) if isinstance(vals, str) else vals
            pts = []
            for (g, _), y in zip(groups, ys):
                v = sum(1 for r in ann if r["query_id"].startswith(g)
                        and r[key] in vals)
                pts.append(f"({v},{y})")
                if v:
                    notes.append((cum[g] + v / 2, y, f"text={txt}", str(v)))
                cum[g] += v
            plots.append(rf"\addplot[fill={fill}, draw=white, "
                         rf"line width=0.4pt] coordinates {{{' '.join(pts)}}};")
        assert cum == sizes, (cum, sizes)
        leg = ", ".join(c[1] for c in classes)
        lin, lout = labels(notes, f"l{name}")
        # the legend is one entry per row above the plot box (the panel
        # is half a column wide); the title sits above the legend
        return rf"""\begin{{axis}}[name={name}, {extra}xbar stacked, {SQ},
  scale only axis, width=0.4\linewidth, y=0.45cm, bar width=7pt,
  xmin=0, xmax={xmax}, xtick={{0,5,10,15,20}}, xlabel={{templates}},
  ytick={{{",".join(map(str, ys))}}}, yticklabels={{{ticklabels}}},
  ymin=-0.6, ymax={len(groups) - 0.4}, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left,
  title={{{title}}}, title style={{at={{(0,1)}}, anchor=south west,
    xshift=-2pt, yshift=27pt, align=left}},
  legend style={{at={{(0,1.0)}}, anchor=south west, legend columns=1,
    yshift=1pt, row sep=-1.5pt}}, legend cell align=left]
{chr(10).join(plots)}
\legend{{{leg}}}
{lin}
\end{{axis}}
{lout}
"""
    left = panel("a", "exec_coverage", [(c[0],) + c[1:] for c in exec_cls],
                 "", r"execution coverage\\\mbox{}", ytl)
    right = panel("b", "claim_full_contract", claim_cls,
                  "at={(a.south east)}, anchor=south west, xshift=0.45cm, ",
                  r"claim coverage\\(full result contract)", "")
    return rf"""{GEN}
% Sources: external_workloads/ldbc/coverage_annotation.jsonl and
% benchmarks/results-v1/eval-ldbc-coverage.json.
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
{left}{right}\end{{tikzpicture}}
\caption{{LDBC SNB read templates per group (template count in
parentheses): how each is executed, and the first full-contract feature
the claim grammar lacks, taking path, groupwise extremum, top-$k$, and
ordered result in that order. Most templates need the SQL path, and
top-$k$ is the dominant claim gap in the interactive and BI groups
alike.}}
\label{{fig:ldbc}}
\end{{figure}}
"""


def fig_efficiency_stub() -> str:
    """fig-efficiency.tex is folded into fig-cost.tex (panel b, label
    fig:cost); this file is now an empty-safe stub so a stale
    \\input{fig-efficiency} is harmless."""
    return ("% fig-efficiency folded into fig-cost.tex (see fig:cost).\n"
            "% This file is intentionally empty -- a stale "
            "\\input{fig-efficiency} is a no-op.\n")


def fig_cost(sc: dict, ov: dict, uc: dict, mac: dict) -> str:
    """RQ4 evidence cost, three panels in one row of a figure*:
    (a) the SQL path, an uncertified answer (page query) against a
        certified one (page query plus the COUNT-wrapped certificate);
    (b) verifier time against delivered result size (former
        fig-efficiency, left panel), with descriptor construction and
        certificate-path exact-count verification flat;
    (c) the serialized descriptor against the model input a run already
        consumes.
    Receipts: evidence-overhead-itiger.json (sql_certificate),
    eval-verifier-scaling.json (timing), eval-unsupported-composition.json
    (run_input_tokens, descriptor_tokens_sql_frozen)."""
    sq = ov["sql_certificate"]
    page, cert = sq["page_query_ms"], sq["count_certificate_ms"]
    check_macro(mac, "pnSqlPageMs", f"{page:.1f}")
    check_macro(mac, "pnSqlCertMs", f"{cert:.1f}")
    assert sc["host"] == ov["host"], "panels a and b must share a host"

    # (a) two bar rows: y=1 uncertified, y=0 certified; labels sit
    # inside the bars on two lines, so the panel keeps its words at a
    # third of the text width
    a_items = [(page / 2, 1, "text=black, align=center",
                r"page query\\\pnSqlPageMs"),
               (page / 2, 0, "text=black, align=center",
                r"page query\\\pnSqlPageMs"),
               (page + cert / 2, 0, "text=white, align=center",
                r"certificate\\\pnSqlCertMs")]
    a_in, a_out = labels(a_items, "ca")

    # (b) verifier scaling, log-log
    t = sc["timing"]
    rows = [r["rows"] for r in t]
    assert rows == sc["sizes"]

    def pts(f):
        return " ".join(f"({r['rows']},{f(r):.5f})" for r in t)
    canon = pts(lambda r: r["canonicalize_ms"] + r["digest_ms"])
    cset = pts(lambda r: r["verify_completeset_ms"])
    mem = pts(lambda r: r["verify_membership_ms"])
    build = pts(lambda r: r["build_ecqr_ms"])
    cnt = pts(lambda r: r["verify_count_cert_ms"])
    check_macro(mac, "pnBuildUsFlat",
                f"{min(r['build_ecqr_ms'] for r in t)*1000:.1f}--"
                f"{max(r['build_ecqr_ms'] for r in t)*1000:.1f}")
    check_macro(mac, "pnCertVerifyUsFlat",
                f"{min(r['verify_count_cert_ms'] for r in t)*1000:.1f}--"
                f"{max(r['verify_count_cert_ms'] for r in t)*1000:.1f}")

    # (c) model input per run (median per dataset) and the descriptor
    rit = uc["run_input_tokens"]
    ops = [rit[f"{d}|operators"]["median"] / 1000 for d in DS]
    sql = [rit[f"{d}|sql"]["median"] / 1000 for d in DS]
    desc = uc["descriptor_tokens_sql_frozen"]["median"]
    check_macro(mac, "pnCtxTokMedOpsLo", f"{min(ops):.1f}")
    check_macro(mac, "pnCtxTokMedOpsHi", f"{max(ops):.1f}")
    check_macro(mac, "pnCtxTokMedSqlLo", f"{min(sql):.1f}")
    check_macro(mac, "pnCtxTokMedSqlHi", f"{max(sql):.1f}")
    check_macro(mac, "pnDescTokMed", desc)
    c_ops = " ".join(f"({v:.4f},2)" for v in ops)
    c_sql = " ".join(f"({v:.4f},1)" for v in sql)
    c_items = [(min(ops), 2, "anchor=east, xshift=-3pt",
                r"\pnCtxTokMedOpsLo--\pnCtxTokMedOpsHi\,k"),
               (max(sql), 1, "anchor=west, xshift=2pt",
                r"\pnCtxTokMedSqlLo--\pnCtxTokMedSqlHi\,k"),
               (desc / 1000, 0, "anchor=west, xshift=2pt, text=figAccent",
                r"\pnDescTokMed")]
    c_in, c_out = labels(c_items, "cc")
    xmax_c = 15
    assert max(ops) < xmax_c and max(sql) < min(ops) - 6, "re-place labels"
    xmax_a = 30
    assert page + cert < xmax_a, "panel a axis too short"

    # three panels in one row of a figure*: every plot box has the same
    # height and the same top edge; widths are fixed per panel so the
    # tick labels and the plot boxes together fill the text width
    H = r"height=2.6cm"
    title = (r"title style={at={(0,1)}, anchor=south west, xshift=-2pt, "
             r"yshift=-1pt, align=left}")
    return rf"""{GEN}
% Sources: benchmarks/results-v1/evidence-overhead-itiger.json
% (sql_certificate), eval-verifier-scaling.json (timing), and
% eval-unsupported-composition.json (run_input_tokens,
% descriptor_tokens_sql_frozen).
\begin{{figure*}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[name=a, xbar stacked, {SQ}, scale only axis,
  width=0.2\textwidth, {H},
  bar width=20pt, xmin=0, xmax={xmax_a}, xtick={{0,10,20,30}},
  ytick={{1,0}}, yticklabels={{uncertified,certified}},
  ymin=-0.6, ymax=1.6, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left, xlabel={{time (ms)}},
  title={{SQL path: answer with and\\without a count certificate}},
  {title}]
\addplot[fill=figLight, draw=white, line width=0.4pt]
  coordinates {{({page},1) ({page},0)}};
\addplot[fill=figAccent, draw=white, line width=0.4pt]
  coordinates {{(0,1) ({cert},0)}};
{a_in}
\end{{axis}}
{a_out}
\begin{{loglogaxis}}[name=b, at={{(a.north east)}}, anchor=north west,
  xshift=1.35cm, scale only axis, width=0.25\textwidth, {H},
  xmin=6, xmax=1.6e5, ymin=1.5e-5, ymax=2e4,
  ytick={{1e-4,1e-2,1e0,1e2,1e4}},
  axis x line*=bottom, axis y line*=left, ymajorgrids,
  xlabel={{delivered rows}}, ylabel={{time (ms)}},
  title={{verifier work per\\delivered result}}, {title},
  legend style={{at={{(0.02,0.98)}}, anchor=north west}},
  legend cell align=left,
  every axis plot/.append style={{line width=0.8pt, mark size=1.5pt}}]
\addplot[figDark, mark=*] coordinates {{{canon}}};
\addplot[figMid, mark=square*] coordinates {{{cset}}};
\addplot[figMid, mark=triangle*, densely dashed] coordinates {{{mem}}};
\addplot[figDark, mark=diamond*, densely dotted, forget plot]
  coordinates {{{build}}};
\addplot[figAccent, mark=*, forget plot] coordinates {{{cnt}}};
\legend{{canonicalize + digest, complete set, membership}}
\coordinate (bbuild) at (axis cs:1e5,{t[-1]['build_ecqr_ms']:.5f});
\coordinate (bcnt) at (axis cs:1e5,{t[-1]['verify_count_cert_ms']:.5f});
\end{{loglogaxis}}
\node[{DL}, anchor=south east, yshift=2pt, align=right] at (bbuild)
  {{descriptor\\construction}};
\node[{DL}, anchor=north east, yshift=-2pt, text=figAccent] at (bcnt)
  {{exact count via certificate}};
\begin{{axis}}[name=c, at={{(b.north east)}}, anchor=north west,
  xshift=2.75cm, scale only axis, width=0.18\textwidth, {H},
  xmin=0, xmax={xmax_c},
  xtick={{0,5,10,15}}, ytick={{2,1,0}},
  yticklabels={{Operators run input,SQL run input,ECQR descriptor}},
  ymin=-0.6, ymax=2.6, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left, xmajorgrids,
  xlabel={{tokens (thousands)}},
  title={{model input per run against\\one descriptor (medians)}},
  {title}]
\addplot[only marks, mark=*, mark size=1.8pt, figDark]
  coordinates {{{c_ops}}};
\addplot[only marks, mark=*, mark size=1.8pt, figDark]
  coordinates {{{c_sql}}};
\addplot[only marks, mark=*, mark size=1.8pt, figAccent]
  coordinates {{({desc / 1000:.3f},0)}};
{c_in}
\end{{axis}}
{c_out}
\end{{tikzpicture}}
\caption{{Evidence cost: an SQL answer without and with the
count-wrapped query that certifies its exact cardinality (left), verifier
time against delivered result size (middle), and the median serialized
descriptor of the SQL runs against the median model input per task-run,
one dot per dataset (right). Checking and carrying evidence are cheap;
producing a strong certificate can cost as much as the query it
certifies.}}
\label{{fig:cost}}
\end{{figure*}}
"""


def fig_probe_ecqr(tp: dict, mac: dict) -> str:
    """Truncation probe with the ECQR gate (lane D, D-170): what the user
    is shown per question family under the three baseline endpoints
    (committed eval-trunc-probe.json) and the two gated conditions
    (eval-trunc-probe-ecqr.json). Each bar splits the family's questions
    into correct shown, wrong shown, withheld (proposed, not certified)
    and no answer; without the gate every committed answer is shown."""
    pe = json.loads((RES / "eval-trunc-probe-ecqr.json").read_text())
    conds = [("C0", "bare page"), ("C1", "+ truncation flag"),
             ("C2", "+ exact total"), ("E", "ECQR gate (E)"),
             ("E-aware", "aware gate (E-aware)")]
    fams = [("COUNT", "count"), ("SET", "set")]

    def row(cond: str, fam: str) -> dict:
        if cond in ("E", "E-aware"):
            b = pe["conditions"][cond]["by_family"][fam]
            o = b["outcome"]
            assert o["error"] == 0
            return {"n": b["n"], "correct": b["certified_correct"],
                    "wrong": b["user_visible_wrong"],
                    "withheld": o["withheld"], "none": o["abstained"]}
        b = tp["conditions"][cond]["by_family"][fam]
        return {"n": b["n"], "correct": b["correct"],
                "wrong": b["page_derived"] + b["other_wrong"],
                "withheld": 0, "none": b["no_commitment"] + b["error"]}

    def pct(a, n):
        return 100.0 * a / n
    check_macro(mac, "pnProbeCountN", pe["conditions"]["E"]["by_family"]
                ["COUNT"]["n"])
    check_macro(mac, "pnProbeSetN", pe["conditions"]["E"]["by_family"]
                ["SET"]["n"])
    check_macro(mac, "pnPeEWrongN", pe["conditions"]["E"]["all"]
                ["user_visible_wrong"])
    bw = []
    for c in ("C0", "C1", "C2"):
        a = tp["conditions"][c]["all"]
        bw.append(pct(a["page_derived"] + a["other_wrong"], a["n"]))
    check_macro(mac, "pnPeBaseWrongLo", f"{min(bw):.1f}")
    check_macro(mac, "pnPeBaseWrongHi", f"{max(bw):.1f}")
    segs = [("correct", "correct shown", "figDark", "white"),
            ("wrong", "wrong shown", "figAccent", "white"),
            ("withheld", "withheld", "figMid", "white"),
            ("none", "no answer", "figLight", "black")]
    # y positions: count group on top, a gap, then the set group
    ys, ticks = {}, []
    y = 0.0
    for fam, flab in reversed(fams):
        for cond, clab in reversed(conds):
            ys[(fam, cond)] = round(y, 2)
            ticks.append((round(y, 2), clab))
            y += 1.0
        y += 0.9
    plots, notes = [], []
    cum = {k: 0.0 for k in ys}
    for key, _lab, fill, txt in segs:
        pts = []
        for fam, _f in fams:
            for cond, _c in conds:
                r = row(cond, fam)
                v = pct(r[key], r["n"])
                yy = ys[(fam, cond)]
                pts.append(f"({v:.1f},{yy})")
                if v >= 9:
                    notes.append((round(cum[(fam, cond)] + v / 2, 2), yy,
                                  f"text={txt}", f"{v:.0f}"))
                cum[(fam, cond)] += v
        plots.append(rf"\addplot[fill={fill}, draw=white, line width=0.4pt] "
                     rf"coordinates {{{' '.join(pts)}}};")
    for k, v in cum.items():
        assert abs(v - 100.0) < 1e-6, (k, v)
    lin, lout = labels(notes, "pe")
    ticks.sort()
    yt = ",".join(str(t) for t, _ in ticks)
    ytl = ",".join(f"{{{lab}}}" for _t, lab in ticks)
    # family headers above each group
    top_set = max(ys[("SET", c)] for c, _ in conds)
    top_cnt = max(ys[("COUNT", c)] for c, _ in conds)
    heads = [(top_cnt + 0.75, rf"count questions (\pnProbeCountN)"),
             (top_set + 0.75, rf"set questions (\pnProbeSetN)")]
    hin = "\n".join(rf"\coordinate (peh{i}) at (axis cs:0,{yy:.2f});"
                    for i, (yy, _t) in enumerate(heads))
    hout = "\n".join(rf"\node[anchor=west, inner sep=0pt, xshift=3pt] at (peh{i}) "
                     rf"{{{t}}};" for i, (_y, t) in enumerate(heads))
    ymax = top_cnt + 1.1
    leg = ", ".join(s[1] for s in segs)
    return rf"""{GEN}
% Source: benchmarks/results-v1/eval-trunc-probe.json (C0, C1, C2 by
% family) and eval-trunc-probe-ecqr.json (E, E-aware by family).
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[xbar stacked, {SQ}, width=0.70\linewidth, y=0.29cm,
  bar width=5.8pt, xmin=0, xmax=100, xtick={{0,20,40,60,80,100}},
  xlabel={{questions of the family (\%)}},
  ytick={{{yt}}}, yticklabels={{{ytl}}},
  ymin=-0.6, ymax={ymax:.2f}, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left,
  legend style={{at={{(0.5,1.0)}}, anchor=south, legend columns=4,
    /tikz/every even column/.append style={{column sep=4pt}}}},
  legend cell align=left]
{chr(10).join(plots)}
\legend{{{leg}}}
{lin}
{hin}
\end{{axis}}
{lout}
{hout}
\end{{tikzpicture}}
\caption{{Truncation probe with the ECQR gate: for each count and set
question, whether the user is shown a correct answer, a wrong one, a
withheld one, or none, under the three endpoints of
Fig.~\ref{{fig:probe}} and the two gated conditions. Without the gate
most shown answers are wrong; the gate shows \pnPeEWrongN\ wrong
answers and pays for it in withheld and unanswered questions.}}
\label{{fig:probeecqr}}
\end{{figure}}
"""


ALL_FIGS = ["main", "frontier", "conformance", "reasons", "efficiency",
            "probe", "rq1", "bird-census", "ldbc", "cost", "topk",
            "probe-ecqr"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    help="a file or directory inside the paper dir")
    ap.add_argument("--outdir", type=Path,
                    help="the paper dir (alternative to --out)")
    ap.add_argument("--only", nargs="+", choices=ALL_FIGS,
                    help="write only these fig-<name>.tex files")
    args = ap.parse_args()
    if not (args.out or args.outdir):
        ap.error("--out or --outdir is required")
    pn = json.loads((RES / "paper_numbers.json").read_text())
    bc = json.loads((RES / "eval-baseline-checkers.json").read_text())
    fm = json.loads((RES / "eval-fault-matrix.json").read_text())
    uc = json.loads(
        (RES / "eval-unsupported-composition.json").read_text())
    sc = json.loads((RES / "eval-verifier-scaling.json").read_text())
    ov = json.loads((RES / "evidence-overhead-itiger.json").read_text())
    tp = json.loads((RES / "eval-trunc-probe.json").read_text())
    lr = json.loads((RES / "eval-ldbc-coverage.json").read_text())
    ann = [json.loads(line) for line in LDBC_ANN.read_text().splitlines()
           if line.strip()]
    if args.outdir:
        outdir = args.outdir
    else:
        outdir = args.out if args.out.is_dir() else args.out.parent
    mac = load_macros(outdir / "pn-macros.tex")
    palette = load_palette(outdir / "preamble-shared.tex")
    makers = {
        "main": lambda: fig5(pn, uc),
        "frontier": lambda: fig6(pn),
        "conformance": lambda: fig_conformance(bc, fm, mac, palette),
        "reasons": fig_reasons_stub,
        "efficiency": fig_efficiency_stub,
        "cost": lambda: fig_cost(sc, ov, uc, mac),
        "probe": lambda: fig_probe(tp, mac),
        "rq1": lambda: fig_rq1(bc, fm, mac),
        "bird-census": fig_bird_census_stub,
        "ldbc": lambda: fig_ldbc(ann, lr, mac),
        "topk": lambda: fig_topk(mac),
        "probe-ecqr": lambda: fig_probe_ecqr(tp, mac),
    }
    names = args.only or ALL_FIGS
    for name in names:
        (outdir / f"fig-{name}.tex").write_text(makers[name]())
    print(f"wrote {', '.join(f'fig-{n}' for n in names)} to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
