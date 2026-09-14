# The overhead ladder

`scripts/bench_overhead_ladder.py` measures five rungs of overhead for the
TGIR/agent path against raw kernel execution, on one store, in one run, and
writes a single result manifest conforming to
`benchmarks/schema/result_manifest.schema.json`.

**Provenance note.** `benchmarks/tgir-v1/merged.yaml` (the file this
harness's brief pointed at for the ladder's frozen definition) and
`docs/design/OSDI27_PAPER_SKELETON_2026-09-15.md` do not exist in this tree
(checked across every branch in the worktree set at the time this was
written). The rung names, boundaries and the `--plans` file format below
are this script's own design, made against the two rungs that already
existed (`bench_leaf_overhead.py`, `bench_compiled_vs_kernel.py`) and the
real agent-path code (`tgms/agent/*`, `tgms/tools/server.py`). If
`merged.yaml` lands later with a different ladder definition, reconcile
against it then.

## The five rungs

| # | name | what it measures | how |
|---|------|-------------------|-----|
| 1 | `leaf_overhead` | cost of wrapping an operator as an opaque TGIR leaf vs. calling it directly | imports `scripts/bench_leaf_overhead.py::cases/run_child` unchanged |
| 2 | `compiled_vs_kernel` | compositional TGIR core route vs. the hand-written kernel | imports `scripts/bench_compiled_vs_kernel.py::cases/run_child` unchanged; population is exactly `entity_history` and `version_history` (`tgms.tgir.compiled.COMPILED`) |
| 3 | `trace_bytes` | size of the execution trace the agent executor actually produces | `canonical_json` of `tgms.agent.executor.Trace.to_json()`, `wall_ms` fields stripped |
| 4 | `verify_ms` | wall time of evidence/claim verification over that trace | `tgms.agent.verifier.ClaimVerifier.verify()` on `tgms.agent.reporter.mechanical_answer(plan, trace)` |
| 5 | `tokens_tool_calls` | the agent path's message and tool-call footprint | tool calls counted at `ToolRouter.call`; tokens counted over the reporter message text and the tool request/response round trip, with **no LLM call** |

Rungs 1-2 operate per **operator**: for a given plan, its distinct step ops
are intersected with each script's own covered set (all fifteen registered
operators for rung 1; `{entity_history, version_history}` for rung 2). A
plan touching neither compiled operator contributes zero rung-2 rows for
that plan — that is rung 2's documented population, not a failure.

Rungs 3-5 operate per **plan**: the plan is loaded as `tgms.agent.ir.Plan`
(`Plan.from_json`) and run once through `tgms.agent.executor.Executor.run`
directly — **no `tgms.agent.agent.Agent`, no `Planner`, no LLM call
anywhere in this harness**. The trace, the deterministic `mechanical_answer`
AnswerObject, and the reporter's `trace_summary` text are all produced by
code that already exists for the LLM-free fallback path
(`Reporter`'s own fallback and the task-suite gold-answer generator use the
same `mechanical_answer`).

### What "verify" means here (an ambiguity that had to be resolved)

`tgms/tgir/check.py::check`/`check_trace` is a **freshness** check — "could
a correction logged since have changed this result?" — not claim
verification, even though its docstring uses the word "verify". The actual
evidence/claim verification step the agent path runs
(`tgms.eval.harness.run_task_ours`) is
`tgms.agent.verifier.ClaimVerifier.verify`, which checks each claim in an
AnswerObject against the trace's own stored evidence payloads. Rung 4 times
that call. `tgms/tgir/*` is imported nowhere in rung 4 and stays frozen.

### Why trace bytes strip `wall_ms`

`Trace.to_json()` and every step record carry a `wall_ms` field — real
elapsed time, not a function of the store or the plan. Left in, two
otherwise-identical runs could serialize to a different byte count purely
because a timing crossed a digit boundary (`"0.5"` vs `"12.3"`). Rung 3
therefore computes its byte count over the trace with every `wall_ms` key
recursively removed; everything else (ops, resolved-arg digests, row
counts, dependency scopes, ECQR descriptors) is a pure function of the
store and the plan and is reproducible byte-for-byte across runs with the
same seed, store and plan.

### `--plans`: agent-IR plan files, not TGIR node plans

`benchmarks/tgir-v1/plans/*.json` are **TGIR node plans**
(`tgms.tgir.plan.Plan`, loaded by `tgms.tgir.loader`) — a different format,
used for equivalence testing against baselines. This harness's `--plans`
takes **agent-IR plan files** instead: the same JSON shape
`tgms.agent.executor.Executor.run` already consumes and
`tests/test_agent.py` already hand-constructs —

```json
{
  "plan_id": "q1",
  "question": "...",
  "steps": [
    {"id": "s1", "op": "entity_history", "args": {"uid": "n1", "limit": 5}, "depends_on": []},
    {"id": "s2", "op": "compute", "args": {"fn": "count", "input": {"$ref": "s1.rows"}}, "depends_on": ["s1"]}
  ],
  "answer_spec": {"kind": "count", "from": "s2.value"}
}
```

`--plans` accepts either a directory (every `*.json` file in it, sorted) or
a comma-separated list of individual file paths.

### What "approx" means

Rung 5's token counts are pluggable via `--tokenizer`:

- `auto` (default): uses `tiktoken` (`cl100k_base`) if importable, else
  falls back to a deterministic whitespace+punctuation splitter.
- `tiktoken`: forces the `tiktoken` encoder; fails if it is not installed.
- `approx`: forces the whitespace+punctuation fallback.

Every token count in the record carries `tokenizer` (e.g.
`"tiktoken:cl100k_base"` or `"approx:whitespace_punct"`) and `approx`
(`true` for the fallback, `false` for `tiktoken`) so a reader never has to
guess which one produced a number. The fallback is not a token-count
estimate calibrated against any real BPE vocabulary — it is a stand-in that
exists so this harness runs with zero extra dependencies; treat `approx`
numbers as size-of-payload proxies, not as a claim about what an LLM
provider would actually bill.

## Command line

```
PYTHONPATH=$PWD python scripts/bench_overhead_ladder.py \
    --store PATH --plans DIR-or-list --rungs 1,2,3,4,5 \
    --reps N --seed S --out record.json [--tokenizer NAME] [--smoke]
```

`--smoke` builds a tiny on-disk store and a two-step plan under a temp
directory and runs all five rungs against it in seconds — use it to check
the harness itself, never to produce a reported number.

### The iTiger run (`scripts/overhead_ladder.slurm`)

```
TGMS_STORE=bitcoinotc TGMS_PLANS=benchmarks/ladder-v1/plans sbatch scripts/overhead_ladder.slurm
```

Single task, one node, partition `bigTiger`, `--time=02:00:00`. Follows
`scripts/storm_campaign.slurm`'s node-local-`TMPDIR` and
`git rev-parse`-shim idioms (iTiger compute nodes carry no `git` binary).
The python invocation is `exec`'d as the script's last line specifically so
`SIGTERM` (from `scancel` or the time limit) reaches the interpreter
directly rather than a wrapping shell that would otherwise need its own
signal-forwarding trap; the tradeoff, spelled out in the script's own
comments, is that a `trap ... EXIT` cannot run after a successful `exec`,
so `$TMPDIR`/the git-shim directory are left for the node rather than
removed by this script.

**This script has not been submitted.** Lane D4 implements and unit-tests
the harness; the coordinator schedules measurement runs.

## Tests

`tests/test_overhead_ladder.py` — tmp_path-only, no store under `stores/`
is ever copied or opened. Covers: schema conformance of a `--smoke` record;
all five rungs present; rung 3 bytes positive and reproducible across two
independent runs with the same seed/store/plan; rung 5's tool-call count
exact on a known two-call plan (and *not* inflated by a step that never
reached the router because its dependency failed); each `(rung, plan)`
condition the orchestrator drives running in its own process (distinct
pid); `--plans` accepting both a directory and a comma-separated list.
