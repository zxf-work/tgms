//! F10 `find_temporal_motif_instances`, motif `M_2node_pingpong`
//! (`ops_motifs.find_temporal_motif_instances`). `compute` is the
//! whole-window reference; the maintained dataflow (`views.rs`) keeps, per
//! (artifact, endpoint pair), that pair's first `offset + limit` instances
//! and its instance count, and merges them per artifact with
//! `payload_from_window`.

use super::motif_common::{events_in_window, pingpong_triples, Event, Inst, MEv};
use crate::model::VersionRow;
use serde_json::{json, Value};

fn edge_json(e: &MEv) -> Value {
    json!({"src": e.src, "dst": e.dst, "t": e.t, "eid": e.eid, "rel_type": e.rel_type})
}

fn owned(e: &Event) -> MEv {
    MEv {
        t: e.t,
        eid: e.eid.to_string(),
        src: e.src.to_string(),
        dst: e.dst.to_string(),
        rel_type: e.rel_type.to_string(),
    }
}

pub fn inst_of(events: &[Event], (a, b, c): (usize, usize, usize)) -> Inst {
    Inst { a: owned(&events[a]), b: owned(&events[b]), c: owned(&events[c]) }
}

pub fn inst_json(i: &Inst) -> Value {
    json!({"edges": [edge_json(&i.a), edge_json(&i.b), edge_json(&i.c)]})
}

/// The paginated payload from the instances at sorted positions
/// `[offset, offset + limit)` and the total instance count.
pub fn payload_from_window(window: &[Inst], total: usize, offset: usize) -> Value {
    crate::families::paginate_window(window.iter().map(inst_json).collect(), total, offset)
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
    let mut insts: Vec<Inst> = pingpong_triples(&events, delta).into_iter().map(|t| inst_of(&events, t)).collect();
    insts.sort();
    let rows: Vec<Value> = insts.iter().map(inst_json).collect();
    crate::families::paginate(&rows, limit, cursor.as_deref())
}
