"""The equivalence comparator (M2 plan §8.1) — what "byte-identical" means.

**`canonical_json(payload_of(envelope))` equality**, where `payload_of` strips
exactly the envelope-level keys. Not envelope equality, and not `result_digest`
alone.

Three properties this definition is chosen to have:

1. **It is exactly the set `digest()` already covers.** `result_digest` is
   `digest(payload)`, so `payload_of(env)` reconstructs the digested object:
   digest equality is the fast path and `canonical_json` inequality is the
   diagnosis — the same object at two granularities.
2. **It is exactly the comparator the oracle suite uses**
   (`tests/conftest.py::ENVELOPE_META_KEYS`). The equivalence comparator and
   the oracle comparator being the same function is not a coincidence to be
   admired but a property to be maintained: if they drift, a compiled path can
   pass one and fail the other.
3. **`dependency` must be excluded, and this costs something real.** §6 #4
   states outright that compiled `diff_snapshots` **is** carve-reachable while
   its leaf is **not**, so their scopes are *required* to differ and a
   comparator including `dependency` would reject every correct compilation.
   The price is that the equivalence suite is blind to scope regressions, which
   is why a **separate, non-gating scope-diff receipt** exists beside it.
"""

from __future__ import annotations

from typing import Any

from tgms.core.model import canonical_json
from tgms.temporal.algebra import ENVELOPE_META_FIELDS

#: Envelope-level keys, i.e. everything `digest()` does not cover.
ENVELOPE_KEYS: tuple[str, ...] = ENVELOPE_META_FIELDS + ("result_digest",)


def payload_of(envelope: dict[str, Any]) -> dict[str, Any]:
    """The digested object: the envelope minus its metadata.

    `truncated`, `cursor`, `rows_total` and every `*_total` stay **inside** the
    comparison — they are C4's contract.
    """
    return {k: v for k, v in envelope.items() if k not in ENVELOPE_KEYS}


def same_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return canonical_json(payload_of(left)) == canonical_json(payload_of(right))


def first_divergence(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    """A human-readable first difference, for the receipt. `None` when the two
    payloads are byte-identical."""
    a, b = payload_of(left), payload_of(right)
    if canonical_json(a) == canonical_json(b):
        return None
    for key in sorted(set(a) | set(b)):
        if key not in a:
            return f"{key}: missing from the compiled payload"
        if key not in b:
            return f"{key}: missing from the leaf payload"
        if canonical_json(a[key]) == canonical_json(b[key]):
            continue
        if isinstance(a[key], list) and isinstance(b[key], list):
            if len(a[key]) != len(b[key]):
                return f"{key}: {len(a[key])} rows vs {len(b[key])}"
            for i, (x, y) in enumerate(zip(a[key], b[key])):
                if canonical_json(x) != canonical_json(y):
                    return _row_divergence(key, i, x, y)
        return f"{key}: {canonical_json(a[key])[:120]} vs {canonical_json(b[key])[:120]}"
    return "payloads differ but no field does"


def _row_divergence(key: str, index: int, left: Any, right: Any) -> str:
    if isinstance(left, dict) and isinstance(right, dict):
        fields = sorted(set(left) | set(right))
        differing = [f for f in fields
                     if canonical_json(left.get(f)) != canonical_json(right.get(f))]
        return (f"{key}[{index}] differs in {differing}: "
                f"{ {f: left.get(f) for f in differing} } vs "
                f"{ {f: right.get(f) for f in differing} }")
    return f"{key}[{index}]: {left} vs {right}"


__all__ = ["ENVELOPE_KEYS", "first_divergence", "payload_of", "same_payload"]
