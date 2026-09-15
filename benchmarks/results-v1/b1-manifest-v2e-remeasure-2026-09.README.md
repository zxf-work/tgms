# B1-v2e remeasure — chain-open component split + commit-cost phase attribution (2026-09)

Companion to `b1-manifest-v2-ab-2026-09.README.md`. That A/B left two cells
unscorable: (a) chain-open at G~10k, K=512 had no component split
(`NativeAdapter()` exposed no internal phase timer); (b) the engine-commit
last/first-decile growth (1.67x treatment, 1.73x control) sat entirely in an
untimed residual (`docs/design/B1V2_AB_DIAGNOSIS_2026-09-15.md` Q1). Main now
carries `open_phase_us()` (merge `bbf48c6`), `build_info()`, and the
fully-timed commit path with harness decile keys (`e5d4171`,
`scripts/bench_manifest_chain_open.py` / `scripts/eval_concurrency.py
commitcost`). This lane re-measures both cells with that instrumentation.
**No verdicts scored here** — that is the coordinator's job, same convention
as the v2 A/B.

Machine: xzgpu, 40 cores / 93 GB, `Linux-5.4.0-216-generic-x86_64-with-glibc2.31`.

Treatment commit `e5d4171f9` — built fresh into a new detached worktree
`work/tgms-xz-e5d4171` (`cargo build --release -p tgms-engine-py`, artifact
copied to `tgms/_engine.cpython-312-x86_64-linux-gnu.so`, same as the prior
lane). `build_info()` from that worktree:
`{'profile': 'release', 'opt_level': '3', 'engine_version': '0.8.0',
'debug_assertions': False, 'manifest_format_version': 3}`.

Control commit `886805f600bc` — the format-2 engine already pinned for the
24h longevity soak; **not rebuilt**. It lacks the new timers entirely:
`hasattr(_engine, 'build_info')` is `False`, `hasattr(_engine,
'open_phase_us')` is `False`. Control's own pinned `scripts/eval_concurrency.py`
and a raw `NativeAdapter()` wall-clock loop were used instead of the new
scripts, so control is reported with whatever keys its own toolchain
produces — no field was fabricated for it.

## Preconditions (recorded with timestamps)

Checked 2026-09-15T07:50:33Z–07:51:00Z: no `longevity_run`/`eval_*`/`bench_*`
processes running; OSV poller (pid 1457823, up 1d04h) and daily loop (pid
1540052, up 1d02h) alive and untouched; `/mnt/project` had 456 GB free of
880 GB; no `tar xf` phase-2 backup running at this baseline check.

**A phase-2 backup started mid-lane.** `ps aux | grep "[t]ar xf"` found
nothing at the 07:50–07:51Z baseline, but a `tar xf -` process (pid 2184056)
started at 07:56Z and was still running (confirmed by `ps aux`) through
every measurement in this lane: cell (b) both arms (08:08–08:09Z), cell (a)
treatment (08:10–08:12Z), and cell (a) control (~08:16–08:22Z). It was not
stopped — allowed per the lane brief — but is called out because cell (a)
control's open time is anomalously high relative to the prior clean-baseline
B1-v2 A/B (see below); concurrent I/O from this backup is the leading
suspect, not a claim.

## Cell (a) — chain-open component split at G~10k, K=512

Setup identical to the v2 A/B's B1(a)/(c): `scripts/build_snb_store.py
--backend native --batch 250 --compact-every 0 --digest manifest
--write-path assert`, same LDBC SF1 CSV path, killed at the `2,500,000 ops`
log line; 3 reps per arm, back-to-back in one process, immediately after
each arm's store build (process-cold first rep, warm thereafter).

| | treatment (e5d4171, G=10,365) | control (886805f, G=10,052) | threshold |
|---|---:|---:|---|
| manifest-chain component (`checkpoint_read_parse_us + merkle_verify_us + state_build_us + delta_replay_us`), median | **87,055 us (87.06 ms)** | not available | <= 70 ms (refuted > 100 ms) |
| — checkpoint_read_parse_us | 42,124 us | — | |
| — merkle_verify_us | 40,204 us | — | |
| — state_build_us | 1,828 us | — | |
| — delta_replay_us | 2,899 us | — | |
| dictionary_open_us, median | 1,066,460 us (1,066.5 ms) | not available | — |
| other_us, median | 388 us | — | — |
| total_us (engine-internal), median | 1,191,773 us (1,191.8 ms) | not available | — |
| open, wall-clock, 3 reps | 1,191.91 / 1,573.89 / 1,167.68 ms | 9,620.4 / 10,365.6 / 10,428.5 ms | — |
| open, wall-clock, median | **1,191.91 ms** | **10,365.63 ms** | total open <= 1.25x control |
| total-open ratio (treatment/control) | 0.115x | | (see caveat) |

**Component threshold:** 87.06 ms sits between the 70 ms pass line and the
100 ms refutation line — printed here, not scored.

**Dictionary open dominates, not the manifest chain.** Of the 1,191.8 ms
engine-internal median, 1,066.5 ms (89.5%) is `dictionary_open_us`; the
manifest-chain component the threshold names is only 87.06 ms (7.3%);
`other_us` is negligible (388 us, 0.03%). Whatever governs chain-open cost
at this store size is overwhelmingly the dictionary tail read/validate step,
not the checkpoint/merkle/delta-replay path the B1(c) threshold was written
against.

**The 0.115x total-open ratio should not be read as a clean win.** Control's
9.6–10.4 s open times are 4.6–5.0x the prior clean-baseline B1-v2 A/B's
control figures (1,427.6 / 2,279.2 / 2,247.9 ms, median 2,247.9 ms) at a
similar generation (10,052 vs. 10,759 then). The phase-2 tar backup was
confirmed running throughout this arm's measurement (above); I/O contention
is the leading candidate for that inflation. Treatment's own numbers
(1,167.7–1,573.9 ms) are close to its own prior-lane figures (1,252.7–2,691.3
ms) and to what its own `open_phase_us` accounts for almost exactly
(`named_phase_us_share_of_total_median` = 0.9997), so treatment's timing
looks self-consistent even under the same contention; control simply has no
internal timer to cross-check the same way.

## Cell (b) — commit-cost phase attribution, 100k-row seed, batch=1, 300 commits, K=512

Setup identical to the v2 A/B's B1(b): 3 reps per arm, same seed-by-row-index
method, `scripts/eval_concurrency.py commitcost --seed-rows 100000 --commits
300 --batch-sizes 1`; treatment ran at K=512 (default — `TGMS_MANIFEST_CHECKPOINT_EVERY`
left unset).

| | treatment (e5d4171, K=512) | control (886805f) | threshold |
|---|---:|---:|---|
| total_us first decile, median of reps | 3,311 us | 3,532 us | |
| total_us last decile, median of reps | 3,368 us | 5,990 us | |
| first->last decile ratio | **1.017x** | 1.696x | <= 1.10x (refute >= 1.2x) |
| total_us p50, median of reps | 3,365 us | 4,704 us | |
| p50 reps | 3,265 / 3,365 / 3,378 us | 4,776 / 4,678 / 4,704 us | |
| paired p50 ratio (treatment/control) | 0.715x | | <= 0.70x **or** <= 5.58 ms absolute |
| paired p50 absolute | 3.365 ms | | (passes the absolute bound; ratio 0.715 is just above 0.70) |
| residual_first_us, mean of reps | 33.1 us | not available | |
| residual_last_us, mean of reps | 30.4 us | not available | |
| dir_entries (seg / manifests), first commit | 6 / 4 | not available | |
| dir_entries (seg / manifests), last commit | 604 / 303 | not available | |
| build_info | `{profile: release, debug_assertions: False, manifest_format_version: 3}` | not available (no `build_info()` in this arm's engine or Python wrapper) | |

Control ran its own pinned `scripts/eval_concurrency.py`, whose `commitcost`
mode only ever produced `phase_p50_us` / `first_decile_us` / `last_decile_us`
— no `phase_decile_first_us`/`last_us`, `residual_first_us`/`last_us`,
`dir_entries_*`, or `build_info` fields exist for it to report; nothing was
computed or filled in on control's behalf.

### Phase-attribution table — where did the growth go?

| arm | first-decile total_us | last-decile total_us | growth | residual (mean, first->last) | verdict on residual |
|---|---:|---:|---:|---:|---|
| treatment (e5d4171, fmt3) | 3,311 | 3,368 | +57 us (1.7%) | 33.1 -> 30.4 us (flat, ~1% of total_us) | **closed** — the B1V2_AB_DIAGNOSIS memo's Q1 residual (previously 100% of the growth, +2,574 to +2,638 us at last decile across both format-3 and format-2 arms) is gone in this arm |
| control (886805f, fmt2, unchanged) | 3,532 | 5,990 | +2,458 us (69.6%) | not measured (no residual field) | **unchanged** — same order of magnitude as the original diagnosis's control residual (+2,638 us) and the prior lane's own 1.733x decile ratio; this arm never received the B1-v2d fix |

**Reading it plainly — corrected 2026-09-15.** This paragraph originally
credited "the fix that added full phase accounting" with making the
previously-untimed cost *disappear*, not just *visible*. That is not what
happened, and the B1-v2 A/B's own 2026-09-15 README correction
(`b1-manifest-v2-ab-2026-09.README.md`) is why: `db3fd6c1`/`e5d4171`'s own
commit message says "No behaviour change: every addition is an
`Instant::now()`/`perf_counter()` pair or a directory scan" — it is
instrumentation, not a performance fix, and it changed nothing about what
the commit path does. What actually changed between the two lanes is which
*chain format* the treatment was measuring. The B1-v2 A/B's treatment reps
(1.674x decile, tracking the control's 1.733x) were, per that correction,
silently running against a **format-2** chain despite the format-3 binary
timing them — `manifest_bytes` there is 1,565→1,568 B, byte-identical to
that lane's own format-2 control. This lane's treatment store was built
fresh by the `e5d4171` engine and confirmed on a genuine **format-3**
chain both by `build_info()` (`manifest_format_version: 3`) and by this
same `manifest_bytes` field, here 1,592→1,595 B. `Manifest::digest()`
(`crates/tgms-engine-core/src/manifest.rs:615-636`) dispatches its digest
rule on the manifest's own `format` field: format 3's Merkle root is
O(1)/O(log n) per commit, formats 1-2's whole-document `legacy_body_sha` is
O(segments). The flatness below is the format-3 path finally being
measured *as* format-3, not a change the instrumentation fix caused.

Every named phase (`manifest_us`, `seal_us`, `dict_us`, `current_us`, etc.)
was already flat across the decile in the old diagnosis; now `total_us` is
flat too (1.017x, well inside the <=1.10x band), and the residual that used
to absorb 100% of the growth sits at 30-35 us regardless of decile —
noise-sized next to a ~3,300-3,400 us total. `segments_named` still grows
35 -> 575 the same as before (confirmed in the raw per-commit records), so
the covariate the diagnosis pointed at is present; the O(segments) cost it
used to hide is not, because this arm is the first one actually running the
O(1)/O(log n) format-3 path the diagnosis's static reading (§1.3) said
should exist. Control, running the unmodified format-2 engine on a genuine
format-2 chain throughout, still carries growth of the same magnitude and
shape the diagnosis found (1.696x here vs. 1.733x in the v2 A/B) — the
O(segments) path neither lane's control ever left.

## Dataset

`dataset.digest` = `5600fe771ed412de7e219aa8046ac0fd0a06c4c1ff7192d35ea16dc6a4cf47a9`,
a freshly generated (2026-09-15) sha256 of
`b1-manifest-v2e-remeasure-2026-09-logs/dataset-manifest.txt` — one line per
file (`relative_path byte_size mtime_utc_iso sha256`), sorted by path, 55
files, covering the same
`.../composite-merged-fk/initial_snapshot` directory the v1/v2 lanes read.
Computed independently for this run (not copied from any prior record).

## Records and transfer

- `b1-manifest-v2e-remeasure-2026-09.json` — schema-conformant summary,
  validated against `benchmarks/schema/result_manifest.schema.json` via
  `scripts/check_result_manifest.py` (passed) on xzgpu. sha256
  `e7a8076c322e9b5e7691e23d15fc0170689d8c2268d63143407c25d55695a58e`.
- `b1-manifest-v2e-remeasure-2026-09-raw.json` — consolidated raw records
  (both cells, both arms, all reps). sha256
  `5059fc4367ebc4e5fe56aa02c03ac5efbc643be1d7114d7b01c75291e82960a5`.
- `b1-manifest-v2e-remeasure-2026-09-logs/` — verbatim per-rep JSON/log
  output plus `dataset-manifest.txt`; every file's sha256 verified
  byte-identical between the xzgpu copy (`/mnt/project/xzhang/tgms/tmp/b1v2e-work/`)
  and the copy committed here before the xzgpu copy was deleted.

Working store copies (`/mnt/project/xzhang/tgms/tmp/b1v2e-work/{treatment,control}-store`
and the ephemeral `tgms-cc-*` seed stores `commitcost` builds per rep) were
deleted from xzgpu after transfer. All three engine worktrees
(`tgms-xz-886805f`, `tgms-xz-7a5ff98`, `tgms-xz-e5d4171`) remain in place, as
instructed.

## Note on main's CI at e5d4171

Per the coordinator: main's CI at `e5d4171` is red only on an over-strict
bound in the new Rust timing test (a 201 us residual on a debug-build CI
runner); the engine code measured in this lane is unaffected, and a bound
fix is in flight. Not independently verified by this lane — recorded as
told, for the record's context.
