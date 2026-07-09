# TGMS operator micro-benchmarks

store: `stores/synth-100k` — |V|=5,000, edge versions=100,000

| operator | case | p50 ms | p95 ms | rows | note |
|---|---|---:|---:|---:|---|
| entity_history | base | 138.0 | 141.6 | 1 | |
| snapshot_subgraph | hop2 | 13.1 | 14.2 | 0 | |
| diff_snapshots | global | 22.6 | 23.2 | 0 | |
| temporal_reachability | w10 | 19.5 | 20.1 | 2 | |
| temporal_reachability | w50 | 78.0 | 79.4 | 3075 | |
| temporal_paths | w10 | 4.7 | 5.1 | 1 | |
| count_temporal_motifs | tri-w10 | 29.0 | 29.3 | 0 | |
| graph_metric_timeseries | events-100b | 144.2 | 146.3 | 100 | |
| burst_detection | zscore | 143.8 | 147.0 | 3 | |
| neighborhood_evolution | base | 143.3 | 144.6 | 0 | |
| co_active | src-narrow | 274.0 | 277.5 | 35 | |
| resolve_entities | substr | 17.7 | 28.6 | 1111 | |
