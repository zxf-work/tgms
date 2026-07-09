"""DuckDB storage adapter (fallback backend, spec R1 — same ABC as Kùzu).

Plain relational tables; version rows are append-mostly (the only UPDATE is
closing tt_e). Columnar reads go engine → Arrow → NumPy with no Python-level
row loops.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import numpy as np

from tgms.core.errors import NotFoundError
from tgms.core.model import OPEN_END, EdgeVersion, NodeVersion, canonical_json, clamp_tt
from tgms.storage.base import StorageAdapter

_DDL = """
CREATE TABLE IF NOT EXISTS entities(
  dense_id BIGINT NOT NULL,
  uid VARCHAR PRIMARY KEY,
  label VARCHAR);
CREATE TABLE IF NOT EXISTS node_versions(
  vid VARCHAR PRIMARY KEY,
  uid VARCHAR NOT NULL, uid_id BIGINT NOT NULL, label VARCHAR,
  vt_s BIGINT, vt_e BIGINT, tt_s BIGINT, tt_e BIGINT,
  props VARCHAR);
CREATE TABLE IF NOT EXISTS edge_versions(
  vid VARCHAR PRIMARY KEY,
  eid VARCHAR NOT NULL,
  src VARCHAR NOT NULL, dst VARCHAR NOT NULL,
  src_id BIGINT NOT NULL, dst_id BIGINT NOT NULL,
  rel_type VARCHAR NOT NULL, disc VARCHAR,
  vt_s BIGINT, vt_e BIGINT, tt_s BIGINT, tt_e BIGINT,
  props VARCHAR);
"""


class DuckDBAdapter(StorageAdapter):
    def __init__(self, path: str | Path = ":memory:", threads: int | None = None) -> None:
        self.conn = duckdb.connect(str(path))
        if threads:
            self.conn.execute(f"SET threads = {int(threads)}")
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                self.conn.execute(stmt)
        # dense-id dictionary cache (uid <-> int64), loaded once at open
        rows = self.conn.execute("SELECT uid, dense_id FROM entities ORDER BY dense_id").fetchall()
        self._ids: dict[str, int] = {u: i for u, i in rows}
        self._uid_list: list[str] = [u for u, _ in rows]

    def close(self) -> None:
        self.conn.close()

    # --- batch transactions ---------------------------------------------- #

    def begin(self) -> None:
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")
        # resync the dense-id cache with the (rolled-back) entities table
        rows = self.conn.execute("SELECT uid, dense_id FROM entities ORDER BY dense_id").fetchall()
        self._ids = {u: i for u, i in rows}
        self._uid_list = [u for u, _ in rows]

    # --- entities ------------------------------------------------------- #

    def ensure_entities(self, uid_labels: Iterable[tuple[str, str]]) -> None:
        new = [(u, l) for u, l in uid_labels if u not in self._ids]
        if not new:
            return
        seen: dict[str, str] = {}
        for u, l in new:  # dedupe, keep first label
            if u not in seen:
                seen[u] = l
        rows = []
        for u, l in seen.items():
            did = len(self._uid_list)
            self._ids[u] = did
            self._uid_list.append(u)
            rows.append((did, u, l))
        self.conn.executemany("INSERT INTO entities VALUES (?, ?, ?)", rows)

    def dense_ids(self, uids: Sequence[str]) -> np.ndarray:
        try:
            return np.fromiter((self._ids[u] for u in uids), dtype=np.int64, count=len(uids))
        except KeyError as e:
            raise NotFoundError(f"unknown uid: {e.args[0]}", uid=e.args[0]) from None

    def uids_for(self, ids: Sequence[int]) -> list[str]:
        return [self._uid_list[i] for i in ids]

    def num_entities(self) -> int:
        return len(self._uid_list)

    # --- version writes -------------------------------------------------- #

    def insert_node_versions(self, rows: Sequence[NodeVersion]) -> None:
        if not rows:
            return
        self.conn.executemany(
            "INSERT INTO node_versions VALUES (?,?,?,?,?,?,?,?,?)",
            [(v.vid, v.uid, self._ids[v.uid], v.label, v.vt_s, v.vt_e, v.tt_s, v.tt_e,
              canonical_json(v.props)) for v in rows])

    def insert_edge_versions(self, rows: Sequence[EdgeVersion]) -> None:
        if not rows:
            return
        self.conn.executemany(
            "INSERT INTO edge_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(v.vid, v.eid, v.src, v.dst, self._ids[v.src], self._ids[v.dst], v.rel_type,
              v.disc, v.vt_s, v.vt_e, v.tt_s, v.tt_e, canonical_json(v.props)) for v in rows])

    def close_node_versions(self, vids: Sequence[str], tt_e: int) -> None:
        self.conn.executemany(
            "UPDATE node_versions SET tt_e = ? WHERE vid = ?", [(tt_e, v) for v in vids])

    def close_edge_versions(self, vids: Sequence[str], tt_e: int) -> None:
        self.conn.executemany(
            "UPDATE edge_versions SET tt_e = ? WHERE vid = ?", [(tt_e, v) for v in vids])

    # --- version reads ---------------------------------------------------- #

    _NODE_COLS = "vid, uid, label, vt_s, vt_e, tt_s, tt_e, props"
    _EDGE_COLS = "eid, vid, src, dst, rel_type, disc, vt_s, vt_e, tt_s, tt_e, props"

    def believed_node_versions(self, uid: str, as_of_tt: int = OPEN_END) -> list[NodeVersion]:
        as_of_tt = clamp_tt(as_of_tt)
        rows = self.conn.execute(
            f"SELECT {self._NODE_COLS} FROM node_versions "
            "WHERE uid = ? AND tt_s <= ? AND ? < tt_e ORDER BY vt_s",
            (uid, as_of_tt, as_of_tt)).fetchall()
        return [_node_from_row(r) for r in rows]

    def believed_edge_versions(self, eid: str, as_of_tt: int = OPEN_END) -> list[EdgeVersion]:
        as_of_tt = clamp_tt(as_of_tt)
        rows = self.conn.execute(
            f"SELECT {self._EDGE_COLS} FROM edge_versions "
            "WHERE eid = ? AND tt_s <= ? AND ? < tt_e ORDER BY vt_s",
            (eid, as_of_tt, as_of_tt)).fetchall()
        return [_edge_from_row(r) for r in rows]

    def all_node_versions(self) -> Iterable[NodeVersion]:
        rows = self.conn.execute(
            f"SELECT {self._NODE_COLS} FROM node_versions").fetchall()
        return (_node_from_row(r) for r in rows)

    def all_edge_versions(self) -> Iterable[EdgeVersion]:
        rows = self.conn.execute(
            f"SELECT {self._EDGE_COLS} FROM edge_versions").fetchall()
        return (_edge_from_row(r) for r in rows)

    # --- columnar read path ------------------------------------------------ #

    def edges_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
        rel_types: Sequence[str] | None = None,
    ) -> dict[str, np.ndarray]:
        as_of_tt = clamp_tt(as_of_tt)
        where = ["tt_s <= ? AND ? < tt_e"]
        params: list[Any] = [as_of_tt, as_of_tt]
        if vt_min is not None:
            where.append("vt_e > ?")
            params.append(vt_min)
        if vt_max is not None:
            where.append("vt_s < ?")
            params.append(vt_max)
        if rel_types is not None:
            where.append(f"rel_type IN ({','.join('?' * len(rel_types))})")
            params.extend(rel_types)
        tbl = self.conn.execute(
            "SELECT src_id, dst_id, vt_s, vt_e, eid, vid, rel_type FROM edge_versions "
            f"WHERE {' AND '.join(where)} ORDER BY vt_s, vid", params).to_arrow_table()
        return _arrow_to_soa(tbl, int_cols=("src_id", "dst_id", "vt_s", "vt_e"),
                             str_cols=("eid", "vid", "rel_type"))

    def nodes_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
    ) -> dict[str, np.ndarray]:
        as_of_tt = clamp_tt(as_of_tt)
        where = ["tt_s <= ? AND ? < tt_e"]
        params: list[Any] = [as_of_tt, as_of_tt]
        if vt_min is not None:
            where.append("vt_e > ?")
            params.append(vt_min)
        if vt_max is not None:
            where.append("vt_s < ?")
            params.append(vt_max)
        tbl = self.conn.execute(
            "SELECT uid_id, vt_s, vt_e, uid, vid, label FROM node_versions "
            f"WHERE {' AND '.join(where)} ORDER BY vt_s, vid", params).to_arrow_table()
        return _arrow_to_soa(tbl, int_cols=("uid_id", "vt_s", "vt_e"),
                             str_cols=("uid", "vid", "label"))

    def props_for_vids(self, kind: str, vids: Sequence[str]) -> dict[str, dict]:
        table = "node_versions" if kind == "node" else "edge_versions"
        if not vids:
            return {}
        rows = self.conn.execute(
            f"SELECT vid, props FROM {table} WHERE vid IN ({','.join('?' * len(vids))})",
            list(vids)).fetchall()
        return {vid: json.loads(props) for vid, props in rows}

    # --- stats -------------------------------------------------------------- #

    def stats(self) -> dict[str, Any]:
        n_nodes, n_edges = (
            self.conn.execute("SELECT count(*) FROM node_versions").fetchone()[0],
            self.conn.execute("SELECT count(*) FROM edge_versions").fetchone()[0])
        extent = self.conn.execute(
            "SELECT min(vt_s), max(CASE WHEN vt_e >= ? THEN vt_s + 1 ELSE vt_e END) "
            "FROM edge_versions", (OPEN_END,)).fetchone()
        rel_counts = dict(self.conn.execute(
            "SELECT rel_type, count(*) FROM edge_versions GROUP BY rel_type").fetchall())
        max_deg = self.conn.execute(
            "SELECT coalesce(max(c), 0) FROM (SELECT count(*) AS c FROM edge_versions "
            "GROUP BY src_id)").fetchone()[0]
        return {
            "n_entities": self.num_entities(),
            "n_node_versions": n_nodes,
            "n_edge_versions": n_edges,
            "vt_min": extent[0], "vt_max": extent[1],
            "rel_type_counts": rel_counts,
            "max_out_degree": max_deg,
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
