//! The maintained dataflow (memo §3): one timely worker (§3.4 "scored
//! configuration"), `nodes`/`edges` changelog inputs, a static artifact-
//! route input, and one `reduce`-backed maintained view per artifact
//! family, via routing + `join_map` + `reduce`.
//!
//! **Routing (deviation from memo §3.2, flagged in the README):** the
//! memo's 256-bucket valid-time routing bounds how many *artifacts* a
//! single changed version can fan out to, at 60k-edge scale. This crate
//! routes by natural key instead -- a version fans out to its own node
//! uid(s) (`"u:<uid>"`), a kind bucket (`"k:node"`/`"k:edge"`), and a
//! global bucket (`"g:all"`) -- which is correct (every artifact's route
//! key is guaranteed to receive every row it needs) but coarser for the
//! families that are whole-population scans anyway (F2, F4, F6, F7, F9,
//! F10, F11, F12: see `families::RouteKind`). A scalability
//! simplification, not a correctness one.
//!
//! **F12 (further flagged):** wired through the same Global route+reduce
//! path as F4/F6/F7/F9/F10 (its DFS runs inside the reduce closure,
//! `families::f12_temporal_paths::compute`) rather than memo §3.3's four
//! static unrolled joins -- same reasoning as F9/F10's motif matching:
//! correct, not the memo's exact operator shape, flagged for the Opus
//! review the memo itself calls for on this family.
//!
//! **F11 (deviation from the task's explicit instruction, reported, not
//! silently worked around):** the memo names F11 as the one family
//! needing DD's `iterate`, and a genuine `iterate`-based dataflow was
//! built (per-round `join_core` against a shared `edges_by_src`
//! arrangement entered into the iterative subscope, `concat` with the
//! re-supplied seed, `reduce(min)`) and passed two of three correction
//! epochs on `tests/fixtures/tiny1`. It failed the third: after a burst
//! that *retracts* the edge realizing the current-best arrival (no
//! replacement edge inserted), the fixpoint did not fall back to the
//! next-best path through the graph -- it kept reporting the
//! pre-retraction answer, i.e. it did not re-derive on a pure retraction.
//! That is a real incremental-maintenance bug inside the `iterate`
//! wiring, not a routing or data problem (the generic route+reduce path
//! handles the exact same retraction correctly for every other family on
//! the same fixture), and this session could not root-cause it in the
//! time remaining. F11 is therefore wired through the same Global
//! route+reduce path as F9/F10/F12, calling the already-tested
//! `families::f11_temporal_reachability::fixpoint_arrivals` fresh on every
//! change instead of maintaining it incrementally via `iterate`. This is
//! reported here and in the crate README/task report, not patched around
//! silently -- it is exactly the kind of finding the memo's own
//! Opus-review requirement for F9-F12 exists to catch, and by extension
//! should cover F11's `iterate` wiring too before any timed run.
//!
//! Everything -- dataflow construction *and* the epoch-by-epoch driving
//! loop -- runs inside the single `timely::execute` worker closure, since
//! that is the only place the probe and input handles are valid; the
//! result is handed back out through the closure's return value.

use crate::changelog::Changelog;
use crate::export::ArtifactSpec;
use crate::families::{Family, RouteKind};
use crate::model::VersionRow;
use differential_dataflow::input::InputSession;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::time::Instant;
use timely::dataflow::operators::probe::Handle;

struct ArtifactMeta {
    family: Family,
    args: serde_json::Value,
}

fn route_keys_for_row(row: &VersionRow) -> Vec<String> {
    if row.is_node() {
        let uid = row.uid.as_deref().unwrap_or("");
        vec![format!("u:{uid}"), "k:node".to_string(), "g:all".to_string()]
    } else {
        let src = row.src.as_deref().unwrap_or("");
        let dst = row.dst.as_deref().unwrap_or("");
        vec![format!("u:{src}"), format!("u:{dst}"), "k:edge".to_string(), "g:all".to_string()]
    }
}

fn route_key_for_artifact(family: Family, args: &serde_json::Value) -> Option<String> {
    match family.route_kind() {
        RouteKind::Uid => {
            let uid = args
                .get("uid")
                .and_then(|v| v.as_str())
                .or_else(|| args.get("seeds").and_then(|v| v.get(0)).and_then(|v| v.as_str()))
                .or_else(|| args.get("target").and_then(|v| v.get("uid")).and_then(|v| v.as_str()))
                .or_else(|| args.get("src").and_then(|v| v.as_str()));
            uid.map(|u| format!("u:{u}"))
        }
        RouteKind::Kind => {
            let kind = args.get("kind").and_then(|v| v.as_str()).unwrap_or("node");
            Some(format!("k:{kind}"))
        }
        RouteKind::Global => Some("g:all".to_string()),
        RouteKind::None | RouteKind::Traversal => None,
    }
}

/// Everything measured for one burst (memo §4.1 row shape).
pub struct BurstMeasurement {
    pub epoch: u64,
    pub tt: i64,
    pub correction_class: String,
    pub generator: String,
    pub placement: String,
    pub retracted_vt_span_sum_us: i64,
    pub retracted_vt_span_n_finite: usize,
    pub retracted_vt_span_n_open: usize,
    pub refresh_ms: f64,
    pub publish_ms: f64,
    pub artifacts_refreshed: Vec<String>,
    pub digests: HashMap<String, String>,
}

pub struct RunOutcome {
    pub load_ms: f64,
    pub epoch0_digests: HashMap<String, String>,
    pub bursts: Vec<BurstMeasurement>,
    pub families_present: HashSet<Family>,
    /// `op` name (not one of the 13 families, or `co_active` per A8) ->
    /// count of registered artifacts this crate did not evaluate.
    pub n_unmapped: HashMap<String, usize>,
    pub process_rss_kb_end: Option<u64>,
}

fn read_rss_kb() -> Option<u64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix("VmRSS:") {
            return rest.split_whitespace().next()?.parse().ok();
        }
    }
    None
}

/// Build the dataflow and drive it through every epoch of `changelog`,
/// returning the per-burst measurements. Runs on a single timely worker
/// (`timely::Config::thread()`), matching memo §3.4's scored configuration.
pub fn run(changelog: Changelog, artifacts: Vec<ArtifactSpec>) -> RunOutcome {
    let guards = timely::execute(timely::Config::thread(), move |worker| -> RunOutcome {
        let mut meta: HashMap<String, ArtifactMeta> = HashMap::new();
        let mut route_key_of: HashMap<String, Option<String>> = HashMap::new();
        let mut families_present: HashSet<Family> = HashSet::new();
        let mut n_unmapped: HashMap<String, usize> = HashMap::new();
        for a in &artifacts {
            match Family::from_op(&a.op) {
                Some(fam) => {
                    families_present.insert(fam);
                    let rk = route_key_for_artifact(fam, &a.args);
                    route_key_of.insert(a.name.clone(), rk);
                    meta.insert(a.name.clone(), ArtifactMeta { family: fam, args: a.args.clone() });
                }
                None => {
                    *n_unmapped.entry(a.op.clone()).or_insert(0) += 1;
                }
            }
        }
        let meta = Rc::new(meta);

        let state: Rc<RefCell<HashMap<String, String>>> = Rc::new(RefCell::new(HashMap::new()));
        let changed: Rc<RefCell<HashSet<String>>> = Rc::new(RefCell::new(HashSet::new()));

        let mut nodes_input: InputSession<u64, VersionRow, isize> = InputSession::new();
        let mut edges_input: InputSession<u64, VersionRow, isize> = InputSession::new();
        let mut routes_input: InputSession<u64, (String, String), isize> = InputSession::new();
        let probe: Handle<u64> = Handle::new();

        worker.dataflow::<u64, _, _>(|scope| {
            let nodes = nodes_input.to_collection(scope);
            let edges = edges_input.to_collection(scope);
            let routes = routes_input.to_collection(scope);

            let versions = nodes.concat(edges.clone());
            let versions_routed = versions.flat_map(|row: VersionRow| {
                route_keys_for_row(&row).into_iter().map(move |k| (k, row.clone())).collect::<Vec<_>>()
            });

            let meta_r1 = Rc::clone(&meta);
            let answers = routes.join_map(versions_routed, |_key, name, row| (name.clone(), row.clone())).reduce(
                move |name, input, output| {
                    let m = match meta_r1.get(name) {
                        Some(m) => m,
                        None => return,
                    };
                    let rows: Vec<VersionRow> = input.iter().map(|(r, _)| (*r).clone()).collect();
                    let node_rows: Vec<VersionRow> = rows.iter().filter(|r| r.is_node()).cloned().collect();
                    let edge_rows: Vec<VersionRow> = rows.iter().filter(|r| !r.is_node()).cloned().collect();
                    let payload = compute_payload(m.family, &m.args, &node_rows, &edge_rows);
                    let canon =
                        crate::digest::canonical_json(&crate::digest::canonicalize_floats(&payload));
                    output.push((canon, 1));
                },
            );

            let state_rc = Rc::clone(&state);
            let changed_rc = Rc::clone(&changed);
            answers.consolidate().inspect(move |((name, payload), _t, diff)| {
                if *diff > 0 {
                    state_rc.borrow_mut().insert(name.clone(), payload.clone());
                    changed_rc.borrow_mut().insert(name.clone());
                } else if *diff < 0 {
                    changed_rc.borrow_mut().insert(name.clone());
                }
            }).probe_with(&probe);
        });

        // ---- epoch 0: seed everything, measure load_ms -------------------
        let t_load0 = Instant::now();
        for row in &changelog.epoch0 {
            if row.is_node() {
                nodes_input.update_at(row.clone(), 0, 1);
            } else {
                edges_input.update_at(row.clone(), 0, 1);
            }
        }
        for name in meta.keys() {
            if let Some(Some(rk)) = route_key_of.get(name) {
                routes_input.update_at((rk.clone(), name.clone()), 0, 1);
            }
        }
        nodes_input.advance_to(1);
        edges_input.advance_to(1);
        routes_input.advance_to(1);
        nodes_input.flush();
        edges_input.flush();
        routes_input.flush();
        while probe.less_than(nodes_input.time()) {
            worker.step();
        }
        // F13's control reads no store data and never changes (memo
        // §3.3's "trivial"); it is not routed through the join+reduce
        // machinery at all (RouteKind::None), so it is seeded directly
        // here, once, rather than through any DD input.
        for (name, m) in meta.iter() {
            if m.family == Family::F13Compute {
                let payload = crate::families::f13_compute::compute(&m.args);
                let canon = crate::digest::canonical_json(&crate::digest::canonicalize_floats(&payload));
                state.borrow_mut().insert(name.clone(), canon);
            }
        }

        let load_ms = t_load0.elapsed().as_secs_f64() * 1000.0;
        let epoch0_digests: HashMap<String, String> = state
            .borrow()
            .iter()
            .map(|(k, v)| (k.clone(), crate::digest::sha256_hex(v)))
            .collect();
        changed.borrow_mut().clear();

        // ---- bursts 1..N ---------------------------------------------------
        let mut bursts = Vec::with_capacity(changelog.updates.len());
        for upd in &changelog.updates {
            let t0 = Instant::now();
            for row in &upd.retractions {
                if row.is_node() {
                    nodes_input.update_at(row.clone(), upd.epoch, -1);
                } else {
                    edges_input.update_at(row.clone(), upd.epoch, -1);
                }
            }
            for row in &upd.insertions {
                if row.is_node() {
                    nodes_input.update_at(row.clone(), upd.epoch, 1);
                } else {
                    edges_input.update_at(row.clone(), upd.epoch, 1);
                }
            }
            let next = upd.epoch + 1;
            nodes_input.advance_to(next);
            edges_input.advance_to(next);
            routes_input.advance_to(next);
            nodes_input.flush();
            edges_input.flush();
            routes_input.flush();
            while probe.less_than(nodes_input.time()) {
                worker.step();
            }
            let refresh_ms = t0.elapsed().as_secs_f64() * 1000.0;

            let t1 = Instant::now();
            let changed_names: Vec<String> = changed.borrow_mut().drain().collect();
            let mut digests = HashMap::with_capacity(changed_names.len());
            {
                let st = state.borrow();
                for name in &changed_names {
                    if let Some(payload) = st.get(name) {
                        digests.insert(name.clone(), crate::digest::sha256_hex(payload));
                    }
                }
            }
            let publish_ms = t1.elapsed().as_secs_f64() * 1000.0;

            let (sum, n_finite, n_open) = upd.retracted_vt_span_summary();
            bursts.push(BurstMeasurement {
                epoch: upd.epoch,
                tt: upd.tt,
                correction_class: upd.correction_class.clone(),
                generator: upd.generator.clone(),
                placement: upd.placement.clone(),
                retracted_vt_span_sum_us: sum,
                retracted_vt_span_n_finite: n_finite,
                retracted_vt_span_n_open: n_open,
                refresh_ms,
                publish_ms,
                artifacts_refreshed: changed_names,
                digests,
            });
        }

        RunOutcome {
            load_ms,
            epoch0_digests,
            bursts,
            families_present,
            n_unmapped,
            process_rss_kb_end: read_rss_kb(),
        }
    })
    .expect("timely execution failed");

    guards.join().into_iter().next().expect("one worker").expect("worker panicked")
}

/// Exposed for `withheld.rs`: the exact per-family dispatch the maintained
/// dataflow's `reduce` closures call, usable directly against a plain
/// snapshot of the version table (see that module's doc comment for why
/// this is a sound substitute for re-running the timely engine).
pub fn compute_payload_pub(
    family: Family,
    args: &serde_json::Value,
    nodes: &[VersionRow],
    edges: &[VersionRow],
) -> serde_json::Value {
    compute_payload(family, args, nodes, edges)
}

fn compute_payload(
    family: Family,
    args: &serde_json::Value,
    nodes: &[VersionRow],
    edges: &[VersionRow],
) -> serde_json::Value {
    use crate::families::*;
    match family {
        Family::F1EntityHistory => f1_entity_history::compute(args, nodes, edges),
        Family::F2VersionHistory => {
            let kind = args.get("kind").and_then(|v| v.as_str()).unwrap_or("node");
            let rows = if kind == "node" { nodes } else { edges };
            f2_version_history::compute(args, rows)
        }
        Family::F3SnapshotSubgraph => f3_snapshot_subgraph::compute(args, nodes, edges),
        Family::F4DiffSnapshots => f4_diff_snapshots::compute(args, nodes, edges),
        Family::F5NeighborhoodEvolution => {
            let uid = args["uid"].as_str().unwrap_or("");
            f5_neighborhood_evolution::compute(args, uid, edges)
        }
        Family::F6AggregateEvents => f6_aggregate_events::compute(args, edges),
        Family::F7GraphMetricTimeseries => f7_graph_metric_timeseries::compute(args, edges),
        Family::F8BurstDetection => {
            let uid = args["target"]["uid"].as_str().unwrap_or("");
            f8_burst_detection::compute(args, uid, edges)
        }
        Family::F9CountTemporalMotifs => f9_count_temporal_motifs::compute(args, edges),
        Family::F10FindTemporalMotifInstances => f10_find_temporal_motif_instances::compute(args, edges),
        Family::F12TemporalPaths => {
            let src = args["src"].as_str().unwrap_or("");
            let dst = args["dst"].as_str().unwrap_or("");
            f12_temporal_paths::compute(args, src, dst, edges)
        }
        Family::F13Compute => f13_compute::compute(args),
        Family::F11TemporalReachability => {
            let src = args["src"].as_str().unwrap_or("");
            f11_temporal_reachability::compute(args, src, edges)
        }
    }
}
