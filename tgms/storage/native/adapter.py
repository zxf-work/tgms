"""StorageAdapter over the native Rust engine (D-028).

This module is deliberately mechanical: it converts between the dataclasses
`tgms.core.model` defines and the columnar dicts `tgms._engine` speaks, and
translates error types. There is no storage logic here at all — if something
needs deciding, it belongs in `crates/tgms-engine-core`. The bi-temporal
semantics (interval carving, disjointness, replay determinism) live once in
`StorageAdapter` and are inherited unchanged, which is exactly why both
backends can be held to the same oracle.

Every row-moving call crosses the boundary once, as a dict of columns rather
than a list of records, so an ingest chunk of 50,000 events is one crossing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from tgms.core.errors import InvalidArgError, NotFoundError, StateError
from tgms.core.model import OPEN_END, EdgeVersion, NodeVersion, canonical_json, clamp_tt
from tgms.storage.base import StorageAdapter

try:
    from tgms import _engine
except ImportError as e:  # pragma: no cover - packaging failure, not a code path
    raise ImportError(
        "the native backend needs the compiled tgms._engine extension. "
        "Install a wheel (`pip install tgms`), or build from source with a "
        "Rust toolchain (`uv sync --reinstall-package tgms`)."
    ) from e


def _translate(e: Exception) -> Exception:
    """Map an engine exception onto the TGMS error taxonomy.

    The Rust layer keeps its category as the message prefix precisely so this
    can be exact rather than a guess at the prose.
    """
    msg = str(e).strip("'\"")
    if isinstance(e, KeyError) or msg.startswith("not_found:"):
        return NotFoundError(msg)
    if isinstance(e, OverflowError) or msg.startswith("capacity:"):
        return InvalidArgError(msg)
    return StateError(msg)


class NativeAdapter(StorageAdapter):
    """Bi-temporal store backed by the native engine.

    Unlike the DuckDB adapter there is no in-memory mode: a store is a
    directory, because immutable segment files plus an atomic manifest swap
    are what give snapshot isolation and crash recovery.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        try:
            self._store = _engine.NativeStore(str(self.path))
        except Exception as e:
            raise _translate(e) from None

    def close(self) -> None:
        self._store.close()

    # --- batch transactions ---------------------------------------------- #
    #
    # `Store._write` brackets every batch, and the engine needs the same
    # bracket because a batch is exactly one published generation. The
    # transaction time is taken from the first row written in the batch:
    # `apply_ops` stamps every row it produces with the batch's tt.

    def apply_ops(self, ops: Sequence[dict[str, Any]], tt: int) -> None:
        """Open the engine batch at the authoritative transaction time.

        `apply_ops` is the only caller that knows the batch's `tt` up front —
        individual writes only carry it per row, and `ensure_entities` does
        not carry it at all. Everything else is the inherited bi-temporal
        semantics, unchanged.
        """
        self._ensure_batch(tt)
        super().apply_ops(ops, tt)

    def begin(self) -> None:
        # the engine batch opens at the first apply_ops/write, when tt is known
        pass

    def commit(self) -> None:
        if not self._store.in_batch():
            return  # a batch that wrote nothing publishes nothing
        try:
            self._store.commit()
        except Exception as e:
            raise _translate(e) from None

    def rollback(self) -> None:
        if not self._store.in_batch():
            return
        try:
            self._store.rollback()
        except Exception as e:
            raise _translate(e) from None

    def _ensure_batch(self, tt: int) -> None:
        """Open the engine batch lazily; `in_batch` is the source of truth."""
        if not self._store.in_batch():
            try:
                self._store.begin(tt)
            except Exception as e:
                raise _translate(e) from None

    # --- entities ---------------------------------------------------------- #

    def ensure_entities(self, uid_labels: Iterable[tuple[str, str]]) -> None:
        uids: list[str] = []
        labels: list[str] = []
        seen: set[str] = set()
        for uid, label in uid_labels:
            if uid not in seen:
                seen.add(uid)
                uids.append(uid)
                labels.append(label)
        if not uids:
            return
        try:
            self._store.ensure_entities(uids, labels)
        except Exception as e:
            raise _translate(e) from None

    def dense_ids(self, uids: Sequence[str]) -> np.ndarray:
        try:
            return self._store.dense_ids(list(uids))
        except Exception as e:
            raise _translate(e) from None

    def uids_for(self, ids: Sequence[int]) -> list[str]:
        try:
            return self._store.uids_for([int(i) for i in ids])
        except Exception as e:
            raise _translate(e) from None

    def num_entities(self) -> int:
        return int(self._store.num_entities())

    # --- version writes ----------------------------------------------------- #

    def insert_node_versions(self, rows: Sequence[NodeVersion]) -> None:
        if not rows:
            return
        self._ensure_batch(rows[0].tt_s)
        try:
            self._store.stage_nodes(
                {
                    "vid": [r.vid for r in rows],
                    "uid": [r.uid for r in rows],
                    "label": [r.label for r in rows],
                    "vt_s": [r.vt_s for r in rows],
                    "vt_e": [r.vt_e for r in rows],
                    "tt_s": [r.tt_s for r in rows],
                    "props": [canonical_json(r.props) for r in rows],
                    "source": [r.source for r in rows],
                    "provenance_ref": [r.provenance_ref for r in rows],
                }
            )
        except Exception as e:
            raise _translate(e) from None

    def insert_edge_versions(self, rows: Sequence[EdgeVersion]) -> None:
        if not rows:
            return
        self._ensure_batch(rows[0].tt_s)
        try:
            self._store.stage_edges(
                {
                    "vid": [r.vid for r in rows],
                    "src": [r.src for r in rows],
                    "dst": [r.dst for r in rows],
                    "rel_type": [r.rel_type for r in rows],
                    "disc": [r.disc for r in rows],
                    "vt_s": [r.vt_s for r in rows],
                    "vt_e": [r.vt_e for r in rows],
                    "tt_s": [r.tt_s for r in rows],
                    "props": [canonical_json(r.props) for r in rows],
                    "source": [r.source for r in rows],
                    "provenance_ref": [r.provenance_ref for r in rows],
                }
            )
        except Exception as e:
            raise _translate(e) from None

    def close_node_versions(self, vids: Sequence[str], tt_e: int) -> None:
        self._close("node", vids, tt_e)

    def close_edge_versions(self, vids: Sequence[str], tt_e: int) -> None:
        self._close("edge", vids, tt_e)

    def _close(self, kind: str, vids: Sequence[str], tt_e: int) -> None:
        if not vids:
            return
        self._ensure_batch(tt_e)
        try:
            self._store.stage_closes(kind, list(vids), tt_e)
        except Exception as e:
            raise _translate(e) from None

    # --- version reads -------------------------------------------------------- #

    @staticmethod
    def _nodes(cols: dict[str, Any]) -> list[NodeVersion]:
        return [
            NodeVersion(
                vid=cols["vid"][i],
                uid=cols["uid"][i],
                label=cols["label"][i],
                vt_s=cols["vt_s"][i],
                vt_e=cols["vt_e"][i],
                tt_s=cols["tt_s"][i],
                tt_e=cols["tt_e"][i],
                props=json.loads(cols["props"][i]),
                source=cols["source"][i] or "ingest",
                provenance_ref=cols["provenance_ref"][i],
            )
            for i in range(len(cols["vid"]))
        ]

    @staticmethod
    def _edges(cols: dict[str, Any]) -> list[EdgeVersion]:
        return [
            EdgeVersion(
                eid=cols["eid"][i],
                vid=cols["vid"][i],
                src=cols["src"][i],
                dst=cols["dst"][i],
                rel_type=cols["rel_type"][i],
                disc=cols["disc"][i] or "",
                vt_s=cols["vt_s"][i],
                vt_e=cols["vt_e"][i],
                tt_s=cols["tt_s"][i],
                tt_e=cols["tt_e"][i],
                props=json.loads(cols["props"][i]),
                source=cols["source"][i] or "ingest",
                provenance_ref=cols["provenance_ref"][i],
            )
            for i in range(len(cols["vid"]))
        ]

    def believed_node_versions(self, uid: str, as_of_tt: int = OPEN_END) -> list[NodeVersion]:
        try:
            return self._nodes(self._store.believed("node", uid, clamp_tt(as_of_tt)))
        except Exception as e:
            raise _translate(e) from None

    def believed_edge_versions(self, eid: str, as_of_tt: int = OPEN_END) -> list[EdgeVersion]:
        try:
            return self._edges(self._store.believed("edge", eid, clamp_tt(as_of_tt)))
        except Exception as e:
            raise _translate(e) from None

    def nodes_with_believed_versions(
        self, uids: Sequence[str], as_of_tt: int = OPEN_END
    ) -> set[str]:
        if not uids:
            return set()
        try:
            return set(self._store.believed_any(list(uids), clamp_tt(as_of_tt)))
        except Exception as e:
            raise _translate(e) from None

    def all_node_versions(self) -> Iterable[NodeVersion]:
        return self._nodes(self._store.all_versions("node"))

    def all_edge_versions(self) -> Iterable[EdgeVersion]:
        return self._edges(self._store.all_versions("edge"))

    def props_for_vids(self, kind: str, vids: Sequence[str]) -> dict[str, dict]:
        if not vids:
            return {}
        try:
            raw = self._store.props_for_vids(kind, list(vids))
        except Exception as e:
            raise _translate(e) from None
        return {vid: json.loads(text) for vid, text in raw.items()}

    # --- columnar read path ----------------------------------------------------- #

    def edges_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
        rel_types: Sequence[str] | None = None,
        columns: Sequence[str] | None = None,
        touching_ids: Sequence[int] | None = None,
        touching_both: bool = False,
    ) -> dict[str, np.ndarray]:
        try:
            got = self._store.scan_edges(
                as_of_tt=clamp_tt(as_of_tt),
                vt_min=vt_min,
                vt_max=vt_max,
                rel_types=list(rel_types) if rel_types is not None else None,
                touching_ids=[int(i) for i in touching_ids]
                if touching_ids is not None
                else None,
                touching_both=touching_both,
                limit=None,
                # a real pushdown: unprojected string columns are never built
                columns=list(columns) if columns is not None else None,
            )
        except Exception as e:
            raise _translate(e) from None
        out: dict[str, np.ndarray] = {}
        for c in self.EDGE_INT_COLS:
            if columns is None or c in columns:
                out[c] = np.asarray(got[c], dtype=np.int64)
        for c in self.EDGE_STR_COLS:
            if columns is None or c in columns:
                out[c] = np.asarray(got[c], dtype=object)
        return out

    def nodes_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
    ) -> dict[str, np.ndarray]:
        try:
            got = self._store.scan_nodes(
                as_of_tt=clamp_tt(as_of_tt), vt_min=vt_min, vt_max=vt_max
            )
        except Exception as e:
            raise _translate(e) from None
        out = {c: np.asarray(got[c], dtype=np.int64) for c in ("uid_id", "vt_s", "vt_e")}
        out.update({c: np.asarray(got[c], dtype=object) for c in ("uid", "vid", "label")})
        return out

    # --- statistics and maintenance ------------------------------------------------ #

    def stats(self) -> dict[str, Any]:
        s = self._store.stats()
        return {
            "n_entities": int(s["n_entities"]),
            "n_node_versions": int(s["n_node_versions"]),
            "n_edge_versions": int(s["n_edge_versions"]),
            "vt_min": s["vt_min"],
            "vt_max": s["vt_max"],
            "rel_type_counts": s["rel_type_counts"],
            "max_out_degree": int(s["max_out_degree"]),
        }

    def compact(self) -> dict[str, int]:
        """Fold close runs into segment sidecars and merge runs (spec §5.6)."""
        try:
            return self._store.compact()
        except Exception as e:
            raise _translate(e) from None

    def gc(self, keep_last: int = 2) -> dict[str, int]:
        """Collect superseded generations (spec §5.6, `tgms store gc`).

        Removes manifests older than the last `keep_last` generations, then
        any segment or close-run file no retained manifest references. The
        generation `CURRENT` names and generations pinned by in-process
        readers are never touched, so this is safe to run any time no batch
        is open.
        """
        try:
            return self._store.gc(keep_last)
        except Exception as e:
            raise _translate(e) from None

    def resolve_entities(self, query: str, as_of_tt: int = OPEN_END):
        """Engine-side entity resolution (O12).

        Present only on this backend; `ops_snapshot.resolve_entities` uses it
        when available and otherwise falls back to its portable scan.
        """
        try:
            return self._store.resolve_entities(query, clamp_tt(as_of_tt))
        except Exception as e:
            raise _translate(e) from None

    def verify(self) -> dict[str, Any]:
        """Checksum-walk every file this generation references.

        Corruption has to be *detected* before bad data reaches a query — the
        engine's durability objective is that an inconsistent generation is
        never silently exposed, which is only true if something checks.
        """
        try:
            return self._store.verify()
        except Exception as e:
            raise _translate(e) from None

    def needs_compaction(self) -> bool:
        return bool(self._store.needs_compaction())

    @property
    def generation(self) -> int:
        """Current manifest generation — the store's publication counter."""
        return int(self._store.generation())
