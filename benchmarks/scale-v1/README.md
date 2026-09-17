# Scale-v1 benchmark records

Pre-registration: `docs/design/SCALE_BUILD_FORECAST_2026-09-15.md`, Addenda
4-6. Stage 0 (iTiger calibration, 1M/10M) is `itiger-calib-2026-09.README.md`
in this directory. This file covers **Stage 1**: 30M (lane B7-S1-30M) and
100M (lane B7-S1-100M, below), a separate lane that shared the same
cluster worktree/engine/tools and used `*-100m` file names throughout.

## Stage 1 — 30M

Lane B7-S1-30M. Host and rules exactly as Stage 0 (Addendum 4/5): cluster
worktree `/project/xzhang12/tgms-b7`, public main **`b159bd1`**
(`b159bd10aa53ad5de5f4e8ff7af1c87789d6afd0`), release build,
`build_info()`: `{"profile": "release", "opt_level": "3", "engine_version":
"0.8.0", "debug_assertions": false, "manifest_format_version": 3}`.
`TGMS_SEGMENT_CACHE_BYTES=46500000000` pinned in every job. Slurm:
`bigTiger`, `--exclude=itiger04,itiger05`, `--gres=gpu:rtx_5000:1`,
`--cpus-per-task=8`, `--mem=128G`. Records/logs under
`/home/xzhang12/b7-work-h/`; commit stamped via `.HEAD_COMMIT` +
`TGMS_COMMIT` (git absent on compute nodes), same mechanism Stage 0
documented. `du -sm /project/xzhang12`: 30,076 MB before this lane's build,
49,343 MB after the last job (well inside the ~200 GB quota; the 30M store
is ≈1.55 GB segments + the two replay copies, ≤ 25 GB total as predicted).

### What ran (jobs, in order)

1. **Build** (job 213067, `--mem=128G --time=06:00:00`, itiger07) —
   `scripts/build_synth_store.py --n-entities 30000000 --batch 250
   --compact-every 1000000 --digest streaming --backend native`, with the
   external 30s `tools/rss_sampler.py` VmHWM/VmRSS sampler alongside the
   harness's own per-decile figures. `BUILD_EXIT=0`.
2. **Query-ready floor, first attempt** (job 213068) — **FAILED** after 33s:
   `WriterLockedError` (writer.lock held by another pid). `tools/
   query_floor.py` opened the store non-read-only and raced jobs 213069/
   213070, which started at the same instant once the build's
   `afterok:213067` dependency released all three together. **Fixed**
   (`scripts/query_floor.py`: added `read_only=True` — a cold-open floor
   measurement is a reader, not a writer) and re-run **alone**
   (`--dependency=afterany:213071`, nothing else touching the store) as job
   **213173**. `n_ok=13/13`.
3. **Scale-curve + check --full, first attempt** (job 213069) — ran
   concurrently with 213068 and 213070 (all three released together, see
   above). **Kept on record, not discarded**, labelled `contended_first_run`
   in `scale-curve-30m.json`/`check-full-30m.json`. Re-run alone as job
   **213174** (`--dependency=afterany:213173`) once the query-floor fix
   landed; that job also shared its node (itiger07) with the *separate*
   100M-build lane's job 213188 from 04:08:31 onward (CPU/IO node sharing
   only — different store, no lock contention) — recorded as
   `concurrent_on_node: ["213188"]`.
4. **version_history probe, first attempt** (job 213070) — same
   concurrency as 213069/213068 above; kept as `contended_first_run`.
   Re-run alone as job **213175** (`--dependency=afterany:213174`, landed
   on itiger08, no node sharing).
5. **Recovery replay, `--compact-every 500`** (job 213071, frozen protocol
   — identical cadence to the Stage-0/EXP-A4 1M recovery) — `python -m
   tgms.cli replay <store>/eventlog.jsonl --store <fresh dir> --backend
   native --compact-every 500`, then `digest_streaming()` compared.
6. **Recovery replay, `--compact-every 5000`** (job 213187, Addendum 6
   cadence-isolation run) — run alone, `--dependency=afterany:213175`,
   after every 30M rerun had finished. **Does not relabel or supersede**
   job 213071's result — both stand as separate, honestly-labelled
   measurements per Addendum 6.

### Build results

| quantity | value |
|---|---:|
| wall | 3,786.004 s (63.10 min) |
| total ops | 30,000,262 |
| compactions | 31 |
| peak RSS (VmHWM) | 59,313,264 KB (59.31 GB) |
| final manifest bytes | 175,244 B |
| final segment bytes | 1,548,718,378 B (1.549 GB) |
| store digest (streaming) | `239118cae2044e5928b68d82423e0e996b6bc96654cbfd34266747486aa7e6d8` |

VmHWM trajectory (external 30s sampler, `rss-30m.jsonl`): ingest rose
~linearly from ~218 MB to a plateau of ~40.6 GB by the time compaction
began, `compact()` pushed it to ~54.2 GB, and the streaming digest pass
pushed it to the final 59.31 GB peak.

**Per-decile bulk ops/s** (10 deciles of 3,000,000 ops each, the incremental
rate over each decile's own elapsed-time window — same convention as
`itiger-calib-10m.json`'s `ops_per_s_by_decile`):

| decile | ops range | ops/s |
|---:|---|---:|
| 1 | 0–3M | 10,145.4 |
| 2 | 3–6M | 14,306.2 |
| 3 | 6–9M | 10,538.1 |
| 4 | 9–12M | 10,933.5 |
| 5 | 12–15M | 8,513.1 |
| 6 | 15–18M | 8,412.2 |
| 7 | 18–21M | 7,788.2 |
| 8 | 21–24M | 6,171.3 |
| 9 | 24–27M | 6,373.2 |
| 10 | 27–30M | 6,175.4 |

**Median: 8,463.0 ops/s.**

### Addendum 5 / 6 quantities and falsifier scoring

| quantity | band (Addendum 5, 30M) | measured | verdict |
|---|---|---:|---|
| build wall | 25 min – 1.5 h | 63.10 min | **PASS** |
| steady-decile ops/s | > 10,000 | 8,463.0 | **TRIPPED** — see mechanism below |
| build VmHWM (H1 vs H2) | H1 ≤ 50 GB; H2 55–65 GB | 59.31 GB | **H2 named, H1 refuted** |
| query-ready floor VmHWM | 5.5–7.5 GB | 6.81 GB (job 213173) | **PASS** |
| `check --full` | healthy, ≤ 2 min | healthy, 98.673 s (213174) / 83.704 s (213069) | **PASS** |
| recovery (`--compact-every 500`, frozen protocol) | 29–46 min, digest equal | **14,618.6 s = 4.06 h**, digest equal | **REFUTED** (5.3× upper bound) |
| recovery (`--compact-every 5000`, Addendum 6) | 35–70 min, digest equal | 2,656.9 s = 44.28 min, digest equal | **PASS** |
| manifest bytes | ≤ 4 GB | 175,244 B | **PASS** |
| segment bytes | ~1.5 GB | 1.549 GB | **PASS** |
| `version_history` (kind=edge, belief=all) | ≤ 20 s / ≤ 4 GB | 5.496 s / 3.863 GB (clean, job 213175) | **PASS** |
| `reach.window` admission | admitted unless `time_est_ms` > 10,000 | `time_est_ms=4,371`, **admitted**, executed both runs | as predicted |
| `reach.window` p50 vs bar | ≤ 3.3× the 10M anchor (493.5 ms) = 1,628.55 ms | 2,354.494 ms (clean, 213174) | **FALSIFIED** |
| `resolve.substr` p50 vs bar (super-linear class) | ≤ 4.5× anchor = 243.9 ms | 342.604 ms (clean) | **FALSIFIED** (expected possible — doc flags this class "not assumed to self-correct") |
| `hist.single`/`hist.asof` p50 vs bar | ≤ 0.716 / ≤ 0.659 ms | 0.977 / 0.980 ms (clean) | **FALSIFIED** (trivial: N=2-row point lookups, sub-millisecond, almost certainly clock-resolution noise — Stage 0 itself flagged this operator class as trivially small) |
| all other 8 operators | per §2h multiplier | — | **PASS** (max deviation from the 10M-anchor bar well inside band; see `scale-curve-30m.json`) |

**Recovery-cadence mechanism (Addendum 6, confirmed).** `--compact-every`
counts *applied replay batches*, not raw events: the 30M eventlog has
120,262 batches, so `--compact-every 500` fires 240 compactions and
`--compact-every 5000` fires 24. `compact()` materialises the whole store
per call (D-164, `compact.rs`) — cost is per-compaction, not per-row-
changed — so 10× fewer compactions gave a ~5.5× shorter replay (2,656.9 s
vs 14,618.6 s) for byte-identical output (`digest_equal: true` both times).
The 4.06h frozen-protocol result is attributed to cadence × D-164, not to
the recovery path being broken; both numbers are on record.

**Contended vs. clean reruns — per-operator delta.** Comparing job 213069
(contended, concurrent with 213068/213070) against job 213174 (clean,
alone): the largest delta is 11.1% (`hist.single`/`hist.asof`, both
sub-millisecond point lookups — noise at that scale); no operator differs
by more than 20%. Both runs are kept in `scale-curve-30m.json`, neither
discarded or averaged, per the coordinator's instruction.

**`expB1-30m.json` — produced (records-only follow-up lane).** The §4 4-row
D-071/`HANDOFF-ENGINE.md` column set (`docs/HANDOFF-ENGINE.md:218-224`:
store on disk, query-ready floor (VmHWM), columnar scan, `version_history`)
is now assembled from the records already on this page — no new
measurement. Values: store on disk **4.522 GB** (`build-30m.json`
`build_info.store_bytes.total_bytes`); query-ready floor **6.81 GB**
(`queryfloor-30m.json` `vmhwm_kb`, job 213173); columnar scan
(`aggregate_events`/`agg.rel_bucket`, the `docs/TECHNICAL_REPORT.md`
D-058-vs-D-069 control) **176.492 ms** clean / 169.596 ms contended
(`scale-curve-30m.json` `per_operator_p50_ms."agg.rel_bucket"`);
`version_history` **5.496 s / 3.863 GB** (`version-history-30m.json`
`clean_alone`, job 213175 — the same figures already in the table above).
Every field is cited to its source file in `expB1-30m.json`'s own
`exp_b1.rows.*.source`; nothing was estimated. `expB1-100m.json` is a
separate file, not yet produced (100M chain still running on iTiger).

### Deviations from the literal Stage-1 recipe

1. **`tools/query_floor.py` writer-lock bug** (job 213068 → fixed → job
   213173). See "What ran" above.
2. **Filename collision in the first `scale_check_30m.slurm` rerun.** The
   fix to job-ID-suffix `scale-curve-30m-raw.json`/`reach-admission-30m.json`
   (to avoid the rerun overwriting the contended run's raw output) was
   pushed to the cluster *after* job 213174 had already been submitted —
   Slurm spools a job's script at submission time, so the already-queued
   213174 ran the old, unsuffixed version. No data was lost: the contended
   run's raw files were copied to `-213069`-suffixed names *before* 213174
   started (confirmed via `sacct`), so the unsuffixed `scale-curve-30m-
   raw.json`/`reach-admission-30m.json` in this directory are unambiguously
   213174's (clean) output, and the `-213069` files are the preserved
   original.
3. **Node sharing, not store contention.** Jobs 213174 (itiger07) shared a
   compute node with the separate 100M-build lane's job 213188 for part of
   its run (`concurrent_on_node` in `scale-curve-30m.json`); this is CPU/IO
   contention only, not store-lock contention (different stores). 213175
   and 213187 both landed on itiger08 (excluded itiger04/05/07 via
   `scontrol update ... ExcNodeList=` once queued), no node sharing.

### Files and provenance (sha256)

| file | sha256 |
|---|---|
| `build-30m.json` | `82cbbfc3ad50f29bf3a7433099599707b7b516c74bbca6022f0eb4b5dc60003a` |
| `build-30m.stdout.log` | `38c725c4bee31edd9a30712da695e7efce8f3c548101bbde2659ebf9feecb109` |
| `build-record-30m.json` | `f1645a4b73bcb752fb1e6bc226bf357df104c36836750df44d8b1acf25e0ba7c` |
| `check-full-30m.json` | `e8dafe98454a0501f519703aa385ec6ac748fe10d46d612a6ca1a984b1a2ffeb` |
| `check-full-30m-213174-raw.json` | `edabf84996a1c0131d55c8f25c10d07b8884487c21a90f954bc23b27a1316b81` |
| `check-full-30m-213069-raw.json` | `edabf84996a1c0131d55c8f25c10d07b8884487c21a90f954bc23b27a1316b81` |
| `queryfloor-30m.json` | `74e158234310aefefe4db1b9ce4e30b4e711b82647e2d7c110969f3e4e01a1a0` |
| `queryfloor-30m.stdout.log` | `a6578939304309bdc1bedcc955b21cbaa7522aeb1729c3feb3da9bbccb37eb26` |
| `queryfloor-30m-213068-failed.stdout.log` | `4fdae1cc9e6068c1172f352e8d6aa686de517c9a14a8d3e32538534143526469` |
| `queryfloor-30m-213068-failed.stderr.log` | `3431056bc0ce2387c987106b1175baf7804a6cb0e798e26f1cc3e4960cfc977d` |
| `reach-admission-30m.json` | `8736c2e03243d85a189eb14b015edec24575cf945ec193556010c0bd3b41481c` |
| `reach-admission-30m-213069.json` | `1a13b14f3e02386b0c78604b56ba9215b380e2e5ab2d00014d518594c696ea48` |
| `recovery-30m.json` | `4c2f08c89a1cbf050881e3678410b19e4e181af70f2728f2ae0b788779847a5b` |
| `recovery-30m.stdout.log` | `55e51328cc35c6ad334feeac0e8dac4c371192c6f18d18448c380f2b811f90d5` |
| `recovery-30m-digest.json` | `9335c16272bec7e53318405852ee5d8557257c04e5b6a77a720981dcf67364b6` |
| `recovery-30m-wall.json` | `7c2c5efb2de4923b91d7a1d1fa0d24b239627ad340c7aa8b93f93871ab9352bf` |
| `recovery-30m-ce5000.json` | `48276f3f9ad86334b6ac31b4aede11d3be83cae27d505ed47146653d5a0a68fb` |
| `recovery-30m-ce5000.stdout.log` | `e10eeb075ac2c540cf2af3e943dba96943f9832c191c0091bd04dacba8c009e2` |
| `recovery-30m-ce5000-raw.json` | `22bedb0d0934d7401abfd505a1515b1aadf10a64344fe75a9fc3d6ce41f897e0` |
| `scale-curve-30m.json` | `48bd1d13c17dd07a24993a3479db4c937e84a7c2e0b66d4108a148307ceda381` |
| `scale-curve-30m-raw.json` (clean, 213174) | `7a35c6c999e3e7c5cf57482aafb84fb51d4971f97a51015395a10f14ebb219f1` |
| `scale-curve-30m-raw-213069.json` (contended) | `497ab83d6c3ffca11ba54f30b4a511c21619c9a71f25825bcf7c44e863ce240a` |
| `scale-check-30m.stdout.log` (213174) | `5d8c4fed649ff25dec741a9f805bfa576bc5f0b9d4a8f622090149d8f24d3d0a` |
| `scale-check-30m-213069.stdout.log` | `7ceb04886bc2ef443c111a25d7ef47951dd3d22b60289366618f9fb5a5d44c46` |
| `version-history-30m.json` | `337133364282dd24f6da95ec8b50670c19f9006d464f7e116079742d5cfa2f23` |
| `version-history-30m.stdout.log` (213175) | `24773f5240ee0aff12ca67c4113fb54bbc7d35cfe5abb528352921e86a6817ec` |
| `version-history-30m-213175-raw.json` | `1cdc6afc85b1bf58963456b547941f9b641aaee0493f76845470b075809a9b91` |
| `version-history-30m-raw.jsonl` (213175, 3 reps) | `5f35675a78bed48613e7c0b6fb505b3b012f0e5031ea3938210b37974ba83f39` |
| `version-history-30m-213070-contended.json` | `b203b7bc4c29b19716987570813fd77314fe50da7f65d6d678a1262fcc1d3871` |
| `version-history-30m-raw-213070-contended.jsonl` | `cf0d1f38d5e195b0c13125a90986fd9458a4cd1681c396d171e204c19332617f` |
| `rss-30m.jsonl` | `f491670ba0d8d19ed041ae07193464120a4e847f00161cf5b24707d9a8bb03dd` |
| `expB1-30m.json` (locally assembled, not cluster-transferred — see note below) | `caac6b4d3fbaf420df88c442c0d920a4423d230c598ae101137e5fda1dd58f66` |

Every file above except `expB1-30m.json` was sha256-verified byte-identical
between its source path on the cluster and the copy in this worktree
(spot-checked directly
against `/project/xzhang12/tgms-b7/...` and `/home/xzhang12/b7-work-h/...`
at commit time); `expB1-30m.json` was instead assembled locally, in this
worktree, from those already-verified records (see "`expB1-30m.json` —
produced" above) — its sha256 above is a tamper check on this worktree's
own copy, not a cluster-vs-worktree transfer check. All seven schema-
bearing records (`build-30m.json`, `scale-curve-30m.json`,
`check-full-30m.json`, `recovery-30m.json`, `recovery-30m-ce5000.json`,
`version-history-30m.json`, `expB1-30m.json`) validate against
`benchmarks/schema/result_manifest.schema.json`
(`scripts/check_result_manifest.py`, this worktree's `uv`-managed venv).

The cluster worktree `/project/xzhang12/tgms-b7`, its `stores/synth-30m-
native/`, and both replay copies (`synth-30m-native-replayed-213071`,
`synth-30m-native-replayed-ce5000-213187`) are left in place on iTiger
(never deleted, per policy).

Slurm job IDs: 213067 (build), 213068 (query-floor, failed), 213069
(scale-curve+check, contended), 213070 (version_history, contended), 213071
(recovery ce500), 213173 (query-floor, clean), 213174 (scale-curve+check,
clean), 213175 (version_history, clean), 213187 (recovery ce5000).

Tool scripts committed alongside this record: `scripts/query_floor.py`,
`scripts/version_history_probe.py` (both new, written for this lane) and
`scripts/rss_sampler.py` (Stage 0's tool, not previously committed —
carried forward here). All three pass `ruff check` (this repo's pinned
version via `uv run ruff check`, matching `.github/workflows/ci.yml`).
Job scripts: `benchmarks/scale-v1/jobs/{build_30m,queryfloor_30m,
scale_check_30m,version_history_30m,recovery_30m,recovery_30m_ce5000}.slurm`.
Both sets were transferred byte-identical from the cluster (sha256-verified
against the running copy before submission, or immediately after for
`scale_check_30m.slurm`'s job-ID-suffix fix — see Deviations above).

| script | sha256 |
|---|---|
| `scripts/query_floor.py` | `a5af6e2dbc8289c1ba123ca2f98adf888196ab0e0ba4c27820044369d1864ebd` |
| `scripts/version_history_probe.py` | `446f8686e6964ece114cc4f196b3b25a0633b026c2bcae0833595fde92d8b488` |
| `scripts/rss_sampler.py` | `773ae576968098f28a25ca41b9fe785c4a8242064743109b5d49338c0abcf488` |
| `benchmarks/scale-v1/jobs/build_30m.slurm` | `50b20c3fb6780736d0ed4ef98a0dc910097297a4e6ccc1ad12d413393d28bc85` |
| `benchmarks/scale-v1/jobs/queryfloor_30m.slurm` | `7cc9c5f55de09a9144620196a40ea9b6b45dea3c666b90f9681721f2dee7c8aa` |
| `benchmarks/scale-v1/jobs/scale_check_30m.slurm` | `6b6994e63b8888d1632214626a450ca11a60f25d0ac6f90a5dfff15fa545f13b` |
| `benchmarks/scale-v1/jobs/version_history_30m.slurm` | `1a02a2b33f630257f70c698b1b671df04a35b23ff4fb6b2c596cfe191d90b21f` |
| `benchmarks/scale-v1/jobs/recovery_30m.slurm` | `5637e891e55e76fa50afc24235868bcea024128e826cc8586871eacaeb879eea` |
| `benchmarks/scale-v1/jobs/recovery_30m_ce5000.slurm` | `2a99fc57316121f17620f50776924448bfb7e9931dafaf89ab6f5693c4be2c65` |

## Stage 1 — 100M

Lane B7-S1-100M. GO per `SCALE_BUILD_FORECAST_2026-09-15.md` Addendum 6
("100M: GO, frozen now"). Shared the same cluster worktree
(`/project/xzhang12/tgms-b7`, engine `b159bd1`
= `b159bd10aa53ad5de5f4e8ff7af1c87789d6afd0`), the same job-script/tool
conventions (`.HEAD_COMMIT`+`TGMS_COMMIT` stamping,
`TGMS_SEGMENT_CACHE_BYTES=46500000000`, live write test on `/home`,
`du -sm /project/xzhang12` fail-fast) and the same
`tools/{query_floor.py,version_history_probe.py,rss_sampler.py}` as the
concurrently-running B7-S1-30M lane, used unchanged. Slurm:
`bigTiger`, `--exclude=itiger04,itiger05`, `--gres=gpu:rtx_5000:1`,
`--cpus-per-task=8`, `--mem=256G` on every job (per Addendum 6's 100M
shape); build `--time=36:00:00`, recovery `--time=24:00:00`, the three
middle jobs `06:00:00`/`12:00:00`. All five jobs ran strictly serialized
via `--dependency=afterany` chains (never two jobs touching the store at
once) and all landed on node **itiger07** (the 30M lane's `scale-check-30m`
job 213174 shared that node with this lane's build 213188 for part of its
run — CPU/IO sharing only, different stores, recorded on both sides).
`du -sm /project/xzhang12`: 38,966 MB before this lane's build, 57,945 MB
after the build, 73,377 MB after the recovery replay (well inside the
~187 GB effective quota; ~129 GB headroom remained even before the
recovery copy landed).

### What ran (jobs, in order)

1. **Build** (job 213188, `--mem=256G --time=36:00:00`) —
   `scripts/build_synth_store.py --n-entities 100000000 --batch 250
   --compact-every 1000000 --digest streaming --backend native`, with the
   external 30s `tools/rss_sampler.py` sampler alongside the harness's own
   per-tick figures. `BUILD_EXIT=0`. Sacct wall (queue+setup+run) 08:41:31;
   the harness's own measured `wall_s` (used for all scoring below, same
   convention as Stage 0/30M) is 28,372.936 s.
2. **Query-ready floor** (job 213189, `--dependency=afterany:213188`) —
   ran once, cleanly (no writer-lock race: the 30M lane's `read_only=True`
   fix to `scripts/query_floor.py` was already in place). `n_ok=6/13`.
3. **Scale-curve + check --full + reach-admission probe** (job 213190,
   `--dependency=afterany:213189`) — `scripts/eval_harness.py --scale
   100000000 --systems native`, then `tgms check <store> --json`, then the
   dedicated `temporal_reachability` admission-estimate probe (same script
   shape as the 30M lane's).
4. **version_history probe** (job 213191, `--dependency=afterany:213190`)
   — 3 reps, separate processes, `kind=edge belief=all window=[0,105e6)
   limit=10`.
5. **Recovery replay, `--compact-every 5000`** (job 213192,
   `--dependency=afterany:213191`) — Addendum 6's 100M recovery-cadence
   pre-registration: `--compact-every 500` was ruled out before dispatch
   (the 30M frozen-protocol result extrapolates to ~45h at 100M, infeasible
   within the 2-day Slurm wall and would only re-measure D-164), so this is
   the campaign's **only** 100M recovery measurement — no 500-cadence
   100M run exists to reconcile against.

No failed/rerun jobs, no writer-lock races, no filename collisions — the
whole chain ran clean on the first attempt.

### Build results

| quantity | value |
|---|---:|
| wall (harness `wall_s`) | 28,372.936 s (7.882 h) |
| total ops | 100,000,262 |
| compactions | 101 (100 in-loop + 1 trailing, before digest) |
| peak RSS (VmHWM) | 186,422,544 KB (186.42 "GB", campaign convention -- see falsifier note below; 177.79 true GiB) |
| final manifest bytes | 206,946 B |
| final segment bytes | 5,218,378,406 B (5.218 GB) |
| store digest (streaming) | `48bcb256874e6ad7ed8712ebff668fb25f81c6eafd78d7274efa245efa8a2d97` |

**Finalisation phases** (`build-record.json build_info.finalisation_phases`,
authoritative, separately timed): final compaction (the 101st) 571.910 s,
streaming digest 2,759.624 s, gc 38.848 s, stats 35.085 s -- sum 3,405.467 s.

**Per-decile bulk ops/s** (10 deciles of 10,000,000 ops each; `build-
record.json`'s own `ops_per_s_by_decile` field was **null** for this run,
unlike the 10M/30M records -- flagged, not silently patched; the table below
is derived from `build-100m.stdout.log`'s progress ticks, same arithmetic
Stage 0/30M used):

| decile | ops range | ops/s |
|---:|---|---:|
| 1 | 0-10M | 10,199.9 |
| 2 | 10-20M | 8,689.6 |
| 3 | 20-30M | 6,159.5 |
| 4 | 30-40M | 4,908.2 |
| 5 | 40-50M | 4,044.0 |
| 6 | 50-60M | 3,433.8 |
| 7 | 60-70M | 2,960.9 |
| 8 | 70-80M | 2,602.9 |
| 9 | 80-90M | 2,334.1 |
| 10 | 90-100M | 2,038.3 |

**Median: 3,738.9 ops/s** -- well under the 30M run's 8,463.0 ops/s median,
consistent with D-164 (`compact()` materialises the whole store per call,
so cost grows with rows compacted, and grows faster the larger the store
already is).

**Compaction share of the build wall** (`build-100m-compaction-walls.json`)
-- the checkpoint log carries no native per-compaction timer for the 100
in-loop compactions, so their combined cost is *estimated* from
checkpoint-delta pairs (each 1,000,000-op cycle's second 500K-op half minus
its first half, a same-cycle steady-rate assumption): **23,194.3 s**
(range 29.1 s early / up to ~524 s late-cycle). Added to the authoritative
final-compaction phase (571.910 s): **23,766.2 s, or 83.76% of the total
28,372.936 s wall**, is compaction time. This does not reconcile cleanly
with the stdout ticks' own cumulative-elapsed gap at the finalization
boundary (786.2 s vs the authoritative finalisation-phase sum of
3,405.467 s, short by ~2,619 s) -- not root-caused here, flagged in the JSON
file's `instrumentation_cross_check_note` for the coordinator. The
authoritative `wall_s` and `finalisation_phases` (both from
`build-record.json`) are what is scored; the tick-based cross-check is
reported only as a discrepancy that did not close.

### Addendum 5 / 6 quantities and falsifier scoring

| quantity | band (Addendum 5/6, 100M) | measured | verdict |
|---|---|---:|---|
| build wall | 1.5 h - 5 h | 7.882 h | **REFUTED** (> 1.25x upper bound of 6.25h) -- but matches the coordinator's own pre-written expectation ("~8-10h total") exactly |
| steady-decile ops/s | > 10,000 | 3,738.9 | **TRIPPED** |
| build VmHWM (H1 vs H2) | H1 <= 55 GB; H2 180-200 GB (falsifier > 260 GB) | 186.42 "GB" (campaign convention; 177.79 true GiB) | **H2 named again, H1 refuted** -- see unit note below |
| query-ready floor VmHWM | 18-24 GB (falsifier > 25/60 GB) | 19.67 GB | **PASS** |
| `check --full` | healthy, <= 6 min | healthy, 336.353 s = 5.606 min | **PASS** (93.4% of the ceiling) |
| recovery (`--compact-every 5000`) | 3-6 h, digest equal | 22,717.685 s = 6.310 h, digest equal | **MISS-not-refuted** (5.2% over the upper bound; falsifier is > 2x upper = 12h, not tripped) |
| manifest bytes | <= 4 GB | 206,946 B | **PASS** |
| segment bytes | ~5.0 GB (falsifier > 2x) | 5.218 GB | **PASS** |
| `version_history` (kind=edge, belief=all) | <= 60 s / <= 10 GB (falsifier > 25 GB) | 18.982 s / 12.817 GB | wall **PASS**; VmHWM **MISS-not-refuted** (exceeds the 10GB point prediction, well under the 25GB falsifier) |
| `reach.window` admission | admitted unless `time_est_ms` > 10,000 | `time_est_ms=14,571`, refused | **anticipated** (Addendum 5's restated fallback branch -- honest outcome, not a failure) |
| `hist.single`/`hist.asof` p50 vs bar | <= 2.40 / <= 2.05 ms | 12.965 / 12.998 ms | **REFUTED** (~30x; both anchors were sub-millisecond, plausibly near fixed-overhead noise at the anchor point -- flagged, not resolved) |
| `paths.k`/`series.count`/`burst.zscore`/`motif.filtered` | per §2h multiplier | 20.164 / 291.521 / 294.233 / 110.861 ms | **PASS** (all 4) |
| `snap.hop2`/`diff.global`/`nbr.evolution`/`coactive.narrow`/`resolve.substr`/`agg.rel_bucket` | per §2h multiplier | refused (CostError, cost guardrail) | **new finding, not anticipated** -- only `reach.window`'s refusal was pre-registered; these six refusing at 100M was not predicted by any addendum |

**Unit note on "GB" for VmHWM figures.** This README (and the campaign's
existing Stage-0/30M records) report VmHWM "GB" as `vmhwm_kb / 1,000,000`
-- verified against the Stage-0 10M anchor (`vmhwm_kb=19,897,476` ->
Addendum 5's own "19.9GB" prose) -- not true decimal GB or GiB
(19,897,476 KiB is actually 20.87 true GB / 18.98 GiB). Using the
campaign's own established convention places this run's 100M build VmHWM
unambiguously inside the H2 180-200GB band; true GiB (177.79) would sit
just under the 180 lower bound. Recorded explicitly in
`build-100m.json`'s `falsifiers.build_vmhwm_h1_h2.unit_note` so the choice
is auditable, not silent.

**Scale-curve -- 6/13 executed, 7/13 refused.** Of the 6 that ran,
`paths.k`/`series.count`/`burst.zscore`/`motif.filtered` pass their
Addendum-5 bars; `hist.single`/`hist.asof` (both simple `entity_history`
point lookups) blow through theirs by ~30x. `reach.window`'s refusal
(`time_est_ms=14,571 > 10,000`) is the fallback branch Addendum 5
explicitly restated as an honest, non-failing outcome. The other six
refusals (`snap.hop2`, `diff.global`, `nbr.evolution`, `coactive.narrow`,
`resolve.substr`, `agg.rel_bucket`) were **not** anticipated by the design
doc -- at 30M all thirteen operators executed (some past their bars); at
100M the cost guardrail now refuses more than half the registry outright.
Full per-operator detail (bars, multipliers, measured p50/p95, refusal
estimates) in `scale-curve-100m.json`.

**Recovery.** `--compact-every 5000` fired 80 compactions over 400,262
replay batches (400,262 // 5000 = 80), replay wall 22,717.685 s = 6.310 h
-- just over the pre-registered 3-6 h band (not the 2x falsifier).
`digest_equal: true` (both source and replayed store hash to
`48bcb256...a8f2d97`). Unlike the 30M lane, there is no companion
500-cadence 100M run: Addendum 6 pre-judged that infeasible (~45h
extrapolated, over the 2-day Slurm wall, and would only re-measure D-164
again rather than answer a new question), so this is the campaign's single
100M recovery data point.

### Deviations from the literal Stage-1 recipe

1. **`build-record.json`'s `ops_per_s_by_decile` field was null** for this
   run (populated for the 10M/30M records). Not root-caused; the per-decile
   table above is derived from the stdout progress ticks instead, same
   arithmetic the campaign already uses elsewhere.
2. **Tick-based finalization gap does not reconcile with the authoritative
   finalisation-phase sum** (786.2 s vs 3,405.467 s, short by ~2,619 s).
   See `build-100m-compaction-walls.json`'s `instrumentation_cross_check_note`.
   Scoring uses the authoritative `build-record.json` figures throughout;
   the tick-based number is reported only as a cross-check that did not
   close.
3. **Six scale-curve operators refused by the cost guardrail at 100M**
   that were not predicted to refuse by `SCALE_BUILD_FORECAST_2026-09-15.md`
   (only `reach.window`'s refusal was pre-registered, in Addendum 5's
   second re-examination). See "Scale-curve" above.
4. **`hist.single`/`hist.asof` REFUTED by ~30x** against their Addendum-5
   bars -- both operators' 10M anchors were sub-millisecond (0.398/0.412 ms),
   so the multiplier-based bar (2.40/2.05 ms) is extremely tight; the
   100M measured values (12.965/12.998 ms) are plausibly dominated by a
   fixed per-call cost rather than true O(scale) growth, but this is not
   resolved here -- flagged for the coordinator.
5. **Recovery wall (6.310 h) exceeds the pre-registered 3-6 h band's upper
   bound by 5.2%** -- not the >2x falsifier, scored MISS-not-refuted.
6. **`version_history` VmHWM (12.817 GB) exceeds the <=10 GB point
   prediction** but stays well under the ~25 GB falsifier line -- scored
   MISS-not-refuted, not REFUTED.

No writer-lock races, no filename collisions, no contended/duplicate runs
occurred in this lane (unlike 30M's query-floor/scale-curve/version-history
first attempts) -- the dependency chain kept every job strictly serialized
on a store nothing else was touching.

### Files and provenance (sha256)

| file | sha256 |
|---|---|
| `build-100m.json` | `7eee07251a5e89bbedd6fc0c6f562729e298c885ea2dea6f1affa23151729ca8` |
| `build-100m.stdout.log` | `cb1d615c4279ee0d3a9fcdd29fde92f6366bb6bdef70981d7b324aec8aabadc9` |
| `build-record-100m.json` | `fd75d5afe84121584a3dd08f26c952f401eea24296565f1bc1f4c6f79ab844bf` |
| `build-100m-compaction-walls.json` | `d8bbadbfc44caa77804f65fee6e79dda1b58b5316a255b833866874c06793da6` |
| `rss-100m.jsonl` | `be3db7be7273777a54f77f12a22726d0d40fb5f93050d3f2126562ce685839c2` |
| `queryfloor-100m.json` | `b426aa5caf82ecbc4f05b3a2a12c8335ee218ea15e4b64209a97a677a9745d08` |
| `queryfloor-100m.stdout.log` | `d28d81ecd95f6d5b02482860341b3b2bf5bee7422ec20b53134eb16cfa8b023f` |
| `scale-curve-100m.json` | `8f39ca9a2b8508ce26d953d05303cc7aae46a47a8208853f4e981ce0933a3c83` |
| `scale-curve-100m-raw.json` | `bed58346dc34ea12f158868e7e12e86611e9d3291a4b552aef4ea52fa703dcd4` |
| `scale-check-100m.stdout.log` | `c7e30a096f4e24cc6b8732129442945e2a652ccffdf10728bbc566f27f4bd769` |
| `check-full-100m.json` | `00dac49960007364d8e1754bc6b201774bab15c16ac75f42ce4664b901d2756e` |
| `check-full-100m-raw.json` | `04de67ef5d62eab0916aff9546dcc2159da6fa7fdf8328d7d8efd612dadc8ef2` |
| `reach-admission-100m.json` | `1b111f052fe1a77c096b2a7eaa24e6bc05801cf1e3077829ab167aa601a31140` |
| `version-history-100m.json` | `08e2a5424dcfae09da0b03e5f47c6cb0fa0aa1c93f4b9680f78291684f9f6a28` |
| `version-history-100m-raw.jsonl` | `fbb72251683d1528697f86cbf22e4b98a861b328bc90a435aa0305f3e9c026d4` |
| `version-history-100m.stdout.log` | `61d08153b6be26579522a08e1b928e90b4560bbc602e0e3b17f227e125826e7f` |
| `recovery-100m-ce5000.json` | `6f6498b8559f31abb781004b7a6d6930467dd37d9ee3fcb2e4365fafad8c95e2` |
| `recovery-100m-ce5000.stdout.log` | `e99811104717cda715ce11957ec9bfd2b137df9bcd142dd6aebcde1ef80849e5` |
| `expB1-100m.json` (locally assembled, not cluster-transferred — see note below) | `d3eff4033ce5293c37c2e48cfbd07004bc7977b6a12ac5370ae5708f3acfcd71` |

Every file above except `expB1-100m.json` was sha256-verified byte-identical
between its source path on the cluster
(`/project/xzhang12/tgms-b7/stores/synth-100m-native/`
or `/home/xzhang12/b7-work-h/{logs,records}/`) and the copy transferred to
this worktree, at transfer time (not only at commit time); `expB1-100m.json`
was instead assembled locally, in this worktree, from those
already-verified records (see "`expB1-100m.json` — produced" below) — its
sha256 above is a tamper check on this worktree's own copy, not a
cluster-vs-worktree transfer check. The six schema-bearing records
(`build-100m.json`, `scale-curve-100m.json`, `check-full-100m.json`,
`recovery-100m-ce5000.json`, `version-history-100m.json`,
`expB1-100m.json`) validate against
`benchmarks/schema/result_manifest.schema.json`
(`scripts/check_result_manifest.py`, run via the cluster worktree's
`.venv` -- `jsonschema==4.26.0` -- since this laptop worktree carries no
Python env, same workaround the 30M lane used, plus this laptop's own
`.venv` for `expB1-100m.json` itself).

**`expB1-100m.json` — produced (records-only follow-up, same lane as the
30M column set).** The same §4 4-row column set, this time at 100M, from
`build-100m.json`, `queryfloor-100m.json`, `scale-curve-100m.json`, and
`version-history-100m.json` — no new measurement. Values: store on disk
**15.263 GB**; query-ready floor **19.67 GB** (job 213189, n_ok=6/13 — see
below); `version_history` **18.982 s / 12.817 GB** (job 213191, the same
figures already in the table above). The third row, columnar scan
(`aggregate_events`/`agg.rel_bucket`), is **null**: that operator is one
of the six refused-without-estimate operators at 100M (a new finding —
all 13 operators, including this one, executed at 30M, where it measured
176.492 ms); the record carries only a `CostError` string with no numeric
`time_est_ms`, so nothing is estimated in its place. Every field is cited
to its source file in `expB1-100m.json`'s own `exp_b1.rows.*.source`.

The cluster worktree `/project/xzhang12/tgms-b7`, its
`stores/synth-100m-native/`, and the replay copy
(`synth-100m-native-replayed-ce5000-213192`) are left in place on iTiger
(never deleted, per policy).

Slurm job IDs: 213188 (build), 213189 (query-floor), 213190
(scale-curve + check --full + reach-admission probe), 213191
(version_history), 213192 (recovery, `--compact-every 5000`).

Tool scripts used **unchanged** from the 30M lane (not recopied/recommitted
here -- same files, same sha256, already on record in this README's Stage
1 -- 30M section above): `scripts/query_floor.py`,
`scripts/version_history_probe.py`, `scripts/rss_sampler.py`.

Job scripts: `benchmarks/scale-v1/jobs/{build_100m,queryfloor_100m,
scale_check_100m,version_history_100m,recovery_100m_ce5000}.slurm`, each
transferred byte-identical from the cluster (sha256-verified both before
submission and again at commit time).

| script | sha256 |
|---|---|
| `benchmarks/scale-v1/jobs/build_100m.slurm` | `376643ee02eb817a5b85aca74dca4233d36c17f361c0293bc2c1324dd22a39ba` |
| `benchmarks/scale-v1/jobs/queryfloor_100m.slurm` | `9bee9f1e08f082cff1adbe2548302e1313b4ff9c5743db4960c4ce364190e6f3` |
| `benchmarks/scale-v1/jobs/scale_check_100m.slurm` | `52f94d06e1628de55c1bc88d5a3ac781947bc8ff63820204ae27c390d7363cc3` |
| `benchmarks/scale-v1/jobs/version_history_100m.slurm` | `6cba706098d7b704c4c87bcb6b06e10ba1dd9f78b0d00ebbb0dfdb49976c6c34` |
| `benchmarks/scale-v1/jobs/recovery_100m_ce5000.slurm` | `8083ab5a0787d17b803c0acde51e147db643ae3c1783494cd2a04a40326a22f1` |

### The §3 "100M is clean" acceptance checklist (informational -- the gate itself is moot)

Per `SCALE_BUILD_FORECAST_2026-09-15.md` §3, the PI's "300M dropped" ruling
means no 300M step is gated on this checklist; the seven conditions are
still scored here as the acceptance definition of the 100M record itself.
**Not all seven hold** -- reported as such, per §3's own instruction ("any
failure is reported as such"):

1. §2a not tripped x1.25 -- **TRIPPED** (build wall 7.882h > 6.25h)
2. §2b VmHWM <= 24GB / <= 60GB outer bar -- query-ready floor **PASS**
   (19.67GB); build VmHWM is a different row (H1/H2), not this one --
   H2 named, not falsified (<=260GB)
3. §2c <= 10GB/<= 60s -- wall PASS, VmHWM MISS-not-refuted (<=25GB held)
4. §2d <= 4GB manifest -- **PASS**
5. §2f exits 0 -- **PASS**
6. §2g recovery not tripped -- **MISS-not-refuted** (not the >2x falsifier)
7. §2h no operator over bar and `reach.window` refused as predicted --
   **NOT MET**: `hist.single`/`hist.asof` REFUTED, six operators refused
   unpredicted

**Verdict: the 100M point is reported as not fully clean** by the §3
definition -- two of seven conditions are outright tripped/not-met
(build-wall falsifier, §2h), three are MISS-not-refuted rather than clean
PASSes (§2c VmHWM, §2g recovery, and the general softness of "not tripped"
readings), and only §2b/§2d/§2f are unambiguous PASSes. This is reported
factually, per §3's instruction, not smoothed over.
