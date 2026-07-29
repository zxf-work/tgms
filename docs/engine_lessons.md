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

Five times we identified "the bottleneck" and were wrong. Every single time,
the fix that mattered was found by measuring *after* the intended fix failed
to move the number.

| we believed | we measured | actual cause |
|---|---|---|
| Rust scan will beat NumPy | 2–5× **slower** | per-row metadata lookups in the loop |
| point lookups are quadratic | per-op cost flat as N grew | per-commit fsync, not scanning |
| `co_active`'s loop is the cost | `meets` cost 443 ms for 500 rows | unwindowed scan, not the join |
| incidence filter needs an index | filter answered in 0.8 ms | `stats()` scanning on every operator call |
| materialization pushes row-by-row | flat 170 ns/row at any segment count | `vid` hex built for unprojected rows |

Two of those five fixes we implemented were *correct but irrelevant* — the
contiguous-run copy and the postings index both stayed, because they are
cheap and right, but neither produced the win attributed to them.

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

---

## The shortest version

Build the reference implementation first. Then measure, and keep measuring
after each fix — because the layer you are about to optimize is usually not
the one costing time, and the only way to find that out is a number that
refuses to move.
