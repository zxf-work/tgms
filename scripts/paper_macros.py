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


def macros(pn: dict, fm: dict, sc: dict, uc: dict) -> str:
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
        ("pnSurvLo", f2(min(fz["total_claim_survival"].values()))),
        ("pnSurvHi", f2(max(fz["total_claim_survival"].values()))),
        ("pnCertCovLo", f2(min(
            fz["claim_carrying_rate"][f"{d}|{a}"] for d in DS
            for a in ("ours", "b6e")))),
        ("pnCertCovHi", f2(max(
            fz["claim_carrying_rate"][f"{d}|{a}"] for d in DS
            for a in ("ours", "b6e")))),
        ("pnCondAccLo", f2(min(
            fz["em_given_claims"][f"{d}|{a}"] for d in DS
            for a in ("ours", "b6e")))),
        ("pnCondAccHi", f2(max(
            fz["em_given_claims"][f"{d}|{a}"] for d in DS
            for a in ("ours", "b6e")))),
        ("pnCorrCertLo", f2(min(
            fz["em_given_claims"][f"{d}|{a}"] *
            fz["claim_carrying_rate"][f"{d}|{a}"] for d in DS
            for a in ("ours", "b6e")))),
        ("pnCorrCertHi", f2(max(
            fz["em_given_claims"][f"{d}|{a}"] *
            fz["claim_carrying_rate"][f"{d}|{a}"] for d in DS
            for a in ("ours", "b6e")))),
        ("pnProbesFpOurs", "/".join(
            f2(ma["32bfp16"][f"{d}|ours"]["probes"]) for d in DS)
         if "32bfp16" in ma else "TBD"),
        # verifier scaling + descriptor space (eval-verifier-scaling.json)
        ("pnBuildUsFlat",
         f"{min(t['build_ecqr_ms'] for t in sc['timing'])*1000:.1f}--"
         f"{max(t['build_ecqr_ms'] for t in sc['timing'])*1000:.1f}"),
        ("pnCertVerifyUsFlat",
         f"{min(t['verify_count_cert_ms'] for t in sc['timing'])*1000:.1f}"
         "--"
         f"{max(t['verify_count_cert_ms'] for t in sc['timing'])*1000:.1f}"),
        ("pnMarginalClaimUs",
         f"{min(t['multiclaim_per_claim_ms'] for t in sc['timing'])*1000:.1f}"
         "--"
         f"{max(t['multiclaim_per_claim_ms'] for t in sc['timing'])*1000:.1f}"),
        ("pnEcqrTgmsBytes", str(sc["space"]["tgms_operator"]["total_bytes"])),
        ("pnEcqrSqlBytes", str(sc["space"]["sql_adapter"]["total_bytes"])),
        ("pnEcqrCoreBytes",
         str(sc["space"]["tgms_operator"]["semantic_core_bytes"])),
        # composition / depth / tokens (eval-unsupported-composition.json)
        ("pnUnsCntOps", str(sum(
            uc["composition"][f"{d}|operators"]
              ["unsupported_claims_by_kind"].get("count", 0)
            for d in DS))),
        ("pnUnsEntOps", str(sum(
            uc["composition"][f"{d}|operators"]
              ["unsupported_claims_by_kind"].get("entity", 0)
            for d in DS))),
        ("pnUnsValOps", str(sum(
            uc["composition"][f"{d}|operators"]
              ["unsupported_claims_by_kind"].get("value", 0)
            for d in DS))),
        ("pnUnsUndet", str(sum(
            uc["composition"][f"{d}|operators"]
              ["unsupported_claims_undetermined_kind"] for d in DS))),
        ("pnUnsSqlNw", str(sum(
            x for d in DS for k, x in
            uc["composition"][f"{d}|sql"]["claim_verdicts"].items()
            if k != "SUPPORTED"))),
        ("pnDepthUOne", f2(uc["depth_operators_pooled"]["1"]
                         ["mean_pre_gate_ucr"])),
        ("pnDepthUTwo", f2(uc["depth_operators_pooled"]["2"]
                         ["mean_pre_gate_ucr"])),
        ("pnDepthUThree", f2(uc["depth_operators_pooled"]["3+"]
                         ["mean_pre_gate_ucr"])),
        ("pnDepthNOne", str(uc["depth_operators_pooled"]["1"]["n"])),
        ("pnDepthNTwo", str(uc["depth_operators_pooled"]["2"]["n"])),
        ("pnDepthNThree", str(uc["depth_operators_pooled"]["3+"]["n"])),
        ("pnCtxTokMedOpsLo", f"{min(uc['run_input_tokens'][f'{d}|operators']['median'] for d in DS)/1000:.1f}"),
        ("pnCtxTokMedOpsHi", f"{max(uc['run_input_tokens'][f'{d}|operators']['median'] for d in DS)/1000:.1f}"),
        ("pnCtxTokTailOps", f"{max(uc['run_input_tokens'][f'{d}|operators']['p95'] for d in DS)/1000:.0f}"),
        ("pnCtxTokMedSqlLo", f"{min(uc['run_input_tokens'][f'{d}|sql']['median'] for d in DS)/1000:.1f}"),
        ("pnCtxTokMedSqlHi", f"{max(uc['run_input_tokens'][f'{d}|sql']['median'] for d in DS)/1000:.1f}"),
        ("pnDescTokMed",
         str(uc["descriptor_tokens_sql_frozen"]["median"])),
        ("pnDescTokTail", str(uc["descriptor_tokens_sql_frozen"]["p95"])),
        ("pnDescBytesMed",
         str(uc["descriptor_bytes_sql_frozen"]["median"])),
        ("pnDescBytesTail",
         str(uc["descriptor_bytes_sql_frozen"]["p95"])),
    ]
    # external-workload macros (D-130); emitted only when receipts exist
    lc_p = RES / "eval-ldbc-coverage.json"
    if lc_p.exists():
        lc = json.loads(lc_p.read_text())
        ex, cl = lc["exec_coverage_counts"], lc["claim_full_contract_counts"]
        m += [
            ("pnLdbcN", str(lc["n_templates"])),
            ("pnLdbcExecDirect", str(ex.get("DIRECT_TGMS", 0))),
            ("pnLdbcExecDecomp", str(ex.get("DECOMPOSABLE_TGMS", 0))),
            ("pnLdbcExecSql", str(ex.get("SQL_ONLY", 0))),
            ("pnLdbcExecUnsup", str(ex.get("UNSUPPORTED_EXECUTION", 0))),
            ("pnLdbcClaimFrag", str(cl.get("CURRENT_ECQR_FRAGMENT", 0))),
            ("pnLdbcClaimOrdered",
             str(cl.get("REQUIRES_ORDERED_RESULT", 0))),
            ("pnLdbcClaimTopK", str(cl.get("REQUIRES_TOP_K", 0))),
            ("pnLdbcClaimGext",
             str(cl.get("REQUIRES_GROUPWISE_EXTREMUM", 0))),
            ("pnLdbcClaimPath",
             str(cl.get("REQUIRES_PATH_CERTIFICATE", 0))),
            ("pnLdbcFlatProj",
             str(lc["flat_projection_in_fragment"])),
            ("pnLdbcUncovered",
             str(lc["n_templates"]
                 - cl.get("CURRENT_ECQR_FRAGMENT", 0))),
            ("pnLdbcProjExcluded",
             ", ".join(lc["projection_excluded"])),
        ]
    os_p = RES / "eval-bird-oracle-smoke.json"
    if os_p.exists():
        osm = json.loads(os_p.read_text())
        m += [("pnBirdOracleCert", str(osm["certified"])),
              ("pnBirdOracleFail",
               str(len(osm.get("gold_contract_failures", [])))),
              ("pnBirdMismatchShapeFail",
               str(len(osm.get("mismatch_fail_gold_shape_gate", [])))),
              ("pnBirdMismatchCompat",
               str(len(osm.get("mismatch_shape_compatible", [])))),
              ("pnBirdOracleOutside",
               str(len(osm.get("gold_failure_not_a_mismatch", []))))]
    ba_p = RES / "eval-bird-agent.json"
    ex_p = RES / "eval-bird-official-ex.json"
    if ex_p.exists():
        ex = json.loads(ex_p.read_text())
        m += [("pnBirdExPinned", f"{ex['ex_vs_pinned_gold_pct']:.2f}"),
              ("pnBirdExZip", f"{ex['ex_vs_zip_gold_pct']:.2f}")]
    if ba_p.exists():
        ba = json.loads(ba_p.read_text())
        fu = ba["funnel"]

        def pc(a: int, b: int) -> str:
            return f"{100.0 * a / b:.1f}" if b else "0.0"
        m += [
            ("pnBirdN", str(fu["universe"])),
            ("pnBirdExec", str(fu["executable_sql"])),
            ("pnBirdClaim", str(fu["claim_constructed"])),
            ("pnBirdCert", str(fu["certified"])),
            ("pnBirdCertCorrect", str(fu["certified_and_correct"])),
            ("pnBirdCertPct", pc(fu["certified"], fu["universe"])),
            ("pnBirdCertCorrectPct",
             pc(fu["certified_and_correct"], fu["universe"])),
            ("pnBirdEm", str(ba["em_overall"])),
            ("pnBirdEmPct", pc(ba["em_overall"], fu["universe"])),
            ("pnBirdCertPrecisionPct",
             pc(fu["certified_and_correct"], fu["certified"])),
            ("pnBirdUncertEm", str(ba["em_overall"]
                                   - fu["certified_and_correct"])),
            ("pnBirdNoSql",
             str(fu["universe"] - fu["executable_sql"])),
            ("pnBirdShapeMismatch",
             str(ba["stage_counts"].get("shape_mismatch", 0))),
            ("pnBirdWithheld",
             str(fu["claim_constructed"] - fu["certified"])),
            ("pnBirdCertWrong",
             str(fu["certified"] - fu["certified_and_correct"])),
            ("pnBirdRetentionPct",
             pc(fu["certified_and_correct"], ba["em_overall"])),
            ("pnBirdNoClaimWrong",
             str(fu["universe"] - fu["certified"]
                 - (ba["em_overall"] - fu["certified_and_correct"]))),
            ("pnBirdOutside", str(ba["outside_fragment"])),
            ("pnBirdCertFull", str(ba["certified_full_contract"])),
            ("pnBirdCertPartial",
             str(ba["certified_partial_contract"])),
            ("pnBirdGoldFull", str(ba["gold_match_full_contract"])),
            ("pnBirdPartialDup",
             str(ba["partial_contract_reasons"].get(
                 "duplicate_bearing_reference", 0))),
            ("pnBirdPartialMulti",
             str(ba["partial_contract_reasons"].get(
                 "multipart_partially_answered", 0))),
            ("pnBirdPartialOrder",
             str(ba["partial_contract_reasons"].get(
                 "ordered_output_demanded", 0))),
            ("pnBirdCertFullPct",
             pc(ba["certified_full_contract"], fu["universe"])),
            ("pnBirdDupBearing",
             str(ba["duplicate_bearing_reference"])),
            ("pnBirdGoldMismatch", str(ba["question_gold_mismatch"])),
            ("pnBirdFullNotCovered",
             str(ba["full_contract_not_covered"])),
            ("pnBirdExactCountEarned",
             str(ba["claim_kind_census"]["exact_count"])),
            ("pnBirdScalarClaims",
             str(ba["claim_kind_census"]["scalar"])),
            ("pnBirdSetClaims",
             str(ba["claim_kind_census"]["complete_set"])),
            ("pnBirdRepairUsed", str(ba["repair_used"])),
            ("pnBirdDescBytesMed",
             str(ba["descriptor_bytes_median"])),
        ]
    # established-interface truncation probe (D-142); motivation
    # validation, no ECQR component. Emitted only when the receipt exists.
    tp_p = RES / "eval-trunc-probe.json"
    if tp_p.exists():
        tp = json.loads(tp_p.read_text())
        cond = tp["conditions"]

        def pf(condition: str, key: str, family: str | None = None) -> str:
            block = (cond[condition]["all"] if family is None
                     else cond[condition]["by_family"][family])
            return f"{100.0 * block['fraction'][key]:.1f}"

        errs = sum(cond[c]["all"]["error"] for c in cond)
        m += [
            ("pnProbeK", str(tp["k"])),
            ("pnProbeN", str(tp["eligible"]["sqlite"])),
            ("pnProbeCountN", str(cond["C0"]["by_family"]["COUNT"]["n"])),
            ("pnProbeSetN", str(cond["C0"]["by_family"]["SET"]["n"])),
            ("pnProbeErr", str(errs)),
            ("pnProbeBarePD", pf("C0", "page_derived")),
            ("pnProbeBareCorr", pf("C0", "correct")),
            ("pnProbeFlagPD", pf("C1", "page_derived")),
            ("pnProbeFlagCorr", pf("C1", "correct")),
            ("pnProbeTotalPD", pf("C2", "page_derived")),
            ("pnProbeTotalCorr", pf("C2", "correct")),
            ("pnProbeTotalCountCorr", pf("C2", "correct", "COUNT")),
            ("pnProbeTotalCountPD", pf("C2", "page_derived", "COUNT")),
            ("pnProbeTotalSetCorr", pf("C2", "correct", "SET")),
            ("pnProbeTotalSetPD", pf("C2", "page_derived", "SET")),
            ("pnProbePgN", str(tp["eligible"]["pg"])),
            ("pnProbePgPD", pf("pg-C0", "page_derived")),
            ("pnProbePgCorr", pf("pg-C0", "correct")),
        ]
    # D-168 item 8: three formerly hand-typed experimental literals.
    # (a) EvidenceBench failing cases of the two simple checkers; a
    #     failing case is a false certification or a false rejection.
    bc = json.loads((RES / "eval-baseline-checkers.json").read_text())
    for name, key in (("pnValueOnlyFail", "b1_value_only"),
                      ("pnTaintFail", "b2_taint_all")):
        b = bc[key]
        assert b["n_cells"] == fmx["cells"], (key, b["n_cells"])
        m.append((name, str(b["false_certifications"]
                            + b["false_rejections"])))
    # (b) gate replay: max over datasets of |strict-mode EM of the gate
    #     replayed over the unenforced operator arm - the enforced
    #     arm's correct-certified coverage|, both replay bounds.
    gcf = fz["gate_counterfactual"]["cells"]
    m.append(("pnReplayGapMax", f3(max(
        abs(gcf[f"{d}|ours-noverify"][k]
            - fz["em_given_claims"][f"{d}|ours"]
            * fz["claim_carrying_rate"][f"{d}|ours"])
        for d in DS for k in ("em_gate_replay_optimistic",
                              "em_gate_replay_pessimistic")))))
    # (c) SQL exact-cardinality certificate vs the page query it
    #     certifies (the ratio is pnSqlCertRatio).
    eo = json.loads(
        (RES / "evidence-overhead-itiger.json").read_text())["sql_certificate"]
    m += [("pnSqlCertMs", f"{eo['count_certificate_ms']:.1f}"),
          ("pnSqlPageMs", f"{eo['page_query_ms']:.1f}")]
    m += strengthening_macros(m)
    m += probe_ecqr_macros(m)
    lines = ["% pn-macros.tex — GENERATED by scripts/paper_macros.py from",
             "% benchmarks/results-v1/{paper_numbers.json,"
             "eval-fault-matrix.json}.",
             "% Never hand-edit; re-run the script.",
             f"% manifest: commit {pn['manifest']['commit']}, "
             f"host {pn['manifest']['host']}", ""]
    lines += [rf"\newcommand{{\{k}}}{{{v}}}" for k, v in m]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------
# D-170 strengthening round (lane I1): five-checker baselines with the
# composition family, the propagation ablation on the frozen runs, the
# SQL snapshot stress test, and the TopK routes on BIRD and LDBC.

BL_CHECKERS = ["value_only", "taint_all", "metadata_rules",
               "ecqr_no_propagation", "ecqr"]
BL_LABEL = {"value_only": "value-only", "taint_all": "taint",
            "metadata_rules": "metadata rules",
            "ecqr_no_propagation": "ECQR, no prop.", "ecqr": "ECQR"}
#: receipt family -> the family rows of the paper's RQ1 figure
BL_FAMILY = {"control": "controls", "page_truncation":
             "delivery incompleteness", "execution_incomplete":
             "execution incompleteness", "value_mutation": "value / witness",
             "citation": "value / witness", "basis": "basis",
             "integrity": "integrity"}
BL_SINGLE_ORDER = ["controls", "delivery incompleteness",
                   "execution incompleteness", "value / witness", "basis",
                   "integrity"]
SNAP_ENGINE = {"sqlite-wal": "SQLite, WAL", "sqlite-rollback":
               "SQLite, rollback journal", "duckdb": "DuckDB"}


def _load(name: str) -> dict:
    return json.loads((RES / name).read_text())


def baseline_rows(bc: dict) -> list[tuple[str, str, int, str, list[int]]]:
    """(scope, family label, n, error kind, errors per checker) rows of
    the five-checker matrix; controls can only be falsely rejected and
    fault cells only falsely accepted."""
    rows = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for c in bc["cells"]:
        if c["scope"] == "single_step":
            fam = BL_FAMILY[c["family"]]
        else:
            fam = ("controls" if c["expectation"] == "must_certify"
                   else "faults")
        groups.setdefault((c["scope"], fam), []).append(c)
    order = [("single_step", f) for f in BL_SINGLE_ORDER] + \
        [("composition", "controls"), ("composition", "faults")]
    for key in order:
        cells = groups[key]
        kinds = {c["expectation"] for c in cells}
        assert len(kinds) == 1, (key, kinds)
        kind = "FR" if kinds.pop() == "must_certify" else "FA"
        errs = [sum(1 for c in cells if not c["decisions"][k]["ok"])
                for k in BL_CHECKERS]
        rows.append((key[0], key[1], len(cells), kind, errs))
    assert sum(len(v) for v in groups.values()) == len(bc["cells"])
    return rows


def strengthening_macros(prior: list[tuple[str, str]]) -> list[tuple[str, str]]:
    pm = dict(prior)
    out: list[tuple[str, str]] = []
    # (1) eval-baseline-checkers-v3.json (v2: the record of the defect)
    bc = _load("eval-baseline-checkers-v3.json")
    bc2 = _load("eval-baseline-checkers-v2.json")
    sc = bc["cells_by_scope"]
    assert str(sc["single_step"]["n"]) == pm["pnCells"]
    sm = bc["summary"]

    def by(k: str, scope: str, f: str) -> int:
        return sm[k]["by_scope"][scope][f]
    for k in ("metadata_rules", "ecqr_no_propagation", "ecqr"):
        assert by(k, "single_step", "false_accepts") == 0, k
        assert by(k, "single_step", "false_rejects") == 0, k
    assert sm["ecqr"]["false_accepts"] == sm["ecqr"]["false_rejects"] == 0
    assert by("metadata_rules", "composition", "false_accepts") == \
        by("ecqr_no_propagation", "composition", "false_accepts") == \
        sc["composition"]["must_not_certify"]
    defect = bc2["summary"]["ecqr"]
    assert defect["fa_cells"] == [c["cell"] for c in
                                  bc["change_from_v2"]["decisions_changed"]]
    out += [
        ("pnBaseCompN", str(sc["composition"]["n"])),
        ("pnBaseCompFaultN", str(sc["composition"]["must_not_certify"])),
        ("pnBaseCompCtrlN", str(sc["composition"]["must_certify"])),
        ("pnBaseAllN", str(sc["single_step"]["n"] + sc["composition"]["n"])),
        ("pnBaseMetaSingleErr", str(
            by("metadata_rules", "single_step", "false_accepts")
            + by("metadata_rules", "single_step", "false_rejects"))),
        ("pnBaseMetaCompFa",
         str(by("metadata_rules", "composition", "false_accepts"))),
        ("pnBaseNoPropCompFa",
         str(by("ecqr_no_propagation", "composition", "false_accepts"))),
        ("pnBaseValueCompFa",
         str(by("value_only", "composition", "false_accepts"))),
        ("pnBaseTaintCompFa",
         str(by("taint_all", "composition", "false_accepts"))),
        ("pnBaseTaintCompFr",
         str(by("taint_all", "composition", "false_rejects"))),
        ("pnBaseEcqrErr",
         str(sm["ecqr"]["false_accepts"] + sm["ecqr"]["false_rejects"])),
        ("pnBaseDefectFa", str(defect["false_accepts"])),
        ("pnBaseDecisionsChanged",
         str(bc["change_from_v2"]["n_decisions_changed"])),
        ("pnBaseDecisionsCompared",
         str(bc["change_from_v2"]["n_decisions_compared"])),
    ]
    # (2) eval-propagation-ablation.json: a lower bound only
    ab = _load("eval-propagation-ablation.json")
    lb = []
    for d in DS:
        o = ab["operators"][d]
        assert o["ours"]["rule_a_blocked_runs_lower_bound"] == \
            o["ours-noverify"]["rule_a_blocked_runs_lower_bound"], d
        lb.append(o["ours"]["rule_a_blocked_runs_lower_bound"])
    assert all(ab["sql"][d]["flips_total"] == 0 for d in DS)
    out += [
        ("pnAblBlockLadder", "/".join(str(x) for x in lb)),
        ("pnAblBlockTotal", str(sum(lb))),
        ("pnAblSqlFlips", str(sum(ab["sql"][d]["flips_total"] for d in DS))),
        ("pnAblSqlInputDesc", str(sum(
            ab["sql"][d]["descriptors_with_input_descriptors"] for d in DS))),
    ]
    # (3) eval-sql-snapshot.json
    ss = _load("eval-sql-snapshot.json")
    unsafe = [b for b in ss["table"] if b["variant"] == "unsafe"]
    cons = [b for b in ss["table"] if b["variant"] == "consistent"]
    assert ss["consistent_variant_passes"] and ss["oracle_checks_pass"]
    assert all(b["certificates_wrong"] == 0 and b["inconsistent_pairs"] == 0
               for b in cons)
    trials = {b["trials"] for b in ss["table"]}
    assert len(trials) == 1
    wil = {round(b["inconsistent_fraction_wilson95_upper"], 6) for b in cons}
    assert len(wil) == 1
    wrong = [100.0 * b["certificates_wrong"] / b["trials"] for b in unsafe]
    out += [
        ("pnSnapBlocks", str(len(ss["table"]))),
        ("pnSnapTrials", f"{trials.pop():,}".replace(",", "{,}")),
        ("pnSnapWrongLo", f"{min(wrong):.1f}"),
        ("pnSnapWrongHi", f"{max(wrong):.1f}"),
        ("pnSnapConsWrong", str(sum(b["certificates_wrong"] for b in cons))),
        ("pnSnapWilsonPct", f"{100 * wil.pop():.2f}"),
        ("pnSnapEngineN", str(len(ss["protocol"]["engines"]))),
    ]
    # (4) eval-bird-topk-v3.json and eval-ldbc-topk.json
    tk = _load("eval-bird-topk-v3.json")
    a, g = tk["agent"], tk["gold"]
    for s in (a, g):
        assert (s["certified_total_order"] + s["certified_sequence_strict"]
                + s["certified_boundary_strict_set"] + s["not_certified_total"]
                == s["n_topk_shaped"])
        assert sum(s["not_certified_by_reason"].values()) == \
            s["not_certified_total"]
    sf = tk["strict_full_contract"]
    assert str(sf["before"]) == pm["pnBirdCertFull"]
    gained = [x["question_id"] for x in sf["non_duplicate_partial_items"]
              if x["would_cover_full_contract"]]
    assert len(gained) == sf["gained_with_topk"] == sf["after"] - sf["before"]
    nr = a["not_certified_by_reason"]
    out += [
        ("pnTopkAgentN", str(a["n_topk_shaped"])),
        ("pnTopkAgentTotal", str(a["certified_total_order"])),
        ("pnTopkAgentSeq", str(a["certified_sequence_strict"])),
        ("pnTopkAgentSet", str(a["certified_boundary_strict_set"])),
        ("pnTopkAgentNone", str(a["not_certified_total"])),
        ("pnTopkAgentTie", str(nr.get("boundary_tie", 0))),
        ("pnTopkAgentNoOrder", str(nr.get("no_order_by", 0))),
        ("pnTopkAgentTiePct",
         f"{100.0 * nr.get('boundary_tie', 0) / a['n_topk_shaped']:.1f}"),
        ("pnTopkGoldN", str(g["n_topk_shaped"])),
        ("pnTopkGoldTotal", str(g["certified_total_order"])),
        ("pnTopkGoldSeq", str(g["certified_sequence_strict"])),
        ("pnTopkGoldSet", str(g["certified_boundary_strict_set"])),
        ("pnTopkGoldNone", str(g["not_certified_total"])),
        ("pnTopkGoldTie",
         str(g["not_certified_by_reason"].get("boundary_tie", 0))),
        ("pnTopkPageMs", f"{a['seconds']['page']['median_ms']:.1f}"),
        ("pnTopkProbeMs",
         f"{a['seconds']['boundary_probe']['median_ms']:.1f}"),
        ("pnTopkCountMs",
         f"{a['seconds']['count_and_commit']['median_ms']:.1f}"),
        ("pnTopkStrictAfter", str(sf["after"])),
        ("pnTopkStrictGain", str(sf["gained_with_topk"])),
        ("pnTopkGainQid", ", ".join(str(q) for q in gained)),
    ]
    lt = _load("eval-ldbc-topk.json")
    lc = _load("eval-ldbc-coverage.json")
    frag = lc["claim_full_contract_counts"].get("CURRENT_ECQR_FRAGMENT", 0)
    cnt = lt["counts"]
    assert str(cnt["n_templates"]) == pm["pnLdbcClaimTopK"]
    assert (cnt["expressible_total_order"]
            + cnt["expressible_only_with_missing_tiebreak"]
            + cnt["not_top_k"] == cnt["n_templates"])
    fl = lt["flagged"]
    out += [
        ("pnLdbcTopkTotal", str(cnt["expressible_total_order"])),
        ("pnLdbcTopkTiebreak",
         str(cnt["expressible_only_with_missing_tiebreak"])),
        ("pnLdbcTopkNot", str(cnt["not_top_k"])),
        ("pnLdbcTopkReach", str(frag + cnt["expressible_total_order"])),
        ("pnLdbcTopkTiebreakNames",
         ", ".join(lt["by_judgment"]["TIEBREAK_MISSING"])),
        ("pnLdbcTopkNestedNames", ", ".join(fl["nested_projection"])),
        ("pnLdbcTopkInnerNames",
         ", ".join(fl["official_sql_ranking_lacks_tiebreak"])),
        ("pnLdbcTopkGroupKeyNames",
         ", ".join(fl["total_order_via_group_key"])),
    ]
    return out


def _tex(s: str) -> str:
    return s.replace("_", r"\_")


def table_baselines() -> str:
    bc = _load("eval-baseline-checkers-v3.json")
    body = []
    scope_lbl = {"single_step": "single-step", "composition": "composition"}
    last = None
    for scope, fam, n, kind, errs in baseline_rows(bc):
        if last is not None and scope != last:
            body.append(r"\midrule")
        lbl = scope_lbl[scope] if scope != last else ""
        last = scope
        body.append(f"{lbl} & {fam} ({n}) & {kind} & "
                    + " & ".join(str(e) for e in errs) + r" \\")
    tot = [sum(1 for c in bc["cells"] if not c["decisions"][k]["ok"])
           for k in BL_CHECKERS]
    body.append(r"\midrule")
    body.append(r"\multicolumn{3}{l}{all " + str(len(bc["cells"]))
                + r" cells} & " + " & ".join(str(t) for t in tot) + r" \\")
    head = " & ".join(r"\shortstack{" + BL_LABEL[k].replace(", ", r",\\")
                      + "}" for k in BL_CHECKERS)
    rows = "\n".join(body)
    comp = []
    for c in bc["cells"]:
        if c["scope"] != "composition":
            continue
        exp = ("certify" if c["expectation"] == "must_certify"
               else "reject")
        v = c["decisions"]["ecqr"]["verdict"]
        v = v.replace("UNSUPPORTED_", "U_").replace("_", r"\_\allowbreak ")
        comp.append(rf"{_tex(c['claim'])} & {_tex(c['fault'])} & {exp} & "
                    rf"\textsf{{{v}}} & {_tex(c['note'])} \\")
    comp_rows = "\n".join(comp)
    return rf"""% Source: benchmarks/results-v1/eval-baseline-checkers-v3.json
\begin{{table*}}[t]
\centering\small
\setlength{{\tabcolsep}}{{3pt}}
\begin{{tabular}}{{@{{}}llcccccc@{{}}}}
\toprule
 & \textbf{{family (cells)}} & & {head} \\
\midrule
{rows}
\bottomrule
\end{{tabular}}
\caption{{EvidenceBench errors of the five checkers per family: FR
counts false rejects on control cells, FA false accepts on fault
cells.}}
\label{{tab:baselines}}
\end{{table*}}

\begin{{table*}}[t]
\centering\small
\begin{{tabular}}{{@{{}}lllp{{0.2\textwidth}}p{{0.34\textwidth}}@{{}}}}
\toprule
\textbf{{claim}} & \textbf{{cell}} & \textbf{{expected}} &
\textbf{{ECQR verdict}} & \textbf{{what the cell exercises}} \\
\midrule
{comp_rows}
\bottomrule
\end{{tabular}}
\caption{{The composition cells: multi-step plans run through the real
executor, cited at a step whose own metadata is clean. U\_ abbreviates
UNSUPPORTED\_; \textsf{{BLOCKED}} is rule (a) of
Lemma~\ref{{M-lem:preserve}}, and \textsf{{NO\_DESCRIPTOR}} is the
failed basis-compatibility check.}}
\label{{tab:compcells}}
\end{{table*}}
"""


def table_snapshot() -> str:
    ss = _load("eval-sql-snapshot.json")
    body = []
    last = None
    for b in ss["table"]:
        eng = SNAP_ENGINE[b["engine"]] if b["engine"] != last else ""
        if last is not None and b["engine"] != last:
            body.append(r"\addlinespace[2pt]")
        last = b["engine"]
        n = f"{b['N']:,}".replace(",", "{,}")
        body.append(
            f"{eng} & {n} & {b['variant']} & {b['inconsistent_pairs']} & "
            f"{b['certificates_wrong']} & "
            f"{100 * b['inconsistent_fraction_wilson95_upper']:.2f} & "
            f"{100 * b['trials_with_writer_commit_in_window_fraction']:.1f}"
            r" \\")
    rows = "\n".join(body)
    trials = f"{ss['table'][0]['trials']:,}".replace(",", "{,}")
    wb = "$" + "$ to $".join(ss["protocol"]["writer_size_band"]) + "$"
    return rf"""% Source: benchmarks/results-v1/eval-sql-snapshot.json
\begin{{table*}}[t]
\centering\small
\setlength{{\tabcolsep}}{{3pt}}
\begin{{tabular}}{{@{{}}lrlrrrr@{{}}}}
\toprule
\textbf{{engine}} & $N$ & \textbf{{variant}} &
\shortstack{{incons.\\pairs}} & \shortstack{{wrong\\cert.}} &
\shortstack{{Wilson\\95\% (\%)}} & \shortstack{{writer in\\window (\%)}} \\
\midrule
{rows}
\bottomrule
\end{{tabular}}
\caption{{Snapshot stress test, {trials} trials per block: a page of
{ss['protocol']['page']} rows of a result of about $N$ rows, then
{ss['protocol']['delay_ms']:g}\,ms later the count, while a writer
commits every {ss['protocol']['writer_interval_ms']:g}\,ms and keeps
the result size within {wb}. Wilson 95\% is the upper bound on the
inconsistent-pair rate; the last column is the share of trials in which
the writer committed between the page and the count.}}
\label{{tab:snapshot}}
\end{{table*}}
"""


def table_topk() -> str:
    tk = _load("eval-bird-topk-v3.json")
    lt = _load("eval-ldbc-topk.json")
    reasons = ["boundary_tie", "no_order_by", "boundary_probe_unavailable"]
    rl = {"boundary_tie": r"tie at $k$/$k{+}1$", "no_order_by":
          "no ORDER BY", "boundary_probe_unavailable": "probe unavailable"}
    rows = []
    for lbl, s in (("agent SQL", tk["agent"]), ("gold SQL", tk["gold"])):
        nr = s["not_certified_by_reason"]
        assert set(nr) <= set(reasons), nr
        cs = s["seconds"]
        rows.append(" & ".join([
            lbl, str(s["n_topk_shaped"]), str(s["certified_total_order"]),
            str(s["certified_sequence_strict"]),
            str(s["certified_boundary_strict_set"])]
            + [str(nr.get(r, 0)) for r in reasons]
            + [f"{cs[k]['median_ms']:.1f}" for k in
               ("page", "boundary_probe", "count_and_commit")]) + r" \\")
    body = "\n".join(rows)
    head = " & ".join(r"\shortstack{" + rl[r].replace(" ", r"\\", 1) + "}"
                      for r in reasons)
    bj = lt["by_judgment"]
    fl = lt["flagged"]
    lrows = [
        rf"total order & {len(bj['TOTAL_ORDER'])} & "
        rf"{', '.join(bj['TOTAL_ORDER'])} \\",
        rf"tie-break missing & {len(bj['TIEBREAK_MISSING'])} & "
        rf"{', '.join(bj['TIEBREAK_MISSING'])} \\",
        rf"not top-$k$ & {len(bj['NOT_TOP_K'])} & "
        rf"{', '.join(bj['NOT_TOP_K']) or '--'} \\",
        r"\midrule",
        rf"flag: nested projection & {len(fl['nested_projection'])} & "
        rf"{', '.join(fl['nested_projection'])} \\",
        rf"flag: inner ranking lacks tie-break & "
        rf"{len(fl['official_sql_ranking_lacks_tiebreak'])} & "
        rf"{', '.join(fl['official_sql_ranking_lacks_tiebreak'])} \\",
        rf"flag: total via group key & "
        rf"{len(fl['total_order_via_group_key'])} & "
        rf"{', '.join(fl['total_order_via_group_key'])} \\",
    ]
    lbody = "\n".join(lrows)
    return rf"""% Source: benchmarks/results-v1/eval-bird-topk-v3.json (agent, gold)
% and eval-ldbc-topk.json (static; nothing executed)
\begin{{table*}}[t]
\centering\small
\setlength{{\tabcolsep}}{{4pt}}
\begin{{tabular}}{{@{{}}lrrrrrrrrrr@{{}}}}
\toprule
 & & \multicolumn{{3}}{{c}}{{certified by route}} &
\multicolumn{{3}}{{c}}{{not certified}} &
\multicolumn{{3}}{{c}}{{median ms}} \\
\cmidrule(lr){{3-5}}\cmidrule(lr){{6-8}}\cmidrule(lr){{9-11}}
 & \shortstack{{ranked\\pages}} & \shortstack{{total\\order}} &
\shortstack{{sequence-\\strict}} & \shortstack{{boundary-\\strict set}} &
{head} & page & \shortstack{{strictness\\statement}} &
\shortstack{{count and\\commit}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
\caption{{TopK routes over the ranked BIRD pages (an outer
\texttt{{ORDER BY}} \ldots{{}} \texttt{{LIMIT}} $k$ with zero offset),
each page counted once, under its first certifying route.}}
\label{{tab:topkroutes}}
\end{{table*}}

\begin{{table*}}[t]
\centering\small
\begin{{tabular}}{{@{{}}lrp{{0.6\textwidth}}@{{}}}}
\toprule
\textbf{{judgment}} & & \textbf{{templates}} \\
\midrule
{lbody}
\bottomrule
\end{{tabular}}
\caption{{Static TopK expressibility of the {lt['counts']['n_templates']}
LDBC SNB templates whose full contract requires top-$k$; no LDBC query
was executed.}}
\label{{tab:ldbctopk}}
\end{{table*}}
"""


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
\caption{{The frozen evidence experiment: test splits, three
seeds, \pnPrimaryRows\ task-runs, metrics as defined in
\S\ref{{sec:metrics}}. Coverage, conditional accuracy, and
correct-certified coverage describe certified-output mode; EM and
its paired contrast describe audit mode, in which text renders in
every arm. Post-gate UCR is 0.000 everywhere and supported-claim
retention is 1.0 by construction. SQL pre-gate UCR is a lower bound
under the SQL-conservative claim surface, so cross-interface UCR
magnitudes are not comparable.}}
\label{{tab:frozen}}
\end{{table*}}
"""
    interface = rf"""% T-interface — secondary contrast
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
    return frozen, interface


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
exclusive and each row sums to its draw count: exact = policy- or
oracle-lane gold, empty = the backend-certified empty-result rule
(suite-ineligible), budget = an oracle-envelope ceiling named per
receipt, n/a = template inapplicable to the store, unres.\ = the
one draw whose zero-row step inherits uncertified delivery from a
truncated upstream page.}}
\label{{tab:census}}
\end{{table}}
"""


def table_sql_surface() -> str:
    # Three layers (review §3.4): adapter capability support, and what
    # the end-to-end SQL benchmark agent constructs — from
    # tgms/evidence/adapter_sql.py and tgms/eval/baselines.py.
    return r"""\begin{table}[t]
\centering\small
\setlength{\tabcolsep}{3.5pt}
\begin{tabular}{lccl}
\toprule
\textbf{Claim form} & \textbf{TGMS ad.} & \textbf{SQL ad.} &
\textbf{SQL benchmark constructs} \\
\midrule
Membership   & \yes & \yes & membership \\
Scalar       & \yes & \yes & witness over cited value \\
Exact count  & \yes & \yes & witness over cited value \\
Complete set & \yes & \yes & not constructed \\
Exists       & \yes & \yes & witness over cited value \\
Nonexistence & \yes & \yes & not constructed \\
Basis-qualified & \yes & \yes & probe tasks \\
\bottomrule
\end{tabular}
\caption{Claim-form support by layer. Both adapters can establish
every capability, and the verifier supports every form; the
end-to-end SQL agent constructs the SQL-conservative claim surface,
checking each cited value, including a count value, as a membership
witness over the certificate-bearing page. Unsupported-claim
prevalence is never compared across interfaces.}
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
page query & $\pnSqlPageMs$\,ms \\
SQL cardinality certificate & $\pnSqlCertMs$\,ms \\
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


def table_scaling(sc: dict) -> str:
    def ms(v: float) -> str:
        return f"{v:.3f}" if v < 0.1 else (f"{v:.2f}" if v < 10
                                           else f"{v:.1f}")

    rows = []
    for t in sc["timing"]:
        n = t["rows"]
        lbl = f"{n:,}".replace(",", "{,}")
        rows.append(
            f"{lbl} & {ms(t['canonicalize_ms'] + t['digest_ms'])} & "
            f"{ms(t['verify_membership_ms'])} & "
            f"{ms(t['verify_completeset_ms'])} & "
            f"{t['verify_count_cert_ms']*1000:.1f} & "
            f"{t['multiclaim_per_claim_ms']*1000:.1f} \\\\")
    body = "\n".join(rows)
    return rf"""% T-scaling — verifier scaling, from eval-verifier-scaling.json
\begin{{table}}[t]
\centering\small
\begin{{tabular}}{{rrrrrr}}
\toprule
 & \multicolumn{{3}}{{c}}{{result-linear (ms)}}
 & \multicolumn{{2}}{{c}}{{flat ($\mu$s)}} \\
\cmidrule(lr){{2-4}}\cmidrule(lr){{5-6}}
rows & canon.+digest & member. & compl.-set
 & count cert. & +1 claim \\
\midrule
{body}
\bottomrule
\end{{tabular}}
\caption{{Verification cost against delivered-result size (median,
{sc['host']}). Canonicalization with digesting and the row-scanning
claim forms are linear in the delivered result. Descriptor
construction (\pnBuildUsFlat\,$\mu$s), certificate-path exact-count
verification, and the marginal cost of an additional claim over an
already-digested result are size-independent. No column touches the
underlying database.}}
\label{{tab:scaling}}
\end{{table}}
"""


# ---------------------------------------------------------------------
# D-170 strengthening round (lane I2): the top-k EvidenceBench cells
# and the truncation probe under ECQR gating (lane D).

PE_FAMILIES = ("COUNT", "SET", "all")
PE_BASELINES = ("C0", "C1", "C2")
PE_GATED = ("E", "E-aware")


def _pe_baseline(tp: dict, cond: str, fam: str) -> dict:
    """A committed baseline row (eval-trunc-probe.json) in user-visible
    terms: every committed answer is shown, so a shown answer is wrong
    unless it is correct."""
    c = tp["conditions"][cond]
    b = c["all"] if fam == "all" else c["by_family"][fam]
    return {"n": b["n"], "page_derived": b["page_derived"],
            "correct_shown": b["correct"],
            "wrong_shown": b["page_derived"] + b["other_wrong"],
            "abstained": b["no_commitment"] + b["error"]}


def _pe_gated(pe: dict, cond: str, fam: str) -> dict:
    c = pe["conditions"][cond]
    b = c["all"] if fam == "all" else c["by_family"][fam]
    o = b["outcome"]
    assert o["error"] == 0, (cond, fam)
    assert o["certified"] == b["certified_correct"] + b["user_visible_wrong"]
    return {"n": b["n"], "page_derived": b["proposed"]["page_derived"],
            "correct_shown": b["certified_correct"],
            "wrong_shown": b["user_visible_wrong"],
            "certified": o["certified"], "withheld": o["withheld"],
            "abstained": o["abstained"],
            "partial": b["withheld_with_partial_list"],
            "delivery_complete": b["delivery_complete"],
            "calls_mean": b["calls_per_question"]["mean"],
            "calls_median": b["calls_per_question"]["median"]}


def _pe_items(pe: dict, cond: str) -> list[dict]:
    return pe["per_item"][cond]


def probe_ecqr_macros(prior: list[tuple[str, str]]) -> list[tuple[str, str]]:
    pm = dict(prior)
    out: list[tuple[str, str]] = []
    # (1) eval-topk-cells.json: the 20 top-k EvidenceBench cells
    tc = _load("eval-topk-cells.json")
    ts = tc["summary"]
    assert not tc["truth_disagreements"]
    for ch in ("ecqr", "b1_value_only", "b2_taint_all"):
        assert not ts[ch]["verdict_differs_from_expected"], ch
    out += [
        ("pnTopkCellsN", str(tc["n_cells"])),
        ("pnTopkCellsCertify",
         str(tc["cells_by_expectation"]["must_certify"])),
        ("pnTopkCellsEcqrErr", str(ts["ecqr"]["false_certifications"]
                                   + ts["ecqr"]["false_rejections"])),
        ("pnTopkCellsValueFc",
         str(ts["b1_value_only"]["false_certifications"])),
        ("pnTopkCellsValueFr", str(ts["b1_value_only"]["false_rejections"])),
        ("pnTopkCellsTaintFc",
         str(ts["b2_taint_all"]["false_certifications"])),
        ("pnTopkCellsTaintFr", str(ts["b2_taint_all"]["false_rejections"])),
    ]
    # (2) eval-trunc-probe-ecqr.json; baseline rows from the committed
    #     eval-trunc-probe.json, whose sha256 the gated receipt records
    import hashlib
    pe = _load("eval-trunc-probe-ecqr.json")
    tp_path = RES / "eval-trunc-probe.json"
    tp = json.loads(tp_path.read_text())
    assert hashlib.sha256(tp_path.read_bytes()).hexdigest() == \
        pe["baseline"]["sha256"]
    assert pe["oracle_validation"]["page_only"]["passed"]
    assert pe["oracle_validation"]["enumerating"]["passed"]
    assert str(pe["eligible"]) == pm["pnProbeN"]
    assert pe["protocol"]["k"] == int(pm["pnProbeK"])

    def pct(a: int, n: int) -> str:
        return f"{100.0 * a / n:.1f}"
    base = {c: _pe_baseline(tp, c, "all") for c in PE_BASELINES}
    for c in PE_BASELINES:   # the gated receipt copied the same rows
        for fam in PE_FAMILIES:
            r = _pe_baseline(tp, c, fam)
            cp = pe["baseline"]["rows"][c][fam]["percent"]
            assert abs(cp["user_visible_wrong"]
                       - r["wrong_shown"] / r["n"]) < 1e-6, (c, fam)
    bw = [100.0 * base[c]["wrong_shown"] / base[c]["n"]
          for c in PE_BASELINES]
    E = {f: _pe_gated(pe, "E", f) for f in PE_FAMILIES}
    A = {f: _pe_gated(pe, "E-aware", f) for f in PE_FAMILIES}
    t2c = _pe_baseline(tp, "C2", "COUNT")
    # set answers under E: memberships inside the withheld answers only
    e_items = _pe_items(pe, "E")
    w_set = [i for i in e_items
             if i["family"] == "SET" and i["outcome"] == "withheld"]
    assert sum(1 for i in w_set if i["membership_supported"] > 0) == \
        E["SET"]["partial"]
    mem_sup = sum(i["membership_supported"] for i in w_set)
    mem_uns = sum(i["membership_unsupported"] for i in w_set)
    assert mem_uns == pe["conditions"]["E"]["by_family"]["SET"][
        "membership_unsupported_total"]
    # page-derived proposals: none certified, under either condition
    for cond in PE_GATED:
        ca = pe["conditions"][cond]["all"]
        assert ca["page_derived_certified"] == 0, cond
        assert ca["page_derived_withheld"] == ca["proposed"]["page_derived"]
    # the user-visible wrong answers under E-aware: certified complete
    # sets whose gold list repeats values (bag vs set)
    dup = [i for i in _pe_items(pe, "E-aware") if i["user_visible_wrong"]]
    assert len(dup) == A["all"]["wrong_shown"] == A["SET"]["wrong_shown"]
    for i in dup:
        assert i["family"] == "SET" and i["c1_verdict"] == "SUPPORTED"
        assert i["delivery_complete"] and i["seen"] == i["N"]
        assert i["membership_unsupported"] == 0
        assert i["membership_supported"] < i["N"]   # repeated gold values
    assert not [i for i in e_items if i["user_visible_wrong"]]
    rep = pe["c0_replay_reproducibility"]
    cr = pe["conditions"]["C0-replay"]["all"]["calls_per_question"]
    words = ["One", "Two", "Three"]
    out += [
        ("pnPeBaseWrongLo", f"{min(bw):.1f}"),
        ("pnPeBaseWrongHi", f"{max(bw):.1f}"),
        ("pnPeEWrongN", str(E["all"]["wrong_shown"])),
        ("pnPeAwareWrongN", str(A["all"]["wrong_shown"])),
        ("pnPeAwareWrongPct", pct(A["all"]["wrong_shown"], A["all"]["n"])),
        ("pnPeAwareSetWrongPct",
         pct(A["SET"]["wrong_shown"], A["SET"]["n"])),
        ("pnPePdE", str(E["all"]["page_derived"])),
        ("pnPePdAware", str(A["all"]["page_derived"])),
        ("pnPeECountCorrPct",
         pct(E["COUNT"]["correct_shown"], E["COUNT"]["n"])),
        ("pnPeECountWithheld", str(E["COUNT"]["withheld"])),
        ("pnPeAwareCountCorrPct",
         pct(A["COUNT"]["correct_shown"], A["COUNT"]["n"])),
        ("pnPeTotalCountWrongPct", pct(t2c["wrong_shown"], t2c["n"])),
        ("pnPeESetN", str(E["SET"]["n"])),
        ("pnPeESetWithheld", str(E["SET"]["withheld"])),
        ("pnPeESetPartial", str(E["SET"]["partial"])),
        ("pnPeESetCert", str(E["SET"]["certified"])),
        ("pnPeESetMemSupp", str(mem_sup)),
        ("pnPeESetMemUnsupp", str(mem_uns)),
        ("pnPeEAbstPct", pct(E["all"]["withheld"] + E["all"]["abstained"],
                             E["all"]["n"])),
        ("pnPeAwareAbstPct",
         pct(A["all"]["withheld"] + A["all"]["abstained"], A["all"]["n"])),
        ("pnPeESetAbstPct",
         pct(E["SET"]["withheld"] + E["SET"]["abstained"], E["SET"]["n"])),
        ("pnPeAwareSetAbstPct",
         pct(A["SET"]["withheld"] + A["SET"]["abstained"], A["SET"]["n"])),
        ("pnPeAwareNoCommitPct", pct(A["all"]["abstained"], A["all"]["n"])),
        ("pnPeECalls", f"{E['all']['calls_mean']:.2f}"),
        ("pnPeAwareCalls", f"{A['all']['calls_mean']:.2f}"),
        ("pnPeReplayCalls", f"{cr['mean']:.2f}"),
        ("pnPeECertCorrPct", pct(E["all"]["correct_shown"], E["all"]["n"])),
        ("pnPeAwareCertCorrPct",
         pct(A["all"]["correct_shown"], A["all"]["n"])),
        ("pnPeAwareSetCertCorrPct",
         pct(A["SET"]["correct_shown"], A["SET"]["n"])),
        ("pnPeEDelivComplete", str(E["all"]["delivery_complete"])),
        ("pnPeAwareDelivComplete", str(A["all"]["delivery_complete"])),
        ("pnPeNleHundred",
         str(pe["conditions"]["E"]["all"]["n_le_100"])),
        ("pnPeOracleN",
         str(pe["oracle_validation"]["page_only"]["items_checked"])),
        ("pnPeReplayAgree", str(rep["class_agrees_with_committed_C0"])),
        ("pnPeBudget", str(pe["protocol"]["tool_call_budget"])),
        ("pnPeDupN", str(len(dup))),
    ]
    for w, i in zip(words, dup):
        out += [(f"pnPeDup{w}Qid", str(i["question_id"])),
                (f"pnPeDup{w}Rows", str(i["N"])),
                (f"pnPeDup{w}Distinct", str(i["membership_supported"]))]
    return out


def table_topk_cells() -> str:
    """The 20 top-k EvidenceBench cells, one row each, with the verdict
    of every checker; a decision that disagrees with the cell's
    expectation is set in bold."""
    tc = _load("eval-topk-cells.json")
    S = "SUPPORTED"
    short = {"SUPPORTED": "supported", "REJECT": "reject",
             "BLOCKED": "blocked", "REJECTED_INTEGRITY": "integrity"}

    def v(x: str) -> str:
        return short.get(x, x.replace("UNSUPPORTED_", "").lower()
                         .replace("_", " "))
    rows = []
    for c in tc["cells"]:
        ok_cert = c["expectation"] == "must_certify"
        cells = []
        for ch in ("ecqr", "b1_value_only", "b2_taint_all"):
            x = c["verdicts"][ch]
            t = v(x)
            if (x == S) != ok_cert:
                t = rf"\textbf{{{t}}}"
            cells.append(t)
        exp = "certify" if ok_cert else "not certify"
        rows.append(" & ".join([rf"\texttt{{{_tex(c['fault'])}}}", exp]
                               + cells) + r" \\")
    body = "\n".join(rows)
    return rf"""% Source: benchmarks/results-v1/eval-topk-cells.json
\begin{{table*}}[t]
\centering\small
\begin{{tabular}}{{@{{}}lllll@{{}}}}
\toprule
\textbf{{cell}} & \textbf{{expected}} & \textbf{{ECQR}} &
\textbf{{value-only}} & \textbf{{taint}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
\caption{{The \pnTopkCellsN\ top-$k$ EvidenceBench cells and each
checker's verdict; a wrong decision is in bold. ECQR errs on
\pnTopkCellsEcqrErr\ cells, value-only checking certifies
\pnTopkCellsValueFc\ faults, and the taint certifies
\pnTopkCellsTaintFc\ fault and rejects \pnTopkCellsTaintFr\ valid
cells.}}
\label{{tab:topkcells}}
\end{{table*}}
"""


def table_probe_ecqr() -> str:
    """All conditions x families x columns of the gated truncation probe
    (counts; baselines from the committed eval-trunc-probe.json)."""
    pe = _load("eval-trunc-probe-ecqr.json")
    tp = json.loads((RES / "eval-trunc-probe.json").read_text())
    cr = pe["conditions"]["C0-replay"]["by_family"]
    cra = pe["conditions"]["C0-replay"]["all"]
    lbl = {"C0": "bare page", "C1": "+ truncation flag",
           "C2": "+ exact total", "E": "ECQR gate (E)",
           "E-aware": "aware gate (E-aware)"}
    fam_l = {"COUNT": "count", "SET": "set", "all": "all"}
    rows = []
    for cond in PE_BASELINES + PE_GATED:
        if cond == "E":
            rows.append(r"\midrule")
        for fam in PE_FAMILIES:
            name = lbl[cond] if fam == "COUNT" else ""
            if cond in PE_BASELINES:
                r = _pe_baseline(tp, cond, fam)
                if cond == "C0":
                    c = cra if fam == "all" else cr[fam]
                    calls = (f"{c['calls_per_question']['mean']:.2f} / "
                             f"{c['calls_per_question']['median']:g}"
                             r"$^\dagger$")
                else:
                    calls = "--"
                cells = [str(r["n"]), str(r["page_derived"]),
                         str(r["correct_shown"]), str(r["wrong_shown"]),
                         "--", "--", str(r["abstained"]), "--", "--", calls]
            else:
                r = _pe_gated(pe, cond, fam)
                cells = [str(r["n"]), str(r["page_derived"]),
                         str(r["correct_shown"]), str(r["wrong_shown"]),
                         str(r["certified"]), str(r["withheld"]),
                         str(r["abstained"]), str(r["partial"]),
                         str(r["delivery_complete"]),
                         f"{r['calls_mean']:.2f} / {r['calls_median']:g}"]
            rows.append(" & ".join([name, fam_l[fam]] + cells) + r" \\")
    body = "\n".join(rows)
    return rf"""% Source: benchmarks/results-v1/eval-trunc-probe-ecqr.json (E, E-aware,
% C0-replay calls) and eval-trunc-probe.json (committed C0, C1, C2 rows)
\begin{{table*}}[t]
\centering\small
\setlength{{\tabcolsep}}{{4pt}}
\begin{{tabular}}{{@{{}}llrrrrrrrrrr@{{}}}}
\toprule
\textbf{{condition}} & & $n$ & \shortstack{{page-derived\\proposed}} &
\shortstack{{correct\\shown}} & \shortstack{{wrong\\shown}} &
certified & withheld & abstained & \shortstack{{partial\\list}} &
\shortstack{{complete\\delivery}} & \shortstack{{calls per question\\mean / median}} \\
\midrule
{body}
\bottomrule
\end{{tabular}}
\caption{{Truncation probe with and without the ECQR gate: every
condition by question family, in questions. Without the gate every
committed answer is shown; with it, a shown answer is a certified one,
a withheld answer was proposed but not certified, and an abstained
question received no answer. Partial list: withheld set answers shown
as a labelled partial list of retrieved records. Complete delivery:
runs whose retrieved pages covered the whole result.
$^\dagger$From the C0 replay, whose classes agree with the committed C0
run on every question.}}
\label{{tab:probeecqr}}
\end{{table*}}
"""



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", type=Path, required=True)
    args = ap.parse_args()
    pn = json.loads((RES / "paper_numbers.json").read_text())
    fm = json.loads((RES / "eval-fault-matrix.json").read_text())
    sc = json.loads((RES / "eval-verifier-scaling.json").read_text())
    uc = json.loads(
        (RES / "eval-unsupported-composition.json").read_text())
    (args.outdir / "pn-macros.tex").write_text(macros(pn, fm, sc, uc))
    hdr = ("% GENERATED by scripts/paper_macros.py; never hand-edit.\n\n")
    (args.outdir / "tab-fault.tex").write_text(hdr + table_fault(fm))
    frozen_t, interface_t = table_frozen(pn)
    (args.outdir / "tab-frozen.tex").write_text(hdr + frozen_t)
    (args.outdir / "tab-interface.tex").write_text(hdr + interface_t)
    (args.outdir / "tab-census.tex").write_text(hdr + table_census(pn))
    (args.outdir / "tab-sqlsurface.tex").write_text(
        hdr + table_sql_surface())
    (args.outdir / "tab-guardrail.tex").write_text(
        hdr + table_guardrail(pn))
    (args.outdir / "tab-cost.tex").write_text(hdr + table_cost(pn))
    (args.outdir / "tab-scaling.tex").write_text(hdr + table_scaling(sc))
    (args.outdir / "tab-baselines.tex").write_text(hdr + table_baselines())
    (args.outdir / "tab-snapshot.tex").write_text(hdr + table_snapshot())
    (args.outdir / "tab-topk.tex").write_text(hdr + table_topk())
    (args.outdir / "tab-probe-ecqr.tex").write_text(
        hdr + table_probe_ecqr())
    (args.outdir / "tab-topk-cells.tex").write_text(
        hdr + table_topk_cells())
    print(f"wrote {args.outdir}/pn-macros.tex, {args.outdir}/tab-data.tex")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
