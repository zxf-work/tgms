# System configurations (as measured)

All loopback-only, on the measurement host, settings echoed into run
manifests (D-030: tuning is part of what is measured).

| system | version | install | key settings |
|---|---|---|---|
| TGMS native | branch head | maturin wheel | compression D-032/33, segment cache, parallel scan; gc D-034 |
| TGMS duckdb | duckdb (eval extra pin) | pip | adapter defaults |
| PostgreSQL | 16.14 | source build, `--locale=C --without-icu` | shared_buffers 16g, effective_cache_size 64g, work_mem 256m, random_page_cost 1.1; session `SET`s in `pg_baseline.py`; partial indexes `WHERE tt_e = OPEN_END` |
| ClickHouse | 26.8.1 | official static binary | ports 19000/18123, mark_cache 5g, server mem cap 32g, max_threads 16; MergeTree ORDER BY (vt_s, vid) / (uid, vt_s, vid) |
| Neo4j | Community 5.26.0 | tarball + Temurin JDK 21 | heap 8g, pagecache 16g, auth off (loopback); indexes: Entity(uid), Entity(dense_id), NodeVersion(uid), rel props vt_s/tt_e |
| Memgraph | 3.12.0 | official container, host uid | memory-limit 32g; vm.max_map_count 524288 host-side; DDL in `memgraph_baseline.py` |

Full DDL and tuning live in the respective `scripts/*_baseline.py`; the
Neo4j `dense_id` index is load-bearing for the iterative queries (its
absence measured 47 s per reachability round, live).
