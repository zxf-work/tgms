"""`benchmarks/ladder-v1` — the frozen overhead-ladder plan set and campaign
(lane D4b, OSDI'27). tmp_path/subprocess-only, seconds: this file never
opens a store under `stores/` and never measures a reported number — it
checks the frozen artifacts' own contract (they parse, they cover the
operator catalogue, their sha256s match the freeze, and they run end to end
through the harness's `--smoke` path).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLANS_DIR = ROOT / "benchmarks" / "ladder-v1" / "plans"
CAMPAIGN_PATH = ROOT / "benchmarks" / "ladder-v1" / "campaign.yaml"
SCHEMA_PATH = ROOT / "benchmarks" / "schema" / "result_manifest.schema.json"

PLAN_FILES = sorted(PLANS_DIR.glob("*.json"))


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "bench_overhead_ladder", ROOT / "scripts" / "bench_overhead_ladder.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ladder = _load_harness()


def _catalogue() -> set[str]:
    """The operator catalogue, computed from the code -- never hard-coded.
    `tgms.tgir.leaves.LEAF_SCOPES` (the 13 derivable read operators) union
    `compute` (the empty-scope control) is exactly the 14-of-15 population
    `tgms.eval.storm.py`'s own `TEMPLATES` tuple documents; `resolve_entities`
    is excluded by ruling (D-161/§13.8.1), never a workload sample."""
    from tgms.tgir.leaves import LEAF_SCOPES

    return set(LEAF_SCOPES) | {"compute"}


# --------------------------------------------------------------------------- #
# every plan file parses as agent-IR                                          #
# --------------------------------------------------------------------------- #

def test_plan_set_is_nonempty():
    assert len(PLAN_FILES) >= 10


@pytest.mark.parametrize("path", PLAN_FILES, ids=lambda p: p.stem)
def test_each_plan_file_parses(path):
    obj = json.loads(path.read_text())
    plan = ladder.load_plan(path)
    assert plan.plan_id == obj["plan_id"]
    assert len(plan.steps) == len(obj["steps"])
    assert 1 <= len(plan.steps) <= 4


# --------------------------------------------------------------------------- #
# the plan set's union of ops covers the catalogue, computed from the code    #
# --------------------------------------------------------------------------- #

def test_plan_set_covers_the_full_operator_catalogue():
    catalogue = _catalogue()
    covered: set[str] = set()
    for path in PLAN_FILES:
        plan = ladder.load_plan(path)
        covered.update(step.op for step in plan.steps)

    missing = catalogue - covered
    assert not missing, f"catalogue operators with no plan coverage: {missing}"
    # every op any plan uses must itself be in the catalogue (no stray ops
    # that would silently inflate a "coverage" count against a wrong set)
    assert covered <= catalogue, f"ops outside the catalogue: {covered - catalogue}"


def test_rung2_population_has_a_plan_with_both_compiled_operators():
    """The campaign brief requires one plan exercising both `entity_history`
    and `version_history` together so rung 2 has population from this
    plan set's own coverage, not just from the imported P2 harness's
    canned per-op arguments."""
    from tgms.tgir.compiled import COMPILED

    compiled_ops = set(COMPILED)
    assert compiled_ops == {"entity_history", "version_history"}
    for path in PLAN_FILES:
        plan = ladder.load_plan(path)
        ops = {step.op for step in plan.steps}
        if compiled_ops <= ops:
            return
    pytest.fail("no plan in benchmarks/ladder-v1/plans touches both "
                "entity_history and version_history")


# --------------------------------------------------------------------------- #
# campaign.yaml's sha256 list matches the files on disk                       #
# --------------------------------------------------------------------------- #

def test_campaign_yaml_shas_match_plan_files():
    campaign = yaml.safe_load(CAMPAIGN_PATH.read_text())
    listed = {entry["file"]: entry["sha256"] for entry in campaign["plans"]}

    on_disk = {p.name for p in PLAN_FILES}
    assert set(listed) == on_disk, (
        f"campaign.yaml plan list vs. plans/ directory mismatch: "
        f"{set(listed) ^ on_disk}")

    for name, expected_sha in listed.items():
        actual = hashlib.sha256((PLANS_DIR / name).read_bytes()).hexdigest()
        assert actual == expected_sha, f"{name}: sha256 mismatch"


def test_campaign_yaml_declares_full_catalogue_coverage():
    campaign = yaml.safe_load(CAMPAIGN_PATH.read_text())
    catalogue = _catalogue()
    assert campaign["catalogue_coverage"] == f"{len(catalogue)}/{len(catalogue)}"


# --------------------------------------------------------------------------- #
# end to end: --smoke --plans benchmarks/ladder-v1/plans, schema-valid,       #
# all five rungs, tmp_path store only                                        #
# --------------------------------------------------------------------------- #

def test_smoke_run_over_the_ladder_v1_plan_dir_is_schema_valid_five_rungs(tmp_path):
    out = tmp_path / "record.json"
    old_argv = sys.argv
    sys.argv = ["bench_overhead_ladder.py", "--smoke",
               "--plans", str(PLANS_DIR), "--out", str(out)]
    try:
        assert ladder.main() == 0
    finally:
        sys.argv = old_argv

    data = json.loads(out.read_text())
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(data, schema)  # raises on failure

    rungs = sorted(set(r["rung"] for r in data["rows"]))
    assert rungs == [1, 2, 3, 4, 5]

    plan_ids = {json.loads(p.read_text())["plan_id"] for p in PLAN_FILES}
    seen_plan_ids = {r["plan_id"] for r in data["rows"]}
    assert plan_ids <= seen_plan_ids

    # rung 5's tool-call count must equal the executed step count for every
    # plan (the falsifier `campaign.yaml` names first) -- on the smoke
    # store's n1/n2/n3 fixture every step in every plan resolves, so this is
    # an exact check here, not just "<=".
    for row in data["rows"]:
        if row["rung"] == 5:
            assert row["tool_calls"] == row["n_steps"], row
