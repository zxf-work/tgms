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

## Files

- `overload-2026-09-15.json` / `.records.json` — rep1
- `overload-2026-09-15-rep2.json` / `.records.json` — rep2
- `overload-2026-09-15-recovery-lowrate.json` / `.records.json` — dedicated
  1-client/2 Hz/60 s step
- `rss-rep1.log`, `rss-rep2.log`, `rss-recovery-lowrate.log` — raw
  `ps -o rss=` samples
- `SHA256SUMS` — sha256 of every file above, verified identical between
  xzgpu and this checkout after transfer
