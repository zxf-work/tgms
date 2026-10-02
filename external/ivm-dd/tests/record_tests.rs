//! Unit test for `record::oracle_agreement`'s handling of an epoch's
//! `refused` list (`oracle.jsonl`'s per-epoch `refused: [name]`, see the
//! module-level NOTE in `src/export.rs`): TGMS's full-recompute oracle
//! carries forward the *previous* epoch's digest for a refused artifact
//! rather than a fresh one (`tests/fixtures/generate_tiny1.py` does the
//! same -- `refused.append(name); continue` before updating
//! `last_digest`), so comparing our fresh output against that stale
//! carried-forward value is meaningless and must not be scored as a
//! disagreement. `scripts/external_check.py` already excludes refused
//! names from its own independent tally (see its module docstring); this
//! test pins the crate's own `oracle_agreement` to the same semantics.

use ivm_dd::dataflow::{BurstMeasurement, RunOutcome};
use ivm_dd::export::{ArtifactSpec, CellBundle, ExportManifest, OracleRow};
use ivm_dd::families::Family;
use ivm_dd::record::oracle_agreement;
use std::collections::{BTreeMap, HashMap, HashSet};

fn bundle_with_oracle(oracle: Vec<OracleRow>) -> CellBundle {
    CellBundle {
        cell_id: "record-unit-test".into(),
        epoch0: vec![],
        artifacts: vec![ArtifactSpec {
            name: "a1".into(),
            op: "entity_history".into(),
            args: serde_json::json!({}),
        }],
        deltas: vec![],
        oracle,
        manifest: ExportManifest {
            cell_id: "record-unit-test".into(),
            cell_digest: "x".into(),
            config: serde_json::json!({}),
            files: BTreeMap::new(),
        },
    }
}

fn empty_outcome(epoch0_digests: HashMap<String, String>, bursts: Vec<BurstMeasurement>) -> RunOutcome {
    RunOutcome {
        load_ms: 0.0,
        epoch0_digests,
        bursts,
        families_present: HashSet::<Family>::new(),
        n_unmapped: HashMap::new(),
        process_rss_kb_end: None,
        valid_time_extent: (0, 1),
    }
}

/// A refused artifact's carried-forward oracle digest ("D0", stale) must
/// not be compared against this run's fresh digest ("D1") -- it would
/// otherwise register as a spurious disagreement. This is the defect the
/// checker lane flagged: no real cell has exercised it yet (every
/// `refused` list seen so far is empty), but a future expansion-budget
/// refusal in `temporal_paths` would hit exactly this path.
#[test]
fn refused_artifact_is_excluded_not_scored_as_disagreement() {
    let mut row0_digests = BTreeMap::new();
    row0_digests.insert("a1".to_string(), Some("D0".to_string()));
    let row0 = OracleRow { epoch: 0, digests: row0_digests, refused: vec![] };

    // Epoch 1: the TGMS oracle refused "a1" this epoch, so oracle.jsonl
    // still carries forward "D0" (not a fresh ground-truth digest) --
    // but ivm-dd's own maintained dataflow has no such budget and
    // produced a fresh "D1".
    let mut row1_digests = BTreeMap::new();
    row1_digests.insert("a1".to_string(), Some("D0".to_string()));
    let row1 = OracleRow { epoch: 1, digests: row1_digests, refused: vec!["a1".to_string()] };

    let bundle = bundle_with_oracle(vec![row0, row1]);

    let epoch0_digests: HashMap<String, String> = [("a1".to_string(), "D0".to_string())].into();
    let burst1 = BurstMeasurement {
        epoch: 1,
        tt: 10,
        correction_class: "A".into(),
        generator: "g".into(),
        placement: "p".into(),
        retracted_vt_span_sum_us: 0,
        retracted_vt_span_n_finite: 0,
        retracted_vt_span_n_open: 0,
        refresh_ms: 0.0,
        publish_ms: 0.0,
        artifacts_refreshed: vec!["a1".to_string()],
        digests: [("a1".to_string(), "D1".to_string())].into(),
    };
    let outcome = empty_outcome(epoch0_digests, vec![burst1]);

    let (agree, disagree, not_answered, oracle_refused) = oracle_agreement(&bundle, &outcome);

    assert_eq!(agree, 1, "epoch 0's a1 agrees (D0 == D0)");
    assert!(
        disagree.is_empty(),
        "epoch 1's refused a1 (stale D0 vs fresh D1) must not be scored as a \
         disagreement: got {disagree:?}"
    );
    assert_eq!(not_answered, 0);
    assert_eq!(oracle_refused, 1, "epoch 1's refused a1 must be tallied as oracle_refused");
}

/// With no refused names (the only case any real cell has exercised so
/// far), behavior is unchanged from before this fix: a genuine
/// disagreement is still scored as one.
#[test]
fn non_refused_disagreement_is_still_scored() {
    let mut row0_digests = BTreeMap::new();
    row0_digests.insert("a1".to_string(), Some("D0".to_string()));
    let row0 = OracleRow { epoch: 0, digests: row0_digests, refused: vec![] };

    let mut row1_digests = BTreeMap::new();
    row1_digests.insert("a1".to_string(), Some("D_ORACLE".to_string()));
    let row1 = OracleRow { epoch: 1, digests: row1_digests, refused: vec![] };

    let bundle = bundle_with_oracle(vec![row0, row1]);

    let epoch0_digests: HashMap<String, String> = [("a1".to_string(), "D0".to_string())].into();
    let burst1 = BurstMeasurement {
        epoch: 1,
        tt: 10,
        correction_class: "A".into(),
        generator: "g".into(),
        placement: "p".into(),
        retracted_vt_span_sum_us: 0,
        retracted_vt_span_n_finite: 0,
        retracted_vt_span_n_open: 0,
        refresh_ms: 0.0,
        publish_ms: 0.0,
        artifacts_refreshed: vec!["a1".to_string()],
        digests: [("a1".to_string(), "D_OURS".to_string())].into(),
    };
    let outcome = empty_outcome(epoch0_digests, vec![burst1]);

    let (agree, disagree, not_answered, oracle_refused) = oracle_agreement(&bundle, &outcome);

    assert_eq!(agree, 1);
    assert_eq!(disagree.len(), 1, "a genuine (non-refused) disagreement is still scored");
    assert_eq!(disagree[0], (1, "a1".to_string(), "D_ORACLE".to_string(), "D_OURS".to_string()));
    assert_eq!(not_answered, 0);
    assert_eq!(oracle_refused, 0);
}
