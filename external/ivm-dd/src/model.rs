//! Bitemporal version row, mirroring `tgms/core/model.py` and the export
//! bundle's `versions-epoch0.jsonl` / `deltas.jsonl` row shape
//! (`scripts/export_storm_workload.py::_version_table`).
//!
//! All timestamps are int64 epoch microseconds, UTC. `OPEN_END` is the open
//! end-of-time sentinel `2**62`, used for both valid-time and
//! transaction-time "still open" rows.

use serde::{Deserialize, Serialize};
use std::cmp::Ordering;

/// `tgms.core.model.OPEN_END`.
pub const OPEN_END: i64 = 1i64 << 62;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize, PartialOrd, Ord)]
pub enum Kind {
    #[serde(rename = "node")]
    Node,
    #[serde(rename = "edge")]
    Edge,
}

/// One node or edge version row, as carried end to end through the
/// changelog. `uid` is populated for node rows, `eid`/`src`/`dst`/`rel_type`
/// for edge rows (the export's own `versions-epoch0.jsonl` / `deltas.jsonl`
/// rows disambiguate by `kind`, matching `_version_table` in
/// `scripts/export_storm_workload.py`).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VersionRow {
    pub kind: Kind,
    pub vid: String,
    #[serde(default)]
    pub uid: Option<String>,
    #[serde(default)]
    pub eid: Option<String>,
    #[serde(default)]
    pub src: Option<String>,
    #[serde(default)]
    pub dst: Option<String>,
    #[serde(default)]
    pub rel_type: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
    /// Edge discriminator (`EdgeVersion.disc`, a string, not a count).
    #[serde(default)]
    pub disc: Option<String>,
    pub vt_s: i64,
    pub vt_e: i64,
    pub tt_s: i64,
    pub tt_e: i64,
    #[serde(default = "default_props")]
    pub props: serde_json::Value,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub provenance_ref: Option<serde_json::Value>,
}

fn default_props() -> serde_json::Value {
    serde_json::Value::Object(Default::default())
}

// Manual trait impls: DD collections key/order on the *whole* row in a few
// places (arrangement sort order), so VersionRow needs a total order. `vid`
// is the row's own identity (never reused, per the store's model), so
// ordering and equality both key off it first -- cheap, deterministic, and
// exactly how the export's own `_version_table_sha256` sorts rows.
impl PartialEq for VersionRow {
    fn eq(&self, other: &Self) -> bool {
        self.vid == other.vid
            && self.vt_s == other.vt_s
            && self.vt_e == other.vt_e
            && self.tt_s == other.tt_s
            && self.tt_e == other.tt_e
            && self.uid == other.uid
            && self.eid == other.eid
            && self.src == other.src
            && self.dst == other.dst
            && self.rel_type == other.rel_type
            && self.label == other.label
    }
}
impl Eq for VersionRow {}
impl PartialOrd for VersionRow {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for VersionRow {
    fn cmp(&self, other: &Self) -> Ordering {
        (&self.vid, self.vt_s, self.vt_e, self.tt_s, self.tt_e).cmp(&(
            &other.vid, other.vt_s, other.vt_e, other.tt_s, other.tt_e,
        ))
    }
}
impl std::hash::Hash for VersionRow {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        self.vid.hash(state);
        self.vt_s.hash(state);
        self.vt_e.hash(state);
        self.tt_s.hash(state);
        self.tt_e.hash(state);
    }
}

impl VersionRow {
    pub fn is_node(&self) -> bool {
        matches!(self.kind, Kind::Node)
    }

    /// Believed at transaction time `as_of` (`tt_s <= as_of < tt_e`).
    /// Mirrors `NodeVersion.believed_at`/`EdgeVersion.believed_at`
    /// (`tgms/core/model.py`): `as_of` is clamped to `OPEN_END - 1` first,
    /// so the default `as_of_tt = OPEN_END` ("current beliefs") correctly
    /// reads a still-open row (`tt_e == OPEN_END`) as believed -- without
    /// the clamp, `OPEN_END < OPEN_END` is false and *every* currently
    /// -believed row would wrongly read as not believed.
    pub fn believed_at(&self, as_of: i64) -> bool {
        let clamped = as_of.min(OPEN_END - 1);
        self.tt_s <= clamped && clamped < self.tt_e
    }

    /// `tt_e` as it would be *reported* under belief state `as_of_tt`
    /// (`ops_snapshot.entity_history` / `ops_versions.version_history`): a
    /// belief that ends after `as_of` had not ended yet from that vantage
    /// point, so it is reported `OPEN_END` rather than leaking the real
    /// close time.
    pub fn reported_tt_e(&self, as_of_tt: i64) -> i64 {
        if self.tt_e > as_of_tt {
            OPEN_END
        } else {
            self.tt_e
        }
    }

    /// Valid at instant `t` (`vt_s <= t < vt_e`).
    pub fn valid_at(&self, t: i64) -> bool {
        self.vt_s <= t && t < self.vt_e
    }

    /// Valid-time interval overlaps the half-open window `[t_a, t_b)`.
    pub fn overlaps(&self, t_a: i64, t_b: i64) -> bool {
        self.vt_s < t_b && self.vt_e > t_a
    }
}
