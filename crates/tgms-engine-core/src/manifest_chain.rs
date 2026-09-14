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
//! **Reconstruction walks backwards.** §4 of the memo resolves the base
//! checkpoint through the delta's `checkpoint` field. This walks parent links
//! back to the nearest checkpoint instead and treats `checkpoint` as a
//! *lower bound* cross-check, because gc materializes a fresh checkpoint at
//! its retention floor (memo §4, "gc with deltas") — after which the deltas
//! above the floor still name the older, now-deleted checkpoint. Both agree
//! whenever the field's target is on disk; the backward walk additionally
//! survives the case gc creates by design. Recorded as a dated deviation.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{EngineError, Result};
use crate::manifest::{
    short_sha, CloseRunRef, DictRef, EventLogRef, Manifest, SegmentEntry, Stats,
};
use crate::{FORMAT_LEGACY, MANIFEST_FORMAT_VERSION};

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

fn removed(before: &[String], after: &[String]) -> Vec<String> {
    let keep: std::collections::HashSet<&str> = after.iter().map(String::as_str).collect();
    before
        .iter()
        .filter(|f| !keep.contains(f.as_str()))
        .cloned()
        .collect()
}

fn added<'a, T: 'a>(before: &[String], after: &'a [T], name: impl Fn(&T) -> &str) -> Vec<&'a T> {
    let had: std::collections::HashSet<&str> = before.iter().map(String::as_str).collect();
    after.iter().filter(|e| !had.contains(name(e))).collect()
}

fn seg_names(v: &[SegmentEntry]) -> Vec<String> {
    v.iter().map(|e| e.file.clone()).collect()
}

fn run_names(v: &[CloseRunRef]) -> Vec<String> {
    v.iter().map(|r| r.file.clone()).collect()
}

/// Apply one list diff: drop the named entries, then append the new ones.
fn patch<T: Clone>(list: &mut Vec<T>, del: &[String], add: &[T], name: impl Fn(&T) -> &str) {
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
    /// write a checkpoint instead. Crucially, the delta is *validated by
    /// replay before it is returned*: whatever the diff heuristics do,
    /// `apply` must reproduce `child` exactly or no delta is offered. That is
    /// what makes an entry whose contents changed in place (rather than being
    /// added or removed), or a reordering, structurally safe instead of a
    /// silent divergence.
    pub fn between(parent: &Manifest, child: &Manifest, checkpoint: u64) -> Option<Self> {
        if parent.format != child.format
            || child.format != MANIFEST_FORMAT_VERSION
            || parent.widths != child.widths
            || child.parent != Some(parent.generation)
            || child.generation != parent.generation.checked_add(1)?
        {
            return None;
        }
        let mut d = Self {
            format: MANIFEST_FORMAT_VERSION,
            kind: "delta".into(),
            generation: child.generation,
            parent: parent.generation,
            parent_sha: parent.manifest_sha.clone(),
            checkpoint,
            created_tt: child.created_tt,
            event_log: child.event_log.clone(),
            dict: child.dict.clone(),
            next_segment_id: child.next_segment_id,
            stats: child.stats.clone(),
            add_nodes: added(&seg_names(&parent.node_store), &child.node_store, |e| &e.file)
                .into_iter()
                .cloned()
                .collect(),
            del_nodes: removed(&seg_names(&parent.node_store), &seg_names(&child.node_store)),
            add_edges_event: added(
                &seg_names(&parent.edge_lanes.event),
                &child.edge_lanes.event,
                |e| &e.file,
            )
            .into_iter()
            .cloned()
            .collect(),
            del_edges_event: removed(
                &seg_names(&parent.edge_lanes.event),
                &seg_names(&child.edge_lanes.event),
            ),
            add_edges_interval: added(
                &seg_names(&parent.edge_lanes.interval),
                &child.edge_lanes.interval,
                |e| &e.file,
            )
            .into_iter()
            .cloned()
            .collect(),
            del_edges_interval: removed(
                &seg_names(&parent.edge_lanes.interval),
                &seg_names(&child.edge_lanes.interval),
            ),
            add_close_runs: added(&run_names(&parent.close_runs), &child.close_runs, |r| {
                &r.file
            })
            .into_iter()
            .cloned()
            .collect(),
            del_close_runs: removed(&run_names(&parent.close_runs), &run_names(&child.close_runs)),
            delta_sha: String::new(),
            manifest_sha: child.manifest_sha.clone(),
        };
        if d.apply(parent) != *child {
            return None;
        }
        d.seal();
        Some(d)
    }

    /// Reconstruct this generation's manifest on top of its parent's.
    pub fn apply(&self, base: &Manifest) -> Manifest {
        let mut m = base.clone();
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
        m
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
        if self.format != MANIFEST_FORMAT_VERSION {
            return Err(EngineError::corrupt(format!(
                "manifest delta format {} is not supported by this build \
                 (expected {MANIFEST_FORMAT_VERSION})",
                self.format
            )));
        }
        if self.kind != "delta" {
            return Err(EngineError::corrupt(format!(
                "manifest delta is tagged kind={:?}",
                self.kind
            )));
        }
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

/// Serialize a full manifest as a format-2 checkpoint document.
///
/// `kind` is a tag on the *document*, not a field of the manifest: the sha
/// stays the digest of the logical manifest alone, so a checkpoint and a
/// delta that reconstruct the same content carry the same `manifest_sha`.
pub fn checkpoint_json(manifest: &Manifest) -> String {
    #[derive(Serialize)]
    struct Doc<'a> {
        kind: &'a str,
        #[serde(flatten)]
        manifest: &'a Manifest,
    }
    serde_json::to_string_pretty(&Doc {
        kind: "checkpoint",
        manifest,
    })
    .expect("manifest is serializable")
}

/// Parse one manifest-directory document.
///
/// A format-1 document carries no `kind`, so an absent tag means "a whole
/// manifest" — which is what format 1 always wrote.
pub fn parse_record(text: &str) -> Result<ManifestRecord> {
    let value: serde_json::Value = serde_json::from_str(text)
        .map_err(|e| EngineError::corrupt(format!("manifest is not valid JSON: {e}")))?;
    let kind = value
        .get("kind")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("checkpoint")
        .to_string();
    match kind.as_str() {
        "checkpoint" => {
            let m: Manifest = serde_json::from_value(value).map_err(|e| {
                EngineError::corrupt(format!("manifest is not a well-formed document: {e}"))
            })?;
            m.verify()?;
            Ok(ManifestRecord::Checkpoint(m))
        }
        "delta" => {
            let d: ManifestDelta = serde_json::from_value(value).map_err(|e| {
                EngineError::corrupt(format!("manifest delta is not a well-formed record: {e}"))
            })?;
            d.verify_self()?;
            Ok(ManifestRecord::Delta(d))
        }
        other => Err(EngineError::corrupt(format!(
            "manifest document has unknown kind {other:?}"
        ))),
    }
}

/// Read and self-check one generation's record.
pub fn read_record(root: &Path, generation: u64) -> Result<ManifestRecord> {
    let path = manifest_path(root, generation);
    let raw = fs::read_to_string(&path).map_err(|e| EngineError::from(e).at_file(&path))?;
    let rec = parse_record(&raw).map_err(|e| e.at_file(&path))?;
    if rec.generation() != generation {
        return Err(EngineError::corrupt(format!(
            "manifest says generation {} but is filed as {generation}",
            rec.generation()
        ))
        .at_file(&path));
    }
    Ok(rec)
}

/// What a reconstruction resolved.
pub struct Reconstructed {
    pub manifest: Manifest,
    /// Generation of the checkpoint the chain was replayed from.
    pub checkpoint: u64,
    /// Deltas replayed on top of it.
    pub deltas: u64,
}

/// Materialize generation `G` from disk alone (memo §4, "Open / recovery").
///
/// Every link is checked: each record is self-sha'd, each delta's `parent` is
/// its predecessor, each `parent_sha` matches the running digest, and each
/// reconstructed step re-derives the `manifest_sha` the record claims. A
/// missing or out-of-order link is `Corrupt`, exactly like a bad checksum.
pub fn reconstruct(root: &Path, generation: u64) -> Result<Reconstructed> {
    let mut chain: Vec<ManifestDelta> = Vec::new();
    let mut g = generation;
    let base = loop {
        // A link below the generation asked for is part of the chain, so its
        // absence is corruption of that chain rather than a plain read
        // failure — same verdict a bad checksum gets (memo §4).
        let rec = read_record(root, g).map_err(|e| {
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
        })?;
        match rec {
            ManifestRecord::Checkpoint(m) => break m,
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
    let checkpoint = base.generation;
    let deltas = chain.len() as u64;
    let mut m = base;
    // chain was collected newest-first; replay it oldest-first
    for d in chain.iter().rev() {
        if d.checkpoint > checkpoint {
            return Err(EngineError::corrupt(format!(
                "manifest delta {} names checkpoint {} but the nearest checkpoint on disk is {checkpoint}",
                d.generation, d.checkpoint
            ))
            .at_file(manifest_path(root, d.generation)));
        }
        if d.parent_sha != m.manifest_sha {
            return Err(EngineError::corrupt(format!(
                "manifest chain broken at generation {}: delta names parent sha {} but \
                 generation {} reconstructs to {}",
                d.generation, d.parent_sha, m.generation, m.manifest_sha
            ))
            .at_file(manifest_path(root, d.generation)));
        }
        m = d.apply(&m);
        let expected = m.body_sha();
        if expected != d.manifest_sha {
            return Err(EngineError::corrupt(format!(
                "reconstructed manifest {} hashes to {expected} but the delta records {}",
                m.generation, d.manifest_sha
            ))
            .at_file(manifest_path(root, d.generation)));
        }
    }
    if m.generation != generation {
        return Err(EngineError::corrupt(format!(
            "reconstruction produced generation {} rather than {generation}",
            m.generation
        )));
    }
    Ok(Reconstructed {
        manifest: m,
        checkpoint,
        deltas,
    })
}

/// Is this a store the current build may write to?
pub fn is_writable_format(format: u32) -> bool {
    format == MANIFEST_FORMAT_VERSION
}

/// The error a write path raises against a format-1 store.
pub fn read_only_format_error(format: u32) -> EngineError {
    let what = if format == FORMAT_LEGACY {
        "was written by a format-1 engine (one full manifest per generation)"
    } else {
        "uses an unknown manifest format"
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
         format-2 checkpoint and flips CURRENT, touching nothing else",
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
}
