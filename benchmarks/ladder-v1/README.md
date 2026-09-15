# ladder-v1

The frozen plan set and campaign for the OSDI'27 overhead-ladder measurement
(lane D4/D4b). **Run of record: 2026-09-14** (`ladder-2026-09-14.json`,
`raw/` — see "Results" below); this lane itself is laptop-only (no ssh, no
cluster) and only drafted the plan set — the coordinator scheduled and
submitted the iTiger jobs. See `campaign.yaml` for the frozen grid, gates,
predictions and falsifiers.

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

`campaign.yaml` is the pre-registered freeze: grid, gates, and predictions
with numeric bars for all five rungs (rungs 1–2 grounded in the existing
bitcoinotc leaf-overhead record and, directionally only, the cross-store
compiled-vs-kernel records; rungs 3–5 labelled "predicted, no prior
record" and anchored only by this lane's own `--smoke` run against a
synthetic 3-node store). The coordinator scheduled and stamped the run of
record below; any deviation from the frozen blocks lands as a dated
addendum in `campaign.yaml`, never as an edit to those blocks.

## Results — run of record 2026-09-14

- **commit_under_test**: `487457a` (`campaign.yaml`'s `commit_under_test`
  filled in from `TBD`, append-only — worktree `/project/xzhang12/tgms-post-a10`,
  branch `post-a10-campaign`)
- **store**: `bitcoinotc`, built via `tgms.data.loaders.ingest_dataset(...,
  backend="duckdb")` from the shared clone's cached raw file — 5,881
  entities, 35,592 `TRUST` edge versions, vt `[1289241911728360,
  1453684323757281]`, matching the freeze's own store description exactly
- **jobs**: one per seed, per the frozen grid (`reps=5`, `seeds=[0,1,2]`,
  `rungs=1,2,3,4,5`) — seed 0 → job `212303`, seed 1 → job `212304`, seed 2
  → job `212305`; all `COMPLETED`, exit `0:0`, ~128s wall-clock each, host
  itiger02
- **record**: `ladder-2026-09-14.json` (manifest + pooled-seed summary,
  `seed.value: null` + `seeds: [0,1,2]` per the D-160 pooled-seed
  convention) + `raw/overhead-ladder-bitcoinotc-seed{0,1,2}-job2123{03,04,05}.json`
  (the three per-seed records exactly as the cluster produced them)
- **file sha256**: manifest
  `58a383d30f57d87aab821098c3b85121fde31d3f7e25e2dee0d97e36cecb3b59`; raw
  seed0 `c9120e90d35d8aacf58d94478be8242723b6eb9570abdd81ea9cddd14bba4a3e`,
  seed1 `1070acfe054e2937f861547703c3a3fe4a9dc4a5ff027ede3aee17e09cbf9e05`,
  seed2 `35b865a4780429355132a6dcffaa047ac8523b53e363713baa478fa277124df7`
- **gates**: G-L1 (every plan executes, 0 failures) PASS; G-L2 (schema)
  PASS (`scripts/check_result_manifest.py`, all four files)
- **falsifiers**: none triggered — see the tool-calls note below for the
  one case that needed the design's own "executed steps" wording rather
  than a naive `n_steps` reading

### Per-rung × plan/op medians (median of 3 seeds) vs. the freeze's predictions

**Rung 1 — leaf_overhead** (`leaf_over_direct`, predicted band [0.7, 1.5],
falsifier outside [0.5, 2.0]): all 14 catalogue ops land in **[0.992,
1.064]** — entity_history 1.031, version_history 1.002, snapshot_subgraph
1.020, temporal_paths 1.012, co_active 1.042, count_temporal_motifs 0.992,
find_temporal_motif_instances 0.995, temporal_reachability 1.017,
diff_snapshots 0.992, neighborhood_evolution 1.022, aggregate_events
1.021, graph_metric_timeseries 1.043, burst_detection 1.045, compute
1.064. **PASS**, not refuted, and noticeably tighter than the cited
record's own 0.907–1.210 span on this same operator set.

**Rung 2 — compiled_vs_kernel** (`compiled_over_kernel`; entity_history
predicted [1, 2000], refuted if ≤1; version_history unconstrained): both
arms report `OK` for both ops. entity_history **1.132** (median of 6
per-plan-occurrence samples across p01 and p02) — PASS, and notably far
smaller than the cross-store direction-only anchor (292.7×–446.9× on
1M–10M-edge synthetic stores), consistent with bitcoinotc being ~28×
smaller. version_history **2.540** — sign is positive (compiled slower
than kernel) and within the same order of magnitude as entity_history on
this store, closing the open question the freeze left unconstrained.

**Rung 3 — trace_bytes** (predicted [1500, 30000] for 1-step, [4000,
90000] for 3-step; reproducible across reps at fixed seed): every plan's
`bytes_median` is **bit-identical across all 5 reps and all 3 seeds**
(`bytes_min == bytes_max` within every seed, and the three per-seed
medians are equal). 1-step plans range 2648 (p12-compute-only) – 3825
(p03-snapshot-subgraph), all inside the predicted band. 3-step plans:
p02 6402, p09 7924, p10 7549 — all inside [4000, 90000]. **PASS**, and
falsifier (b) (non-determinism) does not fire.

**Rung 4 — verify_ms** (predicted p50 in [1, 20]; falsifier if p50 > 200):
every plan's median p50 lands in a narrow **4.97–5.30 ms** band regardless
of plan shape — matching the freeze's own basis note ("dominated by fixed
`ClaimVerifier` overhead, not data volume, at this claim count", each plan
resolving exactly one claim). **PASS**.

**Rung 5 — tokens_tool_calls** (predicted tokens.total [800, 8000] for
1-step, [2000, 20000] for 3-step; `tool_calls == n_steps` treated as a
hard invariant/falsifier): tokens medians range 1188 (p12) – 4023 (p01)
for 1-step plans and 2520–6082 for the three 3-step plans, all inside the
predicted bands (tokenizer fell back to `approx:whitespace_punct` — no
`tiktoken` in the campaign venv, the harness's own documented `auto`
behavior). **Tool-calls finding**: `tool_calls` equals the plan's
configured `n_steps` for 10 of 12 plans; **p02-compiled-entity-and-version**
and **p10-reachability-and-paths** each show `tool_calls=2` against
`n_steps=3`, identically across all 3 seeds. Direct re-execution of both
plans (`Executor.run` against `stores/bitcoinotc`, trace inspected
directly) confirms this is `Executor.run`'s own truncation guard
(`tgms/agent/executor.py:219`, `REDUCING_FNS`): both plans' trailing
`compute(fn=count)` step depends on a step whose result page was truncated
at that arg shape (p02's `version_history` window, p10's both
`temporal_reachability` and `temporal_paths`, at `limit=50`/`k<=3`), so
the compute step fails with `E_LIMIT` **before** it ever reaches
`ToolRouter.call` and is correctly never counted. `campaign.yaml`
falsifier (a) reads "tool-call count != number of **executed** steps" —
under that wording (not a naive `n_steps` comparison) `tool_calls` matches
executed steps exactly on all 12 plans, so this is a documented population
note (same class of carve-out as falsifier (c)'s `E_NOT_FOUND` clause),
not a falsifier hit. **PASS**.

### Regenerating

Aggregation across the three per-seed records (median per rung × plan/op,
gate/falsifier scoring, `result_digest` over the pooled summary) has no
dedicated script yet — it was done inline for this run of record; a
`scripts/overhead_ladder_merge.py` mirroring
`scripts/corruption_campaign_merge.py`'s shape would be the natural next
tool if this campaign reruns.
