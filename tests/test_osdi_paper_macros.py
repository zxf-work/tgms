"""Tests for the OSDI paper's receipts machinery (Lane W, task W2):
``scripts/osdi_paper_macros.py`` and ``scripts/osdi_paper_figures.py``.

Three things are checked, matching the task's own discipline:

1. the macros recompute to frozen values (a moved or edited record fails
   loudly, both here and in the generator's own ``require``/``eq``);
2. a tampered copy of a landed record makes the generator's verification
   fail -- proving the numbers are actually recomputed from row data, not
   copied from the record's own summary or from prose;
3. figure generation runs and produces files. matplotlib is not installed
   in ``$HOME/.venvs/tgms`` at the time this was written, so the PDF/PNG
   assertions are skipped with an explicit reason when it is absent; the
   CSV-writing half of the figure generator has no such dependency and is
   always exercised.

Every test loads a fresh copy of the module under test via
``importlib.util`` (the pattern ``tests/test_e14_harnesses.py`` already
uses for `scripts/*.py`) so the generator's module-level ``CHECKS``/
``FAILURES`` accumulators start clean for each test, and record-path
constants can be monkeypatched to a tampered copy without touching the
real committed record.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """Load scripts/<name>.py as a fresh module (fresh module-level state)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _venv_python() -> str:
    """The interpreter running this test -- always the pinned project venv
    per the task's own rule (never the in-repo .venv)."""
    return sys.executable


def _ruff_path() -> Path | None:
    """The ruff binary next to this interpreter, if any."""
    candidate = Path(sys.executable).parent / "ruff"
    return candidate if candidate.exists() else None


# --------------------------------------------------------------------------
# osdi_paper_macros.py: recomputation and frozen values
# --------------------------------------------------------------------------

def _run_all_landed(mod):
    """Run every landed-claim compute_* function against a fresh Macros()."""
    m = mod.Macros()
    mod.compute_c1(m)
    mod.compute_c3(m)
    mod.compute_b1_v2(m)
    mod._void_b1_v2_treatment_provenance(m)
    mod.compute_b1_v2e(m)
    mod.compute_b1_co7(m)
    mod.compute_c4(m)
    mod.compute_c5(m)
    mod.compute_c6(m)
    mod.compute_c8(m)
    mod.compute_c7_dag(m)
    mod.compute_c7_r18(m)
    mod.compute_c7_storm_v1(m)
    mod.compute_c7_storm_v2_probe(m)
    mod.compute_c7_storm_v2(m)
    mod.compute_d160(m)
    mod.compute_d160_llm_direct_fix(m)
    mod.compute_c2(m)
    mod.compute_ladder(m)
    mod.compute_longevity_soak(m)
    mod.compute_longevity_rederived(m)
    mod.compute_c10_live_osv(m)
    return m


def test_landed_macros_recompute_without_any_verification_failure():
    mod = _load("osdi_paper_macros")
    assert mod.CHECKS == 0 and mod.FAILURES == []
    _run_all_landed(mod)
    assert mod.FAILURES == [], f"unexpected verification failures: {mod.FAILURES}"
    assert mod.CHECKS > 100, "expected many row-level assertions, not a handful"


FROZEN_LANDED_VALUES = {
    "osdiCrashTrials": "10{,}000",
    "osdiCrashProblems": "0",
    "osdiCrashBoundaries": "10",
    "osdiCrashWall": "4{,}912.85",
    "osdiManifestBytesCtl": "25.97",
    "osdiManifestBytesTrt": "62.0",
    "osdiManifestCommitRatio": "1.798",
    "osdiManifestColdOpen": "8.02",
    "osdiB1v2ControlCommit": "886805f",
    "osdiB1v2TreatmentCommit": "7a5ff98",
    "osdiB1v2BytesControlMB": "73.8",
    "osdiB1v2BytesTreatmentMB": "74.4",
    "osdiB1v2BytesPaired": "1.008",
    "osdiB1v2SegmentBytesControlMB": "159.0",
    "osdiB1v2SegmentBytesTreatmentMB": "163.4",
    "osdiB1v2ManifestDecileControl": "1.115",
    "osdiB1v2ManifestDecileTreatment": "1.079",
    "osdiB1v2ManifestDecileK128": "1.068",
    "osdiB1v2ManifestDecileK1024": "1.150",
    "osdiB1v2TotalDecileControl": "1.733",
    "osdiB1v2TotalDecileTreatment": "1.674",
    "osdiB1v2P50ControlMs": "5.430",
    "osdiB1v2P50TreatmentMs": "5.478",
    "osdiB1v2P50Paired": "1.009",
    "osdiB1v2OpenControlMs": "2247.9",
    "osdiB1v2OpenTreatmentMs": "1705.3",
    "osdiB1v2OpenPaired": "0.76",
    "osdiB1v2OpenControlGeneration": "10{,}759",
    "osdiB1v2OpenTreatmentGeneration": "11{,}029",
    "osdiB1v2BuildOpsPerSecRatioAt2p5M": "2.10",
    "osdiB1v2OpenComponentStatus": "not computed",
    "osdiB1v2eTreatmentCommit": "e5d4171",
    "osdiB1v2eTotalDecileTreatment": "1.017",
    "osdiB1v2eTotalDecileControl": "1.696",
    "osdiB1v2eResidualFirstUs": "33.06",
    "osdiB1v2eResidualLastUs": "30.38",
    "osdiB1v2eP50TreatmentMs": "3.365",
    "osdiB1v2eP50ControlMs": "4.704",
    "osdiB1v2eP50Paired": "0.715",
    "osdiB1v2eWallP50TreatmentMs": "4.274",
    "osdiB1v2eWallP50ControlMs": "5.303",
    "osdiB1v2eWallPaired": "0.806",
    "osdiB1v2eOpenComponentMs": "87.06",
    "osdiB1v2eOpenCheckpointMs": "42.124",
    "osdiB1v2eOpenMerkleVerifyMs": "40.204",
    "osdiB1v2eOpenStateBuildMs": "1.828",
    "osdiB1v2eOpenDeltaReplayMs": "2.899",
    "osdiB1v2eOpenDictionaryMs": "1066.5",
    "osdiB1v2eOpenTotalMs": "1191.8",
    "osdiB1v2eOpenGeneration": "10{,}365",
    "osdiB1v2eControlOpenStatus": "confounded (concurrent backup transfer)",
    "osdiB1v2eManifestBytesTreatment": "1592",
    "osdiB1v2eManifestBytesControl": "1565",
    "osdiB1co7TreatmentCommit": "ebe1dc2",
    "osdiB1co7CheckpointReadParseMs": "12.67",
    "osdiB1co7MerkleVerifyMs": "34.86",
    "osdiB1co7StateBuildMs": "5.94",
    "osdiB1co7DeltaReplayMs": "6.26",
    "osdiB1co7ComponentMs": "59.72",
    "osdiB1co7DictionaryOpenMs": "1092.7",
    "osdiB1co7TotalMs": "1155.0",
    "osdiB1co7Generation": "10{,}116",
    "osdiB1co7DeltaCount": "388",
    "osdiB1co7CheckpointGeneration": "9728",
    "osdiB1co7StateBuildPerDeltaUs": "15.3",
    "osdiB1co7DeltaReplayPerDeltaUs": "16.1",
    "osdiB1co7ControlOpenMs": "10100.2",
    "osdiB1co7ControlGeneration": "10{,}042",
    "osdiB1co7ControlDeltaCount": "314",
    "osdiB1co7ControlPerDeltaMs": "28.7",
    "osdiB1co7OpenRatio": "0.114",
    "osdiB1WorstPhaseOpenTreatmentMs": "63.6",
    "osdiB1WorstPhaseOpenControlS": "14.7",
    "osdiVhRss": "1.259",
    "osdiVhWall": "1.87",
    "osdiVhRatio": "7.32",
    "osdiVhProjHundredM": "12.6",
    "osdiReadersMax": "32",
    "osdiReaderVmhwm": "1.19--1.22",
    "osdiAggQpsOne": "2.85",
    "osdiAggQpsThirtyTwo": "27.72",
    "osdiWriterTailCost": "+47.5\\%/+42.7\\%",
    "osdiFalseFreshCarveTwo": "0",
    "osdiRowTouchRate": "47.4",
    "osdiNewIdentityFF": "89",
    "osdiAvoidedPct": "99.0",
    "osdiFaultTrials": "3102",
    "osdiSilentPre": "303",
    "osdiSilentPost": "0",
    "osdiFOneNineBefore": "271",
    "osdiFTwoThree": "32",
    "osdiDagCells": "40",
    "osdiDagV1FalseSafeCells": "20",
    "osdiDagV2FalseSafeCells": "0",
    "osdiDagV3FalseSafeCells": "0",
    "osdiDagV1FalseSafePerCell": "3",
    "osdiDagV2ExtraVisitsSeedZero": "61",
    "osdiDagV2ExtraVisitsSeedOne": "62",
    "osdiDagV3ExtraVisitsSeedZero": "15",
    "osdiDagV3ExtraVisitsSeedOne": "11",
    "osdiDagV3AllTopTerm": "0",
    "osdiDagFalseFreshTotal": "0",
    "osdiR18Artifacts": "10{,}000",
    "osdiR18IntersectsCallsMedian": "13{,}009",
    "osdiR18LookupMsMedian": "26.91",
    "osdiR18SurvivorFraction": "71.3",
    "osdiR18CheckSecondsMedian": "867.6",
    "osdiR18TtfL1Seconds": "1977.6",
    "osdiR18TtfGlobalSeconds": "1595.3",
    "osdiR18Speedup": "0.807",
    "osdiR18Precision": "8.51",
    "osdiR18AvoidedRecompute": "29.9",
    "osdiStormV1Commit": "8962b78",
    "osdiStormV1Cells": "36",
    "osdiStormV1CellsFailed": "0",
    "osdiStormV1FalseFreshTgmsCellsNonzero": "0",
    "osdiStormV1SpeedupN1kSeed0": "1.938",
    "osdiStormV1SpeedupSynthC1None": "1.895",
    "osdiStormV1SpeedupSynthC1Deep": "1.908",
    "osdiStormV1SpeedupSynthC3None": "1.933",
    "osdiStormV1SpeedupSynthC3Deep": "1.939",
    "osdiStormV1SpeedupSynthC4None": "1.788",
    "osdiStormV1SpeedupSynthC4Deep": "1.756",
    "osdiStormV1SpeedupCollegeMsgC1None": "2.333",
    "osdiStormV1SpeedupCollegeMsgC1Deep": "2.304",
    "osdiStormV1SpeedupCollegeMsgC3None": "2.382",
    "osdiStormV1SpeedupCollegeMsgC3Deep": "2.427",
    "osdiStormV1SpeedupCollegeMsgC4None": "2.325",
    "osdiStormV1SpeedupCollegeMsgC4Deep": "2.151",
    "osdiStormV1SpeedupGridMin": "1.742",
    "osdiStormV1SpeedupGridMax": "2.558",
    "osdiStormV1AvoidedDecisionC1Median": "0.309",
    "osdiStormV1AvoidedDecisionC3Median": "0.312",
    "osdiStormV1AvoidedDecisionC4Median": "0.312",
    "osdiStormV1Batches": "720",
    "osdiStormV1RowTouchFalseFreshMedian": "1.000",
    "osdiStormV1EntityTouchFalseFreshMedian": "0.993",
    "osdiStormV1WindowOverlapFalseFreshMedian": "0.189",
    "osdiStormV1WindowOverlapNonzeroBatches": "258",
    "osdiStormV1NewIdentityBatches": "195",
    "osdiStormV1NewIdentityRowTouchMedian": "1.000",
    "osdiStormV1P4ViolationCells": "0",
    "osdiStormV2ProbeCommit": "fdd393c",
    "osdiStormV2ProbeBatches": "5",
    "osdiStormV2ProbeWallS": "12{,}452.8",
    "osdiStormV2ProbeGlobalTtfS": "1{,}569.3",
    "osdiStormV2ProbeTgmsL1TtfS": "806.3",
    "osdiStormV2ProbeSpeedupN10k": "1.946",
    "osdiStormV2ProbeAvoidedDecision": "0.758",
    "osdiStormV2ProbeSurvivorMedian": "0.283",
    "osdiStormV2ProbePrecisionMedian": "0.211",
    "osdiStormV2ProbeIntersectsMedian": "29{,}193",
    "osdiStormV2ProbeR18Tripped": "no",
    "osdiStormV2ProbeAllTopTerms": "0",
    "osdiStormV2ProbeNonComputeArtifacts": "8203",
    "osdiStormV2ProbeCheckWallMedianS": "336.9",
    "osdiStormV2Commit": "fdd393c",
    "osdiStormV2Cells": "36",
    "osdiStormV2CellsFailed": "0",
    "osdiStormV2AllTopTerms": "0",
    "osdiStormV2NonComputeArtifacts": "29{,}826",
    "osdiStormV2SpeedupN1kSeed0": "5.173",
    "osdiStormV2AvoidedDecisionC1Median": "0.755",
    "osdiStormV2C1Batches": "240",
    "osdiStormV2SurvivorFractionC1Median": "0.262",
    "osdiStormV2PrecisionC1Median": "0.187",
    "osdiStormV2SurvivorFractionSynthC1Median": "0.266",
    "osdiStormV2SurvivorFractionCollegeMsgC1Median": "0.257",
    "osdiStormV2PrecisionSynthC1Median": "0.182",
    "osdiStormV2PrecisionCollegeMsgC1Median": "0.194",
    "osdiStormV2FalseFreshTgmsCellsNonzero": "0",
    "osdiStormV2SpeedupSynthC1None": "5.173",
    "osdiStormV2SpeedupSynthC1Deep": "4.902",
    "osdiStormV2SpeedupSynthC3None": "5.642",
    "osdiStormV2SpeedupSynthC3Deep": "5.217",
    "osdiStormV2SpeedupSynthC4None": "6.367",
    "osdiStormV2SpeedupSynthC4Deep": "5.022",
    "osdiStormV2SpeedupCollegeMsgC1None": "6.189",
    "osdiStormV2SpeedupCollegeMsgC1Deep": "6.821",
    "osdiStormV2SpeedupCollegeMsgC3None": "6.662",
    "osdiStormV2SpeedupCollegeMsgC3Deep": "6.332",
    "osdiStormV2SpeedupCollegeMsgC4None": "7.433",
    "osdiStormV2SpeedupCollegeMsgC4Deep": "8.071",
    "osdiStormV2SpeedupGridMin": "4.588",
    "osdiStormV2SpeedupGridMax": "8.863",
    "osdiD160Tasks": "94",
    "osdiD160TaskRuns": "282",
    "osdiD160OursCarrying": "112",
    "osdiD160OursCoverage": "0.397",
    "osdiD160OursCondAcc": "0.509",
    "osdiD160OursUcrGated": "0",
    "osdiD160OursUcrPreGate": "0.212",
    "osdiD160B6eCoverage": "0.830",
    "osdiD160B6eCondAcc": "0.333",
    "osdiD160B5Em": "0.181",
    "osdiD160LlmDirectCarrying": "0",
    "osdiD160LlmDirectOverflowErrors": "216",
    "osdiD160LlmDirectCoverageFixed": "0.000",
    "osdiD160LlmDirectErrorsFixed": "0",
    "osdiD160LlmDirectRawEmFixed": "0.064",
    "osdiD160LlmDirectTokenizerFixed": "hf\\_real",
    "osdiD160LlmDirectBudgetFixed": "8000",
    "osdiOldGateCoverage": "0.706",
    "osdiOldGateUcr": "0",
    "osdiOldGateCondAcc": "0.548",
    "osdiCorruptionTrials": "10{,}000",
    "osdiCorruptionClasses": "13",
    "osdiCorruptionMutations": "7",
    "osdiCorruptionDetected": "6587",
    "osdiCorruptionDetectedPre": "5966",
    "osdiCorruptionDetectedPost": "6587",
    "osdiCorruptionSilentPre": "0",
    "osdiCorruptionSilentPost": "0",
    "osdiCorruptionBlobDetectedPre": "0/621",
    "osdiCorruptionBlobDetectedPost": "621/621",
    "osdiCorruptionBlobTrials": "710",
    "osdiCorruptionBlobDetectedAllPre": "89/710",
    "osdiCorruptionBlobDetectedAllPost": "710/710",
    "osdiCorruptionBlobMutationAlreadyDetected": "delete\\_file",
    "osdiCorruptionCellsMovedPost": "0",
    "osdiLadderPlans": "12",
    "osdiLadderOperatorsCovered": "14",
    "osdiLadderRung1Min": "0.992",
    "osdiLadderRung1Max": "1.064",
    "osdiLadderRung2EntityHistory": "1.13",
    "osdiLadderRung2VersionHistory": "2.54",
    "osdiLadderRung3BytesOneStepMedian": "3309",
    "osdiLadderRung3BytesThreeStepMedian": "7549",
    "osdiLadderRung3Deterministic": "12",
    "osdiLadderRung4VerifyMsMedian": "5.06",
    "osdiLadderRung5TokensMedian": "1811.5",
    "osdiLadderRung5ToolCallsEqualExecutedSteps": "12",
    "osdiLadderPlansTruncated": "2",
    "osdiSoakHours": "24",
    "osdiSoakCommit": "886805f",
    "osdiSoakEntitiesStart": "1{,}000{,}000",
    "osdiSoakEntitiesEnd": "1{,}730{,}492",
    "osdiSoakBatches": "1{,}074{,}952",
    "osdiSoakWriterLives": "42",
    "osdiSoakRecoveries": "41",
    "osdiSoakRecoveriesSigabrt": "23",
    "osdiSoakRecoveriesExit137": "18",
    "osdiSoakUnexpectedRecoveries": "0",
    "osdiSoakReaderDeaths": "2",
    "osdiSoakReaderDeathCause": "reader torn-tail race, pre-fix engine",
    "osdiSoakVerifyHealthy": "true",
    "osdiSoakWriterErrorsManifest": "1",
    "osdiSoakWriterErrorsTrue": "249",
    "osdiSoakThroughputStart": "24.03",
    "osdiSoakThroughputEnd": "16.41",
    "osdiSoakP99StartMs": "485.6",
    "osdiSoakP99EndMs": "3{,}740.5",
    "osdiSoakManifestGrowthBps": "-3.4",
    "osdiSoakSegmentGrowthBps": "1{,}341.1",
    "osdiSoakDigestStatus": "not computed",
    "osdiSoakReplayProjectedTB": "280.8",
    "osdiSoakCompactions": "2415",
    "osdiSoakWriterWithinLifeSlopeMedianKBps": "3165.6",
    "osdiSoakWriterWithinLifeSlopeMinKBps": "1921.1",
    "osdiSoakWriterWithinLifeSlopeMaxKBps": "4154.2",
    "osdiSoakWriterLivesFitted": "42",
    "osdiSoakWriterLivesPositive": "42",
    "osdiSoakReaderWithinLifeSlopeMedianKBps": "5.32",
    "osdiSoakReaderWithinLifeSlopeMaxKBps": "47.07",
    "osdiSoakFirstVsLastSlopeKBps": "111.6",
    "osdiSoakCommitsPerSecMean": "20.2",
    "osdiSoakBytesPerCommitLiveKB": "157",
    "osdiSoakReplayOutcome": "aborted_oom",
    "osdiSoakReplayOomRssGB": "83.0",
    "osdiSoakReplayOomGeneration": "513{,}024",
    "osdiSoakReplayFractionApplied": "0.476",
    "osdiSoakReplayKBPerGeneration": "161.8",
    "osdiSoakReplayElapsedH": "3.37",
    "osdiLiveDays": "1.89",
    "osdiLiveAdvisories": "32{,}827",
    "osdiLiveCorrections": "1",
}


def test_frozen_macro_values_match_the_generator():
    """A moved or edited record would change one of these; a coincidental
    self-consistent-but-wrong recomputation would not also match this
    independently-typed table."""
    mod = _load("osdi_paper_macros")
    m = _run_all_landed(mod)
    values = {name: value for name, value, _ in m.items}
    for name, expected in FROZEN_LANDED_VALUES.items():
        assert name in values, f"expected macro {name} was not emitted"
        assert values[name] == expected, (
            f"{name}: generator emitted {values[name]!r}, frozen test expects {expected!r}")


def test_pending_macros_raise_a_latex_error_never_a_placeholder_number():
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.add_pending_stubs(m)
    expected_names = {
        "osdiTtfSpeedup", "osdiStormCells", "osdiStormFalseFresh",
        "osdiStormSpeedupN1k", "osdiStormAvoidedN1k",
        "osdiLdbcExpressible", "osdiLdbcExecuted", "osdiLdbcValidated",
    }
    got_names = {name for name, _, _ in m.items}
    assert got_names == expected_names
    for name, value, provenance in m.items:
        assert value.startswith(r"\errmessage{"), (
            f"{name}: pending macro must render to \\errmessage, got {value!r}")
        assert provenance.startswith("PENDING -- "), f"{name}: provenance must flag PENDING"
        # the lane name must be inside the rendered LaTeX error, not just the comment,
        # since \newcommand{...}{\errmessage{...}} is what actually halts compilation
        lane = provenance.split("PENDING -- ", 1)[1].split(":", 1)[0]
        assert lane in value, f"{name}: lane {lane!r} missing from the rendered errmessage"


def test_full_macro_set_has_no_duplicate_names_and_covers_every_skeleton_claim():
    mod = _load("osdi_paper_macros")
    m = _run_all_landed(mod)
    mod.add_pending_stubs(m)
    names = [name for name, _, _ in m.items]
    assert len(names) == len(set(names)), "duplicate macro name"
    assert len(names) == len(FROZEN_LANDED_VALUES) + 8


def test_cli_check_mode_agrees_with_committed_output(tmp_path):
    """Running the generator twice must be idempotent (--check passes),
    the property the task's frozen-report discipline depends on."""
    env_root = ROOT
    subprocess.run([_venv_python(), str(env_root / "scripts" / "osdi_paper_macros.py")],
                    cwd=env_root, check=True, capture_output=True, text=True)
    result = subprocess.run(
        [_venv_python(), str(env_root / "scripts" / "osdi_paper_macros.py"), "--check"],
        cwd=env_root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _out_path() -> Path:
    return ROOT / "paper" / "osdi" / "generated" / "osdi-macros.tex"


def _run_cli(*flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_venv_python(), str(ROOT / "scripts" / "osdi_paper_macros.py"), *flags],
        cwd=ROOT, capture_output=True, text=True)


def test_cli_check_mode_regenerates_a_missing_tex_and_exits_zero():
    """A fresh worktree (or one behind a merge) has no gitignored paper/
    tree at all -- ``--check`` must heal that, not just refuse it."""
    out_path = _out_path()
    _run_cli()  # make sure a good copy exists first
    good = out_path.read_text(encoding="utf-8")
    out_path.unlink()
    try:
        result = _run_cli("--check")
        assert result.returncode == 0, result.stderr
        assert "regenerated" in result.stdout
        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == good
    finally:
        if not out_path.exists():
            out_path.write_text(good, encoding="utf-8")


def test_cli_check_mode_regenerates_a_stale_tex_and_exits_zero():
    out_path = _out_path()
    _run_cli()
    good = out_path.read_text(encoding="utf-8")
    out_path.write_text(good + "% stale\n", encoding="utf-8")
    try:
        result = _run_cli("--check")
        assert result.returncode == 0, result.stderr
        assert "regenerated" in result.stdout
        assert out_path.read_text(encoding="utf-8") == good
    finally:
        out_path.write_text(good, encoding="utf-8")


def test_cli_check_mode_reports_up_to_date_and_does_not_rewrite():
    out_path = _out_path()
    _run_cli()
    before = out_path.stat().st_mtime_ns
    result = _run_cli("--check")
    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout
    assert out_path.stat().st_mtime_ns == before, "up-to-date --check must not rewrite the file"


def test_cli_check_only_mode_fails_on_stale_without_writing():
    out_path = _out_path()
    _run_cli()
    good = out_path.read_text(encoding="utf-8")
    out_path.write_text(good + "% stale\n", encoding="utf-8")
    before = out_path.stat().st_mtime_ns
    try:
        result = _run_cli("--check-only")
        assert result.returncode == 1
        assert "stale generated file" in result.stderr
        assert out_path.stat().st_mtime_ns == before, "--check-only must never write"
        assert out_path.read_text(encoding="utf-8") == good + "% stale\n"
    finally:
        out_path.write_text(good, encoding="utf-8")


def test_cli_check_only_mode_fails_on_missing_without_writing():
    out_path = _out_path()
    _run_cli()
    good = out_path.read_text(encoding="utf-8")
    out_path.unlink()
    try:
        result = _run_cli("--check-only")
        assert result.returncode == 1
        assert "stale generated file" in result.stderr
        assert not out_path.exists(), "--check-only must never write"
    finally:
        if not out_path.exists():
            out_path.write_text(good, encoding="utf-8")


def test_cli_check_only_mode_agrees_with_up_to_date_output_and_does_not_rewrite():
    out_path = _out_path()
    _run_cli()
    before = out_path.stat().st_mtime_ns
    result = _run_cli("--check-only")
    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout
    assert out_path.stat().st_mtime_ns == before


def test_cli_no_write_alias_matches_check_only():
    out_path = _out_path()
    _run_cli()
    good = out_path.read_text(encoding="utf-8")
    out_path.write_text(good + "% stale\n", encoding="utf-8")
    try:
        result = _run_cli("--no-write")
        assert result.returncode == 1
        assert "stale generated file" in result.stderr
    finally:
        out_path.write_text(good, encoding="utf-8")


def test_cli_check_and_check_only_are_mutually_exclusive():
    result = _run_cli("--check", "--check-only")
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


# --------------------------------------------------------------------------
# osdi_paper_macros.py: a tampered record fails loudly
# --------------------------------------------------------------------------

def test_tampered_crash_record_fails_verification(tmp_path):
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "eval-crash-campaign-2026-09-13.json"
    data = json.loads(mod.CRASH_V1.read_text(encoding="utf-8"))
    # Flip one trial's Q1 (acknowledged-write survival) outcome without
    # touching total_problems/per_boundary -- exactly the kind of edit a
    # careless hand-fix to a record could make.
    data["results"][0]["q1_acked_survive"] = False
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.CRASH_V1 = tampered
    m = mod.Macros()
    mod.compute_c1(m)
    assert mod.FAILURES, "tampering a Q1 outcome must be caught, not silently accepted"
    assert any("Q1-Q4" in f for f in mod.FAILURES)


def test_tampered_crash_record_wall_time_mismatch_fails(tmp_path):
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "eval-crash-campaign-2026-09-13.json"
    data = json.loads(mod.CRASH_V1.read_text(encoding="utf-8"))
    data["wall_s"] = data["wall_s"] + 1000.0  # record's own field now disagrees with its rows
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.CRASH_V1 = tampered
    m = mod.Macros()
    mod.compute_c1(m)
    assert any("wall_s" in f for f in mod.FAILURES)


def test_tampered_b1_v2_manifest_digest_mismatch_fails(tmp_path):
    """The B1-v2 (manifest format 3) record's own manifest carries a sha256
    of its `measurements` block (canonical, sort_keys JSON) as
    `result_digest`; editing a reported number without recomputing that
    digest must be caught before any B1-v2 macro is even computed -- same
    discipline as the D160 rows-digest tamper tests above, applied to this
    record's own digest scheme."""
    mod = _load("osdi_paper_macros")
    manifest = json.loads(mod.B1_V2_MANIFEST.read_text(encoding="utf-8"))
    manifest["measurements"]["bytes_at_2_5m_ops"]["treatment_manifest_mb"] = 999.9
    tampered = tmp_path / "b1-manifest-v2-ab-2026-09.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    mod.B1_V2_MANIFEST = tampered
    m = mod.Macros()
    mod.compute_b1_v2(m)
    assert mod.FAILURES, "an edited measurements field must fail the sha256 digest check " \
        "against the manifest's own result_digest"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_osdi_b1v2_open_component_status_is_a_text_macro_not_a_number(tmp_path):
    """B1-v2's chain-open component split (checkpoint-load vs. delta-replay)
    was never measured -- NativeAdapter() exposes no internal phase timer.
    The macro documenting that must render as the literal string "not
    computed", not a placeholder number that could be mistaken for one."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_b1_v2(m)
    values = {name: value for name, value, _ in m.items}
    status = values["osdiB1v2OpenComponentStatus"]
    assert status == "not computed"
    with pytest.raises(ValueError):
        float(status)


# --------------------------------------------------------------------------
# osdi_paper_macros.py: B1-v2e (chain-format correction) macros
# --------------------------------------------------------------------------

def test_b1_v2_treatment_provenance_is_voided_without_changing_values(tmp_path):
    """The 2026-09-15 correction found the v2 A/B's commit-cost treatment
    reps measured a format-2 chain under the format-3 binary. The frozen
    v2 macro *values* must not move (they are what was actually measured,
    including the mistake) -- only their provenance strings gain a void
    notice pointing at osdiB1v2e*, and every other v2 macro (chain-open,
    B1(a) bytes, the K-sweep manifest-decile-only figures) is untouched."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_b1_v2(m)
    before = {name: value for name, value, _ in m.items}
    mod._void_b1_v2_treatment_provenance(m)
    after = {name: (value, provenance) for name, value, provenance in m.items}

    voided = {
        "osdiB1v2TotalDecileTreatment", "osdiB1v2ManifestDecileTreatment",
        "osdiB1v2ManifestDecileK128", "osdiB1v2ManifestDecileK1024",
        "osdiB1v2P50TreatmentMs", "osdiB1v2P50Paired",
    }
    for name in voided:
        value, provenance = after[name]
        assert value == before[name], f"{name}: value must not change"
        assert "VOID AS A FORMAT-3 MEASUREMENT" in provenance
        assert "osdiB1v2e" in provenance

    untouched = {
        "osdiB1v2ControlCommit", "osdiB1v2TreatmentCommit",
        "osdiB1v2BytesControlMB", "osdiB1v2BytesTreatmentMB",
        "osdiB1v2TotalDecileControl", "osdiB1v2ManifestDecileControl",
        "osdiB1v2P50ControlMs", "osdiB1v2OpenControlMs", "osdiB1v2OpenTreatmentMs",
    }
    for name in untouched:
        value, provenance = after[name]
        assert value == before[name]
        assert "VOID" not in provenance


def test_tampered_b1_v2e_manifest_digest_mismatch_fails(tmp_path):
    """Unlike B1-v2's own manifest (sha256 of its *own* `measurements`
    block), the v2e record's `result_digest` is the sha256 of the raw
    records *file's bytes* -- editing the raw file without recomputing that
    digest into the summary manifest must be caught before any B1-v2e
    macro trusts a number out of it."""
    mod = _load("osdi_paper_macros")
    raw = json.loads(mod.B1_V2E_RAW.read_text(encoding="utf-8"))
    raw["cell_b_commitcost"]["treatment_summary"]["phase_p50_us_total_us_median"] = 9999
    tampered_raw = tmp_path / "b1-manifest-v2e-remeasure-2026-09-raw.json"
    tampered_raw.write_text(json.dumps(raw), encoding="utf-8")

    mod.B1_V2E_RAW = tampered_raw
    m = mod.Macros()
    mod.compute_b1_v2e(m)
    assert mod.FAILURES, "an edited raw record must fail the sha256 digest check " \
        "against the summary manifest's own result_digest"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_b1_v2e_raw_manifest_bytes_fails_even_with_a_patched_digest(tmp_path):
    """Same discipline as the D160 rows-digest tests: patch `result_digest`
    to match a tampered raw file so the digest check alone would pass, and
    confirm the format-evidence macros (`osdiB1v2eManifestBytesTreatment`/
    `Control`) are still caught -- because they are recomputed per rep and
    cross-checked for internal agreement, not read off the manifest's own
    summary fields."""
    mod = _load("osdi_paper_macros")
    raw = json.loads(mod.B1_V2E_RAW.read_text(encoding="utf-8"))
    raw["cell_b_commitcost"]["treatment_reps_full"][0]["first_decile_us"]["manifest_bytes"] = 1565
    tampered_bytes = json.dumps(raw).encode("utf-8")
    tampered_raw = tmp_path / "b1-manifest-v2e-remeasure-2026-09-raw.json"
    tampered_raw.write_bytes(tampered_bytes)

    manifest = json.loads(mod.B1_V2E_MANIFEST.read_text(encoding="utf-8"))
    manifest["result_digest"] = hashlib.sha256(tampered_bytes).hexdigest()
    tampered_manifest = tmp_path / "b1-manifest-v2e-remeasure-2026-09.json"
    tampered_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    mod.B1_V2E_RAW = tampered_raw
    mod.B1_V2E_MANIFEST = tampered_manifest
    m = mod.Macros()
    mod.compute_b1_v2e(m)
    assert mod.FAILURES, "a rep whose manifest_bytes disagrees with the other two " \
        "must fail the format-evidence constancy check even with a self-consistent digest"
    assert any("1{,}592" in f or "1592" in f or "manifest_bytes" in f.lower()
               for f in mod.FAILURES)


def test_osdi_b1v2e_control_open_status_is_a_text_macro_not_a_number():
    """Cell (a)'s control open time was measured under a concurrent phase-2
    backup transfer and is flagged confounded, not a comparable number --
    the macro must render as that literal text, never a placeholder ratio."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_b1_v2e(m)
    values = {name: value for name, value, _ in m.items}
    status = values["osdiB1v2eControlOpenStatus"]
    assert status == "confounded (concurrent backup transfer)"
    with pytest.raises(ValueError):
        float(status)


def test_osdi_b1v2e_p50_macros_distinguish_engine_commit_from_wall_clock(tmp_path):
    """`osdiB1v2eP50*` is the frozen Addendum 3 quantity -- engine-commit
    p50 (`phase_p50_us.total_us`), the same one `osdiB1v2P50*` already
    tracks -- and `osdiB1v2eWallP50*`/`osdiB1v2eWallPaired` is the
    wall-clock `commit_ms.p50` figure (incl. Python-side eventlog append),
    which is *not* the frozen quantity. The two must never collapse to the
    same value or this split has lost its point."""
    mod = _load("osdi_paper_macros")
    raw = json.loads(mod.B1_V2E_RAW.read_text(encoding="utf-8"))
    cb = raw["cell_b_commitcost"]

    engine_trt = statistics.median(
        r["phase_p50_us"]["total_us"] for r in cb["treatment_reps_full"]) / 1000
    engine_ctl = statistics.median(
        r["phase_p50_us"]["total_us"] for r in cb["control_reps_full"]) / 1000
    wall_trt = statistics.median(r["commit_ms"]["p50"] for r in cb["treatment_reps_full"])
    wall_ctl = statistics.median(r["commit_ms"]["p50"] for r in cb["control_reps_full"])

    m = mod.Macros()
    mod.compute_b1_v2e(m)
    values = {name: value for name, value, _ in m.items}

    assert float(values["osdiB1v2eP50TreatmentMs"]) == pytest.approx(engine_trt, abs=0.001)
    assert float(values["osdiB1v2eP50ControlMs"]) == pytest.approx(engine_ctl, abs=0.001)
    assert float(values["osdiB1v2eWallP50TreatmentMs"]) == pytest.approx(wall_trt, abs=0.001)
    assert float(values["osdiB1v2eWallP50ControlMs"]) == pytest.approx(wall_ctl, abs=0.001)

    # the point of the split: these must not be the same number
    assert values["osdiB1v2eP50TreatmentMs"] != values["osdiB1v2eWallP50TreatmentMs"]
    assert values["osdiB1v2eP50ControlMs"] != values["osdiB1v2eWallP50ControlMs"]
    assert values["osdiB1v2eP50Paired"] != values["osdiB1v2eWallPaired"]


def test_tampered_b1_co7_raw_digest_mismatch_fails(tmp_path):
    """Same digest discipline as B1-v2e: the co7 record's result_digest is
    the sha256 of the raw records file's own bytes. Editing the raw file
    without recomputing that digest into the summary manifest must be
    caught before any osdiB1co7* macro trusts a number out of it."""
    mod = _load("osdi_paper_macros")
    raw = json.loads(mod.B1_CO7_RAW.read_text(encoding="utf-8"))
    raw["cell_a_chain_open"]["treatment"]["open_phase_p50_us"]["dictionary_open_us"] = 1
    tampered_raw = tmp_path / "b1-manifest-co7-chain-open-2026-09-raw.json"
    tampered_raw.write_text(json.dumps(raw), encoding="utf-8")

    mod.B1_CO7_RAW = tampered_raw
    m = mod.Macros()
    mod.compute_b1_co7(m)
    assert mod.FAILURES, "an edited raw record must fail the sha256 digest check " \
        "against the summary manifest's own result_digest"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_b1_co7_raw_component_fails_even_with_a_patched_digest(tmp_path):
    """Patch result_digest to match a tampered raw file (so the digest check
    alone would pass) and confirm a precomputed aggregate figure that
    disagrees with the per-rep data underneath it is still caught -- because
    every osdiB1co7* component is recomputed as a median of the 3
    ``open_phase_us`` reps and cross-checked against the raw record's own
    ``open_phase_p50_us`` aggregate, not read off that aggregate directly.
    Only the aggregate field is edited here; the reps it should match are
    left alone, so a generator that trusted the aggregate without
    recomputing it would sail through."""
    mod = _load("osdi_paper_macros")
    raw = json.loads(mod.B1_CO7_RAW.read_text(encoding="utf-8"))
    raw["cell_a_chain_open"]["treatment"]["open_phase_p50_us"]["state_build_us"] = 1234
    tampered_bytes = json.dumps(raw).encode("utf-8")
    tampered_raw = tmp_path / "b1-manifest-co7-chain-open-2026-09-raw.json"
    tampered_raw.write_bytes(tampered_bytes)

    manifest = json.loads(mod.B1_CO7_MANIFEST.read_text(encoding="utf-8"))
    manifest["result_digest"] = hashlib.sha256(tampered_bytes).hexdigest()
    tampered_manifest = tmp_path / "b1-manifest-co7-chain-open-2026-09.json"
    tampered_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    mod.B1_CO7_RAW = tampered_raw
    mod.B1_CO7_MANIFEST = tampered_manifest
    m = mod.Macros()
    mod.compute_b1_co7(m)
    assert mod.FAILURES, "a precomputed aggregate that disagrees with the per-rep " \
        "data underneath it must fail even with a self-consistent digest"
    assert any("state_build_us" in f.lower() for f in mod.FAILURES)


def test_osdi_b1_worst_phase_open_macros_are_arithmetic_on_measured_per_delta_costs():
    """`osdiB1WorstPhaseOpenTreatmentMs`/`ControlS` are not measurements --
    they project the co7 lane's measured per-delta manifest-chain costs
    forward to K-1=511 deltas (the generation just before the next
    checkpoint reset). Recompute both independently from the emitted
    osdiB1co7* macro values and confirm the generator's own numbers agree,
    proving the projection is arithmetic on those macros and not a separately
    fabricated figure."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_b1_co7(m)
    values = {name: value for name, value, _ in m.items}

    checkpoint_ms = float(values["osdiB1co7CheckpointReadParseMs"])
    merkle_ms = float(values["osdiB1co7MerkleVerifyMs"])
    state_build_per_delta_us = float(values["osdiB1co7StateBuildPerDeltaUs"])
    delta_replay_per_delta_us = float(values["osdiB1co7DeltaReplayPerDeltaUs"])
    control_per_delta_ms = float(values["osdiB1co7ControlPerDeltaMs"])

    expected_trt_ms = (
        checkpoint_ms + merkle_ms
        + (state_build_per_delta_us + delta_replay_per_delta_us) * 511 / 1000
    )
    expected_ctl_s = control_per_delta_ms * 511 / 1000

    assert float(values["osdiB1WorstPhaseOpenTreatmentMs"]) == pytest.approx(
        expected_trt_ms, abs=0.1)
    assert float(values["osdiB1WorstPhaseOpenControlS"]) == pytest.approx(
        expected_ctl_s, abs=0.1)


def test_osdi_b1_worst_phase_open_macros_have_derived_not_measured_provenance():
    """The two projected-cost macros must carry provenance that says
    'derived, not measured' rather than pointing at a raw record field --
    a reader must not mistake this arithmetic for a fourth measurement."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_b1_co7(m)
    provenance = {name: prov for name, _, prov in m.items}
    for name in ("osdiB1WorstPhaseOpenTreatmentMs", "osdiB1WorstPhaseOpenControlS"):
        assert provenance[name].startswith("derived, not measured"), (
            f"{name}: provenance must flag this as arithmetic, not a measurement, "
            f"got {provenance[name]!r}")


def test_tampered_fault_matrix_record_fails_the_frozen_expectation(tmp_path):
    """Edit F1-9's pre-fix silent-violation count (the campaign's own
    headline finding) and confirm the frozen-value assertion, not just a
    cross-file consistency check, is what catches it."""
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "fault-matrix-campaign-2026-09-13.json"
    data = json.loads(mod.FAULTS_PRE.read_text(encoding="utf-8"))
    for cell in data["per_cell_gate_table"]:
        if cell["cell"] == "F1-9" and not cell["strict_gate"]:
            # Move one trial from silent-violation to correct -- keeps
            # n_cases and the row's own internal partition consistent, so
            # only the frozen-value check (271) can catch this, not a
            # same-file partition check.
            cell["counts"]["silent-violation"] -= 1
            cell["counts"]["correct"] += 1
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.FAULTS_PRE = tampered
    m = mod.Macros()
    mod.compute_c8(m)
    assert mod.FAILURES, "an edited F1-9 count must fail the frozen expectation"
    assert any("271" in f or "frozen" in f for f in mod.FAILURES)


def test_tampered_m5_carve_record_fails_when_a_false_fresh_is_introduced(tmp_path):
    """The soundness axis's central number (0 false-fresh in 28,044 carve-2
    trials) must fail if even one row disagrees."""
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "topup-carve2-synth-iv-60k.json"
    data = json.loads(mod.M5_CARVE_TWO.read_text(encoding="utf-8"))
    injected = False
    for row in data["rows"]:
        if row.get("changed"):
            row["verdict"] = "fresh"  # inject one false-fresh
            injected = True
            break
    assert injected, "fixture must contain at least one changed row to tamper"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.M5_CARVE_TWO = tampered
    m = mod.Macros()
    mod.compute_c6(m)
    assert mod.FAILURES, "an injected false-fresh row must not pass silently"
    assert any("false-fresh" in f.lower() for f in mod.FAILURES)


def test_tampered_dag_v2_record_fails_the_extra_visits_constancy_assertion(tmp_path):
    """v2's nodes_visited delta vs v1 must be the same (+61 or +62, by seed)
    in every one of the 20 same-seed cells; a single outlier cell must be
    caught by the constancy assertion, not averaged away."""
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "storm-campaign-dag-v2-2026-09.json"
    data = json.loads(mod.DAG_V2.read_text(encoding="utf-8"))
    for cell in data["per_cell"]:
        if cell["seed"] == 0:
            cell["nodes_visited"] += 1  # breaks the uniform +61 for exactly one seed-0 cell
            break
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.DAG_V2 = tampered
    m = mod.Macros()
    mod.compute_c7_dag(m)
    assert mod.FAILURES, "a non-uniform nodes_visited delta must not pass silently"
    assert any("not constant" in f for f in mod.FAILURES)


def test_tampered_dag_v1_record_fails_the_false_safe_per_cell_frozen_value(tmp_path):
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "storm-campaign-dag-2026-09.json"
    data = json.loads(mod.DAG_V1.read_text(encoding="utf-8"))
    for cell in data["per_cell"]:
        if cell["false_safe_count"] > 0:
            cell["false_safe_count"] = 4  # every affected cell must read exactly 3
            break
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.DAG_V1 = tampered
    m = mod.Macros()
    mod.compute_c7_dag(m)
    assert mod.FAILURES, "an edited false_safe_count must fail the uniform-3 assertion"


def test_tampered_r18_probe_record_fails_the_frozen_speedup(tmp_path):
    mod = _load("osdi_paper_macros")
    tampered = tmp_path / "storm-r18-probe-2026-09-rows.jsonl"
    rows = [json.loads(line) for line in mod.R18_PROBE_ROWS.read_text(encoding="utf-8")
            .splitlines() if line.strip()]
    for row in rows:
        if row["batch_index"] == 1:
            row["arms"]["tgms-L1"]["ttf_ms"] = 1.0  # implausibly fast, breaks the frozen ratio
    tampered.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    mod.R18_PROBE_ROWS = tampered
    m = mod.Macros()
    mod.compute_c7_r18(m)
    assert mod.FAILURES, "a tampered ttf_ms must fail the cross-check against the record's " \
        "own summary.arms field and/or the frozen speedup range"


def test_tampered_storm_v1_records_tarball_sha_mismatch_fails(tmp_path):
    """Lane W2m: storm-v1-records-36-tasks.tar.gz holds the per-batch rows
    the row-touch/entity-touch/window-overlap false-fresh medians (and the
    new-identity-batch/P4 quantities) are computed from, never committed as
    individual files. A single flipped byte in the tarball must fail the
    frozen/README-quoted sha256 check before anything inside it is trusted
    -- the same house rule already applied to the storm-v2 tarball above
    and to every other whole-file digest in this module."""
    mod = _load("osdi_paper_macros")
    original = mod.STORM_V1_RECORDS_TARBALL.read_bytes()
    tampered_bytes = bytearray(original)
    tampered_bytes[-1] ^= 0xFF  # flip the last byte -- still a well-formed gzip trailer byte
    tampered = tmp_path / "storm-v1-records-36-tasks.tar.gz"
    tampered.write_bytes(bytes(tampered_bytes))
    assert tampered.read_bytes() != original

    mod.STORM_V1_RECORDS_TARBALL = tampered
    m = mod.Macros()
    mod.compute_c7_storm_v1(m)
    assert mod.FAILURES, "a tampered tarball byte must fail the sha256 check"
    assert any("sha256" in f.lower() or "records tarball" in f.lower() for f in mod.FAILURES)


def test_tampered_storm_v2_probe_record_digest_mismatch_fails(tmp_path):
    """storm-v2-r18-probe-2026-09-15.json's own result_digest is sha256 of
    the canonical-JSON list of every row (sorted by batch_index), per
    bench_correction_storm.py's own `_sha256_json(rows_json)`. Editing the
    record's result_digest field itself (without touching the rows.jsonl
    it is supposed to summarize) must be caught -- same digest discipline
    as the storm-v2 main grid's own two digest-tamper tests above, applied
    to this probe's single-record digest scheme."""
    mod = _load("osdi_paper_macros")
    d = json.loads(mod.STORM_V2_R18_PROBE.read_text(encoding="utf-8"))
    d["result_digest"] = "0" * 64  # implausible digest, rows.jsonl untouched
    tampered = tmp_path / "storm-v2-r18-probe-2026-09-15.json"
    tampered.write_text(json.dumps(d), encoding="utf-8")

    mod.STORM_V2_R18_PROBE = tampered
    m = mod.Macros()
    mod.compute_c7_storm_v2_probe(m)
    assert mod.FAILURES, "an edited result_digest must fail the sha256 digest check against " \
        "the rows.jsonl it claims to summarize"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_storm_v2_probe_rows_digest_mismatch_fails(tmp_path):
    """The inverse of the above: editing a row's own field (without
    touching the top-level record's result_digest) must also be caught --
    the digest is recomputed fresh from the rows.jsonl every time, not
    trusted from a value cached anywhere."""
    mod = _load("osdi_paper_macros")
    rows = [json.loads(line) for line in mod.STORM_V2_R18_PROBE_ROWS.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    rows[0]["intersects_calls"] = 999999
    tampered = tmp_path / "storm-v2-r18-probe-2026-09-15-rows.jsonl"
    tampered.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    mod.STORM_V2_R18_PROBE_ROWS = tampered
    m = mod.Macros()
    mod.compute_c7_storm_v2_probe(m)
    assert mod.FAILURES, "an edited row must fail the record's own result_digest check"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_storm_v2_probe_ttf_fails_the_frozen_speedup(tmp_path):
    """Same discipline as test_tampered_r18_probe_record_fails_the_frozen_speedup
    above, applied to the v2 probe: tampering a row's ttf_ms (and its
    result_digest, so the digest check alone does not mask the effect)
    must still fail the cross-check against the record's own summary.arms
    field and/or the frozen speedup."""
    mod = _load("osdi_paper_macros")
    rows = [json.loads(line) for line in mod.STORM_V2_R18_PROBE_ROWS.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    # batch_index 0 is the one whose tgms-L1 ttf_ms equals the 5-batch
    # median (806,289.76...) -- tampering any other batch would not move
    # the recomputed median at all, so this must target that batch, not
    # an arbitrary one.
    for row in rows:
        if row["batch_index"] == 0:
            row["arms"]["tgms-L1"]["ttf_ms"] = 1.0  # implausibly fast, breaks the frozen ratio
    tampered_rows_path = tmp_path / "storm-v2-r18-probe-2026-09-15-rows.jsonl"
    tampered_rows_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                                   encoding="utf-8")

    d = json.loads(mod.STORM_V2_R18_PROBE.read_text(encoding="utf-8"))
    d["result_digest"] = hashlib.sha256(
        json.dumps(sorted(rows, key=lambda r: r["batch_index"]),
                   sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()  # keep the digest check itself passing so it doesn't mask the real failure
    tampered_manifest_path = tmp_path / "storm-v2-r18-probe-2026-09-15.json"
    tampered_manifest_path.write_text(json.dumps(d), encoding="utf-8")

    mod.STORM_V2_R18_PROBE = tampered_manifest_path
    mod.STORM_V2_R18_PROBE_ROWS = tampered_rows_path
    m = mod.Macros()
    mod.compute_c7_storm_v2_probe(m)
    assert mod.FAILURES, "a tampered ttf_ms must fail the cross-check against the record's " \
        "own summary.arms field and/or the frozen speedup"


def test_storm_v2_probe_survivor_and_precision_medians_are_recomputed_from_rows():
    """Arithmetic check: osdiStormV2ProbeSurvivorMedian/PrecisionMedian must
    equal the median, over the probe's own 5 batches, of
    candidate_survivors/n_registered and changed_count/candidate_survivors
    respectively -- recomputed independently here from the committed
    rows.jsonl, not merely re-asserted against the generator's own
    intermediate variables."""
    mod = _load("osdi_paper_macros")
    rows = [json.loads(line) for line in mod.STORM_V2_R18_PROBE_ROWS.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 5
    n_registered = {r["n_registered"] for r in rows}
    assert len(n_registered) == 1
    n_registered = next(iter(n_registered))
    expected_survivor_median = statistics.median(
        r["candidate_survivors"] / n_registered for r in rows)
    expected_precision_median = statistics.median(
        r["changed_count"] / r["candidate_survivors"] for r in rows)
    expected_intersects_median = statistics.median(r["intersects_calls"] for r in rows)

    m = mod.Macros()
    mod.compute_c7_storm_v2_probe(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiStormV2ProbeSurvivorMedian"] == f"{expected_survivor_median:.3f}"
    assert values["osdiStormV2ProbePrecisionMedian"] == f"{expected_precision_median:.3f}"
    assert values["osdiStormV2ProbeIntersectsMedian"] == mod.tex_num(
        int(expected_intersects_median))
    assert expected_intersects_median <= 50000
    assert values["osdiStormV2ProbeR18Tripped"] == "no"


def test_tampered_storm_v2_merged_record_digest_mismatch_fails(tmp_path):
    """storm-v2-main-grid-2026-09-15.json's own result_digest is sha256 of
    every row's own (_task_id, result_digest), sorted by task id
    (storm_campaign_merge.py's result_digest()). Editing the merged
    record's result_digest field itself (without touching the rows.jsonl
    it is supposed to summarize) must be caught -- same digest discipline
    as the D160/B1-v2e/ladder tamper tests above, applied to this record's
    own digest scheme."""
    mod = _load("osdi_paper_macros")
    merged = json.loads(mod.STORM_V2_MAIN_GRID.read_text(encoding="utf-8"))
    merged["result_digest"] = "0" * 64  # implausible digest, rows.jsonl untouched
    tampered = tmp_path / "storm-v2-main-grid-2026-09-15.json"
    tampered.write_text(json.dumps(merged), encoding="utf-8")

    mod.STORM_V2_MAIN_GRID = tampered
    m = mod.Macros()
    mod.compute_c7_storm_v2(m)
    assert mod.FAILURES, "an edited result_digest must fail the sha256 digest check against " \
        "the rows.jsonl it claims to summarize"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_storm_v2_rows_digest_mismatch_fails(tmp_path):
    """The inverse of the above: editing a row's own embedded result_digest
    (without touching the merged record) must also be caught -- the merged
    record's result_digest is a chain over every row's own claimed digest,
    not a value copied once and never re-verified."""
    mod = _load("osdi_paper_macros")
    rows = [json.loads(line) for line in mod.STORM_V2_MAIN_GRID_ROWS.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    rows[0]["result_digest"] = "1" * 64
    tampered = tmp_path / "storm-v2-main-grid-2026-09-15-rows.jsonl"
    tampered.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    mod.STORM_V2_MAIN_GRID_ROWS = tampered
    m = mod.Macros()
    mod.compute_c7_storm_v2(m)
    assert mod.FAILURES, "an edited row result_digest must fail the merged record's own " \
        "result_digest check"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_storm_v2_speedup_synth_c1_none_is_the_median_of_its_three_seeds():
    """Arithmetic check on one of the 12 per-(store,mix,age) macros:
    osdiStormV2SpeedupSynthC1None must equal the median, over exactly the
    three seed-0/1/2 cells at (synth-iv-60k, c1, age=none), of
    summary.arms.global-recompute.ttf_p50_ms / summary.arms.tgms-L1.ttf_p50_ms
    -- recomputed independently here from the committed rows.jsonl, not
    trusted from the generator's own arithmetic."""
    mod = _load("osdi_paper_macros")
    rows = [json.loads(line) for line in mod.STORM_V2_MAIN_GRID_ROWS.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    cells = [r for r in rows if r["config"]["store"] == "synth-iv-60k"
             and r["config"]["mix"] == "c1" and r["config"]["age"] is None]
    assert sorted(c["config"]["seed"] for c in cells) == [0, 1, 2]
    speedups = [c["summary"]["arms"]["global-recompute"]["ttf_p50_ms"]
                / c["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"] for c in cells]
    expected_median = statistics.median(speedups)

    m = mod.Macros()
    mod.compute_c7_storm_v2(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiStormV2SpeedupSynthC1None"] == f"{expected_median:.3f}"


def test_tampered_storm_v2_records_tarball_sha_mismatch_fails(tmp_path):
    """Lane W2l: storm-v2-records-36-tasks.tar.gz holds the per-batch rows
    the c1 survivor-fraction/precision macros are computed from, never
    committed as individual files. A single flipped byte in the tarball
    must fail the frozen/README-quoted sha256 check before anything inside
    it is trusted, the same house rule already applied to every other
    whole-file digest in this module."""
    mod = _load("osdi_paper_macros")
    original = mod.STORM_V2_RECORDS_TARBALL.read_bytes()
    tampered_bytes = bytearray(original)
    tampered_bytes[-1] ^= 0xFF  # flip the last byte -- still a well-formed gzip trailer byte
    tampered = tmp_path / "storm-v2-records-36-tasks.tar.gz"
    tampered.write_bytes(bytes(tampered_bytes))
    assert tampered.read_bytes() != original

    mod.STORM_V2_RECORDS_TARBALL = tampered
    m = mod.Macros()
    mod.compute_c7_storm_v2(m)
    assert mod.FAILURES, "a tampered tarball byte must fail the sha256 check"
    assert any("sha256" in f.lower() or "records tarball" in f.lower() for f in mod.FAILURES)


def test_storm_v2_c1_survivor_fraction_and_precision_are_medians_over_240_batches():
    """Arithmetic check, recomputed independently here (not trusted from
    the generator's own arithmetic): the overall and per-store c1
    survivor-fraction/precision medians must equal
    statistics.median(candidate_survivors / config.n_registered) and
    statistics.median(changed_count / candidate_survivors) over the c1-mix
    cells' own per-batch rows inside storm-v2-records-36-tasks.tar.gz,
    located via each cell's own `record` field, matching the generator's
    own -- not just its stated -- values."""
    mod = _load("osdi_paper_macros")
    merged_rows = [json.loads(line) for line in mod.STORM_V2_MAIN_GRID_ROWS.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    c1_rows = [r for r in merged_rows if r["config"]["mix"] == "c1"]
    assert len(c1_rows) == 12

    survivor_fracs: dict[str, list[float]] = {"synth-iv-60k": [], "collegemsg": []}
    precisions: dict[str, list[float]] = {"synth-iv-60k": [], "collegemsg": []}
    with mod.tarfile.open(mod.STORM_V2_RECORDS_TARBALL, "r:gz") as tf:
        names = set(tf.getnames())
        for r in c1_rows:
            idx = r["record"].index("records/")
            member = r["record"][idx:]
            assert member in names
            raw = tf.extractfile(member).read().decode("utf-8")
            batch_rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            assert len(batch_rows) == 20
            n_registered = r["config"]["n_registered"]
            store = r["config"]["store"]
            for br in batch_rows:
                survivor_fracs[store].append(br["candidate_survivors"] / n_registered)
                precisions[store].append(br["changed_count"] / br["candidate_survivors"])

    all_survivor = survivor_fracs["synth-iv-60k"] + survivor_fracs["collegemsg"]
    all_precision = precisions["synth-iv-60k"] + precisions["collegemsg"]
    assert len(all_survivor) == 240 and len(all_precision) == 240
    expected_survivor_median = statistics.median(all_survivor)
    expected_precision_median = statistics.median(all_precision)
    expected_synth_survivor = statistics.median(survivor_fracs["synth-iv-60k"])
    expected_collegemsg_survivor = statistics.median(survivor_fracs["collegemsg"])
    expected_synth_precision = statistics.median(precisions["synth-iv-60k"])
    expected_collegemsg_precision = statistics.median(precisions["collegemsg"])

    m = mod.Macros()
    mod.compute_c7_storm_v2(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiStormV2C1Batches"] == "240"
    assert values["osdiStormV2SurvivorFractionC1Median"] == f"{expected_survivor_median:.3f}"
    assert values["osdiStormV2PrecisionC1Median"] == f"{expected_precision_median:.3f}"
    assert (values["osdiStormV2SurvivorFractionSynthC1Median"]
            == f"{expected_synth_survivor:.3f}")
    assert (values["osdiStormV2SurvivorFractionCollegeMsgC1Median"]
            == f"{expected_collegemsg_survivor:.3f}")
    assert values["osdiStormV2PrecisionSynthC1Median"] == f"{expected_synth_precision:.3f}"
    assert (values["osdiStormV2PrecisionCollegeMsgC1Median"]
            == f"{expected_collegemsg_precision:.3f}")


def test_tampered_d160_rows_digest_mismatch_fails(tmp_path):
    """The record's own manifest carries a sha256 of the rows file; editing
    the rows without updating the manifest must be caught before any metric
    is even computed."""
    mod = _load("osdi_paper_macros")
    rows = json.loads(mod.D160_ROWS.read_text(encoding="utf-8"))
    for r in rows:
        if r["system"] == "ours":
            r["em"] = 1.0 if r["em"] == 0.0 else 0.0
            break
    tampered = tmp_path / "rows-2026-09-14.json"
    tampered.write_text(json.dumps(rows), encoding="utf-8")

    mod.D160_ROWS = tampered
    m = mod.Macros()
    mod.compute_d160(m)
    assert mod.FAILURES, "an edited rows file must fail the sha256 digest check against " \
        "the manifest's result_digest"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_d160_rows_recomputed_coverage_fails_even_with_a_patched_digest(tmp_path):
    """Patch both the rows file and the manifest's result_digest so the
    digest check alone would pass -- proving the frozen carrying-count/
    coverage assertion, not just the digest check, is what would catch a
    doctored record."""
    mod = _load("osdi_paper_macros")
    rows = json.loads(mod.D160_ROWS.read_text(encoding="utf-8"))
    changed = False
    for r in rows:
        if r["system"] == "ours" and not (r.get("answer_object") or {}).get("claims"):
            r["answer_object"] = {
                "claims": [{"id": "c1", "type": "count", "value": 0, "evidence": ["s1"]}],
                "text": "tampered",
            }
            r["em"] = 0.0
            r["ucr"] = 0.0
            changed = True
            break
    assert changed, "fixture must contain a non-carrying ours row to tamper"

    tampered_bytes = json.dumps(rows).encode("utf-8")
    tampered_rows = tmp_path / "rows-2026-09-14.json"
    tampered_rows.write_bytes(tampered_bytes)

    manifest = json.loads(mod.D160_MANIFEST.read_text(encoding="utf-8"))
    manifest["result_digest"] = hashlib.sha256(tampered_bytes).hexdigest()
    tampered_manifest = tmp_path / "manifest-2026-09-14.json"
    tampered_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    mod.D160_ROWS = tampered_rows
    mod.D160_MANIFEST = tampered_manifest
    m = mod.Macros()
    mod.compute_d160(m)
    assert mod.FAILURES, "an extra claim-carrying row must fail the frozen carrying-count/" \
        "coverage assertion, even though the digest was patched to match"
    assert any("carrying" in f.lower() or "coverage" in f.lower() for f in mod.FAILURES)


def test_tampered_d160_llm_direct_fix_rows_digest_mismatch_fails(tmp_path):
    """Same discipline as test_tampered_d160_rows_digest_mismatch_fails, for
    the llm_direct follow-up record: editing rows-llm-direct-fix-2026-09-14.json
    without updating its manifest's result_digest must be caught before any
    of the fixed-record macros (coverage/errors/raw-em/tokenizer/budget) are
    even computed."""
    mod = _load("osdi_paper_macros")
    rows = json.loads(mod.D160_ROWS_FIX.read_text(encoding="utf-8"))
    for r in rows:
        if r["system"] == "llm_direct":
            r["meta"]["tokenizer_kind"] = "whitespace_approx"
            break
    tampered = tmp_path / "rows-llm-direct-fix-2026-09-14.json"
    tampered.write_text(json.dumps(rows), encoding="utf-8")

    mod.D160_ROWS_FIX = tampered
    m = mod.Macros()
    mod.compute_d160_llm_direct_fix(m)
    assert mod.FAILURES, "an edited llm_direct-fix rows file must fail the sha256 digest " \
        "check against its manifest's result_digest"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_corruption_pre_record_digest_mismatch_fails(tmp_path):
    """The corruption record's own result_digest is a sha256 over the sorted
    per-trial results (corruption_campaign_merge.py's scheme); editing a
    trial's verdict without recomputing that digest must be caught before
    any count is even trusted."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.CORRUPTION_PRE.read_text(encoding="utf-8"))
    for r in data["results"]:
        if r["class"] == "artifact_blob" and r["mutation"] == "append_garbage":
            r["verdict"] = "DETECTED"
            break
    tampered = tmp_path / "eval-corruption-campaign-2026-09-14.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.CORRUPTION_PRE = tampered
    m = mod.Macros()
    mod.compute_c2(m)
    assert mod.FAILURES, "an edited verdict must fail the result_digest check"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_corruption_post_record_blob_count_fails_even_with_patched_digest(tmp_path):
    """Patch both the results and result_digest so the digest check alone
    would pass -- proving the frozen 621-of-621 blob-fix assertion, not
    just the digest, is what would catch a doctored post-A10 record."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.CORRUPTION_POST.read_text(encoding="utf-8"))
    changed = False
    for r in data["results"]:
        if r["class"] == "artifact_blob" and r["mutation"] == "flip_bit" and r["verdict"] == "DETECTED":
            r["verdict"] = "BENIGN"
            changed = True
            break
    assert changed, "fixture must contain a DETECTED artifact_blob|flip_bit trial to tamper"

    canon = sorted(data["results"],
                    key=lambda r: (r["class"], r["mutation"], r.get("task_id", -1), r["trial"]))
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    data["result_digest"] = hashlib.sha256(blob).hexdigest()
    tampered = tmp_path / "eval-corruption-campaign-2026-09-14-post-a10.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.CORRUPTION_POST = tampered
    m = mod.Macros()
    mod.compute_c2(m)
    assert mod.FAILURES, "a de-detected artifact_blob trial must fail the frozen 621-of-621 " \
        "post-A10 assertion, even though the digest was patched to match"


def test_tampered_corruption_pre_record_breaks_the_already_detected_partition(tmp_path):
    """osdiCorruptionBlobMutationAlreadyDetected's own trial count must equal
    its DETECTED count exactly (every one of delete_file's 89 pre-A10 trials
    was DETECTED) for the six-mutation-plus-one partition of the 710 blob
    trials to hold; a single trial flipped away from DETECTED must be
    caught, not averaged into the 89 count as noise."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.CORRUPTION_PRE.read_text(encoding="utf-8"))
    changed = False
    for r in data["results"]:
        if r["class"] == "artifact_blob" and r["mutation"] == "delete_file" \
                and r["verdict"] == "DETECTED":
            r["verdict"] = "BENIGN"
            changed = True
            break
    assert changed, "fixture must contain a DETECTED artifact_blob|delete_file trial to tamper"

    canon = sorted(data["results"],
                    key=lambda r: (r["class"], r["mutation"], r.get("task_id", -1), r["trial"]))
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    data["result_digest"] = hashlib.sha256(blob).hexdigest()
    tampered = tmp_path / "eval-corruption-campaign-2026-09-14.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.CORRUPTION_PRE = tampered
    m = mod.Macros()
    mod.compute_c2(m)
    assert mod.FAILURES, "an un-DETECTED delete_file trial must fail the already-detected " \
        "mutation's own every-trial-DETECTED assertion, even though the digest was patched " \
        "to match"
    assert any("every one of its own pre-A10 trials" in f for f in mod.FAILURES)


def test_tampered_ladder_raw_record_digest_mismatch_fails(tmp_path):
    """Each per-seed raw ladder record's result_digest is sha256 over its
    own rows; editing a row without recomputing that digest must be caught
    before any rung's median is trusted."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LADDER_RAW[0].read_text(encoding="utf-8"))
    for row in data["rows"]:
        if row["rung"] == 4 and row["plan_id"] == "p01-entity-history":
            row["p50_ms"] = 999.0
            break
    tampered = tmp_path / "overhead-ladder-bitcoinotc-seed0-job212303.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LADDER_RAW = [tampered, mod.LADDER_RAW[1], mod.LADDER_RAW[2]]
    m = mod.Macros()
    mod.compute_ladder(m)
    assert mod.FAILURES, "an edited raw row must fail the result_digest check"
    assert any("digest" in f.lower() for f in mod.FAILURES)


def test_tampered_ladder_merged_summary_fails_the_frozen_rung3_value(tmp_path):
    """Patch the merged record's summary and its own result_digest together
    (so the digest check alone would pass) and confirm the frozen
    bytes-median assertion catches a doctored trace_bytes number."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LADDER_MERGED.read_text(encoding="utf-8"))
    data["summary"]["rung3_trace_bytes"]["p01-entity-history"]["bytes_median"] = 9999

    blob = json.dumps(data["summary"], sort_keys=True, separators=(",", ":")).encode()
    data["result_digest"] = hashlib.sha256(blob).hexdigest()
    tampered = tmp_path / "ladder-2026-09-14.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LADDER_MERGED = tampered
    m = mod.Macros()
    mod.compute_ladder(m)
    assert mod.FAILURES, "a doctored rung-3 bytes_median must fail the raw-vs-summary " \
        "cross-check, even though the digest was patched to match"


def test_tampered_longevity_manifest_sha256_mismatch_fails(tmp_path):
    """Unlike B1-v2/D160/ladder's own embedded `result_digest` field (a
    digest of that record's internal content, recomputable locally), the
    soak manifest's `result_digest` is the *final store's* content digest --
    a 20 GB store that stays on xzgpu and is never checked into this repo,
    so there is nothing local to recompute it from. The verifiable digest
    here is the whole-file sha256 benchmarks/longevity-v1/README.md's own
    "Files here" table states for the byte-copied record; editing any field
    (even one this generator never reads) must be caught by that check
    before a single macro is computed."""
    mod = _load("osdi_paper_macros")
    manifest = json.loads(mod.LONGEVITY_MANIFEST.read_text(encoding="utf-8"))
    manifest["summary"]["compactions"] = 999999
    tampered = tmp_path / "longevity-synth-1m-native-0.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    mod.LONGEVITY_MANIFEST = tampered
    m = mod.Macros()
    mod.compute_longevity_soak(m)
    assert mod.FAILURES, "an edited manifest field must fail the sha256 check against " \
        "README.md's Files-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_side_file_sha256_mismatch_fails(tmp_path):
    """Same discipline as the manifest check, applied to one of the four
    other README-hash-verified side-files (recoveries.jsonl here) -- an
    edited copy must fail before the recovery-cause counts are trusted."""
    mod = _load("osdi_paper_macros")
    rows = [json.loads(line) for line in
            mod.LONGEVITY_RECOVERIES.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["returncode"] = 137  # flip one SIGABRT into an exit137, still 41 rows total
    tampered = tmp_path / "recoveries.jsonl"
    tampered.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    mod.LONGEVITY_RECOVERIES = tampered
    m = mod.Macros()
    mod.compute_longevity_soak(m)
    assert mod.FAILURES, "an edited recoveries.jsonl must fail the sha256 check against " \
        "README.md's Files-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_longevity_true_error_sum_is_249_over_42_lives():
    """The headline correction of the README's documented harness defect:
    the manifest's own summary.error_count (1) is only the last of 42
    writer lives (a counter_latest label-collision bug); the true total,
    recomputed here by summing writer_error_counts_by_life.json's own
    per-life rows -- never trusted from that file's own
    true_total_errors_all_lives field without the sum matching it -- is
    249."""
    mod = _load("osdi_paper_macros")
    by_life = json.loads(mod.LONGEVITY_WRITER_ERRORS_BY_LIFE.read_text(encoding="utf-8"))
    per_life_errors = by_life["per_life_errors"]
    assert len(per_life_errors) == 42
    assert sum(per_life_errors) == 249
    assert sum(per_life_errors) == by_life["true_total_errors_all_lives"]

    m = mod.Macros()
    mod.compute_longevity_soak(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiSoakWriterErrorsTrue"] == "249"
    assert values["osdiSoakWriterErrorsManifest"] == "1"


def test_tampered_writer_error_counts_by_life_sum_mismatch_fails(tmp_path):
    """writer_error_counts_by_life.json is the README's own "derived
    locally" file (not in the sha256 table) -- its per-life sum must still
    be recomputed and cross-checked against its own summary field and the
    frozen 249, not trusted verbatim; editing one life's count must be
    caught even though no digest scheme covers this file at all."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LONGEVITY_WRITER_ERRORS_BY_LIFE.read_text(encoding="utf-8"))
    data["per_life_errors"][0] += 1  # sum no longer matches the file's own summary field
    tampered = tmp_path / "writer_error_counts_by_life.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_WRITER_ERRORS_BY_LIFE = tampered
    m = mod.Macros()
    mod.compute_longevity_soak(m)
    assert mod.FAILURES, "a tampered per-life error count must fail the recomputed-sum " \
        "cross-check against the file's own true_total_errors_all_lives field"
    assert any("true_total_errors_all_lives" in f for f in mod.FAILURES)


def test_tampered_longevity_manifest_error_count_defect_check_fails_if_fixed(tmp_path):
    """The whole point of osdiSoakWriterErrorsManifest/True is that they
    *disagree* (1 vs. 249) -- that disagreement is the harness defect the
    README documents. If a future manifest ever reported the true total
    directly (i.e. the defect were fixed upstream), this generator's own
    "they must differ" sanity check should catch the now-stale assumption
    rather than silently emitting two identical numbers as if nothing
    changed."""
    mod = _load("osdi_paper_macros")
    manifest = json.loads(mod.LONGEVITY_MANIFEST.read_text(encoding="utf-8"))
    manifest["summary"]["error_count"] = 249
    manifest["summary"]["writer_final"]["errors"] = 249
    tampered = tmp_path / "longevity-synth-1m-native-0.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    mod.LONGEVITY_MANIFEST = tampered
    m = mod.Macros()
    mod.compute_longevity_soak(m)
    assert mod.FAILURES, "a manifest whose error_count now matches the true total must " \
        "fail this generator's own defect-still-present sanity check"


def test_longevity_digest_and_reader_death_cause_are_text_macros_not_numbers():
    """osdiSoakDigestStatus ('not computed') and osdiSoakReaderDeathCause
    ('reader torn-tail race, pre-fix engine') must render as literal text,
    never a placeholder number that could be mistaken for one -- same
    discipline as osdiB1v2OpenComponentStatus above. osdiSoakVerifyHealthy
    is also text ('true'), not the LaTeX-truthy '1'."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak(m)
    values = {name: value for name, value, _ in m.items}

    for name in ("osdiSoakDigestStatus", "osdiSoakReaderDeathCause", "osdiSoakVerifyHealthy"):
        with pytest.raises(ValueError):
            float(values[name])

    assert values["osdiSoakDigestStatus"] == "not computed"
    assert values["osdiSoakReaderDeathCause"] == "reader torn-tail race, pre-fix engine"
    assert values["osdiSoakVerifyHealthy"] == "true"


def test_tampered_longevity_summary_rederived_sha256_mismatch_fails(tmp_path):
    """summary_rederived_2026-09-15.json is not in README.md's original
    "Files here" sha256 table (that table is the original soak's five
    files only) -- this generator freezes its own whole-file sha256 from
    its first read of the committed copy instead. An edited copy (even a
    field this generator never reads) must fail that check before any
    within-life slope statistic is trusted."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LONGEVITY_SUMMARY_REDERIVED.read_text(encoding="utf-8"))
    data["summary"]["compactions"] = 999999
    tampered = tmp_path / "summary_rederived_2026-09-15.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_SUMMARY_REDERIVED = tampered
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    assert mod.FAILURES, "an edited summary_rederived_2026-09-15.json must fail this " \
        "generator's own frozen sha256 check"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_summary_rederived_provenance_mismatch_fails(tmp_path):
    """The re-derivation's own derived_from.original_manifest_sha256 field
    must equal the *actual* sha256 of longevity-synth-1m-native-0.json --
    proof this re-derivation ran against the same soak, not a different
    one. Patching the digest first (so the whole-file check above would
    not itself catch it) and only then editing derived_from must still
    fail."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LONGEVITY_SUMMARY_REDERIVED.read_text(encoding="utf-8"))
    data["derived_from"]["original_manifest_sha256"] = "0" * 64
    tampered = tmp_path / "summary_rederived_2026-09-15.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    # patch the frozen sha256 constant to match the tampered file's own
    # digest, isolating this test to the provenance cross-check alone
    import hashlib as _hashlib
    mod.LONGEVITY_SUMMARY_REDERIVED_SHA256 = _hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_SUMMARY_REDERIVED = tampered
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    assert mod.FAILURES, "a derived_from.original_manifest_sha256 that no longer matches " \
        "the real manifest's sha256 must fail, even with a patched whole-file digest"
    assert any("original_manifest_sha256" in f for f in mod.FAILURES)


def test_tampered_longevity_replay_check_sha256_mismatch_fails(tmp_path):
    """replay-check-2026-09-15.json's sha256 *is* independently recorded
    (README.md's "Post-hoc replay check" section quotes it verbatim) --
    an edited copy must fail before any OOM/generation/timing figure is
    trusted."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LONGEVITY_REPLAY_CHECK.read_text(encoding="utf-8"))
    data["summary"]["attempt_1"]["wall_s"] = 1.0
    tampered = tmp_path / "replay-check-2026-09-15.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_REPLAY_CHECK = tampered
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    assert mod.FAILURES, "an edited replay-check-2026-09-15.json must fail the sha256 " \
        "check against README.md's own quoted value"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_replay_check_generation_arithmetic_fails_even_with_patched_digest(tmp_path):
    """highest_manifest_generation_observed must equal
    compactions_inferred * 501 (500 batch-commit generations + 1
    compact()-commit generation per cycle, per the file's own
    compaction_inference_basis text) -- editing the generation alone,
    with the whole-file digest patched to match, must still fail this
    internal arithmetic cross-check rather than silently emitting a wrong
    osdiSoakReplayOomGeneration."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LONGEVITY_REPLAY_CHECK.read_text(encoding="utf-8"))
    data["summary"]["attempt_1"]["highest_manifest_generation_observed"] = 513025
    tampered = tmp_path / "replay-check-2026-09-15.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    mod.LONGEVITY_REPLAY_CHECK_SHA256 = _hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_REPLAY_CHECK = tampered
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    assert mod.FAILURES, "a generation count that no longer factors as " \
        "compactions_inferred * 501 must fail, even with a patched whole-file digest"
    assert any("501" in f for f in mod.FAILURES)


def test_longevity_rederived_replay_check_sha256_matches_readme_quoted_value():
    """README.md's "Post-hoc replay check" section quotes
    replay-check-2026-09-15.json's sha256 verbatim
    ("Files: replay-check-2026-09-15.json (sha256 ...)") -- this
    generator's frozen constant must be the same string, not just an
    independently-frozen first-read digest like the two re-derived-report
    files above."""
    mod = _load("osdi_paper_macros")
    readme_text = (mod.LONGEVITY_DIR / "README.md").read_text(encoding="utf-8")
    assert f"sha256 `{mod.LONGEVITY_REPLAY_CHECK_SHA256}`" in readme_text
    assert mod.sha256_file(mod.LONGEVITY_REPLAY_CHECK) == mod.LONGEVITY_REPLAY_CHECK_SHA256


def test_longevity_rederived_bytes_per_commit_is_median_slope_over_mean_rate():
    """osdiSoakBytesPerCommitLiveKB is arithmetic, not a measurement:
    round(within-life median RSS slope / mean commits-per-s). Recomputed
    independently here from the same two source macros' own underlying
    floats (not the rounded macro strings) and checked against both the
    generator's internal frozen value and the README's own ~157-158
    KB/commit neighborhood."""
    mod = _load("osdi_paper_macros")
    summary_doc = json.loads(mod.LONGEVITY_SUMMARY_REDERIVED.read_text(encoding="utf-8"))
    median_slope = summary_doc["writer_within_life_rss_slope"]["median_kb_per_s"]
    drift = summary_doc["summary"]["drift"]
    mean_rate = (drift["throughput_first_hour_avg"] + drift["throughput_last_hour_avg"]) / 2
    bytes_per_commit = median_slope / mean_rate
    assert 150 <= bytes_per_commit <= 165
    assert round(bytes_per_commit) == 157

    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiSoakBytesPerCommitLiveKB"] == "157"
    assert values["osdiSoakCommitsPerSecMean"] == "20.2"


def test_longevity_rederived_oom_rss_gb_is_kb_over_1e6():
    """osdiSoakReplayOomRssGB converts the dmesg-parsed anon-rss kB figure
    to GB via decimal division by 1e6 (kilo/giga, not kibi/gibi) --
    checked directly against the dmesg line's own literal figure, not the
    macro string."""
    mod = _load("osdi_paper_macros")
    replay_doc = json.loads(mod.LONGEVITY_REPLAY_CHECK.read_text(encoding="utf-8"))
    dmesg_line = replay_doc["summary"]["attempt_1"]["dmesg_line"]
    match = re.search(r"anon-rss:(\d+)kB", dmesg_line)
    assert match is not None
    rss_kb = int(match.group(1))
    assert rss_kb == 82_997_140
    assert round(rss_kb / 1e6, 1) == 83.0

    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiSoakReplayOomRssGB"] == "83.0"


def test_longevity_rederived_replay_outcome_is_a_text_macro_not_a_number():
    """osdiSoakReplayOutcome ('aborted_oom') must render as literal text,
    same discipline as osdiSoakDigestStatus above."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    values = {name: value for name, value, _ in m.items}
    with pytest.raises(ValueError):
        float(values["osdiSoakReplayOutcome"])
    assert values["osdiSoakReplayOutcome"] == "aborted_oom"


def test_longevity_rederived_first_vs_last_slope_is_labelled_superseded():
    """osdiSoakFirstVsLastSlopeKBps carries the old two-point figure
    forward unchanged for comparison, but its provenance string must
    flag it as superseded by the within-life figures -- it must never be
    read as this record's own memory-FAIL headline number."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    entry = next(item for item in m.items if item[0] == "osdiSoakFirstVsLastSlopeKBps")
    _, value, provenance = entry
    assert value == "111.6"
    assert "SUPERSEDED" in provenance


def test_c10_live_osv_macros_match_frozen_values():
    """osdiLiveDays/osdiLiveAdvisories/osdiLiveCorrections, the C10 macros
    resolved by benchmarks/live-osv-v1/snapshot-2026-09-16.json (Lane
    C10-snap's first committed live-osv record snapshot)."""
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.compute_c10_live_osv(m)
    values = {name: value for name, value, _ in m.items}
    assert values["osdiLiveDays"] == "1.89"
    assert values["osdiLiveAdvisories"] == "32{,}827"
    assert values["osdiLiveCorrections"] == "1"


def test_tampered_live_osv_snapshot_sha256_mismatch_fails(tmp_path):
    """An edited copy of snapshot-2026-09-16.json (even a field this
    generator never otherwise reads) must fail the whole-file sha256
    check against README.md's own quoted value before any advisory/
    correction/days-of-operation figure is trusted."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LIVE_OSV_SNAPSHOT.read_text(encoding="utf-8"))
    data["config"]["compact_every_cycles"] = 999
    tampered = tmp_path / "snapshot-2026-09-16.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LIVE_OSV_SNAPSHOT = tampered
    m = mod.Macros()
    mod.compute_c10_live_osv(m)
    assert mod.FAILURES, "an edited snapshot-2026-09-16.json must fail the sha256 check " \
        "against README.md's own quoted value"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_live_osv_cycles_raw_sum_mismatch_fails_even_with_patched_digest(tmp_path):
    """live_osv.corrections.corrections_written must equal
    sum(cycles_raw[*].corrections_written) -- editing the pre-aggregated
    field alone, with the whole-file sha256 patched to match, must still
    fail this row-level recomputation rather than silently emitting a
    wrong osdiLiveCorrections."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LIVE_OSV_SNAPSHOT.read_text(encoding="utf-8"))
    data["live_osv"]["corrections"]["corrections_written"] = 999
    tampered = tmp_path / "snapshot-2026-09-16.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    mod.LIVE_OSV_SNAPSHOT_SHA256 = _hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LIVE_OSV_SNAPSHOT = tampered
    m = mod.Macros()
    mod.compute_c10_live_osv(m)
    assert mod.FAILURES, "corrections_written that no longer equals " \
        "sum(cycles_raw[*].corrections_written) must fail, even with a patched whole-file digest"
    assert any("corrections_written" in f for f in mod.FAILURES)


def test_tampered_live_osv_advisories_arithmetic_fails_even_with_patched_digest(tmp_path):
    """advisories.total_at_snapshot must equal bootstrap +
    new_since_bootstrap -- breaking that arithmetic, with the whole-file
    sha256 patched to match, must still fail rather than silently
    emitting a wrong osdiLiveAdvisories."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LIVE_OSV_SNAPSHOT.read_text(encoding="utf-8"))
    data["live_osv"]["advisories"]["total_at_snapshot"] = 40000
    tampered = tmp_path / "snapshot-2026-09-16.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    mod.LIVE_OSV_SNAPSHOT_SHA256 = _hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LIVE_OSV_SNAPSHOT = tampered
    m = mod.Macros()
    mod.compute_c10_live_osv(m)
    assert mod.FAILURES, "a total_at_snapshot that no longer equals bootstrap + " \
        "new_since_bootstrap must fail, even with a patched whole-file digest"
    assert any("total_at_snapshot" in f for f in mod.FAILURES)


def test_tampered_live_osv_result_digest_mismatch_fails_even_with_patched_file_digest(tmp_path):
    """The top-level result_digest (sha256 over the canonical counts
    object) must match a fresh recomputation -- editing it directly,
    with the whole-file sha256 patched to match, must still fail."""
    mod = _load("osdi_paper_macros")
    data = json.loads(mod.LIVE_OSV_SNAPSHOT.read_text(encoding="utf-8"))
    data["result_digest"] = "0" * 64
    tampered = tmp_path / "snapshot-2026-09-16.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    mod.LIVE_OSV_SNAPSHOT_SHA256 = _hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LIVE_OSV_SNAPSHOT = tampered
    m = mod.Macros()
    mod.compute_c10_live_osv(m)
    assert mod.FAILURES, "a result_digest that no longer matches the recomputed canonical " \
        "counts hash must fail, even with a patched whole-file digest"
    assert any("result_digest" in f for f in mod.FAILURES)


def test_live_osv_snapshot_sha256_matches_readme_quoted_value():
    """README.md's "Snapshot" section quotes snapshot-2026-09-16.json's
    sha256 verbatim -- this generator's frozen constant must be the same
    string."""
    mod = _load("osdi_paper_macros")
    readme_text = (mod.LIVE_OSV_DIR / "README.md").read_text(encoding="utf-8")
    assert f"`{mod.LIVE_OSV_SNAPSHOT_SHA256}`" in readme_text
    assert mod.sha256_file(mod.LIVE_OSV_SNAPSHOT) == mod.LIVE_OSV_SNAPSHOT_SHA256


def test_r18_and_dag_pending_stubs_cite_the_main_grid_quota_block():
    mod = _load("osdi_paper_macros")
    m = mod.Macros()
    mod.add_pending_stubs(m)
    values = {name: (value, provenance) for name, value, provenance in m.items}
    for name in ("osdiTtfSpeedup", "osdiStormCells", "osdiStormFalseFresh",
                 "osdiStormSpeedupN1k", "osdiStormAvoidedN1k"):
        _, provenance = values[name]
        assert "main grid 12/36" in provenance, f"{name}: reason no longer cites the main-grid " \
            "quota block, update it if the situation has actually changed"


# --------------------------------------------------------------------------
# osdi_paper_figures.py: CSV generation (no matplotlib dependency)
# --------------------------------------------------------------------------

def _load_figures():
    return _load("osdi_paper_figures")


def test_all_deliverables_build_and_write_parseable_csvs(tmp_path, monkeypatch):
    mod = _load_figures()
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    for name, build, write_csv_fn, _plot_fn, csv_filename in mod.DELIVERABLES:
        data = build()
        text = write_csv_fn(data)
        csv_path = tmp_path / csv_filename
        assert csv_path.exists(), f"{name}: CSV was not written to the patched OUT_DIR"
        rows = list(csv.reader(text.splitlines()))
        assert len(rows) >= 2, f"{name}: CSV has no data rows"
        assert len(rows[0]) == len(rows[1]), f"{name}: header/row column-count mismatch"


def test_crash_csv_matches_the_macro_generators_own_numbers(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    macro_mod = _load("osdi_paper_macros")
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_crash_data()
    assert data["total"]["trials"] == 10000
    assert data["total"]["problems"] == 0

    m = macro_mod.Macros()
    macro_mod.compute_c1(m)
    values = dict((n, v) for n, v, _ in m.items)
    assert values["osdiCrashTrials"] == "10{,}000"
    assert str(data["total"]["trials"]) == "10000"


def test_fault_matrix_csv_totals_match_the_macro_frozen_values(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_fault_matrix_data()
    assert data["pre"]["silent-violation"] == 303
    assert data["post"]["silent-violation"] == 32
    assert data["post"]["explicit-failure"] == 2236
    assert data["trials"] == 3102


def test_freshness_table_csv_matches_frozen_values(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_freshness_table_data()
    assert data["avoided_recomputation"] == 5808
    assert data["avoided_denominator"] == 5867
    assert data["avoided_pct"] == 99.0
    m4_row = next(r for r in data["rows"] if r["population"] == "M4 dependency-scope mechanism")
    assert m4_row["trials"] == 447
    assert m4_row["false_fresh_or_false_safe"] == 0
    rowtouch_row = next(r for r in data["rows"] if r["population"] == "M4 naive row-touch control")
    assert rowtouch_row["false_fresh_or_false_safe"] == 212


def test_dag_versions_csv_matches_frozen_values(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_dag_versions_data()
    by_version = {r["version"]: r for r in data["rows"]}
    assert by_version["v1"]["false_safe_cells"] == 20
    assert by_version["v2"]["false_safe_cells"] == 0
    assert by_version["v3"]["false_safe_cells"] == 0
    assert by_version["v2"]["extra_visits_seed0_vs_v1"] == 61
    assert by_version["v2"]["extra_visits_seed1_vs_v1"] == 62
    assert by_version["v3"]["extra_visits_seed0_vs_v1"] == 15
    assert by_version["v3"]["extra_visits_seed1_vs_v1"] == 11
    text = fig_mod.write_dag_versions_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["version", "cells", "false_safe_cells", "extra_visits_seed0_vs_v1",
                        "extra_visits_seed1_vs_v1", "commit"]
    assert len(rows) == 4  # header + v1/v2/v3


def test_r18_crossover_csv_has_five_batches_a_p50_row_and_a_pending_n1000_row(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_r18_crossover_data()
    assert len(data["batches"]) == 5
    assert 0.80 <= data["ttf_global_p50_s"] / data["ttf_l1_p50_s"] <= 0.82
    text = fig_mod.write_r18_crossover_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["batch_index", "check_seconds_tgms_L1", "lookup_ms",
                        "global_recompute_seconds", "ttf_tgms_L1_seconds",
                        "ttf_global_recompute_seconds"]
    assert len(rows) == 1 + 5 + 1 + 1  # header + 5 batches + p50 row + PENDING N=1000 row
    pending_row = rows[-1]
    assert pending_row[0] == "N=1000 c1 seed0"
    assert all(cell == "PENDING" for cell in pending_row[1:])


def test_corruption_matrix_csv_matches_frozen_values(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_corruption_matrix_data()
    assert len(data["classes"]) == 13
    assert len(data["mutations"]) == 7
    assert len(data["rows"]) == 85  # 13x7 minus 6 classes with no swap_same_class cell
    by_key = {(r["class"], r["mutation"]): r for r in data["rows"]}
    blob = by_key[("artifact_blob", "append_garbage")]
    assert blob["trials_pre"] == 106 and blob["detected_pre"] == 0
    assert blob["trials_post"] == 106 and blob["detected_post"] == 106
    text = fig_mod.write_corruption_matrix_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["class", "mutation", "trials_pre", "detected_pre", "detection_rate_pre",
                        "trials_post", "detected_post", "detection_rate_post"]
    assert len(rows) == 1 + 85


def test_overhead_ladder_csv_covers_all_five_rungs_and_twelve_plans(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_ladder_data()
    assert len(data["rung1"]) == 14
    assert len(data["plans"]) == 12
    assert len(data["rung3"]) == 12
    assert len(data["rung4"]) == 12
    assert len(data["rung5"]) == 12
    text = fig_mod.write_ladder_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["rung", "series", "n_steps", "value", "band_low", "band_high"]
    # header + 14 (rung1) + 2 (rung2) + 12*3 (rungs 3/4/5)
    assert len(rows) == 1 + 14 + 2 + 36
    rung_col = [r[0] for r in rows[1:]]
    assert rung_col.count("1") == 14
    assert rung_col.count("2") == 2
    assert rung_col.count("3") == 12
    assert rung_col.count("4") == 12
    assert rung_col.count("5") == 12


def test_cli_csv_only_mode_is_idempotent(tmp_path):
    result1 = subprocess.run(
        [_venv_python(), str(ROOT / "scripts" / "osdi_paper_figures.py"), "--csv-only"],
        cwd=ROOT, capture_output=True, text=True)
    assert result1.returncode == 0, result1.stderr
    result2 = subprocess.run(
        [_venv_python(), str(ROOT / "scripts" / "osdi_paper_figures.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True)
    assert result2.returncode == 0, (
        f"CSVs were not idempotent across two --csv-only runs: {result2.stderr}")


# --------------------------------------------------------------------------
# osdi_paper_figures.py: PDF/PNG rendering (requires matplotlib)
# --------------------------------------------------------------------------

def test_figures_render_to_pdf_and_png(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    if not fig_mod.HAVE_MPL:
        pytest.skip(
            "matplotlib is not installed in this interpreter "
            f"({sys.executable}); scripts/osdi_paper_figures.py falls back to "
            "CSV-only generation in this environment (see its HAVE_MPL guard). "
            "Install matplotlib in $HOME/.venvs/tgms to exercise PDF/PNG rendering.")
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    for name, build, write_csv_fn, plot_fn, csv_filename in fig_mod.DELIVERABLES:
        data = build()
        write_csv_fn(data)
        plot_fn(data)
        pdf_path = (tmp_path / csv_filename).with_suffix(".pdf")
        png_path = (tmp_path / csv_filename).with_suffix(".png")
        assert pdf_path.exists() and pdf_path.stat().st_size > 0, f"{name}: no PDF produced"
        assert png_path.exists() and png_path.stat().st_size > 0, f"{name}: no PNG produced"


def test_figure_rendering_without_matplotlib_fails_clearly(monkeypatch):
    """When matplotlib genuinely is not importable, plot_* must raise a
    clear, actionable error rather than a bare ImportError/traceback."""
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "HAVE_MPL", False)
    with pytest.raises(SystemExit, match="matplotlib is required"):
        fig_mod._require_mpl()


# --------------------------------------------------------------------------
# docs/site_facts.json: the new D-160 facts land beside the old ones, and
# the site-facts CI gate (scripts/site_facts.py check) still passes.
# --------------------------------------------------------------------------

def test_site_facts_d160_facts_land_beside_the_old_gate_facts():
    data = json.loads((ROOT / "docs" / "site_facts.json").read_text(encoding="utf-8"))
    facts = data["facts"]

    old = facts["unsupported_claims"]
    assert old["value"] == "0", "the pre-D-160-gate fact's value must be unchanged"
    assert "0 of 199" in old["prose"] and "21 of 220" in old["prose"], \
        "the pre-D-160-gate fact's prose must be unchanged"
    assert old["label_required"] == "pre-D-160 gate"

    expected = {
        "unsupported_claims_d160": "0",
        "coverage_collegemsg_d160": "0.397",
        "conditional_accuracy_collegemsg_d160": "0.509",
    }
    for key, value in expected.items():
        assert key in facts, f"expected new D-160 fact {key!r} is missing"
        fact = facts[key]
        assert fact["value"] == value, f"{key}: value {fact['value']!r} != {value!r}"
        assert fact["label_required"] == "D-160 gate", f"{key}: must be labelled 'D-160 gate'"
        assert fact["snapshot"] == "benchmarks/d160-collegemsg-v1/manifest-2026-09-14.json"
        assert "d-160" in fact["scope_required"].lower(), \
            f"{key}: scope_required must name the D-160 gate"
        assert "never" in fact["scope_required"].lower(), \
            f"{key}: scope_required must forbid printing this number alone"


def test_site_facts_check_gate_passes():
    result = subprocess.run([_venv_python(), str(ROOT / "scripts" / "site_facts.py"), "check"],
                             cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------
# ruff
# --------------------------------------------------------------------------

def test_ruff_clean():
    ruff = _ruff_path()
    if ruff is None:
        pytest.skip(f"no ruff binary found next to {sys.executable}")
    targets = [
        "scripts/osdi_paper_macros.py",
        "scripts/osdi_paper_figures.py",
        "scripts/site_facts.py",
        "tests/test_osdi_paper_macros.py",
    ]
    result = subprocess.run([str(ruff), "check", *targets], cwd=ROOT,
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------
# cleanup: don't leave a stray tampered file's directory artifacts behind
# in the real paper/osdi/generated/ tree (tests above use tmp_path/
# monkeypatch for everything except the two CLI idempotency tests, which
# intentionally exercise the real generator against the real, committed
# records and write into the documented paper/osdi/generated/ convention).
# --------------------------------------------------------------------------

def test_generated_output_directory_is_the_documented_convention():
    mod = _load("osdi_paper_macros")
    fig_mod = _load_figures()
    assert mod.OUT_DIR == ROOT / "paper" / "osdi" / "generated"
    assert fig_mod.OUT_DIR == ROOT / "paper" / "osdi" / "generated"
    # paper/ is gitignored repo-wide; nothing this task writes there is
    # ever meant to be committed.
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "paper/" in gitignore.splitlines()
