# corruption-v1 — EXP-A3, the ≥10,000-trial seeded corruption-detection sweep

`eval-corruption-campaign-2026-09-14.json` is the committed record for
**EXP-A3** (Lane A, OSDI plan task A9, building on task A4's design in
`docs/eval_durability.md`): a Slurm array running `scripts/
eval_corruption.py` at scale — 40 tasks × 250 seeded trials each,
**10,000 trials total**, every trial independently reproducible from its
`(seed, trial)` pair. Per trial: build one small store with all 13 on-disk
file families present, corrupt exactly one file one of 7 ways (both chosen
uniformly at random), then observe through four surfaces a real caller
would use — never repair — and classify DETECTED / SILENT / BENIGN /
TOLERATED-REBUILT.

- **commit**: `fc2fb98`
- **hosts**: itiger01 (32 tasks), itiger02 (2 tasks), itiger04 (6 tasks) —
  iTiger cluster, partition `bigTiger`
- **array job**: 211154, `--array=0-39%6`, all 40 tasks `COMPLETED`, exit
  `0:0` — submitted 2026-09-13 23:50:30, last task finished 23:56:33,
  **6m3s wall-clock** for the whole campaign (summed per-trial `wall_s`
  across all 10,000 trials: 1592.37s; the wall-clock figure is smaller
  because up to 6 tasks ran concurrently)
- **verify mode**: `--verify-mode full` (the harness's own default as of
  this task — see "The verify-mode upgrade" below)
- **result**: **10,000/10,000 trials classified, 0 SILENT** — the campaign
  gate (`docs/eval_durability.md` EXP-A3: "this count is always zero")
  passes cleanly
- **manifest**: conforms to `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — passes)
- **result_digest** (sha256 over the sorted trial records):
  `b4e434af9fff8ae445c47521107b884d2911832bd186dc357b5baab5bcd3a76e`
- **file sha256**:
  `9bc1314faa81d0ca105dded32d11cd08c5f0fd63c42db08dc386da9066af0d47`

## Verdict counts

| verdict | count |
|---|---:|
| DETECTED | 5966 |
| BENIGN | 3775 |
| TOLERATED-REBUILT | 259 |
| SILENT | **0** |
| **total** | **10000** |

BENIGN is the harness's documented non-detection case, not silence: every
BENIGN trial carries a specific, provable reason (see
`stats.benign_reasons_by_class` in the record) — chiefly "the mutated file
was superseded by this trial's own `compact()` before the mutation ran"
(pre-compaction segments/close-runs/manifests) and, for `artifact_blob`,
"plan blobs are read only by artifact `refresh()`, never by `check()`."
TOLERATED-REBUILT is `tcsr_file` only: the persisted TCSR index is
documented to silently rebuild rather than error or be trusted
(`tests/test_tcsr_persistence.py`).

## Per class × mutation detection matrix

Read directly from `stats.detection_matrix` in the record (`trials`,
`detected`, `detection_rate` — `detected` counts only the DETECTED verdict;
a low rate is not itself a problem where `benign_reasons_by_class`
explains it, as for the segment/close-run classes below).

| class | mutation | trials | detected | rate |
|---|---|---:|---:|---:|
| artifact_blob | append_garbage | 106 | 0 | 0.0 |
| artifact_blob | delete_file | 89 | 89 | 1.0 |
| artifact_blob | flip_bit | 98 | 0 | 0.0 |
| artifact_blob | flip_byte | 103 | 0 | 0.0 |
| artifact_blob | swap_same_class | 103 | 0 | 0.0 |
| artifact_blob | truncate | 94 | 0 | 0.0 |
| artifact_blob | zero_span | 117 | 0 | 0.0 |
| artifacts_jsonl | append_garbage | 109 | 109 | 1.0 |
| artifacts_jsonl | delete_file | 116 | 116 | 1.0 |
| artifacts_jsonl | flip_bit | 126 | 125 | 0.9921 |
| artifacts_jsonl | flip_byte | 211 | 211 | 1.0 |
| artifacts_jsonl | truncate | 109 | 109 | 1.0 |
| artifacts_jsonl | zero_span | 87 | 87 | 1.0 |
| checkpoint_manifest | append_garbage | 98 | 98 | 1.0 |
| checkpoint_manifest | delete_file | 102 | 87 | 0.8529 |
| checkpoint_manifest | flip_bit | 99 | 98 | 0.9899 |
| checkpoint_manifest | flip_byte | 126 | 126 | 1.0 |
| checkpoint_manifest | swap_same_class | 126 | 126 | 1.0 |
| checkpoint_manifest | truncate | 101 | 101 | 1.0 |
| checkpoint_manifest | zero_span | 104 | 104 | 1.0 |
| close_run | append_garbage | 117 | 6 | 0.0513 |
| close_run | delete_file | 119 | 2 | 0.0168 |
| close_run | flip_bit | 95 | 34 | 0.3579 |
| close_run | flip_byte | 102 | 8 | 0.0784 |
| close_run | swap_same_class | 125 | 38 | 0.304 |
| close_run | truncate | 96 | 0 | 0.0 |
| close_run | zero_span | 97 | 1 | 0.0103 |
| current | append_garbage | 118 | 118 | 1.0 |
| current | delete_file | 104 | 104 | 1.0 |
| current | flip_bit | 93 | 93 | 1.0 |
| current | flip_byte | 199 | 199 | 1.0 |
| current | truncate | 133 | 127 | 0.9549 |
| current | zero_span | 103 | 103 | 1.0 |
| delta_manifest | append_garbage | 117 | 117 | 1.0 |
| delta_manifest | delete_file | 105 | 105 | 1.0 |
| delta_manifest | flip_bit | 113 | 111 | 0.9823 |
| delta_manifest | flip_byte | 120 | 120 | 1.0 |
| delta_manifest | swap_same_class | 109 | 109 | 1.0 |
| delta_manifest | truncate | 122 | 122 | 1.0 |
| delta_manifest | zero_span | 91 | 91 | 1.0 |
| dict_tail | append_garbage | 110 | 0 | 0.0 |
| dict_tail | delete_file | 107 | 107 | 1.0 |
| dict_tail | flip_bit | 104 | 70 | 0.6731 |
| dict_tail | flip_byte | 205 | 205 | 1.0 |
| dict_tail | truncate | 104 | 104 | 1.0 |
| dict_tail | zero_span | 121 | 88 | 0.7273 |
| event_log_record | append_garbage | 97 | 97 | 1.0 |
| event_log_record | delete_file | 92 | 92 | 1.0 |
| event_log_record | flip_bit | 118 | 118 | 1.0 |
| event_log_record | flip_byte | 230 | 230 | 1.0 |
| event_log_record | truncate | 114 | 114 | 1.0 |
| event_log_record | zero_span | 116 | 116 | 1.0 |
| event_log_tail | append_garbage | 117 | 117 | 1.0 |
| event_log_tail | delete_file | 116 | 116 | 1.0 |
| event_log_tail | flip_bit | 111 | 111 | 1.0 |
| event_log_tail | flip_byte | 224 | 224 | 1.0 |
| event_log_tail | truncate | 108 | 108 | 1.0 |
| event_log_tail | zero_span | 120 | 120 | 1.0 |
| segment_body | append_garbage | 105 | 4 | 0.0381 |
| segment_body | delete_file | 117 | 6 | 0.0513 |
| segment_body | flip_bit | 117 | 4 | 0.0342 |
| segment_body | flip_byte | 125 | 8 | 0.064 |
| segment_body | swap_same_class | 114 | 3 | 0.0263 |
| segment_body | truncate | 120 | 7 | 0.0583 |
| segment_body | zero_span | 101 | 4 | 0.0396 |
| segment_footer | append_garbage | 109 | 5 | 0.0459 |
| segment_footer | delete_file | 118 | 3 | 0.0254 |
| segment_footer | flip_bit | 101 | 2 | 0.0198 |
| segment_footer | flip_byte | 106 | 2 | 0.0189 |
| segment_footer | swap_same_class | 111 | 5 | 0.045 |
| segment_footer | truncate | 114 | 1 | 0.0088 |
| segment_footer | zero_span | 105 | 2 | 0.019 |
| segment_header | append_garbage | 133 | 6 | 0.0451 |
| segment_header | delete_file | 116 | 3 | 0.0259 |
| segment_header | flip_bit | 129 | 4 | 0.031 |
| segment_header | flip_byte | 119 | 2 | 0.0168 |
| segment_header | swap_same_class | 103 | 8 | 0.0777 |
| segment_header | truncate | 107 | 6 | 0.0561 |
| segment_header | zero_span | 109 | 4 | 0.0367 |
| tcsr_file | append_garbage | 108 | 0 | 0.0 |
| tcsr_file | delete_file | 109 | 0 | 0.0 |
| tcsr_file | flip_bit | 126 | 104 | 0.8254 |
| tcsr_file | flip_byte | 231 | 211 | 0.9134 |
| tcsr_file | truncate | 116 | 116 | 1.0 |
| tcsr_file | zero_span | 115 | 115 | 1.0 |

The `segment_body`/`segment_footer`/`segment_header`/`close_run` low rates
are the same "superseded by `compact()`" BENIGN reason as above — the
fixture builds several segments and close runs before its one `compact()`
call, so a uniformly random pick lands on an orphan most of the time
(`build_meta.orphan_files` records exactly which ones, so this is provable
per trial, not assumed). `tcsr_file|append_garbage` and `|delete_file`
read as 0% DETECTED because they are the TOLERATED-REBUILT path instead
(a missing or merely-appended-to index is a legitimate "nothing persisted
yet" / trailing-garbage-tolerant shape the engine rebuilds around, not an
error) — see `stats.verdict_counts` above, not this table alone, for the
full picture.

## The verify-mode upgrade (Lane A task A9)

Task A4's original harness observed `store.adapter.verify()` at its
default `mode="fast"` — the engine's own file walk (segments, close runs,
the dictionary, manifests the *current* generation names). Two things
landed since task A4 wrote that design: **A3** (`verify_full`, `tgms store
verify --full --json`, findings shaped `{layer, kind, path, generation,
detail, severity}`) and **A8** (a populated store missing `CURRENT` now
refuses to open, rather than reading as empty). Task A9 switched the
sweep's own oracle to match: `scripts/eval_corruption.py --verify-mode
full` (now the default; `--verify-mode fast` reproduces the old behavior
for comparison). Full mode adds the manifest parent chain across *every
retained generation* still on disk (not only the chain reachable from
`CURRENT`), the event log (framing, monotonicity, chain), the persisted
TCSR permutation, and the artifact registry — strictly more than fast
mode reads, never less.

This is not merely aspirational: it changed two unit tests' real, provable
outcomes (`tests/test_eval_corruption.py`) — an orphaned *manifest*
generation's corruption is now DETECTED (full mode's own parent-chain
check reads every manifest file still on disk, orphaned or not, unlike
fast mode), and a severely corrupted `tcsr_file` is now DETECTED via
`verify()` directly rather than only reachable via the harness's separate
degrade-to-rebuild check — both confirmed side-by-side against
`--verify-mode fast` on the identical file and mutation in the test suite,
not asserted from theory.

## Protocol

Each of 40 Slurm array tasks ran `scripts/eval_corruption.py --trials 250
--seed <1000 + task_id> --verify-mode full` against a checkout at commit
`fc2fb98` (`git worktree add` off `/project/xzhang12/tgms`, engine rebuilt
from source on itiger07 via `srun` + `uv sync --extra agent
--reinstall-package tgms`; confirmed `tgms._engine.MANIFEST_FORMAT_VERSION
== 2` and that a populated, `CURRENT`-less store refuses to open, both via
a direct 5-line smoke on the compute node before submitting).

Reuses, unmodified, the infrastructure fixes `scripts/crash_campaign.slurm`'s
smoke testing already discovered (`benchmarks/crash-v1/README.md`):
node-local `TMPDIR` (NFS4 sillyrename under `/project` breaks
`tempfile.TemporaryDirectory` cleanup) and a two-line `git` shim answering
`TGMS_COMMIT` (no `git` binary on iTiger compute nodes).

**One new infrastructure bug found and fixed by this task, before any
trial ran**: `scripts/corruption_campaign.slurm`'s (and
`scripts/diskfull_campaign.slurm`'s) `TGMS_COMMIT` guard —
`${TGMS_COMMIT:?set TGMS_COMMIT to the checkout's commit sha}` — had never
actually been run under `sbatch` before this task's first submission. The
apostrophe in the `:?` error message breaks bash's parser even inside the
enclosing double quotes; both array jobs (first submission: 211093/211094)
failed instantly, exit 2, every task, before a single Python line ran.
Fixed by rewording to drop the possessive (`scripts/corruption_campaign.slurm`,
`scripts/diskfull_campaign.slurm`), verified with `bash -n` on both files
locally and by a clean resubmission (211154/211155) that actually ran.

## Data handling

The 40 per-task JSON records (and per-task stdout/stderr logs, and
per-task host/uname sidecars) were staged at
`/project/xzhang12/corruption-v1-work/{records,logs,node_meta}/`,
sha256-summed there, scp'd down, and **the sha256sums were re-checked
locally before the server-side copies were deleted** — the staging
directory carries nothing from this campaign today. The merge step
(`scripts/corruption_campaign_merge.py --kind corruption`) never
hand-types a number: every count in this README and in
`docs/eval_durability.md`'s EXP-A3 section is read back out of the merged
record.

## Regenerating

```sh
# 1. On iTiger: fetch + fast-forward (or `git worktree add`) a checkout to
#    the target commit, rebuild the engine on a compute node
#    (srun -p bigTiger -w itiger07 -- uv sync --extra agent
#    --reinstall-package tgms), smoke-test with --trials 3 on the node.
# 2. Submit (adjust TGMS_REPO/TGMS_STAGE/TGMS_BASE_SEED/TGMS_COMMIT via
#    --export if regenerating against a different checkout/seed):
sbatch --export=ALL,TGMS_REPO=<checkout>,TGMS_STAGE=<stage>,\
TGMS_BASE_SEED=1000,TGMS_COMMIT=<sha> scripts/corruption_campaign.slurm
# 3. Poll: sacct -j <jobid> --format=JobID,State,Elapsed,ExitCode -X
# 4. Once all 40 tasks show ExitCode 0:0, scp records/ + logs/ + node_meta/
#    down, sha256 verify, THEN delete the server-side copies.
# 5. Merge:
python scripts/corruption_campaign_merge.py --kind corruption \
    --records-dir <pulled>/records --node-meta-dir <pulled>/node_meta \
    --base-seed 1000 --n-tasks 40 --trials-per-task 250 \
    --commit <sha> --array-job-id <jobid> \
    --out benchmarks/corruption-v1/eval-corruption-campaign-<date>.json
# 6. Validate:
python scripts/check_result_manifest.py \
    benchmarks/corruption-v1/eval-corruption-campaign-<date>.json
```

A rerun at a different seed will not reproduce this exact `result_digest`
(each trial's fixture and mutation choice are derived from its seed —
`derive_trial_seed` in `scripts/eval_corruption.py`), but at the same
`--base-seed`/task count/classes/mutations it is bit-for-bit reproducible,
modulo any code change to the harness or engine between commits.
