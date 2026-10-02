//! Unit tests for the changelog encoder: a version is
//! inserted (+1) at the epoch of its `tt_s`, retracted (−1) at the epoch
//! whose transaction closed it, and a correction is exactly
//! retraction-of-superseded + insertion-of-corrected at the same epoch.
//! Hand-built fixtures (no files) so these are independent of the
//! export format and of any real cell.

use ivm_dd::changelog::build;
use ivm_dd::export::{ArtifactSpec, CellBundle, ClosedEntry, DeltaRow, ExportManifest, OracleRow};
use ivm_dd::model::{Kind, VersionRow, OPEN_END};
use std::collections::BTreeMap;

fn node(vid: &str, uid: &str, vt_s: i64, vt_e: i64, tt_s: i64, tt_e: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Node, vid: vid.into(), uid: Some(uid.into()), eid: None, src: None, dst: None,
        rel_type: None, label: Some("Person".into()), disc: None, vt_s, vt_e, tt_s, tt_e,
        props: serde_json::json!({}), source: Some("ingest".into()), provenance_ref: None,
    }
}

#[allow(clippy::too_many_arguments)] // test fixture helper mirroring VersionRow's own fields
fn edge(vid: &str, eid: &str, src: &str, dst: &str, vt_s: i64, vt_e: i64, tt_s: i64, tt_e: i64) -> VersionRow {
    VersionRow {
        kind: Kind::Edge, vid: vid.into(), uid: None, eid: Some(eid.into()), src: Some(src.into()),
        dst: Some(dst.into()), rel_type: Some("knows".into()), label: None, disc: Some("".into()),
        vt_s, vt_e, tt_s, tt_e, props: serde_json::json!({}), source: Some("ingest".into()),
        provenance_ref: None,
    }
}

fn empty_manifest() -> ExportManifest {
    ExportManifest {
        cell_id: "unit-test".into(), cell_digest: "deadbeef".into(),
        config: serde_json::json!({}), files: BTreeMap::new(),
    }
}

#[test]
fn epoch0_rows_pass_through_unchanged() {
    let epoch0 = vec![node("v1", "a", 0, OPEN_END, 0, OPEN_END)];
    let bundle = CellBundle {
        cell_id: "unit-test".into(), epoch0, artifacts: Vec::<ArtifactSpec>::new(),
        deltas: Vec::<DeltaRow>::new(), oracle: Vec::<OracleRow>::new(), manifest: empty_manifest(),
    };
    let cl = build(&bundle);
    assert_eq!(cl.epoch0.len(), 1);
    assert_eq!(cl.epoch0[0].vid, "v1");
    assert!(cl.updates.is_empty());
}

#[test]
fn a_correction_retracts_the_old_belief_and_inserts_the_new_one() {
    // v1 believed open-ended; epoch 1 closes its belief (tt_e=100) and a
    // new version v2 (the corrected valid-time) is inserted in its place.
    let old = node("v1", "a", 0, OPEN_END, 0, OPEN_END);
    let epoch0 = vec![old.clone()];
    let closed = ClosedEntry { kind: Kind::Node, vid: "v1".into(), tt_e: 100 };
    let inserted = node("v2", "a", 0, 50, 100, OPEN_END);
    let delta = DeltaRow {
        epoch: 1, tt: 100, correction_class: "A".into(), generator: "test".into(),
        placement: "test".into(), closed: vec![closed], inserted: vec![inserted.clone()],
    };
    let bundle = CellBundle {
        cell_id: "unit-test".into(), epoch0, artifacts: Vec::<ArtifactSpec>::new(),
        deltas: vec![delta], oracle: Vec::<OracleRow>::new(), manifest: empty_manifest(),
    };
    let cl = build(&bundle);
    assert_eq!(cl.updates.len(), 1);
    let upd = &cl.updates[0];

    // Retraction is the OLD row verbatim (tt_e still OPEN_END) -- a DD
    // consumer must cancel exactly what it saw inserted, not some
    // recomputed approximation of it.
    assert_eq!(upd.retractions.len(), 1);
    assert_eq!(upd.retractions[0].vid, "v1");
    assert_eq!(upd.retractions[0].tt_e, OPEN_END);

    // Insertions: the corrected v1 (new tt_e) plus the brand-new v2.
    assert_eq!(upd.insertions.len(), 2);
    let v1_new = upd.insertions.iter().find(|r| r.vid == "v1").unwrap();
    assert_eq!(v1_new.tt_e, 100);
    assert_eq!(v1_new.vt_s, old.vt_s); // only tt_e changed
    let v2 = upd.insertions.iter().find(|r| r.vid == "v2").unwrap();
    assert_eq!(v2.vt_s, 0);
    assert_eq!(v2.vt_e, 50);
}

#[test]
fn retracted_vt_span_is_the_closed_versions_old_valid_interval() {
    // Two versions closed in the same burst: one with a finite valid
    // interval (span 500), one still open-ended in valid time (reported
    // separately, never summed as a number).
    let v1 = node("v1", "a", 1_000, 1_500, 0, OPEN_END);
    let v2 = edge("v2", "e2", "a", "b", 2_000, OPEN_END, 0, OPEN_END);
    let epoch0 = vec![v1, v2];
    let delta = DeltaRow {
        epoch: 1, tt: 100, correction_class: "A".into(), generator: "test".into(),
        placement: "test".into(),
        closed: vec![
            ClosedEntry { kind: Kind::Node, vid: "v1".into(), tt_e: 100 },
            ClosedEntry { kind: Kind::Edge, vid: "v2".into(), tt_e: 100 },
        ],
        inserted: vec![],
    };
    let bundle = CellBundle {
        cell_id: "unit-test".into(), epoch0, artifacts: Vec::<ArtifactSpec>::new(),
        deltas: vec![delta], oracle: Vec::<OracleRow>::new(), manifest: empty_manifest(),
    };
    let cl = build(&bundle);
    let (sum, n_finite, n_open) = cl.updates[0].retracted_vt_span_summary();
    assert_eq!(sum, 500); // v1's [1000,1500)
    assert_eq!(n_finite, 1);
    assert_eq!(n_open, 1); // v2's vt_e is OPEN_END
}

#[test]
fn multiple_epochs_replay_in_order() {
    let v1 = node("v1", "a", 0, OPEN_END, 0, OPEN_END);
    let epoch0 = vec![v1];
    let d1 = DeltaRow {
        epoch: 1, tt: 10, correction_class: "A".into(), generator: "g".into(), placement: "p".into(),
        closed: vec![ClosedEntry { kind: Kind::Node, vid: "v1".into(), tt_e: 10 }],
        inserted: vec![node("v1b", "a", 0, OPEN_END, 10, OPEN_END)],
    };
    let d2 = DeltaRow {
        epoch: 2, tt: 20, correction_class: "A".into(), generator: "g".into(), placement: "p".into(),
        closed: vec![ClosedEntry { kind: Kind::Node, vid: "v1b".into(), tt_e: 20 }],
        inserted: vec![node("v1c", "a", 0, OPEN_END, 20, OPEN_END)],
    };
    let bundle = CellBundle {
        cell_id: "unit-test".into(), epoch0, artifacts: Vec::<ArtifactSpec>::new(),
        deltas: vec![d1, d2], oracle: Vec::<OracleRow>::new(), manifest: empty_manifest(),
    };
    let cl = build(&bundle);
    assert_eq!(cl.updates.len(), 2);
    // epoch 2 must retract v1b (the row epoch 1 *inserted*), not v1 again.
    assert_eq!(cl.updates[1].retractions[0].vid, "v1b");
    assert_eq!(cl.updates[1].retractions[0].tt_e, OPEN_END);
}

#[test]
#[should_panic(expected = "no prior state")]
fn closing_an_unknown_vid_panics_loudly() {
    let bundle = CellBundle {
        cell_id: "unit-test".into(), epoch0: vec![], artifacts: Vec::<ArtifactSpec>::new(),
        deltas: vec![DeltaRow {
            epoch: 1, tt: 1, correction_class: "A".into(), generator: "g".into(), placement: "p".into(),
            closed: vec![ClosedEntry { kind: Kind::Node, vid: "ghost".into(), tt_e: 1 }],
            inserted: vec![],
        }],
        oracle: Vec::<OracleRow>::new(), manifest: empty_manifest(),
    };
    build(&bundle);
}
