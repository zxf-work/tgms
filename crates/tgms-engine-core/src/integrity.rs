//! Full-mode integrity checking — the engine half of `tgms check` (A3).
//!
//! `NativeStore::verify_fast` answers "is every file this generation names
//! intact and does it hold what the manifest claims". That is the *fast*
//! question, and it is what the commit path's own durability argument rests
//! on. It is not the whole of integrity: a store can pass every checksum and
//! still be wrong about itself — a delta chain whose links are individually
//! sound but whose generations are not monotone, a row naming a dictionary
//! code the dictionary never grew to, two believed versions of one identity
//! overlapping in valid time. Those are the checks here.
//!
//! Everything in this module is **read-only**. Nothing opens a file for
//! writing, nothing truncates, nothing recovers. A torn or contradictory
//! store is *reported*, never repaired: the corruption-sweep lane needs an
//! oracle that observes, and a checker that quietly fixed what it found
//! would make every sweep a tautology.
//!
//! ## Memory
//!
//! Segments are opened one at a time through an mmap source and dropped
//! before the next, so no scan holds more than one segment's decoded columns.
//! The one structure that grows with the store is the per-identity interval
//! map the overlap check folds into, which holds one `(vt_s, vt_e)` pair per
//! *currently believed* row — superseded history costs nothing. That is the
//! honest bound: proportional to the live version count, not to the segment
//! bytes.

use std::collections::HashMap;
use std::path::Path;

use crate::derive::{edge_eid, Id96};
use crate::dict::Dictionary;
use crate::error::Result;
use crate::manifest::Manifest;
use crate::manifest_chain::{self, ManifestRecord};
use crate::segment::{MmapSource, Segment, NULL_REF};
use crate::store::segment_id_of;
use crate::visibility::CloseIndex;
use crate::OPEN_END;

// --- the taxonomy -------------------------------------------------------- #
//
// `layer` says which subsystem owns the defect and `kind` says what shape it
// is. Both are closed vocabularies of stable tokens, because the sweep lane
// matches on them: prose belongs in `detail`, which may be reworded freely.

pub const LAYER_MANIFEST: &str = "manifest";
pub const LAYER_SEGMENT: &str = "segment";
pub const LAYER_CLOSE: &str = "close";
pub const LAYER_DICT: &str = "dict";
pub const LAYER_ROW: &str = "row";

/// A finding that makes the store untrustworthy: `tgms check` exits nonzero.
pub const SEVERITY_ERROR: &str = "error";
/// Something worth saying that does not condemn the store — a disposable
/// cache that will be rebuilt, a layout number worth watching. Reported,
/// listed, and deliberately *not* counted towards the exit code, so the
/// sweep lane's oracle does not fire on every store that has written since
/// its last index build.
pub const SEVERITY_ADVISORY: &str = "advisory";

/// One structured observation. `{layer, kind, path, generation, detail}` is
/// the shape A4's corruption sweep matches against; `severity` rides along
/// so an advisory never has to be distinguished by reading its prose.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finding {
    pub layer: &'static str,
    pub kind: &'static str,
    /// Store-relative path of the file the finding is about, or empty when
    /// the finding is about the store as a whole.
    pub path: String,
    pub generation: u64,
    pub detail: String,
    pub severity: &'static str,
}

impl Finding {
    pub fn error(
        layer: &'static str,
        kind: &'static str,
        path: impl Into<String>,
        generation: u64,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            layer,
            kind,
            path: path.into(),
            generation,
            detail: detail.into(),
            severity: SEVERITY_ERROR,
        }
    }

    pub fn advisory(
        layer: &'static str,
        kind: &'static str,
        path: impl Into<String>,
        generation: u64,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            severity: SEVERITY_ADVISORY,
            ..Self::error(layer, kind, path, generation, detail)
        }
    }

    pub fn is_error(&self) -> bool {
        self.severity == SEVERITY_ERROR
    }
}

// --- the manifest parent chain ------------------------------------------- #

/// Walk every retained manifest at or below `current` and check that the
/// chain they form is monotone and correctly linked.
///
/// `NativeStore::verify_fast` already reconstructs `current` from disk, which
/// checks the links *on the path it walks* — from `current` down to the
/// nearest checkpoint. This walks the whole retained window instead, which
/// catches two things that path structurally cannot: a defect in a
/// generation below the checkpoint (never read by a reconstruction) and a
/// non-monotonicity between two adjacent generations (each link is fine, the
/// sequence is not).
///
/// Generations *above* `current` are skipped on purpose. A crash between the
/// manifest write and the `CURRENT` flip leaves exactly one such file, and
/// the whole commit protocol rests on it being ignorable — flagging it would
/// make a correctly-survived crash look like corruption.
///
/// Gaps are not flagged on their own: gc deletes manifests below the
/// retention window by design, so the *earliest* retained generation is
/// arbitrary. A gap only matters when something needs the missing link, and
/// that is exactly what `parent-missing` reports.
pub fn parent_chain(root: &Path, current: u64) -> Vec<Finding> {
    let mut findings = Vec::new();
    let mut present: Vec<u64> = match std::fs::read_dir(root.join("manifests")) {
        Ok(rd) => rd
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                e.file_name()
                    .to_str()
                    .and_then(|n| n.strip_suffix(".json"))
                    .and_then(|n| n.parse::<u64>().ok())
            })
            .filter(|g| *g <= current)
            .collect(),
        Err(e) => {
            findings.push(Finding::error(
                LAYER_MANIFEST,
                "manifests-unreadable",
                "manifests",
                current,
                format!("manifest directory cannot be listed: {e}"),
            ));
            return findings;
        }
    };
    present.sort_unstable();

    // Every retained record, parsed once. `read_record` verifies each
    // record's own sha (a checkpoint through `Manifest::verify`, a delta
    // through `verify_self`) and that it is filed under the generation it
    // claims, so a self-inconsistent record is caught here even when nothing
    // ever reconstructs through it.
    let mut records: HashMap<u64, ManifestRecord> = HashMap::new();
    for g in &present {
        let rel = format!("manifests/{g:020}.json");
        match manifest_chain::read_record(root, *g) {
            Ok(rec) => {
                records.insert(*g, rec);
            }
            Err(e) => findings.push(Finding::error(
                LAYER_MANIFEST,
                "manifest-unreadable",
                rel,
                *g,
                format!("generation {g}: {}", e.message),
            )),
        }
    }

    let mut prev: Option<(u64, &ManifestRecord)> = None;
    for g in &present {
        let Some(rec) = records.get(g) else { continue };
        let rel = format!("manifests/{g:020}.json");

        if let ManifestRecord::Delta(d) = rec {
            if d.parent + 1 != d.generation {
                findings.push(Finding::error(
                    LAYER_MANIFEST,
                    "parent-not-predecessor",
                    rel.clone(),
                    *g,
                    format!(
                        "generation {g} names parent {} rather than {}",
                        d.parent,
                        g.saturating_sub(1)
                    ),
                ));
            }
            match records.get(&d.parent) {
                Some(p) => {
                    if p.manifest_sha() != d.parent_sha {
                        findings.push(Finding::error(
                            LAYER_MANIFEST,
                            "parent-sha-mismatch",
                            rel.clone(),
                            *g,
                            format!(
                                "generation {g} names parent sha {} but generation {} \
                                 records {}",
                                d.parent_sha,
                                d.parent,
                                p.manifest_sha()
                            ),
                        ));
                    }
                }
                None => findings.push(Finding::error(
                    LAYER_MANIFEST,
                    "parent-missing",
                    rel.clone(),
                    *g,
                    format!(
                        "generation {g} is a delta against generation {}, which is not \
                         on disk",
                        d.parent
                    ),
                )),
            }
        }

        // Monotonicity between adjacent retained generations. Each of these
        // is a counter the commit path only ever advances; a sequence that
        // walks one backwards is a reordered or rewritten chain even when
        // every individual record still hashes correctly.
        if let Some((pg, p)) = prev {
            if pg + 1 == *g {
                let (p_tt, p_next, p_off) = record_counters(p);
                let (c_tt, c_next, c_off) = record_counters(rec);
                for (name, before, after) in [
                    ("created_tt", p_tt, c_tt),
                    ("next_segment_id", p_next as i64, c_next as i64),
                    ("event_log.offset", p_off as i64, c_off as i64),
                ] {
                    if after < before {
                        findings.push(Finding::error(
                            LAYER_MANIFEST,
                            "chain-not-monotone",
                            rel.clone(),
                            *g,
                            format!(
                                "generation {g} moves {name} backwards: {before} at \
                                 generation {pg}, {after} here"
                            ),
                        ));
                    }
                }
            }
        }
        prev = Some((*g, rec));
    }
    findings
}

/// `(created_tt, next_segment_id, event_log.offset)` — the three counters
/// both record shapes carry and neither may ever walk backwards.
fn record_counters(rec: &ManifestRecord) -> (i64, u64, u64) {
    match rec {
        ManifestRecord::Checkpoint(m) => (m.created_tt, m.next_segment_id, m.event_log.offset),
        ManifestRecord::Delta(d) => (d.created_tt, d.next_segment_id, d.event_log.offset),
    }
}

// --- rows, dictionary codes and interval invariants ---------------------- #

/// What a row walk covered, for the report's counts.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RowScan {
    pub rows_walked: u64,
    pub believed_rows: u64,
    pub identities_checked: u64,
}

/// Walk every live segment's rows, checking three things the fast pass never
/// looks inside a segment for:
///
/// 1. **Dictionary-code reference validity.** Every `src_id` / `dst_id` /
///    `uid_id` a row carries must name a record the dictionary holds *up to
///    the manifest's committed count* — not up to whatever the file happens
///    to be long enough for. A row pointing past the committed prefix is a
///    segment published against a dictionary tail that never was.
/// 2. **Per-row bitemporal sanity.** `vt_s < vt_e`, and a closed row's
///    `tt_s < tt_e`.
/// 3. **Disjointness (I1).** No two *currently believed* versions of one
///    identity may overlap in valid time. This is the invariant
///    `tests/test_storage_invariants.py` proves the write path maintains;
///    here it is checked against what is actually on disk, which is the only
///    place a bit-flip or a hand-edited segment could break it.
///
/// `closes` must be the close index this generation makes visible; a row is
/// believed when neither a close run nor its segment's folded sidecar has
/// closed it.
pub fn check_rows(
    root: &Path,
    manifest: &Manifest,
    dict: &Dictionary,
    closes: &CloseIndex,
) -> Result<(RowScan, Vec<Finding>)> {
    let mut findings = Vec::new();
    let mut scan = RowScan::default();
    let generation = manifest.generation;
    let dict_records = manifest.dict.records;

    // Believed intervals per identity, folded across every segment. Edges key
    // on the derived 96-bit eid (the same identity `read.rs` indexes on);
    // nodes key on the dictionary code, which *is* the uid's identity — the
    // dictionary interns each uid exactly once.
    let mut edge_ivals: HashMap<Id96, Vec<(i64, i64)>> = HashMap::new();
    let mut node_ivals: HashMap<u32, Vec<(i64, i64)>> = HashMap::new();

    let edge_files: Vec<&str> = manifest
        .edge_lanes
        .event
        .iter()
        .chain(manifest.edge_lanes.interval.iter())
        .map(|e| e.file.as_str())
        .collect();

    for file in edge_files {
        let path = root.join(file);
        // Checksums were walked by the fast pass; re-walking them per row
        // scan would double the cost of `--full` for no new information, so
        // this open trusts them and reads the columns.
        let seg = match MmapSource::load(&path).and_then(|s| Segment::open(&path, s, false)) {
            Ok(seg) => seg,
            // the fast pass already reported this file; nothing to add
            Err(_) => continue,
        };
        let id = segment_id_of(file);
        check_edge_segment(
            &seg,
            id,
            file,
            generation,
            dict,
            dict_records,
            closes,
            &mut edge_ivals,
            &mut scan,
            &mut findings,
        )?;
    }

    for entry in &manifest.node_store {
        let file = entry.file.as_str();
        let path = root.join(file);
        let seg = match MmapSource::load(&path).and_then(|s| Segment::open(&path, s, false)) {
            Ok(seg) => seg,
            Err(_) => continue,
        };
        let id = segment_id_of(file);
        check_node_segment(
            &seg,
            id,
            file,
            generation,
            dict_records,
            closes,
            &mut node_ivals,
            &mut scan,
            &mut findings,
        )?;
    }

    scan.identities_checked = (edge_ivals.len() + node_ivals.len()) as u64;
    overlaps("edge", generation, edge_ivals, &mut findings, |e: Id96| {
        e.to_hex()
    });
    overlaps("node", generation, node_ivals, &mut findings, |c: u32| {
        dict.uid(c).unwrap_or("<unknown>").to_string()
    });
    Ok((scan, findings))
}

#[allow(clippy::too_many_arguments)]
fn check_edge_segment(
    seg: &Segment<MmapSource>,
    id: u64,
    file: &str,
    generation: u64,
    dict: &Dictionary,
    dict_records: u32,
    closes: &CloseIndex,
    ivals: &mut HashMap<Id96, Vec<(i64, i64)>>,
    scan: &mut RowScan,
    findings: &mut Vec<Finding>,
) -> Result<()> {
    let h = seg.header();
    let strings = seg.strings()?;
    let n_strings = strings.count();
    let sidecar = seg.sidecar();
    let vt_s = seg.i64_column("vt_s")?;
    let src = seg.u32_column("src_id")?;
    let dst = seg.u32_column("dst_id")?;
    let rel = seg.u16_column("rel_code")?;
    let disc = seg.u32_column("disc_ref")?;
    let props = seg.u32_column("props_ref")?;
    let source = seg.u32_column("source_ref")?;
    let prov = seg.u32_column("prov_ref")?;

    for i in 0..vt_s.len() {
        scan.rows_walked += 1;
        let row = i as u32;
        let mut resolvable = true;
        for (col, code) in [("src_id", src[i]), ("dst_id", dst[i])] {
            if code >= dict_records {
                resolvable = false;
                findings.push(Finding::error(
                    LAYER_DICT,
                    "dict-code-out-of-range",
                    file,
                    generation,
                    format!(
                        "{file} row {row}: {col} names dictionary code {code}, but the \
                         manifest commits only {dict_records} records"
                    ),
                ));
            }
        }
        if rel[i] as usize >= h.rel_types.len() {
            resolvable = false;
            findings.push(Finding::error(
                LAYER_SEGMENT,
                "rel-code-out-of-range",
                file,
                generation,
                format!(
                    "{file} row {row}: rel_code {} has no entry among the segment's {} \
                     rel_types",
                    rel[i],
                    h.rel_types.len()
                ),
            ));
        }
        for (col, r) in [
            ("disc_ref", disc[i]),
            ("props_ref", props[i]),
            ("source_ref", source[i]),
            ("prov_ref", prov[i]),
        ] {
            if r != NULL_REF && r >= n_strings {
                if col == "disc_ref" {
                    resolvable = false;
                }
                findings.push(Finding::error(
                    LAYER_SEGMENT,
                    "string-ref-out-of-range",
                    file,
                    generation,
                    format!(
                        "{file} row {row}: {col} names string {r} of {n_strings} in this \
                         segment's heap"
                    ),
                ));
            }
        }

        let vt_e = seg.vt_e_at(i)?;
        let tt_e = row_tt_e(closes, &sidecar, id, row);
        row_intervals(
            file,
            generation,
            row,
            vt_s[i],
            vt_e,
            h.tt_s_at(row)?,
            tt_e,
            findings,
        );

        if tt_e == OPEN_END {
            scan.believed_rows += 1;
            if resolvable {
                let eid = edge_eid(
                    dict.uid(src[i]).unwrap_or_default(),
                    dict.uid(dst[i]).unwrap_or_default(),
                    h.rel_types[rel[i] as usize].as_str(),
                    strings.get(disc[i])?,
                );
                ivals.entry(eid).or_default().push((vt_s[i], vt_e));
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn check_node_segment(
    seg: &Segment<MmapSource>,
    id: u64,
    file: &str,
    generation: u64,
    dict_records: u32,
    closes: &CloseIndex,
    ivals: &mut HashMap<u32, Vec<(i64, i64)>>,
    scan: &mut RowScan,
    findings: &mut Vec<Finding>,
) -> Result<()> {
    let h = seg.header();
    let sidecar = seg.sidecar();
    let vt_s = seg.i64_column("vt_s")?;
    let uid = seg.u32_column("uid_id")?;

    for i in 0..vt_s.len() {
        scan.rows_walked += 1;
        let row = i as u32;
        let mut resolvable = true;
        if uid[i] >= dict_records {
            resolvable = false;
            findings.push(Finding::error(
                LAYER_DICT,
                "dict-code-out-of-range",
                file,
                generation,
                format!(
                    "{file} row {row}: uid_id names dictionary code {}, but the manifest \
                     commits only {dict_records} records",
                    uid[i]
                ),
            ));
        }
        let vt_e = seg.vt_e_at(i)?;
        let tt_e = row_tt_e(closes, &sidecar, id, row);
        row_intervals(
            file,
            generation,
            row,
            vt_s[i],
            vt_e,
            h.tt_s_at(row)?,
            tt_e,
            findings,
        );
        if tt_e == OPEN_END {
            scan.believed_rows += 1;
            if resolvable {
                ivals.entry(uid[i]).or_default().push((vt_s[i], vt_e));
            }
        }
    }
    Ok(())
}

/// A close run outranks a folded sidecar entry — the same precedence
/// `read.rs::edge_rows_sel` materializes rows with, so verify and the read
/// path can never disagree about which rows are believed.
fn row_tt_e(closes: &CloseIndex, sidecar: &crate::visibility::Sidecar<'_>, id: u64, row: u32) -> i64 {
    let from_run = closes.tt_e(id, row);
    if from_run != OPEN_END {
        from_run
    } else {
        sidecar.tt_e(row)
    }
}

#[allow(clippy::too_many_arguments)]
fn row_intervals(
    file: &str,
    generation: u64,
    row: u32,
    vt_s: i64,
    vt_e: i64,
    tt_s: i64,
    tt_e: i64,
    findings: &mut Vec<Finding>,
) {
    if vt_s >= vt_e {
        findings.push(Finding::error(
            LAYER_ROW,
            "valid-interval-empty",
            file,
            generation,
            format!("{file} row {row}: vt_s {vt_s} is not before vt_e {vt_e}"),
        ));
    }
    if tt_e != OPEN_END && tt_s >= tt_e {
        findings.push(Finding::error(
            LAYER_ROW,
            "belief-interval-empty",
            file,
            generation,
            format!("{file} row {row}: tt_s {tt_s} is not before tt_e {tt_e}"),
        ));
    }
}

/// I1 across one identity's believed versions: sorted by `vt_s`, each
/// interval must end at or before the next one starts.
fn overlaps<K>(
    what: &'static str,
    generation: u64,
    ivals: HashMap<K, Vec<(i64, i64)>>,
    findings: &mut Vec<Finding>,
    name: impl Fn(K) -> String,
) where
    K: std::hash::Hash + Eq + Copy,
{
    // Sorted so a corrupt store reports the same findings in the same order
    // every run — a sweep that diffs two reports must not see churn.
    let mut hits: Vec<String> = Vec::new();
    for (key, mut ivs) in ivals {
        if ivs.len() < 2 {
            continue;
        }
        ivs.sort_unstable();
        for w in ivs.windows(2) {
            if w[0].1 > w[1].0 {
                hits.push(format!(
                    "{what} {}: believed versions [{}, {}) and [{}, {}) overlap in valid time",
                    name(key),
                    w[0].0,
                    w[0].1,
                    w[1].0,
                    w[1].1
                ));
            }
        }
    }
    hits.sort_unstable();
    for detail in hits {
        findings.push(Finding::error(
            LAYER_ROW,
            "believed-versions-overlap",
            "",
            generation,
            detail,
        ));
    }
}

/// Close records that address a row past the end of the segment they name.
///
/// Runs naming a segment this generation does not list are skipped rather
/// than flagged: compaction folds runs into sidecars and rewrites the
/// manifest, and a retained older run legitimately outlives the segment it
/// was written against.
pub fn check_close_targets(manifest: &Manifest, records: &[crate::visibility::CloseRecord]) -> Vec<Finding> {
    let rows: HashMap<u64, u32> = manifest
        .node_store
        .iter()
        .chain(manifest.edge_lanes.event.iter())
        .chain(manifest.edge_lanes.interval.iter())
        .map(|e| (segment_id_of(&e.file), e.rows))
        .collect();
    let mut findings = Vec::new();
    for r in records {
        if let Some(n) = rows.get(&r.segment) {
            if r.row >= *n {
                findings.push(Finding::error(
                    LAYER_CLOSE,
                    "close-row-out-of-range",
                    "",
                    manifest.generation,
                    format!(
                        "a close run closes row {} of segment {}, which holds {n} rows",
                        r.row, r.segment
                    ),
                ));
            }
        }
    }
    findings
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::version_vid;
    use crate::manifest::SegmentEntry;
    use crate::row::{EdgeRow, Lane};
    use crate::segment::{write_edge_segment, SegmentSpec};

    /// A hand-built store root: one edge segment, one dictionary, one
    /// manifest naming both. Deliberately assembled from the low-level
    /// writers rather than through `NativeStore`, because the defects below
    /// are exactly the ones the commit path exists to make unreachable —
    /// there is no way to produce them through `apply_ops`, and adding a
    /// hook that could would be a hole in the write path for the benefit of
    /// a test.
    struct Fixture {
        root: std::path::PathBuf,
        manifest: Manifest,
    }

    fn tmp_root(name: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-integrity-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        p
    }

    impl Fixture {
        fn new(name: &str, rows: &[EdgeRow], uids: &[&str], dict_records: Option<u32>) -> Self {
            let root = tmp_root(name);
            std::fs::create_dir_all(root.join("seg")).unwrap();
            let root = root.as_path();
            let file = "seg/000000000000.tgs";
            write_edge_segment(
                &root.join(file),
                rows,
                &SegmentSpec {
                    lane: Lane::Interval,
                    ..Default::default()
                },
            )
            .unwrap();
            let mut dict = Dictionary::open(root.join("dict.log"), 0, 0).unwrap();
            for u in uids {
                dict.ensure(u, "N").unwrap();
            }
            let (records, bytes) = dict.commit_to_disk().unwrap();

            let mut manifest = Manifest::genesis();
            manifest.generation = 1;
            manifest.parent = Some(0);
            manifest.dict.records = dict_records.unwrap_or(records);
            manifest.dict.bytes = bytes;
            manifest.edge_lanes.interval.push(SegmentEntry {
                file: file.into(),
                rows: rows.len() as u32,
                key_lo: (rows[0].vt_s, rows[0].vid.to_hex()),
                key_hi: (
                    rows[rows.len() - 1].vt_s,
                    rows[rows.len() - 1].vid.to_hex(),
                ),
                vt_min: 0,
                vt_max: 0,
                vt_e_max: 0,
                tt_s_min: 0,
                tt_s_max: 0,
                rel_codes: vec![0],
                n_closed_folded: 0,
                all_current: true,
                sha: String::new(),
            });
            manifest.next_segment_id = 1;
            manifest.seal();
            Self {
                root: root.to_path_buf(),
                manifest,
            }
        }

        fn check(&self) -> (RowScan, Vec<Finding>) {
            let dict = Dictionary::open(
                self.root.join("dict.log"),
                self.manifest.dict.records,
                self.manifest.dict.bytes,
            )
            .unwrap();
            check_rows(&self.root, &self.manifest, &dict, &CloseIndex::default()).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    fn edge(src: u32, dst: u32, vt_s: i64, vt_e: i64, disc: &str) -> EdgeRow {
        let eid = crate::derive::edge_eid("a", "b", "R", disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), 10, vt_s),
            src_id: src,
            dst_id: dst,
            rel_type: "R".into(),
            disc: disc.into(),
            vt_s,
            vt_e,
            tt_s: 10,
            props: "{}".into(),
            source: "test".into(),
            provenance_ref: None,
        }
    }

    #[test]
    fn a_sound_segment_yields_no_findings() {
        let f = Fixture::new(
            "sound",
            &[edge(0, 1, 0, 10, "#0"), edge(0, 1, 10, 20, "#0")],
            &["a", "b"],
            None,
        );
        let (scan, findings) = f.check();
        assert_eq!(scan.rows_walked, 2);
        assert_eq!(scan.believed_rows, 2);
        // abutting intervals are disjoint: [0,10) then [10,20)
        assert!(findings.is_empty(), "{findings:?}");
    }

    #[test]
    fn a_row_naming_a_code_past_the_committed_dictionary_is_a_finding() {
        // The dictionary commits one record and the row names code 1 — the
        // shape a segment published against a dictionary tail that never
        // reached a manifest leaves behind. Note the defect *has* to be
        // built this way round: a dictionary file longer than the manifest
        // commits is refused by `Dictionary::open` before verify runs at
        // all, so the reachable version of this fault is a row pointing past
        // the committed prefix, not a prefix that shrank under a row.
        let f = Fixture::new("dictcode", &[edge(0, 1, 0, 10, "#0")], &["a"], None);
        let (_, findings) = f.check();
        let hit = findings
            .iter()
            .find(|f| f.kind == "dict-code-out-of-range")
            .expect("the out-of-range code must be reported");
        assert_eq!(hit.layer, LAYER_DICT);
        assert!(hit.detail.contains("dst_id"), "{}", hit.detail);
        assert!(hit.is_error());
    }

    #[test]
    fn two_believed_versions_of_one_identity_may_not_overlap() {
        // same (src, dst, rel, disc) — one identity — with [0,10) and [5,15)
        // both open. `apply_ops` carves this apart; nothing else may.
        let f = Fixture::new(
            "overlap",
            &[edge(0, 1, 0, 10, "#0"), edge(0, 1, 5, 15, "#0")],
            &["a", "b"],
            None,
        );
        let (scan, findings) = f.check();
        assert_eq!(scan.identities_checked, 1);
        let hit = findings
            .iter()
            .find(|f| f.kind == "believed-versions-overlap")
            .expect("the overlap must be reported");
        assert_eq!(hit.layer, LAYER_ROW);
        assert!(hit.detail.contains("[0, 10)"), "{}", hit.detail);
        assert!(hit.detail.contains("[5, 15)"), "{}", hit.detail);
    }

    #[test]
    fn distinct_identities_may_overlap_freely() {
        // the same endpoints under two discriminators are two identities
        let f = Fixture::new(
            "distinct",
            &[edge(0, 1, 0, 10, "#0"), edge(0, 1, 5, 15, "#1")],
            &["a", "b"],
            None,
        );
        let (scan, findings) = f.check();
        assert_eq!(scan.identities_checked, 2);
        assert!(findings.is_empty(), "{findings:?}");
    }

    #[test]
    fn an_empty_valid_interval_is_a_finding() {
        let f = Fixture::new("emptyival", &[edge(0, 1, 10, 10, "#0")], &["a", "b"], None);
        let (_, findings) = f.check();
        let hit = findings
            .iter()
            .find(|f| f.kind == "valid-interval-empty")
            .expect("vt_s == vt_e must be reported");
        assert_eq!(hit.layer, LAYER_ROW);
    }

    // --- the parent-chain walk ------------------------------------------ #
    //
    // Built by hand rather than by committing through `NativeStore`, for the
    // same reason as the row fixtures above: the commit path exists to make
    // these shapes unreachable. Writing the documents directly is also what
    // lets each defect be isolated — a hand-edited file breaks its own sha
    // and would be reported as unreadable long before the link it was meant
    // to test was ever looked at.

    struct Chain(std::path::PathBuf);

    impl Drop for Chain {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    impl Chain {
        fn new(name: &str) -> Self {
            let root = tmp_root(name);
            std::fs::create_dir_all(root.join("manifests")).unwrap();
            Self(root)
        }

        fn write(&self, m: &Manifest) {
            std::fs::write(
                manifest_chain::manifest_path(&self.0, m.generation),
                manifest_chain::checkpoint_json(m),
            )
            .unwrap();
        }

        fn write_delta(&self, d: &manifest_chain::ManifestDelta) {
            std::fs::write(
                manifest_chain::manifest_path(&self.0, d.generation),
                d.to_json(),
            )
            .unwrap();
        }
    }

    fn gen_after(parent: &Manifest, created_tt: i64) -> Manifest {
        let mut m = parent.successor(created_tt);
        m.seal();
        m
    }

    #[test]
    fn a_sound_chain_of_checkpoints_walks_clean() {
        let c = Chain::new("chain-ok");
        let g0 = Manifest::genesis();
        let g1 = gen_after(&g0, 100);
        let g2 = gen_after(&g1, 200);
        for m in [&g0, &g1, &g2] {
            c.write(m);
        }
        assert!(parent_chain(&c.0, 2).is_empty());
    }

    #[test]
    fn a_generation_above_current_is_ignored() {
        // exactly what a crash between the manifest write and the CURRENT
        // flip leaves; the commit protocol depends on it being ignorable
        let c = Chain::new("chain-orphan");
        let g0 = Manifest::genesis();
        let g1 = gen_after(&g0, 100);
        c.write(&g0);
        c.write(&g1);
        assert!(parent_chain(&c.0, 0).is_empty());
    }

    #[test]
    fn a_corrupt_generation_below_the_checkpoint_is_still_walked() {
        // The value the whole walk exists for: with every generation a
        // checkpoint, reconstructing CURRENT reads exactly one file, so the
        // fast pass never looks at generation 1 at all.
        let c = Chain::new("chain-below");
        let g0 = Manifest::genesis();
        let g1 = gen_after(&g0, 100);
        let g2 = gen_after(&g1, 200);
        for m in [&g0, &g1, &g2] {
            c.write(m);
        }
        let mut doc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(manifest_chain::manifest_path(&c.0, 1)).unwrap())
                .unwrap();
        doc["stats"]["n_entities"] = serde_json::json!(7); // sha not recomputed
        std::fs::write(
            manifest_chain::manifest_path(&c.0, 1),
            serde_json::to_string_pretty(&doc).unwrap(),
        )
        .unwrap();

        let findings = parent_chain(&c.0, 2);
        let hit = findings
            .iter()
            .find(|f| f.kind == "manifest-unreadable")
            .expect("the tampered generation must be reported");
        assert_eq!(hit.generation, 1);
        assert_eq!(hit.layer, LAYER_MANIFEST);
    }

    #[test]
    fn a_delta_naming_the_wrong_parent_sha_is_reported() {
        let c = Chain::new("chain-parentsha");
        let g0 = Manifest::genesis();
        let g1 = gen_after(&g0, 100);
        c.write(&g0);
        let mut d = manifest_chain::ManifestDelta::between(&g0, &g1, 0)
            .expect("a successor is expressible as a delta");
        d.parent_sha = "0000000000000000".into();
        d.seal(); // self-consistent, and still wrong about its parent
        c.write_delta(&d);

        let findings = parent_chain(&c.0, 1);
        let hit = findings
            .iter()
            .find(|f| f.kind == "parent-sha-mismatch")
            .expect("the broken link must be reported");
        assert_eq!(hit.generation, 1);
    }

    #[test]
    fn a_chain_that_walks_a_counter_backwards_is_reported() {
        // every record self-consistent, every link correct, the sequence
        // wrong — the shape only a walk across generations can see
        let c = Chain::new("chain-monotone");
        let g0 = Manifest::genesis();
        let mut g1 = g0.successor(500);
        g1.seal();
        let mut g2 = g1.successor(100); // created_tt goes backwards
        g2.seal();
        for m in [&g0, &g1, &g2] {
            c.write(m);
        }
        let findings = parent_chain(&c.0, 2);
        let hit = findings
            .iter()
            .find(|f| f.kind == "chain-not-monotone")
            .expect("the backwards counter must be reported");
        assert!(hit.detail.contains("created_tt"), "{}", hit.detail);
        assert_eq!(hit.generation, 2);
    }

    #[test]
    fn a_delta_whose_parent_is_gone_is_reported() {
        let c = Chain::new("chain-gap");
        let g0 = Manifest::genesis();
        let g1 = gen_after(&g0, 100);
        let mut d = manifest_chain::ManifestDelta::between(&g0, &g1, 0).unwrap();
        d.seal();
        c.write_delta(&d); // generation 0 never written

        let findings = parent_chain(&c.0, 1);
        assert!(
            findings.iter().any(|f| f.kind == "parent-missing"),
            "{findings:?}"
        );
    }

    #[test]
    fn close_records_may_not_address_rows_past_a_segments_end() {
        let f = Fixture::new("closerange", &[edge(0, 1, 0, 10, "#0")], &["a", "b"], None);
        let good = crate::visibility::CloseRecord {
            kind: crate::row::RowKind::Edge,
            segment: 0,
            row: 0,
            tt_e: 20,
        };
        let bad = crate::visibility::CloseRecord { row: 9, ..good };
        assert!(check_close_targets(&f.manifest, &[good]).is_empty());
        let findings = check_close_targets(&f.manifest, &[bad]);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].kind, "close-row-out-of-range");
        assert_eq!(findings[0].layer, LAYER_CLOSE);
    }
}
