//! Column compression: per-block frame-of-reference bit-packing (codec 1).
//!
//! One codec, chosen by measurement (docs/eval_phase0.md storage breakdown at
//! 1M rows): the store's bytes are dominated by integer columns whose values
//! sit in narrow ranges — `vt_s`/`vt_e` are sorted or nearly so, the four
//! string-ref columns are constant or sequential within a segment, and
//! `src_id`/`dst_id`/`rel_code` are bounded by the dictionary. Frame-of-
//! reference handles all of those with one mechanism: per block, store the
//! minimum and the bit width of (max - min), then pack each delta at that
//! width. A constant block costs one bit per row of overhead-free truth:
//! width 0, no payload at all. The `vid` halves are sha256 prefixes and do
//! not compress; the writer measures and keeps them raw.
//!
//! Decoding is a full-column materialization, not random access. That is a
//! deliberate pairing with the store's segment cache: a segment file is
//! immutable, so it is decoded once per process and the hot path keeps
//! serving plain slices. Nothing in the scan loop changed shape.
//!
//! Layout (little-endian):
//!   u32 n_values | u32 block_len | blocks...
//! block: i64 min | u8 width (0..=64) | ceil(len * width / 8) packed bytes
//!
//! Every read is bounds-checked: a corrupt stream must produce an error,
//! never a panic or an over-read — decode runs even when checksum
//! verification is skipped, so it is itself a structural validator.

use crate::error::{EngineError, Result};

pub const CODEC_RAW: u32 = 0;
pub const CODEC_FOR: u32 = 1;

const BLOCK: usize = 4096;

/// Pack `vals` as frame-of-reference deltas. Total order of bytes is fully
/// determined by the input, so identical columns encode identically —
/// segment bytes stay deterministic (spec §2.3).
pub fn encode_i64(vals: &[i64]) -> Vec<u8> {
    let mut out = Vec::with_capacity(16 + vals.len());
    out.extend_from_slice(&(vals.len() as u32).to_le_bytes());
    out.extend_from_slice(&(BLOCK as u32).to_le_bytes());
    for block in vals.chunks(BLOCK) {
        let min = block.iter().copied().min().expect("chunks are non-empty");
        // wrapping: the delta of any i64 pair fits u64
        let max_delta = block
            .iter()
            .map(|&v| v.wrapping_sub(min) as u64)
            .max()
            .expect("chunks are non-empty");
        let width = 64 - max_delta.leading_zeros() as usize; // 0..=64
        out.extend_from_slice(&min.to_le_bytes());
        out.push(width as u8);
        if width == 0 {
            continue; // constant block: the min is the whole story
        }
        let mut acc: u128 = 0;
        let mut bits = 0usize;
        for &v in block {
            acc |= ((v.wrapping_sub(min) as u64) as u128) << bits;
            bits += width;
            while bits >= 8 {
                out.push((acc & 0xFF) as u8);
                acc >>= 8;
                bits -= 8;
            }
        }
        if bits > 0 {
            out.push((acc & 0xFF) as u8);
        }
    }
    out
}

pub fn decode_i64(bytes: &[u8], expect: usize) -> Result<Vec<i64>> {
    let bad = |what: &str| EngineError::corrupt(format!("FOR stream: {what}"));
    let take = |at: usize, n: usize| -> Result<&[u8]> {
        bytes.get(at..at + n).ok_or_else(|| bad("truncated"))
    };
    let n = u32::from_le_bytes(take(0, 4)?.try_into().unwrap()) as usize;
    let block_len = u32::from_le_bytes(take(4, 4)?.try_into().unwrap()) as usize;
    if n != expect {
        return Err(bad(&format!("has {n} values, column expects {expect}")));
    }
    if block_len == 0 || block_len > 1 << 20 {
        return Err(bad(&format!("implausible block length {block_len}")));
    }
    let mut out = Vec::with_capacity(n);
    let mut at = 8usize;
    while out.len() < n {
        let len = (n - out.len()).min(block_len);
        let min = i64::from_le_bytes(take(at, 8)?.try_into().unwrap());
        let width = *take(at + 8, 1)?.first().unwrap() as usize;
        at += 9;
        if width > 64 {
            return Err(bad(&format!("impossible bit width {width}")));
        }
        if width == 0 {
            out.resize(out.len() + len, min);
            continue;
        }
        let payload = len * width;
        let payload_bytes = payload.div_ceil(8);
        let packed = take(at, payload_bytes)?;
        at += payload_bytes;
        let mut acc: u128 = 0;
        let mut bits = 0usize;
        let mut byte_at = 0usize;
        let mask: u128 = if width == 64 { u128::MAX >> 64 } else { (1u128 << width) - 1 };
        for _ in 0..len {
            while bits < width {
                acc |= (packed[byte_at] as u128) << bits;
                byte_at += 1;
                bits += 8;
            }
            out.push(min.wrapping_add((acc & mask) as u64 as i64));
            acc >>= width;
            bits -= width;
        }
    }
    if at > bytes.len() {
        return Err(bad("truncated"));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(vals: &[i64]) {
        let enc = encode_i64(vals);
        let dec = decode_i64(&enc, vals.len()).unwrap();
        assert_eq!(dec, vals);
    }

    #[test]
    fn roundtrips_the_shapes_the_store_contains() {
        roundtrip(&[]);
        roundtrip(&[42]);
        roundtrip(&(0..10_000i64).collect::<Vec<_>>()); // sequential refs
        roundtrip(&vec![7; 10_000]); // constant column
        roundtrip(&(0..10_000i64).map(|i| 1_600_000_000_000_000 + i * 977).collect::<Vec<_>>());
        // adversarial: full-range values must survive (and stay raw in
        // practice, but correctness cannot depend on the writer's choice)
        roundtrip(&[i64::MIN, i64::MAX, 0, -1, 1]);
        let mut x: u64 = 0x9E3779B97F4A7C15;
        let noise: Vec<i64> = (0..5000)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                x as i64
            })
            .collect();
        roundtrip(&noise);
    }

    #[test]
    fn constant_blocks_cost_nothing_but_headers() {
        let enc = encode_i64(&vec![5; 100_000]);
        // 25 blocks x 9 bytes + 8 stream header
        assert!(enc.len() < 300, "constant column encoded to {} bytes", enc.len());
    }

    #[test]
    fn sorted_timestamps_compress_hard() {
        let vals: Vec<i64> = (0..100_000).map(|i| 1_600_000_000_000_000 + i).collect();
        let enc = encode_i64(&vals);
        assert!(enc.len() < vals.len() * 3, "sorted i64 encoded to {} bytes", enc.len());
    }

    #[test]
    fn corrupt_streams_error_rather_than_panic() {
        let enc = encode_i64(&(0..10_000i64).collect::<Vec<_>>());
        assert!(decode_i64(&enc, 9_999).is_err(), "wrong expected count");
        assert!(decode_i64(&enc[..enc.len() - 3], 10_000).is_err(), "truncation");
        assert!(decode_i64(&enc[..6], 10_000).is_err(), "no room for a block");
        let mut evil = enc.clone();
        evil[16] = 99; // width byte (stream header 8 + block min 8) > 64
        assert!(decode_i64(&evil, 10_000).is_err(), "impossible width");
    }
}

// ------------------------------------------------------------------- //
// string heap (codec: STRINGS_PACKED)                                 //
// ------------------------------------------------------------------- //

/// Sentinel in the heap's first four bytes. A raw heap starts with its
/// string count; `u32::MAX` there is impossible (refs are u32 with MAX
/// reserved as NULL), so an old build reading a packed heap fails its
/// bounds checks instead of misreading text.
pub const HEAP_MAGIC: u32 = u32::MAX;

/// Pack a raw string heap (`count | offsets (n+1) x u32 | payload`).
///
/// The two halves compress differently and are packed separately: offsets
/// are monotone (their deltas are string lengths) so the FOR codec takes
/// them to well under a byte each, while the payload is text and goes
/// through DEFLATE. Measured on a real heap the split beats one DEFLATE
/// stream over everything by 1.4x.
///
/// Layout: u32 HEAP_MAGIC | u32 n | u32 for_len | u32 deflate_len
///         | FOR(offsets) | DEFLATE(payload)
pub fn pack_heap(raw: &[u8]) -> Option<Vec<u8>> {
    if raw.len() < 8 {
        return None;
    }
    let n = u32::from_le_bytes(raw[..4].try_into().unwrap()) as usize;
    let off_end = 4 + 4 * (n + 1);
    // a count equal to the magic could not be told apart when reading
    if n == u32::MAX as usize || raw.len() < off_end {
        return None;
    }
    let offsets: Vec<i64> = raw[4..off_end]
        .as_chunks::<4>()
        .0
        .iter()
        .map(|c| u32::from_le_bytes(*c) as i64)
        .collect();
    let packed_offsets = encode_i64(&offsets);
    let deflated = miniz_oxide::deflate::compress_to_vec(&raw[off_end..], 6);
    let mut out = Vec::with_capacity(16 + packed_offsets.len() + deflated.len());
    out.extend_from_slice(&HEAP_MAGIC.to_le_bytes());
    out.extend_from_slice(&(n as u32).to_le_bytes());
    out.extend_from_slice(&(packed_offsets.len() as u32).to_le_bytes());
    out.extend_from_slice(&(deflated.len() as u32).to_le_bytes());
    out.extend_from_slice(&packed_offsets);
    out.extend_from_slice(&deflated);
    Some(out)
}

/// Is this heap packed? Raw heaps keep their count in the first word.
pub fn heap_is_packed(bytes: &[u8]) -> bool {
    bytes.len() >= 4 && u32::from_le_bytes(bytes[..4].try_into().unwrap()) == HEAP_MAGIC
}

/// Reconstruct the raw heap layout from a packed one.
pub fn unpack_heap(bytes: &[u8]) -> Result<Vec<u8>> {
    let bad = |what: &str| EngineError::corrupt(format!("packed string heap: {what}"));
    if bytes.len() < 16 || !heap_is_packed(bytes) {
        return Err(bad("missing magic"));
    }
    let n = u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
    let for_len = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    let deflate_len = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
    let for_end = 16usize.checked_add(for_len).ok_or_else(|| bad("truncated"))?;
    let total = for_end.checked_add(deflate_len).ok_or_else(|| bad("truncated"))?;
    if bytes.len() < total {
        return Err(bad("truncated"));
    }
    let offsets = decode_i64(&bytes[16..for_end], n + 1)?;
    let payload_len = offsets.last().copied().unwrap_or(0);
    if !(0..=(1i64 << 32)).contains(&payload_len) {
        return Err(bad("implausible payload length"));
    }
    let payload = miniz_oxide::inflate::decompress_to_vec_with_limit(
        &bytes[for_end..total],
        payload_len as usize,
    )
    .map_err(|e| bad(&format!("inflate failed: {e}")))?;
    if payload.len() != payload_len as usize {
        return Err(bad("payload length disagrees with offsets"));
    }
    let mut raw = Vec::with_capacity(4 + 4 * (n + 1) + payload.len());
    raw.extend_from_slice(&(n as u32).to_le_bytes());
    for &o in &offsets {
        if o < 0 || o > payload_len {
            return Err(bad("offset out of range"));
        }
        raw.extend_from_slice(&(o as u32).to_le_bytes());
    }
    raw.extend_from_slice(&payload);
    Ok(raw)
}

#[cfg(test)]
mod heap_tests {
    use super::*;

    fn raw_heap(strings: &[&str]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(&(strings.len() as u32).to_le_bytes());
        let mut cursor = 0u32;
        for s in strings {
            out.extend_from_slice(&cursor.to_le_bytes());
            cursor += s.len() as u32;
        }
        out.extend_from_slice(&cursor.to_le_bytes());
        for s in strings {
            out.extend_from_slice(s.as_bytes());
        }
        out
    }

    #[test]
    fn packed_heaps_roundtrip() {
        for strings in [
            vec![],
            vec![""],
            vec!["{}", "ingest", "élève"],
            (0..5000).map(|i| format!("#{i}")).collect::<Vec<_>>()
                .iter().map(String::as_str).collect(),
        ] {
            let raw = raw_heap(&strings);
            let packed = pack_heap(&raw).unwrap();
            assert!(heap_is_packed(&packed));
            assert_eq!(unpack_heap(&packed).unwrap(), raw, "{strings:?}");
        }
    }

    #[test]
    fn disc_shaped_heaps_shrink_hard() {
        let strings: Vec<String> = (0..50_000).map(|i| format!("#{i}")).collect();
        let refs: Vec<&str> = strings.iter().map(String::as_str).collect();
        let raw = raw_heap(&refs);
        let packed = pack_heap(&raw).unwrap();
        // 2.4x measured on this worst case (every string distinct); real
        // heaps with repeated props/source strings do better
        assert!(
            packed.len() * 2 < raw.len(),
            "packed {} vs raw {}", packed.len(), raw.len()
        );
    }

    #[test]
    fn corrupt_packed_heaps_error_rather_than_panic() {
        let strings: Vec<String> = (0..1000).map(|i| format!("#{i}")).collect();
        let refs: Vec<&str> = strings.iter().map(String::as_str).collect();
        let packed = pack_heap(&raw_heap(&refs)).unwrap();
        assert!(unpack_heap(&packed[..packed.len() - 2]).is_err(), "truncation");
        assert!(unpack_heap(&packed[..12]).is_err(), "header truncation");
        let mut evil = packed.clone();
        evil[4] = 0xFF; evil[5] = 0xFF; // absurd count
        assert!(unpack_heap(&evil).is_err(), "count mismatch vs offsets");
    }
}
