//! The maintained configuration: one timely worker (the scored
//! configuration), `nodes` / `edges` changelog inputs, a static `params`
//! input (one record per registered artifact, inserted at epoch 0), and
//! one maintained differential dataflow per artifact family
//! (`views::build`; the family -> operator table and the valid-time
//! routing are documented there and in the README).
//!
//! Per burst k: push burst k's retractions/insertions at time k, advance
//! every input to k + 1, flush, and step the worker until the probe has
//! passed k ("fresh": every family's output reflects burst k). Output
//! changes are captured by `inspect` during that region; `refresh_ms` is
//! push + maintenance, `publish_ms` is serializing and hashing the changed
//! artifacts' payloads afterwards.
//!
//! Everything -- dataflow construction *and* the epoch-by-epoch driving
//! loop -- runs inside the single `timely::execute` worker closure, since
//! that is the only place the probe and input handles are valid; the
//! result is handed back out through the closure's return value.

use crate::changelog::Changelog;
use crate::export::ArtifactSpec;
use crate::families::Family;
use crate::model::VersionRow;
use crate::views::{Art, Buckets, Params};
use differential_dataflow::input::InputSession;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::time::Instant;
use timely::dataflow::operators::probe::Handle;

/// Everything measured for one burst (one result row per cell x epoch).
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
    /// The valid-time extent `[lo, hi)` the 256 routing buckets divide.
    pub valid_time_extent: (i64, i64),
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
/// (`timely::Config::thread()`), the scored configuration.
pub fn run(changelog: Changelog, artifacts: Vec<ArtifactSpec>) -> RunOutcome {
    let guards = timely::execute(timely::Config::thread(), move |worker| -> RunOutcome {
        let mut arts: Vec<Art> = Vec::new();
        let mut families_present: HashSet<Family> = HashSet::new();
        let mut n_unmapped: HashMap<String, usize> = HashMap::new();
        for a in &artifacts {
            match Family::from_op(&a.op) {
                Some(fam) => {
                    families_present.insert(fam);
                    arts.push(Art { name: a.name.clone(), family: fam, args: a.args.clone(), p: Params::parse(fam, &a.args) });
                }
                None => {
                    *n_unmapped.entry(a.op.clone()).or_insert(0) += 1;
                }
            }
        }
        let arts = Rc::new(arts);
        let bk = Buckets::from_rows(&changelog.epoch0);

        let state: Rc<RefCell<HashMap<String, String>>> = Rc::new(RefCell::new(HashMap::new()));
        let changed: Rc<RefCell<HashSet<String>>> = Rc::new(RefCell::new(HashSet::new()));

        let mut nodes_input: InputSession<u64, VersionRow, isize> = InputSession::new();
        let mut edges_input: InputSession<u64, VersionRow, isize> = InputSession::new();
        let mut params_input: InputSession<u64, u32, isize> = InputSession::new();
        let probe: Handle<u64> = Handle::new();

        worker.dataflow::<u64, _, _>(|scope| {
            let nodes = nodes_input.to_collection(scope);
            let edges = edges_input.to_collection(scope);
            let params = params_input.to_collection(scope);
            let answers = crate::views::build(nodes, edges, params, Rc::clone(&arts), bk);

            let (state_rc, changed_rc, names) = (Rc::clone(&state), Rc::clone(&changed), Rc::clone(&arts));
            answers
                .consolidate()
                .inspect(move |((i, payload), _t, diff)| {
                    let name = &names[*i as usize].name;
                    if *diff > 0 {
                        state_rc.borrow_mut().insert(name.clone(), payload.clone());
                        changed_rc.borrow_mut().insert(name.clone());
                    } else if *diff < 0 {
                        changed_rc.borrow_mut().insert(name.clone());
                    }
                })
                .probe_with(&probe);
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
        for (i, a) in arts.iter().enumerate() {
            if a.family != Family::F13Compute {
                params_input.update_at(i as u32, 0, 1);
            }
        }
        nodes_input.advance_to(1);
        edges_input.advance_to(1);
        params_input.advance_to(1);
        nodes_input.flush();
        edges_input.flush();
        params_input.flush();
        while probe.less_than(nodes_input.time()) {
            worker.step();
        }
        // F13's control reads no store data and never changes (a constant
        // collection); it is seeded directly here, once, rather than
        // through any input.
        for a in arts.iter() {
            if a.family == Family::F13Compute {
                let payload = crate::families::f13_compute::compute(&a.args);
                let canon = crate::digest::canonical_json(&crate::digest::canonicalize_floats(&payload));
                state.borrow_mut().insert(a.name.clone(), canon);
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
            params_input.advance_to(next);
            nodes_input.flush();
            edges_input.flush();
            params_input.flush();
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
            valid_time_extent: (bk.lo, bk.hi),
        }
    })
    .expect("timely execution failed");

    guards.join().into_iter().next().expect("one worker").expect("worker panicked")
}

/// Exposed for `withheld.rs`: the per-family reference payload over a
/// plain snapshot of the version table (each family's `compute`, which
/// `tests/maintained_tests.rs` pins equal to the maintained dataflow's
/// output under insertions and retractions; see `withheld.rs` for why this
/// is a sound substitute there).
pub fn compute_payload_pub(
    family: Family,
    args: &serde_json::Value,
    nodes: &[VersionRow],
    edges: &[VersionRow],
) -> serde_json::Value {
    compute_payload(family, args, nodes, edges)
}

fn incident<'a>(edges: &'a [VersionRow], uid: &'a str) -> impl Iterator<Item = &'a VersionRow> + 'a {
    edges.iter().filter(move |e| e.src.as_deref() == Some(uid) || e.dst.as_deref() == Some(uid))
}

fn compute_payload(
    family: Family,
    args: &serde_json::Value,
    nodes: &[VersionRow],
    edges: &[VersionRow],
) -> serde_json::Value {
    use crate::families::*;
    match family {
        // F1 and F5's `compute` read exactly the rows the uid join hands
        // them (the uid's own versions, its incident edges); over a whole
        // snapshot the scoping is applied here.
        Family::F1EntityHistory => {
            let uid = args["uid"].as_str().unwrap_or("");
            let own: Vec<VersionRow> = nodes.iter().filter(|n| n.uid.as_deref() == Some(uid)).cloned().collect();
            let inc: Vec<VersionRow> = incident(edges, uid).cloned().collect();
            f1_entity_history::compute(args, &own, &inc)
        }
        Family::F2VersionHistory => {
            let kind = args.get("kind").and_then(|v| v.as_str()).unwrap_or("node");
            let rows = if kind == "node" { nodes } else { edges };
            f2_version_history::compute(args, rows)
        }
        Family::F3SnapshotSubgraph => f3_snapshot_subgraph::compute(args, nodes, edges),
        Family::F4DiffSnapshots => f4_diff_snapshots::compute(args, nodes, edges),
        Family::F5NeighborhoodEvolution => {
            let uid = args["uid"].as_str().unwrap_or("");
            let inc: Vec<VersionRow> = incident(edges, uid).cloned().collect();
            f5_neighborhood_evolution::compute(args, uid, &inc)
        }
        Family::F6AggregateEvents => f6_aggregate_events::compute(args, edges),
        Family::F7GraphMetricTimeseries => f7_graph_metric_timeseries::compute(args, edges),
        Family::F8BurstDetection => {
            let uid = args["target"]["uid"].as_str().unwrap_or("");
            f8_burst_detection::compute(args, uid, edges)
        }
        Family::F9CountTemporalMotifs => f9_count_temporal_motifs::compute(args, edges),
        Family::F10FindTemporalMotifInstances => f10_find_temporal_motif_instances::compute(args, edges),
        Family::F11TemporalReachability => {
            let src = args["src"].as_str().unwrap_or("");
            f11_temporal_reachability::compute(args, src, edges)
        }
        Family::F12TemporalPaths => {
            let src = args["src"].as_str().unwrap_or("");
            let dst = args["dst"].as_str().unwrap_or("");
            f12_temporal_paths::compute(args, src, dst, edges)
        }
        Family::F13Compute => f13_compute::compute(args),
    }
}
