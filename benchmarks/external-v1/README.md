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
- **The Neo4j oracle-loading fix and the timed grid's start, reconciled
  from the host log and file hashes (coordinator check, 2026-10-08).**
  `n1/HOST-N1.log` records a first launch at `2026-10-05T16:36:51Z` whose
  first cell was stopped at `16:38:18Z` ("STOPPING grid: load_oracle bug
  found in runner.py"); the fixed `runner.py` was written to the runner
  copy at `16:42:25Z` (its sha256 equals the file at commit `4e1d539b`,
  which differs from the file at `b7674fd6`); the grid was relaunched at
  `16:42:33Z` and the first kept cell ran `16:42:35Z`–`16:45:43Z`
  (`result.json` mtime `16:45:42Z`). The git commit of the fix
  (`16:44:23Z`) postdates the relaunch because the lane committed after
  copying; every kept timed cell ran on the fixed runner. The discarded
  first attempt's output was removed by the lane before the rerun.
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

## Per-batch rows (addendum 2026-10-08)

`tgms-control-2026-10-05-batches.jsonl` is the raw per-batch data behind
the 19 `tgms-control-2026-10-05.json` cells — one line per batch, every
arm's own `ttf_ms`/`check_wall_ms`/`refresh_wall_ms`/`invalidated_count`/
`false_fresh_count`/`false_stale_count` plus the batch's
`global_recompute_wall_ms`, each value carried over byte-exact (no
rounding) from its source file. `tgms-control-2026-10-05-rows.jsonl`
(the file already described above) carries only per-cell equality
manifests, never this per-batch detail, because lane T1's own per-batch
rows stayed on xzgpu rather than being committed at the time that record
landed.

These per-batch rows exist so that the time-to-fresh of the `tgms-L1`
arm can be reconstructed in "sum" mode (`check_wall_ms + refresh_wall_ms`)
directly from the raw batches, rather than read off this record's own
`arms.tgms-L1.ttf_p50_ms`, which 18 of these 19 cells measured in
"end-to-end" mode instead (`tgms-control-2026-10-05.json`'s own
per-cell `config.measure_ttf`; the 19th, `synth-iv-60k-c1-none-n10000-s0`,
already measured "sum"). `scripts/sys_paper_macros.py`'s
`compute_ext_sum_mode` is the reader.

Source: a read-only `scp` pull on 2026-10-08 of each cell's own
`storm-*-rows.jsonl` from `xzgpu`,
`/mnt/project/xzhang/tgms/external-v1/t1/<cell_id>/` — nothing on xzgpu
was modified. `tgms-control-2026-10-05-batches.SOURCES.txt` records each
pulled file's own sha256 and its exact xzgpu path, one line per cell
(19 lines); `tgms-control-2026-10-05-batches.jsonl`'s own sha256 is in
`SHA256SUMS.txt` beside it.

## Fifth-arc re-measurement of the same-host control (2026-10-08)

Lane R1's re-run of the same 19-cell T1 same-host control, on the fixed
engine + harness (public main `93d4543af61f786884884bee1ebf440ca5ce5673`),
with `--check-cache chain` added to every cell — the exact T1 loop
(`external-v1/t1_control.sh`) replayed as `external-v1/t1b_control.sh`,
same cell order, flags, probe placement, and age-cells-last ordering,
against the same `external-v1/export/` bundles T1 itself checked
against. Worktree `work/tgms-xz-93d4543a` on xzgpu; `crates/` unchanged
since `b6cdde0`, so the engine `.so` (sha256
`d48ae71caca80d9f3ea9f360b2e7a2f242f3e9e03ee223d69c4f8aff08b7faca`) was
copied in from `work/tgms-xz-b6cdde0` rather than rebuilt. Outputs under
fresh `external-v1/t1b/` (T1's own `external-v1/t1-stores/` stayed
read-only; R1 worked from fresh copies under `external-v1/t1b-stores/`).
Memgraph was stopped for the timed window at `2026-10-08T20:50:19Z` and
restarted by the driver at completion, `2026-10-09T00:23:26Z`
(`HOST-T1B.log`'s own first/last lines) — same host protocol as T1.

**Addendum ARC5-B — the equality reading.** Every R1 cell's own
`t1-equality.json` reports `verdict: "L2"`, not T1's `L1`
(`l1_eventlog_sha_match: false` in all 19) — all 19 cells' own
`PROGRESS.log` line confirms `equality=L2`, `batches_realized=20/20` (or
`5/5` for the probe). This is not a regression: the edge-correction
records in this re-run carry **fresh, per-correction `disc` stamps**
(confirmed directly — e.g. `collegemsg-c1-deep-n1000-s0`'s batch 0
stamps `correction_disc="a1-d6b1931bec9a77cc"`, batch 1
`"a2-0d75d9f0aaae0247"`, each one different, never the fixed-per-class
`"#0"`/`"a2-disjoint"` the pre-Addendum-7 generator used), the fix landed
in commit `3b55042`. That commit postdates every export bundle under
`external-v1/export/` and predates this arc's fixes, so the exported
`digests.json` a cell is scored against still carries the old
fixed-per-class `disc` values — an **L1** byte-identical-eventlog match
is structurally impossible against that export now, for any run built
after `3b55042`, regardless of correctness. Everything else about the
run — `ttf_mode`, batch/record order, the `changed` lists, `refused`,
and every arm's `invalidated_count`/`false_fresh_count`/
`false_stale_count` — matches T1's on all batches, i.e. the fixes change
`disc` bookkeeping only and leave the scored logs byte-identical in
every other respect.

**Producing the evidence (19 rows, all batches).** For
`correction_class`/`correction_generator`/`correction_placement`/
`changed`, T1's committed `t1_equality.per_batch` (in
`tgms-control-2026-10-05-rows.jsonl`) and R1's own `t1-equality.json`
were each independently checked against the *same* immutable export
bundle's `digests.json`/`deltas.jsonl` — when both report
`all_match: true` for a batch, T1 and R1 are transitively identical on
that batch's correction_class/generator/placement/changed (two things
equal to the same third thing are equal to each other). For each arm's
`invalidated_count`/`false_fresh_count`/`false_stale_count`, T1's
committed per-batch counts (`tgms-control-2026-10-05-batches.jsonl`) were
diffed directly against R1's own raw per-batch rows (pulled read-only
from `external-v1/t1b/<cell>/storm-*-rows.jsonl`, sha256-recorded in
`tgms-control-2026-10-08-arc5-batches.SOURCES.txt`). `refused_count` is
reported for R1 only below — T1's 2026-10-05 committed addendum never
carried that field, so there is no committed T1 value to diff against
(stated as a gap, not assumed zero/equal); every R1 batch's own
`refused_count` is 0 regardless.

| cell_id | R1 verdict (vs. export) | correction-metadata match (vs. T1, transitive) | arm-count match (vs. T1, direct) | fully identical |
|---|---|---:|---:|---|
| `collegemsg-c1-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c1-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-days-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-hours-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-recent-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c4-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c4-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c1-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c1-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | L2 | 5/5 | 5/5 | yes |
| `synth-iv-60k-c3-days-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-hours-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-recent-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c4-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c4-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |

**19/19 cells fully identical** on every field this check could compare
(385 batches total: 18×20 + 5). No mismatch of any kind was found.

**Walls vs. T1's, and per-cell `ttf_p50_ms` for the three TGMS arms.**
R1 is dramatically faster than T1 — not a measurement artifact, but the
expected effect of the engine work landed between T1's commit
(`fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty`) and R1's
(`93d4543a`), including the check-cache chain setting itself (T1 never
set `--check-cache`, so every T1 check re-walked the full event log; R1
walks it once per batch and memoizes):

| cell_id | T1 wall (s) | R1 wall (s) | ratio (R1÷T1) | `global-recompute` p50 (ms) | `tgms-L0` p50 (ms) | `tgms-L1` p50 (ms) |
|---|---:|---:|---:|---:|---:|---:|
| `collegemsg-c1-deep-n1000-s0` | 8905.0 | 374.8 | 0.042 | 5036.5 | 3867.7 | 3844.1 |
| `collegemsg-c1-none-n1000-s0` | 9191.1 | 396.1 | 0.043 | 4898.8 | 4401.6 | 4375.9 |
| `collegemsg-c3-days-n1000-s0` | 8991.9 | 384.8 | 0.043 | 4999.0 | 4061.0 | 4037.5 |
| `collegemsg-c3-deep-n1000-s0` | 9155.7 | 381.8 | 0.042 | 4969.5 | 4241.6 | 4145.2 |
| `collegemsg-c3-hours-n1000-s0` | 8969.5 | 385.9 | 0.043 | 5050.3 | 4394.8 | 4317.1 |
| `collegemsg-c3-none-n1000-s0` | 8744.3 | 369.2 | 0.042 | 4849.2 | 3972.1 | 3938.8 |
| `collegemsg-c3-recent-n1000-s0` | 8512.2 | 359.3 | 0.042 | 5001.7 | 3753.3 | 3712.3 |
| `collegemsg-c4-deep-n1000-s0` | 9403.6 | 693.4 | 0.074 | 5092.5 | 4248.0 | 4198.4 |
| `collegemsg-c4-none-n1000-s0` | 9244.8 | 669.6 | 0.072 | 4973.8 | 4043.8 | 4026.9 |
| `synth-iv-60k-c1-deep-n1000-s0` | 9645.0 | 786.2 | 0.082 | 15484.7 | 8655.7 | 8568.7 |
| `synth-iv-60k-c1-none-n1000-s0` | 9652.0 | 779.9 | 0.081 | 15625.5 | 9058.2 | 9083.8 |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | 18922.9 | 1399.7 | 0.074 | 162369.6 | 128542.9 | 128435.7 |
| `synth-iv-60k-c3-days-n1000-s0` | 9377.6 | 735.2 | 0.078 | 15644.3 | 8840.1 | 8788.9 |
| `synth-iv-60k-c3-deep-n1000-s0` | 9536.1 | 754.4 | 0.079 | 15568.7 | 8953.3 | 8892.2 |
| `synth-iv-60k-c3-hours-n1000-s0` | 9340.4 | 748.5 | 0.080 | 15456.1 | 8748.6 | 8728.9 |
| `synth-iv-60k-c3-none-n1000-s0` | 9328.7 | 738.2 | 0.079 | 15603.3 | 7766.6 | 7727.3 |
| `synth-iv-60k-c3-recent-n1000-s0` | 9557.0 | 779.3 | 0.082 | 15625.7 | 9101.3 | 9070.8 |
| `synth-iv-60k-c4-deep-n1000-s0` | 10012.2 | 1040.5 | 0.104 | 15811.6 | 8974.2 | 8952.9 |
| `synth-iv-60k-c4-none-n1000-s0` | 9574.6 | 990.1 | 0.103 | 15904.9 | 5013.9 | 5004.5 |

Summed wall: T1 186,064.7 s (51.68 h) vs. R1 12,766.8 s (3.55 h) —
overall ratio **0.069** (R1 ≈14.6× faster end-to-end across all 19
cells), consistent with `HOST-T1B.log`'s own start/end timestamps
(`20:50:19Z` → `00:23:26Z` next day ≈ 12,787 s wall-clock for the whole
loop, matching the summed per-cell figure to within inter-cell
overhead). No verdict on what this ratio implies is drawn here — the
coordinator scores it.

**Records**: `tgms-control-2026-10-08-arc5.json` + `-rows.jsonl`,
assembled with `scripts/external_record.py --campaign tgms-control`
(extended this lane to carry `check_cache`/`check_cache_misses_median`/
`e2e_refresh_calls_median`, since T1's original 2026-10-05 run had no
per-batch sidecar on this host for the assembler to read those per-batch
fields from — see the assembler's own commit). `scripts/
check_result_manifest.py` passes. `tgms-control-2026-10-08-arc5-batches.jsonl`
+ `.SOURCES.txt` mirror the 2026-10-05 per-batch-rows addendum exactly
(one line per batch, byte-exact values, no rounding), built from the
same read-only tar pull of `external-v1/t1b/` this section's own
equality reading uses; sha256 of all four files is in `SHA256SUMS.txt`.

## Per-check cost addendum (2026-10-08): the field neither control record carries

Neither `tgms-control-2026-10-05-batches.jsonl` (the pre-arc control) nor
`tgms-control-2026-10-08-arc5-batches.jsonl` (the fifth-arc re-measurement
above) carries `candidate_survivors` in its per-batch rows, so the
same-host control's own per-check cost (`tgms-L0`/`tgms-L1`
`check_wall_ms` ÷ `candidate_survivors`, the quantity
`recStormV3PerCheckMs{Synth,CollegeMsg}` reports for the storm grids) was
landed as the literal text "not measured" in `scripts/sys_paper_macros.py`.
The per-batch rows on xzgpu do carry it: a read-only `scp` pull on
2026-10-08 of each cell's own `storm-*-rows.jsonl` from
`/mnt/project/xzhang/tgms/external-v1/t1b/<cell_id>/` (the fifth-arc
tree) and `/mnt/project/xzhang/tgms/external-v1/t1/<cell_id>/` (the
pre-arc tree) — nothing on xzgpu was modified, one `tar`/`scp` pull of
both trees together.

**Records**: `tgms-control-checks-2026-10-08.jsonl` — one line per
batch, both controls (730 lines total: 2 controls x (18 cells x 20
batches + 1 probe cell x 5 batches)), each line carrying `control`
(`2026-10-05` or `2026-10-08-arc5`), `cell_id`, `batch_index`,
`candidate_survivors`, `intersects_calls`, `tgms_l0_check_wall_ms`, and
`tgms_l1_check_wall_ms`, every value byte-exact (no rounding) from its
source `storm-*-rows.jsonl` row. `tgms-control-checks-2026-10-08
.SOURCES.txt` records each of the 38 pulled per-cell files' own sha256,
which control it belongs to, and its exact xzgpu path. Both files'
sha256 is in `SHA256SUMS.txt`.

**Computed medians** (median over the 9 N=1,000 cells per store of that
cell's own median-over-batches `tgms-L0.check_wall_ms` /
`candidate_survivors`, ms — same two-level-median convention
`recStormV3PerCheckMs{Synth,CollegeMsg}` uses):

| store | pre-arc (2026-10-05) | fifth-arc (2026-10-08-arc5) |
|---|---:|---:|
| collegemsg | 185.9 | 12.3 |
| synth-iv-60k | 204.0 | 12.3 |

The fifth-arc `--check-cache chain` setting (added to every R1 cell, see
the "Fifth-arc re-measurement" section above) drops the per-check cost
by roughly 16-17x on both stores, consistent with that section's own
observation that R1 "walks it once per batch and memoizes" instead of
re-walking the full event log on every check the way the pre-arc T1
control did. The probe cell (`synth-iv-60k-c1-none-n10000-s0`, N=10,000)
medians 11.9 ms/check on the fifth-arc side — landed separately
(`recExt1ControlArcFiveProbePerCheckMs`) rather than folded into either
store's N=1,000 median above. No verdict on what these numbers imply is
drawn here — the coordinator scores it.

## Re-run of the ten affected cells with the fixed translation (2026-10-09)

**Provenance.** Lane C3-records' re-run of the 10 Neo4j-recompute cells
named in the "Oracle-agreement summary" section above as disagreeing
(`n1b/`), on a `git archive` export of public main `08aba6a1efa049fdb409f056326a851240ecb0c2`
(xzgpu copy under `n1b-work/neo4j-recompute`, a plain `git archive`
checkout — `HOST-N1B.log` records every `.py` file's own sha256, not a
git checkout's own commit sha, since the xzgpu copy is not a git working
tree). `neo4j_recompute/queries.py`'s own sha256
(`0e58b584d524886c688357a3cf671e365c21aba9f6047488629f611f52a4e29e`) was
checked directly against the file at the fix commit `1d9e3edb` before the
grid ran (`HOST-N1B.log`'s own "sha256 of queries.py (must equal the file
at 1d9e3edb)" line) and matches. Assembled with
`scripts/external_record.py` at the current public-main version (copied
onto xzgpu's `c1-work/external_record.py`, sha256-verified equal to the
laptop's own `scripts/external_record.py` before running), against the
same `external-v1/export/` bundles the original N1 campaign and the T1
same-host control both checked against: `--campaign neo4j-recompute`
with 10 `--cell export/<cell> n1b/<cell>/result.json n1b/<cell>/check.json`
arguments, `--git-commit 08aba6a1efa049fdb409f056326a851240ecb0c2 --date
2026-10-09`. The ten cells: the nine `synth-iv-60k-{c1-deep,c1-none,
c3-days,c3-deep,c3-hours,c3-none,c3-recent,c4-deep,c4-none}-n1000-s0`
cells plus the N=10,000 probe `synth-iv-60k-c1-none-n10000-s0` — exactly
the 10 cells the original campaign's own per-cell oracle-agreement table
named as disagreeing (195 disagreements total, all tracing to the single
artifact `storm-000911`).

**The defect.** `storm-000911` is one generated artifact whose
`temporal_paths` query has `src == dst` — a path query whose start and
end node are the same artifact. The old (pre-fix) Cypher translation
cycled back through the source node when enumerating hop sequences for
this case, producing extra/incorrect path rows; the oracle (which
treats `src == dst` correctly) disagreed with Neo4j's answer at every
epoch this artifact was queried in every seed-0 `synth-iv-60k` cell (21
disagreements per N=1,000 cell x 9 cells = 189, + 6 at the N=10,000
probe = 195, matching the original record's own tally exactly). Fixed
in commit `1d9e3edb` (ledger id
`neo4j-recompute-temporal-paths-src-eq-dst-cycles`); this re-run is that
fix's measurement, not a new finding.

**Host protocol.** Same protocol as every other lane in this directory
— Memgraph (the project's other co-tenant store) stopped for the timed
window and restarted after, no co-tenant store running during a timed
cell, a host snapshot (`uptime`, `free`, top processes) immediately
before and after every cell. `HOST-N1B.log`: Memgraph stop issued at
`2026-10-09T00:28:13Z` (`docker stop memgraph`, "for the timed window"),
restarted at `2026-10-09T08:00:18Z` (`docker start memgraph`, the log's
own last control line) — a 7h32m timed window, all 10 cells, no
co-tenant store running throughout. Every cell's own `PROGRESS.log` line
reports `oracle=a/a` (every artifact-epoch pair compared, zero
disagreements) and `bursts=20/20` (or `5/5` for the probe).

**The 10-row table.** "wall s" is `n1b/PROGRESS.log`'s own `wall=<N>s`
field for this re-run; "N1 wall s" is the same field, same cell, from
the original campaign's `n1/PROGRESS.log` (committed nowhere in this
repo — both `PROGRESS.log` files are xzgpu-only, read directly for this
table, same convention this README's own "Estimates vs. actuals"
section above already uses for a `PROGRESS.log`-sourced number).
"oracle agree/n_compared" before/after and "recompute_ms_median"
before/after are each read from the two *committed* records
(`neo4j-recompute-2026-10-07.json` for "before", this section's own
`neo4j-recompute-2026-10-09-rerun.json` for "after") —
`summary.per_cell[*].oracle_agreement.{agree,n_compared}` and
`summary.per_cell[*].refresh_wall_ms.median` respectively.

| cell_id | wall s | N1 wall s | ratio | oracle agree/n_compared (before) | oracle agree/n_compared (after) | recompute\_ms\_median (before) | recompute\_ms\_median (after) |
|---|---:|---:|---:|---|---|---:|---:|
| `synth-iv-60k-c1-deep-n1000-s0` | 2397 | 2367 | 1.013 | 18543/18564 | 18564/18564 | 109687.5 | 111367.2 |
| `synth-iv-60k-c1-none-n1000-s0` | 2310 | 3173 | 0.728 | 18543/18564 | 18564/18564 | 118628.1 | 107182.4 |
| `synth-iv-60k-c3-days-n1000-s0` | 2340 | 2468 | 0.948 | 18543/18564 | 18564/18564 | 116540.6 | 108780.2 |
| `synth-iv-60k-c3-deep-n1000-s0` | 2347 | 2469 | 0.951 | 18543/18564 | 18564/18564 | 114723.5 | 109398.7 |
| `synth-iv-60k-c3-hours-n1000-s0` | 2211 | 2831 | 0.781 | 18543/18564 | 18564/18564 | 109054.3 | 102614.6 |
| `synth-iv-60k-c3-none-n1000-s0` | 2218 | 2467 | 0.899 | 18543/18564 | 18564/18564 | 110967.8 | 103276.1 |
| `synth-iv-60k-c3-recent-n1000-s0` | 2312 | 2395 | 0.965 | 18543/18564 | 18564/18564 | 108981.0 | 107974.7 |
| `synth-iv-60k-c4-deep-n1000-s0` | 2231 | 2896 | 0.770 | 18543/18564 | 18564/18564 | 123819.6 | 102700.3 |
| `synth-iv-60k-c4-none-n1000-s0` | 2260 | 2576 | 0.877 | 18543/18564 | 18564/18564 | 118870.3 | 105142.0 |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | 6494 | 6829 | 0.951 | 53526/53532 | 53532/53532 | 1092569.5 | 1069130.5 |

All 10 cells: 0 disagreements after the fix (`n_compared` unchanged,
`agree` now equal to `n_compared` on every cell) — the 195 disagreements
the original campaign reported are fully accounted for by
`storm-000911` alone, exactly as that section already stated, and are
now gone on the fixed translation. Wall time moved in both directions
cell-by-cell (ratio range 0.728-1.013, not a uniform speed-up or
slow-down) — consistent with ordinary host-load variance between two
runs on the same shared, non-exclusive host, not a property of the fix
itself (the fix changes `temporal_paths`'s query plan only for rows
where `src == dst`, a tiny fraction of each cell's total query mix).

**Record status.** The original campaign record
(`neo4j-recompute-2026-10-07.json` + `-rows.jsonl`) stays untouched —
this section's own "before" column reads it, nothing in it was edited.
It remains the record of what was measured on the pre-fix translation
(195 disagreements, stated plainly in this README's "Oracle-agreement
summary" section above). `neo4j-recompute-2026-10-09-rerun.json` +
`-rows.jsonl` is a new, separate record: the defect's re-measurement on
the fixed translation, not a correction or replacement of the original.

**Files.**

| file | cells | sha256 |
|---|---|---|
| `neo4j-recompute-2026-10-09-rerun.json` + `-rows.jsonl` | Neo4j 5.26 full recompute, fixed translation, the 10 affected cells | see `SHA256SUMS.txt` |

Validated: `scripts/check_result_manifest.py benchmarks/external-v1/neo4j-recompute-2026-10-09-rerun.json`
→ `ok: ... conforms to result_manifest.schema.json`.

**Macros** (`scripts/sys_paper_macros.py`'s `compute_external_rerun_n1b`):

| macro | value | field |
|---|---:|---|
| `recExt1RerunCells` | 10 | `neo4j-recompute-2026-10-09-rerun.json`'s `n_cells` / `len(summary.per_cell)` |
| `recExt1RerunAgreeCells` | 10 | count of `summary.per_cell[*]` with `oracle_agreement.disagree == 0` |
| `recExt1RerunDisagreements` | 0 | sum of `summary.per_cell[*].oracle_agreement.disagree` over all 10 cells |
| `recExt1RerunProbeAgree` | `53532/53532` | the probe cell's own `oracle_agreement.{agree}/{n_compared}`, as text |
| `recExt1RerunWallRatioMedian` | 0.927 | median over the 10 cells of (rerun wall / N1 wall); neither record's own `summary.per_cell` carries a field literally named `wall_s`, so both sides are each record's own `-rows.jsonl` sum of `result.per_cell.per_burst[*].wall_ms` (checked directly in `compute_external_rerun_n1b`, not assumed) — a different, narrower field than this section's own "wall s"/"N1 wall s" table columns above, which read `PROGRESS.log`'s full-cell `wall=` instead (that file is not committed, so the macro cannot use it) |


## Watermark hold duration under a withheld correction (2026-10-09)

**Pre-registration.** Memo P-EXT2-H, frozen 2026-10-09T14:30:27Z, before
this lane (D-W) ran anything: on the withheld-correction cell
`synth-iv-60k-c4-deep-n1000-s0` (the same cell "The withheld-correction
cell" section above scores — not a new cell), the F-watermark feeder's
hold duration is predicted to be exactly 1 burst interval, since batch
10's correction is withheld and delivered together with batch 11 by
construction of that cell's own delta schedule — "hold = 1 burst by
construction; ms ≈ the inter-burst wall." This section measures that
prediction for real rather than asserting it.

**What changed.** `external/ivm-dd`'s `withheld` module and `ivm-dd
withheld` CLI (public main, this lane's own commit) were extended to
report, for each feeder: per held artifact, the hold duration in bursts
and in milliseconds; and feeder totals `held_artifacts`, `refused_answers`,
`hold_ms_{median,min,max}`, `hold_bursts_median`. `held` is the mirror of
the existing `false_fresh` bookkeeping ("The withheld-correction cell"
section's own `ivm_f_epoch_false_fresh = 39`): an artifact is **held**
when the probe does *not* claim completeness (F-watermark, always) *and*
the served value would in fact disagree with the epoch-10 oracle — i.e.
exactly the 39 artifacts the F-epoch feeder would otherwise have served
silently wrong. `ivm-dd withheld` additionally now runs a real,
single-worker `dataflow::run` over the same changelog (the same per-burst
driver `ivm-dd run` uses) to measure `inter_burst_wall_ms`, the cell's own
median `refresh_ms + publish_ms` across its bursts — a genuine wall-clock
figure, not an assumption. Every pre-existing `withheld-result.json`
field is unchanged: an untimed smoke run of the new binary against this
cell reproduces the original `ivm_f_epoch_false_fresh = 39` /
`ivm_f_watermark_false_fresh = 0` and every existing per-artifact field
byte for byte; only new fields (`held`, the hold stats,
`inter_burst_wall_ms`) are added.

**Host protocol.** xzgpu, the same host as lanes N1/D1/T1. Build: a fresh
clone of the project mirror (`/mnt/project/xzhang/tgms/repo.git`, at
public main `4fced7e3daa26ad484d6a53f62553c118fb8c34b`) under
`external-v1/dw-work/repo`, this lane's working-tree diff applied on top
(laptop pushes nothing — the diff was copied over and `git apply`'d), then
`cargo build --release` (rustc/cargo 1.97.1, matching every other
external-v1 Rust build in this directory) and `cargo test --release` (41
tests, including 3 in `withheld_tests.rs` — the pre-existing snapshot
test plus a new tiny one-artifact/one-delayed-burst hold-duration test
and an invalid-epoch-ordering panic test) and `cargo clippy --release
--all-targets -- -D warnings`, all clean. `Cargo.lock` sha256
`505819c28e17948e80aa48f5e502fe95bde8aa25cc0d40dada009770a38d3d9e`
(unchanged from the 2026-10-07 build — no dependency changed); new binary
sha256 `5bb44ff6c406583730a797399304d1cffbe2f23894bda1b9917e0705357d59da`.

Timed run: host quiet immediately before (`uptime`: load average
0.69/1.15/0.79; no TGMS/Neo4j/dataflow process running; `HOST-DW.log`).
Memgraph (the project's other co-tenant store) stopped at
`2026-10-09T14:53:54Z` (`docker stop memgraph`) and restarted at
`2026-10-09T14:59:48Z` (`docker start memgraph`), both logged in
`HOST-DW.log`; no co-tenant store running during the timed window. `ivm-dd
withheld` run without `nice`, single worker (the CLI hard-codes
`workers=1`, same as every other lane in this directory): started
`2026-10-09T14:54:04Z`, ended `2026-10-09T14:59:31Z` — 5m27.71s wall per
`/usr/bin/time -v` (323.80s user + 3.87s system, 99% of one CPU, 4.34 GB
peak RSS, 0 major page faults, exit 0). Host snapshot taken again
immediately after (`host-snapshot-after.txt`).

**The numbers.** From this run's own `withheld-result.json`, restated by
`scripts/external_check.py::check_withheld` (independent of
`ivm_dd::record`'s own bookkeeping, same discipline as every other
`check.json` in this directory) and assembled into the standalone record
below:

| quantity | value | field |
|---|---:|---|
| held artifacts | 39 | `ivm_f_watermark_held_artifacts` — the same 39 artifacts `ivm_f_epoch_false_fresh` already named as affected |
| refused answers | 39 | `ivm_f_watermark_refused_answers` — one refusal per held artifact; this check models a single read point (R10) |
| hold, bursts (median) | 1.0 | `ivm_f_watermark_hold_bursts_median` — matches the frozen P-EXT2-H prediction exactly |
| hold, ms (median = min = max) | 69.523 | `ivm_f_watermark_hold_ms_{median,min,max}` — uniform across all 39 held artifacts, since every one shares the same 1-burst hold |
| inter-burst wall | 69.523 ms | `ivm_inter_burst_wall_ms` — this cell's own measured median `refresh_ms + publish_ms` across the dataflow run's bursts |

Hold ms equals the inter-burst wall exactly (both read from the same
`69.52254149999999`): P-EXT2-H's "ms ≈ the inter-burst wall" prediction
holds with 0 error here because the hold is a pure multiple (×1) of that
measured quantity by construction, not a coincidence of two independent
measurements landing close. `ivm_f_epoch_false_fresh = 39` and
`ivm_f_watermark_false_fresh = 0` are reproduced unchanged from "The
withheld-correction cell" section above (frozen there since 2026-10-07).

**Files.**

| file | cells | sha256 |
|---|---|---|
| `ivm-differential-2026-10-09-withheld-hold.json` | 1 (the withheld-correction cell; standalone, not folded into the 43-cell `ivm-differential-2026-10-07.json`) | see `SHA256SUMS.txt` |

Validated: `scripts/check_result_manifest.py
benchmarks/external-v1/ivm-differential-2026-10-09-withheld-hold.json` →
`ok: ... conforms to result_manifest.schema.json`. Assembled with
`scripts/external_record.py`'s new `--withheld-hold-only` mode
(`build_withheld_hold_record`), which carries
`external_check.py::check_withheld`'s own dict verbatim under
`summary.withheld` rather than inventing a 43-cell `per_cell`/
`predictions_measured` shape this single-cell record has no use for — the
schema's top-level required fields
(`schema_version`/`git_commit`/`timestamp_utc`/`machine`/`config`/`seed`/
`dataset`/`result_digest`/`protocol`/`record`) are generic enough to admit
this directly, so no README-documented ad hoc layout was needed.
`git_commit` is `4fced7e3daa26ad484d6a53f62553c118fb8c34b-dirty`: the
fresh clone's base commit (public main, the same commit every other
file in this directory's "Date assembled" header predates) plus this
lane's own patch, applied but not yet committed at measurement time (the
laptop commits separately, after the measurement — this directory's own
house convention, see the module docstring of `scripts/external_record.py`
and the "copy your working-tree changes" build step above).

**Macros** (`scripts/sys_paper_macros.py`'s `compute_external_withheld_hold`):

| macro | value | field |
|---|---:|---|
| `recExt2UnanswerableMs` | 70 | `ivm_f_watermark_hold_ms_median`, rounded — was the literal text "not measured" before this lane |
| `recExt2UnanswerableBursts` | 1.0 | `ivm_f_watermark_hold_bursts_median` |
| `recExt2WithheldHeldArtifacts` | 39 | `ivm_f_watermark_held_artifacts` |
| `recExt2WithheldRefusedAnswers` | 39 | `ivm_f_watermark_refused_answers` |
| `recExt2WithheldInterBurstMs` | 70 | `ivm_inter_burst_wall_ms`, rounded |

Every pre-existing `scripts/sys_paper_macros.py` macro (including
`recExt2WithheldFalseFresh{Ivm,Watermark,Tgms}` and the entire
`compute_external_rerun_n1b`/`compute_external_arc5` lanes) stays
byte-identical — a structural diff of the regenerated `.tex` against the
committed one shows only the 5 new macro lines above plus the
`recExt2UnanswerableMs` line changing from the literal text "not measured"
to `70`, and the assertion-count comment.
