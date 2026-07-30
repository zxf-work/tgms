//! Store manifests — the unit of atomic publication (spec §4.1, D-028 #1).
//!
//! A manifest names *everything* that defines logical visibility at one
//! generation: segments, close runs, dictionary length, index extents, and
//! the event-log offset it corresponds to. Readers pin a generation and
//! therefore see a coherent snapshot; nothing published later can leak into
//! their view. (This is the flaw the v1 blueprint had: a store-wide mutable
//! close set let generation-N readers observe generation-N+1 visibility.)
//!
//! Manifests are JSON on purpose. They are small, written once per commit,
//! and being able to read one with `cat` during an incident is worth more
//! than the bytes a binary encoding would save.

use serde::{Deserialize, Serialize};

use crate::derive::sha256_hex;
use crate::error::{EngineError, Result};
use crate::FORMAT_VERSION;

/// Truncation used for file/manifest digests: 16 hex chars = 64 bits, enough
/// to detect corruption and mismatched pairings (not a security boundary).
pub const SHA_HEX_LEN: usize = 16;

fn short_sha(text: &str) -> String {
    sha256_hex(text)[..SHA_HEX_LEN].to_string()
}

/// Position in the JSONL event log this generation materializes.
///
/// `offset` points immediately past the newline of the last applied record,
/// so recovery can replay the log *suffix* rather than the whole history.
/// `chain` is a rolling hash so that agreement can be checked without
/// rehashing the entire log: `chain_0 = sha256("")`, and thereafter
/// `chain_n = sha256(chain_{n-1} as ASCII hex || record_bytes)`.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq, Default)]
pub struct EventLogRef {
    pub offset: u64,
    pub chain: String,
}

impl EventLogRef {
    /// The chain value of an empty log — the seed for generation 0.
    pub fn seed_chain() -> String {
        short_sha("")
    }

    /// Extend the chain with one raw event-log record (including its newline).
    pub fn extend_chain(prev: &str, record_bytes: &[u8]) -> String {
        let mut buf = Vec::with_capacity(prev.len() + record_bytes.len());
        buf.extend_from_slice(prev.as_bytes());
        buf.extend_from_slice(record_bytes);
        let mut s = crate::derive::sha256_hex_bytes(&buf);
        s.truncate(SHA_HEX_LEN);
        s
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq, Default)]
pub struct DictRef {
    pub records: u32,
    pub bytes: u64,
}

/// Declared column widths (D-028 #11): fixed in format v0, recorded so a
/// later widening is a format-version bump rather than a silent
/// reinterpretation of existing bytes.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Widths {
    pub entity_id: u8,
    pub row_id: u8,
    pub rel_code: u8,
}

impl Default for Widths {
    fn default() -> Self {
        Self {
            entity_id: 32,
            row_id: 32,
            rel_code: 16,
        }
    }
}

/// One immutable segment file. Boundary keys are the **full** 96-bit
/// composite keys (D-028 #4) so ordering never rests on a 64-bit prefix.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct SegmentEntry {
    pub file: String,
    pub rows: u32,
    pub key_lo: (i64, String),
    pub key_hi: (i64, String),
    pub vt_min: i64,
    pub vt_max: i64,
    pub vt_e_max: i64,
    pub tt_s_min: i64,
    pub tt_s_max: i64,
    pub rel_codes: Vec<u16>,
    pub n_closed_folded: u32,
    /// No row in this segment is closed — current-belief scans skip all
    /// visibility work (the overwhelmingly common case for event data).
    pub all_current: bool,
    pub sha: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq, Default)]
pub struct EdgeLanes {
    pub event: Vec<SegmentEntry>,
    pub interval: Vec<SegmentEntry>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq, Default)]
pub struct CloseRunRef {
    pub file: String,
    pub entries: u32,
    pub sha: String,
}

/// Incrementally maintained statistics — `stats()` is served from here and
/// never by scanning (spec §4.1).
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq, Default)]
pub struct Stats {
    pub n_entities: u32,
    pub n_node_versions: u64,
    pub n_edge_versions: u64,
    pub vt_min: Option<i64>,
    pub vt_max: Option<i64>,
    pub max_out_degree: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Manifest {
    pub format: u32,
    pub generation: u64,
    pub parent: Option<u64>,
    pub created_tt: i64,
    pub event_log: EventLogRef,
    pub dict: DictRef,
    pub widths: Widths,
    pub node_store: Vec<SegmentEntry>,
    pub edge_lanes: EdgeLanes,
    pub close_runs: Vec<CloseRunRef>,
    /// Next unused segment file id. Ids are never reused: an older manifest
    /// may still reference a file, and deletion happens only through gc,
    /// which never touches a file a retained generation names.
    pub next_segment_id: u64,
    pub stats: Stats,
    /// SHA of this manifest with this field blanked. Always last.
    pub manifest_sha: String,
}

impl Manifest {
    /// The empty store: generation 0, no data, seeded log chain.
    pub fn genesis() -> Self {
        let mut m = Self {
            format: FORMAT_VERSION,
            generation: 0,
            parent: None,
            created_tt: 0,
            event_log: EventLogRef {
                offset: 0,
                chain: EventLogRef::seed_chain(),
            },
            dict: DictRef::default(),
            widths: Widths::default(),
            node_store: Vec::new(),
            edge_lanes: EdgeLanes::default(),
            close_runs: Vec::new(),
            next_segment_id: 0,
            stats: Stats::default(),
            manifest_sha: String::new(),
        };
        m.seal();
        m
    }

    /// Start the successor generation, inheriting content by default.
    pub fn successor(&self, created_tt: i64) -> Self {
        let mut next = self.clone();
        next.generation = self.generation + 1;
        next.parent = Some(self.generation);
        next.created_tt = created_tt;
        next.manifest_sha = String::new();
        next
    }

    fn body_sha(&self) -> String {
        let mut blanked = self.clone();
        blanked.manifest_sha = String::new();
        short_sha(&serde_json::to_string(&blanked).expect("manifest is serializable"))
    }

    pub fn seal(&mut self) {
        self.manifest_sha = self.body_sha();
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).expect("manifest is serializable")
    }

    pub fn from_json(text: &str) -> Result<Self> {
        let m: Manifest = serde_json::from_str(text)
            .map_err(|e| EngineError::corrupt(format!("manifest is not valid JSON: {e}")))?;
        m.verify()?;
        Ok(m)
    }

    /// Structural checks every manifest must pass before it is trusted.
    pub fn verify(&self) -> Result<()> {
        if self.format != FORMAT_VERSION {
            return Err(EngineError::corrupt(format!(
                "manifest format {} is not supported by this build (expected {FORMAT_VERSION})",
                self.format
            )));
        }
        let expected = self.body_sha();
        if expected != self.manifest_sha {
            return Err(EngineError::corrupt(format!(
                "manifest checksum mismatch: computed {expected}, recorded {}",
                self.manifest_sha
            )));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn genesis_round_trips_and_verifies() {
        let m = Manifest::genesis();
        m.verify().unwrap();
        let back = Manifest::from_json(&m.to_json()).unwrap();
        assert_eq!(back, m);
        assert_eq!(back.generation, 0);
        assert_eq!(back.parent, None);
    }

    #[test]
    fn tampering_with_any_field_is_detected() {
        let m = Manifest::genesis();
        let mut tampered = m.clone();
        tampered.stats.n_entities = 7; // sha not recomputed — exactly the attack
        let err = match tampered.verify() {
            Ok(()) => panic!("tampered manifest must not verify"),
            Err(e) => e,
        };
        assert_eq!(err.category, crate::error::Category::Corrupt);

        // and through the JSON path
        let text = serde_json::to_string_pretty(&tampered).unwrap();
        assert!(Manifest::from_json(&text).is_err());
    }

    #[test]
    fn successor_links_to_parent_and_reseals() {
        let g0 = Manifest::genesis();
        let mut g1 = g0.successor(1234);
        g1.stats.n_entities = 3;
        g1.seal();
        g1.verify().unwrap();
        assert_eq!(g1.generation, 1);
        assert_eq!(g1.parent, Some(0));
        assert_eq!(g1.created_tt, 1234);
        assert_ne!(g1.manifest_sha, g0.manifest_sha);
    }

    #[test]
    fn log_chain_is_order_sensitive() {
        let seed = EventLogRef::seed_chain();
        let a = EventLogRef::extend_chain(&seed, b"{\"tt\":1}\n");
        let b = EventLogRef::extend_chain(&a, b"{\"tt\":2}\n");
        let swapped = EventLogRef::extend_chain(
            &EventLogRef::extend_chain(&seed, b"{\"tt\":2}\n"),
            b"{\"tt\":1}\n",
        );
        assert_ne!(b, swapped, "chain must depend on record order");
        assert_eq!(a.len(), SHA_HEX_LEN);
        // recomputing from the same prefix is deterministic
        assert_eq!(b, EventLogRef::extend_chain(&a, b"{\"tt\":2}\n"));
    }
}
