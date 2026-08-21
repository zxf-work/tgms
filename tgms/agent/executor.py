"""Executor (WP2.2): deterministic topological execution of the plan DAG.

- Resolves $refs against materialized step outputs; the operator layer
  re-validates every resolved arg set against its JSON Schema (defense in
  depth — call_operator always validates).
- Produces an execution trace: per step {step_id, op, resolved_args,
  result_digest, rows_returned, truncated, wall_ms, status} plus full results
  in a content-addressed store on disk keyed by digest.
- Failure policy: a failed step fails its dependents; independent branches
  still run; E_COST / E_NOT_FOUND return control to the planner repair loop.
- Hard limits per plan: <= 12 steps, <= 60 s wall clock, <= 50k rows
  materialized.
"""

from __future__ import annotations

import graphlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tgms.core.errors import InvalidArgError, LimitError
from tgms.core.model import canonical_json, digest, sha256_hex
from tgms.agent.ir import MAX_STEPS, Plan, substitute_refs
from tgms.temporal.algebra import ENVELOPE_META_FIELDS
from tgms.tools.server import ToolRouter

#: The freshness metadata a step carries (TGIR_SPEC §5.6, §5.5). Digest-excluded
#: on the envelope, and likewise absent from the step's content addressing:
#: `result_digest` is the kernel payload's digest and nothing here enters it.
FRESHNESS_KEYS = ("tt_q", "pinned", "clamped", "dependency")

MAX_WALL_S = 60.0
MAX_TOTAL_ROWS = 50_000
MAX_REF_ITEMS = 10_000  # [*] projections truncate to the largest arg maxItems

#: `compute` functions that reduce their input to one number. Running one of
#: these over a truncated page produces a wrong number rather than a partial
#: one — measured at 50.5% low on CollegeMsg's directed pairs — so the step
#: is refused instead of tainted (D-061).
#:
#: Only the scalar-producing functions. `filter`, `derive`, `join` and the
#: set operations hand back rows that still carry `truncated`, and paging
#: plans and preview listings are legitimate readings of page 1. Nothing
#: escapes by that door: the taint is transitive, so a chain ending in a
#: number fails at the number.
REDUCING_FNS = frozenset({"count", "sum", "min", "max", "mean", "median",
                          "ratio", "diff", "percent", "topk"})


@dataclass
class Trace:
    plan_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    answer: Any = None
    answer_error: str | None = None
    wall_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return all(s["status"] == "ok" for s in self.steps) \
            and self.answer_error is None

    def to_json(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "steps": self.steps, "answer": self.answer,
                "answer_error": self.answer_error, "wall_ms": round(self.wall_ms, 3),
                "ok": self.ok, **self.plan_basis()}

    def plan_basis(self) -> dict[str, Any]:
        """The plan record's `(tt_q, pinned, clamped)` and `dependency`.

        **Derived by `⊎` over the steps' own scopes, never captured when the
        plan finishes** (FRESHNESS_SEMANTICS D13.17b). A completion-time capture
        would report a frontier later than the one each step actually read from,
        certifying as fresh a plan whose early steps ran against older
        results — exactly the false freshness D13.17 exists to forbid, one level
        up. The union takes the triple of whichever operand has the smaller
        minimum checkpoint offset and keeps **every** operand's checkpoint
        (D13.8, D13.8a, D13.8b), so no step's tamper-evidence is discarded.

        A plan whose steps all failed before any basis was recorded has nothing
        to union and carries no plan-level basis rather than an invented one.
        """
        from tgms.tgir.depscope import DependencyScope, union_all

        scopes = [DependencyScope.from_json(s["dependency"]) for s in self.steps
                  if isinstance(s.get("dependency"), dict)]
        if not scopes:
            return {}
        try:
            plan_scope = union_all(scopes)
        except InvalidArgError:
            # two steps over different stores do not union (D13.8 refuses at
            # construction); the plan then has no single basis to state
            return {}
        return {"tt_q": plan_scope.tt_q, "pinned": plan_scope.pinned,
                "clamped": plan_scope.clamped, "dependency": plan_scope.to_json()}


class ResultStore:
    """Content-addressed store for full step results (traces stay small)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: dict[str, Any]) -> str:
        """Keyed by `result_digest`, and written only once.

        **Invariant, recorded because it is sound by accident** (M2 plan §6.4):
        two runs producing an identical payload at different times share one
        file, so the **first** write's `tt_q` is the one that persists. Because
        `tt` is monotonic, first = earliest = rounded down = sound. The
        authoritative `tt_q` for a given execution is the **trace step's**, not
        the stored blob's — and a blob written before M2.1 carries no `tt_q` at
        all, which D13.24 step 4 already rules as `UNDECIDABLE("no-tt_q")`,
        i.e. it degrades to `POSSIBLY_STALE` and never to a false `FRESH`.
        Back-filling one would look like a kindness and would be a
        false-freshness bug; readers must not default it either.
        """
        d = payload.get("result_digest") or digest(payload)
        path = self.root / f"{d}.json"
        if not path.exists():
            path.write_text(canonical_json(payload))
        return d

    def get(self, d: str) -> dict[str, Any]:
        return json.loads((self.root / f"{d}.json").read_text())


class Executor:
    def __init__(self, router: ToolRouter, result_store: ResultStore | None = None) -> None:
        self.router = router
        self.results = result_store

    def _basis(self, op: str) -> dict[str, Any]:
        """The freshness metadata for a step, recorded **before** it runs.

        A step that fails, is skipped, or is refused never produces an
        envelope, yet it still contributes its dependency scope — a correction
        can make it succeed (FRESHNESS_SEMANTICS D13.14, prohibition 3), and
        the plan-level `⊎` would otherwise silently omit it. A successful step
        overwrites this with its own capture.
        """
        ask = getattr(self.router, "read_basis", None)
        return ask(op) if ask is not None else {}

    def run(self, plan: Plan) -> Trace:
        if len(plan.steps) > MAX_STEPS:
            raise LimitError(f"plan exceeds {MAX_STEPS} steps")
        order = list(graphlib.TopologicalSorter(
            {s.id: set(s.depends_on) for s in plan.steps}).static_order())
        by_id = {s.id: s for s in plan.steps}

        trace = Trace(plan_id=plan.plan_id)
        outputs: dict[str, Any] = {}
        failed: set[str] = set()
        total_rows = 0
        t_start = time.perf_counter()

        for sid in order:
            step = by_id[sid]
            rec: dict[str, Any] = {"step_id": sid, "op": step.op, **self._basis(step.op)}
            elapsed = time.perf_counter() - t_start
            if elapsed > MAX_WALL_S:
                rec.update(status="skipped", error={"error": "E_LIMIT",
                           "message": f"plan wall clock {elapsed:.1f}s > {MAX_WALL_S}s"})
                trace.steps.append(rec)
                failed.add(sid)
                continue
            if any(d in failed for d in step.depends_on):
                rec.update(status="skipped",
                           error={"error": "E_UPSTREAM",
                                  "message": "a dependency failed"})
                trace.steps.append(rec)
                failed.add(sid)
                continue

            truncations: list[dict[str, Any]] = []
            try:
                resolved = substitute_refs(step.args, outputs, truncations,
                                           MAX_REF_ITEMS)
            except InvalidArgError as e:
                rec.update(status="failed", error=e.to_payload())
                trace.steps.append(rec)
                failed.add(sid)
                continue
            # truncation taints dependents: a count over a truncated page is
            # not a count over the full result — the verifier caps claims
            # citing such steps at weakly_supported
            upstream_truncated = bool(truncations) or any(
                outputs.get(d, {}).get("truncated")
                or next((s2 for s2 in trace.steps
                         if s2["step_id"] == d), {}).get("upstream_truncated")
                for d in step.depends_on)

            if upstream_truncated and step.op == "compute" \
                    and resolved.get("fn") in REDUCING_FNS:
                rec.update(status="failed", upstream_truncated=True,
                           error=LimitError(
                               f"compute {resolved['fn']} would reduce a "
                               f"truncated result to one number, which is a "
                               f"wrong answer rather than a partial one. Page "
                               f"through with `cursor` and combine, or narrow "
                               f"the window or grouping so the result fits "
                               f"one page.").to_payload())
                trace.steps.append(rec)
                failed.add(sid)
                continue

            t0 = time.perf_counter()
            res = self.router.call(step.op, resolved)
            wall = (time.perf_counter() - t0) * 1000
            rec["resolved_args_sha"] = sha256_hex(canonical_json(resolved))[:16]
            rec["wall_ms"] = round(wall, 3)
            if truncations:
                rec["ref_truncations"] = truncations

            if "error" in res:
                rec.update(status="failed", error=res)
                failed.add(sid)
            else:
                rows = res.get("rows_total", len(res.get("rows", [])) or 0)
                total_rows += int(rows or 0)
                # the read's own capture supersedes the pre-call basis: it is
                # the frontier this step was actually served from (D13.16)
                rec.update({k: res[k] for k in FRESHNESS_KEYS if k in res})
                rec.update(status="ok", result_digest=res["result_digest"],
                           rows_returned=rows,
                           truncated=res.get("truncated", False),
                           upstream_truncated=upstream_truncated)
                outputs[sid] = res
                if self.results is not None:
                    self.results.put(res)
                if total_rows > MAX_TOTAL_ROWS:
                    rec["error"] = {"error": "E_LIMIT",
                                    "message": f"materialized rows {total_rows} "
                                               f"> {MAX_TOTAL_ROWS}"}
                    rec["status"] = "failed"
                    failed.add(sid)
            trace.steps.append(rec)

        trace.wall_ms = (time.perf_counter() - t_start) * 1000
        self._resolve_answer(plan, outputs, trace)
        return trace

    @staticmethod
    def _resolve_answer(plan: Plan, outputs: dict[str, Any], trace: Trace) -> None:
        from tgms.agent.ir import parse_ref, resolve_ref

        src = plan.answer_spec["from"]
        try:
            if "." in src:
                ref = parse_ref(src)
                if ref.step_id not in outputs:
                    raise InvalidArgError(f"answer source step {ref.step_id} "
                                          "did not complete")
                trace.answer = resolve_ref(ref, outputs[ref.step_id])
            else:
                if src not in outputs:
                    raise InvalidArgError(f"answer source step {src} did not complete")
                trace.answer = {k: v for k, v in outputs[src].items()
                                if k not in ENVELOPE_META_FIELDS}
        except InvalidArgError as e:
            trace.answer_error = str(e)
