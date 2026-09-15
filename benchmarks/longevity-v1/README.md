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

`do_replay` was `False` because the projected replay cost (~280.8 **million**
MB, i.e. ~268 PB — D-149's O(batches²) manifest-growth pathology the
harness's own module docstring names, applied to this run's real 1,074,952
uncompacted batches at ~243 bytes/batch²) would have blown through
`--max-disk-mb=20000` by roughly seven orders of magnitude, so the
mandatory final replay/digest-equivalence step never ran. `digest_equal`
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

## Honest limits

- One host, one storage stack, one seed. 41 restarts, 2 reader crashes,
  and 249 (miscounted-as-1) writer errors are this run's own numbers, not
  a rate claim for a different duration, mix, or reader count.
- The replay/digest-equivalence check did not run this time; Gate E's
  "deterministic final state" is unverified, not confirmed, for this run.
- The memory FAIL is real as computed but its interpretation (leak vs.
  dataset-growth baseline) is not resolved here.
