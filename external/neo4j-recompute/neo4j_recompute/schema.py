"""Property-graph schema (design memo §2.1) and the per-cell `neo4j.conf`
(§2.7), verbatim as text so the README can quote exactly what runs.
"""
from __future__ import annotations

from .canon import OPEN_END

#: Belief at the current transaction time: `x.tt_e = $O`. Every registered
#: storm artifact reads at `as_of_tt = OPEN_END` (default), so every query in
#: `queries.py` filters on this constant rather than taking `as_of_tt` as a
#: live parameter.
O = OPEN_END

#: DDL, run once per fresh database after CSV import, before the epoch-0 pass.
#: Order matters only for the uniqueness constraint (cheap, run first); every
#: index after it is a plain range index and order among them does not.
DDL_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT e_uid IF NOT EXISTS FOR (x:E) REQUIRE x.uid IS UNIQUE",
    "CREATE RANGE INDEX nv_uid IF NOT EXISTS FOR (v:NV) ON (v.uid)",
    "CREATE RANGE INDEX nv_vid IF NOT EXISTS FOR (v:NV) ON (v.vid)",
    "CREATE RANGE INDEX nv_vt_s IF NOT EXISTS FOR (v:NV) ON (v.vt_s)",
    "CREATE RANGE INDEX nv_tt_e IF NOT EXISTS FOR (v:NV) ON (v.tt_e)",
    "CREATE RANGE INDEX ev_vid IF NOT EXISTS FOR ()-[e:EV]-() ON (e.vid)",
    "CREATE RANGE INDEX ev_eid IF NOT EXISTS FOR ()-[e:EV]-() ON (e.eid)",
    "CREATE RANGE INDEX ev_vt_s IF NOT EXISTS FOR ()-[e:EV]-() ON (e.vt_s)",
    "CREATE RANGE INDEX ev_tt_e IF NOT EXISTS FOR ()-[e:EV]-() ON (e.tt_e)",
    "CREATE RANGE INDEX ev_rel_type IF NOT EXISTS FOR ()-[e:EV]-() ON (e.rel_type)",
)

#: Node-version columns, in CSV-header order (memo §2.1; `props` is a
#: canonical-JSON string — Neo4j has no map properties).
NV_COLUMNS: tuple[str, ...] = (
    "vid", "uid", "label", "vt_s", "vt_e", "tt_s", "tt_e",
    "props", "source", "provenance_ref",
)
#: Edge-version columns on the `(:E)-[:EV]->(:E)` relationship.
EV_COLUMNS: tuple[str, ...] = (
    "eid", "vid", "rel_type", "disc", "vt_s", "vt_e", "tt_s", "tt_e",
    "props", "source", "provenance_ref",
)
#: Integer (64-bit) columns, typed `:long` in the import header files.
LONG_COLUMNS: frozenset[str] = frozenset({"vt_s", "vt_e", "tt_s", "tt_e"})


def conf_lines(data_dir: str, logs_dir: str, bolt_addr: str = "127.0.0.1:7687",
               http_addr: str = "127.0.0.1:7475") -> str:
    """The `neo4j.conf` this lane runs with (memo §2.7: "the LDBC values").

    Base lines copied verbatim from the LDBC reference install's working
    `neo4j.conf` on xzgpu (`/mnt/project/xzhang/neo4j/neo4j-community-5.26.0/
    conf/neo4j.conf`, sha256 recorded in `benchmarks/ldbc-ref-v1/`), with
    only `server.directories.{data,logs}` and the bolt/http listen addresses
    changed to this lane's own `conf-ext-v1` paths so the two Neo4j
    deployments never collide on disk or on a port.
    """
    return "\n".join([
        "server.directories.import=import",
        "server.bolt.enabled=true",
        "server.http.enabled=true",
        "server.https.enabled=false",
        "db.tx_log.rotation.retention_policy=2 days 2G",
        "server.jvm.additional=-XX:+UseG1GC",
        "server.jvm.additional=-XX:-OmitStackTraceInFastThrow",
        "server.jvm.additional=-XX:+AlwaysPreTouch",
        "server.jvm.additional=-XX:+UnlockExperimentalVMOptions",
        "server.jvm.additional=-XX:+TrustFinalNonStaticFields",
        "server.jvm.additional=-XX:+DisableExplicitGC",
        "server.jvm.additional=-Djdk.nio.maxCachedBufferSize=1024",
        "server.jvm.additional=-Dio.netty.tryReflectionSetAccessible=true",
        "server.jvm.additional=-Djdk.tls.ephemeralDHKeySize=2048",
        "server.jvm.additional=-Djdk.tls.rejectClientInitiatedRenegotiation=true",
        "server.jvm.additional=-XX:FlightRecorderOptions=stackdepth=256",
        "server.jvm.additional=-XX:+UnlockDiagnosticVMOptions",
        "server.jvm.additional=-XX:+DebugNonSafepoints",
        "server.jvm.additional=--add-opens=java.base/java.nio=ALL-UNNAMED",
        "server.jvm.additional=--add-opens=java.base/java.io=ALL-UNNAMED",
        "server.jvm.additional=--add-opens=java.base/sun.nio.ch=ALL-UNNAMED",
        "server.jvm.additional=--enable-native-access=ALL-UNNAMED",
        "server.jvm.additional=-Dlog4j2.disable.jmx=true",
        "server.jvm.additional=-Dlog4j.layout.jsonTemplate.maxStringLength=32768",
        "server.windows_service_name=neo4j",
        f"server.directories.data={data_dir}",
        f"server.directories.logs={logs_dir}",
        f"server.bolt.listen_address={bolt_addr}",
        f"server.http.listen_address={http_addr}",
        "dbms.security.auth_enabled=false",
        "server.memory.heap.initial_size=8g",
        "server.memory.heap.max_size=8g",
        "server.memory.pagecache.size=16g",
        "db.transaction.timeout=600s",
        "dbms.security.procedures.unrestricted=apoc.*",
        "",
    ])
