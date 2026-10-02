//! F6 `aggregate_events`, `group_by=[{"dim":"endpoint","role":"src"}]`,
//! `aggregates=[{"agg":"count"}]` in every grid registration
//! (`ops_aggregate.aggregate_events::_native`; memo table "group by src,
//! count"). Routed Global.

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
    let role = args["group_by"][0]["role"].as_str().unwrap_or("src");
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);

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
