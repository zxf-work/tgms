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

    #: The TCSR path scans integer columns plus row addresses — never eid or
    #: rel_type, which cost a sha256 / a string per *scanned* row. Traversal
    #: fetches identities afterwards for the rows that survive it, via
    #: `edge_idents_at`. Portable backends keep the base class columns.
    TCSR_COLS = ("src_id", "dst_id", "vt_s", "vt_e", "seg_id", "seg_row")

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        try:
            self._store = _engine.NativeStore(str(self.path))
        except Exception as e:
            raise _translate(e) from None

    def close(self) -> None:
        self._store.close()

    def tcsr(self):
        """The base lazy build, wrapped in generation-stamped persistence.

        The permutation is saved under `index/` (a directory neither gc nor
        verify walks) stamped with `(generation, manifest_sha)`; a stamp
        match on open replaces the two argsorts with a file read, and any
        mismatch — including a store rebuilt in place, which reuses
        generation numbers with different content — falls back to a rebuild
        and re-save (D-039). Inside an open batch nothing is loaded or
        saved: staged rows are visible to reads before the generation
        advances, so a stamp written mid-batch would lie.
        """
        if self._tcsr is not None:
            return self._tcsr
        from tgms.storage.tcsr import TemporalCSR, load_permutation, save_permutation

        cols = self.edges_columnar(columns=self.TCSR_COLS)
        n = self.num_entities()
        if self._store.in_batch():
            self._tcsr = (TemporalCSR.build(cols, n), cols)
            return self._tcsr
        path = self.path / "index" / "tcsr.npz"
        gen, sha = self._store.generation(), self._store.manifest_sha()
        csr = load_permutation(path, cols, n, gen, sha)
        if csr is None:
            csr = TemporalCSR.build(cols, n)
            save_permutation(path, csr, gen, sha)
        self._tcsr = (csr, cols)
        return self._tcsr

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

    # --- replay cursor (suffix recovery, D-042) --------------------------- #

    def event_cursor(self) -> tuple[int, str]:
        """`(offset, chain)` the current generation recorded.

        `offset` points immediately past the newline of the last applied
        event-log record; `chain` is the rolling hash over that prefix. A
        store written before cursors existed reports chain `""` — the
        caller must treat that as "no cursor", never as "nothing applied".
        """
        offset, chain = self._store.event_cursor()
        return int(offset), chain

    def note_event_cursor(self, offset: int, chain: str) -> None:
        """Stage the cursor the next commit records in its manifest."""
        self._store.set_event_cursor(int(offset), chain)

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

    def retire_node_versions(self, vids: Sequence[str]) -> None:
        self._retire("node", vids)

    def retire_edge_versions(self, vids: Sequence[str]) -> None:
        self._retire("edge", vids)

    def _retire(self, kind: str, vids: Sequence[str]) -> None:
        """Drop rows the open batch staged and has since replaced. They only
        ever exist in staging — a committed row belongs to an earlier
        transaction time and is closed, never retired — so this cannot reach
        a sealed segment."""
        if not vids:
            return
        try:
            self._store.stage_retires(kind, list(vids))
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

    def believed_edge_versions(self, eid: str, as_of_tt: int = OPEN_END, *,
                               src: str | None = None, dst: str | None = None) -> list[EdgeVersion]:
        # src/dst are performance hints for backends that can anchor on them
        # (Kùzu); the native engine's `eid` lookup is already a hash-map hit,
        # so there is nothing to anchor and the hints are ignored.
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

    def versions_page(
        self,
        kind: str,
        *,
        as_of: int = OPEN_END,
        t_a: int,
        t_b: int,
        belief: str = "current",
        rel_types: Sequence[str] | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, Any], int]:
        """The bounded override of the ABC default (D-069's compatibility
        pattern, applied to the operator D-069 left behind).

        Everything the default does over ten object arrays covering the whole
        population — mask, `(tt_s, vid)` order, count, slice — the engine does
        over 32-byte integer keys, and only the page crosses the boundary.
        `props` is not among the columns asked for, so no row is parsed out of
        JSON on the way back either; the default reached `versions_columnar`,
        which reached `all_*_versions`, which built one `props` dict per
        version of the store to answer a 50-row question.

        Peak memory is therefore the page plus the key array plus the segment
        cache budget, not ~1,060 bytes per stored version
        (`docs/design/BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md` §1, §4).
        """
        try:
            got = self._store.version_page(
                kind, clamp_tt(as_of), t_a, t_b, belief,
                list(rel_types) if rel_types is not None else None,
                offset, limit,
            )
        except Exception as e:
            raise _translate(e) from None
        # int columns arrive as int64 arrays, string columns as lists of str
        # — the same two shapes the ABC default's object arrays present at
        # `cols[c][i]`, so the row build above them is unchanged.
        cols = {c: got[c] for c in self.VERSION_COLS[kind]}
        return cols, int(got["rows_total"])

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
        # opt-in only (D-052); the engine already knows how to project these
        for c in self.EDGE_OPT_COLS:
            if columns is not None and c in columns:
                out[c] = np.asarray(got[c], dtype=object)
        # physical row addresses (native only, explicit request only): the
        # ticket for coming back to the engine for derived fields of a few
        # surviving rows without materializing them for the whole scan
        for c in ("seg_id", "seg_row"):
            if columns is not None and c in columns:
                out[c] = np.asarray(got[c], dtype=np.int64)
        return out

    def aggregate_events_columnar(
        self,
        as_of_tt: int,
        t_a: int,
        t_b: int,
        rel_types: Sequence[str] | None,
        stride: int | None,
        group_by: Sequence[tuple[str, str | None]],
        aggregates: Sequence[tuple[str, str | None]],
        max_groups: int,
    ) -> dict[str, Any]:
        """O14 fast path: the whole grouped aggregation in one engine call
        (aggregate.rs — two-phase parallel partials over the scan's
        selections, fixed-width group codes, canonical order). Returns key
        code columns plus rehydration tables; the operator pages in Python.
        """
        try:
            got = self._store.aggregate_edges(
                as_of_tt=clamp_tt(as_of_tt),
                t_a=int(t_a),
                t_b=int(t_b),
                rel_types=list(rel_types) if rel_types is not None else None,
                stride=None if stride is None else int(stride),
                group_by=[(d, r) for d, r in group_by],
                aggregates=[(a, o) for a, o in aggregates],
                max_groups=int(max_groups),
            )
        except Exception as e:
            raise _translate(e) from None
        return {
            "rows_total": int(got["rows_total"]),
            "keys": [np.asarray(k, dtype=np.int64) for k in got["keys"]],
            "aggs": got["aggs"],
            "rel_names": got["rel_names"],
            "label_names": got["label_names"],
        }

    def edge_idents_at(self, seg_ids: Sequence[int],
                       seg_rows: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        """`(eid, rel_type)` for scan addresses, in input order.

        Addresses come from a scan projecting `seg_id`/`seg_row` and are
        valid only against the generation that produced them — the engine
        refuses ids the current generation does not name.
        """
        try:
            eids, rels = self._store.edge_idents_at(
                [int(i) for i in seg_ids], [int(r) for r in seg_rows])
        except Exception as e:
            raise _translate(e) from None
        return np.asarray(eids, dtype=object), np.asarray(rels, dtype=object)

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

    def compact_current_only(self) -> dict[str, int]:
        """Strip the store to the currently believed rows (plan §13).

        Experimental configuration for the current-vs-bi-temporal overhead
        measurement: superseded versions, close runs, and sidecars are
        physically dropped and the store is stamped ``CURRENT_ONLY``, after
        which it refuses past-belief queries and corrections. Current-belief
        answers are unchanged — that equivalence is the experiment's gate.
        """
        try:
            return self._store.compact_current_only()
        except Exception as e:
            raise _translate(e) from None

    def current_only(self) -> bool:
        """Whether this store is the stripped §13 configuration."""
        return bool(self._store.current_only())

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

    def manifest_format(self) -> int:
        """On-disk manifest format: 3 for a store this build wrote, 1 or 2
        for one an earlier engine wrote, which opens read-only. Format 3
        differs from 2 only in what ``manifest_sha`` is — a Merkle root over
        the ordered segment set rather than the sha of the whole document."""
        return int(self._store.manifest_format())

    def upgrade_manifests(self) -> dict[str, Any]:
        """Convert a format-1 or format-2 store to format 3 (`tgms store
        upgrade-manifests`).

        Republishes the current generation's content as a format-3
        checkpoint and flips `CURRENT`: one manifest written, nothing else
        touched. Idempotent — `upgraded` is False if the store is already
        format 3. The generation advances by one and `manifest_sha` changes
        (the format field is inside its preimage, and from format 3 the
        digest is a different function entirely), so a persisted TCSR stamped
        against the old generation rebuilds on next use, which is exactly
        what that stamp is for.
        """
        try:
            return self._store.upgrade_manifests()
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

    def verify(self, mode: str = "fast") -> dict[str, Any]:
        """Check this store's integrity. Read-only, in both modes.

        Corruption has to be *detected* before bad data reaches a query — the
        engine's durability objective is that an inconsistent generation is
        never silently exposed, which is only true if something checks.

        `mode="fast"` is the engine's file walk: every segment, close run,
        dictionary and manifest this generation names, checksummed and
        cross-checked against what the manifest claims. It is bounded by the
        bytes on disk and is what `tgms store verify` has always run.

        `mode="full"` adds everything that spans files or lives outside the
        engine's own directory:

        * the engine's own extra passes — the manifest parent chain across
          every retained generation, dictionary-code reference validity, and
          the bitemporal row invariants (`integrity.rs`);
        * the **event log**: per-record framing, tt monotonicity, and the
          rolling chain over the applied prefix against the `(offset, chain)`
          cursor this generation recorded. The store is a deterministic
          materialization of that log, so a log the store cannot account for
          is a defect in the store even when every segment checksums;
        * the **persisted TCSR permutation**, when one exists: its stamp, its
          shape against the live scan, and a content spot-check that rebuilds
          the index and compares;
        * the **artifact registry**: record digests, the per-name generation
          chain, and the blobs those records point at.

        **Nothing is repaired.** A torn event-log tail is a finding, not a
        trim; a stale index is a finding, not a rebuild. That is what makes
        this usable as an oracle: a checker that quietly fixed what it found
        would make every corruption sweep a tautology. It is also why a
        caller who wants the truth should open the store read-only —
        `Store(..., read_only=True)` — since a writer handle runs crash
        recovery on open and will have trimmed a torn tail before verify is
        ever called.

        The report is the engine's, plus `mode`, `store`, and a `findings`
        list of `{layer, kind, path, generation, detail, severity}` covering
        every layer. `problems` is the prose of the `error` findings and
        `healthy` is whether there are none; `advisory` findings are reported
        and do not make a store unhealthy.
        """
        if mode not in ("fast", "full"):
            raise InvalidArgError(f"verify mode must be 'fast' or 'full', got {mode!r}")
        try:
            report = self._store.verify(full=(mode == "full"))
        except Exception as e:
            raise _translate(e) from None
        findings: list[dict[str, Any]] = list(report.get("findings") or ())
        if mode == "full":
            findings += self._verify_eventlog()
            # The index check rebuilds the index from a live scan, so it can
            # only run over segments the engine has just certified. Against a
            # damaged one it would raise on the scan and take the whole
            # report down with it, hiding the findings that actually matter.
            findings += self._verify_tcsr(
                scan_is_trustworthy=not any(f["severity"] == "error" for f in findings))
            findings += self._verify_registry()
        report["findings"] = findings
        report["problems"] = [f["detail"] for f in findings if f["severity"] == "error"]
        report["healthy"] = not report["problems"]
        report["mode"] = mode
        report["store"] = str(self.path)
        return report

    # --- the full-mode layers outside the engine --------------------------- #
    #
    # The engine directory is `<store>/native`; the event log and the artifact
    # registry are its siblings, one level up. An adapter constructed directly
    # on a bare directory (as the engine's own unit tests do) has neither, and
    # their absence is not a defect — only a store that *claims* an applied
    # log prefix and cannot produce the log is.

    def _finding(self, layer: str, kind: str, path: str, detail: str,
                 severity: str = "error") -> dict[str, Any]:
        return {"layer": layer, "kind": kind, "path": path,
                "generation": self.generation, "detail": detail,
                "severity": severity}

    def _verify_eventlog(self) -> list[dict[str, Any]]:
        from tgms.storage.eventlog import EventLog

        rel = "eventlog.jsonl"
        path = self.path.parent / rel
        offset, chain = self.event_cursor()
        if not path.exists():
            if offset:
                return [self._finding(
                    "eventlog", "log-missing", rel,
                    f"generation {self.generation} records an applied prefix of "
                    f"{offset} bytes, but there is no event log at {path}")]
            return []
        try:
            # `EventLog(path)` creates the file when it is absent; the guard
            # above is what keeps this constructor read-only.
            log = EventLog(path)
        except Exception as e:
            return [self._finding("eventlog", "log-unreadable", rel,
                                  f"the event log cannot be opened: {e}")]
        try:
            defects = log.check_chain(offset, chain or None)
        except OSError as e:
            return [self._finding("eventlog", "log-unreadable", rel,
                                  f"the event log cannot be read: {e}")]
        return [self._finding("eventlog", d["kind"], rel, d["detail"])
                for d in defects]

    def _verify_registry(self) -> list[dict[str, Any]]:
        from tgms.artifact import registry as artifact_registry

        rel = artifact_registry.FILE_NAME
        try:
            defects = artifact_registry.verify(self.path.parent)
        except OSError as e:
            return [self._finding("registry", "registry-unreadable", rel,
                                  f"the artifact registry cannot be read: {e}")]
        layer_of = {"blob-missing": "blob", "blob-digest-mismatch": "blob"}
        return [self._finding(layer_of.get(d["kind"], "registry"), d["kind"], rel,
                              d["detail"])
                for d in defects]

    #: Vertices the TCSR content spot-check compares in full. The permutation
    #: is checked whole through its offsets (cheap, exact); this samples the
    #: gathered neighbour arrays, which is where a corrupt `row` column that
    #: still had the right shape would hide.
    TCSR_SAMPLE = 32

    def _verify_tcsr(self, scan_is_trustworthy: bool = True) -> list[dict[str, Any]]:
        import numpy as np

        from tgms.storage.tcsr import (PERM_FORMAT, TemporalCSR,
                                       load_permutation)

        rel = "index/tcsr.npz"
        path = self.path / "index" / "tcsr.npz"
        if not path.exists():
            return []
        if not scan_is_trustworthy:
            return [self._finding(
                "tcsr", "tcsr-not-checked", rel,
                "the persisted TCSR permutation was not checked: it can only be "
                "compared against a rebuild from the live rows, and this store has "
                "findings against the rows themselves",
                severity="advisory")]
        gen, sha = self._store.generation(), self._store.manifest_sha()
        try:
            with np.load(path) as z:
                stamp = (int(z["format"]), int(z["generation"]), str(z["manifest_sha"]))
        except Exception as e:
            return [self._finding(
                "tcsr", "tcsr-unreadable", rel,
                f"the persisted TCSR permutation exists but cannot be read: {e}")]
        if stamp != (PERM_FORMAT, int(gen), sha):
            # Not a defect in the store. A permutation is meaningless against
            # rows it was not computed from, and `load_permutation` already
            # ignores a mismatched stamp and rebuilds — so every store that
            # has written since its last index build would otherwise report
            # as corrupt, which would make this useless as a sweep oracle.
            return [self._finding(
                "tcsr", "tcsr-stale", rel,
                f"the persisted TCSR permutation is stamped "
                f"(format {stamp[0]}, generation {stamp[1]}, manifest {stamp[2]}) "
                f"and this store is at (format {PERM_FORMAT}, generation {gen}, "
                f"manifest {sha}); it will be ignored and rebuilt",
                severity="advisory")]

        try:
            cols = self.edges_columnar(columns=self.TCSR_COLS)
            n = self.num_entities()
        except Exception as e:  # a scan the engine's own walk did not condemn
            return [self._finding(
                "tcsr", "tcsr-not-checked", rel,
                f"the persisted TCSR permutation could not be compared against a "
                f"rebuild: the base scan failed with {e}",
                severity="advisory")]
        loaded = load_permutation(path, cols, n, gen, sha)
        if loaded is None:
            return [self._finding(
                "tcsr", "tcsr-shape", rel,
                "the persisted TCSR permutation carries this generation's stamp but "
                "does not fit the rows it names")]
        built = TemporalCSR.build(cols, n)
        for direction in ("out", "in"):
            a = loaded.out if direction == "out" else loaded.inn
            b = built.out if direction == "out" else built.inn
            if not np.array_equal(a.offsets, b.offsets):
                return [self._finding(
                    "tcsr", "tcsr-content-mismatch", rel,
                    f"the persisted TCSR permutation's {direction} offsets do not "
                    f"match the index rebuilt from this generation's rows")]
        # Content: rebuild a slice per sampled vertex and compare what the
        # traversal would actually read.
        degrees = np.diff(built.out.offsets) + np.diff(built.inn.offsets)
        sample = np.nonzero(degrees)[0][: self.TCSR_SAMPLE]
        for u in sample:
            for direction in ("out", "in"):
                got = loaded.neighbors(int(u), direction)
                want = built.neighbors(int(u), direction)
                if any(not np.array_equal(g, w) for g, w in zip(got, want)):
                    return [self._finding(
                        "tcsr", "tcsr-content-mismatch", rel,
                        f"the persisted TCSR permutation disagrees with the index "
                        f"rebuilt from this generation's rows at vertex {int(u)} "
                        f"({direction})")]
        return []

    def needs_compaction(self) -> bool:
        return bool(self._store.needs_compaction())

    @property
    def generation(self) -> int:
        """Current manifest generation — the store's publication counter."""
        return int(self._store.generation())
