"""On-disk manifest formats: the checkpoint-plus-delta chain, and its digest.

`docs/design/INCREMENTAL_MANIFEST_FORECAST_2026-09-13.md` (format 2, B1) and
`docs/design/INCREMENTAL_MANIFEST_V2_DIAGNOSIS_2026-09-15.md` (format 3,
B1-v2). The engine-level properties are covered in Rust (`manifest.rs`'s
`merkle` module, `manifest_chain.rs`, `store.rs`, `gc.rs`,
`tests/roundtrip.rs`); what is worth asserting from Python is the part §4's
migration section makes a *product* promise rather than an engine one:

- a store this build writes uses format 3, survives a reopen, and reads back
  the same rows;
- a store a **previous engine** wrote — vendored under
  `tests/fixtures/format1_store` (built at feaab3f, before format 2) and
  `tests/fixtures/format2_store` (built at 89c98b3, before format 3), because
  neither can be produced from this tree any more — opens, reads and
  verifies, but refuses every write with a remedy;
- `tgms store upgrade-manifests` converts either of them, `tgms store verify`
  is clean afterwards, and the logical content is unchanged;
- the TCSR stamp does what §4 says it does: the upgrade invalidates a
  persisted permutation and it rebuilds silently, rather than being served
  against a generation it was not built for.

The format-2 fixture is the one that matters most here, because format 3
changes *only what `manifest_sha` is*. A format-2 store therefore looks
almost identical on disk and must nevertheless keep verifying under its own
whole-document rule, never be reinterpreted under the Merkle rule, and come
out of the upgrade with a different digest and the same rows.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms import _engine  # noqa: E402
from tgms.core.errors import StateError  # noqa: E402
from tgms.storage.native import NativeAdapter  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FORMAT1 = FIXTURES / "format1_store"
FORMAT2 = FIXTURES / "format2_store"

# every read-only format, and what its head record looks like on disk
LEGACY = [
    pytest.param(FORMAT1, 1, id="format1"),
    pytest.param(FORMAT2, 2, id="format2"),
]


def manifests(root: Path) -> list[Path]:
    return sorted((root / "native" / "manifests").glob("*.json"))


def record(root: Path, generation: int) -> dict:
    path = root / "native" / "manifests" / f"{generation:020d}.json"
    return json.loads(path.read_text())


def head_generation(root: Path) -> int:
    return int((root / "native" / "CURRENT").read_text().split()[0])


def legacy_store(tmp_path: Path, fixture: Path) -> Path:
    """A writable copy of a vendored store an older engine wrote."""
    dst = tmp_path / fixture.name
    shutil.copytree(fixture, dst)
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


def flat(text: str) -> str:
    """argparse rewraps help text, so match on words, not on line breaks."""
    return re.sub(r"\s+", " ", text)


# --- a store this build writes ---------------------------------------- //

def test_a_new_store_writes_format_3_and_survives_a_reopen(tmp_path):
    store = build_store(tmp_path / "s")
    adapter = store.adapter
    assert adapter.manifest_format() == _engine.MANIFEST_FORMAT_VERSION == 3
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
    assert report["manifest_format"] == 3
    assert report["manifest_checkpoint"] == 0
    assert report["manifest_deltas"] == generation
    again.close()


def test_every_format_3_record_declares_the_digest_rule_it_was_sealed_under(
        tmp_path):
    """`manifest_sha` is a Merkle root from format 3 on, and a record says so.

    The tag is not the authority — `format` is, and `format` is inside the
    digest's preimage — but a store an operator is reading during an incident
    should not require the source tree to say which rule produced its shas.
    """
    root = tmp_path / "s"
    store = build_store(root)
    generation = store.adapter.generation
    store.close()

    for g in (0, generation):
        m = record(root, g)
        assert m["sha_kind"] == "merkle-v1", g
        assert m["format"] == 3, g

    # and the digest really did change shape: the same logical content under
    # format 2's rule is the sha of the whole document, which the vendored
    # format-2 fixture still carries and which no format-3 store does
    assert "sha_kind" not in record(FORMAT2, head_generation(FORMAT2))


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


def test_verify_full_re_derives_the_digest_independently(tmp_path):
    """A digest maintained by appending to a spine could be wrong and
    self-consistent, so `verify --full` recomputes it the slow way and
    crosses the answer against what CURRENT publishes (memo §4(d))."""
    root = tmp_path / "s"
    store = build_store(root, batches=10)
    report = store.adapter.verify(mode="full")
    assert report["healthy"], report["problems"]

    # stand in for a bad incremental update: what was published is not what a
    # from-scratch recomputation of this generation gives. Done under a live
    # handle, because that is the case only the oracle catches — a *reopen*
    # is refused outright by the CURRENT check in `open`.
    generation = head_generation(root)
    (root / "native" / "CURRENT").write_text(
        f"{generation} 0000000000000000\n")

    full = store.adapter.verify(mode="full")
    assert not full["healthy"]
    assert any("recomputes from scratch" in p for p in full["problems"]), \
        full["problems"]
    # the fast pass does not make this check: it is the price of an
    # incremental digest, paid in the mode nobody runs per commit
    assert store.adapter.verify()["healthy"]
    store.close()

    # and a store in that state does not reopen at all
    with pytest.raises(StateError) as excinfo:
        NativeAdapter(root / "native")
    assert "CURRENT records sha" in str(excinfo.value)


# --- the stores previous engines wrote --------------------------------- //

@pytest.mark.parametrize("fixture,format_version", LEGACY)
def test_the_vendored_fixture_really_is_the_format_it_claims(fixture,
                                                             format_version):
    m = record(fixture, head_generation(fixture))
    assert m["format"] == format_version
    if format_version == 1:
        assert "kind" not in m, "format 1 documents carry no kind tag"
    else:
        assert m["kind"] in ("checkpoint", "delta")
    assert "sha_kind" not in m, "only format 3 declares a digest rule"


def test_the_format_2_fixture_really_has_a_chain_to_reconstruct():
    """The format-1 fixture has no chain at all, so it cannot show that a
    format-2 *chain* still replays under format 3. This one can."""
    kinds = {int(p.stem): json.loads(p.read_text()).get("kind")
             for p in manifests(FORMAT2)}
    assert kinds[0] == "checkpoint"
    assert sum(1 for k in kinds.values() if k == "delta") >= 4
    assert sum(1 for k in kinds.values() if k == "checkpoint") >= 2, \
        "a periodic checkpoint above genesis is what makes the base non-trivial"
    assert kinds[head_generation(FORMAT2)] == "delta"


def test_a_format_2_chain_replays_to_the_digest_the_old_engine_wrote():
    """The sharpest statement of the read-only promise.

    The head of the format-2 fixture is a delta two generations above a
    periodic checkpoint, so opening it *replays a chain*. Under format 3 that
    replay must resolve the base, apply both deltas, and arrive at exactly the
    sha the pre-format-3 engine sealed and wrote into `CURRENT` — computed
    under format 2's whole-document rule, not the Merkle one. A single byte of
    reinterpretation and this number moves.

    Read-only, so the fixture is opened in place rather than copied.
    """
    adapter = NativeAdapter(FORMAT2 / "native")
    try:
        expected_gen, expected_sha = (
            FORMAT2 / "native" / "CURRENT").read_text().split()
        assert adapter.manifest_format() == 2
        assert adapter.generation == int(expected_gen)
        assert adapter._store.manifest_sha() == expected_sha

        report = adapter.verify(mode="full")
        assert report["healthy"], report["problems"]
        assert report["manifest_checkpoint"] == 8
        assert report["manifest_deltas"] == 2, (
            "the head must have been reached by replaying a chain, or this "
            "test proves nothing about chains")
    finally:
        adapter.close()


@pytest.mark.parametrize("fixture,format_version", LEGACY)
def test_an_older_store_opens_read_only_and_names_its_remedy(
        tmp_path, fixture, format_version):
    root = legacy_store(tmp_path, fixture)
    adapter = NativeAdapter(root / "native")
    assert adapter.manifest_format() == format_version

    # reads work, and so does verification — under that format's own digest
    # rule, which is the whole point of keeping `format` inside the preimage
    report = adapter.verify()
    assert report["healthy"], report["problems"]
    assert report["manifest_format"] == format_version
    assert report["segments_checked"] >= 5
    assert report["close_runs_checked"] == 1
    assert adapter.generation == head_generation(root)
    assert adapter.verify(mode="full")["healthy"]

    # writes do not, and say what to do about it
    with pytest.raises(StateError) as excinfo:
        adapter.gc(keep_last=2)
    assert "read-only" in str(excinfo.value)
    assert "upgrade-manifests" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize("fixture,format_version", LEGACY)
def test_upgrade_converts_the_fixture_and_leaves_the_content_alone(
        tmp_path, fixture, format_version):
    root = legacy_store(tmp_path, fixture)
    adapter = NativeAdapter(root / "native")
    before = adapter.verify()
    before_gen = adapter.generation
    before_sha = adapter._store.manifest_sha()

    report = adapter.upgrade_manifests()
    assert report["upgraded"] is True
    assert (report["from_format"], report["to_format"]) == (format_version, 3)
    assert adapter.manifest_format() == 3

    after = adapter.verify(mode="full")
    assert after["healthy"], after["problems"]
    # everything about the *data* is identical
    for key in ("segments_checked", "close_runs_checked", "rows", "closes",
                "dict_records", "tt_s_runs", "max_tt_s_runs"):
        assert after[key] == before[key], key
    # only the chain fields and the publication counter moved
    assert after["manifest_format"] == 3
    assert after["manifest_deltas"] == 0
    assert after["generation"] == before_gen + 1
    assert adapter._store.manifest_sha() != before_sha, (
        "format 3 changes what manifest_sha is, so it must change value even "
        "for content that did not")
    head = record(root, after["generation"])
    assert head["kind"] == "checkpoint"
    assert head["sha_kind"] == "merkle-v1"

    # and writing now works
    adapter.gc(keep_last=2)
    assert adapter.verify(mode="full")["healthy"]

    # idempotent
    assert adapter.upgrade_manifests()["upgraded"] is False
    adapter.close()


@pytest.mark.parametrize("fixture,format_version", LEGACY)
def test_the_upgraded_store_reads_the_same_rows(tmp_path, fixture,
                                                format_version):
    root = legacy_store(tmp_path, fixture)
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
    assert reopened.manifest_format() == 3
    reopened.close()


@pytest.mark.parametrize("fixture,format_version", LEGACY)
def test_tcsr_rebuilds_after_the_upgrade(tmp_path, fixture, format_version):
    """Memo §4: existing TCSR caches are invalidated by the stamp check and
    silently rebuilt — which is exactly what that check is for."""
    root = legacy_store(tmp_path, fixture)
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

@pytest.mark.parametrize("fixture,format_version", LEGACY)
def test_cli_upgrade_manifests_then_verify(tmp_path, fixture, format_version):
    root = legacy_store(tmp_path, fixture)

    # verify refuses nothing on an older store — reads are fine
    done = cli("store", "verify", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert "healthy" in done.stdout

    done = cli("store", "upgrade-manifests", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert f"manifest format {format_version} -> 3" in done.stdout

    done = cli("store", "verify", "--store", str(root), "--full")
    assert done.returncode == 0, done.stderr
    assert "healthy" in done.stdout
    assert "PROBLEMS" not in done.stdout

    # running it again is a no-op, not an error
    done = cli("store", "upgrade-manifests", "--store", str(root))
    assert done.returncode == 0, done.stderr
    assert "already at manifest format 3" in done.stdout

    # gc, previously refused, now works through the CLI too
    done = cli("store", "gc", "--store", str(root), "--keep", "2")
    assert done.returncode == 0, done.stderr


def test_cli_documents_the_verb_in_its_help():
    done = cli("store", "--help")
    assert done.returncode == 0, done.stderr
    text = flat(done.stdout)
    assert "upgrade-manifests" in text
    assert "format 1" in text
    assert "format 2" in text
    assert "format 3" in text
    assert "manifest_sha" in text
