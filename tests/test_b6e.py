"""[tests] The SQL+E arm (b6e): descriptors from SQL's own mechanisms,
witness gating through the generic verifier.

New tests for new machinery, no ground-truth changes. Fake-LLM driven:
the SQL and the answer object are scripted, so what is tested is the
evidence mechanics — the COUNT certificate, truncation awareness, and
the gate dropping a fabricated claim while keeping a witnessed one.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.eval.baselines import BiTemporalSQLEvidence


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    from tgms.data.synth import generate
    tmp = tmp_path_factory.mktemp("b6e")
    generate(tmp / "synth", n_nodes=40, n_events=600, seed=7)
    store = tgms.open(tmp / "store", backend="duckdb")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    store.adapter.conn.execute("CHECKPOINT")
    real_src = store.adapter.conn.execute(
        "SELECT src FROM edge_versions LIMIT 1").fetchone()[0]
    store.close()
    return {"path": tmp / "store" / "store.duckdb", "real_src": real_src}


def _fake_llm(sql: str, answer_obj: dict):
    def llm(model, messages, temperature, seed, **kw):
        last = messages[-1]["content"]
        if last.endswith("SQL:"):
            return sql
        return json.dumps(answer_obj)
    return llm


def test_gate_drops_fabricated_and_keeps_witnessed(db):
    sql = "SELECT DISTINCT src FROM edge_versions ORDER BY src"
    answer = {"text": "grounded and fabricated",
              "claims": [
                  {"id": "c1", "type": "entity", "uids": [db["real_src"]],
                   "evidence": ["s1"]},
                  {"id": "c2", "type": "entity", "uids": ["n999999"],
                   "evidence": ["s1"]}]}
    arm = BiTemporalSQLEvidence(_fake_llm(sql, answer), "fake",
                                db_path=db["path"], seed=0)
    out = arm.answer("who appears?", [])
    meta = out["meta"]
    assert meta["ecqr"] is not None
    assert meta["ecqr"]["scope"]["exact_cardinality"] is not None
    verdicts = {v["id"]: v["ecqr_verdict"] for v in meta["claim_verdicts"]}
    assert verdicts["c1"] == "SUPPORTED"
    assert verdicts["c2"] == "UNSUPPORTED_NO_WITNESS"
    kept_ids = [c["id"] for c in out["answer_object"]["claims"]]
    assert kept_ids == ["c1"]
    assert meta["ucr_pre_gate_e"] == 0.5
    assert meta["pre_gate_answer"]["claims"][1]["id"] == "c2"


def test_truncated_page_is_delivery_incomplete_with_certificate(db):
    sql = "SELECT src, dst, vt_s FROM edge_versions"
    answer = {"text": "n/a", "claims": []}
    arm = BiTemporalSQLEvidence(_fake_llm(sql, answer), "fake",
                                db_path=db["path"], max_rows=10, seed=0)
    out = arm.answer("list them", [])
    e = out["meta"]["ecqr"]
    assert e["scope"]["delivery_complete"] is False   # 600 rows, page of 10
    assert e["scope"]["exact_cardinality"] > 10        # COUNT certificate
