//! Pins `ivm_dd::digest` against literal values computed by the real
//! `tgms.core.model.canonical_json`/`digest` (Python, run on the laptop via
//! `.venv/bin/python3`; values captured 2026-10-02, see the comment above
//! each case). A drift in either implementation should fail here, not at a
//! shape-test mismatch discovered three weeks later against a real cell.
//!
//! **Known limitation, not tested here (documented, not silently
//! papered over):** Python's `json.dumps` switches a very-small-magnitude
//! float to scientific notation (e.g. `round(0.00000000051, 9)` serializes
//! as `"1e-09"`); this crate's `digest::canonical_json` does not replicate
//! that switch. None of the 13 families this crate implements ever emits
//! a float that small (F8 `burst_detection` is the only family that emits
//! floats at all, and its magnitudes are small whole-number event counts
//! and bounded z-scores), so this is flagged rather than fixed.

use ivm_dd::digest::{canonical_json, canonicalize_floats, digest, result_digest};
use serde_json::json;

fn check(value: serde_json::Value, expected_canonical: &str, expected_digest: &str) {
    let canon = canonicalize_floats(&value);
    assert_eq!(canonical_json(&canon), expected_canonical, "canonical_json mismatch for {value:?}");
    assert_eq!(digest(&canon), expected_digest, "digest mismatch for {value:?}");
    assert_eq!(result_digest(&value), expected_digest);
}

#[test]
fn sorts_keys() {
    check(json!({"b": 2, "a": 1}), "{\"a\":1,\"b\":2}",
        "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777");
}

#[test]
fn preserves_non_ascii_unescaped() {
    check(json!({"rows": [1, 2, 3], "name": "héllo wörld"}),
        "{\"name\":\"héllo wörld\",\"rows\":[1,2,3]}",
        "5b3ae558f5c642211fb7219d21df15b084f29d80cf10f880d4007248a82a1d60");
}

#[test]
fn whole_float_keeps_decimal_point() {
    check(json!({"x": 1.0}), "{\"x\":1.0}",
        "bf32f56236899e13ef54db875d136c6cbcc65244464829c54911aa9069b0ae25");
}

#[test]
fn rounds_to_nine_decimals() {
    check(json!({"x": 1.234_567_891_23}), "{\"x\":1.234567891}",
        "3d69eed2b2107ee89ff1bc3b29c5eeb6261ed9be4080a33bdfb35fa2f4657dee");
    check(json!({"x": 0.123_456_789_5}), "{\"x\":0.123456789}",
        "1d1c6f3813faaaf19d2a8068f29eb091e9646aa1001e4fb645024c948b5c486b");
}

#[test]
fn half_values_are_exact() {
    check(json!({"x": 2.5}), "{\"x\":2.5}",
        "25b934393cfa55ee8731d92ce0707735ea809bca2bb7bd099640940014139470");
}

#[test]
fn negative_zero_stays_negative_zero() {
    // Python's round(-0.0, 9) == -0.0, and json.dumps(-0.0) == "-0.0".
    check(json!({"x": -0.0}), "{\"x\":-0.0}",
        "529436936b90ebdff10ece80ead76834aeae76aa1e44d6c334ac3ca3832a8fba");
}

#[test]
fn nested_structure_and_escaping() {
    check(
        json!({"nested": {"z": [1, 2.0, "a\"b\\c", null, true, false]}}),
        "{\"nested\":{\"z\":[1,2.0,\"a\\\"b\\\\c\",null,true,false]}}",
        "f64e7ad200945f19f436bb9a43ce432100e484a4d4a098450df883eb3070f47c",
    );
}

#[test]
fn empty_object_and_array() {
    check(json!({}), "{}", "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a");
    check(json!([]), "[]", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945");
}

#[test]
fn bare_string_value() {
    check(
        json!("plain string with unicode: café 日本語"),
        "\"plain string with unicode: café 日本語\"",
        "c5ddc9e3cf90a7a6d724d657f0f16f855125a3a244dee660d3880b1e7b0f2273",
    );
}

#[allow(clippy::approx_constant)] // deliberately the literal Python rounded, not std::f64::consts::PI
#[test]
fn pi_rounds_half_even() {
    check(json!(3.141_592_653_589_793), "3.141592654",
        "091a15dbddc28644f8025f3f17ca6da74dc1759a6ccc33d197fb0a8338f5d929");
}

#[test]
fn near_tie_ninth_decimal_literals() {
    // Neither literal sits on an *exact* binary tie (a 64-bit float's
    // binary fraction essentially never lands exactly on a decimal
    // `*.5e-9` boundary), so both resolve by ordinary nearest-value
    // rounding, not a hand-written half-even rule -- these pin Python's
    // actual `round(x, 9)` output for the two literals regardless of why
    // it landed there, which is what `digest::round9`'s doc comment
    // relies on (see `src/digest.rs`).
    check(json!({"y": 1.000_000_000_5}), "{\"y\":1.000000001}",
        "dcd39f8ca2610bf6aff2bad41fa1ef67e507ed3eb817ce4866fff9f00e2da5de");
    check(json!({"y": 1.000_000_001_5}), "{\"y\":1.000000001}",
        "dcd39f8ca2610bf6aff2bad41fa1ef67e507ed3eb817ce4866fff9f00e2da5de");
}
