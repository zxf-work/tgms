#!/usr/bin/env python
"""Emit the paper's data figures (F5, F6) as pgfplots from receipts.

The no-hand-transcription rule covers figure data: this script reads
paper_numbers.json and the M8 tables and writes complete tikzpictures
to fig-data.tex. Re-running regenerates them byte-for-byte.

    python scripts/paper_figures.py --out paper/ecqr/fig-data.tex
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RES = Path("benchmarks/results-v1")
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
          "xtick=data, x tick label style={font=\\tiny, rotate=25, "
          "anchor=east}, bar width=4.5pt, ybar, width=0.36\\linewidth, "
          "height=3.1cm, ymin=0")

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
        rows.append((f"{dsrow[d]} / SQL", 0, 0, 0, 0, nw))
    sym = ",".join(r[0] for r in reversed(rows))
    series = [("exact count", 1), ("entity witness", 2),
              ("cited value", 3), ("undetermined", 4),
              ("witness (SQL)", 5)]
    plots = "\n".join(
        rf"\addplot coordinates {{"
        + " ".join(f"({r[si]},{r[0]})" for r in rows) + "};"
        for _, si in series)
    legend = ", ".join(lab for lab, _ in series)

    return rf"""% F-main — merged need/coverage/utility (row 1) + unsupported-claim
% composition (row 2); generated, do not edit.
\begin{{figure*}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[name=a, {ax}, ymax=0.3,
  ylabel={{\scriptsize pre-gate UCR}},
  legend style={{font=\scriptsize, at={{(0.02,0.98)}}, anchor=north west}}]
\addplot coordinates {{{bars_t}}};
\addplot coordinates {{{bars_s}}};
\legend{{Operators, SQL}}
\end{{axis}}
\begin{{axis}}[name=b, at={{(a.outer east)}}, anchor=outer west,
  xshift=2mm, {ax}, ymax=1.0,
  ylabel={{\scriptsize certified-output coverage}},
  legend style={{font=\scriptsize, at={{(0.02,0.3)}}, anchor=north west}}]
\addplot coordinates {{{cov_o}}};
\addplot coordinates {{{cov_s}}};
\legend{{Operators+ECQR, SQL+ECQR}}
\end{{axis}}
\begin{{axis}}[name=c, at={{(b.outer east)}}, anchor=outer west,
  xshift=2mm, {ax}, ymax=0.6,
  ylabel={{\scriptsize correct-certified coverage}}]
\addplot coordinates {{{ccc_o}}};
\addplot coordinates {{{ccc_s}}};
\end{{axis}}
\begin{{axis}}[name=d, at={{(a.south west)}}, anchor=north west,
  yshift=-1.55cm, xbar stacked, width=0.92\linewidth, height=3.2cm,
  symbolic y coords={{{sym}}}, ytick=data,
  y tick label style={{font=\tiny}}, xmin=0,
  x tick label style={{font=\tiny}},
  xlabel={{\scriptsize unsupported proposed claims}},
  bar width=4.5pt,
  legend style={{font=\tiny, at={{(0.98,0.03)}}, anchor=south east}},
  legend cell align=left]
{plots}
\legend{{{legend}}}
\end{{axis}}
\end{{tikzpicture}}
\caption{{Evidence need and enforcement effect. Unsupported claims
occur under both interfaces; operator failures span cardinality,
witness, and cited-value support. Certified output trades answer
availability for support while leaving audit-mode task accuracy
nearly unchanged.}}
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
\begin{{figure}}[t]
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
\end{{figure}}
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    pn = json.loads((RES / "paper_numbers.json").read_text())
    bc = json.loads((RES / "eval-baseline-checkers.json").read_text())
    fm = json.loads((RES / "eval-fault-matrix.json").read_text())
    uc = json.loads(
        (RES / "eval-unsupported-composition.json").read_text())
    sc = json.loads((RES / "eval-verifier-scaling.json").read_text())
    ov = json.loads((RES / "evidence-overhead-itiger.json").read_text())
    outdir = args.out if args.out.is_dir() else args.out.parent
    (outdir / "fig-main.tex").write_text(fig5(pn, uc))
    (outdir / "fig-frontier.tex").write_text(fig6(pn))
    (outdir / "fig-conformance.tex").write_text(fig_conformance(bc, fm))
    (outdir / "fig-reasons.tex").write_text(fig_reasons_stub())
    (outdir / "fig-efficiency.tex").write_text(fig_efficiency(sc, ov))
    print(f"wrote fig-main (merged with former fig-reasons), "
          f"fig-frontier, fig-conformance, fig-reasons (stub), "
          f"fig-efficiency to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
