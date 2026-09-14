"""Unit tests for `scripts/eval_corruption.py` (Lane A task A4).

Each test builds one tiny fixture store (via the harness's own
`build_store`, so the fixture is exactly what the real sweep uses), applies
one deterministic, known mutation, and asserts the harness's own
observe/classify pipeline reaches the expected verdict for the right
reason — not just "some verdict".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from scripts import eval_corruption as ec  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path):
    store_dir = tmp_path / "s"
    build_meta = ec.build_store(store_dir, trial_seed=12345)
    baseline = ec.baseline_digests(store_dir)
    return store_dir, build_meta, baseline


def _classify(store_dir, mut_info, build_meta, baseline, cls):
    obs = ec.observe(store_dir, build_meta["artifact_names"])
    tcsr = ec.check_tcsr_rebuild(store_dir) if cls == "tcsr_file" else None
    return ec.classify(cls, mut_info, obs, baseline, build_meta, tcsr), obs


def test_flipped_segment_byte_is_detected_via_verify(store):
    """A live (non-orphaned) segment's data byte flipped: the native engine's
    checksum walk must catch it, mirroring
    tests/test_native_faults.py::test_flipped_data_byte_fails_a_column_checksum."""
    store_dir, build_meta, baseline = store
    live_segments = [
        p for p in ec.FILE_CLASSES["segment_body"](store_dir)
        if str(p.relative_to(store_dir)) not in build_meta["orphan_files"]
    ]
    assert live_segments, "fixture must have at least one live segment post-compaction"
    seg = live_segments[0]
    data = seg.read_bytes()
    offset = ec._segment_data_start(seg) + 8
    mut_info = {"class": "segment_body", "mutation": "flip_byte", "note": None,
               "file": str(seg.relative_to(store_dir)), "offset": offset}
    buf = bytearray(data)
    buf[offset] ^= 0xFF
    seg.write_bytes(bytes(buf))

    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, "segment_body")
    assert verdict == "DETECTED", reason
    assert obs["verify_problems"], "expected verify() to report a checksum finding"


def test_deleted_current_is_detected_via_open(store):
    """A deleted `CURRENT` used to make the engine treat a populated store
    as freshly initialized (empty) rather than refusing to open — a real
    finding (docs/eval_durability.md), detected only when a fixed query
    against a known uid came back empty. `NativeStore::open` now refuses
    directly: a populated `manifests/`, `seg/`, or dictionary log with no
    `CURRENT` is corruption, not an empty store, so detection happens at
    open and the fixed queries never even run."""
    store_dir, build_meta, baseline = store
    candidates = ec.FILE_CLASSES["current"](store_dir)
    mut_info = ec.apply_mutation("current", "delete_file", candidates, __import__("random").Random(1), store_dir)
    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, "current")
    assert verdict == "DETECTED", reason
    assert not obs["open_ok"], "a populated store missing CURRENT must refuse to open"
    assert "CURRENT" in (obs["open_error"] or ""), obs["open_error"]


def test_tampered_artifacts_jsonl_is_detected_via_artifact_check(store):
    """Mirrors tests/test_artifact_registry.py::test_tamper_raises_on_reopen:
    flipping a byte inside a record's own `record_digest` value must be
    caught — here, by this harness's artifact-check observation."""
    store_dir, build_meta, baseline = store
    path = ec.FILE_CLASSES["artifacts_jsonl"](store_dir)[0]
    raw = bytearray(path.read_bytes())
    lines = bytes(raw).split(b"\n")
    marker = b'"record_digest":"'
    target = next(i for i, line in enumerate(lines) if marker in line)
    line = bytearray(lines[target])
    at = line.index(marker) + len(marker) + 3
    line[at] ^= 0x01
    lines[target] = bytes(line)
    path.write_bytes(b"\n".join(lines))

    mut_info = {"class": "artifacts_jsonl", "mutation": "flip_bit", "note": None,
               "file": str(path.relative_to(store_dir)), "offset": None}
    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, "artifacts_jsonl")
    assert verdict == "DETECTED", reason
    assert obs["artifact_errors"], "expected the registry chain-tamper check to raise"


def test_orphaned_pre_compaction_segment_is_benign(store):
    """A superseded *segment* or *close-run* file — `compact()` already
    stopped referencing it before the mutation ran — is invisible to both
    verify modes and to a query against the current generation: the
    harness's own documented BENIGN case, not silence.

    Restricted to `.tgs`/`.tgc` orphans specifically (not orphaned
    manifests — see `test_orphaned_manifest_generation_is_detected_under_full_verify`
    right below for why `--verify-mode full`'s own manifest-parent-chain
    check, added Lane A task A9, changes that case to DETECTED instead)."""
    store_dir, build_meta, baseline = store
    file_orphans = [r for r in build_meta["orphan_files"]
                    if r.endswith(".tgs") or r.endswith(".tgc")]
    assert file_orphans, "fixture must have at least one orphaned segment/close-run"
    orphan_rel = sorted(file_orphans)[0]  # deterministic: set iteration order is not
    orphan = store_dir / orphan_rel
    assert orphan.exists(), "the orphan file must still be on disk (no gc has run)"
    cls = "segment_body" if orphan_rel.endswith(".tgs") else "close_run"

    data = bytearray(orphan.read_bytes())
    offset = len(data) // 2 if data else 0
    if data:
        data[offset] ^= 0xFF
        orphan.write_bytes(bytes(data))
    else:
        orphan.write_bytes(b"\xff" * 8)

    mut_info = {"class": cls, "mutation": "flip_byte", "note": None,
               "file": orphan_rel, "offset": offset}
    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, cls)
    assert verdict == "BENIGN", (verdict, reason, obs)
    assert "superseded" in reason


def test_orphaned_manifest_generation_is_detected_under_full_verify(store):
    """Found while switching the default verify observation to `mode="full"`
    (Lane A task A9, once A3 landed `verify_full`): unlike a superseded
    segment/close-run, a superseded *manifest* generation is NOT invisible
    under full mode. `NativeStoreAdapter.verify(mode="full")`'s own
    docstring says it walks "the manifest parent chain across every
    retained generation" — and empirically that means every manifest FILE
    still physically present on disk (this fixture's `compact()` writes a
    fresh, self-contained checkpoint but does not delete the superseded
    generations — only `gc()` does that), not only the chain reachable by
    walking backward from `CURRENT`. So corrupting an orphaned manifest
    generation still on disk is DETECTED under full mode, where it was
    BENIGN under fast mode (`--verify-mode fast` reproduces the old,
    weaker result) — a real, desirable strengthening from the stronger
    oracle, not a false positive: full mode is telling the truth about a
    file it now actually reads."""
    store_dir, build_meta, baseline = store
    manifest_orphans = [r for r in build_meta["orphan_files"] if r.endswith(".json")]
    assert manifest_orphans, "fixture must have at least one orphaned manifest generation"
    orphan_rel = sorted(manifest_orphans)[0]
    orphan = store_dir / orphan_rel
    kind = json.loads(orphan.read_text()).get("kind", "checkpoint")
    cls = "checkpoint_manifest" if kind == "checkpoint" else "delta_manifest"

    data = bytearray(orphan.read_bytes())
    offset = len(data) // 2
    data[offset] ^= 0xFF
    orphan.write_bytes(bytes(data))

    mut_info = {"class": cls, "mutation": "flip_byte", "note": None,
               "file": orphan_rel, "offset": offset}
    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, cls)
    assert verdict == "DETECTED", (verdict, reason, obs)
    assert obs["verify_problems"], "expected full-mode verify to walk the orphaned generation"

    # Fast mode still calls it BENIGN — same file, same mutation, weaker oracle.
    obs_fast = ec.observe(store_dir, build_meta["artifact_names"], verify_mode="fast")
    verdict_fast, reason_fast = ec.classify(cls, mut_info, obs_fast, baseline, build_meta, None)
    assert verdict_fast == "BENIGN", (verdict_fast, reason_fast, obs_fast)


def test_torn_event_log_tail_is_detected_even_read_only(store):
    """D-086's trim contract (tests/test_torn_wal.py) says a *writer*-mode
    open silently truncates an unacknowledged torn tail and recovers. This
    harness's `open` observation uses `read_only=True` instead (task A4's
    literal spec) — which skips `Store._recover`'s replay/trim, but does
    **not** skip `Store._seed_frontier`, which unconditionally walks
    `EventLog.batches_from(0)` to compute the applied-prefix frontier even
    for a reader (`tgms/store.py::_seed_frontier`'s own docstring: "a
    read-only handle" still needs this to avoid a false-freshness hazard).
    That walk has no torn-tail tolerance of its own, so it raises on the
    torn bytes before `_recover` would ever have gotten a chance to trim
    them. Net effect, empirically confirmed here rather than assumed: under
    THIS harness's read-only open, a torn tail is DETECTED, not BENIGN —
    the trim contract's tolerance is real but is a writer-mode-only
    property this observation does not exercise (see
    docs/eval_durability.md's EXP-A3 section for the same note)."""
    store_dir, build_meta, baseline = store
    log = ec.FILE_CLASSES["event_log_tail"](store_dir)[0]
    with open(log, "ab") as f:
        f.write(b'{"batch_id": "torn", "tt": 9')
    mut_info = {"class": "event_log_tail", "mutation": "append_garbage", "note": None,
               "file": str(log.relative_to(store_dir)), "offset": None}
    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, "event_log_tail")
    assert verdict == "DETECTED", (verdict, reason, obs)
    assert not obs["open_ok"]

    # And the trim contract itself still holds independently, exactly as
    # tests/test_torn_wal.py pins it: a plain *writer* open (no
    # read_only=True) truncates the torn bytes and recovers cleanly.
    import tgms
    writer = tgms.open(store_dir, backend="native")
    assert writer.adapter.verify()["healthy"]
    writer.close()
    with open(log, "rb") as f:
        assert b'"torn"' not in f.read()


def test_corrupted_tcsr_index_is_detected_under_full_verify(store):
    """tests/test_tcsr_persistence.py::test_a_damaged_file_degrades_to_rebuild:
    a damaged persisted index must never error and must never be trusted —
    the engine itself silently rebuilds it, and that degrade-to-rebuild
    contract still holds (checked below via `check_tcsr_rebuild` directly,
    same as the harness's own fifth, class-gated check).

    But TOLERATED-REBUILT — this harness's verdict name for "the mutation
    was invisible to verify() and only caught by the dedicated tcsr rebuild
    check" — is no longer what a real `flip_byte`-style corruption produces
    once observation 2 is `verify(mode="full")` (Lane A task A9's default,
    once A3 landed `verify_full`): `NativeStoreAdapter._verify_tcsr` reads
    the persisted `.npz` directly as part of full-mode verify and reports
    an "error"-severity `tcsr-unreadable`/`tcsr-shape` finding for any
    corruption severe enough that `np.load` chokes on it or the shape does
    not fit — which is what every real mutation this harness applies
    produces (a singleton class, so `swap_same_class` even falls back to
    `flip_byte`). So this class's mutations are now DETECTED via verify()
    before the dedicated tcsr check ever gets a chance to matter for
    classification; `--verify-mode fast` (below) still reaches
    TOLERATED-REBUILT, since fast mode never reads the tcsr file at all."""
    store_dir, build_meta, baseline = store
    idx = ec.FILE_CLASSES["tcsr_file"](store_dir)[0]
    idx.write_bytes(b"not a zipfile at all")
    mut_info = {"class": "tcsr_file", "mutation": "flip_byte", "note": None,
               "file": str(idx.relative_to(store_dir)), "offset": 0}

    (verdict, reason), obs = _classify(store_dir, mut_info, build_meta, baseline, "tcsr_file")
    assert verdict == "DETECTED", (verdict, reason, obs)
    assert obs["verify_problems"], "expected full-mode verify to read the tcsr file directly"

    # The engine's own degrade-to-rebuild contract is unaffected either way —
    # it is a property of the engine, not of which verify mode observed it.
    tcsr = ec.check_tcsr_rebuild(store_dir)
    assert tcsr["rebuilt"] and tcsr["error"] is None, tcsr

    # Fast mode never reads the tcsr file, so it still reaches the verdict
    # this class was originally documented to produce.
    obs_fast = ec.observe(store_dir, build_meta["artifact_names"], verify_mode="fast")
    verdict_fast, reason_fast = ec.classify(
        "tcsr_file", mut_info, obs_fast, baseline, build_meta, tcsr)
    assert verdict_fast == "TOLERATED-REBUILT", (verdict_fast, reason_fast, obs_fast)


def test_swap_same_class_falls_back_when_only_one_file_exists(store):
    """CURRENT has exactly one instance — `apply_mutation` must not crash on
    a `swap_same_class` request against a singleton class; it must fall
    back to a real mutation and say so in `note`."""
    store_dir, build_meta, baseline = store
    candidates = ec.FILE_CLASSES["current"](store_dir)
    assert len(candidates) == 1
    rng = __import__("random").Random(2)
    mut_info = ec.apply_mutation("current", "swap_same_class", candidates, rng, store_dir)
    assert mut_info["mutation"] != "swap_same_class"
    assert mut_info["note"] is not None


def test_json_record_conforms_to_result_manifest_schema(tmp_path, monkeypatch):
    import jsonschema

    out = tmp_path / "corruption.json"
    monkeypatch.setattr(sys, "argv",
                        ["eval_corruption.py", "--trials", "3", "--seed", "9", "--json", str(out)])
    rc = ec.main()
    assert rc in (0, 1)
    schema = json.loads((ROOT / "benchmarks" / "schema" / "result_manifest.schema.json").read_text())
    data = json.loads(out.read_text())
    jsonschema.validate(data, schema)
    assert data["summary"]["verdict_counts"]["SILENT"] == 0
