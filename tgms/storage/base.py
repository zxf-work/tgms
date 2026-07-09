"""StorageAdapter ABC.

Bi-temporal update semantics (WP1.2) are implemented *once* here, on top of a
small set of backend primitives, so Kùzu and DuckDB adapters cannot diverge
semantically: an adapter only implements row insertion, tt-closing, and scans.

Semantics recap:
- assert_*  — new belief. Any currently-believed version of the same logical
  identity overlapping the asserted valid interval is bi-temporally replaced:
  its tt is closed, and the non-overlapping remainder of its valid interval is
  re-inserted (same props, new tt). Then the new version is inserted. Adjacent
  identical-props versions are NOT coalesced (Phase 1 rule).
- retract   — evolution: the believed version whose vt contains t is closed in
  tt and re-inserted with vt truncated to [vt_s, t).
- correct   — correction: believed versions overlapping vt are closed in tt;
  non-overlapping remainders are preserved (old props, new tt); a corrected
  version with the given vt and new props is inserted.

Version ids: the spec defines vid = hash(identity, tt_s), which collides when
one batch splits a version into two fragments at the same tt. We therefore use
vid = hash(identity, tt_s, vt_s) — a strict refinement (unique because believed
valid intervals of one identity are disjoint).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Sequence

import numpy as np

from tgms.core.errors import InvalidArgError, NotFoundError, StateError
from tgms.core.model import (
    OPEN_END,
    EdgeVersion,
    EntityRef,
    Interval,
    NodeVersion,
    Props,
    canonical_json,
    digest,
    edge_eid,
    sha256_hex,
)


def _vid(identity: str, tt_s: int, vt_s: int) -> str:
    return sha256_hex(f"{identity}:{tt_s}:{vt_s}")[:24]


def _interval(start: int, end: int) -> Interval:
    try:
        return Interval(start, end)
    except ValueError as e:
        raise InvalidArgError(str(e)) from None


class StorageAdapter(ABC):
    """Backend-agnostic bi-temporal store."""

    paranoid: bool = False  # re-check disjointness invariant after every batch
    _tcsr = None  # lazily built current-belief TemporalCSR + its columnar arrays

    def tcsr(self):
        """Current-belief TemporalCSR (+ the columnar arrays it was built
        from), built lazily and invalidated by apply_ops."""
        if self._tcsr is None:
            from tgms.storage.tcsr import TemporalCSR
            cols = self.edges_columnar()
            self._tcsr = (TemporalCSR.build(cols, self.num_entities()), cols)
        return self._tcsr

    # --- batch transactions (backends override; default = no-op) --------- #

    def begin(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    # ------------------------------------------------------------------ #
    # backend primitives                                                  #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def ensure_entities(self, uid_labels: Iterable[tuple[str, str]]) -> None:
        """Register logical node identities and assign dense int64 ids."""

    @abstractmethod
    def insert_node_versions(self, rows: Sequence[NodeVersion]) -> None: ...

    @abstractmethod
    def insert_edge_versions(self, rows: Sequence[EdgeVersion]) -> None: ...

    @abstractmethod
    def close_node_versions(self, vids: Sequence[str], tt_e: int) -> None: ...

    @abstractmethod
    def close_edge_versions(self, vids: Sequence[str], tt_e: int) -> None: ...

    @abstractmethod
    def believed_node_versions(self, uid: str, as_of_tt: int = OPEN_END) -> list[NodeVersion]: ...

    @abstractmethod
    def believed_edge_versions(self, eid: str, as_of_tt: int = OPEN_END) -> list[EdgeVersion]: ...

    @abstractmethod
    def all_node_versions(self) -> Iterable[NodeVersion]:
        """Every version row, deterministic order not required (digest sorts)."""

    @abstractmethod
    def all_edge_versions(self) -> Iterable[EdgeVersion]: ...

    def nodes_with_believed_versions(self, uids: Sequence[str],
                                     as_of_tt: int = OPEN_END) -> set[str]:
        """Subset of `uids` that have at least one believed version.
        Backends should override with a batched query (hot on bulk ingest)."""
        return {u for u in uids if self.believed_node_versions(u, as_of_tt)}

    # --- dense id dictionary ------------------------------------------- #

    @abstractmethod
    def dense_ids(self, uids: Sequence[str]) -> np.ndarray:
        """Map uid strings to dense int64 ids; raises NotFoundError on misses."""

    @abstractmethod
    def uids_for(self, ids: Sequence[int]) -> list[str]: ...

    @abstractmethod
    def num_entities(self) -> int: ...

    # --- columnar read path (operator kernels) ------------------------- #

    @abstractmethod
    def edges_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
        rel_types: Sequence[str] | None = None,
    ) -> dict[str, np.ndarray]:
        """Struct-of-arrays over believed edge versions whose vt overlaps
        [vt_min, vt_max). Keys: src_id, dst_id, vt_s, vt_e (int64),
        eid, vid, rel_type (object). Sorted by vt_s."""

    @abstractmethod
    def nodes_columnar(
        self,
        as_of_tt: int = OPEN_END,
        vt_min: int | None = None,
        vt_max: int | None = None,
    ) -> dict[str, np.ndarray]:
        """Keys: uid_id, vt_s, vt_e (int64), uid, vid, label (object). Sorted by vt_s."""

    @abstractmethod
    def props_for_vids(self, kind: str, vids: Sequence[str]) -> dict[str, dict]:
        """Fetch props JSON for specific version ids. kind: "node" | "edge"."""

    @abstractmethod
    def stats(self) -> dict[str, Any]:
        """Lightweight statistics for cost estimation (WP1.4)."""

    # ------------------------------------------------------------------ #
    # shared bi-temporal semantics                                        #
    # ------------------------------------------------------------------ #

    def apply_ops(self, ops: Sequence[dict[str, Any]], tt: int) -> None:
        """Apply one write batch at transaction time `tt`. Used by both the
        live write path and event-log replay — must stay deterministic."""
        touched_nodes: set[str] = set()
        touched_edges: set[str] = set()
        for op in ops:
            kind = op["op"]
            if kind == "assert_node":
                self._assert_node(op, tt)
                touched_nodes.add(op["uid"])
            elif kind == "assert_edge":
                eid = self._assert_edge(op, tt)
                touched_edges.add(eid)
            elif kind == "retract":
                self._retract(op, tt)
            elif kind == "correct":
                self._correct(op, tt)
            elif kind == "ingest_events":
                self._ingest_events(op, tt)
            else:
                raise InvalidArgError(f"unknown op kind: {kind}")
        self._tcsr = None  # writes invalidate the current-belief index
        if self.paranoid:
            for uid in touched_nodes:
                self._check_disjoint([v for v in self.believed_node_versions(uid)], f"node {uid}")
            for eid in touched_edges:
                self._check_disjoint([v for v in self.believed_edge_versions(eid)], f"edge {eid}")

    @staticmethod
    def _check_disjoint(versions: list, label: str) -> None:
        ivs = sorted((v.vt_s, v.vt_e) for v in versions)
        for (s1, e1), (s2, _) in zip(ivs, ivs[1:]):
            if s2 < e1:
                raise StateError(f"disjointness violated for {label}", intervals=ivs)

    # -- node ops -------------------------------------------------------- #

    def _assert_node(self, op: dict[str, Any], tt: int) -> None:
        uid, label, props = op["uid"], op["label"], op.get("props", {})
        vt = _interval(op["vt_s"], op.get("vt_e", OPEN_END))
        self.ensure_entities([(uid, label)])
        existing = [v for v in self.believed_node_versions(uid)
                    if Interval(v.vt_s, v.vt_e).overlaps(vt)]
        to_close = [v.vid for v in existing]
        fragments: list[NodeVersion] = []
        for v in existing:
            for fs, fe in _remainder(v.vt_s, v.vt_e, vt.start, vt.end):
                fragments.append(NodeVersion(
                    vid=_vid(uid, tt, fs), uid=uid, label=v.label,
                    vt_s=fs, vt_e=fe, tt_s=tt, tt_e=OPEN_END, props=v.props))
        if to_close:
            self.close_node_versions(to_close, tt)
        new = NodeVersion(vid=_vid(uid, tt, vt.start), uid=uid, label=label,
                          vt_s=vt.start, vt_e=vt.end, tt_s=tt, tt_e=OPEN_END, props=props)
        self.insert_node_versions(fragments + [new])

    # -- edge ops -------------------------------------------------------- #

    def _assert_edge(self, op: dict[str, Any], tt: int) -> str:
        src, dst, rel_type = op["src"], op["dst"], op["rel_type"]
        disc, props = op.get("disc", ""), op.get("props", {})
        vt = _interval(op["vt_s"], op.get("vt_e", OPEN_END))
        eid = edge_eid(src, dst, rel_type, disc)
        self.ensure_entities([(src, op.get("src_label", "")), (dst, op.get("dst_label", ""))])
        existing = [v for v in self.believed_edge_versions(eid)
                    if Interval(v.vt_s, v.vt_e).overlaps(vt)]
        to_close = [v.vid for v in existing]
        fragments: list[EdgeVersion] = []
        for v in existing:
            for fs, fe in _remainder(v.vt_s, v.vt_e, vt.start, vt.end):
                fragments.append(EdgeVersion(
                    eid=eid, vid=_vid(eid, tt, fs), src=src, dst=dst, rel_type=rel_type,
                    disc=disc, vt_s=fs, vt_e=fe, tt_s=tt, tt_e=OPEN_END, props=v.props))
        if to_close:
            self.close_edge_versions(to_close, tt)
        new = EdgeVersion(eid=eid, vid=_vid(eid, tt, vt.start), src=src, dst=dst,
                          rel_type=rel_type, disc=disc, vt_s=vt.start, vt_e=vt.end,
                          tt_s=tt, tt_e=OPEN_END, props=props)
        self.insert_edge_versions(fragments + [new])
        return eid

    # -- retract / correct ------------------------------------------------ #

    def _retract(self, op: dict[str, Any], tt: int) -> None:
        ref = _ref_from_op(op)
        t = op["t"]
        if ref.kind == "node":
            hits = [v for v in self.believed_node_versions(ref.identity) if v.valid_at(t)]
            if not hits:
                raise NotFoundError(f"no believed node version of {ref.identity} valid at {t}")
            self.close_node_versions([v.vid for v in hits], tt)
            repl = [NodeVersion(vid=_vid(v.uid, tt, v.vt_s), uid=v.uid, label=v.label,
                                vt_s=v.vt_s, vt_e=t, tt_s=tt, tt_e=OPEN_END, props=v.props)
                    for v in hits if v.vt_s < t]
            self.insert_node_versions(repl)
        else:
            hits = [v for v in self.believed_edge_versions(ref.identity) if v.valid_at(t)]
            if not hits:
                raise NotFoundError(f"no believed edge version of {ref.identity} valid at {t}")
            self.close_edge_versions([v.vid for v in hits], tt)
            repl = [EdgeVersion(eid=v.eid, vid=_vid(v.eid, tt, v.vt_s), src=v.src, dst=v.dst,
                                rel_type=v.rel_type, disc=v.disc, vt_s=v.vt_s, vt_e=t,
                                tt_s=tt, tt_e=OPEN_END, props=v.props)
                    for v in hits if v.vt_s < t]
            self.insert_edge_versions(repl)

    def _correct(self, op: dict[str, Any], tt: int) -> None:
        ref = _ref_from_op(op)
        new_props: Props = op["props"]
        vt = _interval(op["vt_s"], op.get("vt_e", OPEN_END))
        if ref.kind == "node":
            versions = self.believed_node_versions(ref.identity)
            hits = [v for v in versions if Interval(v.vt_s, v.vt_e).overlaps(vt)]
            if not hits:
                raise NotFoundError(f"no believed node version of {ref.identity} overlaps vt")
            self.close_node_versions([v.vid for v in hits], tt)
            rows: list[NodeVersion] = []
            for v in hits:
                for fs, fe in _remainder(v.vt_s, v.vt_e, vt.start, vt.end):
                    rows.append(NodeVersion(vid=_vid(v.uid, tt, fs), uid=v.uid, label=v.label,
                                            vt_s=fs, vt_e=fe, tt_s=tt, tt_e=OPEN_END,
                                            props=v.props))
            label = hits[0].label
            rows.append(NodeVersion(vid=_vid(ref.identity, tt, vt.start), uid=ref.identity,
                                    label=label, vt_s=vt.start, vt_e=vt.end,
                                    tt_s=tt, tt_e=OPEN_END, props=new_props))
            self.insert_node_versions(rows)
        else:
            versions = self.believed_edge_versions(ref.identity)
            hits = [v for v in versions if Interval(v.vt_s, v.vt_e).overlaps(vt)]
            if not hits:
                raise NotFoundError(f"no believed edge version of {ref.identity} overlaps vt")
            self.close_edge_versions([v.vid for v in hits], tt)
            proto = hits[0]
            rows_e: list[EdgeVersion] = []
            for v in hits:
                for fs, fe in _remainder(v.vt_s, v.vt_e, vt.start, vt.end):
                    rows_e.append(EdgeVersion(eid=v.eid, vid=_vid(v.eid, tt, fs), src=v.src,
                                              dst=v.dst, rel_type=v.rel_type, disc=v.disc,
                                              vt_s=fs, vt_e=fe, tt_s=tt, tt_e=OPEN_END,
                                              props=v.props))
            rows_e.append(EdgeVersion(eid=proto.eid, vid=_vid(proto.eid, tt, vt.start),
                                      src=proto.src, dst=proto.dst, rel_type=proto.rel_type,
                                      disc=proto.disc, vt_s=vt.start, vt_e=vt.end,
                                      tt_s=tt, tt_e=OPEN_END, props=new_props))
            self.insert_edge_versions(rows_e)

    # -- bulk event ingestion --------------------------------------------- #

    def _ingest_events(self, op: dict[str, Any], tt: int) -> None:
        """Bulk path for event-stream datasets. Events are instantaneous
        (vt_e defaults to vt_s + 1) and disjoint by construction: each event
        without an explicit disc gets its batch offset as discriminator, so
        every event is its own logical edge. Overlap checks are bypassed."""
        events: list[dict[str, Any]] = op["events"]
        node_first_seen: dict[str, int] = {}
        edge_rows: list[EdgeVersion] = []
        for i, ev in enumerate(events):
            src, dst, rel_type = ev["src"], ev["dst"], ev["rel_type"]
            vt_s = ev["vt_s"]
            vt_e = ev.get("vt_e") or vt_s + 1
            disc = ev.get("disc", f"#{op.get('offset', 0) + i}")
            eid = edge_eid(src, dst, rel_type, disc)
            edge_rows.append(EdgeVersion(
                eid=eid, vid=_vid(eid, tt, vt_s), src=src, dst=dst, rel_type=rel_type,
                disc=disc, vt_s=vt_s, vt_e=vt_e, tt_s=tt, tt_e=OPEN_END,
                props=ev.get("props", {})))
            for u in (src, dst):
                if u not in node_first_seen or vt_s < node_first_seen[u]:
                    node_first_seen[u] = vt_s
        label = op.get("node_label", "Node")
        known = self.nodes_with_believed_versions(list(node_first_seen))
        new_uids = [u for u in node_first_seen if u not in known]
        self.ensure_entities([(u, label) for u in node_first_seen])
        self.insert_node_versions([
            NodeVersion(vid=_vid(u, tt, node_first_seen[u]), uid=u, label=label,
                        vt_s=node_first_seen[u], vt_e=OPEN_END, tt_s=tt, tt_e=OPEN_END,
                        props={})
            for u in sorted(new_uids)])
        self.insert_edge_versions(edge_rows)

    # ------------------------------------------------------------------ #
    # digest (replay-equivalence check)                                   #
    # ------------------------------------------------------------------ #

    def store_digest(self) -> str:
        """Digest of the full logical store content, backend-independent."""
        node_rows = sorted(
            (v.to_json() for v in self.all_node_versions()),
            key=lambda r: (r["uid"], r["tt_s"], r["vt_s"], r["vid"]))
        edge_rows = sorted(
            (v.to_json() for v in self.all_edge_versions()),
            key=lambda r: (r["eid"], r["tt_s"], r["vt_s"], r["vid"]))
        return digest({"nodes": node_rows, "edges": edge_rows})


def _remainder(vs: int, ve: int, cs: int, ce: int) -> list[tuple[int, int]]:
    """Parts of [vs, ve) not covered by [cs, ce)."""
    out = []
    if vs < cs:
        out.append((vs, min(ve, cs)))
    if ce < ve:
        out.append((max(vs, ce), ve))
    return out


def _ref_from_op(op: dict[str, Any]) -> EntityRef:
    r = op["ref"]
    return EntityRef(kind=r["kind"], uid=r.get("uid"), src=r.get("src"), dst=r.get("dst"),
                     rel_type=r.get("rel_type"), disc=r.get("disc", ""))


def make_op(kind: str, **kwargs: Any) -> dict[str, Any]:
    """Canonical op record for the event log."""
    op = {"op": kind, **kwargs}
    # round-trip through canonical JSON so the logged and applied forms agree
    import json as _json
    return _json.loads(canonical_json(op))
