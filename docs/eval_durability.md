# Durability under injected crashes

The commit protocol (store.rs §doc, spec §4/§5.2) has been *argued* safe and
tested at constructed states: the Rust crash-step matrix builds the on-disk
state a crash would leave and checks `open`; two Python tests do the same for
the CURRENT flip and interrupted compaction. What has never existed is the
stronger instrument: **a real process killed at instrumented points mid-write,
through the full stack** — Python event log, engine commit, suffix recovery —
with the four durability questions answered by machine afterward:

- **Q1 acknowledged-write survival** — every batch whose write call returned
  is present after reopen + recovery; the in-flight batch is all-or-nothing.
- **Q2 deterministic recovery** — the recovered store's digest equals a clean
  replay of the same log prefix into a fresh store.
- **Q3 single-generation visibility** — `open` serves exactly the previous or
  the next generation, `verify()` clean, never a blend.
- **Q4 orphan reclamation** — after `compact()`+`gc()`, no unreferenced files
  remain from the crashed attempt.

- harness: `scripts/eval_durability.py` (parent spawns a child per boundary ×
  trial; the child crashes via `TGMS_CRASH_POINT`/SIGKILL; the parent
  recovers and interrogates)
- engine crash points: `crash_point(name)` in the native store, active only
  when `TGMS_CRASH_POINT` is set
- decision record: D-086

## The ten boundaries

| # | boundary | mechanism |
|---|---|---|
| B1 | mid event-log append (torn record, no fsync) | `crash_point("py_torn_wal_append")` |
| B2 | after log fsync, before `apply_ops` | `crash_point("py_after_wal_fsync")` |
| B3 | after `apply_ops`, before engine commit begins | `crash_point("py_before_engine_commit")` |
| B4 | after ALL segments are sealed, before close runs | engine `crash_point("after_seal")` |
| B5 | after close runs are written, before dict append | engine `crash_point("after_close_runs")` |
| B6 | after dict append, before the manifest write | engine `crash_point("after_dict")` |
| B7 | after the new manifest is fsynced and renamed into place, before `CURRENT` is updated | engine `crash_point("after_manifest")` |
| B8 | same physical point as B7 — `after_manifest` fires once, after the rename `write_atomic` already fsyncs; there is no separate "before rename" point to crash at | engine `crash_point("after_manifest")` |
| B9 | after `CURRENT` flip, before return | engine `crash_point("after_current")` |
| B10 | mid compaction publish / mid gc deletion | engine `crash_point("compact_before_install"/"gc_mid_delete")` |

Note: B1–B3 are now product-side `crash_point` calls in
`tgms/storage/eventlog.py::EventLog.append` and
`tgms/store.py::Store._write_locked` (previously harness-local monkeypatches
of those methods) — same `TGMS_CRASH_POINT` env-var protocol as the
engine's, so every boundary is selected identically. B4 and B7 do **not**
catch a segment or manifest
*mid-write* the way this table originally described: `after_seal` fires
only once every segment in the batch has already been sealed (there is no
per-segment crash point), and `after_manifest` fires only after
`write_atomic` has already fsynced the temp file and renamed it into place
— so B7 and B8 observe the same on-disk state. Genuine mid-file tears (a
half-written `.tgs`, a half-written manifest before rename) are covered
instead by the constructed-state tests, not by process injection (D-086,
`docs/DECISIONS.md:4479-4480`).

## Forecast, written 2026-08-06 before the instrument (D-086; score after)

Workload per trial: seed batches, then N acknowledged correction batches,
then the crash batch. Small scale (CI-sized stores); every trial also runs
at 20% correction density so close runs exist at most boundaries.

- **F1 — B4–B8 all recover to the *previous* generation, Q1–Q4 clean.**
  This is the design's central claim (publish files first, flip last) and
  the constructed-state tests already cover B7/B8 shapes. Injection should
  agree. Any failure here is a serious engine defect.
- **F2 — B9 recovers to the *next* generation** (CURRENT is the publication
  point), and the crash batch counts as committed even though the caller
  never saw success — the honest wording for Q1 is "returned-success implies
  present", not the converse.
- **F3 — B1 is the boundary most likely to produce a real finding.** A torn
  final log line must be detected and truncated by recovery before replay;
  the dict has explicit orphan-tail truncation, but I cannot point to the
  equivalent code for the event log's last record, and the chain verifier
  "fails loudly" — which may mean *refuses to open* rather than *truncates
  and proceeds*. Forecast: **at least one of B1/B2 yields either a defect or
  an unhandled recovery path requiring a code change**, and I name B1 as the
  likelier.
- **F4 — B3 is invisible** (nothing durable changed engine-side; the log
  record replays the batch on recovery, so Q1 holds via replay, not via the
  store).
- **F5 — B10-compaction leaves the store readable on the old generation
  (existing constructed-state result), and B10-gc under-collects only** —
  a crash mid-gc may leave orphans (Q4 needs the *next* gc to reclaim them)
  but never removes anything a manifest still references.
- **F6 — recovery is deterministic everywhere (Q2) except possibly after
  B1**, where the answer depends on whether truncation is implemented; if it
  is, Q2 holds there too.

## Results — 30 trials, and the boundary the forecast named

First run: **9 of 10 boundaries clean; `torn_wal_append` made the store
refuse to open** (`StateError: event log … not readable at offset …`) — a
routine power cut mid-append was an outage requiring manual log surgery.
After the fix (`EventLog.trim_torn_tail`, tests-first in
`tests/test_torn_wal.py`): **all 10 boundaries pass 3/3 trials on all four
questions.** Receipt `eval-durability-injection.json`.

One harness lesson worth keeping: the first run also flagged seven
boundaries as "acked value superseded by the crash batch" — which is not a
violation but the write-ahead design working. A batch whose log record was
fsynced before the crash is replayed on recovery even though the caller
never saw success; Q1's contract is "returned-success implies present",
and the converse direction is deliberately open. The harness first
encoded the wrong contract, and the instrument corrected its author.

## Scoring the forecast (D-086)

- **F1 — B4–B8 recover to the previous generation, Q1–Q4 clean: CONFIRMED**
  (with the Q1-contract caveat above: "previous generation" plus the
  replayed suffix, which is the design).
- **F2 — B9 (`after_current`) counts as committed: CONFIRMED** — the crash
  batch is visible, Q1's wording held.
- **F3 — B1/B2 yields a real finding, B1 the likelier: CONFIRMED exactly.**
  The torn final record was unhandled; the dict had orphan-tail truncation
  and the event log did not. One real defect, found by the instrument on
  its first run, fixed the same day with the contract flip pinned in tests
  (an old test asserted the outage behaviour as correct and was rewritten).
- **F4 — B3 invisible: WRONG, instructively.** Nothing engine-side changed,
  but the batch is *not* invisible — its WAL record replays on recovery.
  The forecast forgot the write-ahead half of the design it was
  forecasting; the same misunderstanding briefly lived in the harness's Q1.
- **F5 — compaction crash readable, gc under-collects only: CONFIRMED**
  (gc's orphans are reclaimed by the trial's own follow-up gc — Q4 green).
- **F6 — determinism everywhere, B1 dependent on truncation: CONFIRMED**;
  with the trim implemented, Q2 holds at every boundary including B1.
