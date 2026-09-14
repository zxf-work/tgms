//! The on-disk manifest chain: format-2 checkpoints and deltas.
//!
//! Format 1 wrote the whole logical manifest every generation. A manifest
//! names every live segment, so generation *G* of a store with one segment
//! per batch wrote *G* segment entries and retained Θ(G²) bytes — measured at
//! 25,451 MB of manifests against 163 MB of segments at 2.5M SNB node ops
//! (`docs/design/INCREMENTAL_MANIFEST_FORECAST_2026-09-13.md` §1). Worse, the
//! clone-serialize-sha per commit is O(segments), which is the only part of a
//! commit a longer-lived store pays more for.
//!
//! Format 2 keeps the same file-per-generation layout — one `cat`-able JSON
//! document at `manifests/<G:020>.json`, `CURRENT` flipped last — and changes
//! only what each document *contains*:
//!
//! - a **checkpoint** is the whole logical manifest, tagged `"kind":
//!   "checkpoint"`. Written at generation 0, every `K`th generation
//!   (`TGMS_MANIFEST_CHECKPOINT_EVERY`, default
//!   [`crate::defaults::MANIFEST_CHECKPOINT_EVERY`]), by compaction, by the
//!   gc floor, and by the format-1 upgrade;
//! - a **delta** records only what changed against its parent.
//!
//! **Three hashes, and what each is for.** `delta_sha` makes a delta record
//! self-checking on disk, exactly as `manifest_sha` does for a full document.
//! `parent_sha` extends the hash chain across generations, which format 1 did
//! not have. `manifest_sha` is the sha of the **reconstructed** manifest — the
//! digest of this generation's full logical content — so `CURRENT`'s
//! `"<generation> <sha>"` line, its check in `store::load_current`, the pyo3
//! accessor, and the TCSR stamp all keep their format-1 meaning. Checkpoint or
//! delta is a storage detail: the same content has the same `manifest_sha`
//! either way.
//!
//! **Reconstruction walks forwards, and falls back to walking backwards.**
//! §4 of the memo resolves the base checkpoint through the head record's
//! `checkpoint` field and replays `C+1..=G`; that is the fast path here. It
//! can be stale in exactly one way — gc materializes a fresh checkpoint at
//! its retention floor and deletes below it (memo §4, "gc with deltas"),
//! after which the deltas above the floor still name a checkpoint that is
//! gone — so *any* failure of the forward attempt retries the backward walk
//! from `G` down to the nearest checkpoint on disk, which is the authority
//! and produces every error message this module has ever produced. The two
//! resolve the same base and read the same files whenever the field's target
//! is on disk, which is every case the writer creates.
//!
//! **Format 3: the digest, not the document.** A format-3 record is a
//! format-2 record with `sha_kind: "merkle-v1"` on it; what changed is that
//! `manifest_sha` is now a Merkle root over the ordered segment set
//! (`manifest::merkle`). Replay therefore no longer re-serializes and
//! re-hashes the whole reconstructed manifest at every step — the measured
//! 26.9 ms per delta of the V2 diagnosis §1 — but carries a Merkle state
//! forward and re-derives each generation's digest from it in O(1). Format-2
//! chains still replay, under their own whole-document rule, checked once at
//! the head rather than at every step; they are read-only either way.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{EngineError, Result};
use crate::manifest::{
    merkle, read_only_list, short_sha, CloseRunRef, DictRef, EventLogRef, Manifest, SegmentEntry,
    Stats,
};
use crate::{
    FORMAT_LEGACY, MANIFEST_FORMATS_READ_ONLY, MANIFEST_FORMAT_MERKLE, MANIFEST_FORMAT_VERSION,
};

/// Per-store override for the checkpoint interval, so the A/B can sweep `K`
/// without a rebuild (memo §4, "Choosing K").
pub const CHECKPOINT_EVERY_ENV: &str = "TGMS_MANIFEST_CHECKPOINT_EVERY";

/// The checkpoint interval in force. Garbage and zero fall back to the
/// default rather than erroring: a tuning knob must never make a store fail
/// to open, and 0 would mean "never checkpoint", which no store should get by
/// typo.
pub fn checkpoint_every() -> u64 {
    std::env::var(CHECKPOINT_EVERY_ENV)
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|k| *k > 0)
        .unwrap_or(crate::defaults::MANIFEST_CHECKPOINT_EVERY)
}

/// `manifests/<G:020>.json` — unchanged from format 1.
pub fn manifest_path(root: &Path, generation: u64) -> PathBuf {
    root.join("manifests").join(format!("{generation:020}.json"))
}

/// What one generation's file changed relative to its parent.
///
/// `widths` is deliberately absent: it lives only in checkpoints, so a
/// widening is a format bump by construction (`manifest.rs`, `Widths`).
/// Segment and close-run deletions are by file name — ids are never reused
/// (`Manifest::next_segment_id`), so a name is a stable identity.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct ManifestDelta {
    pub format: u32,
    pub kind: String,
    /// Which rule produced `manifest_sha`: `"merkle-v1"` at format 3, absent
    /// below it. Derivable from `format`, and written anyway so a record an
    /// operator is reading during an incident says which rule it was sealed
    /// under. Skipped when empty, so a format-2 record parsed by this build
    /// re-serializes to the bytes it was written with and its `delta_sha`
    /// still checks out.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub sha_kind: String,
    pub generation: u64,
    pub parent: u64,
    /// The parent generation's `manifest_sha` — the cross-generation chain.
    pub parent_sha: String,
    /// The last checkpoint generation at or below `generation` when this
    /// record was written. A hint and a lower bound, not the sole authority:
    /// see the module docs.
    pub checkpoint: u64,
    pub created_tt: i64,
    pub event_log: EventLogRef,
    pub dict: DictRef,
    pub next_segment_id: u64,
    pub stats: Stats,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub add_nodes: Vec<SegmentEntry>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub del_nodes: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub add_edges_event: Vec<SegmentEntry>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub del_edges_event: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub add_edges_interval: Vec<SegmentEntry>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub del_edges_interval: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub add_close_runs: Vec<CloseRunRef>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub del_close_runs: Vec<String>,
    /// Self-sha of this record with this field blanked. Covers `manifest_sha`,
    /// so a forged reconstruction digest is caught here too.
    pub delta_sha: String,
    /// Sha of the manifest this record reconstructs — same value a checkpoint
    /// of the same content would carry.
    pub manifest_sha: String,
}

fn seg_name(e: &SegmentEntry) -> &str {
    &e.file
}

fn run_name(r: &CloseRunRef) -> &str {
    &r.file
}

/// Names in `parent` that `child` no longer lists.
///
/// Everything here borrows the names rather than copying them: the commit
/// path runs this once per lane per commit, and the whole point of the change
/// is that a commit stops paying per live segment. The only allocations are
/// the `del` names actually returned, which is empty on every path except
/// compaction — and compaction writes a checkpoint instead.
fn removed_names<T>(
    parent: &[T],
    child: &[T],
    name: impl for<'a> Fn(&'a T) -> &'a str,
) -> Vec<String> {
    if parent.is_empty() {
        return Vec::new();
    }
    let keep: std::collections::HashSet<&str> = child.iter().map(&name).collect();
    parent
        .iter()
        .map(&name)
        .filter(|f| !keep.contains(f))
        .map(str::to_string)
        .collect()
}

/// Entries `child` lists that `parent` did not.
fn added_entries<T: Clone>(
    parent: &[T],
    child: &[T],
    name: impl for<'a> Fn(&'a T) -> &'a str,
) -> Vec<T> {
    if parent.is_empty() {
        return child.to_vec();
    }
    let had: std::collections::HashSet<&str> = parent.iter().map(&name).collect();
    child
        .iter()
        .filter(|e| !had.contains(name(e)))
        .cloned()
        .collect()
}

/// Would `parent` minus `del` plus `add` be exactly `child`?
///
/// The validation `between` runs before it will offer a delta. Deliberately
/// not "clone the parent, patch it, compare": that would put an O(segments)
/// deep clone back on the commit path this change exists to flatten. This
/// walks the three sequences in lockstep and allocates nothing beyond the
/// deletion set, which is empty on the commit path.
fn patch_matches<T: PartialEq>(
    parent: &[T],
    del: &[String],
    add: &[T],
    child: &[T],
    name: impl for<'a> Fn(&'a T) -> &'a str,
) -> bool {
    if child.len() + del.len() != parent.len() + add.len() {
        return false;
    }
    let gone: std::collections::HashSet<&str> = del.iter().map(String::as_str).collect();
    let mut want = child.iter();
    let survivors = parent.iter().filter(|e| !gone.contains(name(e)));
    for e in survivors.chain(add.iter()) {
        if want.next() != Some(e) {
            return false;
        }
    }
    want.next().is_none()
}

/// Apply one list diff: drop the named entries, then append the new ones.
fn patch<T: Clone>(
    list: &mut Vec<T>,
    del: &[String],
    add: &[T],
    name: impl for<'a> Fn(&'a T) -> &'a str,
) {
    if !del.is_empty() {
        let gone: std::collections::HashSet<&str> = del.iter().map(String::as_str).collect();
        list.retain(|e| !gone.contains(name(e)));
    }
    list.extend(add.iter().cloned());
}

impl ManifestDelta {
    /// The delta that turns `parent` into `child`, or `None` if this pair
    /// cannot be expressed as one.
    ///
    /// The `None` cases are not failures — they are the caller's signal to
    /// write a checkpoint instead. Crucially, the delta is *validated before
    /// it is returned*: whatever the name diff does, replaying it must
    /// reproduce `child` exactly or no delta is offered. That is what makes an
    /// entry whose contents changed in place (rather than being added or
    /// removed), or a reordering, structurally safe instead of a silent
    /// divergence. The validation walks the sequences in lockstep rather than
    /// cloning and comparing, because a deep clone per commit is exactly the
    /// O(segments) cost this change exists to remove.
    pub fn between(parent: &Manifest, child: &Manifest, checkpoint: u64) -> Option<Self> {
        if parent.format != child.format
            || child.format != MANIFEST_FORMAT_VERSION
            || parent.widths != child.widths
            || child.parent != Some(parent.generation)
            || child.generation != parent.generation.checked_add(1)?
        {
            return None;
        }
        let (seg, run) = (seg_name, run_name);
        let mut d = Self {
            format: MANIFEST_FORMAT_VERSION,
            kind: "delta".into(),
            sha_kind: merkle::SHA_KIND.into(),
            generation: child.generation,
            parent: parent.generation,
            parent_sha: parent.manifest_sha.clone(),
            checkpoint,
            created_tt: child.created_tt,
            event_log: child.event_log.clone(),
            dict: child.dict.clone(),
            next_segment_id: child.next_segment_id,
            stats: child.stats.clone(),
            add_nodes: added_entries(&parent.node_store, &child.node_store, seg),
            del_nodes: removed_names(&parent.node_store, &child.node_store, seg),
            add_edges_event: added_entries(&parent.edge_lanes.event, &child.edge_lanes.event, seg),
            del_edges_event: removed_names(&parent.edge_lanes.event, &child.edge_lanes.event, seg),
            add_edges_interval: added_entries(
                &parent.edge_lanes.interval,
                &child.edge_lanes.interval,
                seg,
            ),
            del_edges_interval: removed_names(
                &parent.edge_lanes.interval,
                &child.edge_lanes.interval,
                seg,
            ),
            add_close_runs: added_entries(&parent.close_runs, &child.close_runs, run),
            del_close_runs: removed_names(&parent.close_runs, &child.close_runs, run),
            delta_sha: String::new(),
            manifest_sha: child.manifest_sha.clone(),
        };
        // Every scalar field is copied from `child` above, so `apply` sets
        // them to child's values by construction; `format`, `widths`,
        // `generation` and `parent` were checked at the top. That leaves the
        // four lists, and these are what could silently differ.
        if !patch_matches(
                &parent.node_store,
                &d.del_nodes,
                &d.add_nodes,
                &child.node_store,
                seg,
            )
            || !patch_matches(
                &parent.edge_lanes.event,
                &d.del_edges_event,
                &d.add_edges_event,
                &child.edge_lanes.event,
                seg,
            )
            || !patch_matches(
                &parent.edge_lanes.interval,
                &d.del_edges_interval,
                &d.add_edges_interval,
                &child.edge_lanes.interval,
                seg,
            )
            || !patch_matches(
                &parent.close_runs,
                &d.del_close_runs,
                &d.add_close_runs,
                &child.close_runs,
                run,
            )
        {
            return None;
        }
        d.seal();
        Some(d)
    }

    /// Reconstruct this generation's manifest on top of its parent's.
    ///
    /// Convenience for callers that hold a parent they must not disturb;
    /// replay uses [`ManifestDelta::apply_into`], because the deep clone this
    /// makes is 50k allocations per step on a 10k-segment store and was a
    /// third of the measured 26.9 ms per delta (V2 diagnosis §1).
    pub fn apply(&self, base: &Manifest) -> Manifest {
        let mut m = base.clone();
        self.apply_into(&mut m, None);
        m
    }

    /// Advance `m` in place to this generation, keeping `state` in step.
    ///
    /// `state` is the Merkle state of `m` before the call and of `m` after
    /// it. Appends walk the right spine, O(added + log n). A **deletion**
    /// cannot be undone on a spine, so any lane this record deletes from is
    /// rebuilt wholesale — which costs nothing in practice, because the only
    /// writer that deletes is compaction and compaction publishes a
    /// checkpoint rather than a delta.
    pub fn apply_into(&self, m: &mut Manifest, state: Option<&mut merkle::ManifestMerkle>) {
        m.generation = self.generation;
        m.parent = Some(self.parent);
        m.created_tt = self.created_tt;
        m.event_log = self.event_log.clone();
        m.dict = self.dict.clone();
        m.next_segment_id = self.next_segment_id;
        m.stats = self.stats.clone();
        patch(&mut m.node_store, &self.del_nodes, &self.add_nodes, |e| {
            &e.file
        });
        patch(
            &mut m.edge_lanes.event,
            &self.del_edges_event,
            &self.add_edges_event,
            |e| &e.file,
        );
        patch(
            &mut m.edge_lanes.interval,
            &self.del_edges_interval,
            &self.add_edges_interval,
            |e| &e.file,
        );
        patch(
            &mut m.close_runs,
            &self.del_close_runs,
            &self.add_close_runs,
            |r| &r.file,
        );
        m.manifest_sha = self.manifest_sha.clone();

        if let Some(state) = state {
            if self.deletes_anything() {
                *state = merkle::ManifestMerkle::from_manifest(m);
            } else {
                for e in &self.add_nodes {
                    state.push_node(e);
                }
                for e in &self.add_edges_event {
                    state.push_edge_event(e);
                }
                for e in &self.add_edges_interval {
                    state.push_edge_interval(e);
                }
                for r in &self.add_close_runs {
                    state.push_close_run(r);
                }
            }
        }
    }

    fn deletes_anything(&self) -> bool {
        !self.del_nodes.is_empty()
            || !self.del_edges_event.is_empty()
            || !self.del_edges_interval.is_empty()
            || !self.del_close_runs.is_empty()
    }

    fn body_sha(&self) -> String {
        let mut blanked = self.clone();
        blanked.delta_sha = String::new();
        short_sha(&serde_json::to_string(&blanked).expect("delta is serializable"))
    }

    pub fn seal(&mut self) {
        self.delta_sha = self.body_sha();
    }

    /// Structural checks a delta must pass before it is trusted, without
    /// reference to any other record.
    pub fn verify_self(&self) -> Result<()> {
        if self.format != MANIFEST_FORMAT_VERSION
            && !MANIFEST_FORMATS_READ_ONLY.contains(&self.format)
        {
            return Err(EngineError::corrupt(format!(
                "manifest delta format {} is not supported by this build \
                 (expected {MANIFEST_FORMAT_VERSION}, or {} read-only)",
                self.format,
                read_only_list()
            )));
        }
        if self.kind != "delta" {
            return Err(EngineError::corrupt(format!(
                "manifest delta is tagged kind={:?}",
                self.kind
            )));
        }
        check_sha_kind(self.format, &self.sha_kind)?;
        let expected = self.body_sha();
        if expected != self.delta_sha {
            return Err(EngineError::corrupt(format!(
                "manifest delta checksum mismatch: computed {expected}, recorded {}",
                self.delta_sha
            )));
        }
        Ok(())
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).expect("delta is serializable")
    }
}

/// One generation's file, whichever shape it holds.
#[derive(Clone, Debug, PartialEq)]
pub enum ManifestRecord {
    /// A full manifest: a format-2 checkpoint, or a format-1 document.
    Checkpoint(Manifest),
    Delta(ManifestDelta),
}

impl ManifestRecord {
    pub fn generation(&self) -> u64 {
        match self {
            Self::Checkpoint(m) => m.generation,
            Self::Delta(d) => d.generation,
        }
    }

    pub fn manifest_sha(&self) -> &str {
        match self {
            Self::Checkpoint(m) => &m.manifest_sha,
            Self::Delta(d) => &d.manifest_sha,
        }
    }

    pub fn is_checkpoint(&self) -> bool {
        matches!(self, Self::Checkpoint(_))
    }

    /// Files this record *introduces*. For a checkpoint that is its whole
    /// segment set; for a delta, only its additions. gc's reference set is
    /// the union over every retained record, which is a superset of what any
    /// retained generation names — conservative in exactly the direction gc
    /// already argues for (`gc.rs`).
    pub fn referenced_files(&self) -> Vec<&str> {
        match self {
            Self::Checkpoint(m) => m
                .node_store
                .iter()
                .chain(m.edge_lanes.event.iter())
                .chain(m.edge_lanes.interval.iter())
                .map(|e| e.file.as_str())
                .chain(m.close_runs.iter().map(|r| r.file.as_str()))
                .collect(),
            Self::Delta(d) => d
                .add_nodes
                .iter()
                .chain(d.add_edges_event.iter())
                .chain(d.add_edges_interval.iter())
                .map(|e| e.file.as_str())
                .chain(d.add_close_runs.iter().map(|r| r.file.as_str()))
                .collect(),
        }
    }
}

/// Serialize a full manifest as a checkpoint document.
///
/// `kind` and `sha_kind` are tags on the *document*, not fields of the
/// manifest: the sha stays the digest of the logical manifest alone, so a
/// checkpoint and a delta that reconstruct the same content carry the same
/// `manifest_sha`. `sha_kind` therefore sits outside the digest's cover —
/// which is harmless, because the digest *rule* is chosen by `format`, and
/// `format` is inside it. Stripping or forging the tag is caught by
/// [`parse_record`] before the digest is even consulted.
pub fn checkpoint_json(manifest: &Manifest) -> String {
    #[derive(Serialize)]
    struct Doc<'a> {
        kind: &'a str,
        #[serde(skip_serializing_if = "str::is_empty")]
        sha_kind: &'a str,
        #[serde(flatten)]
        manifest: &'a Manifest,
    }
    serde_json::to_string_pretty(&Doc {
        kind: "checkpoint",
        sha_kind: sha_kind_for(manifest.format),
        manifest,
    })
    .expect("manifest is serializable")
}

/// The `sha_kind` a record of this format must carry — empty below format 3.
fn sha_kind_for(format: u32) -> &'static str {
    if format >= MANIFEST_FORMAT_MERKLE {
        merkle::SHA_KIND
    } else {
        ""
    }
}

/// A record must declare the digest rule its format implies, and no other.
///
/// This is what gives the tag teeth: it is not the authority on how to hash
/// (`format` is), it is a claim that must agree with `format` or the record
/// is corrupt. A format-3 document with the tag stripped, or any document
/// with a tag naming a rule this build does not know, is refused here.
fn check_sha_kind(format: u32, sha_kind: &str) -> Result<()> {
    let expected = sha_kind_for(format);
    if sha_kind != expected {
        return Err(EngineError::corrupt(format!(
            "manifest format {format} implies sha_kind {expected:?} but the record \
             declares {sha_kind:?}"
        )));
    }
    Ok(())
}

/// Parse one manifest-directory document, and for a format-3 checkpoint hand
/// back the Merkle state its verification already built.
///
/// A format-1 document carries no `kind`, so an absent tag means "a whole
/// manifest" — which is what format 1 always wrote.
///
/// Returning the state matters: verifying a format-3 checkpoint *is* building
/// its Merkle tree, and replay needs that same tree to carry forward. Without
/// this the O(n) pass would be paid twice at every open.
pub fn parse_record_with_state(
    text: &str,
) -> Result<(ManifestRecord, Option<merkle::ManifestMerkle>)> {
    let value: serde_json::Value = serde_json::from_str(text)
        .map_err(|e| EngineError::corrupt(format!("manifest is not valid JSON: {e}")))?;
    let kind = value
        .get("kind")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("checkpoint")
        .to_string();
    let sha_kind = value
        .get("sha_kind")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
        .to_string();
    match kind.as_str() {
        "checkpoint" => {
            let m: Manifest = serde_json::from_value(value).map_err(|e| {
                EngineError::corrupt(format!("manifest is not a well-formed document: {e}"))
            })?;
            check_sha_kind(m.format, &sha_kind)?;
            let state = verify_checkpoint(&m)?;
            Ok((ManifestRecord::Checkpoint(m), state))
        }
        "delta" => {
            let d: ManifestDelta = serde_json::from_value(value).map_err(|e| {
                EngineError::corrupt(format!("manifest delta is not a well-formed record: {e}"))
            })?;
            d.verify_self()?;
            Ok((ManifestRecord::Delta(d), None))
        }
        other => Err(EngineError::corrupt(format!(
            "manifest document has unknown kind {other:?}"
        ))),
    }
}

/// `Manifest::verify`, keeping the Merkle tree it had to build to do it.
fn verify_checkpoint(m: &Manifest) -> Result<Option<merkle::ManifestMerkle>> {
    // Anything but the format this build writes goes down the ordinary path:
    // an older format has no Merkle state to keep, and a newer one is
    // rejected by `verify` before its `format` field is trusted for anything.
    if m.format != MANIFEST_FORMAT_VERSION {
        m.verify()?;
        return Ok(None);
    }
    const { assert!(MANIFEST_FORMAT_VERSION >= MANIFEST_FORMAT_MERKLE) };
    let state = merkle::ManifestMerkle::from_manifest(m);
    let expected = m.digest_with(&state);
    if expected != m.manifest_sha {
        return Err(EngineError::corrupt(format!(
            "manifest checksum mismatch: computed {expected}, recorded {}",
            m.manifest_sha
        )));
    }
    Ok(Some(state))
}

/// Parse one manifest-directory document.
pub fn parse_record(text: &str) -> Result<ManifestRecord> {
    parse_record_with_state(text).map(|(rec, _)| rec)
}

/// Read and self-check one generation's record, keeping a format-3
/// checkpoint's Merkle state.
pub fn read_record_with_state(
    root: &Path,
    generation: u64,
) -> Result<(ManifestRecord, Option<merkle::ManifestMerkle>)> {
    let path = manifest_path(root, generation);
    let raw = fs::read_to_string(&path).map_err(|e| EngineError::from(e).at_file(&path))?;
    let (rec, state) = parse_record_with_state(&raw).map_err(|e| e.at_file(&path))?;
    if rec.generation() != generation {
        return Err(EngineError::corrupt(format!(
            "manifest says generation {} but is filed as {generation}",
            rec.generation()
        ))
        .at_file(&path));
    }
    Ok((rec, state))
}

/// Read and self-check one generation's record.
pub fn read_record(root: &Path, generation: u64) -> Result<ManifestRecord> {
    read_record_with_state(root, generation).map(|(rec, _)| rec)
}

/// What a reconstruction resolved.
#[derive(Clone, Debug)]
pub struct Reconstructed {
    pub manifest: Manifest,
    /// Generation of the checkpoint the chain was replayed from.
    pub checkpoint: u64,
    /// Deltas replayed on top of it.
    pub deltas: u64,
    /// The head manifest's Merkle state, ready for the commit path to append
    /// to. `None` below format 3, where there is no such thing.
    pub merkle: Option<merkle::ManifestMerkle>,
    /// Manifest files this reconstruction opened. Never more than `K`: one
    /// checkpoint plus the deltas above it. Reported so the bound is a test
    /// rather than an argument.
    pub records_read: u64,
}

/// Materialize generation `G` from disk alone (memo §4, "Open / recovery").
///
/// Every link is checked: each record is self-sha'd, each delta's `parent` is
/// its predecessor, and each `parent_sha` matches the running digest. What is
/// **not** done any more is re-deriving the whole reconstructed manifest's
/// digest by re-serializing it at every step — the 26.9 ms per delta of the
/// V2 diagnosis §1. At format 3 a Merkle state is carried forward and each
/// step's claimed `manifest_sha` is re-derived from it in O(1), so the check
/// is strictly the same check for strictly less work; below format 3 the
/// whole-document rule is checked once, at the head, which is where `CURRENT`
/// anchors it anyway.
///
/// A missing or out-of-order link is `Corrupt`, exactly like a bad checksum.
pub fn reconstruct(root: &Path, generation: u64) -> Result<Reconstructed> {
    let (head, state) = read_record_with_state(root, generation)?;
    let head = match head {
        ManifestRecord::Checkpoint(m) => {
            return Ok(Reconstructed {
                manifest: m,
                checkpoint: generation,
                deltas: 0,
                merkle: state,
                records_read: 1,
            })
        }
        ManifestRecord::Delta(d) => d,
    };
    if generation == 0 {
        return Err(EngineError::corrupt(
            "generation 0 is a delta; the chain has no checkpoint to start from",
        )
        .at_file(manifest_path(root, 0)));
    }
    if head.parent != generation - 1 {
        return Err(EngineError::corrupt(format!(
            "manifest delta {generation} names parent {} rather than {}",
            head.parent,
            generation - 1
        ))
        .at_file(manifest_path(root, generation)));
    }
    // The memo's own pseudocode: resolve the base through the head's
    // `checkpoint` field and replay upward. It is a hint, not the authority,
    // so *any* failure retries the backward walk, which resolves the nearest
    // checkpoint actually on disk and owns every error this module reports.
    match replay_forward(root, generation, &head) {
        Ok(done) => Ok(done),
        Err(_) => replay_backward(root, generation, head),
    }
}

/// Replay `C+1..=G` from the checkpoint the head names.
fn replay_forward(root: &Path, generation: u64, head: &ManifestDelta) -> Result<Reconstructed> {
    if head.checkpoint >= generation {
        return Err(EngineError::corrupt(format!(
            "manifest delta {generation} names checkpoint {} at or above itself",
            head.checkpoint
        )));
    }
    let (base, state) = read_record_with_state(root, head.checkpoint)?;
    let ManifestRecord::Checkpoint(base) = base else {
        return Err(EngineError::corrupt(format!(
            "generation {} is named as a checkpoint but is a delta",
            head.checkpoint
        )));
    };
    let mut walk = Walk::new(base, state, head.checkpoint);
    let mut records_read = 1;
    for g in head.checkpoint + 1..=generation {
        if g == generation {
            walk.step_delta(root, head)?;
            continue;
        }
        records_read += 1;
        let (rec, state) = read_record_with_state(root, g).map_err(|e| chain_below(root, generation, g, e))?;
        match rec {
            // gc can materialize a nearer checkpoint than the head names.
            // Adopting it keeps `checkpoint` the *nearest* one, so the value
            // this reports — and therefore what the next delta records — is
            // the same one the backward walk would have found.
            ManifestRecord::Checkpoint(m) => walk.restart(m, state, g),
            ManifestRecord::Delta(d) => walk.step_delta(root, &d)?,
        }
    }
    walk.finish(generation, records_read + 1)
}

/// Walk parent links down to the nearest checkpoint on disk, then replay up.
fn replay_backward(root: &Path, generation: u64, head: ManifestDelta) -> Result<Reconstructed> {
    let mut chain: Vec<ManifestDelta> = vec![head];
    let mut g = generation - 1;
    let (base, state) = loop {
        // A link below the generation asked for is part of the chain, so its
        // absence is corruption of that chain rather than a plain read
        // failure — same verdict a bad checksum gets (memo §4).
        let (rec, state) =
            read_record_with_state(root, g).map_err(|e| chain_below(root, generation, g, e))?;
        match rec {
            ManifestRecord::Checkpoint(m) => break (m, state),
            ManifestRecord::Delta(d) => {
                if g == 0 {
                    return Err(EngineError::corrupt(
                        "generation 0 is a delta; the chain has no checkpoint to start from",
                    )
                    .at_file(manifest_path(root, 0)));
                }
                if d.parent != g - 1 {
                    return Err(EngineError::corrupt(format!(
                        "manifest delta {g} names parent {} rather than {}",
                        d.parent,
                        g - 1
                    ))
                    .at_file(manifest_path(root, g)));
                }
                chain.push(d);
                g -= 1;
            }
        }
    };
    let records_read = chain.len() as u64 + 1;
    let mut walk = Walk::new(base, state, g);
    // chain was collected newest-first; replay it oldest-first
    for d in chain.iter().rev() {
        walk.step_delta(root, d)?;
    }
    walk.finish(generation, records_read)
}

fn chain_below(root: &Path, generation: u64, g: u64, e: EngineError) -> EngineError {
    if g == generation {
        e
    } else {
        EngineError::corrupt(format!(
            "manifest chain broken below generation {generation}: \
             generation {g} is unreadable: {}",
            e.message
        ))
        .at_file(manifest_path(root, g))
    }
}

/// The running reconstruction: one manifest mutated in place, one Merkle
/// state carried alongside it, and no full-document work per step.
struct Walk {
    manifest: Manifest,
    merkle: Option<merkle::ManifestMerkle>,
    checkpoint: u64,
    deltas: u64,
}

impl Walk {
    fn new(base: Manifest, merkle: Option<merkle::ManifestMerkle>, checkpoint: u64) -> Self {
        Self {
            manifest: base,
            merkle,
            checkpoint,
            deltas: 0,
        }
    }

    /// A checkpoint met mid-walk replaces the base outright: its content is
    /// the whole manifest and it has already self-verified.
    fn restart(&mut self, base: Manifest, merkle: Option<merkle::ManifestMerkle>, at: u64) {
        self.manifest = base;
        self.merkle = merkle;
        self.checkpoint = at;
        self.deltas = 0;
    }

    fn step_delta(&mut self, root: &Path, d: &ManifestDelta) -> Result<()> {
        if d.checkpoint > self.checkpoint {
            return Err(EngineError::corrupt(format!(
                "manifest delta {} names checkpoint {} but the nearest checkpoint on disk is {}",
                d.generation, d.checkpoint, self.checkpoint
            ))
            .at_file(manifest_path(root, d.generation)));
        }
        if d.parent_sha != self.manifest.manifest_sha {
            return Err(EngineError::corrupt(format!(
                "manifest chain broken at generation {}: delta names parent sha {} but \
                 generation {} reconstructs to {}",
                d.generation, d.parent_sha, self.manifest.generation, self.manifest.manifest_sha
            ))
            .at_file(manifest_path(root, d.generation)));
        }
        d.apply_into(&mut self.manifest, self.merkle.as_mut());
        // O(1) against the maintained state. Kept per step rather than only
        // at the head because it costs one 200-byte hash and it names the
        // generation the divergence starts at, which is what an operator
        // reading the error actually needs.
        if let Some(state) = &self.merkle {
            let expected = self.manifest.digest_with(state);
            if expected != d.manifest_sha {
                return Err(EngineError::corrupt(format!(
                    "reconstructed manifest {} hashes to {expected} but the delta records {}",
                    self.manifest.generation, d.manifest_sha
                ))
                .at_file(manifest_path(root, d.generation)));
            }
        }
        self.deltas += 1;
        Ok(())
    }

    fn finish(self, generation: u64, records_read: u64) -> Result<Reconstructed> {
        let m = self.manifest;
        if m.generation != generation {
            return Err(EngineError::corrupt(format!(
                "reconstruction produced generation {} rather than {generation}",
                m.generation
            )));
        }
        // Below format 3 there is no incremental digest, so the
        // whole-document rule is applied once here rather than at every step.
        if self.merkle.is_none() && self.deltas > 0 {
            let expected = m.digest();
            if expected != m.manifest_sha {
                return Err(EngineError::corrupt(format!(
                    "reconstructed manifest {generation} hashes to {expected} but the \
                     chain records {}",
                    m.manifest_sha
                ))
                .at_file(manifest_path(Path::new(""), generation)));
            }
        }
        Ok(Reconstructed {
            manifest: m,
            checkpoint: self.checkpoint,
            deltas: self.deltas,
            merkle: self.merkle,
            records_read,
        })
    }
}

/// Is this a store the current build may write to?
pub fn is_writable_format(format: u32) -> bool {
    format == MANIFEST_FORMAT_VERSION
}

/// The error a write path raises against a store an older engine wrote.
pub fn read_only_format_error(format: u32) -> EngineError {
    let what = match format {
        FORMAT_LEGACY => "was written by a format-1 engine (one full manifest per generation)",
        2 => {
            "was written by a format-2 engine (manifest_sha is the sha of the whole \
             document rather than a Merkle root over the segment set)"
        }
        _ => "uses an unknown manifest format",
    };
    EngineError::new(
        crate::error::Category::Invariant,
        format!(
            "this store {what}, and this build writes manifest format \
             {MANIFEST_FORMAT_VERSION}; it is open read-only"
        ),
    )
    .with_remedy(
        "run `tgms store upgrade-manifests --store <path>`: it writes one \
         format-3 checkpoint and flips CURRENT, touching nothing else",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manifest::{SegmentEntry, Widths};

    fn seg(id: u64) -> SegmentEntry {
        SegmentEntry {
            file: format!("seg/{id:012}.tgs"),
            rows: 10,
            key_lo: (1, "a".into()),
            key_hi: (2, "b".into()),
            vt_min: 0,
            vt_max: 5,
            vt_e_max: 6,
            tt_s_min: 100,
            tt_s_max: 100,
            rel_codes: vec![1],
            n_closed_folded: 0,
            all_current: true,
            sha: format!("{id:016x}"),
        }
    }

    /// A parent/child pair the commit path would produce: one appended
    /// segment per lane plus a bumped id counter.
    fn pair() -> (Manifest, Manifest) {
        let mut parent = Manifest::genesis();
        parent.node_store.push(seg(0));
        parent.next_segment_id = 1;
        parent.seal();
        let mut child = parent.successor(500);
        child.node_store.push(seg(1));
        child.edge_lanes.event.push(seg(2));
        child.close_runs.push(CloseRunRef {
            file: "close/000000000001.tgc".into(),
            entries: 3,
            sha: String::new(),
        });
        child.next_segment_id = 3;
        child.stats.n_edge_versions = 10;
        child.dict.records = 4;
        child.dict.bytes = 64;
        child.seal();
        (parent, child)
    }

    #[test]
    fn a_delta_round_trips_to_its_child() {
        let (parent, child) = pair();
        let d = ManifestDelta::between(&parent, &child, 0).expect("expressible as a delta");
        assert_eq!(d.add_nodes.len(), 1);
        assert_eq!(d.add_edges_event.len(), 1);
        assert_eq!(d.add_close_runs.len(), 1);
        assert!(d.del_nodes.is_empty());
        assert_eq!(d.parent_sha, parent.manifest_sha);
        assert_eq!(d.apply(&parent), child);
        assert_eq!(d.manifest_sha, child.manifest_sha);
        d.verify_self().unwrap();
    }

    #[test]
    fn a_delta_is_far_smaller_than_the_document_it_replaces() {
        // the whole point: bytes per commit stop tracking the segment count
        let mut parent = Manifest::genesis();
        for i in 0..400 {
            parent.node_store.push(seg(i));
        }
        parent.next_segment_id = 400;
        parent.seal();
        let mut child = parent.successor(1);
        child.node_store.push(seg(400));
        child.next_segment_id = 401;
        child.seal();

        let d = ManifestDelta::between(&parent, &child, 0).unwrap();
        assert!(
            d.to_json().len() * 20 < checkpoint_json(&child).len(),
            "delta {} B against checkpoint {} B",
            d.to_json().len(),
            checkpoint_json(&child).len()
        );
    }

    #[test]
    fn a_deletion_is_expressed_by_file_name() {
        let (parent, _) = pair();
        let mut child = parent.successor(9);
        child.node_store.clear();
        child.node_store.push(seg(7));
        child.seal();
        let d = ManifestDelta::between(&parent, &child, 0).unwrap();
        assert_eq!(d.del_nodes, vec!["seg/000000000000.tgs".to_string()]);
        assert_eq!(d.add_nodes.len(), 1);
        assert_eq!(d.apply(&parent), child);
    }

    #[test]
    fn a_pair_no_diff_can_express_refuses_to_become_a_delta() {
        // an entry changed *in place* is neither an add nor a remove, so the
        // name diff would silently lose it — `between` must decline instead
        let (parent, _) = pair();
        let mut child = parent.successor(9);
        child.node_store[0].rows = 999;
        child.seal();
        assert!(
            ManifestDelta::between(&parent, &child, 0).is_none(),
            "a modified-in-place entry must fall back to a checkpoint"
        );

        // so must a widths change, which lives only in checkpoints
        let mut wide = parent.successor(9);
        wide.widths = Widths {
            entity_id: 64,
            ..Widths::default()
        };
        wide.seal();
        assert!(ManifestDelta::between(&parent, &wide, 0).is_none());

        // and so must a non-consecutive generation
        let mut skipped = parent.successor(9);
        skipped.generation = parent.generation + 2;
        skipped.seal();
        assert!(ManifestDelta::between(&parent, &skipped, 0).is_none());
    }

    #[test]
    fn tampering_with_a_delta_is_detected() {
        let (parent, child) = pair();
        let mut d = ManifestDelta::between(&parent, &child, 0).unwrap();
        d.stats.n_edge_versions = 7; // sha not recomputed — exactly the attack
        let err = d.verify_self().unwrap_err();
        assert_eq!(err.category, crate::error::Category::Corrupt);

        // the reconstruction digest is inside delta_sha's cover, so forging
        // it alone is caught too
        let mut d = ManifestDelta::between(&parent, &child, 0).unwrap();
        d.manifest_sha = "0000000000000000".into();
        assert!(d.verify_self().is_err());

        // and through the JSON path
        let mut d = ManifestDelta::between(&parent, &child, 0).unwrap();
        d.created_tt = -1;
        assert!(parse_record(&d.to_json()).is_err());
    }

    #[test]
    fn a_checkpoint_document_is_tagged_and_parses_back_unchanged() {
        let (_, child) = pair();
        let json = checkpoint_json(&child);
        assert!(json.contains("\"kind\": \"checkpoint\""));
        match parse_record(&json).unwrap() {
            ManifestRecord::Checkpoint(m) => assert_eq!(m, child),
            other => panic!("expected a checkpoint, got {other:?}"),
        }
    }

    #[test]
    fn a_format_1_document_parses_as_a_checkpoint() {
        // exactly the bytes the pre-change engine wrote: no `kind` tag
        let mut legacy = Manifest::genesis();
        legacy.format = FORMAT_LEGACY;
        legacy.seal();
        let json = serde_json::to_string_pretty(&legacy).unwrap();
        assert!(!json.contains("kind"));
        match parse_record(&json).unwrap() {
            ManifestRecord::Checkpoint(m) => {
                assert_eq!(m.format, FORMAT_LEGACY);
                assert!(!is_writable_format(m.format));
            }
            other => panic!("expected a checkpoint, got {other:?}"),
        }
    }

    #[test]
    fn an_unknown_kind_is_corruption_rather_than_a_guess() {
        let (_, child) = pair();
        let json = checkpoint_json(&child).replace("\"checkpoint\"", "\"snapshot\"");
        let err = parse_record(&json).unwrap_err();
        assert_eq!(err.category, crate::error::Category::Corrupt);
    }

    // --- format 3: the tag, and the chain it labels ---------------------- //

    fn tmp_root(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-chain-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(p.join("manifests")).unwrap();
        p
    }

    fn write(root: &Path, generation: u64, json: &str) {
        fs::write(manifest_path(root, generation), json).unwrap();
    }

    /// A chain `checkpoint at 0, deltas up to G`, with a periodic checkpoint
    /// every `every` generations — the shape the commit path writes.
    fn lay_chain(root: &Path, generations: u64, every: u64) -> Vec<Manifest> {
        let mut all = vec![Manifest::genesis()];
        write(root, 0, &checkpoint_json(&all[0]));
        let mut checkpoint = 0;
        for g in 1..=generations {
            let parent = all[(g - 1) as usize].clone();
            let mut child = parent.successor(g as i64 * 10);
            child.node_store.push(seg(g));
            child.next_segment_id = g + 1;
            child.stats.n_node_versions += 10;
            child.seal();
            if g.is_multiple_of(every) {
                write(root, g, &checkpoint_json(&child));
                checkpoint = g;
            } else {
                let d = ManifestDelta::between(&parent, &child, checkpoint)
                    .expect("a pure append is expressible as a delta");
                write(root, g, &d.to_json());
            }
            all.push(child);
        }
        all
    }

    #[test]
    fn every_record_this_build_writes_declares_the_merkle_digest_rule() {
        let (parent, child) = pair();
        let d = ManifestDelta::between(&parent, &child, 0).unwrap();
        assert_eq!(d.sha_kind, merkle::SHA_KIND);
        assert!(d.to_json().contains("\"sha_kind\": \"merkle-v1\""));
        assert!(checkpoint_json(&child).contains("\"sha_kind\": \"merkle-v1\""));

        // and the tag has to agree with `format`, in both directions
        let mut stripped = d.clone();
        stripped.sha_kind = String::new();
        stripped.seal();
        assert!(stripped.verify_self().is_err(), "a format-3 delta must be tagged");

        let mut wrong = d.clone();
        wrong.sha_kind = "xor-v9".into();
        wrong.seal();
        assert!(wrong.verify_self().is_err());

        // the tag is inside delta_sha's cover, so editing it alone is caught
        // by the checksum as well as by the format check
        let mut forged = d.clone();
        forged.sha_kind = "xor-v9".into();
        assert!(forged.verify_self().is_err());

        // on a checkpoint the tag sits outside manifest_sha, so the check is
        // what defends it — stripping it must not read as a format-2 document
        let json = checkpoint_json(&child).replace("\"sha_kind\": \"merkle-v1\",\n  ", "");
        assert!(!json.contains("sha_kind"));
        assert!(parse_record(&json).is_err());
    }

    #[test]
    fn reconstruction_reads_one_checkpoint_and_the_deltas_above_it_and_no_more() {
        // the bound the open path rests on: never the directory, never the
        // history — at most K files, one of them the checkpoint
        let root = tmp_root("bounded-read");
        let every = 8;
        let all = lay_chain(&root, 20, every);

        for g in 0..=20u64 {
            let done = reconstruct(&root, g).unwrap();
            assert_eq!(done.manifest, all[g as usize], "generation {g}");
            assert_eq!(done.checkpoint, g - g % every);
            assert_eq!(done.deltas, g % every);
            assert_eq!(
                done.records_read,
                done.deltas + 1,
                "generation {g} read more than the checkpoint plus its deltas"
            );
            assert!(
                done.records_read <= every,
                "generation {g} read {} files against K={every}",
                done.records_read
            );
            assert!(done.merkle.is_some(), "format 3 keeps its Merkle state");
        }
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn the_forward_walk_and_the_backward_fallback_agree() {
        // gc's case: the checkpoint the head names is gone, and a nearer one
        // was materialized at the retention floor. The forward attempt fails
        // and the backward walk resolves it — to the same manifest.
        let root = tmp_root("fallback");
        let all = lay_chain(&root, 12, 8);
        let forward = reconstruct(&root, 12).unwrap();
        assert_eq!(forward.checkpoint, 8);

        // materialize 10 as a checkpoint and delete everything below it,
        // exactly as gc's pass 0 and pass 1 do
        write(&root, 10, &checkpoint_json(&all[10]));
        for g in 0..10 {
            fs::remove_file(manifest_path(&root, g)).unwrap();
        }
        let back = reconstruct(&root, 12).unwrap();
        assert_eq!(back.manifest, forward.manifest);
        assert_eq!(back.manifest, all[12]);
        assert_eq!(back.checkpoint, 10, "the nearest checkpoint on disk");
        assert_eq!(back.deltas, 2);
        assert_eq!(back.records_read, 3);
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_checkpoint_materialized_mid_chain_is_adopted_by_the_forward_walk() {
        // the same gc shape, but with the stale checkpoint still on disk: the
        // forward walk must still report the *nearest* checkpoint, or the
        // next delta would record a weaker lower bound than the truth
        let root = tmp_root("mid-chain");
        let all = lay_chain(&root, 12, 8);
        write(&root, 10, &checkpoint_json(&all[10]));
        let done = reconstruct(&root, 12).unwrap();
        assert_eq!(done.manifest, all[12]);
        assert_eq!(done.checkpoint, 10);
        assert_eq!(done.deltas, 2);
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn replay_still_catches_a_forged_link_at_the_generation_it_starts() {
        let root = tmp_root("forged");
        let all = lay_chain(&root, 6, 8);

        // a delta whose reconstruction digest is a lie. `delta_sha` covers
        // `manifest_sha`, so the record has to be resealed to get this far —
        // which is exactly the attack the reconstruction check exists for.
        let mut d = ManifestDelta::between(&all[3], &all[4], 0).unwrap();
        d.manifest_sha = "0000000000000000".into();
        d.seal();
        d.verify_self().unwrap();
        write(&root, 4, &d.to_json());
        let err = reconstruct(&root, 6).unwrap_err();
        assert_eq!(err.category, crate::error::Category::Corrupt);
        assert!(
            err.message.contains("reconstructed manifest 4"),
            "the error must name the generation the divergence starts at: {}",
            err.message
        );

        // and a broken parent_sha chain is still a broken chain
        let all = lay_chain(&root, 6, 8);
        let mut d = ManifestDelta::between(&all[3], &all[4], 0).unwrap();
        d.parent_sha = "0000000000000000".into();
        d.seal();
        write(&root, 4, &d.to_json());
        let err = reconstruct(&root, 6).unwrap_err();
        assert!(err.message.contains("names parent sha"), "{}", err.message);
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn replay_never_re_serializes_the_reconstructed_manifest() {
        // the O(n)-per-step regression guard, expressed as behaviour rather
        // than as a timing: a chain over a manifest big enough that a
        // per-step full re-hash would be visible must still reconstruct, and
        // the state it hands back must equal a from-scratch rebuild — which
        // is only true if the incremental update is right.
        let root = tmp_root("incremental");
        let mut all = vec![Manifest::genesis()];
        {
            let mut base = Manifest::genesis();
            for i in 0..500 {
                base.node_store.push(seg(i));
            }
            base.next_segment_id = 500;
            base.seal();
            write(&root, 0, &checkpoint_json(&base));
            all[0] = base;
        }
        for g in 1..=60u64 {
            let parent = all[(g - 1) as usize].clone();
            let mut child = parent.successor(g as i64);
            child.node_store.push(seg(500 + g));
            child.edge_lanes.event.push(seg(9000 + g));
            child.next_segment_id = 500 + g * 2;
            child.seal();
            let d = ManifestDelta::between(&parent, &child, 0).unwrap();
            write(&root, g, &d.to_json());
            all.push(child);
        }

        let done = reconstruct(&root, 60).unwrap();
        assert_eq!(done.manifest, all[60]);
        assert_eq!(done.deltas, 60);
        assert_eq!(done.records_read, 61);
        assert_eq!(
            done.merkle.as_ref().unwrap(),
            &merkle::ManifestMerkle::from_manifest(&done.manifest),
            "the carried state must equal a from-scratch rebuild of the head"
        );
        assert_eq!(done.manifest.manifest_sha, done.manifest.body_sha_canonical());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn apply_in_place_and_apply_by_clone_agree() {
        let (parent, child) = pair();
        let d = ManifestDelta::between(&parent, &child, 0).unwrap();
        let cloned = d.apply(&parent);
        let mut in_place = parent.clone();
        let mut state = merkle::ManifestMerkle::from_manifest(&in_place);
        d.apply_into(&mut in_place, Some(&mut state));
        assert_eq!(in_place, cloned);
        assert_eq!(in_place, child);
        assert_eq!(state, merkle::ManifestMerkle::from_manifest(&child));
        assert_eq!(in_place.digest_with(&state), child.manifest_sha);
    }

    #[test]
    fn a_delta_that_deletes_rebuilds_the_state_rather_than_appending_to_it() {
        // a spine cannot un-append, so the lane is rebuilt. Rare — compaction
        // publishes a checkpoint — but it must be right when it happens.
        let (parent, _) = pair();
        let mut child = parent.successor(9);
        child.node_store.clear();
        child.node_store.push(seg(7));
        child.seal();
        let d = ManifestDelta::between(&parent, &child, 0).unwrap();
        assert!(!d.del_nodes.is_empty());

        let mut m = parent.clone();
        let mut state = merkle::ManifestMerkle::from_manifest(&m);
        d.apply_into(&mut m, Some(&mut state));
        assert_eq!(m, child);
        assert_eq!(state, merkle::ManifestMerkle::from_manifest(&child));
        assert_eq!(m.digest_with(&state), child.manifest_sha);
    }
}
