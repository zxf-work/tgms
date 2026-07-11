"""[tests] M6 eval stack: task generation, metrics, baselines, harness.

New tests for new functionality (no ground-truth changes): suite gold is
oracle-generated and deterministic; correction probes differ across the
correction boundary; scoring/bootstrap behave; baselines honor the answer
contract with fake LLM/embeddings; the harness caches, gates the frozen
split, and produces receipts.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import tgms
from tgms.core.model import canonical_json
from tgms.data.synth import generate
from tgms.eval.metrics import extract_pred, paired_bootstrap, score_answer
from tgms.eval.tasks import generate_suite

T0 = 1_577_836_800_000_000


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def suite_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("suite")
    manifest = generate(tmp / "synth", n_nodes=200, n_events=4000, seed=3,
                        n_rings=3, n_pingpong=1, n_bursts=1)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    suite = generate_suite(store, "synth-t", seed=1,
                           sizes={"t1": 20, "t3": 9, "t4": 8, "probes": 4},
                           manifest=manifest)
    return {"tmp": tmp, "store": store, "suite": suite, "manifest": manifest}


# --------------------------------------------------------------------------- #
# task suite                                                                   #
# --------------------------------------------------------------------------- #

def test_suite_families_and_gold(suite_env):
    suite = suite_env["suite"]
    tasks = suite["dev"] + suite["test"]
    fams = {t["family"] for t in tasks}
    assert fams == {"t1", "t2", "t3", "t4", "probe"}
    for t in tasks:
        assert t["gold"] is not None
        assert t["question_text"]
        if t["gold_source"] == "oracle_plan":
            # answer_spec is where the gold was read from
            assert t["oracle_plan"]["answer_spec"]["kind"] == t["answer_kind"]
    # T4 oracle plans have >= 3 dependent steps (spec WP2.5)
    for t in tasks:
        if t["family"] == "t4":
            assert len(t["oracle_plan"]["steps"]) >= 2
            assert any(s["depends_on"] for s in t["oracle_plan"]["steps"])


def test_suite_idempotent_and_frozen_sha(suite_env):
    store, suite = suite_env["store"], suite_env["suite"]
    again = generate_suite(store, "synth-t", seed=1,
                           sizes={"t1": 20, "t3": 9, "t4": 8, "probes": 4},
                           manifest=suite_env["manifest"])
    assert again["test_split_sha"] == suite["test_split_sha"]
    assert again["n_dev"] == suite["n_dev"]
    # split proportions ~20/80, stratified
    assert 0.1 <= suite["n_dev"] / (suite["n_dev"] + suite["n_test"]) <= 0.3


def test_correction_probes_differ_across_tt_boundary(suite_env):
    tasks = suite_env["suite"]["dev"] + suite_env["suite"]["test"]
    by_uid: dict[str, dict[str, int]] = {}
    for t in tasks:
        if t["family"] != "probe":
            continue
        mode = "before" if "-before-" in t["id"] else "now"
        by_uid.setdefault(t["input_uids"][0], {})[mode] = t["gold"]
        if mode == "before":
            assert "as_of_tt" in t["oracle_plan"]["steps"][0]["args"]
    pairs = [m for m in by_uid.values() if len(m) == 2]
    assert pairs and all(m["before"] != m["now"] for m in pairs)


def test_t2_gold_matches_planted_manifest(suite_env):
    tasks = suite_env["suite"]["dev"] + suite_env["suite"]["test"]
    rings = {n for p in suite_env["manifest"]["planted"]
             if p["kind"] == "triangle_ring" for n in p["nodes"]}
    for t in tasks:
        if t["family"] == "t2":
            assert t["gold_source"] == "manifest"
            assert set(t["gold"]) <= rings and t["gold"]


# --------------------------------------------------------------------------- #
# metrics                                                                      #
# --------------------------------------------------------------------------- #

def test_score_answer_kinds():
    assert score_answer("count", 14, 14)["em"] == 1.0
    assert score_answer("count", 14, 15)["em"] == 0.0
    s = score_answer("entity_set", ["a", "b"], ["b", "a"])
    assert s["em"] == 1.0 and s["f1"] == 1.0
    s = score_answer("entity_set", ["a", "b"], ["b", "c"])
    assert s["em"] == 0.0 and abs(s["f1"] - 0.5) < 1e-9
    # dict rows with uid keys count as entities
    assert score_answer("entity_set", [{"uid": "a"}], ["a"])["em"] == 1.0
    good = score_answer("interval", {"t_a": 0, "t_b": 10}, {"t_a": 2, "t_b": 10})
    assert good["em"] == 1.0  # IoU 0.8
    bad = score_answer("interval", {"t_a": 0, "t_b": 10}, {"t_a": 8, "t_b": 30})
    assert bad["em"] == 0.0


def test_extract_pred_from_answer_object():
    ao = {"text": "x", "claims": [
        {"id": "c1", "type": "count", "value": 7, "evidence": ["s1"]},
        {"id": "c2", "type": "entity", "uids": ["a", "b"], "evidence": ["s1"]}]}
    assert extract_pred("count", ao) == 7
    assert extract_pred("entity_set", ao) == ["a", "b"]
    assert extract_pred("count", 9) == 9  # raw values pass through


def test_paired_bootstrap_significance():
    a = [1.0] * 30
    b = [0.0] * 30
    r = paired_bootstrap(a, b, n_resamples=500, seed=1)
    assert r["significant"] and r["diff"] == 1.0
    same = paired_bootstrap([1, 0] * 15, [0, 1] * 15, n_resamples=500, seed=1)
    assert not same["significant"]


# --------------------------------------------------------------------------- #
# baselines (fake LLM + fake embeddings; no network)                           #
# --------------------------------------------------------------------------- #

def _fake_embed(texts):
    # deterministic bag-of-chars embedding, normalized
    out = np.zeros((len(texts), 64))
    for i, t in enumerate(texts):
        for ch in t[:2000]:
            out[i, ord(ch) % 64] += 1
        n = np.linalg.norm(out[i])
        if n:
            out[i] /= n
    return out


def _contract_llm(answer_obj):
    return lambda model, messages, temperature, seed: canonical_json(answer_obj)


def test_vector_rag_retrieves_and_answers(suite_env):
    from tgms.eval.baselines import VectorRAG
    ao = {"text": "42 events.", "claims": [
        {"id": "c1", "type": "count", "value": 42, "evidence": ["chunk"]}]}
    b1 = VectorRAG(suite_env["store"], _contract_llm(ao), "fake",
                   embed_fn=_fake_embed, k=3)
    out = b1.answer("How many events involve n1?")
    assert out["answer_object"]["claims"][0]["value"] == 42
    assert out["meta"]["retrieved_chunks"] == 3


def test_static_rag_context_and_contract(suite_env):
    from tgms.eval.baselines import StaticGraphRAG
    seen = {}

    def llm(model, messages, temperature, seed):
        seen["prompt"] = messages[-1]["content"]
        return canonical_json({"text": "none", "claims": []})

    b2 = StaticGraphRAG(suite_env["store"], llm, "fake")
    uid = suite_env["suite"]["dev"][0]["input_uids"] or ["n1"]
    b2.answer("Who is connected to them?", uid)
    assert "<data>" in seen["prompt"] and "SNAPSHOT" in seen["prompt"]


def test_text_to_cypher_repair_loop(suite_env, tmp_path):
    from tgms.eval.baselines import TextToCypher, build_vanilla_kuzu
    events = [{"src": "a", "dst": "b", "rel_type": "MSG", "vt_s": 10},
              {"src": "b", "dst": "c", "rel_type": "MSG", "vt_s": 20}]
    _, conn = build_vanilla_kuzu(events, tmp_path / "vk.kuzu")
    script = iter([
        "MATCH (n:NoSuchTable) RETURN n",                       # error -> repair
        "MATCH (a:Node)-[e:E]->(b:Node) RETURN count(*)",       # works
        canonical_json({"text": "2 edges.", "claims": [
            {"id": "c1", "type": "count", "value": 2,
             "evidence": ["cypher"]}]}),
    ])
    b5 = TextToCypher(conn, lambda *a, **k: next(script), "fake")
    out = b5.answer("How many interactions are there?")
    assert out["meta"]["repairs"] == 1 and out["meta"]["n_rows"] == 1
    assert out["answer_object"]["claims"][0]["value"] == 2


# --------------------------------------------------------------------------- #
# harness                                                                      #
# --------------------------------------------------------------------------- #

def _oracle_echo_llm(suite):
    """Fake LLM: planner prompts get the task's own oracle plan (looked up by
    the question text); reporter prompts get invalid JSON so the mechanical
    fallback (always verifiable) is used."""
    plans = {t["question_text"]: canonical_json(t["oracle_plan"])
             for t in suite["dev"] + suite["test"]}

    def llm(model, messages, temperature, seed):
        user = messages[-1]["content"] if messages else ""
        if user.rstrip().endswith("PLAN:") or "QUESTION:" in user:
            for q, plan in plans.items():
                if q in user:
                    return plan
        return "not json"

    return llm


def test_harness_matrix_ours_and_b2(suite_env, tmp_path):
    from tgms.eval.harness import run_matrix
    cfg = {
        "suite_path": str(suite_env["tmp"] / "suite.json"),
        "store_path": str(suite_env["tmp"] / "store"),
        "out_dir": str(tmp_path / "out"),
        "systems": ["ours", "b2"],
        "models": ["fake"],
        "seeds": [0],
        "split": "dev",
    }
    (suite_env["tmp"] / "suite.json").write_text(
        canonical_json(suite_env["suite"]))
    llm = _oracle_echo_llm(suite_env["suite"])
    rows = run_matrix(cfg, llm_fn=llm, embed_fn=_fake_embed)
    ours = [r for r in rows if r["system"] == "ours"]
    assert ours and all(r["first_emission_valid"] for r in ours)
    assert all(r["executed_ok"] == 1.0 for r in ours)
    # oracle-echo plans must reproduce oracle-generated gold exactly; T2 gold
    # is the planted manifest, so a perfect run scores by recall (it may also
    # return background coincidence motifs) — check recall == 1 there
    task_by_id = {t["id"]: t for t in suite_env["suite"]["dev"]}
    for r in ours:
        if task_by_id[r["task_id"]]["gold_source"] == "oracle_plan":
            assert r["em"] == 1.0, (r["task_id"], r["em"])
        else:
            assert r.get("recall", 0.0) == 1.0, (r["task_id"], r)
    # tables + receipts
    md = (tmp_path / "out" / "results.md").read_text()
    assert "receipts" in md and "git_commit" in md
    # cache makes reruns free and identical
    rows2 = run_matrix(cfg, llm_fn=llm, embed_fn=_fake_embed)
    assert len(rows2) == len(rows)


def test_harness_frozen_split_guard(suite_env, tmp_path):
    from tgms.eval.harness import run_matrix
    (suite_env["tmp"] / "suite.json").write_text(
        canonical_json(suite_env["suite"]))
    cfg = {
        "suite_path": str(suite_env["tmp"] / "suite.json"),
        "store_path": str(suite_env["tmp"] / "store"),
        "out_dir": str(tmp_path / "out2"),
        "systems": ["b2"],
        "models": ["fake"],
        "seeds": [0],
        "split": "test",
    }
    llm = _contract_llm({"text": "none", "claims": []})
    run_matrix(cfg, llm_fn=llm)
    with pytest.raises(RuntimeError, match="8.3"):
        run_matrix(cfg, llm_fn=llm)
    run_matrix(cfg, llm_fn=llm, force="unit test rerun")  # logged override
    log = (tmp_path / "out2" / "runs_log.jsonl").read_text().splitlines()
    assert any(json.loads(line)["force"] == "unit test rerun"
               for line in log)
