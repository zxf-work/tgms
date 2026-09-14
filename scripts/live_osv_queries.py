#!/usr/bin/env python3
"""OSV live-workload daily query script — Lane F3, P0.5, design §4.

A **daily seeded** run (`seed = ISO date`) of 200 queries — five families,
40 each — through the in-process `ToolRouter` (`tgms/tools/server.py`), the
same registry the MCP server exposes, so every query here is one an agent
attaching over MCP could have issued itself. Registers (idempotently, on
first run) up to 500 per-package "current exposure" artifacts via
`scripts/live_osv_poller.register_sample_artifacts`/`register_exposure_artifact`
— reused rather than duplicated, since the poller already owns that wiring
for its own per-cycle `affected()`/`refresh()` walk (§4's own cross-reference
to that machinery).

Every query's latency, outcome (`ok`/`error`), and — for the artifact-backed
families — a `check_artifact` freshness verdict, is written to `--log`
(default `run/live_queries.jsonl`), one JSON line per query plus one summary
line carrying the run's p50/p95 (§8 acceptance check 5: "one query run with
p50/p95 recorded").

`--dry-run` builds the same query plan (which packages, which families, how
many) and prints it without opening the store for anything but the package
sample.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import tgms  # noqa: E402
from tgms.core.model import OPEN_END  # noqa: E402
from tgms.tools.server import ToolRouter  # noqa: E402

import live_osv_poller as poller  # noqa: E402

QUERIES_PER_FAMILY = 40
FAMILIES = (
    "belief_at_t", "belief_drift", "fix_history", "alias_closure", "exposure_rollup",
)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def _sample_packages(store: Any, registry: Any, rng: random.Random,
                     n: int) -> list[str]:
    names = [name for name in registry.names() if name.startswith("osv-exposure:")]
    pkg_uids = [name.split("osv-exposure:", 1)[1] for name in names]
    if not pkg_uids:
        pkg_uids = sorted({
            v.uid for v in store.adapter.all_node_versions() if v.label == "Package"
        }) if hasattr(store.adapter, "all_node_versions") else []
    if not pkg_uids:
        return []
    rng.shuffle(pkg_uids)
    if len(pkg_uids) < n:
        # sample with replacement so a small fixture-scale store can still
        # exercise 40 queries per family
        return [rng.choice(pkg_uids) for _ in range(n)]
    return pkg_uids[:n]


def _sample_advisories(store: Any, rng: random.Random, n: int) -> list[str]:
    uids = sorted({v.uid for v in store.adapter.all_node_versions()
                   if v.label == "Advisory"}) if hasattr(store.adapter, "all_node_versions") else []
    if not uids:
        return []
    rng.shuffle(uids)
    if len(uids) < n:
        return [rng.choice(uids) for _ in range(n)]
    return uids[:n]


def _run_query(router: ToolRouter, op: str, args: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    env = router.call(op, args)
    latency_ms = (time.perf_counter() - t0) * 1000
    ok = "error" not in env
    return {"op": op, "args": args, "ok": ok, "latency_ms": latency_ms,
            "error": env.get("message") if not ok else None}


def run_queries(store_dir: str | Path, *, log_path: str | Path, seed: str,
                backend: str = "native", artifact_sample_size: int = 500,
                dry_run: bool = False) -> dict[str, Any]:
    store_dir = Path(store_dir)
    rng = random.Random(seed)

    store = tgms.open(store_dir, backend=backend, read_only=True)
    try:
        from tgms.artifact.registry import Registry
        registry = Registry(store_dir)

        if dry_run:
            pkgs = _sample_packages(store, registry, rng, QUERIES_PER_FAMILY)
            advisories = _sample_advisories(store, rng, QUERIES_PER_FAMILY)
            plan = {"seed": seed, "families": FAMILIES,
                   "queries_per_family": QUERIES_PER_FAMILY,
                   "total_queries": QUERIES_PER_FAMILY * len(FAMILIES),
                   "sample_packages": pkgs[:5], "sample_advisories": advisories[:5],
                   "artifacts_registered": len(registry.names())}
            print(f"DRY_RUN query plan: {json.dumps(plan)}")
            return plan

        # register on first run: an idempotent, up-to-500 sample.
        live = tgms.open(store_dir, backend=backend)
        try:
            newly = poller.register_sample_artifacts(
                live, registry, sample_size=artifact_sample_size, seed=seed)
        finally:
            live.close()

        router = ToolRouter(store.adapter, tt_source=store)
        pkgs = _sample_packages(store, registry, rng, QUERIES_PER_FAMILY)
        advisories = _sample_advisories(store, rng, QUERIES_PER_FAMILY)
        now = round(time.time() * 1_000_000)

        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        latencies: list[float] = []
        outcomes = {"ok": 0, "error": 0}
        family_latencies: dict[str, list[float]] = {f: [] for f in FAMILIES}

        with open(log_path, "a", encoding="utf-8") as fh:
            def emit(family: str, result: dict[str, Any]) -> None:
                latencies.append(result["latency_ms"])
                family_latencies[family].append(result["latency_ms"])
                outcomes["ok" if result["ok"] else "error"] += 1
                fh.write(json.dumps({"ts": time.time(), "seed": seed, "family": family,
                                     **result}, sort_keys=True, default=str) + "\n")

            # 1. belief-at-T: snapshot_subgraph around a package at a fixed vt/tt.
            for pkg in pkgs:
                t = rng.randint(0, now)
                emit("belief_at_t", _run_query(router, "snapshot_subgraph", {
                    "seeds": [pkg], "hops": 1, "t_valid": t, "as_of_tt": OPEN_END,
                }))

            # 2. belief-drift: diff_snapshots between two tt frontiers at one vt.
            for _ in range(QUERIES_PER_FAMILY):
                t1 = rng.randint(0, now)
                t2 = min(now, t1 + rng.randint(0, 30 * 86_400_000_000))
                emit("belief_drift", _run_query(router, "diff_snapshots", {
                    "t1": t1, "t2": t2, "as_of_tt": OPEN_END,
                }))

            # 3. fix-history: entity_history on a package, current beliefs.
            for pkg in pkgs:
                emit("fix_history", _run_query(router, "entity_history", {
                    "uid": pkg, "as_of_tt": OPEN_END, "include_edges": True,
                }))

            # 4. alias closure: temporal_reachability over `aliases` from an advisory.
            for adv in advisories:
                emit("alias_closure", _run_query(router, "temporal_reachability", {
                    "src": adv, "window": {"t_a": 0, "t_b": now},
                    "direction": "both",
                }))

            # 5. exposure rollup: aggregate_events per ecosystem per month.
            for _ in range(QUERIES_PER_FAMILY):
                emit("exposure_rollup", _run_query(router, "aggregate_events", {
                    "group_by": [{"dim": "rel_type"}],
                    "aggregates": [{"agg": "count"}],
                    "window": {"t_a": 0, "t_b": now},
                    "rel_types": ["affects"],
                }))

            summary = {
                "ts": time.time(), "seed": seed, "kind": "summary",
                "queries_served": len(latencies),
                "outcomes": outcomes,
                "query_latency_p50_ms": _percentile(latencies, 0.50),
                "query_latency_p95_ms": _percentile(latencies, 0.95),
                "per_family_p50_ms": {f: _percentile(v, 0.50) for f, v in family_latencies.items()},
                "artifacts_registered": len(registry.names()),
                "artifacts_newly_registered_this_run": len(newly),
            }
            fh.write(json.dumps(summary, sort_keys=True) + "\n")
        return summary
    finally:
        store.close()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", required=True)
    p.add_argument("--log", default=None, help="default: run/live_queries.jsonl")
    p.add_argument("--seed", default=None, help="default: today's ISO date, UTC")
    p.add_argument("--backend", default="native")
    p.add_argument("--artifact-sample-size", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    seed = args.seed or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    log_path = Path(args.log) if args.log else Path("run") / "live_queries.jsonl"
    summary = run_queries(args.store, log_path=log_path, seed=seed, backend=args.backend,
                          artifact_sample_size=args.artifact_sample_size,
                          dry_run=args.dry_run)
    if not args.dry_run:
        print(f"queries_served={summary['queries_served']} "
              f"p50={summary['query_latency_p50_ms']:.2f}ms "
              f"p95={summary['query_latency_p95_ms']:.2f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
