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


def generate(path: str | Path, n_nodes: int, n_events: int, seed: int,
             **kwargs: Any) -> dict[str, Any]:
    """Write events JSONL + manifest; returns the manifest."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    manifest = {"generator": "synth-v1", "seed": seed, "n_nodes": n_nodes,
                "n_events": n_events, "params": kwargs, "planted": []}
    with open(path / "events.jsonl", "w") as f:
        for ev in background_events(n_nodes, n_events, seed, **kwargs):
            f.write(json.dumps(ev, sort_keys=True) + "\n")
    with open(path / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest
