//! `ivm-dd run --state-report` (`dataflow::run_with_state_report`): the
//! arrangement logger it registers must change no output, and its
//! checkpoints must be taken where they say (after epoch 0 + registration,
//! after burst 1, at the end) with a non-zero arrangement record count.

use ivm_dd::{changelog, dataflow, export::CellBundle};
use std::collections::BTreeSet;
use std::path::Path;

#[test]
fn state_report_changes_no_output_and_takes_its_checkpoints() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/tiny1");
    if !dir.join("export-manifest.json").exists() {
        eprintln!("SKIP: tests/fixtures/tiny1/ not present yet.");
        return;
    }
    let bundle = CellBundle::load(&dir).expect("load tiny1 bundle");
    let plain = dataflow::run(changelog::build(&bundle), bundle.artifacts.clone());
    let reported = dataflow::run_with_state_report(changelog::build(&bundle), bundle.artifacts.clone());

    assert!(plain.state_checkpoints.is_empty());
    assert_eq!(plain.epoch0_digests, reported.epoch0_digests);
    assert_eq!(plain.bursts.len(), reported.bursts.len());
    for (a, b) in plain.bursts.iter().zip(reported.bursts.iter()) {
        assert_eq!(a.digests, b.digests);
        let sa: BTreeSet<_> = a.artifacts_refreshed.iter().collect();
        let sb: BTreeSet<_> = b.artifacts_refreshed.iter().collect();
        assert_eq!(sa, sb);
    }

    let labels: Vec<&str> = reported.state_checkpoints.iter().map(|c| c.label.as_str()).collect();
    let mut expected = vec!["after_load_and_registration"];
    if !reported.bursts.is_empty() {
        expected.push("after_burst_1");
    }
    expected.push("end");
    assert_eq!(labels, expected);
    for cp in &reported.state_checkpoints {
        assert!(cp.arranged_records.unwrap_or(0) > 0, "{}: no arranged records counted", cp.label);
        assert!(cp.arrangements_nonempty.unwrap_or(0) > 0);
        assert!(cp.arrangements_seen.unwrap_or(0) >= cp.arrangements_nonempty.unwrap_or(0));
        if cfg!(target_os = "linux") {
            assert!(cp.vm_rss_kb.unwrap_or(0) > 0);
            assert!(cp.vm_hwm_kb.unwrap_or(0) >= cp.vm_rss_kb.unwrap_or(0));
        }
    }
}
