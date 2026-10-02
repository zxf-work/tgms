//! F8 `burst_detection`, `target.kind="node_activity"`, `method="zscore"`,
//! `params={w:10, z:3.0}` in every grid registration
//! (`ops_series.burst_detection`). The maintained dataflow (`views.rs`)
//! joins `target.uid` with its incident edges, keeps one `count_total` per
//! (artifact, series bucket), and formats it with `payload_from_counts`.

use crate::digest::round9_f64;
use crate::model::VersionRow;
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn window_stride(args: &Value) -> (i64, i64, i64) {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let stride = args["stride"].as_i64().unwrap_or(1).max(1);
    (t_a, t_b, stride)
}

/// Whether an edge counts toward the target's activity series (belief,
/// incidence, optional `target.rel_type`), window aside.
pub fn counts_edge(args: &Value, uid: &str, e: &VersionRow) -> bool {
    let as_of = crate::families::args_as_of_tt(args);
    let rel_type = args["target"]["rel_type"].as_str();
    e.believed_at(as_of)
        && (e.src.as_deref() == Some(uid) || e.dst.as_deref() == Some(uid))
        && rel_type.is_none_or(|rt| e.rel_type.as_deref() == Some(rt))
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

pub fn compute(args: &Value, uid: &str, edges: &[VersionRow]) -> Value {
    let mut counts: BTreeMap<usize, i64> = BTreeMap::new();
    for e in edges.iter().filter(|e| counts_edge(args, uid, e)) {
        if let Some(i) = bucket_index(args, e.vt_s) {
            *counts.entry(i).or_insert(0) += 1;
        }
    }
    payload_from_counts(args, &counts)
}

/// Payload from the per-bucket activity counts (absent bucket = 0): the
/// z-score of each bucket against the up-to-`w` preceding buckets.
pub fn payload_from_counts(args: &Value, counts: &BTreeMap<usize, i64>) -> Value {
    let (t_a, t_b, stride) = window_stride(args);
    let w = args["params"]["w"].as_u64().unwrap_or(10) as usize;
    let z = args["params"]["z"].as_f64().unwrap_or(3.0);
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);

    let mut bucket_starts = Vec::new();
    let mut b = t_a;
    while b < t_b {
        bucket_starts.push(b);
        b += stride;
    }
    let series: Vec<f64> = (0..bucket_starts.len())
        .map(|i| counts.get(&i).copied().unwrap_or(0) as f64)
        .collect();

    let mut flagged = Vec::new();
    for bi in 0..series.len() {
        let lo = bi.saturating_sub(w);
        let hist = &series[lo..bi];
        if hist.is_empty() {
            continue;
        }
        let x = series[bi];
        let mean = hist.iter().sum::<f64>() / hist.len() as f64;
        let var = hist.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / hist.len() as f64;
        let std = var.sqrt();
        let score_raw = if std > 0.0 {
            (x - mean).abs() / std
        } else if x == mean {
            0.0
        } else {
            1e9
        };
        let score = round9_f64(score_raw);
        if score >= z {
            flagged.push(json!({
                "t_a": bucket_starts[bi],
                "t_b": (bucket_starts[bi] + stride).min(t_b),
                "value": x,
                "score": score,
            }));
        }
    }
    let mut out = crate::families::paginate(&flagged, limit, cursor.as_deref());
    out["n_buckets"] = json!(series.len());
    out
}
