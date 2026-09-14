# diskfull-v1 — EXP-A5, the ≥2,000-trial disk-full / short-write injection sweep

`eval-diskfull-campaign-2026-09-14.json` is the committed record for
**EXP-A5** (Lane A, OSDI plan task A9, building on task A5's design in
`docs/eval_durability.md`): a Slurm array running `scripts/
eval_diskfull.py` at scale — 20 tasks × 100 seeded trials each, **2,000
trials total**, every trial independently reproducible from its
`(seed, trial)` pair. Per trial: a seeded workload runs in a short-lived
child process armed with a Python call-boundary injector
(`TGMS_DISKFULL_AT`/`TGMS_SHORT_WRITE_AT`, mode and injection point both
seed-derived), which must terminate cleanly (never hang) with either exit
0 (the injection point was never reached) or a clean typed exception; the
parent then reopens *without* the injector and checks Q1 (every
acknowledged write survives), Q2 (deterministic recovery), Q3
(single-generation visibility), Q4 (orphan reclamation after
`compact()`+`gc()`).

- **commit**: `199f3f5` (includes the Q1 harness fix below — see "A false
  positive found, root-caused, and fixed by this task")
- **hosts**: itiger01 (11 tasks), itiger04 (9 tasks) — iTiger cluster,
  partition `bigTiger`
- **array job**: 211234, `--array=0-19%6`, all 20 tasks `COMPLETED`, exit
  `0:0` — submitted 2026-09-14 00:14:19, last task finished 00:16:58,
  **2m39s wall-clock** for the whole campaign (summed per-trial `wall_s`
  across all 2,000 trials: 781.32s; the wall-clock figure is smaller
  because up to 6 tasks ran concurrently)
- **result**: **2,000/2,000 trials clean** — 0 problems, **0 hangs** (the
  disallowed outcome), Q1–Q4 all pass at every injection site
- **manifest**: conforms to `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — passes)
- **result_digest** (sha256 over the sorted trial records):
  `1343d29136d726a6b35a6d53b2db2d284aa4dd2490b07ec8705320bee5fff8d9`
- **file sha256**:
  `358fcba275d3292aa3d97735f8f2902cdc301d9c70c04c5006eccce795563694`

## Per injection site — Q1–Q4 pass counts

| site | trials | fired | Q1 pass | Q2 pass | Q3 pass | Q4 pass | hangs |
|---|---:|---:|---:|---:|---:|---:|---:|
| `enospc@os.fsync` (mode=diskfull, fired) | 798 | 798 | 798 | 798 | 798 | 798 | 0 |
| mode=diskfull, not fired (exit 0, uninteresting) | 226 | 0 | 226 | 226 | 226 | 226 | 0 |
| mode=short_write, never fires (dead code against the current codebase — see below) | 976 | 0 | 976 | 976 | 976 | 976 | 0 |
| **total** | **2000** | **798** | **2000** | **2000** | **2000** | **2000** | **0** |

`short_write` firing 0/976 times is not a gap in this campaign — it is the
documented, confirmed-dead-code shape from task A5's own design
(`scripts/eval_diskfull.py`'s module docstring): every write in `tgms/`
goes through a buffered Python file object's `.write()`, never the raw
`os.write` syscall wrapper `TGMS_SHORT_WRITE_AT` patches, so the injector
never gets a chance to fire against this harness's workload. The injector
mechanism itself is proven correct independently
(`tests/test_eval_diskfull.py`'s direct `os.write` calls after installing
it); this campaign only confirms that direct-syscall injection remains
unreachable through the product's current write path, at 2,000-trial
scale rather than one unit test.

## A false positive found, root-caused, and fixed by this task

**The first submission of this array (job 211155, commit `fc2fb98`, same
seeds, same scale) reported 152/2,000 trials (7.6%) failing
`q1_acked_survive` — reported here verbatim, per this task's own rule,
rather than silently re-run away:**

```
trial    2 mode=diskfull    at= 58 fired=True  -> PROBLEM acked node r20=137839 but believed=[]
trial    8 mode=diskfull    at= 32 fired=True  -> PROBLEM acked node r13=702279 but believed=[]
trial   22 mode=diskfull    at= 18 fired=True  -> PROBLEM acked edge ('r9s','r9d','R','')=355173 but believed=[539470]
...
```

Every one of the 152 was verified (not assumed from one sample) to share
one exact shape: `mode=diskfull`, `inject_at == calls_seen`, exactly one
`problems` entry, and Q2/Q3/Q4 all clean — 0 hangs throughout. Direct,
deterministic reproduction (`run_trial(2, seed=1000)`, the first case
above) traced the mechanism to the raw event log: the crash-adjacent
operation (here, a `retract` of node `r20`, superseding its last acked
correction) had already been `write()`+`flush()`ed to the real filesystem
before its own `fsync` call raised the injected `ENOSPC` — so it was
correctly never acknowledged (the call raised), but its bytes were already
durable in the ordinary sense, and a subsequent reopen's recovery replayed
it. This is the *same* accepted "acked value superseded by the crash
batch" direction EXP-A1 already documents (Q1: returned-success implies
present, not the converse) — `eval_durability.py`'s own harness special-
cases exactly this for its one fixed crash-write key, `a0`. This
harness's injection point is instead uniformly random over the *entire*
workload, so the crash-adjacent write can land on any already-acked
entity, including a `correct`/`retract` that overwrites or erases that
entity's last acked value on replay — a case the original Q1 check did
not tolerate.

**Fixed same day** in `scripts/eval_diskfull.py` (`_last_batch_targets`):
the (kind, key) touched by the event log's actual last batch is identified
directly from the log and exempted from the strict acked-value comparison
— generalizing `eval_durability.py`'s fixed-key carve-out to whichever key
the random injection point actually hits — with the exemption itself
recorded in each trial's `q1_exempted_last_batch` field (152/2,000 trials
in this corrected record) rather than silently dropped. Every other acked
key is still checked at full strength. Regression-tested
(`tests/test_eval_diskfull.py::
test_crash_adjacent_unacked_correction_does_not_false_positive_q1`,
pinning the exact discovered case) before this array was resubmitted.
**This record (`eval-diskfull-campaign-2026-09-14.json`, job 211234) is
the corrected re-run** — same base seed, same scale, same per-trial
injection points (`fired`/`calls_seen` match the first attempt exactly,
confirmed field-by-field) — now correctly classifying all 2,000 trials
clean. The first attempt's raw per-task files are not committed (they
reflect a harness bug already fully explained and fixed here, not
durability evidence in their own right); this section is the complete
record of what was found.

**Also found and fixed before either array first ran**: the same
`${TGMS_COMMIT:?...}` bash-parse bug documented in
`benchmarks/corruption-v1/README.md` affected `scripts/
diskfull_campaign.slurm` identically (both scripts share the same guard
line) — job 211094 (this harness's first submission attempt) failed
instantly, exit 2, every task, before fixing it.

## Protocol

Each of 20 Slurm array tasks ran `scripts/eval_diskfull.py --trials 100
--seed <1000 + task_id>` against a checkout at commit `199f3f5`
(`git worktree add` off `/project/xzhang12/tgms`, engine rebuilt from
source on itiger07 via `srun` + `uv sync --extra agent
--reinstall-package tgms`; confirmed `tgms._engine.MANIFEST_FORMAT_VERSION
== 2` and the A8 CURRENT-less-open refusal via a direct 5-line smoke on
the compute node before submitting).

Reuses the same node-local `TMPDIR` and `git`-shim infrastructure fixes as
`scripts/crash_campaign.slurm`/`scripts/corruption_campaign.slurm`
(`benchmarks/crash-v1/README.md`); `TGMS_DISKFULL_AT`/`TGMS_SHORT_WRITE_AT`
are armed only inside each trial's own short-lived `--child` subprocess by
`scripts/eval_diskfull.py` itself, never by the Slurm script.

## Data handling

The 20 per-task JSON records (and per-task stdout/stderr logs, and
per-task host/uname sidecars) were staged at
`/project/xzhang12/diskfull-v1-work/{records,logs,node_meta}/`,
sha256-summed there, scp'd down, and **the sha256sums were re-checked
locally before the server-side copies were deleted** (the first attempt's
staging directory was likewise deleted after its logs were read and its
finding transcribed above). The merge step
(`scripts/corruption_campaign_merge.py --kind diskfull`) never hand-types
a number: every count in this README and in `docs/eval_durability.md`'s
EXP-A5 section is read back out of the merged record.

## Regenerating

```sh
# 1. On iTiger: fetch + fast-forward (or `git worktree add`) a checkout to
#    the target commit, rebuild the engine on a compute node
#    (srun -p bigTiger -w itiger07 -- uv sync --extra agent
#    --reinstall-package tgms), smoke-test with --trials 3 on the node.
# 2. Submit (adjust TGMS_REPO/TGMS_STAGE/TGMS_BASE_SEED/TGMS_COMMIT via
#    --export if regenerating against a different checkout/seed):
sbatch --export=ALL,TGMS_REPO=<checkout>,TGMS_STAGE=<stage>,\
TGMS_BASE_SEED=1000,TGMS_COMMIT=<sha> scripts/diskfull_campaign.slurm
# 3. Poll: sacct -j <jobid> --format=JobID,State,Elapsed,ExitCode -X
# 4. Once all 20 tasks show ExitCode 0:0, scp records/ + logs/ + node_meta/
#    down, sha256 verify, THEN delete the server-side copies.
# 5. Merge:
python scripts/corruption_campaign_merge.py --kind diskfull \
    --records-dir <pulled>/records --node-meta-dir <pulled>/node_meta \
    --base-seed 1000 --n-tasks 20 --trials-per-task 100 \
    --commit <sha> --array-job-id <jobid> \
    --out benchmarks/diskfull-v1/eval-diskfull-campaign-<date>.json
# 6. Validate:
python scripts/check_result_manifest.py \
    benchmarks/diskfull-v1/eval-diskfull-campaign-<date>.json
```

A rerun at a different seed will not reproduce this exact `result_digest`
(each trial's workload, mode, and injection point are derived from its
seed — `derive_trial_seed` in `scripts/eval_diskfull.py`), but at the same
`--base-seed`/task count it is bit-for-bit reproducible, modulo any code
change to the harness or engine between commits.
