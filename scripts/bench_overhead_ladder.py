"""The five-rung overhead ladder, on ONE store, in ONE run (OSDI'27 lane D4).

**The ladder.** Five conditions, cheapest to most end-to-end, each answering
"what does going through TGIR / the agent path cost, on top of the bare
kernel, for one query?":

  1. leaf overhead      -- wrapping an operator as an opaque TGIR leaf
                            (`bench_leaf_overhead.py`, imported).
  2. compiled vs kernel -- the compositional core route vs the hand kernel,
                            on the two operators that have both
                            (`bench_compiled_vs_kernel.py`, imported).
  3. trace bytes        -- size of the execution trace the agent executor
                            actually produces and would ship as evidence.
  4. verify ms          -- wall time of claim verification over that
                            evidence (`tgms.agent.verifier.ClaimVerifier`).
  5. tokens / tool calls-- the agent path's message and tool-call footprint,
                            with NO LLM call anywhere (see below).

Rungs 1-2 reuse `scripts/bench_leaf_overhead.py` and
`scripts/bench_compiled_vs_kernel.py` by import (`importlib` from their file
path -- `scripts/` is not a package, and this is the same technique
`tests/test_e14_harnesses.py` already uses). Neither script needed a
refactor: their case/measure/run_child functions were already free
functions guarded by `if __name__ == "__main__"`.

**Ambiguity this harness had to resolve.** `benchmarks/tgir-v1/merged.yaml`
does not exist in this tree (checked across every branch) and neither does
`docs/design/OSDI27_PAPER_SKELETON_2026-09-15.md`, so the ladder's rung
names and boundaries come from this script's own docstring and the task
brief, not from a frozen spec file. Two design calls follow from that:

- **What "verify" means (rung 4).** `tgms/tgir/check.py::check_trace` is a
  *freshness* check ("could a correction since have changed this?"), not
  claim verification. The evidence/claim verification step the agent path
  actually runs is `tgms.agent.verifier.ClaimVerifier.verify`, invoked from
  `tgms.eval.harness.run_task_ours` on the reporter's AnswerObject. Rung 4
  times that call. `tgms/tgir/*` stays import-only either way.
- **What "plan" means (`--plans`).** The two existing rung scripts key off
  raw operator names, not a plan file. `benchmarks/tgir-v1/plans/*.json` are
  TGIR *node* plans (a different Plan class, `tgms.tgir.plan.Plan`) used for
  equivalence testing, not the agent's executable IR. This harness instead
  takes agent-IR plan files (`tgms.agent.ir.Plan.from_json` -- `{plan_id,
  steps:[{id,op,args,depends_on}], answer_spec}`), the same JSON shape
  `Executor.run` already consumes and `tests/test_agent.py` already
  constructs by hand. For rungs 1-2, the plan's distinct step ops are
  intersected with each script's own covered-operator set (all fifteen for
  rung 1, `{entity_history, version_history}` for rung 2); a plan touching
  neither compiled operator simply contributes no rung-2 rows, which is
  rung 2's documented population, not a failure.

**No LLM calls.** Rung 5 never asks a planner or reporter LLM for anything.
The agent-IR plan is loaded from disk and run directly through
`Executor.run` (skips `tgms.agent.agent.Agent`/`Planner` entirely — the
"agent path" being measured is execution + evidence + reporting, not
planning). The AnswerObject rung 4 verifies is
`tgms.agent.reporter.mechanical_answer(plan, trace)`, the same
deterministic, LLM-free fallback `Reporter` itself falls back to and the
task-suite gold-answer generator already relies on. Rung 5's "message"
tokens are counted over the exact system+user message text
`tgms.agent.reporter.Reporter.report` would send an LLM (built, never
sent); its "tool" tokens are counted over each step's resolved request args
and stored response payload -- the round trip `run_task_ours` drives through
`ToolRouter.call`.

**One condition per process.** Exactly as the two rung-1/2 scripts already
do: this script's own orchestrator spawns one child interpreter per
(rung, plan) pair (`--single-rung/--single-plan`), so no rung's cache or
import state leaks into the next. Rungs 1-2 additionally get *their own*
per-arm subprocess isolation for free, inherited from the imported
`run_child` functions.

    PYTHONPATH=$PWD python scripts/bench_overhead_ladder.py \\
        --store stores/bitcoinotc --plans benchmarks/ladder-v1/plans \\
        --rungs 1,2,3,4,5 --reps 10 --seed 0 \\
        --out benchmarks/results-v1/overhead-ladder-bitcoinotc.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

THIS_FILE = Path(__file__).resolve()

#: Rungs 3-5 (agent-path timing) get their own, smaller warmup/rep protocol;
#: rungs 1-2 keep whichever protocol the imported scripts already use.
WARMUPS = 2
CALL_CEILING_S = 300

#: `Reporter.report`'s system message (`tgms.agent.reporter.REPORTER_SYSTEM`)
#: is a fixed string, unrelated to any one query -- rung 5 counts it once per
#: plan alongside the per-query user message, mirroring what one call to
#: `Reporter.report` would actually send.


# --------------------------------------------------------------------------- #
# importing the rung-1/2 scripts by file path (`scripts/` is not a package)   #
# --------------------------------------------------------------------------- #

def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# agent-IR plan loading                                                       #
# --------------------------------------------------------------------------- #

def _iter_plan_paths(spec: str) -> list[Path]:
    """`--plans` is a directory of `*.json` plan files, or a comma-separated
    list of individual file paths."""
    p = Path(spec)
    if p.is_dir():
        return sorted(p.glob("*.json"))
    return [Path(s) for s in spec.split(",") if s.strip()]


def load_plan(path: str | Path):
    from tgms.agent.ir import Plan

    return Plan.from_json(json.loads(Path(path).read_text()))


def _plan_ops(plan) -> list[str]:
    seen: list[str] = []
    for step in plan.steps:
        if step.op not in seen:
            seen.append(step.op)
    return seen


# --------------------------------------------------------------------------- #
# tokenizer (pluggable; default is a labelled approximation)                  #
# --------------------------------------------------------------------------- #

#: word-ish runs, or a single non-space/non-word character -- a cheap,
#: deterministic stand-in for a real BPE tokenizer. Never claims to be exact:
#: every count produced this way is labelled `approx` in the record.
_APPROX_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


def count_tokens(text: str, tokenizer: str = "auto") -> tuple[int, str, bool]:
    """(count, label, approx). `tokenizer`: "auto" (tiktoken if importable,
    else the whitespace+punctuation fallback), "tiktoken", or "approx"."""
    choice = tokenizer
    if choice == "auto":
        try:
            import tiktoken  # noqa: F401
            choice = "tiktoken"
        except ImportError:
            choice = "approx"
    if choice == "tiktoken":
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)), "tiktoken:cl100k_base", False
    if choice != "approx":
        raise ValueError(f"unknown --tokenizer {tokenizer!r}")
    return len(_APPROX_TOKEN_RE.findall(text)), "approx:whitespace_punct", True


# --------------------------------------------------------------------------- #
# rung 1 / 2 -- reuse by import, one arm per subprocess (inherited)           #
# --------------------------------------------------------------------------- #

def measure_rung1(store_path: str, plan) -> dict[str, Any]:
    """Leaf overhead, per operator the plan touches that P1 covers."""
    p1 = _load_module("bench_leaf_overhead")
    import tgms

    store = tgms.open(store_path, read_only=True)
    try:
        stats = store.stats()
        covered = {op for _id, op, _args in p1.cases(stats)}
    finally:
        store.close()
    ops = [op for op in _plan_ops(plan) if op in covered]
    rows = []
    for op in ops:
        on = p1.run_child(store_path, op, "on")
        off = p1.run_child(store_path, op, "off")
        ratio = None
        if on.get("outcome") == "OK" and off.get("outcome") == "OK" and off.get("p50_ms"):
            ratio = on["p50_ms"] / off["p50_ms"]
        rows.append({"op": op, "leaf": on, "direct": off, "leaf_over_direct": ratio})
    return {"rung": 1, "name": "leaf_overhead", "plan_id": plan.plan_id,
            "ops_in_plan": _plan_ops(plan), "ops_measured": ops,
            "protocol": "bench_leaf_overhead: warmups 5, reps 30/10 by "
                        "threshold, one arm per process (imported unchanged)",
            "rows": rows, "pid": os.getpid()}


def measure_rung2(store_path: str, plan) -> dict[str, Any]:
    """Compiled-vs-kernel, per operator the plan touches that has both a
    kernel and a compiled route (population: `entity_history`,
    `version_history` -- see `tests/test_e14_harnesses.py`)."""
    p2 = _load_module("bench_compiled_vs_kernel")
    import tgms
    from tgms.tgir.compiled import COMPILED

    store = tgms.open(store_path, read_only=True)
    try:
        stats = store.stats()
    finally:
        store.close()
    ops = [op for op in _plan_ops(plan) if op in COMPILED]
    rows = []
    for op in ops:
        k = p2.run_child(store_path, op, "kernel")
        c = p2.run_child(store_path, op, "compiled")
        ratio = None
        if k.get("outcome") == "OK" and c.get("outcome") == "OK" and k.get("p50_ms"):
            ratio = c["p50_ms"] / k["p50_ms"]
        rows.append({"op": op, "kernel": k, "compiled": c,
                     "compiled_over_kernel": ratio})
    del stats
    return {"rung": 2, "name": "compiled_vs_kernel", "plan_id": plan.plan_id,
            "ops_in_plan": _plan_ops(plan), "ops_measured": ops,
            "population": "entity_history, version_history only "
                          "(tgms.tgir.compiled.COMPILED)",
            "protocol": "bench_compiled_vs_kernel: warmups 5, budgeted reps, "
                        "one arm per process (imported unchanged)",
            "rows": rows, "pid": os.getpid()}


# --------------------------------------------------------------------------- #
# shared: run an agent-IR plan through the real executor, no LLM             #
# --------------------------------------------------------------------------- #

class CountingRouter:
    """Wraps a `ToolRouter` and counts calls -- the exact instrument rung 5's
    tool-call count is defined over (`tgms/tools/server.py::ToolRouter`),
    rather than an inference from trace step count (a step that is skipped
    before dispatch never reaches the router and must not be counted)."""

    def __init__(self, router: Any) -> None:
        self._router = router
        self.n_calls = 0

    def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.n_calls += 1
        return self._router.call(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)


def _run_plan(store_path: str, plan, results_dir: Path) -> tuple[Any, Any, Any]:
    """Store, CountingRouter, executed Trace -- one execution, no LLM, no
    planner: the plan is already on disk (M2.2's model: a plan *is* data)."""
    import tgms
    from tgms.agent.executor import Executor, ResultStore
    from tgms.tools.server import ToolRouter

    store = tgms.open(store_path, read_only=True)
    router = CountingRouter(ToolRouter(store.adapter, tt_source=store))
    executor = Executor(router, ResultStore(results_dir))
    trace = executor.run(plan)
    return store, router, trace


def _strip_wall_ms(obj: Any) -> Any:
    """Recursively drop `wall_ms` keys before computing a byte count. Every
    other field (ops, args digests, row counts, dependency scopes, ECQR
    descriptors) is a function of the store and the plan and is therefore
    reproducible byte-for-byte; `wall_ms` is real elapsed time and is the
    one field this trace carries that is not."""
    if isinstance(obj, dict):
        return {k: _strip_wall_ms(v) for k, v in obj.items() if k != "wall_ms"}
    if isinstance(obj, list):
        return [_strip_wall_ms(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# rung 3 -- trace bytes                                                       #
# --------------------------------------------------------------------------- #

def measure_rung3(store_path: str, plan, reps: int) -> dict[str, Any]:
    from tgms.core.model import canonical_json

    sizes = []
    with tempfile.TemporaryDirectory(prefix="tgms-ladder-results-") as tmp:
        for _ in range(max(1, reps)):
            store, _router, trace = _run_plan(store_path, plan, Path(tmp))
            try:
                blob = canonical_json(_strip_wall_ms(trace.to_json()))
            finally:
                store.close()
            sizes.append(len(blob.encode("utf-8")))
    sizes.sort()
    return {"rung": 3, "name": "trace_bytes", "plan_id": plan.plan_id,
            "reps": len(sizes), "bytes_all": sizes,
            "bytes_median": sizes[len(sizes) // 2],
            "bytes_min": sizes[0], "bytes_max": sizes[-1],
            "note": "canonical JSON of the executor's own Trace.to_json(), "
                    "with wall_ms fields stripped so the byte count is "
                    "reproducible across runs (see module docstring)",
            "pid": os.getpid()}


# --------------------------------------------------------------------------- #
# rung 4 -- verify ms                                                         #
# --------------------------------------------------------------------------- #

def measure_rung4(store_path: str, plan, reps: int) -> dict[str, Any]:
    from tgms.agent.executor import ResultStore
    from tgms.agent.reporter import mechanical_answer
    from tgms.agent.verifier import ClaimVerifier

    with tempfile.TemporaryDirectory(prefix="tgms-ladder-results-") as tmp:
        store, _router, trace = _run_plan(store_path, plan, Path(tmp))
        answer_object = mechanical_answer(plan, trace)
        # The executor's own `ResultStore(Path(tmp))` already wrote every
        # step's payload to this directory during `_run_plan`; a second
        # handle onto the same directory reads them back for the verifier.
        results = ResultStore(Path(tmp))
        verifier = ClaimVerifier(trace, results, store.adapter)

        times_ms = []
        for _ in range(WARMUPS):
            verifier.verify(answer_object)
        for _ in range(max(1, reps)):
            t0 = time.perf_counter()
            report = verifier.verify(answer_object)
            times_ms.append((time.perf_counter() - t0) * 1000.0)
        store.close()
    times_ms.sort()
    n = len(times_ms)
    return {"rung": 4, "name": "verify_ms", "plan_id": plan.plan_id,
            "reps": n, "warmups": WARMUPS, "ms_all": times_ms,
            "p50_ms": times_ms[n // 2], "p95_ms": times_ms[min(n - 1, int(n * 0.95))],
            "min_ms": times_ms[0], "max_ms": times_ms[-1],
            "n_claims": len(answer_object.get("claims", [])),
            "verify_metrics": report.get("metrics"),
            "note": "ClaimVerifier.verify over tgms.agent.reporter."
                    "mechanical_answer(plan, trace) -- deterministic, no LLM. "
                    "tgms/tgir/check.py::check_trace is a freshness check, "
                    "not claim verification; see module docstring.",
            "pid": os.getpid()}


# --------------------------------------------------------------------------- #
# rung 5 -- tokens and tool calls                                             #
# --------------------------------------------------------------------------- #

def measure_rung5(store_path: str, plan, tokenizer: str = "auto") -> dict[str, Any]:
    from tgms.core.model import canonical_json
    from tgms.agent.reporter import REPORTER_SYSTEM, mechanical_answer, trace_summary
    from tgms.agent.executor import ResultStore

    with tempfile.TemporaryDirectory(prefix="tgms-ladder-results-") as tmp:
        store, router, trace = _run_plan(store_path, plan, Path(tmp))
        try:
            results = ResultStore(Path(tmp))
            summary = trace_summary(plan, trace, results)
            message_text = (
                f"{REPORTER_SYSTEM}\n\nQUESTION: {plan.question}\n\n"
                f"TRACE:\n{summary}\n\nANSWER OBJECT:")
            answer_object = mechanical_answer(plan, trace)

            request_text = "\n".join(
                canonical_json(s.args) for s in plan.steps)
            response_text = "\n".join(
                canonical_json(results.get(rec["result_digest"]))
                for rec in trace.steps if rec.get("status") == "ok")
        finally:
            n_calls = router.n_calls
            store.close()

    msg_n, label, approx = count_tokens(message_text, tokenizer)
    req_n, _, _ = count_tokens(request_text, tokenizer)
    resp_n, _, _ = count_tokens(response_text, tokenizer)

    return {"rung": 5, "name": "tokens_tool_calls", "plan_id": plan.plan_id,
            "tool_calls": n_calls, "n_steps": len(plan.steps),
            "tokens": {"tokenizer": label, "approx": approx,
                       "reporter_message": msg_n, "tool_request": req_n,
                       "tool_response": resp_n, "total": msg_n + req_n + resp_n},
            "answer_claims": len(answer_object.get("claims", [])),
            "note": "reporter_message = the exact system+user message text "
                    "tgms.agent.reporter.Reporter.report would send an LLM "
                    "(built, never sent); tool_request/response = each "
                    "step's resolved args and stored result payload, the "
                    "round trip ToolRouter.call actually drives. "
                    "tool_calls counted at ToolRouter.call itself, not "
                    "inferred from trace step count.",
            "pid": os.getpid()}


# --------------------------------------------------------------------------- #
# child dispatch + orchestration (one condition per process)                  #
# --------------------------------------------------------------------------- #

RUNG_NAMES = {1: "leaf_overhead", 2: "compiled_vs_kernel", 3: "trace_bytes",
              4: "verify_ms", 5: "tokens_tool_calls"}


def _dispatch(rung: int, store_path: str, plan_path: str, reps: int,
             tokenizer: str) -> dict[str, Any]:
    plan = load_plan(plan_path)
    if rung == 1:
        return measure_rung1(store_path, plan)
    if rung == 2:
        return measure_rung2(store_path, plan)
    if rung == 3:
        return measure_rung3(store_path, plan, reps)
    if rung == 4:
        return measure_rung4(store_path, plan, reps)
    if rung == 5:
        return measure_rung5(store_path, plan, tokenizer)
    raise ValueError(f"unknown rung {rung}")


def run_condition(rung: int, plan_path: Path, store_path: str, reps: int,
                  seed: int, tokenizer: str) -> dict[str, Any]:
    """One (rung, plan) condition, in a fresh interpreter."""
    cmd = [sys.executable, "-u", str(THIS_FILE),
           "--single-rung", str(rung), "--single-plan", str(plan_path),
           "--store", store_path, "--reps", str(reps), "--seed", str(seed),
           "--tokenizer", tokenizer, "--out", os.devnull]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=CALL_CEILING_S, cwd=ROOT,
                              env={**os.environ, "PYTHONPATH": str(ROOT)})
    except subprocess.TimeoutExpired:
        return {"rung": rung, "name": RUNG_NAMES[rung], "plan_id": plan_path.stem,
                "outcome": "TIMEOUT"}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"rung": rung, "name": RUNG_NAMES[rung], "plan_id": plan_path.stem,
            "outcome": "ERRORED", "error": (done.stderr or done.stdout)[-500:]}


# --------------------------------------------------------------------------- #
# smoke fixture -- a tiny on-disk store + a two-step plan                     #
# --------------------------------------------------------------------------- #

def _build_smoke_store(root: Path) -> Path:
    """`n1`/`n2`/`n3`, all open from vt=0 -- enough for the built-in
    two-step smoke plan (`n1`, `n2`) and for `benchmarks/ladder-v1/plans`
    run through `--smoke --plans` (lane D4b): `n3` is there only because
    one of that plan set's steps (`snapshot_subgraph`, `seeds=["n1","n3"]`)
    needs a third identity to resolve; every other ladder-v1 plan is
    satisfied by `n1`/`n2` alone. A plan step whose *window* falls outside
    `[0, OPEN_END)` (the ladder-v1 plans mostly use real bitcoinotc-epoch
    windows, far above 0) still executes without error -- it just returns
    zero rows, which is a legitimate answer, not a failure."""
    import tgms
    from tgms.core.model import OPEN_END

    store_path = root / "smoke-store"
    store = tgms.open(str(store_path))
    try:
        store.assert_node("n1", "Person", {"name": "Alice"}, vt_s=0, vt_e=OPEN_END)
        store.assert_node("n2", "Person", {"name": "Bob"}, vt_s=0, vt_e=OPEN_END)
        store.assert_node("n3", "Person", {"name": "Carol"}, vt_s=0, vt_e=OPEN_END)
        store.assert_edge("n1", "n2", "KNOWS", {}, vt_s=0, vt_e=OPEN_END)
        store.assert_edge("n2", "n3", "KNOWS", {}, vt_s=0, vt_e=OPEN_END)
    finally:
        store.close()
    return store_path


def _write_smoke_plan(root: Path) -> Path:
    plan = {
        "plan_id": "smoke1",
        "question": "How many versions does n1 have?",
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "n1", "limit": 5},
             "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
             "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "count", "from": "s2.value"},
    }
    path = root / "smoke-plan.json"
    path.write_text(json.dumps(plan))
    return path


# --------------------------------------------------------------------------- #
# manifest assembly (benchmarks/schema/result_manifest.schema.json)          #
# --------------------------------------------------------------------------- #

def _sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True,
                               check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:  # noqa: BLE001
        return "unknown0000"


def _ram_gb() -> float:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / (1024 ** 3), 2)
    except (ValueError, OSError, AttributeError):
        return 1.0  # best-effort floor; schema requires > 0


def build_manifest(store_path: str, plan_paths: list[Path], rungs: list[int],
                   reps: int, seed: int, tokenizer: str,
                   rows: list[dict[str, Any]], out_path: Path,
                   smoke: bool) -> dict[str, Any]:
    import tgms
    from tgms.core.model import digest

    store = tgms.open(store_path, read_only=True)
    try:
        dataset_digest = store.digest()
    finally:
        store.close()

    try:
        rel_out = str(out_path.resolve().relative_to(ROOT))
    except ValueError:
        rel_out = str(out_path.resolve())

    return {
        "schema_version": "1.0.0",
        "git_commit": _sha(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"host": platform.node(), "platform": platform.platform(),
                   "cpus": os.cpu_count() or 1, "ram_gb": _ram_gb()},
        "config": {"harness": "scripts/bench_overhead_ladder.py",
                  "store": store_path, "plans": [str(p) for p in plan_paths],
                  "rungs": rungs, "tokenizer": tokenizer, "smoke": smoke},
        "seed": {"value": seed},
        "dataset": {"name": Path(store_path).name, "digest": dataset_digest,
                   "digest_kind": "store_digest"},
        "result_digest": digest(rows),
        "protocol": {"warmups": WARMUPS, "reps": reps,
                    "ceilings": {"call_ceiling_s": CALL_CEILING_S}},
        "record": rel_out,
        "rows": rows,
    }


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store")
    ap.add_argument("--plans")
    ap.add_argument("--rungs", default="1,2,3,4,5")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out")
    ap.add_argument("--tokenizer", default="auto")
    ap.add_argument("--smoke", action="store_true")
    # child-mode (internal): one condition
    ap.add_argument("--single-rung", type=int)
    ap.add_argument("--single-plan")
    args = ap.parse_args()

    if args.single_rung is not None:
        result = _dispatch(args.single_rung, args.store, args.single_plan,
                           args.reps, args.tokenizer)
        print(json.dumps(result, default=str))
        if args.out and args.out != os.devnull:
            Path(args.out).write_text(json.dumps(result, indent=1, default=str))
        return 0

    smoke_dir_ctx = None
    if args.smoke:
        smoke_dir_ctx = tempfile.TemporaryDirectory(prefix="tgms-ladder-smoke-")
        smoke_root = Path(smoke_dir_ctx.name)
        store_path = str(_build_smoke_store(smoke_root))
        # additive: `--smoke --plans DIR-or-list` runs a real plan set (e.g.
        # benchmarks/ladder-v1/plans) against the tiny smoke store instead of
        # the built-in two-step plan -- the laptop-side way to exercise a
        # frozen plan set end to end without touching a real store (see
        # tests/test_ladder_v1_plans.py). `--smoke` with no `--plans` is
        # unchanged.
        plan_paths = (_iter_plan_paths(args.plans) if args.plans
                      else [_write_smoke_plan(smoke_root)])
        reps = min(args.reps, 2)
        out_path = Path(args.out) if args.out else smoke_root / "smoke-record.json"
    else:
        if not args.store or not args.plans or not args.out:
            ap.error("--store, --plans and --out are required unless --smoke")
        store_path = args.store
        plan_paths = _iter_plan_paths(args.plans)
        if not plan_paths:
            ap.error(f"no plan files found for --plans {args.plans!r}")
        reps = args.reps
        out_path = Path(args.out)

    rungs = sorted({int(r) for r in args.rungs.split(",") if r.strip()})
    for r in rungs:
        if r not in RUNG_NAMES:
            ap.error(f"unknown rung {r}; expected 1-5")

    print(f"RUN_STARTED store={store_path} plans={len(plan_paths)} "
         f"rungs={rungs} reps={reps} seed={args.seed} smoke={args.smoke}",
         flush=True)
    t0 = time.time()
    rows: list[dict[str, Any]] = []
    for plan_path in plan_paths:
        for rung in rungs:
            row = run_condition(rung, plan_path, store_path, reps, args.seed,
                                args.tokenizer)
            rows.append(row)
            print(f"  rung {rung} ({RUNG_NAMES[rung]:20s}) plan={plan_path.stem:20s} "
                 f"pid={row.get('pid', '?')} outcome={row.get('outcome', 'OK')}",
                 flush=True)

    manifest = build_manifest(store_path, plan_paths, rungs, reps, args.seed,
                              args.tokenizer, rows, out_path, args.smoke)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=1, sort_keys=True, default=str))
    print(f"\nwall_s={time.time() - t0:.1f}")
    print(f"record: {out_path}")

    if smoke_dir_ctx is not None:
        smoke_dir_ctx.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
