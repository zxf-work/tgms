//! Identity derivation — the single canonical implementation (spec §2.2, D-028 #3).
//!
//! `eid` and `vid` are pure functions of other stored fields, so the engine
//! derives them instead of storing 24-byte hex strings per row. That is only
//! sound if this module reproduces `tgms.core.model` **byte for byte**: a
//! single differing escape would change an eid, which would change a vid,
//! which would change the store digest and break replay against the frozen
//! D-018/D-023 SHAs.
//!
//! The parity fixture in `tests/fixtures/derive_vectors.json` is generated
//! from the Python functions themselves (`make derive-vectors`) and asserted
//! in `tests/derive.rs`. Python is the source of truth; this is the copy.

use sha2::{Digest, Sha256};

use crate::error::{EngineError, Result};

/// Number of hex characters in a TGMS identity (96 bits).
pub const ID_HEX_LEN: usize = 24;

/// A derived identity: 96 bits, held as a 64-bit prefix plus a 32-bit tail.
///
/// The derived `Ord` compares `hi` then `lo`, which is exactly the ordering
/// of the 24-character lowercase hex string. That equivalence is what makes
/// it legal to sort and prune on the `hi` prefix alone and only consult `lo`
/// on a tie (spec §2.2).
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug, Default)]
pub struct Id96 {
    pub hi: u64,
    pub lo: u32,
}

impl Id96 {
    pub fn from_hex(hex: &str) -> Result<Self> {
        if hex.len() != ID_HEX_LEN {
            return Err(EngineError::invariant(format!(
                "identity must be {ID_HEX_LEN} hex chars, got {}: {hex:?}",
                hex.len()
            )));
        }
        let parse = |s: &str, what: &str| -> Result<u64> {
            u64::from_str_radix(s, 16).map_err(|e| {
                EngineError::invariant(format!("identity {what} not hex ({e}): {hex:?}"))
            })
        };
        Ok(Self {
            hi: parse(&hex[..16], "prefix")?,
            lo: parse(&hex[16..], "suffix")? as u32,
        })
    }

    pub fn to_hex(self) -> String {
        format!("{:016x}{:08x}", self.hi, self.lo)
    }
}

/// Python's `json.dumps(..., ensure_ascii=False)` string escaping.
///
/// Matches `json.encoder.ESCAPE_DCT`: the seven short escapes, `\u00xx`
/// (lowercase) for the remaining C0 controls, and **everything else verbatim**
/// — notably `/`, DEL, U+2028, and U+2029 are not escaped.
fn escape_into(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                // \u00xx, lowercase hex — Python uses '\\u{0:04x}'
                out.push_str("\\u");
                for shift in [12, 8, 4, 0] {
                    let nib = ((c as u32) >> shift) & 0xf;
                    out.push(char::from_digit(nib, 16).expect("nibble is a hex digit"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Canonical JSON for an array of strings — the only shape identity
/// derivation needs (`[src, dst, rel_type, disc]`). Compact separators, no
/// spaces; key sorting is irrelevant for arrays.
pub fn canonical_json_string_array(items: &[&str]) -> String {
    // 2 brackets + per item: 2 quotes + 1 comma + content
    let mut out = String::with_capacity(2 + items.iter().map(|s| s.len() + 3).sum::<usize>());
    out.push('[');
    for (i, s) in items.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        escape_into(s, &mut out);
    }
    out.push(']');
    out
}

/// Lowercase hex SHA-256 of raw bytes.
pub fn sha256_hex_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let out = hasher.finalize();
    let mut s = String::with_capacity(64);
    for b in out {
        s.push(char::from_digit((b >> 4) as u32, 16).expect("nibble"));
        s.push(char::from_digit((b & 0xf) as u32, 16).expect("nibble"));
    }
    s
}

/// Lowercase hex SHA-256 of the UTF-8 bytes — `tgms.core.model.sha256_hex`.
pub fn sha256_hex(text: &str) -> String {
    sha256_hex_bytes(text.as_bytes())
}

fn truncated_id(text: &str) -> Id96 {
    let hex = sha256_hex(text);
    Id96::from_hex(&hex[..ID_HEX_LEN]).expect("sha256 hex prefix is always valid")
}

/// Logical edge identity: `hash(src, dst, rel_type, disc)` (spec WP1.1).
pub fn edge_eid(src: &str, dst: &str, rel_type: &str, disc: &str) -> Id96 {
    truncated_id(&canonical_json_string_array(&[src, dst, rel_type, disc]))
}

/// Version id: `hash(identity, tt_s, vt_s)` — the D-001 refinement of the
/// spec's `hash(eid, tt_s)`, which collides when one batch splits a version
/// into two fragments at the same transaction time.
///
/// `identity` is the node uid, or the edge eid as its 24-char hex string.
pub fn version_vid(identity: &str, tt_s: i64, vt_s: i64) -> Id96 {
    truncated_id(&format!("{identity}:{tt_s}:{vt_s}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id96_ordering_equals_hex_string_ordering() {
        // the property the whole prefix-sorting scheme rests on
        let samples: Vec<String> = (0u32..400)
            .map(|i| sha256_hex(&format!("sample-{i}"))[..ID_HEX_LEN].to_string())
            .collect();
        let mut by_hex = samples.clone();
        by_hex.sort();
        let mut by_id: Vec<Id96> = samples.iter().map(|h| Id96::from_hex(h).unwrap()).collect();
        by_id.sort();
        let by_id_hex: Vec<String> = by_id.iter().map(|i| i.to_hex()).collect();
        assert_eq!(by_hex, by_id_hex);
    }

    #[test]
    fn id96_hex_round_trips() {
        let id = edge_eid("n1", "n2", "SENT_MSG_TO", "#0");
        assert_eq!(Id96::from_hex(&id.to_hex()).unwrap(), id);
        assert_eq!(id.to_hex().len(), ID_HEX_LEN);
    }

    #[test]
    fn id96_rejects_malformed_hex() {
        assert!(Id96::from_hex("tooshort").is_err());
        assert!(Id96::from_hex("zzzzzzzzzzzzzzzzzzzzzzzz").is_err());
    }

    #[test]
    fn escapes_match_python_encoder_rules() {
        assert_eq!(canonical_json_string_array(&[""]), r#"[""]"#);
        assert_eq!(canonical_json_string_array(&["a", "b"]), r#"["a","b"]"#);
        assert_eq!(canonical_json_string_array(&["q\"t"]), r#"["q\"t"]"#);
        assert_eq!(canonical_json_string_array(&["b\\s"]), r#"["b\\s"]"#);
        assert_eq!(canonical_json_string_array(&["\n\t\r"]), r#"["\n\t\r"]"#);
        assert_eq!(canonical_json_string_array(&["\u{08}\u{0c}"]), r#"["\b\f"]"#);
        assert_eq!(canonical_json_string_array(&["\u{0}"]), r#"["\u0000"]"#);
        assert_eq!(canonical_json_string_array(&["\u{1f}"]), r#"["\u001f"]"#);
        // deliberately NOT escaped
        assert_eq!(canonical_json_string_array(&["a/b"]), r#"["a/b"]"#);
        assert_eq!(canonical_json_string_array(&["\u{7f}"]), "[\"\u{7f}\"]");
        assert_eq!(canonical_json_string_array(&["中文"]), "[\"中文\"]");
    }

    #[test]
    fn direction_and_discriminator_change_identity() {
        let a = edge_eid("n1", "n2", "R", "");
        assert_ne!(a, edge_eid("n2", "n1", "R", ""));
        assert_ne!(a, edge_eid("n1", "n2", "R", "#0"));
        assert_ne!(a, edge_eid("n1", "n2", "S", ""));
    }

    #[test]
    fn vid_separates_same_batch_fragments() {
        // the D-001 case: one batch carving a version in two at the same tt
        let left = version_vid("n1", 100, 0);
        let right = version_vid("n1", 100, 50);
        assert_ne!(left, right);
    }
}
