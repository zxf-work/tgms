# Scale-v1 benchmark records

Pre-registration: `docs/design/SCALE_BUILD_FORECAST_2026-09-15.md`, Addenda
4-6. Stage 0 (iTiger calibration, 1M/10M) is `itiger-calib-2026-09.README.md`
in this directory. This file covers **Stage 1** (30M now; 100M is a
separate, concurrently-running lane using `*-100m` file names — not
authored here, added by that lane once its own record lands).

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
