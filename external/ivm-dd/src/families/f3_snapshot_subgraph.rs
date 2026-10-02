//! F3 `snapshot_subgraph`, hops=1, single seed in every grid registration
//! (`ops_snapshot.snapshot_subgraph`). Routed Global (see
//! `families::mod`'s note on why -- needs a neighbour's own node row, not
//! just the edge to it).

use crate::model::VersionRow;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

pub fn compute(args: &Value, nodes: &[VersionRow], edges: &[VersionRow]) -> Value {
    let seeds: Vec<String> = args["seeds"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default();
    let hops = args.get("hops").and_then(|v| v.as_u64()).unwrap_or(1) as usize;
    let t = args["t_valid"].as_i64().unwrap_or(0);
    let as_of = crate::families::args_as_of_tt(args);
    let rel_types: Option<Vec<String>> = args
        .get("rel_types")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect());
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);

    let nodes_at_t: HashMap<&str, &VersionRow> = nodes
        .iter()
        .filter(|v| v.valid_at(t) && v.believed_at(as_of))
        .map(|v| (v.uid.as_deref().unwrap_or(""), v))
        .collect();
    let edges_at_t: Vec<&VersionRow> = edges
        .iter()
        .filter(|e| e.valid_at(t) && e.believed_at(as_of))
        .filter(|e| match &rel_types {
            None => true,
            Some(rt) => e.rel_type.as_deref().is_some_and(|r| rt.iter().any(|x| x == r)),
        })
        .collect();

    let mut dist: HashMap<String, usize> = HashMap::new();
    for s in &seeds {
        if nodes_at_t.contains_key(s.as_str()) {
            dist.insert(s.clone(), 0);
        }
    }
    let mut frontier: Vec<String> = dist.keys().cloned().collect();
    for h in 1..=hops {
        if frontier.is_empty() {
            break;
        }
        let fset: HashSet<&str> = frontier.iter().map(|s| s.as_str()).collect();
        let mut next = Vec::new();
        for e in &edges_at_t {
            let (src, dst) = (e.src.as_deref().unwrap_or(""), e.dst.as_deref().unwrap_or(""));
            for (a, b) in [(src, dst), (dst, src)] {
                if fset.contains(a) && !dist.contains_key(b) && nodes_at_t.contains_key(b) {
                    dist.insert(b.to_string(), h);
                    next.push(b.to_string());
                }
            }
        }
        next.sort();
        next.dedup();
        frontier = next;
    }

    let mut induced: Vec<&VersionRow> = edges_at_t
        .iter()
        .filter(|e| {
            dist.contains_key(e.src.as_deref().unwrap_or(""))
                && dist.contains_key(e.dst.as_deref().unwrap_or(""))
        })
        .copied()
        .collect();
    induced.sort_by(|a, b| (a.vt_s, &a.vid).cmp(&(b.vt_s, &b.vid)));
    let edge_rows: Vec<Value> = induced
        .iter()
        .map(|e| {
            json!({"eid": e.eid, "vid": e.vid, "src": e.src, "dst": e.dst,
                   "rel_type": e.rel_type, "vt_s": e.vt_s, "vt_e": e.vt_e})
        })
        .collect();

    let mut node_rows: Vec<(usize, String, Value)> = dist
        .iter()
        .filter_map(|(uid, &h)| {
            nodes_at_t.get(uid.as_str()).map(|v| {
                (h, uid.clone(), json!({"uid": uid, "label": v.label, "hop": h}))
            })
        })
        .collect();
    node_rows.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    let node_total = node_rows.len();
    let node_page: Vec<Value> = node_rows.into_iter().take(limit).map(|(_, _, v)| v).collect();
    let nodes_truncated = node_total > limit;

    let mut out = crate::families::paginate(&edge_rows, limit, cursor.as_deref());
    out["nodes"] = Value::Array(node_page);
    out["nodes_total"] = json!(node_total);
    out["nodes_truncated"] = json!(nodes_truncated);
    let base_truncated = out["truncated"].as_bool().unwrap_or(false);
    out["truncated"] = json!(base_truncated || nodes_truncated);
    out
}
