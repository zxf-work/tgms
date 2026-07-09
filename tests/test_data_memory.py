"""M6 foundations: synth planted patterns, loader contract, evolution memory."""

from __future__ import annotations

import json

from tgms.agent.memory import EvolutionMemory, digest_numbers_check, window_facts
from tgms.data.synth import MICROS_PER_DAY, generate
from tgms.temporal.algebra import call_operator, ensure_all_registered

ensure_all_registered()

T0 = 1_577_836_800_000_000


def _ingest(tmp_path, name, **gen_kwargs):
    import tgms
    manifest = generate(tmp_path / name, seed=5, **gen_kwargs)
    store = tgms.open(tmp_path / f"{name}-store")
    with open(tmp_path / name / "events.jsonl") as f:
        store.ingest_events(json.loads(l) for l in f if l.strip())
    return store, manifest


def test_planted_triangle_rings_are_found_by_motif_operator(tmp_path):
    store, manifest = _ingest(tmp_path, "s1", n_nodes=200, n_events=2000,
                              n_rings=3, delta=MICROS_PER_DAY)
    rings = [p for p in manifest["planted"] if p["kind"] == "triangle_ring"]
    assert len(rings) == 3
    for ring in rings:
        res = call_operator(store.adapter, "count_temporal_motifs",
                            {"motif": "M_triangle_cyclic", "delta": ring["delta"],
                             "window": {"t_a": min(ring["times"]),
                                        "t_b": max(ring["times"]) + 1},
                             "node_filter": ring["nodes"]})
        assert res["count"] >= 1, ring
    store.close()


def test_planted_burst_is_flagged(tmp_path):
    store, manifest = _ingest(tmp_path, "s2", n_nodes=200, n_events=5000,
                              n_bursts=1, span_days=30)
    burst = next(p for p in manifest["planted"] if p["kind"] == "burst")
    res = call_operator(store.adapter, "burst_detection",
                        {"target": {"kind": "edge_event_rate"},
                         "window": {"t_a": T0, "t_b": T0 + 30 * MICROS_PER_DAY},
                         "stride": MICROS_PER_DAY, "limit": 100,
                         "params": {"w": 10, "z": 3.0}})
    hit = any(r["t_a"] < burst["t_b"] and r["t_b"] > burst["t_a"]
              for r in res["rows"])
    assert hit, (burst, res["rows"])
    store.close()


def test_memory_build_and_deterministic_retrieval(tmp_path):
    store, _ = _ingest(tmp_path, "s3", n_nodes=100, n_events=3000, span_days=28)
    mem = EvolutionMemory(tmp_path / "mem.sqlite")
    n = mem.build(store.adapter, stride=7 * MICROS_PER_DAY)
    assert n == 4  # ~28 days of data / weekly stride (extent-aligned)

    q_a, q_b = T0 + 8 * MICROS_PER_DAY, T0 + 10 * MICROS_PER_DAY
    notes = mem.retrieve(q_a, q_b, k=3)
    assert notes
    # best note overlaps the query window and is ranked by overlap ratio
    assert notes[0]["window_ta"] < q_b and notes[0]["window_tb"] > q_a
    # every number in a stored digest is one of the computed facts
    for note in notes:
        assert digest_numbers_check(note["text"], note["facts"])
    # rebuild is idempotent
    assert mem.build(store.adapter, stride=7 * MICROS_PER_DAY) == n
    assert mem.conn.execute(
        "SELECT count(*) FROM memory_notes").fetchone()[0] == n
    mem.close()
    store.close()


def test_memory_llm_digest_gated_by_mini_verifier(tmp_path):
    store, _ = _ingest(tmp_path, "s4", n_nodes=50, n_events=500, span_days=7)
    facts = window_facts(store.adapter, T0, T0 + 7 * MICROS_PER_DAY)

    # LLM that fabricates a number -> rejected twice -> numbers-only fallback
    mem = EvolutionMemory(tmp_path / "mem2.sqlite")
    mem.build(store.adapter, stride=7 * MICROS_PER_DAY,
              llm_fn=lambda *a, **k: "There were exactly 987654 events.",
              model="fake")
    note = mem.retrieve(T0, T0 + 7 * MICROS_PER_DAY, k=1)[0]
    assert "987654" not in note["text"]
    assert str(facts["n_events"]) in note["text"]

    # LLM that embeds the exact numbers -> accepted
    honest = f"A quiet week: {facts['n_events']} events, " \
             f"{facts['n_new_nodes']} new nodes."
    mem.build(store.adapter, stride=7 * MICROS_PER_DAY,
              llm_fn=lambda *a, **k: honest, model="fake")
    note = mem.retrieve(T0, T0 + 7 * MICROS_PER_DAY, k=1)[0]
    assert note["text"] == honest
    mem.close()
    store.close()
