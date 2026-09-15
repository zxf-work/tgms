"""Replay memory retention (D-087).

**The incident.** On xzgpu, `tgms replay <eventlog.jsonl> --store <dir>
--backend native --compact-every 500` over the 24h longevity soak's log
(1,074,952 batches, 314,897,038 bytes) was OOM-killed by the kernel after
~3.4h at ~83 GB anon RSS, having reached manifest generation ~513,024 —
~160 KB retained per replayed generation. `store_digest()`-relevant content
was only ~1.7M entities: retention, not data. The soak's live writer (which
also runs `compact()`+`gc()` periodically, `--compact-every-batches 500`)
showed the same ~160 KB/commit growth rate within every one of its 42
lives, so the retention is on the shared engine commit/compaction path, not
specific to the `replay` reader.

**Root cause** (`crates/tgms-engine-core/src/store.rs`, the `Postings`
struct): `by_identity` and `by_vid` are append-only identity indexes with no
prune at all (unlike `open_rows`, which at least prunes per-lookup in
`read.rs::locate_open`). Compaction physically reseals every row that still
exists — open *and* closed, since compaction never drops one — into fresh
segments every cycle (`compact.rs::compact`), and the next lookup
(`read.rs::index_segments`, reached through any correction's
`close_version` -> `locate_vid`) re-indexes them there, on top of every
earlier cycle's now-unreachable entries that nothing ever freed. The fix
(`Postings::retain_segments`, wired into `NativeStore::evict_segments_not_in`,
which `gc()` already called for the segment cache and the verified-segment
set) drops postings entries for any segment gc has actually removed from
disk — costing no information, since the same row is re-indexed under its
new segment's id the next time something looks it up.

**Why this test uses `postings_stats()` rather than process RSS.** RSS is
confounded by `compact()`'s own legitimate, bounded, O(current-row-count)
working set (`all_edge_versions()` builds a fresh list every call) and by
allocator/page-granularity noise — both large enough at a CI-sized batch
count to swamp the signal in either direction. `NativeStore::postings_stats`
(D-087, mirroring the existing `segment_cache_stats` receipt for D-041)
reports the identity postings' own entry counts directly, which is exactly
what the fix bounds and what a leak would multiply — a fast, deterministic
measurement instead of a statistical one. `scripts/`-level RSS sampling
(the reproduction this test is derived from) is still the right tool for a
one-off soak-scale investigation; it is not a good fit for a fast, CI-safe
regression gate.

**Scale.** Every commit here is a real engine commit (segment + dict +
manifest + `CURRENT`, each fsynced), which floors around 30 ms/commit on
the measured host — the actual soak's own 20,000+/500-generation scale
would take minutes here, not seconds. 400 batches over 10 compaction
cycles (compact_every=40) already separates the fixed and unfixed engine by
~5.5x in raw postings entries (399 vs 2190, measured while diagnosing this
bug) and completes in well under 30s.
"""

from __future__ import annotations

import pytest

from tgms.core.model import canonical_json, sha256_hex
from tgms.storage.base import make_op
from tgms.storage.eventlog import HEADER, replay

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.native import NativeAdapter  # noqa: E402

N_BATCHES = 400
COMPACT_EVERY = 40

#: The correction pattern the soak's writer stream exercises (D-087's
#: trigger): every batch after the first closes the one still-open version
#: of the same edge identity and opens a fresh one, which is what drives
#: `close_version` -> `locate_vid` -> `index_segments` on (almost) every
#: commit — the soak's own "10-35% correction" mixture, concentrated.
_REF = {"kind": "edge", "src": "A", "dst": "B", "rel_type": "R", "disc": ""}
_OPEN_END = 4611686018427387904  # tgms.core.model.OPEN_END


def _build_correction_log(path, n: int) -> None:
    """Write `n` batches directly as JSONL (D-042 format), bypassing a real
    `Store`/engine so building the log costs string formatting and hashing
    rather than `n` more fsynced commits. `replay` reconstructs the store
    from scratch, so the log never needs a store that already matches it."""
    lines = [canonical_json(HEADER)]
    for i in range(n):
        tt = i + 1
        if i == 0:
            ops = [make_op("assert_edge", src="A", dst="B", rel_type="R",
                           props={"i": 0}, vt_s=0, vt_e=_OPEN_END, disc="",
                           source="ingest", provenance_ref=None)]
        else:
            ops = [make_op("correct", ref=_REF, props={"i": i}, vt_s=0,
                           vt_e=_OPEN_END, source="ingest",
                           provenance_ref=None)]
        rec = {"batch_id": sha256_hex(canonical_json({"tt": tt, "ops": ops}))[:16],
               "tt": tt, "ops": ops}
        lines.append(canonical_json(rec))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_replay_with_compaction_keeps_identity_postings_bounded(tmp_path):
    """The D-087 regression gate. Every version ever committed legitimately
    lives in `by_identity`/`by_vid` forever (bi-temporal history is never
    dropped), so `N_BATCHES` is the correct floor — a 3x margin over it
    catches the multiplicative leak (unfixed: ~2190 entries, 5.5x the
    floor, at this exact scale) while tolerating the transient double-
    booking `gc(keep_last=2)` still retains across the two most recent
    generations."""
    log_path = tmp_path / "eventlog.jsonl"
    _build_correction_log(log_path, N_BATCHES)

    adapter = NativeAdapter(tmp_path / "store" / "native")
    applied = replay(log_path, adapter, compact_every=COMPACT_EVERY)
    assert applied == N_BATCHES

    by_identity, by_vid, indexed = adapter._store.postings_stats("edge")
    adapter.close()

    assert by_identity <= 3 * N_BATCHES, (
        f"by_identity holds {by_identity} entries for {N_BATCHES} versions "
        f"ever committed over {N_BATCHES // COMPACT_EVERY} compaction "
        f"cycles -- compaction is re-indexing history that nothing ever "
        f"prunes (D-087)"
    )
    assert by_vid <= 3 * N_BATCHES, (
        f"by_vid holds {by_vid} entries for {N_BATCHES} versions -- same "
        f"leak as by_identity (D-087)"
    )
    assert indexed <= 4 * COMPACT_EVERY, (
        f"`indexed` holds {indexed} segment ids after "
        f"{N_BATCHES // COMPACT_EVERY} compactions of {COMPACT_EVERY} "
        f"segments each -- stale segment ids from superseded compaction "
        f"cycles are never forgotten (D-087)"
    )


def test_replay_without_compact_every_also_stays_bounded(tmp_path):
    """The other half of the matrix: uncompacted replay never triggers
    compaction's reseal at all, so it was never exposed to D-087's leak —
    confirmed rather than assumed, and cheap enough (no compaction cost) to
    run at the same batch count."""
    log_path = tmp_path / "eventlog.jsonl"
    _build_correction_log(log_path, N_BATCHES)

    adapter = NativeAdapter(tmp_path / "store" / "native")
    applied = replay(log_path, adapter)
    assert applied == N_BATCHES

    by_identity, by_vid, indexed = adapter._store.postings_stats("edge")
    adapter.close()

    assert by_identity <= 3 * N_BATCHES
    assert by_vid <= 3 * N_BATCHES
    assert indexed <= N_BATCHES
