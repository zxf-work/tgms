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
