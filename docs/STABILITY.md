# Stability contract

This is not a production-grade compatibility promise. TGMS is a **research
system at v0.x**: a single-writer/many-reader embedded graph store (a Rust
core via PyO3, driven from Python — CLI, library, or MCP tool server on top
of one local store directory). It is under active development, and its
on-disk binary format has already changed once (v0.5.0's native engine) and
is expected to change again.

What this document is: a plain statement of what you can currently rely on,
and what you cannot, so a technically competent reader can make an informed
decision before depending on TGMS for anything durable. What it is not: a
semver guarantee, an SLA, or a claim that any of this has been battle-tested
outside our own test and evaluation harnesses.

**Explicitly not covered** — none of these exist, so there is nothing to
promise about them: a distributed mode, multi-writer coordination, live
replication, high availability / failover, or a network protocol other than
the MCP tool surface talking to one local store. TGMS is single-machine,
single-writer, embedded (`docs/ROADMAP.md`: "Single machine only; no
distributed work").

---

## 1. On-disk compatibility

**What is stable:** the event log (`<store_path>/eventlog.jsonl`), an
append-only JSONL write-ahead log written before every batch is applied to
any backend. It carries its own format identity, independent of the
store's binary format — `{"format": "tgms-eventlog", "version": 1}`
(`tgms/storage/eventlog.py`) — and its stated purposes are "provenance,
crash recovery (replay), **backend migration**." It is also the artifact
that moves across backends today: `tgms ingest`/`tgms replay` both take a
`--backend` choice of `native`, `duckdb`, or `kuzu`, all driven from the
same log.

**What is disposable:** the derived store — the native engine's segments,
manifest, dictionary, and close-run files under `<store_path>/native/` (or
the DuckDB/Kuzu database files for those backends). This is a rebuildable
cache of the event log, not a source of truth. Its binary layout is
versioned internally (format v0 today; a v2 compression format is reserved
but gated) and has no cross-version compatibility guarantee — v0.5.0
already replaced the entire storage layer (DuckDB-backed → native Rust
engine) while the event-log format underneath was untouched.

**The rule (v1 policy):** *event logs are stable; stores may be rebuilt
across incompatible versions.* Concretely:

- Keep the event log. Treat the derived store directory as something you
  could delete and regenerate.
- `tgms replay <eventlog.jsonl> --store <path>` rebuilds a store from a
  recorded log. It is documented as producing a result "byte-identical" to
  the original — unlike a fresh `tgms ingest`, replay preserves the
  original transaction times (`tgms/cli.py`, the `replay` subcommand help
  text). `ENGINE_IMPLEMENTATION_SPEC.md`'s acceptance tests run exactly
  this: replay a frozen event log into a fresh store and check the digest.
- When moving a store across a TGMS version whose on-disk format changed,
  the supported path is: replay the event log into a freshly created store
  under the new version. There is no in-place binary migration tool, and
  none is promised.

**Open question — not verified, left as a gap rather than a claim:** there
is no version-negotiation or migration tooling for the store's own binary
format (e.g., no `tgms store migrate v0→v2`). Codec IDs and a v2 layout are
reserved in the format header, but nothing currently reads a v0 store
directly with v2 code. Until such tooling exists, "incompatible version"
should be read as "requires a replay," not "requires a converter."

**Manifest format 2 (added 2026-09-13; the one converter that does exist).**
The manifest document now has its own version, separate from the physical
file-header version the segments and close runs carry. Segment and close-run
bytes did not change and their `FORMAT_VERSION` is still 1. Format 1 wrote
the entire manifest — every live segment — once per generation, which made
retained manifest bytes grow as Θ(generations²): 25,451 MB of manifests
against 163 MB of segments on the SNB SF1 build that motivated the change.
Format 2 writes a *delta* against the parent generation and a full
**checkpoint** every 512 generations (`TGMS_MANIFEST_CHECKPOINT_EVERY`), plus
one whenever compaction runs or gc sets a retention floor.
`manifests/<G>.json`, one file per generation, `CURRENT` flipped last, and
JSON you can read with `cat` are all unchanged; so is `manifest_sha`, which
remains the digest of the *reconstructed* logical manifest, so a checkpoint
and a delta describing the same content hash identically and the TCSR stamp
keeps its meaning.

**Manifest format 3 (added 2026-09-15; `MANIFEST_FORMAT_VERSION` is 3).**
Format 2 left the *document* small and the *digest* O(live segments):
`manifest_sha` was the sha of the whole serialized manifest, so a commit
serialized and hashed every live segment three times over, and opening a
store re-serialized and re-hashed the entire reconstructed manifest on every
replay step — 840 MB hashed to read a 5 MB head manifest at ten thousand
generations. Format 3 changes that one thing. `manifest_sha` becomes a
**Merkle root over the ordered segment set**: position-tagged leaves, an
explicit tag byte separating leaves from internal nodes from empty lanes, one
tree per lane, and the four lane roots combined in lane order. Appending
touches only the right spine, so a commit's digest is O(appended + log n) and
a replay step is O(1).

Nothing else moves. The document layout, the file-per-generation layout,
`CURRENT`, the checkpoint/delta split, and the 64-bit truncation are all as
they were; format-3 records carry one added `sha_kind: "merkle-v1"` field so
the rule is legible on disk. The truncation's scope is unchanged too: enough
to detect corruption and mismatched pairings, not a security boundary.

Because the digest is now maintained incrementally, `tgms store verify
--full` re-derives it from scratch and compares against what `CURRENT`
publishes, reporting a `digest-oracle-mismatch` if the two disagree. `verify
--fast` does not: that check is the price of an incremental digest and it
belongs in the mode nobody runs per commit.

Both format bumps are the exception to "no in-place binary migration tool",
and the exception is narrow and identical in shape for each:

- A store written by an older engine — manifest format 1 (TGMS ≤ v0.8.0) or
  format 2 (TGMS v0.8.0, 2026-09-13 to 2026-09-15) — **opens, reads, and
  passes `tgms store verify` in both modes** under the new engine, under
  *its own* digest rule, but refuses every write — commit, compact, gc —
  with an error naming the remedy. The rule is chosen by the manifest's own
  `format` field, and `format` is inside every digest's preimage, so an older
  store is never silently reinterpreted under a newer rule.
- `tgms store upgrade-manifests --store <path>` converts either: it
  republishes the current generation's content as one format-3 checkpoint and
  flips `CURRENT`. Nothing else is touched — no segment, close-run,
  dictionary, or event-log byte — and running it twice is a no-op.
- Two things do move. The generation counter advances by one, and
  `manifest_sha` changes — for format 1 → 3 and for format 2 → 3 alike,
  because the format field is inside the digest's preimage and, from format
  3, the digest is a different function entirely. Any persisted TCSR index
  therefore fails its stamp check and rebuilds silently, which is what that
  check is for. Build receipts must record `manifest_format` alongside
  `manifest_sha` and **must not be compared on `manifest_sha` across
  formats**; their discriminating half, `store_identity`, is
  event-log-derived and format-independent, so existing receipts stay
  citable. The frozen result digests
  (`scripts/frozen_digests_v1.json`, both backends) are unaffected: they are
  derived from query results, not from the manifest.
- There is no downgrade. Going back to an older engine means replaying the
  event log, which is the standing mitigation for every other format change
  here.

**Forward compatibility is not symmetric, and this is the honest statement
of it.** The log format is stable in the *backward* direction — a newer TGMS
reads every older log — but op records may gain optional fields, and the
appliers read the keys they know and ignore the rest. So an **older** TGMS
replaying a **newer** log does not fail; it silently applies the part it
understands. Concretely, `ingest_events` gained an optional `nodes` array in
v0.7.0, and a pre-v0.7.0 build replaying such a log rebuilds a store missing
those node versions, without an error. Replaying a log with the version that
wrote it (or newer) is therefore not merely advisable — it is the only
direction with a guarantee.

---

## 2. Crash behavior

**What is atomic:** one manifest generation. The commit protocol is
append batch(es) to the event log → fsync the log → write segment/close-run/
index files → fsync those files → write `MANIFEST.G.tmp` → fsync → **atomic
rename to `CURRENT`** → fsync the directory (`ENGINE_BLUEPRINT.md`,
"Group-commit durability"). A crash at any point before the rename
completes leaves the previous manifest — the previous generation — valid
and readable; any partially-written files from the interrupted attempt are
orphaned, not referenced, and ignored. A crash after the rename leaves the
new generation durable. There is one durability mode (group commit): a
logical commit can batch one or more op batches behind a single event-log
fsync, so the atomicity unit for a bulk load is the whole batched commit,
not each individual write call.

**Write-ahead semantics:** every batch is appended to the event log
(fsync'd) *before* it is applied to the store. The guarantee this gives is
one-directional: "a write call that returned success is present after
recovery" — not the converse. A batch whose log record was fsynced before a
crash is replayed and becomes visible on the next open even if the caller
never saw a success return. This was confirmed by injected-crash testing,
not just argued (see below).

**Recovery mechanism — durable event-log cursor with suffix replay
(D-042):** each published generation's manifest records a cursor
`(offset, chain)`: `offset` is the byte position immediately past the last
applied log record, and `chain` is a rolling SHA-256 hash over applied
record bytes, computed identically in Rust and Python. On open, TGMS
verifies the chain over the already-applied prefix and replays only the
*un-applied suffix* of the event log — not a full rebuild. Recovery is
proven deterministic: the resulting store's digest equals a clean replay
of the same log prefix into a fresh store (`tests/test_suffix_replay.py`).
A log tail that is itself a failed batch retries deterministically at each
open (publishing nothing) until a later successful write moves past it.

**Refuse-loudly path:** if the recorded cursor points past the log's end,
lands off a record boundary, or its chain doesn't match the applied
prefix, TGMS treats this as corruption and **refuses to open**, naming
`tgms replay` as the remedy — it does not guess or silently serve a store
it cannot account for. A legacy store with no cursor (`chain == ""`)
recovers nothing (it has no way to know what was applied) but is not
treated as broken; it upgrades to a real cursor on its next write.

**A populated store with `CURRENT` itself missing also refuses (fixed
2026-09-15, Lane A task A8):** `open` no longer treats a missing `CURRENT`
as an empty store — it does so only when the rest of the directory is
genuinely empty too, or holds exactly the harmless bootstrap-crash orphan
(the generation-0 checkpoint alone, written but never published); any other
manifest, segment, or dictionary content found without `CURRENT` is refused
as corruption, naming what was found and how to recover (remove the
orphans, or restore `CURRENT`).

**Tested, not just designed:** `docs/eval_durability.md` (D-086) records a
real-process, SIGKILL-based crash-injection harness at 10 commit-protocol
boundaries (mid-log-append, post-fsync-pre-apply, mid-segment-seal,
mid-manifest-write, post-rename-pre-return, mid-compaction/gc, etc.), 3
trials each, checked against four questions: acknowledged-write survival,
deterministic recovery (digest match), single-generation visibility (never
a blend of two generations), and orphan reclamation after `gc`. Result: one
real defect found on the first run (a torn final log record on a mid-append
crash made the store refuse to open instead of truncating and recovering —
"a routine power cut... was an outage requiring manual log surgery"), fixed
(`EventLog.trim_torn_tail`), and all 10 boundaries then passed all four
questions across 30 trials.

**`tgms store verify`:** a CLI wrapper around the same integrity walk the
engine already does — human-readable report (generation, segments/rows
checked, close-run count, dictionary records checked), a `PROBLEMS` list if
anything failed a checksum or cross-reference, and a nonzero exit code on
any problem (`tgms/cli.py`, the `store verify` action). On failure it
prints the same remedy: rebuild from the event log with `tgms replay`. Since
2026-09-13 it has two modes and a machine-readable form — see §9.

**What a user should do after a crash:**
1. Just reopen the store normally (`tgms.open(...)`). Recovery (chain
   verification + suffix replay, if needed) runs automatically as part of
   open for a writer. If you are opening as a reader alongside a live
   writer, pass `read_only=True` — this is required, not optional, for
   correctness under concurrent access (D-049), and skips recovery.
2. If open refuses with a corruption error, run `tgms store verify` to get
   a specific report of what failed.
3. If the store is unrecoverable (or you just want a clean rebuild), run
   `tgms replay <eventlog.jsonl> --store <fresh_path>` — this is the
   guaranteed-safe path back to a healthy store, because the event log is
   the durable source of truth (§1).

- **2026-09-14 — read-only opens treat a torn final record as uncommitted
  (invariant 1.5 extended to readers).** A reader never runs recovery
  (`Store.__init__(read_only=True)` skips `_recover`, D-049), so
  `trim_torn_tail` never trims for it — but a read-only open still scans
  the whole log to seed its clock and frontier, and used to raise
  `StateError` if that scan landed on a torn final record a live writer's
  `append()` was still fsyncing (CI run 34852755086, first observed on
  `tests/test_concurrency.py`'s readers-throughout-a-write-run test).
  **Refined the same day:** the first fix tolerated *any* torn final
  record and regressed `tests/test_eval_corruption.py::
  test_torn_event_log_tail_is_detected_even_read_only` — a corruption
  sweep's `append_garbage` mutation onto a *closed* store produces bytes
  indistinguishable, by framing alone, from a live writer's not-yet-
  finished record (both are unparseable, missing a trailing newline, and
  run to the file's end, starting exactly at the manifest's applied
  offset). Tolerance now requires two conditions together
  (`Store._compute_reader_torn_tail_floor`): the torn record's start
  offset must be at or past the manifest's own applied event-log offset
  (`EventLog.batches_from`'s `tolerate_torn_tail_from`, the same value
  `trim_torn_tail(applied_offset)` uses for a writer — a torn record
  *before* it is damage to already-applied history and stays corruption
  regardless of where the file now ends), **and** some process must
  currently hold `writer.lock` (`Store._writer_lock_is_held`, a
  non-blocking `flock` probe, released immediately — a reader never holds
  this lock itself, so finding it free proves nothing could still be
  appending). Both hold in the real race; neither holds for the
  corruption sweep's closed-store injection, which stays DETECTED. Only
  the read-only open/replay path ever passes a non-`None`
  `tolerate_torn_tail_from`; a writer, which trims a genuinely torn tail
  during `_recover` before ever reaching a live `batches_from` call, keeps
  the strict (`None`) reading. `Registry`'s read path (`read_only=True`)
  has no analogous applied-offset concept — every record it writes is
  folded synchronously under its own lock, so there is no "log ahead of
  backend" gap — so it keeps unconditional tolerance for a torn final
  line; `Registry.verify()` still reports one as a finding regardless,
  which is what keeps the corruption classifier's `verify_problems` path
  reaching DETECTED for it. **Hazard closed the same day:** the
  `writer.lock` probe now runs only on actually encountering a torn tail
  (never on every read-only open), and a writer's own lock acquisition
  retries briefly, so a reader's momentary probe can no longer make a
  concurrent writer open spuriously refuse with `WriterLockedError`. See
  `docs/system_invariants.md` §1.5 and `tests/test_reader_torn_tail.py`.

---

## 3. Operator semantics

TGMS ships 15 verified temporal operators. Their semantics are currently
pinned by two automated regression suites, not by a written specification
with a version number:

- **`tests/test_operators_oracle.py`** — every operator's output is checked
  for exact (canonical-JSON) equality against a brute-force reference
  oracle, across randomized bi-temporal stores and randomized arguments
  (Hypothesis-generated; the milestone-acceptance sweep runs 500 examples
  per operator family).
- **`tests/test_metamorphic.py`** — property tests independent of any
  oracle: diff composition across three timestamps must be internally
  consistent, and — the signature correctness property of the whole
  bi-temporal model — any operator evaluated at a fixed `as_of_tt` returns
  byte-identical results before and after later corrections or
  retractions.

These two suites are, today, the concrete definition of "current operator
semantics": if a code change makes either suite fail, that change altered
documented behavior.

**Policy going forward:** a change to a documented operator that would
change its oracle or metamorphic-suite expected output is a **breaking
semantic change**, and requires:
- a `CHANGELOG.md` entry in the release that ships it, describing the
  behavior change explicitly (this is already how storage- and
  performance-relevant changes are recorded release over release — see the
  v0.6.0 and v0.5.0 entries); and
- where feasible, a deprecation note in the release *before* the change
  ships, rather than a silent flip.

Non-breaking additions (new operators, new optional arguments with
back-compatible defaults) are not subject to this — only changes to
existing, documented behavior.

**Open question — not verified, stated honestly rather than claimed:**
this policy is adopted starting with this document, not a retroactive
audit. Prior releases were not made under a public compatibility promise,
so no claim is made that every past semantic change was accompanied by a
deprecation note. There is also no automated tooling today that enforces
"deprecate before break" (e.g., no CI check that a semantics-affecting diff
carries a changelog entry) — it is a process commitment, not a machine-
checked one, until further work makes it one.

---

## 4. The freshness check (added M4, 2026-08-22)

**`tgms trace check <record.json> --store <path>`** answers *"could anything
written since have changed this?"* about a saved answer, without recomputing
it. It is a **non-breaking addition** under §3's rule: a second action on
the existing `tgms trace` parser, so every `tgms trace render … -o …`
invocation keeps working verbatim. The only change to `render` is that
`-o/--out` is now enforced in the dispatch rather than by argparse, which
turns a missing `-o` from an argparse usage error into a named one.

**What you can rely on:**

- **It never says an answer is fresh when it might not be.** The verdict is
  `FRESH` / `POSSIBLY_STALE` / `UNDECIDABLE`; a caller asks
  `.actionable_fresh`, and `UNDECIDABLE` is *not* a third answer — every
  consumer treats it as `POSSIBLY_STALE`. The CLI exits `0` for fresh and
  `1` for everything else, so a script that branches on the status code gets
  the conservative answer without having to know that.
- **It reads the event log, not the store.** The verb takes no database
  lock, needs no optional backend extra installed, and runs while a writer
  holds the store. `--store` may name a store directory or an
  `eventlog.jsonl` directly. Since §1 makes the event log the stable format,
  a record stays checkable across a backend change.
- **It writes nothing.** A verdict is computed on demand and persisted
  nowhere — no new field on any result, no cache, no index. Nothing about a
  stored answer's bytes changes because you asked about it.
- **It accepts either record shape** — the `tgms ask --save-record`
  envelope, or a bare trace record.

**What is not stable, and is not claimed:**

- **Precision.** The mechanism over-approximates deliberately: a
  `POSSIBLY_STALE` verdict does not mean the answer *did* change, and the
  rate of such false invalidations will move as the per-operator dependency
  derivations improve. Only the soundness direction is a promise.
- **The witness list's contents.** Which writes are reported, in what order,
  and how many (they are capped, with the true total carried alongside) are
  presentation choices and may change.
- **The prose.** The rendered sentence is for humans; parse `--json`
  instead, and even there the per-step map's shape is v0.x.
- **The library API** (`Store.check_scope` / `check_result` /
  `check_trace`) is newer than the CLI verb and correspondingly less
  settled.

The contract this implements is `docs/design/FRESHNESS_SEMANTICS.md`
(sections 1–14 frozen 2026-08-21, §15 an append-only errata register). The
wire format a result carries is versioned: a reader that does not recognize
a dependency record's version returns `UNDECIDABLE`, never `FRESH`.

**`DependencyScope.as_of_tt` (added M5, §15 errata entry E-10) is additive
and opt-in.** It carries the `as_of_tt` a read actually applied, so that a
pinned answer can claim the D-153 pinned exemption at `check`'s step 8a
instead of being widened like an ordinary unpinned scope. The field is
emitted only when a producer wants that exemption; **absent means no
exemption** — the fail-safe default — so every scope written before E-10,
and every scope a producer chooses not to set it on today, behaves exactly
as it did before this field existed: byte-identical verdicts. This is not a
wire-format version bump, deliberately: a reader that does not recognize the
key simply ignores it and computes today's (sound) verdict. Measured over
600 pinned-artifact trials (200 each on bitcoinotc, collegemsg and
sx-mathoverflow), the exemption avoided the invalidation its unexempted
counterpart flagged in every trial.

---

## 5. Compaction layout and disk retention (fixed 2026-08-24, shipped v0.8.0)

A `compact()` call reorganizes a store's on-disk segments; `gc()` (default
`keep_last=2`) is the separate step that reclaims space no longer
referenced. Two things about this pair are worth stating precisely.

**What is fixed:** a store built by many live ingest batches and then
compacted could scan far slower than a store rebuilt from scratch via
`tgms replay` off the identical event log — same rows, same answers, no
correctness issue, but scan latency that degraded badly with how many
small batches had gone into the store before compaction (measured over
100x slower on one such store, to the point a full scan did not finish in
a reasonable timeout). This was a read-path cost internal to how a
compacted segment tracks transaction-time metadata, not a property of the
data itself. It was fixed on `main` as of 2026-08-24 — v0.7.0 predates the
fix — and ships in v0.8.0. No store rebuild is required: an existing store
on disk gets the fix automatically the next time it is opened with a build
that has it.

**What you can check:** the `tgms store verify` report (§2) carries two
layout-quality counters: `tt_s_runs` (summed over live segments) and
`max_tt_s_runs` (the single worst segment). A value near 1 per segment
means an ingest-shaped layout; a value approaching a segment's row count
means a heavily compacted, interleaved one. With the fix in place, both
shapes scan at the same order of speed — these counters are diagnostic,
not a health check you need to act on.

**Disk retention is not immediate after `compact()` + `gc()`.** `gc()`'s
default `keep_last=2` keeps the current manifest generation and its
parent. Immediately after `compact()` publishes a new generation, the
parent — the pre-compaction one — is still one of the two retained
generations, and it still references every pre-compaction segment file,
so those files are not deleted yet. Expect disk usage to stay elevated
(potentially higher than before compaction, since the new compacted
segments now sit alongside the still-referenced old ones) until a later
commit+gc cycle advances the retained window past that generation.

---

## 6. The artifact registry (added M5, 2026-08-30)

**`tgms artifact register/list/check/refresh <store> ...`** is a **non-breaking
addition** under §3's rule, on the exact pattern §4 already set for
`tgms trace check`: a new CLI verb and a new on-disk file
(`<store_path>/artifacts.jsonl`), touching nothing about how the event log,
the derived store, or the fifteen operators behave. A registered artifact is
a named, generation-numbered result (`name@generation`) that carries the
same freshness machinery `tgms trace check` uses — a `DependencyScope` and a
`tt_q` — plus, optionally, a `refresh` handle that says how to recompute it
and a `parents` list naming other artifacts it was built from.

**What you can rely on:**

- **`artifact check` is the same three-verdict contract as `trace check`**
  (§4): `FRESH` / `POSSIBLY_STALE` / `UNDECIDABLE`, sound in the same one
  direction, exit `0`/`1` the same way. It opens the event log, not the
  store — same posture, same reason.
- **`artifact refresh` either publishes a byte-verified new generation or
  changes nothing.** A successful refresh (exit `0`) re-executes the
  artifact's own recorded plan or operator call and publishes generation
  `g+1`; generation `g` is left untouched on disk. A refused refresh (exit
  `2`) publishes nothing, and names its reason from a closed taxonomy
  (`not-found`, `generation-mismatch`, `no-refresh-handle`,
  `handle-mismatch`, `unknown-refresh-kind`, `ref-not-found`,
  `unknown-plan-format`, `parent-vanished`, `execution-refused`) — printed
  in prose, or in `details.reason` with `--json`. There is no partial or
  silently-degraded refresh outcome.
- **Selectivity is the caller's, not the mechanism's.** `artifact refresh`
  does not itself decide whether an artifact needs refreshing — it re-runs
  whatever you name, whether or not a preceding `artifact check` said
  `FRESH`. This is deliberate: a fixed policy about *when* to refresh would
  be a second, unstated contract on top of a mechanism whose only promise
  is *what happens once you ask*.
- **A `refresh.ref`/`plan.plan_ref` blob is content-addressed end to end,
  since 2026-09-14 (Lane A task A10, closing a corruption-campaign finding:
  `artifact_blob|append_garbage` was 0/106 detected — appended bytes a
  parser never looks at left every recorded digest unchanged).
  `Registry.register()` now stamps a `blob_sha256` of the file's raw bytes
  for any such blob already on disk at registration time, and both
  `Registry.verify()` (`artifact check`'s `verify(mode="full")` oracle) and
  `artifact refresh`'s own blob loader refuse — never silently parse only a
  blob's leading JSON value — when a blob carries anything after its one
  JSON document, or (when `blob_sha256` is on record) when its bytes no
  longer hash to it. A record written before this field existed reads back
  exactly as before: `blob_sha256` is additive and absent, not retrofitted,
  and no existing `record_digest` changes because of it.
  `payload.result_ref` deliberately does **not** get a `blob_sha256` — that
  blob's bytes carry wall-clock telemetry (`tgir.annotations.*.telemetry.
  wall_ms`, itself already digest-excluded per the bullet below) that
  legitimately differs run to run, so hashing the raw file would make
  `Registry.register()` itself non-deterministic.

**What is not stable, and is not claimed:**

- **One-level propagation (`parent_recheck`) has no CLI verb yet.** It
  exists only as a library call (`tgms.artifact.propagate.parent_recheck`):
  given a just-refreshed artifact, it finds every registered artifact
  naming it among their `parents` at a now-superseded generation, and flags
  them for recheck — even when their *own* dependency scope was never
  touched (measured over 5,867 round-2 propagation decisions across three
  stores: 0 false-safe, 99.0% resolved without recomputation, byte-identical
  on deterministic replay — `benchmarks/m5-v1/`). A CLI surface for it is
  future work, not yet a promise about its shape.
- **The `annotations` envelope field is digest-excluded and additive.**
  A `tgir_plan`-kind execution may carry a per-node `annotations` map (today,
  admission-cost telemetry — `rows_scanned_est`, `expansions_est`,
  `out_card`, `time_est_ms`, keyed by `node_digest`) alongside its result.
  It sits outside the payload that `result_digest` covers — structurally,
  not by convention — so a result recomputed byte-identically keeps the same
  digest whether or not this field is present, and whatever it carries is
  presentation/telemetry, not part of what a claim can be verified against.
- **The record wire format (`tgms-artifact`) is v0.x.** Field names, the
  refusal taxonomy's exact members, and the registry file's own layout may
  still change; only the freshness verdict contract above is a promise.

---

## 7. `tgms store backup` / `tgms store restore` (added Lane A EXP-A2, 2026-09-13)

**Backup is the event log; restore is replay.** §1 already establishes the
event log as the durable source of truth and the derived store as a
rebuildable cache; backup and restore are exactly that fact turned into two
verbs, adding no new durability mechanism of their own.

- **`tgms store backup --store <src> --dest <dest>`** writes a *quiesced*
  copy: `<src>/eventlog.jsonl` copied byte for byte to `<dest>/eventlog.jsonl`,
  plus `<dest>/backup_manifest.json` recording `store_identity`, the source's
  generation and `manifest_sha`, the copied log's own sha256 and record
  count, a backend-independent logical digest (`store_digest()`), and the
  `tgms_version`/`commit` that made the backup. The source is opened
  `read_only=True` — the one mode that never runs `Store._recover` or
  publishes a generation (§2's "recovery is a writer's act") — so taking a
  backup never mutates the store being backed up.
- **`tgms store restore --dest <dest> --store <new_store>`** replays that
  backed-up log into a fresh store at `<new_store>`, reusing exactly the
  `tgms replay` CLI path (copy the log into place, `replay(..., thread_cursor=
  True)`), then compares the result's `store_identity` and logical digest
  against the backup manifest and prints a `PASS`/`FAIL` verdict — nonzero
  exit on any mismatch. The backed-up log's sha256 is checked against the
  manifest *before* replay runs at all, so a tampered backup fails loudly
  instead of silently reconstructing something else.
- **What this is not:** a scheduling, retention, or incremental-backup
  mechanism — `--dest` is a plain directory, and running backup twice
  overwrites it. There is also no cross-machine or cross-version portability
  claim beyond what §1 already makes for the event-log format itself.

---
## 8. The service surface: backpressure, limits, logging, health, the
## writer lock (added 2026-09-13)

Everything in this section is **additive**: it changes nothing about the
event log, the derived store's binary format, or the fifteen operators'
semantics, and every one of its defaults reproduces prior behaviour exactly
unless a caller opts in (an environment variable, a constructor argument, or
running as one of the two server entry points, `tgms serve` / `tgms
webapp`). None of it touches TGIR's plan-cost admission
(`tgms.tgir.admission`, frozen) or the write/recover paths' own logic
(`tgms.store._write`, `tgms.store._recover`) — it wraps them.

**Bounded ingestion queue (`tgms.write.GroupCommitWriter`).**
`max_queue=0` (the default) is `queue.Queue`'s own "unbounded," identical to
every construction before this section. A caller that passes `max_queue=N`
gets `QueueFullError` (`E_QUEUE_FULL`) immediately from a non-blocking
`submit()` once `N` submissions are queued awaiting the committer thread —
never a silently unbounded pile of blocked submitter threads. `submit(op,
block=True, timeout=...)` waits for room instead of refusing; a timeout that
elapses still raises `QueueFullError`, not a bare `queue.Full`. `rejected`
and `queue_depth` are both in `GroupCommitWriter.stats()` and, when
`TGMS_METRICS_PATH`/a `Metrics` instance is supplied, on the metrics sink as
a counter and a gauge.

**Service-surface limits (`tgms.tools.limits.Limits`,
`tgms.tools.server.ToolRouter`).** `Limits(max_rows, max_bytes,
max_concurrent, max_wall_s)`, from the constructor or
`TGMS_MAX_ROWS`/`TGMS_MAX_BYTES`/`TGMS_MAX_CONCURRENT`/`TGMS_MAX_WALL_S`. A
bare `Limits()` (every field `None`) enforces nothing — the default for
every pre-existing `ToolRouter` construction, including every experiment
lane. `ToolRouter.call` enforces `max_concurrent` with a non-blocking gate
(refuse, don't queue — queueing is the bounded-queue's job on the *write*
side, not this call surface's) and counts a successful result's rows/bytes
*after* execution, refusing over either ceiling. **Refuse, never truncate**
(§3's "an answer must not rest on a shrunken denominator without saying
so," applied here): a call over a row/byte/concurrency ceiling returns a
structured `E_LIMIT` payload carrying `details.stage == "limit"` — shaped
like a TGIR `RefusalCertificate`'s `stage` field
(`tgms.tgir.admission.RefusalCertificate`, `stage in {"plan", "node",
"runtime"}`) but a distinct mechanism: admission prices a plan *before* it
runs against an estimate; this caps a call *before* (concurrency) and
*after* (realized size) it runs, independent of any estimate. `max_wall_s`
is carried as configuration only — **nothing in-process enforces it**; see
`Budget`'s own docstring (`tgms.tgir.admission`) for why a wall-clock
ceiling cannot be built from inside the process at all, and
`scripts/tgms_supervise.py` below for the out-of-process answer.

**Structured JSON logging (`tgms.tools.jsonlog`).** Off by default in
library use (`CallLogger(enabled=False)`, the default on a bare
`ToolRouter`); on for the two server entry points. Each call logs a
`"started"` line and a `"finished"` line, both carrying a `request_id`
(`uuid4().hex`, fresh per call) so the two can be correlated — this is the
shape `scripts/tgms_supervise.py` tails to find a request that started but
never finished. A **successful** envelope also carries its `request_id` in
a new top-level `annotations` key. This is digest-excluded the same way
§6's `annotations` field already is — *structurally*, not by convention:
`result_digest` is computed from `payload` before the envelope is
assembled, and `ToolRouter.call` only ever adds `annotations` to the
already-built envelope, after `result_digest` is already fixed. Calling the
same operator with the same arguments twice yields two different
`request_id`s and the same `result_digest`
(`tests/test_jsonlog.py::test_digest_is_unaffected_by_request_id_annotation`).
An **error** payload carries `request_id` inside `details` instead, because
`TgmsError.to_payload()`'s `{error, message, details}` shape is itself a
frozen surface (§2.13/`RefusalCertificate.raise_`'s own rule) that must not
grow a new top-level key.

**Health: `GET /live` / `GET /ready` (webapp), `tgms store ready`
(CLI).** `/live` means only "the process answered the request" — it never
touches the store. **`/ready`'s exact contract**
(`tgms.tools.webapp.ReadinessProbe`): the adapter's manifest generation is
readable, and — for a backend that keeps a replay cursor — that cursor is
internally consistent with the event log (within the log's current size,
landing on a record boundary, its recorded chain matches the log's chain
computed over that same prefix). A **writer** handle that exists at all has
already recovered (`Store.__init__` runs `_recover()` to completion before
returning), so `recovery_pending` is reported as `False` by construction,
not because nothing was checked. A **reader** never recovers by design
(D-049) — this probe's own consistency check is what notices a torn or
rewritten log under a reader that `_recover` would otherwise have caught
for a writer. The expensive half of the check (re-hashing the applied
prefix, `EventLog.chain_of_prefix`) runs **once**, at construction, and is
cached; `/ready` itself re-polls only the adapter (`stats()` still
answering) on every hit, so it stays cheap to poll frequently. `tgms store
ready <path>` runs the same probe from the CLI (`--writer` to probe as the
writer instead of the read-only default), exit `0` ready / `1` not ready /
`2` the open itself raised (corruption, or the writer lock already held).
The webapp stays loopback-only (`127.0.0.1` by default, unchanged); these
endpoints inherit that binding, not a new one.

**The OS-level single-writer lock (`tgms.store.Store`, `read_only=False`).**
The single-writer rule (spec §1) was a documented convention, enforced
nowhere — this closes that gap the same way §2's suffix-recovery work
closed the *reader*-opening-mid-commit hole. `Store.__init__` with
`read_only=False` takes `fcntl.flock(LOCK_EX | LOCK_NB)` on
`<store_path>/writer.lock` before running `_recover()`; a second concurrent
writer — another process, or a second `Store(read_only=False)` in the same
process, since `flock` locks are per open file description, not per process
— fails fast with `WriterLockedError`, naming the pid the lock file
records. Readers never take this lock, in either direction: a live writer
does not block a reader from opening, and a reader does not block a writer
from opening after it. The lock is released by the OS the instant the
holder's process exits or the file descriptor is otherwise closed — there
is no stale-lock file to clean up, and no window where a crashed writer's
lock wrongly blocks a new one.

**What is not covered, stated honestly:** the concurrency/row/byte limits
and the writer lock are new refusal *points*; they add no new persisted
state and no new wire format, so nothing above is a promise about a
specific error message's wording, a specific log line's exact key set
beyond the eight named above, or the manifest schema
`scripts/eval_overload.py` writes (`benchmarks/schema/result_manifest.schema.json`
v0.x, itself already marked as such). The wall-clock supervisor
(`scripts/tgms_supervise.py`) bounds elapsed time *per outstanding logged
request*; a call path that never logs a `"started"` line (library code that
bypasses `ToolRouter.call`) is invisible to it, and restarting a child is a
mitigation for availability, not a substitute for finding why a request ran
long.

---

## 9. The integrity checker's full mode (added Lane A A3, 2026-09-13)

`tgms store verify` had one mode: walk every file this generation names and
checksum it. That mode still exists, is still the default, and still costs
what it always did — it is now spelled `--fast`.

`--full` adds the checks that span files or read inside one. `tgms check
<store>` is the same thing with a positional argument, for the moment during
an incident when three words is two too many.

**What each mode checks.**

| Layer | `--fast` | `--full` adds |
| --- | --- | --- |
| `manifest` | this generation's self-sha, and its reconstruction from the checkpoint plus delta chain on disk | every *retained* generation's record, its parent link and `parent_sha`, and monotonicity of `created_tt`, `next_segment_id` and `event_log.offset` between adjacent generations |
| `segment` | every file's magic, header, per-column CRC, completion marker, and row count against the manifest | `rel_code` and string-heap references per row |
| `close` | every close run's header, checksum and entry count | close records addressing a row past the end of the segment they name |
| `dict` | record count against the manifest | every `src_id` / `dst_id` / `uid_id` a row carries, against the record count the manifest **commits** |
| `row` | — | `vt_s < vt_e`; `tt_s < tt_e` for closed rows; and I1 — no two *believed* versions of one identity overlapping in valid time |
| `eventlog` | — | per-record framing and `batch_id`, tt monotonicity, and the rolling chain over the applied prefix against the `(offset, chain)` cursor this generation recorded |
| `tcsr` | — | the persisted permutation's stamp and shape, plus a rebuild-and-compare content check |
| `registry` / `blob` | — | each artifact record's own `record_digest`, the per-name generation chain and its `supersedes` links, and the blobs `refresh.ref` / `plan.plan_ref` / `payload.result_ref` name |

**The report.** Every observation is a structured finding —
`{layer, kind, path, generation, detail, severity}` — and `--json` emits the
whole report. `layer` and `kind` are stable tokens meant to be matched on;
`detail` is prose and may be reworded at any time. `problems` remains the
list of `detail` strings for the `error` findings, and `healthy` remains
"no problems", so anything reading the pre-A3 report keeps working.

`severity` is either `error` or `advisory`. Only errors affect the verdict
and the exit code. The distinction exists because of one real case: a
persisted TCSR permutation goes stale on the very next write, and
`load_permutation` correctly ignores a stale stamp and rebuilds — so
reporting it as corruption would fail almost every store that has ever been
written to. It is reported (`tcsr-stale`) and it does not condemn the store.

**Exit codes.** `0` clean, `1` findings, `2` the store cannot be checked at
all — `CURRENT` malformed, a manifest failing its own checksum, a dictionary
shorter than the manifest commits. Those are refusals the engine makes before
a report exists, and "cannot be checked" is a different answer from "checked,
and here is what is wrong".

**Read-only, and this is a promise, not an implementation detail.** Verify
never writes, truncates, trims or recovers, in either mode. A torn event-log
tail is reported as `eventlog/torn-tail`; it is *not* trimmed, even though
`EventLog.trim_torn_tail` exists and normal recovery would do exactly that. A
stale index is reported; it is not rebuilt. Both commands therefore open the
store read-only and go straight to the storage adapter rather than through
`tgms.open`, because a read-write handle runs crash recovery on open and
would have repaired the defect before the check ran.
`tests/test_verify_full.py` asserts the property directly: a full check of a
store damaged in four places leaves every byte under the store root
unchanged.

**What full mode still does not check, stated honestly.** It does not
re-derive the store from its event log — that is `tgms replay` plus a digest
comparison, and it is the only thing that proves the materialization itself
is right. It does not check `props` beyond the bytes round-tripping, so a
semantically wrong but well-formed value is invisible to it. It reads
`manifests/` only up to `CURRENT`: a manifest above it is what a crash
between the manifest write and the `CURRENT` flip leaves, and the commit
protocol depends on that file being ignorable. And its memory is bounded by
one segment at a time plus one interval pair per currently-believed row —
proportional to the live version count, so a store with a very large believed
set will use more memory in `--full` than in `--fast`.

**Not promised:** the wording of any `detail`, the ordering of `findings`
within one report (it is deterministic for a given store, but not stable
across versions), and the exact set of `kind` tokens, which will grow as the
corruption sweep finds shapes worth naming separately. The six keys of a
finding, the `layer` vocabulary, `severity`'s two values and the three exit
codes are the parts to build on.

## 9. The production claim gate: `unverifiable` claims are also dropped
## (D-160, coordinator ruling, since 2026-09-15)

**Behaviour change, dated.** Since 2026-09-15 an emitted answer never
carries an `unverifiable` claim; coverage falls; UCR semantics unchanged.

Before this date, `tgms.eval.harness.run_task_ours`'s production gate
(`system="ours"`) dropped a claim from the delivered `AnswerObject` only
when `ClaimVerifier` verdicted it `unsupported`; a claim verdicted
`unverifiable` (evidence missing, malformed, or a provenance pointer naming
an uncited step — `tgms/agent/verifier.py`'s verdict vocabulary is
`supported | weakly_supported | unsupported | unverifiable`) survived and
was emitted to the user/record as if it were a normal claim.

The pre-registered trust-boundary fault-matrix campaign
(`benchmarks/faults-v1/fault-matrix-campaign-2026-09-13.json`, iTiger job
211007) found this was not a theoretical gap: under the deployed
(pre-fix) gate, 303 of 3,102 trials were `silent-violation`s of invariant
I1 ("no emitted claim unsupported by the evidence it cites"), of which
271 were the F1-9 wrong-step-citation fault surviving as an emitted
`unverifiable` claim — a wrong-step citation the pipeline could not
verify but delivered anyway. Under the secondary `--strict-gate` arm
(which already dropped both verdicts), the same trials became
`explicit-failure`s and the silent-violation count fell to 32 (all F2-3,
a pre-registered stated-assumption probe outside the trust model, §3 of
the frozen fault-matrix design memo). The finding falsified the
pre-registered "zero silent violations" claim for the *deployed* gate.

**The ruling (D-160, coordinator, 2026-09-15):** the trust boundary must
not emit a claim it cannot verify. The production gate in
`tgms.eval.harness.run_task_ours` — and the fault-matrix driver's own
`gate_answer()` default in `tgms.eval.plan_faults`, which mirrors it —
now drop `unverifiable` claims as well as `unsupported` ones. Both call
sites read the same constant, `tgms.eval.plan_faults.GATED_VERDICTS =
("unsupported", "unverifiable")`, so they cannot silently diverge again.
`scripts/eval_trust_boundary_matrix.py`'s `--strict-gate` flag is kept as
a **no-op alias**: since `unverifiable` is gated by default, `--strict-gate`
and its absence now compute the identical drop set and produce identical
results. `tgms/agent/reporter.py` was audited for a drop rule of its own
and has none — claim gating has always lived only in the harness's
delivered-answer path (`run_task_ours`), never in the reporter, so no
change was needed there. The CLI (`tgms ask`) and the demo webapp do not
gate at all — they print every claim beside its raw verifier verdict for
inspection — so they are unaffected by, and out of scope for, this
ruling.

**What does and does not change.** `ucr_pre_gate` (computed by
`ClaimVerifier.verify()` over the *raw*, un-gated `AnswerObject`, before
either gate applied) is unchanged in meaning and unaffected by this
ruling — it still measures what the reporter LLM produced, not what the
gate lets through. `ucr` and `coverage` (computed by a second
`ClaimVerifier.verify()` call over the *gated* answer) now reflect the
new, narrower gate: a claim that used to survive as `unverifiable` is now
counted as a withheld assertion by `coverage`'s uncovered-text accounting
(the text can still mention the number or uid the dropped claim used to
cover; with the claim gone, that mention becomes uncovered), so
**coverage falls** under the new gate relative to old cached rows.
`rates()` (`tgms/eval/metrics.py`) is untouched, as is every operator
digest and the write/recover paths — this is a claim-presentation change
in the agent evaluation harness, not a change to what any operator
computes or to any persisted on-disk format; `scripts/
check_digest_stability.py` covers that separation and is run as a gate
on this change.

**What is explicitly deferred, and why.** The CollegeMsg agent-loop
numbers already on public surfaces (`docs/site_facts.json`'s `ucr_gated`
0/199, coverage 0.706 at conditional accuracy 0.548; `README.md`;
`docs/TECHNICAL_REPORT.md`) were measured under the *old* gate (drop
`unsupported` only) and are **not** updated by this note or this commit.
They stay as measured until a separate LLM campaign (Qwen2.5-14B-AWQ,
three seeds, on iTiger) re-measures them under the new gate — coverage is
expected to fall once that campaign runs, per the ruling above — and no
public surface may cite the new numbers until that campaign lands.
`benchmarks/faults-v1/fault-matrix-campaign-2026-09-13.json` (the pre-fix
finding) and the `-d160` re-run record (the post-fix result, primary arm
only) are both kept and both reported; neither supersedes the other.

- **2026-09-14 — dependency-scope narrowing coverage 3 → 13 of 14 read
  operators.** `tgms/tgir/leaves.py::LEAF_SCOPES` now derives a Level-0
  scope for `count_temporal_motifs`, `find_temporal_motif_instances`,
  `temporal_reachability`, `temporal_paths`, `burst_detection`,
  `graph_metric_timeseries`, `co_active`, `diff_snapshots`,
  `version_history` and `snapshot_subgraph` (only `resolve_entities` keeps
  the coarse `"*"`; `compute` has an empty scope). Operators that traverse
  neighbourhoods keep ⊤ on the incidence arm. Every operator that resolves
  argument uids through `dense_ids` (including the already-derived
  `neighborhood_evolution`) now also emits an *existence pair*, so
  registering a previously unknown uid is a dependency of a call that
  failed with `E_NOT_FOUND`. Effect: `affected()` candidate sets shrink
  (never grow) for artifacts built on these operators; result digests are
  unchanged (38/38); freshness verdicts can only move from stale-flagged to
  fresh where a recompute would not change, never the reverse — guarded by
  the per-operator differential tests and the read-tracing property test
  (`tests/test_scope_read_tracing.py`). Records taken before this date
  (storm-v1) measured the 3-of-14 state and stand as measured.

## 10. `Store.ingest_events`' default-`disc` offset no longer resets per
## top-level call (fixed 2026-09-15, found in lane B7a)

**Bug (pre-fix).** `tgms/storage/base.py::_ingest_events` gives every event
without an explicit `disc` a default derived from its position in the bulk
stream: `disc = f"#{offset + i}"`, where `i` is the event's index within one
op's own `events` list and `offset` is supplied by the caller.
`Store.ingest_events` (`tgms/store.py`) supplied that `offset` from a local
variable reset to `0` at the top of *every* call to `ingest_events` —
correct only across the `INGEST_CHUNK`-sized chunks *within* one call, since
those chunks shared that call's own running `offset`. A caller that split
one logical bulk load across several **top-level** `ingest_events` calls
(batching) instead got the same default-`disc` sequence (`"#0"`, `"#1"`,
…) from each call. Two events at the same intra-call index in different
calls then collided into the same edge identity (`edge_eid(src, dst,
rel_type, disc)`) whenever they shared `(src, dst, rel_type)` — two
logically distinct edges silently merged into one.

Found auditing `scripts/build_synth_store.py` (lane B7a), which had already
worked around it by stamping `disc` explicitly per event rather than relying
on the implicit default (see that script's module docstring and
`_event()`). The audit found one other real caller on the affected path:
`scripts/longevity_run.py::apply_correction_via_public_api`'s `a1_events`
branch, which calls `store.ingest_events(op["events"], ...)` — a single,
undecorated event, no explicit `disc` — once per correction, many times
over the life of one longevity run. Every such call landed at intra-call
index `i=0`, so any two `a1_events` corrections that happened to share
`(src, dst, rel_type)` over a run's lifetime collided. See
`ops/failure_ledger.jsonl` for the full per-caller audit and which records
this could have reached.

Related but **not** fixed by this change: `tgms/eval/corrections.py::
_a1_events` bakes a literal `offset=0` into the `ingest_events` op it
builds, and `tgms/eval/storm.py`'s `Storm._write` applies that op straight
through `adapter.apply_ops`, bypassing `Store.ingest_events` (and this
fix's counter) entirely — so a storm run applying repeated `a1_events`
corrections can still collide the same way. This is a separate hazard
sharing the same symptom, tracked in `ops/failure_ledger.jsonl` as a
follow-up, not fixed here.

**Fix.** `Store` now keeps the offset base as instance state,
`self._ingest_offset_base`, initialized to `0` in `__init__` and advanced
by each chunk's length after every write — never reset within a `Store`
handle's lifetime, so two top-level `ingest_events` calls against the same
open handle can never produce colliding default `disc` values. A store's
*first* (or only) `ingest_events` call is unaffected: the instance counter
starts at `0`, exactly the old per-call `offset`, so single-call callers
(`scripts/eval_harness.py::build_dataset`, `scripts/build_snb_store.py`'s
bulk path, `scripts/build_sx_store.py`, `scripts/build_synth_iv_store.py`,
and the large majority of the test suite) get byte-identical `disc`
assignments and digests — confirmed by `scripts/check_digest_stability.py`
(38/38 unchanged) and the existing `ingest_events`/footprint/freshness test
suites. This fix does **not** persist the counter to disk or otherwise
protect across closing and reopening a `Store` handle — see the "related
but not fixed" note above and the "not fixed" alternative bypass through
raw `adapter.apply_ops` for what remains open.

New regression coverage: `tests/test_ingest_events_disc_offset.py`.
