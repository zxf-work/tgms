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
    mod.compute_c4(m)
    mod.compute_c5(m)
    mod.compute_c6(m)
    mod.compute_c8(m)
    mod.compute_c7_dag(m)
    mod.compute_c7_r18(m)
    mod.compute_d160(m)
    mod.compute_d160_llm_direct_fix(m)
    mod.compute_c2(m)
    mod.compute_ladder(m)
    mod.compute_longevity_soak(m)
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
        "osdiLiveDays", "osdiLiveAdvisories", "osdiLiveCorrections",
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
    assert len(names) == len(FROZEN_LANDED_VALUES) + 11


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
