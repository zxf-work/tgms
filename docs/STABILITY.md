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
prints the same remedy: rebuild from the event log with `tgms replay`.

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
