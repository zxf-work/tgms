//! Canonical JSON + sha256, reimplemented independently of
//! `tgms.core.model`/`tgms.temporal.algebra` (the Python side of the same
//! contract lives at `external/neo4j-recompute/neo4j_recompute/canon.py`;
//! this module must match both byte-for-byte so the C1 checker can compare
//! `result.json` digests directly against TGMS's own `result_digest`).
//!
//! Contract (mirrors `tgms.core.model.canonical_json`/`digest` and
//! `tgms.temporal.algebra._canonicalize_floats` exactly):
//!   - object keys sorted (byte/code-point order -- ASCII field names only,
//!     so Rust's `str` ordering and Python's agree),
//!   - compact separators (`,` and `:`, no spaces),
//!   - UTF-8 preserved (`ensure_ascii=False` -- never escaped to `\uXXXX`),
//!   - every float rounded to 9 decimals with round-half-even *before*
//!     serialization (`round(x, 9)` in Python),
//!   - sha256 hex of the resulting UTF-8 text.
//!
//! `tests/digest_tests.rs` pins this against literal values computed by the
//! real `tgms.core.model.digest`/`canonical_json` on the laptop, so a drift
//! in either implementation is caught by `cargo test`, not discovered at a
//! shape-test mismatch three weeks later.

use serde_json::{Map, Number, Value};
use sha2::{Digest as _, Sha256};

/// Round every float in `v` to 9 decimals (round-half-even, matching
/// Python's `round()`), recursively. Mirrors
/// `tgms.temporal.algebra._canonicalize_floats`. Integers, strings, bools,
/// null and object/array structure pass through unchanged.
pub fn canonicalize_floats(v: &Value) -> Value {
    match v {
        Value::Number(n) => {
            if let Some(f) = n.as_f64() {
                if n.is_f64() {
                    return Value::Number(round9(f));
                }
            }
            Value::Number(n.clone())
        }
        Value::Object(m) => {
            let mut out = Map::new();
            for (k, val) in m {
                out.insert(k.clone(), canonicalize_floats(val));
            }
            Value::Object(out)
        }
        Value::Array(a) => Value::Array(a.iter().map(canonicalize_floats).collect()),
        other => other.clone(),
    }
}

/// Public f64-to-f64 wrapper of `round9`, for family code that needs to
/// pre-round a score the way `ops_series.burst_detection` does (`score =
/// round(score, 9)`) before a threshold comparison -- not just before
/// serialization.
pub fn round9_f64(x: f64) -> f64 {
    round9(x).as_f64().unwrap_or(x)
}

/// Round to 9 decimals by formatting to a 9-decimal-place decimal string
/// and parsing it back.
///
/// `(x * 1e9).round() / 1e9` was tried first and rejected: multiplying by
/// `1e9` is itself a lossy floating-point operation, and it can push a
/// value that is genuinely just *below* a `*.5e-9` decimal boundary (e.g.
/// the literal `0.1234567895`, stored as approximately
/// `0.12345678949999999...`) to *land on or above* it after the multiply,
/// rounding the wrong way relative to Python's `round(x, 9)` (which
/// inspects the true stored value directly, via a correctly-rounded
/// decimal conversion, and rounds it down). Rust's own `{:.9}` formatter
/// performs that same correctly-rounded decimal conversion (no
/// multiplication involved), so formatting and reparsing reproduces
/// Python's result, including round-half-to-even at a genuine exact tie
/// (`tests/digest_tests.rs` pins both the near-tie and the
/// `0.1234567895`-shaped case that first caught the multiply-based
/// version being wrong).
fn round9(x: f64) -> Number {
    // Mirrors `_canonicalize_floats`'s own refusal: a non-finite float
    // would either be silently dropped by `Number::from_f64` (which
    // returns `None` for NaN/±inf) or render as invalid JSON -- neither
    // is the Python contract's "raise", but panicking here is closer to
    // it than inventing a value.
    assert!(x.is_finite(), "non-finite float in operator output: {x}");
    let s = format!("{:.9}", x);
    let rounded: f64 = s.parse().unwrap_or(x);
    Number::from_f64(rounded).unwrap_or_else(|| Number::from_f64(0.0).unwrap())
}

/// `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
/// `serde_json::Map` is a `BTreeMap` under this crate's default features (no
/// `preserve_order`), so keys are already sorted; this function only has to
/// get separators and escaping right.
pub fn canonical_json(v: &Value) -> String {
    let mut out = String::new();
    write_canonical(v, &mut out);
    out
}

fn write_canonical(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_json_string(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (i, (k, val)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json_string(k, out);
                out.push(':');
                write_canonical(val, out);
            }
            out.push('}');
        }
    }
}

/// JSON string escaping with `ensure_ascii=False` semantics: only the
/// characters JSON itself requires escaped (`"`, `\`, and control chars
/// U+0000..U+001F) are escaped; every other Unicode scalar, including
/// non-ASCII, is written through verbatim as UTF-8 -- exactly what Python's
/// `json.dumps(..., ensure_ascii=False)` does.
fn write_json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn sha256_hex(text: &str) -> String {
    sha256_hex_bytes(text.as_bytes())
}

/// sha256 over raw bytes (a file's literal contents), not a canonicalized
/// value's text -- used for `Cargo.lock`/binary content-addressing in the
/// record, not for any `result_digest`.
pub fn sha256_hex_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let result = hasher.finalize();
    result.iter().map(|b| format!("{:02x}", b)).collect()
}

/// `tgms.core.model.digest`: sha256 of the canonical-JSON payload. Callers
/// must canonicalize floats first (`canonicalize_floats`) -- this function
/// does not do it implicitly, matching the Python-side `canon.py`'s own
/// documented contract, so a caller cannot forget and then wonder why a
/// float field's digest never matches.
pub fn digest(v: &Value) -> String {
    sha256_hex(&canonical_json(v))
}

/// Convenience: canonicalize floats, then digest -- what every family
/// module should call on its finished payload.
pub fn result_digest(v: &Value) -> String {
    digest(&canonicalize_floats(v))
}
