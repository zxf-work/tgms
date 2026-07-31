//! Compaction: fold close runs into segment sidecars and merge runs
//! (spec §5.6, D-028 #8).
//!
//! **Compaction never drops a row.** A closed version is still the answer to
//! "what did we believe before the correction?", and historical belief
//! queries are the entire point of the system. Folding changes only *where*
//! a `tt_e` is written — from a standalone run file into the owning segment's
//! sidecar — never whether the row exists. That is what the equivalence test
//! below pins: the full logical listing and a sample of historical queries
//! must be byte-identical either side of a compaction.
//!
//! This is the minimal implementation WP-N3 calls for: it reads the logical
//! content back through the ordinary read path and re-seals it. That is
//! obviously correct (it reuses tested code) and bounded by memory rather
//! than by store size. A streaming, partition-at-a-time merge is the natural
//! successor once a store outgrows RAM; the file format does not change.
//!
//! Old segment files are left on disk: an older manifest may still reference
//! them, and a reader holding that generation must keep working. Reclaiming
//! them is generation collection's job (gc.rs), which removes a file only
//! once no retained generation names it.

use std::collections::HashMap;

use crate::derive::Id96;
use crate::error::{EngineError, Result};
use crate::manifest::EdgeLanes;
use crate::row::{EdgeRow, Lane, NodeRow};
use crate::staging::Staging;
use crate::store::NativeStore;
use crate::{defaults, OPEN_END};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct CompactionReport {
    pub segments_before: usize,
    pub segments_after: usize,
    pub closes_folded: usize,
    pub edge_rows: usize,
    pub node_rows: usize,
}

impl NativeStore {
    /// Advisory hint only — compaction is explicit (`tgms store compact`),
    /// never a background schedule, because a single writer has no one to
    /// coordinate with and surprise rewrites are worse than a stale layout.
    ///
    /// The run-count half is approximated by total segment count rather than
    /// per-partition runs; it over-triggers on wide stores, which is the safe
    /// direction for a hint.
    pub fn needs_compaction(&self) -> bool {
        let m = self.manifest();
        let segments =
            m.edge_lanes.event.len() + m.edge_lanes.interval.len() + m.node_store.len();
        !m.close_runs.is_empty() || segments > defaults::COMPACTION_RUNS_TRIGGER as usize
    }

    /// Rewrite the store's content into fresh segments with every close
    /// folded in. Publishes a new generation and returns what it did.
    pub fn compact(&mut self) -> Result<CompactionReport> {
        if self.in_batch() {
            return Err(EngineError::invariant(
                "cannot compact while a batch is open",
            ));
        }
        let m = self.manifest();
        let segments_before =
            m.edge_lanes.event.len() + m.edge_lanes.interval.len() + m.node_store.len();
        if segments_before == 0 {
            return Ok(CompactionReport::default());
        }

        // Read the logical content back — closes already applied — and stage
        // it again. Every `tt_e` that is not open becomes a folded close.
        let edges = self.all_edge_versions()?;
        let nodes = self.all_node_versions()?;
        let mut staging = Staging::default();
        let mut closes: HashMap<Id96, i64> = HashMap::new();

        for e in &edges {
            let vid = Id96::from_hex(&e.vid)?;
            if e.tt_e != OPEN_END {
                closes.insert(vid, e.tt_e);
            }
            staging.push_edge(EdgeRow {
                vid,
                src_id: self.dense_or_err(&e.src)?,
                dst_id: self.dense_or_err(&e.dst)?,
                rel_type: e.rel_type.clone(),
                disc: e.disc.clone(),
                vt_s: e.vt_s,
                vt_e: e.vt_e,
                tt_s: e.tt_s,
                props: e.props.clone(),
                source: e.source.clone(),
                provenance_ref: e.provenance_ref.clone(),
            });
        }
        for n in &nodes {
            let vid = Id96::from_hex(&n.vid)?;
            if n.tt_e != OPEN_END {
                closes.insert(vid, n.tt_e);
            }
            staging.push_node(NodeRow {
                vid,
                uid_id: self.dense_or_err(&n.uid)?,
                label: n.label.clone(),
                vt_s: n.vt_s,
                vt_e: n.vt_e,
                tt_s: n.tt_s,
                props: n.props.clone(),
                source: n.source.clone(),
                provenance_ref: n.provenance_ref.clone(),
            });
        }

        // Compaction is a physical rewrite, so it does not advance
        // transaction time — no belief changed.
        let mut next = self.manifest().successor(self.manifest().created_tt);
        let mut next_id = next.next_segment_id;
        let (partitions, target_bytes) = {
            let (p, t) = self.layout();
            (*p, t)
        };
        let sealed = staging.seal(
            &self.root().join("seg"),
            &partitions,
            target_bytes,
            &mut next_id,
            &closes,
        )?;

        next.next_segment_id = next_id;
        next.edge_lanes = EdgeLanes::default();
        next.node_store = Vec::new();
        next.close_runs = Vec::new(); // folded into the new sidecars
        for (lane, entry) in sealed.edges {
            match lane {
                Lane::Event => next.edge_lanes.event.push(entry),
                Lane::Interval => next.edge_lanes.interval.push(entry),
            }
        }
        next.node_store = sealed.nodes;
        next.stats.n_edge_versions = edges.len() as u64;
        next.stats.n_node_versions = nodes.len() as u64;
        next.stats.n_entities = self.dict().len();
        next.seal();

        let segments_after =
            next.edge_lanes.event.len() + next.edge_lanes.interval.len() + next.node_store.len();
        self.install(next)?;

        Ok(CompactionReport {
            segments_before,
            segments_after,
            closes_folded: closes.len(),
            edge_rows: edges.len(),
            node_rows: nodes.len(),
        })
    }

    /// The §13 stripped configuration: rewrite the store keeping **only the
    /// currently believed rows**, then stamp it `CURRENT_ONLY`.
    ///
    /// This is the one deliberate exception to "compaction never drops a
    /// row", and it exists purely so the evaluation plan's current-versus-
    /// bi-temporal overhead question (§13) can be measured on a store that
    /// genuinely lacks historical versions, close runs, sidecars, and the
    /// postings entries that indexed them. It is not reachable from the
    /// default compaction path, and the marker it writes makes the store
    /// refuse past-belief queries and corrections from then on
    /// (`assert_full_belief`, `close_version`).
    ///
    /// The current-belief answer set is unchanged: a row believed now has
    /// `tt_e == OPEN_END` and is kept verbatim, with its vid, tt_s, and
    /// props intact — so current-belief queries must hash identically
    /// against the full store they came from. That equivalence is the
    /// experiment's correctness gate.
    pub fn compact_current_only(&mut self) -> Result<CompactionReport> {
        if self.in_batch() {
            return Err(EngineError::invariant(
                "cannot compact while a batch is open",
            ));
        }
        let m = self.manifest();
        let segments_before =
            m.edge_lanes.event.len() + m.edge_lanes.interval.len() + m.node_store.len();
        if segments_before == 0 {
            self.mark_current_only()?;
            return Ok(CompactionReport::default());
        }

        // Read the logical content back and keep only what is believed now.
        // Everything else — superseded versions and the closes that hid
        // them — is dropped, so the sealed segments are all-current by
        // construction and no close map is passed to seal.
        let edges = self.all_edge_versions()?;
        let nodes = self.all_node_versions()?;
        let dropped_closes = edges.iter().filter(|e| e.tt_e != OPEN_END).count()
            + nodes.iter().filter(|n| n.tt_e != OPEN_END).count();
        let mut staging = Staging::default();
        let mut kept_edges = 0usize;
        let mut kept_nodes = 0usize;

        for e in &edges {
            if e.tt_e != OPEN_END {
                continue;
            }
            kept_edges += 1;
            staging.push_edge(EdgeRow {
                vid: Id96::from_hex(&e.vid)?,
                src_id: self.dense_or_err(&e.src)?,
                dst_id: self.dense_or_err(&e.dst)?,
                rel_type: e.rel_type.clone(),
                disc: e.disc.clone(),
                vt_s: e.vt_s,
                vt_e: e.vt_e,
                tt_s: e.tt_s,
                props: e.props.clone(),
                source: e.source.clone(),
                provenance_ref: e.provenance_ref.clone(),
            });
        }
        for n in &nodes {
            if n.tt_e != OPEN_END {
                continue;
            }
            kept_nodes += 1;
            staging.push_node(NodeRow {
                vid: Id96::from_hex(&n.vid)?,
                uid_id: self.dense_or_err(&n.uid)?,
                label: n.label.clone(),
                vt_s: n.vt_s,
                vt_e: n.vt_e,
                tt_s: n.tt_s,
                props: n.props.clone(),
                source: n.source.clone(),
                provenance_ref: n.provenance_ref.clone(),
            });
        }

        // A physical rewrite: no belief changed, so tt does not advance.
        let mut next = self.manifest().successor(self.manifest().created_tt);
        let mut next_id = next.next_segment_id;
        let (partitions, target_bytes) = {
            let (p, t) = self.layout();
            (*p, t)
        };
        let sealed = staging.seal(
            &self.root().join("seg"),
            &partitions,
            target_bytes,
            &mut next_id,
            &HashMap::new(),
        )?;

        next.next_segment_id = next_id;
        next.edge_lanes = EdgeLanes::default();
        next.node_store = Vec::new();
        next.close_runs = Vec::new(); // nothing hidden remains to describe
        for (lane, entry) in sealed.edges {
            match lane {
                Lane::Event => next.edge_lanes.event.push(entry),
                Lane::Interval => next.edge_lanes.interval.push(entry),
            }
        }
        next.node_store = sealed.nodes;
        next.stats.n_edge_versions = kept_edges as u64;
        next.stats.n_node_versions = kept_nodes as u64;
        next.stats.n_entities = self.dict().len();
        next.seal();

        let segments_after =
            next.edge_lanes.event.len() + next.edge_lanes.interval.len() + next.node_store.len();
        self.install(next)?;
        self.mark_current_only()?;
        // Unlike default compaction this *changed* the logical content, so a
        // cached stats snapshot over all versions is now wrong. Postings need
        // no flush: `locate` resolves hits through the current manifest.
        *self.stats_cell().lock().expect("stats mutex poisoned") = None;

        Ok(CompactionReport {
            segments_before,
            segments_after,
            closes_folded: dropped_closes,
            edge_rows: kept_edges,
            node_rows: kept_nodes,
        })
    }

    fn dense_or_err(&self, uid: &str) -> Result<u32> {
        self.dict()
            .dense_id(uid)
            .ok_or_else(|| EngineError::invariant(format!("uid {uid:?} left the dictionary")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};
    use crate::manifest::EventLogRef;
    use crate::read::EdgeVersionOut;
    use crate::row::RowKind;
    use std::path::PathBuf;

    fn tmp_root(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-compact-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        p
    }

    fn edge(src_id: u32, dst_id: u32, src: &str, dst: &str, vt_s: i64, tt_s: i64, i: u32) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid(src, dst, "R", &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id,
            dst_id,
            rel_type: "R".into(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s,
            props: format!(r#"{{"i":{i}}}"#),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    fn sorted(mut v: Vec<EdgeVersionOut>) -> Vec<EdgeVersionOut> {
        v.sort_by(|a, b| a.vid.cmp(&b.vid));
        v
    }

    /// Several batches, then corrections against committed rows, so the store
    /// has both multiple segments and unfolded close runs.
    fn seeded(name: &str) -> (PathBuf, NativeStore, Vec<EdgeRow>) {
        let root = tmp_root(name);
        let mut s = NativeStore::open(&root).unwrap();
        let mut all = Vec::new();
        for batch in 0..3u32 {
            let tt = 100 + batch as i64;
            s.begin(tt).unwrap();
            let a = s.ensure_entity("n1", "Node").unwrap();
            let b = s.ensure_entity("n2", "Node").unwrap();
            for i in 0..4u32 {
                let r = edge(a, b, "n1", "n2", (batch * 10 + i) as i64, tt, batch * 10 + i);
                all.push(r.clone());
                s.stage_edge(r).unwrap();
            }
            s.commit(EventLogRef::default()).unwrap();
        }
        // correct two committed rows in a later batch
        s.begin(200).unwrap();
        s.close_version(RowKind::Edge, all[1].vid, 200).unwrap();
        s.close_version(RowKind::Edge, all[7].vid, 200).unwrap();
        s.commit(EventLogRef::default()).unwrap();
        (root, s, all)
    }

    #[test]
    fn compaction_preserves_the_logical_store_exactly() {
        let (_root, mut s, _) = seeded("equivalence");
        let before = sorted(s.all_edge_versions().unwrap());
        let gen_before = s.generation();

        let report = s.compact().unwrap();
        let after = sorted(s.all_edge_versions().unwrap());

        assert_eq!(before, after, "compaction changed the logical content");
        assert_eq!(report.edge_rows, before.len());
        assert!(s.generation() > gen_before, "compaction publishes a generation");
    }

    #[test]
    fn historical_queries_agree_either_side_of_a_compaction() {
        let (_root, mut s, _) = seeded("historical");
        let sample = [99i64, 100, 101, 150, 199, 200, 201, OPEN_END];
        let before: Vec<Vec<String>> = sample
            .iter()
            .map(|&as_of| {
                let mut v: Vec<String> = s
                    .all_edge_versions()
                    .unwrap()
                    .into_iter()
                    .filter(|r| crate::believed_at(r.tt_s, r.tt_e, as_of))
                    .map(|r| r.vid)
                    .collect();
                v.sort();
                v
            })
            .collect();

        s.compact().unwrap();

        for (i, &as_of) in sample.iter().enumerate() {
            let mut v: Vec<String> = s
                .all_edge_versions()
                .unwrap()
                .into_iter()
                .filter(|r| crate::believed_at(r.tt_s, r.tt_e, as_of))
                .map(|r| r.vid)
                .collect();
            v.sort();
            assert_eq!(v, before[i], "belief at as_of={as_of} changed");
        }
    }

    #[test]
    fn closes_are_folded_into_sidecars_and_the_runs_retire() {
        let (_root, mut s, _) = seeded("fold");
        assert_eq!(s.manifest().close_runs.len(), 1, "a run exists to fold");

        let report = s.compact().unwrap();
        assert_eq!(report.closes_folded, 2);
        assert!(
            s.manifest().close_runs.is_empty(),
            "folded runs must no longer be referenced"
        );
        let folded: u32 = s
            .manifest()
            .edge_lanes
            .event
            .iter()
            .map(|e| e.n_closed_folded)
            .sum();
        assert_eq!(folded, 2, "the closes moved into segment sidecars");
        assert!(s.manifest().edge_lanes.event.iter().any(|e| !e.all_current));
    }

    #[test]
    fn closed_rows_are_never_dropped() {
        let (_root, mut s, all) = seeded("retain");
        let rows_before = s.all_edge_versions().unwrap().len();
        s.compact().unwrap();
        let after = s.all_edge_versions().unwrap();

        assert_eq!(after.len(), rows_before, "a row went missing");
        for closed in [&all[1], &all[7]] {
            let found = after
                .iter()
                .find(|r| r.vid == closed.vid.to_hex())
                .expect("a closed version was dropped by compaction");
            assert_eq!(found.tt_e, 200, "its close time must survive folding");
        }
    }

    #[test]
    fn compaction_merges_segments_and_never_reuses_ids() {
        let (_root, mut s, _) = seeded("merge");
        let old_files: Vec<String> = s
            .manifest()
            .edge_lanes
            .event
            .iter()
            .map(|e| e.file.clone())
            .collect();
        let report = s.compact().unwrap();

        assert!(
            report.segments_after <= report.segments_before,
            "compaction should not fragment further"
        );
        let new_files: Vec<String> = s
            .manifest()
            .edge_lanes
            .event
            .iter()
            .map(|e| e.file.clone())
            .collect();
        for f in &new_files {
            assert!(!old_files.contains(f), "segment id {f} was reused");
        }
        // the superseded files are still on disk — nothing is deleted
        for f in &old_files {
            assert!(s.root().join(f).exists(), "{f} was deleted");
        }
    }

    #[test]
    fn compaction_survives_reopening_and_is_idempotent() {
        let (root, mut s, _) = seeded("idempotent");
        s.compact().unwrap();
        let once = sorted(s.all_edge_versions().unwrap());

        // a second pass has nothing left to fold and must change nothing
        let report = s.compact().unwrap();
        assert_eq!(report.closes_folded, 2, "closes stay folded, not lost");
        assert_eq!(sorted(s.all_edge_versions().unwrap()), once);
        drop(s);

        let re = NativeStore::open(&root).unwrap();
        assert_eq!(sorted(re.all_edge_versions().unwrap()), once);
        assert!(re.manifest().close_runs.is_empty());
    }

    #[test]
    fn compaction_is_refused_mid_batch_and_trivial_on_an_empty_store() {
        let root = tmp_root("guards");
        let mut s = NativeStore::open(&root).unwrap();
        assert_eq!(s.compact().unwrap(), CompactionReport::default());
        assert!(!s.needs_compaction());

        s.begin(100).unwrap();
        assert!(s.compact().is_err(), "must not compact with a batch open");
    }

    #[test]
    fn current_only_strip_keeps_the_current_belief_and_drops_the_rest() {
        let (root, mut s, all) = seeded("current-only");
        let believed_now: Vec<String> = {
            let mut v: Vec<String> = s
                .all_edge_versions()
                .unwrap()
                .into_iter()
                .filter(|r| r.tt_e == OPEN_END)
                .map(|r| r.vid)
                .collect();
            v.sort();
            v
        };
        let rows_before = s.all_edge_versions().unwrap().len();
        assert!(believed_now.len() < rows_before, "the seed must have history");

        let report = s.compact_current_only().unwrap();
        assert_eq!(report.closes_folded, 2, "two superseded versions dropped");
        assert!(s.current_only());
        assert!(s.manifest().close_runs.is_empty());

        // the current belief is exactly what survives — nothing more hidden
        let mut after: Vec<String> = s
            .all_edge_versions()
            .unwrap()
            .into_iter()
            .map(|r| {
                assert_eq!(r.tt_e, OPEN_END, "a closed row survived the strip");
                r.vid
            })
            .collect();
        after.sort();
        assert_eq!(after, believed_now, "the current belief changed");

        // segments must be all-current by construction
        assert!(s.manifest().edge_lanes.event.iter().all(|e| e.all_current));

        // the stripped store refuses what it can no longer answer: a
        // past-belief read errs, a current-belief read still works
        assert!(s.believed_node_versions("n1", 100).is_err());
        assert!(s.believed_node_versions("n1", OPEN_END).is_ok());
        s.begin(300).unwrap();
        assert!(
            s.close_version(RowKind::Edge, all[2].vid, 300).is_err(),
            "a correction must be refused on a current-only store"
        );
        s.rollback().unwrap();

        // the marker survives reopening
        drop(s);
        let re = NativeStore::open(&root).unwrap();
        assert!(re.current_only(), "CURRENT_ONLY marker was not honoured on open");
        assert!(re.believed_node_versions("n1", 100).is_err());
        let mut re_rows: Vec<String> =
            re.all_edge_versions().unwrap().into_iter().map(|r| r.vid).collect();
        re_rows.sort();
        assert_eq!(re_rows, believed_now);
    }

    #[test]
    fn current_only_strip_leaves_current_belief_queries_identical() {
        let (_root, mut s, _) = seeded("current-only-eq");
        let now = OPEN_END;
        let before = {
            let mut v: Vec<String> = s
                .all_edge_versions()
                .unwrap()
                .into_iter()
                .filter(|r| crate::believed_at(r.tt_s, r.tt_e, now))
                .map(|r| r.vid)
                .collect();
            v.sort();
            v
        };
        s.compact_current_only().unwrap();
        let mut after: Vec<String> = s
            .all_edge_versions()
            .unwrap()
            .into_iter()
            .filter(|r| crate::believed_at(r.tt_s, r.tt_e, now))
            .map(|r| r.vid)
            .collect();
        after.sort();
        assert_eq!(before, after, "current-belief answers must not change");
    }

    #[test]
    fn unfolded_closes_raise_the_compaction_hint() {
        let (_root, mut s, _) = seeded("hint");
        assert!(s.needs_compaction(), "an unfolded close run should hint");
        s.compact().unwrap();
        // after folding, the hint depends only on segment count
        let segments = s.manifest().edge_lanes.event.len()
            + s.manifest().edge_lanes.interval.len()
            + s.manifest().node_store.len();
        assert_eq!(
            s.needs_compaction(),
            segments > defaults::COMPACTION_RUNS_TRIGGER as usize
        );
    }
}
