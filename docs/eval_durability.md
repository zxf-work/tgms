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

## EXP-A1 — 10,000 seeded trials (2026-09-13)

Lane A task A7 (OSDI plan): the 30-trial result above, scaled three orders
of magnitude on the seeded/randomized workload generator (D-086 P0.7,
`--seed`) rather than the fixed legacy workload, as a Slurm array on the
iTiger cluster (20 tasks × 50 trials × 10 boundaries = **10,000 trials**,
commit `cb0e6af`). Full protocol, host list, and the two infrastructure
deviations forced by smoke-testing the campaign script (node-local
`TMPDIR`, a `git` shim for `--json`'s commit stamp) are in
`benchmarks/crash-v1/README.md`; the merged, schema-conformant record is
`benchmarks/crash-v1/eval-crash-campaign-2026-09-13.json`
(`scripts/crash_campaign.slurm` + `scripts/crash_campaign_merge.py`).
Every number below is read from that record, not hand-typed.

| boundary | trials | problems |
|---|---:|---:|
| `py_torn_wal_append` | 1000 | 0 |
| `py_after_wal_fsync` | 1000 | 0 |
| `py_before_engine_commit` | 1000 | 0 |
| `after_seal` | 1000 | 0 |
| `after_close_runs` | 1000 | 0 |
| `after_dict` | 1000 | 0 |
| `after_manifest` | 1000 | 0 |
| `after_current` | 1000 | 0 |
| `compact_before_install` | 1000 | 0 |
| `gc_mid_delete` | 1000 | 0 |
| **total** | **10000** | **0** |

**Result: 10,000/10,000 trials clean at every boundary, on every one of
Q1–Q4.** No failure to report — the campaign that could have surfaced a
scale-dependent or randomization-dependent crash/recovery defect (a rare
interleaving the 30-trial fixed-workload run had no chance to hit) did
not find one. This raises confidence in the commit-protocol's durability
under the ten instrumented boundaries at this scale; it is not evidence
about boundaries, concurrency shapes, or failure modes (concurrent
writers, multi-process crashes, disk-level corruption) this harness does
not exercise. Wall-clock: 16m1s for the full array (job 210821, `--array
=0-19%6`, itiger01/itiger02); result_digest and file sha256 are in
`benchmarks/crash-v1/README.md`.

## EXP-A2 — crashing recovery itself (Lane A, added 2026-09-13)

Everything above answers "what if the *write* path is interrupted?" This
experiment asks the next question: what if **recovery's own replay of the
un-applied suffix** is interrupted, possibly more than once in a row before
the process ever gets a clean start? `Store._recover` (`tgms/store.py`)
carries four crash points for exactly this:

| point | fires |
|---|---|
| `py_recover_after_trim` | the torn tail (if any) is trimmed; nothing in the suffix has been replayed yet |
| `py_recover_before_cursor_publish` | the first un-applied batch's rows are staged in the engine; the cursor has not been touched |
| `py_recover_after_cursor_publish` | the cursor is staged in process memory (`note_event_cursor`); the atomic commit that would make it durable has not run |
| `py_recover_mid_replay` | that commit has landed (batch and cursor durable together, one manifest generation); the loop has not moved on |

A fifth point, `py_tcsr_mid_rebuild` (`tgms/storage/tcsr.py`, after the
permutation is computed, before its generation/manifest_sha stamp is
written), is architecturally adjacent but cannot interrupt `_recover` — no
bare `tgms.open()` calls `adapter.tcsr()` — so it is exercised on its own,
not by the harness mode below.

**Design, not results — no numbers here** (the harness run for this report
was 2 seeded trials, `--recovery-crash --trials 2 --seed 11`, a smoke check
of the mechanism, not a scored sweep):

- **Harness (`scripts/eval_durability.py --recovery-crash`):** per trial —
  seed a store with K acknowledged batches, crash it at a random *write*
  boundary (the existing B1-B10 machinery, minus `after_current`, which
  leaves nothing in the suffix for recovery to interrupt), then crash
  *recovery itself* under a fresh, seed-derived random point from the table
  above, 1-4 times in a row, and finally let one recovery run
  uninterrupted. Q1-Q4 are the same four questions the boundary matrix
  already answers.
- **Q5 — convergence, the property new to this experiment.** `tt` is a
  hybrid-logical clock seeded from wall-clock microseconds
  (`tgms/core/clock.py`), not from the trial seed, so literally re-running
  the write phase twice produces two different (but each internally valid)
  histories — comparing their digests would not test recovery at all. So
  the harness runs the write phase *once*, producing one on-disk checkpoint
  (log plus whatever the crash left of the backend), copies it twice, and
  replays the *identical* seed-derived crash-point sequence against each
  copy independently. Q5 holds when both copies converge to the same
  digest as each other and as a clean replay of the (shared) log — i.e.
  recovery's outcome is a function of the durable log and the crash
  sequence alone, not of which of two identical attempts happened to run.
- **Tests** (`tests/test_crash_during_recovery.py`): one subprocess test per
  recovery point, spawned the same way `tests/test_crash_points.py` spawns
  the write-path ones — child crashes (exit 137), a second open completes,
  `verify()` is clean, and the digest matches a clean replay — plus one test
  that a `py_tcsr_mid_rebuild` crash leaves the store answering correctly
  on the next open (the index just rebuilds live).

## EXP-A3 — the corruption-detection sweep (Lane A task A4, design written 2026-09-13)

EXP-A1/A2 above answer "what if the write, or its own recovery, is
interrupted mid-flight?" — a process killed at an instrumented point. This
experiment asks a different question, about a store nobody is actively
writing to any more: **if one on-disk file is corrupted after the fact —
bit rot, a bad disk, a stray `dd`, a hand-edit — does the engine's own
"never expose an undetected inconsistent generation" promise
(`tests/test_native_faults.py`'s framing) actually hold, file by file and
mutation by mutation?**

**Design, not results — no numbers here** (this section documents the
harness and its local smoke testing, never a scored campaign; the campaign
itself runs later on iTiger, coordinator-run, per the PI no-local-experiments
rule).

- **Harness:** `scripts/eval_corruption.py`. Per trial: build one small
  store — a seeded random workload (`scripts/eval_durability.py`'s
  generator, reused directly), a correction (so a close run exists), one
  `compact()` (forces a checkpoint manifest and supersedes everything
  before it), a few more corrections after compaction (so a delta manifest
  and a fresh, live close run exist too, with `TGMS_MANIFEST_CHECKPOINT_EVERY`
  set low so a delta reliably appears within a handful of writes), one
  `temporal_paths` call (persists the TCSR index), and two registered
  artifacts with on-disk plan blobs. Then: pick one of 13 file classes and
  one of 7 mutations, both uniformly at random; apply it to exactly one
  file; observe, never repair.
- **The 13 file classes** — every on-disk family the layout comments in
  `crates/tgms-engine-core/src/{manifest,manifest_chain,segment,visibility,
  dict}.rs` name: `event_log_record`, `event_log_tail`, `segment_header`,
  `segment_body`, `segment_footer`, `close_run`, `dict_tail`,
  `checkpoint_manifest`, `delta_manifest`, `current`, `tcsr_file`,
  `artifacts_jsonl`, `artifact_blob`.
- **The 7 mutations** — `flip_bit`, `flip_byte`, `zero_span` (64 bytes),
  `truncate` (a random small byte count), `append_garbage`,
  `swap_same_class` (two files of the class trade contents; falls back to
  `flip_byte` with a recorded `note` when fewer than two candidates exist —
  true for every singleton class: `current`, `dict_tail`, `tcsr_file`,
  `artifacts_jsonl`), `delete_file`.
- **Four observations, in order**, matching task A4's literal spec:
  1. `tgms.open(store, backend="native", read_only=True)`.
  2. `store.adapter.verify()` (native engine only — never reads
     `eventlog.jsonl` or `artifacts.jsonl`).
  3. two fixed read queries, `entity_history` and `snapshot_subgraph`,
     against two uids seeded into every trial, compared by `result_digest`
     to a pre-mutation baseline of the same queries.
  4. an artifact-check outcome: `Registry(store)` (which chain-verifies
     `artifacts.jsonl` on load) plus `witness.check_artifact` against the
     event log, for both registered artifacts.
  A fifth, class-gated check runs only for `tcsr_file` trials: force a
  traversal query and see whether the persisted index was re-stamped for
  the current generation (a silent, correct rebuild — not an error, not
  silence about wrong data).
- **Verdicts:** DETECTED (open refused, verify found something, a query
  raised, or the artifact check raised), SILENT (everything succeeded but a
  query's result digest changed — the campaign gate is this count is
  always zero), BENIGN (everything succeeded and every digest matched —
  explained per class: a file `compact()` had already superseded before the
  mutation ran, a plan blob `check()` never reads, or a tail mutation within
  the trim contract), TOLERATED-REBUILT (the TCSR index alone).

**A finding from local smoke testing (5-trial and 40-trial runs, seeds 1 and
42; not a scored result), corrected into the harness's own docstring rather
than left as an assumption:** `tgms.open(..., read_only=True)` does **not**
fully skip event-log sensitivity the way `Store._recover`'s own docstring
("read-only skips crash recovery") suggests in isolation. `Store.
_seed_frontier` runs unconditionally, even for a reader — it has to, to
avoid seeding a false-fresh belief-time frontier (`tgms/store.py`'s own
comment) — and it walks the *entire* event log from byte 0
(`EventLog.batches_from(0)`) with no torn-tail tolerance of its own. So a
torn *tail* that D-086's trim contract (`tests/test_torn_wal.py`) says a
**writer**-mode open should silently truncate and recover from is instead
DETECTED by this harness's read-only open, via a raised `StateError`, before
`Store._recover`'s replay/trim logic ever runs at all. Pinned by
`tests/test_eval_corruption.py::test_torn_event_log_tail_is_detected_even_read_only`,
which also re-confirms the trim contract itself still holds under a plain
writer-mode open on the same corrupted log. Net effect: this harness's
choice of `read_only=True` (task A4's own spec) makes the sweep *more*
strict about event-log tail damage than a real writer process reopening the
same store would be — worth knowing before reading a high detection rate
for `event_log_tail` as proof the trim contract "works," when it is
actually proof this particular observation never gets to exercise it.

A second finding, same smoke runs: deleting `CURRENT` does not make the
native engine refuse to open — `NativeStore::open` (`store.rs`) treats a
missing `CURRENT` as "this is a freshly initialized, empty store," not as
corruption. This harness's fixed queries still catch it (`entity_history`
on a uid that "doesn't exist" in the now-apparently-empty store raises
`NotFoundError`, classified DETECTED via the query-refusal path), but only
because the queries target uids guaranteed to exist in a healthy store — a
read shaped as "does X exist" rather than "assert X's known history" would
not have noticed. Worth a callout for whoever designs the `--full` verify
report task A4's own instructions leave room for: `CURRENT`'s absence
arguably deserves classifying as corruption in its own right, not only as
"whatever reads it next may or may not complain."

**Fixed, 2026-09-15 (Lane A task A8).** `NativeStore::open` (`store.rs`) no
longer treats a missing `CURRENT` as an empty store by default. On open, if
`CURRENT` is absent, the engine now inspects the rest of the directory: a
genuinely empty layout (no manifest files, no segments, an empty or absent
dictionary log) still opens fresh, and so does the one legitimate case where
"nothing published yet" is true despite files existing — a crash between the
bootstrap genesis manifest's write and its `CURRENT` flip, i.e. exactly one
manifest file, generation 0, with no segments and no dictionary tail. Every
other populated-but-`CURRENT`-less shape now refuses with a `Corrupt`-family
error naming the manifest and segment counts found and pointing at the two
remedies ("remove the orphans" or "restore `CURRENT`"). The `current` /
`delete_file` cell in `scripts/eval_corruption.py`'s sweep now classifies
DETECTED via the open step itself, the same as every other construction-time
check — the "detected only via the query-refusal path, and only because the
probe uids happen to exist" caveat above no longer applies; see
`tests/test_native_faults.py::test_current_deleted_refuses_to_open_read_write_and_read_only`
and `tests/test_eval_corruption.py::test_deleted_current_is_detected_via_open`.

Slurm campaign: `scripts/corruption_campaign.slurm` (`bigTiger`, 40 array
tasks × 250 trials = 10,000 trials, `%6`) + `scripts/
corruption_campaign_merge.py --kind corruption`, producing
`benchmarks/corruption-v1/`, reusing the two infrastructure fixes
`scripts/crash_campaign.slurm`'s smoke testing already found (node-local
`TMPDIR`, the `git` shim for `TGMS_COMMIT`) rather than rediscovering them.

## EXP-A5 — disk-full / short-write injection (Lane A task A5, design written 2026-09-13)

Real `ENOSPC` needs either root (a loopback filesystem sized to fail) or an
actual full disk — neither available under the PI no-local-experiments rule
for a harness whose campaign runs later, elsewhere. Instead: a **Python
call-boundary injector**, active only inside a short-lived child process
`scripts/eval_diskfull.py` spawns per trial (the same parent/child split as
`scripts/eval_durability.py`), controlled by two env vars the child alone
sets before opening a store — `TGMS_DISKFULL_AT=<n>` (the n-th call across a
shared counter over `os.write`/`os.fsync`/`os.rename`/`os.replace` raises
`OSError(ENOSPC)`) and `TGMS_SHORT_WRITE_AT=<n>` (the n-th `os.write` call
returns a partial byte count once, instead of raising).

**Design, not results — no numbers here** (local runs so far: a 10-trial and
a 3-trial smoke, seeds 1 and 4242, plus the unit tests in
`tests/test_eval_diskfull.py` — a check that the mechanism works, not a
scored sweep).

**What this can and cannot see — the honest limitation, not a footnote.**
Grepping `tgms/` for direct `os.write`/`os.fsync`/`os.rename`/`os.replace`
calls finds exactly three real sites, and they are *not* the three the
task's literal wording names:

| call | site | reachable by this harness's workload? |
|---|---|---|
| `os.fsync` | `EventLog.append` (`tgms/storage/eventlog.py`) — the WAL fsync | **yes** — the one site every trial actually exercises |
| `os.fsync` | `Registry._append_locked` (`tgms/artifact/registry.py`) — artifact append | no — this harness's workload never registers an artifact |
| `os.replace` | `save_permutation` (`tgms/storage/tcsr.py`) — the persisted TCSR swap | no — this harness's workload never calls `tcsr()` |

`os.write` is never called directly anywhere in `tgms/` — every write goes
through a buffered Python file object's own `.write()`
(`open(path, "ab")`), which calls the C library's `write()` internally, not
the `os.write` Python function — so `TGMS_SHORT_WRITE_AT` is **dead code
against the current product** no matter what `at` is chosen; confirmed
empirically (`mode=short_write` never fires in either smoke run) and pinned
by `tests/test_eval_diskfull.py`'s direct calls to `os.write` after
installing the injector, which prove the *injector* fires correctly even
though the product never reaches it. Likewise `os.rename` is never called
directly (`tcsr.py` calls `os.replace`) — patched anyway, for a future call
site or a different OS path, but currently as dead as `os.write`.

**Every Rust-side write is invisible to this injector, full stop** —
`NativeStore` (pyo3) never touches Python's `os` module. The follow-up
engine-side injection points, for the Rust lane, not implemented here:
`write_atomic` (`crates/tgms-engine-core/src/store.rs` — the manifest
temp-file-then-rename that is the actual publication point, and the
`CURRENT` flip immediately after), segment writes
(`segment.rs::Segment` finalize), close-run writes (`visibility.rs`), the
dictionary append (`dict.rs::Dictionary`), and manifest-chain
checkpoint/delta serialization (`manifest_chain.rs`).

- **Per trial:** a seeded workload (same generator) runs in the
  injector-armed child at a random injection point (`TGMS_DISKFULL_AT` or
  `TGMS_SHORT_WRITE_AT`, mode and point both seed-derived); the parent
  requires the child terminate within a timeout (never a hang — the
  disallowed outcome) with either exit 0 (the injection point was never
  reached — a legitimate, uninteresting trial) or a clean typed exception;
  then, *without* the injector, it reopens and checks Q1 (every acked write
  present — acks are recorded only after the call returns, so this holds
  regardless of where injection landed), Q2 (`clean_replay_digest` of the
  recovered store's own log matches, reusing `eval_durability.py`'s helper
  directly), Q3 (`verify()` clean), Q4 (clean after `compact()`+`gc()`).
- **A confirmed mechanism, not a surprise:** because the injected `fsync`
  failure happens *after* the record's bytes are already `write()`+
  `flush()`ed to the real filesystem (only the fake `fsync` call itself
  raises — there is no actual full disk here), the "failed" write's bytes
  are already durable in the ordinary sense. A subsequent writer-mode
  reopen's normal recovery finds this as an unapplied suffix and replays
  it — the batch may end up present even though the caller's call raised
  and it was correctly never written to the trial's ack ledger. This is the
  same write-ahead direction EXP-A1's Q1 already documents ("returned-
  success implies present," not the converse) and is not scored as a
  violation; `tests/test_eval_diskfull.py::
  test_diskfull_at_first_fsync_is_a_clean_error_and_recovers` pins the
  shape at the earliest possible injection point (the very first write).

Slurm campaign: `scripts/diskfull_campaign.slurm` (`bigTiger`, 20 array
tasks × 100 trials = 2,000 trials, `%6`) + `scripts/
corruption_campaign_merge.py --kind diskfull`, producing
`benchmarks/diskfull-v1/`, same two infrastructure fixes reused from
`scripts/crash_campaign.slurm`.
