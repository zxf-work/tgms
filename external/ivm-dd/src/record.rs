//! `result.json` + `run.log` writer (memo §2.8's per-cell record shape,
//! adapted for DD per §2.8's own closing line: "The P-EXT2 README has the
//! same sections with crate versions, `Cargo.lock`/binary sha256, workers
//! and the two feeder configurations in place of the Neo4j items."). This
//! is this crate's own interpretation of that layout -- §4.1's shared field
//! names are used wherever they apply; nothing here is a frozen schema.

use crate::dataflow::RunOutcome;
use crate::export::CellBundle;
use serde_json::json;
use std::collections::HashMap;
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

fn median(xs: &mut [f64]) -> Option<f64> {
    if xs.is_empty() {
        return None;
    }
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = xs.len();
    Some(if n % 2 == 1 { xs[n / 2] } else { (xs[n / 2 - 1] + xs[n / 2]) / 2.0 })
}

/// Oracle agreement for the epochs covered (memo §2.6/§4.1): per epoch,
/// compares this run's sha256 result digest to the export's own oracle
/// digest (`oracle.jsonl`, itself TGMS's `result_digest`, memo §1.5) for
/// every artifact named in both. An artifact absent from this run's digest
/// map for an epoch where nothing changed is not a disagreement -- it is
/// looked up from the most recent epoch at which this run *did* emit a
/// digest for it (DD's own "unchanged, so no diff" semantics).
/// `(epoch, artifact_name, oracle_digest, our_digest)`.
pub type Disagreement = (u64, String, String, String);

pub fn oracle_agreement(
    bundle: &CellBundle,
    outcome: &RunOutcome,
) -> (usize, Vec<Disagreement>, usize) {
    let mut running: HashMap<String, String> = outcome.epoch0_digests.clone();
    let mut agree = 0usize;
    let mut disagree: Vec<Disagreement> = Vec::new();
    let mut not_answered = 0usize;

    if let Some(row0) = bundle.oracle.first() {
        for (name, oracle_digest) in &row0.digests {
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
        for (name, oracle_digest) in &oracle_row.digests {
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
    (agree, disagree, not_answered)
}

pub fn build_result_json(
    bundle: &CellBundle,
    outcome: &RunOutcome,
    versions: &Versions,
    host_start: &HostSnapshot,
    host_end: &HostSnapshot,
    deviations: &[&str],
) -> serde_json::Value {
    let (agree, disagree, not_answered) = oracle_agreement(bundle, outcome);

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
            "routing": "natural-key (uid / kind / global), not memo §3.2's 256-bucket valid-time routing -- see dataflow.rs module doc",
            "families_f12_routing": "Global route + reduce-closure DFS, not memo §3.3's four static unrolled joins -- see dataflow.rs module doc",
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
            })).collect::<Vec<_>>(), "disagree_count": disagree.len(), "not_answered": not_answered},
        },
        "deviations_from_memo": deviations,
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
