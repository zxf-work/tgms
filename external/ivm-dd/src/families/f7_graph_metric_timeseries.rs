//! F7 `graph_metric_timeseries`, `metric="edge_event_count"` in every grid
//! registration (`ops_series.graph_metric_timeseries`). Routed Global.

use crate::model::VersionRow;
use serde_json::{json, Value};

pub fn compute(args: &Value, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let stride = args["stride"].as_i64().unwrap_or(1).max(1);
    let as_of = crate::families::args_as_of_tt(args);
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);

    let mut bucket_starts = Vec::new();
    let mut b = t_a;
    while b < t_b {
        bucket_starts.push(b);
        b += stride;
    }
    let believed: Vec<&VersionRow> = edges.iter().filter(|e| e.believed_at(as_of)).collect();
    let rows: Vec<Value> = bucket_starts
        .iter()
        .map(|&bs| {
            let be = (bs + stride).min(t_b);
            let value = believed.iter().filter(|e| e.vt_s >= bs && e.vt_s < be).count();
            json!({"t_a": bs, "t_b": be, "value": value})
        })
        .collect();
    let mut out = crate::families::paginate(&rows, limit, cursor.as_deref());
    out["n_buckets"] = json!(rows.len());
    out
}
