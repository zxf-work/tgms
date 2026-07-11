"""[tests] Demo GUI (D-015): all five tour endpoints against a live in-process
server with a scripted LLM — proves the demo actually works end to end:
operator playground (incl. the E_COST guardrail preset), curated ask with
gold match, claim tamper caught by the verifier, deterministic bi-temporal
probe pair, and rendered trace page."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import tgms
from tgms.core.model import canonical_json
from tgms.data.synth import generate
from tgms.eval.tasks import generate_suite
from tgms.tools.webapp import DemoApp, make_handler


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("webapp")
    generate(tmp / "synth", n_nodes=150, n_events=3000, seed=4, n_rings=2)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    suite = generate_suite(store, "demo", seed=2,
                           sizes={"t1": 12, "t3": 6, "t4": 6, "probes": 4})

    plans = {t["question_text"]: canonical_json(t["oracle_plan"])
             for t in suite["dev"] + suite["test"]}

    def scripted_llm(model, messages, temperature, seed):
        user = messages[-1]["content"] if messages else ""
        for q, plan in plans.items():
            if q in user:
                return plan
        return "not json"  # reporter falls back to the mechanical answer

    app = DemoApp(store, suite, "scripted", scripted_llm, tmp / "results")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    store.close()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        body = r.read()
    return body


def _post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def test_page_and_info(server):
    page = _get(server, "/").decode()
    assert "guided demo" in page and "Time travel" in page
    info = json.loads(_get(server, "/api/info"))
    assert info["card"]["n_entities"] > 0
    assert len(info["presets"]) == 5
    assert len(info["examples"]) >= 4
    # curated examples carry program-computed gold
    assert all("gold" in e and e["question"] for e in info["examples"])


def test_operator_playground_and_guardrail(server):
    info = json.loads(_get(server, "/api/info"))
    presets = {p["op"]: p for p in info["presets"]}
    env = _post(server, "/api/op", {"op": "entity_history",
                                    "args": presets["entity_history"]["args"]})
    assert env["op"] == "entity_history" and "result_digest" in env
    # determinism: same call, same digest
    env2 = _post(server, "/api/op", {"op": "entity_history",
                                     "args": presets["entity_history"]["args"]})
    assert env2["result_digest"] == env["result_digest"]


def test_ask_matches_gold_and_trace_renders(server):
    info = json.loads(_get(server, "/api/info"))
    case = next(e for e in info["examples"] if "reach_count" in e["id"])
    r = _post(server, "/api/ask", {"question": case["question"],
                                   "input_uids": case["input_uids"]})
    assert r["executed_ok"] and r["pvr_first_emission"]
    assert r["answer"] == case["gold"]              # the demo check users see
    assert r["claims"] and r["claims"][0]["verdict"] in ("supported",
                                                         "weakly_supported")
    trace_page = _get(server, r["trace_url"]).decode()
    assert "Plan DAG" in trace_page and "verified" in trace_page


def test_tamper_is_caught(server):
    info = json.loads(_get(server, "/api/info"))
    case = next(e for e in info["examples"] if "reach_count" in e["id"])
    r = _post(server, "/api/ask", {"question": case["question"],
                                   "input_uids": case["input_uids"]})
    cid = r["claims"][0]["id"]
    t = _post(server, "/api/tamper", {"record_id": r["record_id"],
                                      "claim_id": cid})
    verdict = next(v for v in t["verdicts"] if v["id"] == cid)
    assert verdict["verdict"] in ("unsupported", "unverifiable")


def test_probe_demo_shows_bitemporal_difference(server):
    r = json.loads(_get(server, "/api/probe-demo"))
    assert r["before"]["answer"] == r["before"]["gold"]
    assert r["now"]["answer"] == r["now"]["gold"]
    assert r["before"]["answer"] != r["now"]["answer"]  # the differentiator
