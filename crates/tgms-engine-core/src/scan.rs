//! The scan cursor — the engine's internal read path (spec §5.3, §6).
//!
//! This is deliberately *not* the `StorageAdapter` interface. Kernels consume
//! selections and column views directly; materializing a sorted struct-of-
//! arrays happens once, at the public boundary, and only for callers that
//! actually need it. Putting the ABC in the middle would pay an allocation
//! and a copy between every stage.
//!
//! Filtering matches `duckdb_adapter.edges_columnar` exactly, because the two
//! backends must return identical rows:
//!
//! ```sql
//! WHERE tt_s <= as_of AND as_of < tt_e          -- belief
//!   AND vt_e > vt_min AND vt_s < vt_max         -- valid-time overlap
//!   AND rel_type IN (...)                       -- optional
//!   AND (src_id IN touching OR dst_id IN touching)
//! ORDER BY vt_s, vid
//! ```
//!
//! Work is skipped in three escalating tiers: whole segments are dropped from
//! the manifest's zone maps without any I/O; inside a surviving segment the
//! sorted `vt_s` column turns both window bounds into binary searches; only
//! what is left is examined per row.

use std::cmp::Reverse;
use std::collections::BinaryHeap;

use crate::derive::Id96;
use crate::error::Result;
use crate::row::Lane;
use crate::segment::{Segment, SegmentSource, StringView};
use crate::visibility::CloseIndex;
use crate::{clamp_tt, OPEN_END};

#[derive(Clone, Debug, Default)]
pub struct ScanRequest {
    /// Belief time. `OPEN_END` (the default) means current beliefs.
    pub as_of_tt: i64,
    /// Half-open valid-time window; `None` is unbounded on that side.
    pub vt_min: Option<i64>,
    pub vt_max: Option<i64>,
    pub rel_types: Option<Vec<String>>,
    /// Incidence filter over dense ids. Must be sorted (binary-searched).
    pub touching_ids: Option<Vec<u32>>,
    /// Stop after this many rows. Applied after ordering.
    pub limit: Option<usize>,
}

impl ScanRequest {
    pub fn current() -> Self {
        Self {
            as_of_tt: OPEN_END,
            ..Default::default()
        }
    }

    pub fn window(mut self, t_a: i64, t_b: i64) -> Self {
        self.vt_min = Some(t_a);
        self.vt_max = Some(t_b);
        self
    }

    pub fn touching(mut self, mut ids: Vec<u32>) -> Self {
        ids.sort_unstable();
        ids.dedup();
        self.touching_ids = Some(ids);
        self
    }
}

/// Pruning effectiveness, surfaced into the existing trace-record counters.
/// Without these, "the zone maps are working" is an assumption rather than an
/// observation (blueprint C8).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ScanStats {
    pub segments_total: usize,
    pub segments_pruned: usize,
    pub rows_examined: u64,
    pub rows_selected: u64,
}

/// Row ids selected within one segment, in ascending (already sorted) order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    pub segment: usize,
    pub rows: Vec<u32>,
}

/// One segment in a scan, with the file id close runs address it by.
pub struct ScanTarget<'a, S: SegmentSource> {
    pub segment: &'a Segment<S>,
    pub lane: Lane,
    pub id: u64,
}

/// A set of segments queried together. Segments within a lane written by one
/// batch have disjoint key ranges, but successive generations overlap, so the
/// merge below is a general k-way merge rather than a concatenation.
pub struct ScanSet<'a, S: SegmentSource> {
    targets: Vec<ScanTarget<'a, S>>,
    closes: CloseIndex,
}

impl<'a, S: SegmentSource> ScanSet<'a, S> {
    pub fn new(targets: Vec<ScanTarget<'a, S>>) -> Self {
        Self {
            targets,
            closes: CloseIndex::default(),
        }
    }

    /// For callers with no close runs — segment ids are positional.
    pub fn from_pairs(pairs: Vec<(&'a Segment<S>, Lane)>) -> Self {
        Self::new(
            pairs
                .into_iter()
                .enumerate()
                .map(|(i, (segment, lane))| ScanTarget {
                    segment,
                    lane,
                    id: i as u64,
                })
                .collect(),
        )
    }

    /// Attach the closes a manifest generation makes visible. Without this a
    /// scan sees every stored row as still believed.
    pub fn with_closes(mut self, closes: CloseIndex) -> Self {
        self.closes = closes;
        self
    }

    pub fn len(&self) -> usize {
        self.targets.len()
    }

    pub fn is_empty(&self) -> bool {
        self.targets.is_empty()
    }

    /// Tier 1: can this segment contribute anything at all? Answered from the
    /// header alone — no column bytes are touched.
    fn prunes(&self, seg: &Segment<S>, req: &ScanRequest) -> bool {
        let h = seg.header();
        let as_of = clamp_tt(req.as_of_tt);
        // nothing in this segment was believed yet at as_of
        if h.tt_s_runs.iter().all(|(_, tt)| *tt > as_of) {
            return true;
        }
        if let Some(vt_min) = req.vt_min {
            if h.vt_e_max <= vt_min {
                return true;
            }
        }
        if let Some(vt_max) = req.vt_max {
            if h.vt_min >= vt_max {
                return true;
            }
        }
        if let Some(rels) = &req.rel_types {
            if !h.rel_types.is_empty() && !h.rel_types.iter().any(|r| rels.contains(r)) {
                return true;
            }
        }
        false
    }

    /// Tiers 2 and 3: narrow by binary search, then test what remains.
    ///
    /// Every column handle and lookup table is resolved **once per segment**.
    /// Doing any of it per row (re-resolving a column by name, walking the
    /// `tt_s` runs, scanning a `Vec` of allowed rel codes) costs more than the
    /// predicate itself and made this path slower than vectorized NumPy —
    /// measured, then fixed; see `docs/engine_probe.md`.
    pub fn select(&self, req: &ScanRequest) -> Result<(Vec<Selection>, ScanStats)> {
        let mut stats = ScanStats {
            segments_total: self.targets.len(),
            ..Default::default()
        };
        let mut out = Vec::new();
        let as_of = clamp_tt(req.as_of_tt);
        let touching = req.touching_ids.as_ref().map(|ids| IdSet::new(ids));

        for (idx, target) in self.targets.iter().enumerate() {
            let seg = target.segment;
            if self.prunes(seg, req) {
                stats.segments_pruned += 1;
                continue;
            }
            let h = seg.header();
            let vt_s = seg.i64_column("vt_s")?;

            // vt_s is sorted, so `vt_s < vt_max` is a suffix cut for free.
            let hi = match req.vt_max {
                Some(t) => vt_s.partition_point(|&v| v < t),
                None => vt_s.len(),
            };
            // When vt_e is elided every row is instantaneous, so
            // `vt_e > vt_min` collapses to `vt_s >= vt_min` — a prefix cut,
            // valid in either lane. Otherwise the real vt_e must be tested.
            let lo = match req.vt_min {
                Some(t) if h.vt_e_elided => vt_s.partition_point(|&v| v < t),
                _ => 0,
            };
            if lo >= hi {
                continue;
            }

            let vt_e_col = if h.vt_e_elided {
                None
            } else {
                Some(seg.i64_column("vt_e")?)
            };
            let rel_codes = seg.u16_column("rel_code").ok();
            let src = seg.u32_column("src_id").ok();
            let dst = seg.u32_column("dst_id").ok();
            // direct lookup by rel_code instead of searching a list per row
            let rel_allowed: Option<Vec<bool>> = req
                .rel_types
                .as_ref()
                .map(|names| h.rel_types.iter().map(|r| names.contains(r)).collect());

            // visibility: a segment nothing has corrected skips this work
            // entirely, which for event-stream data is nearly all of them
            let sidecar = seg.sidecar();
            let has_closes = !sidecar.all_current() || self.closes.touches(target.id);

            let needs_vt_e_test = req.vt_min.is_some() && vt_e_col.is_some();
            let needs_row_test =
                needs_vt_e_test || rel_allowed.is_some() || touching.is_some() || has_closes;

            let mut rows: Vec<u32> = Vec::new();
            for (rs, re) in believed_ranges(h, as_of, vt_s.len()) {
                let (a, b) = (rs.max(lo), re.min(hi));
                if a >= b {
                    continue;
                }
                stats.rows_examined += (b - a) as u64;
                if !needs_row_test {
                    // the binary-search bounds already decided every row here
                    rows.reserve(b - a);
                    rows.extend(a as u32..b as u32);
                    continue;
                }
                for i in a..b {
                    if has_closes {
                        // `tt_s <= as_of` was decided per run above; this is
                        // the other half of the belief predicate, as_of < tt_e.
                        // A close run outranks a folded sidecar entry: it is
                        // the more recent transaction.
                        let from_run = self.closes.tt_e(target.id, i as u32);
                        let tt_e = if from_run != OPEN_END {
                            from_run
                        } else {
                            sidecar.tt_e(i as u32)
                        };
                        if as_of >= tt_e {
                            continue;
                        }
                    }
                    if let (Some(vt_e), Some(vt_min)) = (vt_e_col, req.vt_min) {
                        if vt_e[i] <= vt_min {
                            continue;
                        }
                    }
                    if let (Some(codes), Some(allowed)) = (rel_codes, rel_allowed.as_ref()) {
                        if !allowed.get(codes[i] as usize).copied().unwrap_or(false) {
                            continue;
                        }
                    }
                    if let Some(ids) = &touching {
                        let hit = src.is_some_and(|s| ids.contains(s[i]))
                            || dst.is_some_and(|d| ids.contains(d[i]));
                        if !hit {
                            continue;
                        }
                    }
                    rows.push(i as u32);
                }
            }
            if !rows.is_empty() {
                stats.rows_selected += rows.len() as u64;
                out.push(Selection { segment: idx, rows });
            }
        }
        Ok((out, stats))
    }

    /// Merge selections into one globally `(vt_s, vid)`-ordered stream.
    ///
    /// Only callers that need global order pay for this; a kernel that works
    /// segment-at-a-time consumes `select` directly.
    pub fn merged(
        &self,
        selections: &[Selection],
        limit: Option<usize>,
    ) -> Result<Vec<(usize, u32)>> {
        let mut heap: BinaryHeap<Reverse<(i64, Id96, usize, usize)>> = BinaryHeap::new();
        for (si, sel) in selections.iter().enumerate() {
            if let Some(&row) = sel.rows.first() {
                let seg = self.targets[sel.segment].segment;
                heap.push(Reverse((
                    seg.i64_column("vt_s")?[row as usize],
                    seg.vid_at(row as usize)?,
                    si,
                    0,
                )));
            }
        }
        let cap = limit.unwrap_or(usize::MAX);
        let mut out = Vec::new();
        while let Some(Reverse((_, _, si, pos))) = heap.pop() {
            let sel = &selections[si];
            out.push((sel.segment, sel.rows[pos]));
            if out.len() >= cap {
                break;
            }
            if let Some(&next) = sel.rows.get(pos + 1) {
                let seg = self.targets[sel.segment].segment;
                heap.push(Reverse((
                    seg.i64_column("vt_s")?[next as usize],
                    seg.vid_at(next as usize)?,
                    si,
                    pos + 1,
                )));
            }
        }
        Ok(out)
    }

    /// Materialize a sorted struct-of-arrays — the shape
    /// `edges_columnar` hands to Python. Done once, at the boundary.
    pub fn materialize_edges(&self, req: &ScanRequest) -> Result<(EdgeColumns, ScanStats)> {
        let (selections, stats) = self.select(req)?;
        let order = self.merged(&selections, req.limit)?;
        let mut cols = EdgeColumns::with_capacity(order.len());
        // resolve each segment's columns once, then walk its rows
        let mut views: Vec<Option<SegmentView<'_>>> =
            (0..self.targets.len()).map(|_| None).collect();
        for (seg_idx, row) in order {
            if views[seg_idx].is_none() {
                views[seg_idx] = Some(SegmentView::open(self.targets[seg_idx].segment)?);
            }
            let v = views[seg_idx].as_ref().expect("just populated");
            let r = row as usize;
            cols.vt_s.push(v.vt_s[r]);
            cols.vt_e.push(match v.vt_e {
                Some(col) => col[r],
                None => v.vt_s[r] + 1,
            });
            cols.src_id.push(v.src_id[r]);
            cols.dst_id.push(v.dst_id[r]);
            cols.vid.push(Id96 {
                hi: v.vid64[r],
                lo: v.vid_lo32[r],
            });
            cols.rel_type
                .push(v.rel_types[v.rel_code[r] as usize].clone());
            cols.disc.push(v.strings.get(v.disc_ref[r])?.to_string());
            cols.props.push(v.strings.get(v.props_ref[r])?.to_string());
        }
        Ok((cols, stats))
    }
}

/// Membership test for the incidence filter.
///
/// Dense ids are small integers, so a bitset answers in one shifted load —
/// versus ~12 comparisons for a binary search over the id list, which the
/// probe showed dominating the whole scan (`docs/engine_probe.md`).
struct IdSet {
    bits: Vec<u64>,
    max: u32,
}

impl IdSet {
    fn new(ids: &[u32]) -> Self {
        let max = ids.iter().copied().max().unwrap_or(0);
        let mut bits = vec![0u64; (max as usize / 64) + 1];
        for &id in ids {
            bits[id as usize / 64] |= 1u64 << (id % 64);
        }
        Self { bits, max }
    }

    #[inline]
    fn contains(&self, id: u32) -> bool {
        id <= self.max && (self.bits[id as usize / 64] >> (id % 64)) & 1 == 1
    }
}

/// Row ranges whose transaction time is believed at `as_of`.
///
/// `tt_s` is run-length encoded, so a batch collapses to one range and the
/// belief predicate costs a couple of comparisons per segment rather than one
/// per row.
fn believed_ranges(
    h: &crate::segment::SegmentHeader,
    as_of: i64,
    rows: usize,
) -> Vec<(usize, usize)> {
    let runs = &h.tt_s_runs;
    let mut out = Vec::with_capacity(runs.len());
    for (i, (start, tt)) in runs.iter().enumerate() {
        let end = runs
            .get(i + 1)
            .map(|(s, _)| *s as usize)
            .unwrap_or(rows)
            .min(rows);
        if *tt <= as_of && (*start as usize) < end {
            out.push((*start as usize, end));
        }
    }
    out
}

/// All of one segment's column handles, resolved once.
struct SegmentView<'a> {
    vt_s: &'a [i64],
    vt_e: Option<&'a [i64]>,
    src_id: &'a [u32],
    dst_id: &'a [u32],
    vid64: &'a [u64],
    vid_lo32: &'a [u32],
    rel_code: &'a [u16],
    disc_ref: &'a [u32],
    props_ref: &'a [u32],
    rel_types: &'a [String],
    strings: StringView<'a>,
}

impl<'a> SegmentView<'a> {
    fn open<S: SegmentSource>(seg: &'a Segment<S>) -> Result<Self> {
        Ok(Self {
            vt_s: seg.i64_column("vt_s")?,
            vt_e: if seg.header().vt_e_elided {
                None
            } else {
                Some(seg.i64_column("vt_e")?)
            },
            src_id: seg.u32_column("src_id")?,
            dst_id: seg.u32_column("dst_id")?,
            vid64: seg.u64_column("vid64")?,
            vid_lo32: seg.u32_column("vid_lo32")?,
            rel_code: seg.u16_column("rel_code")?,
            disc_ref: seg.u32_column("disc_ref")?,
            props_ref: seg.u32_column("props_ref")?,
            rel_types: &seg.header().rel_types,
            strings: seg.strings()?,
        })
    }
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct EdgeColumns {
    pub vt_s: Vec<i64>,
    pub vt_e: Vec<i64>,
    pub src_id: Vec<u32>,
    pub dst_id: Vec<u32>,
    pub vid: Vec<Id96>,
    pub rel_type: Vec<String>,
    pub disc: Vec<String>,
    pub props: Vec<String>,
}

impl EdgeColumns {
    fn with_capacity(n: usize) -> Self {
        Self {
            vt_s: Vec::with_capacity(n),
            vt_e: Vec::with_capacity(n),
            src_id: Vec::with_capacity(n),
            dst_id: Vec::with_capacity(n),
            vid: Vec::with_capacity(n),
            rel_type: Vec::with_capacity(n),
            disc: Vec::with_capacity(n),
            props: Vec::with_capacity(n),
        }
    }

    pub fn len(&self) -> usize {
        self.vt_s.len()
    }

    pub fn is_empty(&self) -> bool {
        self.vt_s.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};
    use crate::row::EdgeRow;
    use crate::segment::{write_edge_segment, MemorySource, Segment, SegmentSpec};
    use std::path::PathBuf;

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-scan-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn edge(vt_s: i64, i: u32, tt_s: i64, rel: &str, src: u32, dst: u32) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid("a", "b", rel, &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id: src,
            dst_id: dst,
            rel_type: rel.into(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    /// Reference implementation of the filter, written the obvious slow way.
    fn expected(rows: &[EdgeRow], req: &ScanRequest) -> Vec<(i64, Id96)> {
        let as_of = clamp_tt(req.as_of_tt);
        let mut v: Vec<(i64, Id96)> = rows
            .iter()
            .filter(|r| r.tt_s <= as_of)
            .filter(|r| req.vt_min.is_none_or(|t| r.vt_e > t))
            .filter(|r| req.vt_max.is_none_or(|t| r.vt_s < t))
            .filter(|r| {
                req.rel_types
                    .as_ref()
                    .is_none_or(|rs| rs.contains(&r.rel_type))
            })
            .filter(|r| {
                req.touching_ids
                    .as_ref()
                    .is_none_or(|ids| ids.contains(&r.src_id) || ids.contains(&r.dst_id))
            })
            .map(|r| (r.vt_s, r.vid))
            .collect();
        v.sort();
        if let Some(n) = req.limit {
            v.truncate(n);
        }
        v
    }

    struct Fixture {
        rows: Vec<EdgeRow>,
        segs: Vec<Segment<MemorySource>>,
    }

    impl Fixture {
        /// Two segments with *overlapping* vt ranges, as successive
        /// generations produce — so the merge is genuinely k-way.
        fn build(name: &str) -> Self {
            let dir = tmp(name);
            let mut rows = Vec::new();
            let mut segs = Vec::new();
            for (batch, tt) in [(0u32, 10i64), (1, 20)] {
                let mut b: Vec<EdgeRow> = (0..200u32)
                    .map(|i| {
                        let rel = if i % 3 == 0 { "RATED" } else { "SENT" };
                        edge(
                            1_000 + (i as i64) * 2 + batch as i64,
                            i + batch * 1000,
                            tt,
                            rel,
                            i % 11,
                            (i + 5) % 11,
                        )
                    })
                    .collect();
                b.sort_by_key(|r| r.sort_key());
                let path = dir.join(format!("{batch}.tgs"));
                write_edge_segment(&path, &b, &SegmentSpec::default()).unwrap();
                segs.push(
                    Segment::open(&path, MemorySource::load(&path).unwrap(), true).unwrap(),
                );
                rows.extend(b);
            }
            Self { rows, segs }
        }

        fn set(&self) -> ScanSet<'_, MemorySource> {
            ScanSet::from_pairs(self.segs.iter().map(|s| (s, Lane::Event)).collect())
        }
    }

    fn run(f: &Fixture, req: &ScanRequest) -> Vec<(i64, Id96)> {
        let set = f.set();
        let (sel, _) = set.select(req).unwrap();
        set.merged(&sel, req.limit)
            .unwrap()
            .into_iter()
            .map(|(si, row)| {
                let seg = &f.segs[si];
                (
                    seg.i64_column("vt_s").unwrap()[row as usize],
                    seg.vid_at(row as usize).unwrap(),
                )
            })
            .collect()
    }

    #[test]
    fn unfiltered_scan_matches_the_reference() {
        let f = Fixture::build("all");
        let req = ScanRequest::current();
        assert_eq!(run(&f, &req), expected(&f.rows, &req));
    }

    #[test]
    fn results_are_globally_ordered_across_overlapping_segments() {
        let f = Fixture::build("order");
        let got = run(&f, &ScanRequest::current());
        assert_eq!(got.len(), 400);
        assert!(
            got.windows(2).all(|w| w[0] < w[1]),
            "merged output must be strictly ascending by (vt_s, vid)"
        );
    }

    #[test]
    fn window_filter_matches_the_reference() {
        let f = Fixture::build("window");
        for (a, b) in [(1_000, 1_100), (1_150, 1_250), (0, 10), (1_390, 9_999)] {
            let req = ScanRequest::current().window(a, b);
            assert_eq!(run(&f, &req), expected(&f.rows, &req), "window [{a},{b})");
        }
    }

    #[test]
    fn as_of_tt_hides_later_batches() {
        let f = Fixture::build("asof");
        let mut req = ScanRequest::current();
        req.as_of_tt = 15; // after batch 0 (tt 10), before batch 1 (tt 20)
        let got = run(&f, &req);
        assert_eq!(got, expected(&f.rows, &req));
        assert_eq!(got.len(), 200, "only the first batch was believed at tt=15");

        req.as_of_tt = 5;
        assert!(run(&f, &req).is_empty(), "nothing is believed before tt=10");
    }

    #[test]
    fn rel_type_and_incidence_filters_match_the_reference() {
        let f = Fixture::build("filters");
        let mut req = ScanRequest::current();
        req.rel_types = Some(vec!["RATED".into()]);
        assert_eq!(run(&f, &req), expected(&f.rows, &req));

        let req = ScanRequest::current().touching(vec![3, 7]);
        assert_eq!(run(&f, &req), expected(&f.rows, &req));

        let mut req = ScanRequest::current().touching(vec![3]);
        req.rel_types = Some(vec!["SENT".into()]);
        req.vt_min = Some(1_100);
        assert_eq!(run(&f, &req), expected(&f.rows, &req), "combined filters");
    }

    #[test]
    fn limit_truncates_after_ordering() {
        let f = Fixture::build("limit");
        let mut req = ScanRequest::current();
        req.limit = Some(17);
        let got = run(&f, &req);
        assert_eq!(got.len(), 17);
        assert_eq!(got, expected(&f.rows, &req));
    }

    #[test]
    fn zone_maps_prune_whole_segments_without_reading_them() {
        let f = Fixture::build("prune");
        let set = f.set();

        // a window before every row
        let (_, stats) = set.select(&ScanRequest::current().window(0, 500)).unwrap();
        assert_eq!(stats.segments_pruned, 2);
        assert_eq!(stats.rows_examined, 0, "pruned segments cost no row work");

        // an as_of before any batch was believed
        let mut req = ScanRequest::current();
        req.as_of_tt = 1;
        let (_, stats) = set.select(&req).unwrap();
        assert_eq!(stats.segments_pruned, 2);

        // a rel_type absent from the data
        let mut req = ScanRequest::current();
        req.rel_types = Some(vec!["NOPE".into()]);
        let (_, stats) = set.select(&req).unwrap();
        assert_eq!(stats.segments_pruned, 2);
    }

    #[test]
    fn binary_search_bounds_keep_row_work_proportional_to_the_window() {
        let f = Fixture::build("narrow");
        let set = f.set();
        let (_, wide) = set.select(&ScanRequest::current()).unwrap();
        let (_, narrow) = set
            .select(&ScanRequest::current().window(1_200, 1_240))
            .unwrap();
        assert_eq!(wide.rows_examined, 400);
        assert!(
            narrow.rows_examined < 60,
            "a 40-unit window examined {} rows",
            narrow.rows_examined
        );
    }

    #[test]
    fn materialize_returns_every_column_in_order() {
        let f = Fixture::build("materialize");
        let req = ScanRequest::current().window(1_100, 1_200);
        let (cols, stats) = f.set().materialize_edges(&req).unwrap();
        let want = expected(&f.rows, &req);
        assert_eq!(cols.len(), want.len());
        assert_eq!(stats.rows_selected as usize, want.len());
        for (i, (vt_s, vid)) in want.iter().enumerate() {
            assert_eq!(cols.vt_s[i], *vt_s);
            assert_eq!(cols.vid[i], *vid);
            assert_eq!(cols.vt_e[i], vt_s + 1);
            assert_eq!(cols.props[i], "{}");
            assert!(cols.disc[i].starts_with('#'));
        }
    }
}
