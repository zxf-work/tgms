//! Belief visibility: closing `tt_e` is recorded as data, never as a mutation
//! (spec §4.4, §5.1; D-028 #1, #7).
//!
//! Sealed segments are immutable, so "this version stopped being believed at
//! `tt_e`" cannot be written into the row. Instead each commit appends an
//! immutable **close run** that the manifest lists. Because a reader pins a
//! manifest generation, it sees exactly the closes that existed then — which
//! is what makes the snapshot coherent. (A store-wide mutable close set, as
//! the first blueprint had, would let a generation-N reader observe
//! generation-N+1 visibility.)
//!
//! Compaction later folds runs into a per-segment **sidecar** carried in the
//! segment header. Two representations are implemented, per D-028 #7:
//!
//! | closure density | representation | cost on a current-belief scan |
//! |---|---|---|
//! | zero | `all_current` header flag | nothing at all |
//! | sparse | sorted `(row, tt_e)` pairs | one binary search per closed row |
//!
//! The dense `tt_e`-per-row variant is deliberately *not* built: the format
//! reserves the shape, and no measured workload has needed it.
//!
//! Compaction folds closes; it never drops the rows they refer to. Historical
//! belief queries are the entire point of the system, and a closed version is
//! still the answer to "what did we believe before the correction?".

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;

use crate::error::{EngineError, Result};
use crate::row::RowKind;
use crate::OPEN_END;

const MAGIC: &[u8; 4] = b"TGCR";
const FOOTER_MAGIC: &[u8; 4] = b"TGCE";
const RECORD_BYTES: usize = 1 + 8 + 4 + 8;

/// One version stopped being believed. Rows are addressed physically, by the
/// segment and row they live at — compaction that moves a row rewrites the
/// close into the new segment's sidecar rather than leaving a dangling id.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CloseRecord {
    pub kind: RowKind,
    pub segment: u64,
    pub row: u32,
    pub tt_e: i64,
}

/// Write one commit's closes. Called before the manifest that references it,
/// so a crash here leaves an unreferenced file rather than a dangling pointer.
pub fn write_close_run(path: &Path, records: &[CloseRecord]) -> Result<u32> {
    if records.is_empty() {
        return Err(EngineError::invariant("refusing to write an empty close run"));
    }
    let mut buf = Vec::with_capacity(12 + records.len() * RECORD_BYTES + 8);
    buf.extend_from_slice(MAGIC);
    buf.extend_from_slice(&crate::FORMAT_VERSION.to_le_bytes());
    buf.extend_from_slice(&(records.len() as u32).to_le_bytes());
    for r in records {
        buf.push(match r.kind {
            RowKind::Node => 0,
            RowKind::Edge => 1,
        });
        buf.extend_from_slice(&r.segment.to_le_bytes());
        buf.extend_from_slice(&r.row.to_le_bytes());
        buf.extend_from_slice(&r.tt_e.to_le_bytes());
    }
    buf.extend_from_slice(&crc32c::crc32c(&buf[12..]).to_le_bytes());
    buf.extend_from_slice(FOOTER_MAGIC);

    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| EngineError::from(e).at_file(dir))?;
    }
    let tmp = path.with_extension("tgc.tmp");
    {
        let mut f = File::create(&tmp).map_err(|e| EngineError::from(e).at_file(&tmp))?;
        f.write_all(&buf)
            .map_err(|e| EngineError::from(e).at_file(&tmp))?;
        f.sync_all()
            .map_err(|e| EngineError::from(e).at_file(&tmp))?;
    }
    fs::rename(&tmp, path).map_err(|e| EngineError::from(e).at_file(path))?;
    Ok(records.len() as u32)
}

pub fn read_close_run(path: &Path) -> Result<Vec<CloseRecord>> {
    let bytes = fs::read(path).map_err(|e| EngineError::from(e).at_file(path))?;
    let at = |e: EngineError| e.at_file(path);
    if bytes.len() < 20 || &bytes[..4] != MAGIC {
        return Err(at(EngineError::corrupt("not a TGMS close run (bad magic)")));
    }
    if &bytes[bytes.len() - 4..] != FOOTER_MAGIC {
        return Err(at(EngineError::corrupt(
            "close run has no completion marker — it was never finished being written",
        )));
    }
    let n = u32::from_le_bytes(bytes[8..12].try_into().expect("4 bytes")) as usize;
    let end = 12 + n * RECORD_BYTES;
    if bytes.len() < end + 8 {
        return Err(at(EngineError::corrupt("close run is truncated")));
    }
    let expect = u32::from_le_bytes(bytes[end..end + 4].try_into().expect("4 bytes"));
    let got = crc32c::crc32c(&bytes[12..end]);
    if got != expect {
        return Err(at(EngineError::corrupt(format!(
            "close run failed its checksum (computed {got:#x}, recorded {expect:#x})"
        ))));
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let o = 12 + i * RECORD_BYTES;
        out.push(CloseRecord {
            kind: match bytes[o] {
                0 => RowKind::Node,
                1 => RowKind::Edge,
                other => {
                    return Err(at(
                        EngineError::corrupt(format!("close run has unknown row kind {other}"))
                            .at_offset(o as u64),
                    ))
                }
            },
            segment: u64::from_le_bytes(bytes[o + 1..o + 9].try_into().expect("8 bytes")),
            row: u32::from_le_bytes(bytes[o + 9..o + 13].try_into().expect("4 bytes")),
            tt_e: i64::from_le_bytes(bytes[o + 13..o + 21].try_into().expect("8 bytes")),
        });
    }
    Ok(out)
}

/// All close runs a generation makes visible, resolved for lookup.
///
/// A later close of the same row wins: corrections are applied in transaction
/// order, and replay must reach the same state as the live path.
#[derive(Default, Debug, Clone)]
pub struct CloseIndex {
    by_row: HashMap<(u64, u32), i64>,
    segments: std::collections::HashSet<u64>,
}

impl CloseIndex {
    pub fn from_records(records: impl IntoIterator<Item = CloseRecord>) -> Self {
        let mut idx = Self::default();
        for r in records {
            idx.by_row.insert((r.segment, r.row), r.tt_e);
            idx.segments.insert(r.segment);
        }
        idx
    }

    pub fn is_empty(&self) -> bool {
        self.by_row.is_empty()
    }

    pub fn len(&self) -> usize {
        self.by_row.len()
    }

    /// Does any close in this generation touch the given segment? Lets a scan
    /// skip visibility work entirely for the segments nothing has corrected —
    /// which, for event-stream data, is nearly all of them.
    pub fn touches(&self, segment: u64) -> bool {
        self.segments.contains(&segment)
    }

    /// Transaction-time end of one row: the close if there is one, else open.
    #[inline]
    pub fn tt_e(&self, segment: u64, row: u32) -> i64 {
        self.by_row
            .get(&(segment, row))
            .copied()
            .unwrap_or(OPEN_END)
    }

    pub fn records_for(&self, segment: u64) -> Vec<(u32, i64)> {
        let mut v: Vec<(u32, i64)> = self
            .by_row
            .iter()
            .filter(|((s, _), _)| *s == segment)
            .map(|((_, r), tt)| (*r, *tt))
            .collect();
        v.sort_unstable();
        v
    }
}

/// The folded, per-segment form: sorted `(row, tt_e)` pairs living in the
/// segment header. Empty means `all_current` — the fast path.
#[derive(Clone, Debug, Default)]
pub struct Sidecar<'a> {
    closed: &'a [(u32, i64)],
}

impl<'a> Sidecar<'a> {
    pub fn new(closed: &'a [(u32, i64)]) -> Self {
        Self { closed }
    }

    pub fn all_current(&self) -> bool {
        self.closed.is_empty()
    }

    #[inline]
    pub fn tt_e(&self, row: u32) -> i64 {
        if self.closed.is_empty() {
            return OPEN_END;
        }
        match self.closed.binary_search_by_key(&row, |(r, _)| *r) {
            Ok(i) => self.closed[i].1,
            Err(_) => OPEN_END,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-vis-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p.join("000001.tgc")
    }

    fn rec(seg: u64, row: u32, tt_e: i64) -> CloseRecord {
        CloseRecord {
            kind: RowKind::Edge,
            segment: seg,
            row,
            tt_e,
        }
    }

    #[test]
    fn close_runs_round_trip() {
        let path = tmp("roundtrip");
        let records = vec![
            rec(0, 0, 100),
            rec(0, 17, 200),
            CloseRecord {
                kind: RowKind::Node,
                segment: 3,
                row: 9,
                tt_e: OPEN_END - 1,
            },
        ];
        assert_eq!(write_close_run(&path, &records).unwrap(), 3);
        assert_eq!(read_close_run(&path).unwrap(), records);
    }

    #[test]
    fn truncated_close_run_is_detected() {
        let path = tmp("truncated");
        write_close_run(&path, &[rec(0, 0, 100)]).unwrap();
        let mut b = fs::read(&path).unwrap();
        b.truncate(b.len() - 6);
        fs::write(&path, b).unwrap();
        let err = match read_close_run(&path) {
            Ok(_) => panic!("a truncated close run must not load"),
            Err(e) => e,
        };
        assert_eq!(err.category, crate::error::Category::Corrupt);
    }

    #[test]
    fn flipped_byte_fails_the_checksum() {
        let path = tmp("bitflip");
        write_close_run(&path, &[rec(0, 5, 100), rec(0, 6, 101)]).unwrap();
        let mut b = fs::read(&path).unwrap();
        b[14] ^= 0xff;
        fs::write(&path, b).unwrap();
        assert!(read_close_run(&path).is_err());
    }

    #[test]
    fn empty_close_runs_are_never_written() {
        assert!(write_close_run(&tmp("empty"), &[]).is_err());
    }

    #[test]
    fn later_closes_win() {
        // the same row corrected twice: transaction order decides
        let idx = CloseIndex::from_records([rec(1, 4, 100), rec(1, 4, 250)]);
        assert_eq!(idx.tt_e(1, 4), 250);
        assert_eq!(idx.len(), 1);
    }

    #[test]
    fn unclosed_rows_read_as_open() {
        let idx = CloseIndex::from_records([rec(1, 4, 100)]);
        assert_eq!(idx.tt_e(1, 5), OPEN_END);
        assert_eq!(idx.tt_e(2, 4), OPEN_END);
        assert!(idx.touches(1));
        assert!(!idx.touches(2), "untouched segments skip visibility work");
    }

    #[test]
    fn sidecar_answers_by_binary_search() {
        let closed = [(3u32, 100i64), (9, 200), (40, 300)];
        let s = Sidecar::new(&closed);
        assert!(!s.all_current());
        assert_eq!(s.tt_e(3), 100);
        assert_eq!(s.tt_e(9), 200);
        assert_eq!(s.tt_e(40), 300);
        assert_eq!(s.tt_e(0), OPEN_END);
        assert_eq!(s.tt_e(41), OPEN_END);
    }

    #[test]
    fn empty_sidecar_is_the_all_current_fast_path() {
        let s = Sidecar::default();
        assert!(s.all_current());
        assert_eq!(s.tt_e(12345), OPEN_END);
    }

    #[test]
    fn records_for_segment_come_back_sorted_for_folding() {
        let idx = CloseIndex::from_records([rec(1, 40, 3), rec(1, 4, 1), rec(2, 7, 2)]);
        assert_eq!(idx.records_for(1), vec![(4, 1), (40, 3)]);
        assert_eq!(idx.records_for(2), vec![(7, 2)]);
        assert!(idx.records_for(99).is_empty());
    }
}
