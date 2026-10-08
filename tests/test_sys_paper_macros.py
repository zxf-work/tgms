"""Tests for the submission campaign's receipts machinery (Lane W, task W2):
``scripts/sys_paper_macros.py`` and ``scripts/sys_paper_figures.py``.

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
import shutil
import statistics
import subprocess
import sys
import tempfile
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
# sys_paper_macros.py: recomputation and frozen values
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
    mod.compute_longevity_soak_two(m)
    mod.compute_longevity_verify_and_replay2(m)
    mod.compute_longevity_soak_hunt(m)
    mod.compute_longevity_soak_three(m)
    mod.compute_longevity_compaction_cadence(m)
    mod.compute_longevity_soak_four(m)
    mod.compute_overload(m)
    mod.compute_c10_live_osv(m)
    mod.compute_ldbc_ref_v1(m)
    mod.compute_ldbc_format3_rebuild(m)
    mod.compute_b7_scale(m)
    mod.compute_external_baselines(m)
    return m


def test_landed_macros_recompute_without_any_verification_failure():
    mod = _load("sys_paper_macros")
    assert mod.CHECKS == 0 and mod.FAILURES == []
    _run_all_landed(mod)
    assert mod.FAILURES == [], f"unexpected verification failures: {mod.FAILURES}"
    assert mod.CHECKS > 100, "expected many row-level assertions, not a handful"


FROZEN_LANDED_VALUES = {
    "recCrashTrials": "10{,}000",
    "recCrashProblems": "0",
    "recCrashBoundaries": "10",
    "recCrashWall": "4{,}912.85",
    "recManifestBytesCtl": "25.97",
    "recManifestBytesTrt": "62.0",
    "recManifestCommitRatio": "1.798",
    "recManifestColdOpen": "8.02",
    "recB1v2ControlCommit": "886805f",
    "recB1v2TreatmentCommit": "7a5ff98",
    "recB1v2BytesControlMB": "73.8",
    "recB1v2BytesTreatmentMB": "74.4",
    "recB1v2BytesPaired": "1.008",
    "recB1v2SegmentBytesControlMB": "159.0",
    "recB1v2SegmentBytesTreatmentMB": "163.4",
    "recB1v2ManifestDecileControl": "1.115",
    "recB1v2ManifestDecileTreatment": "1.079",
    "recB1v2ManifestDecileK128": "1.068",
    "recB1v2ManifestDecileK1024": "1.150",
    "recB1v2TotalDecileControl": "1.733",
    "recB1v2TotalDecileTreatment": "1.674",
    "recB1v2P50ControlMs": "5.430",
    "recB1v2P50TreatmentMs": "5.478",
    "recB1v2P50Paired": "1.009",
    "recB1v2OpenControlMs": "2247.9",
    "recB1v2OpenTreatmentMs": "1705.3",
    "recB1v2OpenPaired": "0.76",
    "recB1v2OpenControlGeneration": "10{,}759",
    "recB1v2OpenTreatmentGeneration": "11{,}029",
    "recB1v2BuildOpsPerSecRatioAt2p5M": "2.10",
    "recB1v2OpenComponentStatus": "not computed",
    "recB1v2eTreatmentCommit": "e5d4171",
    "recB1v2eTotalDecileTreatment": "1.017",
    "recB1v2eTotalDecileControl": "1.696",
    "recB1v2eResidualFirstUs": "33.06",
    "recB1v2eResidualLastUs": "30.38",
    "recB1v2eP50TreatmentMs": "3.365",
    "recB1v2eP50ControlMs": "4.704",
    "recB1v2eP50Paired": "0.715",
    "recB1v2eWallP50TreatmentMs": "4.274",
    "recB1v2eWallP50ControlMs": "5.303",
    "recB1v2eWallPaired": "0.806",
    "recB1v2eOpenComponentMs": "87.06",
    "recB1v2eOpenCheckpointMs": "42.124",
    "recB1v2eOpenMerkleVerifyMs": "40.204",
    "recB1v2eOpenStateBuildMs": "1.828",
    "recB1v2eOpenDeltaReplayMs": "2.899",
    "recB1v2eOpenDictionaryMs": "1066.5",
    "recB1v2eOpenTotalMs": "1191.8",
    "recB1v2eOpenGeneration": "10{,}365",
    "recB1v2eControlOpenStatus": "confounded (concurrent backup transfer)",
    "recB1v2eManifestBytesTreatment": "1592",
    "recB1v2eManifestBytesControl": "1565",
    "recB1co7TreatmentCommit": "ebe1dc2",
    "recB1co7CheckpointReadParseMs": "12.67",
    "recB1co7MerkleVerifyMs": "34.86",
    "recB1co7StateBuildMs": "5.94",
    "recB1co7DeltaReplayMs": "6.26",
    "recB1co7ComponentMs": "59.72",
    "recB1co7DictionaryOpenMs": "1092.7",
    "recB1co7TotalMs": "1155.0",
    "recB1co7Generation": "10{,}116",
    "recB1co7DeltaCount": "388",
    "recB1co7CheckpointGeneration": "9728",
    "recB1co7StateBuildPerDeltaUs": "15.3",
    "recB1co7DeltaReplayPerDeltaUs": "16.1",
    "recB1co7ControlOpenMs": "10100.2",
    "recB1co7ControlGeneration": "10{,}042",
    "recB1co7ControlDeltaCount": "314",
    "recB1co7ControlPerDeltaMs": "28.7",
    "recB1co7OpenRatio": "0.114",
    "recB1WorstPhaseOpenTreatmentMs": "63.6",
    "recB1WorstPhaseOpenControlS": "14.7",
    "recVhRss": "1.259",
    "recVhWall": "1.87",
    "recVhRatio": "7.32",
    "recVhProjHundredM": "12.6",
    "recReadersMax": "32",
    "recReaderVmhwm": "1.19--1.22",
    "recAggQpsOne": "2.85",
    "recAggQpsThirtyTwo": "27.72",
    "recWriterTailCost": "+47.5\\%/+42.7\\%",
    "recFalseFreshCarveTwo": "0",
    "recRowTouchRate": "47.4",
    "recNewIdentityFF": "89",
    "recAvoidedPct": "99.0",
    "recFaultTrials": "3102",
    "recSilentPre": "303",
    "recSilentPost": "0",
    "recFOneNineBefore": "271",
    "recFTwoThree": "32",
    "recDagCells": "40",
    "recDagV1FalseSafeCells": "20",
    "recDagV2FalseSafeCells": "0",
    "recDagV3FalseSafeCells": "0",
    "recDagV1FalseSafePerCell": "3",
    "recDagV2ExtraVisitsSeedZero": "61",
    "recDagV2ExtraVisitsSeedOne": "62",
    "recDagV3ExtraVisitsSeedZero": "15",
    "recDagV3ExtraVisitsSeedOne": "11",
    "recDagV3AllTopTerm": "0",
    "recDagFalseFreshTotal": "0",
    "recR18Artifacts": "10{,}000",
    "recR18IntersectsCallsMedian": "13{,}009",
    "recR18LookupMsMedian": "26.91",
    "recR18SurvivorFraction": "71.3",
    "recR18CheckSecondsMedian": "867.6",
    "recR18TtfL1Seconds": "1977.6",
    "recR18TtfGlobalSeconds": "1595.3",
    "recR18Speedup": "0.807",
    "recR18Precision": "8.51",
    "recR18AvoidedRecompute": "29.9",
    "recStormV1Commit": "8962b78",
    "recStormV1Cells": "36",
    "recStormV1CellsFailed": "0",
    "recStormV1FalseFreshTgmsCellsNonzero": "0",
    "recStormV1SpeedupN1kSeed0": "1.938",
    "recStormV1SpeedupSynthC1None": "1.895",
    "recStormV1SpeedupSynthC1Deep": "1.908",
    "recStormV1SpeedupSynthC3None": "1.933",
    "recStormV1SpeedupSynthC3Deep": "1.939",
    "recStormV1SpeedupSynthC4None": "1.788",
    "recStormV1SpeedupSynthC4Deep": "1.756",
    "recStormV1SpeedupCollegeMsgC1None": "2.333",
    "recStormV1SpeedupCollegeMsgC1Deep": "2.304",
    "recStormV1SpeedupCollegeMsgC3None": "2.382",
    "recStormV1SpeedupCollegeMsgC3Deep": "2.427",
    "recStormV1SpeedupCollegeMsgC4None": "2.325",
    "recStormV1SpeedupCollegeMsgC4Deep": "2.151",
    "recStormV1SpeedupGridMin": "1.742",
    "recStormV1SpeedupGridMax": "2.558",
    "recStormV1AvoidedDecisionC1Median": "0.309",
    "recStormV1AvoidedDecisionC3Median": "0.312",
    "recStormV1AvoidedDecisionC4Median": "0.312",
    "recStormV1Batches": "720",
    "recStormV1RowTouchFalseFreshMedian": "1.000",
    "recStormV1EntityTouchFalseFreshMedian": "0.993",
    "recStormV1WindowOverlapFalseFreshMedian": "0.189",
    "recStormV1WindowOverlapNonzeroBatches": "258",
    "recStormV1NewIdentityBatches": "195",
    "recStormV1NewIdentityRowTouchMedian": "1.000",
    "recStormV1P4ViolationCells": "0",
    "recStormV2ProbeCommit": "fdd393c",
    "recStormV2ProbeBatches": "5",
    "recStormV2ProbeWallS": "12{,}452.8",
    "recStormV2ProbeGlobalTtfS": "1{,}569.3",
    "recStormV2ProbeTgmsL1TtfS": "806.3",
    "recStormV2ProbeSpeedupN10k": "1.946",
    "recStormV2ProbeAvoidedDecision": "0.758",
    "recStormV2ProbeSurvivorMedian": "0.283",
    "recStormV2ProbePrecisionMedian": "0.211",
    "recStormV2ProbeIntersectsMedian": "29{,}193",
    "recStormV2ProbeR18Tripped": "no",
    "recStormV2ProbeAllTopTerms": "0",
    "recStormV2ProbeNonComputeArtifacts": "8203",
    "recStormV2ProbeCheckWallMedianS": "336.9",
    "recStormV2Commit": "fdd393c",
    "recStormV2Cells": "36",
    "recStormV2CellsFailed": "0",
    "recStormV2AllTopTerms": "0",
    "recStormV2NonComputeArtifacts": "29{,}826",
    "recStormV2SpeedupN1kSeed0": "5.173",
    "recStormV2AvoidedDecisionC1Median": "0.755",
    "recStormV2C1Batches": "240",
    "recStormV2SurvivorFractionC1Median": "0.262",
    "recStormV2PrecisionC1Median": "0.187",
    "recStormV2SurvivorFractionSynthC1Median": "0.266",
    "recStormV2SurvivorFractionCollegeMsgC1Median": "0.257",
    "recStormV2PrecisionSynthC1Median": "0.182",
    "recStormV2PrecisionCollegeMsgC1Median": "0.194",
    "recStormV2FalseFreshTgmsCellsNonzero": "0",
    "recStormV2SpeedupSynthC1None": "5.173",
    "recStormV2SpeedupSynthC1Deep": "4.902",
    "recStormV2SpeedupSynthC3None": "5.642",
    "recStormV2SpeedupSynthC3Deep": "5.217",
    "recStormV2SpeedupSynthC4None": "6.367",
    "recStormV2SpeedupSynthC4Deep": "5.022",
    "recStormV2SpeedupCollegeMsgC1None": "6.189",
    "recStormV2SpeedupCollegeMsgC1Deep": "6.821",
    "recStormV2SpeedupCollegeMsgC3None": "6.662",
    "recStormV2SpeedupCollegeMsgC3Deep": "6.332",
    "recStormV2SpeedupCollegeMsgC4None": "7.433",
    "recStormV2SpeedupCollegeMsgC4Deep": "8.071",
    "recStormV2SpeedupGridMin": "4.588",
    "recStormV2SpeedupGridMax": "8.863",
    "recD160Tasks": "94",
    "recD160TaskRuns": "282",
    "recD160OursCarrying": "112",
    "recD160OursCoverage": "0.397",
    "recD160OursCondAcc": "0.509",
    "recD160OursUcrGated": "0",
    "recD160OursUcrPreGate": "0.212",
    "recD160B6eCoverage": "0.830",
    "recD160B6eCondAcc": "0.333",
    "recD160B5Em": "0.181",
    "recD160LlmDirectCarrying": "0",
    "recD160LlmDirectOverflowErrors": "216",
    "recD160LlmDirectCoverageFixed": "0.000",
    "recD160LlmDirectErrorsFixed": "0",
    "recD160LlmDirectRawEmFixed": "0.064",
    "recD160LlmDirectTokenizerFixed": "hf\\_real",
    "recD160LlmDirectBudgetFixed": "8000",
    "recOldGateCoverage": "0.706",
    "recOldGateUcr": "0",
    "recOldGateCondAcc": "0.548",
    "recCorruptionTrials": "10{,}000",
    "recCorruptionClasses": "13",
    "recCorruptionMutations": "7",
    "recCorruptionDetected": "6587",
    "recCorruptionDetectedPre": "5966",
    "recCorruptionDetectedPost": "6587",
    "recCorruptionSilentPre": "0",
    "recCorruptionSilentPost": "0",
    "recCorruptionBlobDetectedPre": "0/621",
    "recCorruptionBlobDetectedPost": "621/621",
    "recCorruptionBlobTrials": "710",
    "recCorruptionBlobDetectedAllPre": "89/710",
    "recCorruptionBlobDetectedAllPost": "710/710",
    "recCorruptionBlobMutationAlreadyDetected": "delete\\_file",
    "recCorruptionCellsMovedPost": "0",
    "recLadderPlans": "12",
    "recLadderOperatorsCovered": "14",
    "recLadderRung1Min": "0.992",
    "recLadderRung1Max": "1.064",
    "recLadderRung2EntityHistory": "1.13",
    "recLadderRung2VersionHistory": "2.54",
    "recLadderRung3BytesOneStepMedian": "3309",
    "recLadderRung3BytesThreeStepMedian": "7549",
    "recLadderRung3Deterministic": "12",
    "recLadderRung4VerifyMsMedian": "5.06",
    "recLadderRung5TokensMedian": "1811.5",
    "recLadderRung5ToolCallsEqualExecutedSteps": "12",
    "recLadderPlansTruncated": "2",
    "recSoakHours": "24",
    "recSoakCommit": "886805f",
    "recSoakEntitiesStart": "1{,}000{,}000",
    "recSoakEntitiesEnd": "1{,}730{,}492",
    "recSoakBatches": "1{,}074{,}952",
    "recSoakWriterLives": "42",
    "recSoakRecoveries": "41",
    "recSoakRecoveriesSigabrt": "23",
    "recSoakRecoveriesExit137": "18",
    "recSoakUnexpectedRecoveries": "0",
    "recSoakReaderDeaths": "2",
    "recSoakReaderDeathCause": "reader torn-tail race, pre-fix engine",
    "recSoakVerifyHealthy": "true",
    "recSoakWriterErrorsManifest": "1",
    "recSoakWriterErrorsTrue": "249",
    "recSoakThroughputStart": "24.03",
    "recSoakThroughputEnd": "16.41",
    "recSoakP99StartMs": "485.6",
    "recSoakP99EndMs": "3{,}740.5",
    "recSoakManifestGrowthBps": "-3.4",
    "recSoakSegmentGrowthBps": "1{,}341.1",
    "recSoakDigestStatus": "not computed",
    "recSoakReplayProjectedTB": "280.8",
    "recSoakCompactions": "2415",
    "recSoakWriterWithinLifeSlopeMedianKBps": "3165.6",
    "recSoakWriterWithinLifeSlopeMinKBps": "1921.1",
    "recSoakWriterWithinLifeSlopeMaxKBps": "4154.2",
    "recSoakWriterLivesFitted": "42",
    "recSoakWriterLivesPositive": "42",
    "recSoakReaderWithinLifeSlopeMedianKBps": "5.32",
    "recSoakReaderWithinLifeSlopeMaxKBps": "47.07",
    "recSoakFirstVsLastSlopeKBps": "111.6",
    "recSoakCommitsPerSecMean": "20.2",
    "recSoakBytesPerCommitLiveKB": "157",
    "recSoakReplayOutcome": "aborted_oom",
    "recSoakReplayOomRssGB": "83.0",
    "recSoakReplayOomGeneration": "513{,}024",
    "recSoakReplayFractionApplied": "0.476",
    "recSoakReplayKBPerGeneration": "161.8",
    "recSoakReplayElapsedH": "3.37",
    "recSoakCommitTwo": "eed91c0",
    "recSoakHoursTwo": "24",
    "recSoakWriterLivesTwo": "4",
    "recSoakRecoveriesTwo": "3",
    "recSoakRecoveriesSigabrtTwo": "2",
    "recSoakRecoveriesExit137Two": "1",
    "recSoakUnexpectedRecoveriesTwo": "0",
    "recSoakWriterWithinLifeSlopeMedianKBpsTwo": "27.965",
    "recSoakWriterWithinLifeSlopeMinKBpsTwo": "20.978",
    "recSoakWriterWithinLifeSlopeMaxKBpsTwo": "43.494",
    "recSoakReaderWithinLifeSlopeMinKBpsTwo": "7.212",
    "recSoakReaderWithinLifeSlopeMaxKBpsTwo": "8.098",
    "recSoakDigestEqualTwo": "true",
    "recSoakBatchesTwo": "1{,}891{,}962",
    "recSoakFullVerifyOverlapCountTwo": "0",
    "recSoakFullVerifyVerdictTwo": "healthy",
    "recSoakWriterErrorsTrueTwo": "616",
    "recSoakWriterErrorsClassTwo": "NotFoundError",
    "recSoakReaderErrorsTrueTwo": "150{,}476{,}512",
    "recSoakReaderErrorsOSErrorTwo": "150{,}278{,}720",
    "recSoakReaderErrorsStateErrorTwo": "197{,}792",
    "recSoakReaderQueriesTwo": "16{,}055{,}124",
    "recSoakThroughputStartTwo": "39.91",
    "recSoakThroughputEndTwo": "19.117",
    "recSoakThroughputLastOverFirstRatioTwo": "0.479",
    "recSoakP99StartMsTwo": "60.42",
    "recSoakP99EndMsTwo": "82.075",
    "recSoakManifestGrowthBpsTwo": "3.141",
    "recSoakSegmentGrowthBpsTwo": "2{,}396.455",
    "recSoakEntitiesEndTwo": "3{,}092{,}488",
    "recSoakCompactionsTwo": "4215",
    "recSoakReaderOnsetEarliestRowsTwo": "2{,}235{,}044",
    "recSoakReaderOnsetLatestRowsTwo": "2{,}450{,}815",
    "recSoakReaderOnsetEarliestSTwo": "61417.1",
    "recSoakReaderOnsetLatestSTwo": "75993.1",
    "recLiveDays": "1.89",
    "recLiveAdvisories": "32{,}827",
    "recLiveCorrections": "1",
    # W2aa: the remaining C10 (live OSV) figures the draft still typed --
    # compute_c10_live_osv above.
    "recLiveCycles": "38",
    "recLiveRestarts": "0",
    "recLiveFeedErrors": "0",
    "recLiveNoopRevisions": "41",
    "recLiveRecordsSeen": "95",
    "recLiveRetractions": "1",
    "recLiveNodes": "247{,}845",
    "recLiveEdges": "497{,}522",
    "recB7BuildWall30M": "3786.004",
    "recB7PeakRSS30M": "59.31",
    "recB7VersionHistoryWall30M": "5.496",
    "recB7VersionHistoryRSS30M": "3.863",
    "recB7ManifestBytes30M": "175{,}244",
    "recB7SegmentBytes30M": "1.549",
    "recB7CheckFullWall30M": "98.673",
    "recB7Recovery30M": "14{,}618.6",
    "recB7RecoveryCe5000At30M": "2656.9",
    # W2aa: the 30M recovery ratios the draft typed as "5.3x" and "5.5
    # times" -- compute_b7_scale above.
    "recB7RecoveryBoundRatioAt30M": "5.3",
    "recB7RecoveryCadenceRatioAt30M": "5.5",
    "recB7ScaleCurveP50HistSingle30M": "0.977",
    "recB7ScaleCurveP50HistAsof30M": "0.98",
    "recB7ScaleCurveP50SnapHop230M": "1531.519",
    "recB7ScaleCurveP50DiffGlobal30M": "7505.374",
    "recB7ScaleCurveP50ReachWindow30M": "2354.494",
    "recB7ScaleCurveP50PathsK30M": "8.621",
    "recB7ScaleCurveP50SeriesCount30M": "128.144",
    "recB7ScaleCurveP50BurstZscore30M": "129.433",
    "recB7ScaleCurveP50NbrEvolution30M": "100.237",
    "recB7ScaleCurveP50CoactiveNarrow30M": "302.473",
    "recB7ScaleCurveP50ResolveSubstr30M": "342.604",
    "recB7ScaleCurveP50AggRelBucket30M": "176.492",
    "recB7ScaleCurveP50MotifFiltered30M": "76.195",
    "recB7ReachWindowRefused30M": "false",
    "recB7QueryFloor30M": "6.81",
    "recB7BuildSteadyDecileMedian30M": "8463.0",
    "recB7ReachWindowEstimateMs30M": "4371",
    "recB7300MGate": "false",
    "recB7Calib10MBuildWall": "865.592",
    "recB7Calib10MPeakRSS": "19.90",
    "recB7Calib10MSteadyOps": "24{,}390.8",
    "recB7KBuild": "4.602",
    "recB7KRecover": "0.834",
    "recB7Calib1MBuildWall": "78.339",
    "recB7Calib1MPeakRSS": "2.37",
    "recB7Calib1MRecovery": "75.86",
    "recB7Calib1MSegmentBytes": "0.050",
    "recB7BuildWall100M": "28{,}372.936",
    "recB7PeakRSS100M": "186.42",
    "recB7VersionHistoryWall100M": "18.982",
    "recB7VersionHistoryRSS100M": "12.817",
    "recB7ManifestBytes100M": "206{,}946",
    "recB7SegmentBytes100M": "5.218",
    "recB7CheckFullWall100M": "336.353",
    "recB7RecoveryCe5000At100M": "22{,}717.7",
    "recB7Recovery100M": "22{,}717.7",
    # W2aa: the 100M recovery miss the draft typed as "a 5% miss" --
    # compute_b7_scale above.
    "recB7RecoveryCe5000MissPctAt100M": "5",
    "recB7ScaleCurveP50HistSingle100M": "12.965",
    "recB7ScaleCurveP50HistAsof100M": "12.998",
    "recB7ScaleCurveP50PathsK100M": "20.164",
    "recB7ScaleCurveP50SeriesCount100M": "291.521",
    "recB7ScaleCurveP50BurstZscore100M": "294.233",
    "recB7ScaleCurveP50MotifFiltered100M": "110.861",
    "recB7RefusedSnapHop2100M": "true",
    "recB7RefusedDiffGlobal100M": "true",
    "recB7RefusedNbrEvolution100M": "true",
    "recB7RefusedCoactiveNarrow100M": "true",
    "recB7RefusedResolveSubstr100M": "true",
    "recB7RefusedAggRelBucket100M": "true",
    "recB7ReachWindowRefused100M": "true",
    "recB7QueryFloor100M": "19.67",
    "recB7BuildSteadyDecileMedian100M": "3738.9",
    "recB7ReachWindowEstimateMs100M": "14{,}571",
    "recB7CompactionShare100M": "83.76",
    # Lane W-lane: the original soak's writer corrections (true totals, 42
    # lives) and the writer within-life noise floor -- compute_longevity_soak
    # / compute_longevity_rederived above.
    "recSoakCorrectionsAppliedTrue": "207{,}850",
    "recSoakCorrectionsSkippedTrue": "137{,}163",
    "recSoakWriterNoiseFloorKBps": "5",
    # Lane W-lane: the original soak's full-mode verify (two `tgms check`
    # entries) and REPLAY-2 (the post-D-087-fix replay that completed) --
    # compute_longevity_verify_and_replay2 above.
    "recSoakVerifyFullGeneration": "1{,}076{,}872",
    "recSoakVerifyFullOverlapCount": "13{,}714",
    "recSoakVerifyFullVerdict": "CORRUPT",
    "recSoakReplay2VerifyGeneration": "1{,}076{,}598",
    "recSoakReplay2VerifyOverlapCount": "13{,}714",
    "recSoakReplay2VerifyVerdict": "CORRUPT",
    "recSoakReplay2BatchesApplied": "1{,}074{,}450",
    "recSoakReplay2Compactions": "2148",
    "recSoakReplay2WallS": "30{,}015",
    "recSoakReplay2ElapsedH": "8.34",
    "recSoakReplay2PeakRssKB": "4{,}329{,}996",
    "recSoakReplay2PeakRssGB": "4.33",
    "recSoakReplay2PeakDiskMB": "586",
    "recSoakReplay2RssSamples": "101",
    "recSoakReplay2DigestEqual": "true",
    "recSoakReplay2DigestPrefix": "8eb9bc26",
    # Lane W2u: P-STORM-HUNT, the 6h observation-only run (commit 57952fa)
    # -- compute_longevity_soak_hunt above.
    "recSoakCommitHunt": "57952fa",
    "recSoakDurationSHunt": "21{,}600",
    "recSoakWriterLivesHunt": "1",
    "recSoakRecoveriesHunt": "0",
    "recSoakWriterWithinLifeSlopeKBpsHunt": "20.4",
    "recSoakReaderWithinLifeSlopeMinKBpsHunt": "11.4",
    "recSoakReaderWithinLifeSlopeMaxKBpsHunt": "12.5",
    "recSoakDigestEqualHunt": "true",
    "recSoakBatchesHunt": "476{,}813",
    "recSoakVerifyHealthyHunt": "true",
    "recSoakWriterErrorsTrueHunt": "175",
    "recSoakWriterErrorsClassHunt": "NotFoundError",
    "recSoakReaderErrorsTrueHunt": "0",
    "recSoakReaderOpErrorEventsHunt": "0",
    "recSoakReaderOpErrorCaptureCommitHunt": "c3a5592",
    "recSoakEdgeRowsHunt": "1{,}402{,}818",
    "recSoakHostLoadMinHunt": "10.79",
    "recSoakHostLoadMaxHunt": "48.95",
    # W2aa: the P-STORM-HUNT host's core count the draft typed as "40
    # cores" -- compute_longevity_soak_hunt above.
    "recSoakHostCoresHunt": "40",
    "recSoakHuntPatternReproduced": "false",
    # Lane W-lane: P-OV1, the xzgpu-calibrated overload sweep -- compute_overload above.
    "recOverloadCommit": "ebe1dc2",
    "recOverloadMaxConcurrent": "8",
    "recOverloadClientsMax": "64",
    "recOverloadRefusalKindConcurrencyOnly": "true",
    "recOverloadOperatorErrorsTotal": "0",
    "recOverloadRefusedCapAtMaxRep1": "4438",
    "recOverloadRefusedCapAtMaxRep2": "285",
    "recOverloadRefusalRepRatio": "15.6",
    "recOverloadAdmittedConcurrencyP95AtMax": "8.0",
    "recOverloadP99At32ClientsMs": "40.39",
    "recOverloadRecoveryQps": "20.13",
    "recOverloadRecoveryP50Ms": "1.43",
    "recOverloadServiceHighWaterKB": "227{,}280",
    "recOverloadServiceHighWaterMB": "227.3",
    # Lane W2t: benchmarks/ldbc-ref-v1/ -- compute_ldbc_ref_v1 above.
    "recLdbcTemplates": "24",
    "recLdbcExpressible": "24",
    "recLdbcExecuted": "23",
    "recLdbcValidated": "18",
    "recLdbcAgree": "18",
    "recLdbcNotProjected": "3",
    "recLdbcDisagree": "2",
    "recLdbcTimeout": "1",
    "recLdbcComparableTemplates": "23",
    "recLdbcRowsCompared": "736",
    "recLdbcRowsAgreeing": "678",
    "recLdbcRowAgreementFraction": "0.921",
    "recLdbcGate": "0.90",
    "recLdbcGateMet": "true",
    "recLdbcTemplateAgreementFraction": "0.75",
    "recLdbcRowsBIAgreeing": "606",
    "recLdbcRowsBICompared": "607",
    "recLdbcRowsICAgreeing": "56",
    "recLdbcRowsICCompared": "66",
    "recLdbcRowsISAgreeing": "16",
    "recLdbcRowsISCompared": "63",
    "recLdbcDefectId": "D-090",
    "recLdbcDefectTemplates": "IS3, IC2",
    "recLdbcInterimAgree": "14",
    "recLdbcInterimRowsAgreeing": "282",
    "recLdbcInterimRowsCompared": "329",
    # Lane W2x: benchmarks/longevity-v1/, "Soak 3 (72 h)" -- compute_longevity_soak_three
    # above.
    "recSoakCommitThree": "9e21a83",
    "recSoakHoursThree": "72",
    "recSoakWallHoursThree": "92.05",
    "recSoakWriterLivesThree": "9",
    "recSoakRecoveriesThree": "8",
    "recSoakRecoveryMinSThree": "25.667",
    "recSoakRecoveryMaxSThree": "96.352",
    "recSoakUnexpectedRecoveriesThree": "0",
    "recSoakReaderDeathsThree": "0",
    "recSoakDigestEqualThree": "true",
    "recSoakBatchesThree": "3{,}618{,}225",
    "recSoakReplayCadenceThree": "5000",
    "recSoakWriterWithinLifeSlopeMedianKBpsThree": "16.630",
    "recSoakWriterWithinLifeSlopeMaxKBpsThree": "28.834",
    "recSoakReaderWithinLifeSlopeMinKBpsThree": "4.271",
    "recSoakReaderWithinLifeSlopeMaxKBpsThree": "4.796",
    "recSoakWriterErrorsTrueThree": "854",
    "recSoakWriterErrorsClassThree": "NotFoundError",
    "recD088ReopenIntervalS": "300",
    "recD088GenerationsRetained": "2",
    "recSoakReaderErrorsTrueThree": "2{,}203{,}551{,}806",
    "recSoakReaderErrorsOSErrorThree": "2{,}202{,}621{,}096",
    "recSoakReaderErrorsStateErrorThree": "930{,}710",
    "recSoakReaderReopensMinThree": "777",
    "recSoakReaderReopensMaxThree": "779",
    "recSoakReaderOpErrorEventsThree": "17",
    "recSoakReaderErrorEpisodesThree": "662",
    "recSoakReaderErrorLongestHealedIntervalSThree": "37272.1",
    "recSoakReaderOnsetEarliestRowsThree": "1{,}950{,}613",
    "recSoakReaderOnsetLatestRowsThree": "2{,}356{,}519",
    "recSoakReaderOnsetEarliestSThree": "65655.8",
    "recSoakReaderOnsetLatestSThree": "94769.4",
    "recSoakThroughputStartThree": "40.648",
    "recSoakThroughputEndThree": "11.014",
    "recSoakThroughputFirstDayAvgThree": "19.713",
    "recSoakThroughputLastDayAvgThree": "12.952",
    "recSoakThroughputLastOverFirstDayRatioThree": "0.657",
    "recSoakFullVerifyOverlapCountThree": "0",
    "recSoakFullVerifyGenerationThree": "3{,}624{,}668",
    "recSoakHostLoadMinThree": "1.21",
    "recSoakHostLoadMaxThree": "188.27",
    # Lane W2z
    "recSoakCompactionIntervalMedianSTwo": "20.5",
    "recSoakCompactionDurationMedianSTwo": "12.1",
    "recSoakGenerationWindowSTwo": "40.9",
    "recSoakCompactionIntervalMedianSThree": "31.5",
    "recSoakCompactionDurationMedianSThree": "20.1",
    "recSoakGenerationWindowSThree": "63.1",
    "recStormV2CleanCells": "34",
    "recLdbcFormatThreeRebuildPct": "12.8",
    # Lane W2ab (P-SOAK4, 24h, D-088-fixed engine)
    "recSoakCommitFour": "b6cdde0",
    "recSoakHoursFour": "24",
    "recSoakWallHoursFour": "28.47",
    "recSoakWriterLivesFour": "4",
    "recSoakRecoveriesFour": "3",
    "recSoakRecoveryMinSFour": "17.636",
    "recSoakRecoveryMaxSFour": "47.114",
    "recSoakUnexpectedRecoveriesFour": "0",
    "recSoakReaderDeathsFour": "0",
    "recSoakDigestEqualFour": "true",
    "recSoakBatchesFour": "1{,}894{,}067",
    "recSoakReplayCadenceFour": "5000",
    "recSoakWriterWithinLifeSlopeMedianKBpsFour": "22.085",
    "recSoakWriterWithinLifeSlopeMaxKBpsFour": "37.880",
    "recSoakReaderWithinLifeSlopeMinKBpsFour": "7.957",
    "recSoakReaderWithinLifeSlopeMaxKBpsFour": "8.399",
    "recSoakReaderRssAnonSlopeMinKBpsFour": "4.811",
    "recSoakReaderRssAnonSlopeMaxKBpsFour": "5.413",
    "recSoakReaderRssFileSlopeMinKBpsFour": "1.034",
    "recSoakReaderRssFileSlopeMaxKBpsFour": "1.056",
    "recSoakReaderVmSizeSlopeMinKBpsFour": "6.618",
    "recSoakReaderVmSizeSlopeMaxKBpsFour": "7.170",
    "recSoakWriterErrorsTrueFour": "595",
    "recSoakWriterErrorsClassFour": "NotFoundError",
    "recSoakReaderErrorsTrueFour": "0",
    "recSoakReaderErrorsOSErrorFour": "0",
    "recSoakReaderErrorsStateErrorFour": "0",
    "recSoakReaderReopensMinFour": "272",
    "recSoakReaderReopensMaxFour": "272",
    "recSoakReaderReopenOnEnoentMaxFour": "0",
    "recSoakThroughputStartFour": "40.552",
    "recSoakThroughputEndFour": "19.164",
    "recSoakThroughputFirstHourAvgFour": "40.552",
    "recSoakThroughputLastOverFirstRatioFour": "0.475",
    "recSoakFullVerifyOverlapCountFour": "0",
    "recSoakFullVerifyGenerationFour": "1{,}897{,}091",
    "recSoakCompactionStallMaxReaderP99MsFour": "222.871",
    "recSoakHostLoadMinFour": "0.09",
    "recSoakHostLoadMaxFour": "12.48",
    "recSoakFinalEdgeRowsFour": "2{,}600{,}501",
    "recSoakFourFirstHourBandLo": "37.9145",
    "recSoakFourFirstHourBandHi": "41.9055",
    "recSoakCompactionIntervalMedianSFour": "20.5",
    "recSoakCompactionDurationMedianSFour": "12.2",
    "recSoakGenerationWindowSFour": "41.0",
    "recExt1Cells": "43",
    "recExt1AgreeCells": "33",
    "recExt1Disagreements": "195",
    "recExt1Families": "13",
    "recExt1IteratedFamilies": "1",
    "recExt1NeoVersion": "5.26",
    "recExt1RecomputeMedianS": "57.1",
    "recExt1RatioGrMin": "0.68",
    "recExt1RatioGrMedian": "0.77",
    "recExt1RatioGrMax": "1.14",
    "recExt1RatioGrSameHostMedian": "0.49",
    "recExt1ProbeRatio": "0.70",
    "recExt1SpeedupLOneMedian": "1.23",
    "recExt1SpeedupLOneMin": "0.14",
    "recExt1SpeedupCellsScored": "12",
    "recExt1SpeedupCellsMeeting": "0",
    "recExt2Cells": "43",
    "recExt2AgreeCells": "43",
    "recExt2Families": "13",
    "recExt2DdVersion": "0.25.1",
    "recExt2Workers": "1",
    "recExt2RefreshMedianMs": "19.0",
    "recExt2RatioRecent": "1.55\\times 10^{-3}",
    "recExt2RatioHours": "1.68\\times 10^{-3}",
    "recExt2RatioDays": "1.31\\times 10^{-3}",
    "recExt2RatioDeep": "7.83\\times 10^{-4}",
    "recExt2CrossoverBand": "none",
    "recExt2WithheldFalseFreshIvm": "39",
    "recExt2WithheldFalseFreshWatermark": "0",
    "recExt2WithheldFalseFreshTgms": "0",
    "recExt2UnanswerableMs": "not measured",
}


def test_frozen_macro_values_match_the_generator():
    """A moved or edited record would change one of these; a coincidental
    self-consistent-but-wrong recomputation would not also match this
    independently-typed table."""
    mod = _load("sys_paper_macros")
    m = _run_all_landed(mod)
    values = {name: value for name, value, _ in m.items}
    for name, expected in FROZEN_LANDED_VALUES.items():
        assert name in values, f"expected macro {name} was not emitted"
        assert values[name] == expected, (
            f"{name}: generator emitted {values[name]!r}, frozen test expects {expected!r}")


def test_pending_macros_raise_a_latex_error_never_a_placeholder_number():
    """add_pending_stubs itself now carries no stubs of its own -- the 3
    LDBC (C9) stubs it used to hold have landed (compute_ldbc_ref_v1), and
    P-SOAK3's recSoakWriterErrorsClassThree has since landed too (see
    writer_error_counts_by_class-3.json, compute_longevity_soak_three). The
    one remaining PENDING macro in the whole generator,
    recB7ScaleCurveP50ReachWindow100M, is added directly by its own
    compute_* function (compute_b7_scale, see
    test_b7_scale_100m_reach_window_p50_is_pending_with_its_estimate), not
    by add_pending_stubs, so this function now emits nothing."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.add_pending_stubs(m)
    assert m.items == []


def test_full_macro_set_has_no_duplicate_names_and_covers_every_skeleton_claim():
    mod = _load("sys_paper_macros")
    m = _run_all_landed(mod)
    mod.add_pending_stubs(m)
    names = [name for name, _, _ in m.items]
    assert len(names) == len(set(names)), "duplicate macro name"
    # 1 pending stub, added directly by its own compute_* function (already
    # present in `m` via _run_all_landed before add_pending_stubs runs,
    # which itself adds nothing now that the 3 LDBC (C9) stubs and
    # P-SOAK3's recSoakWriterErrorsClassThree have all landed as ordinary
    # FROZEN_LANDED_VALUES entries): B7 100M's
    # recB7ScaleCurveP50ReachWindow100M (compute_b7_scale).
    assert len(names) == len(FROZEN_LANDED_VALUES) + 1


def test_cli_check_mode_agrees_with_committed_output(tmp_path):
    """Running the generator twice must be idempotent (--check passes),
    the property the task's frozen-report discipline depends on."""
    env_root = ROOT
    subprocess.run([_venv_python(), str(env_root / "scripts" / "sys_paper_macros.py")],
                    cwd=env_root, check=True, capture_output=True, text=True)
    result = subprocess.run(
        [_venv_python(), str(env_root / "scripts" / "sys_paper_macros.py"), "--check"],
        cwd=env_root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _out_path() -> Path:
    return ROOT / "paper" / "sys" / "generated" / "sys-paper-macros.tex"


def _run_cli(*flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_venv_python(), str(ROOT / "scripts" / "sys_paper_macros.py"), *flags],
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
# sys_paper_macros.py: a tampered record fails loudly
# --------------------------------------------------------------------------

def test_tampered_crash_record_fails_verification(tmp_path):
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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


def test_rec_b1v2_open_component_status_is_a_text_macro_not_a_number(tmp_path):
    """B1-v2's chain-open component split (checkpoint-load vs. delta-replay)
    was never measured -- NativeAdapter() exposes no internal phase timer.
    The macro documenting that must render as the literal string "not
    computed", not a placeholder number that could be mistaken for one."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b1_v2(m)
    values = {name: value for name, value, _ in m.items}
    status = values["recB1v2OpenComponentStatus"]
    assert status == "not computed"
    with pytest.raises(ValueError):
        float(status)


# --------------------------------------------------------------------------
# sys_paper_macros.py: B1-v2e (chain-format correction) macros
# --------------------------------------------------------------------------

def test_b1_v2_treatment_provenance_is_voided_without_changing_values(tmp_path):
    """The 2026-09-15 correction found the v2 A/B's commit-cost treatment
    reps measured a format-2 chain under the format-3 binary. The frozen
    v2 macro *values* must not move (they are what was actually measured,
    including the mistake) -- only their provenance strings gain a void
    notice pointing at recB1v2e*, and every other v2 macro (chain-open,
    B1(a) bytes, the K-sweep manifest-decile-only figures) is untouched."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b1_v2(m)
    before = {name: value for name, value, _ in m.items}
    mod._void_b1_v2_treatment_provenance(m)
    after = {name: (value, provenance) for name, value, provenance in m.items}

    voided = {
        "recB1v2TotalDecileTreatment", "recB1v2ManifestDecileTreatment",
        "recB1v2ManifestDecileK128", "recB1v2ManifestDecileK1024",
        "recB1v2P50TreatmentMs", "recB1v2P50Paired",
    }
    for name in voided:
        value, provenance = after[name]
        assert value == before[name], f"{name}: value must not change"
        assert "VOID AS A FORMAT-3 MEASUREMENT" in provenance
        assert "recB1v2e" in provenance

    untouched = {
        "recB1v2ControlCommit", "recB1v2TreatmentCommit",
        "recB1v2BytesControlMB", "recB1v2BytesTreatmentMB",
        "recB1v2TotalDecileControl", "recB1v2ManifestDecileControl",
        "recB1v2P50ControlMs", "recB1v2OpenControlMs", "recB1v2OpenTreatmentMs",
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
    mod = _load("sys_paper_macros")
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
    confirm the format-evidence macros (`recB1v2eManifestBytesTreatment`/
    `Control`) are still caught -- because they are recomputed per rep and
    cross-checked for internal agreement, not read off the manifest's own
    summary fields."""
    mod = _load("sys_paper_macros")
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


def test_rec_b1v2e_control_open_status_is_a_text_macro_not_a_number():
    """Cell (a)'s control open time was measured under a concurrent phase-2
    backup transfer and is flagged confounded, not a comparable number --
    the macro must render as that literal text, never a placeholder ratio."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b1_v2e(m)
    values = {name: value for name, value, _ in m.items}
    status = values["recB1v2eControlOpenStatus"]
    assert status == "confounded (concurrent backup transfer)"
    with pytest.raises(ValueError):
        float(status)


def test_rec_b1v2e_p50_macros_distinguish_engine_commit_from_wall_clock(tmp_path):
    """`recB1v2eP50*` is the frozen Addendum 3 quantity -- engine-commit
    p50 (`phase_p50_us.total_us`), the same one `recB1v2P50*` already
    tracks -- and `recB1v2eWallP50*`/`recB1v2eWallPaired` is the
    wall-clock `commit_ms.p50` figure (incl. Python-side eventlog append),
    which is *not* the frozen quantity. The two must never collapse to the
    same value or this split has lost its point."""
    mod = _load("sys_paper_macros")
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

    assert float(values["recB1v2eP50TreatmentMs"]) == pytest.approx(engine_trt, abs=0.001)
    assert float(values["recB1v2eP50ControlMs"]) == pytest.approx(engine_ctl, abs=0.001)
    assert float(values["recB1v2eWallP50TreatmentMs"]) == pytest.approx(wall_trt, abs=0.001)
    assert float(values["recB1v2eWallP50ControlMs"]) == pytest.approx(wall_ctl, abs=0.001)

    # the point of the split: these must not be the same number
    assert values["recB1v2eP50TreatmentMs"] != values["recB1v2eWallP50TreatmentMs"]
    assert values["recB1v2eP50ControlMs"] != values["recB1v2eWallP50ControlMs"]
    assert values["recB1v2eP50Paired"] != values["recB1v2eWallPaired"]


def test_tampered_b1_co7_raw_digest_mismatch_fails(tmp_path):
    """Same digest discipline as B1-v2e: the co7 record's result_digest is
    the sha256 of the raw records file's own bytes. Editing the raw file
    without recomputing that digest into the summary manifest must be
    caught before any recB1co7* macro trusts a number out of it."""
    mod = _load("sys_paper_macros")
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
    every recB1co7* component is recomputed as a median of the 3
    ``open_phase_us`` reps and cross-checked against the raw record's own
    ``open_phase_p50_us`` aggregate, not read off that aggregate directly.
    Only the aggregate field is edited here; the reps it should match are
    left alone, so a generator that trusted the aggregate without
    recomputing it would sail through."""
    mod = _load("sys_paper_macros")
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


def test_rec_b1_worst_phase_open_macros_are_arithmetic_on_measured_per_delta_costs():
    """`recB1WorstPhaseOpenTreatmentMs`/`ControlS` are not measurements --
    they project the co7 lane's measured per-delta manifest-chain costs
    forward to K-1=511 deltas (the generation just before the next
    checkpoint reset). Recompute both independently from the emitted
    recB1co7* macro values and confirm the generator's own numbers agree,
    proving the projection is arithmetic on those macros and not a separately
    fabricated figure."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b1_co7(m)
    values = {name: value for name, value, _ in m.items}

    checkpoint_ms = float(values["recB1co7CheckpointReadParseMs"])
    merkle_ms = float(values["recB1co7MerkleVerifyMs"])
    state_build_per_delta_us = float(values["recB1co7StateBuildPerDeltaUs"])
    delta_replay_per_delta_us = float(values["recB1co7DeltaReplayPerDeltaUs"])
    control_per_delta_ms = float(values["recB1co7ControlPerDeltaMs"])

    expected_trt_ms = (
        checkpoint_ms + merkle_ms
        + (state_build_per_delta_us + delta_replay_per_delta_us) * 511 / 1000
    )
    expected_ctl_s = control_per_delta_ms * 511 / 1000

    assert float(values["recB1WorstPhaseOpenTreatmentMs"]) == pytest.approx(
        expected_trt_ms, abs=0.1)
    assert float(values["recB1WorstPhaseOpenControlS"]) == pytest.approx(
        expected_ctl_s, abs=0.1)


def test_rec_b1_worst_phase_open_macros_have_derived_not_measured_provenance():
    """The two projected-cost macros must carry provenance that says
    'derived, not measured' rather than pointing at a raw record field --
    a reader must not mistake this arithmetic for a fourth measurement."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b1_co7(m)
    provenance = {name: prov for name, _, prov in m.items}
    for name in ("recB1WorstPhaseOpenTreatmentMs", "recB1WorstPhaseOpenControlS"):
        assert provenance[name].startswith("derived, not measured"), (
            f"{name}: provenance must flag this as arithmetic, not a measurement, "
            f"got {provenance[name]!r}")


def test_tampered_fault_matrix_record_fails_the_frozen_expectation(tmp_path):
    """Edit F1-9's pre-fix silent-violation count (the campaign's own
    headline finding) and confirm the frozen-value assertion, not just a
    cross-file consistency check, is what catches it."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    """Arithmetic check: recStormV2ProbeSurvivorMedian/PrecisionMedian must
    equal the median, over the probe's own 5 batches, of
    candidate_survivors/n_registered and changed_count/candidate_survivors
    respectively -- recomputed independently here from the committed
    rows.jsonl, not merely re-asserted against the generator's own
    intermediate variables."""
    mod = _load("sys_paper_macros")
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
    assert values["recStormV2ProbeSurvivorMedian"] == f"{expected_survivor_median:.3f}"
    assert values["recStormV2ProbePrecisionMedian"] == f"{expected_precision_median:.3f}"
    assert values["recStormV2ProbeIntersectsMedian"] == mod.tex_num(
        int(expected_intersects_median))
    assert expected_intersects_median <= 50000
    assert values["recStormV2ProbeR18Tripped"] == "no"


def test_tampered_storm_v2_merged_record_digest_mismatch_fails(tmp_path):
    """storm-v2-main-grid-2026-09-15.json's own result_digest is sha256 of
    every row's own (_task_id, result_digest), sorted by task id
    (storm_campaign_merge.py's result_digest()). Editing the merged
    record's result_digest field itself (without touching the rows.jsonl
    it is supposed to summarize) must be caught -- same digest discipline
    as the D160/B1-v2e/ladder tamper tests above, applied to this record's
    own digest scheme."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    recStormV2SpeedupSynthC1None must equal the median, over exactly the
    three seed-0/1/2 cells at (synth-iv-60k, c1, age=none), of
    summary.arms.global-recompute.ttf_p50_ms / summary.arms.tgms-L1.ttf_p50_ms
    -- recomputed independently here from the committed rows.jsonl, not
    trusted from the generator's own arithmetic."""
    mod = _load("sys_paper_macros")
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
    assert values["recStormV2SpeedupSynthC1None"] == f"{expected_median:.3f}"


def test_tampered_storm_v2_records_tarball_sha_mismatch_fails(tmp_path):
    """Lane W2l: storm-v2-records-36-tasks.tar.gz holds the per-batch rows
    the c1 survivor-fraction/precision macros are computed from, never
    committed as individual files. A single flipped byte in the tarball
    must fail the frozen/README-quoted sha256 check before anything inside
    it is trusted, the same house rule already applied to every other
    whole-file digest in this module."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    assert values["recStormV2C1Batches"] == "240"
    assert values["recStormV2SurvivorFractionC1Median"] == f"{expected_survivor_median:.3f}"
    assert values["recStormV2PrecisionC1Median"] == f"{expected_precision_median:.3f}"
    assert (values["recStormV2SurvivorFractionSynthC1Median"]
            == f"{expected_synth_survivor:.3f}")
    assert (values["recStormV2SurvivorFractionCollegeMsgC1Median"]
            == f"{expected_collegemsg_survivor:.3f}")
    assert values["recStormV2PrecisionSynthC1Median"] == f"{expected_synth_precision:.3f}"
    assert (values["recStormV2PrecisionCollegeMsgC1Median"]
            == f"{expected_collegemsg_precision:.3f}")


def test_tampered_d160_rows_digest_mismatch_fails(tmp_path):
    """The record's own manifest carries a sha256 of the rows file; editing
    the rows without updating the manifest must be caught before any metric
    is even computed."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    """recCorruptionBlobMutationAlreadyDetected's own trial count must equal
    its DETECTED count exactly (every one of delete_file's 89 pre-A10 trials
    was DETECTED) for the six-mutation-plus-one partition of the 710 blob
    trials to hold; a single trial flipped away from DETECTED must be
    caught, not averaged into the 89 count as noise."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
    by_life = json.loads(mod.LONGEVITY_WRITER_ERRORS_BY_LIFE.read_text(encoding="utf-8"))
    per_life_errors = by_life["per_life_errors"]
    assert len(per_life_errors) == 42
    assert sum(per_life_errors) == 249
    assert sum(per_life_errors) == by_life["true_total_errors_all_lives"]

    m = mod.Macros()
    mod.compute_longevity_soak(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recSoakWriterErrorsTrue"] == "249"
    assert values["recSoakWriterErrorsManifest"] == "1"


def test_tampered_writer_error_counts_by_life_sum_mismatch_fails(tmp_path):
    """writer_error_counts_by_life.json is the README's own "derived
    locally" file (not in the sha256 table) -- its per-life sum must still
    be recomputed and cross-checked against its own summary field and the
    frozen 249, not trusted verbatim; editing one life's count must be
    caught even though no digest scheme covers this file at all."""
    mod = _load("sys_paper_macros")
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
    """The whole point of recSoakWriterErrorsManifest/True is that they
    *disagree* (1 vs. 249) -- that disagreement is the harness defect the
    README documents. If a future manifest ever reported the true total
    directly (i.e. the defect were fixed upstream), this generator's own
    "they must differ" sanity check should catch the now-stale assumption
    rather than silently emitting two identical numbers as if nothing
    changed."""
    mod = _load("sys_paper_macros")
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
    """recSoakDigestStatus ('not computed') and recSoakReaderDeathCause
    ('reader torn-tail race, pre-fix engine') must render as literal text,
    never a placeholder number that could be mistaken for one -- same
    discipline as recB1v2OpenComponentStatus above. recSoakVerifyHealthy
    is also text ('true'), not the LaTeX-truthy '1'."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak(m)
    values = {name: value for name, value, _ in m.items}

    for name in ("recSoakDigestStatus", "recSoakReaderDeathCause", "recSoakVerifyHealthy"):
        with pytest.raises(ValueError):
            float(values[name])

    assert values["recSoakDigestStatus"] == "not computed"
    assert values["recSoakReaderDeathCause"] == "reader torn-tail race, pre-fix engine"
    assert values["recSoakVerifyHealthy"] == "true"


def test_tampered_longevity_summary_rederived_sha256_mismatch_fails(tmp_path):
    """summary_rederived_2026-09-15.json is not in README.md's original
    "Files here" sha256 table (that table is the original soak's five
    files only) -- this generator freezes its own whole-file sha256 from
    its first read of the committed copy instead. An edited copy (even a
    field this generator never reads) must fail that check before any
    within-life slope statistic is trusted."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    recSoakReplayOomGeneration."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
    readme_text = (mod.LONGEVITY_DIR / "README.md").read_text(encoding="utf-8")
    assert f"sha256 `{mod.LONGEVITY_REPLAY_CHECK_SHA256}`" in readme_text
    assert mod.sha256_file(mod.LONGEVITY_REPLAY_CHECK) == mod.LONGEVITY_REPLAY_CHECK_SHA256


def test_longevity_rederived_bytes_per_commit_is_median_slope_over_mean_rate():
    """recSoakBytesPerCommitLiveKB is arithmetic, not a measurement:
    round(within-life median RSS slope / mean commits-per-s). Recomputed
    independently here from the same two source macros' own underlying
    floats (not the rounded macro strings) and checked against both the
    generator's internal frozen value and the README's own ~157-158
    KB/commit neighborhood."""
    mod = _load("sys_paper_macros")
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
    assert values["recSoakBytesPerCommitLiveKB"] == "157"
    assert values["recSoakCommitsPerSecMean"] == "20.2"


def test_longevity_rederived_oom_rss_gb_is_kb_over_1e6():
    """recSoakReplayOomRssGB converts the dmesg-parsed anon-rss kB figure
    to GB via decimal division by 1e6 (kilo/giga, not kibi/gibi) --
    checked directly against the dmesg line's own literal figure, not the
    macro string."""
    mod = _load("sys_paper_macros")
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
    assert values["recSoakReplayOomRssGB"] == "83.0"


def test_longevity_rederived_replay_outcome_is_a_text_macro_not_a_number():
    """recSoakReplayOutcome ('aborted_oom') must render as literal text,
    same discipline as recSoakDigestStatus above."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    values = {name: value for name, value, _ in m.items}
    with pytest.raises(ValueError):
        float(values["recSoakReplayOutcome"])
    assert values["recSoakReplayOutcome"] == "aborted_oom"


def test_longevity_rederived_first_vs_last_slope_is_labelled_superseded():
    """recSoakFirstVsLastSlopeKBps carries the old two-point figure
    forward unchanged for comparison, but its provenance string must
    flag it as superseded by the within-life figures -- it must never be
    read as this record's own memory-FAIL headline number."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_rederived(m)
    entry = next(item for item in m.items if item[0] == "recSoakFirstVsLastSlopeKBps")
    _, value, provenance = entry
    assert value == "111.6"
    assert "SUPERSEDED" in provenance


def test_longevity_soak_two_true_error_totals_match_frozen_and_manifest():
    """Soak2's own cumulative counters are correct (unlike W2g's
    counter_latest label-collision defect) -- the true per-life writer sum
    (616) and the true OSError+StateError reader sum (150,476,512) must
    both equal the manifest's own error_count arithmetic
    (writer + reader + unexpected_writer_deaths == summary.error_count),
    not just self-consistently recompute to the same wrong number."""
    mod = _load("sys_paper_macros")
    by_life = json.loads(mod.LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO.read_text(encoding="utf-8"))
    true_writer_errors = sum(row["errors"] for row in by_life["per_life"].values())
    assert true_writer_errors == 616
    assert true_writer_errors == by_life["true_total_writer_errors_all_lives"]

    reader_by_class = json.loads(
        mod.LONGEVITY_READER_ERRORS_BY_CLASS_TWO.read_text(encoding="utf-8"))
    true_reader_errors = int(reader_by_class["by_class_total"]["OSError"]) + \
        int(reader_by_class["by_class_total"]["StateError"])
    assert true_reader_errors == 150_476_512

    manifest = json.loads(mod.LONGEVITY_MANIFEST_TWO.read_text(encoding="utf-8"))
    assert manifest["summary"]["error_count"] == true_writer_errors + true_reader_errors

    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recSoakWriterErrorsTrueTwo"] == "616"
    assert values["recSoakReaderErrorsTrueTwo"] == "150{,}476{,}512"


def test_longevity_soak_two_text_macros_are_never_numbers():
    """recSoakDigestEqualTwo ('true'), recSoakFullVerifyVerdictTwo
    ('healthy'), and recSoakWriterErrorsClassTwo ('NotFoundError') must
    all render as literal text, same discipline as W2g's
    recSoakDigestStatus/recSoakReaderDeathCause above."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    values = {name: value for name, value, _ in m.items}
    for name in ("recSoakDigestEqualTwo", "recSoakFullVerifyVerdictTwo",
                 "recSoakWriterErrorsClassTwo"):
        with pytest.raises(ValueError):
            float(values[name])
    assert values["recSoakDigestEqualTwo"] == "true"
    assert values["recSoakFullVerifyVerdictTwo"] == "healthy"
    assert values["recSoakWriterErrorsClassTwo"] == "NotFoundError"


def test_tampered_longevity_manifest_two_sha256_mismatch_fails(tmp_path):
    """longevity-synth-1m-native-1.json is in README.md's "Soak 2
    (post-fix)" Files-added-here sha256 table -- an edited copy (even a
    field this generator never reads) must fail before any commit/
    duration/error-count figure is trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_MANIFEST_TWO.read_text(encoding="utf-8"))
    data["summary"]["compactions"] = 999999
    tampered = tmp_path / "longevity-synth-1m-native-1.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_MANIFEST_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "an edited longevity-synth-1m-native-1.json must fail the " \
        "sha256 check against README.md's Files-added-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_rss_slopes_two_sha256_mismatch_fails(tmp_path):
    """rss_slopes-2.json is also in that sha256 table -- an edited copy
    must fail before any within-life writer/reader slope macro is
    trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_RSS_SLOPES_TWO.read_text(encoding="utf-8"))
    data["writer_lives"][0]["slope_kb_per_s_least_squares"] = 0.0
    tampered = tmp_path / "rss_slopes-2.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_RSS_SLOPES_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "an edited rss_slopes-2.json must fail the sha256 check"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_writer_error_counts_by_life_two_sum_mismatch_fails_even_with_patched_digest(tmp_path):
    """Unlike W2g's un-hashed writer_error_counts_by_life.json,
    writer_error_counts_by_life-2.json *is* in the sha256 table -- so
    patch the frozen digest to isolate the internal recomputed-sum
    cross-check (sum(per_life[*].errors) ==
    true_total_writer_errors_all_lives) from the whole-file check."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO.read_text(encoding="utf-8"))
    data["per_life"]["0"]["errors"] += 1
    tampered = tmp_path / "writer_error_counts_by_life-2.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    mod.LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO_SHA256 = hashlib.sha256(
        tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "a per-life error count that no longer sums to " \
        "true_total_writer_errors_all_lives must fail, even with a patched whole-file digest"
    assert any("true_total_writer_errors_all_lives" in f for f in mod.FAILURES)


def test_tampered_reader_error_counts_by_class_two_total_mismatch_fails_even_with_patched_digest(tmp_path):
    """reader_error_counts_by_class-2.json's own total_reader_errors_total
    field must equal the recomputed OSError + StateError sum -- editing
    one class total alone, with the whole-file digest patched to match,
    must still fail this internal cross-check."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_READER_ERRORS_BY_CLASS_TWO.read_text(encoding="utf-8"))
    data["by_class_total"]["OSError"] += 1
    tampered = tmp_path / "reader_error_counts_by_class-2.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    mod.LONGEVITY_READER_ERRORS_BY_CLASS_TWO_SHA256 = hashlib.sha256(
        tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_READER_ERRORS_BY_CLASS_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "an OSError total that no longer matches " \
        "total_reader_errors_total must fail, even with a patched whole-file digest"
    assert any("total_reader_errors_total" in f for f in mod.FAILURES)


def test_tampered_longevity_soak_two_digest_equal_false_fails_even_with_patched_digest(tmp_path):
    """The whole point of recSoakDigestEqualTwo is that P-SOAK2's replay
    actually completed and matched (True, unlike W2g's null) -- a
    manifest reporting False must fail this generator's own require()
    check rather than silently emitting a wrong 'true' macro."""
    mod = _load("sys_paper_macros")
    manifest = json.loads(mod.LONGEVITY_MANIFEST_TWO.read_text(encoding="utf-8"))
    manifest["summary"]["digest_equal"] = False
    tampered = tmp_path / "longevity-synth-1m-native-1.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")
    mod.LONGEVITY_MANIFEST_TWO_SHA256 = hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_MANIFEST_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "summary.digest_equal == False must fail the generator's own " \
        "require(... is True) check, even with a patched whole-file digest"
    assert any("digest_equal" in f for f in mod.FAILURES)


def test_tampered_verify_full_soak2_overlap_finding_fails_even_with_patched_digest(tmp_path):
    """verify-full-soak2-2026-09-17.txt's clean, 0-overlap verdict is the
    record's second strong positive signal (alongside digest_equal) --
    injecting a fabricated PROBLEMS/believed-versions-overlap finding,
    with the whole-file digest patched to match, must still fail the
    overlap-count and no-overlap-text checks."""
    mod = _load("sys_paper_macros")
    text = mod.LONGEVITY_VERIFY_FULL_TWO.read_text(encoding="utf-8")
    tampered_text = text.replace(
        "\nverdict: healthy",
        "\nPROBLEMS (1):\n  - [row/believed-versions-overlap]: fabricated for this test"
        "\n\nverdict: healthy")
    tampered = tmp_path / "verify-full-soak2-2026-09-17.txt"
    tampered.write_text(tampered_text, encoding="utf-8")
    mod.LONGEVITY_VERIFY_FULL_TWO_SHA256 = hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_VERIFY_FULL_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "a fabricated believed-versions-overlap finding must fail, " \
        "even with a patched whole-file digest"
    assert any("PROBLEMS" in f or "believed-versions-overlap" in f for f in mod.FAILURES)


def test_tampered_longevity_compactions_two_sha256_mismatch_fails(tmp_path):
    """compactions-2.jsonl (appended 2026-09-18) is in README.md's Soak 2
    Files-added-here table -- an edited copy (even a line the generator
    never re-derives from directly) must fail before the reader-onset
    edge-row macros are trusted."""
    mod = _load("sys_paper_macros")
    lines = mod.LONGEVITY_COMPACTIONS_TWO.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"edge_rows": 1000376', '"edge_rows": 999999')
    tampered = tmp_path / "compactions-2.jsonl"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mod.LONGEVITY_COMPACTIONS_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "an edited compactions-2.jsonl must fail the sha256 check " \
        "against README.md's Files-added-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_reader_onset_rows_two_sha256_mismatch_fails(tmp_path):
    """reader_onset_rows-2.json (appended 2026-09-18) is also in that
    sha256 table -- an edited copy must fail before either
    recSoakReaderOnset*Two macro is trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_READER_ONSET_ROWS_TWO.read_text(encoding="utf-8"))
    data["earliest_reader_onset"]["last_compaction_before"]["edge_rows"] = 1
    tampered = tmp_path / "reader_onset_rows-2.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_READER_ONSET_ROWS_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "an edited reader_onset_rows-2.json must fail the sha256 check"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_reader_onset_rows_two_wrong_reader_fails_even_with_patched_digest(tmp_path):
    """reader_onset_rows-2.json's earliest/latest onset must actually be the
    minimum/maximum onset_t_plus_s across all 8 readers in
    reader_error_counts_by_class-2.json's own osrror_storm_onset_by_reader --
    swapping in a reader that is not the true extremum, with the whole-file
    digest patched to match, must still fail that cross-check."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_READER_ONSET_ROWS_TWO.read_text(encoding="utf-8"))
    data["earliest_reader_onset"]["reader"] = 4
    data["earliest_reader_onset"]["onset_t_plus_s"] = 75993.1
    tampered = tmp_path / "reader_onset_rows-2.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    mod.LONGEVITY_READER_ONSET_ROWS_TWO_SHA256 = hashlib.sha256(
        tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_READER_ONSET_ROWS_TWO = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_two(m)
    assert mod.FAILURES, "an earliest_reader_onset that is not the true minimum " \
        "onset_t_plus_s across all 8 readers must fail, even with a patched whole-file digest"
    assert any("minimum onset_t_plus_s" in f for f in mod.FAILURES)


def test_tampered_longevity_verify_full_sha256_mismatch_fails(tmp_path):
    """verify-full-2026-09-15.txt has no README-quoted sha256 (it predates
    the soak's original Files-here table), so it is frozen here from this
    lane's own first read -- an edited copy must fail that check before
    either entry's PROBLEMS/generation is even parsed."""
    mod = _load("sys_paper_macros")
    text = mod.LONGEVITY_VERIFY_FULL.read_text(encoding="utf-8")
    tampered = tmp_path / "verify-full-2026-09-15.txt"
    tampered.write_text(text + "\n# tampered\n", encoding="utf-8")

    mod.LONGEVITY_VERIFY_FULL = tampered
    m = mod.Macros()
    mod.compute_longevity_verify_and_replay2(m)
    assert mod.FAILURES, "an edited verify-full-2026-09-15.txt must fail this " \
        "generator's own frozen sha256 check"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_verify_full_overlap_counts_diverge_fails_even_with_patched_sha256(tmp_path):
    """The whole point of landing both entries is that REPLAY-2's replayed
    store finds the *identical* 13,714 overlaps as the original store --
    inflating the second entry's PROBLEMS(N) header (and one matching
    bullet, so the recomputed bullet count still agrees with its own
    header) must still fail the identical-count cross-check, even with a
    patched whole-file digest."""
    mod = _load("sys_paper_macros")
    text = mod.LONGEVITY_VERIFY_FULL.read_text(encoding="utf-8")
    entries = text.split("store:      ")
    assert len(entries) == 3, "expected exactly two `store:` blocks in this fixture"
    second = "store:      " + entries[2]
    tampered_second = second.replace(
        "PROBLEMS (13714):\n",
        "PROBLEMS (13715):\n"
        "  - [row/believed-versions-overlap]: fabricated for this test\n",
        1)
    assert tampered_second != second
    tampered_text = entries[0] + "store:      " + entries[1] + tampered_second
    tampered = tmp_path / "verify-full-2026-09-15.txt"
    tampered.write_text(tampered_text, encoding="utf-8")
    mod.LONGEVITY_VERIFY_FULL_SHA256 = hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_VERIFY_FULL = tampered
    m = mod.Macros()
    mod.compute_longevity_verify_and_replay2(m)
    assert mod.FAILURES, "a REPLAY-2 overlap count that diverges from the original " \
        "store's must fail, even with a patched whole-file digest"
    assert any("identical" in f for f in mod.FAILURES)


def test_tampered_longevity_replay_check_2_sha256_mismatch_fails(tmp_path):
    """replay-check-2-2026-09.json has no README-quoted sha256 either
    (README.md quotes the digests inside it, not the file's own hash), so
    it too is frozen from this lane's own first read."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_REPLAY_CHECK_2.read_text(encoding="utf-8"))
    data["summary"]["peak_rss_kb"] = 1
    tampered = tmp_path / "replay-check-2-2026-09.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_REPLAY_CHECK_2 = tampered
    m = mod.Macros()
    mod.compute_longevity_verify_and_replay2(m)
    assert mod.FAILURES, "an edited replay-check-2-2026-09.json must fail this " \
        "generator's own frozen sha256 check"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_replay_check_2_digest_equal_false_fails_even_with_patched_sha256(tmp_path):
    """REPLAY-2's whole point is that the replayed store's digest equalled
    the soak's pre-registered final_digest -- a manifest reporting
    digest_equal=False must fail, even with a patched whole-file digest,
    rather than silently emitting a wrong 'true' macro."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_REPLAY_CHECK_2.read_text(encoding="utf-8"))
    data["summary"]["digest_equal"] = False
    tampered = tmp_path / "replay-check-2-2026-09.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    mod.LONGEVITY_REPLAY_CHECK_2_SHA256 = hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_REPLAY_CHECK_2 = tampered
    m = mod.Macros()
    mod.compute_longevity_verify_and_replay2(m)
    assert mod.FAILURES, "summary.digest_equal == False must fail the generator's own " \
        "require(... is True) check, even with a patched whole-file digest"
    assert any("digest_equal" in f for f in mod.FAILURES)


def test_longevity_soak_hunt_text_macros_are_never_numbers():
    """recSoakDigestEqualHunt/recSoakVerifyHealthyHunt ('true'),
    recSoakWriterErrorsClassHunt ('NotFoundError'),
    recSoakReaderOpErrorCaptureCommitHunt ('c3a5592'), and
    recSoakHuntPatternReproduced ('false') must render as those exact
    strings, not get coerced through int()/float() anywhere upstream."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES == []
    values = {name: value for name, value, _ in m.items}
    for name in ("recSoakDigestEqualHunt", "recSoakVerifyHealthyHunt",
                 "recSoakWriterErrorsClassHunt", "recSoakReaderOpErrorCaptureCommitHunt",
                 "recSoakHuntPatternReproduced"):
        with pytest.raises(ValueError):
            float(values[name])
    assert values["recSoakDigestEqualHunt"] == "true"
    assert values["recSoakVerifyHealthyHunt"] == "true"
    assert values["recSoakWriterErrorsClassHunt"] == "NotFoundError"
    assert values["recSoakReaderOpErrorCaptureCommitHunt"] == "c3a5592"
    assert values["recSoakHuntPatternReproduced"] == "false"


def test_longevity_soak_hunt_pattern_reproduced_quotes_readme_clause_e():
    """recSoakHuntPatternReproduced's provenance must quote README.md's
    own clause (e) outcome sentence verbatim (not a paraphrase), and the
    macro's own value must be the literal string 'false'."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES == []
    by_name = {name: (value, provenance) for name, value, provenance in m.items}
    value, provenance = by_name["recSoakHuntPatternReproduced"]
    assert value == "false"
    assert "not reproduced within 6 h at 1.40M edge rows" in provenance
    assert "not evidence that it is gone" in provenance


def test_longevity_soak_hunt_reader_slopes_all_exceed_frozen_bound():
    """Unlike P-SOAK2 (every reader passes the 10 kB/s bound), every
    P-STORM-HUNT reader EXCEEDS it -- min/max must both be > 10, and the
    generator's own require() must have checked this, not just recomputed
    a min/max that happens to be consistent with it."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES == []
    values = {name: value for name, value, _ in m.items}
    assert float(values["recSoakReaderWithinLifeSlopeMinKBpsHunt"]) > 10.0
    assert float(values["recSoakReaderWithinLifeSlopeMaxKBpsHunt"]) > 10.0


def test_longevity_soak_hunt_host_load_min_max_exclude_post_run_sample():
    """host_load-stormhunt.log carries one further sample (06:01:21Z, load
    3.56) taken after the run's own --duration 6h clock elapsed, during
    the unscored post-run verify/replay step. The frozen min/max
    (10.79/48.95) must come from the 7 in-window samples only -- if the
    window boundary were computed wrong (or dropped) and the 3.56 sample
    leaked in, the min would come out 3.56, not 10.79."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES == []
    values = {name: value for name, value, _ in m.items}
    assert values["recSoakHostLoadMinHunt"] == "10.79"
    assert values["recSoakHostLoadMaxHunt"] == "48.95"


def test_tampered_longevity_manifest_hunt_sha256_mismatch_fails(tmp_path):
    """stormhunt-2026-09-17.json is in README.md's "P-STORM-HUNT (6 h
    observation run, 2026-09-17/18)" Files-added-here sha256 table -- an
    edited copy (even a field this generator never reads) must fail
    before any commit/duration/error-count figure is trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_MANIFEST_HUNT.read_text(encoding="utf-8"))
    data["summary"]["compactions"] = 999999
    tampered = tmp_path / "stormhunt-2026-09-17.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_MANIFEST_HUNT = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES, "an edited stormhunt-2026-09-17.json must fail the sha256 " \
        "check against README.md's P-STORM-HUNT Files-added-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_rss_slopes_hunt_sha256_mismatch_fails(tmp_path):
    """rss_slopes-stormhunt.json is also in that sha256 table -- an edited
    copy must fail even though every value it carries (writer/reader
    slopes, frozen bounds) would otherwise recompute self-consistently."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_RSS_SLOPES_HUNT.read_text(encoding="utf-8"))
    data["readers"]["0"]["slope_kb_per_s_least_squares"] = 1.0
    tampered = tmp_path / "rss_slopes-stormhunt.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_RSS_SLOPES_HUNT = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES, "an edited rss_slopes-stormhunt.json must fail the sha256 " \
        "check against README.md's P-STORM-HUNT Files-added-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_soak_hunt_digest_equal_false_fails_even_with_patched_readme_sha256(tmp_path):
    """A manifest reporting digest_equal=False must fail the generator's
    own require(... is True) check, even with a patched copy of
    README.md's own sha256 table that matches the tampered file, rather
    than silently emitting a wrong 'true' macro."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_MANIFEST_HUNT.read_text(encoding="utf-8"))
    data["summary"]["digest_equal"] = False
    tampered_manifest = tmp_path / "stormhunt-2026-09-17.json"
    tampered_manifest.write_text(json.dumps(data), encoding="utf-8")
    new_hash = hashlib.sha256(tampered_manifest.read_bytes()).hexdigest()

    readme_text = mod.LONGEVITY_README.read_text(encoding="utf-8")
    old_row = ("| `stormhunt-2026-09-17.json` | "
               "`74ba7c15651bc6cd04895a3deb4b25dfd61a93d5c0d123ffb6923679e2fda661` |")
    assert old_row in readme_text, "the literal table row to patch was not found"
    patched_readme_text = readme_text.replace(
        old_row, f"| `stormhunt-2026-09-17.json` | `{new_hash}` |")
    tampered_readme = tmp_path / "README.md"
    tampered_readme.write_text(patched_readme_text, encoding="utf-8")

    mod.LONGEVITY_README = tampered_readme
    mod.LONGEVITY_MANIFEST_HUNT = tampered_manifest
    m = mod.Macros()
    mod.compute_longevity_soak_hunt(m)
    assert mod.FAILURES, "summary.digest_equal == False must fail the generator's own " \
        "require(... is True) check, even with a patched README sha256 table"
    assert any("digest_equal" in f for f in mod.FAILURES)


def test_longevity_soak_three_reader_episode_reconstruction_matches_frozen():
    """P-SOAK3's D-088 ledger records only 17 first-onset lines (Soak2's
    monotonic-storm ledger convention), but the true reconstructed episode
    count -- sum(active_run_count) over all 8 readers x 2 classes in
    reader_op_error-3.json's healed_at_next_reopen_evidence -- is 662, one
    to two orders of magnitude larger, and every combination healed at
    least once. This must not be confused with reader_op_error-3.json's
    own top-level total_episodes field, which (per that file's own method
    field) is just a second name for the same 17 ledger-onset count, not
    the reconstructed 662."""
    mod = _load("sys_paper_macros")
    doc = json.loads(mod.LONGEVITY_READER_OP_ERROR_THREE.read_text(encoding="utf-8"))
    assert doc["total_episodes"] == doc["n_ledger_reader_op_error_events"] == 17
    true_episode_count = sum(
        v["active_run_count"] for v in doc["healed_at_next_reopen_evidence"].values())
    assert true_episode_count == 662
    assert true_episode_count != doc["total_episodes"]

    m = mod.Macros()
    mod.compute_longevity_soak_three(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recSoakReaderOpErrorEventsThree"] == "17"
    assert values["recSoakReaderErrorEpisodesThree"] == "662"
    assert values["recSoakReaderErrorLongestHealedIntervalSThree"] == "37272.1"


def test_longevity_soak_three_writer_errors_class_is_landed():
    """Unlike the earlier state of this lane, P-SOAK3 now has a committed
    per-life/per-class writer-error side-file
    (writer_error_counts_by_class-3.json, built read-only from xzgpu's
    longevity_ledger.jsonl), so the exception-class label README.md (h)
    states (100% NotFoundError) is recomputed from it and lands as an
    ordinary text macro -- no PENDING stub, same discipline as P-SOAK2's
    recSoakWriterErrorsClassTwo and P-STORM-HUNT's
    recSoakWriterErrorsClassHunt. The per-life breakdown (174/125/86/97/
    132/90/62/36/52, lives 0-8) and the landed writer-error total (854)
    are cross-checked against the same file."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_three(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recSoakWriterErrorsClassThree"] == "NotFoundError"
    assert values["recSoakWriterErrorsTrueThree"] == "854"

    by_class_doc = json.loads(mod.LONGEVITY_WRITER_ERRORS_BY_CLASS_THREE.read_text(
        encoding="utf-8"))
    assert by_class_doc["total_writer_errors"] == 854
    assert by_class_doc["writer_by_class"] == {"NotFoundError": 854}
    per_life = by_class_doc["per_life"]
    assert [per_life[str(i)]["errors"] for i in range(9)] == [
        174, 125, 86, 97, 132, 90, 62, 36, 52]


def test_longevity_soak_three_text_macros_are_never_numbers():
    """recSoakCommitThree ('9e21a83'), recSoakDigestEqualThree ('true')
    and recSoakWriterErrorsClassThree ('NotFoundError') must all render
    as literal text, same discipline as P-SOAK2/P-STORM-HUNT's
    equivalents above."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_longevity_soak_three(m)
    values = {name: value for name, value, _ in m.items}
    for name in ("recSoakCommitThree", "recSoakDigestEqualThree",
                 "recSoakWriterErrorsClassThree"):
        with pytest.raises(ValueError):
            float(values[name])
    assert values["recSoakCommitThree"] == "9e21a83"
    assert values["recSoakDigestEqualThree"] == "true"
    assert values["recSoakWriterErrorsClassThree"] == "NotFoundError"


def test_tampered_longevity_manifest_three_sha256_mismatch_fails(tmp_path):
    """longevity-synth-1m-native-2.json is in README.md's "Soak 3 (72 h)"
    Files-added-here sha256 table -- an edited copy (even a field this
    generator never reads) must fail before any commit/duration/error-count
    figure is trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LONGEVITY_MANIFEST_THREE.read_text(encoding="utf-8"))
    data["summary"]["compactions"] = 999999
    tampered = tmp_path / "longevity-synth-1m-native-2.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LONGEVITY_MANIFEST_THREE = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_three(m)
    assert mod.FAILURES, "an edited longevity-synth-1m-native-2.json must fail the " \
        "sha256 check against README.md's Soak 3 Files-added-here table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_longevity_soak_three_digest_equal_false_fails_even_with_patched_digest(tmp_path):
    """A manifest reporting digest_equal=False must fail the generator's
    own require(... is True) check, even with a patched whole-file digest,
    rather than silently emitting a wrong 'true' macro."""
    mod = _load("sys_paper_macros")
    manifest = json.loads(mod.LONGEVITY_MANIFEST_THREE.read_text(encoding="utf-8"))
    manifest["summary"]["digest_equal"] = False
    tampered = tmp_path / "longevity-synth-1m-native-2.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")
    mod.LONGEVITY_MANIFEST_THREE_SHA256 = hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_MANIFEST_THREE = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_three(m)
    assert mod.FAILURES, "summary.digest_equal == False must fail the generator's own " \
        "require(... is True) check, even with a patched whole-file digest"
    assert any("digest_equal" in f for f in mod.FAILURES)


def test_tampered_verify_full_three_overlap_finding_fails_even_with_patched_digest(tmp_path):
    """verify-full-3.txt's clean, 0-overlap verdict is one of this record's
    strong positive signals (alongside digest_equal) -- injecting a
    fabricated PROBLEMS/believed-versions-overlap finding, with the
    whole-file digest patched to match, must still fail the overlap-count
    and no-overlap-text checks."""
    mod = _load("sys_paper_macros")
    text = mod.LONGEVITY_VERIFY_FULL_THREE.read_text(encoding="utf-8")
    tampered_text = text.replace(
        "\nverdict: healthy",
        "\nPROBLEMS (1):\n  - [row/believed-versions-overlap]: fabricated for this test"
        "\n\nverdict: healthy")
    tampered = tmp_path / "verify-full-3.txt"
    tampered.write_text(tampered_text, encoding="utf-8")
    mod.LONGEVITY_VERIFY_FULL_THREE_SHA256 = hashlib.sha256(tampered.read_bytes()).hexdigest()

    mod.LONGEVITY_VERIFY_FULL_THREE = tampered
    m = mod.Macros()
    mod.compute_longevity_soak_three(m)
    assert mod.FAILURES, "a fabricated believed-versions-overlap finding must fail, " \
        "even with a patched whole-file digest"
    assert any("PROBLEMS" in f or "believed-versions-overlap" in f for f in mod.FAILURES)


def test_tampered_overload_rep1_sha256_not_in_sums_fails(tmp_path):
    """Every overload-v1 record this script reads is checked by sha256
    membership against benchmarks/overload-v1/SHA256SUMS (that file's own
    rep1 entry is misnamed, so membership rather than a filename-keyed
    lookup is the right check) -- an edited copy's hash must not be in
    that set."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.OVERLOAD_REP1.read_text(encoding="utf-8"))
    data["config"]["max_concurrent"] = 999
    tampered = tmp_path / "overload-2026-09-15.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.OVERLOAD_REP1 = tampered
    m = mod.Macros()
    mod.compute_overload(m)
    assert mod.FAILURES, "an edited overload-2026-09-15.json must have a sha256 that is " \
        "not in SHA256SUMS's set of digests"
    assert any("SHA256SUMS" in f for f in mod.FAILURES)


def test_tampered_overload_refusal_stage_not_concurrency_only_fails_even_with_sums_patched(tmp_path):
    """README.md's \"cap-only refusals\" claim (0 result-size, 0 operator
    errors) rests on every n=64 refusal carrying refusal_stage==\"limit\"
    -- relabelling one must fail that recomputed-set check, even once the
    tampered file's hash is added to a patched copy of SHA256SUMS."""
    mod = _load("sys_paper_macros")
    records = json.loads(mod.OVERLOAD_REP1_RECORDS.read_text(encoding="utf-8"))
    step64 = next(s for s in records if s["n_clients"] == 64)
    relabelled = False
    for r in step64["records"]:
        if r["outcome"] == "refused":
            r["refusal_stage"] = "result_size"
            relabelled = True
            break
    assert relabelled, "expected at least one refused record in the n=64 step"
    tampered = tmp_path / "overload-2026-09-15.records.json"
    tampered.write_text(json.dumps(records), encoding="utf-8")
    tampered_sums = (mod.OVERLOAD_SHA256SUMS.read_text(encoding="utf-8") +
                      f"\n{hashlib.sha256(tampered.read_bytes()).hexdigest()}  "
                      "overload-2026-09-15.records.json\n")
    tampered_sums_path = tmp_path / "SHA256SUMS"
    tampered_sums_path.write_text(tampered_sums, encoding="utf-8")

    mod.OVERLOAD_REP1_RECORDS = tampered
    mod.OVERLOAD_SHA256SUMS = tampered_sums_path
    m = mod.Macros()
    mod.compute_overload(m)
    assert mod.FAILURES, "a refusal_stage other than \"limit\" must fail the " \
        "concurrency-cap-only recomputed-set check, even with a patched SHA256SUMS"
    assert any("limit" in f for f in mod.FAILURES)


def test_c10_live_osv_macros_match_frozen_values():
    """recLiveDays/recLiveAdvisories/recLiveCorrections, the C10 macros
    resolved by benchmarks/live-osv-v1/snapshot-2026-09-16.json (Lane
    C10-snap's first committed live-osv record snapshot)."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_c10_live_osv(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recLiveDays"] == "1.89"
    assert values["recLiveAdvisories"] == "32{,}827"
    assert values["recLiveCorrections"] == "1"


def test_tampered_live_osv_snapshot_sha256_mismatch_fails(tmp_path):
    """An edited copy of snapshot-2026-09-16.json (even a field this
    generator never otherwise reads) must fail the whole-file sha256
    check against README.md's own quoted value before any advisory/
    correction/days-of-operation figure is trusted."""
    mod = _load("sys_paper_macros")
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
    wrong recLiveCorrections."""
    mod = _load("sys_paper_macros")
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
    emitting a wrong recLiveAdvisories."""
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
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
    mod = _load("sys_paper_macros")
    readme_text = (mod.LIVE_OSV_DIR / "README.md").read_text(encoding="utf-8")
    assert f"`{mod.LIVE_OSV_SNAPSHOT_SHA256}`" in readme_text
    assert mod.sha256_file(mod.LIVE_OSV_SNAPSHOT) == mod.LIVE_OSV_SNAPSHOT_SHA256


# --------------------------------------------------------------------------
# B7 -- scale campaign (Stage 0 iTiger calibration + Stage 1 30M)
# --------------------------------------------------------------------------

def test_b7_scale_macros_match_frozen_values():
    """The 30M/Stage-0 macros compute_b7_scale emits, checked against the
    values benchmarks/scale-v1/README.md and itiger-calib-2026-09.README.md
    already quote in prose."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b7_scale(m)
    values = {name: value for name, value, _ in m.items}

    assert values["recB7BuildWall30M"] == "3786.004"
    assert values["recB7PeakRSS30M"] == "59.31"
    assert values["recB7VersionHistoryWall30M"] == "5.496"
    assert values["recB7VersionHistoryRSS30M"] == "3.863"
    assert values["recB7ManifestBytes30M"] == "175{,}244"
    assert values["recB7SegmentBytes30M"] == "1.549"
    assert values["recB7CheckFullWall30M"] == "98.673"
    assert values["recB7Recovery30M"] == "14{,}618.6"
    assert values["recB7RecoveryCe5000At30M"] == "2656.9"
    assert values["recB7ReachWindowRefused30M"] == "false"
    assert values["recB7QueryFloor30M"] == "6.81"
    assert values["recB7BuildSteadyDecileMedian30M"] == "8463.0"
    assert values["recB7ReachWindowEstimateMs30M"] == "4371"
    assert values["recB7300MGate"] == "false"
    assert values["recB7Calib10MBuildWall"] == "865.592"
    assert values["recB7Calib10MPeakRSS"] == "19.90"
    assert values["recB7Calib10MSteadyOps"] == "24{,}390.8"
    assert values["recB7KBuild"] == "4.602"
    assert values["recB7KRecover"] == "0.834"
    assert values["recB7Calib1MBuildWall"] == "78.339"
    assert values["recB7Calib1MPeakRSS"] == "2.37"
    assert values["recB7Calib1MRecovery"] == "75.86"
    assert values["recB7Calib1MSegmentBytes"] == "0.050"
    assert "recB7Calib10MRecovery" not in values, (
        "itiger-calib-10m.json carries no recovery field -- no such macro should exist")

    # the 10M peak-RSS macro must be the record field's own kB / 1e6, never
    # the calibration README's mixed-unit-slip prose figure (19.43 GB)
    calib10m = json.loads(mod.B7_ITIGER_CALIB_10M.read_text(encoding="utf-8"))
    calib10m_peak_kb = calib10m["build_info"]["peak_rss"]["vmhwm"]
    assert values["recB7Calib10MPeakRSS"] == f"{calib10m_peak_kb / 1e6:.2f}"

    # every one of the 13 scale-curve operators landed, all 30M
    for frag in mod.B7_SCALE_CURVE_OPS.values():
        assert f"recB7ScaleCurveP50{frag}30M" in values
    assert values["recB7ScaleCurveP50HistSingle30M"] == "0.977"
    assert values["recB7ScaleCurveP50ReachWindow30M"] == "2354.494"
    assert values["recB7ScaleCurveP50DiffGlobal30M"] == "7505.374"


def test_tampered_b7_build_30m_sha256_mismatch_fails(tmp_path):
    """An edited copy of build-30m.json must fail the whole-file sha256
    check against benchmarks/scale-v1/README.md's own table before any
    wall/RSS/byte figure inside it is trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.B7_BUILD_30M.read_text(encoding="utf-8"))
    data["build_info"]["wall_s"] = 1.0
    tampered = tmp_path / "build-30m.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.B7_BUILD_30M = tampered
    m = mod.Macros()
    mod.compute_b7_scale(m)
    assert mod.FAILURES, "an edited build-30m.json must fail the sha256 check " \
        "against README.md's own sha256 table"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def test_tampered_b7_scale_curve_30m_operator_p50_mismatch_fails_even_with_patched_digest(tmp_path):
    """per_operator_p50_ms.<op>.clean_213174_p50_ms must equal the raw
    per-rep file's own p50_ms for that operator -- editing the aggregated
    summary's figure alone, with the whole-file sha256 patched to match,
    must still fail rather than silently emitting a wrong scale-curve
    macro."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.B7_SCALE_CURVE_30M.read_text(encoding="utf-8"))
    data["per_operator_p50_ms"]["hist.single"]["clean_213174_p50_ms"] = 999.0
    tampered = tmp_path / "scale-curve-30m.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    readme_text = mod.B7_README.read_text(encoding="utf-8")
    # keep the sha256 table's entry pointed at the tampered copy so this
    # test isolates the row-level check, not the whole-file digest check
    patched_sha = _hashlib.sha256(tampered.read_bytes()).hexdigest()
    readme_patched = tmp_path / "README.md"
    readme_patched.write_text(
        readme_text.replace(
            "`48bd1d13c17dd07a24993a3479db4c937e84a7c2e0b66d4108a148307ceda381`",
            f"`{patched_sha}`"),
        encoding="utf-8")

    mod.B7_SCALE_CURVE_30M = tampered
    mod.B7_README = readme_patched
    m = mod.Macros()
    mod.compute_b7_scale(m)
    assert mod.FAILURES, "a clean_213174_p50_ms that no longer matches the raw file's " \
        "own p50_ms for that operator must fail, even with a patched whole-file digest"
    assert any("hist.single" in f for f in mod.FAILURES)


def test_tampered_b7_recovery_30m_digest_mismatch_fails(tmp_path):
    """recovery-30m.json's digest_compare.{src,replayed}_digest must equal
    the 30M store's frozen content digest -- editing one, with the
    whole-file sha256 patched to match, must still fail."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.B7_RECOVERY_30M.read_text(encoding="utf-8"))
    data["digest_compare"]["replayed_digest"] = "0" * 64
    tampered = tmp_path / "recovery-30m.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    readme_text = mod.B7_README.read_text(encoding="utf-8")
    patched_sha = _hashlib.sha256(tampered.read_bytes()).hexdigest()
    readme_patched = tmp_path / "README.md"
    readme_patched.write_text(
        readme_text.replace(
            "`4c2f08c89a1cbf050881e3678410b19e4e181af70f2728f2ae0b788779847a5b`",
            f"`{patched_sha}`"),
        encoding="utf-8")

    mod.B7_RECOVERY_30M = tampered
    mod.B7_README = readme_patched
    m = mod.Macros()
    mod.compute_b7_scale(m)
    assert mod.FAILURES, "a replayed_digest that no longer matches the frozen 30M store " \
        "digest must fail, even with a patched whole-file digest"
    assert any("replayed_digest" in f for f in mod.FAILURES)


def test_b7_scale_100m_macros_are_landed():
    """Every core recB7*100M name, the 6 executed scale-curve operators,
    the 6 recB7Refused<Op>100M refusal macros, and
    recB7CompactionShare100M must be landed (not PENDING) now that
    build-100m.json + sidecars are on main."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b7_scale(m)
    values = {name: value for name, value, _ in m.items}

    core_100m = ["recB7BuildWall100M", "recB7PeakRSS100M",
                 "recB7VersionHistoryWall100M", "recB7VersionHistoryRSS100M",
                 "recB7ManifestBytes100M", "recB7SegmentBytes100M",
                 "recB7CheckFullWall100M", "recB7RecoveryCe5000At100M",
                 "recB7Recovery100M", "recB7ReachWindowRefused100M",
                 "recB7QueryFloor100M", "recB7BuildSteadyDecileMedian100M",
                 "recB7ReachWindowEstimateMs100M", "recB7CompactionShare100M"]
    for name in core_100m:
        assert not values[name].startswith("\\errmessage"), f"{name} should be landed"

    executed_100m = ["HistSingle", "HistAsof", "PathsK", "SeriesCount",
                      "BurstZscore", "MotifFiltered"]
    for frag in executed_100m:
        name = f"recB7ScaleCurveP50{frag}100M"
        assert not values[name].startswith("\\errmessage"), f"{name} should be landed"

    for op_id in mod.B7_SCALE_CURVE_100M_REFUSED_NO_ESTIMATE:
        frag = mod.B7_SCALE_CURVE_OPS[op_id]
        name = f"recB7Refused{frag}100M"
        assert values[name] == "true", f"{name} should be a landed refusal macro"
        # no ScaleCurveP50 macro at all for these six at 100M
        assert f"recB7ScaleCurveP50{frag}100M" not in values

    assert not values["recB7BuildWall30M"].startswith("\\errmessage")
    assert not values["recB7300MGate"].startswith("\\errmessage")


def test_b7_scale_100m_reach_window_p50_is_pending_with_its_estimate():
    """recB7ScaleCurveP50ReachWindow100M is the one refused-operator
    exception: it stays PENDING (reach.window's refusal carries a numeric
    time_est_ms, unlike the other six), and the estimate must be inside
    the rendered \\errmessage, not just the comment."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b7_scale(m)
    values = {name: value for name, value, _ in m.items}
    name = "recB7ScaleCurveP50ReachWindow100M"
    assert values[name].startswith("\\errmessage")
    assert "time_est_ms 14571" in values[name]
    assert "cost guardrail" in values[name]


def test_b7_scale_100m_recovery_is_an_alias_of_the_ce5000_measurement():
    """No cadence-500 100M run exists or was ever planned -- recB7Recovery100M
    must equal recB7RecoveryCe5000At100M exactly, not a separate figure."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_b7_scale(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recB7Recovery100M"] == values["recB7RecoveryCe5000At100M"]
    assert values["recB7Recovery100M"] == "22{,}717.7"


def test_tampered_b7_scale_curve_100m_refused_operator_missing_error_field_fails(tmp_path):
    """A refused operator's per_operator_p50_ms entry must carry a
    measured_p50_ms of null and a raw row with ok=false -- if a tampered
    copy fabricates a measured_p50_ms for a refused operator (with the
    whole-file sha256 patched to match), that must fail rather than
    silently emitting a real-looking p50 for an operator the guardrail
    actually refused."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.B7_SCALE_CURVE_100M.read_text(encoding="utf-8"))
    data["per_operator_p50_ms"]["agg.rel_bucket"]["measured_p50_ms"] = 42.0
    tampered = tmp_path / "scale-curve-100m.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    import hashlib as _hashlib
    readme_text = mod.B7_README.read_text(encoding="utf-8")
    patched_sha = _hashlib.sha256(tampered.read_bytes()).hexdigest()
    readme_patched = tmp_path / "README.md"
    readme_patched.write_text(
        readme_text.replace(
            "`8f39ca9a2b8508ce26d953d05303cc7aae46a47a8208853f4e981ce0933a3c83`",
            f"`{patched_sha}`"),
        encoding="utf-8")

    mod.B7_SCALE_CURVE_100M = tampered
    mod.B7_README = readme_patched
    m = mod.Macros()
    mod.compute_b7_scale(m)
    assert mod.FAILURES, "a refused operator with a fabricated measured_p50_ms must fail, " \
        "even with a patched whole-file digest"
    assert any("agg.rel_bucket" in f for f in mod.FAILURES)


def test_tampered_ldbc_ref_v1_compare_sha256_mismatch_fails(tmp_path):
    """An edited copy of compare-2026-09-18.json must fail the whole-file
    sha256 check against benchmarks/ldbc-ref-v1/SHA256SUMS.txt before any
    verdict-count or row-agreement figure inside it is trusted."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LDBC_REF_V1_COMPARE.read_text(encoding="utf-8"))
    data["manifest"]["host"] = "tampered-host"
    tampered = tmp_path / "compare-2026-09-18.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LDBC_REF_V1_COMPARE = tampered
    m = mod.Macros()
    mod.compute_ldbc_ref_v1(m)
    assert mod.FAILURES, "an edited compare-2026-09-18.json must fail the sha256 check " \
        "against SHA256SUMS.txt"
    assert any("sha256" in f.lower() for f in mod.FAILURES)


def _ldbc_ref_v1_patched_sums(mod, tmp_path: Path, tampered_path: Path) -> Path:
    """A copy of SHA256SUMS.txt with `tampered_path`'s basename's entry
    updated to its actual (tampered) digest -- isolates a row-level check
    from the whole-file sha256 gate, the same technique
    test_tampered_b7_scale_curve_100m_refused_operator_missing_error_field_fails
    uses against a README's own sha256 table."""
    import hashlib as _hashlib
    text = mod.LDBC_REF_V1_SHA256SUMS.read_text(encoding="utf-8")
    old_hash = mod._sha256sums_by_name(text)[tampered_path.name]
    new_hash = _hashlib.sha256(tampered_path.read_bytes()).hexdigest()
    patched = tmp_path / "SHA256SUMS.txt"
    patched.write_text(text.replace(old_hash, new_hash), encoding="utf-8")
    return patched


def test_tampered_ldbc_ref_v1_row_agreement_mismatch_fails_even_with_patched_sums(tmp_path):
    """Editing one verdict's agreeing count (without touching compared, or
    any other row) must break the frozen 678/736 row-agreement total --
    even with SHA256SUMS.txt patched to match, so the whole-file sha256
    gate is not what catches this."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LDBC_REF_V1_COMPARE.read_text(encoding="utf-8"))
    by_id = {v["plan_id"]: v for v in data["verdicts"]}
    by_id["BI3"]["agreeing"] = 19  # was 20 of 20 -- must not silently pass as 20/20
    tampered = tmp_path / "compare-2026-09-18.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LDBC_REF_V1_COMPARE = tampered
    mod.LDBC_REF_V1_SHA256SUMS = _ldbc_ref_v1_patched_sums(mod, tmp_path, tampered)
    m = mod.Macros()
    mod.compute_ldbc_ref_v1(m)
    assert mod.FAILURES, "an edited per-template agreeing count must fail the recomputed " \
        "row-agreement total, even with a patched whole-file digest"
    assert any("736" in f or "row agreement" in f for f in mod.FAILURES)


def test_tampered_ldbc_ref_v1_verdict_label_disagrees_with_reference_columns_field_fails(
        tmp_path):
    """A verdict relabelled to 'agreeing' while its
    reference_columns_not_projected list stays non-empty (or vice versa)
    must fail the cross-check between the two fields, even with
    SHA256SUMS.txt patched to match."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LDBC_REF_V1_COMPARE.read_text(encoding="utf-8"))
    by_id = {v["plan_id"]: v for v in data["verdicts"]}
    by_id["BI4"]["verdict"] = "agreeing"  # BI4 still carries 3 unprojected reference columns
    tampered = tmp_path / "compare-2026-09-18.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LDBC_REF_V1_COMPARE = tampered
    mod.LDBC_REF_V1_SHA256SUMS = _ldbc_ref_v1_patched_sums(mod, tmp_path, tampered)
    m = mod.Macros()
    mod.compute_ldbc_ref_v1(m)
    assert mod.FAILURES, "a verdict label that disagrees with its own " \
        "reference_columns_not_projected field must fail, even with a patched digest"


def test_tampered_ldbc_ref_v1_interim_exclusion_set_derivation_fails_even_with_patched_sums(
        tmp_path):
    """The interim scoring excludes exactly the 7 templates
    manifest.supersedes_reason names as having had an invalid reference
    side (plus the always-timed-out BI6.v2) -- editing that list (without
    touching the interim record itself) must fail the frozen count/list
    checks derived from it, even with SHA256SUMS.txt patched to match."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.LDBC_REF_V1_COMPARE.read_text(encoding="utf-8"))
    data["manifest"]["supersedes_reason"] = data["manifest"]["supersedes_reason"].replace(
        "7 templates (BI4 BI9 BI11 BI12 IC2 IC5 IC9)",
        "6 templates (BI4 BI9 BI11 BI12 IC2 IC5)")
    tampered = tmp_path / "compare-2026-09-18.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.LDBC_REF_V1_COMPARE = tampered
    mod.LDBC_REF_V1_SHA256SUMS = _ldbc_ref_v1_patched_sums(mod, tmp_path, tampered)
    m = mod.Macros()
    mod.compute_ldbc_ref_v1(m)
    assert mod.FAILURES, "a supersedes_reason edited to drop IC9 from the invalid-reference " \
        "list must fail the frozen 7-template list check, even with a patched digest"


def test_tampered_ldbc_ref_v1_ledger_entry_id_mismatch_fails(tmp_path):
    """The D-090 cross-check against ops/failure_ledger.jsonl must fail if
    the ledger's own entry no longer names both IS3 and IC2 in its
    symptom -- a cross-check on the defect-templates macro, independent
    of the compare record itself."""
    mod = _load("sys_paper_macros")
    entries = mod.load_jsonl(mod.FAILURE_LEDGER)
    for e in entries:
        if e.get("id") == "D-090-is3-knows-both-ways-double-count":
            e["symptom"] = e["symptom"].replace("IC2", "a different template")
    tampered = tmp_path / "failure_ledger.jsonl"
    tampered.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    mod.FAILURE_LEDGER = tampered
    m = mod.Macros()
    mod.compute_ldbc_ref_v1(m)
    assert mod.FAILURES, "a D-090 ledger entry that no longer names IC2 in its symptom " \
        "must fail the cross-check"
    assert any("D-090" in f for f in mod.FAILURES)


# --------------------------------------------------------------------------
# sys_paper_macros.py: compute_external_baselines (Lane W2ad, the 30
# recExt1*/recExt2* macros landed from benchmarks/external-v1/)
# --------------------------------------------------------------------------

def test_external_baselines_runs_without_verification_failure():
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_external_baselines(m)
    assert mod.FAILURES == [], f"unexpected verification failures: {mod.FAILURES}"
    assert mod.CHECKS > 50, "expected many row/cross-record-level assertions"


def test_external_baselines_ratio_gr_independently_recomputed_from_raw_records():
    """Recompute recExt1RatioGrMedian from the raw JSON here, independently
    of compute_external_baselines's own code path (reading the per-cell
    refresh_wall_ms.median and the committed storm-v2-main-grid row's own
    ttf_p50_ms directly), and check it matches the generated macro --
    exercising the ratio_gr formula itself, not just the generator's
    self-consistency."""
    mod = _load("sys_paper_macros")
    neo = json.loads(mod.EXTERNAL_NEO4J.read_text(encoding="utf-8"))
    neo_pc = {c["cell_id"]: c for c in neo["summary"]["per_cell"]}
    sv2_rows = [json.loads(line) for line in
                mod.STORM_V2_MAIN_GRID_ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    sv2_pc = {}
    for r in sv2_rows:
        cfg = r["config"]
        cid = mod._external_cell_id(cfg["store"], cfg["mix"], cfg["age"], cfg["n_artifacts"],
                                      cfg["seed"])
        sv2_pc[cid] = r

    l2partial_synth_cells = sorted(
        c for c, cell in neo_pc.items()
        if c.startswith("synth-iv-60k-") and cell["equality_level"] == "L2-partial"
        and c != "synth-iv-60k-c1-none-n10000-s0")
    assert len(l2partial_synth_cells) == 18
    ratios = [neo_pc[c]["refresh_wall_ms"]["median"]
              / sv2_pc[c]["summary"]["arms"]["global-recompute"]["ttf_p50_ms"]
              for c in l2partial_synth_cells]
    independently_computed_median = statistics.median(ratios)

    m = mod.Macros()
    mod.compute_external_baselines(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recExt1RatioGrMedian"] == f"{independently_computed_median:.2f}"


def test_external_baselines_speedup_scored_and_meeting_are_a_consistent_pair():
    """recExt1SpeedupCellsScored is the denominator recExt1SpeedupCellsMeeting
    counts against (of the 18 SpeedupLOne cells minus the probe, those
    with a committed storm-v2-main-grid counterpart); the 6 new (A2)
    age-banded cells have none and are excluded, not scored as failing."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_external_baselines(m)
    values = {name: value for name, value, _ in m.items}
    scored = int(values["recExt1SpeedupCellsScored"])
    meeting = int(values["recExt1SpeedupCellsMeeting"])
    assert scored == 12
    assert 0 <= meeting <= scored


def test_external_baselines_unanswerable_ms_is_literal_text_not_a_number():
    """The withheld-correction cell's own record carries only booleans for
    the F-epoch/F-watermark probes, no timing field -- recExt2UnanswerableMs
    must land as the literal text "not measured", never a fabricated
    number nor a PENDING \\errmessage stub (the record itself landed; only
    this one field within it did not)."""
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_external_baselines(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recExt2UnanswerableMs"] == "not measured"
    with pytest.raises(ValueError):
        float(values["recExt2UnanswerableMs"])


def test_external_baselines_crossover_band_is_none_when_every_band_is_below_one():
    mod = _load("sys_paper_macros")
    m = mod.Macros()
    mod.compute_external_baselines(m)
    values = {name: value for name, value, _ in m.items}
    assert values["recExt2CrossoverBand"] == "none"
    for band in ("Recent", "Hours", "Days", "Deep"):
        ratio_str = values[f"recExt2Ratio{band}"]
        # every landed band ratio here is < 0.01, so sci_3sf's "\times 10^{-n}"
        # form is used -- never a plain decimal that could be misread as >= 1.
        assert "\\times 10^{-" in ratio_str


def test_tampered_external_neo4j_sha256_mismatch_fails(tmp_path):
    """Every benchmarks/external-v1/ file this lane reads is sha256-checked
    by filename against that directory's own SHA256SUMS.txt -- an edited
    copy must fail that check before any number is trusted from it."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.EXTERNAL_NEO4J.read_text(encoding="utf-8"))
    data["summary"]["per_cell"][0]["refresh_wall_ms"]["median"] = 999999.0
    tampered = tmp_path / mod.EXTERNAL_NEO4J.name
    tampered.write_text(json.dumps(data), encoding="utf-8")

    mod.EXTERNAL_NEO4J = tampered
    m = mod.Macros()
    mod.compute_external_baselines(m)
    assert mod.FAILURES, "an edited neo4j-recompute-2026-10-07.json must fail the " \
        "SHA256SUMS.txt check"
    assert any("SHA256SUMS" in f for f in mod.FAILURES)


def test_tampered_external_ivm_disagree_total_cross_check_fails_even_with_patched_sha256(
        tmp_path):
    """recExt2AgreeCells is cross-checked against
    summary.predictions_measured.ext2.d_disagree_total, recomputed from the
    per-cell oracle_agreement.disagree fields -- not trusted from that
    self-reported total alone. Corrupting the self-reported total (while
    patching SHA256SUMS.txt so the sha256 gate itself still passes) must
    still fail the cross-check."""
    mod = _load("sys_paper_macros")
    data = json.loads(mod.EXTERNAL_IVM.read_text(encoding="utf-8"))
    data["summary"]["predictions_measured"]["ext2"]["d_disagree_total"] = 7
    tampered = tmp_path / mod.EXTERNAL_IVM.name
    tampered_bytes = json.dumps(data).encode("utf-8")
    tampered.write_bytes(tampered_bytes)

    sums_text = mod.EXTERNAL_V1_SHA256SUMS.read_text(encoding="utf-8")
    new_hash = hashlib.sha256(tampered_bytes).hexdigest()
    patched_lines = []
    for line in sums_text.splitlines():
        if line.strip().endswith(mod.EXTERNAL_IVM.name):
            patched_lines.append(f"{new_hash}  benchmarks/external-v1/{mod.EXTERNAL_IVM.name}")
        else:
            patched_lines.append(line)
    patched_sums = tmp_path / "SHA256SUMS.txt"
    patched_sums.write_text("\n".join(patched_lines) + "\n", encoding="utf-8")

    mod.EXTERNAL_IVM = tampered
    mod.EXTERNAL_V1_SHA256SUMS = patched_sums
    m = mod.Macros()
    mod.compute_external_baselines(m)
    assert mod.FAILURES, "a corrupted ext2.d_disagree_total must fail the recomputed-sum " \
        "cross-check, even with a patched SHA256SUMS.txt"
    assert any("d_disagree_total" in f for f in mod.FAILURES)


# --------------------------------------------------------------------------
# sys_paper_figures.py: CSV generation (no matplotlib dependency)
# --------------------------------------------------------------------------

def _load_figures():
    return _load("sys_paper_figures")


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
    macro_mod = _load("sys_paper_macros")
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_crash_data()
    assert data["total"]["trials"] == 10000
    assert data["total"]["problems"] == 0

    m = macro_mod.Macros()
    macro_mod.compute_c1(m)
    values = dict((n, v) for n, v, _ in m.items)
    assert values["recCrashTrials"] == "10{,}000"
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


def test_r18_crossover_csv_has_five_batches_a_p50_row_and_a_landed_n1000_row(tmp_path, monkeypatch):
    """The main correction-load grid (storm-v1-main-grid-2026-09-15) landed
    at 36/36 cells, so f8's N=1,000 c1 seed-0 point is no longer a PENDING
    annotation -- it reads the same cell
    scripts/sys_paper_macros.py's compute_c7_storm_v1 lands as
    recStormV1SpeedupN1kSeed0 (frozen at 1.938, close tol 0.001 there)."""
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_r18_crossover_data()
    assert len(data["batches"]) == 5
    assert 0.80 <= data["ttf_global_p50_s"] / data["ttf_l1_p50_s"] <= 0.82
    assert data["n1000_status"] == "measured"
    assert abs(data["n1000_speedup"] - 1.938) < 0.001
    text = fig_mod.write_r18_crossover_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["batch_index", "check_seconds_tgms_L1", "lookup_ms",
                        "global_recompute_seconds", "ttf_tgms_L1_seconds",
                        "ttf_global_recompute_seconds"]
    assert len(rows) == 1 + 5 + 1 + 1  # header + 5 batches + p50 row + N=1000 row
    n1000_row = rows[-1]
    assert n1000_row[0] == "N=1000 c1 seed0"
    assert float(n1000_row[4]) > 0 and float(n1000_row[5]) > 0
    assert abs(float(n1000_row[5]) / float(n1000_row[4]) - 1.938) < 0.001


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


def test_scale_curve_csv_has_13_operators_at_3_scales_with_refused_rows(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_scale_curve_data()
    assert len(data["operators"]) == 13
    # 6/13 executed, 7/13 refused at 100M (benchmarks/scale-v1/README.md);
    # reach.window refuses with a numeric time_est_ms (Addendum 5's
    # restated admission ceiling), the other six with only a CostError
    # string and no numeric estimate anywhere in the record.
    assert data["refused_100m"] == [
        "agg.rel_bucket", "coactive.narrow", "diff.global", "nbr.evolution",
        "reach.window", "resolve.substr", "snap.hop2",
    ]

    text = fig_mod.write_scale_curve_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["operator", "scale", "p50_ms", "refused", "time_est_ms", "bar_ms"]
    assert len(rows) == 1 + 13 * 3  # header + 13 operators x 3 scales

    by_op_scale = {(r[0], r[1]): r for r in rows[1:]}

    # No operator refuses at 10M or 30M -- every row has a p50 and no reason.
    for op in data["operators"]:
        for scale in ("10M", "30M"):
            row = by_op_scale[(op, scale)]
            assert row[2] != "", f"{op}/{scale}: expected a p50, got none"
            assert row[3] == "False", f"{op}/{scale}: unexpectedly marked refused"

    # Refused rows at 100M: empty p50, refused=True.
    for op in data["refused_100m"]:
        row = by_op_scale[(op, "100M")]
        assert row[2] == "", f"{op}/100M: refused row must not carry a fabricated p50"
        assert row[3] == "True"

    # reach.window is the only refused-at-100M operator with a numeric
    # time_est_ms (from reach_window_admission, not from a fabricated
    # estimate); the other six refused ops carry no estimate at all.
    reach_100m = by_op_scale[("reach.window", "100M")]
    assert reach_100m[4] == "14571"
    for op in data["refused_100m"]:
        if op == "reach.window":
            continue
        row = by_op_scale[(op, "100M")]
        assert row[4] == "", f"{op}/100M: must not fabricate a time_est_ms"

    # Executed operators at 100M carry a real p50 and the corresponding
    # forecast bar as a reference figure, e.g. motif.filtered (a PASS).
    motif_100m = by_op_scale[("motif.filtered", "100M")]
    assert motif_100m[2] == "110.861"
    assert motif_100m[5] == "157.014"


def test_scale_curve_title_is_short_and_commits_move_to_a_caption(tmp_path, monkeypatch):
    """f_b7_scale_curve's rendered title used to overprint a long
    commit-hash string (one per scale, three total); the title must now be
    just the figure name, with per-record commit provenance moved to a
    caption instead -- a functional-equivalence check, not a data change."""
    fig_mod = _load_figures()
    if not fig_mod.HAVE_MPL:
        pytest.skip(
            "matplotlib is not installed in this interpreter "
            f"({sys.executable}); scripts/sys_paper_figures.py falls back to "
            "CSV-only generation in this environment (see its HAVE_MPL guard). "
            "Install matplotlib in $HOME/.venvs/tgms to exercise this check.")
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    captured = {}
    monkeypatch.setattr(fig_mod, "_savefig", lambda fig, stem: captured.setdefault("fig", fig))

    data = fig_mod.build_scale_curve_data()
    fig_mod.plot_scale_curve(data)
    fig = captured["fig"]
    try:
        title = fig.axes[0].get_title()
        assert title == "B7 scale curve: per-operator p50 at 10M / 30M / 100M"
        for commit in (data["commit_10m"], data["commit_30m"], data["commit_100m"]):
            assert commit not in title, f"commit {commit} must not be in the title anymore"
        caption_texts = [t.get_text() for t in fig.texts]
        assert any(
            data["commit_10m"] in t and data["commit_30m"] in t and data["commit_100m"] in t
            for t in caption_texts
        ), "all three commits must be recoverable from a caption, not the title"
    finally:
        fig_mod.plt.close(fig)


def test_scale_costs_csv_has_expected_rows_and_no_fabricated_values(tmp_path, monkeypatch):
    """f11_b7_scale_costs: every (scale, quantity) the source records
    actually carry must appear exactly once, nothing else -- a quantity a
    record does not carry (query-ready floor at 1M/10M; any recovery
    figure at 10M; the ce500 cadence at 100M, which was never run) must be
    a gap, never an estimated or interpolated point. Every value is
    checked against the exact record field it cites, loaded independently
    of the module under test.
    """
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_scale_costs_data()

    expected = {
        ("1M", "build_wall_s"), ("1M", "build_vmhwm_gb"), ("1M", "recovery_wall_s_ce500"),
        ("10M", "build_wall_s"), ("10M", "build_vmhwm_gb"),
        ("30M", "build_wall_s"), ("30M", "build_vmhwm_gb"),
        ("30M", "query_ready_floor_vmhwm_gb"),
        ("30M", "recovery_wall_s_ce500"), ("30M", "recovery_wall_s_ce5000"),
        ("100M", "build_wall_s"), ("100M", "build_vmhwm_gb"),
        ("100M", "query_ready_floor_vmhwm_gb"), ("100M", "recovery_wall_s_ce5000"),
    }
    actual = {(r["scale"], r["quantity"]) for r in data["rows"]}
    assert actual == expected, f"row set differs from expected: {actual ^ expected}"
    assert len(data["rows"]) == 14

    # Points no record carries -- must never appear as a fabricated gap-fill.
    for missing in (
        ("1M", "query_ready_floor_vmhwm_gb"),
        ("10M", "query_ready_floor_vmhwm_gb"),
        ("10M", "recovery_wall_s_ce500"),
        ("10M", "recovery_wall_s_ce5000"),
        ("100M", "recovery_wall_s_ce500"),
    ):
        assert missing not in actual, f"{missing} has no source record and must be a gap"

    by_key = {(r["scale"], r["quantity"]): r for r in data["rows"]}

    def load(name):
        return json.loads((ROOT / "benchmarks" / "scale-v1" / name).read_text(encoding="utf-8"))

    calib_1m = load("itiger-calib-1m.json")
    calib_10m = load("itiger-calib-10m.json")
    build_30m = load("build-30m.json")
    build_100m = load("build-100m.json")
    qf_30m = load("queryfloor-30m.json")
    qf_100m = load("queryfloor-100m.json")
    rec_30m_ce500 = load("recovery-30m.json")
    rec_30m_ce5000 = load("recovery-30m-ce5000.json")
    rec_100m_ce5000 = load("recovery-100m-ce5000.json")

    assert by_key[("1M", "build_wall_s")]["value"] == calib_1m["build_info"]["wall_s"]
    assert by_key[("1M", "build_vmhwm_gb")]["value"] == round(
        calib_1m["build_info"]["peak_rss"]["vmhwm"] / 1_000_000, 6)
    assert by_key[("1M", "recovery_wall_s_ce500")]["value"] == calib_1m["recovery"]["wall_s"]
    assert "--compact-every 500" in calib_1m["recovery"]["invocation"]

    assert by_key[("10M", "build_wall_s")]["value"] == calib_10m["build_info"]["wall_s"]
    assert by_key[("10M", "build_vmhwm_gb")]["value"] == round(
        calib_10m["build_info"]["peak_rss"]["vmhwm"] / 1_000_000, 6)
    assert "recovery" not in calib_10m, "itiger-calib-10m.json has no recovery sub-record"

    assert by_key[("30M", "build_wall_s")]["value"] == build_30m["build_info"]["wall_s"]
    assert by_key[("30M", "build_wall_s")]["value"] == 3786.004
    assert by_key[("30M", "build_vmhwm_gb")]["value"] == round(
        build_30m["build_info"]["peak_rss"]["vmhwm"] / 1_000_000, 6)
    assert by_key[("30M", "query_ready_floor_vmhwm_gb")]["value"] == round(
        qf_30m["vmhwm_kb"] / 1_000_000, 6)
    assert round(by_key[("30M", "query_ready_floor_vmhwm_gb")]["value"], 2) == 6.81  # README prose
    assert by_key[("30M", "recovery_wall_s_ce500")]["value"] == rec_30m_ce500["replay_wall_s"]
    assert rec_30m_ce500["compact_every"] == 500
    assert by_key[("30M", "recovery_wall_s_ce5000")]["value"] == rec_30m_ce5000["replay_wall_s"]
    assert rec_30m_ce5000["compact_every"] == 5000

    assert by_key[("100M", "build_wall_s")]["value"] == build_100m["build_info"]["wall_s"]
    assert by_key[("100M", "build_wall_s")]["value"] == 28372.936
    assert by_key[("100M", "build_vmhwm_gb")]["value"] == round(
        build_100m["build_info"]["peak_rss"]["vmhwm"] / 1_000_000, 6)
    assert by_key[("100M", "query_ready_floor_vmhwm_gb")]["value"] == round(
        qf_100m["vmhwm_kb"] / 1_000_000, 6)
    assert round(by_key[("100M", "query_ready_floor_vmhwm_gb")]["value"], 2) == 19.67  # README prose
    assert by_key[("100M", "recovery_wall_s_ce5000")]["value"] == rec_100m_ce5000["replay_wall_s"]
    assert rec_100m_ce5000["compact_every"] == 5000

    text = fig_mod.write_scale_costs_csv(data)
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == ["scale", "quantity", "value", "unit", "source_file", "note"]
    assert len(rows) == 1 + 14


def test_scale_costs_csv_is_byte_identical_to_the_frozen_hash(tmp_path, monkeypatch):
    """f11_b7_scale_costs's --csv-only output must not move when its plot
    layout changes (short y-labels, constrained_layout, dropped per-point
    annotations, etc.) -- the CSV writer never touches matplotlib, so this
    pins its exact bytes with a frozen sha256 as the "no data change"
    receipt for that layout work.
    """
    fig_mod = _load_figures()
    monkeypatch.setattr(fig_mod, "OUT_DIR", tmp_path)
    data = fig_mod.build_scale_costs_data()
    text = fig_mod.write_scale_costs_csv(data)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == "4ce62f101d3a07c57d89a25d78bb06031e95f642df5fdd8aa366af4eadc3c774", (
        "f11_b7_scale_costs.csv content changed -- this must stay byte-identical "
        "across figure-layout-only edits")


def test_cli_csv_only_mode_is_idempotent(tmp_path):
    result1 = subprocess.run(
        [_venv_python(), str(ROOT / "scripts" / "sys_paper_figures.py"), "--csv-only"],
        cwd=ROOT, capture_output=True, text=True)
    assert result1.returncode == 0, result1.stderr
    result2 = subprocess.run(
        [_venv_python(), str(ROOT / "scripts" / "sys_paper_figures.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True)
    assert result2.returncode == 0, (
        f"CSVs were not idempotent across two --csv-only runs: {result2.stderr}")


# --------------------------------------------------------------------------
# sys_paper_figures.py: PDF/PNG rendering (requires matplotlib)
# --------------------------------------------------------------------------

def test_figures_render_to_pdf_and_png(tmp_path, monkeypatch):
    fig_mod = _load_figures()
    if not fig_mod.HAVE_MPL:
        pytest.skip(
            "matplotlib is not installed in this interpreter "
            f"({sys.executable}); scripts/sys_paper_figures.py falls back to "
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
# csname emission: no \newcommand{\rec<digit>...} form; \rec{} accessor
#
# The defect this fixes: \newcommand{\recFoo30M}{...} is not a legal LaTeX
# control sequence definition -- macro names built without \csname may not
# contain digits -- yet 267 of the file's 401 names do (e.g.
# recB7BuildWall30M), so the file could not be \input without a catcode
# shim. \csname...\endcsname has no such restriction.
# --------------------------------------------------------------------------

_BARE_DIGIT_NEWCOMMAND_RE = re.compile(r"\\newcommand\{\\rec[A-Za-z0-9]*\d[A-Za-z0-9]*\}")


def test_rendered_macros_never_use_bare_newcommand_with_a_digit_in_the_name():
    """Every digit-bearing name must go through \\csname...\\endcsname, never
    a bare \\newcommand{\\rec...} (which \\TeX{} would refuse to parse)."""
    mod = _load("sys_paper_macros")
    m = _run_all_landed(mod)
    mod.add_pending_stubs(m)
    rendered = m.render()
    assert not _BARE_DIGIT_NEWCOMMAND_RE.search(rendered), (
        "found a bare \\newcommand{\\rec...} control sequence with a digit in its name")
    # sanity: there really are digit-bearing names in this run, so the
    # assertion above is exercising something real, not vacuously true.
    digit_bearing = [name for name, _, _ in m.items if any(c.isdigit() for c in name)]
    assert len(digit_bearing) > 0
    for name, _, _ in m.items:
        assert f"\\expandafter\\newcommand\\csname {name}\\endcsname" in rendered, (
            f"{name}: not emitted via \\csname")


def test_rendered_macros_include_the_rec_accessor():
    """\\providecommand{\\rec}[1]{\\csname rec#1\\endcsname} must appear once,
    near the top, ahead of any macro definition, so prose can write
    \\rec{B7BuildWall30M} instead of the raw \\csname form."""
    mod = _load("sys_paper_macros")
    m = _run_all_landed(mod)
    rendered = m.render()
    accessor = r"\providecommand{\rec}[1]{\csname rec#1\endcsname}"
    assert rendered.count(accessor) == 1
    first_macro = rendered.index("\\expandafter\\newcommand\\csname")
    assert rendered.index(accessor) < first_macro


@pytest.mark.skipif(shutil.which("tectonic") is None, reason="tectonic not installed")
def test_generated_tex_compiles_under_tectonic():
    """The actual regression: a real LaTeX engine must accept the generated
    file, exercising both the \\rec{...} accessor and a bare digit-bearing
    control sequence used directly (\\recSoakHoursTwo has no digit and
    already worked before this fix; \\rec{B7BuildWall30M} names a macro
    that did not compile before it)."""
    subprocess.run([_venv_python(), str(ROOT / "scripts" / "sys_paper_macros.py")],
                    cwd=ROOT, check=True, capture_output=True, text=True)
    tex_src = _out_path()
    with tempfile.TemporaryDirectory() as tmpdir:
        work = Path(tmpdir)
        shutil.copy(tex_src, work / "sys-paper-macros.tex")
        (work / "doc.tex").write_text(
            r"""\documentclass{article}
\input{sys-paper-macros.tex}
\begin{document}
\rec{B7BuildWall30M} \recSoakHoursTwo
\end{document}
""",
            encoding="utf-8",
        )
        result = subprocess.run(["tectonic", "doc.tex"], cwd=work,
                                 capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (work / "doc.pdf").exists()


# --------------------------------------------------------------------------
# ruff
# --------------------------------------------------------------------------

def test_ruff_clean():
    ruff = _ruff_path()
    if ruff is None:
        pytest.skip(f"no ruff binary found next to {sys.executable}")
    targets = [
        "scripts/sys_paper_macros.py",
        "scripts/sys_paper_figures.py",
        "scripts/site_facts.py",
        "tests/test_sys_paper_macros.py",
    ]
    result = subprocess.run([str(ruff), "check", *targets], cwd=ROOT,
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------
# cleanup: don't leave a stray tampered file's directory artifacts behind
# in the real paper/sys/generated/ tree (tests above use tmp_path/
# monkeypatch for everything except the two CLI idempotency tests, which
# intentionally exercise the real generator against the real, committed
# records and write into the documented paper/sys/generated/ convention).
# --------------------------------------------------------------------------

def test_generated_output_directory_is_the_documented_convention():
    mod = _load("sys_paper_macros")
    fig_mod = _load_figures()
    assert mod.OUT_DIR == ROOT / "paper" / "sys" / "generated"
    assert fig_mod.OUT_DIR == ROOT / "paper" / "sys" / "generated"
    # paper/ is gitignored repo-wide; nothing this task writes there is
    # ever meant to be committed.
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "paper/" in gitignore.splitlines()
