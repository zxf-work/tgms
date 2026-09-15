# overload-v1 — EXP-B4, the xzgpu-calibrated overload sweep (P-OV1)

`overload-2026-09-15.json` (+ `-rep2`, + the dedicated low-rate step) are
the committed records for **P-OV1** (plan §4.3b; protocol text
`docs/eval_concurrency.md` §24): does `tgms.tools.limits.ConcurrencyGate`
actually engage once open-loop callers outrun the service, and does the
service recover once load drops. Produced by `scripts/eval_overload.py`
against a fresh working copy of `stores/synth-1m-native`, opened
**read-only** by the harness.

## Environment

- **commit**: `ebe1dc2`, engine `build_info()`: `profile=release`,
  `debug_assertions=False`, `manifest_format_version=3` (verified before
  the run)
- **host**: xzgpu (`Linux-5.4.0-216-generic-x86_64`, 40 cpus, 93 GB RAM) —
  quiet host verified before and after every rep: no
  `longevity_run`/`eval_*`/`bench_*`/`tgms replay` processes, no ambient
  `tar`/`rsync`/`sha256sum`, OSV poller (pid 1457823) and the daily loop
  (pid 1540052) untouched, 414 GB free under `/mnt/project`
- **store**: working copy of
  `/mnt/project/xzhang/tgms/work/tgms/stores/synth-1m-native` under
  `$TMPDIR/ov1-store`, `tgms store upgrade-manifests` run on the copy
  before measurement: **manifest format 1 → 3** (generation 21,
  `sha e7f45e1257dd14c8`); the sweep itself opens this copy read-only and
  issues no commits
- **known commit-path memory-growth defect** (≈160 KB/commit, under repair
  on `main`): **not applicable to this run** — the harness never commits;
  the store handle is read-only for the whole sweep
- **gate cap**: `--max-concurrent` was left at `scripts/eval_overload.py`'s
  own CLI default, **8** — `tgms serve` itself carries *no* concurrency
  cap by default (`ToolRouter`/`Limits.from_env()` leaves `max_concurrent`
  unset unless `TGMS_MAX_CONCURRENT` is exported), so 8 is the harness's
  documented default under test, not a value read from `tgms serve`
- **op under load**: `entity_history` (`uid="n16945"`, `limit=5`)
- **dataset digest** (`store.digest()`, identical across all three runs
  below, confirming the same store state throughout):
  `682f1194f6ca335fd60bda56b3e0630d9d88112651e109067de222e96d9cd51a`
- schema-valid via `scripts/check_result_manifest.py` (all three manifests)

## Protocol

`--clients 1 2 4 8 16 32 64 --duration-s 60 --rate-per-client 20
--max-concurrent 8`, 2 reps. **The harness does not merge reps** — each
invocation writes its own manifest, so this is reported as two separate
manifests (`overload-2026-09-15.json` = rep1, `overload-2026-09-15-rep2.json`
= rep2) rather than one averaged record. The harness's own built-in
"recovery" step reuses the same per-client rate (20 Hz) at `n_clients=1`,
which is not a distinct low-rate condition from the sweep's own first step
(same clients, same rate) — so per the brief, a **separate** dedicated
1-client/2 Hz/60 s step was also run and recorded
(`overload-2026-09-15-recovery-lowrate.json`).

## Per-step results — rep1

| clients | calls | ok | refused (cap) | refused (size) | error | qps | p50 ms | p95 ms | p99 ms | late p95 ms | admitted p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 150 | 150 | 0 | 0 | 0 | 20.13 | 1.44 | 1.68 | 2.06 | 115.68 | 0.0 |
| 2 | 300 | 300 | 0 | 0 | 0 | 40.25 | 1.46 | 1.72 | 1.76 | 0.13 | 0.0 |
| 4 | 600 | 600 | 0 | 0 | 0 | 80.46 | 1.43 | 1.52 | 1.60 | 0.13 | 0.0 |
| 8 | 1200 | 1200 | 0 | 0 | 0 | 160.74 | 1.42 | 1.48 | 1.55 | 0.13 | 0.0 |
| 16 | 2400 | 2400 | 0 | 0 | 0 | 320.92 | 1.45 | 1.51 | 1.65 | 1.48 | 0.0 |
| 32 | 4800 | 4800 | 0 | 0 | 0 | 635.69 | 1.54 | 23.19 | 40.39 | 31.68 | 0.0 |
| 64 | 9600 | 5162 | 4438 | 0 | 0 | 508.08 | 23.13 | 113.09 | 279.99 | 608.90 | 8.0 |
| recovery (built-in, n=1@20 Hz) | 150 | 150 | 0 | 0 | 0 | 20.13 | 1.43 | 1.62 | 1.69 | 0.13 | 0.0 |

## Per-step results — rep2

| clients | calls | ok | refused (cap) | refused (size) | error | qps | p50 ms | p95 ms | p99 ms | late p95 ms | admitted p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 150 | 150 | 0 | 0 | 0 | 20.13 | 1.42 | 1.61 | 2.14 | 77.69 | 0.0 |
| 2 | 300 | 300 | 0 | 0 | 0 | 40.25 | 1.43 | 1.50 | 1.63 | 0.13 | 0.0 |
| 4 | 600 | 600 | 0 | 0 | 0 | 80.46 | 1.42 | 1.48 | 1.62 | 0.14 | 0.0 |
| 8 | 1200 | 1200 | 0 | 0 | 0 | 160.73 | 1.42 | 1.48 | 1.58 | 0.13 | 0.0 |
| 16 | 2400 | 2400 | 0 | 0 | 0 | 319.79 | 1.44 | 1.52 | 2.00 | 1.38 | 0.0 |
| 32 | 4800 | 4800 | 0 | 0 | 0 | 635.58 | 1.54 | 18.96 | 32.88 | 27.78 | 0.0 |
| 64 | 9600 | 9315 | 285 | 0 | 0 | 684.98 | 16.71 | 333.75 | 659.46 | 5439.44 | 7.0 |
| recovery (built-in, n=1@20 Hz) | 150 | 150 | 0 | 0 | 0 | 20.13 | 1.41 | 1.60 | 1.67 | 0.13 | 0.0 |

Refusal kind: `max_rows`/`max_bytes` are unset in this harness's `Limits`
(only `max_concurrent` is configured), so `check_result_limits` never runs
a size check — structurally, every refusal observed carries
`limit_kind="concurrency"`; result-size refusals and operator errors are
both 0 in both reps. Truncations: 0 in both reps (D-155: refuse, never
truncate — enforced by construction, not just observed).

## Dedicated low-rate step (1 client, 2 Hz, 60 s)

| clients | calls | ok | refused | error | qps | p50 ms | p95 ms | p99 ms | late p95 ms | admitted p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 120 | 120 | 0 | 0 | 2.02 | 1.60 | 1.75 | 2.24 | 0.14 | 0.0 |

Recovery step vs. the 1-client step (same-rep, built-in recovery vs. that
rep's own `n=1` step): rep1 qps 20.13 vs 20.13 (0% diff), p50 1.43 ms vs
1.44 ms; rep2 qps 20.13 vs 20.13 (0% diff), p50 1.41 ms vs 1.42 ms.

## Harness process RSS (`ps -o rss=`, sampled ~1 Hz across the run)

| rep | baseline (post-load) | during n=32 | during n=64 | end of run |
|---|---:|---:|---:|---:|
| rep1 | 135.3 MB | 152.8–171.3 MB | 412 MB → 1.97 GB | 1.69 GB |
| rep2 | (same store, same shape) | similar | rises to 2.01 GB | 2.01 GB |
| low-rate (1 client, 2 Hz) | ~139 MB | n/a | n/a | ~139 MB |

The growth tracks in-memory record volume (9,600 `CallRecord`s plus
~10 ms-interval in-flight samples held for the whole sweep until the
manifest/records file is written at the end), concentrated in the
`n_clients=64` step — not a per-commit leak, consistent with the
read-only, no-commit environment note above.

**Attribution diagnostic (2026-09-15, xzgpu, quiet host reverified).** To
tell harness bookkeeping apart from the service surface
(`ToolRouter`/`ConcurrencyGate`/envelope retention), the `n_clients=64`
step was re-run alone (60 s, fresh store copy, no sweep around it) with
`tracemalloc` started before the step and snapshotted after
(76,800 calls: 13,847 ok, 62,953 refused — a harsher single-step version
of the sweep's n=64 cell, run this way only to get a clean before/after
diff). All 8 of the top allocation sites by traced size are in
`scripts/eval_overload.py` (the harness), none in `tgms/…`:
`eval_overload.py:165` (`local.append(CallRecord(...))`, 7.99 MB /
153,603 objects), `:158` and `:157` (the `late_ms`/`wall_ms` float
temporaries per call, 1.84 MB / 76,800-76,801 objects each), and
`:167` (the per-client `records.extend(local)` list growth, 0.69 MB).
The retained-list footprint matches directly: 76,800 `CallRecord`s ×
~142 bytes each (`sys.getsizeof` on a 200-record sample, instance +
its `outcome`/`refusal_stage` strings) ≈ 10.9 MB. Two caveats on reading
this as the full explanation for the sweep's multi-GB RSS: (1) this
isolated step's own `tracemalloc` peak was only 83.48 MB (current
14.19 MB) — two orders of magnitude below the 1.7-2.0 GB `ps` RSS the
sweep reached at the same step, even though this run issued *more*
calls (76,800 vs. the sweep's 9,600 at that step); (2) `tracemalloc` only
sees CPython-heap allocations, not the native `tgms._engine` (Rust)
extension's own heap or glibc arena retention under heavy thread churn
(64 client threads spawned per step). So the identified top sites are
real and 100% harness-side, but they account for at most a low-double-digit
MB share of the sweep's observed RSS growth — the multi-GB majority is not
attributable to any specific line by this method and is most plausibly
native-heap/allocator behavior outside `tracemalloc`'s visibility, not
confirmed as either harness or service code.

## Note on `dev_host_note`

Every manifest listed below (`overload-2026-09-15.json`, `-rep2`, and
`-recovery-lowrate.json`) carries a `dev_host_note` field reading
"measured on a development host for functional verification ... NOT a
reported benchmark result ...". That text was hard-coded into every
manifest `scripts/eval_overload.py` produced, regardless of which host
ran it — it is not an assessment of this run in particular, and it reads
as false here: each manifest's own `machine.host` field says `"xzgpu"`
(40 CPUs, 93 GB RAM, quiet-host verified — see Environment above), and
the coordinator's plan scores these three manifests as **P-OV1's
calibrated run**, not a dev-host smoke test. The harness has since been
fixed (commit `8c125f9`, "eval_overload: require explicit --provenance,
drop hard-coded dev_host_note"): `dev_host_note` is now emitted only when
a caller explicitly passes `--dev-host-note`, and every manifest carries
a required `provenance` string instead of relying on an unconditional
default. The records here predate that fix and are left byte-identical —
this paragraph is the correction, not a rewrite of the data.

## Heap attribution follow-up: harness bookkeeping or the service surface?

The tracemalloc diagnostic above narrowed the sweep's multi-GB `n_clients=64`
RSS growth to "not more than a low-double-digit MB share is harness Python
bookkeeping" but could not rule *in* the service surface, since
`tracemalloc` cannot see the native `tgms._engine` heap. This follow-up
(2026-09-15, xzgpu, `tgms-xz-a6b3e94` worktree — release engine,
`build_info()`: `profile=release`, `debug_assertions=False`,
`manifest_format_version=3`) closes that gap directly: `scripts/eval_overload.py`
gained an additive `--no-call-records` flag (drops the per-call `CallRecord`
list entirely, keeping only running aggregates — counts and a bounded
4096-sample latency reservoir) and an `--rss-samples PATH` flag (once-per-second
whole-process `VmRSS` from `/proc/self/status`, `time,rss_kb,step` CSV). The
`n_clients=64` step was re-run alone twice against a fresh `upgrade-manifests`'d
copy of `synth-1m-native` (format 1 → 3, generation 21, `sha e7f45e1257dd14c8`,
`store.digest()` `682f1194f6ca…` — identical to the digest above, same store
state), 60 s each, `--max-concurrent 8 --rate-per-client 20`, with
`tgms replay` (REPLAY-2, correctness-only, pid 2453943) confirmed running
concurrently on the host — fine for an RSS measurement, not a latency one.

| run | peak RSS (`/usr/bin/time -v`, `Maximum resident set size`) | n_ok / n_refused of 76,800 |
|---|---:|---:|
| with call records (the P-OV1 way) | 1,991,888 KB (≈1.90 GB) | 29,814 / 46,986 |
| `--no-call-records` | 2,004,096 KB (≈1.91 GB) | 31,283 / 45,517 |

**Verdict (v1, HELD — see the amendment below): the growth is in the
service surface, not harness bookkeeping.** Dropping the harness's own
per-call retention changed peak RSS by about 0.6% — the `--no-call-records`
run is if anything marginally *higher*, within run-to-run noise, not lower.
If the multi-GB growth were the harness's `CallRecord` list (and its
end-of-sweep JSON serialization), removing that retention should have
collapsed the peak toward the ~150–200 MB baseline the 1-client steps show;
it did not. `postings_stats("edge")` and `segment_cache_stats()` were
probed before/after each run (via the adapter, read-only) and are
identically zero throughout in both reps, ruling out the edge-postings
index and the byte-budget segment cache as the destination. A grep of
`tgms/tools/server.py`/`tgms/tools/limits.py` finds no accumulating
list/history field on `ToolRouter` or `ConcurrencyGate` either — neither
class retains past call results, so literal "result retention" by the
router or gate is also ruled out. That leaves the native engine
(`tgms._engine`, the Rust `.so`) or glibc allocator behavior under 64
concurrently-calling threads as the leading candidate; this diagnostic
identifies where the growth is *not*, not the exact native allocation site
(that needs native-side profiling — Valgrind/massif or per-thread RSS
breakdown — out of scope for this bounded measurement).

One caveat on the `--rss-samples` series itself: in both reps the 1 Hz
`VmRSS` CSV stays flat near baseline (~190–196 MB) for the whole 120 s run,
an order of magnitude below the same run's `/usr/bin/time -v` lifetime peak.
This says the ~2 GB is a fast-appearing, fast-receding spike (consistent
with transient mmap-backed native allocations, or per-thread malloc-arena
churn across 64 threads) rather than a value that climbs and holds — visible
to the kernel's lifetime peak accounting, invisible to once-a-second
polling. Full numbers, before/after stats, and the sampler-discrepancy note
are in `heap-diagnostic-2026-09-15.json`.

### Amendment v2 (2026-09-15, superseded — see "Pinned" below)

The v1 verdict above was **held**: `/usr/bin/time -v` reports a
process-*lifetime* peak, while the 1 Hz series sat at baseline through the
whole step, so the ~2 GB could belong to a pre-step phase (store
copy/`upgrade-manifests`/open) rather than to the service under load — the
v1 between-run comparison alone couldn't tell the two apart. A second
bounded run (`--no-call-records`, otherwise identical protocol, same store
state — `upgrade-manifests` format 1 → 3 generation 21
`sha e7f45e1257dd14c8` again) added `--hwm-checkpoints` (`VmHWM`, the
lifetime peak-so-far, which — unlike a `VmRSS` poll — cannot miss a spike
that has already receded) at four points, plus a 10 Hz `--rss-samples`
series:

| checkpoint | `VmHWM` (kB) |
|---|---:|
| after imports | 40,752 |
| after store open (post-upgrade) | 99,488 |
| immediately before the 64-client step | 99,488 |
| immediately after the 64-client step | 226,928 |

Same run's `/usr/bin/time -v` peak: **2,016,400 KB (≈1.92 GB)**. The 10 Hz
`VmRSS` series stays flat (~195–220 MB) through the step and the whole
low-rate recovery step that follows it.

**Both the original framing and the coordinator's alternative are now
ruled out.** Store open/upgrade is cheap (99,488 KB) — not a "startup
footprint" of ~1.9 GB. And `VmHWM` right after all 64 client threads join
is only 226,928 KB — since `VmHWM` never decreases, the step itself never
drove RSS anywhere near 2 GB while its threads were alive, contradicting
v1's "growth under 64-client load" framing too. The v1 `--no-call-records`
ablation still stands on its own narrower claim (the ~2 GB isn't the
harness's `CallRecord` list), but the growth's *location* is now known to
be neither store-open nor the load step's execution — it opens somewhere
in the remaining ~1.79 GB gap, during the recovery step and/or
`store.digest()`/`store.close()`/teardown, a phase the 10 Hz poller also
fails to see (flat throughout). **Verdict: still service/native-side, not
harness bookkeeping, but not the phase either prior hypothesis named —
localizing it to "recovery" vs. "teardown" needs two more checkpoints, not
run here to hold to the bounded measurement budget.** Full detail in
`hwm_checkpoint_followup_v2` in `heap-diagnostic-2026-09-15.json`.

### Pinned (2026-09-15)

A third bounded run added the two missing checkpoints — after the recovery
step, and after each of the harness's own finalization calls
(`store.digest()`, then `store.close()`) — plus one just before the sweep
returns:

| checkpoint | `VmHWM` (kB) |
|---|---:|
| after the 64-client step | 227,280 |
| after the recovery step | 227,280 |
| after `store.digest()` | **1,999,968** |
| after `store.close()` | 1,999,968 |
| before exit | 1,999,968 |

Same run's `/usr/bin/time -v` peak: **1,999,968 KB** — identical, kB for
kB, to the `after_store_digest_full` checkpoint. The recovery step adds
exactly 0 KB; `store.digest()` adds 1,772,688 KB in one call; nothing
after it adds anything.

**Pinned verdict: the service surface `VmHWM` stays ≈227 MB through the
entire 64-client step (and the recovery step that follows it) — the
1.9 GB lifetime peak belongs to `scripts/eval_overload.py`'s own
end-of-sweep `store.digest()` call**, not to `ToolRouter`/`ConcurrencyGate`
under load, not to the recovery step, and not to per-call `CallRecord`
retention (the v1 ablation's finding stands, just not for the reason v1
assumed — `store.digest()` is itself a harness-side finalization call, so
"not harness bookkeeping" was wrong in scope even though "not the
`CallRecord` list" was right). `Store.digest()`
(`tgms/storage/base.py::store_digest`) materializes every node/edge
version row into a sorted Python list before hashing — its cost scales
with total row count, not with anything the load actually did, which is
exactly why disabling `--no-call-records` never moved the peak (v1) and
why the peak was already fully formed the instant `store.digest()`
returned (v3).

**The fix:** `Store.digest_streaming()` (`tgms/store.py`, backed by
`StorageAdapter.store_digest_streaming` in `tgms/storage/base.py`) landed
on `main` via the B7a streaming-digest work (commits
`c5c03a9`/`3731a67`, merge `4930213`) after this lane's branch point
(`23bf664`) — proved byte-identical to `store.digest()`
(`tests/test_store_digest_streaming.py`) and bounded-memory by
construction (an external merge sort over spilled, `chunk_rows`-sized
batches instead of materializing every row). `scripts/eval_overload.py`
now takes an additive `--digest-mode {full,streaming}` flag (default
`full`, unchanged behaviour); `streaming` calls `digest_streaming()`
instead, and on a checkout that lacks the method — including this lane's
own branch, which predates the merge — it raises a clear `RuntimeError`
naming the missing method and the commits that add it, rather than
silently falling back to `full` and mislabeling the manifest.
`digest_mode` is recorded in every manifest's `config` for provenance.
Not exercised live here (this branch doesn't have `digest_streaming` yet);
the flag is landed and ready to flip once this lane rebases onto or merges
a `main` that includes it. Full detail in `hwm_checkpoint_pinning_v3` in
`heap-diagnostic-2026-09-15.json`.

## Files

- `overload-2026-09-15.json` / `.records.json` — rep1
- `overload-2026-09-15-rep2.json` / `.records.json` — rep2
- `overload-2026-09-15-recovery-lowrate.json` / `.records.json` — dedicated
  1-client/2 Hz/60 s step
- `rss-rep1.log`, `rss-rep2.log`, `rss-recovery-lowrate.log` — raw
  `ps -o rss=` samples
- `heap-diagnostic-2026-09-15.json` — the `--no-call-records` RSS
  attribution follow-up (see above)
- `step64-with-records.json`, `step64-no-call-records.json` — the two
  harness manifests from that follow-up (schema-valid,
  `scripts/check_result_manifest.py`)
- `rss-with-records.csv`, `rss-no-call-records.csv` — the two
  `--rss-samples` series from that follow-up
- `step64-no-call-records-v2.json`, `rss-no-call-records-10hz.csv`,
  `hwm-checkpoints.json` — the held/amended phase-localization re-check
  (see "Amendment v2" above, superseded)
- `step64-no-call-records-v3.json`, `rss-no-call-records-v3-10hz.csv`,
  `hwm-checkpoints-v3.json` — the pinning run (see "Pinned" above)
- `SHA256SUMS` — sha256 of every file above, verified identical between
  xzgpu and this checkout after transfer
