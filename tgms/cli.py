"""CLI (spec 7.3.3): thin argparse wrappers over the library — no logic here."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tgms")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="ingest an event JSONL file into a store")
    p_ing.add_argument("events_jsonl")
    p_ing.add_argument("--store", required=True)
    p_ing.add_argument("--backend", default="duckdb", choices=["duckdb", "kuzu"])

    p_synth = sub.add_parser("synth", help="generate a synthetic dataset")
    p_synth.add_argument("out_dir")
    p_synth.add_argument("--nodes", type=int, default=1000)
    p_synth.add_argument("--events", type=int, default=100_000)
    p_synth.add_argument("--seed", type=int, default=0)

    p_call = sub.add_parser("call", help="call one operator against a store")
    p_call.add_argument("--store", required=True)
    p_call.add_argument("op")
    p_call.add_argument("args_json")

    p_bench = sub.add_parser("bench", help="run micro-benchmarks")
    p_bench.add_argument("what", choices=["ops"])
    p_bench.add_argument("--store", required=True)
    p_bench.add_argument("--out", default="bench_report.md")

    p_serve = sub.add_parser("serve", help="serve the store over MCP")
    p_serve.add_argument("--store", required=True)
    p_serve.add_argument("--readonly", action="store_true", default=True)

    p_tasks = sub.add_parser("tasks", help="generate a task suite with oracle gold")
    p_tasks.add_argument("--store", required=True)
    p_tasks.add_argument("--dataset", required=True)
    p_tasks.add_argument("--seed", type=int, default=0)
    p_tasks.add_argument("--manifest", default=None,
                         help="synth manifest.json for T2 planted tasks")
    p_tasks.add_argument("--out", required=True)

    p_eval = sub.add_parser("eval", help="run the experiment matrix / C2 readout")
    p_eval.add_argument("action", choices=["run", "c2"])
    p_eval.add_argument("--config", default=None)
    p_eval.add_argument("--store", default=None)
    p_eval.add_argument("--suite", default=None)
    p_eval.add_argument("--mutants", type=int, default=500)
    p_eval.add_argument("--force", default=None,
                        help="rerun the frozen test split; reason is logged (§8.3)")

    p_mem = sub.add_parser("memory", help="evolution-memory maintenance")
    p_mem.add_argument("action", choices=["build"])
    p_mem.add_argument("--store", required=True)
    p_mem.add_argument("--stride-days", type=int, default=7)
    p_mem.add_argument("--refresh-stale", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "ingest":
        import tgms
        store = tgms.open(args.store, backend=args.backend)
        with open(args.events_jsonl) as f:
            tt = store.ingest_events(json.loads(line) for line in f if line.strip())
        print(json.dumps({"last_tt": tt, "stats": store.stats()}, default=str))
        store.close()
    elif args.cmd == "synth":
        from tgms.data.synth import generate
        m = generate(args.out_dir, args.nodes, args.events, args.seed)
        print(json.dumps(m))
    elif args.cmd == "call":
        import tgms
        from tgms.tools.server import ToolRouter
        store = tgms.open(args.store)
        res = ToolRouter(store.adapter).call(args.op, json.loads(args.args_json))
        print(json.dumps(res, indent=1))
        store.close()
        if "error" in res:
            return 1
    elif args.cmd == "bench":
        from tgms.eval.bench_ops import run_bench
        report = run_bench(args.store)
        with open(args.out, "w") as f:
            f.write(report)
        print(report)
    elif args.cmd == "serve":
        from tgms.tools.server import build_mcp_server
        build_mcp_server(args.store).run()
    elif args.cmd == "tasks":
        import tgms
        from tgms.eval.tasks import generate_suite
        store = tgms.open(args.store)
        manifest = None
        if args.manifest:
            with open(args.manifest) as f:
                manifest = json.load(f)
        suite = generate_suite(store, args.dataset, seed=args.seed,
                               manifest=manifest)
        with open(args.out, "w") as f:
            json.dump(suite, f, indent=1, sort_keys=True)
        print(json.dumps({"n_dev": suite["n_dev"], "n_test": suite["n_test"],
                          "test_split_sha": suite["test_split_sha"]}))
        store.close()
    elif args.cmd == "eval" and args.action == "c2":
        import tgms
        from tgms.eval.faults import c2_readout_from_suite
        store = tgms.open(args.store)
        with open(args.suite) as f:
            suite = json.load(f)
        stats = c2_readout_from_suite(store, suite, n_mutants=args.mutants)
        print(json.dumps(stats, indent=1))
        store.close()
        if not stats["accepted"]:
            return 1
    elif args.cmd == "eval":
        import yaml
        from tgms.agent.planner import make_llm_fn
        from tgms.eval.harness import run_matrix
        with open(args.config) as f:
            cfg = yaml.safe_load(f)
        llm_fn = make_llm_fn(api_base=cfg.get("llm_api_base"),
                             api_key=cfg.get("llm_api_key"),
                             extra_body=cfg.get("llm_extra_body"))
        rows = run_matrix(cfg, llm_fn=llm_fn, force=args.force)
        print(json.dumps({"rows": len(rows), "out_dir": cfg["out_dir"]}))
    elif args.cmd == "memory":
        import tgms
        from tgms.agent.memory import MICROS_PER_DAY, EvolutionMemory
        store = tgms.open(args.store)
        mem = EvolutionMemory(f"{args.store}/memory.sqlite")
        n = mem.build(store.adapter, stride=args.stride_days * MICROS_PER_DAY,
                      as_of_tt=store.clock.last_tt,
                      refresh_stale=args.refresh_stale)
        print(json.dumps({"notes": n, "refresh_stale": args.refresh_stale}))
        mem.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
