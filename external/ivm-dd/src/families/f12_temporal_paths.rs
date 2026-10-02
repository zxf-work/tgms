//! F12 `temporal_paths`, `k=2`, `max_hops=4` in every grid registration
//! (`ops_paths.temporal_paths`; memo table "four unrolled steps"). Node-
//! simple, time-respecting DFS from `src`, terminating at `dst`; all
//! `max_hops`-bounded paths are found before the global `(arrival, hops,
//! key)` sort and the top-`k` cut -- a path is not discarded early just
//! because a shorter one to `dst` was already found, matching
//! `ops_paths.py`'s own `dfs` (it only stops expanding a *branch* once it
//! reaches `dst` or `max_hops`).
//!
//! `dataflow.rs` wires this family's own four static hop-joins against the
//! shared `edges_by_src` arrangement (DD's structural equivalent of this
//! recursion, unrolled to the fixed `max_hops=4`); this module's
//! `dfs_paths` is the plain-Rust reference `tests/` pins the expected
//! answer against.

use crate::model::VersionRow;
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

pub fn compute(args: &Value, src: &str, dst: &str, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let k = args.get("k").and_then(|v| v.as_u64()).unwrap_or(5) as usize;
    let max_hops = args.get("max_hops").and_then(|v| v.as_u64()).unwrap_or(4) as usize;
    let as_of = crate::families::args_as_of_tt(args);

    let mut paths = dfs_paths(edges, src, dst, t_a, t_b, max_hops, as_of);
    paths.sort_by(|a, b| (a.arrival, a.hops, &a.key).cmp(&(b.arrival, b.hops, &b.key)));
    let rows_total = paths.len();
    let rows: Vec<Value> = paths
        .into_iter()
        .take(k)
        .map(|p| {
            let edge_rows: Vec<Value> = p
                .trail
                .iter()
                .map(|&i| {
                    let e = &edges[i];
                    json!({"src": e.src, "dst": e.dst, "rel_type": e.rel_type,
                           "eid": e.eid, "t": e.vt_s})
                })
                .collect();
            json!({"arrival": p.arrival, "hops": p.hops, "edges": edge_rows})
        })
        .collect();
    json!({"rows": rows, "rows_total": rows_total, "truncated": rows_total > k, "cursor": null})
}
