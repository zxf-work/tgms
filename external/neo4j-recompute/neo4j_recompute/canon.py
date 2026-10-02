"""Canonical JSON and digest, reimplemented independently of `tgms.core.model`
(see package docstring for why).

Must match `tgms.core.model.canonical_json`/`digest`/`_canonicalize_floats`
byte-for-byte: sorted keys, compact separators, UTF-8 preserved
(`ensure_ascii=False`), sha256 hex over the canonical text, floats rounded to
9 decimals with Python's own (round-half-even) `round()` before hashing.
`tests/external/neo4j_recompute/test_canon.py` pins this against literal
values taken from `tgms/core/model.py` and `tgms/temporal/algebra.py`.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

#: Open-end sentinel (`tgms.core.model.OPEN_END`, spec §1): 2**62.
OPEN_END: int = 2**62


def canonicalize_floats(obj: Any) -> Any:
    """Round every float in `obj` to 9 decimals; raise on NaN/inf.

    Mirrors `tgms.temporal.algebra._canonicalize_floats` exactly, including
    its order of operations (round, not truncate) and its refusal to let a
    non-finite float through silently.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("non-finite float in operator output")
        return round(obj, 9)
    if isinstance(obj, dict):
        return {k: canonicalize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [canonicalize_floats(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization: sorted keys, compact, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(obj: Any) -> str:
    """sha256 of the canonical-JSON payload — TGMS's `result_digest`.

    Callers must pass the payload through `canonicalize_floats` first (the
    formatters in `format.py` do this as their last step) — this function
    does not do it implicitly, so a caller cannot forget to canonicalize and
    then wonder why a float field's digest never matches.
    """
    return sha256_hex(canonical_json(obj))
