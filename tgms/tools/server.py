"""Tool exposure (WP1.5): in-process ToolRouter + MCP server.

- ToolRouter: what the executor uses in experiments (no network hop).
  Read-only by construction: operators never mutate the store.
- MCP server: `tgms serve --store PATH` — any MCP-capable agent attaches to
  a TGMS instance and receives the verified operator toolbox. `verify_claim`
  (WP2.3) is *not* exposed to planners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tgms.core.errors import TgmsError
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import REGISTRY, call_operator, ensure_all_registered


class ToolRouter:
    """Deterministic in-process dispatch of operator tool calls."""

    def __init__(self, adapter: StorageAdapter,
                 cost_ceilings: dict[str, int] | None = None,
                 exclude: tuple[str, ...] = (),
                 tt_source: Any = None) -> None:
        ensure_all_registered()
        self.adapter = adapter
        self.cost_ceilings = cost_ceilings
        self.exclude = set(exclude)
        #: The `Store` behind `adapter`, when the caller has one. It knows the
        #: **applied** frontier and the store's own identity, neither of which
        #: a bare adapter can answer for (TGIR_SPEC §5.6). Optional by design:
        #: every oracle-family test constructs an adapter with no store at all.
        self.tt_source = tt_source

    def tools(self) -> list[str]:
        return sorted(n for n in REGISTRY if n not in self.exclude)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Returns the operator envelope, or a structured error payload
        (never raises TgmsError — the planner repair loop consumes errors)."""
        if name in self.exclude:
            return {"error": "E_NOT_FOUND", "message": f"unknown tool: {name}",
                    "details": {}}
        try:
            return call_operator(self.adapter, name, args,
                                 cost_ceilings=self.cost_ceilings,
                                 tt_source=self.tt_source)
        except TgmsError as e:
            return e.to_payload()

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
    store."""
    from fastmcp import FastMCP

    import tgms

    store = tgms.open(store_path, read_only=readonly)
    # the store, not just its adapter: `tt_q` is the frontier the backend has
    # **applied**, and only the store can say what that is (§5.6). The store
    # used to be opened and then discarded here.
    router = ToolRouter(store.adapter, tt_source=store)
    mcp = FastMCP("tgms")

    from tgms.tools.schemas import tool_description

    for name in router.tools():
        def make_handler(op_name: str):
            def handler(args: dict[str, Any]) -> dict[str, Any]:
                return router.call(op_name, args)
            return handler

        mcp.tool(name=name, description=tool_description(name))(make_handler(name))
    return mcp
