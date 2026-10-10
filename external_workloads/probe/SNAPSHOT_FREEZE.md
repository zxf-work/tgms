# SQL page/count snapshot stress test: pre-registration

Committed BEFORE the stress run. Purpose: measure how often a SQL
(page, count) pair taken as two autocommit statements is mutually
inconsistent under a concurrent writer, and confirm that the same pair
taken through `tgms.evidence.sql_snapshot.consistent_page_and_count`
(one read transaction per engine-documented snapshot semantics) never
is. The count is what `build_sql_ecqr` certifies as `@ExactCardinality`.

## Engines (fixed order)

1. `sqlite-wal`: SQLite, `journal_mode=WAL`.
2. `sqlite-rollback`: SQLite, `journal_mode=DELETE` (rollback journal).
3. `duckdb`: DuckDB, in-memory database, reader and writer on two
   cursors of one database instance.
4. `postgres`: PostgreSQL via pgserver (embedded server, pgdata under
   node-local /tmp), psycopg 3 or psycopg2.

If an engine cannot be started on the run host, it is recorded in the
receipt under `not_run` with the reason. No engine is substituted.

## Workload

- Table `items(id, q)`: N qualifying rows (`q = 1`, ids `1000*i`) and
  N non-qualifying rows (`q = 0`, ids `1000*i + 500`); table
  `meta(k, gen)` with one row, `gen = 0`.
- N in {343, 34300}: the paper's example (a 100-row page of 343) and a
  100x larger input size.
- Page size 100.
- Semantic query: `SELECT id FROM items WHERE q = 1 ORDER BY id`.
- Page statement:
  `SELECT s.id, m.gen, COUNT(*) OVER () AS n_stmt FROM (SELECT id FROM
  items WHERE q = 1) s CROSS JOIN meta m ORDER BY s.id LIMIT 100`.
- Count statement: `SELECT COUNT(*) FROM (<semantic>) AS _tgms_count`
  (issued by the module under test).
- Between the page statement and the count statement the reader sleeps
  50 ms (the module's `between` hook).

## Writer

One thread, one connection, single-row commits every 5 ms (sleep 5 ms
after each commit). Each commit inserts one new qualifying row (a fresh
id drawn uniformly from [0, 1000*N) never used before) or deletes one
uniformly chosen qualifying row, and in the same transaction sets
`meta.gen` to a new generation g. The size of the qualifying set walks
monotonically between N-50 and N+50, reversing direction at either
bound (starting upward). Before committing g, the writer records the
size and the first 100 ids of the qualifying set at g. Ids are never
reused. Writer RNG seed: 20261009 + block index, blocks enumerated in
the order engine x N x (unsafe, consistent).

## Trials

T = 1000 trials per engine x N x variant block; each block uses a fresh
database. The writer starts 200 ms before the first trial. Variants:
`unsafe` (two autocommit statements, `unsafe_page_and_count`) and
`consistent` (`consistent_page_and_count`).

## Per-trial record

page length; count; gen_p (the generation the page statement read);
size_p (the writer's recorded size at gen_p); writer commits that
completed during the trial, and during the 50 ms window between the
page and the count statements; consistent_pair (count == size_p);
page_prefix_ok (page ids == the first 100 ids recorded at gen_p);
stmt_count_ok (n_stmt == size_p); certificate_wrong (the descriptor
`build_sql_ecqr(rows=page, total_count=count, limited=True)` carries
`exact_cardinality != size_p`, or claims delivery-complete while the
page is not the whole result); latency.

## Metrics per block

- fraction of trials with an inconsistent pair (with the Wilson 95%
  upper bound);
- number of wrong certificates the adapter would have emitted;
- fraction of trials in which at least one writer commit completed
  inside the 50 ms window (the window was exercised), and the mean
  number of such commits;
- page_prefix_ok and stmt_count_ok failures (oracle self-checks; any
  failure invalidates the run's oracle, not the module).

## Pass criterion

Every `consistent` block: 0 inconsistent pairs and 0 wrong
certificates. The `unsafe` rates are reported as measured; no threshold.
Under the rollback journal the consistent variant holds the writer off
for the transaction's duration (documented SQLite locking), so its
in-window commit fraction is expected to be 0; it is reported as is.

## Run

One CPU Slurm job on iTiger (`scripts/sql_snapshot.slurm`), one run,
all blocks. No reruns of individual blocks and no exclusion of trials.
If the job dies for an infrastructure reason, the whole run is repeated
with identical parameters and the failed job id is reported.
Receipt: `benchmarks/results-v1/eval-sql-snapshot.json`, carrying this
file's sha256.
