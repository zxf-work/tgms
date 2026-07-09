"""Plan IR (WP2.1): a JSON DAG of operator calls with a tiny $ref binding
language — implemented exactly as specified, and nothing more:

- `sN.<jsonpath-lite>` where jsonpath-lite supports dotted field access,
  `rows[i]` integer index, and `rows[*].field` projection to a list
  (auto-truncated to the target arg's maxItems; truncation recorded in the
  trace).
- Refs may only point to steps in `depends_on` (checked statically).
- No arithmetic, no string templating — computation is operator O13.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tgms.core.errors import InvalidArgError

MAX_STEPS = 12

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_id": {"type": "string", "minLength": 1},
        "question": {"type": "string"},
        "steps": {
            "type": "array", "minItems": 1, "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": r"^s[0-9]+$"},
                    "op": {"type": "string", "minLength": 1},
                    "args": {"type": "object"},
                    "depends_on": {"type": "array",
                                   "items": {"type": "string"}, "default": []},
                },
                "required": ["id", "op", "args"],
                "additionalProperties": False,
            },
        },
        "answer_spec": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["count", "value", "entity_set", "interval",
                                  "series", "paths", "text"]},
                "from": {"type": "string"},
            },
            "required": ["kind", "from"],
            "additionalProperties": False,
        },
    },
    "required": ["plan_id", "steps", "answer_spec"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Step:
    id: str
    op: str
    args: dict[str, Any]
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    plan_id: str
    steps: tuple[Step, ...]
    answer_spec: dict[str, Any]
    question: str = ""

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Plan":
        steps = tuple(Step(id=s["id"], op=s["op"], args=s["args"],
                           depends_on=tuple(s.get("depends_on", [])))
                      for s in obj["steps"])
        return cls(plan_id=obj["plan_id"], steps=steps,
                   answer_spec=obj["answer_spec"], question=obj.get("question", ""))

    def to_json(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "question": self.question,
            "steps": [{"id": s.id, "op": s.op, "args": s.args,
                       "depends_on": list(s.depends_on)} for s in self.steps],
            "answer_spec": self.answer_spec,
        }


# --------------------------------------------------------------------------- #
# $ref binding language                                                        #
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    r"(?:\[(?P<idx>\d+|\*)\])?$")


@dataclass(frozen=True)
class Ref:
    step_id: str
    tokens: tuple[tuple[str, str | int | None], ...]  # (field, index|'*'|None)
    raw: str


def parse_ref(raw: str) -> Ref:
    parts = raw.split(".")
    if len(parts) < 2 or not re.match(r"^s[0-9]+$", parts[0]):
        raise InvalidArgError(f"bad $ref {raw!r}: must be sN.<path>")
    tokens: list[tuple[str, str | int | None]] = []
    star_seen = False
    for part in parts[1:]:
        m = _TOKEN.match(part)
        if not m:
            raise InvalidArgError(f"bad $ref segment {part!r} in {raw!r}")
        idx: str | int | None = m.group("idx")
        if idx == "*":
            if star_seen:
                raise InvalidArgError(f"$ref {raw!r}: only one [*] projection allowed")
            star_seen = True
        elif idx is not None:
            idx = int(idx)
        tokens.append((m.group("name"), idx))
    return Ref(step_id=parts[0], tokens=tuple(tokens), raw=raw)


def resolve_ref(ref: Ref, step_output: Any) -> Any:
    """Evaluate the jsonpath-lite against one step's output."""

    def walk(obj: Any, tokens: tuple[tuple[str, str | int | None], ...]) -> Any:
        for pos, (name, idx) in enumerate(tokens):
            if not isinstance(obj, dict) or name not in obj:
                raise InvalidArgError(f"$ref {ref.raw!r}: field {name!r} not found")
            obj = obj[name]
            if idx is None:
                continue
            if not isinstance(obj, list):
                raise InvalidArgError(f"$ref {ref.raw!r}: {name!r} is not a list")
            if idx == "*":
                rest = tokens[pos + 1:]
                return [walk(el, rest) if rest else el for el in obj]
            if idx >= len(obj):
                raise InvalidArgError(
                    f"$ref {ref.raw!r}: index {idx} out of range ({len(obj)} rows)")
            obj = obj[idx]
        return obj

    return walk(step_output, ref.tokens)


def find_refs(args: Any) -> list[Ref]:
    """All $refs appearing anywhere in an args tree."""
    out: list[Ref] = []
    if isinstance(args, dict):
        if set(args) == {"$ref"}:
            out.append(parse_ref(args["$ref"]))
        else:
            for v in args.values():
                out.extend(find_refs(v))
    elif isinstance(args, list):
        for v in args:
            out.extend(find_refs(v))
    return out


def substitute_refs(args: Any, outputs: dict[str, Any],
                    truncations: list[dict[str, Any]] | None = None,
                    max_items: int | None = None) -> Any:
    """Replace every {"$ref": ...} in the args tree with its resolved value.
    List projections are auto-truncated to `max_items`, recorded in
    `truncations` for the trace."""
    if isinstance(args, dict):
        if set(args) == {"$ref"}:
            ref = parse_ref(args["$ref"])
            if ref.step_id not in outputs:
                raise InvalidArgError(f"$ref {ref.raw!r}: step output unavailable")
            val = resolve_ref(ref, outputs[ref.step_id])
            if isinstance(val, list) and max_items is not None \
                    and len(val) > max_items:
                if truncations is not None:
                    truncations.append({"ref": ref.raw, "from": len(val),
                                        "to": max_items})
                val = val[:max_items]
            return val
        return {k: substitute_refs(v, outputs, truncations, max_items)
                for k, v in args.items()}
    if isinstance(args, list):
        return [substitute_refs(v, outputs, truncations, max_items) for v in args]
    return args
