# iTiger calibration (B7 Stage 0) — 2026-09-16

Pre-registration: `docs/design/SCALE_BUILD_FORECAST_2026-09-15.md` Addendum 4
(coordinator, 2026-09-16 02:15 UTC clock) — "B7 moves to iTiger". This lane
(B7-S0) ran exactly Stage 0: the `synth-1m-native`/`synth-10m-native`
calibration builds plus the 10M scale-curve query set, `tgms check --full`,
and the 1M EXP-A4 recovery replay. **No §2 forecast row is scored from this
lane** — Stage 0 exists only to produce `k_build`/`k_recover`/the iTiger
per-operator anchor for Stage 1 (30M/100M), which this lane did not run.

## Host and controls

Cluster worktree `/project/xzhang12/tgms-b7`, fresh clone of
`https://github.com/zxf-work/tgms.git` at public main **`b159bd1`**
(`b159bd10aa53ad5de5f4e8ff7af1c87789d6afd0`), release build verified via
`NativeAdapter.build_info()`:
`{"profile": "release", "opt_level": "3", "engine_version": "0.8.0",
"debug_assertions": false, "manifest_format_version": 3}`.
`TGMS_SEGMENT_CACHE_BYTES=46500000000` pinned in every job (xzgpu's default:
half of 93 GB). Slurm: partition `bigTiger`, `--exclude=itiger04,itiger05`,
`--gres=gpu:rtx_5000:1` (policy requires a GRES; unused), `--cpus-per-task=8
--mem=64G --time=06:00:00`, one build per job. All four jobs landed on node
**itiger11**, CPU model **AMD EPYC 7513 32-Core Processor** (`lscpu` on the
compute node — note this differs from the login node, an EPYC 7343, and
from the itiger-cluster memory's older "64 cores/515 GB" generalization;
`machine.cpus`/`ram_gb` in the JSON records reflect the whole node as seen
by `os.cpu_count()`/`psutil`, not the 8-CPU cgroup slice). Records/logs
under `/home/xzhang12/b7-work-h/`; a live write test there passed at the
start of every job. `du -sm /project/xzhang12`: 22,207 MB before this lane,
28,698 MB after (well inside the ~200 GB quota headroom).

**Deviation from Addendum 4's literal recipe — commit stamping.** `git` is
not installed on iTiger compute nodes (confirmed by an `srun` probe on
itiger11: no `/usr/bin/git`, no module, not found anywhere in a depth-4
scan). Addendum 4's `RUN_STARTED commit=<HEAD>` stamp and its "stamp !=
worktree HEAD aborts the job" rule are preserved in spirit but not in the
literal mechanism: `git rev-parse HEAD` was run once on the **login** node
right after the clone/checkout (where git does work) and written to
`.HEAD_COMMIT` in the worktree; every job script reads that file instead of
shelling out to git, and still aborts if it doesn't match the pinned SHA.
`TGMS_COMMIT=<the same SHA>` is exported in every job so
`scripts/build_synth_store.py`'s own `_git_commit()` (which also shells out
to git and would otherwise silently fall back to 40 zeros) records the real
commit in `build-record.json`. `scripts/eval_harness.py::manifest()` has no
such env override and is not source we can patch here — its raw output
recorded `commit: "unknown"` and, because `bool("unknown")` is `True`, a
spurious `dirty: true`; both are corrected in `itiger-calib-10m.json`'s
`scale_curve.manifest` block, and the original raw file is kept
byte-identical at `itiger-scale-curve-10m-raw.json` (this file's own sha256
is stated below) so the patch is auditable, not silent.

## What ran

1. **Builds** — `scripts/build_synth_store.py --n-entities {1000000,10000000}
   --batch 250 --compact-every 1000000 --digest streaming --backend native`,
   1M submitted first (job 213044, verified head-of-log before proceeding),
   then 10M (job 213045). Both `BUILD_EXIT=0`.
2. **10M scale-curve + check** (job 213054) — `scripts/eval_harness.py
   --scale 10000000 --systems native --json itiger-scale-curve-10m-raw.json`
   (no ClickHouse on iTiger, so `--systems native` only — this is also
   exactly how `eval-10m-agg.json`'s own native row was produced, same
   script/protocol constants: `WARMUP=5`, `REPS_FAST=30` when the first rep
   is under 1000 ms else `REPS_SLOW=10`), then `tgms check
   stores/synth-10m-native --json` (`check` is `store verify --full`'s
   alias).
3. **1M recovery replay** (job 213046) — `python -m tgms.cli replay
   stores/synth-1m-native/eventlog.jsonl --store
   stores/synth-1m-native-replayed --backend native --compact-every 500`,
   then `store.digest_streaming()` compared between the original and
   replayed stores.

## Build results

| | 1M | 10M |
|---|---:|---:|
| wall | 78.339 s | 865.592 s |
| total ops | 1,000,262 | 10,000,262 |
| compactions | 2 | 11 |
| peak RSS (harness `maxrss_kb`, i.e. VmHWM) | 2,371,648 KB (2.32 GB) | 19,897,476 KB (19.43 GB) |
| final manifest bytes | 159,394 B | 164,859 B |
| final segment bytes | 49,673,103 B | 504,374,563 B |
| store digest (streaming) | `ebcf12aa17c57ff3a2a8b7f86fca5bab6fa19ec5499155ad6d24021874e6ca65` | `41e0699853e82b0f11a0eddbebffb2b3446d30f185fc1e76d84071caf2698483` |

VmHWM was also sampled independently every 30 s from `/proc/<pid>/status`
(`itiger-rss-{1m,10m}.jsonl`), separate from `build_synth_store.py`'s own
per-tick `maxrss` figure, per the calibration brief's "sampled every 30s AND
the harness's own figure" — both series are folded into the JSON records
under `build_info.rss_series_30s` (external sampler) and the per-decile
`maxrss_mb` inside `build_info.ops_per_s_by_decile`'s source ticks (harness).
`finalisation_phases` (compact/gc/stats/digest, each individually
timed+RSS-sampled per Addendum 2/3) are recorded in full in both JSON files.

### 10M per-decile bulk ops/s (from the 500k-op progress ticks)

| decile | ops/s |
|---:|---:|
| 1 | 15,772.9 |
| 2 | 12,594.5 |
| 3 | 24,752.5 |
| 4 | 24,509.8 |
| 5 | 24,875.6 |
| 6 | 24,875.6 |
| 7 | 24,271.8 |
| 8 | 19,841.3 |
| 9 | 24,752.5 |
| 10 | 8,156.6 |

**median of the 10 deciles: 24,390.8 ops/s.** The whole 10M build is
effectively one continuous bulk phase (corrections/retractions contribute
only 262 of the 10,000,262 total ops — negligible), so all ten deciles are
"bulk" in the forecast's sense. The clear odd/even alternation (roughly
24.3–24.9k vs 8–20k) tracks `--compact-every 1000000`: every second 500k-op
tick window straddles a `compact()+gc()` pause that the *next* tick's
cumulative-average rate absorbs, which the xzgpu bulk basis ("~5,300 ops/s
steady, no comparable slowdown", `SCALE_BUILD_FORECAST_2026-09-15.md` §2a)
did not exhibit at whatever cadence produced that figure — flagged here as
an iTiger/cadence-interaction finding, not a bug in this lane's
measurement. The 10 raw values are not all independent draws from one
steady-state distribution; the median is reported as the single number the
calibration brief asks for, with the full table given for transparency.

## Scale-curve (10M, native only) — per-operator p50

| operator | p50 (ms) | rows | note |
|---|---:|---:|---|
| hist.single | 0.4 | 2 | |
| hist.asof | 0.4 | 2 | |
| snap.hop2 | 486.8 | 151 | |
| diff.global | 2114.2 | 749,999 | |
| reach.window | 493.5 | 99,993 | **executed — see deviation below** |
| paths.k | 10.2 | 42 | |
| series.count | 98.4 | 100 | |
| burst.zscore | 96.4 | 1 | |
| nbr.evolution | 44.4 | 18 | |
| coactive.narrow | 233.5 | 489 | |
| resolve.substr | 54.2 | 11,111 | |
| agg.rel_bucket | 119.6 | 196 | |
| motif.filtered | 78.5 | 303 | |

All 13 queries "agree" (trivially — one system). `tgms check --full` on the
10M store: **healthy, 0 findings, 0 problems, exit 0, wall 27.277 s.**

**Deviation — `reach.window` executed instead of being refused.**
`benchmarks/results-v1/eval-10m-agg.json` (xzgpu, commit `d12b30f`) shows
`native` and `duckdb` both refusing this exact query
(`src="n1"`, same window formula) with `CostError: estimated cost for
temporal_reachability exceeds ceilings`; only `clickhouse` executed it
(rows=99993, p50 4057.4 ms). On iTiger, `native` **executed** it: ok=True,
rows=99993 (identical row count to xzgpu's clickhouse answer), p50 493.5 ms,
no `CostError`. `tgms/temporal/guardrails.py`'s admission ceiling
(`DEFAULT_CEILINGS["time_est_ms"]=10_000`,
`TIME_COEFF_MS_PER_M["temporal_reachability"]=170.0`) is a static
per-operator ms-per-million-estimated-units coefficient over
dataset-shape estimates — it is not supposed to depend on which machine
runs the query. This lane did not root-cause the discrepancy (out of Stage
0's scope); flagged for the coordinator as either a synth-generator drift
since commit `d12b30f` (changing the estimated reachability fan-out) or an
admission-path difference between environments. The falsifier language in
`SCALE_BUILD_FORECAST_2026-09-15.md` §2h ("falsified if it *executes*
instead of being refused") is written for the 30M/100M Stage-1 bars, which
this lane does not score — but the same behavior recurring at Stage 1 would
falsify that row, so it is surfaced now rather than only at Stage 1.

## Recovery (EXP-A4, 1M)

Replay wall **75.860 s**; `digest_streaming()` of the original store
(`ebcf12aa...698483`) equals the replayed store's
(`ebcf12aa...698483`) — **`digest_equal: true`**.

## The two calibration numbers Stage 1 needs

- **k_build** = iTiger 10M steady-decile-median bulk ops/s ÷ 5,300 =
  24,390.8 / 5,300 = **4.602**.
- **k_recover** = iTiger 1M replay wall ÷ 91.01 s = 75.860 / 91.01 =
  **0.834**.

**k_build = 4.602 is outside Addendum 4's [0.5, 3] band.** Per Addendum 4:
*"If k_build < 0.5 or > 3 the coordinator re-examines the host controls
(cache pin, CPU pinning, /project I/O) before Stage 1 rather than accepting
the factor silently; the re-examination is written as an addendum, and
Stage 1 does not launch until it is."* This lane does not launch Stage 1 and
flags this explicitly: **iTiger's bulk build path is measured ~4.6x faster
than the xzgpu bulk basis**, plausibly consistent with itiger11's newer,
higher core-count EPYC 7513 vs. xzgpu's older 40-core host, but the
compaction-cadence interaction seen in the decile table (above) means this
factor is sensitive to exactly which deciles are averaged (the 8-decile
trimmed median gives 4.647, not materially different) — the coordinator
should decide whether `k_build` should instead be computed from a
compaction-free sub-window, or whether the xzgpu bulk basis itself needs
re-stating with its own compaction cadence made explicit, before scaling
the §2a wall bands into Stage 1.

## Files and provenance

| file | sha256 |
|---|---|
| `itiger-calib-1m.json` | `9d8f18d50295a4915235cef88d5bbea88b6be73ed70133aa4fb43d547f89630e` |
| `itiger-calib-10m.json` | `bb5c0522bbdf4a4b28db691d570f500f56b4b21279eb03b75adfef8560fb79ac` |
| `itiger-scale-curve-10m-raw.json` | `c7142f6b08020039c81dafaaa9f1883fe7f747bf619ed23ccc4fb0bc753088a1` |
| `itiger-rss-1m.jsonl` | `8ba65e0c982dc3e26181e7b1fca3de2e95094d122f5dea3a0df911d02668ddb2` |
| `itiger-rss-10m.jsonl` | `fea555f123fe1bea1a72342b095aa08303d02e1c7dd800cce8b958f6dd62efa0` |
| `itiger-build-1m.stdout.log` | `75173a435b75a67e9e47bdff94b30fd9c86b4c075ae3fe215528176f07679127` |
| `itiger-build-10m.stdout.log` | `36ccb0a522e48c1765a2ee976c7a0b1ad739f5b25c2ec5d1dde68a155c13e3f8` |

`itiger-calib-{1m,10m}.json` both validate against
`benchmarks/schema/result_manifest.schema.json`
(`scripts/check_result_manifest.py`, run with the cluster venv's
`jsonschema==4.26.0` since this repo's laptop worktree carries no Python
env — see `docs/design/` memory "Experiments remote only"). Every raw
source file above was sha256-verified byte-identical between the cluster
path it was produced at and the copy transferred to this worktree before
this README or the calibration JSON was written. `stores/synth-{1m,10m}-
native/`, `stores/synth-1m-native-replayed/`, and the cluster worktree
`/project/xzhang12/tgms-b7` itself are left in place on iTiger (not deleted,
per policy) for Stage 1 to reuse or for spot-checking.

Slurm job IDs: 213044 (1M build), 213045 (10M build), 213046 (1M recovery
replay), 213054 (10M scale-curve + check --full). All four `RUN_STARTED`/
`RUN_FINISHED` lines and `BUILD_INFO`/cache-pin stamps are preserved
verbatim in `itiger-build-{1m,10m}.stdout.log` and in the two calibration
JSON files' `build_info`/`config.host_controls` blocks.
