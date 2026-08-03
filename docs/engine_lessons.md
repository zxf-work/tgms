# Lessons from building a specialized storage engine

Notes from replacing DuckDB and Kùzu with a purpose-built bi-temporal engine
for TGMS. Written as the work happened, with the numbers that produced each
conclusion, including the ones that contradicted what we expected.

Context: ~7,500 lines of Rust behind an unchanged Python `StorageAdapter`
ABC, validated against a pre-existing 500-case brute-force operator oracle.
Nothing here is novel database research. It is the set of things that
actually cost time.

---

## 1. Measure the layer before optimizing it

Seventeen times we identified "the bottleneck" and were wrong. Every single
time, the fix that mattered was found by measuring *after* the intended fix
failed to move the number. The last row is the one where the wrong belief was
*ours, about our own fix*, and the thing that caught it was the routine
re-run of a published table.

| we believed | we measured | actual cause |
|---|---|---|
| Rust scan will beat NumPy | 2–5× **slower** | per-row metadata lookups in the loop |
| point lookups are quadratic | per-op cost flat as N grew | per-commit fsync, not scanning |
| `co_active`'s loop is the cost | `meets` cost 443 ms for 500 rows | unwindowed scan, not the join |
| incidence filter needs an index | filter answered in 0.8 ms | `stats()` scanning on every operator call |
| materialization pushes row-by-row | flat 170 ns/row at any segment count | `vid` hex built for unprojected rows |
| `diff_snapshots` is scan-bound | the two scans were 54 ms of 419 | a 16-row lookup rebuilding the whole store |
| a point lookup is all storage | arg validation ≈ the lookup, ~2 ms each | `jsonschema.validate` rebuilding a validator per call |
| the motif kernel is the motif cost | the kernel was 11 ms of 369 | an unprojected scan hashing `eid` for rows the filter dropped |
| 10M scans are materialize-bound | parallel materialize moved 811 → 819 ms | the fast path never ran: one overlapping correction segment voids the all-or-nothing disjointness check |
| the 6 GB working-set floor is the segment cache | budgeting the cache still OOM'd at 2 GB; the cache was 794 MB | the stats warm-up materialized all 10M rows as one transient — fixed, suite now runs in 1.76 GB |
| the 1M scan regression is the recalibrated parallel gate | forcing the parallel path on at 1M moved nothing (83.1 vs 84.4 ms) | an index built by an *earlier query in the same process* — resident TCSR, 18% on every later scan (§9g) |
| the 10M scan is selection-bound (belief test, valid-time test, close index per row) | selection is 39 ms of a 349 ms scan, and removing the per-row `vt_e` test saves 1 ms | materialization (47%) and the NumPy boundary (16%) — the cost is *moving* the rows, not deciding on them (§13) |
| projecting to one column changing nothing proves materialization is not the cost | one column and four measured the same, 360 vs 352 ms | the projection was never applied to the fixed-width columns; once it was, the same scan fell to 143 ms |
| exact distinct counting over 10M ids is inherently expensive — it is what ClickHouse's `uniqExact` costs too | the same answer in 9.6 ms instead of 240.7 | the sort was not the algorithm, it was a *representation*: dense ids make a per-group bitset a popcount, and the sort had also been placed in the one serial stage |
| the boundary's `u32 → i64` widening is 77 ms of recoverable cost | moving the cast to NumPy recovered 20 of 73 ms; the other 53 stayed | the cost is not the conversion, it is writing 80 MB of int64 per column — whoever does it pays it, and only *not building the column* removes it |
| the singleton-write floor is the durable generation's fsyncs (§7) — or the full manifest each commit rewrites (`eval_writes.md`) | the fsyncs were 30.4 ms and the manifest write 8.5 of a 90.6 ms one-row write; the Python semantics layer was 59.9 | a two-uid existence probe that materialized every node version in the store. 90.6 → 34.9 ms, after which §7 is right about the floor and `eval_writes` about the growth (§16) |
| that 90.6 → 34.9 ms is the singleton-write floor, full stop | the published single-row append did not move at all: 96 → 95 ev/s | the probe's cost scales with *entities*, and the benchmark's generator makes 1,000 of them where ours made 200,000 — ~0.6 ms against 157 (§16) |
| readers-only concurrency proves the reader story: 16 readers, no interference | with one *writer* running, the writer crashed on its second batch and two of three readers with it | opening a store performed crash recovery, so every reader was a second writer — and `open` truncated a live writer's dictionary tail (§17) |

Three of the fixes we implemented were *correct but irrelevant* — the
contiguous-run copy, the postings index, and the parallel materialization
all stayed, because they are cheap and right, but none produced the win
attributed to them. The eleventh entry produced no fix at all: the
hypothesis was tested with an environment override before any default was
changed, which cost one run and saved a wrong recalibration. The thirteenth
is the worst of the set, because it is the only one where a *measurement*
rather than a guess pointed the wrong way for three sessions: the experiment
was right, the instrument was broken, and nothing in the number said so.
The last entry is the only one where the *correct* course of action was to
stop: the fix worked, it was worth a quarter of what the price tag said,
and the remaining three quarters would have cost a dtype contract shared by
three backends. A priced item is a hypothesis about how much of the price
is recoverable, and that hypothesis deserves testing before the invoice is
paid.

The concrete discipline that worked: after implementing a fix, re-measure
before writing the commit message. If the number did not move, the
hypothesis was wrong even though the code was better.

A useful sharpening question: *does this cost scale with the thing I blame?*
A cost that stays flat at 170 ns/row whether the scan touches one segment or
twenty cannot be the merge or the copy — both scale with segment count. That
single observation located the real cause in one step.

## 2. Writing a kernel in Rust does not make it fast; loop shape does

The first native scan was **2–5× slower than a fairly written NumPy
equivalent**. The predicate was fine. The loop was not:

- a column resolved *by name* (string comparison) on every row;
- the run-length `tt_s` table walked per row;
- a `Vec::contains` for the allowed relation codes per row.

Hoisting all of it per segment, and replacing a per-row binary search with a
bitset, gave 27.5 ms → 0.68 ms at 10M rows — from 5× slower than NumPy to
17× faster. Same language, same algorithm, same data.

The corollary is that "we rewrote it in Rust" is not a performance claim. If
a port is not clearly faster than the vectorized interpreter it replaced,
the loop is doing per-row work that belongs outside it.

## 3. Per-row work at a boundary is the recurring killer

The same defect appeared in five unrelated places, each time as a small
convenience:

- `vt_e_at(row)` re-resolved its column by name — inside the scan loop;
- `edge_desc(i)` called `uids_for` — one dictionary crossing per result row;
- the motif operator did the same;
- `stats()` walked the store — on every operator call, via `dataset_extent`;
- `vid` was formatted as a 24-character hex string per row even when the
  caller had projected it away (172.8 ms → 43.6 ms for a 1M-row scan once
  gated).

None of these is visible in a code review; each reads as a tidy helper. They
are only visible in a profile or in a number that refuses to improve. Where a
boundary exists — language, process, or module — count the crossings per
call, not per operation.

## 4. Projection must be pushed down, not filtered on the way out

`columns=` was honoured at the adapter: the engine built every column and
Python discarded the unwanted ones. The API looked like a projection and was
really a filter. Pushing it into materialization took a 1M-row integer scan
from 130 ms to 32 ms.

The trap that follows: **derived columns have dependencies**. `eid` is a hash
of `(src, dst, rel_type, disc)`, so asking for `eid` must force `rel_type` and
`disc` to be materialized even though the caller never sees them. Getting this
wrong surfaced as an index panic crossing into Python — the worst failure
shape at that boundary. A projection layer needs a dependency graph, however
small, and should error rather than index blindly.

## 5. Caching is not the same as incremental maintenance

`stats()` was made per-generation cacheable, which made repeat reads free.
But a write changes the generation, so a write-then-read loop still paid a
full scan per iteration — the exact shape of an ingest-then-query workload.

Moving the accumulator into the engine and folding each batch in at commit
made it genuinely incremental. A write-then-read cycle then cost 26.3 ms,
which is precisely the commit fsync floor: statistics contribute nothing.

If a cached value is invalidated by the operation that runs next, it is not
a cache. It is a rescan with extra steps.

## 6. Immutability is what buys everything else

Segments are written once and never modified. The only mutation in the whole
model — closing a version's transaction time — is recorded as an append-only
patch, folded into the owning segment only by compaction.

That single property is what makes the following *free* rather than
engineered: snapshot isolation for readers (pin a manifest generation),
safe `mmap` (nothing can change underneath a mapping), verify-once-per-session
checksums (a verified segment stays verified), and lock-free concurrent
readers.

The design mistake we made and had to fix: an early draft kept a store-wide
mutable set of closes. It would have let a reader holding generation *N*
observe visibility from *N+1* — a coherent-snapshot violation that no test
would have caught until it produced a wrong answer under concurrency. Every
structure affecting visibility must be scoped to the generation, not just the
data.

## 7. Know what your durability actually costs, then do not weaken it

Per-row writes cost ~26 ms each. That looked alarming until we isolated it:

| 400 rows | total | per row |
|---|---|---|
| in **1** batch | 39.9 ms | 0.100 ms |
| in **400** batches | 10,575 ms | 26.4 ms |

A **265× difference** for identical data. The cost is per-*commit*, not
per-row: each batch publishes a durable generation, which is several fsyncs.
Real ingest paths batch, which is why bulk replay beats the general engine
3× while single-row writes lose.

The temptation is to relax fsync. That would trade away the one guarantee the
engine exists to make. The right lever is group-commit at the layer that
decides what a batch *is* — not weakening what a batch *means*.

**Amended, and this is the interesting part.** The conclusion above was right
about what to do and wrong about why, and nobody noticed for four months
because the recommendation it produced was correct anyway. When the commit was
finally instrumented per phase, a one-row write was 90.6 ms of which the
fsyncs were 30.4 — the other 59.9 was an existence probe on the *read* path
that materialized the whole node store to answer a question about two uids
(§16). Only after fixing that is "several fsyncs" the floor: 33.8 ms of 34.9.

Two things generalise. First, **a correct recommendation is not evidence for
the reasoning that produced it** — "batch your writes" was going to be the
advice whatever the profile said, so the advice could never falsify the
attribution. Second, the attribution had a *competing* published version the
whole time: `eval_writes.md` blamed the full manifest each commit rewrites,
this file blamed the fsyncs, and neither had been measured against the other.
Two documents disagreeing about a cause is a measurement waiting to be taken,
not a matter of emphasis.

## 8. The oracle is what makes a rewrite tractable

The single highest-leverage asset was pre-existing: a 500-case brute-force
operator oracle, metamorphic tests, and byte-identical replay digests, all
human-owned and off-limits to the implementer.

That let the entire backend be swapped and validated by running the existing
suite **unmodified** against it — one environment variable, zero assertions
changed. It also caught things a code review would not: an instance-ordering
bug in the motif kernel that passed the *count* oracle and failed the
*instance* oracle, because instances must be ordered by `(t, eid)` while the
scan returns `(vt_s, vid)`.

If you are planning a storage rewrite and do not have an independent
reference implementation, build that first. It is cheaper than the rewrite
and it is what makes the rewrite finishable.

## 9. Differential testing has to replay, not re-run

Comparing the two backends by building each from the same inputs reported
**DIFFER on every seed** — and the test was wrong, not the engine.
Transaction times come from a clock at write time, so two independently
built stores of the same data legitimately differ.

The valid comparison replays *one* recorded event log into both. Obvious in
retrospect; it cost a confused half hour and would have cost far more if the
"failure" had been believed.

## 9a. A benchmark can agree perfectly and mean nothing

Adding a PostgreSQL baseline meant reimplementing operators in SQL and
checking them against the canonical result hash. Six of them matched
byte-for-byte on the first run. That should have been the good news; it was
the warning.

Three of the six matched on **empty answers**. Reading the shapes rather than
the verdicts turned up four separate defects in the synthetic dataset the
harness had been generating all along:

- **Endpoints were `src = i mod |V|`, `dst = 7i + 3 mod |V|`.** `dst` is a
  function of `src`, so the "random graph" was a single deterministic cycle —
  every edge out of `n1` went to `n10` and nowhere else. Neighbourhood
  evolution reported zero neighbours gained and zero lost at every scale
  because there was genuinely nothing to find.
- **Edge lifetime was a constant 40 ticks** while one edge started per tick,
  so ~40 edges were valid at any instant no matter the scale. Every instant
  operator was answering over an empty graph, and *more data made it emptier*.
- **The belief probe used `as_of_tt = 1`.** Transaction times are epoch
  microseconds, so that literal predates the entire store; the one query whose
  job was to catch a system ignoring the clock returned nothing, always. Worse,
  the generator wrote no corrections at all, so no `as_of_tt` could have
  discriminated anything.
- **The results table printed `0` for every operator without a `rows` key.** A
  difference of 999 added and 999 removed edges displayed as zero.

Each defect independently made "the systems agree" vacuous, and together they
had been reported as a clean pass. Two more surfaced only after fixing those:
a uniform random graph has almost no triangles, so the motif operator was
compared on a count of zero until the generator grew community structure; and
a node filter sized as a fraction of `|V|` answered at 20k events and tripped
the cost guardrail at 200k, so that row measured the guardrail instead.

The transferable part: **agreement is evidence only if the answer was hard to
agree on.** An equality check over an empty set passes for free. Assert on the
shape of what you compared — non-zero rows, corrections actually present,
truncation actually exercised — or the suite will keep reporting a pass it did
not earn. The oracle (§8) is what makes a rewrite tractable, but an oracle only
tests the inputs you hand it.

## 9c. A cheap primitive can be the whole operator

`diff_snapshots` measured 419 ms against a PostgreSQL baseline's 104 and
DuckDB's 111 — the only operator where the native engine lost to both. The
obvious suspect was the scan: `edges_at` requests no column projection, so it
materializes `eid` — a sha256 per row — for every edge valid at each of two
instants.

That suspicion was right about the waste and wrong about the magnitude. The
two scans together are 54 ms. The other 365 ms was `props_for_vids`, fetching
props for the *sixteen* candidate identities whose version differed between
the instants. It routed through `all_*_versions`, which rebuilds every row in
the store — two dictionary lookups, several string allocations, and a sha256
for `eid` — to find those sixteen. Two measurements settled it in a minute:
the cost was identical for one vid and for 256, and it doubled with the store
(86 ms at 50k versions, 353 ms at 200k). Flat in the query, linear in the
data: that is a full scan wearing a lookup's signature.

Sweeping three integer columns per segment and reading a string only on a
match took it to 6.9 ms, and the operator to 75.6 ms — from last place to
first. The complexity did not change; it is still O(rows), because vids are
hashes and no segment ordering can help. Only the constant changed.

Two things generalize. **A lookup-shaped API can hide a scan**, and the way to
find out costs nothing: vary the query size and vary the data size, and see
which one moves the clock. **And a primitive that looks too small to matter
can be the entire operator** — nobody profiles a function that fetches sixteen
rows, which is exactly why it went unnoticed until an outside baseline made
the operator look wrong.

## 9b. Batch writes and single writes are different systems

The same investigation put the first corrections into the generated data, and
load time went from fractions of a second to twelve. Splitting it: bulk
ingestion of 20,000 events takes **0.27 s**, while 200 single-op corrections
take **9.0 s — about 45 ms each**, in two roughly equal halves. Half is the
commit itself (segment write, dictionary append, manifest swap, each fsynced —
§7, and the cost is real durability rather than waste). The other half was the
identity lookup that finds the version being corrected — which turned out to
be a read-path defect, not a write cost at all, and is fixed in §9d. Loading
200k events fell from 24.1 s to 5.3 s once it was.

Neither half had ever been measured, because every benchmark to date wrote in
bulk and never corrected anything. An append-only engine with an atomic
manifest swap is optimized for batches by construction, and a workload of many
tiny commits meets none of those assumptions. If the write path has a batched
mode, benchmark the unbatched one too — it is a different system, and users
will find it.

## 9d. Count the envelope, not just the engine

An outside baseline answered a two-row point lookup in 0.5 ms against the
engine's 4.2 ms. Nothing in the TGMS-only comparison had ever hinted at a
problem — both TGMS backends were slow in the same way, so they agreed, and
agreement reads as health.

Two causes, roughly equal, neither in the storage layout.

The first was §9c's defect again, in a second place: the postings index
returned exact `(file, row)` pairs and the read path rebuilt the whole segment
to index into it. Same tell, too — `believed_edge_versions` cost 76 ms at 50k
versions and 76 ms at 200k, flat, because a segment has a fixed maximum size.
Flat-in-the-data is as diagnostic as linear-in-the-data: it says the unit of
work is a segment, not a row and not the store. Fixing it: 76 ms → 1.01 ms.

The second was not in the engine at all. `jsonschema.validate` is a
convenience wrapper that re-checks the schema and builds a fresh validator on
every call, re-resolving `$ref`s through `urljoin` as it goes — about 2 ms per
operator call, against a 2 ms lookup. **Validating the arguments cost as much
as answering the query.** Compiling the validator once per operator removed
it, and every operator in the suite got faster, because they all pay it.

Two things worth carrying. **A per-call envelope is invisible in a
storage-layer profile** and is only exposed by a query small enough that the
answer is cheap — which is exactly the query class a storage benchmark tends
not to include. And **a read-path fix moved the write path 4.5×** (200k-event
load, 24.1 s → 5.3 s), because writes read: every correction must first find
the version it corrects. Read and write paths are not separable when the write
is a correction.

## 9e. Filter before you derive, not after

`count_temporal_motifs` was the slowest operator in the suite at 369 ms, and
the obvious place to look was the δ-motif kernel — a three-way join with
ordering and span predicates, the only genuinely algorithmic thing in the
operator. **It was 11.4 ms.** Ninety-six percent of the time was the call that
fetched the events, and it was the same two mistakes the engine had already
been taught elsewhere:

- **No projection.** The scan built props, vid, vt_e, source and provenance
  for every row in the window; the operator reads five columns. That is
  lesson §4 recurring at a call site rather than in the API.
- **Filter after derive.** The node filter ran in NumPy on the returned
  arrays, so `eid` — a sha256 per row — had been computed for all 200,009
  window rows before 93% of them were thrown away.

The second is the interesting one, because the filter *could not* be pushed
down as written: a motif event needs both endpoints in the set, and the scan
signature only offered or-incidence. The first move was to notice that
`{both} ⊆ {either}`, push the weaker filter down as an exact pre-filter, and
keep the exact test above it: **369 → 52.6 ms**, with the kernel untouched.

The weaker pushdown was worth having on its own, but it still derived `eid`
for every or-match — 25k rows to keep 14.5k — so the scan then learned the
and-form outright (`touching_both`), taking the operator to **40.5 ms**. Worth
noting what that cost: a new field on the scan request, a branch in the row
test, and the same keyword threaded through four adapters so the backends stay
interchangeable. Widening a closed scan signature is cheap exactly once per
predicate, and the discipline that keeps it cheap is that every backend must
implement it — which is also what stops the "optimization" from being a
native-only special case.

Two things to carry. **When a column is expensive to derive, the filter that
discards it has to run first** — and if the pushdown you want is not
expressible, a *weaker* pushdown that is a superset is usually available and
costs nothing in correctness. And **the named, algorithmic-looking component
is rarely the cost**; it is the one everybody has already thought about. The
scan call around it is the one nobody has.

A pleasant side effect worth designing for: the work lived in the shared
operator layer and the scan ABC, so the DuckDB backend got it too (576.8 →
69.5 ms). A speedup that comes from removing work rather than from
specializing one backend improves the baseline you are being compared against,
which is the only kind that is unambiguously real.

One more layer sat between the operator and the kernel. `_match` measured
11.0 ms, and it was tempting to read that as "the kernel". It was not: 4.4 ms
of it was the three int64 columns being converted *twice* on the way in —
first by a Python list comprehension building boxed ints, then by PyO3 walking
that list to build a `Vec<i64>`. Passing them as NumPy buffers and borrowing
the slices in Rust — which is what the kernel's `Events` already wanted, since
it takes `&[i64]` — halved it to 5.5 ms and took the operator to 32.0.

So the full arc is 368.9 → 32.0 ms with the matching algorithm never once
touched, and even the "11 ms kernel" turning out to be half boundary.

Splitting the remainder took a throwaway probe — a `#[pyfunction]` with the
identical signature that extracts its arguments and returns — built, measured,
and deleted without being committed. Full call 5.31 ms; extraction 0.94 ms;
the same probe with `eid=[]` **0.00 ms**; matching therefore ~4.37 ms.

Two conclusions, and the second is the point. The NumPy buffers are genuinely
free — three int64 columns cross at no measurable cost. And the obvious next
step is **not worth taking**: `eid` is the one column that cannot be borrowed,
and the scan does hold it as a 96-bit id before formatting it to hex, so
passing `(hi, lo)` integer arrays is a real and correct option — worth at most
0.94 ms of a 32 ms operator, 3%, against changing the scan's output contract
and the kernel's comparison logic.

The measurement that stops you is worth as much as the one that redirects you.
Four of the five optimizations in this section were found by profiling; this
one was *declined* by profiling, and an hour of plausible work went unspent
because a fifteen-minute probe put a number on it.

## 9g. Queries measured in one process are not independent

A routine check before publishing a new table: does the rest of the table
still say what it said? It did not. `series.count` at 1M read 84 ms against
a published 59.0, on the same dataset, with a byte-identical answer.

Bisecting the engine commits in between (five builds, one 1M run each, ~15
minutes total) put the step at the commit that persists the TCSR
permutation — a commit that touches three Python files, none of them on a
scan path. The suspicious part was that it *shouldn't* matter, so the next
step was a probe rather than a patch: time the same operator in one process
before and after building the index.

| condition | series.count |
|---|---:|
| nothing resident | 68.4 ms |
| + 1M rows of plain int columns held | 71.7 ms |
| + the TCSR's own columns held | 71.1 ms |
| **+ the built CSR held too** | **80.7 ms** |
| everything released again | 68.3 ms |

The index costs 18% to *have*, not to build, on a query that never touches
it — and hands it straight back when dropped. `diff.global`, measured in the
same process, does not move at all. It is a working-set effect: the scan
streams tens of megabytes per call, and the resident permutation evicts the
part of it that was staying hot.

Two consequences, and the first is about measurement rather than engines.
**The registry runs thirteen queries in one process, in order, and `paths.k`
comes before `series.count`** — so the published aggregation numbers had
been quietly paying for the traversal index of the query that ran before
them, on every run, for as long as the table has existed. Native is also the
only system in that table that builds such an index, so the tax is
one-sided. None of it was wrong, and no conclusion moved; it was simply
undisclosed, and it is the kind of thing a reader has a right to know when a
column is compared against another system's.

The second is a product observation. An agent process is exactly the
long-lived process this describes: run one path query, keep the index, pay
18% on every scan afterwards. That is a defensible trade — the index saves
400 ms on the query that built it — but it should be a *decision* with a
budget behind it, the way segment residency became one in D-041, rather than
a cache that is born immortal.

The general lesson is the one this file keeps relearning in new costumes:
**a number is a property of a process, not of a query.** Ours had four
different meanings depending on what had run before it, on what day, and
with what resident. Anything published to the tenth of a millisecond should
be able to survive being re-measured; ours survives to about ±20%, which is
now written next to it.

## 9f. A capability tag is a hypothesis, not a measurement

The 110-question study (D-026) was the most useful thing we did for planning:
questions written by people who had never seen the operator list, each
inexpressible one tagged with the capability it wanted. Grouped aggregation
led by a mile — 76 questions touched it, and **30 were blocked by it alone**.
We published that number, and the sentence it justified: one operator family
would make thirty more questions expressible. It was the whole argument for
building `aggregate_events` first.

We built it, then re-audited all 110 against the fourteen-operator algebra
without touching the pre-registered table. **Fourteen questions moved**, not
thirty. Thirteen of the thirty, plus one from elsewhere.

The seventeen that stayed are the lesson. With grouping in hand, what they
actually need became visible:

- **eleven want a set or pair join.** "How many distinct pairs (A, B) where A
  rated B *and* B rated A?" groups by pair perfectly well — and then has to
  match each pair against its own transpose.
- **ten want per-group ordered sequences.** "The longest gap between two
  consecutive ratings by the same account"; "more than 5 ratings in any
  24-hour period". `min`/`max` give the endpoints of a group, never the
  structure inside it, and a sliding window is not a bucket.

Those two counts overlap — **four questions want both**, so the seventeen
partition as 7 set-join only, 6 ordered-sequence only, and 4 wanting each.
(A few also hit rating properties or division, which D-044 declined on
purpose; no question is left needing only those.) Tag counts are
occurrences, not a partition, which is the same reading error the original
histogram invited and this section is about.

None of that was hidden. It was folded into a tag named `G`, applied by
people who could see what was missing but not what was underneath it,
because the thing that would reveal the second layer did not exist yet. A
tag assigned before the capability exists records *the first blocker a
reader hits*, and the first blocker is the shallowest one.

Two things to carry. **A tag histogram is a prioritization tool, not a
forecast** — it ranks what to build next, which it did correctly here (no
other capability would have moved fourteen), but the number attached to it
is an upper bound that will not be met. And the deflation is largely
avoidable: **make every tag name the operator call that would satisfy it.**
The tags that cannot name one — "needs grouped aggregation" for a
reciprocity question — are the ones concealing a second capability. Both new
tags this re-audit had to introduce (`SET` grown from 7 to 36, `SEQ` from
nothing to 14) came out of entries that could not have named a call.

The re-audit is kept beside the pre-registration rather than replacing it
(`C` and `C14` in `scripts/independent_questions.py`), because a prediction
is only worth publishing if the miss is still visible afterwards.

## 10. Fault injection earns its keep in ways you did not plan

A 14-case corruption matrix was written to prove that a damaged store never
silently returns wrong data. It immediately caught something else: the read
path opened segments with checksum verification *disabled*, so a flipped byte
would have flowed into query results. Detection existed; nothing called it.

Later, an over-eager edit deleted a method, and the same matrix caught that
too. Tests written for one failure mode routinely catch a different one —
which is an argument for writing them even when the property feels obviously
true.

## 11. Derived data is a real space win, with a real invariant attached

Identities (`eid`, `vid`) are pure functions of other stored fields, so they
are recomputed rather than stored. Combined with dictionary-encoded strings
and elided uniform columns, a row costs ~58 bytes uncompressed against ~260
in the general engine — roughly 0.22×, before any codec.

The invariant that makes it sound must be explicit: every derivation input
must remain recoverable. We allow eliding a discriminator column *only* when
the segment header declares the reversible encoding that regenerates it.
Without that rule written down, a later optimization would quietly make
identities unrecoverable.

And measure the cost honestly: deriving `eid` is a SHA per row — about 85 ms
per million. That is fine at a `limit`-bounded API boundary and unacceptable
inside a scan, which is exactly why projection pushdown mattered.

## 13. A half-applied optimization is worse than none, because it lies

`columns=` was pushed into materialization (§4) and it worked: a 1M integer
scan went 130 → 32 ms. What nobody re-read afterwards is that the pushdown
covered `vid` and the three string columns and **stopped there**. `vt_s`,
`vt_e`, `src_id` and `dst_id` were copied whatever the caller asked for,
because at the time they were cheap relative to building a string per row.

Three sessions later a profile asked "is the cost column materialization?"
and answered it the right way — by projecting down to one column and
re-measuring. One column: 1170 ms. Four columns: 1167 ms. **Conclusion:
materialization is not the cost.** That conclusion went into a published
table, and the next three sessions spent themselves on the merge, the
cluster geometry and the selection loop.

Materialization was 47% of the scan the whole time. The experiment was
sound; the knob it turned was not connected. When the projection was
finished, the same one-column 10M scan went 360 → 143 ms and the operator
above it 463 → 215.

The generalisable part is not "check your projection". It is that **a
negative result from a control you did not verify is not a negative result**.
A knob you turn to prove something is itself a claim, and it deserves the
same treatment as the thing under test: assert that it changed what you
think it changed. One assertion — that a one-column scan returns one column —
would have caught this, and it now exists as a test rather than as a habit.

The same shape appears one layer up. Once the engine honoured the
projection, `edge_event_count` was still asking for four columns and reading
one; that had been free for as long as the pushdown was broken, so nothing
ever flagged it. Fixing an abstraction can make callers wrong retroactively.

## 12. Small operational traps worth knowing

- `uv sync` does **not** rebuild a compiled extension when only Rust changed;
  it sees unchanged Python metadata and skips. `--reinstall-package` is
  required. This produces a stale `.so` that silently runs old code.
- `#[derive(FromPyObject)]` extracts by *attribute*, not by key. Reading a
  plain dict needs `#[pyo3(from_item_all)]`.
- `pub(crate)` is invisible across a crate boundary — a workspace split turns
  some of it into `pub`.
- `#[pyclass]` requires `Sync`, so an interior-mutability cache must be a
  `Mutex`, not a `RefCell`.
- Patching source by slicing between two markers will silently delete
  anything in between. Assert what is being removed, not only what is added.
- Flipping a default backend is a data-safety question, not a config change.
  Detect the on-disk layout so existing stores keep their own engine; a naive
  flip creates an empty store beside the real one and looks exactly like data
  loss.

## 14. Two operators can share an implementation without sharing a contract

`graph_metric_timeseries` and `aggregate_events` compute the same per-bucket
event count and *disagree about what to emit*: the series operator returns
every bucket in the window, zero where nothing happened, and reports
`n_buckets`; the aggregation operator returns only non-empty groups. D-044
kept them apart for that reason. D-047 then routed the first through the
second's kernel and took it 217 → 84 ms, because the disagreement is four
lines wide — scatter the non-empty groups into a zero-filled array — and
everything else the two contracts differ on lives above it.

The generalisable part is the ordering. What made this safe was not care;
it was that a third, independent implementation of the series operator
already existed in the oracle, and that the property tests comparing them
were **not allowed to be edited**. An implementation swap under a stable
contract is either provably invisible or it is a semantics change wearing a
performance change's clothes, and the only cheap way to tell the two apart
is to have written the contract down somewhere the optimizer cannot reach.
The two assertions worth adding first, both written before the change:
one that the operators still disagree in the way they are supposed to, and
one that the *control* — here, the dtype the boundary returns — is what you
believe it is (§13).

## 15. The cost of an exact aggregate can be its representation, and its place

`count_distinct` was 240 ms of a 338 ms query, and the reflex reading is
that exact distinct counting over ten million ids is simply expensive —
ClickHouse's `uniqExact` costs about the same, which makes the reflex feel
confirmed. Two things were wrong with it.

The representation was wrong. The kernel appended raw `u32` ids per group
and sorted-plus-deduplicated them at the end. But these ids are *dense*: a
group's distinct set is a subset of `0..n_entities`, so a bitset answers the
question with a popcount, and merges with an OR — which is also the
commutative operation the two-phase design already required everywhere else.
Same answer, 9.6 ms.

And the *place* was wrong. The sort lived in `finalize`, which is the one
serial stage; the per-row work lives in the parallel fold. A design can be
parallel and still put its dominant cost in the sequential part, and nothing
about the design says so — only a stage split does.

The trap the fix has to avoid is worth as much as the fix. A bitset per
group is a capacity hazard precisely where groups are numerous, which is the
case the group cap exists for. So the group starts as an id vector and
promotes only once that vector holds as many bytes as the bitset would,
which bounds the state at twice the append path's — *independently of the
group cap and of the entity count*. A performance change that needs a
capacity caveat is not finished; make the caveat impossible instead, and
then state the bound it bought.

## 16. Instrument the thing you are about to fix, not the thing you blame

Group commit was on the roadmap for months with a price tag attached: single
writes cost 26 ms each, batched ones 0.1, and the fix was to coalesce. Before
building it we finally measured a single write layer by layer — the
write-ahead log fsync, the Python bi-temporal semantics, and the engine
commit, which was taught to report its own phase split:

| layer, one-row write | before | after |
|---|---:|---:|
| write-ahead log fsync | 0.19 ms | 0.19 ms |
| `apply_ops` (Python semantics) | **59.86 ms** | **0.53 ms** |
| engine commit (four fsynced writes) | 30.39 ms | 33.78 ms |
| **total** | **90.60** | **34.87** |

Two thirds of the "durability floor" was `nodes_with_believed_versions`, the
existence probe bulk ingest uses to decide which nodes are new. It asked
about two uids and materialized every node version in the store. Measured on
a 100k-event store: the scan costs 50.5 ms about 2 uids and 58.9 ms about
20,000 — flat, because the uids were never the work — while one postings
probe costs 0.005 ms. Both paths kept, chosen by the ratio between them, so
bulk ingest keeps the scan it actually needs.

**And then the same discipline took most of that headline back.** The rule
this project keeps — after changing anything, re-run the published table —
found `docs/eval_writes.md`'s single-row append *unmoved*: 96 ev/s before,
95 after. The probe materializes node *versions*, so its cost scales with
distinct entities rather than with events, and that benchmark's generator
mints `scale / 100` entities: its append test runs against **1,000** of them,
where the defect was worth under a millisecond. The 90.6 → 34.9 figure above
came from a generator with a fresh entity pair per event — 200,000 node
versions, the other end of the same axis. On this host: ~0.6 ms at 1,000
node versions, 24.2 ms at 40,000, 157.4 ms at 200,000; 0.005 ms on the new
path throughout.

Both sentences are true — an O(entities) cost on every ingest batch, and no
change to any published write number — and only the flattering one was going
to get written down. **A speedup is a property of a workload, not of a
patch**, and the workload that produced it deserves the same scrutiny as the
patch: ours differed from the benchmark's on an axis (entity cardinality)
that nobody had thought to hold fixed, because nobody had previously had a
reason to care about it.

This is the sixth entry in the misdiagnosis table with the same shape — *a
small lookup rebuilding the whole store* — and the second time it was found
hiding inside a cost we had labelled as something else (§9b was the first).
The pattern is specific enough now to be a checklist item: **when a write is
slow, time the read inside it.** Writes read. They read to carve intervals,
to find the version being corrected, to decide what is new; and a read on a
write path is invisible to every read benchmark you have.

The fix did not make group commit unnecessary — with the probe gone the
floor is genuinely the durable generation, 33.8 ms of 34.9 — but it changed
what group commit is worth by 2.6× before a line of it was written. An
optimization sized against an unprofiled baseline is sized against a
different system.

## 17. A reader that repairs the store is a writer

The concurrency work started from a comfortable position: readers-only
concurrency had been measured and was clean — 13× aggregate throughput at 16
readers, flat per-reader medians, no interference consistent with locking.
The remaining question looked like a cost question. The first mixed
writer+readers run answered a correctness one instead: **the writer crashed
on its second batch**, with a missing segment file, and took two of the three
readers with it.

Two defects, both invisible to every readers-only measurement, both in the
same place — *what opening a store does*.

The first: the Python `Store` runs crash recovery on open, replaying the
event-log suffix the backend has not applied. That is correct for the writer.
But the write path is write-ahead — the batch is fsynced to the log *before*
it is applied — so a live writer spends the whole of every commit in exactly
the state recovery reads as a crash. Every reader opening in that window
replayed the batch and published a generation, concurrently with the writer
publishing the same generation number: two writers, allocating the same
segment ids, overwriting each other's segment files under the mmap of anyone
already reading them.

The second: `Dictionary::open` truncated `dict.log` when the file was longer
than the manifest claimed, reading that as a batch that died before
publishing. It is also byte-for-byte what a live writer looks like between
its dictionary fsync and its `CURRENT` flip. Open could not tell them apart,
so it deleted bytes the writer had already made durable and was about to
name — and the generation the writer then published would not open.

The generalisable rule is smaller than either bug: **opening a store must not
mutate it.** Both defects are the same instinct — open sees something that
looks like debris and tidies it — and the instinct is safe only under an
assumption ("nobody else is running") that opening is precisely the moment
you cannot check. Recovery is an act of the writer; a reader gets
`read_only=True`, which does no recovery and refuses the write API.

And a note on what measurement can and cannot buy you. §14.4 was a good
measurement, honestly reported, and it licensed a conclusion one word wider
than it had earned: it showed *readers* do not interfere with *readers*, and
was read as showing the concurrency story worked. The missing case was not an
exotic interleaving. It was the second-simplest configuration there is, and
it failed in under a second the first time it ran.

---

## The shortest version

Build the reference implementation first. Then measure, and keep measuring
after each fix — because the layer you are about to optimize is usually not
the one costing time, and the only way to find that out is a number that
refuses to move.
