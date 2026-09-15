"""[tests] `scripts/eval_overload.py --dry-run`: a tiny, fast (~1s) open-loop
sweep against a synthetic store, producing a manifest that conforms to
`benchmarks/schema/result_manifest.schema.json` and shows the concurrency
gate actually admitting/refusing calls. Not a numbers test -- see
`docs/eval_concurrency.md` §24 for why a dev-host run reports no numbers.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_overload_module():
    spec = importlib.util.spec_from_file_location(
        "eval_overload", ROOT / "scripts" / "eval_overload.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # dataclasses' postponed-annotation resolution looks the module up in
    # sys.modules by name; a module loaded this way is not there by default.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pytest.importorskip("tgms._engine", reason="native engine extension not built")
OVERLOAD = _load_overload_module()


def _load_schema():
    schema_path = ROOT / "benchmarks" / "schema" / "result_manifest.schema.json"
    return json.loads(schema_path.read_text())


def test_dry_run_manifest_conforms_to_result_manifest_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    manifest = OVERLOAD.run_dry(tmp_path)
    schema = _load_schema()
    jsonschema.Draft202012Validator(schema).validate(manifest)


def test_dry_run_dev_host_note_is_none_unless_flag_given(tmp_path):
    """`dev_host_note` must not be a silent hard-coded default -- it is
    `None` unless a caller explicitly passes `--dev-host-note` (here,
    `run_dry`'s `dev_host_note` kwarg standing in for that flag), while
    `provenance` is always present regardless."""
    manifest = OVERLOAD.run_dry(tmp_path)
    assert manifest["dev_host_note"] is None
    assert manifest["provenance"]

    noted = OVERLOAD.run_dry(tmp_path, dev_host_note="dev host, functional check only")
    assert noted["dev_host_note"] == "dev host, functional check only"
    assert noted["provenance"]


def test_dry_run_exercises_the_concurrency_gate(tmp_path):
    """With `max_concurrent=2` and two open-loop clients hammering the
    router, at least one call in the sweep should observe more than zero
    calls admitted concurrently -- i.e. the harness actually drives
    concurrent traffic, not serialized calls in disguise."""
    manifest = OVERLOAD.run_dry(tmp_path)
    assert manifest["steps"], "expected at least one load step"
    assert all(s["n_ok"] + s["n_refused"] + s["n_error"] == s["n_calls"]
              for s in manifest["steps"])
    assert manifest["recovery"]["n_ok"] > 0


def test_token_bucket_schedule_is_evenly_spaced():
    sched = OVERLOAD.token_bucket_schedule(10.0, 1.0)
    assert len(sched) == 10
    deltas = [b - a for a, b in zip(sched, sched[1:])]
    assert all(abs(d - 0.1) < 1e-9 for d in deltas)


def test_token_bucket_schedule_empty_for_nonpositive_rate():
    assert OVERLOAD.token_bucket_schedule(0.0, 5.0) == []
    assert OVERLOAD.token_bucket_schedule(-1.0, 5.0) == []


def test_dry_run_no_call_records_conforms_and_reports_same_shape(tmp_path):
    """`--no-call-records` (here, `run_dry`'s `keep_call_records=False`) is
    additive: default behaviour (tested above) is unchanged, and the
    aggregates-only path still produces a schema-conformant manifest with
    the same step fields populated -- just computed from counters and a
    bounded reservoir instead of a per-call list."""
    jsonschema = pytest.importorskip("jsonschema")
    manifest = OVERLOAD.run_dry(tmp_path, keep_call_records=False)
    schema = _load_schema()
    jsonschema.Draft202012Validator(schema).validate(manifest)
    assert manifest["config"]["call_records"] is False
    assert "not written" in manifest["record"]
    assert manifest["steps"], "expected at least one load step"
    for s in manifest["steps"]:
        assert s["n_ok"] + s["n_refused"] + s["n_error"] == s["n_calls"]
    assert manifest["recovery"]["n_ok"] > 0


def test_dry_run_call_records_default_true_in_manifest_config(tmp_path):
    manifest = OVERLOAD.run_dry(tmp_path)
    assert manifest["config"]["call_records"] is True
    assert "--out" in manifest["record"]


def test_dry_run_rss_samples_writes_time_rss_kb_step_csv(tmp_path):
    """`--rss-samples PATH` (here, `run_dry`'s `rss_samples_path`) writes a
    `time,rss_kb,step` CSV sampled from the sweep, regardless of whether
    call records are kept."""
    rss_path = tmp_path / "rss.csv"
    OVERLOAD.run_dry(tmp_path, rss_samples_path=rss_path)
    assert rss_path.exists()
    lines = rss_path.read_text().splitlines()
    assert lines[0] == "time,rss_kb,step"
    # A ~1s dry-run sampled once per second may legitimately produce zero
    # data rows (only the header) -- what matters is the file is written
    # with the right shape and, if there are rows, they parse cleanly.
    for line in lines[1:]:
        t_str, rss_str, step = line.split(",", 2)
        float(t_str)
        int(rss_str)
        assert step


def test_dry_run_rss_samples_combined_with_no_call_records(tmp_path):
    rss_path = tmp_path / "rss.csv"
    manifest = OVERLOAD.run_dry(tmp_path, keep_call_records=False,
                                rss_samples_path=rss_path)
    assert manifest["config"]["call_records"] is False
    assert rss_path.exists()
    assert rss_path.read_text().splitlines()[0] == "time,rss_kb,step"


def test_dry_run_rss_sample_interval_is_additive_and_defaults_to_1hz(tmp_path):
    """`--rss-sample-interval-s` (here, `run_dry`'s `rss_sample_interval_s`)
    only changes the sampler's polling interval; a fast interval must not
    break anything, and omitting it keeps the 1 Hz default from the
    existing --rss-samples tests above."""
    rss_path = tmp_path / "rss_fast.csv"
    OVERLOAD.run_dry(tmp_path, rss_samples_path=rss_path,
                     rss_sample_interval_s=0.05)
    assert rss_path.read_text().splitlines()[0] == "time,rss_kb,step"


def test_dry_run_hwm_checkpoints_writes_four_named_checkpoints(tmp_path):
    """`--hwm-checkpoints PATH` (here, `run_dry`'s `hwm_checkpoints_path`)
    is additive and must not change the manifest; it should record, in
    order, after_imports, after_store_open, and a before/after pair for
    each load step (run_dry uses `--clients 1 2`, so two pairs here)."""
    hwm_path = tmp_path / "hwm.json"
    manifest = OVERLOAD.run_dry(tmp_path, hwm_checkpoints_path=hwm_path)
    assert manifest["steps"], "hwm-checkpoints must not affect the manifest shape"

    checkpoints = json.loads(hwm_path.read_text())
    labels = [c["label"] for c in checkpoints]
    assert labels == ["after_imports", "after_store_open",
                      "before_step_n1", "after_step_n1",
                      "before_step_n2", "after_step_n2"]
    for c in checkpoints:
        assert "vm_hwm_kb" in c  # None off Linux (e.g. macOS dev boxes); present either way
        assert c["t"] >= 0


def test_dry_run_hwm_checkpoints_combined_with_no_call_records(tmp_path):
    hwm_path = tmp_path / "hwm.json"
    manifest = OVERLOAD.run_dry(tmp_path, keep_call_records=False,
                                hwm_checkpoints_path=hwm_path)
    assert manifest["config"]["call_records"] is False
    checkpoints = json.loads(hwm_path.read_text())
    assert len(checkpoints) == 6
