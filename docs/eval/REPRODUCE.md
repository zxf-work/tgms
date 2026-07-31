# Reproducing the six-system evaluation

Everything below runs on one Linux host (the receipts call it xzgpu: 40
cores, 93 GB, Ubuntu 20.04). Timings are only comparable when taken there;
`eval_harness.py` warns on any other host. No root is required except the
two one-time steps marked **sudo**.

## 0. Repo and environment

```bash
git clone <repo> tgms && cd tgms
uv sync --extra duckdb --extra eval     # Python deps incl. all baseline drivers
cargo test -p tgms-engine-core          # 130+ engine tests, ~10 s
```

## 1. Baseline servers (one-time)

**PostgreSQL 16.14** — source build, no root:
```bash
scripts/… see pg_baseline.py docstring; summary:
curl -sSLO https://ftp.postgresql.org/pub/source/v16.14/postgresql-16.14.tar.bz2
./configure --prefix=$BASE/pg --without-icu && make -j && make install
$BASE/pg/bin/initdb -D $BASE/pgdata --locale=C
uv run python scripts/pg_baseline.py --tune-server   # then restart
```

**ClickHouse 26.8** — official static binary, no root:
```bash
curl -sSL https://clickhouse.com/ | sh    # into $BASE/ch/bin
./clickhouse server --config-file=$BASE/ch/config.xml   # ports 19000/18123, loopback
```

**Neo4j Community 5.26** — tarball + user-space Temurin JDK 21:
```bash
# adoptium JDK 21 + neo4j-community-5.26.0-unix.tar.gz under $BASE/neo4j-install
# conf: loopback only, auth off, heap 8g, pagecache 16g  (see D-036)
bin/neo4j start
```

**Memgraph 3.12** — official container. **sudo** once for Docker CE and
`vm.max_map_count=524288`, then:
```bash
docker run -d --name memgraph --user $(id -u):$(id -g) \
  -p 127.0.0.1:7688:7687 \
  -v $BASE/memgraph/data:/var/lib/memgraph \
  -v $BASE/memgraph/logs:/var/log/memgraph \
  memgraph/memgraph:latest --log-level=WARNING
```

## 2. The measurements

Every number in `docs/eval_phase0.md` regenerates from one entry point:

```bash
# any subset of: native,duckdb,postgres,clickhouse,neo4j,memgraph
uv run python scripts/eval_harness.py --scale 200000 \
    --systems native,duckdb,postgres,clickhouse --json out.json
uv run python scripts/eval_harness.py --scale 1000000  --systems ...
uv run python scripts/eval_harness.py --scale 10000000 --systems ...
uv run python scripts/eval_harness.py \
    --log benchmarks/frozen-v1/collegemsg.eventlog.jsonl --systems ...
```

Write path and storage:
```bash
uv run python scripts/eval_writes.py --json writes.json
# storage: load all systems from one 1M dataset and compare bytes
# (docs/eval_phase0.md "one accounting"; script pattern in git history)
```

Protocol (plan §16): 5 warmups; 30 measured reps per sub-second query, 10
for slower; median and p95; raw timings in the JSON. Exit status is nonzero
on any canonical-hash mismatch, so the correctness gate is the same command
as the timing run.

## 3. What guards the results

- one replayed event log per dataset — never re-ingested (D-023)
- every (system, query) cell hash-verified before it is ever timed
- run manifests record commit, host, protocol, and dirty state
- raw records for every published table: `benchmarks/results-v1/`

Long-running launches on the host follow one pattern, learned the hard way:
a script file under nohup that prints `RUN_STARTED commit=<sha>` into its
own log first — never an inline nohup, never `pgrep` as launch evidence,
never kill and relaunch in one ssh command.
