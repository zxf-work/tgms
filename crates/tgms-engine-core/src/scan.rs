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
//!   AND (src_id IN touching OR dst_id IN touching)   -- or AND, see below
//! ORDER BY vt_s, vid
//! ```
//!
//! Work is skipped in three escalating tiers: whole segments are dropped from
//! the manifest's zone maps without any I/O; inside a surviving segment the
//! sorted `vt_s` column turns both window bounds into binary searches; only
//! what is left is examined per row.

use std::cmp::Reverse;
use std::collections::BinaryHeap;

/// Parse a `TGMS_SCAN_THREADS` override: a positive integer, clamped to 64.
/// Anything unparseable (empty, zero, garbage) is treated as unset rather
/// than as an error — a measurement knob must never make reads fail.
fn scan_threads_override(v: Option<&str>) -> Option<usize> {
    v.and_then(|s| s.trim().parse::<usize>().ok())
        .filter(|&n| n >= 1)
        .map(|n| n.min(64))
}

/// Worker count for the parallel scan stages.
///
/// `TGMS_SCAN_THREADS` overrides (evaluation plan §14.3 needs the curve,
/// including oversubscription past the core count, hence the 64 clamp);
/// when unset the behavior is exactly what shipped: one worker per
/// available core, capped at 16. Read per call, so a harness can sweep
/// thread counts within one process against one warm store.
pub(crate) fn scan_threads() -> usize {
    scan_threads_override(std::env::var("TGMS_SCAN_THREADS").ok().as_deref())
        .unwrap_or_else(|| {
            std::thread::available_parallelism()
                .map(|n| n.get().min(16))
                .unwrap_or(1)
        })
}

/// Should a scan stage fan out? `units` is the stage's chunking unit
/// (segments for select, clusters for materialize), `rows` the stage's work
/// proxy (candidate rows for select, selected rows for materialize).
///
/// Recalibrated 2026-08-01 (docs/eval_resources.md §14.3): the old gates
/// were segment-count-only, and parallel select measured *slower* than
/// serial at every width 2–16 on a 1M-row store while paying 4.3× at 10M —
/// so the deciding term is rows, with the unit minimum kept only so there
/// is something to chunk. Widths below `PARALLEL_SCAN_MIN_THREADS` never
/// engage: t=2 lost to t=1 at both measured scales.
pub(crate) fn parallel_gate(threads: usize, units: usize, min_units: usize, rows: u64) -> bool {
    threads >= crate::defaults::PARALLEL_SCAN_MIN_THREADS
        && units >= min_units
        && rows >= crate::defaults::PARALLEL_SCAN_MIN_ROWS
}

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
    /// Require *both* endpoints in `touching_ids` rather than either.
    ///
    /// Or-incidence is what most callers want ("edges at this node"). Motif
    /// matching wants the and-form, and expressing it above the scan means
    /// deriving `eid` — a sha256 per row — for rows the caller is about to
    /// discard. No effect unless `touching_ids` is set.
    pub touching_both: bool,
    /// Stop after this many rows. Applied after ordering.
    pub limit: Option<usize>,
    /// Columns to materialize. `None` means all of them.
    ///
    /// This is a real pushdown, not a filter on the way out: building a
    /// string per row for a column the caller discards was the dominant cost
    /// of a wide scan, and `edges_columnar` never asks for `disc` or `props`.
    pub columns: Option<Vec<String>>,
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

    /// `touching`, but both endpoints must be in the set.
    pub fn touching_both(mut self, ids: Vec<u32>) -> Self {
        self = self.touching(ids);
        self.touching_both = true;
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
    closes: std::sync::Arc<CloseIndex>,
}

impl<'a, S: SegmentSource> ScanSet<'a, S> {
    pub fn new(targets: Vec<ScanTarget<'a, S>>) -> Self {
        Self {
            targets,
            closes: std::sync::Arc::default(),
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
    pub fn with_closes(mut self, closes: std::sync::Arc<CloseIndex>) -> Self {
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
        // Segments are independent, and a Selection carries its segment
        // index, so per-segment work fans out across threads and the results
        // concatenate in segment order — byte-identical output to the serial
        // loop by construction. Serial below the gates: thread spawn costs
        // more than it saves, and correctness never depends on which path
        // ran. The gates are row-count-based, not segment-count-based, since
        // the §14.3 sweep measured the segment gate misfiring by a decade:
        // parallel actively hurt at 1M rows (2–16 threads all slower than
        // serial) while paying 4.3× at 10M — see PARALLEL_SCAN_MIN_ROWS.
        // Candidate rows come from headers already in memory, so the gate
        // itself costs nothing.
        let threads = scan_threads();
        let candidate_rows: u64 = self
            .targets
            .iter()
            .map(|t| t.segment.rows() as u64)
            .sum();
        if parallel_gate(threads, self.targets.len(), 8, candidate_rows) {
            let chunk = self.targets.len().div_ceil(threads);
            let parts: Vec<Result<(Vec<Selection>, ScanStats)>> =
                std::thread::scope(|scope| {
                    let handles: Vec<_> = (0..self.targets.len())
                        .step_by(chunk)
                        .map(|start| {
                            let end = (start + chunk).min(self.targets.len());
                            scope.spawn(move || self.select_range(req, start, end))
                        })
                        .collect();
                    handles.into_iter().map(|h| h.join().expect("scan worker panicked")).collect()
                });
            let mut out = Vec::new();
            let mut stats = ScanStats::default();
            for part in parts {
                let (sel, st) = part?;
                out.extend(sel);
                stats.segments_total += st.segments_total;
                stats.segments_pruned += st.segments_pruned;
                stats.rows_examined += st.rows_examined;
                stats.rows_selected += st.rows_selected;
            }
            return Ok((out, stats));
        }
        self.select_range(req, 0, self.targets.len())
    }

    fn select_range(
        &self,
        req: &ScanRequest,
        lo_idx: usize,
        hi_idx: usize,
    ) -> Result<(Vec<Selection>, ScanStats)> {
        let mut stats = ScanStats {
            // this range's share; the parallel caller sums ranges
            segments_total: hi_idx - lo_idx,
            ..Default::default()
        };
        let mut out = Vec::new();
        let as_of = clamp_tt(req.as_of_tt);
        let touching = req.touching_ids.as_ref().map(|ids| IdSet::new(ids));

        for (idx, target) in self.targets.iter().enumerate().take(hi_idx).skip(lo_idx) {
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
            // The or-form can still answer with one endpoint column missing;
            // the and-form cannot, so say so rather than silently matching
            // nothing.
            if req.touching_both && touching.is_some() && (src.is_none() || dst.is_none()) {
                return Err(crate::EngineError::corrupt(
                    "both-endpoint incidence needs src_id and dst_id",
                ));
            }
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
                        let s_hit = src.is_some_and(|s| ids.contains(s[i]));
                        let d_hit = dst.is_some_and(|d| ids.contains(d[i]));
                        let hit = if req.touching_both {
                            s_hit && d_hit
                        } else {
                            s_hit || d_hit
                        };
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
    /// Group selections into clusters of overlapping composite-key ranges,
    /// in key order.
    ///
    /// Everything in cluster *i* sorts strictly before everything in cluster
    /// *i+1*, so clusters concatenate; only rows *within* a cluster ever need
    /// the heap merge. The earlier version of this check was all-or-nothing
    /// disjointness — which a single correction voided, because a superseding
    /// version lands in a segment overlapping the original's range. On a
    /// corrected store (the normal store) the fast path therefore never ran.
    /// Clusters make it the common case again: correction segments are tiny,
    /// so they form small local clusters while the bulk concatenates.
    fn cluster_order(&self, selections: &[Selection]) -> Result<Vec<Vec<usize>>> {
        let mut keys = Vec::with_capacity(selections.len());
        for sel in selections {
            if sel.rows.is_empty() {
                keys.push(None);
                continue;
            }
            let seg = self.targets[sel.segment].segment;
            let vt_s = seg.i64_column("vt_s")?;
            let hi = seg.u64_column("vid64")?;
            let lo = seg.u32_column("vid_lo32")?;
            let k = |row: u32| -> (i64, Id96) {
                let r = row as usize;
                (vt_s[r], Id96 { hi: hi[r], lo: lo[r] })
            };
            let first = *sel.rows.first().expect("non-empty");
            let last = *sel.rows.last().expect("non-empty");
            keys.push(Some((k(first), k(last))));
        }
        let mut order: Vec<usize> = (0..selections.len())
            .filter(|&si| keys[si].is_some())
            .collect();
        order.sort_by_key(|&si| keys[si].expect("filtered").0);

        let mut clusters: Vec<Vec<usize>> = Vec::new();
        let mut cur_max_last: Option<(i64, Id96)> = None;
        for si in order {
            let (first, last) = keys[si].expect("filtered");
            match (&mut clusters.last_mut(), cur_max_last) {
                (Some(cluster), Some(max_last)) if first < max_last => {
                    // overlaps the running cluster: keys are unique, so
                    // first < max_last is the exact overlap condition
                    cluster.push(si);
                    cur_max_last = Some(max_last.max(last));
                }
                _ => {
                    clusters.push(vec![si]);
                    cur_max_last = Some(last);
                }
            }
        }
        Ok(clusters)
    }

    pub fn merged(
        &self,
        selections: &[Selection],
        limit: Option<usize>,
    ) -> Result<Vec<(usize, u32)>> {
        // Hoisted per selection: the vt_s slice and both vid halves. The
        // first version resolved the vt_s column *by name* and built an Id96
        // for every row popped from the heap — lesson §2's defect, recurring
        // here at 10M rows a call.
        struct Cols<'a> {
            vt_s: &'a [i64],
            hi: &'a [u64],
            lo: &'a [u32],
        }
        let mut cols = Vec::with_capacity(selections.len());
        for sel in selections {
            let seg = self.targets[sel.segment].segment;
            cols.push(Cols {
                vt_s: seg.i64_column("vt_s")?,
                hi: seg.u64_column("vid64")?,
                lo: seg.u32_column("vid_lo32")?,
            });
        }
        let key = |si: usize, row: u32| -> (i64, Id96) {
            let c = &cols[si];
            let r = row as usize;
            (c.vt_s[r], Id96 { hi: c.hi[r], lo: c.lo[r] })
        };
        let cap = limit.unwrap_or(usize::MAX);

        // Cluster-wise: concatenate across clusters, heap-merge only within.
        // A pristine store is all singleton clusters (pure concatenation); a
        // pathological one is a single cluster (the old full heap merge); a
        // corrected store is almost-all singletons plus small local clusters.
        let mut out = Vec::new();
        'clusters: for cluster in self.cluster_order(selections)? {
            if let [si] = cluster[..] {
                let sel = &selections[si];
                for &row in &sel.rows {
                    out.push((sel.segment, row));
                    if out.len() >= cap {
                        break 'clusters;
                    }
                }
                continue;
            }
            let mut heap: BinaryHeap<Reverse<(i64, Id96, usize, usize)>> = BinaryHeap::new();
            for &si in &cluster {
                let row = selections[si].rows[0];
                let (t, vid) = key(si, row);
                heap.push(Reverse((t, vid, si, 0)));
            }
            while let Some(Reverse((_, _, si, pos))) = heap.pop() {
                let sel = &selections[si];
                out.push((sel.segment, sel.rows[pos]));
                if out.len() >= cap {
                    break 'clusters;
                }
                if let Some(&next) = sel.rows.get(pos + 1) {
                    let (t, vid) = key(si, next);
                    heap.push(Reverse((t, vid, si, pos + 1)));
                }
            }
        }
        Ok(out)
    }

    /// One selection's rows, as columns. The run loop mirrors the merged
    /// path: contiguous stretches memcpy, everything else pushes.
    fn materialize_selection(&self, req: &ScanRequest, sel: &Selection) -> Result<EdgeColumns> {
        let want = |name: &str| {
            req.columns
                .as_ref()
                .map(|c| c.iter().any(|x| x == name))
                .unwrap_or(true)
        };
        let (w_rel, w_disc, w_props) = (want("rel_type"), want("disc"), want("props"));
        let w_vid = want("vid");
        let w_addr = want("seg_id") || want("seg_row");
        let v = SegmentView::open(self.targets[sel.segment].segment)?;
        let tid = self.targets[sel.segment].id;
        let mut cols = EdgeColumns::with_capacity(sel.rows.len());
        let mut i = 0usize;
        while i < sel.rows.len() {
            let mut j = i + 1;
            while j < sel.rows.len() && sel.rows[j] == sel.rows[j - 1] + 1 {
                j += 1;
            }
            let (a, b) = (sel.rows[i] as usize, sel.rows[i] as usize + (j - i));
            copy_run(&v, a, b, &mut cols, w_vid, w_rel, w_disc, w_props)?;
            if w_addr {
                cols.seg_id.extend(std::iter::repeat_n(tid, b - a));
                cols.seg_row.extend(a as u32..b as u32);
            }
            i = j;
        }
        Ok(cols)
    }

    /// One cluster's rows, as columns: the direct run-walk for a singleton,
    /// a within-cluster heap merge otherwise.
    fn materialize_cluster(
        &self,
        req: &ScanRequest,
        selections: &[Selection],
        cluster: &[usize],
    ) -> Result<EdgeColumns> {
        if let [si] = cluster[..] {
            return self.materialize_selection(req, &selections[si]);
        }
        let members: Vec<Selection> = cluster
            .iter()
            .map(|&si| selections[si].clone())
            .collect();
        let order = self.merged(&members, None)?;
        self.materialize_order(req, &order)
    }

    pub fn materialize_edges(&self, req: &ScanRequest) -> Result<(EdgeColumns, ScanStats)> {
        let (selections, stats) = self.select(req)?;
        // Clusters materialize independently — on threads, into their own
        // columns, appended in key order. A singleton cluster (the corrected-
        // store norm: bulk segments plus tiny local correction clusters) runs
        // the direct run-walk with no order list; a multi-member cluster
        // heap-merges its own rows only. Byte-identical to the merged path
        // because cluster boundaries are exact key-order boundaries.
        // Unlimited scans only: a limit reintroduces cross-cluster coupling
        // that the merged path already handles.
        if req.limit.is_none() {
            let clusters = self.cluster_order(&selections)?;
            // Same recalibrated gates as select(), but on *selected* rows —
            // known exactly here, and the honest proxy for materialization
            // work. The fan-out width itself is one thread per cluster,
            // exactly as before the override existed.
            let threads = scan_threads();
            let parts: Vec<Result<EdgeColumns>> =
                if parallel_gate(threads, clusters.len(), 4, stats.rows_selected) {
                std::thread::scope(|scope| {
                    let handles: Vec<_> = clusters
                        .iter()
                        .map(|cluster| {
                            let sels = &selections;
                            scope.spawn(move || self.materialize_cluster(req, sels, cluster))
                        })
                        .collect();
                    handles
                        .into_iter()
                        .map(|h| h.join().expect("materialize worker panicked"))
                        .collect()
                })
            } else {
                clusters
                    .iter()
                    .map(|cluster| self.materialize_cluster(req, &selections, cluster))
                    .collect()
            };
            let mut cols = EdgeColumns::with_capacity(stats.rows_selected as usize);
            for part in parts {
                cols.append(part?);
            }
            return Ok((cols, stats));
        }
        let order = self.merged(&selections, req.limit)?;
        let cols = self.materialize_order(req, &order)?;
        Ok((cols, stats))
    }

    /// Materialize an explicit (segment, row) order list — the general path
    /// shared by limited scans and multi-member clusters.
    fn materialize_order(
        &self,
        req: &ScanRequest,
        order: &[(usize, u32)],
    ) -> Result<EdgeColumns> {
        let want = |name: &str| {
            req.columns
                .as_ref()
                .map(|c| c.iter().any(|x| x == name))
                .unwrap_or(true)
        };
        let (w_rel, w_disc, w_props) = (want("rel_type"), want("disc"), want("props"));
        // vid is two integer columns here but a 24-char hex string at the
        // boundary, so building it unasked cost more than the scan itself
        let w_vid = want("vid");
        let w_addr = want("seg_id") || want("seg_row");
        let mut cols = EdgeColumns::with_capacity(order.len());
        // resolve each segment's columns once, then walk its rows
        let mut views: Vec<Option<SegmentView<'_>>> =
            (0..self.targets.len()).map(|_| None).collect();

        // Copy contiguous runs rather than pushing row by row. A selection is
        // usually one ascending stretch of one segment, and for the fixed-width
        // columns that turns a bounds-checked push per row into a memcpy —
        // which was the whole gap against an Arrow-backed reader at 1M rows.
        let mut i = 0usize;
        while i < order.len() {
            let (seg_idx, first_row) = order[i];
            let mut j = i + 1;
            while j < order.len()
                && order[j].0 == seg_idx
                && order[j].1 == order[j - 1].1 + 1
            {
                j += 1;
            }
            if views[seg_idx].is_none() {
                views[seg_idx] = Some(SegmentView::open(self.targets[seg_idx].segment)?);
            }
            let v = views[seg_idx].as_ref().expect("just populated");
            let (a, b) = (first_row as usize, first_row as usize + (j - i));
            copy_run(v, a, b, &mut cols, w_vid, w_rel, w_disc, w_props)?;
            if w_addr {
                let tid = self.targets[seg_idx].id;
                cols.seg_id.extend(std::iter::repeat_n(tid, b - a));
                cols.seg_row.extend(a as u32..b as u32);
            }
            i = j;
        }
        Ok(cols)
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
    /// Physical address of each returned row (`ScanTarget::id`, row within
    /// that segment), filled only when the projection asks for `seg_id` /
    /// `seg_row`. Addresses let a caller come back later for the expensive
    /// derived fields of a few surviving rows (`edge_idents_at`) instead of
    /// materializing them for the whole scan.
    pub seg_id: Vec<u64>,
    pub seg_row: Vec<u32>,
}

/// Copy rows [a, b) of one segment view into the output columns — the one
/// copy routine both materialize paths share. Fixed-width columns memcpy;
/// derived and string columns pay per row only when asked for.
#[allow(clippy::too_many_arguments)]
fn copy_run(
    v: &SegmentView<'_>,
    a: usize,
    b: usize,
    cols: &mut EdgeColumns,
    w_vid: bool,
    w_rel: bool,
    w_disc: bool,
    w_props: bool,
) -> Result<()> {
    cols.vt_s.extend_from_slice(&v.vt_s[a..b]);
    cols.src_id.extend_from_slice(&v.src_id[a..b]);
    cols.dst_id.extend_from_slice(&v.dst_id[a..b]);
    match v.vt_e {
        Some(col) => cols.vt_e.extend_from_slice(&col[a..b]),
        // elided means every row is instantaneous
        None => cols.vt_e.extend(v.vt_s[a..b].iter().map(|t| t + 1)),
    }
    if w_vid {
        cols.vid.extend(
            v.vid64[a..b]
                .iter()
                .zip(&v.vid_lo32[a..b])
                .map(|(&hi, &lo)| Id96 { hi, lo }),
        );
    }
    if w_rel {
        cols.rel_type.extend(
            v.rel_code[a..b]
                .iter()
                .map(|&c| v.rel_types[c as usize].clone()),
        );
    }
    if w_disc {
        for &r in &v.disc_ref[a..b] {
            cols.disc.push(v.strings.get(r)?.to_string());
        }
    }
    if w_props {
        for &r in &v.props_ref[a..b] {
            cols.props.push(v.strings.get(r)?.to_string());
        }
    }
    Ok(())
}

impl EdgeColumns {
    fn append(&mut self, mut o: EdgeColumns) {
        self.vt_s.append(&mut o.vt_s);
        self.vt_e.append(&mut o.vt_e);
        self.src_id.append(&mut o.src_id);
        self.dst_id.append(&mut o.dst_id);
        self.vid.append(&mut o.vid);
        self.rel_type.append(&mut o.rel_type);
        self.disc.append(&mut o.disc);
        self.props.append(&mut o.props);
        self.seg_id.append(&mut o.seg_id);
        self.seg_row.append(&mut o.seg_row);
    }

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
            seg_id: Vec::new(),
            seg_row: Vec::new(),
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
    fn clusters_mix_concatenation_and_local_merges() {
        // The corrected-store geometry: two overlapping segments (a bulk run
        // and its correction) plus one far-away disjoint segment. Output must
        // equal the brute-force (vt_s, vid) sort regardless of which cluster
        // path each segment took.
        let dir = tmp("clusters-mixed");
        let mut rows = Vec::new();
        let mut segs = Vec::new();
        for (name, base, n, tt) in [("a", 1_000i64, 200u32, 10i64),
                                    ("b", 1_150, 60, 20),
                                    ("c", 5_000, 80, 30)] {
            let mut b: Vec<EdgeRow> = (0..n)
                .map(|i| edge(base + i as i64, i + tt as u32 * 100, tt, "SENT",
                              i % 7, (i + 3) % 7))
                .collect();
            b.sort_by_key(|r| r.sort_key());
            let path = dir.join(format!("{name}.tgs"));
            write_edge_segment(&path, &b, &SegmentSpec::default()).unwrap();
            segs.push(Segment::open(&path, MemorySource::load(&path).unwrap(), true).unwrap());
            rows.extend(b);
        }
        let set = ScanSet::from_pairs(segs.iter().map(|s| (s, Lane::Event)).collect());
        let req = ScanRequest::current();
        let (cols, _) = set.materialize_edges(&req).unwrap();
        let want = expected(&rows, &req);
        assert_eq!(cols.len(), want.len());
        for (i, (vt_s, vid)) in want.iter().enumerate() {
            assert_eq!((cols.vt_s[i], cols.vid[i]), (*vt_s, *vid), "row {i}");
        }
        // and the order list agrees with itself under a limit
        let (sel, _) = set.select(&req).unwrap();
        let full = set.merged(&sel, None).unwrap();
        let capped = set.merged(&sel, Some(37)).unwrap();
        assert_eq!(&full[..37], &capped[..]);
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

    /// The env override parses strictly and fails open: only a positive
    /// integer overrides, and it is clamped, never trusted for width.
    /// (Parsing is tested through the pure function — mutating the process
    /// environment inside a threaded test harness races other tests.)
    /// The recalibrated gates (docs/eval_resources.md §14.3): rows decide,
    /// width 2–3 never engages, and the unit minimum only guards chunking.
    #[test]
    fn parallel_gate_is_row_based_and_skips_narrow_widths() {
        const ROWS: u64 = crate::defaults::PARALLEL_SCAN_MIN_ROWS;
        // a 1M-row store stays serial at every width — the measured regression
        for t in [1, 2, 4, 8, 16, 32] {
            assert!(!parallel_gate(t, 20, 8, 1_000_000), "t={t} at 1M rows");
        }
        // a 10M-row store engages from 4 workers up
        assert!(parallel_gate(4, 200, 8, 10_000_000));
        assert!(parallel_gate(16, 200, 8, 10_000_000));
        // t=2 lost to t=1 at both measured scales: never engage
        assert!(!parallel_gate(2, 200, 8, 10_000_000));
        assert!(!parallel_gate(3, 200, 8, 10_000_000));
        // nothing to chunk below the unit minimum, whatever the rows
        assert!(!parallel_gate(16, 7, 8, ROWS));
        assert!(parallel_gate(16, 8, 8, ROWS));
        // the materialize form: clusters >= 4
        assert!(parallel_gate(16, 4, 4, ROWS));
        assert!(!parallel_gate(16, 3, 4, ROWS));
    }

    #[test]
    fn scan_threads_override_parses_strictly() {
        assert_eq!(scan_threads_override(None), None);
        assert_eq!(scan_threads_override(Some("")), None);
        assert_eq!(scan_threads_override(Some("0")), None);
        assert_eq!(scan_threads_override(Some("-4")), None);
        assert_eq!(scan_threads_override(Some("many")), None);
        assert_eq!(scan_threads_override(Some("1")), Some(1));
        assert_eq!(scan_threads_override(Some(" 8 ")), Some(8));
        assert_eq!(scan_threads_override(Some("32")), Some(32));
        assert_eq!(scan_threads_override(Some("4096")), Some(64));
    }
}
