//! The 13 registered operator families as pure payload functions (memo §3.3
//! "families as maintained dataflows"; `dataflow.rs` wires each one behind
//! the DD routing/join/reduce machinery -- this module is the part that has
//! to agree, field for field, with the Python kernels in
//! `tgms/temporal/ops_*.py`, so it is kept independent of timely/differential
//! and unit-testable on a plain `&[VersionRow]`).
//!
//! Every `compute` function takes the *whole* relevant row set for its key
//! (not a pre-filtered one) and returns the exact JSON payload
//! `tgms.temporal.algebra.call_operator` would digest (i.e. the kernel
//! function's own return value, **before** the envelope is added and
//! **before** float canonicalization -- `crate::digest::result_digest`
//! applies that last step uniformly).

pub mod f1_entity_history;
pub mod f2_version_history;
pub mod f3_snapshot_subgraph;
pub mod f4_diff_snapshots;
pub mod f5_neighborhood_evolution;
pub mod f6_aggregate_events;
pub mod f7_graph_metric_timeseries;
pub mod f8_burst_detection;
mod motif_common;
pub mod f9_count_temporal_motifs;
pub mod f10_find_temporal_motif_instances;
pub mod f11_temporal_reachability;
pub mod f12_temporal_paths;
pub mod f13_compute;

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Family {
    F1EntityHistory,
    F2VersionHistory,
    F3SnapshotSubgraph,
    F4DiffSnapshots,
    F5NeighborhoodEvolution,
    F6AggregateEvents,
    F7GraphMetricTimeseries,
    F8BurstDetection,
    F9CountTemporalMotifs,
    F10FindTemporalMotifInstances,
    F11TemporalReachability,
    F12TemporalPaths,
    F13Compute,
}

impl Family {
    /// Maps `artifacts.jsonl`'s own `op` field (== the TGMS operator-registry
    /// name) to a family. `co_active` is deliberately absent: 0 instances
    /// are ever registered in the grid (memo A8) -- a bundle that somehow
    /// carries one is a shape-test failure, not a silently-ignored row.
    pub fn from_op(op: &str) -> Option<Family> {
        use Family::*;
        Some(match op {
            "entity_history" => F1EntityHistory,
            "version_history" => F2VersionHistory,
            "snapshot_subgraph" => F3SnapshotSubgraph,
            "diff_snapshots" => F4DiffSnapshots,
            "neighborhood_evolution" => F5NeighborhoodEvolution,
            "aggregate_events" => F6AggregateEvents,
            "graph_metric_timeseries" => F7GraphMetricTimeseries,
            "burst_detection" => F8BurstDetection,
            "count_temporal_motifs" => F9CountTemporalMotifs,
            "find_temporal_motif_instances" => F10FindTemporalMotifInstances,
            "temporal_reachability" => F11TemporalReachability,
            "temporal_paths" => F12TemporalPaths,
            "compute" => F13Compute,
            _ => return None,
        })
    }

    pub fn label(&self) -> &'static str {
        use Family::*;
        match self {
            F1EntityHistory => "F1 entity_history",
            F2VersionHistory => "F2 version_history",
            F3SnapshotSubgraph => "F3 snapshot_subgraph",
            F4DiffSnapshots => "F4 diff_snapshots",
            F5NeighborhoodEvolution => "F5 neighborhood_evolution",
            F6AggregateEvents => "F6 aggregate_events",
            F7GraphMetricTimeseries => "F7 graph_metric_timeseries",
            F8BurstDetection => "F8 burst_detection",
            F9CountTemporalMotifs => "F9 count_temporal_motifs",
            F10FindTemporalMotifInstances => "F10 find_temporal_motif_instances",
            F11TemporalReachability => "F11 temporal_reachability",
            F12TemporalPaths => "F12 temporal_paths",
            F13Compute => "F13 compute",
        }
    }

    /// Whether this family's maintained dataflow needs DD's `iterate`
    /// (memo §3.3: "F11 is the only one needing `iterate`").
    pub fn needs_iterate(&self) -> bool {
        matches!(self, Family::F11TemporalReachability)
    }

    /// The route key this family's artifacts register under
    /// (`dataflow.rs::route_key_for`); documented here so the family table
    /// and the routing table cannot silently drift apart.
    pub fn route_kind(&self) -> RouteKind {
        use Family::*;
        match self {
            F1EntityHistory | F5NeighborhoodEvolution | F8BurstDetection => RouteKind::Uid,
            F2VersionHistory => RouteKind::Kind,
            // F3 needs 1-hop neighbours' own node rows (a different uid
            // from the seed), which a flat uid route cannot see without a
            // second dependent join -- scoped to Global (see README
            // "Routing"), not the seed-only key the memo's table implies.
            F3SnapshotSubgraph
            | F4DiffSnapshots
            | F6AggregateEvents
            | F7GraphMetricTimeseries
            | F9CountTemporalMotifs
            | F10FindTemporalMotifInstances
            // F12's DFS also runs inside the Global route+reduce closure
            // (see dataflow.rs's module doc -- a deliberate deviation from
            // memo §3.3's four unrolled joins, not a routing bug).
            | F12TemporalPaths
            // F11 *should* be `iterate` per the memo (see `needs_iterate`)
            // -- a DD-`iterate`-based dataflow was built and passed two of
            // three correction epochs on `tests/fixtures/tiny1`, but failed
            // to re-derive a worse fallback path after its best path was
            // *retracted* (the fixpoint stayed at its pre-retraction
            // answer). That is a real incrementality bug this session
            // could not root-cause in the remaining time, so F11 is wired
            // through the same Global route+reduce path as F12, calling
            // the tested, already-correct `fixpoint_arrivals` fresh every
            // time -- flagged for the Opus review the memo calls for.
            | F11TemporalReachability => RouteKind::Global,
            F13Compute => RouteKind::None,
        }
    }
}

pub const DEFAULT_LIMIT: usize = 100;

/// `tgms.temporal.algebra.paginate`: deterministic offset pagination over
/// an already-ordered row list. `cursor` is a plaintext decimal offset.
pub fn paginate(rows: &[serde_json::Value], limit: usize, cursor: Option<&str>) -> serde_json::Value {
    let offset: usize = cursor.and_then(|c| c.parse().ok()).unwrap_or(0);
    let end = (offset + limit).min(rows.len());
    let window: Vec<serde_json::Value> = if offset < rows.len() {
        rows[offset..end].to_vec()
    } else {
        Vec::new()
    };
    let truncated = offset + window.len() < rows.len();
    serde_json::json!({
        "rows": window,
        "rows_total": rows.len(),
        "truncated": truncated,
        "cursor": if truncated { Some((offset + window.len()).to_string()) } else { None },
    })
}

pub fn args_limit(args: &serde_json::Value) -> usize {
    args.get("limit")
        .and_then(|v| v.as_u64())
        .unwrap_or(DEFAULT_LIMIT as u64) as usize
}

pub fn args_cursor(args: &serde_json::Value) -> Option<String> {
    args.get("cursor").and_then(|v| v.as_str()).map(|s| s.to_string())
}

pub fn args_as_of_tt(args: &serde_json::Value) -> i64 {
    args.get("as_of_tt")
        .and_then(|v| v.as_i64())
        .unwrap_or(crate::model::OPEN_END)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RouteKind {
    /// Keyed by a single uid named in the artifact's own args (its `uid`,
    /// or its first `seeds`/`src` entry).
    Uid,
    /// Keyed by `kind` ("node"/"edge") -- a whole-population scan filtered
    /// by window/belief inside the family's own `compute`.
    Kind,
    /// Keyed by a single fixed global bucket -- the family needs the whole
    /// store (memo-flagged simplification; see README "Routing" section).
    Global,
    /// No store data needed at all (F13's control).
    None,
    /// Wired as a dedicated dataflow in `dataflow.rs`, not the generic
    /// route-and-reduce helper (F11's `iterate`, F12's unrolled joins).
    Traversal,
}
