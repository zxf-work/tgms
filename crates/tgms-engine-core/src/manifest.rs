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
//!
//! This module owns the *logical* manifest — the document one generation
//! means. How it reaches disk (a checkpoint or a delta against its parent) is
//! `manifest_chain`'s business; a reader that has reconstructed a generation
//! holds exactly the `Manifest` a format-1 store would have written for the
//! same content, and `manifest_sha` is the digest of that logical document
//! either way.
//!
//! **What `manifest_sha` is, per format.** Formats 1 and 2 hash the whole
//! serialized document, which is O(live segments) — paid on every commit and,
//! worse, on every step of a chain replay. From **format 3** it is a Merkle
//! root over the ordered segment set ([`merkle`]), so a commit's append costs
//! O(appended + log n) and a replay step costs O(1)
//! (`docs/design/INCREMENTAL_MANIFEST_V2_DIAGNOSIS_2026-09-15.md` §4,
//! Addendum 3 ruling 1). The rule is chosen by the manifest's own `format`
//! field, never by what this build would prefer, so a format-2 store keeps
//! verifying against the digests it was written with.

use serde::{Deserialize, Serialize};

use crate::derive::sha256_hex;
use crate::error::{EngineError, Result};
use crate::{MANIFEST_FORMATS_READ_ONLY, MANIFEST_FORMAT_MERKLE, MANIFEST_FORMAT_VERSION};

/// Truncation used for file/manifest digests: 16 hex chars = 64 bits, enough
/// to detect corruption and mismatched pairings (not a security boundary).
pub const SHA_HEX_LEN: usize = 16;

pub(crate) fn short_sha(text: &str) -> String {
    sha256_hex(text)[..SHA_HEX_LEN].to_string()
}

/// `"1 or 2"` — the read-only formats, for an error a human reads.
pub(crate) fn read_only_list() -> String {
    let names: Vec<String> = MANIFEST_FORMATS_READ_ONLY
        .iter()
        .map(u32::to_string)
        .collect();
    names.join(" or ")
}

/// The Merkle digest that backs `manifest_sha` from format 3 on.
///
/// **Why a tree and not a sum.** Format 2's `manifest_sha` was
/// `sha256(whole document)`, which is O(segments) to compute and therefore
/// O(segments) *per commit* — 5.9 µs per segment per commit measured, and a
/// full 3 MB serialize-and-hash on every replay step at open
/// (`docs/design/INCREMENTAL_MANIFEST_V2_DIAGNOSIS_2026-09-15.md` §1-§2). A
/// homomorphic +/- digest over entry hashes would make both O(1), but it
/// loses **order**, and order is load-bearing here: `manifest_chain::apply`
/// defines the child as parent-minus-deletions-*then*-appends,
/// `patch_matches` validates that sequence in lockstep, and [`Manifest`]'s
/// `PartialEq` compares `Vec`s positionally. Two orderings of the same set
/// must not share a digest or `verify_files`' `reconstruction-mismatch` stops
/// detecting anything (Addendum 3 ruling 1).
///
/// **The construction.**
///
/// ```text
/// leaf_i   = sha256( 0x00 || lane || i_be64 || canonical(entry_i) )
/// internal = sha256( 0x01 || left || right )
/// empty    = sha256( 0x02 || lane )
/// ```
///
/// Each of the four lane sequences (`node_store`, `edge_lanes.event`,
/// `edge_lanes.interval`, `close_runs`) gets its own binary tree, built
/// bottom-up with the **last odd node promoted** unchanged to the next level.
/// The four lane roots are then combined in lane order by the same internal
/// rule, so the manifest root is a binary Merkle root over the four ordered
/// lane sequences.
///
/// *Why a tree per lane rather than one tree over the concatenation.* The
/// leaf preimage carries both a lane tag and a position, and a position alone
/// would be unique in a single concatenated sequence — the lane tag is there
/// because the index is per lane. It has to be: a commit appends to several
/// lanes at once, and in one concatenated sequence an append to `node_store`
/// would shift the position of every later lane's leaves, which is exactly
/// the O(n) the change exists to remove. Per-lane trees keep an append on the
/// right spine, O(m + log n).
///
/// **What the tags buy.** `0x00`/`0x01`/`0x02` domain-separate leaves,
/// internal nodes and empty lanes, so no leaf hash can be read as an internal
/// node (which is what would otherwise let a tree of one depth alias a tree
/// of another). `i_be64` puts position in the preimage, so a permutation of
/// the same entries is a different root. Truncation to 64 bits is the one
/// format 1 already shipped ([`SHA_HEX_LEN`]) and its scope is unchanged:
/// enough to detect corruption and mismatched pairings, not a security
/// boundary.
pub mod merkle {
    use serde::Serialize;
    use sha2::{Digest, Sha256};

    use super::{CloseRunRef, DictRef, EventLogRef, Manifest, SegmentEntry, Stats, Widths};

    /// A full 256-bit node value. Only the manifest digest truncates.
    pub type Hash = [u8; 32];

    const TAG_LEAF: u8 = 0x00;
    const TAG_INTERNAL: u8 = 0x01;
    const TAG_EMPTY: u8 = 0x02;

    /// Lane tags, in the order the roots are combined.
    pub const LANE_NODES: u8 = 0;
    pub const LANE_EDGES_EVENT: u8 = 1;
    pub const LANE_EDGES_INTERVAL: u8 = 2;
    pub const LANE_CLOSE_RUNS: u8 = 3;

    /// The value of `sha_kind` on every format-3 record: the digest rule is
    /// derivable from `format`, but a store an operator is staring at during
    /// an incident should say which rule it was written under.
    pub const SHA_KIND: &str = "merkle-v1";

    /// Prefix of the final digest preimage, so a manifest root can never be
    /// confused with any other sha this codebase computes.
    const DOMAIN: &[u8] = b"tgms.manifest.merkle-v1\x00";

    fn hash(parts: &[&[u8]]) -> Hash {
        let mut h = Sha256::new();
        for p in parts {
            h.update(p);
        }
        h.finalize().into()
    }

    /// `sha256(0x00 || lane || index || canonical)`.
    pub fn leaf(lane: u8, index: u64, canonical: &[u8]) -> Hash {
        hash(&[&[TAG_LEAF], &[lane], &index.to_be_bytes(), canonical])
    }

    /// `sha256(0x01 || left || right)`.
    pub fn internal(left: &Hash, right: &Hash) -> Hash {
        hash(&[&[TAG_INTERNAL], left, right])
    }

    /// `sha256(0x02 || lane)` — the root of a lane with no entries. Distinct
    /// per lane so an empty `node_store` and an empty `close_runs` are not
    /// interchangeable.
    pub fn empty(lane: u8) -> Hash {
        hash(&[&[TAG_EMPTY], &[lane]])
    }

    /// The canonical bytes of one entry: its JSON, in declaration order.
    /// Serde emits struct fields in declaration order and every field is a
    /// scalar, a `String` or a `Vec<u16>`, so this is deterministic without a
    /// canonicalization pass.
    pub fn canonical<T: Serialize>(entry: &T) -> Vec<u8> {
        serde_json::to_vec(entry).expect("manifest entries are serializable")
    }

    /// One level's tail: the last node, and the one before it.
    ///
    /// That is the whole state an append needs. Appending a leaf changes only
    /// the **last** node of every level (and may extend a level by one), so a
    /// level's earlier nodes can never be revisited; the second-to-last is
    /// kept because when a level has even length the new parent is
    /// `internal(second_last, last)`.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    struct Level {
        last: Hash,
        second_last: Option<Hash>,
    }

    /// The appendable Merkle tree of one lane.
    ///
    /// Holds O(log n) hashes, not the sequence: everything below the right
    /// spine is already folded into it and can never change, because the only
    /// mutation a commit performs is an append. A *deletion* (compaction, gc)
    /// is not expressible here and is not meant to be — the caller rebuilds
    /// wholesale with [`LaneTree::build`], which costs nothing extra because
    /// every path that deletes already forces a full checkpoint.
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub struct LaneTree {
        lane: u8,
        n: u64,
        /// Level 0 is the leaves; the top level always holds exactly one
        /// node, which is the root. Empty for an empty lane.
        levels: Vec<Level>,
    }

    impl LaneTree {
        pub fn empty_lane(lane: u8) -> Self {
            Self {
                lane,
                n: 0,
                levels: Vec::new(),
            }
        }

        pub fn len(&self) -> u64 {
            self.n
        }

        pub fn is_empty(&self) -> bool {
            self.n == 0
        }

        pub fn root(&self) -> Hash {
            match self.levels.last() {
                Some(top) => top.last,
                None => empty(self.lane),
            }
        }

        /// Build from the whole ordered sequence — O(n). The definition the
        /// incremental path must agree with: pair left to right at every
        /// level, promote a trailing odd node unchanged.
        pub fn build(lane: u8, leaves: &[Hash]) -> Self {
            let mut levels = Vec::new();
            if leaves.is_empty() {
                return Self {
                    lane,
                    n: 0,
                    levels,
                };
            }
            let mut cur: Vec<Hash> = leaves.to_vec();
            loop {
                levels.push(Level {
                    last: *cur.last().expect("a level is never empty here"),
                    second_last: (cur.len() >= 2).then(|| cur[cur.len() - 2]),
                });
                if cur.len() == 1 {
                    break;
                }
                let mut next = Vec::with_capacity(cur.len().div_ceil(2));
                let mut i = 0;
                while i + 1 < cur.len() {
                    next.push(internal(&cur[i], &cur[i + 1]));
                    i += 2;
                }
                if i < cur.len() {
                    next.push(cur[i]); // the last odd node, promoted
                }
                cur = next;
            }
            Self {
                lane,
                n: leaves.len() as u64,
                levels,
            }
        }

        /// Build from the entries themselves.
        pub fn from_entries<T: Serialize>(lane: u8, entries: &[T]) -> Self {
            let leaves: Vec<Hash> = entries
                .iter()
                .enumerate()
                .map(|(i, e)| leaf(lane, i as u64, &canonical(e)))
                .collect();
            Self::build(lane, &leaves)
        }

        /// Append one entry — O(log n) hashes, touching only the right spine.
        pub fn push<T: Serialize>(&mut self, entry: &T) {
            let leaf = leaf(self.lane, self.n, &canonical(entry));
            self.push_leaf(leaf);
        }

        /// Append a leaf whose hash is already known.
        ///
        /// The update rule, derived from the level definition: let level *k*
        /// have `old` nodes before and `new` after. Appending changes only
        /// that level's last node, so
        ///
        /// * `new > old` — a node was appended here; the previous last node
        ///   becomes the second-to-last;
        /// * otherwise the last node was replaced in place.
        ///
        /// and what the level hands upward is its own new last node when
        /// `new` is odd (the promoted trailing node) or
        /// `internal(second_last, last)` when it is even (the last full
        /// pair). The walk stops at the level where `new == 1`: that node is
        /// the root, and no level above it exists.
        pub fn push_leaf(&mut self, leaf: Hash) {
            let mut old = self.n;
            self.n += 1;
            let mut new = self.n;
            let mut value = leaf;
            let mut k = 0usize;
            loop {
                if k == self.levels.len() {
                    // a brand-new top level, reachable only when the level
                    // below just went from 1 node to 2
                    self.levels.push(Level {
                        last: value,
                        second_last: None,
                    });
                } else {
                    let level = &mut self.levels[k];
                    if new > old {
                        level.second_last = Some(level.last);
                    }
                    level.last = value;
                }
                if new == 1 {
                    debug_assert_eq!(self.levels.len(), k + 1, "no level above the root");
                    break;
                }
                value = if new % 2 == 1 {
                    value
                } else {
                    let left = self.levels[k]
                        .second_last
                        .expect("an even level has a left sibling for its last node");
                    internal(&left, &value)
                };
                old = old.div_ceil(2);
                new = new.div_ceil(2);
                k += 1;
            }
        }
    }

    /// The four lane trees of one manifest, and the root over them.
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub struct ManifestMerkle {
        nodes: LaneTree,
        edges_event: LaneTree,
        edges_interval: LaneTree,
        close_runs: LaneTree,
    }

    impl ManifestMerkle {
        /// O(n) over the whole manifest. Every path that deletes entries
        /// comes back through here; the commit path does not.
        pub fn from_manifest(m: &Manifest) -> Self {
            Self {
                nodes: LaneTree::from_entries(LANE_NODES, &m.node_store),
                edges_event: LaneTree::from_entries(LANE_EDGES_EVENT, &m.edge_lanes.event),
                edges_interval: LaneTree::from_entries(
                    LANE_EDGES_INTERVAL,
                    &m.edge_lanes.interval,
                ),
                close_runs: LaneTree::from_entries(LANE_CLOSE_RUNS, &m.close_runs),
            }
        }

        /// A binary Merkle root over the four lane roots, in lane order.
        pub fn root(&self) -> Hash {
            internal(
                &internal(&self.nodes.root(), &self.edges_event.root()),
                &internal(&self.edges_interval.root(), &self.close_runs.root()),
            )
        }

        /// Lane cardinalities, in lane order.
        pub fn lens(&self) -> [u64; 4] {
            [
                self.nodes.len(),
                self.edges_event.len(),
                self.edges_interval.len(),
                self.close_runs.len(),
            ]
        }

        pub fn push_node(&mut self, e: &SegmentEntry) {
            self.nodes.push(e);
        }

        pub fn push_edge_event(&mut self, e: &SegmentEntry) {
            self.edges_event.push(e);
        }

        pub fn push_edge_interval(&mut self, e: &SegmentEntry) {
            self.edges_interval.push(e);
        }

        pub fn push_close_run(&mut self, r: &CloseRunRef) {
            self.close_runs.push(r);
        }
    }

    /// The O(1) fields of a manifest, in a fixed order, plus the lane
    /// cardinalities.
    ///
    /// The lengths are not fields of [`Manifest`] — they are included so the
    /// digest binds the shape of the segment set explicitly rather than only
    /// through the tree, which costs four integers and removes a class of
    /// question nobody wants to have to reason about during an incident.
    #[derive(Serialize)]
    struct Scalars<'a> {
        format: u32,
        generation: u64,
        parent: Option<u64>,
        created_tt: i64,
        event_log: &'a EventLogRef,
        dict: &'a DictRef,
        widths: &'a Widths,
        next_segment_id: u64,
        stats: &'a Stats,
        lens: [u64; 4],
    }

    /// `trunc64( sha256( DOMAIN || canonical(scalars) || root ) )`.
    pub(super) fn manifest_digest(m: &Manifest, state: &ManifestMerkle) -> String {
        let scalars = Scalars {
            format: m.format,
            generation: m.generation,
            parent: m.parent,
            created_tt: m.created_tt,
            event_log: &m.event_log,
            dict: &m.dict,
            widths: &m.widths,
            next_segment_id: m.next_segment_id,
            stats: &m.stats,
            lens: state.lens(),
        };
        let scalar_bytes = serde_json::to_vec(&scalars).expect("scalars are serializable");
        let full = hash(&[DOMAIN, &scalar_bytes, &state.root()]);
        let mut out = String::with_capacity(super::SHA_HEX_LEN);
        for b in full.iter().take(super::SHA_HEX_LEN.div_ceil(2)) {
            out.push(char::from_digit((b >> 4) as u32, 16).expect("nibble"));
            out.push(char::from_digit((b & 0xf) as u32, 16).expect("nibble"));
        }
        out.truncate(super::SHA_HEX_LEN);
        out
    }

    /// Is the manifest set this state describes the one `m` holds? A cheap
    /// guard for the commit path's incremental digest: it cannot prove the
    /// contents agree, but it catches a state that has fallen out of step
    /// with the manifest's shape.
    pub fn matches_shape(m: &Manifest, state: &ManifestMerkle) -> bool {
        state.lens()
            == [
                m.node_store.len() as u64,
                m.edge_lanes.event.len() as u64,
                m.edge_lanes.interval.len() as u64,
                m.close_runs.len() as u64,
            ]
    }
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
            format: MANIFEST_FORMAT_VERSION,
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

    /// The digest this manifest *should* carry, computed **from scratch** —
    /// O(segments). Public because reconstruction has to re-derive it
    /// independently of the record that claimed it (`manifest_chain`).
    ///
    /// Which rule applies is decided by `format`, not by what this build
    /// prefers, so a format-2 store stays verifiable byte-for-byte after the
    /// format-3 change: formats 1 and 2 hash the whole blanked document,
    /// format 3 takes the Merkle root over the ordered segment set. `format`
    /// is itself inside both preimages, so the choice cannot be flipped
    /// without invalidating the digest.
    pub fn digest(&self) -> String {
        if self.format >= MANIFEST_FORMAT_MERKLE {
            merkle::manifest_digest(self, &merkle::ManifestMerkle::from_manifest(self))
        } else {
            self.legacy_body_sha()
        }
    }

    /// The same digest, from a Merkle state the caller already maintains —
    /// O(1). This is the one the commit path and the replay loop call; every
    /// other caller wants [`Manifest::digest`].
    ///
    /// Falls back to the from-scratch rule below format 3, where there is no
    /// state to maintain.
    pub fn digest_with(&self, state: &merkle::ManifestMerkle) -> String {
        if self.format >= MANIFEST_FORMAT_MERKLE {
            debug_assert!(
                merkle::matches_shape(self, state),
                "the Merkle state has fallen out of step with the manifest"
            );
            merkle::manifest_digest(self, state)
        } else {
            self.legacy_body_sha()
        }
    }

    /// The independent oracle `verify_full` re-derives the head generation's
    /// digest with (memo §4(d)): the *full-document* recomputation, never an
    /// incremental update, so a bug in the incremental path is caught by a
    /// disagreement rather than being self-consistent. Identical to
    /// [`Manifest::digest`] by construction — the name is the contract.
    pub fn body_sha_canonical(&self) -> String {
        self.digest()
    }

    /// Format 1/2: the sha of the whole document with `manifest_sha` blanked.
    fn legacy_body_sha(&self) -> String {
        let mut blanked = self.clone();
        blanked.manifest_sha = String::new();
        short_sha(&serde_json::to_string(&blanked).expect("manifest is serializable"))
    }

    pub fn seal(&mut self) {
        self.manifest_sha = self.digest();
    }

    /// Seal from a maintained Merkle state — O(1) where [`Manifest::seal`] is
    /// O(segments). The commit path's whole point.
    pub fn seal_with(&mut self, state: &merkle::ManifestMerkle) {
        self.manifest_sha = String::new();
        self.manifest_sha = self.digest_with(state);
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
    ///
    /// Older formats are *accepted* here rather than rejected (memo §4,
    /// "Migration"): a store a previous engine wrote must still open, and
    /// still verify, so that `tgms store upgrade-manifests` has something to
    /// read — and each is checked under its own digest rule, so a format-2
    /// store verifies exactly as it did before format 3 existed. Writing is
    /// where the line is drawn: every publication path asks
    /// `manifest_chain::is_writable_format` first, so an older store is
    /// read-only rather than silently reinterpreted.
    pub fn verify(&self) -> Result<()> {
        if self.format != MANIFEST_FORMAT_VERSION
            && !MANIFEST_FORMATS_READ_ONLY.contains(&self.format)
        {
            return Err(EngineError::corrupt(format!(
                "manifest format {} is not supported by this build \
                 (expected {MANIFEST_FORMAT_VERSION}, or {} read-only)",
                self.format,
                read_only_list()
            )));
        }
        let expected = self.digest();
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
mod merkle_tests {
    use super::merkle::{self, Hash, LaneTree, ManifestMerkle, LANE_CLOSE_RUNS, LANE_NODES};
    use super::*;

    fn seg(id: u64) -> SegmentEntry {
        SegmentEntry {
            file: format!("seg/{id:012}.tgs"),
            rows: 10 + id as u32,
            key_lo: (id as i64, "a".into()),
            key_hi: (id as i64 + 1, "b".into()),
            vt_min: 0,
            vt_max: 5,
            vt_e_max: 6,
            tt_s_min: 100,
            tt_s_max: 100,
            rel_codes: vec![1, 2],
            n_closed_folded: 0,
            all_current: true,
            sha: format!("{id:016x}"),
        }
    }

    fn run(id: u64) -> CloseRunRef {
        CloseRunRef {
            file: format!("close/{id:012}.tgc"),
            entries: id as u32,
            sha: format!("{id:016x}"),
        }
    }

    /// An **independent** definition of the root, written recursively rather
    /// than by levels, so agreeing with [`LaneTree::build`] means something.
    /// "Pair left to right, promote a trailing odd node" is the same tree as
    /// "split at the largest power of two below n", which is what this is.
    fn reference_root(lane: u8, leaves: &[Hash]) -> Hash {
        match leaves.len() {
            0 => merkle::empty(lane),
            1 => leaves[0],
            n => {
                let mut k = 1usize;
                while k * 2 < n {
                    k *= 2;
                }
                merkle::internal(
                    &reference_root(lane, &leaves[..k]),
                    &reference_root(lane, &leaves[k..]),
                )
            }
        }
    }

    fn leaves_of(lane: u8, entries: &[SegmentEntry]) -> Vec<Hash> {
        entries
            .iter()
            .enumerate()
            .map(|(i, e)| merkle::leaf(lane, i as u64, &merkle::canonical(e)))
            .collect()
    }

    #[test]
    fn the_root_matches_a_reference_recomputation_at_every_length() {
        // every length up to 33 covers each parity and each level-count
        // boundary, including the promotions the odd lengths force
        for n in 0..34u64 {
            let entries: Vec<SegmentEntry> = (0..n).map(seg).collect();
            let leaves = leaves_of(LANE_NODES, &entries);
            let built = LaneTree::from_entries(LANE_NODES, &entries);
            assert_eq!(built.len(), n);
            assert_eq!(
                built.root(),
                reference_root(LANE_NODES, &leaves),
                "level-built root disagrees with the reference at n={n}"
            );
        }
    }

    #[test]
    fn appending_reaches_the_same_root_as_building_from_scratch() {
        // the property the whole change rests on: an O(log n) append and an
        // O(n) rebuild are the same function of the same ordered set
        let mut incremental = LaneTree::empty_lane(LANE_NODES);
        let mut entries: Vec<SegmentEntry> = Vec::new();
        assert_eq!(incremental.root(), merkle::empty(LANE_NODES));
        for id in 0..200u64 {
            let e = seg(id);
            incremental.push(&e);
            entries.push(e);
            let scratch = LaneTree::from_entries(LANE_NODES, &entries);
            assert_eq!(
                incremental.root(),
                scratch.root(),
                "incremental and from-scratch roots diverge at {} entries",
                entries.len()
            );
            assert_eq!(incremental, scratch, "the spine state itself diverges");
        }
    }

    #[test]
    fn an_append_touches_only_the_right_spine() {
        // O(m + log n), not O(n): appending must not recompute the nodes that
        // are already folded in. Checked structurally — the levels a rebuild
        // of the *prefix* produced are still the prefix's, so the only nodes
        // whose values could have moved are the ones on the path up.
        let entries: Vec<SegmentEntry> = (0..64).map(seg).collect();
        let full = LaneTree::from_entries(LANE_NODES, &entries);

        // the left half's root is a subtree of the full tree, and stays put
        // as the right half is appended one entry at a time
        let mut growing = LaneTree::from_entries(LANE_NODES, &entries[..32]);
        let left_root = growing.root();
        for e in &entries[32..] {
            growing.push(e);
        }
        assert_eq!(growing.root(), full.root());
        // 64 = two perfect halves, so the full root is exactly the pair
        let right = LaneTree::build(
            LANE_NODES,
            &entries[32..]
                .iter()
                .enumerate()
                .map(|(i, e)| merkle::leaf(LANE_NODES, 32 + i as u64, &merkle::canonical(e)))
                .collect::<Vec<_>>(),
        );
        assert_eq!(
            full.root(),
            merkle::internal(&left_root, &right.root()),
            "the left half's root must survive the right half being appended"
        );
    }

    #[test]
    fn position_tagging_distinguishes_permutations() {
        let a = seg(1);
        let b = seg(2);
        let forward = LaneTree::from_entries(LANE_NODES, &[a.clone(), b.clone()]);
        let backward = LaneTree::from_entries(LANE_NODES, &[b.clone(), a.clone()]);
        assert_ne!(
            forward.root(),
            backward.root(),
            "a swap of two entries must change the root — order is load-bearing"
        );

        // and a longer permutation: rotating a five-entry lane
        let five: Vec<SegmentEntry> = (0..5).map(seg).collect();
        let mut rotated = five.clone();
        rotated.rotate_left(1);
        assert_ne!(
            LaneTree::from_entries(LANE_NODES, &five).root(),
            LaneTree::from_entries(LANE_NODES, &rotated).root()
        );

        // the position tag is what does it: the same entry at index 0 and at
        // index 1 has different leaves
        assert_ne!(
            merkle::leaf(LANE_NODES, 0, &merkle::canonical(&a)),
            merkle::leaf(LANE_NODES, 1, &merkle::canonical(&a))
        );
    }

    #[test]
    fn domain_separation_keeps_leaves_nodes_and_lanes_apart() {
        let a = merkle::leaf(LANE_NODES, 0, b"x");
        let b = merkle::leaf(LANE_NODES, 1, b"y");
        // a leaf's preimage starts 0x00, an internal node's 0x01, an empty
        // lane's 0x02 — so no one-leaf tree can be read as a two-leaf tree
        assert_ne!(merkle::internal(&a, &b), a);
        assert_ne!(merkle::empty(LANE_NODES), a);
        assert_ne!(merkle::empty(LANE_NODES), merkle::internal(&a, &b));

        // and the lane tag separates the four sequences: the same bytes at
        // the same index in a different lane is a different leaf, and an
        // empty node lane is not an empty close-run lane
        assert_ne!(
            merkle::leaf(LANE_NODES, 0, b"x"),
            merkle::leaf(LANE_CLOSE_RUNS, 0, b"x")
        );
        assert_ne!(merkle::empty(LANE_NODES), merkle::empty(LANE_CLOSE_RUNS));

        // an entry moved between lanes changes the manifest root, even
        // though the *set* of entries is unchanged
        let mut left = Manifest::genesis();
        left.node_store.push(seg(0));
        let mut right = Manifest::genesis();
        right.edge_lanes.event.push(seg(0));
        assert_ne!(
            ManifestMerkle::from_manifest(&left).root(),
            ManifestMerkle::from_manifest(&right).root()
        );
    }

    #[test]
    fn the_manifest_digest_is_incremental_across_a_commit_shaped_append() {
        // exactly what the commit path does: push to several lanes at once,
        // move the scalars, and seal from the maintained state
        let mut m = Manifest::genesis();
        let mut state = ManifestMerkle::from_manifest(&m);
        for g in 1..=40u64 {
            let node = seg(g * 3);
            let event = seg(g * 3 + 1);
            m.generation = g;
            m.parent = Some(g - 1);
            m.created_tt = g as i64 * 10;
            m.next_segment_id += 2;
            m.stats.n_node_versions += 10;
            m.node_store.push(node.clone());
            state.push_node(&node);
            m.edge_lanes.event.push(event.clone());
            state.push_edge_event(&event);
            if g % 7 == 0 {
                let r = run(g);
                m.close_runs.push(r.clone());
                state.push_close_run(&r);
            }
            m.seal_with(&state);
            assert_eq!(
                m.manifest_sha,
                m.body_sha_canonical(),
                "the incremental digest must equal the oracle at generation {g}"
            );
            m.verify().unwrap();
        }
        assert_eq!(state, ManifestMerkle::from_manifest(&m));
    }

    #[test]
    fn the_digest_covers_every_scalar_and_every_entry() {
        let mut m = Manifest::genesis();
        m.node_store.push(seg(0));
        m.edge_lanes.interval.push(seg(1));
        m.close_runs.push(run(2));
        m.next_segment_id = 3;
        m.seal();
        m.verify().unwrap();

        for mutate in [
            (|m: &mut Manifest| m.stats.n_entities = 7) as fn(&mut Manifest),
            |m: &mut Manifest| m.created_tt = -1,
            |m: &mut Manifest| m.dict.records = 99,
            |m: &mut Manifest| m.event_log.offset = 5,
            |m: &mut Manifest| m.next_segment_id = 400,
            |m: &mut Manifest| m.widths.rel_code = 32,
            |m: &mut Manifest| m.parent = Some(9),
            |m: &mut Manifest| m.generation = 9,
            |m: &mut Manifest| m.node_store[0].rows = 1,
            |m: &mut Manifest| m.node_store[0].sha = "deadbeef".into(),
            |m: &mut Manifest| m.edge_lanes.interval[0].all_current = false,
            |m: &mut Manifest| m.close_runs[0].entries = 42,
            |m: &mut Manifest| m.node_store.clear(),
            |m: &mut Manifest| m.close_runs.push(run(3)),
        ] {
            let mut t = m.clone();
            mutate(&mut t);
            assert_ne!(t.digest(), m.manifest_sha, "an edit escaped the digest");
            assert!(t.verify().is_err());
        }
    }

    #[test]
    fn a_format_2_manifest_keeps_its_format_2_digest() {
        // the read-only promise: format 3 must not reinterpret a store the
        // previous engine wrote. The digest rule follows `format`.
        let mut legacy = Manifest::genesis();
        legacy.format = 2;
        legacy.node_store.push(seg(0));
        legacy.seal();
        legacy.verify().unwrap();

        // that value is the whole-document sha, not the Merkle root
        let mut blanked = legacy.clone();
        blanked.manifest_sha = String::new();
        assert_eq!(
            legacy.manifest_sha,
            short_sha(&serde_json::to_string(&blanked).unwrap())
        );

        // and the same content at format 3 hashes differently — which is why
        // this is a format bump and not a silent change
        let mut modern = legacy.clone();
        modern.format = MANIFEST_FORMAT_VERSION;
        modern.seal();
        assert_ne!(modern.manifest_sha, legacy.manifest_sha);
        modern.verify().unwrap();
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
    fn a_format_1_manifest_still_verifies_but_is_not_writable() {
        // migration (memo §4): the previous engine's documents must open, or
        // there is nothing for `upgrade-manifests` to convert
        let mut legacy = Manifest::genesis();
        legacy.format = crate::FORMAT_LEGACY;
        legacy.seal();
        legacy.verify().unwrap();
        assert_eq!(Manifest::from_json(&legacy.to_json()).unwrap(), legacy);
        assert!(!crate::manifest_chain::is_writable_format(legacy.format));
        assert!(crate::manifest_chain::is_writable_format(
            Manifest::genesis().format
        ));

        // a format from the future is still rejected outright
        let mut future = Manifest::genesis();
        future.format = MANIFEST_FORMAT_VERSION + 1;
        future.seal();
        assert_eq!(
            future.verify().unwrap_err().category,
            crate::error::Category::Corrupt
        );
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
