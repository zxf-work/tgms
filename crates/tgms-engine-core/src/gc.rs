//! Generation collection: reclaim superseded manifests and the files only
//! they reference (spec §5.6 deferred item `tgms store gc`).
//!
//! Every commit publishes `manifests/<G>.json` naming every live segment, so
//! the directory grows O(generations × segments) and, at 1M rows after ~250
//! commits, weighed 24 MB against 31 MB of compressed segments (D-032
//! "Open"). Compaction has the same retention question from the other side:
//! it supersedes segment files and deletes nothing, so peak usage is 2× the
//! store. One pass handles both, because both reduce to the same rule: *a
//! file may be removed exactly when no retained generation names it.*
//!
//! **Retention policy.** A generation is retained if it is (a) the one
//! `CURRENT` names, (b) among the last `keep_last` generations (configurable,
//! default `defaults::GC_KEEP_GENERATIONS`), or (c) pinned by a live reader
//! in this process. The engine is single-writer with in-process readers:
//! every open `NativeStore` registers its generation in a process-global pin
//! table keyed by canonical store root, so a reader that opened at generation
//! N keeps N's manifest and files on disk until it drops. There is no
//! cross-process reader registry, deliberately — the single-writer contract
//! already makes a second *writer* undefined, and a reader in another process
//! holds its manifest in memory and its segments via mmap, so on POSIX even a
//! collected file stays readable through handles it already opened. The one
//! exposure is a cross-process reader lazily opening a segment it has never
//! touched after gc removed it: that fails with a *detected* IO error naming
//! the file — never silently wrong data — which is the durability objective's
//! bar (blueprint §1). Run gc from the writer; that is where the CLI puts it.
//!
//! **Crash safety.** Deletion happens in dependency order: superseded
//! manifests first, then the directory is fsynced, and only files that no
//! manifest *still on disk* references are eligible afterwards. Every unlink
//! is individually atomic, `CURRENT` and its generation are categorically
//! untouched, so a crash at any point leaves a store that opens and verifies
//! — at worst with some garbage still present, which the next pass collects.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use crate::error::{EngineError, Result};
use crate::manifest::Manifest;
use crate::store::NativeStore;

/// In-process reader pins: canonical store root → generation → open handles.
///
/// A `HashMap` count rather than a set, because two readers may legitimately
/// pin the same generation and the pin must outlive the first to drop.
static PINS: OnceLock<Mutex<HashMap<PathBuf, HashMap<u64, usize>>>> = OnceLock::new();

fn pins() -> &'static Mutex<HashMap<PathBuf, HashMap<u64, usize>>> {
    PINS.get_or_init(|| Mutex::new(HashMap::new()))
}

pub(crate) fn pin(root: &Path, generation: u64) {
    let mut table = pins().lock().expect("pin-table mutex poisoned");
    *table
        .entry(root.to_path_buf())
        .or_default()
        .entry(generation)
        .or_default() += 1;
}

pub(crate) fn unpin(root: &Path, generation: u64) {
    let mut table = pins().lock().expect("pin-table mutex poisoned");
    if let Some(gens) = table.get_mut(root) {
        if let Some(n) = gens.get_mut(&generation) {
            *n -= 1;
            if *n == 0 {
                gens.remove(&generation);
            }
        }
        if gens.is_empty() {
            table.remove(root);
        }
    }
}

/// Move one handle's pin from `old` to `new` — a commit or compaction
/// advancing the writer's generation.
pub(crate) fn repin(root: &Path, old: u64, new: u64) {
    if old != new {
        pin(root, new);
        unpin(root, old);
    }
}

fn pinned(root: &Path) -> HashSet<u64> {
    pins()
        .lock()
        .expect("pin-table mutex poisoned")
        .get(root)
        .map(|gens| gens.keys().copied().collect())
        .unwrap_or_default()
}

/// What one gc pass removed and what it kept.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct GcReport {
    pub manifests_removed: usize,
    pub segments_removed: usize,
    pub close_runs_removed: usize,
    pub bytes_reclaimed: u64,
    /// Generations whose manifests remain on disk after the pass.
    pub generations_retained: usize,
}

impl NativeStore {
    /// Collect superseded generations and the files only they reference.
    ///
    /// `keep_last` is clamped to at least 1 — the `CURRENT` generation is
    /// never eligible, whatever the caller asks for.
    pub fn gc(&mut self, keep_last: u64) -> Result<GcReport> {
        if self.in_batch() {
            return Err(EngineError::invariant(
                "cannot collect generations while a batch is open",
            ));
        }
        let current = self.generation();
        let floor = current.saturating_sub(keep_last.max(1) - 1);
        let protected = pinned(self.pin_key());
        let mut report = GcReport::default();

        // Pass 1 — superseded manifests, plus temp files a crash between
        // write and rename left behind. Names that parse as neither are left
        // alone: gc removes only what it can prove is garbage.
        let m_dir = self.root().join("manifests");
        for name in list_dir(&m_dir)? {
            let path = m_dir.join(&name);
            if name.ends_with(".tmp") {
                report.bytes_reclaimed += remove(&path)?;
                continue;
            }
            let Some(g) = name
                .strip_suffix(".json")
                .and_then(|s| s.parse::<u64>().ok())
            else {
                continue;
            };
            if g >= floor || g == current || protected.contains(&g) {
                continue;
            }
            report.bytes_reclaimed += remove(&path)?;
            report.manifests_removed += 1;
        }
        fsync_dir(&m_dir)?;

        // Pass 2 — the reference set, rebuilt from what actually remains on
        // disk (not from the retention arithmetic), so a crash between the
        // passes can only make the next pass conservative, never eager. A
        // retained manifest that fails its checksum aborts the pass: deleting
        // by an unreadable reference list would be guessing.
        let mut referenced: HashSet<String> = HashSet::new();
        for name in list_dir(&m_dir)? {
            if !name.ends_with(".json") {
                continue;
            }
            let path = m_dir.join(&name);
            let raw = fs::read_to_string(&path)
                .map_err(|e| EngineError::from(e).at_file(&path))?;
            let m = Manifest::from_json(&raw).map_err(|e| e.at_file(&path))?;
            report.generations_retained += 1;
            for e in m
                .node_store
                .iter()
                .chain(m.edge_lanes.event.iter())
                .chain(m.edge_lanes.interval.iter())
            {
                referenced.insert(e.file.clone());
            }
            for r in &m.close_runs {
                referenced.insert(r.file.clone());
            }
        }

        // Pass 3 — unreferenced segment and close-run files. This collects
        // both compaction's superseded segments and orphans from a batch that
        // crashed before publishing.
        for (dir, ext, count) in [
            ("seg", ".tgs", &mut report.segments_removed),
            ("close", ".tgc", &mut report.close_runs_removed),
        ] {
            let d = self.root().join(dir);
            for name in list_dir(&d)? {
                if !name.ends_with(ext) || referenced.contains(&format!("{dir}/{name}")) {
                    continue;
                }
                report.bytes_reclaimed += remove(&d.join(&name))?;
                *count += 1;
            }
            fsync_dir(&d)?;
        }

        self.evict_segments_not_in(&referenced);
        Ok(report)
    }
}

fn list_dir(dir: &Path) -> Result<Vec<String>> {
    let entries = fs::read_dir(dir).map_err(|e| EngineError::from(e).at_file(dir))?;
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| EngineError::from(e).at_file(dir))?;
        names.push(entry.file_name().to_string_lossy().into_owned());
    }
    names.sort();
    Ok(names)
}

/// Unlink one file, returning the bytes it held.
fn remove(path: &Path) -> Result<u64> {
    let bytes = fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    crate::store::crash_point("gc_mid_delete");
    fs::remove_file(path).map_err(|e| EngineError::from(e).at_file(path))?;
    Ok(bytes)
}

fn fsync_dir(dir: &Path) -> Result<()> {
    fs::File::open(dir)
        .and_then(|f| f.sync_all())
        .map_err(|e| EngineError::from(e).at_file(dir))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};
    use crate::manifest::EventLogRef;
    use crate::row::{EdgeRow, RowKind};

    fn tmp_root(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-gc-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        p
    }

    fn edge(a: u32, b: u32, vt_s: i64, tt_s: i64, i: u32) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid("n1", "n2", "R", &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id: a,
            dst_id: b,
            rel_type: "R".into(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    /// One committed generation holding `n` fresh edge rows.
    fn commit_edges(s: &mut NativeStore, tt: i64, n: u32) -> Vec<EdgeRow> {
        s.begin(tt).unwrap();
        let a = s.ensure_entity("n1", "Node").unwrap();
        let b = s.ensure_entity("n2", "Node").unwrap();
        let rows: Vec<EdgeRow> = (0..n)
            .map(|i| edge(a, b, tt * 100 + i as i64, tt, (tt as u32) * 100 + i))
            .collect();
        for r in &rows {
            s.stage_edge(r.clone()).unwrap();
        }
        s.commit(EventLogRef::default()).unwrap();
        rows
    }

    fn manifest_gens(root: &Path) -> Vec<u64> {
        let mut gens: Vec<u64> = fs::read_dir(root.join("manifests"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter_map(|n| n.strip_suffix(".json").and_then(|s| s.parse().ok()))
            .collect();
        gens.sort();
        gens
    }

    fn seg_files(root: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(root.join("seg"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| n.ends_with(".tgs"))
            .collect();
        names.sort();
        names
    }

    #[test]
    fn gc_keeps_current_and_the_last_k() {
        let root = tmp_root("keep-k");
        let mut s = NativeStore::open(&root).unwrap();
        for tt in 1..=5 {
            commit_edges(&mut s, tt * 10, 1);
        }
        assert_eq!(manifest_gens(&root), vec![0, 1, 2, 3, 4, 5]);

        let report = s.gc(2).unwrap();
        assert_eq!(manifest_gens(&root), vec![4, 5]);
        assert_eq!(report.manifests_removed, 4);
        assert_eq!(report.generations_retained, 2);
        assert!(report.bytes_reclaimed > 0);
        // every commit inherited its parent's segments, so all are referenced
        assert_eq!(report.segments_removed, 0);
        assert_eq!(seg_files(&root).len(), 5);

        // the collected store must open and verify like nothing happened
        drop(s);
        let re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 5);
        assert_eq!(re.all_edge_versions().unwrap().len(), 5);
        assert!(re.verify().unwrap().is_healthy());
    }

    #[test]
    fn keep_last_is_clamped_so_current_is_never_eligible() {
        let root = tmp_root("clamp");
        let mut s = NativeStore::open(&root).unwrap();
        commit_edges(&mut s, 10, 1);
        let report = s.gc(0).unwrap();
        assert_eq!(manifest_gens(&root), vec![1], "gc(0) must behave as gc(1)");
        assert_eq!(report.generations_retained, 1);
        assert!(s.verify().unwrap().is_healthy());
    }

    #[test]
    fn gc_is_refused_mid_batch_and_trivial_on_a_fresh_store() {
        let root = tmp_root("guards");
        let mut s = NativeStore::open(&root).unwrap();
        assert_eq!(s.gc(2).unwrap(), GcReport {
            generations_retained: 1,
            ..GcReport::default()
        });

        s.begin(10).unwrap();
        assert!(s.gc(2).is_err(), "must not collect with a batch open");
        s.rollback().unwrap();
    }

    #[test]
    fn a_large_keep_last_removes_nothing() {
        let root = tmp_root("keep-all");
        let mut s = NativeStore::open(&root).unwrap();
        for tt in 1..=3 {
            commit_edges(&mut s, tt * 10, 1);
        }
        let report = s.gc(u64::MAX).unwrap();
        assert_eq!(report.manifests_removed, 0);
        assert_eq!(report.segments_removed, 0);
        assert_eq!(manifest_gens(&root), vec![0, 1, 2, 3]);
    }

    #[test]
    fn gc_collects_segments_and_close_runs_a_compaction_superseded() {
        let root = tmp_root("compaction");
        let mut s = NativeStore::open(&root).unwrap();
        let mut rows = Vec::new();
        for tt in 1..=3 {
            rows.extend(commit_edges(&mut s, tt * 10, 2));
        }
        // a correction against a committed row, so a close run exists
        s.begin(100).unwrap();
        s.close_version(RowKind::Edge, rows[0].vid, 100).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        let old_segs = seg_files(&root);
        assert_eq!(old_segs.len(), 3);
        assert!(root.join("close").read_dir().unwrap().count() == 1);

        let logical_before = {
            let mut v = s.all_edge_versions().unwrap();
            v.sort_by(|a, b| a.vid.cmp(&b.vid));
            v
        };
        s.compact().unwrap();
        let report = s.gc(1).unwrap();

        assert_eq!(report.segments_removed, 3, "superseded segments collected");
        assert_eq!(report.close_runs_removed, 1, "folded run collected");
        for f in &old_segs {
            assert!(!root.join("seg").join(f).exists(), "{f} survived gc");
        }
        let mut logical_after = s.all_edge_versions().unwrap();
        logical_after.sort_by(|a, b| a.vid.cmp(&b.vid));
        assert_eq!(logical_after, logical_before, "gc changed the logical store");

        drop(s);
        let re = NativeStore::open(&root).unwrap();
        assert!(re.verify().unwrap().is_healthy());
        assert_eq!(re.all_edge_versions().unwrap().len(), logical_before.len());
    }

    #[test]
    fn files_referenced_by_a_retained_generation_survive() {
        let root = tmp_root("retained-refs");
        let mut s = NativeStore::open(&root).unwrap();
        commit_edges(&mut s, 10, 2);
        s.compact().unwrap(); // generation 2 supersedes generation 1's segment

        // keep both generations: the superseded segment is still referenced
        let report = s.gc(3).unwrap();
        assert_eq!(report.segments_removed, 0);
        assert_eq!(seg_files(&root).len(), 2);
        // shrink the window and it goes
        let report = s.gc(1).unwrap();
        assert_eq!(report.segments_removed, 1);
        assert!(s.verify().unwrap().is_healthy());
    }

    #[test]
    fn an_in_process_reader_pins_its_generation_until_dropped() {
        let root = tmp_root("reader-pins");
        let mut w = NativeStore::open(&root).unwrap();
        let pinned_rows = commit_edges(&mut w, 10, 1); // generation 1
        let reader = NativeStore::open(&root).unwrap(); // pins generation 1
        commit_edges(&mut w, 20, 1);
        commit_edges(&mut w, 30, 1); // generation 3

        w.gc(1).unwrap();
        assert_eq!(
            manifest_gens(&root),
            vec![1, 3],
            "the pinned generation must survive alongside CURRENT"
        );
        // the reader's view still works end to end
        let seen = reader.all_edge_versions().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].vid, pinned_rows[0].vid.to_hex());

        drop(reader);
        let report = w.gc(1).unwrap();
        assert_eq!(report.manifests_removed, 1, "unpinned generation collected");
        assert_eq!(manifest_gens(&root), vec![3]);
    }

    #[test]
    fn a_crash_mid_gc_leaves_the_store_openable_and_the_next_pass_finishes() {
        let root = tmp_root("crash-mid");
        let mut s = NativeStore::open(&root).unwrap();
        for tt in 1..=2 {
            commit_edges(&mut s, tt * 10, 1);
        }
        s.compact().unwrap(); // generation 3; generations 0-2 now superseded
        let logical = s.all_edge_versions().unwrap().len();
        drop(s);

        // simulate dying between pass 1 and pass 3: some superseded manifests
        // are gone, the segments only they referenced are still on disk, and
        // a manifest temp file survived a crash during an earlier publish
        fs::remove_file(root.join("manifests").join(format!("{:020}.json", 0))).unwrap();
        fs::remove_file(root.join("manifests").join(format!("{:020}.json", 1))).unwrap();
        fs::write(root.join("manifests").join("junk.tmp"), b"partial").unwrap();

        let mut re = NativeStore::open(&root).unwrap();
        assert_eq!(re.generation(), 3, "a half-collected store must open");
        assert!(re.verify().unwrap().is_healthy());
        assert_eq!(re.all_edge_versions().unwrap().len(), logical);

        let report = re.gc(1).unwrap();
        assert_eq!(manifest_gens(&root), vec![3]);
        assert!(!root.join("manifests").join("junk.tmp").exists());
        assert_eq!(report.segments_removed, 2, "orphaned segments collected");
        assert!(re.verify().unwrap().is_healthy());
    }

    #[test]
    fn segments_a_crashed_batch_orphaned_are_collected() {
        let root = tmp_root("orphans");
        let mut s = NativeStore::open(&root).unwrap();
        commit_edges(&mut s, 10, 1);
        // a batch that wrote its segment and died before publishing
        let real = seg_files(&root);
        let orphan = root.join("seg").join("999999999999.tgs");
        fs::copy(root.join("seg").join(&real[0]), &orphan).unwrap();

        let report = s.gc(u64::MAX).unwrap();
        assert_eq!(report.segments_removed, 1);
        assert!(!orphan.exists());
        assert!(root.join("seg").join(&real[0]).exists());
        assert!(s.verify().unwrap().is_healthy());
    }
}
