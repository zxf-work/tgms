//! The maintained views: one differential dataflow per artifact family,
//! with the registered artifacts as data (a static `params` collection of
//! artifact indices inserted at epoch 0; their parsed arguments are
//! immutable and read through `Rc<Vec<Art>>`), never one dataflow per
//! artifact.
//!
//! Shared arrangements: `nodes_by_uid`, `edges_by_src`, `edges_by_dst`
//! (full version rows), `tedges_by_src` / `tedges_by_dst` /
//! `tedges_by_pair` (compact traversal edges), `ev_by_bucket` (edge events
//! by the valid-time bucket of `vt_s`), `iv_start` / `iv_cont` (compact
//! version references by start bucket / by every further covered bucket)
//! and `versions_by_vid`. Each is built only when a family that reads it
//! has registered artifacts.
//!
//! Every family ends in a `reduce` keyed by artifact that formats the
//! payload from that artifact's maintained input group (differential
//! dataflow's `reduce` re-evaluates its closure over the whole group of a
//! key whose input changed). What that group is, per family, is the
//! README's "What is recomputed per update" table; everything upstream of
//! it is maintained by joins, `count_total`, `distinct` and, for F11,
//! `iterate`.
//!
//! Belief: the changelog keeps a closed version as a historical row (the
//! old row is retracted and re-inserted with its finite `tt_e`), so every
//! operator that reads "the current graph" filters `believed_at(as_of)`
//! itself -- at the join that first touches the row, inside `iterate`'s
//! loop body included.

use crate::families::f12_temporal_paths::{self as f12, PathVal, Step};
use crate::families::f4_diff_snapshots::{self as f4, DiffItem};
use crate::families::motif_common::{self, Event, Inst, MEv};
use crate::families::{self, Family};
use crate::model::{Kind, VersionRow, OPEN_END};
use differential_dataflow::operators::arrange::{Arranged, TraceAgent};
use differential_dataflow::operators::{CountTotal, Iterate};
use differential_dataflow::trace::implementations::ValSpine;
use differential_dataflow::{ExchangeData, VecCollection};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, HashSet};
use std::rc::Rc;

pub type Coll<'s, D> = VecCollection<'s, u64, D, isize>;
type ByKey<'s, K, V> = Arranged<'s, TraceAgent<ValSpine<K, V, u64, isize>>>;

/// Number of equal valid-time buckets over the store's epoch-0 extent; the
/// overflow bucket (everything at or past the extent's end, including
/// open-ended intervals and corrections placed past the extent) is `B`.
pub const B: u16 = 256;

/// Valid-time bucketing (B equal buckets over `[lo, hi)` plus overflow).
/// Only monotonicity matters for correctness: `s <= t` implies
/// `of(s) <= of(t)`, so an interval or window overlapping an instant
/// always covers that instant's bucket; every join on a bucket is followed
/// by the exact predicate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Buckets {
    pub lo: i64,
    pub hi: i64,
}

impl Buckets {
    /// The store's epoch-0 valid-time extent: from the smallest `vt_s` to
    /// the largest finite end (`vt_e`, or `vt_s + 1` for open-ended rows).
    pub fn from_rows(rows: &[VersionRow]) -> Buckets {
        let mut lo = i64::MAX;
        let mut hi = i64::MIN;
        for r in rows {
            lo = lo.min(r.vt_s);
            hi = hi.max(r.vt_s.saturating_add(1));
            if r.vt_e < OPEN_END {
                hi = hi.max(r.vt_e);
            }
        }
        if lo >= hi {
            Buckets { lo: 0, hi: 1 }
        } else {
            Buckets { lo, hi }
        }
    }

    pub fn of(&self, t: i64) -> u16 {
        if t < self.lo {
            0
        } else if t >= self.hi {
            B
        } else {
            (((t - self.lo) as i128 * B as i128) / ((self.hi - self.lo) as i128)) as u16
        }
    }

    /// Inclusive bucket range covered by the half-open interval `[s, e)`
    /// (an empty interval maps to `s`'s bucket alone).
    pub fn cover(&self, s: i64, e: i64) -> (u16, u16) {
        if e <= s {
            (self.of(s), self.of(s))
        } else {
            (self.of(s), self.of(e - 1))
        }
    }
}

/// Parsed, immutable per-artifact parameters.
#[derive(Clone, Debug)]
pub struct Params {
    pub as_of: i64,
    pub t_a: i64,
    pub t_b: i64,
    pub t1: i64,
    pub t2: i64,
    pub t_valid: i64,
    /// F1/F5 `uid`, F8 `target.uid`, F11/F12 `src`.
    pub uid: String,
    pub dst: String,
    pub seeds: Vec<String>,
    pub hops: usize,
    pub max_hops: usize,
    pub delta: i64,
    pub node_filter: Option<Vec<String>>,
    pub offset: usize,
    /// F10: instances kept per endpoint pair (`offset + limit`).
    pub keep: usize,
    pub kind: Kind,
}

impl Params {
    pub fn parse(family: Family, args: &Value) -> Params {
        let s = |v: &Value| v.as_str().unwrap_or("").to_string();
        let uid = match family {
            Family::F8BurstDetection => s(&args["target"]["uid"]),
            Family::F11TemporalReachability | Family::F12TemporalPaths => s(&args["src"]),
            _ => s(&args["uid"]),
        };
        let offset = families::args_offset(args);
        Params {
            as_of: families::args_as_of_tt(args),
            t_a: args["window"]["t_a"].as_i64().unwrap_or(0),
            t_b: args["window"]["t_b"].as_i64().unwrap_or(0),
            t1: args["t1"].as_i64().unwrap_or(0),
            t2: args["t2"].as_i64().unwrap_or(0),
            t_valid: args["t_valid"].as_i64().unwrap_or(0),
            uid,
            dst: s(&args["dst"]),
            seeds: args["seeds"]
                .as_array()
                .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                .unwrap_or_default(),
            hops: args.get("hops").and_then(|v| v.as_u64()).unwrap_or(1) as usize,
            max_hops: f12::args_max_hops(args),
            delta: args["delta"].as_i64().unwrap_or(0),
            node_filter: args
                .get("node_filter")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()),
            offset,
            keep: offset + families::args_limit(args),
            kind: if args.get("kind").and_then(|v| v.as_str()) == Some("edge") { Kind::Edge } else { Kind::Node },
        }
    }
}

pub struct Art {
    pub name: String,
    pub family: Family,
    pub args: Value,
    pub p: Params,
}

fn believed(tt_s: i64, tt_e: i64, as_of: i64) -> bool {
    let c = as_of.min(OPEN_END - 1);
    tt_s <= c && c < tt_e
}

fn kind_code(k: Kind) -> u8 {
    match k {
        Kind::Node => 0,
        Kind::Edge => 1,
    }
}

/// Compact reference to a version, for the bucket-routed band joins (the
/// full row is fetched by `vid` only for matched pairs).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct IvRef {
    pub vid: String,
    pub vt_s: i64,
    pub vt_e: i64,
    pub tt_s: i64,
    pub tt_e: i64,
}

impl IvRef {
    fn of(r: &VersionRow) -> IvRef {
        IvRef { vid: r.vid.clone(), vt_s: r.vt_s, vt_e: r.vt_e, tt_s: r.tt_s, tt_e: r.tt_e }
    }
}

/// Compact edge for traversals (F11, F12, F12's pruning sets).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct TEdge {
    pub vt_s: i64,
    pub eid: String,
    pub src: String,
    pub dst: String,
    pub vt_e: i64,
    pub tt_s: i64,
    pub tt_e: i64,
    pub rel_type: Option<String>,
}

impl TEdge {
    fn of(r: &VersionRow) -> TEdge {
        TEdge {
            vt_s: r.vt_s,
            eid: r.eid.clone().unwrap_or_default(),
            src: r.src.clone().unwrap_or_default(),
            dst: r.dst.clone().unwrap_or_default(),
            vt_e: r.vt_e,
            tt_s: r.tt_s,
            tt_e: r.tt_e,
            rel_type: r.rel_type.clone(),
        }
    }
    fn step(&self) -> Step {
        Step {
            vt_s: self.vt_s,
            eid: self.eid.clone(),
            src: Some(self.src.clone()),
            dst: Some(self.dst.clone()),
            rel_type: self.rel_type.clone(),
        }
    }
}

/// An F12 path prefix (artifact, arrival at its last node, edges so far).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Prefix {
    pub art: u32,
    pub arr: i64,
    pub steps: Vec<Step>,
}

/// Node or edge row in one artifact's group.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
enum Ne {
    N(VersionRow),
    E(VersionRow),
}

/// Value of a final per-artifact `reduce`: the artifact's presence record
/// (so an artifact with an empty group still publishes a payload) or one
/// maintained input record.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
enum In<T> {
    Base,
    V(T),
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
enum PairOut {
    Cnt(i64),
    Inst(Box<Inst>),
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
enum F9V {
    Count(isize),
    Events(isize),
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
enum F10V {
    Total(isize),
    Inst(Box<Inst>),
}

fn canon(payload: &Value) -> String {
    crate::digest::canonical_json(&crate::digest::canonicalize_floats(payload))
}

/// The final per-artifact `reduce`: presence record + the artifact's
/// maintained input records, formatted by `f` (which sees the records in
/// sorted order with their multiplicities).
fn finish<'s, T, F>(base: Coll<'s, u32>, xs: Coll<'s, (u32, T)>, arts: Rc<Vec<Art>>, f: F) -> Coll<'s, (u32, String)>
where
    T: ExchangeData + std::hash::Hash,
    F: Fn(&Art, &[(&T, isize)]) -> Value + 'static,
{
    base.map(|i| (i, In::Base))
        .concat(xs.map(|(i, v)| (i, In::V(v))))
        .reduce(move |i, input, out| {
            let vals: Vec<(&T, isize)> = input
                .iter()
                .filter_map(|(v, r)| match v {
                    In::V(t) => Some((t, *r)),
                    In::Base => None,
                })
                .collect();
            let payload = f(&arts[*i as usize], &vals);
            out.push((canon(&payload), 1isize));
        })
}

fn rows_of(vals: &[(&Ne, isize)]) -> (Vec<VersionRow>, Vec<VersionRow>) {
    let mut nodes = Vec::new();
    let mut edges = Vec::new();
    for (v, _) in vals {
        match v {
            Ne::N(r) => nodes.push(r.clone()),
            Ne::E(r) => edges.push(r.clone()),
        }
    }
    (nodes, edges)
}

/// Build every family's maintained view; returns `(artifact index,
/// canonical payload)` for every registered artifact of families 1-12
/// (F13 is constant and seeded by the driver).
pub fn build<'s>(
    nodes: Coll<'s, VersionRow>,
    edges: Coll<'s, VersionRow>,
    params: Coll<'s, u32>,
    arts: Rc<Vec<Art>>,
    bk: Buckets,
) -> Coll<'s, (u32, String)> {
    use Family::*;
    let present: HashSet<Family> = arts.iter().map(|a| a.family).collect();
    let has = |fs: &[Family]| fs.iter().any(|f| present.contains(f));
    let of = |f: Family| {
        let a = Rc::clone(&arts);
        params.clone().filter(move |i| a[*i as usize].family == f)
    };
    let mut outs: Vec<Coll<'s, (u32, String)>> = Vec::new();

    // ---- shared arrangements -------------------------------------------
    let nodes_by_uid: Option<ByKey<'s, String, VersionRow>> = has(&[F1EntityHistory, F3SnapshotSubgraph])
        .then(|| nodes.clone().map(|r| (r.uid.clone().unwrap_or_default(), r)).arrange_by_key());
    let need_incident = has(&[F1EntityHistory, F3SnapshotSubgraph, F5NeighborhoodEvolution, F8BurstDetection]);
    let edges_by_src: Option<ByKey<'s, String, VersionRow>> =
        need_incident.then(|| edges.clone().map(|r| (r.src.clone().unwrap_or_default(), r)).arrange_by_key());
    // Self-loops are left out of the by-dst side, so `src ∪ dst` lists each
    // incident edge once.
    let edges_by_dst: Option<ByKey<'s, String, VersionRow>> = need_incident.then(|| {
        edges.clone().filter(|r| r.src != r.dst).map(|r| (r.dst.clone().unwrap_or_default(), r)).arrange_by_key()
    });

    // ---- F1 entity_history: uid ⋈ nodes_by_uid, ⋈ incident edges --------
    if has(&[F1EntityHistory]) {
        let a = Rc::clone(&arts);
        let q = of(F1EntityHistory).map(move |i| (a[i as usize].p.uid.clone(), i));
        let n = q.clone().join_core(nodes_by_uid.clone().unwrap(), |_u, &i, r| Some((i, Ne::N(r.clone()))));
        let e1 = q.clone().join_core(edges_by_src.clone().unwrap(), |_u, &i, r| Some((i, Ne::E(r.clone()))));
        let e2 = q.join_core(edges_by_dst.clone().unwrap(), |_u, &i, r| Some((i, Ne::E(r.clone()))));
        outs.push(finish(of(F1EntityHistory), n.concat(e1).concat(e2), Rc::clone(&arts), |art, vals| {
            let (n, e) = rows_of(vals);
            families::f1_entity_history::compute(&art.args, &n, &e)
        }));
    }

    // ---- F5 neighborhood_evolution: uid ⋈ incident edges ---------------
    if has(&[F5NeighborhoodEvolution]) {
        let keep = |p: &Params, r: &VersionRow| {
            r.believed_at(p.as_of) && (r.valid_at(p.t1) || r.valid_at(p.t2) || r.overlaps(p.t1, p.t2))
        };
        let a = Rc::clone(&arts);
        let q = of(F5NeighborhoodEvolution).map(move |i| (a[i as usize].p.uid.clone(), i));
        let (a1, a2) = (Rc::clone(&arts), Rc::clone(&arts));
        let e1 = q.clone().join_core(edges_by_src.clone().unwrap(), move |_u, &i, r| {
            keep(&a1[i as usize].p, r).then(|| (i, Ne::E(r.clone())))
        });
        let e2 = q.join_core(edges_by_dst.clone().unwrap(), move |_u, &i, r| {
            keep(&a2[i as usize].p, r).then(|| (i, Ne::E(r.clone())))
        });
        outs.push(finish(of(F5NeighborhoodEvolution), e1.concat(e2), Rc::clone(&arts), |art, vals| {
            let (_, e) = rows_of(vals);
            families::f5_neighborhood_evolution::compute(&art.args, &art.p.uid, &e)
        }));
    }

    // ---- F8 burst_detection: uid ⋈ incident edges; (art, bucket) count ---
    if has(&[F8BurstDetection]) {
        use families::f8_burst_detection as f8;
        let a = Rc::clone(&arts);
        let q = of(F8BurstDetection).map(move |i| (a[i as usize].p.uid.clone(), i));
        let bucket = |art: &Art, r: &VersionRow| {
            if f8::counts_edge(&art.args, &art.p.uid, r) {
                f8::bucket_index(&art.args, r.vt_s).map(|b| b as u64)
            } else {
                None
            }
        };
        let (a1, a2) = (Rc::clone(&arts), Rc::clone(&arts));
        let b1 = q.clone().join_core(edges_by_src.clone().unwrap(), move |_u, &i, r| bucket(&a1[i as usize], r).map(|b| (i, b)));
        let b2 = q.join_core(edges_by_dst.clone().unwrap(), move |_u, &i, r| bucket(&a2[i as usize], r).map(|b| (i, b)));
        let counts = b1.concat(b2).count_total().map(|((i, b), n)| (i, (b, n as i64)));
        outs.push(finish(of(F8BurstDetection), counts, Rc::clone(&arts), |art, vals| {
            let counts: BTreeMap<usize, i64> = vals.iter().map(|(v, _)| (v.0 as usize, v.1)).collect();
            f8::payload_from_counts(&art.args, &counts)
        }));
    }

    // ---- F3 snapshot_subgraph: seeds ⋈ incident edges valid at t, one join
    // per hop; node rows of the set; induced edges (src in set ⋈ dst in set)
    if has(&[F3SnapshotSubgraph]) {
        let max_hops = arts.iter().filter(|x| x.family == F3SnapshotSubgraph).map(|x| x.p.hops).max().unwrap_or(0);
        let a = Rc::clone(&arts);
        let mut cset: Coll<'s, (String, u32)> = of(F3SnapshotSubgraph)
            .flat_map(move |i| a[i as usize].p.seeds.iter().map(move |s| (s.clone(), i)).collect::<Vec<_>>())
            .distinct();
        for h in 1..=max_hops {
            let (a1, a2) = (Rc::clone(&arts), Rc::clone(&arts));
            let fwd = cset.clone().join_core(edges_by_src.clone().unwrap(), move |_u, &i, r| {
                let p = &a1[i as usize].p;
                (p.hops >= h && r.valid_at(p.t_valid) && r.believed_at(p.as_of))
                    .then(|| (r.dst.clone().unwrap_or_default(), i))
            });
            let bwd = cset.clone().join_core(edges_by_dst.clone().unwrap(), move |_u, &i, r| {
                let p = &a2[i as usize].p;
                (p.hops >= h && r.valid_at(p.t_valid) && r.believed_at(p.as_of))
                    .then(|| (r.src.clone().unwrap_or_default(), i))
            });
            cset = cset.concat(fwd).concat(bwd).distinct();
        }
        let n = cset.clone().join_core(nodes_by_uid.clone().unwrap(), |_u, &i, r| Some((i, Ne::N(r.clone()))));
        let a3 = Rc::clone(&arts);
        let e = cset
            .clone()
            .join_core(edges_by_src.clone().unwrap(), move |_u, &i, r| {
                let p = &a3[i as usize].p;
                (r.valid_at(p.t_valid) && r.believed_at(p.as_of))
                    .then(|| ((i, r.dst.clone().unwrap_or_default()), r.clone()))
            })
            .semijoin(cset.map(|(u, i)| (i, u)))
            .map(|((i, _), r)| (i, Ne::E(r)));
        outs.push(finish(of(F3SnapshotSubgraph), n.concat(e), Rc::clone(&arts), |art, vals| {
            let (n, e) = rows_of(vals);
            families::f3_snapshot_subgraph::compute(&art.args, &n, &e)
        }));
    }

    // ---- F2 / F4: valid-time interval routing ----------------------------
    if has(&[F2VersionHistory, F4DiffSnapshots]) {
        let mut kinds: HashSet<u8> = HashSet::new();
        for x in arts.iter() {
            match x.family {
                F2VersionHistory => {
                    kinds.insert(kind_code(x.p.kind));
                }
                F4DiffSnapshots => {
                    kinds.insert(0);
                    kinds.insert(1);
                }
                _ => {}
            }
        }
        let kinds = Rc::new(kinds);
        let k1 = Rc::clone(&kinds);
        let versions = nodes.clone().concat(edges.clone()).filter(move |r| k1.contains(&kind_code(r.kind)));
        let iv_start: ByKey<'s, (u8, u16), IvRef> =
            versions.clone().map(move |r| ((kind_code(r.kind), bk.cover(r.vt_s, r.vt_e).0), IvRef::of(&r))).arrange_by_key();
        let iv_cont: ByKey<'s, (u8, u16), IvRef> = versions
            .clone()
            .flat_map(move |r| {
                let (s, e) = bk.cover(r.vt_s, r.vt_e);
                let k = kind_code(r.kind);
                let iv = IvRef::of(&r);
                (s + 1..=e).map(move |b| ((k, b), iv.clone())).collect::<Vec<_>>()
            })
            .arrange_by_key();
        let versions_by_vid: ByKey<'s, String, VersionRow> =
            versions.map(|r| (r.vid.clone(), r)).arrange_by_key();

        if has(&[F2VersionHistory]) {
            // Band join, each (version, artifact) pair once: J1 = version's
            // start bucket inside the window's buckets; J2 = window's start
            // bucket strictly after the version's start and inside it.
            let (a1, a2) = (Rc::clone(&arts), Rc::clone(&arts));
            let cover = of(F2VersionHistory).flat_map(move |i| {
                let p = &a1[i as usize].p;
                let (s, e) = bk.cover(p.t_a, p.t_b);
                let k = kind_code(p.kind);
                (s..=e).map(move |b| ((k, b), i)).collect::<Vec<_>>()
            });
            let start = of(F2VersionHistory).map(move |i| {
                let p = &a2[i as usize].p;
                ((kind_code(p.kind), bk.cover(p.t_a, p.t_b).0), i)
            });
            let keep2 = |p: &Params, v: &IvRef| v.vt_s < p.t_b && v.vt_e > p.t_a && v.tt_s <= p.as_of.min(OPEN_END - 1);
            let (a3, a4) = (Rc::clone(&arts), Rc::clone(&arts));
            let j1 = cover.join_core(iv_start.clone(), move |_b, &i, v| keep2(&a3[i as usize].p, v).then(|| (v.vid.clone(), i)));
            let j2 = start.join_core(iv_cont.clone(), move |_b, &i, v| keep2(&a4[i as usize].p, v).then(|| (v.vid.clone(), i)));
            let rows = j1.concat(j2).join_core(versions_by_vid.clone(), |_vid, &i, r| Some((i, r.clone())));
            outs.push(finish(of(F2VersionHistory), rows, Rc::clone(&arts), |art, vals| {
                let rows: Vec<VersionRow> = vals.iter().map(|(r, _)| (*r).clone()).collect();
                families::f2_version_history::compute(&art.args, &rows)
            }));
        }

        if has(&[F4DiffSnapshots]) {
            // Instant routing: the artifact sits in the bucket of t1 and of
            // t2 (per kind); a version is in every bucket it covers, so each
            // (version, instant) pair meets exactly once.
            let a1 = Rc::clone(&arts);
            let inst = of(F4DiffSnapshots).flat_map(move |i| {
                let p = &a1[i as usize].p;
                let mut v = Vec::with_capacity(4);
                for k in [0u8, 1u8] {
                    v.push(((k, bk.of(p.t1)), (i, 1u8)));
                    v.push(((k, bk.of(p.t2)), (i, 2u8)));
                }
                v
            });
            let hit = |arr: ByKey<'s, (u8, u16), IvRef>| {
                let a = Rc::clone(&arts);
                inst.clone().join_core(arr, move |_b, &(i, w), v| {
                    let p = &a[i as usize].p;
                    let t = if w == 1 { p.t1 } else { p.t2 };
                    (v.vt_s <= t && t < v.vt_e && believed(v.tt_s, v.tt_e, p.as_of)).then(|| (v.vid.clone(), (i, w)))
                })
            };
            let rows = hit(iv_start.clone()).concat(hit(iv_cont.clone())).join_core(
                versions_by_vid.clone(),
                |_vid, &(i, w), r| {
                    let node = r.is_node();
                    let id = if node { r.uid.clone() } else { r.eid.clone() }.unwrap_or_default();
                    Some(((i, node, id), (w, r.clone())))
                },
            );
            // Per (artifact, entity): its state at t1 and t2 -> a DiffItem
            // only when the two differ.
            let items = rows
                .reduce(|(_i, node, id), input, out| {
                    let s1 = input.iter().rfind(|(v, _)| v.0 == 1).map(|(v, _)| &v.1);
                    let s2 = input.iter().rfind(|(v, _)| v.0 == 2).map(|(v, _)| &v.1);
                    if let Some(it) = f4::classify(*node, id, s1, s2) {
                        out.push((it, 1isize));
                    }
                })
                .map(|((i, _, _), it)| (i, it));
            outs.push(finish(of(F4DiffSnapshots), items, Rc::clone(&arts), |art, vals| {
                let items: Vec<&DiffItem> = vals.iter().map(|(v, _)| *v).collect();
                f4::payload_from_items(&art.args, &items)
            }));
        }
    }

    // ---- F6 / F7 / F9 / F10: valid-time event routing --------------------
    let event_fams = [F6AggregateEvents, F7GraphMetricTimeseries, F9CountTemporalMotifs, F10FindTemporalMotifInstances];
    if has(&event_fams) {
        let ev_by_bucket: ByKey<'s, u16, VersionRow> = edges.clone().map(move |r| (bk.of(r.vt_s), r)).arrange_by_key();
        let a1 = Rc::clone(&arts);
        let ev_arts = params
            .clone()
            .filter({
                let a = Rc::clone(&arts);
                move |i| event_fams.contains(&a[*i as usize].family)
            })
            .flat_map(move |i| {
                let p = &a1[i as usize].p;
                let (s, e) = bk.cover(p.t_a, p.t_b);
                (s..=e).map(move |b| (b, i)).collect::<Vec<_>>()
            });
        let a2 = Rc::clone(&arts);
        let hits: Coll<'s, (u32, MEv)> = ev_arts.join_core(ev_by_bucket, move |_b, &i, r| {
            let art = &a2[i as usize];
            let p = &art.p;
            if !(r.vt_s >= p.t_a && r.vt_s < p.t_b && r.believed_at(p.as_of)) {
                return None;
            }
            let ok = match art.family {
                F6AggregateEvents => families::f6_aggregate_events::rel_ok(&art.args, r.rel_type.as_deref()),
                F9CountTemporalMotifs | F10FindTemporalMotifInstances => match &p.node_filter {
                    None => true,
                    Some(nf) => {
                        let s = r.src.as_deref().unwrap_or("");
                        let d = r.dst.as_deref().unwrap_or("");
                        nf.iter().any(|x| x == s) && nf.iter().any(|x| x == d)
                    }
                },
                _ => true,
            };
            ok.then(|| (i, MEv::from_row(r)))
        });
        let hits_of = |fams: &'static [Family]| {
            let a = Rc::clone(&arts);
            hits.clone().filter(move |(i, _)| fams.contains(&a[*i as usize].family))
        };

        if has(&[F6AggregateEvents]) {
            let a = Rc::clone(&arts);
            let counts = hits_of(&[F6AggregateEvents])
                .map(move |(i, ev)| {
                    let key = if families::f6_aggregate_events::role(&a[i as usize].args) == "dst" { ev.dst } else { ev.src };
                    (i, key)
                })
                .count_total()
                .map(|((i, key), n)| (i, (key, n as i64)));
            outs.push(finish(of(F6AggregateEvents), counts, Rc::clone(&arts), |art, vals| {
                let counts: Vec<(String, i64)> = vals.iter().map(|(v, _)| (v.0.clone(), v.1)).collect();
                families::f6_aggregate_events::payload_from_counts(&art.args, &counts)
            }));
        }

        if has(&[F7GraphMetricTimeseries]) {
            let a = Rc::clone(&arts);
            let counts = hits_of(&[F7GraphMetricTimeseries])
                .flat_map(move |(i, ev)| {
                    families::f7_graph_metric_timeseries::bucket_index(&a[i as usize].args, ev.t).map(|b| (i, b as u64))
                })
                .count_total()
                .map(|((i, b), n)| (i, (b, n as i64)));
            outs.push(finish(of(F7GraphMetricTimeseries), counts, Rc::clone(&arts), |art, vals| {
                let counts: BTreeMap<usize, i64> = vals.iter().map(|(v, _)| (v.0 as usize, v.1)).collect();
                families::f7_graph_metric_timeseries::payload_from_counts(&art.args, &counts)
            }));
        }

        if has(&[F9CountTemporalMotifs, F10FindTemporalMotifInstances]) {
            // Per (artifact, unordered endpoint pair): the pair's ordered
            // events -> its instance count (F9, F10) and its first
            // `offset + limit` instances (F10).
            let a = Rc::clone(&arts);
            let pair_out = hits_of(&[F9CountTemporalMotifs, F10FindTemporalMotifInstances])
                .map(|(i, ev)| ((i, ev.pair()), ev))
                .reduce(move |(i, _pair), input, out| {
                    let art = &a[*i as usize];
                    let mut evs: Vec<Event> = Vec::with_capacity(input.len());
                    for (e, r) in input {
                        for _ in 0..(*r).max(0) {
                            evs.push(e.as_event());
                        }
                    }
                    let n = motif_common::pingpong_count(&evs, art.p.delta);
                    if n > 0 {
                        out.push((PairOut::Cnt(n as i64), 1isize));
                    }
                    if art.family == F10FindTemporalMotifInstances {
                        for t in motif_common::pingpong_first(&evs, art.p.delta, art.p.keep) {
                            out.push((PairOut::Inst(Box::new(families::f10_find_temporal_motif_instances::inst_of(&evs, t))), 1));
                        }
                    }
                })
                .map(|((i, _pair), o)| (i, o));
            let totals = pair_out
                .clone()
                .explode(|(i, o)| match o {
                    PairOut::Cnt(n) => Some((i, n as isize)),
                    PairOut::Inst(_) => None,
                })
                .count_total();

            if has(&[F9CountTemporalMotifs]) {
                let a = Rc::clone(&arts);
                let n_events = hits_of(&[F9CountTemporalMotifs]).map(|(i, _)| i).count_total();
                let xs = totals
                    .clone()
                    .filter(move |(i, _)| a[*i as usize].family == F9CountTemporalMotifs)
                    .map(|(i, n)| (i, F9V::Count(n)))
                    .concat(n_events.map(|(i, n)| (i, F9V::Events(n))));
                outs.push(finish(of(F9CountTemporalMotifs), xs, Rc::clone(&arts), |_art, vals| {
                    let mut count = 0isize;
                    let mut events = 0isize;
                    for (v, _) in vals {
                        match v {
                            F9V::Count(n) => count = *n,
                            F9V::Events(n) => events = *n,
                        }
                    }
                    families::f9_count_temporal_motifs::payload(count as i64, events as i64)
                }));
            }
            if has(&[F10FindTemporalMotifInstances]) {
                let (a1, a2) = (Rc::clone(&arts), Rc::clone(&arts));
                let xs = totals
                    .filter(move |(i, _)| a1[*i as usize].family == F10FindTemporalMotifInstances)
                    .map(|(i, n)| (i, F10V::Total(n)))
                    .concat(pair_out.flat_map(move |(i, o)| match o {
                        PairOut::Inst(x) if a2[i as usize].family == F10FindTemporalMotifInstances => {
                            Some((i, F10V::Inst(x)))
                        }
                        _ => None,
                    }));
                outs.push(finish(of(F10FindTemporalMotifInstances), xs, Rc::clone(&arts), |art, vals| {
                    let mut total = 0usize;
                    let mut insts: Vec<&Inst> = Vec::new();
                    for (v, r) in vals {
                        match v {
                            F10V::Total(n) => total = *n as usize,
                            F10V::Inst(x) => {
                                for _ in 0..(*r).max(0) {
                                    insts.push(x.as_ref());
                                }
                            }
                        }
                    }
                    let lo = art.p.offset.min(insts.len());
                    let hi = art.p.keep.min(insts.len());
                    let window: Vec<Inst> = insts[lo..hi].iter().map(|x| (*x).clone()).collect();
                    families::f10_find_temporal_motif_instances::payload_from_window(&window, total, art.p.offset)
                }));
            }
        }
    }

    // ---- F11 / F12: traversals along edges_by_src ------------------------
    if has(&[F11TemporalReachability, F12TemporalPaths]) {
        let tedges_by_src: ByKey<'s, String, TEdge> = edges.clone().map(|r| (r.src.clone().unwrap_or_default(), TEdge::of(&r))).arrange_by_key();

        if has(&[F11TemporalReachability]) {
            // Earliest arrival, least fixpoint by `iterate`:
            //   arr(art, src) = t_a;
            //   arr(art, v) = min over believed edges (u -> v) of
            //                 tau = max(arr(art, u), vt_s), kept when
            //                 tau < vt_e and tau < t_b.
            // Each round re-derives from the seeds (the variable at round
            // k+1 is the body applied to round k), so a retracted edge's
            // arrivals are withdrawn and the next-best ones re-derived.
            let a = Rc::clone(&arts);
            let seeds: Coll<'s, ((u32, String), i64)> = of(F11TemporalReachability).map(move |i| {
                let p = &a[i as usize].p;
                ((i, p.uid.clone()), p.t_a)
            });
            let a_body = Rc::clone(&arts);
            let seeds_in = seeds.clone();
            let by_src = tedges_by_src.clone();
            let arrivals = seeds
                .iterate(move |scope, inner| {
                    let by_src = by_src.enter(scope);
                    let seeds = seeds_in.enter(scope);
                    inner
                        .map(|((i, n), tau)| (n, (i, tau)))
                        .join_core(by_src, move |_u, &(i, tau), e| {
                            let p = &a_body[i as usize].p;
                            if !believed(e.tt_s, e.tt_e, p.as_of) {
                                return None;
                            }
                            let t2 = tau.max(e.vt_s);
                            (t2 < e.vt_e && t2 < p.t_b).then(|| ((i, e.dst.clone()), t2))
                        })
                        .concat(seeds)
                        .reduce(|_k, s, t| t.push((*s[0].0, 1isize)))
                })
                .consolidate();
            let a2 = Rc::clone(&arts);
            let rows = arrivals.flat_map(move |((i, n), tau)| (n != a2[i as usize].p.uid).then_some((i, (tau, n))));
            outs.push(finish(of(F11TemporalReachability), rows, Rc::clone(&arts), |art, vals| {
                let rows: Vec<(i64, &str)> = vals.iter().map(|(v, _)| (v.0, v.1.as_str())).collect();
                let cursor = families::args_cursor(&art.args);
                families::f11_temporal_reachability::format_sorted(&rows, families::args_limit(&art.args), cursor.as_deref())
            }));
        }

        if has(&[F12TemporalPaths]) {
            let h_max = arts.iter().filter(|x| x.family == F12TemporalPaths).map(|x| x.p.max_hops).max().unwrap_or(0);
            let tedges_by_dst: ByKey<'s, String, TEdge> =
                edges.clone().map(|r| (r.dst.clone().unwrap_or_default(), TEdge::of(&r))).arrange_by_key();
            let tedges_by_pair: ByKey<'s, (String, String), TEdge> = edges
                .clone()
                .map(|r| ((r.src.clone().unwrap_or_default(), r.dst.clone().unwrap_or_default()), TEdge::of(&r)))
                .arrange_by_key();
            // An edge can lie on a path of this artifact only if believed
            // and overlapping the window (tau >= t_a, tau < vt_e, tau < t_b).
            fn feasible(p: &Params, e: &TEdge) -> bool {
                believed(e.tt_s, e.tt_e, p.as_of) && e.vt_s < p.t_b && e.vt_e > p.t_a
            }
            /// Extend a prefix ending at `e.src` by `e`; `None` when the edge
            /// is not traversable at the prefix's arrival, or revisits the
            /// source or an interior node.
            fn extend(p: &Params, pf: &Prefix, e: &TEdge) -> Option<Prefix> {
                if !believed(e.tt_s, e.tt_e, p.as_of) || e.dst == p.uid {
                    return None;
                }
                if pf.steps.iter().any(|s| s.dst.as_deref() == Some(e.dst.as_str())) {
                    return None;
                }
                let tau = pf.arr.max(e.vt_s);
                if tau >= e.vt_e || tau >= p.t_b {
                    return None;
                }
                let mut steps = pf.steps.clone();
                steps.push(e.step());
                Some(Prefix { art: pf.art, arr: tau, steps })
            }

            // Hop pruning: dist(art, x) = fewest feasible edges from x to the
            // artifact's dst (ignoring time order -- an over-approximation,
            // so pruning never drops a completable prefix), for 1..h_max-1.
            let mut complete: Vec<Coll<'s, (u32, PathVal)>> = Vec::new();
            let dist: Option<ByKey<'s, (u32, String), u64>> = (h_max >= 2).then(|| {
                let a = Rc::clone(&arts);
                let targets = of(F12TemporalPaths).map(move |i| (a[i as usize].p.dst.clone(), i));
                let a1 = Rc::clone(&arts);
                let mut reach: Coll<'s, (String, u32)> = targets
                    .join_core(tedges_by_dst.clone(), move |_d, &i, e| {
                        feasible(&a1[i as usize].p, e).then(|| (e.src.clone(), i))
                    })
                    .distinct();
                let mut parts = reach.clone().map(|(x, i)| ((i, x), 1u64));
                for j in 2..h_max {
                    let a2 = Rc::clone(&arts);
                    let pred = reach.clone().join_core(tedges_by_dst.clone(), move |_x, &i, e| {
                        feasible(&a2[i as usize].p, e).then(|| (e.src.clone(), i))
                    });
                    reach = reach.concat(pred).distinct();
                    parts = parts.concat(reach.clone().map(move |(x, i)| ((i, x), j as u64)));
                }
                parts.reduce(|_k, s, t| t.push((*s[0].0, 1isize))).arrange_by_key()
            });

            let a0 = Rc::clone(&arts);
            let mut cur: Coll<'s, (String, Prefix)> = of(F12TemporalPaths).map(move |i| {
                let p = &a0[i as usize].p;
                (p.uid.clone(), Prefix { art: i, arr: p.t_a, steps: Vec::new() })
            });
            for h in 1..=h_max {
                // Artifacts whose last hop this is: join straight on (node, dst).
                let (a1, a2) = (Rc::clone(&arts), Rc::clone(&arts));
                let last = cur
                    .clone()
                    .filter(move |(_, pf)| a1[pf.art as usize].p.max_hops == h)
                    .map(move |(n, pf)| ((n, a2[pf.art as usize].p.dst.clone()), pf));
                let a3 = Rc::clone(&arts);
                complete.push(last.join_core(tedges_by_pair.clone(), move |_k, pf, e| {
                    extend(&a3[pf.art as usize].p, pf, e)
                        .map(|q| (q.art, PathVal { arrival: q.arr, hops: h, steps: q.steps }))
                }));
                if h == h_max {
                    break;
                }
                // The rest: one join along edges_by_src; prefixes that reach
                // dst complete, the others continue if dst is still within
                // reach in the hops left.
                let (a4, a5) = (Rc::clone(&arts), Rc::clone(&arts));
                let ext = cur
                    .filter(move |(_, pf)| a4[pf.art as usize].p.max_hops > h)
                    .join_core(tedges_by_src.clone(), move |_u, pf, e| {
                        extend(&a5[pf.art as usize].p, pf, e).map(|q| (e.dst.clone(), q))
                    });
                let a6 = Rc::clone(&arts);
                complete.push(ext.clone().flat_map(move |(v, q)| {
                    (v == a6[q.art as usize].p.dst).then_some((q.art, PathVal { arrival: q.arr, hops: h, steps: q.steps }))
                }));
                let (a7, a8) = (Rc::clone(&arts), Rc::clone(&arts));
                let cont = ext
                    .filter(move |(v, q)| *v != a7[q.art as usize].p.dst)
                    .map(|(v, q)| ((q.art, v), q))
                    .join_core(dist.clone().unwrap(), move |(_i, v), q, &d| {
                        (d as usize <= a8[q.art as usize].p.max_hops - h).then(|| (v.clone(), q.clone()))
                    });
                cur = cont;
            }
            let none = of(F12TemporalPaths).filter(|_| false).map(|i| (i, PathVal { arrival: 0, hops: 0, steps: Vec::new() }));
            let paths = complete.into_iter().fold(none, |acc, c| acc.concat(c));
            outs.push(finish(of(F12TemporalPaths), paths, Rc::clone(&arts), |art, vals| {
                let mut ps: Vec<&PathVal> = Vec::new();
                for (v, r) in vals {
                    for _ in 0..(*r).max(0) {
                        ps.push(v);
                    }
                }
                f12::payload_from_paths(f12::args_k(&art.args), &ps)
            }));
        }
    }

    let mut all = outs.pop().unwrap_or_else(|| params.filter(|_| false).map(|i| (i, String::new())));
    for o in outs {
        all = all.concat(o);
    }
    all
}
