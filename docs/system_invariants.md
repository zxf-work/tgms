# System invariants

This is the one consolidated, public statement of what TGMS guarantees. The
guarantees already exist, scattered across `docs/STABILITY.md`, the
evaluation write-ups (`docs/eval_*.md`), source docstrings, and decision
records — this document does not add a new promise anywhere; every
invariant below is condensed or quoted from an existing public source,
cited at the point it is stated.

Each invariant carries a precise statement, where it is enforced in code,
how it is tested, and the decision (`D-NNN`) that ratified it. "Enforced
at" points to the mechanism, not to every caller of it; "Tested by" names
the file(s) that would fail if the invariant broke, not every test that
touches the area.

---

## 1. Durability and the commit protocol

**1.1 Single atomic publication unit.** A generation — one manifest and
everything it names — is the unit of atomicity. A commit publishes
segment, close-run, dictionary, and manifest files, each fsynced before
the next begins, and finishes by atomically renaming a new `CURRENT`
pointer into place. A crash before that rename leaves the previous
generation valid and readable, with the partial work orphaned; a crash
after it leaves the new generation durable. There is never a torn or
partially visible generation.
*Enforced at* `crates/tgms-engine-core/src/store.rs:1-23` (commit-order doc
comment and the implementation it introduces). *Tested by*
`docs/eval_durability.md`'s ten-boundary crash-injection harness
(`scripts/eval_durability.py`) plus the constructed-state tests it
references (Rust crash-step matrix; Python `CURRENT`-flip and
interrupted-compaction tests). *Ruling:* D-028, scored by D-086.

**1.2 Write-ahead, one-directional acknowledgement.** Every batch is
appended to the event log and fsynced before it is applied to the store.
The guarantee is **one-directional**: "a write call that returned success
is present after recovery" — not the converse. A batch fsynced before a
crash replays and becomes visible on the next open even if the caller
never saw the success return; an unacknowledged write staying absent is
never promised.
*Enforced at* `crates/tgms-engine-core/src/store.rs:1-23` and the Python
`Store` write path that fsyncs the log before calling the engine. *Tested
by* `docs/eval_durability.md` Q1 ("acknowledged-write survival"). *Ruling:*
D-028; the one-directional phrasing is `docs/STABILITY.md` §2's own words,
verified under D-086.

**1.3 Deterministic suffix recovery.** Each published generation's
manifest records a durable cursor `(offset, chain)`: `offset` is the byte
position past the last applied log record, `chain` a rolling hash over
applied record bytes, computed identically in Rust and Python. On open,
TGMS verifies the chain over the applied prefix and replays only the
un-applied suffix — never a full rebuild — and the result must be
byte-identical to a clean replay of the same log prefix into a fresh
store.
*Enforced at* the manifest's `EventLogRef {offset, chain}` and the
open-time recovery path (native store + `eventlog.py`'s `extend_chain`).
*Tested by* `tests/test_suffix_replay.py` (recovery ≡ replay digest,
idempotence, refusal on a damaged log, legacy no-cursor upgrade, `tgms
store verify` exit codes). *Ruling:* D-042.

**1.4 Refuse-loudly on unaccountable state.** A cursor past the log's end,
off a record boundary, or with a chain mismatch is treated as corruption
and TGMS **refuses to open** — it never guesses or silently serves a store
it cannot account for. The sole exception is a legacy store with no
cursor, which recovers nothing and upgrades to a real cursor on its next
write; that is a documented degrade, not a violation.
*Enforced at* the same recovery path as 1.3. *Tested by*
`tests/test_suffix_replay.py` (damaged logs refuse loudly) and
`tests/test_native_faults.py` (`test_tampered_manifest_refuses_to_open`,
`test_malformed_current_refuses_to_open`,
`test_current_pointing_past_the_manifests_refuses_to_open`). *Ruling:*
D-042.

**1.5 A torn final log record is recoverable, not fatal.** A crash
mid-append can leave a torn final record (unparseable bytes, a missing
terminal newline, or a syntactically valid record whose `batch_id` no
longer hashes to its own content). Such a record was never fsynced as a
complete unit and so was never acknowledged; trimming it breaks no
promise. Recovery detects and trims exactly these three signatures at the
end of the log, strictly past the applied cursor, and treats any other
damage as loudly fatal.
*Enforced at* `EventLog.trim_torn_tail` (`tgms/storage/eventlog.py`).
*Tested by* `tests/test_torn_wal.py`. *Ruling:* D-086 — the first real
defect the durability-injection harness found.

---

## 2. Single-generation visibility and reader isolation

**2.1 A reader pins one generation for its whole life.** A reader handle
answers every query from the generation it pinned at open. Segments are
immutable and a manifest names a coherent snapshot; the only mutation any
commit makes is appending an append-only close record, so nothing
published after a reader opens can leak into its view. `open` itself
serves exactly the previous or the next generation, never a blend.
*Enforced at* `crates/tgms-engine-core/src/gc.rs:13-16` (the
process-global pin table: "every open `NativeStore` registers its
generation ... so a reader that opened at generation N keeps N's manifest
and files on disk until it drops") and the immutability described in
`manifest.rs:1-12`, `segment.rs:1-6`, `visibility.rs:1-10`. *Tested by*
`tests/test_concurrency.py` — six tests phrased as "what would a violation
look like": a pinned handle answering identically through twelve published
generations; a reopening reader seeing only whole batches, in order; three
processes opening throughout a 40-batch write run, after which the store
verifies and replays to the same digest. *Ruling:* D-028 #1, scored by
`docs/eval_concurrency.md` §19 (see 2.2).

**2.2 Opening a store never mutates state a live writer depends on.** Two
failure modes were found and closed: (a) a plain open used to run crash
recovery unconditionally, indistinguishable from a real mid-batch crash,
letting a concurrent reader publish a generation the writer was also about
to publish; (b) opening used to truncate the dictionary tail to the
manifest's recorded byte count, indistinguishable from a writer's in-flight
tail, letting a reader delete bytes the writer had already fsynced. Both
are closed structurally: `tgms.open(..., read_only=True)` never runs
recovery and refuses every write entry point; dictionary open never
truncates.
*Enforced at* `tgms.open(read_only=True)` and `Dictionary::open`. *Tested
by* `tests/test_concurrency.py`; measured in `docs/eval_concurrency.md`
§19 — before the fix, three readers looping opens against a 40-batch
writer made the writer abort on batch 2 and killed 2 of 3 readers; after,
the writer completed all 40 batches and 8,141 reader opens saw 0 failures.
*Ruling:* D-043 item 3.

**2.3 Generation reclamation never removes a pinned or `CURRENT`
generation.** A generation is retained, and every file it names stays on
disk, exactly when it is the one `CURRENT` names, one of the last
`keep_last` generations, or pinned by a live in-process reader; a file may
be deleted only when no retained generation names it. GC never touches the
current generation and never reclaims a generation a still-open reader
pinned.
*Enforced at* `crates/tgms-engine-core/src/gc.rs:13-16`. *Tested by*
`tests/test_native_faults.py`
(`test_gc_never_touches_the_current_generation`,
`test_gc_spares_a_generation_pinned_by_an_open_reader`,
`test_gc_after_compaction_reclaims_superseded_segments`,
`test_interrupted_gc_leaves_the_store_openable`). *Ruling:* D-032.

---

## 3. Deterministic replay

**Statement.** Replaying a recorded event log — the whole log via `tgms
replay`, or an un-applied suffix during ordinary recovery — always
reproduces the same logical store as a fresh store built by ingesting that
log from empty. Recovery, replay, and a bulk ingest of the same log are
three routes to the identical answer, verified by comparing digests rather
than by argument.
*Enforced at* the replay cursor and chain of invariant 1.3, and the `tgms
replay` CLI path. *Tested by* `tests/test_suffix_replay.py` (recovery ≡
replay digest) and `tests/test_replay.py` (full-log replay determinism and
byte-identical reproduction). *Ruling:* D-042.

---

## 4. Format and checksum integrity

**Statement.** Every column extent and the string table in a segment file
carry a CRC32C, and the segment body carries a whole-body hash. Opening a
segment with checksum verification walks every extent; any mismatch — a
flipped data or header byte, a truncated file, a tampered manifest, a
malformed or out-of-range `CURRENT` pointer — is detected and refused
rather than silently served. `verify()` performs this walk across an
entire store.
*Enforced at* `crates/tgms-engine-core/src/segment.rs` (CRC32C per extent
and string table, checked in `Segment::open`; body-hash check),
`manifest.rs` (§1.1's atomic unit), `visibility.rs` (§2.1's append-only
close runs). *Tested by* `tests/test_native_faults.py`, one fault per
test: `test_healthy_store_verifies_clean`,
`test_truncated_segment_is_detected`,
`test_flipped_data_byte_fails_a_column_checksum`,
`test_flipped_header_byte_is_detected`,
`test_corrupt_segment_refuses_to_serve_rows`,
`test_missing_segment_is_detected`, `test_partial_close_run_is_detected`,
`test_tampered_manifest_refuses_to_open`,
`test_malformed_current_refuses_to_open`,
`test_current_pointing_past_the_manifests_refuses_to_open`,
`test_orphaned_segment_files_are_ignored`. *Ruling:* D-028 #1 and #7; no
single decision covers the whole native-fault suite.

---

## 5. Bitemporal interval invariants

**Statement.** Every stored version's `(transaction time, valid time)`
interval pair obeys fixed structural rules, independent of backend:
intervals are disjoint at every transaction time; a closed `tt` and its
properties are canonical once written; history is bitemporally immutable
(a correction or retraction closes an interval and appends a new one, it
never rewrites the old one); every invariant survives an arbitrary
multi-op bracketed batch; a version created and closed within the same
batch is never materialized; a retraction or correction retires only its
own batch's version; a version closed by a *later* batch remains visible
as history; remainder-carving on a partial correction/retraction preserves
the untouched remainder exactly; retraction is evolution of the record,
never erasure.
*Enforced at* the versioning/closure logic applied at ingest
(`tgms/storage/`, the native close-run mechanism of §2.1). *Tested by*
`tests/test_storage_invariants.py`:
`test_disjointness_at_every_transaction_time`,
`test_closed_tt_and_props_canonical`, `test_bitemporal_immutability`,
`test_every_invariant_survives_a_multi_op_bracketed_batch`,
`test_a_version_created_and_closed_in_one_batch_is_not_stored`,
`test_retract_and_correct_also_retire_their_own_batch_s_version`,
`test_a_version_closed_by_a_LATER_batch_is_still_history`,
`test_remainder_carving`, `test_retract_is_evolution_not_erasure`,
`test_correct_preserves_remainder_and_replaces_props`,
`test_correct_refuses_interval_spanning_disagreeing_labels`,
`test_correct_across_two_hits_with_unanimous_label_preserves_it`.
*Ruling:* D-028 (bitemporal model), pinned as the storage-layer acceptance
baseline.

---

## 6. Freshness soundness (C1–C3)

TGMS can answer "could anything written since have changed this saved
result?" without recomputing it (`tgms trace check`; the artifact
registry's `artifact check`). The contract is deliberately **one-sided**:

- **C1 — soundness in one direction only.** `FRESH ⇒ no relevant
  correction could change the result`. The converse is never claimed: a
  `POSSIBLY_STALE`/`UNDECIDABLE` verdict does not mean the answer *did*
  change — the mechanism over-approximates on purpose.
- **C2 — false invalidation is allowed.** A conservative false alarm is an
  accepted cost of the mechanism, not a bug, and its rate is explicitly
  not covered by any stability promise.
- **C3 — false freshness is never allowed.** No unrecognized input,
  widened scope, or unrecognized enum value may produce `FRESH` when a
  relevant write occurred. Every unrecognized shape lifts to
  `UNDECIDABLE`, and every consumer treats `UNDECIDABLE` exactly as
  `POSSIBLY_STALE` — surfaced separately only for diagnosis, never offered
  as a weaker third contract.

Formally, as the checker's own docstring states it: `V(R, τ_now) = FRESH
⇒ FRESH*(R, τ_now)` — the *only* promise is the forward implication.

*Enforced at* `tgms/tgir/check.py` (module docstring: "This module reads a
log and a scope. It never reads a store"; "`UNDECIDABLE` is not a third
contract — every consumer treats it as `POSSIBLY_STALE`") and the
mechanical import-boundary gate `scripts/check_freshness_boundary.py`,
which restricts `check.py`/`footprint.py` to importing only
`tgms.core.model`, `tgms.core.errors`, `tgms.storage.eventlog`, and
`tgms.tgir.depscope` — never `Store` or any adapter — so the soundness
argument cannot be undermined by a checker that reads live state instead
of the log it was asked about. *Tested by* `tests/test_freshness_soundness.py`
— twenty-one required counterexample scenarios, each asserting the result
really did change (by independent recompute-and-compare, never by
assertion about the store) and that the verdict is `POSSIBLY_STALE`
through the specific arm the contract names — plus contract-level
invariants: the empty scope is always `FRESH`, widening a scope never
turns `POSSIBLY_STALE` back into `FRESH`, `UNDECIDABLE` is never
downgraded to `FRESH`, and a pinned result still has its suffix scanned.
*Public documentation:* `docs/STABILITY.md` §4 states this directly ("It
never says an answer is fresh when it might not be") and lists precision
(the false-invalidation rate) as explicitly not stable.

---

## 7. The artifact registry contract

A registered result (`tgms artifact register/list/check/refresh`) can be
superseded by a newer generation, but the registry never rewrites history
to do it.

- **Backwards-only supersession.** `refresh` either publishes generation
  `g+1` or refuses and changes nothing; generations are numbered
  consecutively from 0, never edited in place.
- **Byte-identical old generations.** Publishing a new generation never
  touches a prior one's bytes. The registry file is append-only and
  chain-verified the same way the event log is (a rolling hash over raw
  record bytes, `tgms.storage.eventlog.extend_chain` reused verbatim).
- **A closed refusal taxonomy.** A `refresh` that cannot proceed exits
  non-zero with exactly one reason from a fixed set: `not-found`,
  `generation-mismatch`, `no-refresh-handle`, `handle-mismatch`,
  `unknown-refresh-kind`, `ref-not-found`, `unknown-plan-format`,
  `parent-vanished`, `execution-refused`. There is no silent partial
  refresh and no unlabeled failure.

*Enforced at* `tgms/artifact/registry.py` (chain verification on open,
per-record digest self-check, consecutive `generation`/`supersedes`
linkage), `refresh.py`, `lookup.py`, `propagate.py`. *Tested by* the
artifact-registry regression suite behind `CHANGELOG.md`'s v0.8.0 entry
(round-2 propagation measured at 5,867 decisions, 0 false-safe). *Ruling:*
the public record is `CHANGELOG.md`'s v0.8.0 entry ("The artifact
registry: `tgms artifact register/list/check/refresh`") and
`docs/STABILITY.md` §6.

---

## 8. Evidence and claim verification

Agent-produced answers are checked against recorded execution evidence
before being reported, at three points:

- **Identity grounding.** A literal entity id in a plan step's arguments
  must already be present in the task's declared input set, or it is
  rejected as `E_GROUNDING` — new identities may enter a plan only through
  `resolve_entities` and a `$ref`, never as a typed-in literal.
  (`tgms/agent/verifier.py:169-175`.)
- **Evidence support.** A numeric or entity claim is checked against the
  recorded payload of the step(s) it cites; it is `supported` only when a
  candidate value in that payload matches within tolerance, `unsupported`
  otherwise, `unverifiable` when the claim or reference is malformed — a
  malformed claim is downgraded, never allowed to crash verification.
  (`tgms/agent/verifier.py:354-380`, `_check_count`.)
- **Truncation taint.** A claim that would otherwise be `supported` is
  downgraded to `weakly_supported` when the evidence it cites is truncated
  (`tgms/agent/verifier.py:503-506`). Taint also propagates forward: a
  `compute` step over a reducing function whose upstream input was
  truncated is refused outright — reducing a truncated result to one
  number is a wrong answer, not a partial one (`tgms/agent/executor.py:195-210`).

*Tested by* the verifier and executor test suites keyed to these three
checks, cross-referenced in `docs/TECHNICAL_REPORT.md`'s verifier
sections. *Ruling:* the evidence model referenced by
`tgms/tgir/admission.py`'s own docstring ("EVIDENCE_MODEL §2").

---

## 9. Admission and refusal

**Statement.** A plan that would exceed a declared cost ceiling is refused
before running the expensive part, and every refusal carries a
machine-checkable certificate rather than a bare error. Admission runs in
three stages, each a strictly narrower re-check than the last — a later
stage can only refuse something an earlier stage already admitted, never
admit something an earlier stage refused:

1. **Plan-level (static).** Walk the DAG bottom-up, sum each cost axis
   over every node, enforce once against the plan before execution begins.
2. **Node-level (realized).** Immediately before a node with no reliable
   static estimate runs (`Join`, `PatternMatch`), re-evaluate it against
   its inputs' realized cardinality — a second opportunity to refuse only.
3. **Runtime.** A budget inside the expansion fixpoint can refuse mid-run,
   recorded as `completeness = refused`, never as `timeout-truncated` —
   conflating the two would let an unbounded expansion masquerade as a
   complete-but-partial answer, the false-absence failure mode this system
   exists to prevent.

A `RefusalCertificate` — `⟨plan_digest, policy_version, estimates, ceiling
hit, calibration_ref⟩` — proves only that the refusal was consistent with
the declared policy; its own docstring states it never proves the plan
actually would have exceeded budget.
*Enforced at* `tgms/tgir/admission.py:79-98` (`RefusalCertificate`).
*Tested by* the admission test suite's plan-level and runtime-refusal
cases keyed to this shape. *Ruling:* D-155 — runtime budget aborts are
refusals, never truncations, and §2.13 has three refusal points, not one.

---

## Trust-boundary integrity (evidence-relative)

Sections 6, 8, and 9 exist to hold four boundary invariants between what
an agent reports and what the system actually executed. All four are
**evidence-relative**: they bound the gap between a claim and the recorded
execution trace, not the gap between a claim and ground truth.

1. **No emitted claim is unsupported by recorded execution evidence.**
   Every claim is checked against the payload of the step(s) it cites; an
   unsupported claim is labeled `unsupported`, never passed through
   silently (§8, evidence support).
2. **No reported identity is minted by the model.** An entity id can enter
   a plan only via the task's declared input set or a `$ref` from
   `resolve_entities`; a literal id typed by the planning step itself is
   rejected (§8, identity grounding).
3. **No derived number is computed outside a trusted operator.** A
   reducing computation over truncated evidence, or over a step whose own
   upstream was truncated, is refused rather than silently producing a
   number from partial data (§8, truncation taint).
4. **No `FRESH` verdict is ever contradicted by recomputation.** The
   freshness checker's only promise is §6's forward implication: a
   `FRESH` verdict is guaranteed not to disagree with independent
   recomputation. It may say `POSSIBLY_STALE` when nothing changed; it may
   never say `FRESH` when something did.

**Certified is not correct.** These four invariants say a claim is
consistent with its own recorded execution — grounded, evidenced, computed
by a trusted operator, never contradicted by a fresher recomputation.
None of them say the claim is *right* against an external gold answer.
Gold-relative correctness (accuracy against a known-correct answer set,
error rates against a reference implementation) is a separate, reported
quantity, evaluated and published independently in the evaluation
documents (`docs/eval_*.md`) — it is never implied by a passing verifier
or a `FRESH` verdict.

---

## Reading this document

Every entry above traces to a file, a test, and a decision number that
exists in this repository today. If any of those three disagree with the
current source, this document is stale and should be corrected against
the source. This document is not itself normative — `docs/STABILITY.md` is
the stability contract, and the internal decision log is the record of
ratification — it only collects and cross-references what those already
say, in one place, so the invariant surface can be read without
reconstructing it from a source search.
