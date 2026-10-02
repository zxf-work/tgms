//! F5 `neighborhood_evolution` (`ops_snapshot.neighborhood_evolution`).
//! Routed by `uid`.

use crate::model::VersionRow;
use serde_json::{json, Value};
use std::collections::BTreeSet;

pub fn compute(args: &Value, uid: &str, edges: &[VersionRow]) -> Value {
    let t1 = args["t1"].as_i64().unwrap_or(0);
    let t2 = args["t2"].as_i64().unwrap_or(0);
    let as_of = crate::families::args_as_of_tt(args);
    let stride = args
        .get("stride")
        .and_then(|v| v.as_i64())
        .filter(|&s| s > 0)
        .unwrap_or_else(|| ((t2 - t1) / 20).max(1));
    let limit = crate::families::args_limit(args);

    let incident: Vec<&VersionRow> = edges.iter().filter(|e| e.believed_at(as_of)).collect();

    let neighbors_at = |t: i64| -> BTreeSet<String> {
        incident
            .iter()
            .filter(|e| e.valid_at(t))
            .filter_map(|e| {
                let (src, dst) = (e.src.as_deref().unwrap_or(""), e.dst.as_deref().unwrap_or(""));
                if src == uid {
                    Some(dst.to_string())
                } else if dst == uid {
                    Some(src.to_string())
                } else {
                    None
                }
            })
            .filter(|n| n != uid)
            .collect()
    };
    let n1 = neighbors_at(t1);
    let n2 = neighbors_at(t2);
    let gained: Vec<&String> = n2.difference(&n1).collect();
    let lost: Vec<&String> = n1.difference(&n2).collect();

    // Only edges whose valid interval overlaps [t1, t2) feed the degree
    // series (`ops_snapshot_evolution`'s own windowed scan).
    let windowed: Vec<&&VersionRow> = incident.iter().filter(|e| e.overlaps(t1, t2)).collect();
    let mut bucket_starts = Vec::new();
    let mut b = t1;
    while b < t2 {
        bucket_starts.push(b);
        b += stride;
    }
    let series: Vec<Value> = bucket_starts
        .iter()
        .map(|&bs| {
            let degree = windowed.iter().filter(|e| e.valid_at(bs)).count();
            json!({"t": bs, "degree": degree})
        })
        .collect();

    json!({
        "neighbors_gained": gained.iter().take(limit).collect::<Vec<_>>(),
        "neighbors_gained_total": gained.len(),
        "neighbors_lost": lost.iter().take(limit).collect::<Vec<_>>(),
        "neighbors_lost_total": lost.len(),
        "degree_series": series,
        "stride": stride,
        "truncated": gained.len() > limit || lost.len() > limit,
    })
}
