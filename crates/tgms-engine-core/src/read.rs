//! Point and full-scan reads that reconstruct whole version rows
//! (implementation spec §5.4, §5.5).
//!
//! These are the shape `StorageAdapter` hands to Python: every field of a
//! `NodeVersion` / `EdgeVersion`, including the ones the engine does not
//! store. `eid` is re-derived from `(src, dst, rel_type, disc)` and `vid` is
//! reassembled from its two columns, which is the whole point of the
//! derivability invariant (D-028 #2) — 24-byte hex identities never occupy a
//! row, they are produced at the boundary where row counts are already small.
//!
//! `props` is returned as the exact canonical-JSON text that was ingested.
//! The store digest is computed in Python over this string, so re-serializing
//! it — even into something equivalent — would change a digest and break
//! replay against the frozen SHAs.
//!
//! Everything here is a linear scan. WP-N3 explicitly allows that: the
//! identity postings index that makes point reads O(1) is WP-N4, and it
//! changes the cost, not the answer.

use std::collections::{HashMap, HashSet};

/// How many stored node versions one identity-postings probe is worth, from
/// the measurement in `nodes_with_believed_versions`: ~5 µs per probe against
/// ~0.25 µs per materialized node version. Only the crossover moves if a host
/// disagrees, and at the crossover the two paths cost the same by definition.
const PROBE_COST_RATIO: u64 = 20;

use crate::derive::{edge_eid, Id96};
use crate::error::{EngineError, Result};
use crate::row::RowKind;
use crate::store::{segment_id_of, NativeStore};
use crate::visibility::CloseIndex;
use crate::{believed_at, OPEN_END};

/// A node version with every field materialized.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NodeVersionOut {
    pub vid: String,
    pub uid: String,
    pub label: String,
    pub vt_s: i64,
    pub vt_e: i64,
    pub tt_s: i64,
    pub tt_e: i64,
    pub props: String,
    pub source: String,
    pub provenance_ref: Option<String>,
}

/// An edge version with every field materialized, `eid` included.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EdgeVersionOut {
    pub eid: String,
    pub vid: String,
    pub src: String,
    pub dst: String,
    pub rel_type: String,
    pub disc: String,
    pub vt_s: i64,
    pub vt_e: i64,
    pub tt_s: i64,
    pub tt_e: i64,
    pub props: String,
    pub source: String,
    pub provenance_ref: Option<String>,
}

/// Postings key for a node identity. Any stable hash works: the full uid is
/// verified on every hit, so this only has to spread, not to be unique.
fn uid_key(uid: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    uid.hash(&mut h);
    h.finish()
}

impl NativeStore {
    fn edge_files(&self) -> Vec<(String, u64)> {
        let m = self.manifest();
        m.edge_lanes
            .event
            .iter()
            .chain(m.edge_lanes.interval.iter())
            .map(|e| (e.file.clone(), segment_id_of(&e.file)))
            .collect()
    }

    fn node_files(&self) -> Vec<(String, u64)> {
        self.manifest()
            .node_store
            .iter()
            .map(|e| (e.file.clone(), segment_id_of(&e.file)))
            .collect()
    }

    fn uid_of(&self, id: u32) -> Result<String> {
        self.dict()
            .uid(id)
            .map(str::to_string)
            .ok_or_else(|| EngineError::invariant(format!("dense id {id} is not in the dictionary")))
    }

    /// `(eid, rel_type)` for explicit `(segment id, row)` addresses, in
    /// input order.
    ///
    /// The companion of the scan's `seg_id` / `seg_row` projection: a
    /// traversal scans integer columns only and comes back here for the two
    /// derived fields of the rows that actually survive it — a sha256 and a
    /// dictionary lookup per *surviving* row instead of per scanned row.
    /// Addresses are meaningful only against the generation that produced
    /// them; ids of segments outside the current generation are refused.
    pub fn edge_idents_at(
        &self,
        seg_ids: &[u64],
        seg_rows: &[u32],
    ) -> Result<Vec<(String, String)>> {
        if seg_ids.len() != seg_rows.len() {
            return Err(EngineError::invariant(
                "edge_idents_at: seg_ids and seg_rows differ in length",
            ));
        }
        let files: HashMap<u64, String> = self
            .edge_files()
            .into_iter()
            .map(|(file, id)| (id, file))
            .collect();
        // group requests by segment so each is opened (and verified) once
        let mut by_seg: HashMap<u64, Vec<usize>> = HashMap::new();
        for (i, &sid) in seg_ids.iter().enumerate() {
            by_seg.entry(sid).or_default().push(i);
        }
        let mut out: Vec<Option<(String, String)>> = vec![None; seg_ids.len()];
        for (sid, idxs) in by_seg {
            let file = files.get(&sid).ok_or_else(|| {
                EngineError::invariant(format!(
                    "segment id {sid} is not in the current generation"
                ))
            })?;
            let seg = self.open_segment(file)?;
            let h = seg.header();
            let strings = seg.strings()?;
            let src = seg.u32_column("src_id")?;
            let dst = seg.u32_column("dst_id")?;
            let rel = seg.u16_column("rel_code")?;
            let disc = seg.u32_column("disc_ref")?;
            for i in idxs {
                let r = seg_rows[i] as usize;
                if r >= src.len() {
                    return Err(EngineError::invariant(format!(
                        "row {r} is out of bounds for segment {sid}"
                    )));
                }
                let src_uid = self.uid_of(src[r])?;
                let dst_uid = self.uid_of(dst[r])?;
                let rel_type = h.rel_types.get(rel[r] as usize).ok_or_else(|| {
                    EngineError::corrupt(format!("rel_code {} has no entry", rel[r]))
                        .at_row(r as u32)
                })?;
                let disc_s = strings.get(disc[r])?;
                out[i] = Some((
                    edge_eid(&src_uid, &dst_uid, rel_type, disc_s).to_hex(),
                    rel_type.clone(),
                ));
            }
        }
        Ok(out
            .into_iter()
            .map(|o| o.expect("every requested address was filled"))
            .collect())
    }

    /// Every edge version in one segment, closes applied.
    fn edge_rows_of(&self, file: &str, id: u64, closes: &CloseIndex) -> Result<Vec<EdgeVersionOut>> {
        self.edge_rows_sel(file, id, closes, None)
    }

    /// Edge versions in one segment: all of them, or just `rows`.
    ///
    /// The selective form exists because the identity postings index hands
    /// back exact `(file, row)` pairs and the point-read path then threw that
    /// away, rebuilding the whole segment to index into it — a sha256 per row
    /// to reach two of them.
    fn edge_rows_sel(
        &self,
        file: &str,
        id: u64,
        closes: &CloseIndex,
        rows: Option<&[u32]>,
    ) -> Result<Vec<EdgeVersionOut>> {
        let seg = self.open_segment(file)?;
        let h = seg.header();
        let strings = seg.strings()?;
        let sidecar = seg.sidecar();
        let vt_s = seg.i64_column("vt_s")?;
        let vt_e = if h.vt_e_elided {
            None
        } else {
            Some(seg.i64_column("vt_e")?)
        };
        let src = seg.u32_column("src_id")?;
        let dst = seg.u32_column("dst_id")?;
        let rel = seg.u16_column("rel_code")?;
        let vid_hi = seg.u64_column("vid64")?;
        let vid_lo = seg.u32_column("vid_lo32")?;
        let props = seg.u32_column("props_ref")?;
        let disc = seg.u32_column("disc_ref")?;
        let source = seg.u32_column("source_ref")?;
        let prov = seg.u32_column("prov_ref")?;

        let emit = |i: usize| -> Result<EdgeVersionOut> {
            let src_uid = self.uid_of(src[i])?;
            let dst_uid = self.uid_of(dst[i])?;
            let rel_type = h
                .rel_types
                .get(rel[i] as usize)
                .ok_or_else(|| {
                    EngineError::corrupt(format!("rel_code {} has no entry", rel[i])).at_row(i as u32)
                })?
                .clone();
            let disc_s = strings.get(disc[i])?.to_string();
            // a close run outranks a folded sidecar entry: later transaction
            let from_run = closes.tt_e(id, i as u32);
            let tt_e = if from_run != OPEN_END {
                from_run
            } else {
                sidecar.tt_e(i as u32)
            };
            Ok(EdgeVersionOut {
                eid: edge_eid(&src_uid, &dst_uid, &rel_type, &disc_s).to_hex(),
                vid: Id96 {
                    hi: vid_hi[i],
                    lo: vid_lo[i],
                }
                .to_hex(),
                src: src_uid,
                dst: dst_uid,
                rel_type,
                disc: disc_s,
                vt_s: vt_s[i],
                vt_e: vt_e.map(|c| c[i]).unwrap_or(vt_s[i] + 1),
                tt_s: h.tt_s_at(i as u32)?,
                tt_e,
                props: strings.get(props[i])?.to_string(),
                source: strings.get(source[i])?.to_string(),
                provenance_ref: strings.get_opt(prov[i])?.map(str::to_string),
            })
        };
        collect_rows(vt_s.len(), rows, emit)
    }

    fn node_rows_of(&self, file: &str, id: u64, closes: &CloseIndex) -> Result<Vec<NodeVersionOut>> {
        self.node_rows_sel(file, id, closes, None)
    }

    /// Node versions in one segment: all of them, or just `rows`.
    fn node_rows_sel(
        &self,
        file: &str,
        id: u64,
        closes: &CloseIndex,
        rows: Option<&[u32]>,
    ) -> Result<Vec<NodeVersionOut>> {
        let seg = self.open_segment(file)?;
        let h = seg.header();
        let strings = seg.strings()?;
        let sidecar = seg.sidecar();
        let vt_s = seg.i64_column("vt_s")?;
        let vt_e = if h.vt_e_elided {
            None
        } else {
            Some(seg.i64_column("vt_e")?)
        };
        let uid_id = seg.u32_column("uid_id")?;
        let vid_hi = seg.u64_column("vid64")?;
        let vid_lo = seg.u32_column("vid_lo32")?;
        let label = seg.u32_column("label_ref")?;
        let props = seg.u32_column("props_ref")?;
        let source = seg.u32_column("source_ref")?;
        let prov = seg.u32_column("prov_ref")?;

        let emit = |i: usize| -> Result<NodeVersionOut> {
            let from_run = closes.tt_e(id, i as u32);
            let tt_e = if from_run != OPEN_END {
                from_run
            } else {
                sidecar.tt_e(i as u32)
            };
            Ok(NodeVersionOut {
                vid: Id96 {
                    hi: vid_hi[i],
                    lo: vid_lo[i],
                }
                .to_hex(),
                uid: self.uid_of(uid_id[i])?,
                label: strings.get(label[i])?.to_string(),
                vt_s: vt_s[i],
                vt_e: vt_e.map(|c| c[i]).unwrap_or(vt_s[i] + 1),
                tt_s: h.tt_s_at(i as u32)?,
                tt_e,
                props: strings.get(props[i])?.to_string(),
                source: strings.get(source[i])?.to_string(),
                provenance_ref: strings.get_opt(prov[i])?.map(str::to_string),
            })
        };
        collect_rows(vt_s.len(), rows, emit)
    }

    /// One staged edge row, materialized. Deriving `eid` is a sha256 and two
    /// dictionary lookups, which is why the callers below select their rows
    /// first and materialize second.
    fn staged_edge_out(&self, r: &crate::row::EdgeRow) -> Result<EdgeVersionOut> {
        let src = self.uid_of(r.src_id)?;
        let dst = self.uid_of(r.dst_id)?;
        Ok(EdgeVersionOut {
            eid: edge_eid(&src, &dst, &r.rel_type, &r.disc).to_hex(),
            vid: r.vid.to_hex(),
            src,
            dst,
            rel_type: r.rel_type.clone(),
            disc: r.disc.clone(),
            vt_s: r.vt_s,
            vt_e: r.vt_e,
            tt_s: r.tt_s,
            tt_e: self.staged_closes().get(&r.vid).copied().unwrap_or(OPEN_END),
            props: r.props.clone(),
            source: r.source.clone(),
            provenance_ref: r.provenance_ref.clone(),
        })
    }

    fn staged_node_out(&self, r: &crate::row::NodeRow) -> Result<NodeVersionOut> {
        Ok(NodeVersionOut {
            vid: r.vid.to_hex(),
            uid: self.uid_of(r.uid_id)?,
            label: r.label.clone(),
            vt_s: r.vt_s,
            vt_e: r.vt_e,
            tt_s: r.tt_s,
            tt_e: self.staged_closes().get(&r.vid).copied().unwrap_or(OPEN_END),
            props: r.props.clone(),
            source: r.source.clone(),
            provenance_ref: r.provenance_ref.clone(),
        })
    }

    /// Edge versions staged in the open batch, if any.
    fn staged_edge_versions(&self) -> Result<Vec<EdgeVersionOut>> {
        self.staged_edges()
            .iter()
            .map(|r| self.staged_edge_out(r))
            .collect()
    }

    fn staged_node_versions(&self) -> Result<Vec<NodeVersionOut>> {
        self.staged_nodes()
            .iter()
            .map(|r| self.staged_node_out(r))
            .collect()
    }

    /// Staged edge versions that may belong to one identity, through the
    /// staging index — the read-your-own-writes half of a point read.
    ///
    /// A point read runs once per op and staging grows with the batch, so
    /// materializing every staged row here (an `eid` per row per read) made
    /// a k-op batch cost O(k²): 0.077 s at 500 asserts, 22 s at 8,000.
    /// `key` is a prefix, so these are candidates; the caller's exact `eid`
    /// comparison still decides, exactly as it does for committed rows.
    fn staged_edge_versions_for(&self, key: u64) -> Result<Vec<EdgeVersionOut>> {
        let positions = self.staging().edge_positions(key, |r| {
            Ok(edge_eid(
                &self.uid_of(r.src_id)?,
                &self.uid_of(r.dst_id)?,
                &r.rel_type,
                &r.disc,
            )
            .hi)
        })?;
        let staged = self.staged_edges();
        positions
            .iter()
            .map(|&i| self.staged_edge_out(&staged[i as usize]))
            .collect()
    }

    /// Staged node versions for one uid. Node identity is dense, so this is
    /// exact: a uid the dictionary does not know cannot have a staged row.
    fn staged_node_versions_for(&self, uid: &str) -> Result<Vec<NodeVersionOut>> {
        let Some(uid_id) = self.dict().dense_id(uid) else {
            return Ok(Vec::new());
        };
        let positions = self.staging().node_positions(uid_id);
        let staged = self.staged_nodes();
        positions
            .iter()
            .map(|&i| self.staged_node_out(&staged[i as usize]))
            .collect()
    }

    // ------------------------------------------------------------------ //
    // public reads                                                        //
    // ------------------------------------------------------------------ //

    /// Fold any not-yet-indexed segment into the identity postings.
    ///
    /// Deriving `eid` per row is the expensive part, so it happens once per
    /// row ever rather than once per lookup — which is the whole point of the
    /// index. Node identities are uids and need no derivation. The vid
    /// postings ride in the same pass: they only read the `vid64` column,
    /// and one `indexed` set keeps both maps consistent.
    fn index_segments(&self, kind: RowKind) -> Result<()> {
        // The cached map, not `edge_files()` (D-077). This runs on *every*
        // point read to notice new segments, and rebuilding the file list
        // here — a String clone and a filename parse per segment — was the
        // larger half of the per-call O(segments) cost; caching only the
        // `by_id` lookup left this one behind and bought 2.2x of a
        // forecast 8x.
        let files = self.segment_files();
        let pending: Vec<(String, u64)> = {
            let p = self.postings_for(kind).lock().expect("postings mutex poisoned");
            files
                .of(kind)
                .iter()
                .filter(|(id, _)| !p.indexed.contains(*id))
                .map(|(id, f)| (f.clone(), *id))
                .collect()
        };
        for (file, id) in pending {
            let seg = self.open_segment(&file)?;
            let mut entries: Vec<(u64, u32)> = Vec::with_capacity(seg.rows() as usize);
            match kind {
                RowKind::Edge => {
                    let strings = seg.strings()?;
                    let src = seg.u32_column("src_id")?;
                    let dst = seg.u32_column("dst_id")?;
                    let rel = seg.u16_column("rel_code")?;
                    let disc = seg.u32_column("disc_ref")?;
                    let rel_types = &seg.header().rel_types;
                    for i in 0..src.len() {
                        let eid = edge_eid(
                            &self.uid_of(src[i])?,
                            &self.uid_of(dst[i])?,
                            rel_types.get(rel[i] as usize).map(String::as_str).unwrap_or(""),
                            strings.get(disc[i])?,
                        );
                        entries.push((eid.hi, i as u32));
                    }
                }
                RowKind::Node => {
                    let uid_id = seg.u32_column("uid_id")?;
                    for (i, &u) in uid_id.iter().enumerate() {
                        entries.push((uid_key(&self.uid_of(u)?), i as u32));
                    }
                }
            }
            let vid_hi = seg.u64_column("vid64")?;
            // the sidecar carries the closes folded in at seal time, so a row
            // born already closed (a same-batch carve) never enters the
            // open-version index; closes that arrive later as close runs are
            // pruned by `locate_open` (D-076)
            let sidecar = seg.sidecar();
            let mut p = self.postings_for(kind).lock().expect("postings mutex poisoned");
            for (key, row) in entries {
                p.by_identity.entry(key).or_default().push((id, row));
                if sidecar.tt_e(row) == OPEN_END {
                    p.open_rows.entry(key).or_default().push((id, row));
                }
            }
            for (i, &hi) in vid_hi.iter().enumerate() {
                p.by_vid.entry(hi).or_default().push((id, i as u32));
            }
            p.indexed.insert(id);
        }
        Ok(())
    }

    fn postings_for(&self, kind: RowKind) -> &std::sync::Mutex<crate::store::Postings> {
        match kind {
            RowKind::Edge => self.edge_postings(),
            RowKind::Node => self.node_postings(),
        }
    }

    /// Candidate `(file, row)` locations for one identity.
    fn locate(&self, kind: RowKind, key: u64) -> Result<Vec<(String, u32)>> {
        self.index_segments(kind)?;
        let files = self.segment_files();
        let by_id = files.of(kind);
        let p = self.postings_for(kind).lock().expect("postings mutex poisoned");
        Ok(p
            .by_identity
            .get(&key)
            .map(|v| {
                v.iter()
                    .filter_map(|(seg, row)| by_id.get(seg).map(|f| (f.clone(), *row)))
                    .collect()
            })
            .unwrap_or_default())
    }

    /// Does this `as_of_tt` ask about *current* belief? (D-076)
    ///
    /// Only the current-belief question has a standing answer worth indexing:
    /// "which rows are open now". Every historical `as_of` is a different
    /// question per tt and keeps the walk.
    fn is_current_belief(as_of_tt: i64) -> bool {
        crate::clamp_tt(as_of_tt) >= OPEN_END - 1
    }

    /// Currently-open `(file, row)` locations for one identity (D-076).
    ///
    /// **`open_rows` is a superset, and that is the design.** Maintaining an
    /// exact set would mean removing a row the moment something closed it,
    /// which needs a `(segment, row) -> identity` reverse map the store does
    /// not have — closes name physical addresses, not identities. Instead the
    /// index is append-only like `by_identity` (rows join it at index time if
    /// their segment's sidecar says they are open), and closes are discovered
    /// *here*, against the in-memory `CloseIndex`, then **pruned in place**.
    ///
    /// So each row is examined exactly once more after it closes, and never
    /// again. For the correction workload — one row closed and one opened per
    /// correction, with a read before each — the list stays at ~2 entries
    /// however deep the identity's history gets, which is the whole point.
    ///
    /// Rows closed by the *open batch* are dropped too but not pruned: the
    /// committed index still rightly thinks they are open, and the batch may
    /// yet roll back.
    fn locate_open(&self, kind: RowKind, key: u64) -> Result<Vec<(String, u32)>> {
        self.index_segments(kind)?;
        // **Committed closes only.** Pruning against `close_index()` would use
        // the open batch's pending overlay and permanently delete rows an
        // abandoned batch must give back — the index is not transactional, so
        // nothing uncommitted may ever mutate it. Pending closes are applied
        // below as a filter instead, which a rollback simply forgets.
        let closes = self.committed_close_index()?;
        let files = self.segment_files();
        let by_id = files.of(kind);
        let closed_here = self.pending_closed_rows(kind);

        let mut p = self.postings_for(kind).lock().expect("postings mutex poisoned");
        let Some(candidates) = p.open_rows.get_mut(&key) else {
            return Ok(Vec::new());
        };
        // prune anything a committed close has since closed, and anything the
        // current manifest no longer lists (compaction dropped its segment)
        candidates.retain(|(seg, row)| {
            by_id.contains_key(seg) && closes.tt_e(*seg, *row) == OPEN_END
        });
        Ok(candidates
            .iter()
            .filter(|(seg, row)| !closed_here.contains(&(*seg, *row)))
            .filter_map(|(seg, row)| by_id.get(seg).map(|f| (f.clone(), *row)))
            .collect())
    }

    /// Physical location of one committed version, or None (WP-N4).
    ///
    /// Candidates come from the vid postings; the full vid at the row
    /// decides, exactly as the identity paths verify their prefix hits.
    /// Candidates are filtered through the current manifest before any file
    /// is opened, so a postings entry for a segment that compaction dropped
    /// (or gc deleted) can never resolve.
    pub(crate) fn locate_vid(&self, kind: RowKind, vid: Id96) -> Result<Option<(u64, u32)>> {
        self.index_segments(kind)?;
        let files = self.segment_files();
        let by_id = files.of(kind);
        let candidates: Vec<(u64, u32)> = {
            let p = self.postings_for(kind).lock().expect("postings mutex poisoned");
            p.by_vid.get(&vid.hi).cloned().unwrap_or_default()
        };
        for (seg_id, row) in candidates {
            let Some(file) = by_id.get(&seg_id) else { continue };
            let seg = self.open_segment(file)?;
            // a prefix hit is a candidate; the full vid decides
            if seg.u64_column("vid64")?[row as usize] == vid.hi
                && seg.u32_column("vid_lo32")?[row as usize] == vid.lo
            {
                return Ok(Some((seg_id, row)));
            }
        }
        Ok(None)
    }

    /// Every edge version ever committed, plus anything staged in the open
    /// batch. Order is unspecified — the digest sorts (spec §2.3).
    pub fn all_edge_versions(&self) -> Result<Vec<EdgeVersionOut>> {
        let closes = self.close_index()?;
        let mut out = Vec::new();
        for (file, id) in self.edge_files() {
            out.extend(self.edge_rows_of(&file, id, &closes)?);
        }
        out.extend(self.staged_edge_versions()?);
        Ok(out)
    }

    pub fn all_node_versions(&self) -> Result<Vec<NodeVersionOut>> {
        let closes = self.close_index()?;
        let mut out = Vec::new();
        for (file, id) in self.node_files() {
            out.extend(self.node_rows_of(&file, id, &closes)?);
        }
        out.extend(self.staged_node_versions()?);
        Ok(out)
    }

    /// Versions of one logical edge believed at `as_of_tt`, ordered by `vt_s`
    /// — the ordering `believed_edge_versions` promises.
    pub fn believed_edge_versions(&self, eid: &str, as_of_tt: i64) -> Result<Vec<EdgeVersionOut>> {
        self.assert_full_belief(as_of_tt)?;
        let key = Id96::from_hex(eid).map(|i| i.hi)?;
        let closes = self.close_index()?;
        let mut out = Vec::new();
        // group candidate rows by file so each segment is opened once
        let mut by_file: std::collections::BTreeMap<String, Vec<u32>> = Default::default();
        // see believed_node_versions: current belief through the index (D-076)
        let located = if Self::is_current_belief(as_of_tt) {
            self.locate_open(RowKind::Edge, key)?
        } else {
            self.locate(RowKind::Edge, key)?
        };
        for (file, row) in located {
            by_file.entry(file).or_default().push(row);
        }
        for (file, rows) in by_file {
            let id = segment_id_of(&file);
            // a prefix hit is a candidate; the full eid decides
            for r in self.edge_rows_sel(&file, id, &closes, Some(&rows))? {
                if r.eid == eid && believed_at(r.tt_s, r.tt_e, as_of_tt) {
                    out.push(r);
                }
            }
        }
        out.extend(
            self.staged_edge_versions_for(key)?
                .into_iter()
                .filter(|r| r.eid == eid && believed_at(r.tt_s, r.tt_e, as_of_tt)),
        );
        out.sort_by_key(|r| (r.vt_s, r.vid.clone()));
        Ok(out)
    }

    pub fn believed_node_versions(&self, uid: &str, as_of_tt: i64) -> Result<Vec<NodeVersionOut>> {
        self.assert_full_belief(as_of_tt)?;
        let closes = self.close_index()?;
        let mut out = Vec::new();
        let mut by_file: std::collections::BTreeMap<String, Vec<u32>> = Default::default();
        // current belief goes through the open-version index, which returns
        // the ~1 row still open instead of every version this identity has
        // ever had (D-076); any historical as_of keeps the walk
        let located = if Self::is_current_belief(as_of_tt) {
            self.locate_open(RowKind::Node, uid_key(uid))?
        } else {
            self.locate(RowKind::Node, uid_key(uid))?
        };
        for (file, row) in located {
            by_file.entry(file).or_default().push(row);
        }
        for (file, rows) in by_file {
            let id = segment_id_of(&file);
            // a uid_key hit is a candidate; the full uid decides
            for r in self.node_rows_sel(&file, id, &closes, Some(&rows))? {
                if r.uid == uid && believed_at(r.tt_s, r.tt_e, as_of_tt) {
                    out.push(r);
                }
            }
        }
        out.extend(
            self.staged_node_versions_for(uid)?
                .into_iter()
                .filter(|r| r.uid == uid && believed_at(r.tt_s, r.tt_e, as_of_tt)),
        );
        out.sort_by_key(|r| (r.vt_s, r.vid.clone()));
        Ok(out)
    }

    /// Which of `uids` have at least one believed version — the batched
    /// existence probe bulk ingest leans on.
    /// Two implementations, chosen by size, because the probe and the scan
    /// have opposite shapes: a point probe through the identity postings is
    /// flat in store size and linear in `uids`, while materializing every
    /// node version is flat in `uids` and linear in store size.
    ///
    /// Measured (100k events, 200k node versions, macOS): the scan costs
    /// **50.5 ms whether it is asked about 2 uids or 20,000**, while one
    /// probe costs **0.005 ms** — 0.25 µs per stored node version against
    /// 5 µs per uid, so the probe wins below roughly one uid per 20 stored
    /// versions. Bulk ingest asks about ~100,000 uids at a time and stays on
    /// the scan; a singleton append asks about two and used to pay for the
    /// whole store.
    ///
    /// That singleton case was 57.7 ms of a 90 ms single-row write — the
    /// actual singleton-write floor, which `engine_lessons.md` §7 had
    /// attributed to fsyncs (30 ms) and `docs/eval_writes.md` to manifest
    /// size. It is the sixth appearance of the same shape in the
    /// misdiagnosis table: *a small lookup rebuilding the whole store*.
    pub fn nodes_with_believed_versions(
        &self,
        uids: &[String],
        as_of_tt: i64,
    ) -> Result<HashSet<String>> {
        self.assert_full_belief(as_of_tt)?;
        let stored = self.manifest().stats.n_node_versions;
        if (uids.len() as u64).saturating_mul(PROBE_COST_RATIO) < stored {
            let mut out = HashSet::new();
            for uid in uids {
                if !self.believed_node_versions(uid, as_of_tt)?.is_empty() {
                    out.insert(uid.clone());
                }
            }
            return Ok(out);
        }
        let wanted: HashSet<&str> = uids.iter().map(String::as_str).collect();
        Ok(self
            .all_node_versions()?
            .into_iter()
            .filter(|r| believed_at(r.tt_s, r.tt_e, as_of_tt) && wanted.contains(r.uid.as_str()))
            .map(|r| r.uid)
            .collect())
    }

    /// Entity resolution (O12): match a query against uids and names, and
    /// report each matched entity through its latest believed version.
    ///
    /// Scored exactly as the operator defines: 0 for an exact uid, 1 for a
    /// uid substring, 2 for a name substring, lowest wins. `name` is read
    /// from the promoted `name_ref` column, so no JSON is parsed per row —
    /// that parse over every node version was the whole cost of this
    /// operator. Name matching is over JSON *string* names only (D-031): the
    /// typed column indexes nothing else, and the reference implementation's
    /// former `str()` coercion matched text like "None" that no version
    /// actually contained.
    ///
    /// Returns `(uid, score, label, props)`. The canonical label and props
    /// come from the latest believed version by `(vt_s, vid)` **whether or
    /// not that version itself matched** — an entity found by an old name
    /// still resolves to what it is now, not to what it was when it matched.
    /// The vid tiebreak is unreachable for believed versions of one uid
    /// (disjoint valid intervals) but keeps every implementation
    /// order-independent by construction.
    pub fn resolve_entities(
        &self,
        query: &str,
        as_of_tt: i64,
    ) -> Result<Vec<(String, u8, String, String)>> {
        self.assert_full_belief(as_of_tt)?;
        let ql = query.to_lowercase();
        let closes = self.close_index()?;
        // uid -> (best matching score, canonical (vt_s, vid), label, props)
        type Entry = (u8, (i64, Id96), String, String);
        let mut best: HashMap<String, Entry> = HashMap::new();
        let mut upsert = |uid: String,
                          score: Option<u8>,
                          key: (i64, Id96),
                          label: &str,
                          props: &str| {
            let e = best.entry(uid).or_insert_with(|| {
                (u8::MAX, key, label.to_string(), props.to_string())
            });
            if let Some(sc) = score {
                e.0 = e.0.min(sc);
            }
            if key >= e.1 {
                e.1 = key;
                e.2 = label.to_string();
                e.3 = props.to_string();
            }
        };

        for (file, id) in self.node_files() {
            let seg = self.open_segment(&file)?;
            let h = seg.header();
            let strings = seg.strings()?;
            let sidecar = seg.sidecar();
            let vt_s = seg.i64_column("vt_s")?;
            let uid_id = seg.u32_column("uid_id")?;
            let vid_hi = seg.u64_column("vid64")?;
            let vid_lo = seg.u32_column("vid_lo32")?;
            let label_ref = seg.u32_column("label_ref")?;
            let props_ref = seg.u32_column("props_ref")?;
            // segments written before the column was promoted simply have no
            // names to match on; uid matching still works
            let name_ref = seg.u32_column("name_ref").ok();

            for i in 0..vt_s.len() {
                let from_run = closes.tt_e(id, i as u32);
                let tt_e = if from_run != OPEN_END {
                    from_run
                } else {
                    sidecar.tt_e(i as u32)
                };
                if !believed_at(h.tt_s_at(i as u32)?, tt_e, as_of_tt) {
                    continue;
                }
                let uid = self.uid_of(uid_id[i])?;
                let name = match name_ref {
                    Some(col) => strings.get_opt(col[i])?,
                    None => None,
                };
                let score = if uid == query {
                    Some(0u8)
                } else if uid.to_lowercase().contains(&ql) {
                    Some(1)
                } else if name.is_some_and(|n| !n.is_empty() && n.to_lowercase().contains(&ql)) {
                    Some(2)
                } else {
                    None // no match — still a candidate for canonical state
                };
                upsert(
                    uid,
                    score,
                    (vt_s[i], Id96 { hi: vid_hi[i], lo: vid_lo[i] }),
                    strings.get(label_ref[i])?,
                    strings.get(props_ref[i])?,
                );
            }
        }

        // rows staged in an open batch: a batch must read its own writes
        let staged_closes = self.staged_closes();
        for r in self.staged_nodes() {
            let tt_e = staged_closes.get(&r.vid).copied().unwrap_or(OPEN_END);
            if !believed_at(r.tt_s, tt_e, as_of_tt) {
                continue;
            }
            let uid = self.uid_of(r.uid_id)?;
            let name = crate::segment::name_of(&r.props);
            let score = if uid == query {
                Some(0u8)
            } else if uid.to_lowercase().contains(&ql) {
                Some(1)
            } else if name.as_deref().is_some_and(|n| !n.is_empty() && n.to_lowercase().contains(&ql)) {
                Some(2)
            } else {
                None
            };
            upsert(uid, score, (r.vt_s, r.vid), &r.label, &r.props);
        }

        let mut out: Vec<(String, u8, String, String)> = best
            .into_iter()
            .filter(|(_, e)| e.0 != u8::MAX) // tracked for state, never matched
            .map(|(uid, (score, _, label, props))| (uid, score, label, props))
            .collect();
        out.sort_by(|a, b| a.1.cmp(&b.1).then_with(|| a.0.cmp(&b.0)));
        Ok(out)
    }

    /// Running statistics, building them once if this is the first call.
    ///
    /// After the initial build every commit folds its own batch in, so a
    /// write-then-read loop never rescans. Compaction rewrites rows without
    /// changing content, so it leaves these untouched by construction.
    ///
    /// The build folds integer columns segment by segment. It used to route
    /// through `all_edge_versions`, which materializes every row — two
    /// dictionary lookups, several string allocations, and a sha256-derived
    /// `eid` per row that statistics never read — as one store-sized
    /// transient `Vec`. At 10M rows that transient alone exceeded a 2 GB
    /// memory cap (docs/eval_resources.md §14.2 rerun), so the first query
    /// OOM'd before the byte-budgeted segment cache could matter. Counts
    /// cover every stored row, belief ignored, exactly as before — the
    /// DuckDB adapter must agree here or `estimate_cost` diverges.
    pub fn stats_accum(&self) -> Result<crate::store::StatsAccum> {
        {
            let cell = self.stats_cell().lock().expect("stats mutex poisoned");
            if let Some(acc) = cell.as_ref() {
                return Ok(acc.clone());
            }
        }
        let mut acc = crate::store::StatsAccum::default();
        for (file, _id) in self.edge_files() {
            let seg = self.open_segment(&file)?;
            let h = seg.header();
            let vt_s = seg.i64_column("vt_s")?;
            let vt_e = if h.vt_e_elided {
                None
            } else {
                Some(seg.i64_column("vt_e")?)
            };
            let src = seg.u32_column("src_id")?;
            let rel = seg.u16_column("rel_code")?;
            // per-code counts within the segment, names merged once at the
            // end — a string allocation per row was most of the fold
            let mut code_counts = vec![0u64; h.rel_types.len()];
            for i in 0..vt_s.len() {
                let ve = vt_e.map(|c| c[i]).unwrap_or(vt_s[i] + 1);
                acc.n_edge_versions += 1;
                acc.vt_min = Some(acc.vt_min.map_or(vt_s[i], |m| m.min(vt_s[i])));
                let ve = if ve >= OPEN_END { vt_s[i] + 1 } else { ve };
                acc.vt_max = Some(acc.vt_max.map_or(ve, |m| m.max(ve)));
                if let Some(c) = code_counts.get_mut(rel[i] as usize) {
                    *c += 1;
                }
                *acc.out_degree.entry(src[i]).or_default() += 1;
            }
            for (code, n) in code_counts.into_iter().enumerate() {
                if n > 0 {
                    *acc
                        .rel_type_counts
                        .entry(h.rel_types[code].clone())
                        .or_default() += n;
                }
            }
        }
        for r in self.staged_edges() {
            acc.add_edge(r.vt_s, r.vt_e, &r.rel_type, r.src_id);
        }
        acc.n_node_versions = self
            .manifest()
            .node_store
            .iter()
            .map(|e| e.rows as u64)
            .sum::<u64>()
            + self.staged_nodes().len() as u64;
        let mut cell = self.stats_cell().lock().expect("stats mutex poisoned");
        // an open batch's rows are already counted above; do not cache a
        // snapshot that a rollback could invalidate
        if !self.in_batch() {
            *cell = Some(acc.clone());
        }
        Ok(acc)
    }

    /// Canonical-JSON props for specific version ids, returned verbatim.
    /// Props for a handful of version ids.
    ///
    /// Deliberately does not route through `all_*_versions`. That materializes
    /// every row in the store — two dictionary lookups, several string
    /// allocations, and a sha256 to derive `eid` — so a lookup of 16 vids cost
    /// 353 ms at 200k edge versions, and cost exactly the same for one vid as
    /// for 256. It was the whole of `diff_snapshots`, its only caller.
    ///
    /// The sweep below touches three integer columns and reads a string only
    /// once a vid actually matches. It is still O(rows): vids are hashes, so
    /// segments have no order to search them by and there is no vid index.
    /// Only the constant changed — but the constant was the problem.
    pub fn props_for_vids(&self, kind: RowKind, vids: &[String]) -> Result<HashMap<String, String>> {
        // Keyed by the caller's own spelling, so the result is looked up with
        // the same string that was asked for. A vid that is not a well-formed
        // identity cannot name a row, so it is dropped rather than swept for.
        let wanted: HashMap<(u64, u32), &str> = vids
            .iter()
            .filter_map(|v| Id96::from_hex(v).ok().map(|id| ((id.hi, id.lo), v.as_str())))
            .collect();
        let mut out = HashMap::with_capacity(wanted.len());
        if wanted.is_empty() {
            return Ok(out);
        }
        let files = match kind {
            RowKind::Edge => self.edge_files(),
            RowKind::Node => self.node_files(),
        };
        for (file, _id) in files {
            let seg = self.open_segment(&file)?;
            let hi = seg.u64_column("vid64")?;
            let lo = seg.u32_column("vid_lo32")?;
            let props = seg.u32_column("props_ref")?;
            let strings = seg.strings()?;
            for i in 0..hi.len() {
                if let Some(&hex) = wanted.get(&(hi[i], lo[i])) {
                    out.insert(hex.to_string(), strings.get(props[i])?.to_string());
                }
            }
        }
        // Staged rows are inserted last so they win, as they do in
        // `all_*_versions`: a batch must read its own writes.
        match kind {
            RowKind::Edge => {
                for r in self.staged_edges() {
                    if let Some(&hex) = wanted.get(&(r.vid.hi, r.vid.lo)) {
                        out.insert(hex.to_string(), r.props.clone());
                    }
                }
            }
            RowKind::Node => {
                for r in self.staged_nodes() {
                    if let Some(&hex) = wanted.get(&(r.vid.hi, r.vid.lo)) {
                        out.insert(hex.to_string(), r.props.clone());
                    }
                }
            }
        }
        Ok(out)
    }
}

/// Materialize either every row of a segment or just the selected ones.
///
/// Row indices that fall outside the segment are skipped rather than
/// erroring, matching what the previous `all.get(row)` lookup did: a stale
/// posting names a row that is no longer there, and that is not corruption.
fn collect_rows<T>(
    n: usize,
    rows: Option<&[u32]>,
    emit: impl Fn(usize) -> Result<T>,
) -> Result<Vec<T>> {
    match rows {
        Some(sel) => {
            let mut out = Vec::with_capacity(sel.len());
            for &row in sel {
                let i = row as usize;
                if i < n {
                    out.push(emit(i)?);
                }
            }
            Ok(out)
        }
        None => {
            let mut out = Vec::with_capacity(n);
            for i in 0..n {
                out.push(emit(i)?);
            }
            Ok(out)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::version_vid;
    use crate::manifest::EventLogRef;
    use crate::row::{EdgeRow, NodeRow};
    use std::path::PathBuf;

    fn tmp_root(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-read-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        p
    }

    fn edge(src_id: u32, dst_id: u32, src: &str, dst: &str, vt_s: i64, tt_s: i64, i: u32) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid(src, dst, "R", &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id,
            dst_id,
            rel_type: "R".into(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s,
            props: format!(r#"{{"i":{i}}}"#),
            source: "ingest".into(),
            provenance_ref: if i.is_multiple_of(2) { None } else { Some("p/1".into()) },
        }
    }

    /// A store with two edges and one node, committed at tt=100.
    fn seeded(name: &str) -> (PathBuf, NativeStore, Vec<EdgeRow>) {
        let root = tmp_root(name);
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let b = s.ensure_entity("n2", "Node").unwrap();
        let rows = vec![
            edge(a, b, "n1", "n2", 10, 100, 0),
            edge(b, a, "n2", "n1", 20, 100, 1),
        ];
        for r in &rows {
            s.stage_edge(r.clone()).unwrap();
        }
        s.stage_node(NodeRow {
            vid: version_vid("n1", 100, 5),
            uid_id: a,
            label: "Node".into(),
            vt_s: 5,
            vt_e: OPEN_END,
            tt_s: 100,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        })
        .unwrap();
        s.commit(EventLogRef::default()).unwrap();
        (root, s, rows)
    }

    #[test]
    fn all_edge_versions_reconstructs_every_field() {
        let (_root, s, rows) = seeded("all-edges");
        let mut got = s.all_edge_versions().unwrap();
        got.sort_by_key(|r| r.vt_s);
        assert_eq!(got.len(), 2);
        for (g, want) in got.iter().zip(&rows) {
            assert_eq!(g.vid, want.vid.to_hex());
            assert_eq!(g.rel_type, want.rel_type);
            assert_eq!(g.disc, want.disc);
            assert_eq!(g.vt_s, want.vt_s);
            assert_eq!(g.vt_e, want.vt_e);
            assert_eq!(g.tt_s, want.tt_s);
            assert_eq!(g.tt_e, OPEN_END, "an uncorrected row is still believed");
            assert_eq!(g.props, want.props, "props come back byte-identical");
            assert_eq!(g.source, want.source);
            assert_eq!(g.provenance_ref, want.provenance_ref);
        }
        // uids survive the dense-id round trip, and direction is preserved
        assert_eq!((got[0].src.as_str(), got[0].dst.as_str()), ("n1", "n2"));
        assert_eq!((got[1].src.as_str(), got[1].dst.as_str()), ("n2", "n1"));
    }

    #[test]
    fn derived_eid_matches_the_identity_the_row_was_written_under() {
        let (_root, s, rows) = seeded("eid");
        let got = s.all_edge_versions().unwrap();
        for want in &rows {
            let expect = edge_eid("n1", "n2", "R", &want.disc).to_hex();
            let expect_rev = edge_eid("n2", "n1", "R", &want.disc).to_hex();
            assert!(
                got.iter().any(|g| g.eid == expect || g.eid == expect_rev),
                "no row carries the re-derived eid for disc {}",
                want.disc
            );
        }
    }

    #[test]
    fn believed_versions_filter_by_identity_and_belief() {
        let (_root, mut s, rows) = seeded("believed");
        let eid = s.all_edge_versions().unwrap()[0].eid.clone();
        assert_eq!(s.believed_edge_versions(&eid, OPEN_END).unwrap().len(), 1);
        assert!(
            s.believed_edge_versions(&eid, 99).unwrap().is_empty(),
            "nothing was believed before the batch that wrote it"
        );

        // close it, then check both belief states
        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, rows[0].vid, 200).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        assert!(s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty());
        assert_eq!(s.believed_edge_versions(&eid, 150).unwrap().len(), 1);
        // and the row is still present in the full listing, with its close time
        let all = s.all_edge_versions().unwrap();
        assert_eq!(all.len(), 2);
        assert!(all.iter().any(|r| r.tt_e == 200));
    }

    #[test]
    fn nodes_and_probes_round_trip() {
        let (_root, s, _) = seeded("nodes");
        let nodes = s.all_node_versions().unwrap();
        assert_eq!(nodes.len(), 1);
        assert_eq!(nodes[0].uid, "n1");
        assert_eq!(nodes[0].label, "Node");
        assert_eq!(nodes[0].vt_e, OPEN_END);

        assert_eq!(s.believed_node_versions("n1", OPEN_END).unwrap().len(), 1);
        assert!(s.believed_node_versions("n2", OPEN_END).unwrap().is_empty());

        let probe = s
            .nodes_with_believed_versions(
                &["n1".to_string(), "n2".to_string(), "nope".to_string()],
                OPEN_END,
            )
            .unwrap();
        assert_eq!(probe, HashSet::from(["n1".to_string()]));
    }

    /// `nodes_with_believed_versions` picks between a postings probe and a
    /// full materialization by size. Two implementations of one contract is
    /// exactly the arrangement where a divergence hides, and the write path
    /// (`_ingest_events`) is what would silently create duplicate nodes if
    /// the cheap branch ever said "absent" about something present. So: both
    /// branches, same store, same answer, and each branch asserted to be the
    /// one that ran.
    #[test]
    fn both_existence_probes_agree_and_each_branch_is_reachable() {
        let root = tmp_root("probe-branches");
        let mut s = NativeStore::open(&root).unwrap();
        s.begin(100).unwrap();
        let mut present: Vec<String> = Vec::new();
        for i in 0..200u32 {
            let uid = format!("n{i}");
            let id = s.ensure_entity(&uid, "Node").unwrap();
            s.stage_node(NodeRow {
                vid: version_vid(&uid, 100, 10),
                uid_id: id,
                label: "Node".into(),
                vt_s: 10,
                vt_e: OPEN_END,
                tt_s: 100,
                props: "{}".into(),
                source: "ingest".into(),
                provenance_ref: None,
            })
            .unwrap();
            present.push(uid);
        }
        s.commit(EventLogRef::default()).unwrap();
        let stored = s.manifest().stats.n_node_versions;
        assert_eq!(stored, 200);

        let absent: Vec<String> = (0..200).map(|i| format!("ghost{i}")).collect();
        let mixed: Vec<String> = present
            .iter()
            .take(3)
            .chain(absent.iter().take(3))
            .cloned()
            .collect();

        // small ask: the probe branch, by the ratio rule
        assert!((mixed.len() as u64) * PROBE_COST_RATIO < stored);
        let probed = s.nodes_with_believed_versions(&mixed, OPEN_END).unwrap();

        // large ask: the same store, the scan branch
        let all: Vec<String> = present.iter().chain(absent.iter()).cloned().collect();
        assert!((all.len() as u64) * PROBE_COST_RATIO >= stored);
        let scanned = s.nodes_with_believed_versions(&all, OPEN_END).unwrap();

        assert_eq!(
            probed,
            mixed
                .iter()
                .filter(|u| present.contains(u))
                .cloned()
                .collect::<HashSet<String>>()
        );
        assert_eq!(scanned, present.iter().cloned().collect::<HashSet<String>>());
        for uid in &mixed {
            assert_eq!(
                probed.contains(uid),
                scanned.contains(uid),
                "the two branches disagree about {uid}"
            );
        }

        // belief is part of the answer on both paths: close every version and
        // the small ask must go empty while the past stays populated
        s.begin(200).unwrap();
        let vids: Vec<_> = s
            .all_node_versions()
            .unwrap()
            .iter()
            .map(|r| Id96::from_hex(&r.vid).expect("engine-produced vid"))
            .collect();
        for vid in vids {
            s.close_version(RowKind::Node, vid, 200).unwrap();
        }
        s.commit(EventLogRef::default()).unwrap();
        assert!(s
            .nodes_with_believed_versions(&mixed, OPEN_END)
            .unwrap()
            .is_empty());
        assert_eq!(
            s.nodes_with_believed_versions(&mixed, 150).unwrap().len(),
            3,
            "past belief must survive on the probe branch too"
        );
    }

    #[test]
    fn props_are_fetched_by_vid_and_returned_verbatim() {
        let (_root, s, rows) = seeded("props");
        let vids: Vec<String> = rows.iter().map(|r| r.vid.to_hex()).collect();
        let got = s.props_for_vids(RowKind::Edge, &vids).unwrap();
        assert_eq!(got.len(), 2);
        for r in &rows {
            assert_eq!(got[&r.vid.to_hex()], r.props);
        }
        // unknown vids are simply absent, never fabricated
        assert!(s
            .props_for_vids(RowKind::Edge, &["deadbeef".repeat(3)])
            .unwrap()
            .is_empty());
    }

    #[test]
    fn reads_inside_an_open_batch_see_staged_rows() {
        // base.py calls believed_* in the middle of apply_ops, so a version
        // written earlier in the batch must already be visible
        let (_root, mut s, _) = seeded("staged");
        s.begin(200).unwrap();
        let c = s.ensure_entity("n3", "Node").unwrap();
        let fresh = edge(c, c, "n3", "n3", 30, 200, 9);
        s.stage_edge(fresh.clone()).unwrap();

        let eid = edge_eid("n3", "n3", "R", &fresh.disc).to_hex();
        let believed = s.believed_edge_versions(&eid, OPEN_END).unwrap();
        assert_eq!(believed.len(), 1, "staged row must be visible in its own batch");
        assert_eq!(believed[0].vid, fresh.vid.to_hex());

        // closing it in the same batch hides it immediately
        s.close_version(RowKind::Edge, fresh.vid, 200).unwrap();
        assert!(s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty());

        // and a rollback removes it entirely
        s.rollback().unwrap();
        assert!(s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty());
        assert_eq!(s.all_edge_versions().unwrap().len(), 2);
    }

    #[test]
    fn reads_inside_an_open_batch_see_closes_against_committed_rows() {
        // the other half of read-your-own-writes, and the half D-058 found
        // missing: an ordinary correction closes a row a *previous* batch
        // committed, and `apply_ops` reads belief again before it carves
        let (_root, mut s, rows) = seeded("pending-closes");
        let eid = {
            let r = &rows[0];
            edge_eid("n1", "n2", "R", &r.disc).to_hex()
        };
        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, rows[0].vid, 200).unwrap();

        assert!(
            s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty(),
            "a version this batch closed is not believed inside it"
        );
        // the row is still there, reported with the tt_e the batch gave it
        let all = s.all_edge_versions().unwrap();
        let closed = all.iter().find(|r| r.vid == rows[0].vid.to_hex()).unwrap();
        assert_eq!(closed.tt_e, 200);
        // and the belief it had before this batch is untouched
        assert_eq!(s.believed_edge_versions(&eid, 150).unwrap().len(), 1);

        s.commit(EventLogRef::default()).unwrap();
        assert!(s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty());
    }

    #[test]
    fn an_abandoned_batch_s_closes_do_not_reach_the_next_one() {
        // the overlay is cached under (generation, closes so far), and a
        // rollback moves neither — so the cache must be dropped with it
        let (_root, mut s, rows) = seeded("rolled-back-closes");
        let eid = edge_eid("n1", "n2", "R", &rows[0].disc).to_hex();
        let other = edge_eid("n2", "n1", "R", &rows[1].disc).to_hex();

        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, rows[0].vid, 200).unwrap();
        assert!(s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty());
        s.rollback().unwrap();
        assert_eq!(s.believed_edge_versions(&eid, OPEN_END).unwrap().len(), 1);

        // same generation, same number of pending closes, different row
        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, rows[1].vid, 200).unwrap();
        assert_eq!(
            s.believed_edge_versions(&eid, OPEN_END).unwrap().len(),
            1,
            "the abandoned batch's close came back"
        );
        assert!(s.believed_edge_versions(&other, OPEN_END).unwrap().is_empty());
        s.rollback().unwrap();
    }

    #[test]
    fn a_retired_row_leaves_no_trace_of_having_been_staged() {
        // D-059: a version created and closed by one batch was believed over
        // [tt, tt) — no transaction time at all — so the batch commits as
        // though it had never staged it, and its vid is free to be reused
        let (_root, mut s, _) = seeded("retire");
        s.begin(200).unwrap();
        let c = s.ensure_entity("n3", "Node").unwrap();
        let first = edge(c, c, "n3", "n3", 30, 200, 9);
        let eid = edge_eid("n3", "n3", "R", &first.disc).to_hex();
        s.stage_edge(first.clone()).unwrap();
        s.retire_version(RowKind::Edge, first.vid).unwrap();
        assert!(s.believed_edge_versions(&eid, OPEN_END).unwrap().is_empty());

        // the replacement derives the same vid, which is only sound because
        // the row it replaces is gone rather than closed
        let mut replacement = first.clone();
        replacement.vt_e = 40;
        s.stage_edge(replacement.clone()).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        let believed = s.believed_edge_versions(&eid, OPEN_END).unwrap();
        assert_eq!(believed.len(), 1);
        assert_eq!(believed[0].vt_e, 40);
        assert_eq!(
            s.all_edge_versions()
                .unwrap()
                .iter()
                .filter(|r| r.vid == first.vid.to_hex())
                .count(),
            1,
            "the retired row must not be sealed alongside its replacement"
        );

        // retiring something no batch staged is an error, not a silent no-op
        s.begin(300).unwrap();
        assert!(s.retire_version(RowKind::Edge, first.vid).is_err());
        s.rollback().unwrap();
    }

    #[test]
    fn stats_fold_matches_the_materializing_definition() {
        // the column fold must agree with the old all_edge_versions walk —
        // same counts, extents, rel histogram, and degrees — including rows
        // staged in an open batch and interval-lane rows with real vt_e
        let (_root, mut s, _) = seeded("stats-fold");
        s.begin(200).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let mut long = edge(a, a, "n1", "n1", 40, 200, 5);
        long.vt_e = crate::OPEN_END; // routes to the interval lane
        s.stage_edge(long).unwrap();
        s.stage_edge(edge(a, a, "n1", "n1", 50, 200, 6)).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        s.begin(300).unwrap();
        let b = s.ensure_entity("n9", "Node").unwrap();
        s.stage_edge(edge(b, a, "n9", "n1", 60, 300, 7)).unwrap();

        let acc = s.stats_accum().unwrap();
        let mut want = crate::store::StatsAccum::default();
        for e in s.all_edge_versions().unwrap() {
            let src_id = s.dict().dense_id(&e.src).unwrap_or(0);
            want.add_edge(e.vt_s, e.vt_e, &e.rel_type, src_id);
        }
        want.n_node_versions = s.all_node_versions().unwrap().len() as u64;

        assert_eq!(acc.n_edge_versions, want.n_edge_versions);
        assert_eq!(acc.n_node_versions, want.n_node_versions);
        assert_eq!(acc.vt_min, want.vt_min);
        assert_eq!(acc.vt_max, want.vt_max);
        assert_eq!(acc.rel_type_counts, want.rel_type_counts);
        assert_eq!(acc.out_degree, want.out_degree);
        s.rollback().unwrap();
    }

    #[test]
    fn scan_addresses_round_trip_through_edge_idents_at() {
        use crate::row::Lane;
        use crate::scan::{ScanRequest, ScanSet, ScanTarget};

        // two commits so the manifest accumulates more than one segment and
        // the address column has to distinguish them
        let (_root, mut s, _) = seeded("idents-at");
        s.begin(200).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let c = s.ensure_entity("n3", "Node").unwrap();
        s.stage_edge(edge(a, c, "n1", "n3", 15, 200, 7)).unwrap();
        s.stage_edge(edge(c, a, "n3", "n1", 25, 200, 8)).unwrap();
        s.commit(EventLogRef::default()).unwrap();

        // assemble the scan exactly as the binding does
        let m = s.manifest().clone();
        let files: Vec<(Lane, String)> = m
            .edge_lanes
            .event
            .iter()
            .map(|e| (Lane::Event, e.file.clone()))
            .chain(m.edge_lanes.interval.iter().map(|e| (Lane::Interval, e.file.clone())))
            .collect();
        let segs: Vec<_> = files
            .iter()
            .map(|(lane, f)| (s.open_segment(f).unwrap(), *lane, crate::store::segment_id_of(f)))
            .collect();
        let targets: Vec<ScanTarget<'_, _>> = segs
            .iter()
            .map(|(segment, lane, id)| ScanTarget { segment, lane: *lane, id: *id })
            .collect();
        let set = ScanSet::new(targets).with_closes(s.close_index().unwrap());

        let req = ScanRequest {
            columns: Some(
                ["vt_s", "src_id", "dst_id", "rel_type", "disc", "seg_id", "seg_row"]
                    .iter()
                    .map(|c| c.to_string())
                    .collect(),
            ),
            ..ScanRequest::current()
        };
        let (cols, _) = set.materialize_edges(&req).unwrap();
        assert_eq!(cols.seg_id.len(), cols.len(), "addresses cover every row");
        assert_eq!(cols.seg_row.len(), cols.len());
        assert!(cols.len() >= 4);
        assert!(
            cols.seg_id.iter().collect::<std::collections::HashSet<_>>().len() > 1,
            "rows must come from more than one segment for this to test anything"
        );

        // the point read must agree with the full materialization, row by row
        let got = s.edge_idents_at(&cols.seg_id, &cols.seg_row).unwrap();
        for (i, (eid, rel)) in got.iter().enumerate() {
            let src = s.dict().uid(cols.src_id[i]).unwrap();
            let dst = s.dict().uid(cols.dst_id[i]).unwrap();
            let expect = edge_eid(src, dst, &cols.rel_type[i], &cols.disc[i]).to_hex();
            assert_eq!(*eid, expect, "eid at scan position {i}");
            assert_eq!(*rel, cols.rel_type[i], "rel_type at scan position {i}");
        }

        // an unprojected scan carries no addresses…
        let bare = ScanRequest {
            columns: Some(vec!["vt_s".into()]),
            ..ScanRequest::current()
        };
        let (bare_cols, _) = set.materialize_edges(&bare).unwrap();
        assert!(bare_cols.seg_id.is_empty() && bare_cols.seg_row.is_empty());

        // …and addresses from another generation's segments are refused
        assert!(s.edge_idents_at(&[u64::MAX - 1], &[0]).is_err());
        assert!(s.edge_idents_at(&[cols.seg_id[0]], &[u32::MAX]).is_err());
    }
}
