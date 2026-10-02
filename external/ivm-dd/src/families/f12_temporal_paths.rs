//! F12 `temporal_paths`, `k=2`, `max_hops=4` in every grid registration
//! (`ops_paths.temporal_paths`). Node-
//! simple, time-respecting DFS from `src`, terminating at `dst`; all
//! `max_hops`-bounded paths are found before the global `(arrival, hops,
//! key)` sort and the top-`k` cut -- a path is not discarded early just
//! because a shorter one to `dst` was already found, matching
//! `ops_paths.py`'s own `dfs` (it only stops expanding a *branch* once it
//! reaches `dst` or `max_hops`).
//!
//! `dfs_paths` is the plain-Rust reference. The maintained dataflow
//! (`views.rs`) enumerates the same paths with one join per hop against
//! the shared `edges_by_src` arrangement (unrolled to the largest
//! registered `max_hops`, 4 in every grid registration), and formats them
//! with `payload_from_paths`. One difference is deliberate: the reference
//! (like `ops_paths.py`) gives up after 2,000,000 expansions -- TGMS then
//! refuses the call (`CostError`) and the oracle records the artifact as
//! refused, so it is not compared -- while the dataflow has no expansion
//! budget and always enumerates every path.

use crate::model::VersionRow;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;

const MAX_EXPANSIONS: u64 = 2_000_000;

pub struct Found {
    pub arrival: i64,
    pub hops: usize,
    pub key: Vec<(i64, String)>, // (vt_s, eid) per edge, in order
    pub trail: Vec<usize>,       // indices into `edges`
}

pub fn dfs_paths(
    edges: &[VersionRow],
    src: &str,
    dst: &str,
    t_a: i64,
    t_b: i64,
    max_hops: usize,
    as_of: i64,
) -> Vec<Found> {
    // out-adjacency: src uid -> indices into `edges`, ordered by (vt_s, vid)
    // (the TCSR's own per-node slice order; `ops_paths.py` relies on it).
    // `believed_at(as_of)` matters here exactly as it does for the motif
    // families (tests/fixtures/tiny1 caught the same bug there first): a
    // superseded belief must not contribute a traversable edge just
    // because its `vt_s` falls in range.
    let mut by_src: HashMap<&str, Vec<usize>> = HashMap::new();
    let mut order: Vec<usize> = (0..edges.len()).filter(|&i| edges[i].believed_at(as_of)).collect();
    order.sort_by(|&a, &b| (edges[a].vt_s, &edges[a].vid).cmp(&(edges[b].vt_s, &edges[b].vid)));
    for &i in &order {
        by_src.entry(edges[i].src.as_deref().unwrap_or("")).or_default().push(i);
    }

    let mut found = Vec::new();
    let mut expansions = 0u64;
    let mut visited: std::collections::HashSet<&str> = std::collections::HashSet::new();
    visited.insert(src);
    let mut trail: Vec<usize> = Vec::new();

    #[allow(clippy::too_many_arguments)] // a plain-Rust reference DFS; splitting
    // the state into a struct would obscure the Python original it mirrors
    // (`ops_paths.py`'s own `dfs` closure) more than it would clarify this one.
    fn dfs<'a>(
        node: &'a str,
        arrival: i64,
        hops: usize,
        visited: &mut std::collections::HashSet<&'a str>,
        trail: &mut Vec<usize>,
        edges: &'a [VersionRow],
        by_src: &HashMap<&'a str, Vec<usize>>,
        dst: &str,
        t_b: i64,
        max_hops: usize,
        expansions: &mut u64,
        found: &mut Vec<Found>,
    ) {
        if node == dst && !trail.is_empty() {
            found.push(Found {
                arrival,
                hops,
                key: trail.iter().map(|&i| (edges[i].vt_s, edges[i].eid.clone().unwrap_or_default())).collect(),
                trail: trail.clone(),
            });
            return;
        }
        if hops == max_hops {
            return;
        }
        if let Some(nbrs) = by_src.get(node) {
            for &i in nbrs {
                *expansions += 1;
                if *expansions > MAX_EXPANSIONS {
                    return;
                }
                let e = &edges[i];
                let v = e.dst.as_deref().unwrap_or("");
                if visited.contains(v) {
                    continue;
                }
                let tau = arrival.max(e.vt_s);
                if tau >= e.vt_e || tau >= t_b {
                    continue;
                }
                visited.insert(v);
                trail.push(i);
                dfs(v, tau, hops + 1, visited, trail, edges, by_src, dst, t_b, max_hops, expansions, found);
                trail.pop();
                visited.remove(v);
            }
        }
    }

    dfs(src, t_a, 0, &mut visited, &mut trail, edges, &by_src, dst, t_b, max_hops, &mut expansions, &mut found);
    found
}

/// One edge of a path. Field order makes the derived `Ord` of a
/// `Vec<Step>` the reference's path key `((vt_s, eid), ...)`.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Step {
    pub vt_s: i64,
    pub eid: String,
    pub src: Option<String>,
    pub dst: Option<String>,
    pub rel_type: Option<String>,
}

impl Step {
    pub fn from_row(e: &VersionRow) -> Step {
        Step {
            vt_s: e.vt_s,
            eid: e.eid.clone().unwrap_or_default(),
            src: e.src.clone(),
            dst: e.dst.clone(),
            rel_type: e.rel_type.clone(),
        }
    }
}

/// One complete path; the derived `Ord` is the reference's sort key
/// `(arrival, hops, key)`.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct PathVal {
    pub arrival: i64,
    pub hops: usize,
    pub steps: Vec<Step>,
}

/// Payload from all complete paths, sorted ascending (`PathVal`'s `Ord`).
pub fn payload_from_paths(k: usize, paths: &[&PathVal]) -> Value {
    let rows_total = paths.len();
    let rows: Vec<Value> = paths
        .iter()
        .take(k)
        .map(|p| {
            let edge_rows: Vec<Value> = p
                .steps
                .iter()
                .map(|s| json!({"src": s.src, "dst": s.dst, "rel_type": s.rel_type, "eid": s.eid, "t": s.vt_s}))
                .collect();
            json!({"arrival": p.arrival, "hops": p.hops, "edges": edge_rows})
        })
        .collect();
    json!({"rows": rows, "rows_total": rows_total, "truncated": rows_total > k, "cursor": null})
}

pub fn args_k(args: &Value) -> usize {
    args.get("k").and_then(|v| v.as_u64()).unwrap_or(5) as usize
}

pub fn args_max_hops(args: &Value) -> usize {
    args.get("max_hops").and_then(|v| v.as_u64()).unwrap_or(4) as usize
}

pub fn compute(args: &Value, src: &str, dst: &str, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let k = args_k(args);
    let max_hops = args_max_hops(args);
    let as_of = crate::families::args_as_of_tt(args);

    let found = dfs_paths(edges, src, dst, t_a, t_b, max_hops, as_of);
    let mut paths: Vec<PathVal> = found
        .into_iter()
        .map(|p| PathVal {
            arrival: p.arrival,
            hops: p.hops,
            steps: p.trail.iter().map(|&i| Step::from_row(&edges[i])).collect(),
        })
        .collect();
    paths.sort();
    let refs: Vec<&PathVal> = paths.iter().collect();
    payload_from_paths(k, &refs)
}
