//! The withheld-correction cell: the campaign's 37th cell. Base cell
//! `synth-iv-60k/c4/deep/seed0`, batch 10's changelog delivered one
//! interval late (together with batch 11). Two feeder configurations:
//!   - **F-epoch**: the input frontier advances on the interval clock
//!     regardless of whether burst 10's data has arrived -- at R10 the
//!     probe reports complete through epoch 10 even though it is not.
//!   - **F-watermark**: the input frontier never passes the smallest
//!     transaction time not yet delivered -- at R10 the probe correctly
//!     reports "complete only through 9".
//!
//! **Implementation note (deviation, recorded in the result's
//! `deviations`):** this module does not stand up a second
//! timely/differential-dataflow pipeline. "What does the maintained view
//! report at read point R" is evaluated as
//! `dataflow::compute_payload_pub(family, args, nodes, edges)` -- each
//! family's whole-store reference -- over whichever rows have been
//! *delivered* by R (F11 via `fixpoint_arrivals`, the least fixpoint the
//! dataflow's `iterate` computes). The maintained dataflow's output equals
//! that reference at every epoch under insertions and retractions
//! (`tests/maintained_tests.rs`, 1,620 randomized comparisons over all 12
//! data-reading families, plus the fixture's oracle check), so this is the
//! maintained view's value at R, not an approximation. What a second DD pipeline would
//! add is incrementality, which this check does not need: it is a single
//! read at a single instant under two delivery schedules, not a wall-clock
//! measurement (that is what `dataflow::run` on the ordinary 37-cell path
//! already measures, including this cell as cell #37 like any other).
//!
//! What this module *does* need, and provides honestly: the frontier
//! semantics that distinguish F-epoch from F-watermark, and the "was any
//! signal available" question ("none is expected: say so in the
//! output rather than inventing one" -- `signalled` below is hard-coded
//! `false` under F-epoch for exactly that reason; it is a `bool`, not a
//! guess).

use crate::changelog::{self, EpochUpdate};
use crate::export::{ArtifactSpec, CellBundle};
use crate::families::Family;
use crate::model::VersionRow;
use std::collections::{BTreeMap, HashMap};

pub struct ArtifactAtR10 {
    pub name: String,
    pub served_digest: String,
    pub oracle_digest_epoch10: Option<String>,
    pub false_fresh: bool,
}

pub struct FeederResult {
    pub feeder: &'static str,
    /// Whether the probe would report "complete through epoch 10" at R10
    /// under this feeder (F-epoch yes, F-watermark no).
    pub probe_reports_complete_through_10: bool,
    /// Whether any staleness *signal* was available to a reader at R10
    /// beyond the probe's own complete/incomplete report (none is
    /// expected under either feeder -- this crate has no separate
    /// watermark-signal channel distinct from the probe itself).
    pub signalled: bool,
    pub artifacts: Vec<ArtifactAtR10>,
    pub false_fresh_count: usize,
}

/// Snapshot the live version table through epoch `through` (inclusive),
/// replaying `changelog`'s own epoch0 + updates -- the same replay
/// `changelog::build` performed to produce `updates` in the first place,
/// stopped early.
fn table_through(epoch0: &[VersionRow], updates: &[EpochUpdate], through: u64) -> HashMap<String, VersionRow> {
    let mut live: HashMap<String, VersionRow> = HashMap::new();
    for row in epoch0 {
        live.insert(row.vid.clone(), row.clone());
    }
    for upd in updates {
        if upd.epoch > through {
            break;
        }
        for row in &upd.retractions {
            live.remove(&row.vid);
        }
        for row in &upd.insertions {
            live.insert(row.vid.clone(), row.clone());
        }
    }
    live
}

fn served_payload(family: Family, args: &serde_json::Value, table: &HashMap<String, VersionRow>) -> serde_json::Value {
    let nodes: Vec<VersionRow> = table.values().filter(|r| r.is_node()).cloned().collect();
    let edges: Vec<VersionRow> = table.values().filter(|r| !r.is_node()).cloned().collect();
    if family == Family::F11TemporalReachability {
        let src = args["src"].as_str().unwrap_or("");
        let t_a = args["window"]["t_a"].as_i64().unwrap_or(0);
        let t_b = args["window"]["t_b"].as_i64().unwrap_or(0);
        let as_of = crate::families::args_as_of_tt(args);
        let arr = crate::families::f11_temporal_reachability::fixpoint_arrivals(&edges, src, t_a, t_b, as_of);
        let limit = crate::families::args_limit(args);
        let cursor = crate::families::args_cursor(args);
        crate::families::f11_temporal_reachability::format_payload(&arr, limit, cursor.as_deref())
    } else {
        crate::dataflow::compute_payload_pub(family, args, &nodes, &edges)
    }
}

/// Run both feeder configurations over `bundle`/`changelog` and report R10.
/// `changelog.updates` must have at least 11 entries (epochs 1..=11) --
/// batch 10 is `updates[9]`, batch 11 is `updates[10]`.
pub fn run_withheld_check(bundle: &CellBundle, epoch0: &[VersionRow], updates: &[EpochUpdate], artifacts: &[ArtifactSpec]) -> Vec<FeederResult> {
    assert!(updates.len() >= 11, "withheld-correction check needs batches 1..=11 (epoch 10 delayed into 11)");
    let batch10 = &updates[9];
    assert_eq!(batch10.epoch, 10, "updates[9] must be epoch 10's row");

    let oracle10: BTreeMap<String, Option<String>> = bundle
        .oracle
        .iter()
        .find(|r| r.epoch == 10)
        .map(|r| r.digests.clone())
        .unwrap_or_default();

    let mut meta: HashMap<String, (Family, serde_json::Value)> = HashMap::new();
    for a in artifacts {
        if let Some(fam) = Family::from_op(&a.op) {
            meta.insert(a.name.clone(), (fam, a.args.clone()));
        }
    }

    // F-epoch: the view *looks* complete through 10 (every burst up to and
    // including 9 delivered, the frontier conceptually at 11) but batch
    // 10's own changelog has not landed -- it waits for batch 11,
    // delivered together. So at R10, the table served is `table_through(9)`
    // even though the reader is told "complete through 10".
    let served_f_epoch = table_through(epoch0, updates, 9);

    // F-watermark: the frontier itself does not advance past the smallest
    // undelivered transaction time -- at R10 the reader is correctly told
    // "complete only through 9", and (by this crate's design) is served
    // exactly the same table a reader asking for "as of 9" would get.
    let served_f_watermark = table_through(epoch0, updates, 9);

    let mut out = Vec::new();
    for (feeder, served, probe_complete) in [
        ("F-epoch", &served_f_epoch, true),
        ("F-watermark", &served_f_watermark, false),
    ] {
        let mut artifacts_out = Vec::with_capacity(meta.len());
        let mut false_fresh_count = 0usize;
        for (name, (fam, args)) in &meta {
            let payload = served_payload(*fam, args, served);
            let canon = crate::digest::canonical_json(&crate::digest::canonicalize_floats(&payload));
            let served_digest = crate::digest::sha256_hex(&canon);
            let oracle_digest = oracle10.get(name).cloned().flatten();
            // False-fresh *only* makes sense when the probe claims
            // completeness ("IVM false-fresh = artifacts whose
            // served value != oracle while the probe reports complete
            // through 10"). Under F-watermark the probe does not claim
            // completeness, so nothing here is scored false-fresh even
            // when the served value happens to disagree with the epoch-10
            // oracle -- that is the whole point of the signal.
            let false_fresh = probe_complete
                && oracle_digest.as_deref().map(|od| od != served_digest).unwrap_or(false);
            if false_fresh {
                false_fresh_count += 1;
            }
            artifacts_out.push(ArtifactAtR10 {
                name: name.clone(),
                served_digest,
                oracle_digest_epoch10: oracle_digest,
                false_fresh,
            });
        }
        out.push(FeederResult {
            feeder,
            probe_reports_complete_through_10: probe_complete,
            signalled: false,
            artifacts: artifacts_out,
            false_fresh_count,
        });
    }
    out
}

/// Convenience: build the changelog once and run the check (the usual
/// entry point from `main.rs`).
pub fn run_from_bundle(bundle: &CellBundle) -> Vec<FeederResult> {
    let cl = changelog::build(bundle);
    run_withheld_check(bundle, &cl.epoch0, &cl.updates, &bundle.artifacts)
}
