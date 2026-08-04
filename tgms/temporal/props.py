"""Reading typed values out of untyped JSON property bags (D-052).

Props are persisted as canonical JSON *text* and the store digest is taken
over those bytes, so nothing here rewrites a property — this module only
decides, per call, whether a stored value qualifies for what the caller asked
of it.

THE RULE, stated once and used by the operator, the portable fallback and the
oracle alike:

  * a value qualifies for arithmetic iff it is a JSON **number** — and a
    boolean is not one, however much `True == 1` in Python;
  * a value qualifies for a comparison iff it is in the **same type class**
    as the literal it is compared against (number/number, string/string,
    bool/bool). `"3" > 0` is not a comparison that returned False, it is not
    a comparison at all;
  * text is never parsed into a number. `"3"` is not `3`. Silent string
    coercion is the failure D-044 refused to ship, and refusing it is what
    makes this rule one sentence long;
  * a missing key, a JSON `null`, and a wrong-typed value are the same
    outcome — the row does not participate — and every one of them is
    counted, never dropped in silence.

Sharing this predicate across the three implementations is deliberate, on
the same grounds D-044 shared its `_mean`: it is a *definition*, not an
algorithm, and the point of the exercise is that all three agree on it
exactly. What stays independent is the loop each one wraps around it.
"""

from __future__ import annotations

import json
from typing import Any

#: Comparisons a property predicate may use — `compute`'s vocabulary minus
#: `contains`, which would need a containment rule per JSON type.
PROP_CMPS = ["eq", "ne", "lt", "le", "gt", "ge"]

#: Sentinel for "this row does not participate", distinct from any JSON
#: value a property could hold — `None` is a legitimate stored `null`.
SKIP = object()


def prop_keys(args: dict[str, Any]) -> list[str]:
    """Every property a call reads, in first-mention order. Lives here so the
    operator and the oracle agree on *which* properties are in play without
    the oracle reaching into the implementation for it."""
    keys: list[str] = []
    f = args.get("prop_filter")
    if f is not None:
        keys.append(f["prop"])
    for a in args["aggregates"]:
        if a.get("of") == "prop" and a["prop"] not in keys:
            keys.append(a["prop"])
    return keys


def parse_props(raw: Any) -> dict[str, Any]:
    """One property bag, from whatever the scan handed back."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def is_number(v: Any) -> bool:
    """A JSON number. `True`/`False` are JSON booleans and are excluded."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _type_class(v: Any) -> str | None:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    return None


def numeric_value(props: dict[str, Any], key: str) -> Any:
    """The property's value if it may take part in arithmetic, else SKIP."""
    v = props.get(key, SKIP)
    if v is SKIP or not is_number(v):
        return SKIP
    return v


def matches(props: dict[str, Any], key: str, cmp: str, literal: Any) -> Any:
    """True/False when the comparison is well typed, SKIP when it is not."""
    v = props.get(key, SKIP)
    if v is SKIP:
        return SKIP
    tv, tl = _type_class(v), _type_class(literal)
    if tv is None or tv != tl:
        return SKIP
    return {"eq": v == literal, "ne": v != literal, "lt": v < literal,
            "le": v <= literal, "gt": v > literal, "ge": v >= literal}[cmp]
