//! Unit test for the 37th-cell withheld-correction check (memo §3.5),
//! on a hand-built 11-batch bundle (no real synth-iv-60k cell is
//! available to this session -- see the crate README). Exercises
//! `withheld::run_withheld_check`'s slicing (`table_through(9)` for both
//! feeders at R10) and its false-fresh bookkeeping end to end, independent
//! of whether a real 37th cell ever lands.

use ivm_dd::export::{ArtifactSpec, CellBundle, ClosedEntry, DeltaRow, ExportManifest, OracleRow};
use ivm_dd::model::{Kind, VersionRow, OPEN_END};
use ivm_dd::withheld::run_from_bundle;
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

#[test]
fn withheld_check_reads_the_pre_correction_table_under_both_feeders() {
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
    let bundle = CellBundle { cell_id: "withheld-unit-test".into(), epoch0, artifacts, deltas, oracle, manifest };

    let results = run_from_bundle(&bundle);
    assert_eq!(results.len(), 2);

    let f_epoch = results.iter().find(|r| r.feeder == "F-epoch").unwrap();
    assert!(f_epoch.probe_reports_complete_through_10);
    assert!(!f_epoch.signalled, "memo: no staleness signal is expected under either feeder");
    // The served table is table_through(9), which still has a->b believed
    // -- so the artifact's served digest disagrees with the epoch-10
    // oracle digest, and the probe claims completeness: false-fresh.
    assert_eq!(f_epoch.false_fresh_count, 1);
    assert!(f_epoch.artifacts[0].false_fresh);

    let f_watermark = results.iter().find(|r| r.feeder == "F-watermark").unwrap();
    assert!(!f_watermark.probe_reports_complete_through_10);
    // Same served value, but the probe does NOT claim completeness, so
    // nothing here is scored false-fresh (memo §3.5's whole point).
    assert_eq!(f_watermark.false_fresh_count, 0);
    assert!(!f_watermark.artifacts[0].false_fresh);

    // Both feeders serve the identical (pre-correction) value at R10.
    assert_eq!(f_epoch.artifacts[0].served_digest, f_watermark.artifacts[0].served_digest);
}
