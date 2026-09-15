"""[tests] `scripts/eval_concurrency.py commitcost` -- the per-commit phase
aggregation the B1-v2d lane added (`_aggregate_commit_phases`,
`_dir_entry_counts`), exercised on a synthetic per-commit list rather than a
real store: no native engine needed, no remote run, just the arithmetic.

`docs/design/B1V2_AB_DIAGNOSIS_2026-09-15.md` (internal, git-ignored) found
the B1 A/B's untimed residual (`total_us` minus the named engine phases)
hiding entirely inside a *median*-based `first_decile_us`/`last_decile_us`.
`phase_decile_first_us`/`phase_decile_last_us` add the *mean* over the same
window instead -- a mean moves on a fat right tail a median can hide -- and
`residual_first_us`/`residual_last_us` re-derive the diagnosis's own metric
per commit so a commitcost record shows, on its own, whether growth is
explained.
"""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_eval_concurrency_module():
    # `eval_concurrency.py` does `import eval_harness as H` / `import
    # eval_resources as R` unqualified, exactly as it would when run as
    # `python scripts/eval_concurrency.py` -- put `scripts/` on sys.path so
    # those resolve the same way here.
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        "eval_concurrency", ROOT / "scripts" / "eval_concurrency.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


CC = _load_eval_concurrency_module()

# Sums to 17us -- an arbitrary but fixed baseline for the named engine
# phases, held constant across every synthetic commit below so `total_us`
# alone controls the residual.
_NAMED_PHASES = {
    "capture_us": 1, "seal_us": 2, "closes_us": 0, "stats_us": 0,
    "dict_us": 3, "digest_us": 1, "debug_verify_us": 0,
    "delta_build_us": 1, "manifest_us": 4, "current_us": 5,
}
_NAMED_SUM = sum(_NAMED_PHASES.values())
assert _NAMED_SUM == 17


def _synthetic_commit(residual_us: int, **overrides) -> dict[str, int]:
    """One `_timed_write`-shaped phase dict: named engine phases fixed at
    `_NAMED_PHASES`, `total_us` set so `total_us - sum(named phases) ==
    residual_us` exactly, plus the Python-side fields a real record also
    carries."""
    total_us = _NAMED_SUM + residual_us
    row = dict(_NAMED_PHASES)
    row.update({
        "total_us": total_us,
        "manifest_bytes": 1_000,
        "segments_named": 5,
        "manifest_checkpoint": 0,
        "wal_us": 1,
        "apply_us": 1,
        "write_us": total_us + 3,
        "python_wrap_us": 1,
    })
    row.update(overrides)
    return row


def test_engine_phase_keys_matches_the_named_phases_used_here():
    # if store.rs ever renames/adds an engine phase, this constant (and the
    # fixture above) should be the thing that goes stale loudly, not a test
    # that silently keeps summing the old set
    assert set(CC.ENGINE_PHASE_KEYS) == set(_NAMED_PHASES)


def test_phase_p50_and_decile_fields_are_unchanged_for_a_flat_series():
    # 30 commits, no growth: every new field should agree with a flat
    # series' obvious answer, and with the pre-B1-v2d median fields it sits
    # beside.
    phases = [_synthetic_commit(residual_us=5) for _ in range(30)]
    row = CC._aggregate_commit_phases(phases)
    assert row["phase_p50_us"]["total_us"] == _NAMED_SUM + 5
    assert row["first_decile_us"]["total_us"] == _NAMED_SUM + 5
    assert row["last_decile_us"]["total_us"] == _NAMED_SUM + 5
    assert row["phase_decile_first_us"]["total_us"] == _NAMED_SUM + 5
    assert row["phase_decile_last_us"]["total_us"] == _NAMED_SUM + 5
    assert row["residual_first_us"] == pytest.approx(5.0)
    assert row["residual_last_us"] == pytest.approx(5.0)


def test_phase_decile_uses_mean_not_median_and_catches_a_fat_tail():
    # first 3 of 30: two ordinary commits and one outlier -- a median would
    # report the ordinary value and hide the outlier entirely; a mean must
    # not.
    phases = ([
        _synthetic_commit(residual_us=10),
        _synthetic_commit(residual_us=10),
        _synthetic_commit(residual_us=1_000),
    ] + [_synthetic_commit(residual_us=10) for _ in range(24)] + [
        _synthetic_commit(residual_us=20),
        _synthetic_commit(residual_us=20),
        _synthetic_commit(residual_us=2_000),
    ])
    assert len(phases) == 30
    row = CC._aggregate_commit_phases(phases)

    first_totals = [_NAMED_SUM + 10, _NAMED_SUM + 10, _NAMED_SUM + 1_000]
    last_totals = [_NAMED_SUM + 20, _NAMED_SUM + 20, _NAMED_SUM + 2_000]

    # the pre-existing median field is blind to the outlier
    assert row["first_decile_us"]["total_us"] == int(statistics.median(first_totals))
    assert row["first_decile_us"]["total_us"] == _NAMED_SUM + 10

    # the new mean field is not
    assert row["phase_decile_first_us"]["total_us"] == pytest.approx(
        statistics.fmean(first_totals))
    assert row["phase_decile_first_us"]["total_us"] > row["first_decile_us"]["total_us"]

    assert row["phase_decile_last_us"]["total_us"] == pytest.approx(
        statistics.fmean(last_totals))
    assert row["phase_decile_last_us"]["total_us"] > row["last_decile_us"]["total_us"]

    # residual grows first -> last decile, mean-of-residual, matching the
    # injected outliers rather than the flat named-phase baseline
    assert row["residual_first_us"] == pytest.approx(statistics.fmean([10, 10, 1_000]))
    assert row["residual_last_us"] == pytest.approx(statistics.fmean([20, 20, 2_000]))
    assert row["residual_last_us"] > row["residual_first_us"]


def test_aggregate_commit_phases_rejects_an_empty_series():
    with pytest.raises(ValueError):
        CC._aggregate_commit_phases([])


def test_dir_entry_counts_reads_seg_and_manifests_subdirs(tmp_path):
    native_root = tmp_path / "native"
    (native_root / "seg").mkdir(parents=True)
    (native_root / "manifests").mkdir(parents=True)
    for i in range(7):
        (native_root / "seg" / f"{i:012d}.tgs").write_bytes(b"")
    for i in range(3):
        (native_root / "manifests" / f"{i}.json").write_text("{}")

    counts = CC._dir_entry_counts(native_root)
    assert counts == {"seg": 7, "manifests": 3}


def test_dir_entry_counts_is_zero_for_a_store_that_has_not_opened_yet(tmp_path):
    counts = CC._dir_entry_counts(tmp_path / "native")
    assert counts == {"seg": 0, "manifests": 0}


# --------------------------------------------------------------------------
# the 2026-09-15 chain-format guard (`_check_chain_format`) -- these need the
# native engine extension actually built, unlike everything above.
# --------------------------------------------------------------------------

FORMAT2_FIXTURE = ROOT / "tests" / "fixtures" / "format2_store"


def _legacy_store_copy(tmp_path: Path):
    """A writable copy of the vendored format-2 fixture `test_manifest_
    format2.py` also uses (built at 89c98b3, before format 3) -- copied
    rather than opened in place so a test failure never mutates the
    committed fixture."""
    import shutil

    dst = tmp_path / FORMAT2_FIXTURE.name
    shutil.copytree(FORMAT2_FIXTURE, dst)
    return dst


def test_check_chain_format_passes_on_a_fresh_store():
    """`cmd_commitcost` always builds its own store fresh via `tgms.open` --
    the guard must not reject the ordinary case."""
    pytest.importorskip("tgms._engine", reason="native engine extension not built")
    import tempfile

    import tgms
    from tgms import _engine

    root = Path(tempfile.mkdtemp(prefix="tgms-cc-guard-")) / "s"
    store = tgms.open(root, backend="native")
    try:
        got = CC._check_chain_format(store, allow_legacy=False)
        assert got == _engine.MANIFEST_FORMAT_VERSION
    finally:
        store.close()


def test_check_chain_format_refuses_a_downgraded_format2_store(tmp_path):
    """The exact failure mode the B1-v2 A/B's 2026-09-15 correction found:
    a store whose manifest chain is older than the loaded engine writes
    must be refused, loudly, naming both format numbers -- not silently
    timed as if it were current."""
    pytest.importorskip("tgms._engine", reason="native engine extension not built")
    from tgms import _engine
    from tgms.storage.native import NativeAdapter

    if not FORMAT2_FIXTURE.exists():
        pytest.skip("format2_store fixture not present in this checkout")

    root = _legacy_store_copy(tmp_path)
    try:
        adapter = NativeAdapter(root / "native")
    except Exception as e:  # noqa: BLE001 -- "if it opens under the current
        # engine": an incompatible fixture is a skip, not a failure, of this
        # guard test -- test_manifest_format2.py is what pins compatibility.
        pytest.skip(f"format2_store fixture did not open under this engine: {e}")

    try:
        assert adapter.manifest_format() == 2
        fake_store = types.SimpleNamespace(adapter=adapter)

        with pytest.raises(SystemExit) as excinfo:
            CC._check_chain_format(fake_store, allow_legacy=False)
        message = str(excinfo.value)
        assert "2" in message
        assert str(_engine.MANIFEST_FORMAT_VERSION) in message

        # the escape hatch: explicitly allowed, the legacy format is
        # returned rather than refused
        assert CC._check_chain_format(fake_store, allow_legacy=True) == 2
    finally:
        adapter.close()


def test_cmd_commitcost_records_the_chain_format_field():
    """Every commitcost row must say which manifest chain format it ran
    against -- the field the 2026-09-15 correction found missing entirely
    from the B1-v2 A/B's records."""
    pytest.importorskip("tgms._engine", reason="native engine extension not built")
    from tgms import _engine

    args = argparse.Namespace(
        batch_sizes="1", seed_rows=50, commits=5, allow_legacy_chain=False)
    rc, payload = CC.cmd_commitcost(args)
    assert rc == 0
    assert len(payload["records"]) == 1
    assert payload["records"][0]["chain_format"] == _engine.MANIFEST_FORMAT_VERSION
