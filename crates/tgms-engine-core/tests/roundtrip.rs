//! Randomized round-trip: many batches through the real write path must be
//! byte-identical, on re-open, to an in-memory reference (WP-N2 acceptance).
//!
//! The engine path exercised here is the whole vertical slice — stage, sort,
//! route to lanes, split into segments, checksum, publish a generation,
//! re-open from `CURRENT`, scan back. The reference is a plain `Vec` of the
//! same rows filtered the obvious way. Anything the engine does cleverly
//! (pruning, binary-search bounds, elided `vt_e`, k-way merge) has to end up
//! agreeing with the naive version, or it is wrong.
//!
//! The generator is a seeded xorshift so a failure reproduces exactly; no
//! dependency, and no wall-clock or thread nondeterminism to chase.

use tgms_engine_core::derive::{edge_eid, version_vid, Id96};
use tgms_engine_core::manifest::EventLogRef;
use tgms_engine_core::row::{EdgeRow, Lane};
use tgms_engine_core::scan::{ScanRequest, ScanSet};
use tgms_engine_core::segment::{MemorySource, Segment};
use tgms_engine_core::staging::PartitionMap;
use tgms_engine_core::store::NativeStore;
use tgms_engine_core::{clamp_tt, OPEN_END};

struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Self(seed | 1)
    }
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    fn chance(&mut self, one_in: u64) -> bool {
        self.below(one_in) == 0
    }
}

const DAY: i64 = 24 * 60 * 60 * 1_000_000;
const RELS: [&str; 3] = ["SENT_MSG_TO", "RATED", "FOLLOWS"];

fn tmp_root(name: &str) -> std::path::PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!("tgms-roundtrip-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&p);
    p
}

/// Build a random store; returns the reference rows in write order.
fn build(root: &std::path::Path, seed: u64, batches: usize) -> Vec<EdgeRow> {
    let mut rng = Rng::new(seed);
    let mut store = NativeStore::open(root).unwrap();
    // a deliberately small segment target so multi-segment splitting happens
    store.set_layout(PartitionMap::new(0, 3 * DAY).unwrap(), 8 * 1024);

    let mut reference = Vec::new();
    let mut tt = 100i64;
    let mut counter = 0u32;

    for _ in 0..batches {
        tt += 1 + rng.below(5) as i64;
        store.begin(tt).unwrap();
        let n = 1 + rng.below(140);
        let mut staged = Vec::new();
        for _ in 0..n {
            let src = rng.below(23) as u32;
            let dst = rng.below(23) as u32;
            let uid_s = format!("n{src}");
            let uid_d = format!("n{dst}");
            let src_id = store.ensure_entity(&uid_s, "Node").unwrap();
            let dst_id = store.ensure_entity(&uid_d, "Node").unwrap();

            let rel = RELS[rng.below(3) as usize];
            let disc = format!("#{counter}");
            counter += 1;
            let vt_s = rng.below(40) as i64 * DAY / 4 + rng.below(1_000) as i64;
            // mostly instantaneous events, with some long-lived facts and a
            // few open-ended ones so both lanes and the vt_e column are hit
            let vt_e = if rng.chance(12) {
                OPEN_END
            } else if rng.chance(6) {
                vt_s + 1 + rng.below(30) as i64 * DAY
            } else {
                vt_s + 1
            };
            let eid = edge_eid(&uid_s, &uid_d, rel, &disc);
            let row = EdgeRow {
                vid: version_vid(&eid.to_hex(), tt, vt_s),
                src_id,
                dst_id,
                rel_type: rel.to_string(),
                disc,
                vt_s,
                vt_e,
                tt_s: tt,
                props: if rng.chance(3) {
                    "{}".to_string()
                } else {
                    format!(r#"{{"rating":{}}}"#, rng.below(21) as i64 - 10)
                },
                source: "ingest".to_string(),
                provenance_ref: if rng.chance(7) {
                    Some(format!("p-{}/s{}", rng.below(50), rng.below(4)))
                } else {
                    None
                },
            };
            staged.push(row.clone());
            store.stage_edge(row).unwrap();
        }

        // occasionally throw the batch away: a rolled-back batch must leave
        // no trace, so the reference simply never learns about it
        if rng.chance(9) {
            store.rollback().unwrap();
        } else {
            store.commit(EventLogRef::default()).unwrap();
            reference.extend(staged);
        }
    }
    reference
}

/// Open every segment the manifest lists, keeping lane tags for the scan.
fn open_segments(root: &std::path::Path) -> (NativeStore, Vec<(Segment<MemorySource>, Lane)>) {
    let store = NativeStore::open(root).unwrap();
    let m = store.manifest();
    let mut segs = Vec::new();
    for (lane, entries) in [
        (Lane::Event, &m.edge_lanes.event),
        (Lane::Interval, &m.edge_lanes.interval),
    ] {
        for e in entries {
            let path = root.join(&e.file);
            let src = MemorySource::load(&path).unwrap();
            segs.push((Segment::open(&path, src, true).unwrap(), lane));
        }
    }
    (store, segs)
}

fn reference_filter(rows: &[EdgeRow], req: &ScanRequest) -> Vec<(i64, Id96)> {
    let as_of = clamp_tt(req.as_of_tt);
    let mut v: Vec<(i64, Id96)> = rows
        .iter()
        .filter(|r| r.tt_s <= as_of)
        .filter(|r| req.vt_min.is_none_or(|t| r.vt_e > t))
        .filter(|r| req.vt_max.is_none_or(|t| r.vt_s < t))
        .filter(|r| {
            req.rel_types
                .as_ref()
                .is_none_or(|rs| rs.contains(&r.rel_type))
        })
        .filter(|r| {
            req.touching_ids
                .as_ref()
                .is_none_or(|ids| ids.contains(&r.src_id) || ids.contains(&r.dst_id))
        })
        .map(|r| (r.vt_s, r.vid))
        .collect();
    v.sort();
    if let Some(n) = req.limit {
        v.truncate(n);
    }
    v
}

fn scan(segs: &[(Segment<MemorySource>, Lane)], req: &ScanRequest) -> Vec<(i64, Id96)> {
    let set = ScanSet::from_pairs(segs.iter().map(|(s, l)| (s, *l)).collect());
    let (sel, _) = set.select(req).unwrap();
    set.merged(&sel, req.limit)
        .unwrap()
        .into_iter()
        .map(|(si, row)| {
            let seg = &segs[si].0;
            (
                seg.i64_column("vt_s").unwrap()[row as usize],
                seg.vid_at(row as usize).unwrap(),
            )
        })
        .collect()
}

#[test]
fn every_committed_row_survives_the_round_trip() {
    for seed in [1u64, 7, 42, 1337, 90210] {
        let root = tmp_root(&format!("all-{seed}"));
        let reference = build(&root, seed, 25);
        let (store, segs) = open_segments(&root);

        let got = scan(&segs, &ScanRequest::current());
        let want = reference_filter(&reference, &ScanRequest::current());

        assert_eq!(
            got.len(),
            reference.len(),
            "seed {seed}: row count changed in the round trip"
        );
        assert_eq!(got, want, "seed {seed}: content or order differs");
        assert_eq!(
            store.manifest().stats.n_edge_versions as usize,
            reference.len(),
            "seed {seed}: manifest statistics disagree with the data"
        );
        assert!(
            got.windows(2).all(|w| w[0] < w[1]),
            "seed {seed}: output is not strictly ordered"
        );
    }
}

#[test]
fn random_queries_agree_with_the_reference() {
    let root = tmp_root("queries");
    let reference = build(&root, 20260728, 30);
    let (_store, segs) = open_segments(&root);

    let mut rng = Rng::new(99);
    for case in 0..300 {
        let mut req = ScanRequest::current();
        if rng.chance(2) {
            let a = rng.below(12) as i64 * DAY / 4;
            let b = a + rng.below(10) as i64 * DAY / 4;
            req.vt_min = Some(a);
            req.vt_max = Some(b.max(a + 1));
        }
        if rng.chance(3) {
            req.rel_types = Some(vec![RELS[rng.below(3) as usize].to_string()]);
        }
        if rng.chance(3) {
            let ids: Vec<u32> = (0..1 + rng.below(4)).map(|_| rng.below(23) as u32).collect();
            req = req.touching(ids);
        }
        if rng.chance(4) {
            req.as_of_tt = 100 + rng.below(120) as i64;
        }
        if rng.chance(5) {
            req.limit = Some(1 + rng.below(50) as usize);
        }
        assert_eq!(
            scan(&segs, &req),
            reference_filter(&reference, &req),
            "case {case} disagreed for {req:?}"
        );
    }
}

#[test]
fn both_lanes_and_multiple_segments_are_actually_exercised() {
    // a property test that never hits the interesting paths proves nothing
    let root = tmp_root("coverage");
    build(&root, 5150, 25);
    let (store, segs) = open_segments(&root);
    let m = store.manifest();

    assert!(
        m.edge_lanes.event.len() > 1,
        "expected the event lane to split into several segments, got {}",
        m.edge_lanes.event.len()
    );
    assert!(
        !m.edge_lanes.interval.is_empty(),
        "expected some long-lived facts in the interval lane"
    );
    assert!(
        segs.iter().any(|(s, _)| s.header().vt_e_elided),
        "expected at least one all-instantaneous segment to elide vt_e"
    );
    assert!(
        segs.iter().any(|(s, _)| !s.header().vt_e_elided),
        "expected at least one segment to store vt_e explicitly"
    );
    assert!(m.generation > 1, "expected several published generations");
}

#[test]
fn a_reopened_store_sees_exactly_what_was_committed() {
    let root = tmp_root("reopen");
    let reference = build(&root, 31337, 20);

    // three independent opens must agree — nothing may depend on process state
    let mut counts = Vec::new();
    for _ in 0..3 {
        let (store, segs) = open_segments(&root);
        counts.push((
            store.generation(),
            store.manifest().stats.n_edge_versions,
            scan(&segs, &ScanRequest::current()).len(),
        ));
    }
    assert_eq!(counts[0], counts[1]);
    assert_eq!(counts[1], counts[2]);
    assert_eq!(counts[0].2, reference.len());
}
