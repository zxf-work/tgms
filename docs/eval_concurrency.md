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

**Neither published attribution survived, in a way neither document could
have predicted.** The measurement that mattered was taken *before* this
table, on the same code: at batch=1 the total was 90.6 ms, of which
`apply_ops` was 59.9 — an existence probe materializing the whole node store
to answer a question about two uids (`engine_lessons.md` §16). That is fixed,
and the table above is the world after it.

*Now* §7 is right and `eval_writes.md` is not:

- The engine commit is **5.07 of 5.76 ms, 88%**, at batch=1 — it is the
  durable generation, as §7 says.
- The manifest is **1.17 ms of it**, and stays 1.17 ms while the manifest
  itself grows from 18 KB to 286 KB across the run (15.9×). Manifest *size*
  is a space cost, not a time one; four fsynced file writes are the time.
- What does grow: total write 3.92 → 7.46 ms first decile to last (1.90×) at
  batch=1. Real, and much smaller than a 15.9× manifest would produce.

The `eval_writes.md` claim that **64 KB of store growth per correction is
almost all manifest** is unaffected — that is a space measurement, and it
stands.

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

100k-row store, 40 rows per writer, single trial:

| writers | serialized rows/s | coalesced rows/s | speedup | serialized p50 | coalesced p50 | generations |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 143.8 | 103.5 | 0.72× | 3.44 ms | 6.35 ms | 40 → 40 |
| 2 | 153.2 | 197.6 | 1.29× | 9.60 ms | 6.65 ms | 80 → 40 |
| 4 | 158.1 | 370.1 | 2.34× | 22.12 ms | 7.37 ms | 160 → 40 |
| 8 | 134.4 | 651.2 | 4.85× | 56.91 ms | 8.95 ms | 320 → 41 |
| 16 | 99.6 | 1,078.1 | 10.8× | 155.21 ms | 11.35 ms | 640 → 42 |
| 32 | 73.0 | 1,592.2 | 21.8× | 414.98 ms | 16.08 ms | 1,280 → 45 |

Both claims, kept separate:

- **Throughput** scales with writers instead of being flat. Serialized
  throughput *falls* past 4 writers; coalesced rises to 21.8× at 32.
- **Per-caller latency** stays inside one commit's cost — 3.4 → 16.1 ms
  across a 32× increase in writers — where serialized latency grows linearly
  to 415 ms.

**Where it does not pay, stated plainly.** It buys nothing for bulk ingest,
which already batches, and nothing for a single writer — by design, since it
must not. At one writer it is a measured *cost*, and §22 says how big.

---

## §22 The single-writer cost of group commit

*(Section filled from `conc-groupcommit-t{1,2,3}.json`; the single-trial
40-sample cell above is not enough to call it.)*

---

## §23 Index residency: the D-045 budget

*(Section filled from `conc-residency-1m.json`.)*

---

## Honest limits

- Everything here is one host and one storage stack. The commit floor is
  dominated by fsync latency, which is the single most hardware-dependent
  number in this document: the same profile on macOS/APFS reads 34.9 ms
  where xzgpu reads 5.76.
- Group commit is measured with threads in one process, because that is what
  the single-writer contract permits. It says nothing about multi-process
  writers, which remain undefined by design (D-028).
- The mixed measurement uses a writer committing 100-row batches as fast as
  it can. That is an upper bound on writer interference, not a duty cycle any
  real workload runs.
- Reader interference is measured at 1M, not 10M, and deliberately: §14.4
  found the host OOM killer taking 2 of 16 readers at 10M (~4 GB per reader
  on a 93 GB host). Adding a writer to a configuration already at the memory
  ceiling measures the OOM killer, not concurrency.
