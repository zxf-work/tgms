"""On-disk manifest format 2: the checkpoint-plus-delta chain (P0.8 / B1).

`docs/design/INCREMENTAL_MANIFEST_FORECAST_2026-09-13.md`. The engine-level
properties are covered in Rust (`manifest_chain.rs`, `store.rs`, `gc.rs`,
`tests/roundtrip.rs`); what is worth asserting from Python is the part the
memo's §4 migration section makes a *product* promise rather than an engine
one:

- a store this build writes uses format 2, survives a reopen, and reads back
  the same rows;
- a store the **previous engine** wrote — vendored under
  `tests/fixtures/format1_store`, built at feaab3f before the format change,
  because it can no longer be produced from this tree — opens, reads and
  verifies, but refuses every write with a remedy;
- `tgms store upgrade-manifests` converts it, `tgms store verify` is clean
  afterwards, and the logical content is unchanged;
- the TCSR stamp does what §4 says it does: the upgrade invalidates a
  persisted permutation and it rebuilds silently, rather than being served
  against a generation it was not built for.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms import _engine  # noqa: E402
from tgms.core.errors import StateError  # noqa: E402
from tgms.storage.native import NativeAdapter  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "format1_store"


def manifests(root: Path) -> list[Path]:
    return sorted((root / "native" / "manifests").glob("*.json"))


def record(root: Path, generation: int) -> dict:
    path = root / "native" / "manifests" / f"{generation:020d}.json"
    return json.loads(path.read_text())


def legacy_store(tmp_path: Path) -> Path:
    """A writable copy of the vendored format-1 store."""
    dst = tmp_path / "legacy"
    shutil.copytree(FIXTURE, dst)
    return dst


def build_store(root: Path, batches: int = 6):
    import tgms

    store = tgms.open(root, backend="native")
    for b in range(batches):
        store.ingest_events([
            {"src": f"n{(b * 3 + i) % 5}", "dst": f"n{(b * 3 + i + 1) % 5}",
             "rel_type": "R", "vt_s": b * 10 + i, "vt_e": b * 10 + i + 4,
             "props": {"w": b}}
            for i in range(3)
        ])
    return store


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tgms.cli", *argv],
        capture_output=True, text=True, check=False,
    )


# --- a store this build writes ---------------------------------------- //

def test_a_new_store_writes_format_2_and_survives_a_reopen(tmp_path):
    store = build_store(tmp_path / "s")
    adapter = store.adapter
    assert adapter.manifest_format() == _engine.MANIFEST_FORMAT_VERSION == 2
    generation = adapter.generation
    assert generation >= 6
    before = store.stats()
    rows = adapter.verify()["rows"]
    store.close()

    # genesis is a checkpoint; the generations above it are deltas, because
    # the default K is far above this run length
    assert record(tmp_path / "s", 0)["kind"] == "checkpoint"
    assert record(tmp_path / "s", generation)["kind"] == "delta"
    assert record(tmp_path / "s", generation)["parent"] == generation - 1

    import tgms
    again = tgms.open(tmp_path / "s", backend="native")
    assert again.adapter.generation == generation
    assert again.stats() == before
    report = again.adapter.verify()
    assert report["healthy"], report["problems"]
    assert report["rows"] == rows
    assert report["manifest_format"] == 2
    assert report["manifest_checkpoint"] == 0
    assert report["manifest_deltas"] == generation
    again.close()


def test_a_delta_is_smaller_than_the_checkpoint_it_replaces(tmp_path,
                                                            monkeypatch):
    """The measured pathology in one line: what a generation costs on disk
    must stop tracking how many segments the store holds.

    K is dropped to 8 so both shapes appear in one short run — the delta at
    generation 23 and the periodic checkpoint at 24 describe the same store
    within one commit of each other, so their sizes are directly comparable.
    """
    monkeypatch.setenv("TGMS_MANIFEST_CHECKPOINT_EVERY", "8")
    root = tmp_path / "s"
    store = build_store(root, batches=24)
    generation = store.adapter.generation
    assert generation == 24
    segments = store.adapter.verify()["segments_checked"]
    assert segments >= 20, "the point only shows once there are segments"
    store.close()

    def size(g: int) -> int:
        return (root / "native" / "manifests" / f"{g:020d}.json").stat().st_size

    assert record(root, 24)["kind"] == "checkpoint"
    assert record(root, 23)["kind"] == "delta"
    assert size(23) * 4 < size(24), (
        f"delta {size(23)} B against checkpoint {size(24)} B at {segments} "
        f"segments — the delta is the one that must not grow")

    # flat: the last delta is no bigger than the first, though the store now
    # holds an order of magnitude more segments
    assert size(23) < size(1) * 1.5, (size(1), size(23))
    m = record(root, 23)
    named = (len(m.get("add_nodes", [])) + len(m.get("add_edges_event", []))
             + len(m.get("add_edges_interval", [])))
    assert named <= 2, "a delta names only what this generation added"


def test_gc_leaves_every_retained_generation_readable(tmp_path):
    store = build_store(tmp_path / "s", batches=8)
    root = tmp_path / "s"
    before = store.stats()
    report = store.adapter.gc(keep_last=2)
    assert report["manifests_removed"] > 0
    assert report["generations_retained"] == 2
    store.close()

    # the floor is materialized as a checkpoint so the chain below it can go
    kept = [int(p.stem) for p in manifests(root)]
    assert len(kept) == 2
    assert record(root, min(kept))["kind"] == "checkpoint"

    import tgms
    again = tgms.open(root, backend="native")
    assert again.stats() == before
    assert again.adapter.verify()["healthy"]
    again.close()


# --- the format-1 store the previous engine wrote ---------------------- //

def test_the_vendored_fixture_really_is_format_1():
    head = json.loads((FIXTURE / "native" / "CURRENT").read_text().split()[0])
    m = record(FIXTURE, head)
    assert m["format"] == 1
    assert "kind" not in m, "format 1 documents carry no kind tag"


def test_a_format_1_store_opens_read_only_and_names_its_remedy(tmp_path):
    root = legacy_store(tmp_path)
    adapter = NativeAdapter(root / "native")
    assert adapter.manifest_format() == 1

    # reads work
    report = adapter.verify()
    assert report["healthy"], report["problems"]
    assert report["manifest_format"] == 1
    assert report["segments_checked"] == 5
    assert report["close_runs_checked"] == 1
    assert adapter.generation == 4

    # writes do not, and say what to do about it
    with pytest.raises(StateError) as excinfo:
        adapter.gc(keep_last=2)
    assert "read-only" in str(excinfo.value)
    assert "upgrade-manifests" in str(excinfo.value)
    adapter.close()


def test_upgrade_converts_the_fixture_and_leaves_the_content_alone(tmp_path):
    root = legacy_store(tmp_path)
    adapter = NativeAdapter(root / "native")
    before = adapter.verify()
    before_gen = adapter.generation
    before_sha = adapter._store.manifest_sha()

    report = adapter.upgrade_manifests()
    assert report["upgraded"] is True
    assert (report["from_format"], report["to_format"]) == (1, 2)
    assert adapter.manifest_format() == 2

    after = adapter.verify()
    assert after["healthy"], after["problems"]
    # everything about the *data* is identical
    for key in ("segments_checked", "close_runs_checked", "rows", "closes",
                "dict_records", "tt_s_runs", "max_tt_s_runs"):
        assert after[key] == before[key], key
    # only the chain fields and the publication counter moved
    assert after["manifest_format"] == 2
    assert after["manifest_deltas"] == 0
    assert after["generation"] == before_gen + 1
    assert adapter._store.manifest_sha() != before_sha
    assert record(root, after["generation"])["kind"] == "checkpoint"

    # and writing now works
    adapter.gc(keep_last=2)
    assert adapter.verify()["healthy"]

    # idempotent
    assert adapter.upgrade_manifests()["upgraded"] is False
    adapter.close()


def test_the_upgraded_store_reads_the_same_rows(tmp_path):
    root = legacy_store(tmp_path)
    adapter = NativeAdapter(root / "native")
    before_edges = adapter.all_edge_versions()
    before_nodes = adapter.all_node_versions()
    adapter.upgrade_manifests()
    assert adapter.all_edge_versions() == before_edges
    assert adapter.all_node_versions() == before_nodes
    adapter.close()

    # ... and after a fresh open, off the new checkpoint alone
    reopened = NativeAdapter(root / "native")
    assert reopened.all_edge_versions() == before_edges
    assert reopened.manifest_format() == 2
    reopened.close()


def test_tcsr_rebuilds_after_the_upgrade(tmp_path):
    """Memo §4: existing TCSR caches are invalidated by the stamp check and
    silently rebuilt — which is exactly what that check is for."""
    root = legacy_store(tmp_path)
    adapter = NativeAdapter(root / "native")
    first, _ = adapter.tcsr()
    index = adapter.path / "index" / "tcsr.npz"
    assert index.exists(), "the permutation should have been persisted"
    stamp_before = index.stat().st_mtime_ns
    adapter.close()

    adapter = NativeAdapter(root / "native")
    adapter.upgrade_manifests()
    second, _ = adapter.tcsr()
    # a stale stamp must not be served: the file is rewritten for the new
    # generation, and the answer is the same
    assert index.stat().st_mtime_ns != stamp_before
    import numpy as np
    for direction in ("out", "inn"):
        for field in ("offsets", "nbr", "vt_s", "vt_e", "row"):
            np.testing.assert_array_equal(
                np.asarray(getattr(getattr(first, direction), field)),
                np.asarray(getattr(getattr(second, direction), field)),
                err_msg=f"{direction}.{field} changed across the upgrade",
            )
    adapter.close()


# --- the CLI verb ------------------------------------------------------- //

def test_cli_upgrade_manifests_then_verify(tmp_path):
    root = legacy_store(tmp_path)

    # verify refuses nothing on a format-1 store — reads are fine
    done = cli("store", "verify", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert "healthy" in done.stdout

    done = cli("store", "upgrade-manifests", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert "manifest format 1 -> 2" in done.stdout

    done = cli("store", "verify", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert "healthy" in done.stdout
    assert "PROBLEMS" not in done.stdout

    # running it again is a no-op, not an error
    done = cli("store", "upgrade-manifests", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert "already at manifest format 2" in done.stdout

    # gc, previously refused, now works through the CLI too
    done = cli("store", "gc", "--store", str(root), "--keep", "2")
    assert done.returncode == 0, done.stderr


def test_cli_documents_the_verb_in_its_help():
    done = cli("store", "--help")
    assert done.returncode == 0, done.stderr
    assert "upgrade-manifests" in done.stdout
    assert "format 1" in done.stdout
    assert "manifest_sha" in done.stdout
