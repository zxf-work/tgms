//! `result.json` + `run.log` writer: one record per exported cell, with the
//! crate versions, `Cargo.lock`/binary sha256, worker count, the routing
//! configuration, host snapshots, one row per burst (epoch, correction
//! class, generator, placement, `retracted_vt_span`, `refresh_ms`,
//! `publish_ms`, artifacts whose output changed) and the oracle agreement
//! gate. The field names are the ones shared with the other external
//! configuration's records; nothing here is a frozen schema.

use crate::dataflow::RunOutcome;
use crate::export::CellBundle;
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::path::Path;

pub const SCHEMA_VERSION: &str = "1.0.0";

pub struct Versions {
    pub crate_version: String,
    pub rustc: String,
    pub timely: String,
    pub differential_dataflow: String,
    pub cargo_lock_sha256: Option<String>,
    pub binary_sha256: Option<String>,
}

pub struct HostSnapshot {
    pub label: String,
    pub timestamp_utc: String,
    pub uname: String,
    pub loadavg1: Option<f64>,
    pub free_g: String,
}

pub fn median(xs: &mut [f64]) -> Option<f64> {
    if xs.is_empty() {
        return None;
    }
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = xs.len();
    Some(if n % 2 == 1 { xs[n / 2] } else { (xs[n / 2 - 1] + xs[n / 2]) / 2.0 })
}

/// Oracle agreement for the epochs covered: per epoch,
/// compares this run's sha256 result digest to the export's own oracle
/// digest (`oracle.jsonl`, itself TGMS's `result_digest`) for
/// every artifact named in both. An artifact absent from this run's digest
/// map for an epoch where nothing changed is not a disagreement -- it is
/// looked up from the most recent epoch at which this run *did* emit a
/// digest for it (DD's own "unchanged, so no diff" semantics).
///
/// A name in that epoch's own `refused` list is excluded from the
/// comparison entirely rather than counted as agree/disagree/not_answered:
/// TGMS's full-recompute oracle could not produce a ground-truth digest
/// for it this epoch, so `oracle.jsonl` carries forward the *previous*
/// epoch's digest for that name (not fresh ground truth, see
/// `tests/fixtures/generate_tiny1.py`) -- comparing our fresh output
/// against that stale carried-forward value would be meaningless. Such
/// names are tallied separately as `oracle_refused`, matching
/// `scripts/external_check.py`'s independent reconstruction.
/// `(epoch, artifact_name, oracle_digest, our_digest)`.
pub type Disagreement = (u64, String, String, String);

pub fn oracle_agreement(
    bundle: &CellBundle,
    outcome: &RunOutcome,
) -> (usize, Vec<Disagreement>, usize, usize) {
    let mut running: HashMap<String, String> = outcome.epoch0_digests.clone();
    let mut agree = 0usize;
    let mut disagree: Vec<Disagreement> = Vec::new();
    let mut not_answered = 0usize;
    let mut oracle_refused = 0usize;

    if let Some(row0) = bundle.oracle.first() {
        let refused: HashSet<&str> = row0.refused.iter().map(|s| s.as_str()).collect();
        for (name, oracle_digest) in &row0.digests {
            if refused.contains(name.as_str()) {
                oracle_refused += 1;
                continue;
            }
            match (oracle_digest, running.get(name)) {
                (Some(od), Some(ours)) => {
                    if od == ours {
                        agree += 1;
                    } else {
                        disagree.push((0, name.clone(), od.clone(), ours.clone()));
                    }
                }
                (None, _) => {}
                (Some(_), None) => not_answered += 1,
            }
        }
    }

    for (burst, oracle_row) in outcome.bursts.iter().zip(bundle.oracle.iter().skip(1)) {
        for (name, digest) in &burst.digests {
            running.insert(name.clone(), digest.clone());
        }
        let refused: HashSet<&str> = oracle_row.refused.iter().map(|s| s.as_str()).collect();
        for (name, oracle_digest) in &oracle_row.digests {
            if refused.contains(name.as_str()) {
                oracle_refused += 1;
                continue;
            }
            match (oracle_digest, running.get(name)) {
                (Some(od), Some(ours)) => {
                    if od == ours {
                        agree += 1;
                    } else {
                        disagree.push((oracle_row.epoch, name.clone(), od.clone(), ours.clone()));
                    }
                }
                (None, _) => {}
                (Some(_), None) => not_answered += 1,
            }
        }
    }
    (agree, disagree, not_answered, oracle_refused)
}

pub fn build_result_json(
    bundle: &CellBundle,
    outcome: &RunOutcome,
    versions: &Versions,
    host_start: &HostSnapshot,
    host_end: &HostSnapshot,
    deviations: &[&str],
) -> serde_json::Value {
    let (agree, disagree, not_answered, oracle_refused) = oracle_agreement(bundle, outcome);

    let per_burst: Vec<serde_json::Value> = outcome
        .bursts
        .iter()
        .map(|b| {
            json!({
                "epoch": b.epoch, "tt": b.tt,
                "correction_class": b.correction_class, "generator": b.generator,
                "placement": b.placement,
                "retracted_vt_span": {
                    "sum_us": b.retracted_vt_span_sum_us,
                    "n_finite": b.retracted_vt_span_n_finite,
                    "n_open_ended": b.retracted_vt_span_n_open,
                },
                "refresh_ms": b.refresh_ms, "publish_ms": b.publish_ms,
                "refresh_plus_publish_ms": b.refresh_ms + b.publish_ms,
                "artifacts_refreshed": b.artifacts_refreshed,
                "artifacts_refreshed_count": b.artifacts_refreshed.len(),
            })
        })
        .collect();

    let mut refresh: Vec<f64> = outcome.bursts.iter().map(|b| b.refresh_ms).collect();
    let mut total: Vec<f64> = outcome.bursts.iter().map(|b| b.refresh_ms + b.publish_ms).collect();

    json!({
        "schema_version": SCHEMA_VERSION,
        "cell_id": bundle.cell_id,
        "cell_digest": bundle.manifest.cell_digest,
        "config": {
            "export_config": bundle.manifest.config,
            "workers": 1,
            "routing": {
                "valid_time_buckets": crate::views::B,
                "overflow_bucket": true,
                "valid_time_extent": [outcome.valid_time_extent.0, outcome.valid_time_extent.1],
                "interval_and_event_families": "F2 interval band join, F4 instant routing, F6/F7/F9/F10 event routing on valid-time buckets",
                "uid_families": "F1, F3, F5, F8 joined on uid with nodes_by_uid / incident edges",
                "traversal_families": "F11 iterate, F12 one join per hop, along edges_by_src",
            },
        },
        "versions": {
            "crate_version": versions.crate_version,
            "rustc": versions.rustc,
            "timely": versions.timely,
            "differential_dataflow": versions.differential_dataflow,
            "cargo_lock_sha256": versions.cargo_lock_sha256,
            "binary_sha256": versions.binary_sha256,
        },
        "host_snapshots": [host_snapshot_json(host_start), host_snapshot_json(host_end)],
        "load_ms": outcome.load_ms,
        "process_rss_kb_end": outcome.process_rss_kb_end,
        "families_present": outcome.families_present.iter().map(|f| f.label()).collect::<Vec<_>>(),
        "n_unmapped_artifacts_by_op": outcome.n_unmapped,
        "per_burst": per_burst,
        "totals": {
            "n_bursts": outcome.bursts.len(),
            "refresh_ms_median": median(&mut refresh),
            "refresh_plus_publish_ms_median": median(&mut total),
            "refresh_plus_publish_ms_sum": outcome.bursts.iter().map(|b| b.refresh_ms + b.publish_ms).sum::<f64>(),
        },
        "gates": {
            "oracle_agreement": {"agree": agree, "disagree": disagree.iter().map(|(e,n,o,m)| json!({
                "epoch": e, "artifact": n, "oracle_digest": o, "our_digest": m,
            })).collect::<Vec<_>>(), "disagree_count": disagree.len(), "not_answered": not_answered,
                "oracle_refused": oracle_refused},
        },
        "deviations": deviations,
    })
}

fn host_snapshot_json(h: &HostSnapshot) -> serde_json::Value {
    json!({
        "label": h.label, "timestamp_utc": h.timestamp_utc, "uname": h.uname,
        "loadavg1": h.loadavg1, "free_g": h.free_g,
    })
}

pub fn write_cell_output(
    out_dir: &Path,
    result: &serde_json::Value,
    log_lines: &[String],
) -> std::io::Result<()> {
    std::fs::create_dir_all(out_dir)?;
    std::fs::write(out_dir.join("result.json"), serde_json::to_string_pretty(result)? + "\n")?;
    std::fs::write(out_dir.join("run.log"), log_lines.join("\n") + "\n")?;
    Ok(())
}
