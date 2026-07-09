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
                 exclude: tuple[str, ...] = ()) -> None:
        ensure_all_registered()
        self.adapter = adapter
        self.cost_ceilings = cost_ceilings
        self.exclude = set(exclude)

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
                                 cost_ceilings=self.cost_ceilings)
        except TgmsError as e:
            return e.to_payload()


def build_mcp_server(store_path: str | Path, readonly: bool = True):
    """FastMCP server over a TGMS store. Import is deferred so the core
    library works without the `agent` extra installed."""
    from fastmcp import FastMCP

    import tgms

    store = tgms.open(store_path)
    router = ToolRouter(store.adapter)
    mcp = FastMCP("tgms")

    from tgms.tools.schemas import tool_description

    for name in router.tools():
        spec = REGISTRY[name]

        def make_handler(op_name: str):
            def handler(args: dict[str, Any]) -> dict[str, Any]:
                return router.call(op_name, args)
            return handler

        mcp.tool(name=name, description=tool_description(name))(make_handler(name))
    return mcp
