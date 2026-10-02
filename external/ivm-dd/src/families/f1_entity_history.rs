//! F1 `entity_history` (`ops_snapshot.entity_history`). Takes the uid's own
//! node versions and its incident edges (params keyed by uid ⋈
//! `nodes_by_uid`, ⋈ `edges_by_src` ∪ `edges_by_dst`, self-loops once);
//! orders by (vt_s, vid), first `limit`, totals.

use crate::families::{args_as_of_tt, args_cursor, args_limit, paginate};
use crate::model::VersionRow;
use serde_json::{json, Value};

pub fn compute(args: &Value, nodes: &[VersionRow], edges: &[VersionRow]) -> Value {
    let as_of = args_as_of_tt(args);
    let include_edges = args.get("include_edges").and_then(|v| v.as_bool()).unwrap_or(false);
    let limit = args_limit(args);
    let cursor = args_cursor(args);

    let mut believed: Vec<&VersionRow> = nodes.iter().filter(|v| v.believed_at(as_of)).collect();
    believed.sort_by(|a, b| (a.vt_s, &a.vid).cmp(&(b.vt_s, &b.vid)));
    let rows: Vec<Value> = believed
        .iter()
        .map(|v| {
            json!({
                "vid": v.vid, "uid": v.uid, "label": v.label,
                "vt_s": v.vt_s, "vt_e": v.vt_e, "tt_s": v.tt_s,
                "tt_e": v.reported_tt_e(as_of),
                "props": v.props, "source": v.source, "provenance_ref": v.provenance_ref,
            })
        })
        .collect();
    let mut out = paginate(&rows, limit, cursor.as_deref());

    if include_edges {
        let mut incident: Vec<&VersionRow> = edges.iter().filter(|e| e.believed_at(as_of)).collect();
        incident.sort_by(|a, b| (a.vt_s, &a.vid).cmp(&(b.vt_s, &b.vid)));
        let n_incident = incident.len();
        let take = n_incident.min(limit);
        let edge_rows: Vec<Value> = incident[..take]
            .iter()
            .map(|e| {
                json!({
                    "eid": e.eid, "vid": e.vid, "src": e.src, "dst": e.dst,
                    "rel_type": e.rel_type, "vt_s": e.vt_s, "vt_e": e.vt_e,
                })
            })
            .collect();
        out["edges"] = Value::Array(edge_rows);
        out["edges_truncated"] = Value::Bool(n_incident > limit);
    }
    out
}
