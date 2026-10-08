# external-v1 — external-baseline campaign (Neo4j recompute / differential-dataflow IVM / TGMS same-host control)

**Date assembled: 2026-10-08 (lane C1-records). Status: landed.** All three
campaign records in this directory were assembled on xzgpu from real,
completed grids (`scripts/external_record.py` at public main `79e79c7b`,
run against the result trees under `/mnt/project/xzhang/tgms/external-v1/
{export,t1,n1,d1}` — read-only inputs, not touched by this assembly).

| file | campaign | cells | sha256 |
|---|---|---|---|
| `neo4j-recompute-2026-10-07.json` + `-rows.jsonl` | Neo4j 5.26 full recompute | 43 | see `SHA256SUMS.txt` |
| `ivm-differential-2026-10-07.json` + `-rows.jsonl` | differential-dataflow IVM | 43 (+1 withheld-only) | see `SHA256SUMS.txt` |
| `tgms-control-2026-10-05.json` + `-rows.jsonl` | TGMS same-host control (lane T1) | 19 | see `SHA256SUMS.txt` |

All three validate against `benchmarks/schema/result_manifest.schema.json`
(`scripts/check_result_manifest.py benchmarks/external-v1/<file>.json`).

## What was measured

Three configurations, all run against the same 43-cell export of the
`storm-v2` correction-storm workload (12 seed-0 main-grid cells × 2 stores
+ the N=10,000 probe + 6 new age-banded cells; three-seed collegemsg/
synth-iv-60k variants bring the export to 43 total). The export bundle and
all three configurations' result trees live under
`/mnt/project/xzhang/tgms/external-v1/` on xzgpu and are not copied into
this repo (only their assembled summaries are).

1. **Neo4j 5.26 full recompute** (`neo4j-recompute-*`) — every registered
   artifact's query re-run after each burst, on Neo4j 5.26.0 + APOC
   5.26.0-core + Temurin 21.0.12 (JDK) + the official Java driver 5.28.6.
   Lane N1.
2. **differential-dataflow incremental view maintenance** (`ivm-differential-*`)
   — one maintained view per operator family (`ivm-dd`, single worker),
   built from public main `79e79c7b`. Binary sha256
   `3e5a890bdfdac0ad056ce0e02da02de8e62e547917f440eb880578b995e83f31`
   (`cargo_lock_sha256` `505819c28e17948e80aa48f5e502fe95bde8aa25cc0d40dada009770a38d3d9e`;
   `differential_dataflow` 0.25.1, `timely` 0.31.0). Lane D1.
3. **TGMS same-host control** (`tgms-control-*`) — the same
   `bench_correction_storm.py` harness the committed `storm-v2` grid used,
   re-run on xzgpu so a reader never has to cross hosts to get a TGMS
   reference number for a cell measured on xzgpu. Harness built at
   `fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty` (every T1 cell's own
   `git_commit` field, consistent across all 19). Lane T1.

**Host** (all three): xzgpu — 93 GB RAM, 40 (logical) CPUs, Linux
5.4.0-216-generic, verified from every campaign's own `machine` field and
cross-checked against the per-cell `host-snapshot-{before,after}.txt`
`uptime`/`free` captures lanes N1/D1/T1 each took.

**Host protocol**: the Memgraph container was stopped for each timed
window and restarted after (no co-tenant store running during a timed
cell); no `nice`; a host snapshot (`uptime`, `free`, top processes) was
taken immediately before and after every cell, independent of the
configuration's own self-reported load. Lane D1's grid waited out a
co-tenant job (2026-10-06T12:39Z → 2026-10-07T~14:00Z) and ran on a quiet
box starting 2026-10-07T14:23:19Z — confirmed directly: `d1/.../
host-snapshot-before.txt`'s first cell timestamp is
`2026-10-07T14:23:19Z` with `load average: 0.13`.

## Reading notes (verbatim meaning, restated)

- **A1** — the three external configurations were measured on xzgpu; the
  *committed* TGMS numbers lane C1's assembler reads for its EXT1/EXT2
  ratios (`benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json` +
  probe) are from the iTiger cluster, a different host. The
  `tgms-control` record above is the same-host control reported beside
  them so a reader is never forced to compare an xzgpu number to an
  iTiger number without also having an xzgpu-vs-xzgpu number on hand.
- **A2** — six age cells were added beyond the committed grid: 2 stores
  (`collegemsg`, `synth-iv-60k`) × `c3` × `{recent, hours, days}` × seed
  0. Each one's `digests.json` records its own `equality_level` literally
  as `"new cell (no committed digest)"` (abbreviated `new (A2)` in the
  table below), with the note: *"extra cell (Addendum-A2-style); no
  committed grid row, so there is no L1/L2 reference to check against —
  this export's own digest becomes the reference for the T1 control and
  both external configurations, per the memo's own A2/T1 ruling."*
- **A3** — per-cell equality level is stated plainly in the table below;
  L1 is never claimed without the digest match (`l1_eventlog_sha_match`
  in `t1-equality.json`, byte-identical final event log) — all 19 T1
  cells are L1, exactly as measured, none asserted from a weaker check.
- **A11** (proposed, applied at scoring) — on xzgpu the reference for
  every cell is the same-host control T1 on the identical exported
  bundle, not the committed iTiger-cluster row the assembler's own
  EXT1/EXT2 ratio arithmetic reads. **P-EXT1(a)** ("the committed internal
  baseline is representative") is scored only on the 22 synth cells that
  reproduce the committed campaign (L2/L2-partial); the 18 collegemsg
  main-grid cells are not tied to the cluster records — their own
  `digests.json` explains why: *"the committed per-batch detail file for
  this cell is not in the repo (main-grid cells' per-batch rows lived
  only on the now-cleared iTiger checkout); L2 here is log_bytes-per-batch
  + n_registered + narrowing_coverage.per_template_counts only"* — which
  is why all 18 collegemsg main-grid cells show `FAIL` in the table
  below, not a weaker-but-real L2. Applying the A11 substitution itself
  (scoring EXT1/EXT2 against `tgms-control` instead of the committed
  cluster rows) is the paper-macro generator's job (lane W2ad), not this
  assembler's — `scripts/external_record.py`'s own ratios are built and
  tested against the committed cluster rows only, by design (see
  "Macro-stub landing" below).

## Per-cell equality level (43 export cells)

"export equality" is each cell's own `digests.json` `equality_level`
(shared by both external configurations, since both check against the
same export bundle). "T1 equality" is lane T1's `t1-equality.json`
`l1_eventlog_sha_match`-derived level, for the 19 cells T1 measured.

| cell_id | export equality | T1 equality |
|---|---|---|
| `collegemsg-c1-deep-n1000-s0` | FAIL | L1 |
| `collegemsg-c1-deep-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c1-deep-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c1-none-n1000-s0` | FAIL | L1 |
| `collegemsg-c1-none-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c1-none-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c3-days-n1000-s0` | new (A2) | L1 |
| `collegemsg-c3-deep-n1000-s0` | FAIL | L1 |
| `collegemsg-c3-deep-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c3-deep-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c3-hours-n1000-s0` | new (A2) | L1 |
| `collegemsg-c3-none-n1000-s0` | FAIL | L1 |
| `collegemsg-c3-none-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c3-none-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c3-recent-n1000-s0` | new (A2) | L1 |
| `collegemsg-c4-deep-n1000-s0` | FAIL | L1 |
| `collegemsg-c4-deep-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c4-deep-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c4-none-n1000-s0` | FAIL | L1 |
| `collegemsg-c4-none-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c4-none-n1000-s2` | FAIL | -- (not in T1) |
| `synth-iv-60k-c1-deep-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c1-deep-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-deep-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-none-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c1-none-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-none-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-none-n10000-s0` | L2 | L1 |
| `synth-iv-60k-c3-days-n1000-s0` | new (A2) | L1 |
| `synth-iv-60k-c3-deep-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c3-deep-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-deep-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-hours-n1000-s0` | new (A2) | L1 |
| `synth-iv-60k-c3-none-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c3-none-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-none-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-recent-n1000-s0` | new (A2) | L1 |
| `synth-iv-60k-c4-deep-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c4-deep-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c4-deep-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c4-none-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c4-none-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c4-none-n1000-s2` | L2-partial | -- (not in T1) |

## Oracle-agreement summary

Independently reconstructed per cell by `scripts/external_check.py` from
each export bundle's own `oracle.jsonl` (never a re-print of either
configuration's own self-reported `gates.oracle_agreement`).

| campaign | cells | agree | disagree | not_answered | oracle_refused | n_compared |
|---|---|---|---|---|---|---|
| neo4j-recompute | 43 | 857,469 | **195** | 0 | 0 | 857,664 |
| ivm-differential | 43 | 857,664 | 0 | 0 | 0 | 857,664 |

`ivm-differential`: 0/43 cells disagree (`d_pass: true` in the record).

`neo4j-recompute`: **10/43 cells disagree**, not the 2 the pre-registration
memo's text names. All 195 disagreements trace to one artifact,
`storm-000911`, in the `synth-iv-60k` store (seed 0 only — the seed-1/
seed-2 `synth-iv-60k` cells fully agree, per `n1/PROGRESS.log`:
`synth-iv-60k-c4-deep-n1000-s1 ... oracle=18543/18543`,
`...-s2 ... oracle=18963/18963`). It disagrees at every burst/epoch of
every seed-0 `synth-iv-60k` cell (9 cells at `n1000`, 1 at the `n10000`
probe) — i.e. the same persistent single-artifact mismatch recurs across
cells sharing that store+seed, not an independent disagreement per cell:

| cell_id | agree | n_compared | disagree |
|---|---|---|---|
| `synth-iv-60k-c1-deep-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c1-none-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | 53,526 | 53,532 | 6 |
| `synth-iv-60k-c3-days-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-deep-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-hours-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-none-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-recent-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c4-deep-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c4-none-n1000-s0` | 18,543 | 18,564 | 21 |

Per `n1/synth-iv-60k-c1-none-n1000-s0/check.json` and
`n1/synth-iv-60k-c1-none-n10000-s0/check.json` (the probe), the
disagreeing artifact is `storm-000911` at every epoch (epochs 0-20 for
the n1000 cells, 0-5 for the probe) — one artifact, compared
cross-cell/cross-epoch to confirm the identical name recurs: `disagree:
['storm-000911']` at every single burst in all 10 cells above, never a
different name.

## The withheld-correction cell

`synth-iv-60k-c4-deep-n1000-s0-withheld` (not one of the 43 scored
cells): the `ivm-dd` dataflow configuration's `F-epoch` feeder reports
`false_fresh_count = 39` at the probe point (`probe_reports_complete_through_10:
true`); the `F-watermark` feeder's probe never completes through batch 10
(`probe_reports_complete_through_10: false`, recorded as
`ivm_f_watermark_unanswerable: true`). TGMS's own `false_fresh`/
`stale_marked` at R10 is 0 by contract (the harness never serves a stale
answer as fresh — a property of the maintenance protocol, not a
measurement this check performs) — but that number was **not** supplied
to this check run: `d1/synth-iv-60k-c4-deep-n1000-s0-withheld/
check-withheld.json`'s own `withheld.tgms_false_fresh` is `null`, with
the note *"TGMS's own false_fresh/stale_marked at R10 ... was not
supplied to this run ... left null rather than assumed 0"*. This README
states the 0-by-contract figure as the harness's design property, not as
a number read from `check-withheld.json` — that file's own `tgms_false_fresh`
row is the one to cite for "measured, not asserted."

## Estimates vs. actuals

| campaign | memo estimate | measured | source |
|---|---|---|---|
| Neo4j recompute (N1) | 40-160 h | **18.2 h** (65,497 s summed `wall=` across 43 `n1/PROGRESS.log` lines) | `n1/PROGRESS.log` |
| differential-dataflow (D1) | ~40 min | **22.5 min** (1,351 s summed `wall=` across 43 cells + the withheld cell, `d1/PROGRESS.log`) | `d1/PROGRESS.log` |
| TGMS same-host control (T1) | ~51 h | **51.68 h** (186,065 s summed `config.wall_s` across the 19 `tgms-control` cells) | `tgms-control-2026-10-05.json` |

Note: this lane's own assignment text states the Neo4j figure as "≈16 h
incl. probe" and the dataflow figure as "≈40 min" — the measured sums
above (18.2 h and 22.5 min respectively) are what `PROGRESS.log` actually
records; the assignment text's figures are given as task-brief
approximations, not restated here as the measured values. The T1 figure
matches the brief's "~51 h" closely.

## Known contradictions

- **The dataflow CLI hard-codes `workers=1`.** Every `d1/*/result.json`'s
  `config.workers` is `1` (confirmed on the sampled cell above); the
  memo's planned 8-worker rerun was never run — there is no 8-worker
  result tree anywhere under `/mnt/project/xzhang/tgms/external-v1/`.
- **The Neo4j oracle-loading fix landed in git only ~8 minutes after N1's
  timed grid started, not comfortably before it.** `n1/HOST-N1.log`
  records N1 starting at `2026-10-05T16:36:51Z` from laptop checkout
  `b7674fd6bb16f378c546b4b84ff10f746a397d0a` (committed 2026-10-03
  08:44:42 -0500, i.e. two days *before* the fix). The fix,
  `4e1d539b neo4j-recompute: fix load_oracle flattening bug; add N1
  timed-grid driver`, was committed 2026-10-05 11:44:23 -0500
  (`2026-10-05T16:44:23Z`) — eight minutes *after* N1 started, per
  `git log -1 --format=%ci`. `git merge-base --is-ancestor b7674fd6
  4e1d539b` confirms `b7674fd6` is an ancestor of (strictly older than)
  `4e1d539b`, the reverse of what a "fixed before the grid ran" reading
  would need. The actual `neo4j_recompute` package that ran on xzgpu was
  copied as a sha256-pinned plain file tree (`n1-work/neo4j-recompute`,
  per `HOST-N1.log`'s own note), not a git checkout, so this does not by
  itself prove a stale (pre-fix) oracle-loading path ran any real timed
  cell — but the ~18.2 h grid's own git-commit evidence does not
  establish "fixed before any timed cell was kept" either; it only shows
  the fix landed in git 8 minutes into an 18-hour run.
- **`ivm_dd::record::oracle_agreement`'s own bookkeeping never reads
  `refused`** (documented in `scripts/external_check.py`'s own module
  docstring as a known latent bug in that crate, not patched by this
  lane — out of scope; `external_check.py` works around it by excluding
  refused names itself).
- **`versions.rustc` is recorded as `"unknown"`** in every `d1/*/result.json`
  (confirmed on the sampled cell), not `"1.97.1"` — the toolchain
  currently installed on xzgpu (`~/.cargo/bin/rustc --version`) reports
  `rustc 1.97.1 (8bab26f4f 2026-07-14)`, consistent with but not
  confirmed by the recorded build metadata, which has a version-capture
  gap for this field. This README states "rustc 1.97.1" as the current
  toolchain on the build host, not as a figure read from the binary's
  own recorded `versions.rustc`.
- **`HOST-N1.log`'s own pre-flight check reports `openjdk version
  "11.0.27"`**, while every Neo4j cell's own `result.json.config.jdk_version`
  reports `"Temurin 21.0.12"` — two different JVMs visible in the same
  lane's logs (the pre-flight line is a generic host `java -version`
  check, not necessarily the JVM Neo4j itself ran under; not
  reconciled further here).
- **18 collegemsg main-grid cells read `FAIL`, not a weaker-but-real L2**,
  because the committed per-batch reference file for the main grid lived
  only on the now-cleared iTiger checkout (see reading note A11 above) —
  this is a data-availability gap in the *committed* reference, not a
  defect in the xzgpu measurement.

## Macro-stub landing (`scripts/sys_paper_macros.py`)

See the hand-back for the exact landed/pending count and the field gap.
`scripts/sys_paper_macros.py`'s `compute_external_baselines` currently
calls `m.add_pending(...)` unconditionally for all 29 `recExt1*`/`recExt2*`
macros — it does not attempt to read `benchmarks/external-v1/*.json` at
all yet, so landing these three records changes 0/29 stubs until that
generator is wired up (lane W2ad's task, not this lane's — this lane
does not edit `scripts/sys_paper_macros.py`).
