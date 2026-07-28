//! Scan performance probe (WP-N2): how fast is the native read path, and
//! which parts would actually benefit from staying in Rust?
//!
//! Emits `key=value` lines so `scripts/engine_probe.py` can merge these
//! numbers with its NumPy counterpart into `docs/engine_probe.md`.
//!
//!   cargo run --release --example scan_probe -- [rows] [segments]

use std::time::Instant;

use tgms_engine_core::derive::{edge_eid, version_vid};
use tgms_engine_core::row::{EdgeRow, Lane};
use tgms_engine_core::scan::{ScanRequest, ScanSet};
use tgms_engine_core::segment::{
    write_edge_segment, MemorySource, MmapSource, Segment, SegmentSource, SegmentSpec,
};

const RELS: [&str; 3] = ["SENT_MSG_TO", "RATED", "FOLLOWS"];

fn synth(count: usize, batch: usize, tt: i64) -> Vec<EdgeRow> {
    let mut rows = Vec::with_capacity(count);
    for i in 0..count {
        let global = batch * count + i;
        let disc = format!("#{global}");
        let rel = RELS[global % 3];
        let src = (global % 5_000) as u32;
        let dst = ((global * 7 + 3) % 5_000) as u32;
        let vt_s = 1_600_000_000_000_000 + (global as i64) * 1_000;
        let eid = edge_eid("s", "d", rel, &disc);
        rows.push(EdgeRow {
            vid: version_vid(&eid.to_hex(), tt, vt_s),
            src_id: src,
            dst_id: dst,
            rel_type: rel.to_string(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s: tt,
            props: if global.is_multiple_of(4) {
                "{}".to_string()
            } else {
                r#"{"rating":3}"#.to_string()
            },
            source: "ingest".to_string(),
            provenance_ref: None,
        });
    }
    rows.sort_by_key(|r| r.sort_key());
    rows
}

fn time<T>(f: impl FnOnce() -> T) -> (T, f64) {
    let t0 = Instant::now();
    let out = f();
    (out, t0.elapsed().as_secs_f64() * 1e3)
}

fn scan_ms<S: SegmentSource>(segs: &[Segment<S>], req: &ScanRequest, reps: usize) -> (f64, u64) {
    let set = ScanSet::from_pairs(segs.iter().map(|s| (s, Lane::Event)).collect());
    // warm the page cache / branch predictors before measuring
    let _ = set.select(req).unwrap();
    let mut best = f64::MAX;
    let mut selected = 0;
    for _ in 0..reps {
        let ((sel, stats), ms) = time(|| set.select(req).unwrap());
        selected = stats.rows_selected;
        std::hint::black_box(&sel);
        best = best.min(ms);
    }
    (best, selected)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let per_segment: usize = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1_000_000);
    let n_segments: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1);
    let total = per_segment * n_segments;

    let dir = std::env::temp_dir().join(format!("tgms-probe-{}-{total}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();

    // --- build (staging cost is measured separately from writing) ------- //
    let mut build_ms = 0.0;
    let mut write_ms = 0.0;
    let mut paths = Vec::new();
    for b in 0..n_segments {
        let (rows, ms) = time(|| synth(per_segment, b, 100 + b as i64));
        build_ms += ms;
        let path = dir.join(format!("{b:06}.tgs"));
        let (_, ms) = time(|| write_edge_segment(&path, &rows, &SegmentSpec::default()).unwrap());
        write_ms += ms;
        paths.push(path);
    }
    let bytes: u64 = paths
        .iter()
        .map(|p| std::fs::metadata(p).unwrap().len())
        .sum();

    println!("rows={total}");
    println!("segments={n_segments}");
    println!("stage_build_ms={build_ms:.1}");
    println!("segment_write_ms={write_ms:.1}");
    println!("store_bytes={bytes}");
    println!("bytes_per_row={:.2}", bytes as f64 / total as f64);

    // --- open (this is where mmap vs read-into-memory shows up) --------- //
    let (mem_segs, mem_open_ms) = time(|| {
        paths
            .iter()
            .map(|p| Segment::open(p, MemorySource::load(p).unwrap(), false).unwrap())
            .collect::<Vec<_>>()
    });
    let (mmap_segs, mmap_open_ms) = time(|| {
        paths
            .iter()
            .map(|p| Segment::open(p, MmapSource::load(p).unwrap(), false).unwrap())
            .collect::<Vec<_>>()
    });
    let (_, verify_ms) = time(|| {
        paths
            .iter()
            .map(|p| Segment::open(p, MmapSource::load(p).unwrap(), true).unwrap())
            .collect::<Vec<_>>()
    });
    println!("open_buffered_ms={mem_open_ms:.2}");
    println!("open_mmap_ms={mmap_open_ms:.2}");
    println!("open_mmap_verified_ms={verify_ms:.2}");

    // --- scans ---------------------------------------------------------- //
    let reps = if total > 2_000_000 { 3 } else { 10 };
    let vt0 = 1_600_000_000_000_000i64;
    let span = total as i64 * 1_000;

    let (full_ms, full_rows) = scan_ms(&mmap_segs, &ScanRequest::current(), reps);
    println!("scan_full_ms={full_ms:.3}");
    println!("scan_full_rows={full_rows}");
    println!(
        "scan_full_rows_per_sec={:.0}",
        full_rows as f64 / (full_ms / 1e3)
    );

    let (mem_ms, _) = scan_ms(&mem_segs, &ScanRequest::current(), reps);
    println!("scan_full_buffered_ms={mem_ms:.3}");

    // 1% window — exercises zone-map pruning and the binary-search bounds
    let narrow = ScanRequest::current().window(vt0 + span / 2, vt0 + span / 2 + span / 100);
    let (narrow_ms, narrow_rows) = scan_ms(&mmap_segs, &narrow, reps);
    println!("scan_window1pct_ms={narrow_ms:.3}");
    println!("scan_window1pct_rows={narrow_rows}");

    let mut rel = ScanRequest::current();
    rel.rel_types = Some(vec!["RATED".to_string()]);
    let (rel_ms, rel_rows) = scan_ms(&mmap_segs, &rel, reps);
    println!("scan_reltype_ms={rel_ms:.3}");
    println!("scan_reltype_rows={rel_rows}");

    let touching = ScanRequest::current().touching((0..50).collect());
    let (touch_ms, touch_rows) = scan_ms(&mmap_segs, &touching, reps);
    println!("scan_incidence_ms={touch_ms:.3}");
    println!("scan_incidence_rows={touch_rows}");

    // full materialization to a struct-of-arrays, strings included: this is
    // what the public adapter boundary actually costs
    let set = ScanSet::from_pairs(mmap_segs.iter().map(|s| (s, Lane::Event)).collect());
    let ((cols, _), mat_ms) = time(|| set.materialize_edges(&narrow).unwrap());
    println!("materialize_window1pct_ms={mat_ms:.3}");
    println!("materialize_window1pct_rows={}", cols.len());

    let _ = std::fs::remove_dir_all(&dir);
}
