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
    mod.compute_c4(m)
    mod.compute_c5(m)
    mod.compute_c6(m)
    mod.compute_c8(m)
    mod.compute_c7_dag(m)
    mod.compute_c7_r18(m)
    mod.compute_d160(m)
    mod.compute_d160_llm_direct_fix(m)
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
        "osdiCorruptionClasses", "osdiCorruptionDetected",
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
    assert len(names) == len(FROZEN_LANDED_VALUES) + 13


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
