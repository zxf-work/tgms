# B1 A/B — incremental (delta) manifest (2026-09)

Pre-registered forecast and falsifiers:
`docs/design/INCREMENTAL_MANIFEST_FORECAST_2026-09-13.md` SS5/SS6 (untracked,
coordinator-internal; not in this checkout's git history — see
`docs/STABILITY.md`-adjacent internal-docs gitignore, commit `feaab3f`).

Machine: xzgpu, 40 cores / 93 GB, `Linux-5.4.0-x86_64`, Python 3.12.13.
Control commit `cb0e6afc17e7` (pre-B1/B2, format 1, `MANIFEST_FORMAT_VERSION`
absent — verified). Treatment commit `4af218192d8c` (format 2,
`MANIFEST_FORMAT_VERSION == 2`, checkpoint-every default 512). Both engines
built fresh on xzgpu into separate checkouts; never mixed `.so` files.

Records: `b1-manifest-ab-2026-09.json` (schema-conformant summary),
`b1-manifest-ab-2026-09-raw.json` (consolidated raw records), companion
`b1-manifest-ab-2026-09-logs/` (verbatim run logs + per-rep JSON, sha256
verified against the server copies before those were deleted).

## B1(a) — SF1 manifest bytes at matched 2.5M node ops

`scripts/build_snb_store.py --backend native --batch 250 --compact-every 0
--digest manifest --write-path assert`, killed at the `2,500,000 ops` log
line. **`--write-path assert` is required** — the default `bulk` path
checkpoints every 50,000 ops and does not reproduce the pathology (confirmed
by reading the script, not assumed from the memo's illustrative command).

| | control (cb0e6af) | treatment (4af2181) |
|---|---:|---:|
| manifest bytes | 25,970,987,762 B (25.97 GB) | 62,046,913 B (62.0 MB) |
| segment bytes | 146,613,591 B (146.6 MB) | 146,516,413 B (146.5 MB) |
| manifest share of store | 99.4% | 29.7% |
| wall to 2.5M ops | ~18m0s | ~16m3s |

Control reproduces the frozen pathology closely (baseline: 10,147 segments /
163 MB / 25,451 MB manifests at 2.5M ops). Treatment's 62.0 MB matches the
memo's F1 prediction (62 MB ± 30%) almost exactly, a **~419x** manifest-byte
reduction — decisive.

## B1(b) — SS20 batch=1 commitcost, 300 commits, 100k-row seed store

3 reps each (control, treatment default K=512), 1 rep per K-sweep value.

| | control | treatment (K=512) | treatment K=128 | treatment K=1024 |
|---|---:|---:|---:|---:|
| manifest_us: first->last decile | 799->1,718 (2.15x) | 761->826 (1.09x) | 752->760 (1.01x) | 756->808 (1.07x) |
| total_us (engine commit): first->last decile | 3,835->8,023 (2.09x) | 3,921->7,051 (1.80x) | 3,885->6,321 (1.63x) | 3,899->6,788 (1.74x) |
| engine-commit p50 (total_us) | 6,017 us | 5,584 us | 5,512 us | 5,557 us |

The manifest-write phase alone is now flat (ratio ~1.0-1.1x, as designed),
but **engine-commit total stays at ~1.8x growth** — the `successor` clone and
`seal()`'s serialize-plus-sha of the *reconstructed* manifest remain
O(segments) per commit, exactly the caveat the memo's own Addendum 1
anticipated ("F2 may fail on CPU even while bytes pass decisively"). And the
new p50 (5.58 ms) is *above* the historical 5.07 ms baseline, not 15% below
it — this run's own control p50 (6.02 ms) is also above that baseline
(~19% higher), so today's host/data conditions aren't identical to the
original measurement, but the paired treatment-vs-this-control improvement
is only ~7%, still short of 15% either way.

## B1(c) — cold/warm open at G~10k

Corrected methodology: times `NativeAdapter(store/"native")` construction
directly (the Rust-level `NativeStore::open`), **not** `tgms.open()` — the
latter additionally replays the event log (a CLI-code-documented, materially
larger cost) and gave misleading first-pass numbers (23-32 s) that are kept
in the raw-records file as `superseded_first_attempt`, not used for scoring.

| | control (G=10,014, format 1) | treatment (G=10,008, checkpoint=9,728 + 280 deltas) |
|---|---:|---:|
| open, 3 reps | 1,066 / 1,070 / 1,071 ms | 8,578 / 8,589 / 8,649 ms |

Treatment opens **~8x slower** than control at a comparable generation,
both far above the memo's predictions (control ≈305 ms predicted / 1,070 ms
measured; treatment ≤40 ms predicted / ~8,600 ms measured). Consistent with
the memo's own SS3(b) risk note that the implemented candidate (a) pays a
per-generation `open()` cost the rejected append-log candidate (b) would not
have — 281 separate small-file opens (1 checkpoint + 280 deltas) versus
control's one large sequential read.

## B1(d) — verify() wall, treatment store

9,833 ms over 10,008 generations / 2,502,000 rows, `healthy: true`, 0
problems. No falsifier threshold is attached to this figure; reported as
required.

## Falsifier 5 — invariant tests (treatment engine)

`tests/test_concurrency.py tests/test_native_faults.py
tests/test_tcsr_persistence.py`: **32 passed, 0 failed.**

## Verdicts (verbatim)

1. **F1** manifest bytes <= 500 MB at 2.5M ops: **PASS** (62.0 MB).
2. **F2** both commit phases' last/first decile <= 1.2x: **REFUTED** — the
   manifest-write component passes (1.04-1.13x) but the engine-commit-total
   component fails (1.78-1.80x); the falsifier is an OR of the two.
3. **F3** cold open at G~10k, K=512, <= 100 ms: **REFUTED** — measured
   ~8,578-8,649 ms, ~86x over bound, and slower than the control it was
   meant to beat.
4. **F4** engine-commit p50 >= 15% below the 5.07 ms baseline: **REFUTED** —
   measured 5.58 ms, above the baseline itself.
5. **F5** no invariant test regressions: **PASS**.

**Overall: REFUTED (3 of 5).** The byte-savings claim is decisive and the
invariants hold, but every CPU/latency claim in the forecast (F2, F3, F4)
is refuted by this measurement. F3 in particular is a large, reproducible
(3 reps, ~1% spread), and previously-unflagged regression: opening the
new format is *slower*, not faster, than the format it replaces at a
comparable generation.
