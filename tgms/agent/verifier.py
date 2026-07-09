"""Verifier (WP2.3). Part (a) — static plan validation — lands with M4
because the planner repair loop and the executor depend on it; part (b) —
dynamic claim verification against execution traces — is the M5 deliverable.

Static checks: plan-schema validity; DAG acyclicity; $ref scoping (refs only
to depends_on); arg-name validity; temporal sanity on literal values;
grounding rule (literal uids must come from the task input); cost pre-check.
PVR is measured on the first emission, pre-repair.
"""

from __future__ import annotations

import graphlib
from typing import Any

import jsonschema

from tgms.core.errors import TgmsError
from tgms.core.model import OPEN_END
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import REGISTRY, ensure_all_registered
from tgms.temporal.guardrails import DEFAULT_CEILINGS
from tgms.agent.ir import PLAN_SCHEMA, Plan, find_refs

# arg keys whose literal string values are node identifiers (grounding rule)
UID_ARG_KEYS = {"uid", "src", "dst", "seeds", "node_filter"}


def _violation(code: str, message: str, step_id: str | None = None) -> dict[str, Any]:
    v = {"code": code, "message": message}
    if step_id:
        v["step_id"] = step_id
    return v


def _has_ref(x: Any) -> bool:
    if isinstance(x, dict):
        return set(x) == {"$ref"} or any(_has_ref(v) for v in x.values())
    if isinstance(x, list):
        return any(_has_ref(v) for v in x)
    return False


def _literal_uids(args: Any, inside_uid_key: bool = False) -> list[str]:
    """Literal strings sitting in uid-typed positions of an args tree."""
    out: list[str] = []
    if isinstance(args, dict):
        if set(args) == {"$ref"}:
            return []
        for k, v in args.items():
            out.extend(_literal_uids(v, inside_uid_key=k in UID_ARG_KEYS))
    elif isinstance(args, list):
        for v in args:
            out.extend(_literal_uids(v, inside_uid_key=inside_uid_key))
    elif inside_uid_key and isinstance(args, str):
        out.append(args)
    return out


def validate_static(plan_json: dict[str, Any], adapter: StorageAdapter | None = None,
                    task_input_uids: set[str] | None = None) -> dict[str, Any]:
    """Returns {"valid": bool, "violations": [...]}. Never raises."""
    ensure_all_registered()
    violations: list[dict[str, Any]] = []

    try:
        jsonschema.validate(plan_json, PLAN_SCHEMA)
    except jsonschema.ValidationError as e:
        return {"valid": False,
                "violations": [_violation("E_SCHEMA", f"plan schema: {e.message}")]}
    try:
        plan = Plan.from_json(plan_json)
    except (TgmsError, KeyError, TypeError) as e:
        return {"valid": False,
                "violations": [_violation("E_SCHEMA", f"plan parse: {e}")]}

    ids = [s.id for s in plan.steps]
    if len(set(ids)) != len(ids):
        violations.append(_violation("E_SCHEMA", "duplicate step ids"))
        return {"valid": False, "violations": violations}
    by_id = {s.id: s for s in plan.steps}

    # DAG: dependencies exist and are acyclic
    try:
        list(graphlib.TopologicalSorter(
            {s.id: set(s.depends_on) for s in plan.steps}).static_order())
    except graphlib.CycleError:
        violations.append(_violation("E_SCHEMA", "dependency cycle"))
    for s in plan.steps:
        for d in s.depends_on:
            if d not in by_id:
                violations.append(_violation("E_SCHEMA",
                                             f"unknown dependency {d!r}", s.id))

    stats = adapter.stats() if adapter is not None else {}
    vt_min, vt_max = stats.get("vt_min"), stats.get("vt_max")

    for s in plan.steps:
        spec = REGISTRY.get(s.op)
        if spec is None:
            violations.append(_violation("E_NOT_FOUND", f"unknown operator {s.op!r}",
                                         s.id))
            continue
        # $ref scoping
        try:
            refs = find_refs(s.args)
        except TgmsError as e:
            violations.append(_violation("E_SCHEMA", str(e), s.id))
            refs = []
        for r in refs:
            if r.step_id not in s.depends_on:
                violations.append(_violation(
                    "E_SCHEMA",
                    f"$ref {r.raw!r} targets step not in depends_on", s.id))
        # arg names must exist on the operator
        for k in s.args:
            if k not in spec.args_schema["properties"]:
                violations.append(_violation(
                    "E_SCHEMA", f"unknown arg {k!r} for {s.op}", s.id))
        # required args present (literal or ref)
        for k in spec.args_schema.get("required", []):
            if k not in s.args:
                violations.append(_violation(
                    "E_SCHEMA", f"missing required arg {k!r} for {s.op}", s.id))
        # temporal sanity on literal values
        w = s.args.get("window")
        if isinstance(w, dict) and not _has_ref(w) and {"t_a", "t_b"} <= set(w):
            if not (isinstance(w["t_a"], int) and isinstance(w["t_b"], int)
                    and w["t_a"] < w["t_b"]):
                violations.append(_violation(
                    "E_INVALID_ARG", f"window requires t_a < t_b, got {w}", s.id))
            elif vt_min is not None and (w["t_b"] <= vt_min or w["t_a"] > vt_max):
                violations.append(_violation(
                    "E_INVALID_ARG",
                    f"window {w} does not intersect dataset extent "
                    f"[{vt_min}, {vt_max}]", s.id))
        d = s.args.get("delta")
        if isinstance(d, int) and d <= 0:
            violations.append(_violation("E_INVALID_ARG", "delta must be > 0", s.id))
        a = s.args.get("as_of_tt")
        if isinstance(a, int) and not (0 <= a <= OPEN_END):
            violations.append(_violation("E_INVALID_ARG",
                                         "as_of_tt out of range", s.id))
        if s.op == "diff_snapshots" and isinstance(s.args.get("t1"), int) \
                and s.args.get("t1") == s.args.get("t2"):
            violations.append(_violation(
                "E_INVALID_ARG", "diff_snapshots with t1 == t2 is vacuous", s.id))
        # grounding rule
        if task_input_uids is not None:
            for uid in _literal_uids(s.args):
                if uid not in task_input_uids:
                    violations.append(_violation(
                        "E_GROUNDING",
                        f"literal uid {uid!r} not in task input; obtain uids "
                        "via resolve_entities and a $ref", s.id))
        # cost pre-check (only for ref-free args — bindings unknown statically)
        if spec.cost_fn is not None and adapter is not None and not _has_ref(s.args):
            try:
                est = spec.cost_fn({**{k: v.get("default")
                                       for k, v in spec.args_schema["properties"].items()
                                       if isinstance(v, dict)}, **s.args}, stats)
                over = {k: est.get(k, 0) for k in DEFAULT_CEILINGS
                        if est.get(k, 0) > DEFAULT_CEILINGS[k]}
                if over:
                    violations.append(_violation(
                        "E_COST", f"estimated cost exceeds ceilings: {over}", s.id))
            except (TgmsError, KeyError, TypeError):
                pass  # runtime validation will produce the authoritative error

    # answer_spec must reference an existing step
    root = plan.answer_spec.get("from", "")
    if not any(root == s.id or root.startswith(s.id + ".") for s in plan.steps):
        violations.append(_violation("E_SCHEMA",
                                     f"answer_spec.from {root!r} references no step"))

    return {"valid": not violations, "violations": violations}
