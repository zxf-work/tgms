# ladder-v1

The frozen plan set and campaign for the OSDI'27 overhead-ladder measurement
(lane D4/D4b). **Not yet run** — this lane is laptop-only (no ssh, no
cluster); the coordinator schedules and submits the iTiger job. See
`campaign.yaml` for the frozen grid, gates, predictions and falsifiers.

## What the ladder measures

`scripts/bench_overhead_ladder.py` (docs: `docs/eval/overhead_ladder.md`)
answers, per query plan, "what does going through TGIR / the agent path cost
on top of the bare kernel?" across five rungs:

1. **leaf_overhead** — wrapping an operator as an opaque TGIR leaf vs.
   calling it directly (imports `scripts/bench_leaf_overhead.py`).
2. **compiled_vs_kernel** — the compositional TGIR core route vs. the
   hand-written kernel, on the two operators that have both
   (`entity_history`, `version_history`; `tgms.tgir.compiled.COMPILED`).
3. **trace_bytes** — size of the execution trace the agent executor
   produces (canonical JSON, `wall_ms` stripped for reproducibility).
4. **verify_ms** — wall time of `tgms.agent.verifier.ClaimVerifier.verify`
   over that trace's mechanical answer.
5. **tokens_tool_calls** — the agent path's message/tool-call footprint,
   with no LLM call anywhere.

Rungs 1–2 operate **per operator** (the plan's distinct ops, intersected
with each rung's own covered set). Rungs 3–5 operate **per plan**: the
agent-IR plan (`tgms.agent.ir.Plan`) is loaded from `benchmarks/ladder-v1/plans`
and run once through `tgms.agent.executor.Executor.run` directly — no
`Agent`, no `Planner`, no LLM call.

## The plan set: operator × plan coverage

`benchmarks/ladder-v1/plans/*.json` are twelve agent-IR plans (1–3 steps
each) written against `stores/bitcoinotc` (5,881 entities, 35,592 `TRUST`
edge versions, vt in `[1289241911728360, 1453684323757281]`, epoch
microseconds; node ids `n<raw SNAP id>`). Together they cover **all 14**
operators in the campaign's operator catalogue — `tgms.tgir.leaves.LEAF_SCOPES`
(13 entries) union `compute` — the same 14-of-15 population
`tgms.eval.storm.py`'s own `TEMPLATES` tuple documents (`resolve_entities`
is excluded by ruling, D-161/§13.8.1: a constructed-counterexample operator,
never a workload sample). `tests/test_ladder_v1_plans.py` computes this
catalogue from the code (`LEAF_SCOPES` + `{"compute"}`), not a hard-coded
list, and asserts full coverage.

| plan | steps | operators |
|---|---|---|
| `p01-entity-history` | 1 | `entity_history` |
| `p02-compiled-entity-and-version` | 3 | `entity_history`, `version_history`, `compute` |
| `p03-snapshot-subgraph` | 1 | `snapshot_subgraph` |
| `p04-diff-snapshots` | 1 | `diff_snapshots` |
| `p05-neighborhood-evolution` | 1 | `neighborhood_evolution` |
| `p06-aggregate-events` | 1 | `aggregate_events` |
| `p07-graph-metric-timeseries` | 1 | `graph_metric_timeseries` |
| `p08-burst-detection` | 1 | `burst_detection` |
| `p09-motifs` | 3 | `count_temporal_motifs`, `find_temporal_motif_instances`, `compute` |
| `p10-reachability-and-paths` | 3 | `temporal_reachability`, `temporal_paths`, `compute` |
| `p11-co-active` | 1 | `co_active` |
| `p12-compute-only` | 1 | `compute` (the empty-scope control) |

Coverage: **14/14** operators, each in at least one plan. `p02` is the one
plan built specifically to exercise both `tgms.tgir.compiled.COMPILED`
operators (`entity_history` and `version_history`) together, so rung 2 has
a non-empty population from this campaign's own plan set. `p02`, `p09` and
`p10` are the multi-step plans (3 steps each, mixing operators via a
trailing `compute` step over the preceding op's `rows`); the other nine are
single-step. Arguments mirror `scripts/bench_leaf_overhead.py::cases()`'s
own proven-safe style for this store (full-extent windows, `n1`/`n2`/`n3`
uids, `stride = span // 64`-scale bucket widths) — every op in this set is
already known to execute `OK` on `stores/bitcoinotc` at those argument
shapes (`benchmarks/results-v1/e14-p1-leaf-overhead-bitcoinotc.json`).

## Running it

On iTiger (`scripts/overhead_ladder.slurm`; single task, one node,
`bigTiger` partition, `exec`'d python so `SIGTERM` reaches the interpreter
directly — see that script's own header):

```
TGMS_STORE=bitcoinotc TGMS_PLANS=benchmarks/ladder-v1/plans sbatch scripts/overhead_ladder.slurm
```

(`TGMS_STORE` names a store directory under `$CHECKOUT/stores/`; the slurm
script resolves it to `stores/bitcoinotc` and passes `--rungs 1,2,3,4,5
--reps 10 --seed 0` by default — `campaign.yaml`'s frozen grid instead runs
`reps=5` across `seeds=[0,1,2]`, one job per seed, per the grid block.)

Locally (laptop; **smoke only, never a reported number** — no store under
`stores/` is ever opened, only a synthetic tmp_path store):

```
PYTHONPATH=$PWD python scripts/bench_overhead_ladder.py \
    --smoke --plans benchmarks/ladder-v1/plans --out /tmp/ladder-smoke-record.json
```

**This campaign has not been run.** `campaign.yaml` is the pre-registered
freeze: grid, gates, and predictions with numeric bars for all five rungs
(rungs 1–2 grounded in the existing bitcoinotc leaf-overhead record and,
directionally only, the cross-store compiled-vs-kernel records; rungs 3–5
labelled "predicted, no prior record" and anchored only by this lane's own
`--smoke` run against a synthetic 3-node store). The coordinator schedules
and stamps the run of record; any deviation lands as a dated addendum in
`campaign.yaml`, never as an edit to the frozen blocks.
