"""Kùzu storage adapter (primary backend, spec WP1.1).

Kùzu has no native versioning, so versions are rows (NodeVersion node table,
EdgeVersion rel table). Row-at-a-time writes are acceptable for the update
path; bulk ingest via COPY is an M3 optimization (see R1 fallback decision).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import kuzu
import numpy as np

from tgms.core.errors import NotFoundError
from tgms.core.model import OPEN_END, EdgeVersion, NodeVersion, canonical_json, clamp_tt
from tgms.storage.base import StorageAdapter

_DDL = [
    """CREATE NODE TABLE IF NOT EXISTS Entity(
        uid STRING, label STRING, dense_id INT64, PRIMARY KEY(uid))""",
    """CREATE NODE TABLE IF NOT EXISTS NodeVersion(
        vid STRING, uid STRING, label STRING,
        vt_s INT64, vt_e INT64, tt_s INT64, tt_e INT64,
        props STRING, PRIMARY KEY(vid))""",
    """CREATE REL TABLE IF NOT EXISTS OF_ENTITY(FROM NodeVersion TO Entity)""",
    """CREATE REL TABLE IF NOT EXISTS EdgeVersion(
        FROM Entity TO Entity,
        eid STRING, vid STRING, rel_type STRING, disc STRING,
        vt_s INT64, vt_e INT64, tt_s INT64, tt_e INT64,
        props STRING)""",
]

_NODE_RET = "v.vid, v.uid, v.label, v.vt_s, v.vt_e, v.tt_s, v.tt_e, v.props"
_EDGE_RET = ("e.eid, e.vid, a.uid, b.uid, e.rel_type, e.disc, "
             "e.vt_s, e.vt_e, e.tt_s, e.tt_e, e.props")


class KuzuAdapter(StorageAdapter):
    def __init__(self, path: str | Path) -> None:
        self.db = kuzu.Database(str(path))
        self.conn = kuzu.Connection(self.db)
        for stmt in _DDL:
            self.conn.execute(stmt)
        rows = self._rows("MATCH (e:Entity) RETURN e.uid, e.dense_id ORDER BY e.dense_id")
        self._ids: dict[str, int] = {u: i for u, i in rows}
        self._uid_list: list[str] = [u for u, _ in rows]

    def close(self) -> None:
        self.conn.close()
        self.db.close()

    def _rows(self, query: str, params: dict[str, Any] | None = None) -> list[tuple]:
        res = self.conn.execute(query, parameters=params or {})
        out = []
        while res.has_next():
            out.append(tuple(res.get_next()))
        return out

    # --- batch transactions ---------------------------------------------- #

    def begin(self) -> None:
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")
        rows = self._rows("MATCH (e:Entity) RETURN e.uid, e.dense_id ORDER BY e.dense_id")
        self._ids = {u: i for u, i in rows}
        self._uid_list = [u for u, _ in rows]

    # --- entities ---------------------------------------------------------- #

    def ensure_entities(self, uid_labels: Iterable[tuple[str, str]]) -> None:
        for uid, label in uid_labels:
            if uid in self._ids:
                continue
            did = len(self._uid_list)
            self.conn.execute(
                "CREATE (:Entity {uid: $uid, label: $label, dense_id: $did})",
                parameters={"uid": uid, "label": label, "did": did})
            self._ids[uid] = did
            self._uid_list.append(uid)

    def dense_ids(self, uids: Sequence[str]) -> np.ndarray:
        try:
            return np.fromiter((self._ids[u] for u in uids), dtype=np.int64, count=len(uids))
        except KeyError as e:
            raise NotFoundError(f"unknown uid: {e.args[0]}", uid=e.args[0]) from None

    def uids_for(self, ids: Sequence[int]) -> list[str]:
        return [self._uid_list[i] for i in ids]

    def num_entities(self) -> int:
        return len(self._uid_list)

    # --- version writes ------------------------------------------------------ #

    def insert_node_versions(self, rows: Sequence[NodeVersion]) -> None:
        for v in rows:
            self.conn.execute(
                "CREATE (nv:NodeVersion {vid: $vid, uid: $uid, label: $label, "
                "vt_s: $vt_s, vt_e: $vt_e, tt_s: $tt_s, tt_e: $tt_e, props: $props}) "
                "WITH nv MATCH (e:Entity {uid: $uid}) CREATE (nv)-[:OF_ENTITY]->(e)",
                parameters={"vid": v.vid, "uid": v.uid, "label": v.label,
                            "vt_s": v.vt_s, "vt_e": v.vt_e, "tt_s": v.tt_s, "tt_e": v.tt_e,
                            "props": canonical_json(v.props)})

    def insert_edge_versions(self, rows: Sequence[EdgeVersion]) -> None:
        for v in rows:
            self.conn.execute(
                "MATCH (a:Entity {uid: $src}), (b:Entity {uid: $dst}) "
                "CREATE (a)-[:EdgeVersion {eid: $eid, vid: $vid, rel_type: $rel_type, "
                "disc: $disc, vt_s: $vt_s, vt_e: $vt_e, tt_s: $tt_s, tt_e: $tt_e, "
                "props: $props}]->(b)",
                parameters={"src": v.src, "dst": v.dst, "eid": v.eid, "vid": v.vid,
                            "rel_type": v.rel_type, "disc": v.disc,
                            "vt_s": v.vt_s, "vt_e": v.vt_e, "tt_s": v.tt_s, "tt_e": v.tt_e,
                            "props": canonical_json(v.props)})

    def close_node_versions(self, vids: Sequence[str], tt_e: int) -> None:
        for vid in vids:
            self.conn.execute(
                "MATCH (v:NodeVersion {vid: $vid}) SET v.tt_e = $tt",
                parameters={"vid": vid, "tt": tt_e})

    def close_edge_versions(self, vids: Sequence[str], tt_e: int) -> None:
        for vid in vids:
            self.conn.execute(
                "MATCH ()-[e:EdgeVersion {vid: $vid}]->() SET e.tt_e = $tt",
                parameters={"vid": vid, "tt": tt_e})

    # --- version reads --------------------------------------------------------- #

    def believed_node_versions(self, uid: str, as_of_tt: int = OPEN_END) -> list[NodeVersion]:
        a = clamp_tt(as_of_tt)
        rows = self._rows(
            f"MATCH (v:NodeVersion) WHERE v.uid = $uid AND v.tt_s <= $a AND $a < v.tt_e "
            f"RETURN {_NODE_RET} ORDER BY v.vt_s",
            {"uid": uid, "a": a})
        return [_node_from_row(r) for r in rows]

    def believed_edge_versions(self, eid: str, as_of_tt: int = OPEN_END) -> list[EdgeVersion]:
        a = clamp_tt(as_of_tt)
        rows = self._rows(
            f"MATCH (a:Entity)-[e:EdgeVersion]->(b:Entity) "
            f"WHERE e.eid = $eid AND e.tt_s <= $a AND $a < e.tt_e "
            f"RETURN {_EDGE_RET} ORDER BY e.vt_s",
            {"eid": eid, "a": a})
        return [_edge_from_row(r) for r in rows]

    def all_node_versions(self) -> Iterable[NodeVersion]:
        rows = self._rows(f"MATCH (v:NodeVersion) RETURN {_NODE_RET}")
        return (_node_from_row(r) for r in rows)

    def all_edge_versions(self) -> Iterable[EdgeVersion]:
        rows = self._rows(f"MATCH (a:Entity)-[e:EdgeVersion]->(b:Entity) RETURN {_EDGE_RET}")
        return (_edge_from_row(r) for r in rows)

    # --- columnar read path -------------------------------------------------------- #

    def edges_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
        rel_types: Sequence[str] | None = None,
        columns: Sequence[str] | None = None,
        touching_ids: Sequence[int] | None = None,
    ) -> dict[str, np.ndarray]:
        a = clamp_tt(as_of_tt)
        int_cols = tuple(c for c in self.EDGE_INT_COLS
                         if columns is None or c in columns)
        str_cols = tuple(c for c in self.EDGE_STR_COLS
                         if columns is None or c in columns)
        exprs = {"src_id": "x.dense_id", "dst_id": "y.dense_id", "vt_s": "e.vt_s",
                 "vt_e": "e.vt_e", "eid": "e.eid", "vid": "e.vid",
                 "rel_type": "e.rel_type"}
        where = ["e.tt_s <= $a", "$a < e.tt_e"]
        params: dict[str, Any] = {"a": a}
        if vt_min is not None:
            where.append("e.vt_e > $vt_min")
            params["vt_min"] = vt_min
        if vt_max is not None:
            where.append("e.vt_s < $vt_max")
            params["vt_max"] = vt_max
        if rel_types is not None:
            where.append("e.rel_type IN $rel_types")
            params["rel_types"] = list(rel_types)
        if touching_ids is not None:
            where.append("(x.dense_id IN $touch OR y.dense_id IN $touch)")
            params["touch"] = [int(i) for i in touching_ids]
        select = ", ".join(f"{exprs[c]} AS {c}" for c in int_cols + str_cols)
        order = "e.vt_s, e.vid"
        res = self.conn.execute(
            "MATCH (x:Entity)-[e:EdgeVersion]->(y:Entity) "
            f"WHERE {' AND '.join(where)} RETURN {select} ORDER BY {order}",
            parameters=params)
        tbl = res.get_as_arrow()
        return _arrow_to_soa(tbl, int_cols=int_cols, str_cols=str_cols)

    def nodes_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
    ) -> dict[str, np.ndarray]:
        a = clamp_tt(as_of_tt)
        where = ["v.tt_s <= $a", "$a < v.tt_e"]
        params: dict[str, Any] = {"a": a}
        if vt_min is not None:
            where.append("v.vt_e > $vt_min")
            params["vt_min"] = vt_min
        if vt_max is not None:
            where.append("v.vt_s < $vt_max")
            params["vt_max"] = vt_max
        res = self.conn.execute(
            "MATCH (v:NodeVersion)-[:OF_ENTITY]->(e:Entity) "
            f"WHERE {' AND '.join(where)} "
            "RETURN e.dense_id AS uid_id, v.vt_s AS vt_s, v.vt_e AS vt_e, "
            "v.uid AS uid, v.vid AS vid, v.label AS label "
            "ORDER BY vt_s, vid",
            parameters=params)
        tbl = res.get_as_arrow()
        return _arrow_to_soa(tbl, int_cols=("uid_id", "vt_s", "vt_e"),
                             str_cols=("uid", "vid", "label"))

    def props_for_vids(self, kind: str, vids: Sequence[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for vid in vids:
            if kind == "node":
                rows = self._rows("MATCH (v:NodeVersion {vid: $vid}) RETURN v.props",
                                  {"vid": vid})
            else:
                rows = self._rows("MATCH ()-[e:EdgeVersion {vid: $vid}]->() RETURN e.props",
                                  {"vid": vid})
            if rows:
                out[vid] = json.loads(rows[0][0])
        return out

    # --- stats ------------------------------------------------------------------------ #

    def stats(self) -> dict[str, Any]:
        n_nodes = self._rows("MATCH (v:NodeVersion) RETURN count(*)")[0][0]
        n_edges = self._rows("MATCH ()-[e:EdgeVersion]->() RETURN count(*)")[0][0]
        extent = self._rows(
            "MATCH ()-[e:EdgeVersion]->() RETURN min(e.vt_s), "
            "max(CASE WHEN e.vt_e >= $oe THEN e.vt_s + 1 ELSE e.vt_e END)",
            {"oe": OPEN_END})
        rel_counts = dict(self._rows(
            "MATCH ()-[e:EdgeVersion]->() RETURN e.rel_type, count(*)"))
        deg = self._rows(
            "MATCH (a:Entity)-[e:EdgeVersion]->() RETURN a.uid, count(*) AS c "
            "ORDER BY c DESC LIMIT 1")
        return {
            "n_entities": self.num_entities(),
            "n_node_versions": n_nodes,
            "n_edge_versions": n_edges,
            "vt_min": extent[0][0] if extent else None,
            "vt_max": extent[0][1] if extent else None,
            "rel_type_counts": rel_counts,
            "max_out_degree": deg[0][1] if deg else 0,
        }


def _node_from_row(r: tuple) -> NodeVersion:
    vid, uid, label, vt_s, vt_e, tt_s, tt_e, props = r
    return NodeVersion(vid=vid, uid=uid, label=label, vt_s=vt_s, vt_e=vt_e,
                       tt_s=tt_s, tt_e=tt_e, props=json.loads(props))


def _edge_from_row(r: tuple) -> EdgeVersion:
    eid, vid, src, dst, rel_type, disc, vt_s, vt_e, tt_s, tt_e, props = r
    return EdgeVersion(eid=eid, vid=vid, src=src, dst=dst, rel_type=rel_type, disc=disc or "",
                       vt_s=vt_s, vt_e=vt_e, tt_s=tt_s, tt_e=tt_e, props=json.loads(props))


def _arrow_to_soa(tbl, int_cols: tuple[str, ...], str_cols: tuple[str, ...]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for c in int_cols:
        out[c] = tbl.column(c).to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    for c in str_cols:
        out[c] = np.asarray(tbl.column(c).to_pylist(), dtype=object)
    return out
