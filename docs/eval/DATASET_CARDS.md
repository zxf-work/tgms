# Dataset cards

## LDBC SNB SF1 (BI initial data set)

Full card: `docs/eval/LDBC_SF1_CARD.md` — source, version pins, license
(CC-BY 4.0, LDBC disclaimer), the M1-M12 mapping rules and their two
pre-ingest amendments, sizes and the mapping-fidelity gate, the acquisition
checksums, the 21 frozen templates, the scored-bi / characterization-interactive
arms and the `--csv` anchor-binding rule that voided the 2026-09-15 rerun's
interactive arm, the BI6 defect and its BI6.v2 repair, and the gold arm's
unavailability.

## synth (200k / 1M / 10M events)

Generator: `scripts/eval_harness.py::build_dataset` — deterministic from
the scale alone (splitmix64 endpoints, no runtime randomness). Properties,
each added after its absence made a comparison vacuous (lessons §9a):
constant average degree (~10) via |V| and edge lifetime scaling together;
community structure (50-node blocks, 70% intra) so motifs exist; one
deliberate 10× burst; a second belief epoch (corrections + retractions,
including interval-splitting corrections so `props_changed` is exercised);
`tt_epoch1` captured at build so belief probes discriminate. Sizes: |V| =
scale/100, edge versions ≈ scale + scale/200 corrections.

**30M/100M builds: `scripts/build_synth_store.py`, same family, streamed.**
`build_dataset` materializes `[_event(i, scale) for i in range(scale)]` as
one Python list before `Store.ingest_events` ever chunks it — tens of GB of
dicts at 100M before the store opens
(`docs/design/SCALE_BUILD_FORECAST_2026-09-15.md` §1a). `build_synth_store.py`
is the same generator math (`n_nodes`, `edge_life`, `_vt_s`, `COMMUNITY`,
`INTRA_PCT`, the splitmix64 finalizer, and `build_dataset`'s full four-phase
op sequence — bulk ingest, belief-epoch node assertion, epoch-2 corrections,
partial mid-interval corrections, retractions — all reused verbatim), rebuilt
to generate and commit one `--batch`-sized chunk at a time, resumable
(`--resume`, `--stop-at-ops`), reporting progress, RSS and manifest/segment
bytes as it goes. It adds one parameter `build_dataset` never had: `--seed`
(folded into the two per-event splitmix64 draws by XORing it into the mix
input before the finalizer); `--seed 0` reduces algebraically to
`build_dataset`'s own unseeded formula, so it is the value every existing
`synth-*` record (built before `--seed` existed) is consistent with.

**Equivalence proof, and the one thing that cannot be proved.** `--n-entities
50000 --seed 0 --batch 50000` (single-chunk, matching `build_dataset`'s own
chunking at this scale — `INGEST_CHUNK` is also 50,000) produces a store
whose *logical content* is identical to `build_dataset(50000)`'s, verified by
a **content digest**: the same node/edge fields `Store.store_digest()` sorts
and hashes, minus `vid`, `tt_s` and `tt_e`. Those three are excluded because
`store.digest()` (`store_digest()`) itself can never be proved equal between
*any* two independent builds of the same logical data, this pair included —
already-settled fact, not a new finding: `tt_s` comes from
`HybridLogicalClock.tick()` (`tgms/core/clock.py`), a wall-clock value, and
`vid = sha256(identity:tt_s:vt_s)` inherits it. This file's own "Loading
rule" above and `scripts/check_digest_stability.py`'s docstring both already
state it (D-023): "independently built stores of the same data legitimately
differ in tt and every derived id." So `build_synth_store.py`'s test suite
(`tests/test_build_synth_store.py`) proves content-digest equality, not
`store_digest()` equality — that is the record of truth for this family's
reproducibility from here on, and `store_digest()` differing between two
builds of the same `--seed` is expected, not a regression.

One `--batch`-sensitive corner, inherited from `Store.ingest_events` itself
and not introduced by this script: an auto-created bare node version's
`vt_s` is the minimum `vt_s` among same-*chunk* occurrences before the
node's first believed version is asserted; a node already known from an
earlier chunk is never re-asserted regardless of a smaller `vt_s` arriving
later. Every edge version's own identity, endpoints, `rel_type`, `vt_s`/
`vt_e` and props are fully `--batch`-invariant (disc is stamped explicitly
per event, `f"#{i}"`, never left to `ingest_events`'s own per-call offset
default — see the script's module docstring for why relying on that default
across multiple top-level calls is itself a trap: the offset resets to 0
on every call, not just every internal chunk). `tests/
test_build_synth_store.py::test_determinism_per_batch` checks the
batch-invariant half (edge content) directly across two different `--batch`
values.

**`--digest {none,full,streaming}` (B7a).** `build_synth_store.py` used to
call `store.digest()` unconditionally at the end of every build — the same
`store_digest()` (`tgms/storage/base.py`) that sorts and canonical-JSON-
encodes one Python dict per version, whole-store, in one shot. A 17.4M-edge
SF1 store already showed what that costs at scale: a flat ~2.4 GB RSS
through streaming ingest, spiking to ~25 GB in its own finalization pass
(`benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09.README.md`); at
100M+ entities `store.digest()` alone would exceed a 93 GB host. `--digest`
now controls that pass: `full` (default below `DIGEST_AUTO_THRESHOLD` =
10,000,000 `--n-entities`) is the old unconditional behavior; `none`
(default at or above that threshold) skips both `store.digest()` and the
script's own `content_digest()`, leaving the sidecar's `dataset.digest` as
the O(1) generation + manifest-sha identity instead; `streaming` computes
`store.digest_streaming()` in place of `store.digest()`. The streaming
digest is **byte-identical to `store_digest()` for the same store, proved
rather than assumed** (`tests/test_store_digest_streaming.py`,
`tests/test_build_synth_store.py::test_digest_mode_full_and_streaming_agree`):
`canonical_json`'s compact separators mean a row's JSON text never depends
on where in the outer array it lands, so an external merge sort over
disk-spilled, pre-sorted chunks can feed a running `sha256` the identical
byte stream `store_digest()` builds in memory, bounded by `chunk_rows` (and
by one buffered row per spilled chunk during the merge) rather than by the
store's row count.

Whether `scripts/build_snb_store.py`'s own SF1 build spike (above) is this
same digest cost was checked directly, and it is not: that build's own
provenance (the README cited above) used `--digest manifest`, and
`_identity()`'s `manifest` branch (`build_snb_store.py`) never calls
`store.digest()` at all — it reads `store.store_identity`,
`adapter.generation`, and the engine's pre-computed `manifest_sha()`
(`crates/tgms-engine-py/src/lib.rs`, a stored field, not a scan). The 25 GB
spike traces to the finalization steps immediately before that identity is
read: the forced end-of-build `compact()`+`gc(keep_last=2)`
(`build_snb_store.py::build`, `maybe_compact(force=True)`) and
`store.stats()`, whose `stats_accum()` (`crates/tgms-engine-core/
src/read.rs`) walks every edge segment's `vt_s`/`vt_e`/`src_id`/`rel_code`
columns into memory — already a rewrite away from an earlier, worse
`all_edge_versions()` route per that function's own docstring, but still a
whole-store pass. The README's own instrumentation samples RSS every 60s
and cannot separate compaction from stats within that window ("the late
spike is the whole-store stats/digest pass ... fidelity check + card
write"), so which of the two dominates is not resolved here. Either way it
is not `--digest`, which is why `build_snb_store.py`'s existing
`none`/`manifest`/`full` flag (already defaulting to the cheap `manifest`
mode, unlike `build_synth_store.py`'s old unconditional `full`) was left
unchanged by this task — narrowing `stats()`/`compact()`'s own cost is a
separate piece of work.

## CollegeMsg (frozen replay)

`benchmarks/frozen-v1/collegemsg.eventlog.jsonl` — 59,835 instantaneous
messaging events, real timestamps (Apr–Oct 2004), 1,899 users, heavy
degree skew, no corrections. Replay digest is byte-frozen across backends.
Known dataset truths: instant snapshots and strict-overlap joins are
legitimately thin (microsecond intervals); the belief probe pins
mid-ingestion state; degree skew is what exposed the motif cost-model
false positive.

## sx-mathoverflow / sx-superuser (Stack-Exchange, typed edges)

SNAP Stack-Exchange interaction networks, three raw files per dataset
(one per edge type: `A2Q` answer-to-question, `C2Q` comment-to-question,
`C2A` comment-to-answer), streamed in that fixed order — so the recorded
event log is deterministic but valid time interleaves across types, a
real tt≠vt workload the single-file datasets don't produce. Verified at
load against SNAP's published stats:

- **sx-mathoverflow**: 506,550 events / 24,818 nodes / 2,350 days
  (A2Q 107,581 · C2Q 203,639 · C2A 195,330)
- **sx-superuser**: 1,443,339 events / 194,085 nodes / 2,773 days
  (A2Q 430,033 · C2Q 479,067 · C2A 534,239)

The typed edges make `rel_types`-filtered operators meaningful on real
data for the first time (CollegeMsg and wiki-talk are single-type).

## wiki-talk (temporal)

SNAP `wiki-talk-temporal`: 7,833,140 events / 1,140,149 nodes / 2,320
days, single edge type `TALK`, instantaneous. The 10M-class real graph
with extreme hub skew (admins and bots) — selected as the guardrail
stressor: the D-086 frontier's skew forecasts (F2) were written against
synthetic skew, and this is the real thing. Build the store on a server;
the raw file alone is 7.8M lines.

## Loading rule (all datasets, all systems)

One recorded event log per dataset; every system loads *that* (TGMS
backends by replay, baselines from the canonical rows a native store
produces). Independently built stores of the same data legitimately differ
in tt and every derived id — the first differential run failed exactly
this way (D-023).

## synth-iv-60k (interval-valid-time synth)

Deterministic interval-valid-time synth, adopted 2026-08-28
(M5_CARVE_POPULATION_PROPOSAL, DECISION 5 ratified by the owner) to
answer §13.10's carve-arm question, which bitcoinotc/collegemsg cannot:
both are instantaneous-event stores (every believed interval `[t, t+1)`),
so no outside-window correction can ever carve a version their windows
read — measured twice (M4 §10, M5 Addendum 5).
`scripts/build_synth_iv_store.py` reuses the synth community/degree
structure (600 nodes, avg degree ~10, one burst band) and replaces the
constant 5%-of-extent edge lifetime with a log-uniform interval-length
distribution spanning 0.5%–50% of the extent, drawn from an independent
splitmix64 stream per event. 60,000 events / 600 nodes, epoch-1 only.
Event-log op payloads are byte-identical across independent builds;
`store_identity` is not (HLC tt, D-023). The builder self-verifies a
fitness probe at build time: one seeded outside-window correction must
change an `aggregate_events` duration answer, printed in the card.
Scored population for the carve arm only, per the M5 campaign freeze's
Addendum 6.

## osv-live (live workload, added 2026-09-13, Lane F3 P0.5)

The OSDI paper's live-ingestion arm: a real, continuously-revised feed
rather than a frozen replay. Design of record:
`docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md`. Loader:
`tgms/data/osv_loader.py`; service: `scripts/live_osv_poller.py` +
`scripts/live_supervise.sh`; daily query workload: `scripts/live_osv_queries.py`.

**Source and license.** OSV advisory JSON (schema 1.x) for four ecosystems —
PyPI, Go, Maven, crates.io — from the public per-ecosystem `all.zip` exports
at `https://osv-vulnerabilities.storage.googleapis.com/<ECOSYSTEM>/all.zip`.
`MAL-` (OpenSSF Malicious-Packages) records are filtered in every ecosystem
(design §3: 96.8% of npm's records are `MAL-`, which is why npm is excluded
entirely). The OSV schema/tooling are Apache-2.0; the advisory *content* our
four ecosystems draw on is CC-BY-4.0 (GitHub Advisory DB, PyPI/Go/OSS-Fuzz
style feeds) or CC0-1.0 (Rust Advisory DB) — never the CC-BY-SA (Ubuntu) or
unaudited sources OSV also lists (design §6). Redistribution is
attribution-only, per §6: the raw daily feed tarballs ship with a `NOTICE`
naming each upstream database and license.

**Bootstrap graph** (design §3, measured against the 2026-09-13 export):
32,912 advisories, 8,893 packages, 50,842 `affects` edges, 112,317 range
events, 64,027 alias links, 186,147 references, 931 already withdrawn —
≈413k edge versions, ≈155k node versions. Entities: `Advisory`, `Package`
(ecosystem-qualified), `PackageVersion`, `Range`, `Reference`, `Ecosystem`.
Relations: `affects`, `range_of`, `fixed_by`/`introduced_in`, `aliases`
(written both arms), `references`, `withdraws`. Identity is one function,
`osv_uid(kind, *parts) = kind + ":" + "\x1f".join(parts)` — mirrors
`snb_loader.snb_uid`'s one-definition-site discipline; OSV ids are strings,
so the SNB integer interleave does not apply. **Ranges are the belief;
enumerated `affected[].versions` are never materialized** (design §1: the
four ecosystems carry 1,975,063 explicit version enumerations against only
112,317 range events — enumerating them would triple the edge count with an
83%-redundant re-expansion of what the ranges already state).

**Live delta mechanism.** Hourly polling of each ecosystem's
`modified_id.csv` (reverse-chronological; the poller stops at its own
high-water mark), `GET /v1/vulns/{id}` for every id past it, self-limited to
5 req/s. Projected 60-day volume (design §3): ~2,200 new advisories, ~8,900
revisions, ~5,000–6,000 genuine content corrections atop the bootstrap.

**What is a correction (design §2).** `published` maps to `vt_s`
(`vt_e = OPEN_END`); `modified` carries no valid-time meaning at all — it
only triggers a re-read. A re-observed advisory is diffed on
**canonicalised content** (`osv_loader.canonical_digest`), never raw bytes:
an unchanged digest is a no-op (`noop_revisions`, roughly a third of feed
churn per design §2); a changed digest becomes exactly the ops the §2 table
names — `correct` for a range edit or a severity/summary change, `assert_*`
for a newly-acquired alias/reference/affected-package (belief is new, even
though the fact "was always true"), and, for a `withdrawn` transition, both
a `correct` (the belief record) and a `retract` of every open `affects` edge
at the withdrawal instant — deliberately both, per §2, so the store keeps
the audit that the advisory was once believed live *and* stops answering
"what is vulnerable now" with it.

**Service.** One writer (`scripts/live_osv_poller.py`), one batch per poll
cycle — small and frequent rather than large and rare, since each cycle is
one event-log record, one generation, one freshness-checkable correction
batch. `compact()` + `gc(keep_last=2)` every 100 cycles (design §5,
evidenced by `scripts/build_snb_store.py`'s own manifest-growth measurement:
uncompacted, manifest bytes alone reached 25,451 MB on a 27 GB store).
Crash safety is the engine's write-ahead log, already measured
(`docs/eval_durability.md`) — the supervisor (`scripts/live_supervise.sh`)
only restarts and lets `open()` recover; it carries no WAL of its own.
Metrics: one JSON object per cycle in `run/live_metrics.jsonl` (design §5's
full field list); failures append to a JSONL ledger before re-raising (see
`scripts/live_osv_poller.py::_write_failure_ledger_entry` for the documented
gap between this live-incident record and `ops/failure_ledger.jsonl`'s own
curated-defect schema — the two are not the same file by default).

**Query workload** (design §4): `scripts/live_osv_queries.py`, a daily
seeded (`seed = ISO date`) run of 200 queries — five families of 40 — through
the same `ToolRouter` the MCP server exposes, over up to 500 registered
per-package "current exposure" artifacts (`Registry.register`, `kind:
"osv_exposure"`, an opaque `snapshot_subgraph` leaf). Each cycle's
correction batch is walked through `artifact.lookup.affected` and every
flagged record refreshed via `artifact.refresh.refresh` — the live
instantiation of M5 §3.2's all-scopes walk, `intersects_calls` measured
per cycle rather than predicted.

## Loading rule, extended for osv-live

The loading rule above (one recorded event log per dataset, replay
reproduces it) holds here too, with the caveat every live/appended dataset
shares: `tt` and every derived id are store-specific (D-023), so what is
reproducible is the **op stream** a fresh replay of `eventlog.jsonl`
produces, not a byte-identical second bootstrap+poll run against the live
feed (which would fetch a different `modified_id.csv` snapshot entirely).
`tests/test_osv_loader.py::test_replay_reproduces_digest` verifies the
former on the 20-advisory fixture under `tests/fixtures/osv/` (see its own
`README.md` for exactly what each fixture record covers and its
provenance/license).
