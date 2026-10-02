"""`neo4j_recompute.canon` must reproduce `tgms.core.model`'s canonicalization
and digest algorithm byte-for-byte (see `canon.py`'s own docstring for why
this package reimplements it instead of importing `tgms`). This test pins
that against literal values rather than importing `tgms` itself, so it
stays meaningful even if the two ever drift (which is exactly what it is
here to catch).
"""
import hashlib
import json

import pytest
from neo4j_recompute.canon import (
    OPEN_END,
    canonical_json,
    canonicalize_floats,
    digest,
    sha256_hex,
)


def test_open_end_is_2_pow_62():
    assert OPEN_END == 2**62 == 4611686018427387904


def test_canonical_json_sorts_keys_and_is_compact():
    obj = {"b": 1, "a": 2, "c": [3, 2, 1]}
    assert canonical_json(obj) == '{"a":2,"b":1,"c":[3,2,1]}'


def test_canonical_json_preserves_utf8():
    assert canonical_json({"name": "café"}) == '{"name":"café"}'


def test_sha256_hex_matches_hashlib():
    assert sha256_hex("hello") == hashlib.sha256(b"hello").hexdigest()


def test_digest_is_sha256_of_canonical_json():
    obj = {"rows_total": 3, "rows": [1, 2, 3]}
    expected = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    assert digest(obj) == expected


def test_digest_independent_of_key_order():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_canonicalize_floats_rounds_to_9_decimals_half_even():
    # Python's own round() is half-even; this must match it exactly, not
    # reimplement half-up (the memo's §2.3 note on Neo4j's `round()`).
    assert canonicalize_floats(0.12345678949) == round(0.12345678949, 9)
    assert canonicalize_floats({"x": [1.0, 2.5, {"y": 3.000000001}]}) == {
        "x": [1.0, 2.5, {"y": round(3.000000001, 9)}]
    }


def test_canonicalize_floats_rejects_non_finite():
    with pytest.raises(ValueError):
        canonicalize_floats(float("nan"))
    with pytest.raises(ValueError):
        canonicalize_floats(float("inf"))


def test_canonicalize_floats_leaves_ints_and_strings_alone():
    assert canonicalize_floats({"n": 5, "s": "x", "b": True, "z": None}) == {
        "n": 5, "s": "x", "b": True, "z": None
    }


def test_canonical_json_roundtrips_through_stdlib_json():
    obj = {"a": [1, 2.5, None, "x"], "b": {"c": False}}
    assert json.loads(canonical_json(obj)) == obj
