//! F4 `diff_snapshots`, `scope: null` (whole graph) in every grid
//! registration (`ops_snapshot.diff_snapshots`).
//! The scoped (`seeds`/`hops`-restricted) branch is not implemented: no
//! registered instance in the grid uses it.
//!
//! `compute` is the whole-store reference. The maintained dataflow
//! (`views.rs`) classifies each (artifact, entity) separately with
//! `classify` -- from the entity's versions valid at `t1` and at `t2` --
//! and keeps only the entities that differ; `payload_from_items` assembles
//! the five sorted lists from those items. `compute` goes through the same
//! two functions, so the reference and the maintained path share their
//! formatting.

use crate::model::VersionRow;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// One entity that differs between the two instants. The derived `Ord`
/// (variant, then id) is exactly the order of the payload's lists:
/// nodes added / removed by uid, edges added / removed by eid, then the
/// property changes, nodes (by uid) before edges (by eid).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum DiffItem {
    NodeAdded(String),
    NodeRemoved(String),
    EdgeAdded(String, VersionRow),
    EdgeRemoved(String, VersionRow),
    NodeChanged(String, VersionRow, VersionRow),
    EdgeChanged(String, VersionRow, VersionRow),
}

/// The entity's state at `t1` (`s1`) and `t2` (`s2`): its version valid at
/// that instant and believed, if any. Returns the item when they differ.
pub fn classify(is_node: bool, id: &str, s1: Option<&VersionRow>, s2: Option<&VersionRow>) -> Option<DiffItem> {
    match (s1, s2) {
        (None, None) => None,
        (None, Some(b)) => Some(if is_node {
            DiffItem::NodeAdded(id.to_string())
        } else {
            DiffItem::EdgeAdded(id.to_string(), b.clone())
        }),
        (Some(a), None) => Some(if is_node {
            DiffItem::NodeRemoved(id.to_string())
        } else {
            DiffItem::EdgeRemoved(id.to_string(), a.clone())
        }),
        (Some(a), Some(b)) => {
            if is_node {
                (a.vid != b.vid && (a.props != b.props || a.label != b.label))
                    .then(|| DiffItem::NodeChanged(id.to_string(), a.clone(), b.clone()))
            } else {
                (a.vid != b.vid && a.props != b.props)
                    .then(|| DiffItem::EdgeChanged(id.to_string(), a.clone(), b.clone()))
            }
        }
    }
}

fn edge_desc(row: &VersionRow) -> Value {
    json!({"eid": row.eid, "src": row.src, "dst": row.dst, "rel_type": row.rel_type})
}

/// Assemble the payload from the differing entities, which must be sorted
/// (`DiffItem`'s `Ord`).
pub fn payload_from_items(args: &Value, items: &[&DiffItem]) -> Value {
    let limit = crate::families::args_limit(args);
    let mut nodes_added = Vec::new();
    let mut nodes_removed = Vec::new();
    let mut edges_added = Vec::new();
    let mut edges_removed = Vec::new();
    let mut changed = Vec::new();
    for it in items {
        match it {
            DiffItem::NodeAdded(u) => nodes_added.push(json!(u)),
            DiffItem::NodeRemoved(u) => nodes_removed.push(json!(u)),
            DiffItem::EdgeAdded(_, r) => edges_added.push(edge_desc(r)),
            DiffItem::EdgeRemoved(_, r) => edges_removed.push(edge_desc(r)),
            DiffItem::NodeChanged(u, a, b) => changed.push(json!({
                "kind": "node", "id": u,
                "from": {"label": a.label, "props": a.props},
                "to": {"label": b.label, "props": b.props},
            })),
            DiffItem::EdgeChanged(e, a, b) => changed.push(json!({
                "kind": "edge", "id": e,
                "from": {"props": a.props},
                "to": {"props": b.props},
            })),
        }
    }
    let cap = |n: usize| n > limit;
    let head = |v: &Vec<Value>| v.iter().take(limit).cloned().collect::<Vec<_>>();
    json!({
        "nodes_added": head(&nodes_added), "nodes_added_total": nodes_added.len(),
        "nodes_removed": head(&nodes_removed), "nodes_removed_total": nodes_removed.len(),
        "edges_added": head(&edges_added), "edges_added_total": edges_added.len(),
        "edges_removed": head(&edges_removed), "edges_removed_total": edges_removed.len(),
        "props_changed": head(&changed), "props_changed_total": changed.len(),
        "truncated": cap(nodes_added.len()) || cap(nodes_removed.len()) || cap(edges_added.len())
            || cap(edges_removed.len()) || cap(changed.len()),
    })
}

/// Per-instant state, keyed by uid (nodes) / eid (edges); when two
/// versions of one id are valid and believed at the same instant, the last
/// in the input order wins (the maintained path feeds rows in `VersionRow`
/// order, which is what this reference sees too when called on a
/// reduce's input).
fn point_state(rows: &[VersionRow], t: i64, as_of: i64, node: bool) -> BTreeMap<&str, &VersionRow> {
    rows.iter()
        .filter(|v| v.valid_at(t) && v.believed_at(as_of))
        .map(|v| {
            let id = if node { v.uid.as_deref() } else { v.eid.as_deref() };
            (id.unwrap_or(""), v)
        })
        .collect()
}

pub fn compute(args: &Value, nodes: &[VersionRow], edges: &[VersionRow]) -> Value {
    let t1 = args["t1"].as_i64().unwrap_or(0);
    let t2 = args["t2"].as_i64().unwrap_or(0);
    let as_of = crate::families::args_as_of_tt(args);

    let mut items: Vec<DiffItem> = Vec::new();
    for (rows, node) in [(nodes, true), (edges, false)] {
        let s1 = point_state(rows, t1, as_of, node);
        let s2 = point_state(rows, t2, as_of, node);
        let mut ids: Vec<&str> = s1.keys().chain(s2.keys()).copied().collect();
        ids.sort();
        ids.dedup();
        for id in ids {
            if let Some(it) = classify(node, id, s1.get(id).copied(), s2.get(id).copied()) {
                items.push(it);
            }
        }
    }
    items.sort();
    let refs: Vec<&DiffItem> = items.iter().collect();
    payload_from_items(args, &refs)
}
