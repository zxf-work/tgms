//! Store layout, recovery, and the commit protocol (spec §4, §5.2).
//!
//! Durability objective (D-028, blueprint §1): the store is a deterministic
//! materialization of the event log, so we do not need to make every partial
//! physical update recoverable — we need to *never expose an undetected
//! inconsistent generation*. Everything here serves that: a commit publishes
//! files first and flips a single pointer last, so any crash leaves the
//! previous generation intact and the partial work orphaned.
//!
//! Commit order (each step durable before the next begins):
//!
//! 1. the event log is appended and fsynced — by the Python `Store`, before
//!    the engine is called at all (write-ahead, `store.py::_write`);
//! 2. segment and close-run files are written and fsynced;
//! 3. the dictionary tail is appended and fsynced;
//! 4. `manifests/<G>.json` is written, fsynced, renamed, and its directory
//!    fsynced — as a delta against generation G−1, or as a full checkpoint
//!    every `K`th generation (`manifest_chain`);
//! 5. `CURRENT` is rewritten atomically — **the publication point**.
//!
//! A crash before step 5 is invisible: `open` reads the old `CURRENT`, and
//! the orphaned manifest and dictionary tail are ignored (and the tail
//! truncated). A crash during step 5 leaves either the old or the new
//! pointer, never a torn one, because the rename is atomic.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::dict::Dictionary;
use crate::error::{EngineError, Result};
use crate::derive::Id96;
use crate::manifest::{self, merkle, CloseRunRef, DictRef, EventLogRef, Manifest, Stats, Widths};
use crate::integrity::{self, Finding};
use crate::manifest_chain::{self, AppendSpan, ManifestDelta};
use crate::row::{EdgeRow, Lane, NodeRow, RowKind};
use crate::segment::MmapSource;
use crate::staging::{PartitionMap, Staging};
use crate::visibility::{read_close_run, write_close_run, CloseIndex, CloseRecord};

const CURRENT: &str = "CURRENT";

/// Durability-injection point (D-086). A no-op unless `TGMS_CRASH_POINT`
/// names this exact point, in which case the process aborts — no unwinding,
/// no destructors — so the on-disk state is what a power cut at this line
/// would leave. The env read costs one lookup per call on the write path
/// only; the harness (`scripts/eval_durability.py`) is the only intended
/// setter.
pub(crate) fn crash_point(name: &str) {
    if let Ok(v) = std::env::var("TGMS_CRASH_POINT") {
        if v == name {
            eprintln!("TGMS_CRASH_POINT hit: {name}");
            std::process::abort();
        }
    }
}
const DICT: &str = "dict.log";
const SUBDIRS: [&str; 4] = ["manifests", "seg", "close", "idx"];
/// Marker written by `compact_current_only` (plan §13). A store carrying it
/// has physically discarded its superseded versions, so it must refuse the
/// questions it can no longer answer honestly — past-belief reads and new
/// corrections — no matter which handle opens it.
const CURRENT_ONLY_MARKER: &str = "CURRENT_ONLY";

pub struct NativeStore {
    root: PathBuf,
    /// Canonicalized root — the key under which this handle pins its
    /// generation in the process-global reader table (gc.rs). Canonical so
    /// two handles opened through different spellings of one path agree.
    pin_key: PathBuf,
    dict: Dictionary,
    /// The reconstructed manifest of the generation this handle sees — and,
    /// on the write path, the *working* manifest a commit mutates in place.
    ///
    /// It used to be cloned three times per commit: once by `successor`, once
    /// by `seal`'s `body_sha`, once by `publish`'s redundant `verify`. That is
    /// 5.9 µs per live segment per commit, and 100% of the measured 1.8×
    /// last/first-decile growth (V2 diagnosis §2). The store already carried
    /// this across commits; it simply did not reuse it.
    manifest: Manifest,
    /// The Merkle state of `manifest`, carried across commits so a commit's
    /// digest costs O(appended + log n) rather than a full re-serialization.
    /// `None` on a store this build may not write (formats 1 and 2), where
    /// there is no such digest to maintain.
    merkle: Option<manifest::merkle::ManifestMerkle>,
    /// Generation of the last full checkpoint on the manifest chain — where
    /// `open` would start replaying deltas from, and what each delta this
    /// handle writes records as its `checkpoint` hint.
    checkpoint_gen: u64,
    /// Per-handle override of the checkpoint interval (`None` = the process
    /// default, `TGMS_MANIFEST_CHECKPOINT_EVERY` or
    /// `defaults::MANIFEST_CHECKPOINT_EVERY`). Exists for the same reason
    /// `set_segment_cache_budget` does: a sweep or a test must be able to
    /// change K without mutating the process environment, which races other
    /// threads.
    checkpoint_every: Option<u64>,
    staging: Staging,
    /// Closes landing on rows staged in this same batch — folded into the
    /// owning segment's sidecar at seal, so no run file is needed.
    staged_closes: HashMap<Id96, i64>,
    /// Closes landing on already-committed rows, written as a close run.
    pending_closes: Vec<CloseRecord>,
    partitions: PartitionMap,
    segment_target_bytes: u64,
    /// Cost-guardrail statistics, maintained incrementally (spec §4.1: stats
    /// are served from the manifest, never by scanning). Built once on first
    /// use, then each commit folds in its own batch, so a write-then-read
    /// loop never rescans.
    stats: std::sync::Mutex<Option<StatsAccum>>,
    /// Identity postings, built incrementally (C3.1). Segments are
    /// immutable and manifests only accumulate, so a segment is indexed once
    /// and never revisited — which keeps a store built by N single-row
    /// batches linear overall instead of quadratic.
    edge_postings: std::sync::Mutex<Postings>,
    node_postings: std::sync::Mutex<Postings>,
    /// Segments whose checksums have been walked in this session. Corruption
    /// must be caught *before* rows reach a query, but re-hashing a file on
    /// every read would be ruinous — so each segment is verified the first
    /// time this process opens it, and trusted thereafter. Segments are
    /// immutable, so once verified they stay valid for the session.
    verified: std::sync::Mutex<std::collections::HashSet<String>>,
    /// The close index the current generation makes visible, built once per
    /// generation and shared by every read (keyed by manifest generation).
    /// Sound for the same reason the segment cache is: close-run files are
    /// immutable, and the visible set of runs only changes when a commit
    /// publishes a new manifest — so within one generation the rebuilt index
    /// is always identical. Without this, every point read re-reads every
    /// run file, which made correction-heavy replay quadratic.
    close_cache: std::sync::Mutex<Option<CloseCacheEntry>>,
    /// `segment id -> filename`, per lane, built once per generation (D-077).
    ///
    /// Every point read — `locate`, `locate_open`, `locate_vid` — needs to
    /// turn a posting's segment id back into a file, and each was rebuilding
    /// this from the manifest per call: a String clone *and* a filename parse
    /// per segment, to answer a question about one identity. At 1,000
    /// segments that was 94% of a `believed_*` call. Sound for the same
    /// reason `close_cache` is: the segment set changes only when a commit
    /// publishes a new manifest.
    files_cache: std::sync::Mutex<Option<(u64, std::sync::Arc<SegmentFiles>)>>,
    /// The open batch's `pending_closes`, as a layer over the committed
    /// index, so a read inside the batch sees them (D-059). Maintained as
    /// they are recorded rather than rebuilt per read: `apply_ops` reads
    /// belief once per op, and rebuilding a K-close overlay on each of K
    /// reads is the "small lookup rebuilding the whole store" shape this
    /// engine has now met six times.
    ///
    /// `Arc::make_mut` is what keeps it cheap *and* honest: the layer is
    /// updated in place while this handle holds the only reference, and
    /// copied if a reader is still holding one — so no reader ever sees a
    /// close appear underneath it.
    pending_overlay: Option<std::sync::Arc<CloseIndex>>,
    /// Open segments, by file name. Sound because segment files are
    /// immutable — closes live in separate .tgc files and compaction writes
    /// new files — so a cached entry can never be stale. This is also what
    /// makes compressed columns viable: they are decoded once per process
    /// here, not once per operator call.
    ///
    /// Byte-budgeted since the §14.2 measurement priced the unbounded form
    /// at a ~6 GB per-process floor for a 268 MB store (docs/
    /// eval_resources.md): least-recently-used whole segments are dropped
    /// once the accounted bytes exceed the budget, and an evicted segment
    /// simply reopens on next touch — checksum walk skipped via `verified`,
    /// which is never evicted, so the once-per-session guarantee holds.
    /// Correctness never depends on residency; the budget trades re-decode
    /// latency for memory. `TGMS_SEGMENT_CACHE_BYTES` overrides (0 =
    /// unbounded); the default is half of detected physical RAM (D-041).
    segments: std::sync::Mutex<SegmentCache>,
    /// Transaction time of the open batch, if any (`begin` .. `commit`).
    batch_tt: Option<i64>,
    /// True when the `CURRENT_ONLY` marker is present: the stripped
    /// experimental configuration of plan §13, which has dropped historical
    /// versions and must refuse past-belief queries and corrections.
    current_only: bool,
    /// Where the last commit spent its time (instrumentation, not contract).
    last_commit: Option<CommitPhases>,
    /// Where this handle's `open` call spent its time (instrumentation, not
    /// contract) — always populated, since every live handle came from a
    /// successful `open`.
    open_phases: OpenPhases,
}

/// Wall-clock microseconds per phase of one commit, plus the two numbers
/// that decide whether the singleton-write floor is fsyncs or manifest size.
///
/// Lessons §7 attributes the 265× batch-vs-singleton gap to "several
/// fsyncs"; `docs/eval_writes.md` attributes the same shape to "a fresh full
/// manifest per commit". Those are different fixes, so the split is measured
/// rather than argued.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CommitPhases {
    /// Step 1: `CommitBase::capture` — the O(log n) snapshot of scalars plus
    /// the Merkle state, taken before `commit_inner` touches anything, so a
    /// failed commit can roll back to it (B1-v2d: previously untimed, folded
    /// into the residual between `commit_start` and `seal_us`).
    pub capture_us: u64,
    /// Step 2: staged rows sealed into segment files, written and fsynced.
    pub seal_us: u64,
    /// Step 2b: the close run for corrections against committed rows.
    pub closes_us: u64,
    /// Folding this batch into the running statistics.
    pub stats_us: u64,
    /// Step 3: the dictionary tail, written and fsynced.
    pub dict_us: u64,
    /// `Manifest::seal_with` — the manifest digest, O(1) from the maintained
    /// Merkle state at format >= 3, O(segments) (`legacy_body_sha`) below it
    /// (B1-v2d: previously untimed, the gap between `dict_us` and step 4's
    /// JSON build the B1-v2 A/B diagnosis (2026-09-15) found absorbing the
    /// fmt2 arms' decile growth).
    pub digest_us: u64,
    /// `publish`'s `debug_assert_eq!` that recomputes the manifest digest
    /// from scratch (O(segments)) to double-check `seal_with`'s incremental
    /// one — compiled out entirely in release builds, where this is always
    /// 0. In debug builds this used to run untimed ahead of `manifest_us`'s
    /// timer, so its O(segments) cost was folded into the residual and grew
    /// with generation count (`commit_phases_fully_account_for_total_us_
    /// over_sixty_singleton_commits`, CI 2026-09-15).
    pub debug_verify_us: u64,
    /// Step 4a: building the manifest record's JSON — a delta
    /// (`ManifestDelta::from_appends`, O(appended)) most generations, or a
    /// full checkpoint (`manifest_chain::checkpoint_json`, O(segments)) every
    /// `every`th one. Split from `manifest_us` because it used to run before
    /// that timer started (B1-v2d).
    pub delta_build_us: u64,
    /// Step 4b: manifest JSON written, fsynced, renamed, dir fsynced.
    pub manifest_us: u64,
    /// Step 5: `CURRENT` rewritten atomically — the publication point.
    pub current_us: u64,
    pub total_us: u64,
    /// Bytes of the manifest this commit wrote, and how many segments it
    /// names: a manifest is rewritten in full every generation, so both grow
    /// with store history rather than with the batch.
    pub manifest_bytes: u64,
    pub segments_named: u64,
    /// Whether step 4 wrote a full checkpoint rather than a delta. A
    /// checkpoint every `K`th generation is the amortized cost the delta
    /// design trades for; separating the two keeps that a measurement rather
    /// than an assumption.
    pub manifest_checkpoint: bool,
}

/// Wall-clock microseconds `open` spent, by phase — the open-path sibling of
/// [`CommitPhases`], written for the same reason: B1-v2's A/B
/// (`benchmarks/results-v1/b1-manifest-v2-ab-2026-09.README.md` §B1(c))
/// could not score "manifest-chain open ≤ 70 ms" because nothing broke the
/// open call down, and a single opaque number cannot tell a checkpoint-read
/// cost from a delta-replay one.
///
/// Instrumentation only, exactly like `CommitPhases`: no behaviour change,
/// nothing different on disk, and the only overhead beyond a normal open is
/// the handful of `Instant::now()` calls this and `manifest_chain` add.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct OpenPhases {
    /// Reading and JSON-deserializing the checkpoint the manifest chain
    /// resolved to.
    pub checkpoint_read_parse_us: u64,
    /// Verifying that checkpoint — building the Merkle tree at format 3 and
    /// up, or the O(n) whole-document digest recompute below it (see
    /// `chain_format`).
    pub merkle_verify_us: u64,
    /// The format of the checkpoint the manifest chain resolved to, i.e.
    /// which of the two `merkle_verify_us` shapes ran.
    pub chain_format: u32,
    /// Applying each delta above the checkpoint into the running
    /// manifest/Merkle state.
    pub state_build_us: u64,
    /// Reading and JSON-deserializing each delta above the checkpoint.
    pub delta_replay_us: u64,
    /// How many deltas were replayed to reach this generation.
    pub delta_count: u64,
    /// `Dictionary::open`: reading and validating the dictionary tail.
    pub dictionary_open_us: u64,
    /// Everything else `open` does: directory creation, the genesis publish
    /// on a fresh store, the gc pin, the current-only marker check, and
    /// assembling the `NativeStore` itself. Total minus the six phases
    /// above, so it absorbs whatever this list does not yet name rather than
    /// silently dropping it.
    pub other_us: u64,
    pub total_us: u64,
}

/// Everything a commit has to be able to put back, and everything the delta
/// it writes needs to know about the generation it came from.
///
/// The commit path advances `self.manifest` in place rather than building a
/// clone of it, so there is no parent object left to diff against or to fall
/// back to. This is that parent, in O(1) space: the scalars are a handful of
/// small values, the four lanes only ever grow within a commit so a length is
/// enough to truncate back to, and a Merkle state is O(log n) hashes.
struct CommitBase {
    generation: u64,
    parent: Option<u64>,
    created_tt: i64,
    event_log: EventLogRef,
    dict: DictRef,
    widths: Widths,
    next_segment_id: u64,
    stats: Stats,
    manifest_sha: String,
    /// Lane lengths before the commit, in lane order.
    split: [usize; 4],
    merkle: Option<merkle::ManifestMerkle>,
}

impl CommitBase {
    fn capture(m: &Manifest, merkle: &Option<merkle::ManifestMerkle>) -> Self {
        Self {
            generation: m.generation,
            parent: m.parent,
            created_tt: m.created_tt,
            event_log: m.event_log.clone(),
            dict: m.dict.clone(),
            widths: m.widths.clone(),
            next_segment_id: m.next_segment_id,
            stats: m.stats.clone(),
            manifest_sha: m.manifest_sha.clone(),
            split: [
                m.node_store.len(),
                m.edge_lanes.event.len(),
                m.edge_lanes.interval.len(),
                m.close_runs.len(),
            ],
            merkle: merkle.clone(),
        }
    }

    /// What the commit appended over, for a delta cut in O(appended).
    fn span(&self) -> AppendSpan {
        AppendSpan {
            parent: self.generation,
            parent_sha: self.manifest_sha.clone(),
            widths: self.widths.clone(),
            split: self.split,
        }
    }

    /// Put the handle's view back where it was. Only the lanes need
    /// truncating: a commit appends to them and never edits one in place, so
    /// the prefix below each split is untouched.
    fn restore(&self, m: &mut Manifest, merkle: &mut Option<merkle::ManifestMerkle>) {
        m.generation = self.generation;
        m.parent = self.parent;
        m.created_tt = self.created_tt;
        m.event_log = self.event_log.clone();
        m.dict = self.dict.clone();
        m.widths = self.widths.clone();
        m.next_segment_id = self.next_segment_id;
        m.stats = self.stats.clone();
        m.manifest_sha = self.manifest_sha.clone();
        m.node_store.truncate(self.split[0]);
        m.edge_lanes.event.truncate(self.split[1]);
        m.edge_lanes.interval.truncate(self.split[2]);
        m.close_runs.truncate(self.split[3]);
        merkle.clone_from(&self.merkle);
        debug_assert_eq!(
            m.manifest_sha,
            m.body_sha_canonical(),
            "rollback did not restore the generation the handle came in with"
        );
    }
}

impl NativeStore {
    pub fn open(root: impl Into<PathBuf>) -> Result<Self> {
        let open_start = std::time::Instant::now();
        let root = root.into();
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub))
                .map_err(|e| EngineError::from(e).at_file(root.join(sub)))?;
        }
        let (manifest, checkpoint_gen, merkle, chain_phases) = if root.join(CURRENT).exists() {
            Self::load_current(&root)?
        } else {
            Self::refuse_if_populated_without_current(&root)?;
            let genesis = Manifest::genesis();
            // generation 0 is always a checkpoint: a chain must start
            // somewhere, and there is no parent to diff against
            Self::publish(&root, None, &genesis, true, u64::MAX)?;
            let state = merkle::ManifestMerkle::from_manifest(&genesis);
            let phases = manifest_chain::ChainOpenPhases {
                chain_format: genesis.format,
                ..Default::default()
            };
            (genesis, 0, Some(state), phases)
        };
        let t = std::time::Instant::now();
        let dict = Dictionary::open(
            root.join(DICT),
            manifest.dict.records,
            manifest.dict.bytes,
        )?;
        let dictionary_open_us = t.elapsed().as_micros() as u64;
        let pin_key = fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
        crate::gc::pin(&pin_key, manifest.generation);
        let current_only = root.join(CURRENT_ONLY_MARKER).exists();
        let total_us = open_start.elapsed().as_micros() as u64;
        let named_us = chain_phases.checkpoint_read_parse_us
            + chain_phases.merkle_verify_us
            + chain_phases.state_build_us
            + chain_phases.delta_replay_us
            + dictionary_open_us;
        let open_phases = OpenPhases {
            checkpoint_read_parse_us: chain_phases.checkpoint_read_parse_us,
            merkle_verify_us: chain_phases.merkle_verify_us,
            chain_format: chain_phases.chain_format,
            state_build_us: chain_phases.state_build_us,
            delta_replay_us: chain_phases.delta_replay_us,
            delta_count: chain_phases.delta_count,
            dictionary_open_us,
            other_us: total_us.saturating_sub(named_us),
            total_us,
        };
        Ok(Self {
            root,
            pin_key,
            dict,
            manifest,
            merkle,
            checkpoint_gen,
            checkpoint_every: None,
            staging: Staging::default(),
            staged_closes: HashMap::new(),
            pending_closes: Vec::new(),
            partitions: PartitionMap::default(),
            segment_target_bytes: crate::defaults::SEGMENT_TARGET_BYTES,
            stats: std::sync::Mutex::new(None),
            edge_postings: std::sync::Mutex::new(Postings::default()),
            node_postings: std::sync::Mutex::new(Postings::default()),
            verified: std::sync::Mutex::new(std::collections::HashSet::new()),
            close_cache: std::sync::Mutex::new(None),
            files_cache: std::sync::Mutex::new(None),
            pending_overlay: None,
            segments: std::sync::Mutex::new(SegmentCache::new(cache_budget(
                std::env::var("TGMS_SEGMENT_CACHE_BYTES").ok().as_deref(),
                detected_ram_bytes(),
            ))),
            batch_tt: None,
            current_only,
            last_commit: None,
            open_phases,
        })
    }

    /// Where the last commit spent its time, if this handle has committed.
    pub fn last_commit_phases(&self) -> Option<CommitPhases> {
        self.last_commit
    }

    /// Where this handle's `open` call spent its time, by phase. Unlike
    /// [`Self::last_commit_phases`] this is never `None`: every live handle
    /// came from a successful `open`.
    pub fn open_phases(&self) -> OpenPhases {
        self.open_phases
    }

    /// Whether this store is the stripped current-only configuration
    /// (plan §13): historical versions discarded, past-belief refused.
    pub fn current_only(&self) -> bool {
        self.current_only
    }

    /// Refuse a past-belief question on a current-only store. Answering it
    /// would silently use only the surviving rows and be wrong; an error is
    /// the honest response. "Current" is anything that clamps to
    /// `OPEN_END - 1`: the Python adapter passes `clamp_tt(as_of_tt)` down,
    /// so both the raw sentinel and its clamped form must count as now.
    pub fn assert_full_belief(&self, as_of_tt: i64) -> Result<()> {
        if self.current_only && crate::clamp_tt(as_of_tt) < crate::OPEN_END - 1 {
            return Err(EngineError::invariant(format!(
                "this store is current-only (plan §13 stripped configuration): \
                 historical versions were discarded, so belief at tt={as_of_tt} \
                 cannot be answered"
            ))
            .with_remedy("rebuild the store from its event log for bi-temporal queries"));
        }
        Ok(())
    }

    /// Stamp the store as current-only. Called by `compact_current_only`
    /// after the stripped generation is published; the marker outlives this
    /// handle so every later open refuses what the store can no longer
    /// answer.
    pub(crate) fn mark_current_only(&mut self) -> Result<()> {
        write_atomic(&self.root.join(CURRENT_ONLY_MARKER), "stripped by compact_current_only\n")?;
        self.current_only = true;
        Ok(())
    }

    /// Resolve `CURRENT` into `(manifest, checkpoint generation)`.
    ///
    /// The `CURRENT` half is unchanged from format 1: one line of
    /// `"<generation> <sha>"`, and the sha must equal the manifest's. What
    /// changed is that the manifest may now be assembled from a checkpoint
    /// plus up to `K−1` deltas rather than read whole — which the sha check
    /// cannot tell apart, because `manifest_sha` is the digest of the logical
    /// document either way.
    #[allow(clippy::type_complexity)]
    fn load_current(
        root: &Path,
    ) -> Result<(
        Manifest,
        u64,
        Option<merkle::ManifestMerkle>,
        manifest_chain::ChainOpenPhases,
    )> {
        let cur_path = root.join(CURRENT);
        let text = fs::read_to_string(&cur_path)
            .map_err(|e| EngineError::from(e).at_file(&cur_path))?;
        let mut parts = text.split_whitespace();
        let (gen, sha) = match (parts.next(), parts.next()) {
            (Some(g), Some(s)) => (g, s),
            _ => {
                return Err(EngineError::corrupt(format!(
                    "CURRENT must contain '<generation> <sha>', found {text:?}"
                ))
                .at_file(&cur_path))
            }
        };
        let generation: u64 = gen.parse().map_err(|_| {
            EngineError::corrupt(format!("CURRENT generation is not a number: {gen:?}"))
                .at_file(&cur_path)
        })?;

        let m_path = Self::manifest_path(root, generation);
        if !m_path.exists() {
            return Err(EngineError::corrupt(format!(
                "CURRENT points at generation {generation} but its manifest is missing"
            ))
            .at_file(&m_path));
        }
        let resolved = manifest_chain::reconstruct(root, generation)?;
        let manifest = resolved.manifest;
        if manifest.manifest_sha != sha {
            return Err(EngineError::corrupt(format!(
                "CURRENT records sha {sha} but manifest {generation} has {}",
                manifest.manifest_sha
            ))
            .at_file(&m_path));
        }
        Ok((manifest, resolved.checkpoint, resolved.merkle, resolved.phases))
    }

    fn manifest_path(root: &Path, generation: u64) -> PathBuf {
        manifest_chain::manifest_path(root, generation)
    }

    /// Refuse to treat a populated directory as a fresh store just because
    /// `CURRENT` happens to be missing (corruption-sweep finding,
    /// 2026-09-15: deleting `native/CURRENT` used to make `open` silently
    /// materialize an empty store, so every query returned empty results
    /// instead of failing).
    ///
    /// `CURRENT` legitimately does not exist yet in exactly one case: the
    /// bootstrap crash inside a *previous* call to `open`, between the
    /// genesis manifest's write (step 4 of the commit protocol, applied to
    /// generation 0) and the `CURRENT` flip that was supposed to follow it
    /// (step 5). There is no prior generation for that crash to have left
    /// intact — the commit protocol's guarantee only starts to apply once a
    /// first generation exists — so an orphaned, unpublished generation-0
    /// checkpoint with nothing else on disk is legitimately "nothing
    /// published yet" rather than corruption. `open` heals it by falling
    /// through to the normal genesis-publish path, which rewrites the
    /// (deterministic) genesis document and this time completes the flip.
    ///
    /// Anything else without `CURRENT` — any other manifest file, any
    /// segment, or a non-empty dictionary log — means a store was populated
    /// at some point and `CURRENT` is now gone: refuse instead of guessing.
    fn refuse_if_populated_without_current(root: &Path) -> Result<()> {
        let manifests_dir = root.join("manifests");
        let mut manifest_gens: Vec<u64> = Vec::new();
        if manifests_dir.is_dir() {
            for entry in fs::read_dir(&manifests_dir)
                .map_err(|e| EngineError::from(e).at_file(&manifests_dir))?
            {
                let entry = entry.map_err(|e| EngineError::from(e).at_file(&manifests_dir))?;
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) != Some("json") {
                    continue; // ignore `.tmp` leftovers from an interrupted write_atomic
                }
                if let Some(gen) = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .and_then(|s| s.parse::<u64>().ok())
                {
                    manifest_gens.push(gen);
                }
            }
        }
        manifest_gens.sort_unstable();

        let seg_dir = root.join("seg");
        let n_segments = if seg_dir.is_dir() {
            fs::read_dir(&seg_dir)
                .map_err(|e| EngineError::from(e).at_file(&seg_dir))?
                .filter_map(|e| e.ok())
                .filter(|e| e.path().extension().and_then(|x| x.to_str()) == Some("tgs"))
                .count()
        } else {
            0
        };

        let dict_bytes = fs::metadata(root.join(DICT)).map(|m| m.len()).unwrap_or(0);

        if manifest_gens.is_empty() && n_segments == 0 && dict_bytes == 0 {
            return Ok(()); // a genuinely new, empty directory
        }

        // the bootstrap-crash exception: exactly the orphaned generation-0
        // checkpoint, nothing else, and its own event log starts at cursor 0
        if manifest_gens == [0] && n_segments == 0 && dict_bytes == 0 {
            if let Ok(manifest_chain::ManifestRecord::Checkpoint(m)) =
                manifest_chain::read_record(root, 0)
            {
                if m.generation == 0 && m.parent.is_none() && m.event_log.offset == 0 {
                    return Ok(());
                }
            }
        }

        Err(EngineError::corrupt(format!(
            "store has {} manifest(s) / {} segment(s) but no CURRENT — \
             refusing to treat a populated directory as empty; if this is a \
             crashed first commit, remove the orphans or restore CURRENT",
            manifest_gens.len(),
            n_segments
        ))
        .with_remedy(
            "remove the orphaned manifests/segments if this was a crashed \
             first commit, or restore CURRENT from a backup",
        ))
    }

    /// Steps 4 and 5: write this generation's manifest record, then flip
    /// `CURRENT`.
    ///
    /// `appended` is the span the commit path appended over, plus the
    /// checkpoint the chain currently rests on; `None` means "nothing to cut
    /// a delta from", which forces a checkpoint — which is what every caller
    /// but `commit` wants anyway, since each of them replaces the whole
    /// segment list or has no parent at all. `force_checkpoint` is the memo's
    /// rules 2 and 3 — compaction and gc's retention floor.
    ///
    /// **The manifest is not re-verified here.** It used to be, on its first
    /// line: a third deep clone plus a second serialize-and-sha256 of bytes
    /// the caller's own `seal` had produced microseconds earlier on the same
    /// thread, with nothing in between (V2 diagnosis §2, item 3 — "pure
    /// redundancy"; Addendum 3 ruling 4). The format check it also made is
    /// kept, because that one is not redundant. A `debug_assert` keeps the
    /// digest check in test builds, where it is a real net over the
    /// incremental seal.
    ///
    /// Returns `(manifest_us, current_us, manifest_bytes, checkpoint_gen,
    /// delta_build_us, debug_verify_us)`. The timing split matters because
    /// step 4 used to rewrite the whole manifest every generation while step
    /// 5 writes forty bytes; the point of this change is that step 4 no
    /// longer grows with store history either, so the split is what shows
    /// it. `delta_build_us` is split out separately (B1-v2d) because it used
    /// to run entirely before `manifest_us`'s timer started — the
    /// delta-or-checkpoint JSON this builds is what the B1-v2 A/B diagnosis
    /// (2026-09-15, §1.5) flagged as needing its own timer, isolated from
    /// `digest_us`. `debug_verify_us` times the `debug_assert_eq!` below,
    /// which is itself O(segments) and otherwise ran untimed ahead of
    /// `manifest_us`'s clock start — always 0 in release, where the assert
    /// compiles away.
    fn publish(
        root: &Path,
        appended: Option<(&AppendSpan, u64)>,
        manifest: &Manifest,
        force_checkpoint: bool,
        every: u64,
    ) -> Result<(u64, u64, u64, u64, u64, u64)> {
        #[cfg(debug_assertions)]
        let debug_verify_us = {
            let t = std::time::Instant::now();
            debug_assert_eq!(
                manifest.manifest_sha,
                manifest.body_sha_canonical(),
                "publish was handed a manifest its caller had not sealed"
            );
            t.elapsed().as_micros() as u64
        };
        #[cfg(not(debug_assertions))]
        let debug_verify_us = 0u64;
        if !manifest_chain::is_writable_format(manifest.format) {
            return Err(manifest_chain::read_only_format_error(manifest.format));
        }
        let generation = manifest.generation;
        let t = std::time::Instant::now();
        let delta = if force_checkpoint || generation.is_multiple_of(every) {
            None
        } else {
            appended.and_then(|(span, ckpt)| ManifestDelta::from_appends(manifest, span, ckpt))
        };
        let (json, checkpoint_gen) = match &delta {
            Some(d) => (d.to_json(), appended.expect("a delta needs a parent").1),
            None => (manifest_chain::checkpoint_json(manifest), generation),
        };
        let delta_build_us = t.elapsed().as_micros() as u64;

        let m_path = Self::manifest_path(root, generation);
        let t = std::time::Instant::now();
        write_atomic(&m_path, &json)?;
        let manifest_us = t.elapsed().as_micros() as u64;
        crash_point("after_manifest");
        let t = std::time::Instant::now();
        write_atomic(
            &root.join(CURRENT),
            &format!("{} {}\n", generation, manifest.manifest_sha),
        )?;
        crash_point("after_current");
        Ok((
            manifest_us,
            t.elapsed().as_micros() as u64,
            json.len() as u64,
            checkpoint_gen,
            delta_build_us,
            debug_verify_us,
        ))
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub(crate) fn pin_key(&self) -> &Path {
        &self.pin_key
    }

    /// Adopt a freshly published manifest as this handle's view, moving its
    /// reader pin with it. Every path that advances `self.manifest` after
    /// open must come through here or gc could collect the old view early —
    /// or keep protecting a generation nobody holds.
    /// Adopt a manifest that was *built* rather than appended to — compaction
    /// and the format upgrade. Both replace the whole segment list, so the
    /// Merkle state is rebuilt wholesale; both already force a checkpoint, so
    /// the O(n) is one they were paying anyway.
    fn adopt(&mut self, next: Manifest) {
        self.repin(self.manifest.generation, next.generation);
        self.merkle = manifest_chain::is_writable_format(next.format)
            .then(|| merkle::ManifestMerkle::from_manifest(&next));
        self.manifest = next;
    }

    /// Move this handle's reader pin. Taken explicitly rather than read off
    /// `self.manifest`, because the commit path advances the manifest it
    /// already holds — by the time it repins, `self.manifest.generation` is
    /// the destination, and a `repin(new, new)` would silently strand the old
    /// pin and stop gc from ever collecting that generation.
    fn repin(&self, from: u64, to: u64) {
        crate::gc::repin(&self.pin_key, from, to);
    }

    /// Drop cached segments gc just removed from disk. Sound because ids are
    /// never reused (`Manifest::next_segment_id`), so an evicted name can
    /// never come back meaning different bytes.
    ///
    /// Also bounds the identity postings (D-087): `keep` is translated to
    /// segment ids and handed to `Postings::retain_segments`, which is the
    /// only thing standing between periodic compaction and an unbounded
    /// `by_identity`/`by_vid` — see that method's own doc for why.
    pub(crate) fn evict_segments_not_in(&self, keep: &std::collections::HashSet<String>) {
        self.segments
            .lock()
            .expect("segment-cache mutex poisoned")
            .retain_files(keep);
        self.verified
            .lock()
            .expect("verified-set mutex poisoned")
            .retain(|file| keep.contains(file));
        let keep_ids: std::collections::HashSet<u64> =
            keep.iter().map(|f| segment_id_of(f)).collect();
        self.edge_postings
            .lock()
            .expect("edge-postings mutex poisoned")
            .retain_segments(&keep_ids);
        self.node_postings
            .lock()
            .expect("node-postings mutex poisoned")
            .retain_segments(&keep_ids);
    }

    /// Override the segment-cache byte budget for this handle (`None` =
    /// unbounded). Exists so tests and harnesses can exercise eviction
    /// without mutating the process environment, which races other threads.
    pub fn set_segment_cache_budget(&self, budget: Option<u64>) {
        self.segments
            .lock()
            .expect("segment-cache mutex poisoned")
            .set_budget(budget);
    }

    /// `(entries, accounted_bytes, budget, evictions)` — observability for
    /// the byte-budget cache, so "the cache stayed under budget" is a
    /// measurement rather than an assumption.
    pub fn segment_cache_stats(&self) -> (usize, u64, Option<u64>, u64) {
        self.segments
            .lock()
            .expect("segment-cache mutex poisoned")
            .stats()
    }

    /// `(by_identity_entries, by_vid_entries, indexed_segments)` for one row
    /// kind's identity postings — observability for D-087's bound, the same
    /// role `segment_cache_stats` plays for the segment cache. Exists so a
    /// test (or an operator) can measure "postings stayed bounded across
    /// compaction cycles" directly, rather than inferring it from process
    /// RSS, which conflates the postings leak with compaction's own
    /// legitimate O(current rows) working set.
    pub fn postings_stats(&self, kind: RowKind) -> (usize, usize, usize) {
        let p = match kind {
            RowKind::Edge => &self.edge_postings,
            RowKind::Node => &self.node_postings,
        };
        let p = p.lock().expect("postings mutex poisoned");
        (
            p.by_identity.values().map(Vec::len).sum(),
            p.by_vid.values().map(Vec::len).sum(),
            p.indexed.len(),
        )
    }

    pub fn generation(&self) -> u64 {
        self.manifest.generation
    }

    pub fn manifest(&self) -> &Manifest {
        &self.manifest
    }

    pub fn dict(&self) -> &Dictionary {
        &self.dict
    }

    pub(crate) fn stats_cell(&self) -> &std::sync::Mutex<Option<StatsAccum>> {
        &self.stats
    }

    /// Rows this open batch has closed, as physical addresses (D-076).
    ///
    /// The open-version index is built per committed generation and does not
    /// know about an in-flight batch, so a mid-batch read subtracts these.
    /// Bounded by the batch, not the store.
    pub(crate) fn pending_closed_rows(&self, kind: RowKind) -> std::collections::HashSet<(u64, u32)> {
        self.pending_closes
            .iter()
            .filter(|c| c.kind == kind)
            .map(|c| (c.segment, c.row))
            .collect()
    }

    pub(crate) fn edge_postings(&self) -> &std::sync::Mutex<Postings> {
        &self.edge_postings
    }

    pub(crate) fn node_postings(&self) -> &std::sync::Mutex<Postings> {
        &self.node_postings
    }

    /// Layout policy in force, for callers that re-seal existing data.
    pub(crate) fn layout(&self) -> (&PartitionMap, u64) {
        (&self.partitions, self.segment_target_bytes)
    }

    /// Publish an already-built manifest as the next generation. Used by
    /// compaction, which rewrites content without going through a batch.
    ///
    /// Always a checkpoint (memo §4, checkpoint rule 2): the only caller
    /// replaces the entire segment list, so the delta would be no smaller
    /// than the full document — and the chain conveniently resets at exactly
    /// the point the store shrinks.
    pub(crate) fn install(&mut self, next: Manifest) -> Result<u64> {
        if self.in_batch() {
            return Err(EngineError::invariant(
                "cannot publish a generation while a batch is open",
            ));
        }
        self.require_writable_format()?;
        let (_, _, _, ckpt, _, _) = Self::publish(
            &self.root,
            None,
            &next,
            true,
            self.checkpoint_every(),
        )?;
        self.checkpoint_gen = ckpt;
        self.adopt(next);
        Ok(self.manifest.generation)
    }

    /// Rewrite one generation's record as a full checkpoint, unless it is one
    /// already. Returns whether a file was written.
    ///
    /// The rewrite is content-preserving by construction — the checkpoint is
    /// serialized from the reconstruction of the very record it replaces — so
    /// `manifest_sha` is unchanged and `CURRENT` keeps pointing at the same
    /// digest whether or not this generation is the live one. That is what
    /// lets gc cut a chain below its retention floor without a publication
    /// step (`gc.rs`, pass 0).
    pub(crate) fn materialize_checkpoint(&self, generation: u64) -> Result<bool> {
        if manifest_chain::read_record(&self.root, generation)?.is_checkpoint() {
            return Ok(false);
        }
        self.require_writable_format()?;
        let resolved = manifest_chain::reconstruct(&self.root, generation)?;
        write_atomic(
            &Self::manifest_path(&self.root, generation),
            &manifest_chain::checkpoint_json(&resolved.manifest),
        )?;
        Ok(true)
    }

    /// The on-disk manifest format this store was opened at: 2 for a store
    /// this build wrote, 1 for one the previous engine wrote.
    pub fn manifest_format(&self) -> u32 {
        self.manifest.format
    }

    /// Generation of the checkpoint the current chain rests on. Equal to
    /// `generation()` right after a checkpoint; at most `K−1` below it
    /// otherwise.
    pub fn checkpoint_generation(&self) -> u64 {
        self.checkpoint_gen
    }

    /// Override the checkpoint interval for this handle (`None` = the
    /// process default). The A/B sweeps K; tests use it to reach a periodic
    /// checkpoint in a handful of commits.
    pub fn set_checkpoint_every(&mut self, every: Option<u64>) {
        self.checkpoint_every = every.filter(|k| *k > 0);
    }

    /// The checkpoint interval in force for this handle.
    pub fn checkpoint_every(&self) -> u64 {
        self.checkpoint_every
            .unwrap_or_else(manifest_chain::checkpoint_every)
    }

    /// Refuse a write against a store this build cannot write (memo §4,
    /// "Migration"): format-1 and format-2 stores open read-only and are
    /// converted by an explicit command, never silently reinterpreted.
    pub(crate) fn require_writable_format(&self) -> Result<()> {
        if manifest_chain::is_writable_format(self.manifest.format) {
            return Ok(());
        }
        Err(manifest_chain::read_only_format_error(self.manifest.format))
    }

    /// Convert an older store in place: publish the current generation's
    /// content again as a format-3 checkpoint (memo §4, "Migration").
    ///
    /// Format 1 or format 2, the shape is the same. From format 2 the
    /// document barely changes at all — a `sha_kind` tag and a bumped
    /// `format` — and yet `manifest_sha` changes completely, because format 3
    /// changes what that digest *is* (a Merkle root over the ordered segment
    /// set rather than the sha of the whole document). That is exactly why
    /// this is a migration and not a silent reinterpretation.
    ///
    /// **Deviation from the memo, and why.** §4 says "write generation `G` as
    /// a checkpoint … flip `CURRENT`". `manifest_sha` covers the
    /// `format` field, so the converted document hashes differently from the
    /// one `CURRENT` currently names — and rewriting `manifests/<G>.json` in
    /// place would leave a window in which `CURRENT` names a sha no file on
    /// disk has. A crash there is an unopenable store, which is invariant 1
    /// (single publication point; everything before `CURRENT` is invisible on
    /// crash) lost. So the checkpoint is published as generation `G+1` with
    /// identical logical content: the ordinary shape, where a crash before the
    /// flip leaves `G` intact and `G+1` orphaned. One file written, nothing
    /// else touched, and the generation counter advances by one — which the
    /// TCSR stamp already treats as a rebuild trigger, as §4 anticipates.
    ///
    /// Idempotent: a store already at format 2 is returned unchanged.
    pub fn upgrade_manifests(&mut self) -> Result<UpgradeReport> {
        if self.in_batch() {
            return Err(EngineError::invariant(
                "cannot upgrade manifests while a batch is open",
            ));
        }
        if manifest_chain::is_writable_format(self.manifest.format) {
            return Ok(UpgradeReport {
                upgraded: false,
                from_format: self.manifest.format,
                generation: self.manifest.generation,
                manifest_sha: self.manifest.manifest_sha.clone(),
            });
        }
        if !crate::MANIFEST_FORMATS_READ_ONLY.contains(&self.manifest.format) {
            return Err(manifest_chain::read_only_format_error(self.manifest.format));
        }
        let from_format = self.manifest.format;
        let mut next = self.manifest.successor(self.manifest.created_tt);
        next.format = crate::MANIFEST_FORMAT_VERSION;
        next.seal();
        let (_, _, _, ckpt, _, _) = Self::publish(
            &self.root,
            None,
            &next,
            true,
            self.checkpoint_every(),
        )?;
        self.checkpoint_gen = ckpt;
        self.adopt(next);
        Ok(UpgradeReport {
            upgraded: true,
            from_format,
            generation: self.manifest.generation,
            manifest_sha: self.manifest.manifest_sha.clone(),
        })
    }

    /// Open a segment, verifying its checksums the first time this session
    /// touches it. Every read path must come through here — opening a segment
    /// directly would skip the check and could serve corrupt rows.
    ///
    /// A budget-evicted segment lands on the slow path again and simply
    /// reopens: `verified` still names it, so the checksum walk stays
    /// once-per-session and only the decode is repaid. Callers holding an
    /// `Arc` from before an eviction keep a valid segment either way —
    /// eviction drops the cache's reference, never the data under a reader.
    pub fn open_segment(&self, file: &str) -> Result<std::sync::Arc<crate::segment::Segment<MmapSource>>> {
        if let Some(seg) = self
            .segments
            .lock()
            .expect("segment-cache mutex poisoned")
            .get(file)
        {
            return Ok(seg);
        }
        let path = self.root.join(file);
        let first_time = {
            let seen = self.verified.lock().expect("verified-set mutex poisoned");
            !seen.contains(file)
        };
        // mapped, not read: a point lookup touches a few pages, and reading
        // the whole file per open made `believed_*` cost a full scan even
        // when it wanted one row
        let seg = std::sync::Arc::new(crate::segment::Segment::open(
            &path,
            MmapSource::load(&path)?,
            first_time,
        )?);
        if first_time {
            self.verified
                .lock()
                .expect("verified-set mutex poisoned")
                .insert(file.to_string());
        }
        self.segments
            .lock()
            .expect("segment-cache mutex poisoned")
            .insert(file.to_string(), seg.clone());
        Ok(seg)
    }

    /// Open a segment for a one-pass streaming walk, without adding it to
    /// the session's segment cache.
    ///
    /// `open_segment` is right for point reads and scans that revisit
    /// segments across calls: caching the decoded columns pays for itself.
    /// A full-store fold that touches every segment exactly once is the
    /// opposite case — routing it through `open_segment` would leave every
    /// segment's decoded columns resident (up to the cache's byte budget,
    /// which defaults to half of physical RAM, i.e. effectively unbounded
    /// for this purpose) for the rest of the session, so peak RSS grows with
    /// the number of segments touched rather than staying at one segment's
    /// worth. This opens the file directly and returns an owned `Segment`
    /// the caller drops after folding it — the same one-at-a-time discipline
    /// `integrity.rs`'s row scan uses, and for the same reason.
    ///
    /// Checksums are still verified the first time this session touches a
    /// file (tracked in `verified`, shared with `open_segment` so the two
    /// paths never re-verify each other's work) — this only skips caching
    /// the *decoded* segment, not the corruption check.
    pub(crate) fn open_segment_uncached(&self, file: &str) -> Result<crate::segment::Segment<MmapSource>> {
        let path = self.root.join(file);
        let first_time = {
            let seen = self.verified.lock().expect("verified-set mutex poisoned");
            !seen.contains(file)
        };
        let seg = crate::segment::Segment::open(&path, MmapSource::load(&path)?, first_time)?;
        if first_time {
            self.verified
                .lock()
                .expect("verified-set mutex poisoned")
                .insert(file.to_string());
        }
        Ok(seg)
    }

    /// Walk every file this generation references, checking magic numbers,
    /// checksums, completion markers, and cross-references.
    ///
    /// Corruption must be *detected*, not merely survived: the durability
    /// objective is "never expose an undetected inconsistent generation"
    /// (blueprint §1). Problems are collected rather than raised on the first
    /// one, so an operator sees the whole picture instead of peeling the
    /// onion a file at a time.
    ///
    /// This is the *fast* mode of `tgms store verify`. It answers "is every
    /// file this generation names intact, and does it hold what the manifest
    /// claims" — bounded by the bytes on disk, and nothing more. What it does
    /// not do is look inside a segment at the rows, walk the retained
    /// manifests it did not have to read, or check any invariant that spans
    /// two files; that is [`verify_full`](Self::verify_full).
    pub fn verify(&self) -> Result<VerifyReport> {
        self.verify_fast()
    }

    /// Fast mode. See [`verify`](Self::verify).
    pub fn verify_fast(&self) -> Result<VerifyReport> {
        Ok(self.verify_files()?.0)
    }

    /// The file-level walk both modes start from. Full mode wants the close
    /// records this pass already read, so it hands them back rather than
    /// making the caller read every run file a second time.
    fn verify_files(&self) -> Result<(VerifyReport, Vec<CloseRecord>)> {
        let generation = self.manifest.generation;
        let mut closes: Vec<CloseRecord> = Vec::new();
        let mut report = VerifyReport {
            generation,
            manifest_format: self.manifest.format,
            mode: "fast".into(),
            ..Default::default()
        };
        self.manifest.verify()?;

        // Invariant 4: this generation must be checkable *from disk alone*.
        // With format 2 that is no longer a single document read — it is the
        // checkpoint plus the delta chain above it, each record self-sha'd,
        // each link's `parent_sha` matched, and the reconstruction re-hashed
        // against what each delta claims. Doing it here rather than trusting
        // the in-memory manifest is the whole point: a handle that has been
        // open across a gc must still be able to prove its own view.
        match manifest_chain::reconstruct(&self.root, self.manifest.generation) {
            Ok(resolved) => {
                report.manifest_deltas = resolved.deltas;
                report.manifest_checkpoint = resolved.checkpoint;
                if resolved.manifest != self.manifest {
                    report.flag(Finding::error(
                        integrity::LAYER_MANIFEST,
                        "reconstruction-mismatch",
                        manifest_chain::manifest_path(Path::new(""), self.manifest.generation)
                            .to_string_lossy()
                            .into_owned(),
                        self.manifest.generation,
                        format!(
                            "generation {} reconstructs from disk to sha {} but this handle holds {}",
                            self.manifest.generation,
                            resolved.manifest.manifest_sha,
                            self.manifest.manifest_sha
                        ),
                    ));
                }
            }
            Err(e) => report.flag(Finding::error(
                integrity::LAYER_MANIFEST,
                "chain-broken",
                "",
                self.manifest.generation,
                format!("manifest chain: {}", e.message),
            )),
        }

        let m = &self.manifest;
        let segments: Vec<(&str, u32)> = m
            .edge_lanes
            .event
            .iter()
            .chain(m.edge_lanes.interval.iter())
            .chain(m.node_store.iter())
            .map(|e| (e.file.as_str(), e.rows))
            .collect();

        for (file, claimed_rows) in segments {
            let path = self.root.join(file);
            match crate::segment::MemorySource::load(&path)
                .and_then(|src| crate::segment::Segment::open(&path, src, true))
            {
                Ok(seg) => {
                    report.segments_checked += 1;
                    report.rows += seg.rows() as u64;
                    // Layout quality, not integrity: a batch-written segment
                    // holds exactly one tt_s run, and compaction's global
                    // re-sort can leave nearly one per row. Nothing about
                    // that is corrupt, so it is not a problem — but it is
                    // what the read path pays per materialized row, so it
                    // belongs somewhere a regression can be seen.
                    let runs = seg.header().tt_s_runs.len() as u32;
                    report.max_tt_s_runs = report.max_tt_s_runs.max(runs);
                    report.tt_s_runs += runs as u64;
                    if seg.rows() != claimed_rows {
                        report.flag(Finding::error(
                            integrity::LAYER_SEGMENT,
                            "row-count-mismatch",
                            file,
                            generation,
                            format!(
                                "{file}: manifest claims {claimed_rows} rows, segment holds {}",
                                seg.rows()
                            ),
                        ));
                    }
                }
                Err(e) => report.flag(Finding::error(
                    integrity::LAYER_SEGMENT,
                    if path.exists() {
                        "segment-unreadable"
                    } else {
                        "segment-missing"
                    },
                    file,
                    generation,
                    format!("{file}: {e}"),
                )),
            }
        }

        for run in &m.close_runs {
            let path = self.root.join(&run.file);
            match crate::visibility::read_close_run(&path) {
                Ok(records) => {
                    report.close_runs_checked += 1;
                    report.closes += records.len() as u64;
                    if records.len() as u32 != run.entries {
                        report.flag(Finding::error(
                            integrity::LAYER_CLOSE,
                            "entry-count-mismatch",
                            run.file.clone(),
                            generation,
                            format!(
                                "{}: manifest claims {} entries, file holds {}",
                                run.file,
                                run.entries,
                                records.len()
                            ),
                        ));
                    }
                    closes.extend(records);
                }
                Err(e) => report.flag(Finding::error(
                    integrity::LAYER_CLOSE,
                    if path.exists() {
                        "close-run-unreadable"
                    } else {
                        "close-run-missing"
                    },
                    run.file.clone(),
                    generation,
                    format!("{}: {e}", run.file),
                )),
            }
        }

        report.dict_records = self.dict.len();
        if self.dict.len() != m.dict.records {
            report.flag(Finding::error(
                integrity::LAYER_DICT,
                "record-count-mismatch",
                DICT,
                generation,
                format!(
                    "dictionary holds {} records, manifest claims {}",
                    self.dict.len(),
                    m.dict.records
                ),
            ));
        }
        Ok((report, closes))
    }

    /// Full mode: everything `verify_fast` checks, plus the invariants that
    /// span files or live inside a segment's rows.
    ///
    /// Three checks the fast pass structurally cannot make:
    ///
    /// * the **manifest parent chain** across every retained generation, not
    ///   just the links a reconstruction of `CURRENT` happens to walk;
    /// * **dictionary-code reference validity** - every code a row names must
    ///   exist in the dictionary prefix the manifest commits;
    /// * the **bitemporal row invariants**, including I1 disjointness of
    ///   believed versions per identity, checked against the bytes on disk
    ///   rather than against the write path that is supposed to maintain it.
    ///
    /// Read-only, like everything else here. A defect is a finding; nothing
    /// is trimmed, recovered or rewritten. This is the mode the corruption
    /// sweep uses as its oracle, so the answer has to describe the store as
    /// it found it.
    pub fn verify_full(&self) -> Result<VerifyReport> {
        let (mut report, closes) = self.verify_files()?;
        report.mode = "full".into();

        for f in self.digest_oracle() {
            report.flag(f);
        }
        for f in integrity::parent_chain(&self.root, self.manifest.generation) {
            report.flag(f);
        }
        for f in integrity::check_close_targets(&self.manifest, &closes) {
            report.flag(f);
        }

        let index = CloseIndex::from_records(closes);
        let (scan, findings) =
            integrity::check_rows(&self.root, &self.manifest, &self.dict, &index)?;
        report.rows_walked = scan.rows_walked;
        report.believed_rows = scan.believed_rows;
        report.identities_checked = scan.identities_checked;
        for f in findings {
            report.flag(f);
        }
        Ok(report)
    }

    /// The price of an incremental digest, paid in the mode nobody runs per
    /// commit (memo §4(d)).
    ///
    /// From format 3 `manifest_sha` is maintained by appending to a spine, so
    /// a bug in that update would be *self-consistent*: the store would seal
    /// with a wrong digest, publish it, and check it against itself forever.
    /// The defence is a recomputation that shares no code with the
    /// incremental path — [`Manifest::body_sha_canonical`], the whole tree
    /// rebuilt from the whole ordered segment set — compared against the
    /// value on disk in `CURRENT`, which is what was actually published
    /// rather than what this handle currently believes.
    ///
    /// `verify_files` already re-derives the handle's own digest from scratch
    /// and refuses outright if it disagrees; what this adds is the crossing
    /// to the published bytes. Read-only, like everything else in verify.
    fn digest_oracle(&self) -> Vec<Finding> {
        let path = self.root.join(CURRENT);
        let rel = CURRENT.to_string();
        let generation = self.manifest.generation;
        let text = match fs::read_to_string(&path) {
            Ok(t) => t,
            Err(e) => {
                return vec![Finding::error(
                    integrity::LAYER_MANIFEST,
                    "current-unreadable",
                    rel,
                    generation,
                    format!("CURRENT could not be read: {e}"),
                )]
            }
        };
        let mut parts = text.split_whitespace();
        let (Some(gen), Some(sha)) = (parts.next(), parts.next()) else {
            return vec![Finding::error(
                integrity::LAYER_MANIFEST,
                "current-malformed",
                rel,
                generation,
                format!("CURRENT must contain '<generation> <sha>', found {text:?}"),
            )];
        };
        let mut findings = Vec::new();
        // A handle that has committed since it opened is *ahead* of nothing —
        // it publishes CURRENT itself — but one pinned to an older generation
        // legitimately is behind, and that is not a defect.
        if gen.parse::<u64>() != Ok(generation) {
            return findings;
        }
        let oracle = self.manifest.body_sha_canonical();
        if oracle != sha {
            findings.push(Finding::error(
                integrity::LAYER_MANIFEST,
                "digest-oracle-mismatch",
                rel,
                generation,
                format!(
                    "generation {generation} recomputes from scratch to {oracle} but \
                     CURRENT publishes {sha} — the incrementally maintained digest and \
                     an independent recomputation of it disagree"
                ),
            ));
        }
        findings
    }

    /// Closes every read in this handle must honour: the committed ones, plus
    /// the open batch's, which are not durable yet but are this writer's own.
    ///
    /// Read-your-own-writes is not a convenience here. `apply_ops` asks what
    /// is believed *between* its ops to decide what to carve, so a batch that
    /// could not see its own corrections would carve against a version it had
    /// already stopped believing and write a fragment over the top of it
    /// (D-058's divergence: DuckDB's uncommitted `UPDATE` was visible to the
    /// same connection, and this was not). Staged rows and closes against
    /// them were already visible; closes against *committed* rows — the only
    /// kind an ordinary correction makes — were the hole.
    ///
    /// Nothing leaks the other way: `pending_closes` belongs to the writing
    /// handle, and a second handle cannot have an open batch (single-writer).
    pub fn close_index(&self) -> Result<std::sync::Arc<CloseIndex>> {
        match &self.pending_overlay {
            Some(idx) => Ok(idx.clone()),
            None => self.committed_close_index(),
        }
    }

    /// Closes this generation makes visible, resolved for scanning. A reader
    /// on an older generation loads that generation's runs and therefore sees
    /// that generation's beliefs — never a newer correction: each handle
    /// caches against its *own* manifest generation, so a pinned older view
    /// serves its own generation's closes, not a newer handle's.
    /// `segment id -> filename` for both lanes, for this generation (D-077).
    ///
    /// Returned behind an `Arc` so callers share one map instead of each
    /// building its own; the caller looks names up rather than owning them.
    pub(crate) fn segment_files(&self) -> std::sync::Arc<SegmentFiles> {
        let generation = self.manifest.generation;
        if let Some((cached_gen, files)) = self
            .files_cache
            .lock()
            .expect("files-cache mutex poisoned")
            .as_ref()
        {
            if *cached_gen == generation {
                return files.clone();
            }
        }
        let m = &self.manifest;
        let files = std::sync::Arc::new(SegmentFiles {
            edge: m
                .edge_lanes
                .event
                .iter()
                .chain(m.edge_lanes.interval.iter())
                .map(|e| (segment_id_of(&e.file), e.file.clone()))
                .collect(),
            node: m
                .node_store
                .iter()
                .map(|e| (segment_id_of(&e.file), e.file.clone()))
                .collect(),
        });
        *self.files_cache.lock().expect("files-cache mutex poisoned") =
            Some((generation, files.clone()));
        files
    }

    pub(crate) fn committed_close_index(&self) -> Result<std::sync::Arc<CloseIndex>> {
        if self.current_only {
            // the stripped configuration has no closes by construction; a
            // run appearing anyway means the marker and the manifest
            // disagree, which must surface rather than be half-honoured
            if !self.manifest.close_runs.is_empty() {
                return Err(EngineError::invariant(
                    "current-only store has close runs — the CURRENT_ONLY \
                     marker does not match the manifest",
                ));
            }
            return Ok(std::sync::Arc::new(CloseIndex::default()));
        }
        let generation = self.manifest.generation;
        let runs = &self.manifest.close_runs;

        // Reuse the cached index when this generation only *appended* runs to
        // the one it was built from (D-079). Close-run files are immutable and
        // the manifest normally appends, so the cached index is a valid prefix
        // of the answer and only the new runs need reading — rather than all
        // of them, which was 37 ms at 999 runs and quadratic over a run of
        // corrections (D-078).
        //
        // The prefix check is deliberately strict, because a wrong answer here
        // is a silently wrong belief: `close_runs` is **not** append-only
        // across compaction, which folds runs into sidecars and empties the
        // list. Length, plus the file name *and* its sha at the last folded
        // index, must all still match; anything else falls back to a rebuild.
        // The entry is *taken*, not cloned, and the guard dropped before
        // anything else runs — both halves matter (D-081). The mutex is not
        // reentrant, and an earlier draft held the guard across the write
        // below, which deadlocked the whole suite. And a draft that *cloned*
        // the entry left the cache holding a second Arc, so `make_mut` below
        // cloned the whole map on every extend — O(all closes) memcpy per
        // commit, quietly contradicting the "in place" design. Taking the
        // entry makes this handle's copy the only one (unless a reader holds
        // a pinned Arc, which is exactly when a copy is correct). Every
        // return path below restores the cache. A concurrent reader that
        // finds the cache empty mid-flight rebuilds redundantly, which is
        // wasted work, never a wrong answer.
        let cached: Option<CloseCacheEntry> = self
            .close_cache
            .lock()
            .expect("close-cache mutex poisoned")
            .take();

        let mut reuse: Option<(std::sync::Arc<CloseIndex>, usize)> = None;
        if let Some((cached_gen, folded, last, idx)) = cached {
            if cached_gen == generation {
                let out = idx.clone();
                *self.close_cache.lock().expect("close-cache mutex poisoned") =
                    Some((cached_gen, folded, last, idx));
                return Ok(out);
            }
            let extends = runs.len() >= folded
                && match (folded, &last) {
                    (0, _) => true,
                    (n, Some((file, sha))) => runs
                        .get(n - 1)
                        .is_some_and(|r| &r.file == file && &r.sha == sha),
                    _ => false,
                };
            if extends {
                if folded == runs.len() {
                    // same run set, newer generation (a commit that closed
                    // nothing): the index is already the answer
                    *self.close_cache.lock().expect("close-cache mutex poisoned") =
                        Some((generation, folded, last, idx.clone()));
                    return Ok(idx);
                }
                reuse = Some((idx, folded));
            }
        }

        let (mut idx, from) = match reuse {
            Some((cached, folded)) => (cached, folded),
            None => (std::sync::Arc::new(CloseIndex::default()), 0),
        };
        {
            // copy-on-write, and — after D-081 — genuinely in place in the
            // common case: the cache's Arc was taken above, so this handle
            // holds the only reference unless a reader is pinned to this very
            // index, and that pin is precisely when `make_mut`'s clone is
            // correct rather than waste.
            let target = std::sync::Arc::make_mut(&mut idx);
            for r in &runs[from..] {
                for rec in read_close_run(&self.root.join(&r.file))? {
                    target.close_row(rec.segment, rec.row, rec.tt_e);
                }
            }
        }
        let last = runs.last().map(|r| (r.file.clone(), r.sha.clone()));
        *self.close_cache.lock().expect("close-cache mutex poisoned") =
            Some((generation, runs.len(), last, idx.clone()));
        Ok(idx)
    }

    pub fn in_batch(&self) -> bool {
        self.batch_tt.is_some()
    }

    pub fn begin(&mut self, tt: i64) -> Result<()> {
        if let Some(open_tt) = self.batch_tt {
            return Err(EngineError::invariant(format!(
                "a batch at tt={open_tt} is already open (single-writer, no nesting)"
            )));
        }
        if tt <= self.manifest.created_tt && self.manifest.generation > 0 {
            return Err(EngineError::invariant(format!(
                "transaction time must advance: batch tt={tt} is not after \
                 generation {}'s tt={}",
                self.manifest.generation, self.manifest.created_tt
            )));
        }
        self.batch_tt = Some(tt);
        Ok(())
    }

    /// Register an entity, returning its dense id.
    ///
    /// Deliberately does not require an open batch: `apply_ops` registers
    /// entities before it knows which rows it will write, and a staged
    /// dictionary entry is discarded on rollback and published on commit
    /// either way. Visible to reads immediately; durable only at commit.
    pub fn ensure_entity(&mut self, uid: &str, label: &str) -> Result<u32> {
        self.dict.ensure(uid, label)
    }

    /// Layout policy. Changing it affects only future segments — lane
    /// assignment is physical, so already-written rows are unaffected.
    pub fn set_layout(&mut self, partitions: PartitionMap, segment_target_bytes: u64) {
        self.partitions = partitions;
        self.segment_target_bytes = segment_target_bytes;
    }

    pub fn stage_edge(&mut self, row: EdgeRow) -> Result<()> {
        self.require_batch()?;
        self.staging.push_edge(row);
        Ok(())
    }

    pub fn stage_node(&mut self, row: NodeRow) -> Result<()> {
        self.require_batch()?;
        self.staging.push_node(row);
        Ok(())
    }

    /// Stop believing a version from `tt_e` on.
    ///
    /// The target may be a row staged in this very batch (a carve that splits
    /// a version and closes the original) or one committed earlier. Staged
    /// hits fold into the segment's sidecar; committed hits become a close
    /// run. Either way the row itself is never touched — closing is data.
    ///
    /// Locating a committed row goes through the identity postings (WP-N4):
    /// candidates by vid prefix, the full vid verified at the row. Same
    /// answer the linear scan gave, without the per-close segment walk that
    /// made correction-heavy replay superlinear (docs/eval_bitemporal.md
    /// §"close_version's linear scan").
    pub fn close_version(&mut self, kind: RowKind, vid: Id96, tt_e: i64) -> Result<()> {
        if self.current_only {
            return Err(EngineError::invariant(
                "a current-only store cannot record a correction: closing a \
                 version creates the belief history this configuration \
                 deliberately does not keep",
            ));
        }
        self.require_batch()?;
        let staged_hit = match kind {
            RowKind::Edge => self.staging.has_edge_vid(vid),
            RowKind::Node => self.staging.has_node_vid(vid),
        };
        if staged_hit {
            self.staged_closes.insert(vid, tt_e);
            return Ok(());
        }
        match self.locate_committed(kind, vid)? {
            Some((segment, row)) => {
                self.pending_closes.push(CloseRecord {
                    kind,
                    segment,
                    row,
                    tt_e,
                });
                if self.pending_overlay.is_none() {
                    let committed = self.committed_close_index()?;
                    self.pending_overlay =
                        Some(std::sync::Arc::new(CloseIndex::layered_over(committed)));
                }
                let overlay = self.pending_overlay.as_mut().expect("just set");
                std::sync::Arc::make_mut(overlay).close_row(segment, row, tt_e);
                Ok(())
            }
            None => Err(EngineError::not_found(format!(
                "no version {} to close",
                vid.to_hex()
            ))),
        }
    }

    /// Discard a version this batch staged and has already replaced.
    ///
    /// Closing it would record a belief that ran from `tt` to `tt` — held at
    /// no transaction time at all — and there is no such thing, so the row
    /// goes instead (D-059). Only staging is reachable: a committed row was
    /// written by an earlier transaction time and is closed, never retired,
    /// which is also why this can never touch a sealed segment.
    pub fn retire_version(&mut self, kind: RowKind, vid: Id96) -> Result<()> {
        self.require_batch()?;
        let removed = match kind {
            RowKind::Edge => self.staging.retire_edge(vid),
            RowKind::Node => self.staging.retire_node(vid),
        };
        if !removed {
            return Err(EngineError::not_found(format!(
                "no version {} staged in this batch to retire",
                vid.to_hex()
            )));
        }
        self.staged_closes.remove(&vid);
        Ok(())
    }

    /// Physical location of a committed version, through the vid postings.
    fn locate_committed(&self, kind: RowKind, vid: Id96) -> Result<Option<(u64, u32)>> {
        self.locate_vid(kind, vid)
    }

    /// Rows staged in the open batch — the read-your-own-writes overlay that
    /// `apply_ops` depends on (spec §2.4).
    pub fn staged_edges(&self) -> &[EdgeRow] {
        self.staging.edges()
    }

    /// Closes recorded against rows staged in this batch — the overlay that
    /// makes a same-batch carve visible to a read inside the same batch.
    pub(crate) fn staged_closes(&self) -> &HashMap<Id96, i64> {
        &self.staged_closes
    }

    pub fn staged_nodes(&self) -> &[NodeRow] {
        self.staging.nodes()
    }

    /// The staging buffers themselves, for the reads that go through the
    /// identity index instead of walking every staged row.
    pub(crate) fn staging(&self) -> &Staging {
        &self.staging
    }

    /// Publish the open batch as a new generation. `event_log` records where
    /// in the (already durable) log this generation ends.
    ///
    /// **The manifest is advanced in place.** It used to be cloned by
    /// `successor`, cloned again inside `seal`'s digest, and a third time by
    /// `publish`'s redundant `verify` — three O(live-segments) deep copies
    /// and two full serialize-and-sha256 passes, none of which the commit's
    /// timed phases counted, and which were 100% of the measured 1.8×
    /// last/first-decile growth (V2 diagnosis §2). What is left is an append
    /// to four `Vec`s, a spine update per appended entry, and one 200-byte
    /// hash.
    ///
    /// In-place means a failure part-way has to be undone, which
    /// [`CommitBase`] does. Nothing is visible to any other reader until
    /// `CURRENT` flips regardless — that is invariant 1 and it has not moved
    /// — so the rollback is about this handle's own view, not about
    /// durability.
    pub fn commit(&mut self, event_log: EventLogRef) -> Result<u64> {
        let tt = self.require_batch()?;
        self.require_writable_format()?;
        let mut phases = CommitPhases::default();
        let commit_start = std::time::Instant::now();
        let capture_t = std::time::Instant::now();
        let base = CommitBase::capture(&self.manifest, &self.merkle);
        phases.capture_us = capture_t.elapsed().as_micros() as u64;
        match self.commit_inner(tt, event_log, &base, &mut phases, commit_start) {
            Ok(generation) => Ok(generation),
            Err(e) => {
                base.restore(&mut self.manifest, &mut self.merkle);
                Err(e)
            }
        }
    }

    fn commit_inner(
        &mut self,
        tt: i64,
        event_log: EventLogRef,
        base: &CommitBase,
        phases: &mut CommitPhases,
        commit_start: std::time::Instant,
    ) -> Result<u64> {
        let next = &mut self.manifest;
        next.generation = base.generation + 1;
        next.parent = Some(base.generation);
        next.created_tt = tt;
        next.manifest_sha = String::new();
        let state = self
            .merkle
            .as_mut()
            .expect("a writable store always carries a Merkle state");

        // step 2 — segments: written and fsynced before anything names them
        let phase = std::time::Instant::now();
        let mut next_id = next.next_segment_id;
        let sealed = self.staging.seal(
            &self.root.join("seg"),
            &self.partitions,
            self.segment_target_bytes,
            &mut next_id,
            &self.staged_closes,
        )?;
        next.next_segment_id = next_id;
        for (lane, entry) in sealed.edges {
            next.stats.n_edge_versions += entry.rows as u64;
            match lane {
                Lane::Event => {
                    state.push_edge_event(&entry);
                    next.edge_lanes.event.push(entry);
                }
                Lane::Interval => {
                    state.push_edge_interval(&entry);
                    next.edge_lanes.interval.push(entry);
                }
            }
        }
        for entry in sealed.nodes {
            next.stats.n_node_versions += entry.rows as u64;
            state.push_node(&entry);
            next.node_store.push(entry);
        }
        phases.seal_us = phase.elapsed().as_micros() as u64;
        crash_point("after_seal");

        // close run for corrections landing on already-committed rows,
        // durable before the manifest that lists it
        let phase = std::time::Instant::now();
        if !self.pending_closes.is_empty() {
            let file = format!("close/{:012}.tgc", next.generation);
            let path = self.root.join(&file);
            let entries = write_close_run(&path, &self.pending_closes)?;
            let run = CloseRunRef {
                file,
                entries,
                sha: String::new(),
            };
            state.push_close_run(&run);
            next.close_runs.push(run);
        }
        phases.closes_us = phase.elapsed().as_micros() as u64;
        crash_point("after_close_runs");

        // fold this batch into the running statistics rather than
        // invalidating them; staging is still intact here
        let phase = std::time::Instant::now();
        {
            let mut cell = self.stats.lock().expect("stats mutex poisoned");
            if let Some(acc) = cell.as_mut() {
                for r in self.staging.edges() {
                    acc.add_edge(r.vt_s, r.vt_e, &r.rel_type, r.src_id);
                }
                acc.n_node_versions += self.staging.nodes().len() as u64;
            }
        }
        phases.stats_us = phase.elapsed().as_micros() as u64;

        // step 3 — dictionary tail durable before anything references it
        let phase = std::time::Instant::now();
        let (records, bytes) = self.dict.commit_to_disk()?;
        phases.dict_us = phase.elapsed().as_micros() as u64;
        crash_point("after_dict");
        next.event_log = event_log;
        next.dict.records = records;
        next.dict.bytes = bytes;
        next.stats.n_entities = self.dict.len();
        // O(1) against the state the appends above kept in step, where
        // `seal()` would re-serialize every live segment — except below
        // format 3, where `seal_with` itself falls back to the O(segments)
        // whole-document rehash (`Manifest::legacy_body_sha`); timed either
        // way so that fallback shows up rather than vanishing into the gap
        // between `dict_us` and step 4.
        let phase = std::time::Instant::now();
        next.seal_with(state);
        phases.digest_us = phase.elapsed().as_micros() as u64;
        phases.segments_named = (next.edge_lanes.event.len()
            + next.edge_lanes.interval.len()
            + next.node_store.len()) as u64;

        // steps 4-5 — manifest record, then CURRENT
        let span = base.span();
        let (manifest_us, current_us, manifest_bytes, checkpoint_gen, delta_build_us, debug_verify_us) =
            Self::publish(
                &self.root,
                Some((&span, self.checkpoint_gen)),
                &self.manifest,
                false,
                self.checkpoint_every(),
            )?;
        phases.debug_verify_us = debug_verify_us;
        phases.delta_build_us = delta_build_us;
        phases.manifest_us = manifest_us;
        phases.current_us = current_us;
        phases.manifest_bytes = manifest_bytes;
        phases.manifest_checkpoint = checkpoint_gen == self.manifest.generation;
        self.checkpoint_gen = checkpoint_gen;
        self.repin(base.generation, self.manifest.generation);
        self.staging.clear();
        self.staged_closes.clear();
        self.pending_closes.clear();
        self.pending_overlay = None;
        self.batch_tt = None;
        phases.total_us = commit_start.elapsed().as_micros() as u64;
        self.last_commit = Some(*phases);
        Ok(self.manifest.generation)
    }

    /// Abandon the open batch. Staged dictionary entries are dropped; nothing
    /// was published, so the store is already at the previous generation.
    pub fn rollback(&mut self) -> Result<()> {
        self.require_batch()?;
        self.dict.discard_staged();
        self.staging.clear();
        self.staged_closes.clear();
        self.pending_closes.clear();
        self.pending_overlay = None;
        self.batch_tt = None;
        Ok(())
    }

    fn require_batch(&self) -> Result<i64> {
        self.batch_tt
            .ok_or_else(|| EngineError::invariant("no batch is open; call begin() first"))
    }
}

impl Drop for NativeStore {
    fn drop(&mut self) {
        crate::gc::unpin(&self.pin_key, self.manifest.generation);
    }
}

/// Running statistics. Counts cover every stored row, not just believed
/// ones, matching what the DuckDB adapter reports — the two backends have to
/// agree here or `estimate_cost` would diverge between them.
#[derive(Default, Clone)]
pub struct StatsAccum {
    pub n_node_versions: u64,
    pub n_edge_versions: u64,
    pub vt_min: Option<i64>,
    pub vt_max: Option<i64>,
    pub rel_type_counts: std::collections::HashMap<String, u64>,
    /// Out-degree per source, so the max is available without a group-by.
    pub out_degree: std::collections::HashMap<u32, u64>,
}

impl StatsAccum {
    /// Fold in one edge version.
    pub fn add_edge(&mut self, vt_s: i64, vt_e: i64, rel_type: &str, src_id: u32) {
        self.n_edge_versions += 1;
        self.vt_min = Some(self.vt_min.map_or(vt_s, |m| m.min(vt_s)));
        // an open-ended interval contributes vt_s + 1, as DuckDB does
        let ve = if vt_e >= crate::OPEN_END { vt_s + 1 } else { vt_e };
        self.vt_max = Some(self.vt_max.map_or(ve, |m| m.max(ve)));
        *self.rel_type_counts.entry(rel_type.to_string()).or_default() += 1;
        *self.out_degree.entry(src_id).or_default() += 1;
    }

    pub fn max_out_degree(&self) -> u64 {
        self.out_degree.values().copied().max().unwrap_or(0)
    }
}

/// What `close_cache` holds (D-079): the generation the index is valid for,
/// how many close runs are folded into it, the last folded run's
/// `(file, sha)` — which is what proves the manifest still *starts* with what
/// was folded and has merely appended — and the index itself.
type CloseCacheEntry = (
    u64,
    usize,
    Option<(String, String)>,
    std::sync::Arc<CloseIndex>,
);

/// `segment id -> filename`, per lane, for one manifest generation (D-077).
pub(crate) struct SegmentFiles {
    pub(crate) edge: std::collections::HashMap<u64, String>,
    pub(crate) node: std::collections::HashMap<u64, String>,
}

impl SegmentFiles {
    pub(crate) fn of(&self, kind: RowKind) -> &std::collections::HashMap<u64, String> {
        match kind {
            RowKind::Edge => &self.edge,
            RowKind::Node => &self.node,
        }
    }
}

/// Physical locations of each logical identity, keyed by the first 64 bits
/// of its id. A hit is only a candidate: the caller verifies the full
/// identity, so a prefix collision can never return the wrong version.
#[derive(Default)]
pub(crate) struct Postings {
    pub(crate) by_identity: std::collections::HashMap<u64, Vec<(u64, u32)>>,
    /// The same shape keyed by the version id's `hi` prefix (the `vid64`
    /// column) — the WP-N4 path `close_version` locates committed rows
    /// through. A hit is a candidate here too: the full vid at the row
    /// decides.
    pub(crate) by_vid: std::collections::HashMap<u64, Vec<(u64, u32)>>,
    /// Segment ids already folded in.
    pub(crate) indexed: std::collections::HashSet<u64>,
    /// The open-version index (D-076): identity prefix -> the rows of that
    /// identity that are *currently believed*, i.e. `tt_e == OPEN_END`.
    ///
    /// `by_identity` above answers "where does this identity live", which is
    /// every version it has ever had; the correction path only ever wants the
    /// one or two still open, and paid O(depth) to find them (D-075 measured
    /// that walk at 58% of a correction at batch 100). This map answers the
    /// hot question directly.
    ///
    /// **It is a superset, and it is append-only, which is what keeps it
    /// cheap.** A row joins when its segment is indexed, if that segment's
    /// sidecar says it is open. Later closes arrive as close *runs*, which
    /// name physical addresses rather than identities, so removing them
    /// eagerly would need a `(segment, row) -> identity` reverse map. Instead
    /// `read.rs::locate_open` discovers them against the `CloseIndex` and
    /// prunes in place — each row examined exactly once after it closes.
    ///
    /// Compaction needs no special handling day to day: its fresh segments
    /// are indexed like any other, and a *looked-up* identity whose entries
    /// name a segment the manifest no longer lists is pruned in place by
    /// `read.rs::locate_open`. An identity nobody looks up again is not
    /// caught by that lazy prune, which is what `retain_segments` (D-087)
    /// below exists to bound.
    pub(crate) open_rows: std::collections::HashMap<u64, Vec<(u64, u32)>>,
}

impl Postings {
    /// Drop every posting naming a segment gc has actually removed from
    /// disk (D-087). `open_rows` has a *lazy* per-lookup prune
    /// (`read.rs::locate_open`) that only fires for an identity queried
    /// again; `by_identity` and `by_vid` are pure append-only history and
    /// have no prune at all. Neither gap mattered until compaction started
    /// running periodically (`tgms replay --compact-every`, a long soak's
    /// own `compact()`+`gc()` cadence): compaction physically reseals every
    /// *live* row into fresh segments under new ids, and the next lookup
    /// re-indexes them there — so each compaction cycle leaves the
    /// *previous* cycle's now-unreachable entries behind forever, on top of
    /// whatever the cycle before that left. Costs no information to drop:
    /// a stale entry's segment is already gone from the current file map,
    /// so `locate`/`locate_vid`/`locate_open` silently skip it today, and
    /// the same row is re-indexed under its new segment's id the next time
    /// something looks it up. `keep_ids` is the id set gc just proved is
    /// still referenced by a retained generation (`gc::gc`'s own
    /// `referenced`, translated from filenames via `segment_id_of`).
    pub(crate) fn retain_segments(&mut self, keep_ids: &std::collections::HashSet<u64>) {
        for map in [&mut self.by_identity, &mut self.by_vid, &mut self.open_rows] {
            map.retain(|_, v| {
                v.retain(|(seg, _)| keep_ids.contains(seg));
                !v.is_empty()
            });
        }
        self.indexed.retain(|id| keep_ids.contains(id));
    }
}

/// The store's open-segment cache, accounted in bytes (D-041).
///
/// Eviction unit: whole cached segments, least-recently-used first. The
/// entry being inserted is never the eviction victim — a single segment
/// larger than the whole budget still gets served (over budget transiently)
/// rather than thrashing on itself. All access happens under the same mutex
/// the unbounded map already took, so the read hot path gains no lock.
pub(crate) struct SegmentCache {
    entries: std::collections::HashMap<String, CacheEntry>,
    /// Logical access clock: bumped per touch, recorded per entry. Cheaper
    /// and simpler than a linked LRU list at segment-count scale (hundreds),
    /// where the O(n) victim scan is noise against the decode it replaces.
    clock: u64,
    total_bytes: u64,
    budget: Option<u64>,
    evictions: u64,
}

struct CacheEntry {
    seg: std::sync::Arc<crate::segment::Segment<MmapSource>>,
    bytes: u64,
    last_used: u64,
}

impl SegmentCache {
    fn new(budget: Option<u64>) -> Self {
        Self {
            entries: std::collections::HashMap::new(),
            clock: 0,
            total_bytes: 0,
            budget,
            evictions: 0,
        }
    }

    fn get(&mut self, file: &str) -> Option<std::sync::Arc<crate::segment::Segment<MmapSource>>> {
        self.clock += 1;
        let clock = self.clock;
        self.entries.get_mut(file).map(|e| {
            e.last_used = clock;
            e.seg.clone()
        })
    }

    fn insert(&mut self, file: String, seg: std::sync::Arc<crate::segment::Segment<MmapSource>>) {
        let bytes = seg.resident_bytes();
        self.clock += 1;
        if let Some(old) = self.entries.insert(
            file.clone(),
            CacheEntry {
                seg,
                bytes,
                last_used: self.clock,
            },
        ) {
            // two threads raced the same miss; the replaced entry is identical
            self.total_bytes -= old.bytes;
        }
        self.total_bytes += bytes;
        if let Some(budget) = self.budget {
            while self.total_bytes > budget && self.entries.len() > 1 {
                let victim = self
                    .entries
                    .iter()
                    .filter(|(name, _)| name.as_str() != file)
                    .min_by_key(|(_, e)| e.last_used)
                    .map(|(name, _)| name.clone());
                match victim {
                    Some(name) => {
                        let e = self.entries.remove(&name).expect("victim came from the map");
                        self.total_bytes -= e.bytes;
                        self.evictions += 1;
                    }
                    None => break, // only the just-inserted entry remains
                }
            }
        }
    }

    fn retain_files(&mut self, keep: &std::collections::HashSet<String>) {
        let total = &mut self.total_bytes;
        self.entries.retain(|file, e| {
            let kept = keep.contains(file);
            if !kept {
                *total -= e.bytes;
            }
            kept
        });
    }

    fn set_budget(&mut self, budget: Option<u64>) {
        self.budget = budget;
        if let Some(b) = budget {
            while self.total_bytes > b && self.entries.len() > 1 {
                let victim = self
                    .entries
                    .iter()
                    .min_by_key(|(_, e)| e.last_used)
                    .map(|(name, _)| name.clone());
                match victim {
                    Some(name) => {
                        let e = self.entries.remove(&name).expect("victim came from the map");
                        self.total_bytes -= e.bytes;
                        self.evictions += 1;
                    }
                    None => break,
                }
            }
        }
    }

    fn stats(&self) -> (usize, u64, Option<u64>, u64) {
        (self.entries.len(), self.total_bytes, self.budget, self.evictions)
    }
}

/// Resolve the segment-cache budget: the env override wins, otherwise half
/// of detected physical RAM, otherwise unbounded (D-041).
///
/// The override is plain bytes; `0` means unbounded explicitly. Garbage is
/// treated as unset rather than as an error — a tuning knob must never make
/// a store fail to open.
pub(crate) fn cache_budget(env: Option<&str>, ram_bytes: Option<u64>) -> Option<u64> {
    if let Some(v) = env.and_then(|s| s.trim().parse::<u64>().ok()) {
        return if v == 0 { None } else { Some(v) };
    }
    ram_bytes.map(|r| r / 2)
}

/// Total physical RAM, where the platform makes it cheap to ask (Linux
/// `/proc/meminfo`). `None` elsewhere — the cache is then unbounded by
/// default, which is exactly the pre-D-041 behavior.
fn detected_ram_bytes() -> Option<u64> {
    let text = std::fs::read_to_string("/proc/meminfo").ok()?;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            let kb: u64 = rest.trim().trim_end_matches("kB").trim().parse().ok()?;
            return Some(kb * 1024);
        }
    }
    None
}

/// What a `verify` pass found. An empty `problems` list is the only
/// acceptable outcome for a healthy store.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct VerifyReport {
    pub generation: u64,
    /// On-disk manifest format this generation was read at: 3 is what this
    /// build writes (checkpoint/delta chain, `manifest_sha` a Merkle root);
    /// 1 and 2 open read-only.
    pub manifest_format: u32,
    /// Generation of the checkpoint the chain was replayed from, and how many
    /// deltas sat on top of it. `manifest_deltas == 0` means `CURRENT` names
    /// a checkpoint outright.
    pub manifest_checkpoint: u64,
    pub manifest_deltas: u64,
    pub segments_checked: u32,
    pub close_runs_checked: u32,
    pub rows: u64,
    pub closes: u64,
    pub dict_records: u32,
    /// Layout quality: `tt_s` runs summed over every live segment,
    /// and the worst single segment. `max_tt_s_runs == 1` per segment is what
    /// batch ingest writes; compaction re-sorts rows from all generations
    /// into global key order while each row keeps its origin `tt_s`, so it
    /// can leave this near the row count. Read cost per materialized row is
    /// logarithmic in it, and the whole-segment belief fast paths depend on
    /// it staying small, so it is reported rather than left to be inferred
    /// from a wall clock.
    pub tt_s_runs: u64,
    pub max_tt_s_runs: u32,
    /// Human-readable text for every *error* finding, in the order they were
    /// made. Kept alongside `findings` because it is what the CLI's prose
    /// report and every pre-A3 caller already read; advisories deliberately
    /// stay out of it, so `problems.is_empty()` and "healthy" still mean the
    /// same thing they did.
    pub problems: Vec<String>,
    /// `"fast"` or `"full"` - which pass produced this report.
    pub mode: String,
    /// Every structured observation, advisories included. This is what A4's
    /// corruption sweep matches on.
    pub findings: Vec<Finding>,
    /// Full mode only: rows read out of live segments, of which
    /// `believed_rows` were still believed, spread over
    /// `identities_checked` identities.
    pub rows_walked: u64,
    pub believed_rows: u64,
    pub identities_checked: u64,
}

impl VerifyReport {
    pub fn is_healthy(&self) -> bool {
        self.problems.is_empty()
    }

    /// Record one finding. An error also lands in `problems`, which is what
    /// `is_healthy` and the prose report are built from; an advisory is
    /// listed and does not condemn the store.
    pub(crate) fn flag(&mut self, f: Finding) {
        if f.is_error() {
            self.problems.push(f.detail.clone());
        }
        self.findings.push(f);
    }

    /// Findings that make the store untrustworthy.
    pub fn errors(&self) -> impl Iterator<Item = &Finding> {
        self.findings.iter().filter(|f| f.is_error())
    }
}

/// What `upgrade_manifests` did. `upgraded == false` means the store was
/// already format 2 and nothing was written — the command is idempotent, so
/// running it twice is not an error.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct UpgradeReport {
    pub upgraded: bool,
    pub from_format: u32,
    pub generation: u64,
    pub manifest_sha: String,
}

/// Segment file id from its manifest path (`seg/000000000042.tgs` -> 42).
pub fn segment_id_of(file: &str) -> u64 {
    file.rsplit('/')
        .next()
        .and_then(|n| n.strip_suffix(".tgs"))
        .and_then(|n| n.parse().ok())
        .unwrap_or(u64::MAX)
}

/// Write via a temp file and rename, so readers see the old bytes or the new
/// bytes and never a partial write. The parent directory is fsynced too —
/// without it the rename itself can be lost on crash.
fn write_atomic(path: &Path, contents: &str) -> Result<()> {
    let tmp = path.with_extension("tmp");
    {
        let mut f = File::create(&tmp).map_err(|e| EngineError::from(e).at_file(&tmp))?;
        f.write_all(contents.as_bytes())
            .map_err(|e| EngineError::from(e).at_file(&tmp))?;
        f.sync_all().map_err(|e| EngineError::from(e).at_file(&tmp))?;
    }
    fs::rename(&tmp, path).map_err(|e| EngineError::from(e).at_file(path))?;
    if let Some(dir) = path.parent() {
        fsync_dir(dir)?;
    }
    Ok(())
}

fn fsync_dir(dir: &Path) -> Result<()> {
    File::open(dir)
        .and_then(|f| f.sync_all())
        .map_err(|e| EngineError::from(e).at_file(dir))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::segment::{MemorySource, Segment};
    use crate::OPEN_END;

    fn tmp_root(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-store-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        p
    }

    fn commit_with(store: &mut NativeStore, tt: i64, uids: &[&str]) -> u64 {
        store.begin(tt).unwrap();
        for u in uids {
            store.ensure_entity(u, "Node").unwrap();
        }
        let prev = store.manifest().event_log.chain.clone();
        store
            .commit(EventLogRef {
                offset: tt as u64,
                chain: EventLogRef::extend_chain(&prev, format!("{{\"tt\":{tt}}}\n").as_bytes()),
            })
            .unwrap()
    }

    #[test]
    fn fresh_store_bootstraps_at_generation_zero() {
        let root = tmp_root("bootstrap");
        let s = NativeStore::open(&root).unwrap();
        assert_eq!(s.generation(), 0);
        assert!(root.join("CURRENT").exists());
        assert!(NativeStore::manifest_path(&root, 0).exists());
        assert_eq!(s.dict().len(), 0);
    }

    #[test]
    fn commits_advance_generations_and_survive_reopen() {
        let root = tmp_root("advance");
        let mut s = NativeStore::open(&root).unwrap();
        assert_eq!(commit_with(&mut s, 10, &["n1", "n2"]), 1);
        assert_eq!(commit_with(&mut s, 20, &["n3"]), 2);
        drop(s);

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 2);
        assert_eq!(re.dict().len(), 3);
        assert_eq!(re.dict().dense_id("n3"), Some(2));
        assert_eq!(re.manifest().parent, Some(1));
    }

    #[test]
    fn open_phases_default_is_all_zero_and_sums_to_zero() {
        // pure struct arithmetic, no I/O: the zero value every field of
        // `OpenPhases` must agree on, and the sum-of-named-phases invariant
        // an empty phase record trivially satisfies.
        let p = OpenPhases::default();
        assert_eq!(p.checkpoint_read_parse_us, 0);
        assert_eq!(p.merkle_verify_us, 0);
        assert_eq!(p.chain_format, 0);
        assert_eq!(p.state_build_us, 0);
        assert_eq!(p.delta_replay_us, 0);
        assert_eq!(p.delta_count, 0);
        assert_eq!(p.dictionary_open_us, 0);
        assert_eq!(p.other_us, 0);
        assert_eq!(p.total_us, 0);
        let named = p.checkpoint_read_parse_us
            + p.merkle_verify_us
            + p.state_build_us
            + p.delta_replay_us
            + p.dictionary_open_us;
        assert_eq!(named + p.other_us, p.total_us);
    }

    #[test]
    fn open_phases_account_for_every_delta_and_sum_within_the_total() {
        // B1-v2 (benchmarks/results-v1/b1-manifest-v2-ab-2026-09.README.md
        // §B1(c)) could not score "manifest-chain open <= 70 ms" because
        // NativeAdapter exposed no open-phase timing. This is that timing's
        // contract: every key present, delta_count matching what was
        // written, and the named phases never outrunning the total.
        let root = tmp_root("open-phases");
        let mut s = NativeStore::open(&root).unwrap();
        s.set_checkpoint_every(Some(1_000_000)); // one checkpoint, then all deltas
        const N: i64 = 12;
        for tt in 1..=N {
            s.begin(tt * 10).unwrap();
            s.ensure_entity(&format!("n{tt}"), "Node").unwrap();
            s.commit(EventLogRef::default()).unwrap();
        }
        drop(s);

        for _ in 0..2 {
            // opening the same store twice must yield the same delta_count
            let re = NativeStore::open(&root).unwrap();
            assert_eq!(re.generation(), N as u64);
            let p = re.open_phases();
            assert_eq!(p.delta_count, N as u64, "one delta per commit above the genesis checkpoint");
            assert_eq!(p.chain_format, crate::MANIFEST_FORMAT_VERSION);
            let named = p.checkpoint_read_parse_us
                + p.merkle_verify_us
                + p.state_build_us
                + p.delta_replay_us
                + p.dictionary_open_us;
            assert!(
                p.total_us >= named,
                "total_us {} must cover the named phases {named} \
                 (other_us {})",
                p.total_us,
                p.other_us
            );
            assert_eq!(p.other_us, p.total_us - named);
        }
    }

    #[test]
    fn rollback_leaves_no_trace() {
        let root = tmp_root("rollback");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["kept"]);
        s.begin(20).unwrap();
        s.ensure_entity("dropped", "Node").unwrap();
        s.rollback().unwrap();
        assert_eq!(s.generation(), 1);
        assert_eq!(s.dict().dense_id("dropped"), None);
        drop(s);

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 1);
        assert_eq!(re.dict().len(), 1);
        assert_eq!(re.dict().dense_id("dropped"), None);
    }

    // --- staged rows -> segments (WP-N2) ------------------------------- //

    fn edge_row(src: u32, dst: u32, vt_s: i64, tt_s: i64, i: u32) -> EdgeRow {
        use crate::derive::{edge_eid, version_vid};
        let disc = format!("#{i}");
        let eid = edge_eid("n1", "n2", "SENT_MSG_TO", &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id: src,
            dst_id: dst,
            rel_type: "SENT_MSG_TO".into(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    fn read_segment(root: &Path, file: &str) -> crate::segment::Segment<crate::segment::MemorySource> {
        let path = root.join(file);
        let src = crate::segment::MemorySource::load(&path).unwrap();
        crate::segment::Segment::open(&path, src, true).unwrap()
    }

    #[test]
    fn staged_rows_become_readable_segments_on_commit() {
        let root = tmp_root("segments");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let b = s.ensure_entity("n2", "Node").unwrap();
        for i in 0..10u32 {
            s.stage_edge(edge_row(a, b, 1_000 + i as i64, 100, i)).unwrap();
        }
        assert_eq!(s.staged_edges().len(), 10, "read-your-own-writes");
        s.commit(EventLogRef::default()).unwrap();
        assert!(s.staged_edges().is_empty(), "commit drains staging");
        drop(s);

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.manifest().stats.n_edge_versions, 10);
        assert_eq!(re.manifest().edge_lanes.event.len(), 1);
        assert!(re.manifest().edge_lanes.interval.is_empty());
        let seg = read_segment(&root, &re.manifest().edge_lanes.event[0].file);
        assert_eq!(seg.rows(), 10);
        assert_eq!(seg.i64_column("vt_s").unwrap()[0], 1_000);
    }

    #[test]
    fn rollback_writes_no_segments_and_advances_nothing() {
        let root = tmp_root("rollback-segments");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        s.ensure_entity("n1", "Node").unwrap();
        s.stage_edge(edge_row(0, 0, 5, 100, 0)).unwrap();
        s.rollback().unwrap();

        assert!(s.staged_edges().is_empty());
        assert_eq!(s.generation(), 0);
        assert_eq!(s.manifest().next_segment_id, 0);
        assert_eq!(
            fs::read_dir(root.join("seg")).unwrap().count(),
            0,
            "a rolled-back batch must leave no segment files"
        );
    }

    #[test]
    fn segment_ids_never_repeat_across_generations() {
        let root = tmp_root("segment-ids");
        let mut s = NativeStore::open(&root).unwrap();
        for gen in 0..3u32 {
            s.begin(100 + gen as i64).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            s.stage_edge(edge_row(a, a, 10 + gen as i64, 100 + gen as i64, gen)).unwrap();
            s.commit(EventLogRef::default()).unwrap();
        }
        // each generation inherits its parent's segments and adds its own
        let files: Vec<String> = s
            .manifest()
            .edge_lanes
            .event
            .iter()
            .map(|e| e.file.clone())
            .collect();
        assert_eq!(files.len(), 3, "every generation's segment is still listed");
        let mut unique = files.clone();
        unique.sort();
        unique.dedup();
        assert_eq!(unique.len(), files.len(), "ids were reused: {files:?}");
        assert_eq!(s.manifest().next_segment_id, 3);
        assert_eq!(s.manifest().stats.n_edge_versions, 3);
    }

    #[test]
    fn long_lived_facts_route_to_the_interval_lane() {
        let root = tmp_root("lanes");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let mut forever = edge_row(a, a, 0, 100, 0);
        forever.vt_e = crate::OPEN_END;
        s.stage_edge(forever).unwrap();
        s.stage_edge(edge_row(a, a, 5, 100, 1)).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        assert_eq!(s.manifest().edge_lanes.event.len(), 1);
        assert_eq!(s.manifest().edge_lanes.interval.len(), 1);
    }

    // --- belief visibility (WP-N3) -------------------------------------- //

    fn scan_at(root: &Path, store: &NativeStore, as_of: i64) -> Vec<Id96> {
        use crate::scan::{ScanRequest, ScanSet, ScanTarget};
        let m = store.manifest();
        let mut segs = Vec::new();
        let mut ids = Vec::new();
        for e in m.edge_lanes.event.iter().chain(m.edge_lanes.interval.iter()) {
            let path = root.join(&e.file);
            segs.push(Segment::open(&path, MemorySource::load(&path).unwrap(), true).unwrap());
            ids.push(super::segment_id_of(&e.file));
        }
        let targets: Vec<ScanTarget<'_, MemorySource>> = segs
            .iter()
            .zip(&ids)
            .map(|(segment, id)| ScanTarget {
                segment,
                lane: Lane::Event,
                id: *id,
            })
            .collect();
        let set = ScanSet::new(targets).with_closes(store.close_index().unwrap());
        let mut req = ScanRequest::current();
        req.as_of_tt = as_of;
        let (sel, _) = set.select(&req).unwrap();
        set.merged(&sel, None)
            .unwrap()
            .into_iter()
            .map(|(si, row)| segs[si].vid_at(row as usize).unwrap())
            .collect()
    }

    #[test]
    fn closing_a_version_hides_it_only_from_later_beliefs() {
        // the bi-temporal immutability property: a correction must not change
        // what the database believed *before* the correction happened
        let root = tmp_root("close-bitemporal");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let rows: Vec<EdgeRow> = (0..3).map(|i| edge_row(a, a, 10 + i as i64, 100, i)).collect();
        for r in &rows {
            s.stage_edge(r.clone()).unwrap();
        }
        s.commit(EventLogRef::default()).unwrap();
        assert_eq!(scan_at(&root, &s, OPEN_END).len(), 3);

        // a later batch stops believing the middle version
        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, rows[1].vid, 200).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        let now = scan_at(&root, &s, OPEN_END);
        assert_eq!(now.len(), 2, "the closed version is no longer believed");
        assert!(!now.contains(&rows[1].vid));

        let before = scan_at(&root, &s, 150);
        assert_eq!(before.len(), 3, "history must be unchanged by a correction");
        assert!(before.contains(&rows[1].vid));

        // and exactly at the close time it is already gone (half-open tt)
        assert_eq!(scan_at(&root, &s, 200).len(), 2);
        assert_eq!(scan_at(&root, &s, 199).len(), 3);
    }

    #[test]
    fn closes_survive_reopening_the_store() {
        let root = tmp_root("close-reopen");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let row = edge_row(a, a, 10, 100, 0);
        s.stage_edge(row.clone()).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, row.vid, 200).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        assert_eq!(s.manifest().close_runs.len(), 1);
        drop(s);

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.close_index().unwrap().len(), 1);
        assert!(scan_at(&root, &re, OPEN_END).is_empty());
        assert_eq!(scan_at(&root, &re, 150).len(), 1);
    }

    #[test]
    fn a_version_closed_in_its_own_batch_folds_into_the_sidecar() {
        // a carve: the batch inserts a version and closes it again, so no
        // close run is needed — the segment carries its own sidecar
        let root = tmp_root("close-staged");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let keep = edge_row(a, a, 10, 100, 0);
        let doomed = edge_row(a, a, 20, 100, 1);
        s.stage_edge(keep.clone()).unwrap();
        s.stage_edge(doomed.clone()).unwrap();
        s.close_version(RowKind::Edge, doomed.vid, 100).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        assert!(
            s.manifest().close_runs.is_empty(),
            "a same-batch close needs no run file"
        );
        let entry = &s.manifest().edge_lanes.event[0];
        assert_eq!(entry.n_closed_folded, 1);
        assert!(!entry.all_current);

        let now = scan_at(&root, &s, OPEN_END);
        assert_eq!(now, vec![keep.vid], "only the surviving version is believed");
        // the row is still stored — closed is not deleted
        let path = root.join(&entry.file);
        let seg = Segment::open(&path, MemorySource::load(&path).unwrap(), true).unwrap();
        assert_eq!(seg.rows(), 2, "a closed row is retained, only hidden");
    }

    #[test]
    fn closing_an_unknown_version_is_not_found() {
        let root = tmp_root("close-missing");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let ghost = crate::derive::version_vid("nope", 1, 1);
        let err = match s.close_version(RowKind::Edge, ghost, 200) {
            Ok(()) => panic!("closing a nonexistent version must fail"),
            Err(e) => e,
        };
        assert_eq!(err.category, crate::error::Category::NotFound);
    }

    #[test]
    fn a_close_verifies_the_full_vid_not_just_its_prefix() {
        // the postings key is only the 64-bit prefix; a candidate whose lo
        // differs must be rejected, or a prefix collision could close the
        // wrong version
        let root = tmp_root("close-prefix");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let row = edge_row(a, a, 10, 100, 0);
        s.stage_edge(row.clone()).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        s.begin(200).unwrap();
        let imposter = Id96 {
            hi: row.vid.hi,
            lo: row.vid.lo.wrapping_add(1),
        };
        let err = s.close_version(RowKind::Edge, imposter, 200).unwrap_err();
        assert_eq!(err.category, crate::error::Category::NotFound);
        s.close_version(RowKind::Edge, row.vid, 200).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        assert_eq!(s.close_index().unwrap().len(), 1);
    }

    #[test]
    fn a_node_close_locates_through_the_postings_too() {
        let root = tmp_root("close-node");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let row = NodeRow {
            vid: crate::derive::version_vid("n1", 100, 10),
            uid_id: a,
            label: "Node".into(),
            vt_s: 10,
            vt_e: 11,
            tt_s: 100,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        };
        s.stage_node(row.clone()).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        s.begin(200).unwrap();
        s.close_version(RowKind::Node, row.vid, 200).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        assert!(s.believed_node_versions("n1", OPEN_END).unwrap().is_empty());
        assert_eq!(s.believed_node_versions("n1", 150).unwrap().len(), 1);
    }

    #[test]
    fn corrections_at_scale_locate_through_the_postings() {
        // WP-N4 regression scale. The §13 sweep (docs/eval_bitemporal.md)
        // showed replay superlinear in correction volume because every close
        // walked every segment. Enough segments and closes here that the old
        // shape is exercised — and the postings must come out built and
        // consulted, not bypassed.
        let root = tmp_root("close-scale");
        let mut s = NativeStore::open(&root).unwrap();
        let mut rows = Vec::new();
        for batch in 0..8u32 {
            let tt = 100 + batch as i64;
            s.begin(tt).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            let b = s.ensure_entity("n2", "Node").unwrap();
            for i in 0..250u32 {
                let n = batch * 250 + i;
                let r = edge_row(a, b, n as i64, tt, n);
                rows.push(r.clone());
                s.stage_edge(r).unwrap();
            }
            s.commit(EventLogRef::default()).unwrap();
        }
        assert!(
            s.manifest().edge_lanes.event.len() >= 8,
            "the scale must span many segments"
        );

        s.begin(500).unwrap();
        for r in rows.iter().step_by(10) {
            s.close_version(RowKind::Edge, r.vid, 500).unwrap();
        }
        s.commit(EventLogRef::default()).unwrap();

        {
            let p = s.edge_postings().lock().unwrap();
            assert_eq!(p.indexed.len(), 8, "closes must build the postings");
            assert_eq!(
                p.by_vid.values().map(Vec::len).sum::<usize>(),
                rows.len(),
                "every committed row is posted exactly once"
            );
        }

        let closed: std::collections::HashSet<String> =
            rows.iter().step_by(10).map(|r| r.vid.to_hex()).collect();
        assert_eq!(s.close_index().unwrap().len(), closed.len());
        for r in s.all_edge_versions().unwrap() {
            let expected = if closed.contains(&r.vid) { 500 } else { OPEN_END };
            assert_eq!(r.tt_e, expected, "vid {}", r.vid);
        }
    }

    #[test]
    fn the_open_index_shrinks_to_what_is_still_believed() {
        // D-076 regression gate. The index must (a) get built at all, (b) hold
        // only rows a segment sealed open, and (c) *prune* on lookup as closes
        // arrive — without pruning it degenerates back into `by_identity` and
        // the O(depth) walk it replaced comes back with extra memory.
        let root = tmp_root("open-index");
        let mut s = NativeStore::open(&root).unwrap();
        let depth = 40usize;
        let mut vids = Vec::new();
        for d in 0..depth {
            let tt = 100 + d as i64;
            s.begin(tt).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            let b = s.ensure_entity("n2", "Node").unwrap();
            let r = edge_row(a, b, 0, tt, 0);
            vids.push(r.vid);
            s.stage_edge(r).unwrap();
            // supersede the previous version, exactly as the correction path
            // does: closed, not retired, because it was believed at an
            // earlier transaction time
            if d > 0 {
                s.close_version(RowKind::Edge, vids[d - 1], tt).unwrap();
            }
            s.commit(EventLogRef::default()).unwrap();
        }

        // one identity, `depth` versions, exactly one of them still believed.
        // The lookup is also what triggers the prune, so it must come first.
        let eid = s.all_edge_versions().unwrap()[0].eid.clone();
        let believed = s.believed_edge_versions(&eid, OPEN_END).unwrap();
        assert_eq!(believed.len(), 1, "exactly one version of the identity is open");
        assert_eq!(believed[0].vid, vids[depth - 1].to_hex(), "the newest one");

        let held = {
            let p = s.edge_postings().lock().unwrap();
            assert_eq!(
                p.by_identity.values().map(Vec::len).sum::<usize>(),
                depth,
                "every version is still reachable by identity"
            );
            p.open_rows.values().map(Vec::len).sum::<usize>()
        };
        assert_eq!(
            held, 1,
            "the open index must prune closed rows on lookup: it holds {held} \
             of {depth} versions, so it is tracking history rather than belief"
        );
    }

    #[test]
    fn compaction_cycles_do_not_multiply_the_identity_postings() {
        // D-087 regression. `open_rows` has a lazy per-lookup prune
        // (`read.rs::locate_open`); `by_identity` and `by_vid` have none at
        // all, which was fine until compaction started running
        // periodically (`tgms replay --compact-every`, and a long soak's
        // own compact()+gc() cadence): compaction reseals every row that
        // still exists — open *and* closed, since compaction never drops
        // one — into fresh segments every cycle, and the next lookup
        // re-indexes them there, on top of every earlier cycle's now-
        // unreachable entries that nothing ever freed. Observed in the
        // wild: `tgms replay --compact-every 500` over a 24h soak's log
        // (1,074,952 batches, 314,897,038 bytes) was OOM-killed at ~83 GB
        // anon RSS after reaching manifest generation ~513,024 — ~160 KB
        // retained per replayed generation — while `store_digest()`-
        // relevant content was only ~1.7M entities: retention, not data.
        let root = tmp_root("compaction-postings-bound");
        let mut s = NativeStore::open(&root).unwrap();
        const CYCLES: usize = 8;
        const PER_CYCLE: usize = 25;
        const N: usize = CYCLES * PER_CYCLE;

        let mut vids = Vec::with_capacity(N);
        for d in 0..N {
            let tt = 100 + d as i64;
            s.begin(tt).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            let b = s.ensure_entity("n2", "Node").unwrap();
            let r = edge_row(a, b, 0, tt, 0);
            vids.push(r.vid);
            s.stage_edge(r).unwrap();
            // supersede the previous version, exactly as a correction does
            // — this is also what triggers `index_segments` every commit.
            if d > 0 {
                s.close_version(RowKind::Edge, vids[d - 1], tt).unwrap();
            }
            s.commit(EventLogRef::default()).unwrap();

            if (d + 1) % PER_CYCLE == 0 {
                s.compact().unwrap();
                s.gc(2).unwrap();
            }
        }

        let (by_identity_entries, by_vid_entries, indexed_len) = {
            let p = s.edge_postings().lock().unwrap();
            (
                p.by_identity.values().map(Vec::len).sum::<usize>(),
                p.by_vid.values().map(Vec::len).sum::<usize>(),
                p.indexed.len(),
            )
        };

        // Every version ever committed legitimately lives in `by_identity`
        // forever (bi-temporal history is never dropped), so N is the
        // correct floor. The bug's shape is multiplicative in CYCLES, not
        // additive to it: unfixed, this comes out within a small constant
        // of PER_CYCLE * CYCLES^2 / 2 (each cycle re-indexes the entire
        // row set sealed so far) — tens of thousands of entries here,
        // against a few thousand fixed. A 3x margin over N catches the
        // multiplication while tolerating the legitimate double-booking of
        // the one or two generations `gc(keep_last=2)` still retains.
        assert!(
            by_identity_entries <= 3 * N,
            "by_identity holds {by_identity_entries} entries for {N} \
             versions ever committed over {CYCLES} compaction cycles — \
             compaction is re-indexing history that nothing ever prunes \
             (D-087)"
        );
        assert!(
            by_vid_entries <= 3 * N,
            "by_vid holds {by_vid_entries} entries for {N} versions — same \
             leak as by_identity (D-087)"
        );
        assert!(
            indexed_len <= 4 * PER_CYCLE,
            "`indexed` holds {indexed_len} segment ids after {CYCLES} \
             compactions of {PER_CYCLE} segments each — stale segment ids \
             from superseded compaction cycles are never forgotten (D-087)"
        );
    }


    #[test]
    fn rolled_back_closes_leave_no_run() {
        let root = tmp_root("close-rollback");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let row = edge_row(a, a, 10, 100, 0);
        s.stage_edge(row.clone()).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, row.vid, 200).unwrap();
        s.rollback().unwrap();

        assert!(s.manifest().close_runs.is_empty());
        assert_eq!(scan_at(&root, &s, OPEN_END).len(), 1, "the close was abandoned");
    }

    // --- crash-step matrix (spec WP-N1 acceptance) --------------------- //

    #[test]
    fn crash_after_manifest_before_current_serves_previous_generation() {
        let root = tmp_root("crash-manifest");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        drop(s);

        // simulate: generation 2's *delta* reached disk, CURRENT did not.
        // Reconstruction from CURRENT stops at 1 and never reads 2, so this
        // is the same orphaned-manifest case format 1 had.
        let g1 = manifest_chain::reconstruct(&root, 1).unwrap().manifest;
        let mut g2 = g1.successor(20);
        g2.stats.n_entities = 99;
        g2.seal();
        let d = ManifestDelta::between(&g1, &g2, 0).expect("expressible as a delta");
        write_atomic(&NativeStore::manifest_path(&root, 2), &d.to_json()).unwrap();

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 1, "orphaned delta must not be adopted");
        assert_eq!(re.dict().len(), 1);
        assert_eq!(re.manifest().stats.n_entities, 1);
        assert!(re.verify().unwrap().is_healthy());
    }

    // --- CURRENT missing: a populated store must refuse, not go empty --- //
    //
    // Corruption-sweep finding (2026-09-15): deleting `native/CURRENT` from a
    // populated store used to make `open` treat the directory as fresh and
    // empty, so every query silently returned no rows. `open` must now
    // distinguish that from the one case where "CURRENT is absent" really
    // does mean "nothing published yet": the bootstrap crash between the
    // genesis manifest write and the very first `CURRENT` flip.

    #[test]
    fn empty_directory_without_current_still_opens_fresh() {
        let root = tmp_root("current-missing-empty");
        // `open` itself creates the subdirectories; nothing else is on disk.
        let s = NativeStore::open(&root).unwrap();
        assert_eq!(s.generation(), 0);
        assert!(root.join("CURRENT").exists());
    }

    // --- the commit path's working manifest (V2 §4(a)) -------------------- //

    #[test]
    fn a_commit_leaves_the_handle_holding_a_manifest_its_own_state_seals() {
        // the invariant the whole in-place commit path rests on: the carried
        // Merkle state is still the state of the manifest the handle holds,
        // commit after commit, and the O(1) seal agrees with the O(n) oracle
        let root = tmp_root("working-manifest");
        let mut s = NativeStore::open(&root).unwrap();
        for g in 1..=12i64 {
            commit_with(&mut s, g * 10, &[&format!("n{g}")]);
            let state = s.merkle.as_ref().expect("a writable store carries one");
            assert_eq!(
                state,
                &merkle::ManifestMerkle::from_manifest(s.manifest()),
                "the carried state drifted from the manifest at generation {g}"
            );
            assert_eq!(
                s.manifest().manifest_sha,
                s.manifest().body_sha_canonical(),
                "the incremental seal disagrees with the oracle at generation {g}"
            );
        }
        assert!(s.verify().unwrap().is_healthy());
    }

    #[test]
    fn the_delta_cut_from_the_append_span_is_the_one_a_full_diff_would_produce() {
        // Addendum 3 ruling 3: `between`'s eight O(n) scans come off the
        // commit path. The record they used to produce must not change.
        let root = tmp_root("append-span");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        let parent = s.manifest().clone();
        let ckpt = s.checkpoint_generation();
        commit_with(&mut s, 20, &["n2"]);
        let child = s.manifest().clone();

        let from_span = match manifest_chain::read_record(&root, child.generation).unwrap() {
            manifest_chain::ManifestRecord::Delta(d) => d,
            other => panic!("expected a delta, got {other:?}"),
        };
        let from_diff = ManifestDelta::between(&parent, &child, ckpt)
            .expect("the same pair is expressible as a delta either way");
        assert_eq!(from_span, from_diff);
        assert_eq!(from_span.apply(&parent), child);
    }

    #[test]
    fn a_rolled_back_commit_puts_the_handle_back_exactly_where_it_was() {
        // in-place mutation means a failure part-way has to be undone. This
        // exercises the guard directly: capture, make the mess a commit makes,
        // restore, and demand byte equality with what was captured.
        let root = tmp_root("commit-rollback");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        commit_with(&mut s, 20, &["n2"]);

        let before = s.manifest().clone();
        let before_state = s.merkle.clone();
        let base = CommitBase::capture(&s.manifest, &s.merkle);

        // exactly the mutations `commit_inner` performs before it can fail
        {
            let m = &mut s.manifest;
            m.generation += 1;
            m.parent = Some(before.generation);
            m.created_tt = 999;
            m.manifest_sha = String::new();
            m.next_segment_id += 3;
            m.stats.n_node_versions += 7;
            m.dict.records += 2;
            m.event_log.offset += 64;
            let seg = crate::manifest::SegmentEntry {
                file: "seg/000000000099.tgs".into(),
                rows: 3,
                key_lo: (1, "a".into()),
                key_hi: (2, "b".into()),
                vt_min: 0,
                vt_max: 5,
                vt_e_max: 6,
                tt_s_min: 1,
                tt_s_max: 1,
                rel_codes: vec![1],
                n_closed_folded: 0,
                all_current: true,
                sha: "00000000000000ff".into(),
            };
            let state = s.merkle.as_mut().unwrap();
            state.push_node(&seg);
            m.node_store.push(seg.clone());
            state.push_edge_event(&seg);
            m.edge_lanes.event.push(seg.clone());
            let run = CloseRunRef {
                file: "close/000000000099.tgc".into(),
                entries: 1,
                sha: String::new(),
            };
            state.push_close_run(&run);
            m.close_runs.push(run);
            m.seal_with(s.merkle.as_ref().unwrap());
        }
        assert_ne!(s.manifest, before, "the fixture must actually dirty it");

        base.restore(&mut s.manifest, &mut s.merkle);
        assert_eq!(s.manifest, before);
        assert_eq!(s.merkle, before_state);
        assert_eq!(
            s.merkle.as_ref().unwrap(),
            &merkle::ManifestMerkle::from_manifest(&s.manifest)
        );

        // and the handle is still usable: the next commit is the one the
        // rolled-back attempt would have been
        let g = commit_with(&mut s, 30, &["n3"]);
        assert_eq!(g, before.generation + 1);
        assert!(s.verify().unwrap().is_healthy());
        drop(s);
        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), g);
    }

    #[test]
    fn compaction_and_gc_rebuild_the_state_rather_than_appending_to_it() {
        // both replace or truncate the segment set, so neither can extend a
        // spine. Both already force a checkpoint, so the rebuild is free —
        // what matters is that the handle is not left with a stale state.
        let root = tmp_root("rebuild-state");
        let mut s = NativeStore::open(&root).unwrap();
        for g in 1..=6i64 {
            commit_with(&mut s, g * 10, &[&format!("n{g}")]);
        }
        s.compact().unwrap();
        assert_eq!(
            s.merkle.as_ref().unwrap(),
            &merkle::ManifestMerkle::from_manifest(s.manifest()),
            "compaction left a stale Merkle state"
        );
        assert!(s.verify().unwrap().is_healthy());

        s.gc(2).unwrap();
        assert_eq!(
            s.merkle.as_ref().unwrap(),
            &merkle::ManifestMerkle::from_manifest(s.manifest())
        );
        commit_with(&mut s, 100, &["after"]);
        assert!(s.verify().unwrap().is_healthy());
        drop(s);
        assert!(NativeStore::open(&root)
            .unwrap()
            .verify()
            .unwrap()
            .is_healthy());
    }

    #[test]
    fn orphaned_genesis_manifest_alone_still_opens_fresh() {
        // exactly what a crash at `after_manifest` during the very first
        // `open` (publishing generation 0) leaves behind: the genesis
        // checkpoint on disk, no CURRENT, no segments, no dictionary tail.
        let root = tmp_root("current-missing-genesis-orphan");
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub)).unwrap();
        }
        let genesis = Manifest::genesis();
        write_atomic(
            &NativeStore::manifest_path(&root, 0),
            &manifest_chain::checkpoint_json(&genesis),
        )
        .unwrap();
        assert!(!root.join("CURRENT").exists());

        let s = NativeStore::open(&root).unwrap();
        assert_eq!(s.generation(), 0);
        assert!(root.join("CURRENT").exists(), "the crash is healed");
        assert!(s.verify().unwrap().is_healthy());
    }

    #[test]
    fn populated_store_without_current_refuses_to_open() {
        let root = tmp_root("current-missing-populated");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        commit_with(&mut s, 20, &["n2"]);
        drop(s);

        fs::remove_file(root.join("CURRENT")).unwrap();

        let err = open_err(&root);
        assert_eq!(err.category, crate::error::Category::Corrupt);
        assert!(err.message.contains("CURRENT"), "{}", err.message);
        assert!(
            err.remedy.as_deref().unwrap_or_default().contains("CURRENT"),
            "{:?}",
            err.remedy
        );
    }

    #[test]
    fn populated_store_with_only_segments_and_no_manifest_dir_entries_refuses() {
        // the manifests directory can itself still hold generation 0 (the
        // genesis checkpoint is not deleted by this scenario), but a real
        // segment on disk means the store is not "nothing published yet".
        let root = tmp_root("current-missing-with-segments");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        drop(s);

        fs::remove_file(root.join("CURRENT")).unwrap();
        // even if every manifest after genesis vanished too, a segment file
        // alone is enough evidence that this store was not empty
        fs::remove_file(NativeStore::manifest_path(&root, 1)).unwrap();

        let err = open_err(&root);
        assert_eq!(err.category, crate::error::Category::Corrupt);
        assert!(err.message.contains("CURRENT"), "{}", err.message);
    }

    #[test]
    fn dictionary_tail_alone_is_enough_evidence_to_refuse() {
        // a store whose manifests and segments are both gone but whose
        // dictionary log still holds bytes: still not an empty directory.
        let root = tmp_root("current-missing-dict-only");
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub)).unwrap();
        }
        fs::write(root.join(DICT), b"not actually a valid dict tail, just bytes").unwrap();

        let err = open_err(&root);
        assert_eq!(err.category, crate::error::Category::Corrupt);
        assert!(err.message.contains("CURRENT"), "{}", err.message);
    }

    // --- format 2: the manifest chain (memo §6) ------------------------- //

    fn record_of(root: &Path, g: u64) -> crate::manifest_chain::ManifestRecord {
        manifest_chain::read_record(root, g).unwrap()
    }

    /// `NativeStore` is not `Debug`, so `unwrap_err` cannot be used on it.
    fn open_err(root: &Path) -> EngineError {
        match NativeStore::open(root) {
            Ok(_) => panic!("a broken manifest chain must not open"),
            Err(e) => e,
        }
    }

    #[test]
    fn commits_write_deltas_and_a_checkpoint_every_k() {
        let root = tmp_root("chain-shape");
        let mut s = NativeStore::open(&root).unwrap();
        // a tiny K so the periodic checkpoint is reachable in a unit test
        s.set_checkpoint_every(Some(4));
        {
            for tt in 1..=9i64 {
                s.begin(tt * 10).unwrap();
                let a = s.ensure_entity(&format!("n{tt}"), "Node").unwrap();
                s.stage_edge(edge_row(a, a, tt, tt * 10, tt as u32)).unwrap();
                s.commit(EventLogRef::default()).unwrap();
            }
            assert_eq!(s.generation(), 9);
            assert_eq!(s.checkpoint_generation(), 8);

            let kinds: Vec<bool> = (0..=9).map(|g| record_of(&root, g).is_checkpoint()).collect();
            assert_eq!(
                kinds,
                vec![true, false, false, false, true, false, false, false, true, false],
                "generation 0 and every 4th generation are checkpoints"
            );

            // the delta really is the smaller document
            let delta_bytes = fs::metadata(NativeStore::manifest_path(&root, 9))
                .unwrap()
                .len();
            let ckpt_bytes = fs::metadata(NativeStore::manifest_path(&root, 8))
                .unwrap()
                .len();
            assert!(delta_bytes < ckpt_bytes, "{delta_bytes} vs {ckpt_bytes}");

            // and the phase record says which was written
            assert!(!s.last_commit_phases().unwrap().manifest_checkpoint);
        }
    }

    #[test]
    fn commit_phases_fully_account_for_total_us_over_sixty_singleton_commits() {
        // B1V2_AB_DIAGNOSIS_2026-09-15.md Q1: the B1 A/B's untimed residual
        // (total_us minus the sum of every named phase) absorbed 100% of the
        // last/first-decile growth in engine-commit `total_us`, in every
        // format and every arm. B1-v2d adds `capture_us` (`CommitBase::
        // capture`), `digest_us` (`Manifest::seal_with`), and
        // `delta_build_us` (the delta-or-checkpoint JSON `publish` used to
        // build before its own timer started) to close that gap. This
        // asserts the invariant holds on *every* commit, not just on
        // average, over a run long enough to grow the segment/manifest
        // history the diagnosis's candidate mechanisms depend on.
        //
        // The CI runner (GitHub Actions, an unoptimized `cargo test` debug
        // build on a slow shared disk) cannot hold a 50us/2% bar: scheduler
        // and I/O jitter alone blow past it on an individual commit. A debug
        // build also pays real, non-representative overhead (e.g.
        // `store::publish`'s O(segments) `debug_assert_eq!`), so the bar
        // itself widens under `cfg!(debug_assertions)` -- this is about the
        // build profile, not the underlying instrumentation, which is
        // correct and unchanged.
        //
        // Even the widened bar is not CI-safe *per commit*: a shared runner
        // can preempt the test process for a scheduling quantum in the
        // middle of any single commit, inflating that one commit's residual
        // by an amount that has nothing to do with the engine. A hard
        // per-commit assertion therefore has an irreducible flake rate on
        // shared infrastructure no matter how wide the bar. What must not
        // regress is the *distribution*: a genuine, systematic accounting
        // leak shows up on (nearly) every commit, so it moves the median and
        // the tail together, and it grows with history size, so it moves the
        // back of the run relative to the front. A single preempted commit
        // can only move one point in the distribution and cannot manufacture
        // a first-to-last trend, so median/p90/no-growth checks catch a real
        // leak while tolerating scheduler noise.
        fn residual_bound(total_us: u64) -> u64 {
            if cfg!(debug_assertions) {
                std::cmp::max(1_000, total_us / 10) // max(1000us, 10%)
            } else {
                std::cmp::max(100, total_us / 50) // max(100us, 2%)
            }
        }
        fn median(mut xs: Vec<u64>) -> u64 {
            xs.sort_unstable();
            let n = xs.len();
            if n % 2 == 1 {
                xs[n / 2]
            } else {
                (xs[n / 2 - 1] + xs[n / 2]) / 2
            }
        }
        fn p90(mut xs: Vec<u64>) -> u64 {
            xs.sort_unstable();
            let n = xs.len();
            let idx = ((n as f64 - 1.0) * 0.9).round() as usize;
            xs[idx.min(n - 1)]
        }
        // The three worst commits by residual, for failure messages -- a
        // scheduler-hiccup failure and a systematic-leak failure look
        // different here (one outlier vs. three-in-a-row elevated values).
        fn worst_three(commits: &[(i64, u64, u64, u64)]) -> String {
            let mut v = commits.to_vec();
            v.sort_unstable_by_key(|a| std::cmp::Reverse(a.3));
            v.iter()
                .take(3)
                .map(|(idx, total, named_sum, residual)| {
                    format!(
                        "commit {idx}: total_us={total} named_sum={named_sum} \
                         residual={residual}us"
                    )
                })
                .collect::<Vec<_>>()
                .join("; ")
        }

        let root = tmp_root("phase-accounting");
        let mut s = NativeStore::open(&root).unwrap();
        let mut residuals = Vec::with_capacity(60);
        let mut totals = Vec::with_capacity(60);
        let mut commits: Vec<(i64, u64, u64, u64)> = Vec::with_capacity(60);
        for tt in 1..=60i64 {
            let uid = format!("n{tt}");
            let g = commit_with(&mut s, tt, &[uid.as_str()]);
            assert_eq!(g, tt as u64);
            let p = s.last_commit_phases().unwrap();
            let named_sum = p.capture_us
                + p.seal_us
                + p.closes_us
                + p.stats_us
                + p.dict_us
                + p.digest_us
                + p.debug_verify_us
                + p.delta_build_us
                + p.manifest_us
                + p.current_us;
            assert!(
                p.total_us >= named_sum,
                "commit {tt}: named phases ({named_sum}us) exceed total_us \
                 ({}us) -- a phase is double-counting another's window",
                p.total_us
            );
            let residual = p.total_us - named_sum;
            residuals.push(residual);
            totals.push(p.total_us);
            commits.push((tt, p.total_us, named_sum, residual));
        }

        let bar = residual_bound(median(totals.clone()));
        let median_residual = median(residuals.clone());
        let p90_residual = p90(residuals.clone());
        let first_ten_median = median(residuals[..10].to_vec());
        let last_ten_median = median(residuals[residuals.len() - 10..].to_vec());

        assert!(
            median_residual <= bar,
            "median residual over 60 commits ({median_residual}us) exceeds \
             bar={bar}us -- a systematic accounting leak, not a one-off \
             scheduler hiccup (worst three: {})",
            worst_three(&commits)
        );
        assert!(
            p90_residual <= 3 * bar,
            "p90 residual over 60 commits ({p90_residual}us) exceeds \
             3*bar={}us -- too many commits are elevated for this to be a \
             single scheduler hiccup (worst three: {})",
            3 * bar,
            worst_three(&commits)
        );
        assert!(
            last_ten_median <= first_ten_median + bar,
            "median residual grew from {first_ten_median}us (first 10 commits) \
             to {last_ten_median}us (last 10 commits), more than bar={bar}us \
             -- consistent with an O(segments) leak, not scheduler noise \
             (worst three: {})",
            worst_three(&commits)
        );
    }

    #[test]
    fn a_reconstructed_generation_equals_the_full_document_for_the_same_ops() {
        // the load-bearing equivalence: replaying deltas must land on exactly
        // the manifest a checkpoint at that generation would have held
        let root = tmp_root("chain-equals-full");
        let mut s = NativeStore::open(&root).unwrap();
        s.set_checkpoint_every(Some(1_000_000)); // deltas all the way down
        {
            let mut rows = Vec::new();
            for tt in 1..=6i64 {
                s.begin(tt * 10).unwrap();
                let a = s.ensure_entity(&format!("n{tt}"), "Node").unwrap();
                let r = edge_row(a, a, tt, tt * 10, tt as u32);
                rows.push(r.clone());
                s.stage_edge(r).unwrap();
                if tt == 4 {
                    s.close_version(RowKind::Edge, rows[0].vid, tt * 10).unwrap();
                }
                s.commit(EventLogRef::default()).unwrap();
            }
            // every generation is a delta except genesis
            assert_eq!(s.checkpoint_generation(), 0);
            for g in 1..=6 {
                assert!(!record_of(&root, g).is_checkpoint());
            }

            let resolved = manifest_chain::reconstruct(&root, 6).unwrap();
            assert_eq!(resolved.checkpoint, 0);
            assert_eq!(resolved.deltas, 6);
            assert_eq!(&resolved.manifest, s.manifest());
            // and the sha is the one CURRENT names, computed the format-1 way
            assert_eq!(resolved.manifest.digest(), s.manifest().manifest_sha);
            assert_eq!(
                fs::read_to_string(root.join(CURRENT)).unwrap().trim(),
                format!("6 {}", s.manifest().manifest_sha)
            );

            drop(s);
            let re = NativeStore::open(&root).unwrap();
            assert_eq!(re.generation(), 6);
            assert_eq!(re.all_edge_versions().unwrap().len(), 6);
            let report = re.verify().unwrap();
            assert!(report.is_healthy(), "{:?}", report.problems);
            assert_eq!(report.manifest_deltas, 6);
            assert_eq!(report.manifest_format, crate::MANIFEST_FORMAT_VERSION);
        }
    }

    #[test]
    fn a_break_anywhere_in_the_chain_is_corruption() {
        let root = tmp_root("chain-break");
        let mut s = NativeStore::open(&root).unwrap();
        s.set_checkpoint_every(Some(1_000_000)); // deltas all the way down
        {
            for tt in 1..=4i64 {
                commit_with(&mut s, tt * 10, &[&format!("n{tt}")]);
            }
            drop(s);
            let intact: Vec<String> = (0..=4)
                .map(|g| fs::read_to_string(NativeStore::manifest_path(&root, g)).unwrap())
                .collect();
            let restore = || {
                for (g, text) in intact.iter().enumerate() {
                    fs::write(NativeStore::manifest_path(&root, g as u64), text).unwrap();
                }
            };

            // (a) a gap: generation 2 is gone
            fs::remove_file(NativeStore::manifest_path(&root, 2)).unwrap();
            let err = open_err(&root);
            assert_eq!(err.category, crate::error::Category::Corrupt);
            restore();

            // (b) a reordered chain: generation 2 is replaced by 3's record,
            //     so the parent link and the running sha disagree
            fs::write(NativeStore::manifest_path(&root, 2), &intact[3]).unwrap();
            let err = open_err(&root);
            assert_eq!(err.category, crate::error::Category::Corrupt);
            restore();

            // (c) a tampered parent_sha, resealed so delta_sha still passes:
            //     the chain check has to be the thing that catches it
            let mut d: crate::manifest_chain::ManifestDelta =
                serde_json::from_str(&intact[3]).unwrap();
            d.parent_sha = "dead0000dead0000".into();
            d.seal();
            d.verify_self().unwrap();
            fs::write(NativeStore::manifest_path(&root, 3), d.to_json()).unwrap();
            let err = open_err(&root);
            assert_eq!(err.category, crate::error::Category::Corrupt);
            assert!(
                err.message.contains("chain broken"),
                "unhelpful message: {}",
                err.message
            );
            restore();

            // (d) a flipped byte inside a delta: the record's own sha
            let poisoned = intact[3].replace("\"next_segment_id\": 0", "\"next_segment_id\": 7");
            assert_ne!(poisoned, intact[3]);
            fs::write(NativeStore::manifest_path(&root, 3), poisoned).unwrap();
            assert_eq!(
                open_err(&root).category,
                crate::error::Category::Corrupt
            );
            restore();

            // and after every restore the store is healthy again
            assert!(NativeStore::open(&root).unwrap().verify().unwrap().is_healthy());
        }
    }

    #[test]
    fn compaction_always_emits_a_checkpoint() {
        let root = tmp_root("chain-compact");
        let mut s = NativeStore::open(&root).unwrap();
        s.set_checkpoint_every(Some(1_000_000)); // deltas all the way down
        {
            for tt in 1..=3i64 {
                s.begin(tt * 10).unwrap();
                let a = s.ensure_entity("n1", "Node").unwrap();
                s.stage_edge(edge_row(a, a, tt, tt * 10, tt as u32)).unwrap();
                s.commit(EventLogRef::default()).unwrap();
            }
            assert!(!record_of(&root, 3).is_checkpoint());
            s.compact().unwrap();
            let g = s.generation();
            assert!(
                record_of(&root, g).is_checkpoint(),
                "compaction replaces the whole segment list; the delta would \
                 be no smaller, and the chain should reset here"
            );
            assert_eq!(s.checkpoint_generation(), g);
            assert!(s.verify().unwrap().is_healthy());
        }
    }

    #[test]
    fn a_format_1_store_opens_read_only_and_refuses_writes_with_a_remedy() {
        let root = tmp_root("legacy-open");
        // a store exactly as the pre-change engine left it: one untagged,
        // format-1 document per generation
        fs::create_dir_all(root.join("manifests")).unwrap();
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub)).unwrap();
        }
        let mut legacy = Manifest::genesis();
        legacy.format = crate::FORMAT_LEGACY;
        legacy.seal();
        write_atomic(
            &NativeStore::manifest_path(&root, 0),
            &serde_json::to_string_pretty(&legacy).unwrap(),
        )
        .unwrap();
        write_atomic(
            &root.join(CURRENT),
            &format!("0 {}\n", legacy.manifest_sha),
        )
        .unwrap();

        let mut s = NativeStore::open(&root).unwrap();
        assert_eq!(s.manifest_format(), crate::FORMAT_LEGACY);
        assert!(s.verify().unwrap().is_healthy(), "reads must still work");

        s.begin(10).unwrap();
        s.ensure_entity("n1", "Node").unwrap();
        let err = s.commit(EventLogRef::default()).unwrap_err();
        assert_eq!(err.category, crate::error::Category::Invariant);
        assert!(err.message.contains("read-only"), "{}", err.message);
        assert!(
            err.remedy
                .as_deref()
                .unwrap_or_default()
                .contains("upgrade-manifests"),
            "a refusal without a remedy is a dead end: {:?}",
            err.remedy
        );
        s.rollback().unwrap();
        assert!(s.gc(2).is_err(), "gc is a write path too");

        // the upgrade converts it, and then writing works
        let report = s.upgrade_manifests().unwrap();
        assert!(report.upgraded);
        assert_eq!(report.from_format, crate::FORMAT_LEGACY);
        assert_eq!(s.manifest_format(), crate::MANIFEST_FORMAT_VERSION);
        assert!(record_of(&root, s.generation()).is_checkpoint());
        assert!(s.verify().unwrap().is_healthy());
        // idempotent
        assert!(!s.upgrade_manifests().unwrap().upgraded);
        commit_with(&mut s, 20, &["n1"]);
        drop(s);
        assert_eq!(NativeStore::open(&root).unwrap().dict().len(), 1);
    }

    #[test]
    fn the_upgrade_preserves_the_generation_content_verbatim() {
        let root = tmp_root("legacy-upgrade");
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub)).unwrap();
        }
        // build a real store, then rewrite its head as a format-1 document —
        // the same logical content the old engine would have written
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(10).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        s.stage_edge(edge_row(a, a, 1, 10, 0)).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        let content = s.manifest().clone();
        let before = s.verify().unwrap();
        drop(s);

        let mut legacy = content.clone();
        legacy.format = crate::FORMAT_LEGACY;
        legacy.seal();
        write_atomic(
            &NativeStore::manifest_path(&root, legacy.generation),
            &serde_json::to_string_pretty(&legacy).unwrap(),
        )
        .unwrap();
        write_atomic(
            &root.join(CURRENT),
            &format!("{} {}\n", legacy.generation, legacy.manifest_sha),
        )
        .unwrap();

        let mut s = NativeStore::open(&root).unwrap();
        assert_eq!(s.manifest_format(), crate::FORMAT_LEGACY);
        s.upgrade_manifests().unwrap();
        let after = s.verify().unwrap();

        // everything the report says about the *data* is identical; only the
        // generation counter, the format, and the chain fields move
        assert_eq!(after.segments_checked, before.segments_checked);
        assert_eq!(after.rows, before.rows);
        assert_eq!(after.closes, before.closes);
        assert_eq!(after.dict_records, before.dict_records);
        assert_eq!(after.problems, before.problems);
        assert_eq!(after.manifest_format, crate::MANIFEST_FORMAT_VERSION);
        assert_eq!(after.manifest_deltas, 0);
        let m = s.manifest();
        assert_eq!(m.node_store, content.node_store);
        assert_eq!(m.edge_lanes, content.edge_lanes);
        assert_eq!(m.close_runs, content.close_runs);
        assert_eq!(m.stats, content.stats);
        assert_eq!(m.dict, content.dict);
        assert_eq!(m.next_segment_id, content.next_segment_id);
        assert_eq!(m.event_log, content.event_log);
        assert_eq!(s.all_edge_versions().unwrap().len(), 1);
    }

    #[test]
    fn a_format_2_store_opens_read_only_and_upgrades_to_format_3() {
        // format 3 changes only what `manifest_sha` *is*, so a format-2 chain
        // must keep verifying under its own rule and refuse every write until
        // it is converted — the same promise format 1 got, one format later
        let root = tmp_root("format2-upgrade");
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub)).unwrap();
        }
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(10).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        s.stage_edge(edge_row(a, a, 1, 10, 0)).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        let content = s.manifest().clone();
        let before = s.verify().unwrap();
        drop(s);

        // the head as the previous engine wrote it: format 2, tagged
        // checkpoint, `manifest_sha` the sha of the whole document
        let mut legacy = content.clone();
        legacy.format = 2;
        legacy.seal();
        assert_ne!(
            legacy.manifest_sha, content.manifest_sha,
            "the same content under the two rules must not share a digest"
        );
        write_atomic(
            &NativeStore::manifest_path(&root, legacy.generation),
            &manifest_chain::checkpoint_json(&legacy),
        )
        .unwrap();
        write_atomic(
            &root.join(CURRENT),
            &format!("{} {}\n", legacy.generation, legacy.manifest_sha),
        )
        .unwrap();

        let mut s = NativeStore::open(&root).unwrap();
        assert_eq!(s.manifest_format(), 2);
        assert!(s.merkle.is_none(), "a read-only store maintains no state");
        // reads and verification work
        let report = s.verify().unwrap();
        assert!(report.is_healthy(), "{:?}", report.problems);
        assert_eq!(report.manifest_format, 2);
        assert_eq!(s.all_edge_versions().unwrap().len(), 1);
        // writes do not
        let err = s.gc(1).unwrap_err();
        assert_eq!(err.category, crate::error::Category::Invariant);
        assert!(err.message.contains("format-2"), "{}", err.message);
        assert!(err
            .remedy
            .as_deref()
            .unwrap_or_default()
            .contains("upgrade-manifests"));

        let up = s.upgrade_manifests().unwrap();
        assert!(up.upgraded);
        assert_eq!(up.from_format, 2);
        assert_eq!(s.manifest_format(), crate::MANIFEST_FORMAT_VERSION);
        assert!(s.merkle.is_some());
        let after = s.verify_full().unwrap();
        assert!(after.is_healthy(), "{:?}", after.problems);
        assert_eq!(after.segments_checked, before.segments_checked);
        assert_eq!(after.rows, before.rows);
        assert_eq!(after.manifest_deltas, 0);
        assert_eq!(s.manifest().node_store, content.node_store);
        assert_eq!(s.manifest().edge_lanes, content.edge_lanes);
        assert_eq!(s.all_edge_versions().unwrap().len(), 1);
        // and writing works now
        commit_with(&mut s, 30, &["n2"]);
        assert!(s.verify_full().unwrap().is_healthy());
    }

    #[test]
    fn verify_full_re_derives_the_published_digest_from_scratch() {
        // memo §4(d): an incrementally maintained digest could be wrong and
        // self-consistent. The oracle is a recomputation that shares no code
        // with the incremental path, crossed against what CURRENT publishes.
        let root = tmp_root("digest-oracle");
        let mut s = NativeStore::open(&root).unwrap();
        for g in 1..=4i64 {
            commit_with(&mut s, g * 10, &[&format!("n{g}")]);
        }
        let clean = s.verify_full().unwrap();
        assert!(clean.is_healthy(), "{:?}", clean.problems);

        // stand in for a bad incremental update: the value published is not
        // the value a from-scratch recomputation of this generation gives
        write_atomic(
            &root.join(CURRENT),
            &format!("{} 0000000000000000\n", s.generation()),
        )
        .unwrap();
        let report = s.verify_full().unwrap();
        assert!(!report.is_healthy());
        assert!(
            report
                .findings
                .iter()
                .any(|f| f.kind == "digest-oracle-mismatch"),
            "{:?}",
            report.problems
        );
        // the fast pass does not make this check — it is the price of an
        // incremental digest, paid in the mode nobody runs per commit
        assert!(s.verify().unwrap().is_healthy());
    }

    #[test]
    fn crash_after_dict_append_before_manifest_leaves_the_tail_invisible() {
        let root = tmp_root("crash-dict");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        let committed_bytes = s.manifest().dict.bytes;
        drop(s);

        // simulate: a later batch appended to dict.log and died pre-manifest
        let mut d = Dictionary::open(root.join(DICT), 1, committed_bytes).unwrap();
        d.ensure("orphan", "Node").unwrap();
        d.commit_to_disk().unwrap();
        let orphaned_len = fs::metadata(root.join(DICT)).unwrap().len();
        assert!(orphaned_len > committed_bytes);

        // the orphan is invisible: the manifest's byte count is the only
        // authority on what the dictionary contains
        let mut re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 1);
        assert_eq!(re.dict().len(), 1);
        assert_eq!(re.dict().dense_id("orphan"), None);

        // and the next commit reclaims those bytes by overwriting them —
        // open does not truncate, because open must not mutate the store
        // (a *live* writer's tail looks identical to a dead one's)
        commit_with(&mut re, 20, &["n2"]);
        assert_eq!(
            fs::metadata(root.join(DICT)).unwrap().len(),
            re.manifest().dict.bytes,
            "the writer rewrites from its committed offset and trims the rest"
        );
        assert_eq!(re.dict().dense_id("orphan"), None);
        assert_eq!(re.dict().dense_id("n2"), Some(1));
        drop(re);
        assert_eq!(NativeStore::open(&root).unwrap().dict().len(), 2);
    }

    /// Opening a store must not mutate it.
    ///
    /// The single writer spends a window inside every commit between step 3
    /// (the dictionary tail is fsynced) and step 5 (`CURRENT` flips). In that
    /// window `dict.log` is longer than the published generation claims —
    /// byte-for-byte indistinguishable from the orphaned tail a crashed batch
    /// leaves behind. A reader process that opens there and "cleans up" the
    /// tail destroys bytes the writer has already made durable and is about
    /// to name: the generation the writer then publishes is unreadable, so a
    /// commit that returned successfully has lost its durability guarantee to
    /// an unrelated reader. Lessons §6: every mutation must be scoped, and
    /// the cheapest scoping is not mutating at all.
    #[test]
    fn a_reader_opening_mid_commit_cannot_brick_the_generation_being_published() {
        let root = tmp_root("open-midcommit");
        let mut w = NativeStore::open(&root).unwrap();
        commit_with(&mut w, 10, &["n1"]);
        let base = w.manifest().dict.bytes;

        // the writer is inside commit(): step 3 has fsynced the tail for the
        // generation it is about to publish; steps 4-5 have not run
        let mut tail = Dictionary::open(root.join(DICT), 1, base).unwrap();
        tail.ensure("n2", "Node").unwrap();
        let (records, bytes) = tail.commit_to_disk().unwrap();
        assert!(bytes > base);

        // a reader process opens the store in exactly that window
        let reader = NativeStore::open(&root).unwrap();
        assert_eq!(reader.generation(), 1);
        assert_eq!(reader.dict().len(), 1, "a reader sees its own generation");
        drop(reader);
        assert_eq!(
            fs::metadata(root.join(DICT)).unwrap().len(),
            bytes,
            "a reader deleted a live writer's fsynced dictionary tail"
        );

        // steps 4-5 complete: what the writer promised is durable
        let mut next = w.manifest().successor(20);
        next.dict.records = records;
        next.dict.bytes = bytes;
        next.seal();
        let span = AppendSpan::of(w.manifest());
        NativeStore::publish(&root, Some((&span, 0)), &next, false, 512).unwrap();

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 2);
        assert_eq!(re.dict().len(), 2);
        assert_eq!(re.dict().dense_id("n2"), Some(1));
    }

    #[test]
    fn current_pointing_at_a_missing_manifest_is_corruption() {
        let root = tmp_root("missing-manifest");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        drop(s);
        fs::remove_file(NativeStore::manifest_path(&root, 1)).unwrap();

        let err = match NativeStore::open(&root) {
            Ok(_) => panic!("must not open with a dangling CURRENT"),
            Err(e) => e,
        };
        assert_eq!(err.category, crate::error::Category::Corrupt);
        assert!(err.remedy.is_some(), "corruption must name a remedy");
    }

    #[test]
    fn tampered_manifest_is_rejected() {
        let root = tmp_root("tampered");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        drop(s);

        let path = NativeStore::manifest_path(&root, 1);
        let text = fs::read_to_string(&path).unwrap();
        fs::write(&path, text.replace("\"n_entities\": 1", "\"n_entities\": 4")).unwrap();

        assert!(NativeStore::open(&root).is_err(), "checksum must catch edits");
    }

    #[test]
    fn malformed_current_is_corruption() {
        let root = tmp_root("bad-current");
        NativeStore::open(&root).unwrap();
        fs::write(root.join(CURRENT), "garbage\n").unwrap();
        let err = match NativeStore::open(&root) {
            Ok(_) => panic!("malformed CURRENT must not open"),
            Err(e) => e,
        };
        assert_eq!(err.category, crate::error::Category::Corrupt);
    }

    // --- byte-budget segment cache (D-041) ------------------------------ //

    #[test]
    fn cache_budget_resolution() {
        // explicit override wins, 0 means unbounded, garbage falls through
        assert_eq!(cache_budget(Some("1048576"), Some(1 << 33)), Some(1 << 20));
        assert_eq!(cache_budget(Some("0"), Some(1 << 33)), None);
        assert_eq!(cache_budget(Some(" 42 "), None), Some(42));
        assert_eq!(cache_budget(Some("lots"), Some(1 << 33)), Some(1 << 32));
        assert_eq!(cache_budget(Some(""), None), None);
        // default: half of detected RAM, unbounded where undetectable
        assert_eq!(cache_budget(None, Some(1 << 33)), Some(1 << 32));
        assert_eq!(cache_budget(None, None), None);
    }

    /// Results must be byte-identical under any budget: an evicted segment
    /// reopens transparently, and the checksum walk stays once-per-session.
    #[test]
    fn tiny_cache_budget_changes_memory_not_answers() {
        let root = tmp_root("cache-budget");
        let mut s = NativeStore::open(&root).unwrap();
        for batch in 0..6u32 {
            let tt = 100 + batch as i64;
            s.begin(tt).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            let b = s.ensure_entity("n2", "Node").unwrap();
            for i in 0..50u32 {
                s.stage_edge(edge_row(a, b, (batch * 50 + i) as i64, tt, batch * 50 + i))
                    .unwrap();
            }
            s.commit(EventLogRef::default()).unwrap();
        }
        assert!(s.manifest().edge_lanes.event.len() >= 6);

        let unbounded: Vec<_> = s.all_edge_versions().unwrap();
        let (entries, bytes, _, evictions) = s.segment_cache_stats();
        assert_eq!(entries, 6, "unbounded: every touched segment stays");
        assert!(bytes > 0);
        assert_eq!(evictions, 0);

        // one segment's worth of budget: the walk must evict as it goes
        let one = s
            .open_segment(&s.manifest().edge_lanes.event[0].file)
            .unwrap()
            .resident_bytes();
        s.set_segment_cache_budget(Some(one));
        let capped: Vec<_> = s.all_edge_versions().unwrap();
        assert_eq!(capped, unbounded, "answers must not depend on residency");

        let (entries, bytes, budget, evictions) = s.segment_cache_stats();
        assert!(evictions > 0, "a one-segment budget must have evicted");
        assert!(entries < 6, "the cache cannot hold every segment");
        assert!(
            bytes <= budget.unwrap() || entries == 1,
            "over budget with multiple entries: {bytes} of {budget:?}"
        );

        // and point reads through the postings still agree after evictions
        let eid = unbounded[0].eid.clone();
        let vids: Vec<String> = s
            .believed_edge_versions(&eid, OPEN_END)
            .unwrap()
            .iter()
            .map(|r| r.vid.clone())
            .collect();
        s.set_segment_cache_budget(None);
        let vids_unbounded: Vec<String> = s
            .believed_edge_versions(&eid, OPEN_END)
            .unwrap()
            .iter()
            .map(|r| r.vid.clone())
            .collect();
        assert_eq!(vids, vids_unbounded);
    }

    #[test]
    fn an_arc_held_across_an_eviction_stays_valid() {
        let root = tmp_root("cache-arc");
        let mut s = NativeStore::open(&root).unwrap();
        for batch in 0..3u32 {
            let tt = 100 + batch as i64;
            s.begin(tt).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            s.stage_edge(edge_row(a, a, batch as i64, tt, batch)).unwrap();
            s.commit(EventLogRef::default()).unwrap();
        }
        let file = s.manifest().edge_lanes.event[0].file.clone();
        let held = s.open_segment(&file).unwrap();
        let vt_before = held.i64_column("vt_s").unwrap().to_vec();

        s.set_segment_cache_budget(Some(1)); // evicts everything but the MRU
        for e in &s.manifest().edge_lanes.event.clone() {
            s.open_segment(&e.file).unwrap();
        }
        let (_, _, _, evictions) = s.segment_cache_stats();
        assert!(evictions > 0);
        // the reader's view is untouched: eviction drops the cache's
        // reference, never the data under a live Arc
        assert_eq!(held.i64_column("vt_s").unwrap(), &vt_before[..]);
    }

    #[test]
    fn batches_do_not_nest_and_time_must_advance() {
        let root = tmp_root("batch-rules");
        let mut s = NativeStore::open(&root).unwrap();
        commit_with(&mut s, 10, &["n1"]);
        s.begin(20).unwrap();
        assert!(s.begin(21).is_err(), "single-writer: no nested batches");
        s.rollback().unwrap();
        assert!(s.begin(10).is_err(), "tt must be strictly monotone");
        assert!(s.begin(11).is_ok());
    }
}
