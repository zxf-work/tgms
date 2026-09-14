"""Tool exposure (WP1.5): in-process ToolRouter + MCP server.

- ToolRouter: what the executor uses in experiments (no network hop).
  Read-only by construction: operators never mutate the store.
- MCP server: `tgms serve --store PATH` — any MCP-capable agent attaches to
  a TGMS instance and receives the verified operator toolbox. `verify_claim`
  (WP2.3) is *not* exposed to planners.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tgms.core.errors import TgmsError
from tgms.storage.base import StorageAdapter
from tgms.telemetry.metrics import Metrics
from tgms.temporal.algebra import REGISTRY, call_operator, ensure_all_registered
from tgms.tools.jsonlog import CallLogger, new_request_id
from tgms.tools.limits import ConcurrencyGate, Limits, LimitRefusal, check_result_limits


class ToolRouter:
    """Deterministic in-process dispatch of operator tool calls.

    `limits`/`metrics`/`call_logger` are all optional and off/unenforced by
    default (a bare `ToolRouter(adapter)` behaves exactly as before B5/F2):
    every experiment-lane construction of this class predates those
    surfaces, and none of them should have to change to keep working.
    `tgms serve` / `tgms webapp` are the callers that turn them on.
    """

    def __init__(self, adapter: StorageAdapter,
                 cost_ceilings: dict[str, int] | None = None,
                 exclude: tuple[str, ...] = (),
                 tt_source: Any = None,
                 skip_cost_check: bool = False,
                 limits: Limits | None = None,
                 metrics: Metrics | None = None,
                 call_logger: CallLogger | None = None) -> None:
        ensure_all_registered()
        self.adapter = adapter
        self.cost_ceilings = cost_ceilings
        self.exclude = set(exclude)
        #: The `Store` behind `adapter`, when the caller has one. It knows the
        #: **applied** frontier and the store's own identity, neither of which
        #: a bare adapter can answer for (TGIR_SPEC §5.6). Optional by design:
        #: every oracle-family test constructs an adapter with no store at all.
        self.tt_source = tt_source
        #: benchmark oracle lane only (plan §2c / D-098): bypasses the
        #: admission policy under the lane's own declared budget.
        #: Production surfaces never set this.
        self.skip_cost_check = skip_cost_check
        #: B5/F2 service surface — see `tgms.tools.limits`. `Limits()` (every
        #: field `None`) enforces nothing, which is the default.
        self.limits = limits if limits is not None else Limits()
        self._gate = ConcurrencyGate(self.limits.max_concurrent)
        self.metrics = metrics if metrics is not None else Metrics()
        self.call_logger = call_logger if call_logger is not None else CallLogger()

    def tools(self) -> list[str]:
        return sorted(n for n in REGISTRY if n not in self.exclude)

    def call(self, name: str, args: dict[str, Any],
            request_id: str | None = None) -> dict[str, Any]:
        """Returns the operator envelope, or a structured error payload
        (never raises TgmsError — the planner repair loop consumes errors).

        A successful envelope carries its `request_id` in the `annotations`
        channel — added after the envelope is built, so it rides beside
        `result_digest` exactly the way `tgir.execute._tgir`'s own
        `annotations` field does (structurally digest-excluded: `digest()`
        already ran over `payload` alone before this method ever sees the
        envelope). An error payload carries it inside `details` instead,
        because `TgmsError.to_payload()`'s `{error, message, details}` shape
        is itself a frozen surface that must not grow a new top-level key.
        """
        request_id = request_id if request_id is not None else new_request_id()
        t0 = time.perf_counter()
        outcome = "ok"
        refusal_stage: str | None = None
        envelope: dict[str, Any]
        self.call_logger.log_start(request_id=request_id, tool=name)
        try:
            if name in self.exclude:
                outcome = "error"
                envelope = {"error": "E_NOT_FOUND",
                           "message": f"unknown tool: {name}",
                           "details": {"request_id": request_id}}
                return envelope

            if not self._gate.try_acquire():
                outcome, refusal_stage = "refused", "limit"
                self.metrics.counter("rejected", kind="concurrency", tool=name)
                try:
                    LimitRefusal("concurrency", self._gate.in_flight,
                                self.limits.max_concurrent, name).raise_(
                        f"{name} call")
                except TgmsError as e:
                    envelope = e.to_payload()
                    envelope["details"]["request_id"] = request_id
                return envelope
            try:
                try:
                    envelope = call_operator(
                        self.adapter, name, args,
                        cost_ceilings=self.cost_ceilings,
                        tt_source=self.tt_source,
                        skip_cost_check=self.skip_cost_check)
                except TgmsError as e:
                    outcome = "error"
                    refusal_stage = "cost" if e.code == "E_COST" else None
                    envelope = e.to_payload()
                    envelope["details"]["request_id"] = request_id
                    return envelope
                # D-155's rule applied to the service surface: count rows/
                # bytes *after* execution and refuse, never truncate.
                try:
                    check_result_limits(envelope, self.limits, name)
                except TgmsError as e:
                    outcome, refusal_stage = "refused", "limit"
                    self.metrics.counter("rejected", kind="result_limit", tool=name)
                    envelope = e.to_payload()
                    envelope["details"]["request_id"] = request_id
                    return envelope
                envelope.setdefault("annotations", {})["request_id"] = request_id
                return envelope
            finally:
                self._gate.release()
        finally:
            wall_ms = (time.perf_counter() - t0) * 1000
            store_identity = None
            generation = None
            if self.tt_source is not None:
                try:
                    store_identity = self.tt_source.store_identity
                except Exception:  # noqa: BLE001 — logging must never crash a call
                    store_identity = None
                try:
                    generation = self.adapter.generation
                except Exception:  # noqa: BLE001
                    generation = None
            self.call_logger.log_finish(
                request_id=request_id, tool=name, store_identity=store_identity,
                generation=generation, wall_ms=wall_ms, outcome=outcome,
                refusal_stage=refusal_stage)

    def leaf_meta(self, op: str, envelope: dict[str, Any]) -> dict[str, Any]:
        """The TGIR plan record for a completed call: `node_digest` /
        `plan_digest`, `completeness`, `exactness`, `provenance`, the output
        schema and `(T_v, T_b)` (TGIR_SPEC §5).

        Rebuilt from the envelope's own `args_echo` — which *is* the filled
        argument set the leaf was constructed from — so this costs one leaf
        construction and no re-validation, and cannot disagree with what ran.
        """
        from tgms.tgir.evaluate import meta_for

        spec = REGISTRY.get(op)
        if spec is None or "args_echo" not in envelope:
            return {}
        return meta_for(op, envelope["args_echo"], envelope, spec.output_fields)

    def read_basis(self, op: str) -> dict[str, Any]:
        """The freshness metadata a call to `op` would carry, without calling.

        A step that **failed or was refused still contributes its scope**
        (FRESHNESS_SEMANTICS D13.14, prohibition 3) — a correction can make it
        succeed — but an error payload is a frozen shape that must not grow new
        keys. So the executor asks for the basis separately and records it on
        the trace step. `tt_q` is captured at the moment of the ask, which is
        after the failed attempt and therefore still a lower bound.

        The basis is the **unpinned** one, over the coarse `"*"` scope: a step
        that never ran has no resolved arguments to pin an `as_of_tt` or a
        derivation to — its `$ref`s may be exactly what failed — and reporting
        the frontier over ⊤ is the conservative reading of both.
        """
        from tgms.tgir.ttq import envelope_metadata

        return envelope_metadata(self.adapter, op, None, self.tt_source)


def build_mcp_server(store_path: str | Path, readonly: bool = True):
    """FastMCP server over a TGMS store. Import is deferred so the core
    library works without the `agent` extra installed.

    `readonly=True` (the default) opens the store in reader-process mode
    (`tgms.open(..., read_only=True)`): no crash recovery, no write API, and
    the store must already exist — see `tgms.store.open` for why a second
    recovering handle is unsafe alongside a live writer. Pass
    `readonly=False` only when this server is the single writer for the
    store.

    B5/F2: this is a *server* surface, so structured JSON logging and the
    env-configured `Limits` are turned on here — unlike a bare `ToolRouter`
    built for an in-process experiment, which stays silent and unlimited
    unless its caller opts in explicitly."""
    from fastmcp import FastMCP

    import tgms
    from tgms.tools.jsonlog import CallLogger, configure_logging

    store = tgms.open(store_path, read_only=readonly)
    logger = configure_logging()
    # the store, not just its adapter: `tt_q` is the frontier the backend has
    # **applied**, and only the store can say what that is (§5.6). The store
    # used to be opened and then discarded here.
    router = ToolRouter(store.adapter, tt_source=store,
                        limits=Limits.from_env(), metrics=Metrics(),
                        call_logger=CallLogger(logger, enabled=True))
    mcp = FastMCP("tgms")

    from tgms.tools.schemas import tool_description

    for name in router.tools():
        def make_handler(op_name: str):
            def handler(args: dict[str, Any]) -> dict[str, Any]:
                return router.call(op_name, args)
            return handler

        mcp.tool(name=name, description=tool_description(name))(make_handler(name))
    return mcp
