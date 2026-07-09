"""Operator registry, JSON Schemas, and the self-describing result envelope.

Design rules enforced here for every operator (WP1.3):
1. Typed      — args validate against the JSON Schema before execution.
2. Determin.  — same store state + same args => byte-identical canonical output.
3. Bounded    — every operator takes `limit` (default 100, max 10,000) and
                returns `truncated` + `cursor`.
4. Bi-temporal— every operator takes `as_of_tt` (default OPEN_END = current).
5. Self-desc. — every result carries {op, args_echo, dataset_extent,
                result_digest, truncated}.

The tool schemas (WP1.5) are generated from this registry — single source of
truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

from tgms.core.errors import InvalidArgError, SchemaError
from tgms.core.model import OPEN_END, canonical_json, digest
from tgms.storage.base import StorageAdapter

DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000

# shared schema fragments
TIMESTAMP = {"type": "integer", "minimum": 0, "maximum": OPEN_END,
             "description": "int64 epoch microseconds, UTC"}
WINDOW = {
    "type": "object",
    "properties": {"t_a": TIMESTAMP, "t_b": TIMESTAMP},
    "required": ["t_a", "t_b"],
    "additionalProperties": False,
    "description": "half-open valid-time window [t_a, t_b)",
}
AS_OF_TT = {"type": "integer", "minimum": 0, "maximum": OPEN_END, "default": OPEN_END,
            "description": "belief state to evaluate under; OPEN_END = current beliefs"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT}
CURSOR = {"type": ["string", "null"], "default": None,
          "description": "opaque pagination cursor from a previous call"}
UID = {"type": "string", "minLength": 1}
UID_LIST = {"type": "array", "items": UID, "minItems": 1, "maxItems": 10_000}


@dataclass
class OperatorSpec:
    name: str
    fn: Callable[[StorageAdapter, dict[str, Any]], dict[str, Any]]
    args_schema: dict[str, Any]
    description: str
    cost_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, int]] | None = None
    validators: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)


REGISTRY: dict[str, OperatorSpec] = {}


def operator(name: str, args_schema: dict[str, Any], description: str,
             cost_fn: Callable | None = None,
             validators: list[Callable] | None = None) -> Callable:
    """Register an operator kernel. The kernel receives (adapter, args) with
    schema defaults already filled in, and returns the payload dict; the
    envelope is added by `call_operator`."""

    def deco(fn: Callable) -> Callable:
        schema = {"type": "object", "properties": dict(args_schema),
                  "additionalProperties": False,
                  "required": [k for k, v in args_schema.items()
                               if isinstance(v, dict) and v.get("_required")]}
        # strip the internal _required marker
        for v in schema["properties"].values():
            if isinstance(v, dict):
                v.pop("_required", None)
        REGISTRY[name] = OperatorSpec(name=name, fn=fn, args_schema=schema,
                                      description=description, cost_fn=cost_fn,
                                      validators=list(validators or []))
        return fn

    return deco


def required(fragment: dict[str, Any]) -> dict[str, Any]:
    return {**fragment, "_required": True}


def _fill_defaults(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    for key, sub in schema["properties"].items():
        if key not in out and isinstance(sub, dict) and "default" in sub:
            out[key] = sub["default"]
    return out


def validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    spec = REGISTRY.get(name)
    if spec is None:
        raise InvalidArgError(f"unknown operator: {name}",
                              known=sorted(REGISTRY))
    try:
        jsonschema.validate(args, spec.args_schema)
    except jsonschema.ValidationError as e:
        raise SchemaError(f"invalid args for {name}: {e.message}",
                          path=list(e.absolute_path)) from None
    filled = _fill_defaults(spec.args_schema, args)
    for v in spec.validators:
        v(filled)
    return filled


def check_window(args: dict[str, Any]) -> None:
    w = args.get("window")
    if w is not None and not (w["t_a"] < w["t_b"]):
        raise InvalidArgError(f"window requires t_a < t_b, got [{w['t_a']}, {w['t_b']})")


def call_operator(adapter: StorageAdapter, name: str, args: dict[str, Any],
                  skip_cost_check: bool = False,
                  cost_ceilings: dict[str, int] | None = None) -> dict[str, Any]:
    """Validate, execute, and wrap an operator call in the self-describing
    envelope. All agent-facing surfaces (ToolRouter, MCP) go through here."""
    filled = validate_args(name, args)
    spec = REGISTRY[name]
    stats = adapter.stats()
    if spec.cost_fn is not None and not skip_cost_check:
        from tgms.temporal.guardrails import enforce_cost  # local import: avoid cycle
        enforce_cost(name, spec.cost_fn(filled, stats), cost_ceilings)
    payload = spec.fn(adapter, filled)
    payload = _canonicalize_floats(payload)
    envelope = {
        "op": name,
        "args_echo": filled,
        # informational, reflects *current* beliefs — deliberately excluded
        # from result_digest so results pinned to a past as_of_tt stay
        # byte-identical under later writes (bi-temporal immutability)
        "dataset_extent": {"vt_min": stats.get("vt_min"), "vt_max": stats.get("vt_max")},
        "truncated": payload.get("truncated", False),
        **payload,
        "result_digest": digest(payload),
    }
    return envelope


def _canonicalize_floats(obj: Any) -> Any:
    """Round floats to 9 decimals so canonical output is platform-stable."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise InvalidArgError("non-finite float in operator output")
        return round(obj, 9)
    if isinstance(obj, dict):
        return {k: _canonicalize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonicalize_floats(v) for v in obj]
    return obj


def paginate(rows: list[Any], limit: int, cursor: str | None) -> dict[str, Any]:
    """Deterministic offset pagination over an already-ordered row list."""
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        raise InvalidArgError(f"bad cursor: {cursor!r}") from None
    window = rows[offset:offset + limit]
    truncated = offset + len(window) < len(rows)
    return {
        "rows": window,
        "rows_total": len(rows),
        "truncated": truncated,
        "cursor": str(offset + len(window)) if truncated else None,
    }


def ensure_all_registered() -> None:
    """Import all operator modules so REGISTRY is fully populated."""
    from tgms.temporal import ops_motifs, ops_paths, ops_series, ops_snapshot  # noqa: F401
