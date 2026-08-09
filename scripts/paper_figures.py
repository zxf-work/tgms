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


def fig5(pn: dict) -> str:
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
    return rf"""% F-main — need / safety-coverage / utility (round-3 review)
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
\begin{{axis}}[at={{(b.outer east)}}, anchor=outer west,
  xshift=2mm, {ax}, ymax=0.6,
  ylabel={{\scriptsize correct-certified coverage}}]
\addplot coordinates {{{ccc_o}}};
\addplot coordinates {{{ccc_s}}};
\end{{axis}}
\end{{tikzpicture}}
\caption{{Evidence need and certified-output tradeoff. Pre-gate UCR
measures unsupported proposed claims; certified-output coverage
measures answer availability after enforcement; correct-certified
coverage measures useful certified output. SQL UCR is defined within
the SQL-conservative claim surface and is not compared with operator
UCR.}}
\label{{fig:frozen}}
\end{{figure*}}
"""


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
    """Horizontal decision strip: 27 cells x 3 checkers."""
    def classify(c):
        if c["ok"]:
            return "okc"
        if c["expectation"] == "must_not_certify":
            return "fac"          # certified an injected fault
        return "frc"              # rejected a clean control

    ecqr = [dict(c, ok=c["ok"]) for c in fm["cells"]]
    b1 = bc["b1_value_only"]["cells"]
    b2 = bc["b2_taint_all"]["cells"]
    n = len(ecqr)
    assert len(b1) == n and len(b2) == n

    labels, seen = [], {}
    for c in ecqr:
        key = (c["claim"], c["fault"])
        seen[key] = seen.get(key, 0) + 1
        lab = f"{CLAIM_AB[c['claim']]} {FAULT_AB[c['fault']]}"
        if c["fault"] == "clean" and seen[key] == 2:
            lab = f"{CLAIM_AB[c['claim']]} w/trunc"
        labels.append(lab)

    rows = [("value-only", b1), ("incompl.\\ taint", b2),
            ("ECQR", ecqr)]
    cw, ch = 0.29, 0.30
    cells_tex = []
    for ri, (_, cells) in enumerate(rows):
        for xi, c in enumerate(cells):
            cells_tex.append(
                rf"\fill[{classify(c)}] ({xi*cw:.2f},{-ri*ch:.2f}) "
                rf"rectangle ({(xi+1)*cw-0.04:.2f},"
                rf"{-ri*ch+ch-0.05:.2f});")
    body = "\n".join(cells_tex)
    labs = "\n".join(
        rf"\node[rotate=64, anchor=east, font=\fontsize{{4.6}}{{5.2}}"
        rf"\selectfont] at ({xi*cw+0.12:.2f},-0.68) {{{lab}}};"
        for xi, lab in enumerate(labels))
    rownames = "\n".join(
        rf"\node[anchor=east, font=\tiny] at (-0.08,{-ri*ch+0.12:.2f}) "
        rf"{{{rname}}};"
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
{body}
{rownames}
{labs}
\node[anchor=west, font=\tiny] at (0.4,0.5)
  {{\tikz{{\fill[okc] (0,0) rectangle (0.18,0.18);}} correct\quad
   \tikz{{\fill[fac] (0,0) rectangle (0.18,0.18);}} false accept\quad
   \tikz{{\fill[frc] (0,0) rectangle (0.18,0.18);}} false reject}};
\end{{tikzpicture}}
\caption{{Conformance decisions over the \pnCells\ EvidenceBench
cells. Columns are claim/fault cells in matrix order; \emph{{w/trunc}}
marks the two controls whose evidence is truncated yet sufficient.
The simple checkers fail in opposite directions; the verifier makes
no error.}}
\label{{fig:conformance}}
\end{{figure}}
"""


def fig_reasons(uc: dict) -> str:
    comp = uc["composition"]
    dsrow = {"sx-mathoverflow": "MathOverflow",
             "sx-superuser": "SuperUser", "wiki-talk": "wiki-talk"}
    rows = []
    for ds in DS:
        k = comp[f"{ds}|operators"]["unsupported_claims_by_kind"]
        rows.append((f"{dsrow[ds]} / Ops", k.get("count", 0),
                     k.get("entity", 0), k.get("value", 0), 0))
    for ds in DS:
        v = comp[f"{ds}|sql"]["claim_verdicts"]
        nw = sum(x for kk, x in v.items() if kk != "SUPPORTED")
        rows.append((f"{dsrow[ds]} / SQL", 0, 0, 0, nw))
    sym = ",".join(r[0] for r in reversed(rows))
    series = [("exact count", 1), ("entity witness", 2),
              ("cited value", 3), ("witness (SQL)", 4)]
    plots = "\n".join(
        rf"\addplot coordinates {{"
        + " ".join(f"({r[si]},{r[0]})" for r in rows) + "};"
        for _, si in series)
    legend = ", ".join(lab for lab, _ in series)
    return rf"""% F-reasons — generated from eval-unsupported-composition.json
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[xbar stacked, width=0.80\linewidth, height=3.4cm,
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
\caption{{Composition of unsupported pre-gate proposals: operator
rows by proposed-claim kind over the determinate runs, SQL rows by
verdict. Six operator runs with fractional per-run UCR are excluded
rather than allocated.}}
\label{{fig:reasons}}
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
\begin{{loglogaxis}}[name=a, width=0.58\linewidth, height=4.2cm,
  xlabel={{\scriptsize delivered rows}},
  ylabel={{\scriptsize ms}},
  x tick label style={{font=\tiny}}, y tick label style={{font=\tiny}},
  label style={{font=\scriptsize}},
  legend style={{font=\tiny, at={{(0.02,0.98)}}, anchor=north west}},
  legend cell align=left,
  every axis plot/.append style={{mark size=1.3pt}}]
\addplot+[mark=*] coordinates {{{canon}}};
\addplot+[mark=square*] coordinates {{{mem}}};
\addplot+[mark=triangle*] coordinates {{{cset}}};
\addplot+[mark=o, dashed] coordinates {{{cert}}};
\legend{{canon.+digest, membership, complete set, count cert.}}
\end{{loglogaxis}}
\begin{{axis}}[at={{(a.outer east)}}, anchor=outer west, xshift=1mm,
  width=0.40\linewidth, height=4.2cm, ybar, ymode=log,
  symbolic x coords={{page query,certificate,descriptors}},
  xtick=data, x tick label style={{font=\tiny, rotate=25,
  anchor=east}}, y tick label style={{font=\tiny}},
  ylabel={{\scriptsize ms (log)}}, label style={{font=\scriptsize}},
  bar width=9pt, log origin=infty, enlarge x limits=0.3]
\addplot coordinates {{(page query,{page_ms}) (certificate,{cert_ms})
  (descriptors,{plan_ms})}};
\end{{axis}}
\end{{tikzpicture}}
\caption{{Checking is result-local and cheap; strong evidence is
query-scale. Left: median verification cost against delivered-result
size; the certificate path stays flat at
\pnCertVerifyUsFlat\,$\mu$s. Right: the SQL cardinality certificate
costs about one page query, while whole-plan descriptor production
costs {plan_ms}\,ms.}}
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
    (outdir / "fig-main.tex").write_text(fig5(pn))
    (outdir / "fig-frontier.tex").write_text(fig6(pn))
    (outdir / "fig-conformance.tex").write_text(fig_conformance(bc, fm))
    (outdir / "fig-reasons.tex").write_text(fig_reasons(uc))
    (outdir / "fig-efficiency.tex").write_text(fig_efficiency(sc, ov))
    print(f"wrote fig-main, fig-frontier, fig-conformance, "
          f"fig-reasons, fig-efficiency to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
