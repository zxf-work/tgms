"""`NativeAdapter.open_phase_us` — component timing for manifest-chain open.

B1-v2's A/B (`benchmarks/results-v1/b1-manifest-v2-ab-2026-09.README.md`
§B1(c)) could not score "manifest-chain open <= 70 ms" because the native
adapter exposed no open-phase timing: `NativeAdapter()` was a single opaque
constructor call, so a checkpoint-load vs delta-replay split had to be
flagged as unmeasured rather than reported
(`benchmarks/results-v1/b1-manifest-v2-ab-2026-09-raw.json`'s
`component_breakdown_note`). This mirrors the commit path's `phase_p50_us`
(`crates/tgms-engine-py/src/lib.rs::last_commit_phases`,
`scripts/eval_concurrency.py`): `open_phase_us()` on the raw `_engine.NativeStore`
returned by `NativeAdapter._store`.

Engine-level properties (per-phase arithmetic, format-3 vs. legacy chains)
belong in Rust (`crates/tgms-engine-core/src/manifest_chain.rs`,
`crates/tgms-engine-core/src/store.rs`); what is worth asserting from Python
is the product-facing promise: every key is present, `delta_count` matches
what was actually written, and a reopen is stable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.native import NativeAdapter  # noqa: E402

OPEN_PHASE_KEYS = {
    "checkpoint_read_parse_us",
    "merkle_verify_us",
    "state_build_us",
    "delta_replay_us",
    "delta_count",
    "dictionary_open_us",
    "other_us",
    "total_us",
    "chain_format",
}


def build_store(root: Path, generations: int):
    """A native store with a genesis checkpoint and `generations` deltas.

    The default checkpoint interval is 512 (`lib.rs::MANIFEST_CHECKPOINT_EVERY`),
    so any run well under that keeps generation 0 the only checkpoint and
    every generation above it a delta — exactly what `delta_count` below
    counts.
    """
    import tgms

    store = tgms.open(root, backend="native")
    for g in range(generations):
        store.ingest_events([
            {"src": f"n{g}", "dst": f"n{g + 1}", "rel_type": "R",
             "vt_s": g, "vt_e": g + 10, "props": {"g": g}}
        ])
    assert store.adapter._store.generation() == generations
    store.close()


def test_open_phase_us_has_every_key(tmp_path):
    build_store(tmp_path / "s", 5)
    adapter = NativeAdapter(tmp_path / "s" / "native")
    try:
        phases = adapter._store.open_phase_us()
    finally:
        adapter.close()
    assert set(phases) == OPEN_PHASE_KEYS
    for key in OPEN_PHASE_KEYS:
        assert isinstance(phases[key], int), f"{key} is not an int: {phases[key]!r}"
    # this reopen actually reads and parses generation 0's checkpoint from
    # disk (unlike a fresh, never-yet-written store -- see
    # test_a_fresh_store_has_no_deltas_to_replay), so the single-parse path
    # in manifest_chain.rs must report positive time for it.
    assert phases["checkpoint_read_parse_us"] > 0


def test_delta_count_matches_generations_written(tmp_path):
    n = 12
    build_store(tmp_path / "s", n)
    adapter = NativeAdapter(tmp_path / "s" / "native")
    try:
        phases = adapter._store.open_phase_us()
    finally:
        adapter.close()
    assert phases["delta_count"] == n


def test_total_us_covers_the_named_phases(tmp_path):
    build_store(tmp_path / "s", 20)
    adapter = NativeAdapter(tmp_path / "s" / "native")
    try:
        phases = adapter._store.open_phase_us()
    finally:
        adapter.close()
    named = (
        phases["checkpoint_read_parse_us"]
        + phases["merkle_verify_us"]
        + phases["state_build_us"]
        + phases["delta_replay_us"]
        + phases["dictionary_open_us"]
    )
    assert phases["total_us"] >= named
    assert phases["other_us"] == phases["total_us"] - named


def test_reopening_the_same_store_yields_the_same_delta_count(tmp_path):
    n = 8
    build_store(tmp_path / "s", n)

    counts = []
    for _ in range(2):
        adapter = NativeAdapter(tmp_path / "s" / "native")
        try:
            counts.append(adapter._store.open_phase_us()["delta_count"])
        finally:
            adapter.close()
    assert counts[0] == counts[1] == n


BUILD_INFO_KEYS = {
    "debug_assertions",
    "profile",
    "opt_level",
    "engine_version",
    "manifest_format_version",
}


def test_build_info_has_every_key_and_agrees_with_module_constants():
    # The 2026-09 engine-commit A/B diagnosis found the untimed residual
    # absorbing 100% of the treatment's decile growth, and a debug-assertions
    # build (which runs `store::publish`'s O(segments) `debug_assert_eq!`
    # every commit) was the top candidate -- every timing record should be
    # able to state its build profile.
    info = NativeAdapter.build_info()
    assert set(info) == BUILD_INFO_KEYS
    assert isinstance(info["debug_assertions"], bool)
    assert info["profile"] in ("debug", "release", "unknown")
    assert isinstance(info["opt_level"], str) and info["opt_level"]
    assert isinstance(info["engine_version"], str) and info["engine_version"]

    from tgms import _engine

    assert info["manifest_format_version"] == _engine.MANIFEST_FORMAT_VERSION
    # no store needed -- it is a property of the loaded extension, not of an
    # open handle
    assert info == _engine.build_info()


def test_build_info_is_stable_across_calls():
    assert NativeAdapter.build_info() == NativeAdapter.build_info()


def test_a_fresh_store_has_no_deltas_to_replay(tmp_path):
    # genesis: no CURRENT on disk yet, so the manifest chain has nothing to
    # reconstruct -- delta_count and delta_replay_us are both zero, and
    # chain_format still reports the format the genesis checkpoint was
    # written under.
    adapter = NativeAdapter(tmp_path / "fresh" / "native")
    try:
        phases = adapter._store.open_phase_us()
    finally:
        adapter.close()
    assert phases["delta_count"] == 0
    assert phases["delta_replay_us"] == 0
    assert phases["chain_format"] > 0
