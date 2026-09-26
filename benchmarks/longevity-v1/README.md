# longevity-v1 — Lane B task B7a, the 24h soak (Gate G1)

`longevity-synth-1m-native-0.json` is the committed record for the real
(non-dev-host) 24h longevity soak: `scripts/longevity_run.py`, commit
`886805f` (pinned worktree `/mnt/project/xzhang/tgms/work/tgms-xz-886805f`
on xzgpu), against `stores/synth-1m-native`, `--mix balanced --readers 8
--compact-every-batches 500 --compact-min-interval-s 5
--reader-reopen-every-s 300 --restart-every 30m --artifacts 500 --seed 0
--max-disk-mb 20000`, launched 2026-09-14 ~05:15 UTC, `RUN_DONE` at
2026-09-15 05:16:02 UTC. Everything under `--out` stayed on xzgpu's own
project storage (`/mnt/project/xzhang/tgms/longevity/2026-09-15/`), never
this repository's working tree, per the doc's own PI ruling — this
directory holds only the manifest and the small side-record files (a few
KB to a few hundred KB each); `metrics.jsonl` (33 MB) and the 20 GB store
copy stay on xzgpu.

- **commit measured**: `886805f` (pre-fix for D-086-reader-torn-tail-race —
  see below)
- **host**: xzgpu, 40 CPUs, 93 GB RAM, Linux 5.4.0-216-generic
- **wall clock**: 86,523.3 s (24h 2m 3s; the ~2m over the nominal 86,400 s
  duration is final `verify()` + the disk-guard-gated replay check +
  manifest write, after the soak's own deadline)
- **manifest**: conforms to `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — exit 0)
- **manifest sha256**:
  `a94a0c2c3d343d84161c911f04c6a29b586a4b3ddfc113bffb38549d988b3500`
- **two flags I was explicitly given that differ from `docs/
  eval_concurrency.md` §24's own coordinator example line**: `--artifacts
  500` (doc example: 20) and `--max-disk-mb 20000` (doc example: 512000).
  Both were used as instructed. The disk ceiling choice turned out not to
  matter for the one thing it gates here (see "digest_equal" below): the
  actual run needed a ~547x larger ceiling than even the doc's own
  512,000 MB to avoid the replay skip, so raising it to 512,000 would not
  have changed the outcome.

## Gate E table

See `gate_e_report.md` (written by `scripts/longevity_report.py`, run
locally/read-only against the fetched manifest — no measurement code ran
locally).

| check | verdict | detail |
|---|---|---|
| deterministic final state (verify() clean + replay digest equality) | **FAIL** | verify_healthy=True digest_equal=**not computed** (see below — this is not "found non-deterministic", it is "never checked") |
| bounded metadata growth (manifest bytes slope) | PASS | manifests -3.4 B/s, segments 1,341.1 B/s |
| no unbounded memory (RSS slope) | **FAIL** | 111.639 kB/s (see caveat below) |
| throughput/latency drift (first hour vs. last hour) | PASS | throughput 24.03 -> 16.41 commits/s, p99 485.6 ms -> 3,740.5 ms |
| compaction stalls | info | 0.000 ms max reader p99 during a compaction window |
| errors observed | **FLAG** | manifest says **1** — this number is wrong; the real count is **249** (see below) |
| recoveries / reader restarts | info | 41 / 2 (both broken down below) |

**Read the FAIL/FLAG rows with the caveats below before treating them as
findings about the engine.** Two of the four non-PASS rows are the
harness's own reporting artifacts, not (necessarily) engine behavior; one
is a real, substantive result; the fourth is real but needs its own
caveat.

## `digest_equal`: not computed, not "false" — and why

`scripts/longevity_run.py::cmd_run` initializes `digest_equal: bool | None
= None` and only ever assigns it inside `if do_replay: ... digest_equal =
replay_digest == final_digest`. This run's own log:

```
LEDGER disk_guard_replay_skip: total_batches=1074952, projected_mb=280791798.0, current_mb=569.7, limit_mb=20000.0
SKIPPING final replay: 1074952 uncompacted batches would project to ~280791798 MB of manifests (D-149's own O(batches^2) pathology), which would push --out over --max-disk-mb=20000; digest equivalence not checked this run.
```

`do_replay` was `False` because the projected replay cost (280,791,798 MB,
i.e. ≈280.8 TB — not the "~268 PB" an earlier version of this note said, a
units error off by roughly 1000x, corrected 2026-09-15; computed by
`scripts/longevity_run.py::cmd_run`'s own `projected_mb = (243.0 *
(total_batches ** 2)) / 1e6`, D-149's O(batches²) manifest-growth
pathology the harness's own module docstring names — quadratic because
`tgms.storage.eventlog.replay` has no mid-replay compaction hook, so every
uncompacted batch in the *whole* event log replays into its own manifest
regardless of how often this run's own `--compact-every-batches 500` fired
live — applied to this run's real 1,074,952 uncompacted batches at ~243
bytes/batch²) would have blown through `--max-disk-mb=20000` by roughly
four orders of magnitude (~14,000x), so the mandatory final
replay/digest-equivalence step never ran. `digest_equal`
therefore stayed `None` — "not computed", never "computed and found equal
to False" — and `manifest["summary"]["digest_equal"]` is `null` in the
JSON, not `false`.

`scripts/longevity_report.py::build_table` does `digest_equal =
bool(s.get("digest_equal"))`, which maps `None` to `False` — so the Gate E
table's "deterministic final state" row reads **FAIL** with
`digest_equal=False` regardless of whether the replay ran and found a
mismatch, or never ran at all. **This does weaken Gate E**: the harness
provides no signal at all on replay/digest equivalence for a run whose
batch count is this large under a 20 GB ceiling — not "checked and
passed", not "checked and failed", genuinely absent. `verify_healthy=True`
(the final store's own internal consistency check) is unaffected and did
pass. The 1,074,952-batch count itself is a direct consequence of `mix
balanced`'s `writer_sleep_s=0.0` (effectively unthrottled writer) over 24h;
a future run that wants the replay/digest check to actually run at this
duration needs either a much larger `--max-disk-mb` (real requirement here
is >280 TB, not a realistic ceiling to raise to) or `--writer-sleep-s` to
bound the batch count, per the harness's own module docstring on this
exact tradeoff.

**2026-09-15 follow-up, after this record was measured**: the check is
feasible from this commit on, without either workaround.
`tgms.storage.eventlog.replay` gained a `compact_every` cadence (B7c),
proved digest-preserving (`tests/test_replay_compaction.py`), and
`scripts/longevity_run.py::cmd_run` now uses it by default for the final
replay, projecting the disk-guard cost as one compaction cycle's own peak
rather than the whole run's uncompacted total. This run's own numbers
(1,074,952 batches, `--compact-every-batches 500`) project to ≈60.75 MB
under that formula — comfortably inside `--max-disk-mb=20000` — so a
future run of this same shape would have its digest-equivalence check
actually run. This is a statement about what a future run can now do, not
a re-measurement of this record, which stays exactly as measured at
`886805f`, a commit that predates `compact_every` entirely.

## `errors observed`: manifest says 1, the real total is 249

`scripts/longevity_run.py::summarize()` aggregates writer-side counters
(`writer_errors_total`, `appends_total`, `corrections_applied_total`,
`corrections_skipped_total`, `artifact_checks/invalidations/refreshes_total`,
`compactions_throttled_total`) via `counter_latest`, keyed only on
`(metric_name, labels_json)`. Every writer life reports these under the
same **unlabeled** key (no life index in the label set), and
`counter_latest` keeps only the sample with the latest timestamp for a
key — so for any run with writer restarts, this silently discards every
life's counters but the last one's. This run had 42 writer lives
(0 through 41); `manifest["summary"]["error_count"]` (1) and
`manifest["summary"]["writer_final"]` are both **life 41 only**, not a
run total, even though `error_count` is presented in the manifest and in
`longevity_report.py`'s table as a whole-run figure.

`writer_error_counts_by_life.json` in this directory recomputes the true
totals directly from the 42 `writer_progress-<life>.json` files (each
life's own final counters, before it was overwritten by the next
restart):

- **true total writer errors, all 42 lives: 249** (not 1)
- true total appends: 853,817; corrections applied: 207,850; corrections
  skipped: 137,163; artifact checks/invalidations/refreshes: 3,284 / 1,264
  / 1,263
- per-life error counts range from 0 to 15 (median well above 1) — this
  was a routine, recurring event roughly every few minutes of writer
  uptime, not a single blip

The Gate E verdict happens to be FLAG either way (1 ≠ 0 already flags),
but "1" understates the frequency by ~250x and would mislead anyone
reading it as "an isolated hiccup." **Root cause of these 249 errors is
not recoverable from this run's own data**: `child_writer`'s
`do_append`/`do_correction` call sites catch `Exception` and increment the
counter without logging the exception type or a traceback (unlike the
reader's per-query path, which does label errors by `type(e).__name__`) —
an instrumentation gap, not just an aggregation one. I have not changed
`scripts/longevity_run.py` (this run's own measured commit, 886805f, is
pinned and should not move); a fix to the per-life counter-collision bug
and the missing exception-type labels belongs in a follow-up on `main`.

## Recoveries (41) — writer restart-cycle kills, by cause

All 41 are the harness's **own designed** restart cycle (`--restart-every
30m`, a `TGMS_CRASH_POINT` at a random `eval_durability.py` boundary),
confirmed in `recoveries.jsonl` (`"kind": "designed"` on every entry) and
by `manifest["summary"]["unexpected_writer_deaths"] == 0`:

| returncode | meaning | count |
|---|---|---:|
| `-6` (SIGABRT) | a Rust engine crash point (`crates/tgms-engine-core/src/store.rs::crash_point`, `std::process::abort()`) | 23 |
| `137` (128+SIGKILL, `os._exit(137)`) | a Python-side crash point (`tgms/storage/crashpoint.py`) | 18 |

Every one of the 41 recovered cleanly (`_reopen_rw_with_retry` +
`verify()` succeeded each time; recovery time 14.1s-27.2s). 41 restarts
over ~24h at a 30-minute cadence is close to the naive ceiling of 48
(86,400s / 1,800s) — the shortfall is expected: the last one or two
30-minute windows near the run's end either landed after the final control
signal was superseded by the run's own deadline, or the writer's final
life simply ran out its clock before the next scheduled kill fired.

## Reader restarts (2) — NOT part of the designed restart cycle

Both are reader-process deaths, distinct from the writer's restart cycle
(the orchestrator restarts a dead reader unconditionally, "never treated
as a run failure by itself" per the harness's own module docstring), both
`returncode=1` (an uncaught Python exception), both during a reader's
periodic handle reopen (`--reader-reopen-every-s 300`):

| reader idx | died at (UTC) | eventlog offset | traceback |
|---|---|---:|---|
| 6 | 2026-09-15T03:15:04Z | 300,871,482 | `StateError: ... is not readable at offset 300871482: Expecting ':' delimiter: line 1 column 114 (char 113) — the replay cursor may not be on a record boundary` |
| 5 | 2026-09-15T04:44:47Z | 311,080,182 | `StateError: ... is not readable at offset 311080182: Expecting ':' delimiter: line 1 column 114 (char 113) — the replay cursor may not be on a record boundary` |

**Reader 6's death is `ops/failure_ledger.jsonl`'s `D-086-reader-torn-tail-race`
entry's `observed_in_the_wild` instance**, already recorded on `main`
(commit `653eeaa`, landed 2026-09-14 22:37 -0500 = 2026-09-15T03:37Z, i.e.
*while this soak was still running*): "reader 6 reopening on its 300s
cadence hit the writer's in-flight record at eventlog offset 300871482
during `Store.__init__ -> _seed_frontier -> batches_from(0)` and died with
`StateError` (1 hit in ~2,100 reader reopens over 22h); the orchestrator
restarted it; the store was undamaged." The fix (series `ef97d2d`,
`43f6ef4`, final `3a664a8`, all already on `main`, **not** in this run's
pinned commit `886805f`) requires **two conditions together** before a
torn final record is tolerated by a read-only opener
(`Store._compute_reader_torn_tail_floor`): (1) the torn record starts at
or after the manifest's own applied event-log offset
(`tolerate_torn_tail_from`, the same value `trim_torn_tail(applied_offset)`
uses for a writer), **and** (2) some process currently holds
`writer.lock` (`Store._writer_lock_is_held`, a non-blocking `flock`
probe) — a reader never holds this lock itself, so finding it free proves
nothing could still be appending. An earlier, unconditional-tolerance fix
attempt (`ef97d2d` alone) regressed
`tests/test_eval_corruption.py::test_torn_event_log_tail_is_detected_even_read_only`
because a corruption sweep's garbage-append onto a *closed* store is
byte-for-byte indistinguishable from a live writer's not-yet-fsynced tail;
the second (writer-lock) condition is what tells them apart. This run's
own store was confirmed undamaged (`verify_healthy: true`).

**Reader 5's death is the same failure shape** (same `StateError` class,
same "not on a record boundary" message, same periodic-reopen trigger,
~1h30m later) but is **not** named in the ledger's `observed_in_the_wild`
note, which mentions only reader 6. I did not edit
`ops/failure_ledger.jsonl` myself (it is being actively maintained on
`main` by the parallel fix work, and this pinned worktree's job is to
measure at a fixed commit, not to edit `main`), but reader 5's death
should be treated as a second, undocumented occurrence of the same D-086
race rather than a separate finding, and the ledger entry's
`observed_in_the_wild` note is incomplete on this point.

## Memory (RSS slope): a real FAIL, with a likely (unconfirmed) explanation

111.639 kB/s over the whole run is not a two-point artifact of noise: the
writer's raw `rss_kb` gauge series (1,329 points, all 42 lives
concatenated) shows 41 drops of more than 30% — one at almost exactly each
of the 41 restarts — i.e. RSS **saw-tooths on every writer restart**
(each life is a fresh OS process). `metadata_growth_slope_bytes_per_s` is
computed the same crude way (`(last - first) / (t_last - t_first)`, no
regression, no detrending), so a saw-tooth signal's "slope" is dominated
by wherever the first and last sample happen to land, not a fitted trend.
That said, the *overall envelope* does climb across the run (first
writer-life peak ~1.8 GB, last life's ~9.8-15.5 GB), and this is plausibly
explained by the store's own growth: `final_stats.n_entities` grew to
1,730,492 and `n_node_versions`/`n_edge_versions` to 241,109 /
1,910,268 over the run, and the native backend's resident index (D-045)
scales with entity count — a larger store means a larger baseline resident
footprint for every *fresh* writer process, which would show up as
climbing per-life peaks with **zero** true per-process leak. I have not
fit peak-RSS-per-life against store size to confirm this, so I am not
claiming this explains all of it — only that "unbounded memory" as a
verdict conflates two very different claims (a real leak within a
long-lived process vs. a growing dataset's baseline footprint across many
short-lived processes), and this harness's two-point slope cannot tell
them apart. This is worth a dedicated per-life-peak-vs-store-size
follow-up before treating it as an engine memory-leak finding.

## Files here

| file | sha256 |
|---|---|
| `longevity-synth-1m-native-0.json` | `a94a0c2c3d343d84161c911f04c6a29b586a4b3ddfc113bffb38549d988b3500` |
| `recoveries.jsonl` | `a3ef427f47a4801ffcb4eab03bd05fd4d979b6d3ce507318b01c89783b3081da` |
| `reader_restarts.jsonl` | `ff7375c22a6c660ab565641d8ecce6a82de2de7a0628e20d50b7eda6f50170fe` |
| `longevity_ledger.jsonl` | `edc13c40f50b849ee4fde1adfdad1ebbbed0e7be24e97a853bea4e0e414d7db4` |
| `orchestrator.log` | `3c66d62554a1d19051a166510ec4f003af0f9b4e3ed36c4ceca7da5dce80f7a0` |

All five verified byte-identical (sha256) between xzgpu
(`/mnt/project/xzhang/tgms/longevity/2026-09-15/`) and this copy before
commit. `gate_e_report.md` and `writer_error_counts_by_life.json` are
derived locally (not copied) — the former by
`scripts/longevity_report.py` run read-only against the fetched manifest,
the latter by a one-off local aggregation over the 42
`writer_progress-<life>.json` files (not committed here — small enough to
quote fully above, not so small it belongs only in prose). `metrics.jsonl`
(33 MB) and the live store copy stay on xzgpu per the doc's own PI ruling;
their path is recorded in the manifest's own `record`/`config.metrics`
fields.

## Post-hoc replay check (2026-09-15) — aborted: replay process OOM-killed

**Pre-registered in the OSDI'27 campaign plan (§4.3a), prediction untested.**
B7c's `tgms.storage.eventlog.replay(..., compact_every=N)` (see the
"Replay with periodic compaction" section of `docs/eval_durability.md`,
2026-09-15) makes the Gate E replay/digest-equivalence check *disk*-feasible
for this soak's 1,074,952-batch log — but attempting it for real surfaced a
second, previously unmeasured resource limit: **host memory**, not disk.

**Setup.** Pinned worktree `/mnt/project/xzhang/tgms/work/tgms-xz-039fda7`
at commit `039fda7` (a descendant of the soak's own measured commit
`886805f`); engine built release (`build_info()`: `profile: release`,
`debug_assertions: False`, `engine_version: 0.8.0`). `git diff 886805f
039fda7 -- tgms/storage/base.py` is **empty** — `store_digest()`'s
definition is unchanged between the two commits, so a replay at `039fda7`
is a valid check of the `886805f` soak's own final digest. Command:
`tgms replay /mnt/project/xzhang/tgms/longevity/2026-09-15/store/
eventlog.jsonl --store .../replay-check --backend native
--compact-every 500`, logged to `replay-check.log`, against the preserved
314,897,038-byte / 1,074,952-batch event log (sha256
`e212b641a0ccd46248575cd0919768c699a256934388d305f465116c2b900d2a`).

**Attempt 1** (started 2026-09-15T08:37:28Z): the process was killed by the
kernel OOM killer at 2026-09-15T11:59:50Z, after 3h22m22s (12,142 s),
before printing anything (`replay-check.log` is 0 bytes — `tgms replay`
only prints its `{"batches": ..., "stats": ...}` line after `replay()`
returns, which it never did). Verbatim `dmesg`:

```
Out of memory: Killed process 2213341 (python) total-vm:94184404kB, anon-rss:82997140kB, file-rss:2408kB, shmem-rss:0kB, UID:1000 pgtables:181316kB oom_score_adj:0
```

xzgpu has 93 GB RAM total (`free -g`); this single process alone reached
~83 GB resident before the kill, with only ~8.6 GB used by everything else
on the (shared, multi-tenant) host at the time — the crash is this
process's own footprint, not contention. The partial store's own manifest
numbering reached generation `513024` before the kill (directly observed;
that store was deleted before this was written up, to make room for
attempt 2). Taking `compact_every=500` at face value —
each cycle publishes 500 batch-commit generations plus one
`compact()`-commit generation (`gc()` publishes none) — 501
generations/cycle, and `513024 / 501 = 1024` exactly, so **inferred**
(not directly logged): 1,024 compactions, ~512,000 of 1,074,952 batches
(47.6%) applied. The out-directory itself stayed small throughout
(peak ~485 MB observed, far under the 20,000 MB watch ceiling) — **the
disk-growth fix (B7c) worked exactly as designed; the failure is in
resident memory, which `compact_every` does not bound.** Dividing peak
RSS by the generation reached (82,997,140 kB / 513,024) gives **≈161.8
KB of resident memory per generation** — a rate that, if it holds for the
whole 1,074,952-batch/513,024-generation-equivalent replay-with-compaction
run, projects well past this host's 93 GB long before the log is fully
replayed. A single continuous `tgms replay` process holds the *entire*
run's resident-index growth in one process lifetime; the live soak itself
never did this — its writer was restarted every 30 minutes
(`--restart-every 30m`), which reset RSS to a fresh-process baseline each
time (see "Memory (RSS slope)" above: "RSS saw-tooths on every writer
restart... each life is a fresh OS process") and would have **masked**
this same growth from ever being visible during the original soak. This
looks like a real memory-growth defect in the replay path (or possibly in
the native store's resident index generally, only exposed here because
nothing resets it) and is under investigation as a follow-up — not fixed
or root-caused by this record, which is a measurement, not a patch.

**Attempt 2** (retry, started 2026-09-15T12:05:22Z): launched to check
whether attempt 1 was a one-off host-contention artifact rather than
reproducible. It was **deliberately killed** (`kill -9`, pid `2345872`) at
2026-09-15T12:09:23Z once its early trajectory (generation `8015` reached
within ~4 minutes, tracking the same 501-generations/cycle pattern) made
clear it was headed for the same ~83 GB/similar-generation OOM in another
~3.5h, which would have blocked the shared host for that long to
reconfirm a conclusion already well supported by attempt 1's own numbers —
this is itself a finding (a reproducible resource ceiling), not
infrastructure noise, so it was not left to run to completion. Its
partial store is **kept** (not deleted) for a separate diagnosis lane, at
`/mnt/project/xzhang/tgms/longevity/2026-09-15/replay-check/`, 396 MB as
of the kill.

**Prediction (frozen): untested.** The pre-registered prediction — that
the replayed store's digest equals the soak manifest's `final_digest`
`8eb9bc26fbf418df30b89fa85b5fd827c56ae90d14da84eb94a5f8683f6d9d72` — was
never checked on either attempt; no replay digest was ever computed.
`digest_status: "not computed (process OOM-killed)"` in
`replay-check-2026-09-15.json`, not "computed and found equal/unequal."
Per the pre-registration's own rule against re-running after an unfavorable
result: that rule applies to a completed run with a digest that turned out
to mismatch, which did not happen here — nothing was discarded to get a
different answer, since no digest ever existed to discard.

**Files**: `replay-check-2026-09-15.json`
(sha256 `a5c7a93c79af6a97160f7262fd16c98ff99cb982f22d282e896f3805ab7e4d9b`).

See also "Re-derived Gate E report (2026-09-15, post-fix harness)" below:
the live writer's own within-life RSS slope shows the same defect on the
*write* path, not only in this post-hoc replay of an already-written log —
the two numbers agree to within a few percent (see that section's
arithmetic).

## Post-hoc replay check — attempt 3 (post-D-087 fix) (2026-09-15)

**What changed from attempts 1/2 above.** Two things, together: (1) the
worktree is now pinned at `a6b3e94` (merge of `03100c1`/`ddc2f1e`, "engine:
bound the identity postings across compaction cycles (D-087 — the
soak/replay memory growth)"), not `039fda7`; `git diff 886805f a6b3e94 --
tgms/storage/base.py` is still empty, so `store_digest()`'s definition is
unchanged and this replay remains a valid check of the `886805f` soak's own
digest. (2) the run was launched under `nohup` with an independent RSS
watcher (`ps -o rss=` on the replay PID every 5 minutes, logged separately)
rather than relying on a foreground terminal, so a second OOM would not
also cost the watching session. `--compact-every 500` is unchanged from
attempts 1/2.

**Pre-step.** Before this replay, `tgms check` (full mode, read-only) was
run against the *original, untouched* soak store
(`/mnt/project/xzhang/tgms/longevity/2026-09-15/store`): 13,714 findings,
all `row/believed-versions-overlap`, verdict `CORRUPT`, wall 26.37s.
Verbatim output is in `verify-full-2026-09-15.txt` (first entry).

**This run.** Pinned worktree `/mnt/project/xzhang/tgms/work/tgms-xz-a6b3e94`,
engine `build_info()`: `profile: release`, `debug_assertions: False`,
`manifest_format_version: 3`. Launched 2026-09-15T15:00:39Z,
`replay-check-2.log` and `replay-check-2/native/CURRENT` last written
2026-09-15T23:20:5{0,4}Z (wall 30,015 s ≈ 8h20m). `{"batches": 1074450,
"stats": {...}}` written to `replay-check-2.log` on completion (1,074,450 of
the log's 1,074,952 batches applied — the same shortfall pattern as
attempts 1/2's partial runs, not new to this attempt). Manifest `CURRENT`
generation reached 1,076,598; taking `compact_every=500` at face value (500
batch-commit generations + 1 compact()-commit generation per cycle, as
attempt 1 also assumed), `1076598 - 1074450 = 2148` compactions. RSS was
sampled every 5 minutes for the full run (101 samples, all recorded in
`replay-check-2-2026-09.json:summary.rss_series`): peak 4,329,996 KB at
21:50:51Z, final sample 2,913,052 KB at 23:20:52Z. Peak out-directory size
observed: 586 MB (watch ceiling was 20,000 MB; never approached). Neither
the RSS abort ceiling (30 GB) nor the disk ceiling was reached.

**Digest.** The replayed store's digest (`Store.digest()` →
`NativeAdapter.store_digest()`, `tgms/storage/base.py` — the same routine
`scripts/longevity_run.py::cmd_run` uses for its own replay-equivalence
check; this codebase has no separately-named "streaming" digest entry
point) was computed read-only against the completed replay store:
`8eb9bc26fbf418df30b89fa85b5fd827c56ae90d14da84eb94a5f8683f6d9d72`. The
pre-registered prediction was the soak manifest's own recorded
`final_digest`, the same value.

**Full verify of the replayed store.** `tgms check` (full mode, read-only)
against `replay-check-2/`: 13,714 findings, all
`row/believed-versions-overlap`, verdict `CORRUPT` — the same count and
kind as the pre-step's check of the original soak store above. Verbatim
output is in `verify-full-2026-09-15.txt` (second entry, appended after the
pre-step's, which was left unedited).

**Record.** `replay-check-2-2026-09.json` (schema-valid against
`benchmarks/schema/result_manifest.schema.json`), `supersedes`:
`replay-check-2026-09-15.json` (attempts 1/2, aborted on OOM).

## Honest limits

- One host, one storage stack, one seed. 41 restarts, 2 reader crashes,
  and 249 (miscounted-as-1) writer errors are this run's own numbers, not
  a rate claim for a different duration, mix, or reader count.
- The replay/digest-equivalence check did not run this time; Gate E's
  "deterministic final state" is unverified, not confirmed, for this run.
- The memory FAIL is real as computed but its interpretation (leak vs.
  dataset-growth baseline) is not resolved here.
- The 2026-09-15 post-hoc replay check (see above) also did not resolve
  Gate E's replay/digest-equivalence question: `compact_every` fixed the
  disk side but the replay process was OOM-killed by the host kernel
  before producing a digest, on both of two attempts. The frozen
  prediction (`final_digest` == replay digest) stays untested, and a
  ~161.8 KB/generation resident-memory growth rate is now an open,
  under-investigation defect, not a confirmed root cause.


## Re-derived Gate E report (2026-09-15, post-fix harness)

`gate_e_report_rederived_2026-09-15.md` / `summary_rederived_2026-09-15.json`
re-derive Gate E for this same run using the **current** (post-fix)
`scripts/longevity_run.py::summarize` and `scripts/longevity_report.py`,
run read-only on xzgpu against the preserved raw inputs
(`/mnt/project/xzhang/tgms/longevity/2026-09-15/`) — no new soak, no
store open, no replay re-attempt; the original manifest above is
unchanged, and `derived_from` in the JSON names its sha256 and the
script commit used.

| check | old report (886805f-era script) | re-derived (post-fix script) |
|---|---|---|
| deterministic final state | FAIL (digest_equal coerced `None` -> `False`) | NOT COMPUTED (replay skipped: `projected_replay_exceeds_limit`) — correctly distinguishes "never checked" from "checked, failed" |
| no unbounded memory | FAIL, 111.639 kB/s first-vs-last (dismissed as restart saw-tooth) | **FAIL, 3,165.6 kB/s within-life median** — fitted separately within each of the 42 writer lives' own samples; **all 42 lives** land between 1,921.1 and 4,154.2 kB/s, every one of them far above the 5 kB/s noise floor |
| errors observed | FLAG, 1 (last life only, via `counter_latest`) | FLAG, **249** (life-summed across all 42 lives, matching this README's own independently-computed `writer_error_counts_by_life.json` exactly) |

**What changed and why**: the "restart saw-tooth, not a real trend"
explanation this README gave earlier addressed a real flaw in the
*first-vs-last* slope (it is dominated by wherever the first/last sample
land across 41 saw-tooth resets), but it never actually checked whether
memory grows *within* one writer life. It does — clearly, in every one
of the 42 lives, at a median ~3.17 MB/s sustained over each ~35-minute
life before the restart cycle resets it. The same re-derivation also
fits the 8 reader processes' own RSS within their lives (they only
restart on a crash, not on the 300s reopen cadence) and finds a much
smaller but still positive and consistent slope (~5.1-5.5 kB/s across
all 6 readers that never crashed) — see the full table in
`gate_e_report_rederived_2026-09-15.md`.

**This is a real, positive finding, not a re-interpretation of noise**:
every one of 42 independent writer-life measurements agrees in sign and
order of magnitude. It directly corroborates the same-day
"Post-hoc replay check (2026-09-15) — aborted: replay process OOM-killed"
section above: that post-hoc `tgms replay` of this run's own log was
OOM-killed at ≈83 GB RSS after ≈513,024 generations, ≈161.8 KB of
resident memory retained per generation (82,997,140 kB / 513,024).

**The arithmetic lines up across both measurements.** This run's own
first/last-hour write throughput averaged 24.030 -> 16.412 commits/s,
call it ≈20 commits/s; the within-life median RSS slope of 3,165.6 kB/s
divided by that rate is 3,165.6 / 20 ≈ **158 KB retained per commit** on
the live writer's own write path — matching, to within a few percent,
the replay's independently measured **≈161.8 KB per generation**
retained (each generation being, to a first approximation, one
committed batch plus the periodic compaction generation). Two
independently-run processes, measured two different ways (a live
30-minute-lifetime writer's RSS regression vs. a single long-lived
replay process's peak RSS at its OOM point), converge on the same
order-of-magnitude per-unit-of-work retention rate. That agreement is
what elevates this from "two separate FAILs" to one finding: a real,
unbounded per-generation/per-commit memory retention defect somewhere in
the shared code both paths exercise (the native store's resident index
and/or version-retention bookkeeping — see the "Post-hoc replay check"
section's own root-cause discussion above), not a harness artifact and
not two unrelated issues.

The combination — a positive within-life slope in the live writer, and a
much larger confirmed leak in `replay()` over the same log, with matching
per-unit-of-work arithmetic — points at a real, unbounded-growth defect
in this codebase's generation/version retention path, not at a harness
measurement artifact.


## Correction `disc` caveat (2026-09-15)

Read-only semantic review (`docs/design/CORRECTION_DISC_SEMANTICS_REVIEW_2026-09-15.md`)
found that every `a1_events` correction this soak applied through
`apply_correction_via_public_api` carried the disc `"#0"` — `_a1_events`
built no explicit `disc` at all, so `Store.ingest_events` fell back to its
position-derived default, which is `"#0"` for every single-event call. By
the review's own arithmetic over the generator's draw distributions (not a
re-measurement — the record itself carries no per-correction identity to
measure), an estimated **7,200–11,100** of this run's `a1_events`
corrections merged into another correction's edge identity rather than
becoming the "new fact" the generator's own docstring promises (D2.1). A
**second, independent** defect found in the same review —
`_a2_disjoint`'s edge branch hard-coding the literal `disc="a2-disjoint"`,
unaffected by the `"#0"` issue above and by its fix — produces a
comparable estimated count (**~7,200–11,100**) of nominally Class-A
"adds belief" corrections that in fact *carved* their predecessor, a
class error rather than merely an identity error. See the review
document for the full derivation and parameters.

This does not change any frozen number in this README: every Gate E
check above is computed from re-execution/digest-compare against the
*same* post-write store state, never from `disc`, so it is internally
consistent regardless of which identity a correction actually landed
under. Two things it does affect:

- **`corrections_applied` (207,850, all lives) counts ops issued, not
  distinct logical corrections landed.** A correction that merged into an
  existing identity, or carved one, still counts as one "applied"
  correction in that total — it is not a count of distinct edge
  identities created.
- **`verify_healthy = true` is FAST-mode evidence only.**
  `longevity_run.py`'s final `verify()` call runs in fast (file-walk)
  mode; the review's own micro-check found `mode="fast"` reports
  `healthy=True` on a store `mode="full"` flags with a
  `believed-versions-overlap` error for exactly this collision shape. A
  full-mode `verify()` of the preserved store
  (`/mnt/project/xzhang/tgms/longevity/2026-09-15`, xzgpu, read-only) is
  **owed and scheduled**, not yet run as of this caveat.

`_a1_events` and `_a2_disjoint` were both fixed (fresh, unique-per-correction
`disc`, never the implicit default or a hard-coded literal) after this soak
ran; this caveat documents what the already-committed record above
measured, not a re-run.

## Soak 2 (post-fix)

**Pre-registration (OSDI'27 plan §4.3b, P-SOAK2).** Same harness and
parameters as the first soak above (`stores/synth-1m-native`, `--mix
balanced --readers 8 --compact-every-batches 500
--compact-min-interval-s 5 --reader-reopen-every-s 300 --artifacts 500
--seed 0 --max-disk-mb 20000 --duration 24h`), with two frozen changes:
`--restart-every 6h` (4 writer lives instead of 42, so a leak cannot hide
behind frequent restarts) and the end-of-run replay/digest step left ON
(the harness now compacts every 500 applied batches during replay by
default, per B7c). **Pin addendum (2026-09-16, before dispatch):** moved
from `54dcab0` to `eed91c0` — `54dcab0` + the D-086 stale-snapshot reader
fix (`tgms/storage/eventlog.py`) + the storm-v1 macros script;
`git diff 54dcab0 eed91c0 -- crates/` is empty (confirmed before the
build), so the engine is unchanged and this remains a valid measurement
of the D-086 fix's effect on the read path the soak's 8 reopening readers
exercise.

- **commit measured**: `eed91c0`, pinned worktree
  `/mnt/project/xzhang/tgms/work/tgms-xz-eed91c0` on xzgpu, engine built
  release (`build_info()`: `profile: release`, `debug_assertions: False`,
  `manifest_format_version: 3`; `build_info-2.log`)
- **launched**: 2026-09-16T00:53:01Z; **`RUN_DONE`**: 2026-09-17T18:27:06Z
  (`digest_equal=True verify_healthy=True recoveries=3 reader_restarts=0
  errors=150477128` — the last figure is explained below, not corrected)
- **wall clock**: 149,616.5 s per the manifest's own `summary.wall_s`
  (includes the final `verify()` + replay/digest check + the full-mode
  `tgms check` this record adds below; the soak's own `--duration 24h`
  elapsed at 86,400s as designed)
- **manifest**: `longevity-synth-1m-native-1.json`, conforms to
  `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — exit 0)
- **manifest sha256**:
  `19b597db12c6830859f5317ec8bdb630e4599c2fed8924ed26d41edc1cc23f68`
- **result_digest**: `4d1f4c3b5fba78373759de5b8cbef599b7ab5cdfd75174c4ac4d35d130b86644`
- as with the first soak, `metrics.jsonl` (40.8 MB) and the store copy
  stay on xzgpu (`/mnt/project/xzhang/tgms/longevity/2026-09-16-soak2/`);
  this directory holds only the manifest and small side-record files.

### Gate E table (`gate_e_report-2.md`, `scripts/longevity_report.py`, current harness)

| check | verdict | detail |
|---|---|---|
| deterministic final state (verify() clean + replay digest equality) | **PASS** | verify_healthy=True digest_equal=**True** (the replay actually ran this time — `compact_every=500` kept the disk-guard projection to ≈61 MB, as the first soak's own follow-up predicted) |
| bounded metadata growth (manifest bytes slope) | FAIL | manifests 3.141 B/s, segments 2,396.455 B/s |
| no unbounded memory (RSS slope, whole-run first-vs-last) | FAIL | 56.275 kB/s |
| no unbounded memory (RSS slope, **within-life**, the frozen prediction's own metric) | **PASS** | median 27.965 kB/s; all 4 lives 20.98-43.49 kB/s, every one **under the 50 kB/s frozen bound** and an order of magnitude below the first soak's 1,921.1-4,154.2 kB/s per-life range |
| throughput/latency drift (first hour vs. last hour) | FAIL | throughput 39.91 -> 19.12 commits/s, p99 60.42 -> 82.08 ms |
| compaction stalls | info | 0.000 ms max reader p99 during a compaction window |
| errors observed | **FLAG** | manifest says **150,477,128** — real, not a double-counting artifact (see below); dominated by a reader-side failure storm that is this soak's headline finding |
| recoveries / reader restarts | info | 3 designed / 0 | 

**Reading the FAIL rows.** "Bounded metadata growth" and "throughput/latency
drift" are FAIL under the *current* `longevity_report.py`'s own verdict
logic, which is a genuine discrepancy against this run's own pre-registered
prediction ("metadata growth PASS", "throughput/p99 drift within the frozen
bar") — flagged here, not explained away. The formal refutation clause in
the pre-registration names four specific conditions only (any life's
within-life RSS slope > 50 kB/s, the digest differing, a reader dying, or
full verify reporting any overlap); none of those four occurred, so P-SOAK2
is **not refuted** by its own stated bar, but the metadata-growth and
drift-verdict mismatches against the softer, descriptive predictions are
real and unresolved — not investigated further here (out of this record
step's scope).

### `errors observed`: 150,477,128 is real, not a delta-vs-cumulative bug — it is a reader-side failure storm

The coordinator's first suspect was the `54dcab0` cumulative-vs-delta
counter fix (`_emit_cumulative_counters`, see the first soak's own "errors
observed" section above) recurring in a new form. **Checked and ruled
out**: every `metrics.counter()` call site in this commit's
`scripts/longevity_run.py` (`writer_errors_total`, `reader_errors_total`,
`reader_reopens_total`, `queries_total`, `artifact_*_total`,
`compactions*_total`) passes a genuine per-event or per-period **delta**
(mostly the implicit default of 1, once per exception or event), not a
snapshotted running total; `Metrics.flush()` rewrites every tracked key's
current in-memory value on every call, so `summarize()`'s `counter_sum`
(latest value per `(name, labels)` key, summed across keys) is
mathematically the correct total regardless of flush frequency. Cross-checked
directly: `reader_error_counts_by_class-2.json`'s per-reader totals (from
`metrics.jsonl`'s own latest-per-key counter values) match each reader's
own final `errors` dict in `reader-<idx>-progress.json` exactly (e.g.
reader 0: `hist.single=7,809,559` both places, modulo one final flush's
worth of drift). The number is real.

`error_count = writer_errors_life_summed (616) + reader_errors_total
(150,476,512) + unexpected_recoveries (0) = 150,477,128`.

**Writer side (616, unremarkable, same shape as soak 1's 249):**
`writer_error_counts_by_life-2.json`, re-derived from the 4
`writer_progress-<life>.json` files plus `longevity_ledger-2.jsonl`'s 616
`writer_op_error` entries (this run's ledger carries `error_type` +
`error_msg` per event, unlike soak 1's):

| life | errors | exception class |
|---|---:|---|
| 0 | 220 | `NotFoundError` (100%) |
| 1 | 159 | `NotFoundError` (100%) |
| 2 | 131 | `NotFoundError` (100%) |
| 3 | 106 | `NotFoundError` (100%) |

All 616 are `NotFoundError: no believed node version of <id> overlaps vt`
(a correction racing ahead of its target's visibility window — the same
class the harness's own module docstring already expects from an
unthrottled correction stream) and decline monotonically life-over-life
(220 -> 106) as the store grows, consistent with the first soak's
observation that this is a routine, recurring event, not a regression.

**Reader side (150,476,512 — the real story):** `reader_error_counts_by_class-2.json`
breaks it down by exception class and by reader:

| exception class | total (8 readers x 4 queries) | first soak's equivalent |
|---|---:|---|
| `OSError` | 150,278,720 | **0 records of any kind** — `reader_errors_total` never fired once in soak 1's `metrics.jsonl` |
| `StateError` | 197,792 | 0 (soak 1's only reader-side StateErrors were 2 fatal, uncaught ones that killed and restarted the process — see soak 1's "Reader restarts" section) |

Both classes are **new to this run** — soak 1's `metrics.jsonl` has zero
`reader_errors_total` records of any kind (checked directly). This is the
headline, unpredicted finding of P-SOAK2, not a metrics artifact:

- **`OSError` (150.28M events).** All 4 query ids in the mix fail with an
  identical count for a given reader in most windows — i.e. once a
  reader's store handle enters this state, every operator call on it
  fails, not just one query shape. Onset is clustered late in the run:
  earliest at t+61,417s (reader 6, still inside writer life 2, which
  spans roughly t+43,003 to t+64,621s) and latest at t+75,993s (reader 4,
  well into life 3); every affected reader is still erroring at the last
  sample before `RUN_DONE` (t+86,201-86,215s). None of the 272 periodic
  reopens (`--reader-reopen-every-s 300`) any affected reader performed
  after its onset appears to have cleared the condition — the storm is
  monotonic and does not self-heal. `reader_error_counts_by_class-2.json`
  has the exact onset timestamp and final count per reader (2.32M-31.24M
  per reader in OSError-events terms, i.e. summed across its 4 queries).
  Despite this, `reader_queries_total` (16,055,124, in the manifest) shows
  real work continued to complete throughout — the storm degrades
  throughput severely without producing a hard reader crash
  (`reader_restarts=0` — every affected reader stayed alive and kept
  retrying). **Root cause not established here.** `pyo3`'s default
  `std::io::Error -> PyErr` conversion (a bare `OSError`, not the
  errno-specific subclass CPython's own `OSError.__new__` would pick,
  because no message-text capture reached this counter's labels) means
  the label alone cannot say *which* I/O failure this is; onset timing
  (late in the run, correlated with accumulated store size — `n_entities`
  reached 3,092,488 by the end, roughly 1.8x soak 1's final count) is
  consistent with, but does not prove, a compaction-cadence-vs-store-size
  interaction: this run compacted every ~40s on average (4,215
  compactions over 86,400s) against a `--reader-reopen-every-s 300`
  window that was sized for the first soak's much smaller, faster-to-scan
  store. This needs a dedicated follow-up with message-text capture added
  to `reader_errors_total`'s labels before it can be root-caused; it is
  reported here as a real, serious, previously-unobserved regression
  between `886805f` and `eed91c0`, not resolved.
- **`StateError` (197,792 events, present in 6 of 8 readers — 3 and 6
  show none).** `tgms/storage/eventlog.py`'s `EventLog.batches_from` has
  exactly two `StateError` raise sites, both reachable by a reader racing
  a live writer's tail: "... is not readable at offset ...: {parse_error}
  — the replay cursor may not be on a record boundary" (a JSON parse
  failure) and "... record at offset ... has no terminating newline and
  is not the log's last record — the replay cursor may not be on a record
  boundary" (the literal message the pin addendum's added observation
  named). **This observation is refuted by its own stated bar**: the pin
  addendum predicted this class of reader-open failure "must not raise it
  above 0" (soak 1's `reader_errors_total` was 0 under the old rule,
  though that comparison is confounded — soak 1 never populated this
  counter for *any* reader error, so "0" there measured "the counter path
  never fired," not "the specific race never happened"). This run's total
  is 197,792, unambiguously above 0. The metrics label carries only the
  exception class, not the message text, so the two raise sites cannot be
  distinguished here and the exact predicted message cannot be isolated
  from its sibling — but both indicate the D-086 fix did not eliminate
  torn-tail races against a live writer at this reopen cadence, only
  reduced their consequence from "the process dies" (soak 1: 2 reader
  deaths) to "the process catches it and keeps going" (this run: caught,
  counted, retried, no death). That direction-of-change is itself
  consistent with D-086's own design (tolerate more torn-tail cases
  instead of refusing them) even though the raw count went up, not to 0.

### Per-life / per-reader RSS: least-squares slopes (`rss_slopes-2.json`)

Ordinary least squares of `rss_kb` vs. wall-clock seconds, fit separately
within each of the writer's 4 lives (split at the 3 recorded restart death
timestamps in `recoveries-2.jsonl`) and within each of the 8 readers' single
life each (`reader_restarts=0`, so no reader ever restarts):

| writer life | span (s) | rss first -> last (kB) | slope (kB/s, least squares) | frozen bound |
|---|---:|---|---:|---:|
| 0 | 21,412.4 | 2,175,344 -> 3,052,040 | **43.494** | <= 50 |
| 1 | 21,469.7 | 3,444,068 -> 4,313,180 | **32.365** | <= 50 |
| 2 | 21,463.3 | 4,054,572 -> 4,919,352 | **20.978** | <= 50 |
| 3 | 21,412.1 | 3,942,752 -> 4,997,444 | **23.564** | <= 50 |

All 4 lives PASS the frozen 50 kB/s bound (median 27.97 kB/s), an order of
magnitude below soak 1's 1,921.1-4,154.2 kB/s per-life range — the
strongest positive signal in this record that the memory-growth-within-a-life
defect soak 1 found is substantially fixed. (Life 0/1/2's 43.49/32.37/20.98
kB/s match the coordinator's own independently-computed fit exactly.)

| reader | span (s) | rss first -> last (kB) | slope (kB/s, least squares) | frozen bound |
|---|---:|---|---:|---:|
| 0 | 86,334.6 | 137,920 -> 930,948 | 7.212 | <= 10 |
| 1 | 86,334.4 | 137,864 -> 867,248 | 7.682 | <= 10 |
| 2 | 86,334.3 | 137,876 -> 887,768 | 8.098 | <= 10 |
| 3 | 86,334.4 | 137,692 -> 944,276 | 7.599 | <= 10 |
| 4 | 86,334.5 | 137,724 -> 923,316 | 7.952 | <= 10 |
| 5 | 86,334.3 | 137,780 -> 941,584 | 7.822 | <= 10 |
| 6 | 86,347.9 | 138,072 -> 837,628 | 7.772 | <= 10 |
| 7 | 86,334.3 | 139,416 -> 833,404 | 7.999 | <= 10 |

All 8 readers PASS the frozen 10 kB/s bound, including the readers hit
hardest by the OSError storm above — the storm degrades read throughput,
not (visibly) reader memory.

### Full-mode `tgms check` of the final soak store (`verify-full-soak2-2026-09-17.txt`)

Run read-only against the live store as left by the writer at `RUN_DONE`
(`/mnt/project/xzhang/tgms/longevity/2026-09-16-soak2/store`, generation
1,894,946; not the throwaway replay store), wall 1m15.8s:

```
checked:    5 segments (3022877 rows), 0 close runs (0 closes), 3092488 dictionary records
walked:     3022877 rows (2734334 believed) across 2647008 identities
layout:     1995715 tt_s runs across live segments, 1583554 in the worst one

verdict: healthy — every referenced file passed its checksums, every cross-reference resolved, and every bitemporal invariant held
```

**0 `believed-versions-overlap` findings — the pre-registered prediction
holds**, against soak 1's 13,714 findings (all that same kind, `CORRUPT`)
on the pre-fix store. This is the second strong positive signal in this
record, alongside the within-life RSS slopes: the two problems soak 1's
post-hoc analysis found (per-life memory growth, and a full-mode-only
corruption class invisible to fast-mode `verify()`) both look fixed by the
commits between `886805f` and `eed91c0`, even as the reader OSError storm
above shows a new, different problem was introduced somewhere in that same
range.

### Files added here

| file | sha256 |
|---|---|
| `longevity-synth-1m-native-1.json` | `19b597db12c6830859f5317ec8bdb630e4599c2fed8924ed26d41edc1cc23f68` |
| `recoveries-2.jsonl` | `23a70f0e38c89ab1478a13389c93e86112aceabc2b07b91fc18ca669ad34827b` |
| `longevity_ledger-2.jsonl` | `c418a0e303ec763cb4270f0e67d763e4cf886aa6af061ff19ebd348c618516af` |
| `orchestrator-2.log` | `1938772baab3d58729d02fa6766f9831f4daf359a52ece0060a02c906fc78cfa` |
| `gate_e_report-2.md` | `4a02fb42f092be22c39dfc2eed21170310e9c5dfd88836b42d459a1bb9da9814` |
| `writer_error_counts_by_life-2.json` | `d46fa237e5b02c6420c0735f7375454e82051a6c666ec6b3bb32183d13ef6126` |
| `reader_error_counts_by_class-2.json` | `e6d8624fc410c26289541557b17de132aeb08597b4c9bf83327c3cf22de34f41` |
| `rss_slopes-2.json` | `8a690ece6a9579b2ccd5d8a4473cb7f7c6b07ada390657e6a7ea5dee188c3054` |
| `verify-full-soak2-2026-09-17.txt` | `f18892a01759422854d3d09e4bfb04a6b4cc65b18d2e60aee55f5c8c711bdb59` |
| `build_info-2.log` | `e6045d966013189e36236fca8f8015c962e8e37b45898cc8b05beecce6124905` |
| `compactions-2.jsonl` | `f17788eb7a1ca197dafefe8a627ea7441641e5a3f6786752f0638a2aa2e0673b` |
| `reader_onset_rows-2.json` | `48597e90f8146318afa60ad668403492b3464cd5c29933a329804341a9c6c0e7` |

All ten verified byte-identical (sha256) between xzgpu
(`/mnt/project/xzhang/tgms/longevity/2026-09-16-soak2/`) and this copy
before commit. `reader_restarts.jsonl` has no soak-2 counterpart: the file
is only ever written on a reader restart, and this run had zero
(`reader_restarts=0`), so it was never created. `metrics.jsonl` (40.8 MB)
and the live store copy stay on xzgpu per the same PI ruling as soak 1;
`rss_slopes-2.json` and `reader_error_counts_by_class-2.json` are derived
from it (method documented in each file) rather than copying it.

**Added later (2026-09-18): `compactions-2.jsonl` and
`reader_onset_rows-2.json`.** `compactions-2.jsonl` (4,215 lines, one per
compaction the writer performed) is copied from xzgpu
(`.../2026-09-16-soak2/compactions.jsonl`), sha256-verified byte-identical
between xzgpu and this copy before commit, same as the ten files above.
`reader_onset_rows-2.json` answers "what was the edge-row count when the
reader OSError/StateError storm's onset began" (earliest and latest onset
across the 8 readers, per `reader_error_counts_by_class-2.json`'s own
`osrror_storm_onset_by_reader`) by reconciling that field's wall-clock
`onset_t_plus_s` (elapsed seconds since RUN_STARTED) against
`compactions-2.jsonl`'s `t_start`/`t_end` (the writer child's
`time.perf_counter()`, a different, monotonic-but-not-epoch clock that
resets at each of the 4 writer lives) via `metrics.jsonl`'s
`compactions_total` counter and `generation` gauge (kept on xzgpu, not
copied; read read-only, nothing written back to the host). It is derived
locally in this worktree, not copied from an xzgpu twin (no such twin
exists) — its own `method` field states the full reconciliation procedure,
both clocks, and the bound on the mapping error (bracketed by the ~60-72s
metrics flush interval, up to 3 ambiguous compaction rows per onset).

### Honest limits

- One host, one storage stack, one seed, four writer lives. The reader
  OSError storm's root cause is not established — this record documents
  its existence, timing, and scale, not its mechanism.
- The `StateError` class total (197,792) cannot be split between the pin
  addendum's specific predicted message and its sibling raise site without
  message-text capture the current harness does not have; the addendum's
  observation is reported as refuted by class-total evidence, not by an
  exact string match.
- "Bounded metadata growth" and "throughput/latency drift" read FAIL under
  the current `longevity_report.py` against this run's own softer,
  descriptive predictions ("PASS", "within the frozen bar") — flagged, not
  investigated further; the formal refutation clause (the four explicit
  conditions in the pre-registration) does not depend on these two rows,
  and none of those four conditions occurred.
- `verify_healthy=True` in the manifest is the harness's own final
  `verify()` call (documented elsewhere in this file as fast-mode); the
  dedicated **full**-mode check reported above is the one that actually
  clears the `believed-versions-overlap` class soak 1 found, and it is
  clean.
- `reader_onset_rows-2.json`'s edge-row counts at the earliest/latest
  reader-error onset are only as precise as the ~60-72s metrics flush
  interval used to reconcile the writer's `perf_counter` compaction clock
  against the reader's wall-clock onset times; up to 3 compactions per
  onset fall inside that bracket and cannot be ordered against the onset
  from these files (see the file's own `ambiguous_range` fields).

## P-STORM-HUNT (6 h observation run, 2026-09-17/18)

**Pre-registration.** Same harness and store as P-SOAK2 (`stores/synth-1m-native`,
`--mix balanced --readers 8 --compact-every-batches 500
--compact-min-interval-s 5 --reader-reopen-every-s 300 --artifacts 500
--seed 0 --max-disk-mb 20000`), with no restarts (`--restart-every` unset /
never, a single writer life) and a shortened window, `--duration 6h`, run
at commit `57952fa`, engine built release (`build_info-stormhunt.log`:
`profile: release`, `debug_assertions: False`, `engine_version: 0.8.0`,
`manifest_format_version: 3`). Purpose: a soak whose only job was to try to
capture reader-side exception messages if the P-SOAK2 reader error pattern
(the `OSError`/`StateError` storm documented above) recurred, using the
D-088 bounded reader-error-message capture merged at `c3a5592`.

- **commit measured**: `57952fa`, pinned worktree
  `/mnt/project/xzhang/tgms/work/tgms-xz-57952fa` on xzgpu
- **launched**: 2026-09-17T23:01:21Z; **`RUN_DONE`**:
  `digest_equal=True verify_healthy=True recoveries=0 reader_restarts=0
  errors=175` (`orchestrator-stormhunt.log`)
- **manifest**: `stormhunt-2026-09-17.json`, conforms to
  `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — exit 0, re-checked for this record
  step; the record was not rewritten)
- **result_digest**: `78b445786e86782185b5a5ab6759cc877d36faad9b3197f88098e93e89fee7f0`
- **end-of-run replay**: 476,813 batches replayed into a fresh store
  (compacting every 500 applied batches, per B7c) to compute
  `digest_equal` — this step is unscored (not part of the 6h observation
  window) and, per the harness's current design, could not be disabled.
- `metrics.jsonl` (10.0 MB) and the store/replay-store copies stay on
  xzgpu (`/mnt/project/xzhang/tgms/longevity/2026-09-17-stormhunt/`); this
  directory holds only the manifest and small side-record files, as with
  both soaks above.

**Outcome, stated exactly as the pre-registration's clause (e):** the
reader error pattern was not reproduced within 6 h at 1.40M edge rows;
this is not evidence that it is gone; the capture stays armed for the
next 24 h soak.

### RSS slopes (`rss_slopes-stormhunt.json`)

Ordinary least squares of `rss_kb` vs. wall-clock seconds, one fit for the
writer (single life, no restarts) and one fit per reader (`reader_restarts=0`):

| process | span (s) | rss first -> last (kB) | slope (kB/s, least squares) | frozen bound |
|---|---:|---|---:|---:|
| writer | 21,261.2 | 2,010,868 -> 2,625,128 (2.01 -> 2.63 GB) | **20.444** | <= 50 |
| reader 0 | 21,528.5 | 138,188 -> 367,732 | 12.249 | <= 10 |
| reader 1 | 21,527.9 | 138,252 -> 325,728 | 11.362 | <= 10 |
| reader 2 | 21,529.5 | 138,344 -> 393,696 | 11.950 | <= 10 |
| reader 3 | 21,529.2 | 138,376 -> 441,040 | 12.036 | <= 10 |
| reader 4 | 21,528.8 | 138,236 -> 381,240 | 11.739 | <= 10 |
| reader 5 | 21,529.7 | 138,240 -> 403,580 | 12.543 | <= 10 |
| reader 6 | 21,529.1 | 138,188 -> 380,888 | 12.028 | <= 10 |
| reader 7 | 21,529.1 | 138,096 -> 389,696 | 12.358 | <= 10 |

The writer's fit (20.444 kB/s, 2.01 -> 2.63 GB) agrees with the
coordinator's own independently-computed value (20.4 kB/s) to within
0.1 kB/s and **PASSes** the 50 kB/s frozen bound. All 8 readers
(11.4-12.5 kB/s, first -> last 0.14 -> 0.33-0.44 GB, matching the
coordinator's own fit to the same tolerance) **exceed** the 10 kB/s
frozen reader bound — unlike P-SOAK2, where every reader passed under
that same bound. `rss_slopes-stormhunt.json` records the exact method,
per-process series, and a host-load caveat: co-tenant load averaged
10.79-48.95 (1-min, hourly samples) on 40 cores throughout the run's own
6h window (`host_load.log`, 23:01:21Z-05:01:21Z), with one further
post-run sample at 06:01:21Z (during the unscored verify/replay step)
recording a drop to 3.56. This run's host was not idle or quiet; that
context is offered alongside the reader-slope bound miss, not as an
explanation for it — root cause not established here.

### Errors: 175, all writer-side, 0 reader errors (`writer_error_counts_by_class-stormhunt.json`)

`longevity_ledger-stormhunt.jsonl` has exactly 175 lines, all
`event=writer_op_error`, `op=correction`, `error_type=NotFoundError`
(`no believed node version of <id> overlaps vt`) — 100% one class, the
same recurring correction-races-visibility-window pattern documented for
soak 1 (249) and soak 2 (616), not a new failure mode
(`corrections_applied=93045`, `corrections_skipped=60882` this run per
the manifest; these 175 are a small subset of skipped corrections that
raised rather than silently skipping).

**Reader side: 0 errors, 0 `reader_op_error` events, explicitly.** The
manifest's `summary.reader_errors_total` is 0 and
`longevity_ledger-stormhunt.jsonl` contains zero `reader_op_error` lines.
The D-088 bounded reader-error-message capture merged at `c3a5592` was
armed for the entire 6h run and simply had nothing to capture: the
P-SOAK2 reader-side `OSError`/`StateError` storm did not recur here. See
`reader_op_error-stormhunt.jsonl` (empty, 0 bytes) and its sidecar
`reader_op_error-stormhunt.README.txt`.

### Files added here

| file | sha256 |
|---|---|
| `stormhunt-2026-09-17.json` | `74ba7c15651bc6cd04895a3deb4b25dfd61a93d5c0d123ffb6923679e2fda661` |
| `build_info-stormhunt.log` | `a7e203c8ec88a1b24cb54783140391ac829089e1b31d06f37233ddcf40614df3` |
| `gate_e_report-stormhunt.md` | `5bd449c5b981542b8e105f801e1ba5f63b7f3e0aeb2a3bdd7d21645dc578b84f` |
| `host_load-stormhunt.log` | `5e12bf9a411462e5dea3502ec6f68618001c9d985b5b43a1c055ac9f63b936cb` |
| `longevity_ledger-stormhunt.jsonl` | `8e7d212d37721ab003068fc47dd2e3debe46df98670089bffdfaf6ea87dcf28c` |
| `orchestrator-stormhunt.log` | `1048b76aac90239067861dc587576d49da0967c0a2f86332b2f359b66b919fb9` |
| `rss_slopes-stormhunt.json` | `1f766e6076782f4d3296eafd9c54aabede2292ce72f57cc4632d74722c33f45e` |
| `writer_error_counts_by_class-stormhunt.json` | `ce02014fae5781cfbac6aed5d9eb6199543c26e6570e0fd6975f039fb9f20775` |
| `reader_op_error-stormhunt.jsonl` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `reader_op_error-stormhunt.README.txt` | `d95919f0a2c7ec09a80aca4644e41e1b7210755622ea4f1e78d4aa908c9bacfe` |

All ten verified byte-identical (sha256) between xzgpu
(`/mnt/project/xzhang/tgms/work/tgms-xz-57952fa/benchmarks/longevity-v1/`
and, for the raw record inputs, `/mnt/project/xzhang/tgms/longevity/2026-09-17-stormhunt/`)
and the laptop worktree copy committed alongside this README change.
`metrics.jsonl` (10.0 MB) and the store/replay-store copies stay on
xzgpu per the same PI ruling as both soaks above; `rss_slopes-stormhunt.json`
is derived from it (method documented in the file) rather than copying it.

### Honest limits

- One host, one storage stack, one seed, one writer life, 6 h instead of
  24 h. A negative result (pattern not reproduced) at this duration and
  edge-row count is not evidence the P-SOAK2 pattern is gone — the
  pre-registration's own clause (e), reproduced verbatim above, is the
  full and only claim this run supports.
- Co-tenant host load (10.79-48.95 on 40 cores, hourly samples) was
  elevated and variable throughout, unlike a dedicated/idle host; whether
  it affects reader RSS growth or error-storm onset timing is untested
  here.
- Both reader RSS slopes (11.4-12.5 kB/s) exceed the 10 kB/s frozen
  reader bound the P-SOAK2 readers all passed under — flagged here, not
  investigated further; root cause not established.
- The end-of-run replay/digest step (476,813 batches) is unscored and,
  per the current harness design, could not be disabled for this
  observation-only run.

## Soak 3 (72 h) (2026-09-19/22)

**Pre-registration.** Same harness and store as P-SOAK2/P-STORM-HUNT
(`stores/synth-1m-native`, `--mix balanced --readers 8
--compact-every-batches 500 --compact-min-interval-s 5
--reader-reopen-every-s 300 --artifacts 500 --seed 0 --max-disk-mb
20000`), extended to `--duration 72h` with `--restart-every 6h` (12 writer
lives pre-registered). Run directory on xzgpu:
`/mnt/project/xzhang/tgms/longevity/2026-09-19-soak3` (kept read-only for
this record step). Engine/harness commit `9e21a83`, engine built release
(`build_info()`: `profile: release`, `debug_assertions: False`,
`engine_version: 0.8.0`, `manifest_format_version: 3`), pinned worktree
`/mnt/project/xzhang/tgms/work/tgms-xz-9e21a83` on xzgpu.

- **launched**: 2026-09-19T00:46:42Z (run_started_epoch, `min` timestamp
  over `metrics.jsonl`); **`RUN_DONE`**: `digest_equal=True
  verify_healthy=True recoveries=8 reader_restarts=0
  errors=2203552660` (`run.log`)
- **wall clock**: 331,364.8 s per the manifest's own `summary.wall_s`
  (includes the final `verify()`, the end-of-run replay/digest check, and
  the full-mode `tgms check` this record adds below; the soak's own
  metrics stop at 2026-09-22T00:46:06Z, 259,164.0 s ≈ 71h59m24s after
  launch, matching the configured `duration_s=259200`)
- **manifest**: `longevity-synth-1m-native-2.json` (pre-registered name
  for this record; the harness's own output file on xzgpu is named
  `longevity-synth-1m-native-0.json` — a fresh numbering the harness
  started from for this run directory, not a soak-0 artifact), conforms
  to `benchmarks/schema/result_manifest.schema.json`
  (`.venv/bin/python scripts/check_result_manifest.py` — exit 0)
- **manifest sha256**:
  `e094fd5acbcb95e523f3f00fa544e634a531d3b3c991525422a6546981c2251a`
  (verified byte-identical between xzgpu and this copy before commit)
- **result_digest**: `a9441352a2f988b7dc227d6826a54429400889a91092382767484f72d5da3b3c`
- `metrics.jsonl` (118 MB) and the store/replay-store copies stay on
  xzgpu (`/mnt/project/xzhang/tgms/longevity/2026-09-19-soak3/`), per the
  same PI ruling as soak1/soak2/stormhunt; this directory holds only the
  manifest and small side-record files. `rss_slopes-3.json`,
  `throughput-3.json`, `reader_op_error-3.json` and
  `reader_onset_rows-3.json` are derived from it read-only (method
  documented in each file), not copies of it.

**Known facts, stated up front (not re-derived as findings below):**

1. **Only 9 writer lives ran, not the pre-registered 12.** The harness
   copy that actually ran predates the `gc_mid_delete` restart-arming fix
   now on main (`3267725` / `7b5d6ba`, both after `9e21a83`) — a defect
   in which `compact()` never called `gc()`, so the harness's own
   designed-restart boundary for that scenario never fired. `recoveries.jsonl`
   has exactly 8 designed writer deaths (returncodes alternating `-6`
   /`137`), giving 9 lives: lives 4, 5 and 8 ran ~12 h each
   (42,895.0-43,018.8 s), the other 6 ran ~6 h each (21,311.4-21,469.7 s).
   This is a known harness-vintage limitation, not a store defect.
2. **The compaction-stall row is NOT COMPUTABLE, not zero.** The
   manifest's `summary.compaction_stall_max_reader_p99_ms` reads `0.0`.
   Checked directly: none of `compactions.jsonl`'s 8,147 rows carry
   `ts_start`/`ts_end` epoch fields — only `t_start`/`t_end`, whose values
   (~3.09e7 s) match the host's own multi-hundred-day uptime
   (`CLOCK_MONOTONIC`), not epoch time and not `time.perf_counter()`'s
   per-process reference either. The D-091 fix that would add epoch
   timestamps to this log is not present in the `9e21a83` build. Without
   an epoch-comparable compaction window, this row cannot be computed
   from these files at all; the `0.0` in the manifest is a default, not a
   measurement, and must not be read as "no stalls observed."
3. **A co-tenant ran on xzgpu throughout, and especially hard during
   wind-down.** `host_load-3.log` (hourly 1-min load average, 93
   samples, min 1.21 / max 188.27) shows user `jding` present alongside
   `xzhang` the entire time, load ≈20-24 (1-min) through 2026-09-19 and
   ≈10-20 on 09-20/21. During the post-soak replay
   (2026-09-22 ~01:12-20:48Z) the host ran three `run_kuzu.py`
   processes, a Neo4j JVM and two DyGFormer GPU trainings concurrently
   with the replay, swap was fully used, and xzgpu's `sshd` was
   unreachable from ~06:35Z onward (the replay process itself kept
   running server-side and finished regardless — only remote access was
   affected). **Every measurement below — RSS slopes, throughput,
   reader-error timing — was made under shared, at times extreme, host
   load, not a dedicated or idle host.**
4. **The end-of-run replay (3,618,225 batches at `replay_compact_every`
   5000) is a digest check, not a timing measurement.** It took ≈19.6 h
   (2026-09-22 01:12Z→20:48Z); its rate fell from ≈11.9k to ≈1.2k
   batches/min as each 5,000-batch compaction rewrote the growing replay
   store (parent `write_bytes` reached 1.19 TB) under the co-tenant load
   described above. `digest_equal=True` — the replay completed and
   matched the live store's digest despite all of this.

### (a) Writer within-life RSS slopes (`rss_slopes-3.json`)

Ordinary least squares of `rss_kb` (gauge, writer, unlabeled series) vs.
wall-clock seconds since each life's first sample, split at the 8
recorded restart death timestamps in `recoveries-3.jsonl`:

| life | duration | span (s) | rss first → last (kB) | slope (kB/s, OLS) |
|---|---|---:|---|---:|
| 0 | 6h | 21,391.0 | 2,150,016 → 2,748,648 | **24.867** |
| 1 | 6h | 21,464.3 | 2,511,888 → 3,038,892 | **12.910** |
| 2 | 6h | 21,402.3 | 3,161,176 → 3,925,844 | **17.098** |
| 3 | 6h | 21,389.3 | 3,732,884 → 5,082,832 | **12.527** |
| 4 | 12h | 43,018.8 | 4,612,452 → 5,752,196 | **11.768** |
| 5 | 12h | 42,969.2 | 4,733,224 → 6,037,844 | **16.111** |
| 6 | 6h | 21,332.3 | 5,159,892 → 7,021,092 | **28.834** |
| 7 | 6h | 21,311.4 | 5,354,280 → 6,844,388 | **16.878** |
| 8 (final, ran to `RUN_DONE`) | 12h | 42,895.0 | 6,149,324 → 8,208,448 | **16.630** |

Median 16.630 kB/s; every life is well under the 50 kB/s frozen bound
(max 28.834, life 6) — the same order-of-magnitude-fixed picture soak2
first established, holding over 9 lives and up to 12 h each instead of 4
lives of 6 h. **Matches the coordinator's independent fit** (24.9 / 12.9
/ 17.1 / 12.5 / 11.8 / 16.1 / 28.8 / 16.9 / 16.6, median 16.6) to within
rounding on all 9 lives — no disagreement.

### (b) Reader RSS slopes, full 72 h run (`rss_slopes-3.json`)

`reader_restarts=0`, so each reader is a single life, OLS over the full
259,133.7-259,163.3 s span:

| reader | rss first → last (kB) | slope (kB/s, OLS) |
|---|---|---:|
| 0 | 138,056 → 1,441,360 | 4.572 |
| 1 | 138,308 → 1,330,864 | 4.271 |
| 2 | 138,128 → 1,395,140 | 4.704 |
| 3 | 138,192 → 1,712,864 | 4.633 |
| 4 | 138,148 → 1,463,764 | 4.409 |
| 5 | 138,348 → 1,473,728 | 4.786 |
| 6 | 138,228 → 1,530,204 | 4.372 |
| 7 | 138,048 → 1,488,940 | 4.796 |

All 8 readers are well under the 10 kB/s frozen bound — better than
soak2 (7.2-8.1 kB/s) and much better than stormhunt's failing 11.4-12.5
kB/s. **Matches the coordinator's independent fit** (4.57/4.27/4.70/4.63/
4.41/4.79/4.37/4.80 kB/s; first ≈138 MB, last 1.33-1.71 GB) to within
rounding on all 8 readers — no disagreement.

### (c) Reader deaths, unexpected recoveries

Zero. `reader_restarts=0` and `reader_restarts_recorded=0` in the
manifest; no reader ever died or was restarted. `unexpected_writer_deaths=0`
— all 8 writer deaths in `recoveries-3.jsonl` are `"kind": "designed"`
(the restart-arming cycle), recovery times 25.667 / 45.686 / 67.501 /
49.925 / 64.023 / 77.665 / 86.796 / 96.352 s, increasing with store size
across the run exactly as soak2 first observed. **Matches the
coordinator's independent fit** (25.7/45.7/67.5/49.9/64.0/77.7/86.8/96.4 s)
— no disagreement.

### (d) `digest_equal`

`True` (manifest `summary.digest_equal`), confirmed by the 19.6h
end-of-run replay described in known fact 4 above.

### (e) Full-mode `tgms check` of the final store (`verify-full-3.txt`)

Run read-only against the live store as left by the writer at `RUN_DONE`
(`/mnt/project/xzhang/tgms/longevity/2026-09-19-soak3/store`; not the
throwaway replay store), from a fresh worktree
`/mnt/project/xzhang/tgms/work/tgms-xz-b6cdde0` built off the
`repo.git` mirror at current main (`b6cdde0` — `crates/` has changed
since `9e21a83`, so the run's own `9e21a83` engine build could not be
reused for this check; the extension was rebuilt with `cargo build
--release -p tgms-engine-py`, `PYO3_PYTHON` pinned at
`/mnt/project/xzhang/tgms/venv/bin/python3`, verified both `tgms.__file__`
and `tgms._engine.__file__` resolve under the new worktree before
running), `nice -n 19`, wall 1m13.6s:

```
store:      /mnt/project/xzhang/tgms/longevity/2026-09-19-soak3/store
mode:       full
generation: 3624668
checked:    5 segments (4836692 rows), 0 close runs (0 closes), 5887903 dictionary records
walked:     4836692 rows (4293685 believed) across 4117913 identities
layout:     3808211 tt_s runs across live segments, 2684355 in the worst one

verdict: healthy — every referenced file passed its checksums, every cross-reference resolved, and every bitemporal invariant held
```

**0 `believed-versions-overlap` findings — 0 findings of any kind**
(`--json` form: `"findings": [], "problems": [], "healthy": true`),
matching soak2's clean full-mode result and confirming `generation:
3624668` against the manifest's own `summary.generation_final=3624668.0`.

### (f) Writer throughput (`throughput-3.json`)

`commits_per_s` gauge, hourly buckets = `floor((ts - run_started_epoch) /
3600)`:

| quantity | value |
|---|---:|
| manifest's own first-hour avg → last-hour avg | 40.648 → 11.014 commits/s |
| this record's hour-0 avg → hour-71 avg (independently bucketed) | 40.736 → 11.014 (exact match at the tail; hour-0 differs by 0.2%, most likely a different hour-0 clock origin — the harness's own internal start reference vs. this record's `min(ts)` over all of `metrics.jsonl` — not a computation error) |
| first-day avg (hours 0-23) | 19.713 commits/s |
| last-day avg (hours 48-71, the last 24 h of the pre-registered 72 h soak window) | 12.952 commits/s |
| **ratio, last day / first day** | **0.657** |

Per-life first-hour average (commits/s): life0 40.736, life1 21.082,
life2 17.249, life3 19.375, life4 18.836, life5 16.781, life6 15.382,
life7 12.814, life8 12.679 — declining life-over-life as the store grows,
consistent with the manifest's own drift figures. `throughput-3.json`
also carries the full 72-hour `hourly_commits_per_s_avg` series and an
`hourly_edge_row_count_avg` series so the throughput decline can be read
against store growth directly: edge rows grew from ≈1.05M (hour 0) to
≈4.05M (hour 71). `writer_progress-*.json` carries no edge-row field, so
the edge-row series is reconciled from `compactions-3.jsonl`'s
`compact.edge_rows` via `metrics.jsonl`'s `compactions_total` counter —
the same per-life counter-to-line-number correspondence documented in
`reader_onset_rows-3.json` (§ below), not a new method.

### (g) D-088 reader error pattern (`reader_op_error-3.json`, `reader_onset_rows-3.json`)

`longevity_ledger.jsonl` carries **17** `reader_op_error` lines (the
D-088 bounded reader-error-message capture, armed throughout this run) —
**not 16 as the coordinator's independent fit states**; every one of the
17 lines is reproduced verbatim in `reader_op_error-3.json`. Per-reader
totals by class (summed from each reader's own `error_details[].count`
in `reader-<idx>-progress.json`) match the coordinator's fit exactly:

| reader | OSError | StateError | total | ledger onset lines |
|---|---:|---:|---:|---:|
| 0 | 267,715,496 | 56,538 | 267,772,034 | 2 |
| 1 | 323,141,508 | 120,950 | 323,262,458 | **3** |
| 2 | 280,365,688 | 99,621 | 280,465,309 | 2 |
| 3 | 283,866,604 | 154,965 | 284,021,569 | 2 |
| 4 | 266,644,756 | 103,589 | 266,748,345 | 2 |
| 5 | 231,967,960 | 120,778 | 232,088,738 | 2 |
| 6 | 280,540,608 | 121,937 | 280,662,545 | 2 |
| 7 | 268,378,476 | 152,332 | 268,530,808 | 2 |
| **sum** | | | **2,203,551,806** | **17** |

The sum matches `manifest.summary.reader_errors_total` exactly, and
`2,203,551,806 + 854 writer errors + 0 unexpected recoveries =
2,203,552,660` matches `manifest.summary.error_count` exactly.

**Two corrections against the coordinator's fit, both in
`reader_op_error-3.json`'s verbatim ledger dump:**

1. **17 events, not 16** (reader 1 has 3 onset lines, not the 2 every
   other reader has).
2. **Not every message matches `.../seg/00…`.** 16 of the 17 do; the
   17th (reader 1's third onset, `ts=1789962367.6888`, StateError, deep
   into life 6) reads `io: No such file or directory (os error 2)
   [file=.../store/native/close/` — a different file class (a *close
   run* file, not a *segment* file). This is reader 1's only StateError
   onset that is not the seg/00 pattern; it happens ~78,000s after
   reader 1's first StateError onset (seg/00) and may be a distinct
   underlying cause, not investigated further here.

**Every episode counted by the ledger keeps erroring/every reopen fails
to clear it — this is FALSE for soak3, unlike soak2.** The ledger only
records the *first-ever* occurrence of a given (reader, class) signature
(D-088's own design), so its 17-event, 2-3-per-reader count is not the
number of times the condition actually fired. Reconstructing the true
episode structure from `metrics.jsonl`'s `reader_errors_total` counter
(summed across each reader's 4 per-query sub-counters, which are
independent accumulators; a single flush occasionally lands at slightly
different values per query, cross-checked and confirmed additive against
each reader's final `errors` dict) shows a starkly different pattern from
soak2's "storm is monotonic and does not self-heal": **every
(reader, class) combination cycles repeatedly between short, extremely
bursty active-error runs (up to hundreds of millions of `OSError`s
accumulating in roughly a minute of rapid retries) and genuinely healed
intervals lasting thousands to tens of thousands of seconds, in which the
error counter is exactly flat while the reader's own `queries_total`
counter keeps climbing at its normal, slow, real-query cadence** — cross-
checked directly for reader 0's OSError class over its single largest
healed interval (35,382.5 s, 1789873699→1789909082): `reader_errors_total`
stays pinned at 8,804,476 for the entire window while `queries_total`
climbs steadily from 298 to 406 (real completed query cycles), and the
metrics-flush cadence stays regular (~66-70 s, matching `rss_kb`'s own
steady sampling for the same reader) throughout — ruling out a metrics-
reporting gap as the explanation. Counting these active/healed
transitions directly gives, per (reader, class), 57-72 true error
episodes (662 total across all 16 reader×class combinations) — one to
two orders of magnitude more than the ledger's 17 first-onset events —
with 43-91% of each combination's post-onset observation window spent in
a healed state (`total_healed_time_s` in `reader_op_error-3.json`'s
`healed_at_next_reopen_evidence`, per reader×class, alongside the full
list of healing-interval start/end times). **`healed_at_next_reopen`:
yes, for all 8 readers and both error classes, repeatedly** — a materially
different answer from soak2's monotonic, never-healing storm for what is
very likely the same underlying missing-segment defect. Root cause
(what makes the condition clear and later recur, and whether the 6/12 h
restart-arming cadence or the host contention in known fact 3 plays a
role) is not established here.

`reader_onset_rows-3.json` reconciles the earliest (reader 1, OSError,
t+65,655.8s, life 3, edge_rows≈1,950,613-1,951,769) and latest (reader 2,
StateError, t+94,769.4s, life 4, edge_rows≈2,356,519-2,357,267) first-ever
onsets against `compactions-3.jsonl`, generalizing soak2's
`compactions_total`-counter reconciliation from 4 to this run's 9 writer
lives (confirmed exact: each life's final `compactions_total` sample
matches that life's own compaction-log line count). As in soak2,
`compactions.jsonl`'s own `t_start`/`t_end` cannot be used directly
(known fact 2 above) — the reconciliation goes through the metrics
counter, not the log's own clock.

### (h) Writer errors by class

854 total (`manifest.summary.writer_totals_all_lives.errors`), **100%
`NotFoundError`** (`no believed node version of <id> overlaps vt`) —
the same recurring correction-races-visibility-window pattern as every
prior soak (249 in soak1, 616 in soak2, 175 in stormhunt), confirmed
directly against `longevity_ledger.jsonl`'s 854 `writer_op_error` lines
(all `error_type=NotFoundError`). Per life: 174 / 125 / 86 / 97 / 132 /
90 / 62 / 36 / 52 (lives 0-8; sums to 854), from each
`writer_progress-<life>.json`'s own `errors` field.

### Files added here

| file | sha256 |
|---|---|
| `longevity-synth-1m-native-2.json` | `e094fd5acbcb95e523f3f00fa544e634a531d3b3c991525422a6546981c2251a` |
| `compactions-3.jsonl` | `9c66241d34b45c9b550b931fe78471e6789113338e163c3f4a7ba293e89a0bdf` |
| `recoveries-3.jsonl` | `36032c9ad858150e0d914d98e6399b62124492238b8dd013a9c15c8f78b9329b` |
| `host_load-3.log` | `93836026f0c88eb8733b746a1114d716ad463eb2b9e81898291d6be93419e02d` |
| `verify-full-3.txt` | `6643bbda8fd0508c53715346b0a00796a733b8aa55326fe3275fa40f79e9c89b` |

All five verified byte-identical (sha256) between xzgpu and this copy
before commit. `rss_slopes-3.json`, `throughput-3.json`,
`reader_op_error-3.json`, `reader_onset_rows-3.json` and
`writer_error_counts_by_class-3.json` are derived locally from
`metrics.jsonl`, `longevity_ledger.jsonl` and the files above (no xzgpu
twin to hash against — each file's own `method` field documents the full
procedure, same convention as soak2's `reader_onset_rows-2.json`).
`writer_error_counts_by_class-3.json` gives the per-life breakdown behind
(h) above (174/125/86/97/132/90/62/36/52, all `NotFoundError`), read
directly off `longevity_ledger.jsonl`'s `writer_op_error` events and
cross-checked against each `writer_progress-<life>.json`'s own `errors`
field. `metrics.jsonl` (118 MB) and the store/replay-store copies stay on
xzgpu, per the PI ruling reiterated at the top of this section.

### Honest limits

- One host, one storage stack, one seed. Only 9 of the pre-registered 12
  writer lives ran (known fact 1); the pre-registration's own per-life
  RSS-slope bound is still checked and passed on every life that did run,
  but this is not the full 12-life design.
- The compaction-stall row is not computable from this run's files
  (known fact 2) — reported as such, not as a measured `0.0`.
- Every measurement in this section was made under shared, sometimes
  extreme, co-tenant host load (known fact 3), including during the
  reader-error-onset window and the full 72 h RSS/throughput series. No
  attempt is made here to separate host-contention effects from the
  store's own behavior.
- The intermittent-healing finding in (g) is new to this run and not
  investigated for root cause: it is not established here whether it is
  driven by the larger number of writer restarts (9 lives vs. soak2's 4),
  the co-tenant host load (known fact 3), the larger store, or something
  else.
- `reader_op_error-3.json`'s `healed_at_next_reopen_evidence` uses a
  300 s (one reopen interval) threshold to call a plateau a genuine heal;
  shorter flat spans between individual metrics flushes are not counted
  and are not claimed to be meaningful.
- `reader_onset_rows-3.json`'s edge-row counts at the two reported onsets
  carry the same bracketing-flush-interval ambiguity soak2's version
  documented (up to 3 further compactions per onset cannot be ordered
  against it from these files); see each onset's own `ambiguous_range`.
