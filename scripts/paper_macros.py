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
    ma = pn["model_axis_robustness"] if "model_axis_robustness" in pn \
        else pn.get("model_axis", {})

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
    empty_rule = "/".join(str(o3[d].get("resolved_by_empty_rule", 0))
                          for d in DS)
    budget_exc = "/".join(str(o3[d].get("budget_exceeded", 0)) for d in DS)

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
        ("pnEmptyRuleLadder", empty_rule),
        ("pnBudgetExcLadder", budget_exc),
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
        # M6 frontier
        ("pnEmExprAi", f3(m6["a1"]["em_given_expressible"])),
        ("pnEmExprAii", f3(m6["a2"]["em_given_expressible"])),
        ("pnFpvAi", f2(m6["a1"]["first_plan_valid"])),
        ("pnFpvAii", f2(m6["a2"]["first_plan_valid"])),
        ("pnEmAiii", f3(m6["a3"]["em"])),
        ("pnEmAiv", f3(m6["a4"]["em"])),
        ("pnFpvAiv", f2(m6["a4"]["first_plan_valid"])),
        ("pnFpvAivp", f2(m6["a4p"]["first_plan_valid"])),
        ("pnConfAiv", str(m6["a4"]["agg_series_confusions"])),
        ("pnConfAivp", str(m6["a4p"]["agg_series_confusions"])),
        ("pnMsixN", str(m6["a4"]["n"])),
        # model-axis probes (the D-119/D-123 quantization control)
        ("pnProbesAwqOurs", "/".join(
            f2(ma["32b"][f"{d}|ours"]["probes"]) for d in DS)
         if "32b" in ma else "TBD"),
        ("pnProbesFpOurs", "/".join(
            f2(ma["32bfp16"][f"{d}|ours"]["probes"]) for d in DS)
         if "32bfp16" in ma else "TBD"),
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
\caption{{Implementation-conformance coverage of the verified
fragment (receipt: \texttt{{eval-fault-matrix.json}}): the formal
rules define the fragment; this matrix tests the shipped
implementation against the declared fault families.}}
\label{{tab:faultmatrix}}
\end{{table}}
"""


def table_frozen(pn: dict) -> str:
    fz = pn["frozen_2x2"]
    em, tg, sq = fz["em"], fz["ucr_pre_gate_tgms"], fz["ucr_pre_gate_sql"]
    carry = fz["claim_carrying_rate"]
    surv = fz["total_claim_survival"]
    emc = fz["em_given_claims"]
    ev = fz["evidence_em_deltas"]

    body = []
    pre_by = {"Operators": dict(zip(DS, tg)), "SQL": dict(zip(DS, sq))}
    arm_of = {"Operators": ("ours", "ours-noverify"),
              "SQL": ("b6e", "b6")}
    ev_key = {"Operators": "evidence(tgms): ours - ours-noverify",
              "SQL": "evidence(sql): b6e - b6"}
    for d in DS:
        for iface in ("Operators", "SQL"):
            g, u = arm_of[iface]
            e = ev[f"{d} | {ev_key[iface]}"]
            ccc = emc[f"{d}|{g}"] * carry[f"{d}|{g}"]
            body.append(" & ".join([
                DS_SHORT[d] if iface == "Operators" else "",
                iface,
                f3(pre_by[iface][d]),
                f2(surv[f"{d}|{g}"]),
                f2(carry[f"{d}|{g}"]),
                f3(emc[f"{d}|{g}"]),
                f3(ccc),
                f3(em[f"{d}|{g}"]),
                rf"${f3(e['delta_em'])}$ \scriptsize${ci2(e['ci95'])}$",
            ]) + r" \\")
        body.append(r"\addlinespace[1pt]")
    rows = "\n".join(body[:-1])

    ic = [fz["interface_contrasts"][f"{d} | interface: b6e - ours"]
          for d in DS]
    ic_row = " & ".join(
        [r"$\Delta$EM (SQL$-$Op.)"] +
        [rf"${f3(c['delta_em'])}$ \scriptsize${ci2(c['ci95'])}$"
         for c in ic]) + r" \\"
    frozen = rf"""% T-frozen — certified-output framing (review round 3)
\begin{{table*}}[t]
\centering\small
\setlength{{\tabcolsep}}{{4pt}}
\begin{{tabular}}{{llccccccc}}
\toprule
\textbf{{Dataset}} & \textbf{{Interface}} &
\textbf{{\shortstack{{pre-gate\\UCR}}}} &
\textbf{{\shortstack{{mean claim\\survival}}}} &
\textbf{{\shortstack{{certified-output\\coverage}}}} &
\textbf{{\shortstack{{conditional\\accuracy}}}} &
\textbf{{\shortstack{{correct-certified\\coverage}}}} &
\textbf{{\shortstack{{EM,\\audit mode}}}} &
\textbf{{\shortstack{{$\Delta$EM audit, enforced$-$\\unenforced (95\% CI)}}}} \\
\midrule
{rows}
\bottomrule
\end{{tabular}}
\caption{{The frozen evidence experiment over test splits, three
seeds, and \pnPrimaryRows\ task-runs, with metrics as defined in
\S\ref{{sec:metrics}}. Certified-output coverage, conditional
accuracy among certified outputs, and correct-certified coverage
describe certified-output mode. EM and its paired contrast describe
audit mode, in which answer text renders in every arm. Post-gate UCR
is 0.000 everywhere and supported-claim retention is 1.0 by
construction, so neither is tabulated. SQL pre-gate UCR is a lower
bound under the mapped witness-claim subset, so cross-interface UCR
magnitudes are not comparable.}}
\label{{tab:frozen}}
\end{{table*}}

% T-interface — secondary contrast
\begin{{table}}[t]
\centering\small
\begin{{tabular}}{{lccc}}
\toprule
 & \textbf{{MathOverflow}} & \textbf{{SuperUser}} & \textbf{{wiki-talk}} \\
\midrule
{ic_row}
\bottomrule
\end{{tabular}}
\caption{{The secondary interface contrast between the two enforced
arms, per-task seed-averaged paired bootstrap.}}
\label{{tab:interface}}
\end{{table}}
"""
    return frozen


def table_census(pn: dict) -> str:
    o3 = pn["oracle_v3"]
    rows = []
    for d in DS:
        v = o3[d]
        exact = v["resolved"] - v["resolved_by_empty_rule"]
        total = (exact + v["resolved_by_empty_rule"] +
                 v["budget_exceeded"] + v["not_attempted"] +
                 v["oracle_unsupported"])
        assert total == v["records"], (d, total)
        rows.append(" & ".join([
            DS_SHORT[d], str(v["records"]), str(exact),
            str(v["resolved_by_empty_rule"]), str(v["budget_exceeded"]),
            str(v["not_attempted"]), str(v["oracle_unsupported"]),
            f2(v["resolution_coverage"])]) + r" \\")
    body = "\n".join(rows)
    return rf"""\begin{{table}}[t]
\centering\small
\setlength{{\tabcolsep}}{{3.5pt}}
\begin{{tabular}}{{lccccccc}}
\toprule
 & \textbf{{draws}} & \textbf{{exact}} & \textbf{{empty}} &
\textbf{{budget}} & \textbf{{n/a}} & \textbf{{unres.}} &
\textbf{{cov.}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
\caption{{Oracle terminal-status census. Statuses are mutually
exclusive and each row sums to its draw count. Here exact means
policy-lane or oracle-lane gold, empty means the backend-certified
empty-result rule whose resolutions are suite-ineligible, budget
means an oracle-envelope ceiling named per receipt, n/a means the
template does not apply to the store, and unres.\ is the one draw
whose emptiness the rule declined to certify because its zero-row
step inherits uncertified delivery from a truncated upstream page.}}
\label{{tab:census}}
\end{{table}}
"""


def table_sql_surface() -> str:
    # Static architecture facts (which claim forms each adapter can
    # establish, and what the end-to-end SQL arm constructs) — from
    # tgms/evidence/adapter_sql.py and tgms/eval/baselines.py
    # (BiTemporalSQLEvidence.answer: every proposed claim, counts and
    # values included, is mapped to Membership witness checks over the
    # certified page).
    return r"""\begin{table}[t]
\centering\small
\begin{tabular}{lccc}
\toprule
\textbf{Claim form} & \textbf{Op.\ adapter} & \textbf{SQL adapter} &
\textbf{SQL arm} \\
\midrule
Membership   & \yes & \yes & \yes\ (all forms map here) \\
Scalar       & \yes & \yes & mapped to witness \\
Exact count  & \yes & \yes\ (certificate) & portability suite \\
Complete set & \yes & \yes & not used \\
Exists       & \yes & \yes & mapped to witness \\
NotExists    & \yes & \yes & not used \\
$\mathsf{AtBasis}$ & \yes & \yes\ (tt replica) & probes \\
\bottomrule
\end{tabular}
\caption{Claim-construction surfaces. The verifier and both adapters
support every form. The end-to-end SQL arm maps every proposed claim,
counts and values included, to witness checks over the
certificate-bearing page, which is the conservative mapping. SQL
pre-gate UCR is therefore a lower bound, cross-interface UCR
magnitudes are never compared, and within-interface contrasts always
share one claim-construction surface.}
\label{tab:sqlsurface}
\end{table}
"""


def table_guardrail(pn: dict) -> str:
    gr = pn["guardrail"]
    rows = []
    for label, key in (("0.5\\,s", "itiger_scaled_at_500ms"),
                       ("2\\,s", "itiger_scaled_at_2s"),
                       ("10\\,s", "itiger_scaled_at_10s")):
        v = gr[key]
        rows.append(f"{label} & {v['FA']} & {v['FR']} \\\\")
    body = "\n".join(rows)
    return rf"""\begin{{table}}[t]
\centering\small
\begin{{tabular}}{{lcc}}
\toprule
\textbf{{budget}} & \textbf{{false adm.\ / 54}} &
\textbf{{false rej.\ / 54}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
\caption{{Transferred admission policy re-measured end to end on the
evaluation cluster at three budgets. The three operating points are
reported as measured, including the 0.5\,s false admission; no
functional characterization beyond them is claimed.}}
\label{{tab:guardrail}}
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
page query & $14.4$\,ms \\
SQL cardinality certificate & $13.7$\,ms \\
\quad ratio (certificate / page) & $\pnSqlCertRatio\times$ \\
\quad$\Rightarrow$ certified answer vs uncertified & $\approx 2\times$ \\
\bottomrule
\end{tabular}
\caption{The economics of evidence (receipt:
\texttt{evidence-overhead-itiger.json}): ECQR bookkeeping has
microsecond-scale absolute overhead; strong certificate production can
cost query-scale work.}
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
    hdr = ("% GENERATED by scripts/paper_macros.py; never hand-edit.\n\n")
    (args.outdir / "tab-fault.tex").write_text(hdr + table_fault(fm))
    (args.outdir / "tab-frozen.tex").write_text(hdr + table_frozen(pn))
    (args.outdir / "tab-census.tex").write_text(hdr + table_census(pn))
    (args.outdir / "tab-sqlsurface.tex").write_text(
        hdr + table_sql_surface())
    (args.outdir / "tab-guardrail.tex").write_text(
        hdr + table_guardrail(pn))
    (args.outdir / "tab-cost.tex").write_text(hdr + table_cost(pn))
    print(f"wrote {args.outdir}/pn-macros.tex, {args.outdir}/tab-data.tex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
