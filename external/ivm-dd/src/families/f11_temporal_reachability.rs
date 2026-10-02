//! F11 `temporal_reachability`, `direction="out"`, `delta_max_wait=null`
//! ("no wait bound") in every grid registration
//! (`ops_paths.temporal_reachability`).
//!
//! `fixpoint_arrivals` (a plain-Rust label-correcting fixpoint,
//! `ops_paths.py`'s own vectorized version without `delta_max_wait`) is the
//! reference. The maintained dataflow (`views.rs`) computes the same
//! least fixpoint with differential dataflow's `iterate`, and formats it
//! with `format_sorted`; `tests/maintained_tests.rs` pins the two against
//! each other under insertions and retractions.

use crate::model::{VersionRow, OPEN_END};
use serde_json::{json, Value};
use std::collections::HashMap;

/// Label-correcting fixpoint, `direction="out"`, no wait bound: `arr[src] =
/// t_a`; repeatedly relax every edge `(u -> v, [vt_s, vt_e))` with
/// `tau = max(arr[u], vt_s)`; `arr[v] = min(arr[v], tau)` wherever
/// `tau < vt_e && tau < t_b`; until no label changes.
pub fn fixpoint_arrivals(
    edges: &[VersionRow],
    src: &str,
    t_a: i64,
    t_b: i64,
    as_of: i64,
) -> HashMap<String, i64> {
    let believed: Vec<&VersionRow> = edges
        .iter()
        .filter(|e| e.believed_at(as_of) && e.overlaps(t_a, t_b))
        .collect();
    let mut arr: HashMap<String, i64> = HashMap::new();
    arr.insert(src.to_string(), t_a);
    loop {
        let mut changed = false;
        for e in &believed {
            let u = e.src.as_deref().unwrap_or("");
            let v = e.dst.as_deref().unwrap_or("");
            let Some(&a_u) = arr.get(u) else { continue };
            if a_u >= OPEN_END {
                continue;
            }
            let tau = a_u.max(e.vt_s);
            if tau < e.vt_e && tau < t_b {
                let better = match arr.get(v) {
                    None => true,
                    Some(&cur) => tau < cur,
                };
                if better {
                    arr.insert(v.to_string(), tau);
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }
    arr.remove(src);
    arr
}

pub fn format_payload(arr: &HashMap<String, i64>, limit: usize, cursor: Option<&str>) -> Value {
    let mut rows: Vec<(i64, &str)> = arr.iter().map(|(u, &a)| (a, u.as_str())).collect();
    rows.sort();
    format_sorted(&rows, limit, cursor)
}

/// Payload from `(earliest_arrival, uid)` rows already sorted ascending
/// (the source itself excluded).
pub fn format_sorted(rows: &[(i64, &str)], limit: usize, cursor: Option<&str>) -> Value {
    let json_rows: Vec<Value> = rows
        .iter()
        .map(|(a, u)| json!({"uid": u, "earliest_arrival": a}))
        .collect();
    crate::families::paginate(&json_rows, limit, cursor)
}

pub fn compute(args: &Value, src: &str, edges: &[VersionRow]) -> Value {
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let as_of = crate::families::args_as_of_tt(args);
    let limit = crate::families::args_limit(args);
    let cursor = crate::families::args_cursor(args);
    let arr = fixpoint_arrivals(edges, src, t_a, t_b, as_of);
    format_payload(&arr, limit, cursor.as_deref())
}
