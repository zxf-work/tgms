#!/usr/bin/env python
"""Emit the paper's number macros and data tables from receipts.

The no-hand-transcription rule, applied to prose and tables: every
receipt-derived number in the paper resolves through a macro defined
here (prose) or appears in a fully generated float (tables). Reading
paper_numbers.json and eval-fault-matrix.json, this writes

    pn-macros.tex   \\newcommand definitions, loaded in main.tex's
                    preamble; one macro per prose-cited number
    tab-data.tex    tables T2-T4 as complete floats

Re-running regenerates both byte-for-byte.

    python scripts/paper_macros.py --outdir paper/ecqr

Numbers deliberately NOT bound (hand-carried literals, provenance in
the tex comments where they appear):
  - the 50.5% CollegeMsg page-undercount (D-061 measurement note;
    no committed receipt on this branch)
  - RQ5's EM-given-expressible pair at the a1/a2 surfaces (D-107
    expressible-subset analysis; paper_numbers.json carries only
    all-row metrics — queued for the oracle-v3.1 regeneration)
  - public dataset scale constants (facts of the datasets, validated
    at load, not measurements)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RES = Path("benchmarks/results-v1")
DS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]
DS_SHORT = {"sx-mathoverflow": "MathOverflow", "sx-superuser": "SuperUser",
            "wiki-talk": "wiki-talk"}
CLAIM_ORDER = ["membership", "scalar", "exact_count", "complete_set",
               "existence", "nonexistence", "historical_basis"]
CLAIM_LABEL = {"membership": "Membership", "scalar": "Scalar",
               "exact_count": "Exact count", "complete_set": "Complete set",
               "existence": "Existence", "nonexistence": "Nonexistence",
               "historical_basis": "Historical basis"}


def f2(x: float) -> str:
    return f"{x:.2f}"


def f3(x: float) -> str:
    return f"{x:.3f}"


def ci2(ci: list[float]) -> str:
    lo, hi = ci
    def s(v: float) -> str:
        return f"{v:+.2f}".replace("+0.00", "0.00").replace("-0.00", "0.00")
    return f"[{s(lo)}, {s(hi)}]"


def macros(pn: dict, fm: dict) -> str:
    fz = pn["frozen_2x2"]
    fmx = pn["fault_matrix"]
    ov = pn["overhead"]
    gr = pn["guardrail"]
    o3 = pn["oracle_v3"]
    m6 = pn["m6_frontier"]

    fault_cells = sum(1 for c in fm["cells"]
                      if c["expectation"] == "must_not_certify")
    control_cells = sum(1 for c in fm["cells"]
                        if c["expectation"] == "must_certify")
    assert fault_cells + control_cells == fmx["cells"]

    tg, sq = fz["ucr_pre_gate_tgms"], fz["ucr_pre_gate_sql"]
    icon = fz["interface_contrasts"]
    mo_i = icon["sx-mathoverflow | interface: b6e - ours"]
    su_i = icon["sx-superuser | interface: b6e - ours"]

    cov = "/".join(f2(o3[d]["resolution_coverage"]) for d in DS)
    ana = "/".join(str(o3[d]["answerable_not_admitted"]) for d in DS)

    m = [
        ("pnPrimaryRows", f"{fz['primary_rows']:,}".replace(",", "{,}")),
        # ucr ranges, 2dp, min/max over datasets
        ("pnUcrPreLo", f2(fz["ucr_pre_range_all"][0])),
        ("pnUcrPreHi", f2(fz["ucr_pre_range_all"][1])),
        ("pnUcrPreRangePct",
         f"{fz['ucr_pre_range_all'][0]*100:.0f}--"
         f"{fz['ucr_pre_range_all'][1]*100:.0f}\\%"),
        ("pnUcrPreTgmsLo", f2(min(tg))), ("pnUcrPreTgmsHi", f2(max(tg))),
        ("pnUcrPreSqlLo", f2(min(sq))), ("pnUcrPreSqlHi", f2(max(sq))),
        ("pnUcrGatedZ", f3(max(fz["ucr_gated"]))),
        ("pnMaxEvCost", f3(fz["max_evidence_em_cost"])),
        # interface contrasts (prose cites MathOverflow + SuperUser)
        ("pnIfDeltaMo", f3(mo_i["delta_em"])),
        ("pnIfCiMo", ci2(mo_i["ci95"])),
        ("pnIfDeltaSu", f3(su_i["delta_em"])),
        # fault matrix
        ("pnCells", str(fmx["cells"])),
        ("pnFaultCellsN", str(fault_cells)),
        ("pnControlCellsN", str(control_cells)),
        ("pnFalseCert", str(fmx["false_certifications"])),
        ("pnFalseRej", str(fmx["false_rejections"])),
        ("pnFcCpUpper", f"{fmx['fc_cp95_upper']*100:.1f}\\%"),
        # oracle ladder
        ("pnOracleCov", cov),
        ("pnAnaLadder", ana),
        # overhead
        ("pnDescUsSmall", f"{ov['descriptor_us']['small_envelope']:g}"),
        ("pnDescUsLarge", f"{ov['descriptor_us']['large_envelope']:g}"),
        ("pnPlanUs", f"{ov['plan_overhead_ms']*1000:.0f}"),
        ("pnSqlCertRatio", f2(ov["sql_certificate_over_page"])),
        # guardrail
        ("pnGrFaXz", str(gr["xzgpu_at_2s"]["FA"])),
        ("pnGrFrXz", str(gr["xzgpu_at_2s"]["FR"])),
        ("pnGrNXz", str(gr["xzgpu_at_2s"]["n"])),
        ("pnGrFaCp", f"{gr['xzgpu_fa_cp95_upper']*100:.1f}\\%"),
        ("pnGrFaIt", str(gr["itiger_scaled_at_2s"]["FA"])),
        ("pnGrNIt", str(gr["itiger_scaled_at_2s"]["n"])),
        ("pnHostScale", f2(gr["host_scale_median"])),
        # M6 frontier (all-row metrics; prose subset numbers stay literal)
        ("pnFpvAi", f2(m6["a1"]["first_plan_valid"])),
        ("pnFpvAii", f2(m6["a2"]["first_plan_valid"])),
        ("pnEmAiii", f3(m6["a3"]["em"])),
        ("pnEmAiv", f3(m6["a4"]["em"])),
        ("pnFpvAiv", f2(m6["a4"]["first_plan_valid"])),
        ("pnFpvAivp", f2(m6["a4p"]["first_plan_valid"])),
        ("pnConfAiv", str(m6["a4"]["agg_series_confusions"])),
        ("pnConfAivp", str(m6["a4p"]["agg_series_confusions"])),
        ("pnMsixN", str(m6["a4"]["n"])),
    ]
    lines = ["% pn-macros.tex — GENERATED by scripts/paper_macros.py from",
             "% benchmarks/results-v1/{paper_numbers.json,"
             "eval-fault-matrix.json}.",
             "% Never hand-edit; re-run the script.",
             f"% manifest: commit {pn['manifest']['commit']}, "
             f"host {pn['manifest']['host']}", ""]
    lines += [rf"\newcommand{{\{k}}}{{{v}}}" for k, v in m]
    return "\n".join(lines) + "\n"


def table_fault(fm: dict) -> str:
    rows = []
    for ct in CLAIM_ORDER:
        cells = [c for c in fm["cells"] if c["claim"] == ct]
        n_f = sum(1 for c in cells if c["expectation"] == "must_not_certify")
        n_c = sum(1 for c in cells if c["expectation"] == "must_certify")
        ok = all(c["ok"] for c in cells)
        rows.append(f"{CLAIM_LABEL[ct]:<16} & {n_f} & {n_c} & "
                    f"{'verified' if ok else '\\textbf{FAILED}'} \\\\")
    body = "\n".join(rows)
    nc = ", ".join(fm["not_covered"][:3])
    nc2 = ", ".join(fm["not_covered"][3:]).replace("wrong_extremum",
        "extremal").replace("wrong_top_k", "top-$k$")
    return rf"""% T2 — fault x claim matrix, from eval-fault-matrix.json
\begin{{table}}[t]
\centering\small
\begin{{tabular}}{{lccc}}
\toprule
\textbf{{Claim type}} & \textbf{{Faults}} & \textbf{{Controls}} &
\textbf{{Status}}\\
\midrule
{body}
\midrule
\multicolumn{{4}}{{l}}{{\emph{{\pnCells\ cells: \pnFalseCert\ false
  certifications, \pnFalseRej\ false rejections}}}} \\
\multicolumn{{4}}{{l}}{{\scriptsize declared uncovered:
  {nc.replace('_', ' ')},}} \\
\multicolumn{{4}}{{l}}{{\scriptsize {nc2.replace('_', ' ')}}}\\
\bottomrule
\end{{tabular}}
\caption{{The fault $\times$ claim conformance matrix (receipt:
\texttt{{eval-fault-matrix.json}}). The verified fragment is what this
table certifies.}}
\label{{tab:faultmatrix}}
\end{{table}}
"""


def table_frozen(pn: dict) -> str:
    fz = pn["frozen_2x2"]
    em, tg, sq = fz["em"], fz["ucr_pre_gate_tgms"], fz["ucr_pre_gate_sql"]
    icon = fz["interface_contrasts"]
    ev = fz["evidence_em_deltas"]

    def row(vals: list[str]) -> str:
        return " & ".join(vals) + r" \\"

    ucr_t = row(["operators, ungated"] + [f3(v) for v in tg])
    ucr_s = row(["SQL, ungated (witness map)"] + [f3(v) for v in sq])
    gated = row(["both, gated"] +
                [rf"\textbf{{{f3(v)}}}" for v in fz["ucr_gated"]])
    em_o = row(["operators"] + [f3(em[f"{d}|ours"]) for d in DS])
    em_s = row(["SQL"] + [f3(em[f"{d}|b6e"]) for d in DS])
    ic = [icon[f"{d} | interface: b6e - ours"] for d in DS]
    d_em = row([r"$\Delta$EM"] + [f"${f3(c['delta_em'])}$" for c in ic])
    d_ci = row([""] + [rf"\scriptsize${ci2(c['ci95'])}$" for c in ic])
    ev_o = row(["operators"] +
               [f"${f3(ev[f'{d} | evidence(tgms): ours - ours-noverify']
                   ['delta_em'])}$" for d in DS])
    ev_s = row(["SQL"] +
               [f"${f3(ev[f'{d} | evidence(sql): b6e - b6']['delta_em'])}$"
                for d in DS])
    heads = " & ".join([""] + [rf"\textbf{{{DS_SHORT[d]}}}" for d in DS])
    return rf"""% T3 — the frozen 2x2, from paper_numbers.json:frozen_2x2
\begin{{table}}[t]
\centering\small
\setlength{{\tabcolsep}}{{2.5pt}}
\begin{{tabular}}{{lccc}}
\toprule
{heads} \\
\midrule
\multicolumn{{4}}{{l}}{{\emph{{unsupported-claim rate (mean per-answer
  fraction)}}}}\\
{ucr_t}
{ucr_s}
{gated}
\midrule
\multicolumn{{4}}{{l}}{{\emph{{exact match, gated arms}}}}\\
{em_o}
{em_s}
\midrule
\multicolumn{{4}}{{l}}{{\emph{{interface contrast (SQL $-$ operators),
  95\% CI}}}}\\
{d_em}
{d_ci}
\midrule
\multicolumn{{4}}{{l}}{{\emph{{evidence contrast (gated $-$ ungated),
  $\Delta$EM}}}}\\
{ev_o}
{ev_s}
\bottomrule
\end{{tabular}}
\caption{{The frozen 2$\times$2 (test splits, 3 seeds, \pnPrimaryRows\
runs; receipt: \texttt{{m8/m8-tables.json}}). Enforcement removes every
measured unsupported claim at the cost of one changed outcome.}}
\label{{tab:frozen}}
\end{{table}}
"""


def table_cost(pn: dict) -> str:
    return r"""% T4 — evidence economics, from paper_numbers.json:overhead
\begin{table}[t]
\centering\small
\begin{tabular}{lr}
\toprule
\textbf{Cost component} & \\
\midrule
descriptor construction (per envelope) &
  $\pnDescUsSmall$--$\pnDescUsLarge\,\mu$s \\
descriptor production (per 2-step plan) & $\sim$$\pnPlanUs\,\mu$s \\
verification (per claim) & $<1$\,ms \\
\midrule
SQL cardinality certificate / page query & $\pnSqlCertRatio\times$ \\
\quad$\Rightarrow$ certified answer vs uncertified & $\approx 2\times$ \\
\bottomrule
\end{tabular}
\caption{The economics of evidence (receipt:
\texttt{evidence-overhead-itiger.json}): bookkeeping is free;
certificates cost real query work.}
\label{tab:cost}
\end{table}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", type=Path, required=True)
    args = ap.parse_args()
    pn = json.loads((RES / "paper_numbers.json").read_text())
    fm = json.loads((RES / "eval-fault-matrix.json").read_text())
    (args.outdir / "pn-macros.tex").write_text(macros(pn, fm))
    (args.outdir / "tab-data.tex").write_text(
        "% tab-data.tex — GENERATED by scripts/paper_macros.py; never "
        "hand-edit.\n\n" + table_fault(fm) + "\n" + table_frozen(pn) +
        "\n" + table_cost(pn))
    print(f"wrote {args.outdir}/pn-macros.tex, {args.outdir}/tab-data.tex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
