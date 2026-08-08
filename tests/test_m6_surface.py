"""[tests] The M6 nested-surface lever: exclusion restricts the manual
and the router together.

New tests for new machinery, no ground-truth changes. If the manual still
advertised an excluded operator, the experiment would measure error
recovery instead of interface size — the assertion here is the study's
validity precondition.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.agent.agent import Agent

EXCLUDED = ("aggregate_events", "version_history", "diff_snapshots")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    from tgms.data.synth import generate
    tmp = tmp_path_factory.mktemp("m6")
    generate(tmp / "synth", n_nodes=30, n_events=400, seed=9)
    s = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        s.ingest_events(json.loads(line) for line in f if line.strip())
    return s


def _fake_llm(model, messages, temperature, seed, **kw):
    return "{}"  # never a valid plan; planning outcome is not under test


def test_exclusion_restricts_manual_and_router(store):
    agent = Agent(store, model="fake", llm_fn=_fake_llm,
                  exclude_ops=EXCLUDED)
    for op in EXCLUDED:
        assert op not in agent.router.tools()
        assert f"### {op}" not in agent.planner.tool_manual
    assert "### entity_history" in agent.planner.tool_manual


def test_router_refuses_excluded_op_repairably(store):
    agent = Agent(store, model="fake", llm_fn=_fake_llm,
                  exclude_ops=EXCLUDED)
    res = agent.router.call("aggregate_events", {})
    assert res["error"] == "E_NOT_FOUND"  # in REPAIRABLE: a plan that
    # guesses an unlisted op gets a structured second chance


def test_empty_exclusion_is_the_full_surface(store):
    agent = Agent(store, model="fake", llm_fn=_fake_llm)
    assert len(agent.router.tools()) == 15
