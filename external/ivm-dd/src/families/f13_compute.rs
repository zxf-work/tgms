//! F13 `compute`, the ∅-scope control: `{"fn": "count", "input": [{"x":1},
//! {"x":2}]}` (`tgms/eval/storm.py`'s own control-template construction,
//! not the memo's guess at it -- see the crate README's "deviations"
//! section). Reads no store data and never changes across any burst;
//! `dataflow.rs` seeds it once at epoch 0 and never re-derives it.

use serde_json::{json, Value};

pub fn compute(args: &Value) -> Value {
    let fn_name = args.get("fn").and_then(|v| v.as_str()).unwrap_or("count");
    assert_eq!(fn_name, "count", "F13 control is only ever registered as fn=count (storm.py TEMPLATES)");
    let n = args.get("input").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
    json!({"value": n, "truncated": false})
}
