//! F8 `burst_detection`, `target.kind="node_activity"`, `method="zscore"`,
//! `params={w:10, z:3.0}` in every grid registration
//! (`ops_series.burst_detection`). Routed by `uid` (`target.uid`).

use crate::digest::round9_f64;
use crate::model::VersionRow;
use serde_json::{json, Value};

pub fn compute(args: &Value, uid: &str, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let stride = args["stride"].as_i64().unwrap_or(1).max(1);
    let as_of = crate::families::args_as_of_tt(args);
    let rel_type = args["target"]["rel_type"].as_str();
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
    let incident: Vec<&VersionRow> = edges
        .iter()
        .filter(|e| e.believed_at(as_of))
        .filter(|e| e.src.as_deref() == Some(uid) || e.dst.as_deref() == Some(uid))
        .filter(|e| rel_type.is_none_or(|rt| e.rel_type.as_deref() == Some(rt)))
        .collect();
    let series: Vec<f64> = bucket_starts
        .iter()
        .map(|&bs| {
            let be = (bs + stride).min(t_b);
            incident.iter().filter(|e| e.vt_s >= bs && e.vt_s < be).count() as f64
        })
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
