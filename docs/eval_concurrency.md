# Concurrency evaluation (D-043 item 3)

Regenerate: `uv run python scripts/eval_concurrency.py {mixed,commitcost,groupcommit,residency} --json out.json`

Every concurrent number this project had before today was **readers-only**
(§14.4 of `docs/eval_resources.md`): N reader processes over a store nobody
was writing. This measures the mixed case, the singleton-write floor, and the
index-residency budget D-045 deferred.

Two conventions, both from things that went wrong here before:

- **Aggregate throughput and per-query latency are different claims** and are
  never substituted for one another. Each table carries both, or says which
  one it is.
- **Concurrency is the noisiest thing this project measures.** Every cell is
  a distribution over repeated trials, per-trial medians are shown rather
  than pooled into one number, and a difference inside the between-trial
  spread is reported as a tie.

## Receipts (spec §8.4)

- commit `cb3248e`, xzgpu (40 cores, 93 GB, Linux 5.4), Python 3.12
- one fresh process per condition (§9g); readers open `read_only=True`
- mixed mode copies the store per trial, so a writer's thousands of
  generations never leak into the next condition or into the cached store
  other harnesses replay from
- raw records: `conc-commitcost.json`, `conc-groupcommit*.json`,
  `conc-mixed-1m.json`, `conc-residency-1m.json`
- **±20% reproducibility bound** applies here as everywhere (D-045)

---

## §19 Correctness first: what a commit does to a reader

The design argument is that a commit cannot disturb a reader: segments are
immutable, a reader pins a manifest generation at open, and the only mutation
is an append-only close record. `engine_lessons.md` §6 is the reason that
argument is not sufficient on its own — an earlier draft of the same design
kept a store-wide mutable close set that would have let a reader holding
generation *N* observe visibility from *N+1*.

`tests/test_concurrency.py` asserts the contract instead of arguing it. Six
tests, each written as "what would a violation look like": a pinned handle
answering four differently-shaped questions identically through twelve
published generations; a reopening reader seeing only whole batches, in
order; a fixed past-belief question taking exactly one value while ten
corrections land; a read-only handle refusing all five write entry points;
opening inside the write-ahead window publishing nothing and changing no
byte; and three processes opening throughout a write run, after which the
store verifies and still replays to the same digest.

**The contract holds. Two defects had to be fixed to make it hold**, and both
were in what *opening* a store does rather than in what reading one does.

| defect | what it did | fix |
|---|---|---|
| `Store.__init__` ran crash recovery on every open | a live writer is always in the state recovery reads as a crash (the log is fsynced before the batch is applied), so every reader replayed the suffix and published a generation concurrently with the writer publishing the same one | `tgms.open(..., read_only=True)`: no recovery, write API refused |
| `Dictionary::open` truncated `dict.log` past the manifest's byte count | indistinguishable from the live writer's in-flight tail; a reader opening there deleted bytes the writer had fsynced and was about to name, so the generation it then published would not open | open never mutates; the writer overwrites from its own committed offset |

Measured, three reader processes opening in a loop against a writer
committing 40 batches:

| | before | after |
|---|---|---|
| writer | **aborted on batch 2**, `io: No such file or directory [seg/000000000016.tgs]` | completed all 40 batches |
| readers | 2 of 3 died the same way | 8,141 opens, 0 failures |
| rows landed | 24 of 336 | 336 of 336 |
| store afterwards | opens, but is not what was written | verifies; digest matches its own event log |

Neither is exotic. This is the second-simplest concurrent configuration there
is, and it failed in under a second the first time it ran.

---

## §19b What a live writer costs readers, and readers cost the writer

1M events, N reader processes looping the §14.4 mix, against a writer
committing 100-row batches as fast as it can. Three trials per condition,
30 s window each, one fresh process per participant, a private copy of the
store per trial. **1M rather than 10M deliberately**: §14.4 found the host
OOM killer taking 2 of 16 readers at 10M (~4 GB each on a 93 GB host), and
adding a writer to a configuration already at the memory ceiling would
measure the OOM killer rather than concurrency.

### Aggregate throughput (queries/second, per-trial values)

| readers | writer idle | writer running | cost |
|---:|---|---|---:|
| 1 | 15.62, 15.41, 15.66 | 15.38, 15.30, 15.66 | 1.5% |
| 2 | 31.45, 31.44, 31.65 | 30.77, 30.98, 31.18 | 1.5% |
| 4 | 60.84, 60.98, 60.81 | 60.80, 60.68, 60.29 | 0.3% |
| 8 | 117.31, 117.85, 117.31 | 116.78, 116.99, 117.01 | 0.3% |

Reader throughput scales **7.5× at 8 readers with the writer idle and 7.6×
with it running** — the live writer does not change the scaling shape.

### Per-query latency (p50 ms, median of three trial p50s)

This is a *different claim* from the row above and is reported separately.

| query | I1 | W1 | I2 | W2 | I4 | W4 | I8 | W8 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hist.single | 0.281 | 0.283 | 0.273 | 0.293 | 0.308 | 0.310 | 0.320 | 0.318 |
| snap.hop2 | 100.4 | 100.9 | 99.0 | 100.6 | 103.0 | 102.8 | 107.4 | 106.8 |
| series.count | 43.45 | 43.71 | 43.20 | 44.46 | 44.25 | 45.18 | 45.70 | 46.15 |
| coactive.narrow | 112.5 | 112.8 | 111.3 | 114.9 | 114.6 | 117.1 | 118.8 | 119.7 |

Tails move no differently: pooled p99 for `coactive.narrow` is 123.2 (I1) →
122.8 (W1), 124.4 (I2) → 128.3 (W2), 128.6 (I4) → 128.8 (W4), 134.6 (I8) →
134.0 (W8).

**A live writer costs concurrent readers 0–3% of scan latency**, with
per-trial spreads under 1%, so the 2–3% at 2 and 4 readers is a real effect
and the ~0% at 1 and 8 is a tie. It is smaller than the reader-count effect
sitting next to it: `coactive.narrow` moves 112.5 → 118.8 ms going from one
reader to eight *with no writer at all*, which is the scan-thread
oversubscription §14.4 already recorded.

### What readers cost the writer

| concurrent readers | commit p50 per trial (ms) | pooled p50/p90/p99 | commits/s |
|---:|---|---|---|
| 1 | 36.93, 36.33, 36.14 | 36.45 / 56.41 / 61.54 | 27.02, 27.13, 27.26 |
| 2 | 36.97, 36.38, 36.67 | 36.68 / 57.22 / 62.44 | 26.99, 27.20, 27.15 |
| 4 | 36.32, 36.84, 36.87 | 36.75 / 57.77 / 63.48 | 26.95, 26.83, 26.89 |
| 8 | 36.67, 37.02, 36.86 | 36.82 / 57.70 / 62.75 | 26.85, 26.76, 26.88 |

**Nothing measurable**: 1.0% across an eightfold increase in readers, with
per-trial spreads of the same size, and identical p90/p99 shape. ~2,400
commits per condition.

### Why it is this small

Every reader in every trial reported **exactly one pinned generation (282)**
for its whole life, while the writer published thousands underneath it. That
is the design working rather than a coincidence: the reader's manifest is
read once at open, its segments are immutable, and the writer's commits
create new files instead of touching existing ones. What remains — the 0–3%
— is page-cache and I/O-bandwidth contention from the writer's fsyncs, not
coordination. There is no lock on this path to contend for.

---

## §20 The singleton-write floor, by layer

Before building group commit, measure what a commit costs. Two published
documents disagreed about this: `engine_lessons.md` §7 attributed the
batch-versus-singleton gap to "several fsyncs", `docs/eval_writes.md` to "a
fresh full manifest per commit naming every segment". Different fixes.

One write batch, driven layer by layer against a 100k-row store, 300 commits
per batch size, p50:

| layer | batch=1 | batch=10 | batch=100 | batch=1000 |
|---|---:|---:|---:|---:|
| write-ahead log fsync | 0.34 | 0.37 | 0.64 | 2.92 |
| `apply_ops` (Python semantics) | 0.24 | 1.39 | 10.60 | 104.67 |
| engine commit | **5.07** | 5.45 | 5.47 | 8.79 |
| — seal (segments) | 0.93 | 1.04 | 1.27 | 4.02 |
| — dictionary tail | 0.35 | 0.35 | 0.40 | 0.56 |
| — manifest | 1.17 | 1.20 | 1.20 | 1.25 |
| — `CURRENT` | 0.72 | 0.73 | 0.70 | 0.72 |
| **total ms** | **5.76** | 7.27 | 16.80 | 117.29 |
| **ms per row** | 5.764 | 0.727 | 0.168 | 0.117 |

**A third cost was in front of both of them, and it is not on this table
because it was fixed first.** At batch=1 the total was 90.6 ms, of which
`apply_ops` was 59.9 — an existence probe materializing the whole node store
to answer a question about two uids (`engine_lessons.md` §16). The table
above is the world after that fix.

### How big that third cost was depends on entity cardinality — including on this table

The probe materialized every *node version*, so its cost scales with the
number of distinct entities, not with the number of events. Measured on
xzgpu, the old engine-side scan (recovered by forcing the same branch, which
is flat in the number of uids asked about) against the postings probe that
replaced it:

| stored node versions | old probe | new probe |
|---:|---:|---:|
| 1,000 | ~0.6 ms | 0.005 ms |
| 40,000 | 24.2 ms | 0.005 ms |
| 200,000 | 157.4 ms | 0.007 ms |

**This retired a claim of our own, one paragraph after making it.** The
90.6 → 34.9 ms headline was measured with a generator that mints a fresh
entity pair per event — 200,000 node versions at a 100k-event store, the
high-cardinality end. `docs/eval_writes.md` uses the project's standard
generator, where `n_nodes = scale / 100`: its append benchmark runs against
**1,000 entities**, where the defect was worth under a millisecond. And
indeed the re-measured write table below is unchanged.

So the honest form of the claim: the defect was an **O(entities) cost paid by
every ingest batch**, invisible at the entity cardinality this project's own
write benchmark uses and dominant an order of magnitude above it. Both
statements are true and only one of them was going to get written down if the
published table had not been re-run.

### The write table, re-measured (xzgpu, commit `cb3248e`)

| | published (`caa5246`) | today |
|---|---|---|
| load 200k | 4.58 s / 43,700 ev/s | 4.56 s / 43,818 ev/s |
| load 1M | 23.56 s / 42,400 ev/s | 23.63 s / 42,311 ev/s |
| append b=1 | 9.83 / 17.71 ms, 96 ev/s | 10.31 / 16.58 ms, **95 ev/s** |
| append b=10 | 4.06 / 6.30 ms, 1,846 ev/s | 3.78 / 5.44 ms, 2,582 ev/s |
| append b=100 | 5.28 / 6.13 ms, 18,703 ev/s | 5.30 / 6.97 ms, 17,806 ev/s |
| append b=1000 | 21.76 / 21.43 ms, 45,958 ev/s | 22.28 / 21.77 ms, 44,874 ev/s |
| correction p50 | 4.84 / 6.98 ms | 4.66 / 6.8 ms |
| correction growth | 63,671 B | 63,694 B |

Every cell inside the ±20% band, and `append b=1` unmoved at 95 ev/s. **No
published write number changes.** The registry is likewise unaffected: all 13
queries still agree by hash across native and DuckDB at 1M.

*Now* both are partly right, about different things, and the split says which
fix belongs to which.

**§7 owns the floor.** At batch=1 the engine commit is **5.07 of 5.76 ms,
88%** — it is the durable generation, four fsynced file writes, exactly as
§7 says. There is nothing else there to remove.

**`eval_writes.md` owns the growth.** Its claim is that singleton commits get
progressively more expensive because each rewrites a manifest naming every
segment. First decile against last decile of the same batch=1 run, as the
store goes from 35 to 575 segments and the manifest from 18 KB to 286 KB
(15.9×):

| phase, µs | first decile | last decile | ratio |
|---|---:|---:|---:|
| write-ahead log | 373 | 314 | 0.84× |
| `apply_ops` | 241 | 277 | 1.15× |
| seal (segments) | 1,025 | 877 | 0.86× |
| dictionary | 350 | 315 | 0.90× |
| **manifest write + fsync** | **728** | **1,615** | **2.22×** |
| `CURRENT` | 729 | 688 | 0.94× |
| **engine commit total** | **3,278** | **6,854** | **2.09×** |

The manifest phase is the **only** phase that moves. Everything else is flat
to within measurement noise across a 16× growth in store history — which is
also the cleanest available confirmation that the fsyncs really are a fixed
floor rather than something that scales with anything.

Two refinements the original claim did not have:

1. **The manifest's time grows far more slowly than its bytes** — 2.22×
   against 15.9× — because a `write_atomic` is two fsyncs (fixed) plus a
   write (linear), and at these sizes the fsyncs still dominate.
2. **Most of the growth is not the write at all.** The timed phases account
   for 2,832 of the first decile's 3,278 µs but only 3,495 of the last
   decile's 6,854. So of the engine commit's 3,576 µs of growth, 887 µs is
   the manifest write and fsync and **2,689 µs is manifest *handling*
   outside the timed phases** — cloning the parent's segment lists in
   `successor`, serializing, sha256-ing in `seal`, and `verify` — every one
   of them O(segments). By elimination, because every other phase is flat.

So the per-generation manifest is a CPU cost as well as a byte cost, and it
is the one part of a commit that a longer-lived store pays more for. Group
commit amortizes it along with the fsyncs (§21); bounding it directly is
compaction's and gc's job and is not attempted here.

The `eval_writes.md` claim that **64 KB of store growth per correction is
almost all manifest** is a space measurement and is unaffected.

---

## §21 Group commit

With the floor established, the lever §7 names: coalesce at the write API so
that concurrent single writes share one durable generation. `tgms.write.
GroupCommitWriter` queues ops; one committer thread drains what is queued and
publishes one generation for all of it.

**The durability contract is unchanged.** A submit returns only after the
commit containing it has published. Nothing defers or weakens an fsync; the
number of commits falls, not the cost of one. Replay (D-042) is unchanged: a
coalesced batch is one log record in queue order, replayed as one batch,
advancing the cursor once; if the coalesced apply fails the group rolls back
and each submission retries alone, so one caller's bad op fails one caller.

Baseline note: **N threads calling `assert_edge` did not previously work at
all.** `Store._write` was not thread-safe — concurrent callers interleaved
and surfaced as `transaction time must advance ... this is an engine bug`,
losing writes and blaming the engine. `Store` now serializes `_write` under a
lock, which is both the correct behaviour and the only baseline group commit
can honestly be measured against.

100k-row store, **400 rows per writer** — one trial per condition, 400
latency samples in each. (Two further trials were planned and abandoned: the
serialized 32-writer condition alone commits 12,800 generations and takes
about thirteen minutes, and the shape had already replicated. **The trial
count is one**; the independent run below is what stands in for a spread.)

| writers | serialized rows/s | coalesced rows/s | speedup | serialized p50 / p99 | coalesced p50 / p99 | generations |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 164.5 | 109.0 | **0.66×** | 5.67 / 8.86 ms | 8.86 / 12.95 ms | 400 → 400 |
| 2 | 96.5 | 211.4 | 2.19× | 20.36 / 35.18 ms | 9.18 / 12.59 ms | 800 → 400 |
| 4 | 63.2 | 398.9 | 6.31× | 63.31 / 153.12 ms | 9.58 / 13.85 ms | 1,600 → 401 |
| 8 | 43.1 | 698.7 | 16.2× | 182.68 / 485.78 ms | 10.70 / 15.62 ms | 3,200 → 401 |
| 16 | 27.6 | 1,085.1 | 39.3× | 614.90 / 983.81 ms | 13.65 / 20.21 ms | 6,400 → 405 |
| 32 | 16.3 | 1,730.0 | **106×** | 1,972.0 / 3,479.8 ms | 17.51 / 28.01 ms | 12,800 → 407 |

Replicated by an earlier independent run at 40 rows per writer, same host and
commit: 73.0 → 1,592.2 rows/s at 32 writers (21.8×), p50 415.0 → 16.1 ms,
1,280 → 45 generations. The shapes agree and the coalesced absolutes nearly
do (1,592 against 1,730 rows/s); the *serialized* column differs a lot (73.0
against 16.3) because that run committed a tenth as many generations into a
much smaller final store — which is the next paragraph's point, arriving as
a side effect.

Both claims, kept separate:

- **Throughput.** Coalesced throughput rises with writers (109 → 1,730
  rows/s, 16×). Serialized throughput *falls* — 164.5 → 16.3 rows/s, a 10×
  degradation — and that is not contention. It is §20 compounding: 12,800
  singleton commits publish 12,800 generations, each adding a segment that
  every later manifest must name, clone, serialize, hash and verify. The
  singleton write path makes itself slower as it runs. Coalescing to 407
  generations removes the pressure rather than parallelizing around it, which
  is why the ratio reaches 106× — most of that is the serialized path
  degrading, not the coalesced path accelerating.
- **Per-caller latency.** Stays inside a small multiple of one commit across
  a 32× increase in writers — 8.9 → 17.5 ms — where serialized latency grows
  to 1,972 ms p50 and 3,480 ms p99.

**Where it does not pay, stated plainly.** Nothing for bulk ingest, which
already batches. Nothing for a single writer — by design, since it must not
— and there it is a measured *cost*: see §22.

---

## §22 The single-writer cost of group commit

The one cell worth its own section, because it is the one that argues against
the feature. At **one** writer, coalescing has nothing to coalesce — the
measured `max_group` is 1, every time — so what it measures is pure overhead:

| one writer | serialized | coalesced | cost |
|---|---:|---:|---:|
| submit p50 | 5.67 ms | 8.86 ms | **+3.19 ms (+56%)** |
| submit p99 | 8.86 ms | 12.95 ms | +4.09 ms |
| throughput | 164.5 rows/s | 109.0 rows/s | **−34%** |

One trial, 400 samples per condition. The earlier independent 40-sample run
agrees in shape (3.44 → 6.35 ms, 143.8 → 103.5 rows/s) — which is why it was
repeated at ten times the sample count rather than reported from 40, since 40
samples could not distinguish this from noise and 400 can: the p99 of the
serialized condition (8.86 ms) is below the p50 of the coalesced one (8.86)
rather than overlapping it.

**It is a thread-handoff cost, not a durability or design one.** Nothing
extra is fsynced; the same batch is committed once either way. A bare
`queue.Queue` + `threading.Event` round trip with an idle consumer measures
6 µs, so the 3.2 ms is what the handoff costs when the consumer holds the GIL
through a multi-millisecond commit between the put and the set — the
submitter has to win the GIL back, and Python's default switch interval is
5 ms.

Two honest consequences:

- **Group commit is opt-in and should stay opt-in.** Making it the default
  write path would tax the single-writer case — which is every bulk load,
  every replay, and every one of this project's own harnesses — to help a
  case none of them run.
- **The tax is removable and was not removed.** A leader/follower
  arrangement, where the first submitter commits inline instead of handing
  to a dedicated thread, makes the one-writer path identical to the
  serialized one by construction. It is more concurrent code in the one
  session where correctness was the headline finding, so it is named here
  rather than written.

---

## §23 Index residency: the D-045 budget

D-045 deferred this decision explicitly, and said what it was waiting for:
"Dropping the TCSR costs 400 ms to rebuild; keeping it costs 18% per scan.
Which trade is right depends on the workload mix, and no workload mix has
been measured yet."

So: one path query (`paths.k`, which builds the TCSR) followed by K scans
(`series.count`, the query D-045 measured the tax on), five rounds, three
trials, one fresh process per condition, 1M events. The control is asserted
rather than trusted (lessons §13): each round checks the index is present
after the path query, and absent after dropping it, before the scans run.

| scans per path query | round p50, index kept | round p50, index dropped | path query, kept | path query, dropped | scan, kept | scan, dropped |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 55.2 | 212.4 | 12.0 | 168.7 | 43.28 | 44.31 |
| 4 | 183.1 | 329.9 | 12.0 | 161.9 | 42.71 | 41.49 |
| 16 | 674.1 | 832.7 | 11.9 | 163.0 | 41.12 | 41.65 |
| 64 | 2,660 | 2,810 | 12.4 | 166.8 | 41.00 | 40.98 |

*(medians of three trial p50s; per-trial spreads under 1.5% everywhere)*

**Both numbers D-045 was waiting on have changed, and both in the direction
that settles it.**

**The 18% tax is gone — it is now ≤2.5%, and it changes sign.** Scan latency
with the index resident against without: +2.4% at K=1, **−2.9%** at K=4,
+1.3% at K=16, −0.05% at K=64. An effect that reverses across conditions of
the same experiment is not an effect. D-045's mechanism explains why it
vanished: the tax was a working-set effect, "the scan streams tens of
megabytes per call, and the resident permutation evicts the part of it that
was staying hot". **D-047 stopped that scan streaming anything** — the event
rate now counts inside the O14 aggregation kernel and never materializes a
column. The resident permutation has nothing left to evict. The 18% was
retired by work done for a different reason, and nothing in either session
would have noticed without re-measuring.

**The rebuild is 155 ms, not 400.** Dropping the index costs the next path
query 12.0 → 166.8 ms, every time, in every condition. D-045's 400 ms
predates D-039 persisting the permutation: a rebuild is now a stamped file
read plus a gather, not two argsorts.

**Decision: keep it, and say so as a decision.** There is no crossover to
find. Keeping wins at every ratio measured — most heavily at K=1 (55 vs 212
ms per round, 3.8×) and still by 5% at K=64, where a hypothetical tax would
have had 64 scans to accumulate over. The memory case is equally weak: the
permutation is 8.16 B/row (D-039), so 8 MB at 1M and 82 MB at 10M against
794 MB of decoded segments, which is why D-041's budget was worth building
and this one is not.

What the decision is *not*:

- **It is not "residency is free forever."** It is a measurement of one index
  at one scale on today's operators. The tax was real when it was measured
  and disappeared because an unrelated query got faster; the same could
  reverse if a future scan starts materializing again.
- **It is not measured at 10M.** The working-set argument is scale-sensitive
  by construction. It was not run at 10M for the reason §14.4 gives: that
  configuration is at the memory ceiling, and this experiment holds two
  large structures resident on purpose.
- **One real residency cost is left standing and is not a budget question.**
  The persisted permutation (`index/tcsr.npz`) is stamped with a single
  `(generation, manifest_sha)`. A reader on generation *N* and a writer on
  *N+1* each find the other's stamp foreign, rebuild, and overwrite the file
  — correct, since the stamp is the gate, but it means the shared cache
  thrashes under exactly the mixed workload §19b measures. Nothing here
  depends on it (the file is disposable and saved atomically) and it is not
  fixed: a per-generation filename would fix it, and that is a separate
  change.

---

## §24 The overload protocol (added 2026-09-13)

`scripts/eval_overload.py` is the harness for a different question from
everything above: not "what does a live writer cost readers" but "what does
the service surface do once callers arrive faster than it can serve them" —
does the new backpressure (`docs/STABILITY.md` §7: `ConcurrencyGate`,
`GroupCommitWriter`'s bounded queue) actually engage, and does the system
recover cleanly once load drops back down. **This section is protocol only
— no numbers.** Per the task this harness was built under, a dev-host run is
for functional verification of the mechanism, not a reported measurement;
publishing a table from it here would misrepresent a laptop-class,
single-run number as one of this document's calibrated, multi-trial results.

**Why open-loop, not closed-loop (the same lesson §21/§22 already teach
about coalescing, applied to admission this time).** A closed-loop client —
issue a call, wait for it to finish, issue the next — can never apply more
arrival pressure than its own round trip allows. N closed-loop clients
measure whichever throughput N threads happen to sustain; the concurrency
cap under test never actually engages, because a client blocked waiting for
its previous call can never also be an arriving one. So each simulated
client schedules its own call times from a token bucket at a *target* rate,
independent of how long the previous call took — arrivals that outrun
service are exactly what should trip the gate, and open-loop is the only
protocol shape that can produce that condition on purpose.

**What "process" means in this harness, and why it differs from §19b's.**
§19b's readers are genuinely separate OS processes, because the thing under
measurement there — page-cache and fsync contention — exists between
processes. Here the thing under measurement is one shared
`ConcurrencyGate` inside one `ToolRouter`, which by construction exists
once per process; N separate client processes would each build their own
gate and measure N independent limiters rather than the one a real `tgms
serve`/`tgms webapp` enforces. So `eval_overload.py` uses threads sharing
one router instead of separate client processes — a deliberate, documented
deviation from a literal reading of "closed-loop clients (processes)",
not an oversight.

**What each step records:** aggregate throughput, p50/p95/p99 latency over
successful calls, the count and kind of refusals (concurrency-cap vs. a
result-size cap vs. an ordinary operator error), and — since this harness
runs against a **read-only** store and therefore has no write-side queue to
sample — a same-idea substitute on the read path: the 95th-percentile
number of calls the gate had admitted concurrently, and how late (versus
its own schedule) a call started once the gate or the underlying store fell
behind. A final low-rate step after the swept range confirms throughput and
latency return to the unloaded baseline rather than staying degraded —
"recovery" in the sense this section's opening question asks about.

**Output is one manifest per run**, conforming to
`benchmarks/schema/result_manifest.schema.json` (validated by
`scripts/check_result_manifest.py`) — the same schema `scripts/eval_overload.py
--dry-run` produces and validates in `tests/` — with the per-step summary
under `steps`/`recovery` and a `dev_host_note` field marking any given run's
provenance, so a manifest from a laptop is never silently mistaken for one
of this document's calibrated xzgpu numbers.

## §24 The longevity harness (2026-09-13)

P4.5 of `docs/design/M5_EXECUTION_PLAN_2026-08-27.md` §7 — "the phase's
centerpiece" — is a sustained soak, not a trial: everything above this
section runs for seconds to tens of seconds, one condition at a time, and
stops. `scripts/longevity_run.py` runs the write path, the read path, and
the artifact/freshness path together for hours (Gate G1's 24h run, the
OSDI plan's 72h Phase-4 run), because D-149's own class of bug — O(batches²)
manifest growth — was found by a production build running long, not by any
test. This section describes the protocol only; it carries no numbers,
because no number here is a reported one until the 24h/72h runs land (the
commit report's dev-host run is explicitly labelled as such, not as a
measurement of this section's claims).

**Process layout.** One **writer** process (group-commit, a seeded
append+correction stream, `compact()`+`gc(keep_last=2)` every
`--compact-every-batches` batches), N **reader** processes (`read_only=True`,
looping this document's own §14.4-shaped mix, sampled from whatever store
the run was given rather than assumed), and the orchestrating process
itself, which copies the input store, drives a restart cycle (`SIGKILL`-
equivalent: `TGMS_CRASH_POINT`, reused from `scripts/eval_durability.py`'s
own ten boundaries, at a random boundary per cycle) against the writer, and
does the final `verify()` + replay/digest-equivalence check. The artifact
checker/refresher runs as a **background thread inside the writer process**,
not a fourth OS process — see the harness's own module docstring for why:
`tgms.artifact.refresh.refresh` needs a live read-write `Store` handle, and
two OS processes each holding one on the same store directory is explicitly
undefined here (D-028; §22 above: "multi-process writers ... remain
undefined by design"). Sharing the writer's one handle across a thread is
the configuration the design *does* define (`Store._write_locked`'s own
`threading.Lock`), so that is what the checker uses.

**Metrics.** One JSONL sink (`tgms.telemetry.metrics.Metrics`,
`TGMS_METRICS_PATH`) shared by every process: ingest/commit/correction/query
rates, per-query-class and per-commit p50/p95/p99 computed by each process
over its own one-minute window (not read back from the sink's histogram,
which is cumulative-since-start by construction and therefore not a
per-minute number — see the harness's own note on this), RSS, disk bytes by
kind (segments/manifests/log/dict/registry/plans, from a filesystem walk —
there is no on-disk TCSR file to bucket, since D-045's index is resident,
not persisted), generation count, artifact checks/invalidations/refreshes
and time-to-fresh, and recovery counts and wall-clock.

**What the harness does not, and cannot, get from the public API alone:**

- **Queue depth.** `tgms.write.GroupCommitWriter` exposes `.stats()`
  (commits/submissions/max_group/solo_fallbacks) but no live queue-depth
  accessor — the plan's own metric list names this "if exposed", and it is
  not.
- **A literal 60/25/10/5 global operation mix.** The blueprint mixture
  describes proportions across the whole system; the harness controls the
  append:correction ratio inside the writer's own stream and a writer duty-
  cycle throttle, and ties artifact/freshness checks to the correction
  stream rather than to an independent rate. See the harness's module
  docstring for the full reasoning.
- **Class-E ("within-batch retirement") corrections**
  (`tgms.eval.corrections`), whose entire point is two ops landing in one
  event-log batch: every public write method (`assert_node`/`assert_edge`/
  `correct`/`retract`/`ingest_events`) is its own batch, so this class is
  skipped rather than silently applied as two batches (a different, weaker
  claim than the class is defined to test).

**The DuckDB writer-vs-reader asymmetry (P4.5's other ask).** The native
backend's whole concurrent-reader story is "lock-free reads off immutable
segments" (§19 above) — a live writer never blocks a `read_only=True`
reader. DuckDB does not have this property, and the difference is not a
performance gap but a **hard refusal**: `DuckDBAdapter` opens its file with
`duckdb.connect(path, read_only=...)`, and DuckDB's own file lock is
exclusive-or-shared at the process level — a second process cannot open the
same `store.duckdb` `read_only=True` while a first process holds it
read-write. Verified directly (two processes, one `tgms.open(..., backend=
"duckdb")`, one `tgms.open(..., backend="duckdb", read_only=True)` a moment
later): the second raises `duckdb.IOException: ... Conflicting lock is held
in <pid> ...` and never opens. So the entire premise of this document's
mixed/readers/residency modes — N reader processes concurrent with a live
writer — is native-only; on DuckDB there is no concurrent-reader
configuration to measure, only a queue of processes each waiting for the
lock. This was previously undocumented (the survey behind P4.5 found no
entry for it); it is now recorded here rather than in STABILITY.md, since
every other concurrency claim in this document already lives here and the
asymmetry is a fact about *this document's* scope, not a top-level
stability guarantee.

**A local disk-safety addendum (2026-09-14), after a local run of the
above filled its host's disk.** The harness's own module docstring
(`scripts/longevity_run.py`) carries the full account; this is the
qualitative summary, deliberately without the numbers the docstring backs
with a small, direct measurement. Two things contribute, and the harness
now guards against both rather than betting on one diagnosis: the final
replay/digest-equivalence step re-applies every logged batch as its own
uncompacted generation, which is D-149's own O(batches²) manifest-growth
pathology named earlier in this document's history — a mandatory step in
every run, not only a long one, and now sized up (from the real batch
count) before it is paid for rather than after. Separately, and only
plausibly rather than confirmed locally (see the docstring for why this
harness cannot safely reproduce it on this host), this section's own
"three more races" above already documents that the native engine's
generation-pin table is in-process only — a reader running as its own OS
process, as this harness's design calls for, is invisible to a writer
process's `gc()`, and POSIX does not reclaim a file's blocks on `unlink()`
while another process still holds it open. Neither story requires an engine
change to guard against from the harness side (`tgms/` stays public-API-
only): a wall-clock floor between compactions on top of the existing
batch-count trigger, a periodic reader handle refresh so no reader process
holds a stale generation's files open indefinitely, and — underneath both,
independent of which story is right — a disk-usage ceiling checked
periodically through the whole run (not only before the risky step) that
aborts cleanly, with a ledger entry, the moment the run's own output
directory crosses it.

The coordinator's own xzgpu launch line for the real (non-dev-host,
non-reported-until-it-lands) 24h run, one PI ruling later than the rest of
this section — everything under `--out` and everything the run itself
writes stays on xzgpu's own project storage, never this repository's
working tree:

    nohup python scripts/longevity_run.py \
        --store stores/synth-1m-native --duration 24h --mix balanced \
        --readers 8 --compact-every-batches 500 --compact-min-interval-s 5 \
        --reader-reopen-every-s 300 --restart-every 30m --artifacts 20 \
        --seed 0 --out /mnt/project/xzhang/tgms/longevity/<date> \
        --metrics /mnt/project/xzhang/tgms/longevity/<date>/metrics.jsonl \
        --max-disk-mb 512000 \
        > /mnt/project/xzhang/tgms/longevity/<date>/orchestrator.log 2>&1 &

---

## Honest limits

- Everything here is one host and one storage stack. The commit floor is
  dominated by fsync latency, which is the single most hardware-dependent
  number in this document: the same profile on macOS/APFS reads 34.9 ms
  where xzgpu reads 5.76.
- Group commit is measured with threads in one process, because that is what
  the single-writer contract permits. It says nothing about multi-process
  writers, which are refused rather than merely undefined as of 2026-09-13
  (`Store.__init__`'s `fcntl.flock` writer lock, `docs/STABILITY.md` §7) —
  a second writer process now fails fast at open instead of racing
  `_recover`/`_write` against the first, but no measurement of *that*
  exclusion's cost exists here (it is refused, not raced, so there is
  nothing to time).
- The mixed measurement uses a writer committing 100-row batches as fast as
  it can. That is an upper bound on writer interference, not a duty cycle any
  real workload runs.
- Reader interference is measured at 1M, not 10M, and deliberately: §14.4
  found the host OOM killer taking 2 of 16 readers at 10M (~4 GB per reader
  on a 93 GB host). Adding a writer to a configuration already at the memory
  ceiling measures the OOM killer, not concurrency.

---

## 2026-09-13 — three more races, and a real one closed

Everything above is the native engine's own concurrency contract: segments,
manifests, readers. `tgms/artifact/` sits beside it as a second piece of
on-disk state (`artifacts.jsonl`, the registry) with its own concurrency
questions, and the OSDI27 fault-matrix design memo (§7) named three of them
as untested. This section records what each test found, not numbers — the
point of these tests is which of a small number of qualitative outcomes
happens, never a rate or a latency.

**`Registry.append` was an unlocked check-then-act, and it was wrong.**
`register()`/`append()` used to read the current generation from the
registry's own in-memory fold, decide the next generation from it, and only
then write — with no lock protecting that read-decide-write sequence from a
second process doing the same thing on the same `artifacts.jsonl`. Two
processes racing to append the next generation of one artifact name could
both read the same "current" generation, both pass the check, and both
append — leaving two records on disk claiming the same generation, a
corruption the loader only ever caught later, on the *next* fresh
`Registry(path)` open, as a chain-consecutiveness error. Fixed with an
OS-level exclusive lock (`fcntl.flock`) on a sidecar `artifacts.jsonl.lock`
— never the registry file itself, so a reader never contends with a writer —
held across the whole check-then-act: a re-read of whatever another process
appended since this object's own fold last looked, the generation check, the
append and its `fsync`, and the in-memory index update. A post-append
re-read stays in as belt-and-braces alongside the lock, verifying on disk
that nothing landed where this process's own write believed it was landing.
A losing writer now raises immediately, in its own `append()` call, instead
of silently writing a corrupt second generation.

**Query vs. cross-process compaction/gc: no cross-process contract, and the
engine does not pretend to have one.** The native engine's generation-pin
table (`gc.rs`) is explicit that it is in-process only — "no cross-process
reader registry, deliberately" — so a reader in one process pinning a
generation is not visible to `gc()` running in another. The new test
exercises exactly that: a reader repeatedly answers a fixed set of questions
while a second process commits more batches and runs `compact()` +
`gc(keep_last=2)` underneath it. The property that must hold either way is
not "gc leaves the reader's generation alone" — nothing promises that
cross-process — it is that gc can never make the reader silently *wrong*.
The two acceptable outcomes are answer-stable (the reader's already-open
file handles keep answering the same thing regardless of what gc collects
on disk, since POSIX does not invalidate a handle a process still holds
open) and loud failure (an exception naming what went missing). The test
records whichever actually happens rather than asserting one; see the E2
task report for which one this host produced.

**A correction landing mid-refresh does not make `refresh()` claim
freshness it should not.** There is no natural I/O gap inside `refresh()`
between its read (`run_plan`, capturing the fresh execution's envelope) and
its publish (`registry.register`, inside `_publish`) for a correction to
race into, so the new test pauses that exact window from the test side —
monkeypatching `tgms.artifact.refresh.run_plan` in the refresher process
only, `refresh.py` itself untouched — and lands a correction from a second,
separate process during the pause. Checking the published generation
against the post-correction log reports `POSSIBLY_STALE` with a witness
naming the correction, never `FRESH`: the published basis is honest about
what it actually read, under the race, not only in the sequential case
`tests/test_artifact_refresh.py` already covers.

Also added: a one-process test for `tgms/storage/eventlog.py`'s
non-monotonic-`tt` refusal in `replay()` (previously untested — `append()`
itself does not enforce ordering, so a log that somehow acquires two batches
out of `tt` order must be refused at replay, not silently applied as if time
ran backwards), and a `pace_s`-paced variant of the existing pinned-reader
test that stretches the same one-generation-per-handle property over many
more writer-committed generations instead of twelve.
