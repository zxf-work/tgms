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
    /// Memo P-EXT2-H (frozen 2026-10-09T14:30:27Z): true exactly when this
    /// artifact's served value (`table_through(9)`) disagrees with the
    /// epoch-10 oracle *and* the probe does not claim completeness --
    /// i.e. the watermark variant correctly refuses to answer rather than
    /// (like F-epoch) silently serving the stale value as fresh. Always
    /// `false` under F-epoch (`probe_complete` is always `true` there).
    pub held: bool,
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
    /// Count of `artifacts` with `held == true` (memo P-EXT2-H). Always 0
    /// under F-epoch.
    pub held_count: usize,
}

/// One held artifact's hold duration (memo P-EXT2-H). `hold_bursts` and
/// `hold_ms` are the same for every entry a given `compute_hold` call
/// produces -- the hold is a property of the *burst schedule* (how many
/// burst intervals elapse between the held read point and the delayed
/// batch's actual delivery), not of the individual artifact, since this
/// cell delays exactly one batch (10, delivered together with 11) for
/// every artifact alike. The per-artifact shape is kept anyway because
/// the task this computes for is stated per artifact, and a cell that
/// someday delays more than one batch would want distinct values here.
#[derive(Clone, Debug, PartialEq)]
pub struct HeldArtifactHold {
    pub name: String,
    pub hold_ms: f64,
    pub hold_bursts: u64,
}

/// Hold-duration statistics for one feeder's held artifacts.
#[derive(Clone, Debug, PartialEq)]
pub struct HoldStats {
    pub held: Vec<HeldArtifactHold>,
    pub held_artifacts: usize,
    /// Count of refused read attempts across the hold window: each held
    /// artifact refuses once per read point inside the hold
    /// (`reads_per_hold`, generically the number of distinct read
    /// attempts this check models during the hold -- 1 for this cell,
    /// which only evaluates the single read point R10).
    pub refused_answers: usize,
    pub hold_ms_median: f64,
    pub hold_ms_min: f64,
    pub hold_ms_max: f64,
    pub hold_bursts_median: f64,
}

fn median_f64(xs: &[f64]) -> f64 {
    if xs.is_empty() {
        return 0.0;
    }
    let mut v = xs.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 }
}

fn min_f64(xs: &[f64]) -> f64 {
    if xs.is_empty() { 0.0 } else { xs.iter().cloned().fold(f64::INFINITY, f64::min) }
}

fn max_f64(xs: &[f64]) -> f64 {
    if xs.is_empty() { 0.0 } else { xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max) }
}

/// Hold-duration statistics for `feeder`'s held artifacts (memo P-EXT2-H,
/// frozen 2026-10-09T14:30:27Z): "hold = 1 burst by construction; ms ~=
/// the inter-burst wall" -- `held_epoch` is the epoch whose batch was
/// delayed (10 for this cell), `delivered_epoch` is the epoch at which it
/// actually lands (11, delivered together with batch 11), and
/// `inter_burst_wall_ms` is the cell's own measured per-burst wall time
/// (median over a real timed run's bursts). `reads_per_hold` is the
/// number of distinct read attempts this check models during the hold
/// window (1 for this cell: only R10 is evaluated).
///
/// Always returns an empty/zeroed `HoldStats` for a feeder with no held
/// artifacts (F-epoch, always -- `ArtifactAtR10::held` is always `false`
/// there since its probe always claims completeness).
pub fn compute_hold(
    feeder: &FeederResult,
    held_epoch: u64,
    delivered_epoch: u64,
    inter_burst_wall_ms: f64,
    reads_per_hold: usize,
) -> HoldStats {
    assert!(
        delivered_epoch > held_epoch,
        "compute_hold: the delayed batch must be delivered at a later epoch than it was held \
         (held_epoch={held_epoch}, delivered_epoch={delivered_epoch})"
    );
    let hold_bursts = delivered_epoch - held_epoch;
    let hold_ms = hold_bursts as f64 * inter_burst_wall_ms;
    let held: Vec<HeldArtifactHold> = feeder
        .artifacts
        .iter()
        .filter(|a| a.held)
        .map(|a| HeldArtifactHold { name: a.name.clone(), hold_ms, hold_bursts })
        .collect();
    let held_artifacts = held.len();
    let refused_answers = held_artifacts * reads_per_hold;
    let ms_vals: Vec<f64> = held.iter().map(|h| h.hold_ms).collect();
    let bursts_vals: Vec<f64> = held.iter().map(|h| h.hold_bursts as f64).collect();
    HoldStats {
        held,
        held_artifacts,
        refused_answers,
        hold_ms_median: median_f64(&ms_vals),
        hold_ms_min: min_f64(&ms_vals),
        hold_ms_max: max_f64(&ms_vals),
        hold_bursts_median: median_f64(&bursts_vals),
    }
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
        let mut held_count = 0usize;
        for (name, (fam, args)) in &meta {
            let payload = served_payload(*fam, args, served);
            let canon = crate::digest::canonical_json(&crate::digest::canonicalize_floats(&payload));
            let served_digest = crate::digest::sha256_hex(&canon);
            let oracle_digest = oracle10.get(name).cloned().flatten();
            let disagrees_with_oracle10 =
                oracle_digest.as_deref().map(|od| od != served_digest).unwrap_or(false);
            // False-fresh *only* makes sense when the probe claims
            // completeness ("IVM false-fresh = artifacts whose
            // served value != oracle while the probe reports complete
            // through 10"). Under F-watermark the probe does not claim
            // completeness, so nothing here is scored false-fresh even
            // when the served value happens to disagree with the epoch-10
            // oracle -- that is the whole point of the signal.
            let false_fresh = probe_complete && disagrees_with_oracle10;
            // Held (memo P-EXT2-H): the mirror image of false-fresh. When
            // the probe does *not* claim completeness and the served
            // value would in fact disagree with the epoch-10 oracle, the
            // watermark variant is correctly refusing an answer it cannot
            // yet stand behind, rather than serving it silently wrong.
            let held = !probe_complete && disagrees_with_oracle10;
            if false_fresh {
                false_fresh_count += 1;
            }
            if held {
                held_count += 1;
            }
            artifacts_out.push(ArtifactAtR10 {
                name: name.clone(),
                served_digest,
                oracle_digest_epoch10: oracle_digest,
                false_fresh,
                held,
            });
        }
        out.push(FeederResult {
            feeder,
            probe_reports_complete_through_10: probe_complete,
            signalled: false,
            artifacts: artifacts_out,
            false_fresh_count,
            held_count,
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
