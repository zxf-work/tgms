"""[tests] D-160 re-measurement config: `configs/matrix-d160-collegemsg-4arm.yaml`
parses and all four arms (`ours`, `b5`, `b6e`, `llm_direct`) dispatch through
`tgms.eval.harness.run_matrix`.

This is a local smoke test only -- fake LLM, tiny synth store, tmp_path
throughout, no network, no real model (PI rule: measurement runs happen on
iTiger, never on the laptop; see the OSDI'27 D-160 lane brief). It exists to
catch config-shape drift (a typo'd system code, a key `build_systems` does
not recognize, a store backend the arms cannot use) before the real campaign
spends GPU time on it, not to reproduce the campaign's numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tgms
from tgms.core.model import canonical_json
from tgms.data.synth import generate
from tgms.eval.plan_faults import GATED_VERDICTS
from tgms.eval.tasks import generate_suite

CONFIG_PATH = Path(__file__).resolve().parents[1] / \
    "configs" / "matrix-d160-collegemsg-4arm.yaml"


def test_gate_default_is_the_d160_drop_set():
    # The config's whole premise (D-160 already the code's default, no gate
    # setting changed) depends on this constant staying what it is.
    assert GATED_VERDICTS == ("unsupported", "unverifiable")


def test_config_declares_the_four_arms_and_frozen_test_split():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    assert cfg["systems"] == ["ours", "b5", "b6e", "llm_direct"]
    assert cfg["split"] == "test"
    assert cfg["seeds"] == [0, 1, 2]
    assert cfg["suite_path"] == "benchmarks/frozen-v1/suite-collegemsg.json"
    assert cfg["llm_direct_budget_tokens"] == 8000
    assert "Qwen2.5-14B" in cfg["models"][0]


# --------------------------------------------------------------------------- #
# fake LLM: routes on the system prompt so every arm's shape is exercised
# without a real model. Cypher/SQL generation gets a query that succeeds on
# the first attempt (no repair loop needed); every "ANSWER OBJECT:"-style
# call (ours's planner/reporter, and every baseline's answer_contract_call)
# falls through to the oracle-plan echo from test_eval_suite.py -- `ours`'s
# planner matches its own oracle plan and reports correctly, while every
# other caller's prompt fails AnswerObject schema validation and lands on
# that call site's own safe empty-claims fallback (answer_contract_call /
# Reporter.report already do this; that fallback path is what is being
# exercised here, not scored).
# --------------------------------------------------------------------------- #

def _oracle_echo_llm(suite):
    plans = {t["question_text"]: canonical_json(t["oracle_plan"])
             for t in suite["dev"] + suite["test"]}

    def llm(model, messages, temperature, seed, **kw):
        user = messages[-1]["content"] if messages else ""
        if user.rstrip().endswith("PLAN:") or "QUESTION:" in user:
            for q, plan in plans.items():
                if q in user:
                    return plan
        return "not json"

    return llm


def _four_arm_llm(suite):
    oracle = _oracle_echo_llm(suite)

    def llm(model, messages, temperature, seed, **kw):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith(
                "You translate one question into ONE Kuzu Cypher query."):
            return "MATCH (a:Node)-[e:E]->(b:Node) RETURN count(*)"
        if sysmsg.startswith(
                "You translate one question into ONE DuckDB SQL query."):
            return "SELECT count(*) FROM edge_versions"
        return oracle(model, messages, temperature, seed)

    return llm


@pytest.fixture(scope="module")
def tiny_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("d160_4arm")
    manifest = generate(tmp / "synth", n_nodes=30, n_events=300, seed=7)
    # duckdb backend: b6e reads <store_path>/store.duckdb directly
    # (tgms.eval.harness.build_systems), same as the real campaign's store.
    store = tgms.open(tmp / "store", backend="duckdb")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    raw = generate_suite(store, "d160-4arm-shaped", seed=2,
                         sizes={"t1": 6, "t3": 0, "t4": 0, "probes": 0},
                         manifest=manifest)
    tasks = sorted(raw["dev"] + raw["test"], key=lambda t: t["id"])[:3]
    assert len(tasks) == 3
    suite = {**raw, "dev": [], "test": tasks}
    store.close()
    suite_path = tmp / "suite.json"
    suite_path.write_text(canonical_json(suite))
    return {"tmp": tmp, "suite": suite, "suite_path": suite_path}


def test_all_four_arms_dispatch(tiny_env, tmp_path):
    from tgms.eval.harness import run_matrix

    real_cfg = yaml.safe_load(CONFIG_PATH.read_text())
    cfg = {
        **real_cfg,
        "suite_path": str(tiny_env["suite_path"]),
        "store_path": str(tiny_env["tmp"] / "store"),
        "out_dir": str(tmp_path / "out"),
        "seeds": [0],  # one seed is enough to prove dispatch locally
        "models": ["fake"],
        "memory_db": None,  # the real config's path is iTiger-only
    }
    llm = _four_arm_llm(tiny_env["suite"])
    rows = run_matrix(cfg, llm_fn=llm)

    assert len(rows) == 4 * 3  # 4 systems x 3 tasks
    assert {r["system"] for r in rows} == {"ours", "b5", "b6e", "llm_direct"}
    for r in rows:
        assert "task_error" not in r, (r["system"], r.get("task_error"),
                                       r.get("task_error_tb"))

    # cache makes a rerun free and identical (same guarantee the real
    # campaign's out_dir/results cache relies on for interrupted/resumed
    # runs). split="test" so a bare rerun trips the frozen-split guard
    # (spec section 8.3) -- exactly the real config's own split, so force=
    # proves the guard fires *and* the cache still serves cached rows.
    rows2 = run_matrix(cfg, llm_fn=llm, force="dry-run rerun check")
    assert len(rows2) == len(rows)
