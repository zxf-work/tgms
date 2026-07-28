//! Identity-derivation parity against Python (implementation spec §2.2).
//!
//! The fixture is generated from `tgms.core.model` itself by
//! `scripts/gen_derive_vectors.py` (`make derive-vectors`). If any assertion
//! here fails, the Rust core would produce different eids/vids than the
//! Python semantics layer — which would silently change store digests and
//! break replay against the frozen D-018/D-023 SHAs. There is no acceptable
//! fix except making Rust match Python.

use std::path::Path;

use serde::Deserialize;
use tgms_engine_core::derive::{
    canonical_json_string_array, edge_eid, sha256_hex, version_vid, Id96,
};

#[derive(Deserialize)]
struct Vectors {
    canonical_json_string_arrays: Vec<CanonCase>,
    sha256_hex: Vec<HashCase>,
    eid: Vec<EidCase>,
    vid: Vec<VidCase>,
}

#[derive(Deserialize)]
struct CanonCase {
    input: Vec<String>,
    output: String,
}

#[derive(Deserialize)]
struct HashCase {
    input: String,
    output: String,
}

#[derive(Deserialize)]
struct EidCase {
    src: String,
    dst: String,
    rel_type: String,
    disc: String,
    canonical: String,
    eid: String,
}

#[derive(Deserialize)]
struct VidCase {
    identity: String,
    tt_s: i64,
    vt_s: i64,
    vid: String,
}

fn load() -> Vectors {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/derive_vectors.json");
    let raw = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "missing parity fixture {} ({e}) — regenerate with `make derive-vectors`",
            path.display()
        )
    });
    serde_json::from_str(&raw).expect("parity fixture is valid JSON")
}

#[test]
fn canonical_json_matches_python() {
    let v = load();
    assert!(v.canonical_json_string_arrays.len() >= 30, "fixture too thin");
    for case in &v.canonical_json_string_arrays {
        let items: Vec<&str> = case.input.iter().map(String::as_str).collect();
        assert_eq!(
            canonical_json_string_array(&items),
            case.output,
            "canonical_json mismatch for {:?}",
            case.input
        );
    }
}

#[test]
fn sha256_matches_python() {
    for case in &load().sha256_hex {
        assert_eq!(
            sha256_hex(&case.input),
            case.output,
            "sha256 mismatch for {:?}",
            case.input
        );
    }
}

#[test]
fn eid_matches_python() {
    for case in &load().eid {
        // the intermediate canonical form is asserted too: when an eid ever
        // disagrees, this says whether the cause is escaping or hashing
        assert_eq!(
            canonical_json_string_array(&[&case.src, &case.dst, &case.rel_type, &case.disc]),
            case.canonical,
            "canonical form mismatch for {:?}",
            (&case.src, &case.dst, &case.rel_type, &case.disc)
        );
        let got = edge_eid(&case.src, &case.dst, &case.rel_type, &case.disc);
        assert_eq!(
            got.to_hex(),
            case.eid,
            "eid mismatch for {:?}",
            (&case.src, &case.dst, &case.rel_type, &case.disc)
        );
        // and the hex round-trip through the packed representation
        assert_eq!(Id96::from_hex(&case.eid).unwrap(), got);
    }
}

#[test]
fn vid_matches_python() {
    for case in &load().vid {
        assert_eq!(
            version_vid(&case.identity, case.tt_s, case.vt_s).to_hex(),
            case.vid,
            "vid mismatch for {:?}",
            (&case.identity, case.tt_s, case.vt_s)
        );
    }
}
