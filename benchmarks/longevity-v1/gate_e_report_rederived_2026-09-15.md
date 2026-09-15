# Gate E — re-derived report, 2026-09-15 (post-fix harness)

Re-derivation of Gate E for the 24h soak recorded in this directory
(`longevity-synth-1m-native-0.json`, measured at `886805f`), using the
**current** (post-fix) `scripts/longevity_run.py::summarize` and
`scripts/longevity_report.py` against the same preserved raw inputs on
xzgpu (`/mnt/project/xzhang/tgms/longevity/2026-09-15/`). No new soak was
run, no store was opened, and no replay was re-attempted: the original
manifest is unchanged. Only the metrics-derived parts of Gate E were
recomputed, via a read-only call to the current `summarize()` against
the preserved `metrics.jsonl` / `recoveries.jsonl` / `reader_restarts.jsonl`
/ `writer_progress-<life>.json` / `compactions.jsonl`, merged with the
fields that only the original run itself could produce (`verify_healthy`,
`digest_equal`, `final_stats`, `recoveries`, `reader_restarts`).

- **derived from**: `longevity-synth-1m-native-0.json`,
  sha256 `a94a0c2c3d343d84161c911f04c6a29b586a4b3ddfc113bffb38549d988b3500`
  (byte-identical to the copy in this directory and to xzgpu's own)
- **script commit**: `ebe1dc22e1f125ee87fe60c7680422211fc06a39` (this
  lane's base; executed on xzgpu from worktree `tgms-xz-039fda7`, a
  descendant with byte-identical `scripts/longevity_report.py` /
  `scripts/longevity_run.py`)
- **full machine-readable output**: `summary_rederived_2026-09-15.json`

## Gate E table

| check | verdict | detail |
|---|---|---|
| deterministic final state (verify() clean + replay digest equality) | NOT COMPUTED (replay skipped: projected_replay_exceeds_limit) | Gate E inconclusive on this row — verify_healthy=True, digest_equal=not computed; replay skipped because total_batches=1,074,952 projected 280,791,798.0 MB (~280.8 TB) of manifests (uncompacted projection, whole run), over the limit_mb=20,000.0 ceiling |
| bounded metadata growth (manifest bytes slope) | PASS | manifests -3.384 B/s, segments 1,341.111 B/s |
| **no unbounded memory (RSS slope)** | **FAIL** | first-vs-last 111.639 kB/s; **within-life median 3,165.582 kB/s** (gate uses the within-life figure) |
| throughput/latency drift (first hour vs. last hour) | PASS | throughput 24.030 -> 16.412 commits/s, p99 485.579 ms -> 3,740.529 ms |
| compaction stalls (max reader p99 during a compaction window) | info | 0.000 ms |
| errors observed | FLAG | **249** (life-summed; the original manifest's own field still reads 1, see below) |
| recoveries / reader restarts | info | 41 / 2 |

The "deterministic final state" and "bounded metadata growth" /
"throughput/latency drift" rows are unchanged from the original
`gate_e_report.md` (they read fields the original run itself computed,
which this re-derivation cannot and does not touch). The two rows that
change with the current harness are **memory** and **errors observed**.

## Memory: the within-life slope is unambiguously positive, in every life

The old report's naive first-vs-last slope (111.639 kB/s) was computed
over the whole, uncorrected 1,329-point series and is dominated by the
once-per-restart saw-tooth (a fresh OS process per writer life). The
current harness instead fits a separate least-squares RSS regression
**within each of the 42 writer lives' own samples** (split at each
`recoveries.jsonl` restart time) and reports the median of those 42
slopes.

| statistic | value |
|---|---|
| lives with a fitted within-life slope | 42 / 42 |
| min within-life slope | 1,921.1 kB/s |
| **median within-life slope** | **3,165.6 kB/s** |
| max within-life slope | 4,154.2 kB/s |
| lives with slope > 5 kB/s (noise threshold used by the gate) | **42 / 42** |

**Every single one of the 42 writer lives shows a clearly positive RSS
slope, three orders of magnitude above the 5 kB/s noise floor the gate
itself uses.** This is not the "restart saw-tooth, not a real trend"
explanation the original README offered (that explanation addressed only
the *first-vs-last* number's own known flaw, not what happens inside a
life) — a leak-shaped signal is present *within every individual writer
life*, each of which is a fresh, otherwise-unremarkable ~35-minute-old OS
process. See the full per-life table in `summary_rederived_2026-09-15.json`
(`writer_within_life_rss_slope.per_life`).

This finding directly corroborates the separate 2026-09-15 discovery that
a post-hoc `tgms replay` of this same run's log was OOM-killed at ≈83 GB
RSS after ≈513k generations (≈160 KB/generation retained) — see
`benchmarks/longevity-v1/replay-check-2026-09-15.json` and ledger entry
`D-087-replay-memory-growth` (both written by other lanes working this
same finding; referenced here by path, not reproduced). A within-life
writer RSS slope in the low single-digit MB/s range, sustained over a
~35-minute life before the restart cycle resets it, is consistent in
order of magnitude with a retained-per-generation leak of that shape.

### Reader RSS: also positive, but ~600x smaller than the writer's

The 8 reader processes (`--reader-reopen-every-s 300`, i.e. they close
and reopen their read handle every 5 minutes without restarting the
*process*) each show a small but consistent positive slope within their
own life (the whole run for the 6 readers that never crashed; the
pre-/post-restart segments for readers 5 and 6, which each hit the
D-086 torn-tail race once):

| reader idx | restarts | segment slope(s), kB/s |
|---|---:|---|
| 0 | 0 | 5.12 |
| 1 | 0 | 5.12 |
| 2 | 0 | 5.49 |
| 3 | 0 | 5.20 |
| 4 | 0 | 5.34 |
| 5 | 1 | 5.32, 47.07 (short 28-sample post-restart segment) |
| 6 | 1 | 5.33, 15.79 (short 113-sample post-restart segment) |
| 7 | 0 | 5.28 |

Pooled across all 10 fitted segments: min 5.12 kB/s, median 5.32 kB/s,
max 47.07 kB/s. **Readers do grow within a life**, at a slow, remarkably
uniform ~5.1-5.5 kB/s across all 6 never-restarted readers over the
full ~22-24h they each ran — small next to the writer's ~3.17 MB/s
median, but not zero, and not obviously noise either (all 8 baseline
segments land within a narrow ±8% band). The two post-restart segments
for readers 5/6 read higher, but each is a short tail (28 and 113
samples respectively, against a restart late in the run against an
already-larger store) and should not be over-read as a distinct effect
without more samples.

## Life-summed writer counters (the true totals, not the last life only)

`writer_totals_all_lives`, summed across each of the 42
`writer_progress-<life>.json` files' own final snapshot (the aggregation
the pre-fix harness's `counter_latest` silently discarded down to the
last life only — see this directory's README, "errors observed: manifest
says 1, the real total is 249"):

| counter | total |
|---|---:|
| errors | 249 |
| appends | 853,817 |
| corrections applied | 207,850 |
| corrections skipped | 137,163 |
| artifact checks | 3,284 |
| artifact invalidations | 1,264 |
| artifact refreshes | 1,263 |
| lives | 42 |

These match the README's own independently-computed
`writer_error_counts_by_life.json` totals exactly (249 errors; 853,817
appends; 207,850/137,163 corrections applied/skipped; 3,284/1,264/1,263
artifact checks/invalidations/refreshes) — the current harness's
`summarize()` now produces this life-summed total directly, rather than
needing a one-off local aggregation script.

## What changed and why

The original `gate_e_report.md` was produced by the pre-fix report
script against a pre-fix manifest (commit `886805f`): it collapsed
`digest_equal: null` to `False` (an inconclusive check misreported as a
FAIL), reported `error_count` from a `counter_latest` aggregation that
silently kept only the last of 42 writer lives' counters (1, not 249),
and reported memory via a first-vs-last slope dominated by the
once-per-restart saw-tooth. B7b/B7c on `main` since then added: (1) a
structured `replay_skipped` reason (rendered here as `NOT COMPUTED`,
matching the original row's intent even though the pre-fix manifest only
carries a flat `replay_skipped_reason` string, reconstructed here from
that string plus this run's own `longevity_ledger.jsonl` entry); (2)
life-summed writer counters in `summary.writer_totals_all_lives`; (3)
`scripts/longevity_report.py::median_within_life_slope`, the within-life
regression this report leans on for its central memory finding. None of
this reruns or reinterprets the soak itself — `verify_healthy`,
`digest_equal`, `final_stats`, `recoveries`, and `reader_restarts` are
carried over unchanged from the original manifest, since recomputing
those would require re-running the store/replay, which this lane
explicitly does not do. The practical upshot: the memory row's FAIL is no
longer a hand-wavable "restart saw-tooth" — it is a positive slope
present within all 42 individual writer lives, and (much more weakly) in
all 8 readers too.
