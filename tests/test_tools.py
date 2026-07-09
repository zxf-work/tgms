"""M3: tool schema snapshot, ToolRouter error paths, E_COST, MCP round-trip."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tgms.tools.schemas import anthropic_tools, mcp_tool_definitions, openai_tools
from tgms.tools.server import ToolRouter

from .test_operators_oracle import T_MAX, build_store

SNAPSHOT = Path(__file__).parent / "snapshots" / "tool_schemas.json"


def test_tool_schema_snapshot():
    """Generated schemas are part of the research artifact — any change must
    be deliberate. Regenerate with TGMS_UPDATE_SNAPSHOTS=1."""
    tools = anthropic_tools()
    assert len(tools) == 13
    current = json.dumps(tools, indent=1, sort_keys=True)
    if os.environ.get("TGMS_UPDATE_SNAPSHOTS") or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(current)
    assert current == SNAPSHOT.read_text(), \
        "tool schemas changed; review and rerun with TGMS_UPDATE_SNAPSHOTS=1"


def test_schema_formats_are_consistent():
    a, o, m = anthropic_tools(), openai_tools(), mcp_tool_definitions()
    assert [t["name"] for t in a] == [t["function"]["name"] for t in o] \
        == [t["name"] for t in m]
    for ta, to, tm in zip(a, o, m):
        assert ta["input_schema"] == to["function"]["parameters"] == tm["inputSchema"]
        assert ta["description"]  # every tool has manual prose


def test_router_happy_path_and_errors():
    adapter, _, _ = build_store(1)
    router = ToolRouter(adapter)
    assert "resolve_entities" in router.tools()

    ok = router.call("resolve_entities", {"query": "u1"})
    assert ok["op"] == "resolve_entities" and "result_digest" in ok

    bad = router.call("resolve_entities", {"query": 42})
    assert bad["error"] == "E_SCHEMA"

    unknown = router.call("no_such_tool", {})
    assert unknown["error"] == "E_INVALID_ARG"

    invalid_window = router.call("temporal_reachability",
                                 {"src": "u0", "window": {"t_a": 9, "t_b": 9}})
    assert invalid_window["error"] == "E_INVALID_ARG"

    not_found = router.call("entity_history", {"uid": "does-not-exist"})
    assert not_found["error"] == "E_NOT_FOUND"


def test_cost_guardrail_rejects_with_suggestions():
    adapter, _, _ = build_store(1)
    router = ToolRouter(adapter, cost_ceilings={"rows_scanned_est": 1})
    res = router.call("count_temporal_motifs",
                      {"motif": "M_path_3", "delta": 10,
                       "window": {"t_a": 0, "t_b": T_MAX}})
    assert res["error"] == "E_COST"
    assert res["details"]["estimate"]["rows_scanned_est"] > 1
    assert any("window" in s for s in res["details"]["suggestions"])
    # narrowing per the suggestions succeeds under normal ceilings
    ok = ToolRouter(adapter).call("count_temporal_motifs",
                                  {"motif": "M_path_3", "delta": 10,
                                   "window": {"t_a": 0, "t_b": T_MAX},
                                   "node_filter": ["u0", "u1", "u2"]})
    assert "count" in ok


def test_mcp_round_trip(tmp_path):
    fastmcp = pytest.importorskip("fastmcp")
    import tgms
    from tgms.tools.server import build_mcp_server

    store = tgms.open(tmp_path / "mcp-store")
    store.ingest_events([
        {"src": "a", "dst": "b", "rel_type": "MSG", "vt_s": 10},
        {"src": "b", "dst": "c", "rel_type": "MSG", "vt_s": 20},
    ])
    store.close()

    mcp = build_mcp_server(tmp_path / "mcp-store")

    async def roundtrip():
        async with fastmcp.Client(mcp) as client:
            tools = await client.list_tools()
            assert {t.name for t in tools} >= {"resolve_entities",
                                               "temporal_reachability"}
            res = await client.call_tool(
                "temporal_reachability",
                {"args": {"src": "a", "window": {"t_a": 0, "t_b": 100}}})
            return res

    res = roundtrip()
    res = asyncio.run(res)
    payload = json.loads(res.content[0].text)
    assert [r["uid"] for r in payload["rows"]] == ["b", "c"]
