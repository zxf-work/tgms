//! F2 `version_history` (`ops_versions.version_history`), node kind only in
//! every grid registration (memo table). Routed by `kind` -- a whole
//! population scan filtered by window/belief inside this function (memo
//! §3.2's bucket routing would prune by window; this crate routes coarser,
//! see README "Routing").

use crate::families::{args_as_of_tt, args_cursor, args_limit, paginate};
use crate::model::{VersionRow, OPEN_END};
use serde_json::{json, Value};

fn clamp_tt(as_of_tt: i64) -> i64 {
    as_of_tt.min(OPEN_END - 1)
}

pub fn compute(args: &Value, rows_of_kind: &[VersionRow]) -> Value {
    let as_of = clamp_tt(args_as_of_tt(args));
    let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
    let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
    let belief = args.get("belief").and_then(|v| v.as_str()).unwrap_or("current");
    let rel_types: Option<Vec<String>> = args
        .get("rel_types")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect());
    let limit = args_limit(args);
    let cursor = args_cursor(args);

    let mut matched: Vec<&VersionRow> = rows_of_kind
        .iter()
        .filter(|v| v.overlaps(t_a, t_b))
        .filter(|v| match belief {
            "current" => v.tt_s <= as_of && v.tt_e > as_of,
            "superseded" => v.tt_s <= as_of && v.tt_e <= as_of,
            "all" => v.tt_s <= as_of,
            _ => false,
        })
        .filter(|v| match (&rel_types, v.is_node()) {
            (_, true) => true,
            (None, false) => true,
            (Some(rt), false) => v.rel_type.as_deref().is_some_and(|r| rt.iter().any(|x| x == r)),
        })
        .collect();
    matched.sort_by(|a, b| (a.tt_s, &a.vid).cmp(&(b.tt_s, &b.vid)));

    let rows: Vec<Value> = matched
        .iter()
        .map(|v| {
            if v.is_node() {
                json!({
                    "vid": v.vid, "uid": v.uid, "label": v.label,
                    "vt_s": v.vt_s, "vt_e": v.vt_e, "tt_s": v.tt_s,
                    "tt_e": v.reported_tt_e(as_of),
                })
            } else {
                json!({
                    "vid": v.vid, "eid": v.eid, "src": v.src, "dst": v.dst,
                    "rel_type": v.rel_type, "vt_s": v.vt_s, "vt_e": v.vt_e,
                    "tt_s": v.tt_s, "tt_e": v.reported_tt_e(as_of),
                })
            }
        })
        .collect();
    paginate(&rows, limit, cursor.as_deref())
}
