//! TGMS native bi-temporal storage engine — core.
//!
//! This crate has **no Python dependency**: it is a plain Rust library so it
//! can be unit-tested with `cargo test`, fuzzed, and later driven by a
//! standalone inspect/repair CLI. The PyO3 bindings live in
//! `tgms-engine-py`.
//!
//! Design: `ENGINE_BLUEPRINT.md` (v3). Binding contract:
//! `ENGINE_IMPLEMENTATION_SPEC.md`. The store is a deterministic
//! materialization of the JSONL event log, so the durability objective is
//! *never expose an undetected inconsistent generation* — roll back to the
//! last valid generation and replay the log suffix.

pub mod codec;
pub mod compact;
pub mod derive;
pub mod dict;
pub mod error;
pub mod interval;
pub mod manifest;
pub mod motif;
pub mod read;
pub mod row;
pub mod scan;
pub mod segment;
pub mod staging;
pub mod store;
pub mod visibility;

pub use derive::{edge_eid, version_vid, Id96};
pub use dict::Dictionary;
pub use manifest::Manifest;
pub use row::{EdgeRow, Lane, NodeRow, RowKind};
pub use store::NativeStore;
pub use error::{Category, EngineError, Result};

/// Crate version, surfaced to Python for receipts (spec §1.4).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// On-disk format version. Bumped only by a breaking format change; every
/// segment, manifest, and close-run file records it.
pub const FORMAT_VERSION: u32 = 1;

/// Open-end sentinel — must equal `tgms.core.model.OPEN_END` (spec §2.1).
pub const OPEN_END: i64 = 1 << 62;

/// Format defaults (spec §4.5). Recorded in each file header, so changing a
/// default never invalidates already-written data.
pub mod defaults {
    /// Rows per block — the codec and pruning unit, sized to stay in L2.
    pub const BLOCK_ROWS: u32 = 32_768;
    /// Target uncompressed segment size before splitting.
    pub const SEGMENT_TARGET_BYTES: u64 = 128 * 1024 * 1024;
    /// Compaction trigger: runs per logical partition.
    pub const COMPACTION_RUNS_TRIGGER: u32 = 4;
    /// Compaction trigger: unfolded close entries as a fraction of segment rows.
    pub const COMPACTION_CLOSE_FRACTION: f64 = 0.20;
    /// Lane rule K: max adjacent partitions an event-lane interval may cross.
    pub const LANE_MAX_PARTITION_CROSSINGS: u32 = 2;
    /// TCSR switches to dense per-vertex offsets at or above this density.
    pub const TCSR_DENSE_THRESHOLD: f64 = 0.5;
}

/// `as_of_tt = OPEN_END` means "current beliefs"; clamping keeps the
/// half-open belief predicate `tt_s <= as_of < tt_e` true for open rows.
/// Mirrors `tgms.core.model.clamp_tt` exactly.
#[inline]
pub const fn clamp_tt(as_of_tt: i64) -> i64 {
    if as_of_tt > OPEN_END - 1 {
        OPEN_END - 1
    } else {
        as_of_tt
    }
}

/// Is a row with belief interval `[tt_s, tt_e)` believed at `as_of_tt`?
#[inline]
pub const fn believed_at(tt_s: i64, tt_e: i64, as_of_tt: i64) -> bool {
    let a = clamp_tt(as_of_tt);
    tt_s <= a && a < tt_e
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_end_matches_python() {
        assert_eq!(OPEN_END, 4_611_686_018_427_387_904);
    }

    #[test]
    fn clamp_tt_matches_python_semantics() {
        assert_eq!(clamp_tt(0), 0);
        assert_eq!(clamp_tt(OPEN_END), OPEN_END - 1);
        assert_eq!(clamp_tt(OPEN_END + 5), OPEN_END - 1);
        assert_eq!(clamp_tt(OPEN_END - 1), OPEN_END - 1);
    }

    #[test]
    fn open_rows_are_believed_at_open_end() {
        // the row every event-stream ingest writes: believed from tt_s, never closed
        assert!(believed_at(100, OPEN_END, OPEN_END));
        assert!(believed_at(100, OPEN_END, 100));
        assert!(!believed_at(100, OPEN_END, 99));
        // a closed row is invisible at or after its close time
        assert!(believed_at(100, 200, 199));
        assert!(!believed_at(100, 200, 200));
    }
}
