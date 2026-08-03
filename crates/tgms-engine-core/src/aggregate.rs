//! Grouped aggregation over edge events (O14 `aggregate_events`, D-044).
//!
//! Two-phase parallel aggregation, designed from the ClickHouse lessons
//! D-043 names: per-thread partial states built over the cluster-parallel
//! scan's per-segment selections, then a deterministic merge. Group keys are
//! **fixed-width codes end-to-end** — bucket index `i64`, global rel code,
//! endpoint dense id `u32`, label code — never strings; names rehydrate only
//! at output, and canonical ordering is computed on integer ranks.
//!
//! Determinism is by construction rather than by discipline: every partial
//! state is integer-valued and its merge is commutative (`count` and `sum`
//! add, `min`/`max` fold, distinct sets are id-vector *appends* that are
//! sorted and deduplicated exactly once at finalize), so the merged result
//! is byte-identical at any thread count — tested the way `scan.rs` tests
//! order, against a brute-force reference and across widths.
//!
//! The mean is the one place a float exists, and it is produced from the
//! exact integer sum by `q = s div n; q as f64 + (r as f64) / (n as f64)` —
//! the identical IEEE sequence as the Python fallback's `divmod` form, so
//! the two paths agree bit-for-bit (`ops_aggregate._mean`).

use std::collections::{BTreeSet, HashMap};

use crate::error::Result;
use crate::row::Lane;
use crate::scan::{ScanRequest, ScanSet, ScanTarget, Selection};
use crate::segment::{Segment, SegmentSource};
use crate::store::{segment_id_of, NativeStore};
use crate::{believed_at, EngineError, OPEN_END};

/// One grouping dimension, already reduced to what the kernel needs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dim {
    /// Bucket index of `vt_s` over `[t_a, t_b)` at `stride`.
    TimeBucket { stride: i64 },
    /// Global rel code (index into the sorted global rel-name table).
    RelType,
    /// Endpoint dense id; `dst` selects which end.
    Endpoint { dst: bool },
    /// Label code of the endpoint's believed node version valid at the
    /// event's `vt_s`; `-1` when none (sorts first, as the contract's
    /// null-first ordering).
    Label { dst: bool },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    VtS,
    /// `vt_e - vt_s`; rows with `vt_e = OPEN_END` contribute nothing.
    Duration,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Agg {
    Count,
    CountDistinct { dst: bool },
    Min(Source),
    Max(Source),
    Mean(Source),
}

#[derive(Clone, Debug)]
pub struct AggregateRequest {
    pub as_of_tt: i64,
    /// Events are believed edge versions with `t_a <= vt_s < t_b`.
    pub t_a: i64,
    pub t_b: i64,
    pub rel_types: Option<Vec<String>>,
    pub dims: Vec<Dim>,
    pub aggs: Vec<Agg>,
    /// Emitted-group cap; exceeding it is a `capacity` error naming both.
    pub max_groups: usize,
}

/// One aggregate's output column, aligned with the sorted group rows.
#[derive(Clone, Debug, PartialEq)]
pub enum AggValues {
    Int(Vec<Option<i64>>),
    Float(Vec<Option<f64>>),
}

#[derive(Clone, Debug, PartialEq)]
pub struct AggregateOut {
    /// One code column per requested dimension, in canonical group order.
    pub keys: Vec<Vec<i64>>,
    /// One column per requested aggregate, same order as the request.
    pub aggs: Vec<AggValues>,
    /// Global rel code -> name (sorted, so code order is name order).
    pub rel_names: Vec<String>,
    /// Label code -> name (sorted; `-1` in a key column means null).
    pub label_names: Vec<String>,
}

/// How a group remembers which endpoints it has seen (D-047).
///
/// Endpoint ids are *dense* `u32`, so a group's distinct set is a subset of
/// `0..n_entities` and a bitset over that space answers `count_distinct`
/// with a popcount instead of a sort. A bitset per group is a capacity
/// hazard, though — the endpoint dimension can emit one group per entity —
/// so a group starts as an id vector and is promoted only once that vector
/// occupies as many bytes as the bitset would (`promote_at`). Two bounds
/// follow, and both are independent of the group cap:
///
/// * a promoted group never costs more than twice what the append-only path
///   was already paying for it at the moment of promotion;
/// * at most `rows / promote_at` groups can promote, so total distinct state
///   is at most **8 bytes per selected row per thread** — twice the 4 bytes
///   per row the id-append path costs, whatever the entity count.
///
/// Both forms answer a set cardinality, so the merged result is
/// order-independent and therefore byte-identical at any thread count; the
/// merge of two bitsets is a commutative OR.
#[derive(Clone, Debug)]
enum Distinct {
    Ids(Vec<u32>),
    Bits(Vec<u64>),
}

/// Bitset geometry for one call, derived from the dense id space.
#[derive(Clone, Copy, Debug)]
pub struct DistinctPlan {
    words: usize,
    promote_at: usize,
}

impl DistinctPlan {
    /// `n_entities` is the dictionary's dense id space; ids are `< n_entities`.
    pub fn for_space(n_entities: u32) -> Self {
        let words = (n_entities as usize).div_ceil(64).max(1);
        Self {
            words,
            // 8 bytes per word against 4 bytes per appended id
            promote_at: words * 2,
        }
    }
}

fn set_bit(w: &mut Vec<u64>, id: u32) {
    let i = (id >> 6) as usize;
    // dense ids are `< dict.len()`, so this never grows in practice; the
    // check is here because silently dropping an id would be a wrong answer
    // and a panic across the PyO3 boundary is the worst failure shape there
    if i >= w.len() {
        w.resize(i + 1, 0);
    }
    w[i] |= 1u64 << (id & 63);
}

impl Distinct {
    fn insert(&mut self, id: u32, plan: &DistinctPlan) {
        match self {
            Distinct::Ids(v) => {
                v.push(id);
                if v.len() >= plan.promote_at {
                    let mut w = vec![0u64; plan.words];
                    for &x in v.iter() {
                        set_bit(&mut w, x);
                    }
                    *self = Distinct::Bits(w);
                }
            }
            Distinct::Bits(w) => set_bit(w, id),
        }
    }

    fn merge(&mut self, other: Distinct) {
        match (&mut *self, other) {
            (Distinct::Ids(a), Distinct::Ids(mut b)) => a.append(&mut b),
            (Distinct::Bits(a), Distinct::Bits(b)) => {
                if b.len() > a.len() {
                    a.resize(b.len(), 0);
                }
                for (x, y) in a.iter_mut().zip(b.iter()) {
                    *x |= *y;
                }
            }
            (Distinct::Bits(a), Distinct::Ids(b)) => {
                for id in b {
                    set_bit(a, id);
                }
            }
            (Distinct::Ids(a), Distinct::Bits(mut w)) => {
                for &id in a.iter() {
                    set_bit(&mut w, id);
                }
                *self = Distinct::Bits(w);
            }
        }
    }

    fn cardinality(&self) -> i64 {
        match self {
            Distinct::Ids(v) => {
                let mut ids = v.clone();
                ids.sort_unstable();
                ids.dedup();
                ids.len() as i64
            }
            Distinct::Bits(w) => w.iter().map(|x| x.count_ones() as i64).sum(),
        }
    }
}

/// Per-group partial state. Shapes follow the request: `distinct[i]` and
/// `stats[j]` align with the distinct/stat aggregates in request order.
#[derive(Clone, Debug)]
pub(crate) struct GroupState {
    count: u64,
    /// Seen-endpoint sets, one per `count_distinct` aggregate.
    distinct: Vec<Distinct>,
    /// `(min, max, sum, n)` per stat aggregate.
    stats: Vec<(i64, i64, i128, u64)>,
}

impl GroupState {
    fn new(n_distinct: usize, n_stats: usize) -> Self {
        Self {
            count: 0,
            distinct: vec![Distinct::Ids(Vec::new()); n_distinct],
            stats: vec![(i64::MAX, i64::MIN, 0, 0); n_stats],
        }
    }

    fn merge(&mut self, other: GroupState) {
        self.count += other.count;
        for (a, b) in self.distinct.iter_mut().zip(other.distinct) {
            a.merge(b);
        }
        for (a, b) in self.stats.iter_mut().zip(other.stats.iter()) {
            a.0 = a.0.min(b.0);
            a.1 = a.1.max(b.1);
            a.2 += b.2;
            a.3 += b.3;
        }
    }
}

/// The aggregate list split into fixed slots, resolved once per call so the
/// row loop never re-inspects the request (lesson §2: hoist everything).
struct AggLayout {
    /// Distinct aggregates: `dst?` per slot.
    distinct: Vec<bool>,
    /// Stat aggregates: source per slot.
    stats: Vec<Source>,
}

impl AggLayout {
    fn of(aggs: &[Agg]) -> Self {
        let mut distinct = Vec::new();
        let mut stats = Vec::new();
        for a in aggs {
            match a {
                Agg::Count => {}
                Agg::CountDistinct { dst } => distinct.push(*dst),
                Agg::Min(s) | Agg::Max(s) | Agg::Mean(s) => stats.push(*s),
            }
        }
        Self { distinct, stats }
    }
}

/// Label lookup: per dense id, believed valid intervals sorted by `vt_s`.
/// Believed valid intervals of one identity are disjoint (the store
/// invariant), so at most one interval contains an instant.
pub struct LabelIndex {
    by_uid: HashMap<u32, Vec<(i64, i64, u32)>>,
    pub names: Vec<String>,
}

impl LabelIndex {
    fn lookup(&self, uid: u32, t: i64) -> i64 {
        let Some(ivs) = self.by_uid.get(&uid) else {
            return -1;
        };
        let i = ivs.partition_point(|&(s, _, _)| s <= t);
        if i == 0 {
            return -1;
        }
        let (s, e, code) = ivs[i - 1];
        if s <= t && t < e {
            code as i64
        } else {
            -1
        }
    }
}

/// Everything one selection's row loop reads, resolved once per segment.
struct SegCols<'a> {
    vt_s: &'a [i64],
    vt_e: Option<&'a [i64]>,
    src: &'a [u32],
    dst: &'a [u32],
    rel: Option<&'a [u16]>,
    /// Segment-local rel code -> global rel code.
    rel_map: Vec<i64>,
}

impl<'a> SegCols<'a> {
    fn open<S: SegmentSource>(
        seg: &'a Segment<S>,
        need_rel: bool,
        global_rel: &HashMap<&str, i64>,
    ) -> Result<Self> {
        let h = seg.header();
        Ok(Self {
            vt_s: seg.i64_column("vt_s")?,
            vt_e: if h.vt_e_elided {
                None
            } else {
                Some(seg.i64_column("vt_e")?)
            },
            src: seg.u32_column("src_id")?,
            dst: seg.u32_column("dst_id")?,
            rel: if need_rel {
                Some(seg.u16_column("rel_code")?)
            } else {
                None
            },
            rel_map: if need_rel {
                h.rel_types
                    .iter()
                    .map(|r| global_rel.get(r.as_str()).copied().unwrap_or(-1))
                    .collect()
            } else {
                Vec::new()
            },
        })
    }
}

type GroupMap = HashMap<[i64; 2], GroupState>;

/// Phase 1 over one chunk of selections: fold rows into a private map.
#[allow(clippy::too_many_arguments)]
fn fold_selections<S: SegmentSource>(
    segments: &[&Segment<S>],
    selections: &[Selection],
    req: &AggregateRequest,
    layout: &AggLayout,
    global_rel: &HashMap<&str, i64>,
    labels: Option<&LabelIndex>,
    plan: &DistinctPlan,
) -> Result<GroupMap> {
    let need_rel = req.dims.iter().any(|d| matches!(d, Dim::RelType));
    let n_distinct = layout.distinct.len();
    let n_stats = layout.stats.len();
    let mut map = GroupMap::new();
    for sel in selections {
        let seg = segments[sel.segment];
        let c = SegCols::open(seg, need_rel, global_rel)?;
        // The scan's window filter is interval overlap; an *event* is
        // containment of vt_s. Selected rows ascend and vt_s is sorted
        // within a segment, so the exact cut is a prefix binary search.
        let cut = sel
            .rows
            .partition_point(|&r| c.vt_s[r as usize] < req.t_a);
        for &r in &sel.rows[cut..] {
            let i = r as usize;
            let mut key = [0i64; 2];
            for (k, d) in req.dims.iter().enumerate() {
                key[k] = match d {
                    Dim::TimeBucket { stride } => (c.vt_s[i] - req.t_a) / stride,
                    Dim::RelType => {
                        let codes = c.rel.expect("rel column resolved");
                        c.rel_map[codes[i] as usize]
                    }
                    Dim::Endpoint { dst } => {
                        (if *dst { c.dst[i] } else { c.src[i] }) as i64
                    }
                    Dim::Label { dst } => {
                        let ep = if *dst { c.dst[i] } else { c.src[i] };
                        labels.expect("label index built").lookup(ep, c.vt_s[i])
                    }
                };
            }
            let g = map
                .entry(key)
                .or_insert_with(|| GroupState::new(n_distinct, n_stats));
            g.count += 1;
            for (slot, dst) in layout.distinct.iter().enumerate() {
                g.distinct[slot].insert(if *dst { c.dst[i] } else { c.src[i] }, plan);
            }
            if !layout.stats.is_empty() {
                let vt_e = c.vt_e.map(|col| col[i]).unwrap_or(c.vt_s[i] + 1);
                for (slot, src) in layout.stats.iter().enumerate() {
                    let v = match src {
                        Source::VtS => c.vt_s[i],
                        Source::Duration => {
                            if vt_e >= OPEN_END {
                                continue; // open-ended: contributes nothing
                            }
                            vt_e - c.vt_s[i]
                        }
                    };
                    let s = &mut g.stats[slot];
                    s.0 = s.0.min(v);
                    s.1 = s.1.max(v);
                    s.2 += v as i128;
                    s.3 += 1;
                }
            }
        }
    }
    Ok(map)
}

/// Phases 1+2: parallel partials over selection chunks, merged into one map.
/// `threads` is explicit so tests can pin any width (the store entry decides
/// its width through the recalibrated scan gates); any width yields the same
/// map, because every per-group state merges commutatively.
#[allow(clippy::too_many_arguments)]
pub(crate) fn aggregate_partials<S: SegmentSource>(
    segments: &[&Segment<S>],
    selections: &[Selection],
    req: &AggregateRequest,
    global_rel: &HashMap<&str, i64>,
    labels: Option<&LabelIndex>,
    plan: DistinctPlan,
    threads: usize,
) -> Result<GroupMap> {
    let layout = AggLayout::of(&req.aggs);
    if threads <= 1 || selections.len() < 2 {
        return fold_selections(segments, selections, req, &layout, global_rel, labels, &plan);
    }
    let chunk = selections.len().div_ceil(threads);
    let parts: Vec<Result<GroupMap>> = std::thread::scope(|scope| {
        let handles: Vec<_> = selections
            .chunks(chunk)
            .map(|part| {
                let layout = &layout;
                let plan = &plan;
                scope.spawn(move || {
                    fold_selections(segments, part, req, layout, global_rel, labels, plan)
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("aggregate worker panicked"))
            .collect()
    });
    let mut merged = GroupMap::new();
    for part in parts {
        for (key, state) in part? {
            match merged.entry(key) {
                std::collections::hash_map::Entry::Occupied(mut e) => {
                    e.get_mut().merge(state)
                }
                std::collections::hash_map::Entry::Vacant(e) => {
                    e.insert(state);
                }
            }
        }
    }
    Ok(merged)
}

/// The exact-integer mean, mirroring `ops_aggregate._mean` operation for
/// operation: sums are exact, and exactly one rounding happens per term.
fn mean(sum: i128, n: u64) -> f64 {
    let q = sum.div_euclid(n as i128);
    let r = sum.rem_euclid(n as i128);
    q as f64 + (r as f64) / (n as f64)
}

/// Finalize: cap check, canonical ordering on integer ranks, aggregate
/// columns. `endpoint_rank` turns an endpoint dense id into its rank in uid
/// code-point order — the one ordering integers cannot carry on their own.
fn finalize(
    map: GroupMap,
    req: &AggregateRequest,
    rel_names: Vec<String>,
    label_names: Vec<String>,
    endpoint_rank: impl Fn(&[i64]) -> HashMap<i64, i64>,
) -> Result<AggregateOut> {
    let mut groups: Vec<([i64; 2], GroupState)> = map.into_iter().collect();
    if groups.len() > req.max_groups {
        return Err(EngineError::capacity(format!(
            "aggregate group count {} exceeds cap {}",
            groups.len(),
            req.max_groups
        )));
    }

    // ranks per dimension: integers whose order is the canonical order
    let mut rank_of: Vec<Option<HashMap<i64, i64>>> = Vec::new();
    for (k, d) in req.dims.iter().enumerate() {
        rank_of.push(match d {
            // codes already carry the order: bucket index, sorted rel
            // names, sorted label names with -1 (null) first
            Dim::TimeBucket { .. } | Dim::RelType | Dim::Label { .. } => None,
            Dim::Endpoint { .. } => {
                let codes: Vec<i64> = groups.iter().map(|(key, _)| key[k]).collect();
                Some(endpoint_rank(&codes))
            }
        });
    }
    let rank = |key: &[i64; 2]| -> [i64; 2] {
        let mut r = [0i64; 2];
        for k in 0..req.dims.len() {
            r[k] = match &rank_of[k] {
                None => key[k],
                Some(m) => m[&key[k]],
            };
        }
        r
    };
    groups.sort_by_key(|(key, _)| rank(key));

    let n = groups.len();
    let mut keys: Vec<Vec<i64>> = vec![Vec::with_capacity(n); req.dims.len()];
    for (key, _) in &groups {
        for k in 0..req.dims.len() {
            keys[k].push(key[k]);
        }
    }

    let layout = AggLayout::of(&req.aggs);
    let mut aggs_out = Vec::with_capacity(req.aggs.len());
    let (mut di, mut si) = (0usize, 0usize);
    for a in &req.aggs {
        match a {
            Agg::Count => aggs_out.push(AggValues::Int(
                groups.iter().map(|(_, g)| Some(g.count as i64)).collect(),
            )),
            Agg::CountDistinct { .. } => {
                let slot = di;
                di += 1;
                aggs_out.push(AggValues::Int(
                    groups
                        .iter()
                        .map(|(_, g)| Some(g.distinct[slot].cardinality()))
                        .collect(),
                ));
            }
            Agg::Min(_) | Agg::Max(_) | Agg::Mean(_) => {
                let slot = si;
                si += 1;
                let col: Vec<(i64, i64, i128, u64)> =
                    groups.iter().map(|(_, g)| g.stats[slot]).collect();
                aggs_out.push(match a {
                    Agg::Min(_) => AggValues::Int(
                        col.iter()
                            .map(|s| (s.3 > 0).then_some(s.0))
                            .collect(),
                    ),
                    Agg::Max(_) => AggValues::Int(
                        col.iter()
                            .map(|s| (s.3 > 0).then_some(s.1))
                            .collect(),
                    ),
                    _ => AggValues::Float(
                        col.iter()
                            .map(|s| (s.3 > 0).then(|| mean(s.2, s.3)))
                            .collect(),
                    ),
                });
            }
        }
    }
    debug_assert_eq!(di, layout.distinct.len());
    debug_assert_eq!(si, layout.stats.len());

    Ok(AggregateOut {
        keys,
        aggs: aggs_out,
        rel_names,
        label_names,
    })
}

impl NativeStore {
    /// Believed node labels as disjoint valid intervals per dense id, with
    /// a sorted global label table — the label dimension's temporal join,
    /// built once per call (nodes are few; events are many).
    fn label_index(&self, as_of_tt: i64) -> Result<LabelIndex> {
        let rows = self.all_node_versions()?;
        let mut names: BTreeSet<String> = BTreeSet::new();
        for r in &rows {
            if believed_at(r.tt_s, r.tt_e, as_of_tt) {
                names.insert(r.label.clone());
            }
        }
        let names: Vec<String> = names.into_iter().collect();
        let code: HashMap<&str, u32> = names
            .iter()
            .enumerate()
            .map(|(i, s)| (s.as_str(), i as u32))
            .collect();
        let mut by_uid: HashMap<u32, Vec<(i64, i64, u32)>> = HashMap::new();
        for r in &rows {
            if !believed_at(r.tt_s, r.tt_e, as_of_tt) {
                continue;
            }
            let Some(id) = self.dict().dense_id(&r.uid) else {
                continue;
            };
            by_uid
                .entry(id)
                .or_default()
                .push((r.vt_s, r.vt_e, code[r.label.as_str()]));
        }
        for ivs in by_uid.values_mut() {
            ivs.sort_unstable();
        }
        Ok(LabelIndex { by_uid, names })
    }

    /// O14 `aggregate_events`, engine side: scan-select in parallel, fold
    /// per-thread partials, merge, order canonically. Group keys stay
    /// fixed-width codes; the caller rehydrates names from the returned
    /// tables. Results are byte-identical at any thread count.
    pub fn aggregate_edges(&self, req: &AggregateRequest) -> Result<AggregateOut> {
        self.assert_full_belief(req.as_of_tt)?;
        let labels = if req.dims.iter().any(|d| matches!(d, Dim::Label { .. })) {
            Some(self.label_index(req.as_of_tt)?)
        } else {
            None
        };

        let m = self.manifest();
        let mut files = Vec::new();
        for (lane, entries) in [
            (Lane::Event, &m.edge_lanes.event),
            (Lane::Interval, &m.edge_lanes.interval),
        ] {
            for e in entries {
                files.push((lane, e.file.clone()));
            }
        }
        let mut segs = Vec::with_capacity(files.len());
        for (lane, file) in &files {
            segs.push((self.open_segment(file)?, *lane, segment_id_of(file)));
        }
        let seg_refs: Vec<&Segment<crate::segment::MmapSource>> =
            segs.iter().map(|(s, _, _)| &**s).collect();
        let targets: Vec<ScanTarget<'_, crate::segment::MmapSource>> = segs
            .iter()
            .map(|(segment, lane, id)| ScanTarget {
                segment,
                lane: *lane,
                id: *id,
            })
            .collect();
        let set = ScanSet::new(targets).with_closes(self.close_index()?);

        let scan_req = ScanRequest {
            as_of_tt: req.as_of_tt,
            vt_min: Some(req.t_a),
            vt_max: Some(req.t_b),
            rel_types: req.rel_types.clone(),
            ..Default::default()
        };
        let (selections, _stats) = set.select(&scan_req)?;

        // global rel codes: sorted names over the target headers, so code
        // order *is* name order and no per-row string ever exists
        let rel_names: Vec<String> = {
            let mut s: BTreeSet<String> = BTreeSet::new();
            for r in seg_refs.iter().flat_map(|seg| seg.header().rel_types.iter()) {
                s.insert(r.clone());
            }
            s.into_iter().collect()
        };
        let global_rel: HashMap<&str, i64> = rel_names
            .iter()
            .enumerate()
            .map(|(i, s)| (s.as_str(), i as i64))
            .collect();

        // same recalibrated gates as the scan stages: fan out only where the
        // §14.3 sweep showed it pays (docs/eval_resources.md)
        let rows: u64 = selections.iter().map(|s| s.rows.len() as u64).sum();
        let width = crate::scan::scan_threads();
        let width = if crate::scan::parallel_gate(width, selections.len(), 2, rows) {
            width
        } else {
            1
        };
        let mut map = aggregate_partials(
            &seg_refs,
            &selections,
            req,
            &global_rel,
            labels.as_ref(),
            // endpoint ids are dense over the dictionary, which is what
            // makes a per-group bitset possible at all (D-047)
            DistinctPlan::for_space(self.dict().len()),
            width,
        )?;
        // the contract's SQL scalar-aggregate shape: an ungrouped call emits
        // exactly one row even over an empty window (count 0, stats null)
        if req.dims.is_empty() && map.is_empty() {
            let layout = AggLayout::of(&req.aggs);
            map.insert(
                [0, 0],
                GroupState::new(layout.distinct.len(), layout.stats.len()),
            );
        }

        let label_names = labels.map(|l| l.names).unwrap_or_default();
        finalize(map, req, rel_names, label_names, |codes| {
            // rank endpoint dense ids by uid code-point order: one sort of
            // the distinct ids present, never a per-row string
            let mut distinct: Vec<i64> = codes.to_vec();
            distinct.sort_unstable();
            distinct.dedup();
            let mut order: Vec<usize> = (0..distinct.len()).collect();
            order.sort_by_key(|&i| self.dict().uid(distinct[i] as u32));
            let mut rank = HashMap::with_capacity(distinct.len());
            for (r, &i) in order.iter().enumerate() {
                rank.insert(distinct[i], r as i64);
            }
            rank
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};
    use crate::row::EdgeRow;
    use crate::scan::ScanSet;
    use crate::segment::{write_edge_segment, MemorySource, SegmentSpec};
    use std::path::PathBuf;

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-agg-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    fn edge(vt_s: i64, vt_e: i64, i: u32, tt_s: i64, rel: &str, src: u32, dst: u32) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid("a", "b", rel, &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id: src,
            dst_id: dst,
            rel_type: rel.into(),
            disc,
            vt_s,
            vt_e,
            tt_s,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    struct Fixture {
        rows: Vec<EdgeRow>,
        segs: Vec<Segment<MemorySource>>,
    }

    impl Fixture {
        /// Three segments with overlapping vt ranges, mixed rels, some
        /// open-ended rows — the shapes every aggregate must handle.
        fn build(name: &str) -> Self {
            let dir = tmp(name);
            let mut rows = Vec::new();
            let mut segs = Vec::new();
            for (batch, tt, n) in [(0u32, 10i64, 300u32), (1, 20, 200), (2, 30, 57)] {
                let mut b: Vec<EdgeRow> = (0..n)
                    .map(|i| {
                        let vt_s = 1_000 + ((i * 7 + batch * 13) % 400) as i64;
                        let vt_e = if i % 5 == 0 {
                            OPEN_END
                        } else {
                            vt_s + 1 + (i % 23) as i64
                        };
                        let rel = ["RATED", "SENT", "PAID"][(i % 3) as usize];
                        edge(vt_s, vt_e, i + batch * 1000, tt, rel, i % 13, (i + 5) % 13)
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
    }

    fn req(dims: Vec<Dim>, aggs: Vec<Agg>) -> AggregateRequest {
        AggregateRequest {
            as_of_tt: OPEN_END,
            t_a: 1_050,
            t_b: 1_350,
            rel_types: None,
            dims,
            aggs,
            max_groups: 100_000,
        }
    }

    type Stat = (i64, i64, i128, u64);
    type RefRow = ([i64; 2], (u64, Vec<i64>, Vec<Option<Stat>>));

    /// Brute-force reference over the raw rows, written the obvious way.
    fn reference(rows: &[EdgeRow], r: &AggregateRequest,
                 rel_names: &[String]) -> Vec<RefRow> {
        #[allow(clippy::type_complexity)]
        let mut groups: HashMap<[i64; 2], (u64, Vec<Vec<u32>>, Vec<(i64, i64, i128, u64)>)> =
            HashMap::new();
        let layout = AggLayout::of(&r.aggs);
        for row in rows {
            if !(row.vt_s >= r.t_a && row.vt_s < r.t_b) {
                continue;
            }
            let mut key = [0i64; 2];
            for (k, d) in r.dims.iter().enumerate() {
                key[k] = match d {
                    Dim::TimeBucket { stride } => (row.vt_s - r.t_a) / stride,
                    Dim::RelType => rel_names
                        .iter()
                        .position(|x| *x == row.rel_type)
                        .unwrap() as i64,
                    Dim::Endpoint { dst } => {
                        (if *dst { row.dst_id } else { row.src_id }) as i64
                    }
                    Dim::Label { .. } => unreachable!("no label dim in core tests"),
                };
            }
            let g = groups.entry(key).or_insert_with(|| {
                (0, vec![Vec::new(); layout.distinct.len()],
                 vec![(i64::MAX, i64::MIN, 0, 0); layout.stats.len()])
            });
            g.0 += 1;
            for (slot, dst) in layout.distinct.iter().enumerate() {
                g.1[slot].push(if *dst { row.dst_id } else { row.src_id });
            }
            for (slot, src) in layout.stats.iter().enumerate() {
                let v = match src {
                    Source::VtS => row.vt_s,
                    Source::Duration => {
                        if row.vt_e >= OPEN_END {
                            continue;
                        }
                        row.vt_e - row.vt_s
                    }
                };
                let s = &mut g.2[slot];
                s.0 = s.0.min(v);
                s.1 = s.1.max(v);
                s.2 += v as i128;
                s.3 += 1;
            }
        }
        let mut out: Vec<_> = groups
            .into_iter()
            .map(|(k, (c, dis, st))| {
                let dcounts = dis
                    .into_iter()
                    .map(|mut v| {
                        v.sort_unstable();
                        v.dedup();
                        v.len() as i64
                    })
                    .collect();
                let stats = st.into_iter().map(|s| (s.3 > 0).then_some(s)).collect();
                (k, (c, dcounts, stats))
            })
            .collect();
        out.sort_by_key(|(k, _)| *k);
        out
    }

    /// Dense id space of the fixture (uids `0..13`), and a plan that keeps
    /// every group in the id-vector form.
    const IDS: u32 = 13;

    fn run_partials(f: &Fixture, r: &AggregateRequest, threads: usize) -> GroupMap {
        run_partials_with(f, r, DistinctPlan::for_space(IDS), threads)
    }

    fn run_partials_with(f: &Fixture, r: &AggregateRequest, plan: DistinctPlan,
                         threads: usize) -> GroupMap {
        let set = ScanSet::from_pairs(f.segs.iter().map(|s| (s, Lane::Event)).collect());
        let scan_req = ScanRequest {
            as_of_tt: r.as_of_tt,
            vt_min: Some(r.t_a),
            vt_max: Some(r.t_b),
            rel_types: r.rel_types.clone(),
            ..Default::default()
        };
        let (selections, _) = set.select(&scan_req).unwrap();
        let seg_refs: Vec<&Segment<MemorySource>> = f.segs.iter().collect();
        let rel_names = ["PAID", "RATED", "SENT"];
        let global_rel: HashMap<&str, i64> = rel_names
            .iter()
            .enumerate()
            .map(|(i, s)| (*s, i as i64))
            .collect();
        aggregate_partials(&seg_refs, &selections, r, &global_rel, None, plan, threads)
            .unwrap()
    }

    #[test]
    fn partials_match_the_reference_and_every_thread_width_agrees() {
        let f = Fixture::build("widths");
        let rel_names: Vec<String> =
            ["PAID", "RATED", "SENT"].iter().map(|s| s.to_string()).collect();
        let r = req(
            vec![Dim::RelType, Dim::TimeBucket { stride: 40 }],
            vec![
                Agg::Count,
                Agg::CountDistinct { dst: true },
                Agg::Min(Source::VtS),
                Agg::Max(Source::Duration),
                Agg::Mean(Source::Duration),
            ],
        );
        let want = reference(&f.rows, &r, &rel_names);

        let base = run_partials(&f, &r, 1);
        for threads in [2, 3, 5, 8] {
            let got = run_partials(&f, &r, threads);
            // identical group sets and identical finalized values at any
            // width — the distinct vectors may differ in order, which is
            // exactly what finalize's sort+dedup erases
            assert_eq!(got.len(), base.len(), "threads={threads}");
            let a = finalize(got, &r, rel_names.clone(), vec![], |_| HashMap::new()).unwrap();
            let b = finalize(base.clone(), &r, rel_names.clone(), vec![], |_| HashMap::new())
                .unwrap();
            assert_eq!(a, b, "threads={threads}");
        }

        // and the finalized answer equals the brute-force reference
        let out = finalize(base, &r, rel_names.clone(), vec![], |_| HashMap::new()).unwrap();
        assert_eq!(out.keys[0].len(), want.len());
        for (gi, (key, (count, dcounts, stats))) in want.iter().enumerate() {
            assert_eq!([out.keys[0][gi], out.keys[1][gi]], *key);
            let AggValues::Int(counts) = &out.aggs[0] else { panic!() };
            assert_eq!(counts[gi], Some(*count as i64));
            let AggValues::Int(d) = &out.aggs[1] else { panic!() };
            assert_eq!(d[gi], Some(dcounts[0]));
            let AggValues::Int(mn) = &out.aggs[2] else { panic!() };
            assert_eq!(mn[gi], stats[0].map(|s| s.0));
            let AggValues::Int(mx) = &out.aggs[3] else { panic!() };
            assert_eq!(mx[gi], stats[1].map(|s| s.1));
            let AggValues::Float(me) = &out.aggs[4] else { panic!() };
            assert_eq!(me[gi], stats[2].map(|s| mean(s.2, s.3)));
        }
    }

    /// The non-negotiable for D-047's distinct rewrite: the answer must not
    /// depend on which representation a group happened to take, nor on how
    /// the folding was split. `promote_at` is the only knob separating the
    /// two forms, so forcing it — rather than relying on the heuristic to
    /// pick both — is what makes this a control (lessons §13); a middle
    /// value guarantees partials that promoted merging with partials that
    /// did not. Same shape as scan.rs's
    /// `every_width_materializes_the_identical_columns`.
    #[test]
    fn count_distinct_is_identical_in_both_representations_at_every_width() {
        let f = Fixture::build("distinct");
        let rel_names: Vec<String> =
            ["PAID", "RATED", "SENT"].iter().map(|s| s.to_string()).collect();
        let r = req(
            vec![Dim::RelType, Dim::TimeBucket { stride: 60 }],
            vec![
                Agg::Count,
                Agg::CountDistinct { dst: true },
                Agg::CountDistinct { dst: false },
            ],
        );
        let words = (IDS as usize).div_ceil(64).max(1);
        let ids_only = DistinctPlan { words, promote_at: usize::MAX };
        let base = finalize(
            run_partials_with(&f, &r, ids_only, 1),
            &r,
            rel_names.clone(),
            vec![],
            |_| HashMap::new(),
        )
        .unwrap();
        // and the id-vector form is the one the brute-force reference
        // mirrors, so anchor the pair to it before comparing them
        let want = reference(&f.rows, &r, &rel_names);
        let AggValues::Int(d_dst) = &base.aggs[1] else { panic!() };
        let AggValues::Int(d_src) = &base.aggs[2] else { panic!() };
        assert_eq!(d_dst.len(), want.len());
        for (gi, (_, (_, dcounts, _))) in want.iter().enumerate() {
            assert_eq!(d_dst[gi], Some(dcounts[0]));
            assert_eq!(d_src[gi], Some(dcounts[1]));
        }

        for promote_at in [1usize, 4, 37, usize::MAX] {
            for threads in [1usize, 2, 3, 5, 8] {
                let plan = DistinctPlan { words, promote_at };
                let got = finalize(
                    run_partials_with(&f, &r, plan, threads),
                    &r,
                    rel_names.clone(),
                    vec![],
                    |_| HashMap::new(),
                )
                .unwrap();
                assert_eq!(got, base, "promote_at={promote_at} threads={threads}");
            }
        }
    }

    /// A bitset sized from a stale dense-id space must still be exact: an id
    /// past its end grows it rather than being dropped, because a silently
    /// missing id is a wrong count and a panic here crosses into Python.
    #[test]
    fn an_id_past_the_planned_id_space_still_counts() {
        let mut d = Distinct::Ids(Vec::new());
        let plan = DistinctPlan { words: 1, promote_at: 1 };
        for id in [3u32, 3, 70, 4096, 70] {
            d.insert(id, &plan);
        }
        assert!(matches!(d, Distinct::Bits(_)));
        assert_eq!(d.cardinality(), 3);
    }

    #[test]
    fn group_cap_is_a_loud_capacity_error() {
        let f = Fixture::build("cap");
        let mut r = req(vec![Dim::Endpoint { dst: false }], vec![Agg::Count]);
        r.max_groups = 3;
        let map = run_partials(&f, &r, 1);
        let err = finalize(map, &r, vec![], vec![], |codes| {
            codes.iter().map(|&c| (c, c)).collect()
        })
        .unwrap_err();
        assert!(err.to_string().contains("exceeds cap 3"), "{err}");
    }

    #[test]
    fn ungrouped_request_folds_to_one_global_group() {
        let f = Fixture::build("global");
        let r = req(vec![], vec![Agg::Count, Agg::CountDistinct { dst: false }]);
        let map = run_partials(&f, &r, 1);
        assert_eq!(map.len(), 1);
        let want = reference(&f.rows, &r, &[]);
        let g = &map[&[0, 0]];
        assert_eq!(g.count, want[0].1 .0);
    }

    #[test]
    fn mean_uses_the_exact_integer_form() {
        // 2^60 + 1 over n=3: float accumulation would lose the +1
        let s: i128 = (1i128 << 60) * 3 + 2;
        let q = (s.div_euclid(3)) as f64;
        let r = (s.rem_euclid(3)) as f64;
        assert_eq!(mean(s, 3), q + r / 3.0);
    }
}
