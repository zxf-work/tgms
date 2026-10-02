//! F10 `find_temporal_motif_instances`, motif `M_2node_pingpong`
//! (`ops_motifs.find_temporal_motif_instances`). Routed Global.

use super::motif_common::{events_in_window, pingpong_triples, Event};
use crate::model::VersionRow;
use serde_json::{json, Value};

fn edge_json(e: &Event) -> Value {
    json!({"src": e.src, "dst": e.dst, "t": e.t, "eid": e.eid, "rel_type": e.rel_type})
}

pub fn compute(args: &Value, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let delta = args["delta"].as_i64().unwrap_or(0);
    let node_filter: Option<Vec<String>> = args
        .get("node_filter")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect());
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);

    let as_of = crate::families::args_as_of_tt(args);
    let events = events_in_window(edges, t_a, t_b, as_of, &node_filter);
    let mut triples = pingpong_triples(&events, delta);
    triples.sort_by(|&(a1, b1, c1), &(a2, b2, c2)| {
        let key = |a: usize, b: usize, c: usize| {
            (events[a].t, events[a].eid, events[b].t, events[b].eid, events[c].t, events[c].eid)
        };
        key(a1, b1, c1).cmp(&key(a2, b2, c2))
    });
    let rows: Vec<Value> = triples
        .iter()
        .map(|&(a, b, c)| json!({"edges": [edge_json(&events[a]), edge_json(&events[b]), edge_json(&events[c])]}))
        .collect();
    crate::families::paginate(&rows, limit, cursor.as_deref())
}
