//! Write staging: buffer a batch, then seal it into immutable segments.
//!
//! `base.py::apply_ops` calls `insert_*` and `believed_*` repeatedly and
//! interleaved within one batch, so staging must serve **read-your-own-writes**
//! (spec §2.4) — a carve inserts fragments and then closes the originals, and
//! the second half must see the first. Rows therefore live in memory until
//! commit, when they are sorted, routed to lanes, split, and written.
//!
//! Nothing here is durable. A rolled-back batch simply drops the buffers; the
//! event log keeps the record and replay re-fails it identically (D-004).

use std::collections::HashMap;
use std::path::Path;

use crate::defaults::LANE_MAX_PARTITION_CROSSINGS;
use crate::error::{EngineError, Result};
use crate::manifest::SegmentEntry;
use crate::derive::Id96;
use crate::row::{EdgeRow, Lane, NodeRow, SortKey};
use crate::segment::{write_edge_segment, write_node_segment, SegmentSpec};

/// Maps valid time onto logical partitions. Partitions are a *logical*
/// concept: they decide lane membership and pruning granularity, while
/// physical segments are sized by bytes (D-028 #5).
#[derive(Clone, Copy, Debug)]
pub struct PartitionMap {
    pub origin: i64,
    pub width: i64,
}

impl PartitionMap {
    /// Default partition width: 7 days in microseconds.
    pub const DEFAULT_WIDTH: i64 = 7 * 24 * 60 * 60 * 1_000_000;

    pub fn new(origin: i64, width: i64) -> Result<Self> {
        if width <= 0 {
            return Err(EngineError::invariant(format!(
                "partition width must be positive, got {width}"
            )));
        }
        Ok(Self { origin, width })
    }

    pub fn partition_of(&self, t: i64) -> i64 {
        t.saturating_sub(self.origin).div_euclid(self.width)
    }

    /// How many adjacent partitions `[vt_s, vt_e)` touches (at least 1).
    pub fn crossings(&self, vt_s: i64, vt_e: i64) -> i64 {
        let last = self.partition_of(vt_e.saturating_sub(1).max(vt_s));
        last.saturating_sub(self.partition_of(vt_s)).saturating_add(1)
    }

    /// Lane assignment (D-028 #6). Defined by *actual partition crossings*,
    /// not by comparing a duration against the width — those differ the
    /// moment partitions stop being uniform, and the crossing count is what
    /// the pruning argument actually depends on.
    pub fn lane_for(&self, vt_s: i64, vt_e: i64) -> Lane {
        if self.crossings(vt_s, vt_e) <= LANE_MAX_PARTITION_CROSSINGS as i64 {
            Lane::Event
        } else {
            Lane::Interval
        }
    }
}

impl Default for PartitionMap {
    fn default() -> Self {
        Self {
            origin: 0,
            width: Self::DEFAULT_WIDTH,
        }
    }
}

/// Fixed per-row cost of the edge columns, used to size segments before the
/// bytes exist. The header records what was actually written.
const EDGE_FIXED_BYTES: usize = 8 + 4 + 4 + 2 + 8 + 4 + 4 + 4 + 4 + 4;
const NODE_FIXED_BYTES: usize = 8 + 4 + 8 + 4 + 4 + 4 + 4 + 4;

#[derive(Default)]
pub struct Staging {
    edges: Vec<EdgeRow>,
    nodes: Vec<NodeRow>,
}

impl Staging {
    pub fn is_empty(&self) -> bool {
        self.edges.is_empty() && self.nodes.is_empty()
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn push_edge(&mut self, row: EdgeRow) {
        self.edges.push(row);
    }

    pub fn push_node(&mut self, row: NodeRow) {
        self.nodes.push(row);
    }

    /// Staged rows, for read-your-own-writes inside the open batch.
    pub fn edges(&self) -> &[EdgeRow] {
        &self.edges
    }

    pub fn nodes(&self) -> &[NodeRow] {
        &self.nodes
    }

    pub fn clear(&mut self) {
        self.edges.clear();
        self.nodes.clear();
    }

    /// Sort, route to lanes, split, and write. Returns the manifest entries
    /// for the segments produced, and advances `next_segment_id`.
    ///
    /// Segment ids are never reused: a published manifest may still reference
    /// an old file, and nothing is deleted in this version of the engine.
    /// `closes` carries versions created and corrected inside this same batch:
    /// they are written normally and their `tt_e` is folded into the owning
    /// segment's sidecar, so a sealed segment is never revisited.
    pub fn seal(
        &mut self,
        seg_dir: &Path,
        partitions: &PartitionMap,
        target_bytes: u64,
        next_segment_id: &mut u64,
        closes: &HashMap<Id96, i64>,
    ) -> Result<SealedBatch> {
        let mut out = SealedBatch::default();

        self.edges.sort_by_key(|r| r.sort_key());
        for (lane, rows) in split_lanes(&self.edges, partitions, |r| (r.vt_s, r.vt_e)) {
            for chunk in chunk_by_size(&rows, target_bytes, edge_row_bytes, |r| r.sort_key()) {
                let id = *next_segment_id;
                *next_segment_id += 1;
                let path = seg_dir.join(format!("{id:012}.tgs"));
                let owned: Vec<EdgeRow> = chunk.to_vec();
                let closed_rows = sidecar_for(&owned, closes, |r| r.vid);
                write_edge_segment(
                    &path,
                    &owned,
                    &SegmentSpec {
                        lane,
                        closed_rows: closed_rows.clone(),
                        ..Default::default()
                    },
                )?;
                let mut entry = entry_for(&path, &owned, |r| r.sort_key(), |r| r.vt_s,
                                          |r| r.vt_e, |r| r.tt_s)?;
                entry.n_closed_folded = closed_rows.len() as u32;
                entry.all_current = closed_rows.is_empty();
                out.edges.push((lane, entry));
                out.segment_ids.push(id);
            }
        }

        self.nodes.sort_by_key(|r| r.sort_key());
        for (lane, rows) in split_lanes(&self.nodes, partitions, |r| (r.vt_s, r.vt_e)) {
            for chunk in chunk_by_size(&rows, target_bytes, node_row_bytes, |r| r.sort_key()) {
                let id = *next_segment_id;
                *next_segment_id += 1;
                let path = seg_dir.join(format!("{id:012}.tgs"));
                let owned: Vec<NodeRow> = chunk.to_vec();
                let closed_rows = sidecar_for(&owned, closes, |r| r.vid);
                write_node_segment(
                    &path,
                    &owned,
                    &SegmentSpec {
                        lane,
                        closed_rows: closed_rows.clone(),
                        ..Default::default()
                    },
                )?;
                let mut entry = entry_for(&path, &owned, |r| r.sort_key(), |r| r.vt_s,
                                          |r| r.vt_e, |r| r.tt_s)?;
                entry.n_closed_folded = closed_rows.len() as u32;
                entry.all_current = closed_rows.is_empty();
                out.nodes.push(entry);
                out.segment_ids.push(id);
            }
        }
        Ok(out)
    }
}

#[derive(Default, Debug)]
pub struct SealedBatch {
    pub edges: Vec<(Lane, SegmentEntry)>,
    pub nodes: Vec<SegmentEntry>,
    /// File ids assigned, in the order the segments were written.
    pub segment_ids: Vec<u64>,
}

/// Rows of one chunk that this batch also closed, as a sorted sidecar.
fn sidecar_for<T>(
    rows: &[T],
    closes: &HashMap<Id96, i64>,
    vid: impl Fn(&T) -> Id96,
) -> Vec<(u32, i64)> {
    if closes.is_empty() {
        return Vec::new();
    }
    let mut out: Vec<(u32, i64)> = rows
        .iter()
        .enumerate()
        .filter_map(|(i, r)| closes.get(&vid(r)).map(|tt| (i as u32, *tt)))
        .collect();
    out.sort_unstable();
    out
}

fn edge_row_bytes(r: &EdgeRow) -> usize {
    // fixed columns + the one string that is usually unique per row
    EDGE_FIXED_BYTES + r.disc.len() + 4
}

fn node_row_bytes(_r: &NodeRow) -> usize {
    NODE_FIXED_BYTES + 4
}

/// Partition rows by lane, preserving sort order within each lane.
fn split_lanes<T: Clone>(
    rows: &[T],
    partitions: &PartitionMap,
    interval: impl Fn(&T) -> (i64, i64),
) -> Vec<(Lane, Vec<T>)> {
    let mut event = Vec::new();
    let mut long = Vec::new();
    for r in rows {
        let (vt_s, vt_e) = interval(r);
        match partitions.lane_for(vt_s, vt_e) {
            Lane::Event => event.push(r.clone()),
            Lane::Interval => long.push(r.clone()),
        }
    }
    let mut out = Vec::new();
    if !event.is_empty() {
        out.push((Lane::Event, event));
    }
    if !long.is_empty() {
        out.push((Lane::Interval, long));
    }
    out
}

/// Split into byte-bounded chunks, never cutting a composite-key tie group
/// across a boundary (D-028 #4) — segment key ranges must stay disjoint for
/// manifest pruning to be sound.
///
/// **Requires sorted input** (as `seal` guarantees): tie detection only
/// compares adjacent rows, so equal keys must already be adjacent.
fn chunk_by_size<T>(
    rows: &[T],
    target_bytes: u64,
    size_of: impl Fn(&T) -> usize,
    key: impl Fn(&T) -> SortKey,
) -> Vec<&[T]> {
    let mut chunks = Vec::new();
    let (mut start, mut acc) = (0usize, 0u64);
    for i in 0..rows.len() {
        acc += size_of(&rows[i]) as u64;
        let boundary_ok = i + 1 == rows.len() || key(&rows[i]) != key(&rows[i + 1]);
        if acc >= target_bytes && boundary_ok && i + 1 < rows.len() {
            chunks.push(&rows[start..=i]);
            start = i + 1;
            acc = 0;
        }
    }
    if start < rows.len() {
        chunks.push(&rows[start..]);
    }
    chunks
}

#[allow(clippy::type_complexity)]
fn entry_for<T>(
    path: &Path,
    rows: &[T],
    key: impl Fn(&T) -> SortKey,
    vt_s: impl Fn(&T) -> i64,
    vt_e: impl Fn(&T) -> i64,
    tt_s: impl Fn(&T) -> i64,
) -> Result<SegmentEntry> {
    let first = rows.first().ok_or_else(|| {
        EngineError::invariant("a sealed segment must contain at least one row")
    })?;
    let last = &rows[rows.len() - 1];
    let file = path
        .file_name()
        .map(|f| format!("seg/{}", f.to_string_lossy()))
        .unwrap_or_default();
    let (lo_t, lo_id) = key(first);
    let (hi_t, hi_id) = key(last);
    Ok(SegmentEntry {
        file,
        rows: rows.len() as u32,
        key_lo: (lo_t, lo_id.to_hex()),
        key_hi: (hi_t, hi_id.to_hex()),
        vt_min: rows.iter().map(&vt_s).min().unwrap_or(0),
        vt_max: rows.iter().map(&vt_s).max().unwrap_or(0),
        vt_e_max: rows.iter().map(&vt_e).max().unwrap_or(0),
        tt_s_min: rows.iter().map(&tt_s).min().unwrap_or(0),
        tt_s_max: rows.iter().map(&tt_s).max().unwrap_or(0),
        rel_codes: Vec::new(),
        n_closed_folded: 0,
        // freshly written rows have never been closed
        all_current: true,
        sha: String::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};
    use crate::segment::{MemorySource, Segment};
    use crate::OPEN_END;
    use std::path::PathBuf;

    const DAY: i64 = 24 * 60 * 60 * 1_000_000;

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-staging-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn edge(vt_s: i64, vt_e: i64, i: u32) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid("n1", "n2", "R", &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), 100, vt_s),
            src_id: i % 5,
            dst_id: (i + 1) % 5,
            rel_type: "R".into(),
            disc,
            vt_s,
            vt_e,
            tt_s: 100,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    #[test]
    fn lane_rule_follows_partition_crossings_not_duration() {
        let p = PartitionMap::new(0, PartitionMap::DEFAULT_WIDTH).unwrap();
        // instantaneous: one partition
        assert_eq!(p.lane_for(DAY, DAY + 1), Lane::Event);
        // spans a boundary but only two partitions: still the event lane
        assert_eq!(p.lane_for(6 * DAY, 8 * DAY), Lane::Event);
        // three partitions: too wide to keep pruning tight
        assert_eq!(p.lane_for(6 * DAY, 20 * DAY), Lane::Interval);
        // open-ended facts are always long-lived
        assert_eq!(p.lane_for(0, OPEN_END), Lane::Interval);
    }

    #[test]
    fn crossings_never_underflow_at_the_extremes() {
        let p = PartitionMap::new(0, PartitionMap::DEFAULT_WIDTH).unwrap();
        assert_eq!(p.crossings(0, 1), 1);
        assert!(p.crossings(i64::MIN, i64::MIN + 1) >= 1);
        assert!(p.crossings(0, OPEN_END) > 1);
    }

    #[test]
    fn zero_width_partitions_are_rejected() {
        assert!(PartitionMap::new(0, 0).is_err());
        assert!(PartitionMap::new(0, -1).is_err());
    }

    #[test]
    fn seal_routes_rows_to_both_lanes() {
        let dir = tmp("lanes");
        let mut s = Staging::default();
        s.push_edge(edge(DAY, DAY + 1, 0)); // event
        s.push_edge(edge(2 * DAY, 2 * DAY + 1, 1)); // event
        s.push_edge(edge(3 * DAY, 60 * DAY, 2)); // interval
        let mut next = 0u64;
        let sealed = s
            .seal(&dir, &PartitionMap::default(), 1 << 20, &mut next, &HashMap::new())
            .unwrap();

        assert_eq!(sealed.edges.len(), 2, "one segment per occupied lane");
        let lanes: Vec<Lane> = sealed.edges.iter().map(|(l, _)| *l).collect();
        assert!(lanes.contains(&Lane::Event) && lanes.contains(&Lane::Interval));
        let event = sealed.edges.iter().find(|(l, _)| *l == Lane::Event).unwrap();
        assert_eq!(event.1.rows, 2);
        assert_eq!(next, 2, "segment ids advanced");
    }

    #[test]
    fn seal_splits_by_size_and_ids_never_repeat() {
        let dir = tmp("split");
        let mut s = Staging::default();
        for i in 0..1000 {
            s.push_edge(edge(1000 + i as i64, 1001 + i as i64, i));
        }
        let mut next = 7u64; // start from a non-zero id to prove it is honoured
        let sealed = s
            .seal(&dir, &PartitionMap::default(), 4096, &mut next, &HashMap::new())
            .unwrap();

        assert!(sealed.edges.len() > 1, "1000 rows should exceed a 4 KiB target");
        let total: u32 = sealed.edges.iter().map(|(_, e)| e.rows).sum();
        assert_eq!(total, 1000, "every row lands in exactly one segment");

        let mut ids: Vec<&str> = sealed.edges.iter().map(|(_, e)| e.file.as_str()).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), sealed.edges.len(), "segment files are distinct");
        assert_eq!(next, 7 + sealed.edges.len() as u64);
    }

    #[test]
    fn segment_key_ranges_stay_disjoint_and_ordered() {
        let dir = tmp("ranges");
        let mut s = Staging::default();
        for i in 0..600 {
            s.push_edge(edge(1000 + i as i64, 1001 + i as i64, i));
        }
        let mut next = 0u64;
        let sealed = s
            .seal(&dir, &PartitionMap::default(), 4096, &mut next, &HashMap::new())
            .unwrap();
        let entries: Vec<_> = sealed.edges.iter().map(|(_, e)| e).collect();
        for w in entries.windows(2) {
            assert!(
                w[0].key_hi < w[1].key_lo,
                "segment ranges must be disjoint and ascending: {:?} vs {:?}",
                w[0].key_hi,
                w[1].key_lo
            );
        }
    }

    #[test]
    fn tie_groups_are_never_split_across_segments() {
        // rows sharing an exact (vt_s, vid) key must stay together, or the
        // manifest's key ranges would overlap and pruning would be unsound.
        // Two keys, ten rows each (chunking assumes sorted input, as `seal`
        // guarantees, so equal keys are adjacent).
        let mut rows: Vec<EdgeRow> = (0..20).map(|i| edge(500, 501, i % 2)).collect();
        rows.sort_by_key(|r| r.sort_key());

        // a 1-byte target would cut after *every* row if ties could be split
        let chunks = chunk_by_size(&rows, 1, edge_row_bytes, |r| r.sort_key());
        assert_eq!(chunks.len(), 2, "expected exactly one chunk per tie group");
        for c in &chunks {
            let key = c[0].sort_key();
            assert_eq!(c.len(), 10, "a tie group was split across segments");
            assert!(c.iter().all(|r| r.sort_key() == key));
        }
    }

    #[test]
    fn sealed_segments_read_back_identical_to_staged_rows() {
        let dir = tmp("roundtrip");
        let mut s = Staging::default();
        let mut expected: Vec<EdgeRow> = (0..300)
            .map(|i| edge(2000 - i as i64, 2001 - i as i64, i))
            .collect();
        for r in &expected {
            s.push_edge(r.clone());
        }
        let mut next = 0u64;
        let sealed = s
            .seal(&dir, &PartitionMap::default(), 4096, &mut next, &HashMap::new())
            .unwrap();
        expected.sort_by_key(|r| r.sort_key());

        let mut seen = Vec::new();
        let mut files: Vec<String> =
            sealed.edges.iter().map(|(_, e)| e.file.clone()).collect();
        files.sort();
        for f in files {
            let path = dir.join(f.trim_start_matches("seg/"));
            let seg = Segment::open(&path, MemorySource::load(&path).unwrap(), true).unwrap();
            let vt_s = seg.i64_column("vt_s").unwrap();
            for (i, &t) in vt_s.iter().enumerate() {
                seen.push((t, seg.vid_at(i).unwrap()));
            }
        }
        let want: Vec<_> = expected.iter().map(|r| (r.vt_s, r.vid)).collect();
        assert_eq!(seen, want, "sealed content must equal staged content, in order");
    }

    #[test]
    fn clear_drops_everything_for_rollback() {
        let mut s = Staging::default();
        s.push_edge(edge(1, 2, 0));
        assert!(!s.is_empty());
        s.clear();
        assert!(s.is_empty());
        assert_eq!(s.edge_count(), 0);
    }
}
