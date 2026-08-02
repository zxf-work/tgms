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
    assert len(tools) == 14
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


def test_motif_cost_prices_skew_by_filter_not_by_max_degree():
    """The motif estimate must not explode on skewed degree distributions.

    Shapes are the two anchors from the Phase 0 evaluation (docs/
    eval_phase0.md): CollegeMsg — 59,835 events, one user with out-degree
    1091 — where the registry's 200-uid filtered query touches ~1.1k events
    and must be answerable; and the synthetic 200k log with a |V|/5 filter,
    whose ~30k filtered events pair 18.7M times within delta and must stay
    refused. The old `max_out_degree**2` form refused both.
    """
    from tgms.temporal.guardrails import DEFAULT_CEILINGS
    from tgms.temporal.ops_motifs import _motif_cost

    ceiling = DEFAULT_CEILINGS["expansions_est"]

    collegemsg = {"n_edge_versions": 59_835, "n_entities": 1_899,
                  "max_out_degree": 1_091, "vt_min": 0, "vt_max": 16_736_181}
    span = collegemsg["vt_max"]
    skewed = _motif_cost(
        {"delta": span // 50, "window": {"t_a": 0, "t_b": span + 1},
         "node_filter": [f"n{i}" for i in range(200)]},
        collegemsg)
    assert skewed["expansions_est"] < ceiling

    synth200k = {"n_edge_versions": 200_269, "n_entities": 2_000,
                 "max_out_degree": 142, "vt_min": 0, "vt_max": 210_000}
    explosive = _motif_cost(
        {"delta": 4_000, "window": {"t_a": 0, "t_b": 200_000},
         "node_filter": [f"n{i}" for i in range(400)]},
        synth200k)
    assert explosive["expansions_est"] > ceiling

    # same log, no filter at all: the whole window pairs with itself
    unfiltered = _motif_cost(
        {"delta": 4_000, "window": {"t_a": 0, "t_b": 200_000},
         "node_filter": None},
        synth200k)
    assert unfiltered["expansions_est"] > explosive["expansions_est"] > ceiling


def test_paths_cost_prices_the_frontier_not_the_scan():
    """The paths estimate must scale with the DFS frontier, not event count.

    Anchors are the Phase 0 evaluation shapes (docs/eval_phase0.md,
    benchmarks/results-v1/eval-10m-4sys.json): the 10M synthetic store's
    registry query — quarter-span window, max_hops=3, mean windowed
    out-degree ~25 — was refused by the old `rows * 8` form while the
    PostgreSQL baseline answered it in 37 ms, and must be answerable; the
    same store over the full window at 6 hops, and the dense 200k store
    (mean degree ~100) over the full window at 4 hops, are genuine
    frontier explosions and must stay refused.
    """
    from tgms.temporal.guardrails import DEFAULT_CEILINGS
    from tgms.temporal.ops_paths import _paths_cost

    ceiling = DEFAULT_CEILINGS["expansions_est"]

    synth10m = {"n_edge_versions": 10_000_000, "n_entities": 100_000,
                "vt_min": 0, "vt_max": 10_500_000}
    span = synth10m["vt_max"]
    registry = _paths_cost(
        {"window": {"t_a": 0, "t_b": span // 4}, "k": 3, "max_hops": 3},
        synth10m)
    assert registry["expansions_est"] < ceiling

    deep = _paths_cost(
        {"window": {"t_a": 0, "t_b": span + 1}, "max_hops": 6}, synth10m)
    assert deep["expansions_est"] > ceiling

    synth200k = {"n_edge_versions": 200_269, "n_entities": 2_000,
                 "vt_min": 0, "vt_max": 210_000}
    dense = _paths_cost(
        {"window": {"t_a": 0, "t_b": 200_000}, "max_hops": 4}, synth200k)
    assert dense["expansions_est"] > ceiling

    # the 200k registry shape keeps answering: narrowing the window or
    # the hop budget must be an effective repair suggestion
    narrowed = _paths_cost(
        {"window": {"t_a": 0, "t_b": 210_000 // 4}, "max_hops": 3}, synth200k)
    assert narrowed["expansions_est"] < ceiling


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
