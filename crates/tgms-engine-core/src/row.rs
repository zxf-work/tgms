//! Version rows as they cross the boundary into staging.
//!
//! These mirror `tgms.core.model.{NodeVersion, EdgeVersion}` minus the fields
//! the engine derives or relocates:
//!
//! * `eid` / `vid` hex are **derived**, never stored (D-028 #2). Staging
//!   carries `vid` as an `Id96`; `eid` is recomputed from src/dst/rel/disc.
//! * `tt_e` is **not a column** — closing a version is recorded in the
//!   visibility layer (C2), not by mutating the row.
//!
//! `props` is the canonical-JSON string exactly as Python produced it. The
//! engine stores those bytes verbatim and hands them back unchanged: the
//! store digest is computed in Python over this text, so re-serializing it
//! — even "equivalently" — could change a digest.

use crate::derive::Id96;

/// Which physical lane a row belongs to (D-028 #6). Assignment is physical:
/// compaction or a partition-map change may move a row between lanes, and
/// that is invisible to logical identity and to digests.
#[derive(Clone, Copy, PartialEq, Eq, Debug, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Lane {
    /// Instantaneous or short-lived: crosses at most K adjacent partitions.
    Event,
    /// Long-lived facts: explicit `vt_e`, searched via the prefix-max staircase.
    Interval,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RowKind {
    Node,
    Edge,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EdgeRow {
    pub vid: Id96,
    pub src_id: u32,
    pub dst_id: u32,
    pub rel_type: String,
    pub disc: String,
    pub vt_s: i64,
    pub vt_e: i64,
    pub tt_s: i64,
    /// Canonical JSON, stored byte-for-byte as received.
    pub props: String,
    pub source: String,
    pub provenance_ref: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NodeRow {
    pub vid: Id96,
    pub uid_id: u32,
    pub label: String,
    pub vt_s: i64,
    pub vt_e: i64,
    pub tt_s: i64,
    pub props: String,
    pub source: String,
    pub provenance_ref: Option<String>,
}

/// The physical sort key: `(vt_s, vid)`, with `vid` compared in full so
/// ordering never depends on the 64-bit prefix alone (D-028 #4).
pub type SortKey = (i64, Id96);

impl EdgeRow {
    pub fn sort_key(&self) -> SortKey {
        (self.vt_s, self.vid)
    }

    /// True when this row's valid interval is a single microsecond — the
    /// shape every event-stream loader produces (`vt_e = vt_s + 1`).
    pub fn is_instantaneous(&self) -> bool {
        self.vt_e == self.vt_s + 1
    }
}

impl NodeRow {
    pub fn sort_key(&self) -> SortKey {
        (self.vt_s, self.vid)
    }

    pub fn is_instantaneous(&self) -> bool {
        self.vt_e == self.vt_s + 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};

    fn row(vt_s: i64, disc: &str) -> EdgeRow {
        let eid = edge_eid("n1", "n2", "R", disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), 10, vt_s),
            src_id: 0,
            dst_id: 1,
            rel_type: "R".into(),
            disc: disc.into(),
            vt_s,
            vt_e: vt_s + 1,
            tt_s: 10,
            props: "{}".into(),
            source: "ingest".into(),
            provenance_ref: None,
        }
    }

    #[test]
    fn sort_key_orders_by_time_then_full_identity() {
        let mut rows = [row(5, "#1"), row(1, "#2"), row(5, "#0")];
        rows.sort_by_key(|r| r.sort_key());
        assert_eq!(rows[0].vt_s, 1);
        // the two vt_s == 5 rows are ordered by their full vid
        assert!(rows[1].vid < rows[2].vid);
    }

    #[test]
    fn instantaneous_detection_matches_loader_shape() {
        assert!(row(7, "#0").is_instantaneous());
        let mut long = row(7, "#0");
        long.vt_e = 1_000_000;
        assert!(!long.is_instantaneous());
    }
}
