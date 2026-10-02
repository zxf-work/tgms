# external-v1: Neo4j 5 recompute configuration for P-EXT1

Lane N1's implementation of the Neo4j side of the external-baseline campaign
P-EXT1 (global recompute externalized on Neo4j 5), per the internal design
memo `docs/design/EXTERNAL_BASELINES_DESIGN_2026-10-02.md` §2 (property-graph
schema, loading path, Cypher per artifact family, the recompute protocol per
burst, correctness vs. the oracle, host protocol, record layout) and the
frozen pre-registration "P-EXT1" + "Addendum EXT-A" in
`docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md` §4.3b. This directory and
its tests are the only parts of that work committed to the public tree;
`docs/design/` and `paper/` are read-only reference and are never committed
from here.

**This is not an LDBC Benchmark** and nothing here is an LDBC Benchmark
Result — it is a correctness-and-wall-time comparison between TGMS's own
global-recompute configuration and an independently loaded Neo4j 5 property
graph answering the same 13 registered artifact families via Cypher.

## Status (as written)

Implementation (this package + README) and unit tests are complete and
committed. **The timed 36/43-cell + probe grid has not run and must not run
from this code without the coordinator's go-ahead** (memo §4.6, Addendum
EXT-A's run order: X1 export → T1 control → N1 timed cells → D1 → C1). What
*has* run, under `nice -n 19`, untimed, is a full shape test and a smoke run
against a **tiny synthetic cell this lane built locally** (not an X1 export —
see "Why not a real exported cell" below); results are in "Validation" below.

## 1. Versions and configuration

| component | version |
|---|---|
| Neo4j | Community **5.26.0** |
| JDK | Temurin **21.0.12** |
| APOC | **5.26.0-core** |
| Python driver (`neo4j`) | **5.28.6** |

`neo4j.conf` (memo §2.7 — base lines copied verbatim from the LDBC
reference install's working `neo4j.conf` on xzgpu,
`/mnt/project/xzhang/neo4j/neo4j-community-5.26.0/conf/neo4j.conf`, sha256
`b22fb1a80eac03284f4d046a7b6c3d4813e153bb2e7bde8e2c3c7b98583a6fac`
per `benchmarks/ldbc-ref-v1/README.md`; only `server.directories.{data,logs}`
and the bolt/http listen addresses are lane-specific — see
`neo4j_recompute/schema.py::conf_lines`):

```
server.directories.import=import
server.bolt.enabled=true
server.http.enabled=true
server.https.enabled=false
db.tx_log.rotation.retention_policy=2 days 2G
server.jvm.additional=-XX:+UseG1GC
server.jvm.additional=-XX:-OmitStackTraceInFastThrow
server.jvm.additional=-XX:+AlwaysPreTouch
server.jvm.additional=-XX:+UnlockExperimentalVMOptions
server.jvm.additional=-XX:+TrustFinalNonStaticFields
server.jvm.additional=-XX:+DisableExplicitGC
server.jvm.additional=-Djdk.nio.maxCachedBufferSize=1024
server.jvm.additional=-Dio.netty.tryReflectionSetAccessible=true
server.jvm.additional=-Djdk.tls.ephemeralDHKeySize=2048
server.jvm.additional=-Djdk.tls.rejectClientInitiatedRenegotiation=true
server.jvm.additional=-XX:FlightRecorderOptions=stackdepth=256
server.jvm.additional=-XX:+UnlockDiagnosticVMOptions
server.jvm.additional=-XX:+DebugNonSafepoints
server.jvm.additional=--add-opens=java.base/java.nio=ALL-UNNAMED
server.jvm.additional=--add-opens=java.base/java.io=ALL-UNNAMED
server.jvm.additional=--add-opens=java.base/sun.nio.ch=ALL-UNNAMED
server.jvm.additional=--enable-native-access=ALL-UNNAMED
server.jvm.additional=-Dlog4j2.disable.jmx=true
server.jvm.additional=-Dlog4j.layout.jsonTemplate.maxStringLength=32768
server.windows_service_name=neo4j
server.directories.data=<conf-ext-v1-specific>/data-ext-v1
server.directories.logs=<conf-ext-v1-specific>/logs-ext-v1
server.bolt.listen_address=127.0.0.1:7687
server.http.listen_address=127.0.0.1:7475
dbms.security.auth_enabled=false
server.memory.heap.initial_size=8g
server.memory.heap.max_size=8g
server.memory.pagecache.size=16g
db.transaction.timeout=600s
dbms.security.procedures.unrestricted=apoc.*
```

A dedicated `NEO4J_CONF` dir (`conf-ext-v1/`), own `data-ext-v1`/`logs-ext-v1`
(memo §2.7) — never the LDBC install's `data-ldbc-ref`/`logs-ldbc-ref`, and
never running at the same time as T1's control or another TGMS timed run on
xzgpu.

## 2. Property-graph schema (DDL)

One element per bitemporal version (memo §2.1):

```cypher
CREATE CONSTRAINT e_uid IF NOT EXISTS FOR (x:E) REQUIRE x.uid IS UNIQUE
CREATE RANGE INDEX nv_uid IF NOT EXISTS FOR (v:NV) ON (v.uid)
CREATE RANGE INDEX nv_vid IF NOT EXISTS FOR (v:NV) ON (v.vid)
CREATE RANGE INDEX nv_vt_s IF NOT EXISTS FOR (v:NV) ON (v.vt_s)
CREATE RANGE INDEX nv_tt_e IF NOT EXISTS FOR (v:NV) ON (v.tt_e)
CREATE RANGE INDEX ev_vid IF NOT EXISTS FOR ()-[e:EV]-() ON (e.vid)
CREATE RANGE INDEX ev_eid IF NOT EXISTS FOR ()-[e:EV]-() ON (e.eid)
CREATE RANGE INDEX ev_vt_s IF NOT EXISTS FOR ()-[e:EV]-() ON (e.vt_s)
CREATE RANGE INDEX ev_tt_e IF NOT EXISTS FOR ()-[e:EV]-() ON (e.tt_e)
CREATE RANGE INDEX ev_rel_type IF NOT EXISTS FOR ()-[e:EV]-() ON (e.rel_type)
```

- `(:E {uid})` — one node per identity seen in any node or edge version.
- `(:NV {vid, uid, label, vt_s, vt_e, tt_s, tt_e, props, source,
  provenance_ref})` — node versions. `uid` is an ordinary (indexed)
  property, not a graph edge to `:E` — every lookup is `MATCH (v:NV
  {uid:$uid})`.
- `(:E)-[:EV {eid, vid, rel_type, disc, vt_s, vt_e, tt_s, tt_e, props,
  source, provenance_ref}]->(:E)` — edge versions. **The Neo4j relationship
  type is always the uniform `EV`**; `rel_type` (`KNOWS`, `FOLLOWS`, ...) is
  carried as an ordinary property, never as the graph's own relationship
  type (see the CSV-import bug below — this was gotten wrong once).
- `props` is a canonical-JSON **string** (Neo4j has no map properties);
  `format.py` parses it back into a value when building a payload.
- Belief at the current transaction time: `x.tt_e = $O` with
  `$O = 4611686018427387904` (`2**62`). Every registered storm artifact reads
  at `as_of_tt = OPEN_END` (the default), so `$O` is a constant in every
  query, never a live `as_of_tt` parameter — a registered artifact pinned to
  a past `as_of_tt` is out of this frozen scope (`NotExpressibleError`).

## 3. Loading (memo §2.2)

1. `csv_export.py` turns an exported cell's `versions-epoch0.jsonl` into
   headerless CSV part-files + separate header files: `entities.csv`
   (`uid:ID(E)`, one row per distinct identity, sorted/deduplicated),
   `node_versions.csv` (`vid:ID(NV)`, ...), `edge_versions.csv`
   (`:START_ID(E),:END_ID(E),...,:TYPE` with `:TYPE` always the literal
   string `EV`).
2. `loader.run_import` calls `neo4j-admin database import full` — **no
   trailing `<database>` argument** (picocli parses it as one more
   `--relationships` file) and **headerless part-files** (the header lives
   in its own file) — the same two corrections already proven on the LDBC
   run (`benchmarks/ldbc-ref-v1/README.md` §3).
3. Start, apply the DDL above, `CALL db.awaitIndexes($timeout)`,
   `neo4j-admin database dump` to a pristine per-store dump.
4. Per cell: `database load --overwrite-destination` from that dump, start,
   await indexes, structural check (`version_table_sha256` recomputed from
   Neo4j equals the export's epoch-0 value — not yet wired into this
   package's runner; see "Deviations" below).
5. Corrections go through the Bolt driver, one write transaction per burst
   (`corrections.py`, memo §2.2's four statements: close node versions,
   close edge versions, insert new node versions, insert new edge versions).

## 4. The 13 Cypher texts, one per family

`neo4j_recompute/queries.py` (`FAMILY_TEXT`). F11 has no single-statement
form in 5.26 (quantified path patterns cannot carry arrival time across
iterations, and Cypher 25's `allReduce` is not in 5.26) — it is a
client-driven iterated query (`reachability.py`). F13 needs no database
access: the registered instance is the literal-count ∅-scope control
(`fn="count", input=[{"x":1},{"x":2}]`), always `2`.

| F | family (op) | Cypher verdict | builder |
|---|---|---|---|
| F1 | `entity_history` | single statement | `f1_params` |
| F2 | `version_history` (node, belief current) | single statement | `f2_params` |
| F3 | `snapshot_subgraph` (1 seed, hops 1) | single statement | `f3_params` |
| F4 | `diff_snapshots` (whole graph) | single statement, 6 `CALL` sub-queries | `f4_params` |
| F5 | `neighborhood_evolution` | single statement | `f5_params` |
| F6 | `aggregate_events` (group by src, count) | single statement | `f6_params` |
| F7 | `graph_metric_timeseries` (edge_event_count) | single statement | `f7_params` |
| F8 | `burst_detection` (node_activity, zscore) | single statement | `f8_params` |
| F9 | `count_temporal_motifs` (M_2node_pingpong) | single statement | `f9_params` |
| F10 | `find_temporal_motif_instances` | single statement, 2 `CALL` sub-queries | `f10_params` |
| F11 | `temporal_reachability` (out, no wait bound) | **iterated** (client-driven fixpoint) | `f11_window_params` + `reachability.run_reachability` |
| F12 | `temporal_paths` (k 2, max_hops 4) | single statement, 4 hop-pruned branches `UNION ALL` | `f12_params` (+ `f12_query`) |
| F13 | `compute` (∅-scope control) | constant, no query | `f13_params` |

All 13 registered families are expressible — **13/13**, F11 iterated, as the
memo predicted (§2.3/§4.5: "none found on paper"). `co_active` is drawn by
storm but 0 instances are registered at admission (Addendum A8) — listed as
"drawn, refused by TGMS at registration, not compared", never built here.

Cypher texts (`$t_a,$t_b` = window; `$t` = `t_valid`; `$d` = delta; `$O` =
`OPEN_END`; valid-at-`$t` is `x.vt_s <= $t < x.vt_e`):

```cypher
-- F1 entity_history (include_edges)
CALL { MATCH (v:NV {uid:$uid}) WHERE v.tt_e=$O WITH v ORDER BY v.vt_s, v.vid
       RETURN collect(v{.vid,.uid,.label,.vt_s,.vt_e,.tt_s,.tt_e,.props,.source,.provenance_ref}) AS rs }
CALL { MATCH (:E {uid:$uid})-[e:EV]-() WHERE e.tt_e=$O WITH DISTINCT e ORDER BY e.vt_s, e.vid
       RETURN collect(e{.eid,.vid,src:startNode(e).uid,dst:endNode(e).uid,.rel_type,.vt_s,.vt_e}) AS es }
RETURN rs[0..$lim] AS rows, size(rs) AS rows_total, es[0..$lim] AS edges, size(es) > $lim AS edges_truncated

-- F2 version_history (node, belief current)
MATCH (v:NV) WHERE v.tt_e=$O AND v.vt_s < $t_b AND $t_a < v.vt_e WITH v ORDER BY v.tt_s, v.vid
WITH collect(v{.vid,.uid,.label,.vt_s,.vt_e,.tt_s,.tt_e}) AS rs RETURN rs[0..$lim] AS rows, size(rs) AS rows_total

-- F3 snapshot_subgraph (one seed, hops 1)
CALL { MATCH (s:NV {uid:$seed}) WHERE s.tt_e=$O AND s.vt_s<=$t<s.vt_e RETURN collect({uid:s.uid,label:s.label,hop:0}) AS h0 }
CALL { WITH h0 MATCH (:E {uid:$seed})-[e:EV]-(y:E) WHERE size(h0)>0 AND y.uid<>$seed AND e.tt_e=$O AND e.vt_s<=$t<e.vt_e
       MATCH (w:NV {uid:y.uid}) WHERE w.tt_e=$O AND w.vt_s<=$t<w.vt_e WITH DISTINCT w ORDER BY w.uid
       RETURN collect({uid:w.uid,label:w.label,hop:1}) AS h1 }
WITH h0+h1 AS ns WITH ns, [n IN ns | n.uid] AS us
CALL { WITH us MATCH (a:E)-[e:EV]->(b:E) WHERE a.uid IN us AND b.uid IN us AND e.tt_e=$O AND e.vt_s<=$t<e.vt_e
       WITH a,b,e ORDER BY e.vt_s, e.vid RETURN collect(e{.eid,.vid,src:a.uid,dst:b.uid,.rel_type,.vt_s,.vt_e}) AS es }
RETURN es[0..$lim] AS rows, size(es) AS rows_total, ns[0..$lim] AS nodes, size(ns) AS nodes_total

-- F4 diff_snapshots (scope null) -- nodes_added/removed (uid-string lists),
-- edges_added/removed (dicts, no props), props_changed (nodes then edges,
-- each sorted by id; a version present at both instants via a different vid
-- with different props or label)
CALL { MATCH (v:NV) WHERE v.tt_e=$O AND v.vt_s<=$t1<v.vt_e RETURN collect(v.uid) AS u1 }
CALL { MATCH (v:NV) WHERE v.tt_e=$O AND v.vt_s<=$t2<v.vt_e RETURN collect(v.uid) AS u2 }
WITH u1, u2, apoc.coll.sort(apoc.coll.subtract(u2,u1)) AS na, apoc.coll.sort(apoc.coll.subtract(u1,u2)) AS nr
CALL { MATCH (a:E)-[e:EV]->(b:E) WHERE e.tt_e=$O AND e.vt_s<=$t2<e.vt_e
         AND NOT EXISTS { MATCH ()-[f:EV {eid:e.eid}]->() WHERE f.tt_e=$O AND f.vt_s<=$t1<f.vt_e }
       WITH a,b,e ORDER BY e.eid RETURN collect({eid:e.eid,src:a.uid,dst:b.uid,rel_type:e.rel_type}) AS ea }
CALL { MATCH (a:E)-[e:EV]->(b:E) WHERE e.tt_e=$O AND e.vt_s<=$t1<e.vt_e
         AND NOT EXISTS { MATCH ()-[f:EV {eid:e.eid}]->() WHERE f.tt_e=$O AND f.vt_s<=$t2<f.vt_e }
       WITH a,b,e ORDER BY e.eid RETURN collect({eid:e.eid,src:a.uid,dst:b.uid,rel_type:e.rel_type}) AS er }
CALL { MATCH (v1:NV) WHERE v1.tt_e=$O AND v1.vt_s<=$t1<v1.vt_e
       MATCH (v2:NV) WHERE v2.tt_e=$O AND v2.vt_s<=$t2<v2.vt_e AND v2.uid=v1.uid AND v2.vid<>v1.vid
       WITH v1,v2 WHERE v1.props<>v2.props OR v1.label<>v2.label
       WITH v1,v2 ORDER BY v1.uid
       RETURN collect({kind:'node',id:v1.uid,from:{label:v1.label,props:v1.props},
                       to:{label:v2.label,props:v2.props}}) AS pn }
CALL { MATCH (:E)-[e1:EV]->() WHERE e1.tt_e=$O AND e1.vt_s<=$t1<e1.vt_e
       MATCH (:E)-[e2:EV]->() WHERE e2.tt_e=$O AND e2.vt_s<=$t2<e2.vt_e AND e2.eid=e1.eid AND e2.vid<>e1.vid
       WITH e1,e2 WHERE e1.props<>e2.props
       WITH e1,e2 ORDER BY e1.eid
       RETURN collect({kind:'edge',id:e1.eid,from:{props:e1.props},to:{props:e2.props}}) AS pe }
WITH na,nr,ea,er,pn,pe, pn+pe AS pc
RETURN na[0..$lim] AS nodes_added, size(na) AS nodes_added_total,
       nr[0..$lim] AS nodes_removed, size(nr) AS nodes_removed_total,
       ea[0..$lim] AS edges_added, size(ea) AS edges_added_total,
       er[0..$lim] AS edges_removed, size(er) AS edges_removed_total,
       pc[0..$lim] AS props_changed, size(pc) AS props_changed_total

-- F5 neighborhood_evolution
MATCH (:E {uid:$uid})-[e:EV]-() WHERE e.tt_e=$O WITH DISTINCT e
WITH collect({vs:e.vt_s, ve:e.vt_e, nb:CASE WHEN startNode(e).uid=$uid THEN endNode(e).uid ELSE startNode(e).uid END}) AS inc
WITH inc, apoc.coll.sort(apoc.coll.toSet([p IN inc WHERE p.nb<>$uid AND p.vs<=$t1<p.ve | p.nb])) AS n1,
          apoc.coll.sort(apoc.coll.toSet([p IN inc WHERE p.nb<>$uid AND p.vs<=$t2<p.ve | p.nb])) AS n2
WITH inc, apoc.coll.sort(apoc.coll.subtract(n2,n1)) AS g, apoc.coll.sort(apoc.coll.subtract(n1,n2)) AS l
RETURN g[0..$lim] AS neighbors_gained, size(g) AS neighbors_gained_total, l[0..$lim] AS neighbors_lost, size(l) AS neighbors_lost_total,
       [bs IN range($t1,$t2-1,$stride) | {t:bs, degree:size([p IN inc WHERE p.vs<=bs<p.ve])}] AS degree_series,
       $stride AS stride

-- F6 aggregate_events (group_by endpoint src, count)
MATCH (a:E)-[e:EV]->() WHERE e.tt_e=$O AND $t_a<=e.vt_s<$t_b WITH a.uid AS src, count(*) AS c ORDER BY src
WITH collect({src:src, count:c}) AS rs RETURN rs[0..$lim] AS rows, size(rs) AS rows_total

-- F7 graph_metric_timeseries (edge_event_count)
UNWIND range($t_a,$t_b-1,$stride) AS bs WITH bs, CASE WHEN bs+$stride<$t_b THEN bs+$stride ELSE $t_b END AS be
CALL { WITH bs,be MATCH ()-[e:EV]->() WHERE e.tt_e=$O AND bs<=e.vt_s<be RETURN count(e) AS v }
WITH bs,be,v ORDER BY bs WITH collect({t_a:bs,t_b:be,value:v}) AS rs RETURN rs[0..$lim] AS rows, size(rs) AS rows_total, size(rs) AS n_buckets

-- F8 burst_detection (node_activity, zscore, w=10, z=3.0)
MATCH (:E {uid:$uid})-[e:EV]-() WHERE e.tt_e=$O WITH collect(DISTINCT e) AS es
WITH [bs IN range($t_a,$t_b-1,$stride) | {bs:bs, be:CASE WHEN bs+$stride<$t_b THEN bs+$stride ELSE $t_b END}] AS bk, es
WITH bk, [b IN bk | toFloat(size([e IN es WHERE b.bs<=e.vt_s<b.be]))] AS x
WITH bk, x, [i IN range(1,size(x)-1) | {i:i, h:x[CASE WHEN i<$w THEN 0 ELSE i-$w END..i]}] AS hs
WITH bk, x, [p IN hs | {i:p.i, h:p.h, m:reduce(s=0.0, y IN p.h | s+y)/size(p.h)}] AS ms
WITH bk, x, [p IN ms | {i:p.i, m:p.m, sd:sqrt(reduce(s=0.0, y IN p.h | s+(y-p.m)^2)/size(p.h))}] AS ss
WITH bk, x, [p IN ss | {i:p.i, sc:round(CASE WHEN p.sd>0 THEN abs(x[p.i]-p.m)/p.sd WHEN x[p.i]=p.m THEN 0.0 ELSE 1e9 END, 9)}] AS sc
WITH x, [p IN sc WHERE p.sc >= $z | {t_a:bk[p.i].bs, t_b:bk[p.i].be, value:x[p.i], score:p.sc}] AS fl
RETURN fl[0..$lim] AS rows, size(fl) AS rows_total, size(x) AS n_buckets

-- F9 count_temporal_motifs (M_2node_pingpong); order is the (vt_s, eid) total order
MATCH (u:E)-[a:EV]->(v:E) WHERE u<>v AND a.tt_e=$O AND $t_a<=a.vt_s<$t_b
MATCH (v)-[b:EV]->(u) WHERE b.tt_e=$O AND b.vt_s<$t_b AND b.vt_s-a.vt_s<=$d AND (b.vt_s>a.vt_s OR (b.vt_s=a.vt_s AND b.eid>a.eid))
MATCH (u)-[c:EV]->(v) WHERE c.tt_e=$O AND c.vt_s<$t_b AND c.vt_s-a.vt_s<=$d AND (c.vt_s>b.vt_s OR (c.vt_s=b.vt_s AND c.eid>b.eid))
WITH count(*) AS n CALL { MATCH ()-[e:EV]->() WHERE e.tt_e=$O AND $t_a<=e.vt_s<$t_b RETURN count(e) AS nw }
RETURN n AS count, nw AS n_events_in_window

-- F10 find_temporal_motif_instances: F9's three MATCH clauses, then two
-- sub-queries -- count(*) and ORDER BY a.vt_s,a.eid,b.vt_s,b.eid,c.vt_s,c.eid
-- LIMIT $lim (25) returning [{src,dst,t,eid,rel_type}] x3 per instance.

-- F11 temporal_reachability -- one round; the client keeps best[uid]
-- (best[src]=$t_a), sends the improved frontier, stops when no label
-- improves; rows = best minus src, sorted (arrival, uid). Earliest arrival
-- is exact here because delta_max_wait is null.
UNWIND $front AS f MATCH (:E {uid:f.uid})-[e:EV]->(y:E) WHERE e.tt_e=$O AND e.vt_e>$t_a AND e.vt_s<$t_b
WITH y, e, CASE WHEN f.arr>e.vt_s THEN f.arr ELSE e.vt_s END AS tau WHERE tau<e.vt_e AND tau<$t_b
RETURN y.uid AS uid, min(tau) AS arr

-- F12 temporal_paths -- CALL { b1 UNION ALL b2 UNION ALL b3 UNION ALL b4 };
-- branch L expands hop by hop, pruning at every hop (tau_i = max(tau_{i-1},
-- r_i.vt_s) < min(r_i.vt_e, $t_b); simple path; only the last node is $dst),
-- returning arrival, hops, key=[[r.vt_s,r.eid],...], edges. Generated by
-- `queries.f12_branch`/`f12_query` rather than hand-duplicated. Hop i:
MATCH (n{i-1})-[r{i}:EV]->(n{i}) WHERE r{i}.tt_e=$O AND r{i}.vt_e>$t_a AND r{i}.vt_s<$t_b
  [AND n{i}.uid<>$dst AND n{i}.uid<>$src AND n{i}.uid<>n{j}.uid for each earlier j]
WITH *, CASE WHEN t{i-1}>r{i}.vt_s THEN t{i-1} ELSE r{i}.vt_s END AS t{i} WHERE t{i}<r{i}.vt_e AND t{i}<$t_b
-- outer: WITH ... ORDER BY arrival, hops, key; rows[0..$k], rows_total=count, truncated = rows_total > $k

-- F13 compute (empty-scope control) -- no database access
RETURN 2 AS value
```

Strings compare by UTF-16 code unit in Neo4j and by code point in Python;
every uid/eid in the committed grid is ASCII, so orders agree. Neo4j's
`round(x, 9)` rounds half-up where Python's `round()` rounds the binary
value half-even; `format.py` re-rounds every float with
`canon.canonicalize_floats` (Python's own `round()`) as the formatter's last
step, so no digest ever depends on which side's `round()` ran — only the
Cypher-side `round()` in F8's score (used there purely to stabilize the
threshold comparison `>= $z`, memo §2.3) is not itself trusted as final.

## 5. Recompute protocol (memo §2.5)

`runner.run_cell`: load pristine dump → start → indexes online → epoch-0
pass (every artifact's query once, untimed — warms plan cache/JIT/page
cache, and is epoch 0's own correctness check) → for each burst: apply the
delta (`apply_ms`) → every registered artifact's query, serially, one
session, auto-commit read transactions, results fully consumed
(`recompute_ms`) → untimed: digest and score against the oracle. Timed
quantity for predictions (a)/(b)/(c) is `recompute_ms` alone; `apply_ms` and
the sum (`wall_ms`) are reported alongside, never substituted for it.
Ceiling `db.transaction.timeout=600s`: a timed-out artifact is recorded
`not_answered` and excluded from the agreement count (not from
`recompute_ms`, which the ceiling itself bounds at 600 s for that query).

## 6. Output per cell

`result.json` (`record.py::build_result`, matching
`benchmarks/schema/result_manifest.schema.json`'s required top level plus
the memo's `cell_id`/`cell_digest`/`equality_level`/`per_cell`/`gates`
fields: versions, DDL text, every Cypher text keyed by family, `neo4j.conf`
sha256, export-manifest sha256, per-burst rows, oracle-agreement gate) and
`run.log` (`record.py::setup_run_log`). The merged multi-cell record
(`benchmarks/external-v1/neo4j-recompute-<date>.json` + `-rows.jsonl`,
sha-gated) is lane C1's job, not this package's.

## 7. Validation

### 7a. Unit tests (laptop, `tests/external/neo4j_recompute/`, committed separately)

Cypher-generation unit tests per family (`test_queries.py`) on a tiny
synthetic cell this lane built locally (`tests/external/neo4j_recompute/
fixtures/tiny-cell/`: 5 nodes, 13 artifacts — one per registered family,
using `tgms/eval/storm.py`'s own template args) — every registered family's
param builder is exercised and asserted not to raise, plus explicit
rejection tests for each family's out-of-frozen-scope args. `test_canon.py`
pins the independent canonicalization/digest reimplementation against
literal values. `test_csv_export.py` checks the CSV import headers
structurally (**these two assertions are regression tests for bugs the live
shape test below actually hit** — see "Deviations"). `test_format.py`
checks pagination/truncation/cursor derivation and float re-rounding.
`test_reachability.py` checks the F11 client-driven fixpoint against a
brute-force reference over several hand-built small graphs, using a fake
Bolt session (no live Neo4j needed for any of these).

### 7b. Shape test (live Neo4j, xzgpu, untimed, `nice -n 19`)

**Why not a real exported cell.** The go/no-go checklist (memo §4.6 item 2)
and this task both call for the shape test to run on an exported collegemsg
cell from lane X1's `INDEX.json`. As of this session, **X1 has not produced
any finished cell**: `/mnt/project/xzhang/tgms/external-v1/export/INDEX.json`
does not exist, and the one partial cell directory present
(`collegemsg-c3-none-n1000-s0/`, `artifacts.jsonl` +
`versions-epoch0.jsonl` only — no `deltas.jsonl`/`oracle.jsonl`) is the debris
of a **failed** X1 run: `smoke_collegemsg_c3_none_s0.log` on xzgpu records
`FAILED: collegemsg-c3-none-n1000-s0: only realized 0/20 batches within 80
draws (mix starved) -- not exported`. Per the lane rules ("read only
finished cells listed in its INDEX.json") and the task's own instruction
("a family that fails is reported, never patched in the client" / "stop and
report rather than improvise"), this lane did not treat that partial
directory as usable and did not attempt to work around X1's failure.

**What ran instead.** To validate the implementation end to end before
reporting readiness, this lane built its own tiny synthetic cell locally
(`tests/external/neo4j_recompute/fixtures/tiny-cell/`, built via
`tgms.store.Store` + `tgms.temporal.algebra.call_operator` directly — no
native-engine build needed beyond the laptop's own `.venv`) and ran the full
pipeline against a live Neo4j 5.26.0 on xzgpu: `neo4j-admin database import
full` → DDL + `awaitIndexes` → epoch-0 pass → apply one correction burst
(a `correct`, a `retract`, and an `assert_edge`, diffed into the
`closed`/`inserted` delta shape) → recompute pass → digest and score against
the TGMS oracle recorded for that fixture.

**Result: 13/13 families agree, at both epoch 0 and epoch 1 (post-burst).**
No family was refused, no family timed out.

```
13 artifacts, 1 delta burst
epoch0: agree 13 disagree [] errors []           recompute_ms=5610.9 (cold: import + first JIT/plan-cache pass)
epoch1: agree 13 disagree [] errors []  apply_ms=282.7  recompute_ms=132.5 (warm)
```

Neo4j server confirmed stopped (no `java` process, no listener on
7687/7475) at the end of this lane's session.

### 7c. Deviations found and fixed during the shape test

Three defects were found only by actually running against live Neo4j — none
were visible from reading the Cypher text alone, which is the reason this
validation step exists rather than being skipped:

1. **`:E` nodes had no queryable `uid` property.** `csv_export.py`'s
   entities header was bare `:ID(E)`; every `{uid:$x}` lookup matched
   nothing (`entity_history`'s `edges` came back empty, among others). Fixed
   to `uid:ID(E)` — naming the id-space column `uid` makes the importer
   store it as both the id space *and* an ordinary node property, which the
   LDBC run's own CSVs rely on the same way.
2. **Every relationship was typed by its own `rel_type` value** (`:KNOWS`,
   `:FOLLOWS`, ...) instead of the uniform `:EV` the schema and every Cypher
   text assume, because `csv_export.py`'s `:TYPE` column echoed `rel_type`
   instead of the literal string `"EV"`. Fixed; `rel_type` stays an ordinary
   property.
3. **F4's `diff_snapshots` Cypher had a bare `WHERE` after two chained
   `MATCH` clauses inside a `CALL` subquery** (`CypherSyntaxError`) — Cypher
   requires a free-standing `WHERE` to follow a `WITH`, not another `MATCH`.
   Fixed by inserting `WITH v1,v2`/`WITH e1,e2` before the filter in both
   `props_changed` sub-queries.
4. (Minor, caught before the live run) F5's Cypher did not return `stride`,
   which `neighborhood_evolution`'s own payload always includes — added
   `$stride AS stride` to the `RETURN`.

`test_csv_export.py::test_entities_header_names_the_id_column_uid` and
`::test_edge_versions_type_column_is_always_literal_ev` are regression
tests for (1) and (2).

### 7d. Smoke run and the calibrated estimate — NOT obtained; blocked

The task's validation item 4 (a smoke run on the real
`collegemsg/c3/none/seed 0` cell, reporting per-burst recompute wall to
calibrate the memo's 40–160 h serial estimate, and an N = 10,000 probe
memory estimate from that smoke) **could not be produced**, for the same
reason as 7b: no finished X1 export exists to smoke-test. The tiny
synthetic cell in 7b is 5 nodes / 7 edge versions — its wall times
(milliseconds) say nothing about N ≈ 900–1,000 or N = 10,000 behavior and
are not offered as a substitute calibration.

**The memo's own estimate (§2.9) stands, unrevised by this lane**: cheap
families (F1–F3, F5–F8, F13) < 5 s total per burst; F4 ≈ 72 × 0.15 s ≈ 10 s;
F9/F10 ≈ 135 × 0.2–2 s; F11 ≈ 70 × 10–30 rounds × ≈ 20 ms; F12 ≈ 27–65 ×
1–30 s ⇒ **≈ 100–800 s per burst** at N ≈ 900 (TGMS iTiger global recompute
133–212 s); 21 passes per cell ≈ 1–4.5 h; 36 cells ≈ **40–160 h serial**;
probe (8,922 artifacts, 6 passes) ≈ **5–15 h**. The N = 10,000 probe's memory
footprint (memo §4.5: data ≈ 100 MB, so page cache is not the risk; query
heap is, bounded by `$lim`/counted-without-`collect` and the 8g heap) has
not been independently re-estimated here either, for the same reason.

**This lane's finding, to report rather than fix:** X1's own export of the
exact smoke cell the go/no-go checklist names (`collegemsg/c3/none/seed 0`)
is currently failing ("mix starved", 0/20 batches realized). Until X1
delivers a finished cell for this store/mix/seed, item 4 (and the real
13-family shape test on real data, item 2) cannot be run by this lane or
by anyone downstream of X1.

## 8. Known gaps / not yet wired up

- The per-cell structural check after `database load`
  (`version_table_sha256` recomputed from Neo4j vs. the export's epoch-0
  value, memo §2.2/§2.5) is described in `loader.py`'s docstrings but not
  yet implemented as code — `C1`'s checker (`ext_check_oracle.py`, lane C1)
  is the natural place for the shared digest routine; this package does not
  duplicate it.
- Host-sample fields in `result.json` (`loadavg1` before/after, Neo4j JVM
  RSS, memo §2.7/§4.1) are in the schema but `record.machine_snapshot` only
  fills `host`/`platform`/`cpus`/`ram_gb` today; wiring the per-burst
  co-tenant sample into `runner.py` is left for the timed-run lane, since it
  needs a decision on sampling cadence this memo does not fix.
- `cli.py`'s `--timed` flag is a soft reminder, not an enforced gate — see
  its own docstring. Software cannot verify "the coordinator's go-ahead and
  the run order were honored"; a human (the coordinator) must.

## 9. Contradictions / things that stopped this lane (none)

Nothing in the memo or the pre-registration itself was found to be
inconsistent while implementing this package — the one real blocker (X1's
export not landing) is an upstream dependency failure, not a defect in the
frozen text, and is reported above rather than worked around.
