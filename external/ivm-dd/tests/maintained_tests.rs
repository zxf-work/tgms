//! The maintained dataflow (`views.rs`, driven by `dataflow::run`) against
//! each family's whole-store reference (`dataflow::compute_payload_pub`,
//! i.e. the families' `compute` functions) under insertions, retractions
//! and corrections, epoch by epoch:
//!
//! - F11's `iterate` on a pure retraction of the edge realizing the
//!   current-best arrival: the arrival must fall back to the next-best
//!   path, then disappear when that path is retracted too, then return
//!   when an edge is re-inserted;
//! - a seeded randomized changelog over every family 1-12 (closures,
//!   corrections, fresh insertions, node property corrections, versions
//!   placed past the epoch-0 valid-time extent) compared at every epoch;
//! - the per-pair motif counting/enumeration helpers against the
//!   reference enumeration, and the valid-time bucketing's monotonicity.

use ivm_dd::changelog::{Changelog, EpochUpdate, RetractedSpan};
use ivm_dd::dataflow;
use ivm_dd::export::ArtifactSpec;
use ivm_dd::families::motif_common::{pingpong_count, pingpong_first, pingpong_triples, Event};
use ivm_dd::families::Family;
use ivm_dd::model::{Kind, VersionRow, OPEN_END};
use ivm_dd::views::{Buckets, B};
use serde_json::{json, Value};
use std::collections::HashMap;

fn node(vid: &str, uid: &str, props: Value, tt_s: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Node, vid: vid.into(), uid: Some(uid.into()), eid: None, src: None, dst: None,
        rel_type: None, label: Some("P".into()), disc: None, vt_s: 0, vt_e: OPEN_END, tt_s, tt_e: OPEN_END,
        props, source: Some("ingest".into()), provenance_ref: None,
    }
}

#[allow(clippy::too_many_arguments)] // test fixture helper mirroring VersionRow's own fields
fn edge(vid: &str, eid: &str, src: &str, dst: &str, vt_s: i64, vt_e: i64, tt_s: i64, props: Value) -> VersionRow {
    VersionRow {
        kind: Kind::Edge, vid: vid.into(), uid: None, eid: Some(eid.into()), src: Some(src.into()),
        dst: Some(dst.into()), rel_type: Some("R".into()), label: None, disc: Some("".into()),
        vt_s, vt_e, tt_s, tt_e: OPEN_END, props, source: Some("ingest".into()), provenance_ref: None,
    }
}

fn art(name: &str, op: &str, args: Value) -> ArtifactSpec {
    ArtifactSpec { name: name.into(), op: op.into(), args }
}

fn digest_of(payload: &Value) -> String {
    let canon = ivm_dd::digest::canonical_json(&ivm_dd::digest::canonicalize_floats(payload));
    ivm_dd::digest::sha256_hex(&canon)
}

/// Builder for a changelog that also keeps the live version table, so the
/// reference can be evaluated at every epoch.
struct Script {
    epoch0: Vec<VersionRow>,
    updates: Vec<EpochUpdate>,
    live: Vec<VersionRow>,
    lives: Vec<Vec<VersionRow>>,
    pending: Option<EpochUpdate>,
}

impl Script {
    fn new(epoch0: Vec<VersionRow>) -> Script {
        Script { live: epoch0.clone(), lives: vec![epoch0.clone()], epoch0, updates: Vec::new(), pending: None }
    }
    fn begin(&mut self, epoch: u64, tt: i64) {
        self.pending = Some(EpochUpdate {
            epoch, tt, correction_class: "test".into(), generator: "test".into(), placement: "test".into(),
            retractions: Vec::new(), insertions: Vec::new(), retracted_spans: Vec::new(),
        });
    }
    fn tt(&self) -> i64 {
        self.pending.as_ref().unwrap().tt
    }
    /// Close a believed version: retract the open row, insert it back with
    /// `tt_e` = this transaction (the changelog's own encoding).
    fn close(&mut self, vid: &str) -> VersionRow {
        let tt = self.tt();
        let pos = self.live.iter().position(|r| r.vid == vid && r.tt_e == OPEN_END).expect("believed vid");
        let old = self.live.remove(pos);
        let mut closed = old.clone();
        closed.tt_e = tt;
        let up = self.pending.as_mut().unwrap();
        up.retractions.push(old.clone());
        up.insertions.push(closed.clone());
        up.retracted_spans.push(RetractedSpan(if old.vt_e == OPEN_END { None } else { Some(old.vt_e - old.vt_s) }));
        self.live.push(closed);
        old
    }
    fn insert(&mut self, row: VersionRow) {
        self.pending.as_mut().unwrap().insertions.push(row.clone());
        self.live.push(row);
    }
    fn end(&mut self) {
        self.updates.push(self.pending.take().unwrap());
        self.lives.push(self.live.clone());
    }
    fn believed(&self) -> Vec<VersionRow> {
        self.live.iter().filter(|r| r.tt_e == OPEN_END).cloned().collect()
    }
}

/// Families whose reference payload was non-trivial at least once (so the
/// randomized comparison is not passing on empty answers).
fn nontrivial(op: &str, p: &Value) -> bool {
    let n = |k: &str| p[k].as_u64().unwrap_or(0) > 0;
    match op {
        "count_temporal_motifs" => n("count"),
        "diff_snapshots" => n("edges_added_total") || n("edges_removed_total") || n("props_changed_total"),
        "snapshot_subgraph" => n("rows_total") && n("nodes_total"),
        "neighborhood_evolution" => n("neighbors_gained_total") || n("neighbors_lost_total"),
        "burst_detection" => n("rows_total"),
        _ => n("rows_total"),
    }
}

/// Run the maintained dataflow over the script and compare every
/// artifact's digest, at every epoch, with the reference evaluated on that
/// epoch's version table. Returns the number of comparisons.
fn check(script: Script, artifacts: Vec<ArtifactSpec>) -> usize {
    check_cov(script, artifacts, &mut std::collections::BTreeSet::new())
}

fn check_cov(script: Script, artifacts: Vec<ArtifactSpec>, seen: &mut std::collections::BTreeSet<String>) -> usize {
    let lives = script.lives.clone();
    let cl = Changelog { cell_id: "test".into(), epoch0: script.epoch0, updates: script.updates };
    let outcome = dataflow::run(cl, artifacts.clone());
    let mut running = outcome.epoch0_digests.clone();
    let mut failures = Vec::new();
    let mut compared = 0usize;
    for (k, live) in lives.iter().enumerate() {
        if k > 0 {
            for (name, d) in &outcome.bursts[k - 1].digests {
                running.insert(name.clone(), d.clone());
            }
        }
        let nodes: Vec<VersionRow> = live.iter().filter(|r| r.is_node()).cloned().collect();
        let edges: Vec<VersionRow> = live.iter().filter(|r| !r.is_node()).cloned().collect();
        for a in &artifacts {
            let fam = Family::from_op(&a.op).unwrap();
            let reference = dataflow::compute_payload_pub(fam, &a.args, &nodes, &edges);
            if nontrivial(&a.op, &reference) {
                seen.insert(a.op.clone());
            }
            let want = digest_of(&reference);
            compared += 1;
            match running.get(&a.name) {
                Some(got) if *got == want => {}
                got => failures.push(format!(
                    "epoch {k}: {} ({}): maintained={:?} reference={want}\n  reference payload: {}",
                    a.name, a.op, got,
                    dataflow::compute_payload_pub(fam, &a.args, &nodes, &edges)
                )),
            }
        }
    }
    assert!(failures.is_empty(), "{} of {compared} comparisons disagree:\n{}", failures.len(), failures.join("\n"));
    compared
}

/// The arrival rows F11 publishes, read back from its reference payload.
fn arrivals(edges: &[VersionRow]) -> HashMap<String, i64> {
    ivm_dd::families::f11_temporal_reachability::fixpoint_arrivals(edges, "a", 0, 1000, OPEN_END)
}

#[test]
fn f11_iterate_falls_back_to_next_best_path_on_pure_retraction() {
    // a -> b at 10, b -> c at 20 (best: c at 20); a -> c at 50 (next best);
    // c -> d at 60; a cycle c -> b at 30 that must not let a retracted
    // arrival support itself.
    let e0 = vec![
        node("na", "a", json!({}), 0), node("nb", "b", json!({}), 0),
        node("nc", "c", json!({}), 0), node("nd", "d", json!({}), 0),
        edge("v1", "e1", "a", "b", 10, 11, 0, json!({})),
        edge("v2", "e2", "b", "c", 20, 21, 0, json!({})),
        edge("v3", "e3", "a", "c", 50, 51, 0, json!({})),
        edge("v4", "e4", "c", "d", 60, 61, 0, json!({})),
        edge("v5", "e5", "c", "b", 30, 31, 0, json!({})),
    ];
    let mut s = Script::new(e0);
    // epoch 1: pure retraction of the edge realizing c's best arrival.
    s.begin(1, 10);
    s.close("v2");
    s.end();
    let after1: Vec<VersionRow> = s.believed().into_iter().filter(|r| !r.is_node()).collect();
    assert_eq!(arrivals(&after1).get("c"), Some(&50), "reference: c falls back to the direct edge");
    // epoch 2: retract the fallback too -> c and d unreachable.
    s.begin(2, 20);
    s.close("v3");
    s.end();
    let after2: Vec<VersionRow> = s.believed().into_iter().filter(|r| !r.is_node()).collect();
    assert_eq!(arrivals(&after2).get("c"), None);
    // epoch 3: a new edge restores c (later), and d through it.
    s.begin(3, 30);
    s.insert(edge("v6", "e6", "b", "c", 40, 41, 30, json!({})));
    s.end();
    // epoch 4: the retracted edge is corrected back in at a different time.
    s.begin(4, 40);
    s.insert(edge("v7", "e2", "b", "c", 15, 16, 40, json!({})));
    s.end();

    let arts = vec![
        art("reach", "temporal_reachability", json!({"src": "a", "window": {"t_a": 0, "t_b": 1000}})),
        art("reach_narrow", "temporal_reachability", json!({"src": "a", "window": {"t_a": 0, "t_b": 45}})),
        art("paths", "temporal_paths", json!({"src": "a", "dst": "d", "k": 2, "max_hops": 4, "window": {"t_a": 0, "t_b": 1000}})),
    ];
    let n = check(s, arts);
    assert_eq!(n, 5 * 3);
}

#[test]
fn tiny1_epoch3_shape_superseded_row_is_not_traversable() {
    // The tiny1 fixture's epoch-3 burst in miniature: closing n0 -> n2
    // leaves the closed row (finite tt_e) in the edges collection; F11 and
    // F12 must not traverse it.
    let e0 = vec![
        node("x0", "n0", json!({}), 0), node("x1", "n1", json!({}), 0), node("x2", "n2", json!({}), 0),
        edge("a1", "e01", "n0", "n1", 100, 101, 0, json!({})),
        edge("a2", "e02", "n0", "n2", 150, 151, 0, json!({})),
        edge("a3", "e12", "n1", "n2", 200, 201, 0, json!({})),
    ];
    let mut s = Script::new(e0);
    s.begin(1, 40);
    s.close("a2");
    s.end();
    let edges_now: Vec<VersionRow> = s.believed().into_iter().filter(|r| !r.is_node()).collect();
    let arr = ivm_dd::families::f11_temporal_reachability::fixpoint_arrivals(&edges_now, "n0", 0, 1000, OPEN_END);
    assert_eq!(arr.get("n2"), Some(&200));
    let arts = vec![
        art("reach", "temporal_reachability", json!({"src": "n0", "window": {"t_a": 0, "t_b": 1000}})),
        art("paths", "temporal_paths", json!({"src": "n0", "dst": "n2", "k": 2, "window": {"t_a": 0, "t_b": 1000}})),
    ];
    check(s, arts);
}

/// xorshift64*, deterministic.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn random_artifacts(r: &mut Rng, uids: &[String]) -> Vec<ArtifactSpec> {
    let mut out = Vec::new();
    let win = |r: &mut Rng| {
        let a = r.below(900) as i64;
        let w = 50 + r.below(700) as i64;
        json!({"t_a": a, "t_b": a + w})
    };
    for j in 0..3 {
        let u = uids[r.below(uids.len() as u64) as usize].clone();
        let v = uids[r.below(uids.len() as u64) as usize].clone();
        let w = win(r);
        let (t_a, t_b) = (w["t_a"].as_i64().unwrap(), w["t_b"].as_i64().unwrap());
        let stride = ((t_b - t_a) / 8).max(1);
        let t1 = r.below(1000) as i64;
        let t2 = t1 + 1 + r.below(600) as i64;
        let tv = r.below(1100) as i64;
        let delta = 20 + r.below(200) as i64;
        let belief = ["current", "all", "superseded"][j];
        let max_hops = [4, 2, 3][j];
        out.push(art(&format!("f1_{j}"), "entity_history", json!({"uid": u, "include_edges": true, "limit": 4})));
        out.push(art(&format!("f2_{j}"), "version_history",
            json!({"kind": if j == 2 { "edge" } else { "node" }, "window": w.clone(), "limit": 5,
                   "belief": belief})));
        out.push(art(&format!("f3_{j}"), "snapshot_subgraph", json!({"seeds": [u], "t_valid": tv, "hops": 1 + (j % 2)})));
        out.push(art(&format!("f4_{j}"), "diff_snapshots", json!({"t1": t1, "t2": t2, "limit": 3})));
        out.push(art(&format!("f5_{j}"), "neighborhood_evolution", json!({"uid": u, "t1": t1, "t2": t2, "stride": ((t2 - t1) / 8).max(1)})));
        out.push(art(&format!("f6_{j}"), "aggregate_events",
            json!({"group_by": [{"dim": "endpoint", "role": if j == 1 { "dst" } else { "src" }}],
                   "aggregates": [{"agg": "count"}], "window": w.clone(), "limit": 3})));
        out.push(art(&format!("f7_{j}"), "graph_metric_timeseries", json!({"metric": "edge_event_count", "window": w.clone(), "stride": stride})));
        out.push(art(&format!("f8_{j}"), "burst_detection",
            json!({"target": {"kind": "node_activity", "uid": u}, "window": w.clone(), "stride": ((t_b - t_a) / 16).max(1),
                   "params": {"w": 3, "z": 1.0}})));
        let mut f9 = json!({"motif": "M_2node_pingpong", "window": w.clone(), "delta": delta});
        if j == 2 {
            f9["node_filter"] = json!([uids[0], uids[1], uids[2]]);
        }
        out.push(art(&format!("f9_{j}"), "count_temporal_motifs", f9.clone()));
        let mut f10 = f9.clone();
        f10["limit"] = json!(2 + j);
        if j == 1 {
            f10["cursor"] = json!("1");
        }
        out.push(art(&format!("f10_{j}"), "find_temporal_motif_instances", f10));
        out.push(art(&format!("f11_{j}"), "temporal_reachability", json!({"src": u, "window": w.clone(), "limit": 4})));
        out.push(art(&format!("f12_{j}"), "temporal_paths",
            json!({"src": u, "dst": v, "window": w.clone(), "k": 2, "max_hops": max_hops})));
    }
    out
}

fn random_script(seed: u64) -> (Script, Vec<String>) {
    let mut r = Rng(seed);
    let uids: Vec<String> = (0..7).map(|i| format!("n{i}")).collect();
    let mut e0: Vec<VersionRow> = uids.iter().map(|u| node(&format!("nv-{u}-0"), u, json!({"v": 0}), 0)).collect();
    let mut next_id = 0u64;
    let mut new_edge = |r: &mut Rng, tt: i64, max_t: u64| {
        next_id += 1;
        let s = format!("n{}", r.below(7));
        // a few self-loops, mostly distinct endpoints; many pairs repeat so
        // motifs and multi-edges occur.
        let d = if r.below(10) == 0 { s.clone() } else { format!("n{}", r.below(7)) };
        let vt_s = r.below(max_t) as i64;
        let vt_e = match r.below(5) {
            0 => OPEN_END,
            1 => vt_s + 1,
            _ => vt_s + 1 + r.below(250) as i64,
        };
        edge(&format!("ev{next_id}"), &format!("eid{next_id}"), &s, &d, vt_s, vt_e, tt, json!({"w": r.below(3)}))
    };
    for _ in 0..40 {
        e0.push(new_edge(&mut r, 0, 1000));
    }
    // a chatty pair, so the motif families have instances to maintain
    for j in 0..14i64 {
        let (a, b) = if r.below(3) == 0 { ("n1", "n0") } else if j % 2 == 0 { ("n0", "n1") } else { ("n1", "n0") };
        let t = 100 + 20 * j + r.below(15) as i64;
        e0.push(edge(&format!("cv{j}"), &format!("ceid{j}"), a, b, t, t + 1, 0, json!({})));
    }
    let mut s = Script::new(e0);
    for k in 1..=8u64 {
        let tt = 10 * k as i64;
        s.begin(k, tt);
        for _ in 0..(1 + r.below(4)) {
            let believed_edges: Vec<String> =
                s.believed().iter().filter(|x| !x.is_node()).map(|x| x.vid.clone()).collect();
            match r.below(5) {
                // pure retraction
                0 if !believed_edges.is_empty() => {
                    let vid = believed_edges[r.below(believed_edges.len() as u64) as usize].clone();
                    s.close(&vid);
                }
                // correction: same eid, new valid time (sometimes past the
                // epoch-0 extent, into the overflow bucket)
                1 if !believed_edges.is_empty() => {
                    let vid = believed_edges[r.below(believed_edges.len() as u64) as usize].clone();
                    let old = s.close(&vid);
                    let mut row = new_edge(&mut r, tt, 1600);
                    row.eid = old.eid.clone();
                    row.src = old.src.clone();
                    row.dst = old.dst.clone();
                    s.insert(row);
                }
                // property correction on an edge (same interval)
                2 if !believed_edges.is_empty() => {
                    let vid = believed_edges[r.below(believed_edges.len() as u64) as usize].clone();
                    let old = s.close(&vid);
                    let mut row = old.clone();
                    row.vid = format!("{}-p{k}", old.vid);
                    row.tt_s = tt;
                    row.props = json!({"w": 9});
                    s.insert(row);
                }
                // node property correction
                3 => {
                    let u = format!("n{}", r.below(7));
                    let nv = s.believed().into_iter().find(|x| x.uid.as_deref() == Some(u.as_str())).unwrap();
                    s.close(&nv.vid);
                    let mut row = nv.clone();
                    row.vid = format!("nv-{u}-{k}");
                    row.tt_s = tt;
                    row.props = json!({"v": k});
                    s.insert(row);
                }
                // fresh insertion
                _ => {
                    let row = new_edge(&mut r, tt, 1300);
                    s.insert(row);
                }
            }
        }
        s.end();
    }
    (s, uids)
}

#[test]
fn every_family_matches_the_reference_on_randomized_changelogs() {
    let mut total = 0usize;
    let mut seen = std::collections::BTreeSet::new();
    for seed in [0x9E37_79B9_7F4A_7C15u64, 0xD1B5_4A32_D192_ED03, 0x2545_F491_4F6C_DD1D, 42, 7] {
        let (s, uids) = random_script(seed);
        let mut r = Rng(seed ^ 0xA5A5_A5A5);
        let arts = random_artifacts(&mut r, &uids);
        assert_eq!(arts.len(), 36);
        total += check_cov(s, arts, &mut seen);
    }
    assert_eq!(total, 5 * 9 * 36);
    // every family produced a non-empty answer somewhere
    assert_eq!(seen.len(), 12, "families with a non-trivial reference answer: {seen:?}");
}

#[test]
fn pair_count_and_first_k_match_the_reference_enumeration() {
    let mut r = Rng(0x1234_5678);
    for _ in 0..300 {
        let n = 1 + r.below(30) as usize;
        let eids: Vec<String> = (0..n).map(|i| format!("e{i}")).collect();
        // sorted by (t, eid), the order the reference and the dataflow's
        // per-pair reduce both see.
        let mut evs: Vec<(i64, &str, &str, &str)> = (0..n)
            .map(|i| {
                let fwd = r.below(2) == 0;
                (r.below(60) as i64, eids[i].as_str(), if fwd { "u" } else { "v" }, if fwd { "v" } else { "u" })
            })
            .collect();
        evs.sort();
        let events: Vec<Event> = evs
            .iter()
            .map(|(t, eid, s, d)| Event { t: *t, eid, src: s, dst: d, rel_type: "R" })
            .collect();
        let delta = r.below(40) as i64;
        let all = pingpong_triples(&events, delta);
        assert_eq!(pingpong_count(&events, delta), all.len() as u64);
        for k in [0usize, 1, 3, 1000] {
            let first = pingpong_first(&events, delta, k);
            assert_eq!(first, all.iter().take(k).cloned().collect::<Vec<_>>());
        }
    }
}

#[test]
fn valid_time_buckets_are_monotone_and_cover_their_intervals() {
    let rows = vec![
        edge("a", "a", "x", "y", 1_000, 1_001, 0, json!({})),
        edge("b", "b", "x", "y", 9_000, 9_500, 0, json!({})),
        node("c", "x", json!({}), 0),
    ];
    let bk = Buckets::from_rows(&rows);
    assert_eq!((bk.lo, bk.hi), (0, 9_500));
    let mut prev = 0;
    for t in (-100..12_000).step_by(7) {
        let b = bk.of(t);
        assert!(b >= prev && b <= B);
        prev = b;
    }
    assert_eq!(bk.of(9_500), B, "at or past the extent's end is the overflow bucket");
    assert_eq!(bk.cover(5, OPEN_END).1, B, "open-ended intervals reach the overflow bucket");
    assert_eq!(bk.cover(100, 101), (bk.of(100), bk.of(100)));
}
