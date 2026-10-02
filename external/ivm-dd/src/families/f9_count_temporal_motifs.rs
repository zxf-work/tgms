//! F9 `count_temporal_motifs`, motif `M_2node_pingpong` (the only
//! grid-registered motif; `ops_motifs.count_temporal_motifs`). `compute`
//! is the whole-window reference; the maintained dataflow (`views.rs`)
//! counts per (artifact, endpoint pair) with `motif_common::pingpong_count`
//! and assembles the same payload with `payload`.

use super::motif_common::{events_in_window, pingpong_triples};
use crate::model::VersionRow;
use serde_json::{json, Value};

pub fn compute(args: &Value, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let delta = args["delta"].as_i64().unwrap_or(0);
    let node_filter: Option<Vec<String>> = args
        .get("node_filter")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect());

    let as_of = crate::families::args_as_of_tt(args);
    let events = events_in_window(edges, t_a, t_b, as_of, &node_filter);
    let count = pingpong_triples(&events, delta).len();
    payload(count as i64, events.len() as i64)
}

pub fn payload(count: i64, n_events_in_window: i64) -> Value {
    json!({"count": count, "n_events_in_window": n_events_in_window, "truncated": false})
}
