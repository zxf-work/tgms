"""Schema tests for `benchmarks/schema/result_manifest.schema.json` (P0.4).

The schema formalizes what a benchmark result manifest should carry so a
result can be checked and reproduced without reading the harness that
produced it (see `benchmarks/schema/README.md` for the fields and the audit
of the existing corpus against it — the audit's own finding is that no
legacy manifest conforms, which these tests do not re-litigate). These tests
cover the schema's own mechanics: a conforming example validates, a required
top-level section's absence is caught, and the `dataset.digest_kind` and
nullable-`seed` constraints are enforced as specified.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = (Path(__file__).resolve().parent.parent / "benchmarks"
               / "schema" / "result_manifest.schema.json")


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture()
def valid_manifest() -> dict:
    return {
        "schema_version": "1.0.0",
        "git_commit": "feaab3f",
        "timestamp_utc": "2026-09-13T00:00:00Z",
        "machine": {
            "host": "xzgpu",
            "platform": "Linux-5.4.0-216-generic-x86_64",
            "cpus": 40,
            "ram_gb": 128,
        },
        "config": "configs/eval_10m.yaml",
        "seed": {"value": 12345},
        "dataset": {
            "name": "synth",
            "digest": "a1b2c3d4",
            "digest_kind": "store_digest",
        },
        "result_digest": "deadbeef",
        "protocol": {
            "warmups": 5,
            "reps": 30,
            "ceilings": {},
        },
        "record": "benchmarks/results-v1/eval-10m-d047.json",
    }


def test_valid_example_passes(schema, valid_manifest):
    jsonschema.validate(valid_manifest, schema)


def test_missing_machine_fails(schema, valid_manifest):
    bad = copy.deepcopy(valid_manifest)
    del bad["machine"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_digest_kind_enum_enforced(schema, valid_manifest):
    bad = copy.deepcopy(valid_manifest)
    bad["dataset"]["digest_kind"] = "checksum"  # not in the enum
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_null_seed_without_reason_fails(schema, valid_manifest):
    bad = copy.deepcopy(valid_manifest)
    bad["seed"] = {"value": None}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_null_seed_with_reason_passes(schema, valid_manifest):
    ok = copy.deepcopy(valid_manifest)
    ok["seed"] = {"value": None, "reason": "workload is seedless"}
    jsonschema.validate(ok, schema)


def test_missing_top_level_required_field_fails(schema, valid_manifest):
    bad = copy.deepcopy(valid_manifest)
    del bad["result_digest"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
