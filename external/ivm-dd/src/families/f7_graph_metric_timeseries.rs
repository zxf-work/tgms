//! F7 `graph_metric_timeseries`, `metric="edge_event_count"` in every grid
//! registration (`ops_series.graph_metric_timeseries`). The maintained
//! dataflow (`views.rs`) keeps one `count_total` per (artifact, bucket
//! index) and formats it with `payload_from_counts`.

use crate::model::VersionRow;
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn window_stride(args: &Value) -> (i64, i64, i64) {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let stride = args["stride"].as_i64().unwrap_or(1).max(1);
    (t_a, t_b, stride)
}

/// The series bucket an event at `t` falls in, if inside the window.
pub fn bucket_index(args: &Value, t: i64) -> Option<usize> {
    let (t_a, t_b, stride) = window_stride(args);
    if t >= t_a && t < t_b {
        Some(((t - t_a) / stride) as usize)
    } else {
        None
    }
}

pub fn compute(args: &Value, edges: &[VersionRow]) -> Value {
    let as_of = crate::families::args_as_of_tt(args);
    let mut counts: BTreeMap<usize, i64> = BTreeMap::new();
    for e in edges.iter().filter(|e| e.believed_at(as_of)) {
        if let Some(i) = bucket_index(args, e.vt_s) {
            *counts.entry(i).or_insert(0) += 1;
        }
    }
    payload_from_counts(args, &counts)
}

/// Payload from the per-bucket event counts (absent bucket = 0).
pub fn payload_from_counts(args: &Value, counts: &BTreeMap<usize, i64>) -> Value {
    let (t_a, t_b, stride) = window_stride(args);
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);

    let mut bucket_starts = Vec::new();
    let mut b = t_a;
    while b < t_b {
        bucket_starts.push(b);
        b += stride;
    }
    let rows: Vec<Value> = bucket_starts
        .iter()
        .enumerate()
        .map(|(i, &bs)| {
            let be = (bs + stride).min(t_b);
            let value = counts.get(&i).copied().unwrap_or(0);
            json!({"t_a": bs, "t_b": be, "value": value})
        })
        .collect();
    let mut out = crate::families::paginate(&rows, limit, cursor.as_deref());
    out["n_buckets"] = json!(rows.len());
    out
}
