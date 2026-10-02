//! The 13 registered operator families as pure payload functions
//! (`views.rs` builds the maintained dataflow for each one -- this module
//! is the part that has to agree, field for field, with the Python kernels
//! in `tgms/temporal/ops_*.py`, so it is kept independent of
//! timely/differential and unit-testable on a plain `&[VersionRow]`).
//!
//! Each family's `compute` is its whole-store reference: given every row it
//! may read (F1/F5: the uid's own versions and incident edges, which
//! `dataflow::compute_payload_pub` selects from a full snapshot), it
//! applies its own window/belief filters and returns the exact JSON payload
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
pub mod motif_common;
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
    /// are ever registered in the grid -- a bundle that somehow
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

    /// Whether this family's maintained dataflow uses DD's `iterate` (F11
    /// only; F12's bounded paths are unrolled joins).
    pub fn needs_iterate(&self) -> bool {
        matches!(self, Family::F11TemporalReachability)
    }

    /// How this family's artifacts meet the version collections in
    /// `views.rs` (see the README's "Family -> dataflow" table and
    /// "Routing" section for the fan-out analysis).
    pub fn route_kind(&self) -> RouteKind {
        use Family::*;
        match self {
            F1EntityHistory | F5NeighborhoodEvolution | F8BurstDetection => RouteKind::Uid,
            F3SnapshotSubgraph => RouteKind::UidExpansion,
            F2VersionHistory => RouteKind::IntervalBucket,
            F4DiffSnapshots => RouteKind::InstantBucket,
            F6AggregateEvents
            | F7GraphMetricTimeseries
            | F9CountTemporalMotifs
            | F10FindTemporalMotifInstances => RouteKind::EventBucket,
            F11TemporalReachability | F12TemporalPaths => RouteKind::Traversal,
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

/// `paginate` for a caller that already holds only the requested window
/// (the rows at sorted positions `[offset, offset + limit)`) and the total
/// row count -- the maintained families (F10) that never materialize the
/// full ordered list. Same output shape and `truncated`/`cursor` rule as
/// `paginate`.
pub fn paginate_window(window: Vec<serde_json::Value>, total: usize, offset: usize) -> serde_json::Value {
    let truncated = offset + window.len() < total;
    let next = offset + window.len();
    serde_json::json!({
        "rows": window,
        "rows_total": total,
        "truncated": truncated,
        "cursor": if truncated { Some(next.to_string()) } else { None },
    })
}

/// The plaintext decimal offset a `cursor` arg encodes (0 when absent).
pub fn args_offset(args: &serde_json::Value) -> usize {
    args_cursor(args).and_then(|c| c.parse().ok()).unwrap_or(0)
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
    /// Parameters keyed by the uid named in the artifact's args, joined
    /// with `nodes_by_uid` and/or the uid's incident edges
    /// (`edges_by_src` and `edges_by_dst`).
    Uid,
    /// Seed uids joined with incident edges valid at the instant, one
    /// unrolled join per hop, then the induced edges among the node set.
    UidExpansion,
    /// Valid-time band join: a version is routed to every bucket its
    /// `[vt_s, vt_e)` covers, an artifact to every bucket its window covers;
    /// each (version, artifact) pair is emitted once, at the later of the
    /// two start buckets.
    IntervalBucket,
    /// An artifact is routed to the bucket of each instant it reads
    /// (`t1`, `t2`), a version to every bucket it covers.
    InstantBucket,
    /// An edge event is routed to the bucket of its `vt_s`, an artifact to
    /// every bucket its window covers.
    EventBucket,
    /// Traversal along `edges_by_src` from the artifact's source: F11 by
    /// `iterate`, F12 by unrolled per-hop joins.
    Traversal,
    /// No store data (F13's control).
    None,
}
