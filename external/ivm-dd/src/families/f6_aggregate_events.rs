//! F6 `aggregate_events`, `group_by=[{"dim":"endpoint","role":"src"}]`,
//! `aggregates=[{"agg":"count"}]` in every grid registration
//! (`ops_aggregate.aggregate_events::_native`). The maintained dataflow (`views.rs`) keeps one `count_total`
//! per (artifact, group key) and formats it with `payload_from_counts`.

use crate::model::VersionRow;
use serde_json::{json, Value};
use std::collections::BTreeMap;

pub fn compute(args: &Value, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let as_of = crate::families::args_as_of_tt(args);
    let rel_types: Option<Vec<String>> = args
        .get("rel_types")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect());
    let role = role(args);

    let mut counts: BTreeMap<&str, i64> = BTreeMap::new();
    for e in edges {
        if !(e.vt_s >= t_a && e.vt_s < t_b) || !e.believed_at(as_of) {
            continue;
        }
        if let Some(rt) = &rel_types {
            if !e.rel_type.as_deref().is_some_and(|r| rt.iter().any(|x| x == r)) {
                continue;
            }
        }
        let key = if role == "dst" { e.dst.as_deref() } else { e.src.as_deref() }.unwrap_or("");
        *counts.entry(key).or_insert(0) += 1;
    }
    let counts: Vec<(String, i64)> = counts.into_iter().map(|(k, c)| (k.to_string(), c)).collect();
    payload_from_counts(args, &counts)
}

/// The group-by role (`src` or `dst`) whose endpoint is the group key.
pub fn role(args: &Value) -> &str {
    args["group_by"][0]["role"].as_str().unwrap_or("src")
}

/// Whether an edge passes the artifact's `rel_types` filter.
pub fn rel_ok(args: &Value, rel_type: Option<&str>) -> bool {
    match args.get("rel_types").and_then(|v| v.as_array()) {
        None => true,
        Some(rt) => rel_type.is_some_and(|r| rt.iter().any(|x| x.as_str() == Some(r))),
    }
}

/// Payload from the per-group counts, which must be sorted by group key
/// (the reference's `BTreeMap` order).
pub fn payload_from_counts(args: &Value, counts: &[(String, i64)]) -> Value {
    let role = role(args);
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);
    let rows: Vec<Value> = counts
        .iter()
        .map(|(k, c)| {
            let mut m = serde_json::Map::new();
            m.insert(role.to_string(), json!(k));
            m.insert("count".to_string(), json!(c));
            Value::Object(m)
        })
        .collect();
    crate::families::paginate(&rows, limit, cursor.as_deref())
}
