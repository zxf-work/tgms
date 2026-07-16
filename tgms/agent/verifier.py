"""Verifier (WP2.3).

Part (a) — static plan validation: plan-schema validity; DAG acyclicity;
$ref scoping; arg-name validity; temporal sanity on literal values;
grounding rule (literal uids must come from the task input); cost pre-check.
PVR is measured on the first emission, pre-repair.

Part (b) — dynamic claim verification: the reporter's AnswerObject is checked
claim-by-claim against the execution trace (full payloads from the
content-addressed result store). Machine checks per claim type:
- count/value: exact match (float tol 1e-9) against the referenced trace
  field ("from" jsonpath-lite) or the canonical fields count/value/rows_total.
- entity: every claimed uid must appear in evidence outputs.
- ordering: Allen-relation (or within-X) re-evaluated; endpoints must be
  grounded in evidence numbers.
- temporal_pattern: re-verified by re-running O9/O8 around the claimed
  interval with default params (reported, not gated — risk R4).
Claims citing truncated results are capped at weakly_supported. Verdicts:
supported | weakly_supported | unsupported | unverifiable.
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
                    task_input_uids: set[str] | None = None,
                    check_output_fields: bool = True) -> dict[str, Any]:
    """Returns {"valid": bool, "violations": [...]}. Never raises.
    check_output_fields=False disables output-contract validation — the E1
    ablation (CIDR): plans may then reference result fields that operators
    never emit, failing only at execution time."""
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
            elif r.tokens and check_output_fields:
                v = _check_output_field(by_id, r.step_id, r.tokens[0][0],
                                        f"$ref {r.raw!r}", s.id)
                if v:
                    violations.append(v)
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

    # answer_spec must reference an existing step and a real output field
    root = plan.answer_spec.get("from", "")
    src_step = next((s for s in plan.steps
                     if root == s.id or root.startswith(s.id + ".")), None)
    if src_step is None:
        violations.append(_violation("E_SCHEMA",
                                     f"answer_spec.from {root!r} references no step"))
    elif "." in root and check_output_fields:
        first_field = root.split(".")[1].split("[")[0]
        v = _check_output_field(by_id, src_step.id, first_field,
                                f"answer_spec.from {root!r}", None)
        if v:
            violations.append(v)

    return {"valid": not violations, "violations": violations}


def _check_output_field(by_id: dict[str, Any], step_id: str, first_field: str,
                        what: str, at_step: str | None) -> dict[str, Any] | None:
    """Paths into a step's output must name a field the operator actually
    emits — 's2.count' on entity_history is a plan bug we can reject
    statically, with the real field list in the repair payload."""
    step = by_id.get(step_id)
    spec = REGISTRY.get(step.op) if step else None
    if spec is None:
        return None  # unknown op is reported separately
    if first_field not in spec.output_fields:
        return _violation(
            "E_SCHEMA",
            f"{what}: {step.op} outputs no field {first_field!r} "
            f"(available: {', '.join(spec.output_fields)})", at_step)
    return None


# =========================================================================== #
# (b) Answer contract + dynamic claim verification                            #
# =========================================================================== #

FLOAT_TOL = 1e-9

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string",
                             "enum": ["count", "value", "entity", "ordering",
                                      "temporal_pattern"]},
                    "value": {"type": ["number", "string", "boolean", "null"]},
                    "from": {"type": "string",
                             "description": "jsonpath-lite into an evidence step"},
                    "uids": {"type": "array", "items": {"type": "string"}},
                    "a": {"type": "object"},
                    "b": {"type": "object"},
                    "relation": {"type": ["string", "object"]},
                    "assertion": {"type": "string",
                                  "enum": ["burst", "concentration", "trend"]},
                    "direction": {"type": "string",
                                  "enum": ["increasing", "decreasing"]},
                    "interval": {"type": "object"},
                    "evidence": {"type": "array", "items": {"type": "string"},
                                 "minItems": 1},
                },
                "required": ["id", "type", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["text", "claims"],
    "additionalProperties": False,
}


def _collect_all_strings(obj: Any, out: set[str]) -> None:
    """Every string leaf. The entity-grounding lexicon uses this: a claimed
    uid is grounded iff it appears as a value anywhere in the evidence
    (uid-bearing fields are not limited to keys named uid/src/dst — e.g.
    neighbors_gained is a plain list of uid strings)."""
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_all_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_all_strings(v, out)


def _collect_strings(obj: Any, keys: frozenset[str], out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                out.add(v)
            else:
                _collect_strings(v, keys, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, keys, out)


def _collect_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_numbers(v, out)


def _as_interval(x: dict[str, Any]) -> dict[str, int] | None:
    if not isinstance(x, dict):
        return None
    if "start" in x and "end" in x:
        return {"start": int(x["start"]), "end": int(x["end"])}
    if "t" in x:
        return {"start": int(x["t"]), "end": int(x["t"]) + 1}
    if "t_a" in x and "t_b" in x:
        return {"start": int(x["t_a"]), "end": int(x["t_b"])}
    return None


class ClaimVerifier:
    """Trace-grounded claim verification (the C2 mechanism)."""

    def __init__(self, trace: Any, result_store: Any,
                 adapter: StorageAdapter | None = None,
                 honor_truncation: bool = True) -> None:
        self.trace = trace
        self.results = result_store
        self.adapter = adapter
        # honor_truncation=False is the E2 ablation (CIDR): truncated or
        # tainted evidence no longer caps a claim at weakly_supported, so
        # arithmetically correct claims over incomplete pages pass as
        # fully supported — the failure mode the taint machinery prevents
        self.honor_truncation = honor_truncation
        self._steps = {s["step_id"]: s for s in trace.steps}

    # -- evidence access ---------------------------------------------------- #

    def _evidence_payloads(self, step_ids: list[str]) -> tuple[list[dict], bool, bool]:
        """(payloads, any_missing, any_truncated)"""
        payloads, missing, truncated = [], False, False
        for sid in step_ids:
            rec = self._steps.get(sid)
            if rec is None or rec.get("status") != "ok" \
                    or "result_digest" not in rec or self.results is None:
                missing = True
                continue
            payloads.append(self.results.get(rec["result_digest"]))
            truncated = truncated or bool(rec.get("truncated")) \
                or bool(rec.get("upstream_truncated"))
        return payloads, missing, truncated

    # -- per-type checks ----------------------------------------------------- #

    def _check_count(self, claim: dict, payloads: list[dict]) -> tuple[str, str]:
        if claim.get("value") is None:
            return "unverifiable", "no value in claim"
        candidates: list[Any] = []
        if "from" in claim:
            from tgms.agent.ir import parse_ref, resolve_ref
            try:
                ref = parse_ref(claim["from"])
                rec = self._steps.get(ref.step_id)
                if rec and rec.get("status") == "ok":
                    payload = self.results.get(rec["result_digest"])
                    candidates.append(resolve_ref(ref, payload))
            except TgmsError:
                return "unverifiable", f"bad from-path {claim['from']!r}"
        else:
            for p in payloads:
                candidates.extend(p.get(k) for k in ("count", "value", "rows_total")
                                  if k in p)
        want = claim["value"]
        for c in candidates:
            if isinstance(want, (int, float)) and isinstance(c, (int, float)) \
                    and not isinstance(c, bool) and abs(float(c) - float(want)) <= FLOAT_TOL:
                return "supported", f"matched {c}"
            if isinstance(want, str) and c == want:
                return "supported", f"matched {c!r}"
        return "unsupported", f"value {want!r} not found in evidence " \
                              f"(candidates: {candidates[:5]})"

    def _check_entity(self, claim: dict, payloads: list[dict]) -> tuple[str, str]:
        uids = claim.get("uids") or []
        if not uids:
            return "unverifiable", "no uids in claim"
        lexicon: set[str] = set()
        for p in payloads:
            _collect_all_strings(p, lexicon)
        missing = [u for u in uids if u not in lexicon]
        if missing:
            return "unsupported", f"uids not in evidence: {missing[:5]}"
        return "supported", "all uids grounded"

    def _check_ordering(self, claim: dict, payloads: list[dict]) -> tuple[str, str]:
        from tgms.temporal.ops_compute import allen_relation
        a, b = _as_interval(claim.get("a")), _as_interval(claim.get("b"))
        rel = claim.get("relation")
        if a is None or b is None or rel is None:
            return "unverifiable", "ordering claim needs a, b, relation"
        nums: set[float] = set()
        for p in payloads:
            _collect_numbers(p, nums)
        endpoints = {float(a["start"]), float(b["start"])}
        if not endpoints <= nums:
            return "unverifiable", "claimed timestamps not grounded in evidence"
        if isinstance(rel, dict) and "within" in rel:
            ok = abs(a["start"] - b["start"]) <= int(rel["within"])
            return ("supported", "within bound holds") if ok else \
                   ("unsupported", "within bound violated")
        actual = allen_relation(a, b)
        if actual == rel:
            return "supported", f"relation {actual} holds"
        return "unsupported", f"claimed {rel} but intervals are {actual}"

    def _check_pattern(self, claim: dict, payloads: list[dict]) -> tuple[str, str]:
        if self.adapter is None:
            return "unverifiable", "no store access for re-verification"
        iv = _as_interval(claim.get("interval"))
        if iv is None:
            return "unverifiable", "no interval in claim"
        from tgms.temporal.algebra import call_operator
        stats = self.adapter.stats()
        vt_min, vt_max = stats.get("vt_min"), stats.get("vt_max")
        if vt_min is None or vt_max is None or vt_max <= vt_min:
            return "unverifiable", "empty dataset extent"
        span = max(1, iv["end"] - iv["start"])
        w = {"t_a": max(vt_min, iv["start"] - 10 * span),
             "t_b": min(vt_max, iv["end"] + 10 * span)}
        if w["t_a"] >= w["t_b"]:
            return "unverifiable", "claimed interval outside dataset extent"
        assertion = claim.get("assertion")
        try:
            if assertion == "burst":
                res = call_operator(self.adapter, "burst_detection",
                                    {"target": {"kind": "edge_event_rate"},
                                     "window": w, "stride": span, "limit": 10_000},
                                    skip_cost_check=True)
                hit = any(r["t_a"] < iv["end"] and r["t_b"] > iv["start"]
                          for r in res["rows"])
                return ("supported", "burst re-detected") if hit else \
                       ("unsupported", "no burst flagged in claimed interval")
            series = call_operator(self.adapter, "graph_metric_timeseries",
                                   {"metric": "edge_event_count", "window": w,
                                    "stride": span, "limit": 10_000},
                                   skip_cost_check=True)["rows"]
            if assertion == "concentration":
                if not series:
                    return "unsupported", "no events in context window"
                best = max(series, key=lambda r: (r["value"], -r["t_a"]))
                hit = best["t_a"] < iv["end"] and best["t_b"] > iv["start"]
                return ("supported", "peak bucket overlaps claim") if hit else \
                       ("unsupported", f"peak at [{best['t_a']}, {best['t_b']})")
            if assertion == "trend":
                vals = [r["value"] for r in series]
                if len(vals) < 4:
                    return "unverifiable", "too few buckets for a trend"
                half = len(vals) // 2
                diff = (sum(vals[half:]) / (len(vals) - half)
                        - sum(vals[:half]) / half)
                got = "increasing" if diff > 0 else "decreasing"
                want = claim.get("direction", "increasing")
                return ("supported", f"trend {got}") if got == want else \
                       ("unsupported", f"trend is {got}, claimed {want}")
        except TgmsError as e:
            return "unverifiable", f"re-verification failed: {e}"
        return "unverifiable", f"unknown assertion {assertion!r}"

    # -- entry point ---------------------------------------------------------- #

    def verify(self, answer_object: dict[str, Any]) -> dict[str, Any]:
        try:
            jsonschema.validate(answer_object, ANSWER_SCHEMA)
        except jsonschema.ValidationError as e:
            return {"schema_valid": False, "error": e.message, "claims": [],
                    "metrics": {"ucr": 1.0, "coverage": 0.0, "n_claims": 0}}

        checks = {"count": self._check_count, "value": self._check_count,
                  "entity": self._check_entity, "ordering": self._check_ordering,
                  "temporal_pattern": self._check_pattern}
        results = []
        for claim in answer_object["claims"]:
            payloads, missing, truncated = self._evidence_payloads(claim["evidence"])
            if missing and not payloads:
                verdict, reason = "unverifiable", "evidence steps unavailable"
            else:
                try:
                    verdict, reason = checks[claim["type"]](claim, payloads)
                except Exception as e:
                    # a claim the checker cannot even parse (e.g. an
                    # unresolved $ref string where a timestamp belongs) is
                    # unverifiable — malformed evidence must never crash
                    # verification, especially on raw un-gated answers
                    verdict, reason = "unverifiable", \
                        f"malformed claim ({type(e).__name__}: {str(e)[:80]})"
                if verdict == "supported" and truncated \
                        and self.honor_truncation:
                    verdict, reason = "weakly_supported", \
                        reason + " (evidence truncated)"
            results.append({"id": claim["id"], "type": claim["type"],
                            "verdict": verdict, "reason": reason})

        uncovered = self._uncovered_assertions(answer_object)
        n = len(results)
        n_unsupported = sum(r["verdict"] == "unsupported" for r in results)
        gated = [r for r in results if r["type"] != "temporal_pattern"]  # R4
        metrics = {
            "n_claims": n,
            "ucr": (n_unsupported / n) if n else 0.0,
            "ucr_gated": (sum(r["verdict"] == "unsupported" for r in gated)
                          / len(gated)) if gated else 0.0,
            "coverage": uncovered["coverage"],
            "uncovered_assertions": uncovered["uncovered"],
        }
        return {"schema_valid": True, "claims": results, "metrics": metrics}

    def _uncovered_assertions(self, answer_object: dict[str, Any]) -> dict[str, Any]:
        """Numbers/uids in `text` with no covering claim (measured, not
        blocked)."""
        import re as _re
        text = answer_object.get("text", "")
        claimed_numbers: set[float] = set()
        claimed_uids: set[str] = set()
        for c in answer_object["claims"]:
            _collect_numbers({k: v for k, v in c.items() if k != "id"},
                             claimed_numbers)
            claimed_uids.update(c.get("uids") or [])
        lexicon: set[str] = set()
        for rec in self.trace.steps:
            if rec.get("status") == "ok" and self.results is not None:
                _collect_strings(self.results.get(rec["result_digest"]),
                                 frozenset({"uid", "src", "dst"}), lexicon)
        numbers = [float(m.replace(",", "")) for m in
                   _re.findall(r"\b\d[\d,]*\.?\d*\b", text)]
        uid_mentions = [u for u in lexicon if u and u in text]
        uncovered = [n for n in numbers if n not in claimed_numbers] + \
                    [u for u in uid_mentions if u not in claimed_uids]
        total = len(numbers) + len(uid_mentions)
        return {"uncovered": uncovered,
                "coverage": 1.0 - (len(uncovered) / total) if total else 1.0}
