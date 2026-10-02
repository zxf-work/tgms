//! F4 `diff_snapshots`, `scope: null` (whole graph) in every grid
//! registration (`ops_snapshot.diff_snapshots`; memo table "whole graph").
//! The scoped (`seeds`/`hops`-restricted) branch is not implemented: no
//! registered instance in the grid uses it, and adding it later needs no
//! change to this family's route (still Global).

use crate::model::VersionRow;
use serde_json::{json, Value};
use std::collections::BTreeMap;

struct PointState<'a> {
    nodes: BTreeMap<&'a str, &'a VersionRow>, // uid -> row
    edges: BTreeMap<&'a str, &'a VersionRow>, // eid -> row
}

fn point_state<'a>(nodes: &'a [VersionRow], edges: &'a [VersionRow], t: i64, as_of: i64) -> PointState<'a> {
    PointState {
        nodes: nodes
            .iter()
            .filter(|v| v.valid_at(t) && v.believed_at(as_of))
            .map(|v| (v.uid.as_deref().unwrap_or(""), v))
            .collect(),
        edges: edges
            .iter()
            .filter(|e| e.valid_at(t) && e.believed_at(as_of))
            .map(|e| (e.eid.as_deref().unwrap_or(""), e))
            .collect(),
    }
}

fn edge_desc(row: &VersionRow) -> Value {
    json!({"eid": row.eid, "src": row.src, "dst": row.dst, "rel_type": row.rel_type})
}

pub fn compute(args: &Value, nodes: &[VersionRow], edges: &[VersionRow]) -> Value {
    let t1 = args["t1"].as_i64().unwrap_or(0);
    let t2 = args["t2"].as_i64().unwrap_or(0);
    let as_of = crate::families::args_as_of_tt(args);
    let limit = crate::families::args_limit(args);

    let s1 = point_state(nodes, edges, t1, as_of);
    let s2 = point_state(nodes, edges, t2, as_of);

    let mut nodes_added: Vec<&str> = s2.nodes.keys().filter(|u| !s1.nodes.contains_key(*u)).copied().collect();
    nodes_added.sort();
    let mut nodes_removed: Vec<&str> = s1.nodes.keys().filter(|u| !s2.nodes.contains_key(*u)).copied().collect();
    nodes_removed.sort();
    let mut edges_added: Vec<&str> = s2.edges.keys().filter(|e| !s1.edges.contains_key(*e)).copied().collect();
    edges_added.sort();
    let mut edges_removed: Vec<&str> = s1.edges.keys().filter(|e| !s2.edges.contains_key(*e)).copied().collect();
    edges_removed.sort();

    let mut changed: Vec<Value> = Vec::new();
    let mut node_pairs: Vec<&str> = s1.nodes.keys().filter(|u| s2.nodes.contains_key(*u)).copied().collect();
    node_pairs.sort();
    for u in node_pairs {
        let (a, b) = (s1.nodes[u], s2.nodes[u]);
        if a.vid != b.vid && (a.props != b.props || a.label != b.label) {
            changed.push(json!({
                "kind": "node", "id": u,
                "from": {"label": a.label, "props": a.props},
                "to": {"label": b.label, "props": b.props},
            }));
        }
    }
    let mut edge_pairs: Vec<&str> = s1.edges.keys().filter(|e| s2.edges.contains_key(*e)).copied().collect();
    edge_pairs.sort();
    for e in edge_pairs {
        let (a, b) = (s1.edges[e], s2.edges[e]);
        if a.vid != b.vid && a.props != b.props {
            changed.push(json!({
                "kind": "edge", "id": e,
                "from": {"props": a.props},
                "to": {"props": b.props},
            }));
        }
    }

    let cap = |n: usize| n > limit;
    let out = json!({
        "nodes_added": nodes_added.iter().take(limit).collect::<Vec<_>>(), "nodes_added_total": nodes_added.len(),
        "nodes_removed": nodes_removed.iter().take(limit).collect::<Vec<_>>(), "nodes_removed_total": nodes_removed.len(),
        "edges_added": edges_added.iter().take(limit).map(|e| edge_desc(s2.edges[e])).collect::<Vec<_>>(),
        "edges_added_total": edges_added.len(),
        "edges_removed": edges_removed.iter().take(limit).map(|e| edge_desc(s1.edges[e])).collect::<Vec<_>>(),
        "edges_removed_total": edges_removed.len(),
        "props_changed": changed.iter().take(limit).collect::<Vec<_>>(), "props_changed_total": changed.len(),
        "truncated": cap(nodes_added.len()) || cap(nodes_removed.len()) || cap(edges_added.len())
            || cap(edges_removed.len()) || cap(changed.len()),
    });
    out
}
