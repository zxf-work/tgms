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
//!    fsynced;
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
use crate::manifest::{CloseRunRef, EventLogRef, Manifest};
use crate::row::{EdgeRow, Lane, NodeRow, RowKind};
use crate::segment::MmapSource;
use crate::staging::{PartitionMap, Staging};
use crate::visibility::{read_close_run, write_close_run, CloseIndex, CloseRecord};

const CURRENT: &str = "CURRENT";
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
    manifest: Manifest,
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
    /// Step 2: staged rows sealed into segment files, written and fsynced.
    pub seal_us: u64,
    /// Step 2b: the close run for corrections against committed rows.
    pub closes_us: u64,
    /// Folding this batch into the running statistics.
    pub stats_us: u64,
    /// Step 3: the dictionary tail, written and fsynced.
    pub dict_us: u64,
    /// Step 4: manifest serialized, written, fsynced, renamed, dir fsynced.
    pub manifest_us: u64,
    /// Step 5: `CURRENT` rewritten atomically — the publication point.
    pub current_us: u64,
    pub total_us: u64,
    /// Bytes of the manifest this commit wrote, and how many segments it
    /// names: a manifest is rewritten in full every generation, so both grow
    /// with store history rather than with the batch.
    pub manifest_bytes: u64,
    pub segments_named: u64,
}

impl NativeStore {
    pub fn open(root: impl Into<PathBuf>) -> Result<Self> {
        let root = root.into();
        for sub in SUBDIRS {
            fs::create_dir_all(root.join(sub))
                .map_err(|e| EngineError::from(e).at_file(root.join(sub)))?;
        }
        let manifest = if root.join(CURRENT).exists() {
            Self::load_current(&root)?
        } else {
            let genesis = Manifest::genesis();
            Self::publish(&root, &genesis)?;
            genesis
        };
        let dict = Dictionary::open(
            root.join(DICT),
            manifest.dict.records,
            manifest.dict.bytes,
        )?;
        let pin_key = fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
        crate::gc::pin(&pin_key, manifest.generation);
        let current_only = root.join(CURRENT_ONLY_MARKER).exists();
        Ok(Self {
            root,
            pin_key,
            dict,
            manifest,
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
        })
    }

    /// Where the last commit spent its time, if this handle has committed.
    pub fn last_commit_phases(&self) -> Option<CommitPhases> {
        self.last_commit
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

    fn load_current(root: &Path) -> Result<Manifest> {
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
        let raw = fs::read_to_string(&m_path).map_err(|e| {
            EngineError::corrupt(format!(
                "CURRENT points at generation {generation} but its manifest is unreadable: {e}"
            ))
            .at_file(&m_path)
        })?;
        let manifest = Manifest::from_json(&raw).map_err(|e| e.at_file(&m_path))?;
        if manifest.generation != generation {
            return Err(EngineError::corrupt(format!(
                "manifest says generation {} but is filed as {generation}",
                manifest.generation
            ))
            .at_file(&m_path));
        }
        if manifest.manifest_sha != sha {
            return Err(EngineError::corrupt(format!(
                "CURRENT records sha {sha} but manifest {generation} has {}",
                manifest.manifest_sha
            ))
            .at_file(&m_path));
        }
        Ok(manifest)
    }

    fn manifest_path(root: &Path, generation: u64) -> PathBuf {
        root.join("manifests").join(format!("{generation:020}.json"))
    }

    /// Steps 4 and 5: write the manifest, then flip `CURRENT`.
    ///
    /// Returns `(manifest_us, current_us, manifest_bytes)` — the split
    /// matters because step 4 rewrites the whole manifest every generation
    /// while step 5 writes forty bytes, and only one of those grows with
    /// store history.
    fn publish(root: &Path, manifest: &Manifest) -> Result<(u64, u64, u64)> {
        manifest.verify()?;
        let m_path = Self::manifest_path(root, manifest.generation);
        let json = manifest.to_json();
        let t = std::time::Instant::now();
        write_atomic(&m_path, &json)?;
        let manifest_us = t.elapsed().as_micros() as u64;
        let t = std::time::Instant::now();
        write_atomic(
            &root.join(CURRENT),
            &format!("{} {}\n", manifest.generation, manifest.manifest_sha),
        )?;
        Ok((manifest_us, t.elapsed().as_micros() as u64, json.len() as u64))
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
    fn adopt(&mut self, next: Manifest) {
        crate::gc::repin(&self.pin_key, self.manifest.generation, next.generation);
        self.manifest = next;
    }

    /// Drop cached segments gc just removed from disk. Sound because ids are
    /// never reused (`Manifest::next_segment_id`), so an evicted name can
    /// never come back meaning different bytes.
    pub(crate) fn evict_segments_not_in(&self, keep: &std::collections::HashSet<String>) {
        self.segments
            .lock()
            .expect("segment-cache mutex poisoned")
            .retain_files(keep);
        self.verified
            .lock()
            .expect("verified-set mutex poisoned")
            .retain(|file| keep.contains(file));
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
    pub(crate) fn install(&mut self, next: Manifest) -> Result<u64> {
        if self.in_batch() {
            return Err(EngineError::invariant(
                "cannot publish a generation while a batch is open",
            ));
        }
        Self::publish(&self.root, &next)?;
        self.adopt(next);
        Ok(self.manifest.generation)
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

    /// Walk every file this generation references, checking magic numbers,
    /// checksums, completion markers, and cross-references.
    ///
    /// Corruption must be *detected*, not merely survived: the durability
    /// objective is "never expose an undetected inconsistent generation"
    /// (blueprint §1). Problems are collected rather than raised on the first
    /// one, so an operator sees the whole picture instead of peeling the
    /// onion a file at a time.
    pub fn verify(&self) -> Result<VerifyReport> {
        let mut report = VerifyReport {
            generation: self.manifest.generation,
            ..Default::default()
        };
        self.manifest.verify()?;

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
                    if seg.rows() != claimed_rows {
                        report.problems.push(format!(
                            "{file}: manifest claims {claimed_rows} rows, segment holds {}",
                            seg.rows()
                        ));
                    }
                }
                Err(e) => report.problems.push(format!("{file}: {e}")),
            }
        }

        for run in &m.close_runs {
            let path = self.root.join(&run.file);
            match crate::visibility::read_close_run(&path) {
                Ok(records) => {
                    report.close_runs_checked += 1;
                    report.closes += records.len() as u64;
                    if records.len() as u32 != run.entries {
                        report.problems.push(format!(
                            "{}: manifest claims {} entries, file holds {}",
                            run.file,
                            run.entries,
                            records.len()
                        ));
                    }
                }
                Err(e) => report.problems.push(format!("{}: {e}", run.file)),
            }
        }

        report.dict_records = self.dict.len();
        if self.dict.len() != m.dict.records {
            report.problems.push(format!(
                "dictionary holds {} records, manifest claims {}",
                self.dict.len(),
                m.dict.records
            ));
        }
        Ok(report)
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
            RowKind::Edge => self.staging.edges().iter().any(|r| r.vid == vid),
            RowKind::Node => self.staging.nodes().iter().any(|r| r.vid == vid),
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

    /// Publish the open batch as a new generation. `event_log` records where
    /// in the (already durable) log this generation ends.
    pub fn commit(&mut self, event_log: EventLogRef) -> Result<u64> {
        let tt = self.require_batch()?;
        let mut phases = CommitPhases::default();
        let commit_start = std::time::Instant::now();
        let mut next = self.manifest.successor(tt);

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
                Lane::Event => next.edge_lanes.event.push(entry),
                Lane::Interval => next.edge_lanes.interval.push(entry),
            }
        }
        for entry in sealed.nodes {
            next.stats.n_node_versions += entry.rows as u64;
            next.node_store.push(entry);
        }
        phases.seal_us = phase.elapsed().as_micros() as u64;

        // close run for corrections landing on already-committed rows,
        // durable before the manifest that lists it
        let phase = std::time::Instant::now();
        if !self.pending_closes.is_empty() {
            let file = format!("close/{:012}.tgc", next.generation);
            let path = self.root.join(&file);
            let entries = write_close_run(&path, &self.pending_closes)?;
            next.close_runs.push(CloseRunRef {
                file,
                entries,
                sha: String::new(),
            });
        }
        phases.closes_us = phase.elapsed().as_micros() as u64;

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
        next.event_log = event_log;
        next.dict.records = records;
        next.dict.bytes = bytes;
        next.stats.n_entities = self.dict.len();
        next.seal();
        phases.segments_named = (next.edge_lanes.event.len()
            + next.edge_lanes.interval.len()
            + next.node_store.len()) as u64;

        // steps 4-5 — manifest, then CURRENT
        let (manifest_us, current_us, manifest_bytes) = Self::publish(&self.root, &next)?;
        phases.manifest_us = manifest_us;
        phases.current_us = current_us;
        phases.manifest_bytes = manifest_bytes;
        self.adopt(next);
        self.staging.clear();
        self.staged_closes.clear();
        self.pending_closes.clear();
        self.pending_overlay = None;
        self.batch_tt = None;
        phases.total_us = commit_start.elapsed().as_micros() as u64;
        self.last_commit = Some(phases);
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
    /// Compaction needs no special handling: its fresh segments are indexed
    /// like any other, and entries naming segments the manifest no longer
    /// lists are dropped by the same prune.
    pub(crate) open_rows: std::collections::HashMap<u64, Vec<(u64, u32)>>,
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
    pub segments_checked: u32,
    pub close_runs_checked: u32,
    pub rows: u64,
    pub closes: u64,
    pub dict_records: u32,
    pub problems: Vec<String>,
}

impl VerifyReport {
    pub fn is_healthy(&self) -> bool {
        self.problems.is_empty()
    }
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

        // simulate: generation 2's manifest reached disk, CURRENT did not
        let g1 = Manifest::from_json(
            &fs::read_to_string(NativeStore::manifest_path(&root, 1)).unwrap(),
        )
        .unwrap();
        let mut g2 = g1.successor(20);
        g2.stats.n_entities = 99;
        g2.seal();
        write_atomic(&NativeStore::manifest_path(&root, 2), &g2.to_json()).unwrap();

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 1, "orphaned manifest must not be adopted");
        assert_eq!(re.dict().len(), 1);
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
        NativeStore::publish(&root, &next).unwrap();

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
