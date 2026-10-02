//! Direct unit tests for each family's pure payload function
//! (`ivm_dd::families::*`), independent of the timely/differential-dataflow
//! wiring in `dataflow.rs` -- these pin the Rust reimplementation's logic
//! against hand-worked-out expected answers. `tests/fixtures/tiny1/` (once
//! generated) cross-checks the same functions against real TGMS oracle
//! digests end to end through `dataflow::run`; these tests are the first,
//! faster line of defense and are easier to debug when something disagrees.

use ivm_dd::model::{Kind, VersionRow, OPEN_END};
use serde_json::json;

fn node(vid: &str, uid: &str, vt_s: i64, vt_e: i64, tt_s: i64, tt_e: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Node, vid: vid.into(), uid: Some(uid.into()), eid: None, src: None, dst: None,
        rel_type: None, label: Some("Person".into()), disc: None, vt_s, vt_e, tt_s, tt_e,
        props: json!({"n": vid}), source: Some("ingest".into()), provenance_ref: None,
    }
}

fn edge(vid: &str, eid: &str, src: &str, dst: &str, vt_s: i64, vt_e: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Edge, vid: vid.into(), uid: None, eid: Some(eid.into()), src: Some(src.into()),
        dst: Some(dst.into()), rel_type: Some("knows".into()), label: None, disc: Some("".into()),
        vt_s, vt_e, tt_s: 0, tt_e: OPEN_END, props: json!({}), source: Some("ingest".into()),
        provenance_ref: None,
    }
}

#[test]
fn f1_entity_history_orders_by_vt_s_then_vid() {
    let nodes = vec![
        node("v2", "a", 100, 200, 0, OPEN_END),
        node("v1", "a", 0, 100, 0, OPEN_END),
    ];
    let out = ivm_dd::families::f1_entity_history::compute(&json!({}), &nodes, &[]);
    let rows = out["rows"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["vid"], "v1");
    assert_eq!(rows[1]["vid"], "v2");
    assert_eq!(out["rows_total"], 2);
    assert_eq!(out["truncated"], false);
}

#[test]
fn f1_include_edges_caps_at_limit_and_sets_truncated() {
    let nodes = vec![node("v1", "a", 0, OPEN_END, 0, OPEN_END)];
    let edges = vec![
        edge("e1", "eid1", "a", "b", 0, 10),
        edge("e2", "eid2", "a", "c", 5, 15),
        edge("e3", "eid3", "a", "d", 10, 20),
    ];
    let args = json!({"include_edges": true, "limit": 2});
    let out = ivm_dd::families::f1_entity_history::compute(&args, &nodes, &edges);
    assert_eq!(out["edges"].as_array().unwrap().len(), 2);
    assert_eq!(out["edges_truncated"], true);
}

#[test]
fn f3_snapshot_subgraph_one_hop() {
    let nodes = vec![
        node("nva", "a", 0, 1000, 0, OPEN_END),
        node("nvb", "b", 0, 1000, 0, OPEN_END),
        node("nvc", "c", 0, 1000, 0, OPEN_END),
    ];
    let edges = vec![
        edge("eva", "ea", "a", "b", 0, 1000),
        edge("evb", "eb", "b", "c", 0, 1000), // 2 hops from a, excluded at hops=1
    ];
    let args = json!({"seeds": ["a"], "hops": 1, "t_valid": 500});
    let out = ivm_dd::families::f3_snapshot_subgraph::compute(&args, &nodes, &edges);
    assert_eq!(out["nodes_total"], 2); // a (hop0), b (hop1)
    assert_eq!(out["rows_total"], 1); // induced edge a-b only
    let node_uids: Vec<&str> = out["nodes"].as_array().unwrap().iter().map(|n| n["uid"].as_str().unwrap()).collect();
    assert_eq!(node_uids, vec!["a", "b"]);
}

#[test]
fn f4_diff_snapshots_detects_added_removed_and_changed() {
    let nodes = vec![
        node("n1", "a", 0, 50, 0, OPEN_END),  // present at t1=10 only
        node("n2", "b", 0, 1000, 0, OPEN_END), // present at both
        node("n3", "c", 60, 1000, 0, OPEN_END), // present at t2=70 only
    ];
    let args = json!({"t1": 10, "t2": 70});
    let out = ivm_dd::families::f4_diff_snapshots::compute(&args, &nodes, &[]);
    assert_eq!(out["nodes_removed"], json!(["a"]));
    assert_eq!(out["nodes_added"], json!(["c"]));
    assert_eq!(out["nodes_removed_total"], 1);
    assert_eq!(out["nodes_added_total"], 1);
}

#[test]
fn f5_neighborhood_evolution_gained_and_lost() {
    let edges = vec![
        edge("e1", "eid1", "a", "b", 0, 50),   // neighbor b at t1=10, gone by t2=60
        edge("e2", "eid2", "a", "c", 55, 200), // neighbor c at t2=60, not at t1=10
    ];
    let args = json!({"uid": "a", "t1": 10, "t2": 60, "stride": 10});
    let out = ivm_dd::families::f5_neighborhood_evolution::compute(&args, "a", &edges);
    assert_eq!(out["neighbors_lost"], json!(["b"]));
    assert_eq!(out["neighbors_gained"], json!(["c"]));
}

#[test]
fn f6_aggregate_events_groups_by_src_and_counts() {
    let edges = vec![
        edge("e1", "eid1", "a", "b", 5, 1000),
        edge("e2", "eid2", "a", "c", 6, 1000),
        edge("e3", "eid3", "b", "c", 7, 1000),
    ];
    let args = json!({"group_by": [{"dim": "endpoint", "role": "src"}], "window": {"t_a": 0, "t_b": 100}});
    let out = ivm_dd::families::f6_aggregate_events::compute(&args, &edges);
    assert_eq!(out["rows"], json!([{"src": "a", "count": 2}, {"src": "b", "count": 1}]));
}

#[test]
fn f7_graph_metric_timeseries_buckets_event_counts() {
    let edges = vec![
        edge("e1", "eid1", "a", "b", 5, 1000),
        edge("e2", "eid2", "a", "c", 15, 1000),
        edge("e3", "eid3", "b", "c", 25, 1000),
    ];
    let args = json!({"window": {"t_a": 0, "t_b": 30}, "stride": 10});
    let out = ivm_dd::families::f7_graph_metric_timeseries::compute(&args, &edges);
    assert_eq!(out["n_buckets"], 3);
    let rows = out["rows"].as_array().unwrap();
    assert_eq!(rows[0]["value"], 1);
    assert_eq!(rows[1]["value"], 1);
    assert_eq!(rows[2]["value"], 1);
}

#[test]
fn f8_burst_detection_flags_a_spike() {
    // uid "a" touched by a steady trickle, then a spike in the last bucket.
    let mut edges = Vec::new();
    for i in 0..10 {
        edges.push(edge(&format!("e{i}"), &format!("eid{i}"), "a", "x", i * 10 + 1, 1000));
    }
    // bucket 10 (t=[100,110)): a burst of 5 events
    for i in 0..5 {
        edges.push(edge(&format!("s{i}"), &format!("sid{i}"), "a", "x", 100 + i, 1000));
    }
    let args = json!({
        "target": {"kind": "node_activity", "uid": "a"},
        "window": {"t_a": 0, "t_b": 110}, "stride": 10,
        "params": {"w": 10, "z": 3.0},
    });
    let out = ivm_dd::families::f8_burst_detection::compute(&args, "a", &edges);
    assert_eq!(out["n_buckets"], 11);
    let rows = out["rows"].as_array().unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["t_a"], 100);
    assert_eq!(rows[0]["value"], 5.0);
}

#[test]
fn f9_and_f10_pingpong_motif() {
    // a->b at t=0, b->a at t=5 (within delta of a), a->b at t=8 (within delta of a).
    let edges = vec![
        edge("e1", "eid1", "a", "b", 0, 1000),
        edge("e2", "eid2", "b", "a", 5, 1000),
        edge("e3", "eid3", "a", "b", 8, 1000),
    ];
    let args = json!({"motif": "M_2node_pingpong", "delta": 10, "window": {"t_a": 0, "t_b": 100}});
    let count_out = ivm_dd::families::f9_count_temporal_motifs::compute(&args, &edges);
    assert_eq!(count_out["count"], 1);
    assert_eq!(count_out["n_events_in_window"], 3);

    let find_out = ivm_dd::families::f10_find_temporal_motif_instances::compute(&args, &edges);
    let rows = find_out["rows"].as_array().unwrap();
    assert_eq!(rows.len(), 1);
    let triple = rows[0]["edges"].as_array().unwrap();
    assert_eq!(triple.len(), 3);
    assert_eq!(triple[0]["eid"], "eid1");
    assert_eq!(triple[1]["eid"], "eid2");
    assert_eq!(triple[2]["eid"], "eid3");
}

#[test]
fn f9_respects_delta_bound() {
    let edges = vec![
        edge("e1", "eid1", "a", "b", 0, 1000),
        edge("e2", "eid2", "b", "a", 5, 1000),
        edge("e3", "eid3", "a", "b", 50, 1000), // outside delta of a (t=0)
    ];
    let args = json!({"motif": "M_2node_pingpong", "delta": 10, "window": {"t_a": 0, "t_b": 100}});
    let out = ivm_dd::families::f9_count_temporal_motifs::compute(&args, &edges);
    assert_eq!(out["count"], 0);
}

#[test]
fn f11_temporal_reachability_earliest_arrival() {
    let edges = vec![
        edge("e1", "eid1", "a", "b", 0, 100),
        edge("e2", "eid2", "b", "c", 10, 100),
        edge("e3", "eid3", "a", "c", 50, 100), // slower direct path
    ];
    let args = json!({"src": "a", "window": {"t_a": 0, "t_b": 1000}});
    let arr = ivm_dd::families::f11_temporal_reachability::fixpoint_arrivals(&edges, "a", 0, 1000, OPEN_END);
    assert_eq!(arr.get("b"), Some(&0));
    assert_eq!(arr.get("c"), Some(&10)); // via b, earlier than the direct edge's own vt_s=50
    assert!(!arr.contains_key("a"));

    let out = ivm_dd::families::f11_temporal_reachability::compute(&args, "a", &edges);
    let rows = out["rows"].as_array().unwrap();
    assert_eq!(rows[0]["uid"], "b");
    assert_eq!(rows[0]["earliest_arrival"], 0);
    assert_eq!(rows[1]["uid"], "c");
    assert_eq!(rows[1]["earliest_arrival"], 10);
}

#[test]
fn f12_temporal_paths_node_simple_and_time_respecting() {
    let edges = vec![
        edge("e1", "eid1", "a", "b", 0, 100),
        edge("e2", "eid2", "b", "d", 10, 100),
        edge("e3", "eid3", "a", "c", 5, 100),
        edge("e4", "eid4", "c", "d", 20, 100),
    ];
    let args = json!({"src": "a", "dst": "d", "window": {"t_a": 0, "t_b": 1000}, "k": 2, "max_hops": 4});
    let out = ivm_dd::families::f12_temporal_paths::compute(&args, "a", "d", &edges);
    assert_eq!(out["rows_total"], 2);
    let rows = out["rows"].as_array().unwrap();
    // a->b->d arrives at 10, a->c->d arrives at 20: sorted by arrival.
    assert_eq!(rows[0]["arrival"], 10);
    assert_eq!(rows[1]["arrival"], 20);
}

#[test]
fn f13_compute_control_is_constant() {
    let args = json!({"fn": "count", "input": [{"x": 1}, {"x": 2}]});
    let out = ivm_dd::families::f13_compute::compute(&args);
    assert_eq!(out, json!({"value": 2, "truncated": false}));
}
