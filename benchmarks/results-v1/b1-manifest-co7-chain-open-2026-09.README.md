# P-CO7 — chain-open re-measurement on a confirmed-quiet host (2026-09)

Companion to `b1-manifest-v2e-remeasure-2026-09.README.md`. That lane's cell
(a) ran under a concurrent phase-2 `tar` backup it never controlled for;
this lane re-runs exactly the same cell (a) protocol — nothing else — with
the host verified quiet at every precondition check, so the earlier
contention caveat can be checked rather than assumed. **No verdicts scored
here**, same convention as v2 and v2e.

Machine: xzgpu, 40 cores / 93 GB, `Linux-5.4.0-216-generic-x86_64-with-glibc2.31`.

Treatment commit `ebe1dc2` — built fresh into a new detached worktree
`work/tgms-xz-ebe1dc2` (`cargo build --release -p tgms-engine-py`, artifact
copied to `tgms/_engine.cpython-312-x86_64-linux-gnu.so`). `build_info()`:
`{'profile': 'release', 'opt_level': '3', 'engine_version': '0.8.0',
'debug_assertions': False, 'manifest_format_version': 3}`.

Control commit `886805f600bc` — the format-2 engine pinned for the 24h
longevity soak; **not rebuilt**, no `open_phase_us()`. Timed with a raw
`NativeAdapter()` wall-clock loop, same method v2/v2e used for control.

## Preconditions (recorded with timestamps)

Checked 2026-09-15T12:12:10Z–12:24:07Z (before both store builds) and again
2026-09-15T12:40:47Z (before the control open measurement): no
`longevity_run`/`eval_*`/`bench_*`/`tgms replay` processes; no
`tar`/`rsync`/`sha256sum` processes running; OSV poller (pid 1457823) and
daily loop (pid 1540052) alive and untouched; 414 GB free of 880 GB under
`/mnt/project`. The soak replay process this lane was dispatched to wait
for was already confirmed dead (OOM-killed 11:59:50Z, retry stopped
12:11Z) before this lane started; `ps aux | grep "[t]gms" | grep replay`
was empty at every check. No `longevity_report.py` process was observed
running at any check point. The `replay-check/` store under the longevity
directory was not touched.

## Cell (a) — chain-open component split at G~10k, K=512

Setup identical to v2/v2e's B1(a)/(c): `scripts/build_snb_store.py
--backend native --batch 250 --compact-every 0 --digest manifest
--write-path assert`, same LDBC SF1 CSV path, killed at the
`2,500,000 ops` log line; 3 reps per arm, back-to-back in one process,
immediately after each arm's store build (process-cold first rep, warm
thereafter).

| | treatment (ebe1dc2, G=10,116) | control (886805f, G=10,042) | v2e frozen prediction | threshold |
|---|---:|---:|---:|---|
| checkpoint_read_parse_us, median | 12,670 | — | 42,124 | <= 25,000 us predicted |
| merkle_verify_us, median | 34,858 | — | 40,204 | +-10% of v2e |
| state_build_us, median | 5,935 | — | 1,828 | +-10% of v2e |
| delta_replay_us, median | 6,256 | — | 2,899 | +-10% of v2e |
| manifest-chain component (sum of the four above), median | **59,719 us (59.72 ms)** | not available | 87,055 us (87.06 ms) | <= 70,000 us (refuted > 75,000 us) |
| dictionary_open_us, median | 1,092,729 (1,092.7 ms) | not available | 1,066,460 (1,066.5 ms) | +-10% of v2e |
| total_us (engine-internal), median | 1,155,032 (1,155.0 ms) | not available | 1,191,773 | — |
| open, wall-clock, 3 reps | 1,182.15 / 1,155.30 / 1,071.95 ms | 9,281.93 / 10,100.24 / 10,184.33 ms | — | — |
| open, wall-clock, median | **1,155.30 ms** | **10,100.24 ms** | control ~2.2s (quiet), 10.4s (v2e, under transfer) | total open <= 1.25x control |
| total-open ratio (treatment/control) | 0.1144x | | | (see note) |

**Control's open time did not drop under a confirmed-quiet host.** No
`tar`/`rsync`/other backup process was present at either precondition
check surrounding this cell — unlike v2e, where a phase-2 `tar xf` ran
throughout. Control's median open time here (10.10 s) is close to v2e's
contended figure (10.37 s) and roughly 4.6x the frozen quiet-control
prediction (~2.2 s) inherited from the earlier clean-baseline v2 A/B. This
is recorded as measured; no cause is investigated or claimed here, and the
v2e caveat attributing that inflation to I/O contention should not be read
as confirmed or refuted by this lane alone — that reading is left to the
coordinator.

Treatment's own component figures moved in both directions relative to
v2e's single prior sample: `checkpoint_read_parse_us` and `merkle_verify_us`
came in below the v2e figure (12,670 vs 42,124; 34,858 vs 40,204) while
`state_build_us` and `delta_replay_us` came in above (5,935 vs 1,828;
6,256 vs 2,899) — the manifest-chain component sum (59,719 us) nonetheless
lands under the 70 ms pass line this time, versus v2e's 87,055 us between
the pass and refutation lines. `dictionary_open_us` (1,092.7 ms) is within
+-10% of v2e's 1,066.5 ms.

## Dataset

`dataset.digest` = `5600fe771ed412de7e219aa8046ac0fd0a06c4c1ff7192d35ea16dc6a4cf47a9`,
a freshly generated (2026-09-15) sha256 of
`b1-manifest-co7-chain-open-2026-09-logs/dataset-manifest.txt` — one line
per file (`relative_path byte_size mtime_utc_iso sha256`), sorted by path,
55 files, covering the same `.../composite-merged-fk/initial_snapshot`
directory the v1/v2/v2e lanes read. Byte-identical to the v2e lane's
digest (corpus unchanged); computed independently for this run, not
copied from any prior record.

## Records and transfer

- `b1-manifest-co7-chain-open-2026-09.json` — schema-conformant summary,
  validated against `benchmarks/schema/result_manifest.schema.json` via
  `scripts/check_result_manifest.py` (passed) on xzgpu.
- `b1-manifest-co7-chain-open-2026-09-raw.json` — consolidated raw record
  (preconditions, both arms, all reps).
- `b1-manifest-co7-chain-open-2026-09-logs/` — verbatim per-rep JSON output
  (`treatment-chain-open.json`, `control-chain-open.json`), both stores'
  build logs, and `dataset-manifest.txt`; every file's sha256 verified
  byte-identical between the xzgpu copy
  (`/mnt/project/xzhang/tgms/tmp/co7-work/`) and the copy committed here
  before the xzgpu working copies were deleted.

Working store copies (`/mnt/project/xzhang/tgms/tmp/co7-work/{treatment,control}-store`
and the transferred logs) were deleted from xzgpu after transfer. Both
engine worktrees (`tgms-xz-ebe1dc2`, `tgms-xz-886805f`) remain in place, as
instructed.
