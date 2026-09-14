# crash-v1 — EXP-A1, the ≥10,000-trial seeded crash/recovery campaign

`eval-crash-campaign-2026-09-13.json` is the committed record for **EXP-A1**
(Lane A, OSDI plan task A7): a Slurm array running `scripts/
eval_durability.py` (P0.7, D-086) at scale — 10 boundaries × 1,000 seeded
trials each, **10,000 trials total**, every trial independently
reproducible from its `(seed, boundary, trial)`.

- **commit**: `cb0e6af`
- **hosts**: itiger01 (2 tasks), itiger02 (18 tasks) — iTiger cluster,
  partition `bigTiger`
- **array job**: 210821, `--array=0-19%6`, submitted 2026-09-13 19:49:08,
  last task finished 20:05:09 — **16m1s wall-clock** for the whole
  campaign (summed per-trial `wall_s` across all 10,000 trials: 4912.85s;
  the wall-clock figure is smaller because up to 6 tasks ran concurrently)
- **result**: **10,000/10,000 trials clean** — 0 problems on every one of
  Q1 (acknowledged-write survival), Q2 (deterministic recovery), Q3
  (single-generation visibility), Q4 (orphan reclamation), at every
  boundary
- **manifest**: conforms to `benchmarks/schema/result_manifest.schema.json`
  (checked with `scripts/check_result_manifest.py`)
- **result_digest** (sha256 over the sorted trial records):
  `3b449172a072b1a3b3bb2f54bfa36d82bf06772efc27f451683b749ba4dae72f`
- **file sha256**:
  `33a95fe3079f1b912de3a0e69f3bd8a51e95d7225983605961c2996ee2e5a081`

## Per-boundary results

| boundary | trials | problems | failed Q1 | failed Q2 | failed Q3 | failed Q4 |
|---|---:|---:|---:|---:|---:|---:|
| `py_torn_wal_append` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `py_after_wal_fsync` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `py_before_engine_commit` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `after_seal` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `after_close_runs` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `after_dict` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `after_manifest` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `after_current` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `compact_before_install` | 1000 | 0 | 0 | 0 | 0 | 0 |
| `gc_mid_delete` | 1000 | 0 | 0 | 0 | 0 | 0 |
| **total** | **10000** | **0** | 0 | 0 | 0 | 0 |

This extends the harness's own 30-trial result (`docs/eval_durability.md`,
"Results — 30 trials") by three orders of magnitude at the same 10
boundaries, on the fully seeded/randomized workload generator (D-086 P0.7)
rather than the fixed legacy workload — no crash/recovery defect surfaced
at this scale. A clean campaign is itself the finding this task was run to
get; it does not retroactively prove no defect exists at boundaries or
scales this harness does not cover (concurrent writers, multi-process
crashes, disk-level corruption).

## Protocol

Each of 20 Slurm array tasks ran `scripts/eval_durability.py --trials 50
--seed <1000 + task_id> --boundaries <all 10>` (500 trials/task), against
a checkout at commit `cb0e6af` (`git worktree add` off `/project/xzhang12/
tgms`, so the object store — including the compiled Linux engine
`.so`, copied in from that checkout, built Aug 28 2026, newer than the
last `crates/` change of Aug 27 2026 — is shared, not duplicated).

Two deviations from the literal original plan, both forced by smoke
testing `scripts/crash_campaign.slurm` before submitting the real
campaign (see the script's own comments for the full detail):

1. **Trial scratch (`TMPDIR`) is node-local (`/tmp/tgms-crash-$SLURM_JOB_ID`),
   not under `/project`.** `/project` is NFS4
   (`itigercage-ibnet:/project`); the harness's `clean_replay_digest`
   deletes a `tempfile.TemporaryDirectory` after the native store closes
   its files, and on NFS that produced a hard `OSError: Directory not
   empty` on the very first smoke trial (NFS "sillyrename" `.nfsXXXX`
   semantics). Every iTiger compute node has a large node-local NVMe at
   `/tmp` (verified: xfs on `/dev/nvme0n1p1`, ~14T, itiger07-11); trial
   scratch goes there and is `rm -rf`'d when the task exits. Only the
   final per-task `--json` record (small — 500 trials/task) was ever
   written to `/project`, and even that staging copy was deleted after
   this campaign's records were scp'd down and sha256-verified (see
   below) — nothing from this campaign remains on `/project`.
2. **iTiger compute nodes have no `git` binary** (`which git` fails under
   `srun`; only login nodes have it). `scripts/eval_durability.py`
   (a harness file this campaign does not modify) unconditionally shells
   out to `git rev-parse HEAD` when writing `--json`. `scripts/
   crash_campaign.slurm` puts a two-line `git` shim ahead of it on `PATH`
   that answers exactly that one invocation from `$TGMS_COMMIT` — smoke-
   tested to be sufficient (the harness's only git call).

## Data handling

The 20 per-task JSON records (and per-task stdout/stderr logs, and
per-task host/uname sidecars) were staged at
`/project/xzhang12/crash-v1-work/{records,logs,node_meta}/`, sha256-summed
there, scp'd to the machine that ran this campaign, and **the sha256sums
were re-checked locally before the server-side copies were deleted** — the
staging directory carries nothing from this campaign today. The merge step
(`scripts/crash_campaign_merge.py`) never hand-types a number: every count
in this README and in `docs/eval_durability.md`'s EXP-A1 section is read
back out of the merged record.

## Regenerating

```sh
# 1. On iTiger: fetch + fast-forward a checkout to the target commit
#    (or `git worktree add` a fresh one off an existing checkout), copy in
#    the compiled tgms/_engine*.so if missing/stale, smoke-test with
#    --trials 1 --seed 1 on a compute node via srun.
# 2. Submit the campaign (adjust TGMS_REPO/TGMS_STAGE/TGMS_BASE_SEED via
#    --export if regenerating against a different checkout/seed):
sbatch scripts/crash_campaign.slurm
# 3. Poll: sacct -j <jobid> --format=JobID,State,Elapsed,ExitCode -X
#    (or watch /project/.../crash-v1-work/logs/task-<id>.out for the
#    RUN_STARTED / TASK_DONE lines).
# 4. Once all 20 tasks show ExitCode 0:0, scp records/ + logs/ +
#    node_meta/ down, sha256 verify, THEN delete the server-side copies.
# 5. Merge:
python scripts/crash_campaign_merge.py \
    --records-dir <pulled>/records --node-meta-dir <pulled>/node_meta \
    --base-seed 1000 --n-tasks 20 --trials-per-task 50 \
    --commit cb0e6af --array-job-id <jobid> \
    --out benchmarks/crash-v1/eval-crash-campaign-<date>.json
# 6. Validate:
python scripts/check_result_manifest.py benchmarks/crash-v1/eval-crash-campaign-<date>.json
```

A rerun at a different seed or scale will not reproduce this exact
`result_digest` (each trial's synthetic workload is derived from its
seed — `derive_trial_seed` in `scripts/eval_durability.py`), but at the
same `--base-seed`/task count/boundaries it is bit-for-bit reproducible,
modulo any code change to the harness or engine between commits.
