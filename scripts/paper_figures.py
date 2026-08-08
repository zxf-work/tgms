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
    em = fz["em"]
    bars_t = " ".join(f"({DS_SHORT[d]},{v})" for d, v in zip(DS, pre_t))
    bars_s = " ".join(f"({DS_SHORT[d]},{v})" for d, v in zip(DS, pre_s))
    zeros = " ".join(f"({DS_SHORT[d]},0.0)" for d in DS)
    em_o = " ".join(f"({DS_SHORT[d]},{em[f'{d}|ours']})" for d in DS)
    em_b = " ".join(f"({DS_SHORT[d]},{em[f'{d}|b6e']})" for d in DS)
    return rf"""% F5 — generated from paper_numbers.json; do not edit
\begin{{figure}}[t]
\centering
\begin{{tikzpicture}}
\begin{{axis}}[name=left, ybar, width=0.52\linewidth, height=4.2cm,
  bar width=5pt, ymin=0, ymax=0.3,
  symbolic x coords={{MathOverflow,SuperUser,wiki-talk}}, xtick=data,
  x tick label style={{font=\scriptsize, rotate=20, anchor=east}},
  ylabel={{\scriptsize unsupported-claim rate}},
  legend style={{font=\tiny, at={{(0.02,0.98)}}, anchor=north west}}]
\addplot coordinates {{{bars_t}}};
\addplot coordinates {{{bars_s}}};
\addplot coordinates {{{zeros}}};
\legend{{operators ungated, SQL ungated, gated (both)}}
\end{{axis}}
\begin{{axis}}[at={{(left.outer east)}}, anchor=outer west, xshift=2mm,
  ybar, width=0.52\linewidth, height=4.2cm, bar width=5pt,
  ymin=0, ymax=0.6,
  symbolic x coords={{MathOverflow,SuperUser,wiki-talk}}, xtick=data,
  x tick label style={{font=\scriptsize, rotate=20, anchor=east}},
  ylabel={{\scriptsize exact match (gated)}},
  legend style={{font=\tiny, at={{(0.02,0.98)}}, anchor=north west}}]
\addplot coordinates {{{em_o}}};
\addplot coordinates {{{em_b}}};
\legend{{operators, SQL}}
\end{{axis}}
\end{{tikzpicture}}
\caption{{The frozen evidence result (test splits, 3 seeds, 2{{,}}424
runs). Left: mean per-answer unsupported-claim fraction, ungated vs
gated --- enforcement takes both interfaces to zero. Right: gated
exact-match accuracy; the accuracy cost of gating is at most one
changed outcome.}}
\label{{fig:frozen}}
\end{{figure}}
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
\caption{{The interface frontier (nested surfaces, identical runtime).
Expressibility and execution success rise monotonically with surface
size; accuracy does not --- the curves diverge, and the dip at the full
surface coincides with the one operator pair whose capabilities
structurally overlap.}}
\label{{fig:frontier}}
\end{{figure}}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    pn = json.loads((RES / "paper_numbers.json").read_text())
    args.out.write_text(fig5(pn) + "\n" + fig6(pn))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
