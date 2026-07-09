"""Synthetic temporal graph generator (synth-v1, spec §4).

Background: temporal preferential-attachment event stream. Planted structures
(with ground-truth manifests) for T2: triangle rings, burst windows,
ping-pong pairs — plant_* functions land with M6; the background generator
is here now because benchmarks need it.

Deterministic given (seed, params); manifest saved beside the data.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Iterator

MICROS_PER_DAY = 86_400_000_000


def background_events(n_nodes: int, n_events: int, seed: int,
                      t0: int = 1_577_836_800_000_000,  # 2020-01-01 UTC
                      span_days: int = 90,
                      rel_types: tuple[str, ...] = ("MSG",),
                      pa_bias: float = 0.75) -> Iterator[dict[str, Any]]:
    """Preferential-attachment event stream: each event picks its source
    uniformly and its destination preferentially (prob. pa_bias) from prior
    event endpoints, else uniformly. Event times are sorted-uniform over the
    span."""
    rng = random.Random(seed)
    times = sorted(rng.randrange(t0, t0 + span_days * MICROS_PER_DAY)
                   for _ in range(n_events))
    touched: list[int] = []
    for i in range(n_events):
        src = rng.randrange(n_nodes)
        if touched and rng.random() < pa_bias:
            dst = touched[rng.randrange(len(touched))]
        else:
            dst = rng.randrange(n_nodes)
        if dst == src:
            dst = (src + 1 + rng.randrange(n_nodes - 1)) % n_nodes
        touched.append(src)
        touched.append(dst)
        yield {"src": f"n{src}", "dst": f"n{dst}",
               "rel_type": rng.choice(rel_types), "vt_s": times[i]}


def plant_structures(rng: random.Random, t0: int, span_days: int,
                     n_rings: int = 0, n_pingpong: int = 0, n_bursts: int = 0,
                     delta: int = MICROS_PER_DAY,
                     burst_factor: int = 8,
                     background_rate: float = 0.0,
                     rel_type: str = "MSG") -> tuple[list[dict], list[dict]]:
    """Planted structures with ground-truth manifests (T2). Planted nodes use
    reserved uids (ring{i}_*, pp{i}_*) so entity-set gold answers are exact.

    - triangle ring: u->v, v->w, w->u strictly time-ordered within delta.
    - ping-pong: u->v, v->u, u->v within delta.
    - burst: extra background events multiplying the base rate by
      `burst_factor` inside a one-day window.
    Returns (events, manifest_entries).
    """
    span = span_days * MICROS_PER_DAY
    events: list[dict] = []
    manifest: list[dict] = []

    def ordered_times(n: int) -> list[int]:
        base = t0 + rng.randrange(span - delta)
        offs = sorted(rng.sample(range(1, max(delta, n + 1)), n))
        return [base + o for o in offs]

    for i in range(n_rings):
        u, v, w = (f"ring{i}_a", f"ring{i}_b", f"ring{i}_c")
        ts = ordered_times(3)
        for (s, d), t in zip(((u, v), (v, w), (w, u)), ts):
            events.append({"src": s, "dst": d, "rel_type": rel_type, "vt_s": t})
        manifest.append({"kind": "triangle_ring", "nodes": [u, v, w],
                         "times": ts, "delta": delta})
    for i in range(n_pingpong):
        u, v = f"pp{i}_a", f"pp{i}_b"
        ts = ordered_times(3)
        for (s, d), t in zip(((u, v), (v, u), (u, v)), ts):
            events.append({"src": s, "dst": d, "rel_type": rel_type, "vt_s": t})
        manifest.append({"kind": "pingpong", "nodes": [u, v], "times": ts,
                         "delta": delta})
    for i in range(n_bursts):
        b_s = t0 + rng.randrange(span - MICROS_PER_DAY)
        b_e = b_s + MICROS_PER_DAY
        n_extra = max(10, int(background_rate * MICROS_PER_DAY
                              * (burst_factor - 1)))
        for _ in range(n_extra):
            s = f"burst{i}_{rng.randrange(20)}"
            d = f"burst{i}_{rng.randrange(20)}"
            if d == s:
                d = s + "x"
            events.append({"src": s, "dst": d, "rel_type": rel_type,
                           "vt_s": rng.randrange(b_s, b_e)})
        manifest.append({"kind": "burst", "t_a": b_s, "t_b": b_e,
                         "factor": burst_factor, "n_events": n_extra})
    return events, manifest


def generate(path: str | Path, n_nodes: int, n_events: int, seed: int,
             n_rings: int = 0, n_pingpong: int = 0, n_bursts: int = 0,
             delta: int = MICROS_PER_DAY,
             t0: int = 1_577_836_800_000_000, span_days: int = 90,
             **kwargs: Any) -> dict[str, Any]:
    """Write events JSONL (background + planted, time-sorted) + manifest."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed ^ 0x9E3779B9)
    background = list(background_events(n_nodes, n_events, seed, t0=t0,
                                        span_days=span_days, **kwargs))
    planted, entries = plant_structures(
        rng, t0, span_days, n_rings=n_rings, n_pingpong=n_pingpong,
        n_bursts=n_bursts, delta=delta,
        background_rate=n_events / (span_days * MICROS_PER_DAY))
    all_events = sorted(background + planted, key=lambda e: e["vt_s"])
    manifest = {"generator": "synth-v1", "seed": seed, "n_nodes": n_nodes,
                "n_events": len(all_events), "params": {**kwargs, "delta": delta,
                "t0": t0, "span_days": span_days}, "planted": entries}
    with open(path / "events.jsonl", "w") as f:
        for ev in all_events:
            f.write(json.dumps(ev, sort_keys=True) + "\n")
    with open(path / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest
