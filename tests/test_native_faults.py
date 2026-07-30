"""Fault injection against the native engine (spec WP-N5).

The engine's durability objective is not "never lose a write" — the event log
already guarantees that, and `tgms replay` rebuilds any store. It is the
stricter, narrower promise:

    never expose an undetected inconsistent generation.

So every case below corrupts or truncates one file and demands one of exactly
two outcomes: the store still serves the previous complete generation, or it
refuses with an error naming the problem. Silently returning partial or mixed
data is the only real failure, and it is what these tests exist to rule out.

These run regardless of TGMS_TEST_BACKEND — they are about the native engine
specifically, not about adapter conformance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgms.core.errors import TgmsError
from tgms.core.model import OPEN_END, EntityRef

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.native import NativeAdapter  # noqa: E402


def build(root: Path, batches: int = 3) -> NativeAdapter:
    """A store with several published generations and a correction."""
    import tgms

    store = tgms.open(root, backend="native")
    for b in range(batches):
        store.assert_edge(f"n{b}", f"n{b + 1}", "R", {"w": b}, vt_s=b, vt_e=b + 10, disc=f"#{b}")
    store.assert_node("n0", "N", {"p": 1}, vt_s=0, vt_e=50)
    store.correct(EntityRef(kind="node", uid="n0"), {"p": 99}, vt_s=0, vt_e=50)
    return store.adapter


def native_dir(root: Path) -> Path:
    return root / "native"


def reopen(root: Path) -> NativeAdapter:
    return NativeAdapter(native_dir(root))


def flip_byte(path: Path, offset: int) -> None:
    b = bytearray(path.read_bytes())
    b[offset] ^= 0xFF
    path.write_bytes(bytes(b))


def any_segment(root: Path) -> Path:
    segs = sorted((native_dir(root) / "seg").glob("*.tgs"))
    assert segs, "expected the fixture to have written segments"
    return segs[0]


def current_generation(root: Path) -> int:
    return int((native_dir(root) / "CURRENT").read_text().split()[0])


# --- a healthy store is the baseline every case is measured against ------ #


def test_healthy_store_verifies_clean(tmp_path):
    adapter = build(tmp_path / "s")
    report = adapter.verify()
    assert report["healthy"], report["problems"]
    assert report["segments_checked"] > 0
    assert report["rows"] > 0


# --- corruption must be detected, never served --------------------------- #


def test_truncated_segment_is_detected(tmp_path):
    root = tmp_path / "s"
    build(root)
    seg = any_segment(root)
    seg.write_bytes(seg.read_bytes()[:-32])  # lose the completion marker

    report = reopen(root).verify()
    assert not report["healthy"]
    assert any(seg.name in p for p in report["problems"]), report["problems"]
    assert any("never finished" in p or "truncated" in p for p in report["problems"])


def data_start(seg: Path) -> int:
    """Offset of the first column extent: magic(4) + format(4) + len(4) +
    header JSON, rounded up to the 64-byte alignment the writer uses."""
    header_len = int.from_bytes(seg.read_bytes()[8:12], "little")
    return -(-(12 + header_len) // 64) * 64


def test_flipped_data_byte_fails_a_column_checksum(tmp_path):
    """Aimed squarely at the CRC path: flip a byte inside a column extent,
    where nothing else would notice."""
    root = tmp_path / "s"
    build(root)
    seg = any_segment(root)
    flip_byte(seg, data_start(seg) + 8)

    report = reopen(root).verify()
    assert not report["healthy"]
    assert any("checksum" in p for p in report["problems"]), report["problems"]


def test_flipped_header_byte_is_detected(tmp_path):
    """Anywhere else in the file, detection may come from the header parse or
    the body digest instead — the mechanism is not the point, catching it is."""
    root = tmp_path / "s"
    build(root)
    seg = any_segment(root)
    flip_byte(seg, len(seg.read_bytes()) // 2)

    report = reopen(root).verify()
    assert not report["healthy"]
    assert any(seg.name in p for p in report["problems"]), report["problems"]


def test_corrupt_segment_refuses_to_serve_rows(tmp_path):
    """Detection is not enough — a corrupt segment must not yield data."""
    root = tmp_path / "s"
    build(root)
    seg = any_segment(root)
    flip_byte(seg, data_start(seg) + 8)

    adapter = reopen(root)
    with pytest.raises((TgmsError, RuntimeError, OSError)):
        list(adapter.all_edge_versions())


def test_missing_segment_is_detected(tmp_path):
    root = tmp_path / "s"
    build(root)
    any_segment(root).unlink()

    report = reopen(root).verify()
    assert not report["healthy"]


def test_partial_close_run_is_detected(tmp_path):
    root = tmp_path / "s"
    build(root)
    runs = sorted((native_dir(root) / "close").glob("*.tgc"))
    assert runs, "the fixture's correction should have written a close run"
    runs[0].write_bytes(runs[0].read_bytes()[:-6])

    report = reopen(root).verify()
    assert not report["healthy"]
    assert any(runs[0].name in p for p in report["problems"]), report["problems"]


def test_tampered_manifest_refuses_to_open(tmp_path):
    root = tmp_path / "s"
    build(root)
    gen = current_generation(root)
    path = native_dir(root) / "manifests" / f"{gen:020}.json"
    doc = json.loads(path.read_text())
    doc["stats"]["n_edge_versions"] += 1  # plausible, but the sha will not match
    path.write_text(json.dumps(doc, indent=2))

    with pytest.raises((TgmsError, RuntimeError)):
        reopen(root)


def test_malformed_current_refuses_to_open(tmp_path):
    root = tmp_path / "s"
    build(root)
    (native_dir(root) / "CURRENT").write_text("not-a-generation\n")

    with pytest.raises((TgmsError, RuntimeError)):
        reopen(root)


def test_current_pointing_past_the_manifests_refuses_to_open(tmp_path):
    """The 'manifest ahead of segments' shape: CURRENT names a generation
    whose manifest was never written."""
    root = tmp_path / "s"
    build(root)
    gen = current_generation(root)
    (native_dir(root) / "CURRENT").write_text(f"{gen + 5} deadbeefdeadbeef\n")

    with pytest.raises((TgmsError, RuntimeError)):
        reopen(root)


# --- interrupted writes must leave the previous generation intact -------- #


def test_crash_before_current_rename_serves_the_previous_generation(tmp_path):
    """A manifest reached disk but CURRENT was never flipped."""
    root = tmp_path / "s"
    adapter = build(root)
    gen = current_generation(root)
    before = sorted(v.vid for v in adapter.all_edge_versions())

    # simulate: generation gen+1's manifest exists, CURRENT still points at gen
    src = native_dir(root) / "manifests" / f"{gen:020}.json"
    orphan = native_dir(root) / "manifests" / f"{gen + 1:020}.json"
    orphan.write_text(src.read_text())

    re = reopen(root)
    assert re.generation == gen, "an orphaned manifest must not be adopted"
    assert sorted(v.vid for v in re.all_edge_versions()) == before
    assert re.verify()["healthy"]


def test_orphaned_segment_files_are_ignored(tmp_path):
    """Segments written by a batch that never published are inert."""
    root = tmp_path / "s"
    adapter = build(root)
    before = sorted(v.vid for v in adapter.all_edge_versions())
    gen = current_generation(root)

    seg = any_segment(root)
    (native_dir(root) / "seg" / "999999999999.tgs").write_bytes(seg.read_bytes())

    re = reopen(root)
    assert re.generation == gen
    assert sorted(v.vid for v in re.all_edge_versions()) == before, (
        "a segment no manifest references must contribute nothing"
    )
    assert re.verify()["healthy"]


def test_interrupted_compaction_leaves_the_store_readable(tmp_path):
    """Compaction writes new segments before publishing. Killed in between,
    the store must still serve exactly what it did before."""
    root = tmp_path / "s"
    adapter = build(root)
    before = sorted(v.vid for v in adapter.all_edge_versions())
    gen = current_generation(root)

    # the pre-publication state: new segment files exist, manifest unchanged
    seg = any_segment(root)
    for i in range(3):
        (native_dir(root) / "seg" / f"90000000000{i}.tgs").write_bytes(seg.read_bytes())

    re = reopen(root)
    assert re.generation == gen
    assert sorted(v.vid for v in re.all_edge_versions()) == before
    assert re.verify()["healthy"]

    # and a real compaction afterwards still preserves content
    re.compact()
    assert sorted(v.vid for v in re.all_edge_versions()) == before


# --- generation scoping -------------------------------------------------- #


def test_a_reader_holding_an_older_generation_is_unaffected_by_a_writer(tmp_path):
    """Manifest generations are what make a snapshot coherent: a reader that
    opened at generation N must not observe a correction published at N+1."""
    import tgms

    root = tmp_path / "s"
    store = tgms.open(root, backend="native")
    store.assert_node("n1", "N", {"p": 1}, vt_s=0, vt_e=50)

    reader = reopen(root)  # pinned at the current generation
    pinned_gen = reader.generation
    before = [v.props for v in reader.believed_node_versions("n1", OPEN_END)]
    assert before == [{"p": 1}]

    store.correct(EntityRef(kind="node", uid="n1"), {"p": 99}, vt_s=0, vt_e=50)

    assert reader.generation == pinned_gen, "the reader's generation moved"
    assert [v.props for v in reader.believed_node_versions("n1", OPEN_END)] == before, (
        "a reader pinned to an older generation saw a newer correction"
    )
    # a freshly opened reader does see it
    assert [v.props for v in reopen(root).believed_node_versions("n1", OPEN_END)] == [{"p": 99}]


# --- generation collection ------------------------------------------------ #
#
# gc's contract mirrors the durability objective: whatever it removes, the
# store must keep opening, verifying clean, and serving exactly the current
# generation's content. The generation CURRENT names is categorically
# untouchable, and an interrupted pass may only under-collect.


def manifest_files(root: Path) -> list[Path]:
    return sorted((native_dir(root) / "manifests").glob("*.json"))


def segment_count(root: Path) -> int:
    return len(list((native_dir(root) / "seg").glob("*.tgs")))


def test_gc_never_touches_the_current_generation(tmp_path):
    root = tmp_path / "s"
    adapter = build(root)
    gen = current_generation(root)
    before = sorted(v.vid for v in adapter.all_edge_versions())
    assert len(manifest_files(root)) > 1, "fixture should span generations"

    report = adapter.gc(keep_last=1)

    assert report["manifests_removed"] > 0
    assert report["bytes_reclaimed"] > 0
    assert manifest_files(root) == [native_dir(root) / "manifests" / f"{gen:020}.json"]
    assert current_generation(root) == gen, "CURRENT must be untouched"
    assert sorted(v.vid for v in adapter.all_edge_versions()) == before
    # and a fresh open of the collected store is healthy end to end
    re = reopen(root)
    assert re.generation == gen
    assert sorted(v.vid for v in re.all_edge_versions()) == before
    assert re.verify()["healthy"]


def test_gc_after_compaction_reclaims_superseded_segments(tmp_path):
    root = tmp_path / "s"
    adapter = build(root)
    before = sorted(v.vid for v in adapter.all_edge_versions())
    segments_before = segment_count(root)
    assert list((native_dir(root) / "close").glob("*.tgc")), (
        "the fixture's correction should have written a close run"
    )

    adapter.compact()  # supersedes every old segment, folds the close run
    report = adapter.gc(keep_last=1)

    assert report["segments_removed"] == segments_before
    assert report["close_runs_removed"] == 1
    assert segment_count(root) < segments_before
    assert not list((native_dir(root) / "close").glob("*.tgc"))
    assert sorted(v.vid for v in adapter.all_edge_versions()) == before
    assert reopen(root).verify()["healthy"]


def test_interrupted_gc_leaves_the_store_openable(tmp_path):
    """gc killed between its passes: some superseded manifests deleted, the
    segments only they referenced still on disk, a temp file left behind."""
    root = tmp_path / "s"
    adapter = build(root)
    before = sorted(v.vid for v in adapter.all_edge_versions())
    adapter.compact()
    gen = current_generation(root)

    doomed = [p for p in manifest_files(root) if int(p.stem) != gen]
    assert doomed, "expected superseded manifests for the pass to be amid"
    doomed[0].unlink()
    (native_dir(root) / "manifests" / "junk.tmp").write_bytes(b"partial")

    re = reopen(root)
    assert re.generation == gen
    assert sorted(v.vid for v in re.all_edge_versions()) == before
    assert re.verify()["healthy"]

    # the next pass finishes the job
    re.gc(keep_last=1)
    assert not (native_dir(root) / "manifests" / "junk.tmp").exists()
    assert manifest_files(root) == [native_dir(root) / "manifests" / f"{gen:020}.json"]
    assert sorted(v.vid for v in re.all_edge_versions()) == before
    assert re.verify()["healthy"]


def test_gc_spares_a_generation_pinned_by_an_open_reader(tmp_path):
    """The reader-pin rule: a reader that opened at generation N keeps N on
    disk through any number of gc passes, and N collects once it closes."""
    import gc as pygc

    import tgms

    root = tmp_path / "s"
    store = tgms.open(root, backend="native")
    store.assert_node("n1", "N", {"p": 1}, vt_s=0, vt_e=50)

    reader = reopen(root)
    pinned_gen = reader.generation
    pinned_manifest = native_dir(root) / "manifests" / f"{pinned_gen:020}.json"
    before = [v.props for v in reader.believed_node_versions("n1", OPEN_END)]

    store.correct(EntityRef(kind="node", uid="n1"), {"p": 99}, vt_s=0, vt_e=50)
    store.assert_node("n2", "N", {"q": 1}, vt_s=0, vt_e=50)
    store.adapter.gc(keep_last=1)

    assert pinned_manifest.exists(), "a pinned generation must survive gc"
    assert reader.generation == pinned_gen
    assert [v.props for v in reader.believed_node_versions("n1", OPEN_END)] == before

    del reader
    pygc.collect()  # drop the engine handle, and with it the pin
    store.adapter.gc(keep_last=1)
    assert not pinned_manifest.exists(), "an unpinned generation must collect"
    assert reopen(root).verify()["healthy"]


def test_projected_scan_over_empty_window_keeps_requested_columns(tmp_path):
    """Regression: a projection must decide the key set, not the row count.

    scan_edges once omitted the vid key whenever zero rows matched, so a
    projected scan over an empty window raised KeyError in the adapter while
    the same call over a non-empty window worked (fixed alongside the
    diff_snapshots projection).
    """
    adapter = build(tmp_path)
    for cols in (None, ("vid",), ("eid", "vid", "src_id", "dst_id", "rel_type")):
        # every fixture row lives at vt >= 0; this window matches nothing
        got = adapter.edges_columnar(vt_min=-20, vt_max=-10, columns=cols)
        expected = {"src_id", "dst_id", "vt_s", "vt_e",
                    "eid", "vid", "rel_type"} if cols is None else set(cols)
        assert expected <= set(got), (cols, sorted(got))
        assert all(len(got[c]) == 0 for c in expected)
