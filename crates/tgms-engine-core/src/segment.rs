//! Immutable columnar segment files — the `.tgs` format (spec §4.3).
//!
//! A segment is written once and never modified. That single property is what
//! buys snapshot isolation, safe `mmap`, and lock-free concurrent readers: a
//! reader holding a generation can map these bytes and know nothing will
//! change underneath it.
//!
//! Layout:
//!
//! ```text
//! magic "TGSG" | format u32 | header_len u32 | header JSON | pad to 64
//! <data_start>  column extents, each padded to 64 bytes
//!               string table
//! <footer>      footer JSON | footer_len u32 | magic "TGSE"
//! ```
//!
//! Column offsets in the header are relative to `data_start`, so they do not
//! depend on how long the header serializes to.
//!
//! Every column extent and the string table carry a CRC32C, and the whole
//! pre-footer body carries a SHA. The trailing magic doubles as the
//! *complete marker*: a file without it was never finished, which is exactly
//! what a crash mid-write leaves behind.
//!
//! Hot columns are fixed-width and 64-byte aligned so a scan streams cache
//! lines and the typed views below are zero-copy. Everything variable-width
//! (props, disc, labels) lives out-of-line in the string table, referenced by
//! `u32` index — which is also why event data compresses so well later: the
//! distinct payloads in a segment are usually a handful.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::derive::{sha256_hex_bytes, Id96};
use crate::error::{EngineError, Result};
use crate::manifest::SHA_HEX_LEN;
use crate::row::{EdgeRow, Lane, NodeRow, RowKind};
use crate::FORMAT_VERSION;

const MAGIC: &[u8; 4] = b"TGSG";
const FOOTER_MAGIC: &[u8; 4] = b"TGSE";
const ALIGN: usize = 64;
const PREAMBLE: usize = 12; // magic + format + header_len

/// Reference into the segment string table meaning SQL NULL (`None`).
pub const NULL_REF: u32 = u32::MAX;

#[derive(Serialize, Deserialize, Clone, Copy, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum DType {
    I64,
    U64,
    U32,
    U16,
}

impl DType {
    const fn size(self) -> usize {
        match self {
            DType::I64 | DType::U64 => 8,
            DType::U32 => 4,
            DType::U16 => 2,
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct ColumnDesc {
    pub name: String,
    pub dtype: DType,
    /// Offset from `data_start`, always a multiple of 64.
    pub offset: u64,
    pub bytes: u64,
    /// 0 = raw little-endian. Codec ids are reserved from format v0 so that
    /// adding compression later is a value change, not a format break.
    pub codec: u32,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct SegmentHeader {
    pub format: u32,
    pub kind: RowKind,
    pub lane: Lane,
    pub rows: u32,
    pub block_rows: u32,
    /// Full 96-bit composite boundary keys (D-028 #4).
    pub key_lo: (i64, String),
    pub key_hi: (i64, String),
    pub vt_min: i64,
    pub vt_max: i64,
    /// Max `vt_e` in the segment — the cross-segment overlap bound.
    pub vt_e_max: i64,
    /// Run-length encoded transaction times: `(first_row, tt_s)`. A batch
    /// writes one entry; only compaction produces more.
    pub tt_s_runs: Vec<(u32, i64)>,
    pub rel_types: Vec<String>,
    pub columns: Vec<ColumnDesc>,
    /// `vt_e` is omitted because every row is instantaneous (`vt_s + 1`).
    pub vt_e_elided: bool,
    pub strings_offset: u64,
    pub strings_bytes: u64,
    pub strings_count: u32,
    /// Folded closes: sorted `(row, tt_e)`. Empty means every row in this
    /// segment is still believed — the `all_current` fast path (D-028 #7).
    #[serde(default)]
    pub closed_rows: Vec<(u32, i64)>,
}

impl SegmentHeader {
    pub fn column(&self, name: &str) -> Option<&ColumnDesc> {
        self.columns.iter().find(|c| c.name == name)
    }

    pub fn tt_s_at(&self, row: u32) -> Result<i64> {
        self.tt_s_runs
            .iter()
            .rev()
            .find(|(start, _)| *start <= row)
            .map(|(_, tt)| *tt)
            .ok_or_else(|| EngineError::corrupt(format!("no tt_s run covers row {row}")))
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
struct SegmentFooter {
    extent_crcs: Vec<u32>,
    strings_crc: u32,
    rows: u64,
    /// SHA of every byte before the footer.
    body_sha: String,
}

// --------------------------------------------------------------------- //
// string table                                                          //
// --------------------------------------------------------------------- //

/// Deduplicating string pool. Index 0 is always `""`, so a "no value" ref is
/// cheap; genuine NULL uses [`NULL_REF`] and is distinct from empty.
#[derive(Default)]
pub struct StringTable {
    items: Vec<String>,
    lookup: HashMap<String, u32>,
}

impl StringTable {
    pub fn new() -> Self {
        let mut t = Self::default();
        t.intern("");
        t
    }

    pub fn intern(&mut self, s: &str) -> u32 {
        if let Some(&i) = self.lookup.get(s) {
            return i;
        }
        let i = self.items.len() as u32;
        self.items.push(s.to_string());
        self.lookup.insert(s.to_string(), i);
        i
    }

    pub fn intern_opt(&mut self, s: Option<&str>) -> u32 {
        match s {
            None => NULL_REF,
            Some(v) => self.intern(v),
        }
    }

    pub fn len(&self) -> u32 {
        self.items.len() as u32
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    /// `count u32 | offsets u32 x (count+1) | utf-8 bytes`
    fn encode(&self) -> Vec<u8> {
        let payload: usize = self.items.iter().map(|s| s.len()).sum();
        let mut out = Vec::with_capacity(4 + 4 * (self.items.len() + 1) + payload);
        out.extend_from_slice(&(self.items.len() as u32).to_le_bytes());
        let mut cursor = 0u32;
        for s in &self.items {
            out.extend_from_slice(&cursor.to_le_bytes());
            cursor += s.len() as u32;
        }
        out.extend_from_slice(&cursor.to_le_bytes());
        for s in &self.items {
            out.extend_from_slice(s.as_bytes());
        }
        out
    }
}

/// Read-side view over an encoded string table.
pub struct StringView<'a> {
    offsets: &'a [u8],
    payload: &'a [u8],
    count: u32,
}

impl<'a> StringView<'a> {
    fn parse(bytes: &'a [u8]) -> Result<Self> {
        let count = read_u32(bytes, 0)?;
        let table_end = 4 + 4 * (count as usize + 1);
        if bytes.len() < table_end {
            return Err(EngineError::corrupt("string table offsets are truncated"));
        }
        Ok(Self {
            offsets: &bytes[4..table_end],
            payload: &bytes[table_end..],
            count,
        })
    }

    pub fn count(&self) -> u32 {
        self.count
    }

    pub fn get(&self, idx: u32) -> Result<&'a str> {
        if idx >= self.count {
            return Err(EngineError::corrupt(format!(
                "string ref {idx} out of range (table holds {})",
                self.count
            )));
        }
        let i = idx as usize * 4;
        let start = u32::from_le_bytes(self.offsets[i..i + 4].try_into().expect("4 bytes")) as usize;
        let end =
            u32::from_le_bytes(self.offsets[i + 4..i + 8].try_into().expect("4 bytes")) as usize;
        let raw = self
            .payload
            .get(start..end)
            .ok_or_else(|| EngineError::corrupt(format!("string ref {idx} spans past the table")))?;
        std::str::from_utf8(raw)
            .map_err(|e| EngineError::corrupt(format!("string ref {idx} is not UTF-8: {e}")))
    }

    /// `None` for [`NULL_REF`], otherwise the interned string.
    pub fn get_opt(&self, idx: u32) -> Result<Option<&'a str>> {
        if idx == NULL_REF {
            Ok(None)
        } else {
            self.get(idx).map(Some)
        }
    }
}

// --------------------------------------------------------------------- //
// writing                                                               //
// --------------------------------------------------------------------- //

struct ColumnBuilder {
    name: &'static str,
    dtype: DType,
    bytes: Vec<u8>,
}

impl ColumnBuilder {
    fn new(name: &'static str, dtype: DType, rows: usize) -> Self {
        Self {
            name,
            dtype,
            bytes: Vec::with_capacity(rows * dtype.size()),
        }
    }

    fn push_i64(&mut self, v: i64) {
        self.bytes.extend_from_slice(&v.to_le_bytes());
    }
    fn push_u64(&mut self, v: u64) {
        self.bytes.extend_from_slice(&v.to_le_bytes());
    }
    fn push_u32(&mut self, v: u32) {
        self.bytes.extend_from_slice(&v.to_le_bytes());
    }
    fn push_u16(&mut self, v: u16) {
        self.bytes.extend_from_slice(&v.to_le_bytes());
    }
}

/// Everything the caller must decide before rows become a file.
pub struct SegmentSpec {
    pub lane: Lane,
    pub block_rows: u32,
    /// Closes already known at seal time — a version created and corrected
    /// inside one batch. Sorted `(row, tt_e)`.
    pub closed_rows: Vec<(u32, i64)>,
}

impl Default for SegmentSpec {
    fn default() -> Self {
        Self {
            lane: Lane::Event,
            block_rows: crate::defaults::BLOCK_ROWS,
            closed_rows: Vec::new(),
        }
    }
}

/// Write `rows` (already sorted by `(vt_s, vid)`) as one segment file.
pub fn write_edge_segment(path: &Path, rows: &[EdgeRow], spec: &SegmentSpec) -> Result<()> {
    if rows.is_empty() {
        return Err(EngineError::invariant("refusing to write an empty segment"));
    }
    debug_assert!(
        rows.windows(2).all(|w| w[0].sort_key() <= w[1].sort_key()),
        "rows must be sorted by (vt_s, vid) before writing"
    );

    let mut strings = StringTable::new();
    let mut rel_types: Vec<String> = Vec::new();
    let n = rows.len();

    let vt_e_elided = rows.iter().all(EdgeRow::is_instantaneous);
    let mut c_vt_s = ColumnBuilder::new("vt_s", DType::I64, n);
    let mut c_src = ColumnBuilder::new("src_id", DType::U32, n);
    let mut c_dst = ColumnBuilder::new("dst_id", DType::U32, n);
    let mut c_rel = ColumnBuilder::new("rel_code", DType::U16, n);
    let mut c_vid_hi = ColumnBuilder::new("vid64", DType::U64, n);
    let mut c_vid_lo = ColumnBuilder::new("vid_lo32", DType::U32, n);
    let mut c_vt_e = ColumnBuilder::new("vt_e", DType::I64, if vt_e_elided { 0 } else { n });
    let mut c_props = ColumnBuilder::new("props_ref", DType::U32, n);
    let mut c_disc = ColumnBuilder::new("disc_ref", DType::U32, n);
    let mut c_source = ColumnBuilder::new("source_ref", DType::U32, n);
    let mut c_prov = ColumnBuilder::new("prov_ref", DType::U32, n);

    let mut tt_s_runs: Vec<(u32, i64)> = Vec::new();
    let (mut vt_min, mut vt_max, mut vt_e_max) = (i64::MAX, i64::MIN, i64::MIN);

    for (i, r) in rows.iter().enumerate() {
        let rel_code = match rel_types.iter().position(|t| t == &r.rel_type) {
            Some(p) => p as u16,
            None => {
                if rel_types.len() == u16::MAX as usize {
                    return Err(EngineError::capacity(
                        "a segment cannot hold more than 65535 distinct rel_types",
                    ));
                }
                rel_types.push(r.rel_type.clone());
                (rel_types.len() - 1) as u16
            }
        };
        c_vt_s.push_i64(r.vt_s);
        c_src.push_u32(r.src_id);
        c_dst.push_u32(r.dst_id);
        c_rel.push_u16(rel_code);
        c_vid_hi.push_u64(r.vid.hi);
        c_vid_lo.push_u32(r.vid.lo);
        if !vt_e_elided {
            c_vt_e.push_i64(r.vt_e);
        }
        c_props.push_u32(strings.intern(&r.props));
        c_disc.push_u32(strings.intern(&r.disc));
        c_source.push_u32(strings.intern(&r.source));
        c_prov.push_u32(strings.intern_opt(r.provenance_ref.as_deref()));

        if tt_s_runs.last().map(|(_, tt)| *tt) != Some(r.tt_s) {
            tt_s_runs.push((i as u32, r.tt_s));
        }
        vt_min = vt_min.min(r.vt_s);
        vt_max = vt_max.max(r.vt_s);
        vt_e_max = vt_e_max.max(r.vt_e);
    }

    let mut columns = vec![c_vt_s, c_src, c_dst, c_rel, c_vid_hi, c_vid_lo];
    if !vt_e_elided {
        columns.push(c_vt_e);
    }
    columns.extend([c_props, c_disc, c_source, c_prov]);

    let first = &rows[0];
    let last = &rows[n - 1];
    write_segment(
        path,
        SegmentHeader {
            format: FORMAT_VERSION,
            kind: RowKind::Edge,
            lane: spec.lane,
            rows: n as u32,
            block_rows: spec.block_rows,
            key_lo: (first.vt_s, first.vid.to_hex()),
            key_hi: (last.vt_s, last.vid.to_hex()),
            vt_min,
            vt_max,
            vt_e_max,
            tt_s_runs,
            rel_types,
            columns: Vec::new(), // filled by write_segment
            vt_e_elided,
            strings_offset: 0,
            strings_bytes: 0,
            strings_count: strings.len(),
            closed_rows: spec.closed_rows.clone(),
        },
        columns,
        &strings,
    )
}

/// Write node versions. Nodes are identity-clustered rather than
/// vt-partitioned (D-028 #15), but share the segment container.
pub fn write_node_segment(path: &Path, rows: &[NodeRow], spec: &SegmentSpec) -> Result<()> {
    if rows.is_empty() {
        return Err(EngineError::invariant("refusing to write an empty segment"));
    }
    let mut strings = StringTable::new();
    let n = rows.len();
    let vt_e_elided = rows.iter().all(NodeRow::is_instantaneous);

    let mut c_vt_s = ColumnBuilder::new("vt_s", DType::I64, n);
    let mut c_uid = ColumnBuilder::new("uid_id", DType::U32, n);
    let mut c_vid_hi = ColumnBuilder::new("vid64", DType::U64, n);
    let mut c_vid_lo = ColumnBuilder::new("vid_lo32", DType::U32, n);
    let mut c_vt_e = ColumnBuilder::new("vt_e", DType::I64, if vt_e_elided { 0 } else { n });
    let mut c_label = ColumnBuilder::new("label_ref", DType::U32, n);
    let mut c_props = ColumnBuilder::new("props_ref", DType::U32, n);
    // `name` promoted out of the props blob into its own column, so entity
    // resolution can scan it without parsing JSON per row. It is duplicated,
    // never moved: `props` is returned byte-identical because the store
    // digest is computed over that exact text.
    let mut c_name = ColumnBuilder::new("name_ref", DType::U32, n);
    let mut c_source = ColumnBuilder::new("source_ref", DType::U32, n);
    let mut c_prov = ColumnBuilder::new("prov_ref", DType::U32, n);

    let mut tt_s_runs: Vec<(u32, i64)> = Vec::new();
    let (mut vt_min, mut vt_max, mut vt_e_max) = (i64::MAX, i64::MIN, i64::MIN);

    for (i, r) in rows.iter().enumerate() {
        c_vt_s.push_i64(r.vt_s);
        c_uid.push_u32(r.uid_id);
        c_vid_hi.push_u64(r.vid.hi);
        c_vid_lo.push_u32(r.vid.lo);
        if !vt_e_elided {
            c_vt_e.push_i64(r.vt_e);
        }
        c_label.push_u32(strings.intern(&r.label));
        c_props.push_u32(strings.intern(&r.props));
        c_name.push_u32(match name_of(&r.props) {
            Some(v) => strings.intern(&v),
            None => NULL_REF,
        });
        c_source.push_u32(strings.intern(&r.source));
        c_prov.push_u32(strings.intern_opt(r.provenance_ref.as_deref()));

        if tt_s_runs.last().map(|(_, tt)| *tt) != Some(r.tt_s) {
            tt_s_runs.push((i as u32, r.tt_s));
        }
        vt_min = vt_min.min(r.vt_s);
        vt_max = vt_max.max(r.vt_s);
        vt_e_max = vt_e_max.max(r.vt_e);
    }

    let mut columns = vec![c_vt_s, c_uid, c_vid_hi, c_vid_lo];
    if !vt_e_elided {
        columns.push(c_vt_e);
    }
    columns.extend([c_label, c_props, c_name, c_source, c_prov]);

    let first = &rows[0];
    let last = &rows[n - 1];
    write_segment(
        path,
        SegmentHeader {
            format: FORMAT_VERSION,
            kind: RowKind::Node,
            lane: spec.lane,
            rows: n as u32,
            block_rows: spec.block_rows,
            key_lo: (first.vt_s, first.vid.to_hex()),
            key_hi: (last.vt_s, last.vid.to_hex()),
            vt_min,
            vt_max,
            vt_e_max,
            tt_s_runs,
            rel_types: Vec::new(),
            columns: Vec::new(),
            vt_e_elided,
            strings_offset: 0,
            strings_bytes: 0,
            strings_count: strings.len(),
            closed_rows: spec.closed_rows.clone(),
        },
        columns,
        &strings,
    )
}

fn write_segment(
    path: &Path,
    mut header: SegmentHeader,
    columns: Vec<ColumnBuilder>,
    strings: &StringTable,
) -> Result<()> {
    // Offsets are relative to data_start, so they do not depend on how long
    // the header serializes to — which would otherwise be circular.
    let mut descs = Vec::with_capacity(columns.len());
    let mut cursor = 0u64;
    for c in &columns {
        descs.push(ColumnDesc {
            name: c.name.to_string(),
            dtype: c.dtype,
            offset: cursor,
            bytes: c.bytes.len() as u64,
            codec: 0,
        });
        cursor += align_up(c.bytes.len()) as u64;
    }
    let encoded_strings = strings.encode();
    header.columns = descs;
    header.strings_offset = cursor;
    header.strings_bytes = encoded_strings.len() as u64;

    let header_json = serde_json::to_vec(&header).expect("segment header is serializable");
    let data_start = align_up(PREAMBLE + header_json.len());

    let mut buf = Vec::with_capacity(data_start + cursor as usize + encoded_strings.len() + 512);
    buf.extend_from_slice(MAGIC);
    buf.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    buf.extend_from_slice(&(header_json.len() as u32).to_le_bytes());
    buf.extend_from_slice(&header_json);
    pad_to(&mut buf, data_start);

    let mut extent_crcs = Vec::with_capacity(columns.len());
    for c in &columns {
        extent_crcs.push(crc32c::crc32c(&c.bytes));
        buf.extend_from_slice(&c.bytes);
        let padded = align_up(buf.len());
        pad_to(&mut buf, padded);
    }
    let strings_crc = crc32c::crc32c(&encoded_strings);
    buf.extend_from_slice(&encoded_strings);

    let footer = SegmentFooter {
        extent_crcs,
        strings_crc,
        rows: header.rows as u64,
        body_sha: sha256_hex_bytes(&buf)[..SHA_HEX_LEN].to_string(),
    };
    let footer_json = serde_json::to_vec(&footer).expect("segment footer is serializable");
    buf.extend_from_slice(&footer_json);
    buf.extend_from_slice(&(footer_json.len() as u32).to_le_bytes());
    buf.extend_from_slice(FOOTER_MAGIC);

    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| EngineError::from(e).at_file(dir))?;
    }
    let tmp = path.with_extension("tgs.tmp");
    {
        let mut f = File::create(&tmp).map_err(|e| EngineError::from(e).at_file(&tmp))?;
        f.write_all(&buf)
            .map_err(|e| EngineError::from(e).at_file(&tmp))?;
        f.sync_all()
            .map_err(|e| EngineError::from(e).at_file(&tmp))?;
    }
    fs::rename(&tmp, path).map_err(|e| EngineError::from(e).at_file(path))?;
    Ok(())
}

/// The `name` property, if the row has one as a JSON string.
///
/// Parsed once at seal time rather than on every lookup. Anything that is not
/// a string (absent, null, a number) simply has no name — resolution matches
/// on text, so there is nothing to index.
pub(crate) fn name_of(props: &str) -> Option<String> {
    if !props.contains("\"name\"") {
        return None; // the overwhelmingly common case: skip the parse
    }
    serde_json::from_str::<serde_json::Value>(props)
        .ok()?
        .get("name")?
        .as_str()
        .map(str::to_string)
}

fn align_up(n: usize) -> usize {
    n.div_ceil(ALIGN) * ALIGN
}

fn pad_to(buf: &mut Vec<u8>, target: usize) {
    buf.resize(target, 0);
}

// --------------------------------------------------------------------- //
// reading                                                               //
// --------------------------------------------------------------------- //

/// How a segment's bytes are obtained. Kept swappable because `mmap` is a
/// *policy*, not a format assumption (D-028 #10): it is usually the right
/// choice for immutable local files and can be the wrong one on network
/// storage, so both must stay available and measurable.
pub trait SegmentSource: Send + Sync {
    fn bytes(&self) -> &[u8];
}

pub struct MemorySource(Vec<u8>);

impl MemorySource {
    pub fn load(path: &Path) -> Result<Self> {
        Ok(Self(
            fs::read(path).map_err(|e| EngineError::from(e).at_file(path))?,
        ))
    }
}

impl SegmentSource for MemorySource {
    fn bytes(&self) -> &[u8] {
        &self.0
    }
}

pub struct MmapSource(memmap2::Mmap);

impl MmapSource {
    pub fn load(path: &Path) -> Result<Self> {
        let f = File::open(path).map_err(|e| EngineError::from(e).at_file(path))?;
        // SAFETY: segments are immutable once published — nothing in this
        // process or a well-behaved operator ever rewrites a sealed segment,
        // so the mapping cannot observe a concurrent modification.
        let map = unsafe { memmap2::Mmap::map(&f) }
            .map_err(|e| EngineError::from(e).at_file(path))?;
        Ok(Self(map))
    }
}

impl SegmentSource for MmapSource {
    fn bytes(&self) -> &[u8] {
        &self.0
    }
}

pub struct Segment<S: SegmentSource> {
    source: S,
    header: SegmentHeader,
    data_start: usize,
    path: PathBuf,
}

impl<S: SegmentSource> Segment<S> {
    /// Parse and validate. `verify_checksums` walks every extent; skip it on
    /// the hot path once a store has been verified, but never skip the
    /// structural checks — a truncated file must not reach a query.
    pub fn open(path: impl Into<PathBuf>, source: S, verify_checksums: bool) -> Result<Self> {
        let path = path.into();
        let bytes = source.bytes();
        let at = |e: EngineError| e.at_file(&path);

        if bytes.len() < PREAMBLE + 8 || &bytes[..4] != MAGIC {
            return Err(at(EngineError::corrupt("not a TGMS segment (bad magic)")));
        }
        if &bytes[bytes.len() - 4..] != FOOTER_MAGIC {
            return Err(at(EngineError::corrupt(
                "segment has no completion marker — it was never finished being written",
            )));
        }
        let format = read_u32(bytes, 4).map_err(at)?;
        if format != FORMAT_VERSION {
            return Err(at(EngineError::corrupt(format!(
                "segment format {format} is not supported (expected {FORMAT_VERSION})"
            ))));
        }
        let header_len = read_u32(bytes, 8).map_err(at)? as usize;
        let header_end = PREAMBLE + header_len;
        let header_bytes = bytes
            .get(PREAMBLE..header_end)
            .ok_or_else(|| at(EngineError::corrupt("segment header is truncated")))?;
        let header: SegmentHeader = serde_json::from_slice(header_bytes)
            .map_err(|e| at(EngineError::corrupt(format!("segment header is invalid: {e}"))))?;
        let data_start = align_up(header_end);

        let footer_len =
            read_u32(bytes, bytes.len() - 8).map_err(at)? as usize;
        let footer_end = bytes.len() - 8;
        let footer_start = footer_end
            .checked_sub(footer_len)
            .ok_or_else(|| at(EngineError::corrupt("segment footer length is impossible")))?;
        let footer: SegmentFooter = serde_json::from_slice(&bytes[footer_start..footer_end])
            .map_err(|e| at(EngineError::corrupt(format!("segment footer is invalid: {e}"))))?;

        if footer.rows != header.rows as u64 {
            return Err(at(EngineError::corrupt(format!(
                "row count disagrees: header {} vs footer {}",
                header.rows, footer.rows
            ))));
        }
        if footer.extent_crcs.len() != header.columns.len() {
            return Err(at(EngineError::corrupt(
                "footer checksum count does not match the column count",
            )));
        }

        let seg = Self {
            source,
            header,
            data_start,
            path,
        };
        if verify_checksums {
            seg.verify(&footer, footer_start)?;
        }
        Ok(seg)
    }

    fn verify(&self, footer: &SegmentFooter, footer_start: usize) -> Result<()> {
        let bytes = self.source.bytes();
        let at = |e: EngineError| e.at_file(&self.path);
        let body_sha = sha256_hex_bytes(&bytes[..footer_start])[..SHA_HEX_LEN].to_string();
        if body_sha != footer.body_sha {
            return Err(at(EngineError::corrupt(format!(
                "segment body checksum mismatch: computed {body_sha}, recorded {}",
                footer.body_sha
            ))));
        }
        for (desc, &expect) in self.header.columns.iter().zip(&footer.extent_crcs) {
            let got = crc32c::crc32c(self.raw(desc)?);
            if got != expect {
                return Err(at(EngineError::corrupt(format!(
                    "column '{}' failed its checksum (computed {got:#x}, recorded {expect:#x})",
                    desc.name
                ))));
            }
        }
        let got = crc32c::crc32c(self.raw_strings()?);
        if got != footer.strings_crc {
            return Err(at(EngineError::corrupt(
                "segment string table failed its checksum",
            )));
        }
        Ok(())
    }

    pub fn header(&self) -> &SegmentHeader {
        &self.header
    }

    pub fn rows(&self) -> u32 {
        self.header.rows
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn raw(&self, desc: &ColumnDesc) -> Result<&[u8]> {
        let start = self.data_start + desc.offset as usize;
        let end = start + desc.bytes as usize;
        self.source.bytes().get(start..end).ok_or_else(|| {
            EngineError::corrupt(format!("column '{}' extends past the file", desc.name))
                .at_file(&self.path)
        })
    }

    fn raw_strings(&self) -> Result<&[u8]> {
        let start = self.data_start + self.header.strings_offset as usize;
        let end = start + self.header.strings_bytes as usize;
        self.source.bytes().get(start..end).ok_or_else(|| {
            EngineError::corrupt("string table extends past the file").at_file(&self.path)
        })
    }

    pub fn strings(&self) -> Result<StringView<'_>> {
        StringView::parse(self.raw_strings()?)
    }

    fn typed<T: Copy>(&self, name: &str, dtype: DType) -> Result<&[T]> {
        let desc = self.header.column(name).ok_or_else(|| {
            EngineError::corrupt(format!("segment has no column '{name}'")).at_file(&self.path)
        })?;
        if desc.dtype != dtype {
            return Err(EngineError::corrupt(format!(
                "column '{name}' is {:?}, not {dtype:?}",
                desc.dtype
            ))
            .at_file(&self.path));
        }
        if desc.codec != 0 {
            return Err(EngineError::corrupt(format!(
                "column '{name}' uses codec {} which this build cannot decode",
                desc.codec
            ))
            .at_file(&self.path));
        }
        let bytes = self.raw(desc)?;
        let size = std::mem::size_of::<T>();
        if bytes.len() % size != 0 {
            return Err(EngineError::corrupt(format!(
                "column '{name}' length {} is not a multiple of {size}",
                bytes.len()
            ))
            .at_file(&self.path));
        }
        if !(bytes.as_ptr() as usize).is_multiple_of(std::mem::align_of::<T>()) {
            return Err(EngineError::corrupt(format!(
                "column '{name}' is misaligned for zero-copy access"
            ))
            .at_file(&self.path));
        }
        if cfg!(target_endian = "big") {
            return Err(EngineError::corrupt(
                "segments are little-endian; this build is big-endian",
            ));
        }
        // SAFETY: T is only ever a plain integer type (the callers below);
        // length is an exact multiple of its size and the pointer alignment
        // was just checked. Extents are 64-byte aligned by construction, so
        // this holds for every column the writer emits.
        Ok(unsafe { std::slice::from_raw_parts(bytes.as_ptr() as *const T, bytes.len() / size) })
    }

    pub fn i64_column(&self, name: &str) -> Result<&[i64]> {
        self.typed(name, DType::I64)
    }
    pub fn u64_column(&self, name: &str) -> Result<&[u64]> {
        self.typed(name, DType::U64)
    }
    pub fn u32_column(&self, name: &str) -> Result<&[u32]> {
        self.typed(name, DType::U32)
    }
    pub fn u16_column(&self, name: &str) -> Result<&[u16]> {
        self.typed(name, DType::U16)
    }

    /// `vt_e` for one row, materializing the elided instantaneous case.
    pub fn vt_e_at(&self, row: usize) -> Result<i64> {
        if self.header.vt_e_elided {
            Ok(self.i64_column("vt_s")?[row] + 1)
        } else {
            Ok(self.i64_column("vt_e")?[row])
        }
    }

    /// Folded per-segment visibility (empty = every row still believed).
    pub fn sidecar(&self) -> crate::visibility::Sidecar<'_> {
        crate::visibility::Sidecar::new(&self.header.closed_rows)
    }

    /// Full 96-bit version id for one row, reassembled from its two columns.
    pub fn vid_at(&self, row: usize) -> Result<Id96> {
        Ok(Id96 {
            hi: self.u64_column("vid64")?[row],
            lo: self.u32_column("vid_lo32")?[row],
        })
    }
}

fn read_u32(bytes: &[u8], at: usize) -> Result<u32> {
    bytes
        .get(at..at + 4)
        .map(|s| u32::from_le_bytes(s.try_into().expect("4 bytes")))
        .ok_or_else(|| EngineError::corrupt("unexpected end of segment").at_offset(at as u64))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::derive::{edge_eid, version_vid};

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("tgms-seg-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p.join("000001.tgs")
    }

    fn edge(vt_s: i64, i: u32, tt_s: i64) -> EdgeRow {
        let disc = format!("#{i}");
        let eid = edge_eid("n1", "n2", "SENT_MSG_TO", &disc);
        EdgeRow {
            vid: version_vid(&eid.to_hex(), tt_s, vt_s),
            src_id: i % 7,
            dst_id: (i + 3) % 7,
            rel_type: "SENT_MSG_TO".into(),
            disc,
            vt_s,
            vt_e: vt_s + 1,
            tt_s,
            props: if i.is_multiple_of(3) { "{}" } else { r#"{"rating":-2}"# }.into(),
            source: "ingest".into(),
            provenance_ref: if i.is_multiple_of(5) { None } else { Some("p/1".into()) },
        }
    }

    fn sorted_edges(n: u32) -> Vec<EdgeRow> {
        let mut rows: Vec<EdgeRow> = (0..n).map(|i| edge(1_000 + i as i64, i, 42)).collect();
        rows.sort_by_key(|r| r.sort_key());
        rows
    }

    fn open(path: &Path) -> Segment<MemorySource> {
        Segment::open(path, MemorySource::load(path).unwrap(), true).unwrap()
    }

    #[test]
    fn edge_segment_round_trips_every_field() {
        let path = tmp("roundtrip");
        let rows = sorted_edges(500);
        write_edge_segment(&path, &rows, &SegmentSpec::default()).unwrap();

        let seg = open(&path);
        assert_eq!(seg.rows(), 500);
        assert_eq!(seg.header().kind, RowKind::Edge);
        assert!(seg.header().vt_e_elided, "instantaneous events elide vt_e");

        let strings = seg.strings().unwrap();
        let vt_s = seg.i64_column("vt_s").unwrap();
        let src = seg.u32_column("src_id").unwrap();
        let dst = seg.u32_column("dst_id").unwrap();
        let rel = seg.u16_column("rel_code").unwrap();
        let props = seg.u32_column("props_ref").unwrap();
        let disc = seg.u32_column("disc_ref").unwrap();
        let prov = seg.u32_column("prov_ref").unwrap();

        for (i, r) in rows.iter().enumerate() {
            assert_eq!(vt_s[i], r.vt_s);
            assert_eq!(src[i], r.src_id);
            assert_eq!(dst[i], r.dst_id);
            assert_eq!(seg.header().rel_types[rel[i] as usize], r.rel_type);
            assert_eq!(seg.vid_at(i).unwrap(), r.vid);
            assert_eq!(seg.vt_e_at(i).unwrap(), r.vt_e);
            assert_eq!(seg.header().tt_s_at(i as u32).unwrap(), r.tt_s);
            assert_eq!(strings.get(props[i]).unwrap(), r.props);
            assert_eq!(strings.get(disc[i]).unwrap(), r.disc);
            assert_eq!(
                strings.get_opt(prov[i]).unwrap(),
                r.provenance_ref.as_deref()
            );
        }
    }

    #[test]
    fn null_provenance_is_distinct_from_empty_string() {
        let path = tmp("null-vs-empty");
        let mut rows = sorted_edges(2);
        rows[0].provenance_ref = None;
        rows[1].provenance_ref = Some(String::new());
        write_edge_segment(&path, &rows, &SegmentSpec::default()).unwrap();

        let seg = open(&path);
        let strings = seg.strings().unwrap();
        let prov = seg.u32_column("prov_ref").unwrap();
        assert_eq!(strings.get_opt(prov[0]).unwrap(), None);
        assert_eq!(strings.get_opt(prov[1]).unwrap(), Some(""));
    }

    #[test]
    fn long_intervals_keep_an_explicit_vt_e() {
        let path = tmp("interval");
        let mut rows = sorted_edges(4);
        rows[2].vt_e = rows[2].vt_s + 5_000_000;
        write_edge_segment(
            &path,
            &rows,
            &SegmentSpec {
                lane: Lane::Interval,
                ..Default::default()
            },
        )
        .unwrap();

        let seg = open(&path);
        assert!(!seg.header().vt_e_elided);
        assert_eq!(seg.header().lane, Lane::Interval);
        assert_eq!(seg.vt_e_at(2).unwrap(), rows[2].vt_s + 5_000_000);
        assert_eq!(seg.header().vt_e_max, rows[2].vt_s + 5_000_000);
    }

    #[test]
    fn zone_map_bounds_the_data() {
        let path = tmp("zonemap");
        let rows = sorted_edges(100);
        write_edge_segment(&path, &rows, &SegmentSpec::default()).unwrap();
        let seg = open(&path);
        let h = seg.header();
        assert_eq!(h.vt_min, rows.iter().map(|r| r.vt_s).min().unwrap());
        assert_eq!(h.vt_max, rows.iter().map(|r| r.vt_s).max().unwrap());
        assert_eq!(h.key_lo, (rows[0].vt_s, rows[0].vid.to_hex()));
        assert_eq!(h.key_hi, (rows[99].vt_s, rows[99].vid.to_hex()));
    }

    #[test]
    fn mmap_and_memory_sources_agree() {
        let path = tmp("sources");
        let rows = sorted_edges(64);
        write_edge_segment(&path, &rows, &SegmentSpec::default()).unwrap();

        let mem = Segment::open(&path, MemorySource::load(&path).unwrap(), true).unwrap();
        let mapped = Segment::open(&path, MmapSource::load(&path).unwrap(), true).unwrap();
        assert_eq!(mem.header(), mapped.header());
        assert_eq!(
            mem.i64_column("vt_s").unwrap(),
            mapped.i64_column("vt_s").unwrap()
        );
        assert_eq!(
            mem.strings().unwrap().get(3).unwrap(),
            mapped.strings().unwrap().get(3).unwrap()
        );
    }

    #[test]
    fn columns_are_64_byte_aligned_for_zero_copy() {
        let path = tmp("align");
        write_edge_segment(&path, &sorted_edges(37), &SegmentSpec::default()).unwrap();
        let seg = open(&path);
        for c in &seg.header().columns {
            assert_eq!(
                (seg.data_start + c.offset as usize) % ALIGN,
                0,
                "column '{}' is not aligned",
                c.name
            );
        }
    }

    #[test]
    fn tt_s_runs_collapse_a_uniform_batch() {
        let path = tmp("ttruns");
        write_edge_segment(&path, &sorted_edges(1000), &SegmentSpec::default()).unwrap();
        let seg = open(&path);
        assert_eq!(
            seg.header().tt_s_runs.len(),
            1,
            "one batch writes one transaction time"
        );
    }

    #[test]
    fn string_table_dedups_repeated_payloads() {
        let path = tmp("dedup");
        write_edge_segment(&path, &sorted_edges(1000), &SegmentSpec::default()).unwrap();
        let seg = open(&path);
        // "" + 2 props + 1000 discs + rel/source/prov strings — the point is
        // that the two distinct props payloads are stored once each
        assert!(
            seg.header().strings_count < 1010,
            "expected dedup, got {} strings",
            seg.header().strings_count
        );
    }

    // --- corruption detection ------------------------------------------ //

    fn corrupt_at(path: &Path, offset: usize, byte: u8) {
        let mut b = fs::read(path).unwrap();
        b[offset] ^= byte;
        fs::write(path, b).unwrap();
    }

    #[test]
    fn truncated_file_has_no_completion_marker() {
        let path = tmp("truncated");
        write_edge_segment(&path, &sorted_edges(50), &SegmentSpec::default()).unwrap();
        let mut b = fs::read(&path).unwrap();
        b.truncate(b.len() - 16);
        fs::write(&path, &b).unwrap();

        let err = match Segment::open(&path, MemorySource(b), true) {
            Ok(_) => panic!("a truncated segment must not open"),
            Err(e) => e,
        };
        assert_eq!(err.category, crate::error::Category::Corrupt);
        assert!(err.to_string().contains("never finished"));
    }

    #[test]
    fn flipped_data_byte_fails_its_column_checksum() {
        let path = tmp("bitflip");
        let rows = sorted_edges(50);
        write_edge_segment(&path, &rows, &SegmentSpec::default()).unwrap();
        let seg = open(&path);
        let vt_s_offset = seg.data_start + seg.header().column("vt_s").unwrap().offset as usize;
        drop(seg);
        corrupt_at(&path, vt_s_offset + 8, 0xff);

        let err = match Segment::open(&path, MemorySource::load(&path).unwrap(), true) {
            Ok(_) => panic!("a flipped byte must be detected"),
            Err(e) => e,
        };
        assert!(err.to_string().contains("checksum"), "{err}");
    }

    #[test]
    fn bad_magic_is_rejected() {
        let path = tmp("magic");
        write_edge_segment(&path, &sorted_edges(4), &SegmentSpec::default()).unwrap();
        corrupt_at(&path, 1, 0xff);
        assert!(Segment::open(&path, MemorySource::load(&path).unwrap(), true).is_err());
    }

    #[test]
    fn empty_segments_are_refused() {
        let path = tmp("empty");
        assert!(write_edge_segment(&path, &[], &SegmentSpec::default()).is_err());
    }

    #[test]
    fn name_is_promoted_to_a_column_without_leaving_props() {
        let path = tmp("name-column");
        let props = [
            r#"{"name":"Alice"}"#,                 // plain
            r#"{"age":3,"name":"Bob"}"#,           // not the first key
            r#"{}"#,                               // absent
            r#"{"name":null}"#,                    // present but not a string
            r#"{"name":"\u00e9l\u00e8ve","x":1}"#, // escaped non-ascii
            r#"{"nickname":"trap"}"#,              // substring of the key only
        ];
        let mut rows: Vec<NodeRow> = props
            .iter()
            .enumerate()
            .map(|(i, p)| NodeRow {
                vid: version_vid(&format!("n{i}"), 7, i as i64),
                uid_id: i as u32,
                label: "Node".into(),
                vt_s: i as i64,
                vt_e: crate::OPEN_END,
                tt_s: 7,
                props: (*p).to_string(),
                source: "ingest".into(),
                provenance_ref: None,
            })
            .collect();
        rows.sort_by_key(|r| r.sort_key());
        write_node_segment(&path, &rows, &SegmentSpec::default()).unwrap();

        let seg = open(&path);
        let strings = seg.strings().unwrap();
        let name = seg.u32_column("name_ref").unwrap();
        let props_col = seg.u32_column("props_ref").unwrap();
        for (i, r) in rows.iter().enumerate() {
            // props survives byte-identical: the store digest depends on it
            assert_eq!(strings.get(props_col[i]).unwrap(), r.props);
            let got = strings.get_opt(name[i]).unwrap();
            let want = match r.props.as_str() {
                p if p.contains(r#""name":"Alice""#) => Some("Alice"),
                p if p.contains(r#""name":"Bob""#) => Some("Bob"),
                p if p.contains(r#"\u00e9l"#) => Some("élève"),
                _ => None,
            };
            assert_eq!(got, want, "props {}", r.props);
        }
    }

    #[test]
    fn node_segment_round_trips() {
        let path = tmp("nodes");
        let mut rows: Vec<NodeRow> = (0..50u32)
            .map(|i| NodeRow {
                vid: version_vid(&format!("n{i}"), 7, i as i64),
                uid_id: i,
                label: "Node".into(),
                vt_s: i as i64,
                vt_e: crate::OPEN_END,
                tt_s: 7,
                props: "{}".into(),
                source: "ingest".into(),
                provenance_ref: None,
            })
            .collect();
        rows.sort_by_key(|r| r.sort_key());
        write_node_segment(&path, &rows, &SegmentSpec::default()).unwrap();

        let seg = open(&path);
        assert_eq!(seg.header().kind, RowKind::Node);
        assert!(!seg.header().vt_e_elided, "OPEN_END is not vt_s + 1");
        let uid = seg.u32_column("uid_id").unwrap();
        let strings = seg.strings().unwrap();
        let label = seg.u32_column("label_ref").unwrap();
        for (i, r) in rows.iter().enumerate() {
            assert_eq!(uid[i], r.uid_id);
            assert_eq!(strings.get(label[i]).unwrap(), r.label);
            assert_eq!(seg.vt_e_at(i).unwrap(), crate::OPEN_END);
        }
    }
}
