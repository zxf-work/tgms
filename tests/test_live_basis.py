"""[tests] The live-basis experiment (M7): execution basis is meaningful
under corrections applied after evidence was produced.

Three properties from the evidence model §1/§2, on a real store with a
real writer interleaved:

- pinned repeatability: evidence produced at basis tt=T reproduces
  byte-identically after later corrections;
- basis distinction: current-basis results change and carry a different
  (unpinned) basis;
- mixed-basis rejection: a claim about basis T is not certifiable from
  current-basis evidence, and vice versa.

New tests for new semantics; the engine-side bi-temporal immutability
these lean on is long verified — what is tested here is that the
*evidence layer* carries it faithfully.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.core.model import EntityRef
from tgms.evidence import ExactCount, Verdict, verify
from tgms.evidence.adapter_tgms import build_ecqr


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from tgms.data.synth import generate
    from tgms.temporal.algebra import call_operator
    from tgms.tools.server import ensure_all_registered
    ensure_all_registered()
    tmp = tmp_path_factory.mktemp("basis")
    generate(tmp / "synth", n_nodes=30, n_events=400, seed=11)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    uid = store.adapter.uids_for([0])[0]
    t0 = store.clock.last_tt
    args = {"uid": uid, "as_of_tt": t0, "limit": 10000}
    before = call_operator(store.adapter, "entity_history", args)
    # the writer moves the world after the evidence exists; correction
    # windows must overlap the node's believed history (the July gotcha)
    v0 = store.adapter.believed_node_versions(uid)[0]
    for i in range(3):
        store.correct(EntityRef(kind="node", uid=uid),
                      {"revised": True, "round": i},
                      vt_s=v0.vt_s + 1 + i, vt_e=v0.vt_s + 10_000_000 + i)
    return {"path": tmp / "store", "store": store, "uid": uid, "t0": t0,
            "args": args, "before": before, "call": call_operator}


def test_pinned_repeatability_across_corrections(env):
    again = env["call"](env["store"].adapter, "entity_history", env["args"])
    assert again["result_digest"] == env["before"]["result_digest"]
    assert len(again["rows"]) == len(env["before"]["rows"])


def test_pinned_repeatability_from_a_second_readonly_handle(env):
    reader = tgms.open(env["path"], read_only=True)
    again = env["call"](reader.adapter, "entity_history", env["args"])
    assert again["result_digest"] == env["before"]["result_digest"]


def test_basis_distinction(env):
    current = env["call"](env["store"].adapter, "entity_history",
                          {"uid": env["uid"], "limit": 10000})
    assert len(current["rows"]) > len(env["before"]["rows"])
    e_pin = build_ecqr(env["before"], store_id="basis-store")
    e_cur = build_ecqr(current, store_id="basis-store")
    assert e_pin.basis.pinned and e_pin.basis.as_of_tt == env["t0"]
    assert not e_cur.basis.pinned


def test_mixed_basis_rejection_both_directions(env):
    current = env["call"](env["store"].adapter, "entity_history",
                          {"uid": env["uid"], "limit": 10000})
    e_pin = build_ecqr(env["before"], store_id="basis-store")
    e_cur = build_ecqr(current, store_id="basis-store")
    n_pin = len(env["before"]["rows"])
    # a claim about basis t0, offered current-basis evidence: rejected
    j = verify(ExactCount(n=n_pin, basis_tt=env["t0"]), e_cur, current)
    assert j.verdict == Verdict.UNSUPPORTED_BASIS_MISMATCH
    # the same claim against the pinned evidence: supported
    j2 = verify(ExactCount(n=n_pin, basis_tt=env["t0"]), e_pin,
                env["before"])
    assert j2.verdict == Verdict.SUPPORTED
    # a claim pinned to a basis that never produced this evidence: rejected
    j3 = verify(ExactCount(n=n_pin, basis_tt=env["t0"] + 999), e_pin,
                env["before"])
    assert j3.verdict == Verdict.UNSUPPORTED_BASIS_MISMATCH
