"""Unit tests for `scripts/external_record.py` — the external-baseline
campaign record assembler (lane C1).

As in `tests/test_external_check.py`, everything is a tiny synthetic cell
built in `tmp_path`; the one real exported cell
(`collegemsg-c3-none-n1000-s0`, on xzgpu) is never copied to the laptop.
These tests cover: cell-id construction, the committed storm-v2 lookup
(against a tiny hand-built grid file, not the real 36-cell one, so the
test is not coupled to that file's contents), age-band classification,
prediction arithmetic for both campaigns, and that the assembled record
validates against `benchmarks/schema/result_manifest.schema.json`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "benchmarks" / "schema" / "result_manifest.schema.json"

MODULE_PATH = ROOT / "scripts" / "external_record.py"
_spec = importlib.util.spec_from_file_location("external_record", MODULE_PATH)
external_record = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("external_record", external_record)
assert _spec.loader is not None
_spec.loader.exec_module(external_record)

CHECK_MODULE_PATH = ROOT / "scripts" / "external_check.py"
_check_spec = importlib.util.spec_from_file_location("external_check", CHECK_MODULE_PATH)
external_check = importlib.util.module_from_spec(_check_spec)
sys.modules.setdefault("external_check", external_check)
assert _check_spec.loader is not None
_check_spec.loader.exec_module(external_check)


# ---------------------------------------------------------------------------
# cell identity
# ---------------------------------------------------------------------------

def test_cell_id_matches_export_storm_workload_convention() -> None:
    assert external_record.cell_id("collegemsg", "c3", None, 1000, 0) == \
        "collegemsg-c3-none-n1000-s0"
    assert external_record.cell_id("synth-iv-60k", "c4", "deep", 1000, 2) == \
        "synth-iv-60k-c4-deep-n1000-s2"
    # the main grid's lean `per_cell` rows store age as the string "none"
    # rather than null -- both must produce the same id (module docstring).
    assert external_record.cell_id("collegemsg", "c3", "none", 1000, 0) == \
        "collegemsg-c3-none-n1000-s0"


# ---------------------------------------------------------------------------
# committed storm-v2 lookup
# ---------------------------------------------------------------------------

def _write_tiny_main_grid(path: Path) -> None:
    grid = {
        "per_cell": [
            {"task_id": 0, "store": "collegemsg", "mix": "c3", "age": "none",
             "n_artifacts": 1000, "seed": 0,
             "arms": {
                 "global-recompute": {"ttf_p50_ms": 100000.0, "false_fresh": 0,
                                      "false_stale": 900},
                 "tgms-L1": {"ttf_p50_ms": 5000.0, "false_fresh": 0, "false_stale": 900},
             }},
            {"task_id": 1, "store": "synth-iv-60k", "mix": "c4", "age": "deep",
             "n_artifacts": 1000, "seed": 0,
             "arms": {
                 "global-recompute": {"ttf_p50_ms": 200000.0},
                 "tgms-L1": {"ttf_p50_ms": 8000.0},
             }},
        ],
    }
    path.write_text(json.dumps(grid))


def _write_tiny_probe(path: Path) -> None:
    probe = {
        "config": {"store": "synth-iv-60k", "mix": "c1", "age": None,
                  "n_artifacts": 10000, "seed": 0},
        "summary": {"arms": {
            "global-recompute": {"ttf_p50_ms": 806290.0},
            "tgms-L1": {"ttf_p50_ms": 300000.0},
        }},
    }
    path.write_text(json.dumps(probe))


def test_load_storm_v2_committed(tmp_path: Path) -> None:
    grid_path = tmp_path / "grid.json"
    probe_path = tmp_path / "probe.json"
    _write_tiny_main_grid(grid_path)
    _write_tiny_probe(probe_path)

    committed = external_record.load_storm_v2_committed(grid_path, probe_path)
    assert "collegemsg-c3-none-n1000-s0" in committed
    assert committed["collegemsg-c3-none-n1000-s0"]["arms"]["tgms-L1"]["ttf_p50_ms"] == 5000.0
    assert "synth-iv-60k-c4-deep-n1000-s0" in committed
    assert "synth-iv-60k-c1-none-n10000-s0" in committed  # the probe cell
    assert committed["synth-iv-60k-c1-none-n10000-s0"]["source"].endswith("probe.json")


def test_load_storm_v2_committed_missing_files_is_empty(tmp_path: Path) -> None:
    committed = external_record.load_storm_v2_committed(
        tmp_path / "nope.json", tmp_path / "nope2.json")
    assert committed == {}


# ---------------------------------------------------------------------------
# age-band classification (memo A5/A6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("generator,mix,expected", [
    ("age_recent", "c3", "recent"),
    ("age_hours", "c3", "hours"),
    ("age_days", "c3", "days"),
    ("c4_burst", "c4", "deep"),
    ("c4_burst", "c1", None),  # same name, wrong mix -- not a deep burst (A5)
    ("a1_events", "c3", None),  # ordinary class-A burst, not age-banded at all
])
def test_band_for_burst(generator: str, mix: str, expected: str | None) -> None:
    assert external_record.band_for_burst(generator, mix) == expected


def test_load_burst_generators(tmp_path: Path) -> None:
    export_dir = tmp_path / "cell"
    export_dir.mkdir()
    (export_dir / "deltas.jsonl").write_text(
        json.dumps({"epoch": 1, "generator": "age_recent", "tt": 1,
                   "correction_class": "A", "placement": "x",
                   "closed": [], "inserted": []}) + "\n"
        + json.dumps({"epoch": 2, "generator": "age_hours", "tt": 2,
                     "correction_class": "A", "placement": "x",
                     "closed": [], "inserted": []}) + "\n")
    gens = external_record.load_burst_generators(export_dir)
    assert gens == {1: "age_recent", 2: "age_hours"}


def test_load_burst_generators_missing_file(tmp_path: Path) -> None:
    assert external_record.load_burst_generators(tmp_path / "nope") == {}


# ---------------------------------------------------------------------------
# a full synthetic cell, end to end
# ---------------------------------------------------------------------------

def _write_cell(export_dir: Path, *, cell_id: str, mix: str, age: str | None,
                n_artifacts: int) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "export-manifest.json").write_text(json.dumps({
        "cell_id": cell_id, "cell_digest": "abc123",
        "config": {"store": "tiny", "mix": mix, "age": age, "n_artifacts": n_artifacts,
                  "seed": 0},
    }))
    (export_dir / "digests.json").write_text(json.dumps({
        "cell_id": cell_id, "equality_level": "L2-partial",
    }))
    (export_dir / "deltas.jsonl").write_text(
        json.dumps({"epoch": 1, "generator": "age_recent", "tt": 1,
                   "correction_class": "A", "placement": "x",
                   "closed": [], "inserted": []}) + "\n"
        + json.dumps({"epoch": 2, "generator": "age_recent", "tt": 2,
                     "correction_class": "A", "placement": "x",
                     "closed": [], "inserted": []}) + "\n")


def _write_check(check_path: Path, *, config_kind: str, cell_id: str = "x") -> None:
    check_path.write_text(json.dumps({
        "schema_version": "1.0.0", "cell_id": cell_id, "cell_digest": "abc123",
        "export_manifest_verified": True, "config_kind": config_kind,
        "n_registered": 10, "epochs": [0, 1, 2],
        "per_burst": [], "notes": [],
        "totals": {"n_compared": 20, "agree": 20, "disagree": 0, "not_answered": 0,
                  "oracle_refused": 0, "false_fresh": 0, "false_stale": None},
    }))


def _write_ivmdd_result(result_path: Path) -> None:
    result_path.write_text(json.dumps({
        "config": {"routing": {}},
        "versions": {"crate_version": "0.1.0", "timely": "0.31.0",
                    "differential_dataflow": "0.25.1"},
        "host_snapshots": [{"label": "start", "loadavg1": 1.0},
                          {"label": "end", "loadavg1": 1.2}],
        "per_burst": [
            {"epoch": 1, "artifacts_refreshed": ["a"], "artifacts_refreshed_count": 1,
             "refresh_ms": 2.0, "publish_ms": 0.2, "refresh_plus_publish_ms": 2.2},
            {"epoch": 2, "artifacts_refreshed": ["b"], "artifacts_refreshed_count": 1,
             "refresh_ms": 3.0, "publish_ms": 0.3, "refresh_plus_publish_ms": 3.3},
        ],
        "gates": {"oracle_agreement": {"agree": 20, "disagree": [], "disagree_count": 0,
                                       "not_answered": 0}},
    }))


def _write_neo4j_result(result_path: Path) -> None:
    result_path.write_text(json.dumps({
        "config": {"cypher": {}, "neo4j_version": "5.26.0", "apoc_version": "5.26.0-core",
                  "jdk_version": "Temurin 21.0.12", "driver_version": "5.28.6"},
        "machine": {"host": "xzgpu", "platform": "Linux", "cpus": 40, "ram_gb": 93.0},
        "per_cell": {"per_burst": [
            {"epoch": 1, "apply_ms": 1.0, "recompute_ms": 50.0, "wall_ms": 51.0,
             "agree": 10, "disagree": [], "not_answered": [], "oracle_refused": []},
            {"epoch": 2, "apply_ms": 1.0, "recompute_ms": 60.0, "wall_ms": 61.0,
             "agree": 10, "disagree": [], "not_answered": [], "oracle_refused": []},
        ]},
    }))


def test_build_record_ivm_differential(tmp_path: Path) -> None:
    export_dir = tmp_path / "export" / "tiny-c3-recent-n100-s0"
    _write_cell(export_dir, cell_id="tiny-c3-recent-n100-s0", mix="c3", age="recent",
               n_artifacts=100)
    result_path = tmp_path / "result.json"
    _write_ivmdd_result(result_path)
    check_path = tmp_path / "check.json"
    _write_check(check_path, config_kind="ivm-dd", cell_id="tiny-c3-recent-n100-s0")

    grid_path = tmp_path / "grid.json"
    probe_path = tmp_path / "probe.json"
    _write_tiny_main_grid(grid_path)
    # add a committed row for the recent-band cell itself so the ratio is computable:
    grid = json.loads(grid_path.read_text())
    grid["per_cell"].append({"task_id": 2, "store": "tiny", "mix": "c3", "age": "recent",
                             "n_artifacts": 100, "seed": 0,
                             "arms": {"tgms-L1": {"ttf_p50_ms": 2.0}}})
    grid_path.write_text(json.dumps(grid))

    ci = external_record.CellInput(export_dir, result_path, check_path)
    committed = external_record.load_storm_v2_committed(grid_path, probe_path)
    record, rows = external_record.build_record(
        "ivm-differential", [ci], git_commit="deadbeef", timestamp_utc="2026-10-02T00:00:00Z",
        committed=committed)

    assert record["schema_version"] == "1.0.0"
    assert record["config"]["campaign"] == "ivm-differential"
    assert len(rows) == 1
    assert rows[0]["cell_id"] == "tiny-c3-recent-n100-s0"
    assert rows[0]["equality_level"] == "L2-partial"

    per_cell = record["summary"]["per_cell"][0]
    assert per_cell["refresh_wall_ms"]["median"] == 2.75  # median(2.2, 3.3)
    assert per_cell["refresh_wall_ms"]["sum"] == pytest.approx(5.5)
    assert per_cell["oracle_agreement"]["agree"] == 20

    ext2 = record["summary"]["predictions_measured"]["ext2"]
    recent = ext2["a_b_per_band"]["recent"]
    assert recent["n_bursts_ivm"] == 2  # both bursts are generator "age_recent"
    assert recent["ivm_median_ms"] == 2.75
    assert recent["tgms_l1_median_ms"] == 2.0
    assert recent["ratio_ivm_over_l1"] == pytest.approx(2.75 / 2.0)
    assert ext2["d_pass"] is True
    assert ext2["d_disagree_total"] == 0


def test_build_record_neo4j_recompute(tmp_path: Path) -> None:
    export_dir = tmp_path / "export" / "tiny-c3-none-n100-s0"
    _write_cell(export_dir, cell_id="tiny-c3-none-n100-s0", mix="c3", age=None,
               n_artifacts=100)
    result_path = tmp_path / "result.json"
    _write_neo4j_result(result_path)
    check_path = tmp_path / "check.json"
    _write_check(check_path, config_kind="neo4j", cell_id="tiny-c3-none-n100-s0")

    grid_path = tmp_path / "grid.json"
    grid = {"per_cell": [{"task_id": 0, "store": "tiny", "mix": "c3", "age": "none",
                         "n_artifacts": 100, "seed": 0,
                         "arms": {"global-recompute": {"ttf_p50_ms": 100.0},
                                 "tgms-L1": {"ttf_p50_ms": 10.0}}}]}
    grid_path.write_text(json.dumps(grid))

    ci = external_record.CellInput(export_dir, result_path, check_path)
    committed = external_record.load_storm_v2_committed(grid_path, tmp_path / "noprobe.json")
    record, rows = external_record.build_record(
        "neo4j-recompute", [ci], git_commit="deadbeef", timestamp_utc="2026-10-02T00:00:00Z",
        committed=committed)

    ext1 = record["summary"]["predictions_measured"]["ext1"]
    # median(recompute_ms) = median(50, 60) = 55
    assert ext1["a_ratio_gr_median"] == pytest.approx(55.0 / 100.0)
    assert ext1["b_speedup_median"] == pytest.approx(55.0 / 10.0)
    assert ext1["b_n_cells_meeting_half_margin"] == 1  # 5.5 >= 2.586
    assert ext1["d_pass"] is True
    assert ext1["per_cell"]["tiny-c3-none-n100-s0"]["committed_source"].endswith("grid.json")


def test_build_record_missing_committed_row_is_null_not_guessed(tmp_path: Path) -> None:
    export_dir = tmp_path / "export" / "tiny-c3-none-n100-s0"
    _write_cell(export_dir, cell_id="tiny-c3-none-n100-s0", mix="c3", age=None,
               n_artifacts=100)
    result_path = tmp_path / "result.json"
    _write_neo4j_result(result_path)
    check_path = tmp_path / "check.json"
    _write_check(check_path, config_kind="neo4j", cell_id="tiny-c3-none-n100-s0")

    ci = external_record.CellInput(export_dir, result_path, check_path)
    record, _ = external_record.build_record(
        "neo4j-recompute", [ci], git_commit="deadbeef", timestamp_utc="2026-10-02T00:00:00Z",
        committed={})  # no committed grid at all

    ext1 = record["summary"]["predictions_measured"]["ext1"]
    assert ext1["a_ratio_gr_median"] is None
    assert ext1["b_speedup_median"] is None
    assert "no committed storm-v2 row" in ext1["per_cell"]["tiny-c3-none-n100-s0"]["note"]


# ---------------------------------------------------------------------------
# schema conformance + round trip through write_record
# ---------------------------------------------------------------------------

def test_written_record_conforms_to_result_manifest_schema(tmp_path: Path) -> None:
    export_dir = tmp_path / "export" / "tiny-c3-none-n100-s0"
    _write_cell(export_dir, cell_id="tiny-c3-none-n100-s0", mix="c3", age=None,
               n_artifacts=100)
    result_path = tmp_path / "result.json"
    _write_ivmdd_result(result_path)
    check_path = tmp_path / "check.json"
    _write_check(check_path, config_kind="ivm-dd", cell_id="tiny-c3-none-n100-s0")

    ci = external_record.CellInput(export_dir, result_path, check_path)
    record, rows = external_record.build_record(
        "ivm-differential", [ci], git_commit="deadbeef", timestamp_utc="2026-10-02T00:00:00Z",
        committed={})

    out_dir = tmp_path / "out"
    record_path, rows_path = external_record.write_record(
        record, rows, out_dir, "ivm-differential", "2026-10-02")
    assert record_path.exists() and rows_path.exists()
    assert len(rows_path.read_text().splitlines()) == 1

    schema = json.loads(SCHEMA_PATH.read_text())
    written = json.loads(record_path.read_text())
    jsonschema.Draft202012Validator(schema).validate(written)


def test_build_record_rejects_unknown_campaign(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        external_record.build_record("not-a-campaign", [], git_commit="x",
                                     timestamp_utc="2026-10-02T00:00:00Z", committed={})


# ---------------------------------------------------------------------------
# tgms-control (lane T1, Addendum EXT-A A1) -- the same-host control
# ---------------------------------------------------------------------------

def test_parse_loadavg1() -> None:
    text = (" 16:11:22 up 2 days,  1:05,  0 users,  load average: 1.03, 1.07, 1.08\n"
           "              total        used        free\n")
    assert external_record.parse_loadavg1(text) == 1.03


def test_parse_loadavg1_missing_line() -> None:
    assert external_record.parse_loadavg1("no load line here\n") is None


def _write_t1_cell(t1_cell_dir: Path, *, store: str, mix: str, age: str | None,
                   n_artifacts: int, seed: int = 0, store_path_prefix: str = "t1-stores/",
                   l1_match: bool = True) -> None:
    t1_cell_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0.0",
        "git_commit": "fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty",
        "timestamp_utc": "2026-10-05T00:00:00Z",
        "machine": {"host": "xzgpu", "platform": "Linux", "cpus": 40, "ram_gb": 93.0},
        "config": {"store": f"{store_path_prefix}{store}", "mix": mix, "age": age,
                  "n_artifacts": n_artifacts, "seed": seed, "batches": 20,
                  "wall_s": 9000.0},
        "seed": {"value": seed},
        "dataset": {"name": store, "digest": "deadbeef", "digest_kind": "eventlog_sha"},
        "result_digest": "cafe",
        "protocol": {"reps": 1, "warmups": 0},
        "record": "storm-rows.jsonl",
        "summary": {
            "arms": {
                "global-recompute": {"ttf_p50_ms": 100000.0, "ttf_p95_ms": 110000.0,
                                     "false_fresh": 0, "false_stale": 900,
                                     "avoided_recompute_wall": 0.0},
                "tgms-L1": {"ttf_p50_ms": 5000.0, "ttf_p95_ms": 6000.0,
                           "false_fresh": 0, "false_stale": 900,
                           "avoided_recompute_wall": 0.75},
            },
        },
    }
    (t1_cell_dir / "storm-tiny-0.json").write_text(json.dumps(manifest))
    equality = {
        "cell_id": external_record.cell_id(store, mix, age, n_artifacts, seed),
        "batches_realized": 20,
        "git_commit": "fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty",
        "l1_eventlog_sha_match": l1_match,
        "t1_eventlog_sha256": "d0f52ed5" if l1_match else "mismatch",
    }
    (t1_cell_dir / "t1-equality.json").write_text(json.dumps(equality))
    (t1_cell_dir / "host-snapshot-before.txt").write_text(
        "== 2026-10-05T00:00:00Z ==\n 00:00:00 up 1 day, load average: 1.03, 1.07, 1.08\n")
    (t1_cell_dir / "host-snapshot-after.txt").write_text(
        "== 2026-10-05T02:00:00Z ==\n 02:00:00 up 1 day, load average: 1.10, 1.09, 1.08\n")


def test_find_t1_manifest_requires_exactly_one(tmp_path: Path) -> None:
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    with pytest.raises(ValueError):
        external_record.find_t1_manifest(cell_dir)
    (cell_dir / "storm-a-0.json").write_text("{}")
    (cell_dir / "storm-a-0-rows.jsonl").write_text("")  # must be excluded, not ambiguous
    assert external_record.find_t1_manifest(cell_dir).name == "storm-a-0.json"
    (cell_dir / "storm-b-0.json").write_text("{}")
    with pytest.raises(ValueError):
        external_record.find_t1_manifest(cell_dir)


def test_t1_cell_input_strips_store_path(tmp_path: Path) -> None:
    cell_dir = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_dir, store="tiny", mix="c3", age=None, n_artifacts=1000)
    ci = external_record.T1CellInput(cell_dir)
    assert ci.store_name == "tiny"
    assert ci.cell_id == "tiny-c3-none-n1000-s0"
    assert ci.mix == "c3" and ci.age is None


def test_build_t1_record(tmp_path: Path) -> None:
    cell_a = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_a, store="tiny", mix="c3", age=None, n_artifacts=1000)
    cell_b = tmp_path / "tiny-c3-recent-n1000-s0"
    _write_t1_cell(cell_b, store="tiny", mix="c3", age="recent", n_artifacts=1000,
                   l1_match=False)

    cells = [external_record.T1CellInput(cell_a), external_record.T1CellInput(cell_b)]
    record, rows = external_record.build_t1_record(
        cells, git_commit="fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty",
        timestamp_utc="2026-10-05T00:00:00Z")

    assert record["schema_version"] == "1.0.0"
    assert record["config"]["campaign"] == "tgms-control"
    assert record["config"]["cell_ids"] == ["tiny-c3-none-n1000-s0", "tiny-c3-recent-n1000-s0"]
    assert record["machine"]["host"] == "xzgpu"
    assert len(rows) == 2

    per_cell = {row["cell_id"]: row for row in record["summary"]["per_cell"]}
    a = per_cell["tiny-c3-none-n1000-s0"]
    assert a["equality_level"] == "L1"
    assert a["batches_realized"] == 20
    assert a["speedup_global_recompute_over_l1"] == pytest.approx(100000.0 / 5000.0)
    assert a["arms"]["tgms-L1"]["ttf_p50_ms"] == 5000.0
    assert a["host_load"] == {"before": 1.03, "after": 1.10}

    b = per_cell["tiny-c3-recent-n1000-s0"]
    assert b["equality_level"] == "no-L1 (eventlog sha mismatch)"

    assert rows[0]["cell_id"] == "tiny-c3-none-n1000-s0"
    assert "manifest" in rows[0] and "t1_equality" in rows[0]


def _write_t1_rows_sidecar(t1_cell_dir: Path, *, check_cache: str = "chain") -> None:
    """A tiny 3-batch `storm-tiny-0-rows.jsonl` sidecar beside `_write_t1_cell`'s
    own `storm-tiny-0.json` manifest -- `BatchResult.to_json()`'s shape,
    restated minimally (just the fields `e2e_and_check_cache_aggregates`
    reads): `check_cache`/`check_cache_misses` per batch, `e2e_refresh_calls`
    on `tgms-L1` only (the other arm, `global-recompute`, never times an
    end-to-end interval, matching `ArmOutcome.to_json`'s real behavior of
    omitting the field entirely for an arm that never set it)."""
    rows = [
        {"batch_index": 0, "check_cache": check_cache, "check_cache_misses": 1,
         "arms": {"global-recompute": {"arm": "global-recompute"},
                  "tgms-L1": {"arm": "tgms-L1", "e2e_refresh_calls": 200}}},
        {"batch_index": 1, "check_cache": check_cache, "check_cache_misses": 1,
         "arms": {"global-recompute": {"arm": "global-recompute"},
                  "tgms-L1": {"arm": "tgms-L1", "e2e_refresh_calls": 240}}},
        {"batch_index": 2, "check_cache": check_cache, "check_cache_misses": 3,
         "arms": {"global-recompute": {"arm": "global-recompute"},
                  "tgms-L1": {"arm": "tgms-L1", "e2e_refresh_calls": 260}}},
    ]
    with (t1_cell_dir / "storm-tiny-0-rows.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_e2e_and_check_cache_aggregates_empty_rows() -> None:
    agg = external_record.e2e_and_check_cache_aggregates([])
    assert agg == {"check_cache": None, "check_cache_misses_median": None,
                   "e2e_refresh_calls_median": {}}


def test_e2e_and_check_cache_aggregates_from_rows(tmp_path: Path) -> None:
    cell_dir = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_dir, store="tiny", mix="c3", age=None, n_artifacts=1000)
    _write_t1_rows_sidecar(cell_dir)

    ci = external_record.T1CellInput(cell_dir)
    rows = ci.load_batch_rows()
    assert len(rows) == 3

    agg = external_record.e2e_and_check_cache_aggregates(rows)
    assert agg["check_cache"] == "chain"
    assert agg["check_cache_misses_median"] == 1  # median of [1, 1, 3]
    assert agg["e2e_refresh_calls_median"] == {"tgms-L1": 240}  # median of [200, 240, 260]
    assert "global-recompute" not in agg["e2e_refresh_calls_median"]


def test_t1_cell_input_load_batch_rows_missing_sidecar(tmp_path: Path) -> None:
    cell_dir = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_dir, store="tiny", mix="c3", age=None, n_artifacts=1000)
    ci = external_record.T1CellInput(cell_dir)
    assert ci.load_batch_rows() == []


def test_summarize_t1_cell_carries_e2e_and_check_cache_when_sidecar_present(
        tmp_path: Path) -> None:
    cell_dir = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_dir, store="tiny", mix="c3", age=None, n_artifacts=1000)
    _write_t1_rows_sidecar(cell_dir)

    ci = external_record.T1CellInput(cell_dir)
    summary = external_record.summarize_t1_cell(ci)

    assert summary["check_cache"] == "chain"
    assert summary["check_cache_misses_median"] == 1
    assert summary["arms"]["tgms-L1"]["e2e_refresh_calls_median"] == 240
    # global-recompute never recorded the field -- never a fabricated 0/None key
    assert "e2e_refresh_calls_median" not in summary["arms"]["global-recompute"]


def test_summarize_t1_cell_without_sidecar_leaves_fields_none(tmp_path: Path) -> None:
    """The 2026-10-05 T1 cells have no `-rows.jsonl` sidecar on this host
    (module docstring) -- their summaries must keep working exactly as
    before, with the new fields explicitly absent/`None`, never guessed."""
    cell_dir = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_dir, store="tiny", mix="c3", age=None, n_artifacts=1000)
    ci = external_record.T1CellInput(cell_dir)
    summary = external_record.summarize_t1_cell(ci)

    assert summary["check_cache"] is None
    assert summary["check_cache_misses_median"] is None
    assert "e2e_refresh_calls_median" not in summary["arms"]["tgms-L1"]


def test_written_t1_record_conforms_to_result_manifest_schema(tmp_path: Path) -> None:
    cell_dir = tmp_path / "tiny-c3-none-n1000-s0"
    _write_t1_cell(cell_dir, store="tiny", mix="c3", age=None, n_artifacts=1000)
    ci = external_record.T1CellInput(cell_dir)
    record, rows = external_record.build_t1_record(
        [ci], git_commit="fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty",
        timestamp_utc="2026-10-05T00:00:00Z")

    out_dir = tmp_path / "out"
    record_path, rows_path = external_record.write_record(
        record, rows, out_dir, "tgms-control", "2026-10-05")
    assert record_path.exists() and rows_path.exists()

    schema = json.loads(SCHEMA_PATH.read_text())
    written = json.loads(record_path.read_text())
    jsonschema.Draft202012Validator(schema).validate(written)


# ---------------------------------------------------------------------------
# standalone withheld-hold record (lane D-W, memo P-EXT2-H)
# ---------------------------------------------------------------------------

def _withheld_check() -> dict:
    """A tiny `external_check.py::check_withheld`-shaped dict -- exactly
    what `tests/test_external_check.py::test_check_withheld_forwards_the_hold_duration_fields`
    exercises `check_withheld` into producing, restated here so this
    module's test does not import that test module."""
    return {
        "cell_id": "synth-iv-60k-c4-deep-n1000-s0",
        "ivm_f_epoch_false_fresh": 39,
        "ivm_f_epoch_probe_complete_through_10": True,
        "ivm_f_watermark_false_fresh": 0,
        "ivm_f_watermark_probe_complete_through_10": False,
        "ivm_f_watermark_unanswerable": True,
        "ivm_f_watermark_held_artifacts": 39,
        "ivm_f_watermark_refused_answers": 39,
        "ivm_f_watermark_hold_ms_median": 239.57,
        "ivm_f_watermark_hold_ms_min": 239.57,
        "ivm_f_watermark_hold_ms_max": 239.57,
        "ivm_f_watermark_hold_bursts_median": 1.0,
        "ivm_inter_burst_wall_ms": 239.57,
        "tgms_false_fresh": None,
        "tgms_stale_marked": None,
        "notes": [],
    }


def test_build_withheld_hold_record_carries_the_check_dict_verbatim() -> None:
    record = external_record.build_withheld_hold_record(
        _withheld_check(), git_commit="deadbeef", timestamp_utc="2026-10-09T14:30:27Z")

    assert record["schema_version"] == "1.0.0"
    assert record["config"]["campaign"] == "ivm-differential-withheld-hold"
    assert record["config"]["cell_id"] == "synth-iv-60k-c4-deep-n1000-s0"
    assert record["n_cells"] == 1
    assert record["summary"]["withheld"] == _withheld_check()
    # never silently drops the hold-duration fields this lane added:
    assert record["summary"]["withheld"]["ivm_f_watermark_held_artifacts"] == 39
    assert record["summary"]["withheld"]["ivm_inter_burst_wall_ms"] == 239.57


def test_build_withheld_hold_record_uses_the_supplied_host_block() -> None:
    host = {"host": "xzgpu", "platform": "Linux-test", "cpus": 8, "ram_gb": 64.0}
    record = external_record.build_withheld_hold_record(
        _withheld_check(), git_commit="deadbeef", timestamp_utc="2026-10-09T14:30:27Z",
        host=host)
    assert record["machine"] == host


def test_written_withheld_hold_record_conforms_to_result_manifest_schema(
        tmp_path: Path) -> None:
    record = external_record.build_withheld_hold_record(
        _withheld_check(), git_commit="deadbeef", timestamp_utc="2026-10-09T14:30:27Z")
    record_path = tmp_path / "ivm-differential-2026-10-09-withheld-hold.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    schema = json.loads(SCHEMA_PATH.read_text())
    written = json.loads(record_path.read_text())
    jsonschema.Draft202012Validator(schema).validate(written)


def test_cli_withheld_hold_only_writes_the_standalone_record(tmp_path: Path) -> None:
    withheld_path = tmp_path / "check-withheld.json"
    withheld_path.write_text(json.dumps(_withheld_check()))
    out_dir = tmp_path / "out"

    rc = external_record.main([
        "--withheld-hold-only", "--withheld-check", str(withheld_path),
        "--git-commit", "deadbeef", "--timestamp-utc", "2026-10-09T14:30:27Z",
        "--date", "2026-10-09", "--out-dir", str(out_dir),
    ])
    assert rc == 0

    record_path = out_dir / "ivm-differential-2026-10-09-withheld-hold.json"
    assert record_path.exists()
    written = json.loads(record_path.read_text())
    assert written["config"]["campaign"] == "ivm-differential-withheld-hold"
    assert written["summary"]["withheld"]["ivm_f_watermark_held_artifacts"] == 39

    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema).validate(written)


def test_cli_withheld_hold_only_requires_withheld_check(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        external_record.main(["--withheld-hold-only", "--out-dir", str(tmp_path)])


def test_cli_withheld_hold_only_rejects_campaign_flags(tmp_path: Path) -> None:
    withheld_path = tmp_path / "check-withheld.json"
    withheld_path.write_text(json.dumps(_withheld_check()))
    with pytest.raises(SystemExit):
        external_record.main([
            "--withheld-hold-only", "--withheld-check", str(withheld_path),
            "--campaign", "ivm-differential",
        ])


def test_cli_requires_campaign_without_withheld_hold_only() -> None:
    with pytest.raises(SystemExit):
        external_record.main([])
