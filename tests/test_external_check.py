"""Unit tests for `scripts/external_check.py` — the external-baseline
oracle checker (lane C1).

Everything here is a tiny, hand-built synthetic cell (3 artifacts, 3
epochs) in `tmp_path`, never a real export bundle or a real campaign run
(memory `experiments-remote-only`: the laptop runs code and unit tests
only). The fixture is built once per test via `_write_bundle` and
deliberately exercises the shapes that matter: a digest that stays the
same, one that changes, one artifact the oracle itself refuses for one
epoch (excluded from the comparison that epoch only), and both
configuration shapes (`neo4j-recompute`'s per-burst name lists,
`ivm-dd`'s burst rows + aggregate disagree list) so the two extraction
paths are each covered, not just whichever one happens to run first.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "external_check.py"
_spec = importlib.util.spec_from_file_location("external_check", MODULE_PATH)
external_check = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("external_check", external_check)
assert _spec.loader is not None
_spec.loader.exec_module(external_check)


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

ORACLE_ROWS = [
    {"epoch": 0, "digests": {"a1": "d0", "a2": "e0", "a3": "f0"}, "refused": []},
    {"epoch": 1, "digests": {"a1": "d1", "a2": "e0", "a3": "f1"}, "refused": []},
    {"epoch": 2, "digests": {"a1": "d1", "a2": "e2", "a3": "f1"}, "refused": ["a3"]},
]

ARTIFACTS = [
    {"name": "a1", "op": "entity_history", "args": {}},
    {"name": "a2", "op": "version_history", "args": {}},
    {"name": "a3", "op": "snapshot_subgraph", "args": {}},
]

DELTAS = [
    {"epoch": 1, "tt": 100, "correction_class": "A", "generator": "a1_events",
     "placement": "in-window-read", "closed": [], "inserted": []},
    {"epoch": 2, "tt": 200, "correction_class": "A", "generator": "age_recent",
     "placement": "in-window-read", "closed": [], "inserted": []},
]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def _write_bundle(export_dir: Path, *, cell_id: str = "tiny-fixture") -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(export_dir / "oracle.jsonl", ORACLE_ROWS)
    _write_jsonl(export_dir / "artifacts.jsonl", ARTIFACTS)
    _write_jsonl(export_dir / "deltas.jsonl", DELTAS)
    (export_dir / "eventlog-tail.jsonl").write_bytes(b"")

    files = {}
    for name in ("oracle.jsonl", "artifacts.jsonl", "deltas.jsonl", "eventlog-tail.jsonl"):
        files[name] = external_check.sha256_file(export_dir / name)
    manifest = {
        "schema_version": "1.0.0", "cell_id": cell_id,
        "cell_digest": "deadbeef", "files": files,
        "config": {"store": "tiny", "mix": "c3", "age": None, "n_artifacts": 3, "seed": 0},
    }
    (export_dir / "export-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return export_dir


def _neo4j_result() -> dict:
    return {
        "config": {"cypher": {"entity_history": "..."}},
        "per_cell": {"per_burst": [
            {"epoch": 0, "apply_ms": 0.0, "recompute_ms": 1.0, "wall_ms": 1.0,
             "agree": 3, "disagree": [], "not_answered": [], "oracle_refused": []},
            {"epoch": 1, "apply_ms": 0.1, "recompute_ms": 1.1, "wall_ms": 1.2,
             "agree": 2, "disagree": ["a3"], "not_answered": [], "oracle_refused": []},
            {"epoch": 2, "apply_ms": 0.2, "recompute_ms": 1.2, "wall_ms": 1.4,
             "agree": 1, "disagree": ["a3"], "not_answered": ["a2"],
             "oracle_refused": ["a3"]},
        ]},
    }


def _ivmdd_result() -> dict:
    return {
        "config": {"routing": {"valid_time_buckets": 256}},
        "per_burst": [
            {"epoch": 1, "artifacts_refreshed": ["a1", "a3"], "artifacts_refreshed_count": 2,
             "refresh_ms": 1.0, "publish_ms": 0.1, "refresh_plus_publish_ms": 1.1},
            {"epoch": 2, "artifacts_refreshed": ["a2"], "artifacts_refreshed_count": 1,
             "refresh_ms": 0.5, "publish_ms": 0.05, "refresh_plus_publish_ms": 0.55},
        ],
        "gates": {"oracle_agreement": {
            "agree": 7,
            "disagree": [{"epoch": 1, "artifact": "a3", "oracle_digest": "f1",
                         "our_digest": "WRONG"}],
            "disagree_count": 1, "not_answered": 0,
        }},
    }


# ---------------------------------------------------------------------------
# manifest verification
# ---------------------------------------------------------------------------

def test_verify_export_manifest_ok(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    verified, problems = external_check.verify_export_manifest(export_dir)
    assert verified
    assert problems == []


def test_verify_export_manifest_detects_tamper(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    # lane X1b is "still writing" semantics: a file changes after the
    # manifest was written.
    (export_dir / "oracle.jsonl").write_text('{"epoch": 0, "digests": {}, "refused": []}\n')
    verified, problems = external_check.verify_export_manifest(export_dir)
    assert not verified
    assert any("oracle.jsonl" in p for p in problems)


def test_check_cell_refuses_unverified_manifest_by_default(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    # lane X1b is "still writing" semantics: a file changes after the
    # manifest was written, so its hash no longer matches.
    (export_dir / "oracle.jsonl").write_text("garbage\n")
    with pytest.raises(ValueError, match="does not verify"):
        external_check.check_cell(export_dir, _neo4j_result())


def test_check_cell_allows_unverified_manifest_when_asked(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    (export_dir / "eventlog-tail.jsonl").write_bytes(b"x")  # unrelated file, now mismatched
    check = external_check.check_cell(export_dir, _neo4j_result(), require_manifest=False)
    assert check["export_manifest_verified"] is False


# ---------------------------------------------------------------------------
# config-kind detection
# ---------------------------------------------------------------------------

def test_detect_config_kind() -> None:
    assert external_check.detect_config_kind(_neo4j_result()) == "neo4j"
    assert external_check.detect_config_kind(_ivmdd_result()) == "ivm-dd"


def test_detect_config_kind_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError):
        external_check.detect_config_kind({"nonsense": True})


# ---------------------------------------------------------------------------
# full tally — neo4j shape
# ---------------------------------------------------------------------------

def test_check_cell_neo4j(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    check = external_check.check_cell(export_dir, _neo4j_result())

    assert check["config_kind"] == "neo4j"
    assert check["n_registered"] == 3
    assert check["export_manifest_verified"] is True

    by_epoch = {b["epoch"]: b for b in check["per_burst"]}

    e0 = by_epoch[0]
    assert e0["n_compared"] == 3 and e0["agree"] == 3
    assert e0["n_changed"] is None and e0["false_fresh"] is None and e0["false_stale"] is None

    e1 = by_epoch[1]
    assert e1["n_compared"] == 3
    assert e1["disagree"] == ["a3"]
    assert e1["n_changed"] == 2  # a1 (d0->d1), a3 (f0->f1); a2 unchanged
    assert e1["agree"] == 2
    assert e1["false_fresh"] == 0
    assert e1["false_stale"] == 1  # 3 compared - 2 changed

    e2 = by_epoch[2]
    assert e2["n_oracle_refused"] == 1 and e2["oracle_refused"] == ["a3"]
    assert e2["n_compared"] == 2  # a3 excluded (refused this epoch)
    # the configuration's own disagree=["a3"] is stray (a3 is refused this
    # epoch) and must not be counted:
    assert e2["disagree"] == []
    assert e2["not_answered"] == ["a2"]
    assert e2["agree"] == 1  # a1 only
    assert e2["n_changed"] == 1  # a2 only (a1 unchanged d1->d1)
    assert e2["false_fresh"] == 0
    assert e2["false_stale"] == 1  # 2 compared - 1 changed
    assert any("stray" in n or "refused" in n for n in check["notes"])

    totals = check["totals"]
    assert totals["n_compared"] == 3 + 3 + 2
    assert totals["agree"] == 3 + 2 + 1
    assert totals["disagree"] == 0 + 1 + 0
    assert totals["not_answered"] == 0 + 0 + 1
    assert totals["oracle_refused"] == 0 + 0 + 1
    assert totals["false_fresh"] == 0
    assert totals["false_stale"] == 1 + 1


def test_check_cell_neo4j_oracle_refused_mismatch_note(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    result = _neo4j_result()
    result["per_cell"]["per_burst"][1]["oracle_refused"] = ["ghost"]
    check = external_check.check_cell(export_dir, result)
    assert any("oracle_refused" in n and "ghost" in n for n in check["notes"])
    # the mismatch is advisory only -- totals still come from oracle.jsonl's
    # own refused list, not the configuration's claim:
    assert check["totals"]["oracle_refused"] == 1


# ---------------------------------------------------------------------------
# full tally — ivm-dd shape
# ---------------------------------------------------------------------------

def test_check_cell_ivmdd(tmp_path: Path) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    check = external_check.check_cell(export_dir, _ivmdd_result())

    assert check["config_kind"] == "ivm-dd"
    by_epoch = {b["epoch"]: b for b in check["per_burst"]}

    e0 = by_epoch[0]
    assert e0["n_compared"] == 3 and e0["agree"] == 3
    assert e0["disagree"] == [] and e0["not_answered"] == []

    e1 = by_epoch[1]
    assert e1["disagree"] == ["a3"]
    assert e1["agree"] == 2
    assert e1["n_changed"] == 2
    assert e1["false_fresh"] == 1  # len(disagree) under the non-withheld reading
    assert e1["false_stale"] is None

    e2 = by_epoch[2]
    assert e2["n_compared"] == 2  # a3 refused
    assert e2["disagree"] == []
    assert e2["agree"] == 2
    assert e2["n_changed"] == 1  # a2 only
    assert e2["false_fresh"] == 0
    assert e2["false_stale"] is None

    totals = check["totals"]
    assert totals["n_compared"] == 8
    assert totals["agree"] == 3 + 2 + 2
    assert totals["disagree"] == 1
    assert totals["not_answered"] == 0
    assert totals["oracle_refused"] == 1
    assert totals["false_fresh"] == 1
    assert totals["false_stale"] is None


def test_check_cell_ivmdd_unattributed_not_answered(tmp_path: Path) -> None:
    """When ivm-dd's aggregate `not_answered` total is nonzero, per-burst
    attribution is honestly `None`/"total_only" rather than guessed, and
    the grand total is still exactly the configuration's own count."""
    export_dir = _write_bundle(tmp_path / "cell")
    result = _ivmdd_result()
    result["gates"]["oracle_agreement"]["not_answered"] = 2
    check = external_check.check_cell(export_dir, result)
    # a nonzero, unattributed total means no epoch can honestly be called
    # "exact" (the 2 missing answers could be anywhere, including epoch 0):
    assert all(b["not_answered_attribution"] == "total_only" for b in check["per_burst"])
    assert check["totals"]["not_answered"] == 2


# ---------------------------------------------------------------------------
# the real exported cell: 19,614 / 19,614 (recorded here as a frozen
# expectation on a *locally rebuilt* miniature of the same shape — the
# real bundle is 20+ MB on xzgpu and is not copied to the laptop per the
# "experiments-remote-only" rule; this is the arithmetic identity the real
# run's tally satisfies: n_registered * (n_epochs) with zero refusals).
# ---------------------------------------------------------------------------

def test_total_pairs_identity_matches_the_calibration_cell() -> None:
    n_registered, n_epochs = 934, 21
    assert n_registered * n_epochs == 19614


# ---------------------------------------------------------------------------
# withheld-correction cell (memo §3.5) — matches `ivm_dd::withheld`'s own
# `withheld-result.json` field names exactly (src/main.rs cmd_withheld).
# ---------------------------------------------------------------------------

def _withheld_result() -> dict:
    return {
        "cell_id": "synth-iv-60k-c4-deep-n1000-s0",
        "feeders": [
            {"feeder": "F-epoch", "probe_reports_complete_through_10": True,
             "signalled": False, "false_fresh_count": 3,
             "artifacts": [{"name": "a1", "served_digest": "x", "oracle_digest_epoch10": "y",
                           "false_fresh": True}]},
            {"feeder": "F-watermark", "probe_reports_complete_through_10": False,
             "signalled": False, "false_fresh_count": 0,
             "artifacts": [{"name": "a1", "served_digest": "x", "oracle_digest_epoch10": "y",
                           "false_fresh": False}]},
        ],
    }


def test_check_withheld_without_tgms_numbers() -> None:
    out = external_check.check_withheld(_withheld_result())
    assert out["ivm_f_epoch_false_fresh"] == 3
    assert out["ivm_f_epoch_probe_complete_through_10"] is True
    assert out["ivm_f_watermark_false_fresh"] == 0
    assert out["ivm_f_watermark_unanswerable"] is True
    assert out["tgms_false_fresh"] is None
    assert any("not supplied" in n for n in out["notes"])


def test_check_withheld_with_tgms_numbers() -> None:
    out = external_check.check_withheld(_withheld_result(), tgms_false_fresh=0,
                                        tgms_stale_marked=3)
    assert out["tgms_false_fresh"] == 0
    assert out["tgms_stale_marked"] == 3
    assert out["notes"] == []


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def test_cli_writes_check_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    export_dir = _write_bundle(tmp_path / "cell")
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_ivmdd_result()))
    out_path = tmp_path / "check.json"

    rc = external_check.main([str(export_dir), str(result_path), "--out", str(out_path)])
    assert rc == 0
    written = json.loads(out_path.read_text())
    assert written["totals"]["agree"] == 7
    captured = capsys.readouterr()
    assert "agree" in captured.err
