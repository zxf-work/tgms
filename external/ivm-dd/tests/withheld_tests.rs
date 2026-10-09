//! Unit test for the withheld-correction check,
//! on a hand-built 11-batch bundle (no real synth-iv-60k cell is
//! available to this session -- see the crate README). Exercises
//! `withheld::run_withheld_check`'s slicing (`table_through(9)` for both
//! feeders at R10) and its false-fresh bookkeeping end to end, independent
//! of whether a real 37th cell ever lands.

use ivm_dd::export::{ArtifactSpec, CellBundle, ClosedEntry, DeltaRow, ExportManifest, OracleRow};
use ivm_dd::model::{Kind, VersionRow, OPEN_END};
use ivm_dd::withheld::{compute_hold, run_from_bundle};
use std::collections::BTreeMap;

fn node(vid: &str, uid: &str, vt_s: i64, vt_e: i64, tt_s: i64, tt_e: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Node, vid: vid.into(), uid: Some(uid.into()), eid: None, src: None, dst: None,
        rel_type: None, label: Some("P".into()), disc: None, vt_s, vt_e, tt_s, tt_e,
        props: serde_json::json!({}), source: Some("ingest".into()), provenance_ref: None,
    }
}

#[allow(clippy::too_many_arguments)] // test fixture helper mirroring VersionRow's own fields
fn edge(vid: &str, eid: &str, src: &str, dst: &str, vt_s: i64, vt_e: i64, tt_s: i64, tt_e: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Edge, vid: vid.into(), uid: None, eid: Some(eid.into()), src: Some(src.into()),
        dst: Some(dst.into()), rel_type: Some("R".into()), label: None, disc: Some("".into()),
        vt_s, vt_e, tt_s, tt_e, props: serde_json::json!({}), source: Some("ingest".into()),
        provenance_ref: None,
    }
}

/// A plain insert-only delta (epoch k inserts one more edge a->b at vt_s=k*10).
fn insert_delta(epoch: u64, vid: &str, eid: &str) -> DeltaRow {
    DeltaRow {
        epoch, tt: epoch as i64 * 10, correction_class: "A".into(), generator: "g".into(),
        placement: "p".into(), closed: vec![],
        inserted: vec![edge(vid, eid, "a", "c", epoch as i64 * 10, OPEN_END, epoch as i64 * 10, OPEN_END)],
    }
}

/// One artifact (`reach_from_a`), one delayed burst: batch 10 retracts
/// `a->b` (no replacement), delivered together with batch 11 -- the same
/// shape `withheld.rs`'s module docstring describes for the real 37th
/// cell, at unit-test scale. Shared by both tests below: the first checks
/// the snapshot (false-fresh / held) bookkeeping, the second checks the
/// hold-duration arithmetic that bookkeeping feeds.
fn single_held_artifact_bundle() -> CellBundle {
    // Base state: a->b believed open-ended, batch 10 will retract it.
    let base_edge = edge("e_ab", "eid_ab", "a", "b", 5, OPEN_END, 0, OPEN_END);
    let epoch0 = vec![node("n_a", "a", 0, OPEN_END, 0, OPEN_END), node("n_b", "b", 0, OPEN_END, 0, OPEN_END), base_edge];

    let mut deltas: Vec<DeltaRow> = (1..=9).map(|k| insert_delta(k, &format!("vfiller{k}"), &format!("efiller{k}"))).collect();
    // batch 10: retract a->b (no replacement) -- the withheld correction.
    deltas.push(DeltaRow {
        epoch: 10, tt: 100, correction_class: "D".into(), generator: "g".into(), placement: "p".into(),
        closed: vec![ClosedEntry { kind: Kind::Edge, vid: "e_ab".into(), tt_e: 100 }],
        inserted: vec![],
    });
    deltas.push(insert_delta(11, "vfiller11", "efiller11"));

    let artifacts = vec![ArtifactSpec {
        name: "reach_from_a".into(), op: "temporal_reachability".into(),
        args: serde_json::json!({"src": "a", "window": {"t_a": 0, "t_b": 1000}}),
    }];

    // oracle at epoch 10 says b is no longer reachable (the retraction landed).
    let mut digests10 = BTreeMap::new();
    digests10.insert("reach_from_a".to_string(), Some("ORACLE_EPOCH10_NO_B".to_string()));
    let oracle = vec![OracleRow { epoch: 10, digests: digests10, refused: vec![] }];

    let manifest = ExportManifest {
        cell_id: "withheld-unit-test".into(), cell_digest: "x".into(),
        config: serde_json::json!({}), files: BTreeMap::new(),
    };
    CellBundle { cell_id: "withheld-unit-test".into(), epoch0, artifacts, deltas, oracle, manifest }
}

#[test]
fn withheld_check_reads_the_pre_correction_table_under_both_feeders() {
    let bundle = single_held_artifact_bundle();

    let results = run_from_bundle(&bundle);
    assert_eq!(results.len(), 2);

    let f_epoch = results.iter().find(|r| r.feeder == "F-epoch").unwrap();
    assert!(f_epoch.probe_reports_complete_through_10);
    assert!(!f_epoch.signalled, "no staleness signal is expected under either feeder");
    // The served table is table_through(9), which still has a->b believed
    // -- so the artifact's served digest disagrees with the epoch-10
    // oracle digest, and the probe claims completeness: false-fresh.
    assert_eq!(f_epoch.false_fresh_count, 1);
    assert!(f_epoch.artifacts[0].false_fresh);
    // F-epoch's probe always claims completeness, so nothing is ever held.
    assert_eq!(f_epoch.held_count, 0);
    assert!(!f_epoch.artifacts[0].held);

    let f_watermark = results.iter().find(|r| r.feeder == "F-watermark").unwrap();
    assert!(!f_watermark.probe_reports_complete_through_10);
    // Same served value, but the probe does NOT claim completeness, so
    // nothing here is scored false-fresh (the point of the watermark feeder).
    assert_eq!(f_watermark.false_fresh_count, 0);
    assert!(!f_watermark.artifacts[0].false_fresh);
    // Instead it is correctly held (refused): the served value disagrees
    // with the epoch-10 oracle, but the probe never claims completeness.
    assert_eq!(f_watermark.held_count, 1);
    assert!(f_watermark.artifacts[0].held);

    // Both feeders serve the identical (pre-correction) value at R10.
    assert_eq!(f_epoch.artifacts[0].served_digest, f_watermark.artifacts[0].served_digest);
}

/// Memo P-EXT2-H: "hold = 1 burst by construction; ms ~= the inter-burst
/// wall." On the tiny one-artifact, one-delayed-burst fixture above
/// (batch 10 held, delivered together with batch 11), the single held
/// artifact's hold is exactly 1 burst, and its hold_ms is exactly the
/// (synthetic, caller-supplied) inter-burst wall -- never approximated,
/// never drifting from that input.
#[test]
fn watermark_hold_is_one_burst_for_the_single_held_artifact() {
    let bundle = single_held_artifact_bundle();
    let results = run_from_bundle(&bundle);
    let f_epoch = results.iter().find(|r| r.feeder == "F-epoch").unwrap();
    let f_watermark = results.iter().find(|r| r.feeder == "F-watermark").unwrap();

    let inter_burst_wall_ms = 42.0;
    let epoch_hold = compute_hold(f_epoch, 10, 11, inter_burst_wall_ms, 1);
    assert_eq!(epoch_hold.held_artifacts, 0, "F-epoch never holds anything");
    assert_eq!(epoch_hold.refused_answers, 0);
    assert_eq!(epoch_hold.hold_ms_median, 0.0);
    assert_eq!(epoch_hold.hold_bursts_median, 0.0);

    let watermark_hold = compute_hold(f_watermark, 10, 11, inter_burst_wall_ms, 1);
    assert_eq!(watermark_hold.held_artifacts, 1);
    assert_eq!(watermark_hold.refused_answers, 1, "one read point (R10) during the hold");
    assert_eq!(watermark_hold.held.len(), 1);
    assert_eq!(watermark_hold.held[0].name, "reach_from_a");
    assert_eq!(watermark_hold.held[0].hold_bursts, 1, "batch 10 held, delivered at batch 11");
    assert_eq!(watermark_hold.held[0].hold_ms, inter_burst_wall_ms);
    assert_eq!(watermark_hold.hold_bursts_median, 1.0);
    assert_eq!(watermark_hold.hold_ms_median, inter_burst_wall_ms);
    assert_eq!(watermark_hold.hold_ms_min, inter_burst_wall_ms);
    assert_eq!(watermark_hold.hold_ms_max, inter_burst_wall_ms);

    // Multiple read points during the hold window multiply refused
    // answers but never the hold duration itself (a property of the
    // burst schedule, not of how many times a reader asked).
    let watermark_hold_3_reads = compute_hold(f_watermark, 10, 11, inter_burst_wall_ms, 3);
    assert_eq!(watermark_hold_3_reads.held_artifacts, 1);
    assert_eq!(watermark_hold_3_reads.refused_answers, 3);
    assert_eq!(watermark_hold_3_reads.hold_ms_median, inter_burst_wall_ms);
}

#[test]
#[should_panic(expected = "delivered_epoch")]
fn compute_hold_rejects_a_delivered_epoch_not_after_the_held_epoch() {
    let bundle = single_held_artifact_bundle();
    let results = run_from_bundle(&bundle);
    let f_watermark = results.iter().find(|r| r.feeder == "F-watermark").unwrap();
    compute_hold(f_watermark, 10, 10, 1.0, 1);
}
