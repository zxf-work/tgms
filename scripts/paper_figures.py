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
          "xtick=data, x tick label style={rotate=25, anchor=north east, "
          "inner sep=1pt}, ybar, bar width=5pt, width=0.355\\linewidth, "
          "height=3.4cm, ymin=0, enlarge x limits=0.22, "
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
  yshift=-1mm, xbar stacked, {SQ}, width=0.975\linewidth,
  height=4.0cm, symbolic y coords={{{sym}}}, ytick=data, xmin=0,
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


def fig_conformance(bc: dict, fm: dict) -> str:
    """Horizontal decision strip: 27 cells x 3 checkers, grouped."""
    def classify(c):
        if c["ok"]:
            return "okc"
        if c["expectation"] == "must_not_certify":
            return "fac"          # certified an injected fault
        return "frc"              # rejected a clean control

    GROUP = {"clean": "controls", "page_truncation": "completeness",
             "execution_incomplete": "execution",
             "wrong_count": "value/witness", "wrong_scalar":
             "value/witness", "omitted_member": "value/witness",
             "fabricated_member": "value/witness", "false_membership":
             "value/witness", "false_existence": "value/witness",
             "false_nonexistence": "value/witness",
             "wrong_snapshot": "basis", "unpinned_snapshot": "basis",
             "uncited_value": "citation",
             "digest_mismatch": "integrity"}

    ecqr = [dict(c, ok=c["ok"]) for c in fm["cells"]]
    b1 = bc["b1_value_only"]["cells"]
    b2 = bc["b2_taint_all"]["cells"]
    n = len(ecqr)
    assert len(b1) == n and len(b2) == n

    labels, seen = [], {}
    for c in ecqr:
        key = (c["claim"], c["fault"])
        seen[key] = seen.get(key, 0) + 1
        lab = CLAIM_AB[c["claim"]]
        if c["fault"] == "clean" and seen[key] == 2:
            lab = f"{CLAIM_AB[c['claim']]}*"
        labels.append(lab)

    rows = [("value-only", b1), ("incompl.\\ taint", b2),
            ("ECQR", ecqr)]
    cw, ch = 0.29, 0.32
    cells_tex = []
    for ri, (_, cells) in enumerate(rows):
        for xi, c in enumerate(cells):
            cells_tex.append(
                rf"\fill[{classify(c)}] ({xi*cw:.2f},{-ri*ch:.2f}) "
                rf"rectangle ({(xi+1)*cw-0.04:.2f},"
                rf"{-ri*ch+ch-0.05:.2f});")
    body = "\n".join(cells_tex)
    labs = "\n".join(
        rf"\node[anchor=north, font=\fontsize{{5.2}}{{5.6}}"
        rf"\selectfont] at ({xi*cw+0.12:.2f},-0.70) {{{lab}}};"
        for xi, lab in enumerate(labels))
    # group brackets above the strip
    groups, start = [], 0
    for i in range(1, n + 1):
        if i == n or GROUP[ecqr[i]["fault"]] != GROUP[ecqr[start]["fault"]]:
            groups.append((GROUP[ecqr[start]["fault"]], start, i - 1))
            start = i
    gtex = []
    for gname, a, b in groups:
        x0, x1 = a * cw, (b + 1) * cw - 0.04
        xm = (x0 + x1) / 2
        gtex.append(
            rf"\draw[black!60] ({x0:.2f},0.42) -- ({x0:.2f},0.50) -- "
            rf"({x1:.2f},0.50) -- ({x1:.2f},0.42);")
        if (x1 - x0) < 0.85:
            gtex.append(
                rf"\node[anchor=south west, rotate=35, "
                rf"font=\fontsize{{5.2}}{{5.6}}\selectfont] at "
                rf"({xm - 0.06:.2f},0.52) {{{gname}}};")
        else:
            gtex.append(
                rf"\node[anchor=south, font=\fontsize{{5.6}}{{6}}"
                rf"\selectfont] at ({xm:.2f},0.52) {{{gname}}};")
    gbody = "\n".join(gtex)
    rownames = "\n".join(
        rf"\node[anchor=east, font=\scriptsize] at "
        rf"(-0.08,{-ri*ch+0.13:.2f}) {{{rname}}};"
        for ri, (rname, _) in enumerate(rows))
    return rf"""% F-conformance — generated from eval-baseline-checkers.json +
% eval-fault-matrix.json; do not edit
\begin{{figure*}}[t]
\centering
\begin{{tikzpicture}}
\definecolor{{okcol}}{{RGB}}{{223,232,223}}
\definecolor{{facol}}{{RGB}}{{176,49,44}}
\definecolor{{frcol}}{{RGB}}{{230,159,0}}
\tikzset{{okc/.style={{fill=okcol}}, fac/.style={{fill=facol}},
  frc/.style={{fill=frcol}}}}
{gbody}
{body}
{rownames}
{labs}
\node[anchor=west, font=\scriptsize] at (0.2,-1.15)
  {{\tikz{{\fill[okc] (0,0) rectangle (0.18,0.18);}} correct\quad
   \tikz{{\fill[fac] (0,0) rectangle (0.18,0.18);}} false accept\quad
   \tikz{{\fill[frc] (0,0) rectangle (0.18,0.18);}} false reject}};
\end{{tikzpicture}}
\caption{{Conformance decisions over the \pnCells\ EvidenceBench
cells, grouped by fault family; column labels give the claim form
(mem, scl, cnt, set, ext, nex, bas), and * marks the two controls
whose evidence is truncated yet sufficient. The simple checkers fail
in opposite directions; the verifier matches every expected
verdict.}}
\label{{fig:conformance}}
\end{{figure*}}
"""


def fig_efficiency(sc: dict, ov: dict) -> str:
    t = sc["timing"]
    canon = " ".join(
        f"({r['rows']},{r['canonicalize_ms']+r['digest_ms']:.5f})"
        for r in t)
    mem = " ".join(f"({r['rows']},{r['verify_membership_ms']})" for r in t)
    cset = " ".join(f"({r['rows']},{r['verify_completeset_ms']})"
                    for r in t)
    cert = " ".join(f"({r['rows']},{r['verify_count_cert_ms']})"
                    for r in t)
    page_ms = ov["sql_certificate"]["page_query_ms"]
    cert_ms = ov["sql_certificate"]["count_certificate_ms"]
    plan_ms = ov["plan_overhead"]["overhead_ms"]
    return rf"""% F-efficiency — generated from eval-verifier-scaling.json +
% evidence-overhead-itiger.json
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{loglogaxis}}[name=a, width=0.58\linewidth, height=4.4cm,
  xlabel={{delivered rows}},
  ylabel={{ms}},
  x tick label style={{font=\scriptsize}},
  y tick label style={{font=\scriptsize}},
  label style={{font=\small}},
  legend style={{font=\scriptsize, at={{(0.02,0.98)}},
    anchor=north west, draw=none, fill=none}},
  legend cell align=left,
  every axis plot/.append style={{mark size=1.5pt}}]
\addplot+[mark=*] coordinates {{{canon}}};
\addplot+[mark=square*] coordinates {{{mem}}};
\addplot+[mark=triangle*] coordinates {{{cset}}};
\addplot+[mark=o, dashed, thick] coordinates {{{cert}}};
\legend{{canon.+digest, membership, complete set}}
\node[font=\scriptsize, anchor=west]
  at (axis cs:20,0.0004) {{count certificate (flat)}};
\end{{loglogaxis}}
\begin{{axis}}[at={{(a.outer east)}}, anchor=outer west, xshift=1mm,
  width=0.40\linewidth, height=4.4cm, ybar, ymode=log,
  symbolic x coords={{page query,certificate,descriptors}},
  xtick=data, x tick label style={{font=\scriptsize, rotate=25,
  anchor=east}}, y tick label style={{font=\scriptsize}},
  ylabel={{ms (log)}}, label style={{font=\small}},
  bar width=10pt, log origin=infty, enlarge x limits=0.3,
  nodes near coords, every node near coord/.append style={{
    font=\scriptsize, anchor=south}},
  point meta=rawy]
\addplot coordinates {{(page query,{page_ms}) (certificate,{cert_ms})
  (descriptors,{plan_ms})}};
\end{{axis}}
\end{{tikzpicture}}
\caption{{Cost of checking versus producing evidence.
\emph{{Left}}: result-local verification scales with delivered
output size, while certificate-path checks stay flat at
\pnCertVerifyUsFlat\,$\mu$s after binding. \emph{{Right}}: in the
measured SQL path, producing an exact-cardinality certificate
costs approximately one additional query, while whole-plan
descriptor production costs {plan_ms}\,ms.}}
\label{{fig:efficiency}}
\end{{figure}}
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
\begin{{axis}}[xbar, bar shift=0pt, {SQ}, width=0.92\linewidth, y=0.62cm,
  bar width=5pt, xmin=0, xmax=68, xtick={{0,20,40,60}},
  ytick={{{yt}}}, yticklabels={{{ytl}}},
  ymin=-0.3, ymax=5.4, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left, xmajorgrids,
  xlabel={{answers (\%)}},
  point meta=explicit symbolic, nodes near coords,
  every node near coord/.append style={{{DLX}}},
  legend style={{at={{(0.5,1.02)}}, anchor=south, legend columns=-1,
    /tikz/every even column/.append style={{column sep=6pt}}}}]
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
    """Errors of the two simple checkers and of the ECQR verifier per
    EvidenceBench fault family (receipts: eval-baseline-checkers.json,
    eval-fault-matrix.json). A control cell can only be falsely rejected
    and a fault cell only falsely accepted, so the figure has one panel
    for each error kind."""
    from paper_fault_families import FAMILY, ORDER
    cells = fm["cells"]
    assert set(c["fault"] for c in cells) <= set(FAMILY), "unmapped fault"

    def errs(key):
        d = bc[key]
        return set(d.get("fc_cells", [])) | set(d.get("fr_cells", []))

    b1, b2 = errs("b1_value_only"), errs("b2_taint_all")
    fams = []
    for fam in ORDER:
        sub = [c for c in cells if FAMILY[c["fault"]] == fam]
        if not sub:
            continue
        ids = {f"{c['claim']}/{c['fault']}" for c in sub}
        kinds = {c["expectation"] for c in sub}
        assert len(kinds) == 1, (fam, kinds)
        fams.append((fam, len(sub), len(ids & b1), len(ids & b2),
                     sum(1 for c in sub if not c["ok"]), kinds.pop()))
    check_macro(mac, "pnCells", len(cells))
    check_macro(mac, "pnValueOnlyFail", sum(f[2] for f in fams))
    check_macro(mac, "pnTaintFail", sum(f[3] for f in fams))
    check_macro(mac, "pnControlCellsN",
                sum(f[1] for f in fams if f[5] == "must_certify"))
    check_macro(mac, "pnFaultCellsN",
                sum(f[1] for f in fams if f[5] != "must_certify"))
    assert sum(f[4] for f in fams) == 0, "ECQR must match every cell"
    short = {"controls (must certify)": "controls"}

    def panel(sel, name, extra, title, xlabel):
        sub = [f for f in fams if sel(f)]
        ys = list(range(len(sub)))[::-1]          # first family on top
        ytl = ",".join(f"{{{short.get(f[0], f[0])} ({f[1]})}}" for f in sub)

        def series(i, dy):
            return " ".join(f"({f[i]},{y + dy:.2f}) [{f[i]}]"
                            for f, y in zip(sub, ys))
        xl = f"xlabel={{{xlabel}}}, " if xlabel else ""
        return rf"""\begin{{axis}}[name={name}, {extra}xbar, bar shift=0pt,
  {SQ}, width=0.84\linewidth, y=0.78cm, bar width=4.5pt, xmin=0, xmax=3.4,
  xtick={{0,1,2,3}}, {xl}
  ytick={{{",".join(map(str, ys))}}}, yticklabels={{{ytl}}},
  ymin=-0.55, ymax={len(sub) - 0.45}, y tick style={{draw=none}},
  clip=false,
  axis x line*=bottom, axis y line*=left, xmajorgrids,
  title={{{title}}}, title style={{at={{(0,1)}}, anchor=south west,
    xshift=-2pt, yshift=-1pt}},
  point meta=explicit symbolic, nodes near coords,
  every node near coord/.append style={{{DLX}}},
  legend style={{at={{(0.5,1)}}, anchor=south, yshift=14pt,
    legend columns=-1,
    /tikz/every even column/.append style={{column sep=6pt}}}}]
\addplot[fill=figDark, draw=none] coordinates {{{series(2, 0.25)}}};
\addplot[fill=figMid, draw=none] coordinates {{{series(3, 0.0)}}};
\addplot[only marks, mark=*, mark size=1.6pt, figAccent, {MK},
  every node near coord/.append style={{text=figAccent, xshift=1.5pt}}]
  coordinates {{{series(4, -0.25)}}};
"""

    top = panel(lambda f: f[5] == "must_certify", "a", "",
                r"false rejects on control cells", None)
    bot = panel(lambda f: f[5] != "must_certify", "b",
                "at={(a.south west)}, anchor=north west, yshift=-1.0cm, ",
                r"false accepts on fault cells", "cells in error")
    return rf"""{GEN}
% Source: benchmarks/results-v1/eval-baseline-checkers.json (b1_value_only,
% b2_taint_all) and eval-fault-matrix.json (ECQR); families as in
% scripts/paper_fault_families.py.
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
{top}\legend{{value-only, incompleteness taint, ECQR}}
\end{{axis}}
{bot}\end{{axis}}
\end{{tikzpicture}}
\caption{{EvidenceBench decisions that disagree with the expected
verdict, per fault family and checker, over the \pnCells\ cells (cells
per family in parentheses). Value-only checking accepts every
incompleteness and basis fault, the incompleteness taint rejects valid
controls yet still accepts both basis faults, and ECQR makes no error
in any family.}}
\label{{fig:rq1}}
\end{{figure}}
"""


def fig_bird_census(ba: dict, mac: dict) -> str:
    """BIRD questions per intent contract, split by certification outcome
    (receipt: eval-bird-agent.json, by_form; the census that
    scripts/paper_bird_census.py tabulates)."""
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
    return rf"""{GEN}
% Source: benchmarks/results-v1/eval-bird-agent.json (by_form).
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[xbar stacked, {SQ}, width=0.86\linewidth,
  y=0.5cm, bar width=7pt, xmin=0, xmax={xmax}, xtick={{0,100,200,300}},
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
{lout}
\end{{tikzpicture}}
\caption{{BIRD Mini-Dev questions by intent contract, split by
certification outcome; numbers at the bar ends give the questions that
meet the strict full contract over all questions of that contract. The
\pnBirdCertPartial\ runs between any-claim and strict-full certification
all lie in the complete-collection row.}}
\label{{fig:birdcensus}}
\end{{figure}}
"""


def fig_ldbc(ann: list, rec: dict, mac: dict) -> str:
    """LDBC SNB read templates per group (IC/IS/BI): execution coverage
    and full-contract claim coverage as two stacked panels (sources:
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

    def panel(name, key, classes, extra, title, xlabel):
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
        xl = f"xlabel={{{xlabel}}}, " if xlabel else ""
        lin, lout = labels(notes, f"l{name}")
        return rf"""\begin{{axis}}[name={name}, {extra}xbar stacked, {SQ},
  width=\linewidth, y=0.5cm, bar width=7pt, xmin=0, xmax={xmax},
  xtick={{0,5,10,15,20}}, {xl}
  ytick={{{",".join(map(str, ys))}}}, yticklabels={{{ytl}}},
  ymin=-0.6, ymax={len(groups) - 0.4}, y tick style={{draw=none}},
  axis x line*=bottom, axis y line*=left,
  title={{{title}}}, title style={{at={{(0,1)}}, anchor=south west,
    xshift=-2pt, yshift=10pt}},
  legend style={{at={{(0,1.0)}}, anchor=south west, legend columns=-1,
    /tikz/every even column/.append style={{column sep=4pt}}}}]
{chr(10).join(plots)}
\legend{{{leg}}}
{lin}
\end{{axis}}
{lout}
"""
    top = panel("a", "exec_coverage", [(c[0],) + c[1:] for c in exec_cls],
                "", "execution coverage", None)
    bot = panel("b", "claim_full_contract", claim_cls,
                "at={(a.south west)}, anchor=north west, yshift=-1.55cm, ",
                "claim coverage (full result contract)", "templates")
    return rf"""{GEN}
% Sources: external_workloads/ldbc/coverage_annotation.jsonl and
% benchmarks/results-v1/eval-ldbc-coverage.json.
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
{top}{bot}\end{{tikzpicture}}
\caption{{LDBC SNB read templates per group (template count in
parentheses): how each is executed, and the first full-contract feature
the claim grammar lacks, taking path, groupwise extremum, top-$k$, and
ordered result in that order. Most templates need the SQL path, and
top-$k$ is the dominant claim gap in the interactive and BI groups
alike.}}
\label{{fig:ldbc}}
\end{{figure}}
"""


ALL_FIGS = ["main", "frontier", "conformance", "reasons", "efficiency",
            "probe", "rq1", "bird-census", "ldbc"]


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
    ba = json.loads((RES / "eval-bird-agent.json").read_text())
    lr = json.loads((RES / "eval-ldbc-coverage.json").read_text())
    ann = [json.loads(line) for line in LDBC_ANN.read_text().splitlines()
           if line.strip()]
    if args.outdir:
        outdir = args.outdir
    else:
        outdir = args.out if args.out.is_dir() else args.out.parent
    mac = load_macros(outdir / "pn-macros.tex")
    makers = {
        "main": lambda: fig5(pn, uc),
        "frontier": lambda: fig6(pn),
        "conformance": lambda: fig_conformance(bc, fm),
        "reasons": fig_reasons_stub,
        "efficiency": lambda: fig_efficiency(sc, ov),
        "probe": lambda: fig_probe(tp, mac),
        "rq1": lambda: fig_rq1(bc, fm, mac),
        "bird-census": lambda: fig_bird_census(ba, mac),
        "ldbc": lambda: fig_ldbc(ann, lr, mac),
    }
    names = args.only or ALL_FIGS
    for name in names:
        (outdir / f"fig-{name}.tex").write_text(makers[name]())
    print(f"wrote {', '.join(f'fig-{n}' for n in names)} to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
