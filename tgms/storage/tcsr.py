"""Temporal CSR index (spec 7.2.2).

Per-node adjacency arrays sorted by (node, vt_s, eid): `offsets: int64[V+1]`
into parallel `nbr / vt_s / vt_e / row` arrays, one structure per direction.
`row` indexes back into the columnar edge arrays the index was built from,
so eid/rel_type lookups stay zero-copy. Built lazily over *current* beliefs
and invalidated on write; time-respecting traversals (O4 multi-label, O5)
run over it. Persistable via np.savez / mmap load for the bench node.

TODO(M3-bench): incremental rebuild per appended time bucket.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class _Direction:
    __slots__ = ("offsets", "nbr", "vt_s", "vt_e", "row")

    def __init__(self, offsets, nbr, vt_s, vt_e, row):
        self.offsets = offsets
        self.nbr = nbr
        self.vt_s = vt_s
        self.vt_e = vt_e
        self.row = row


def _build_direction(key: np.ndarray, other: np.ndarray, vt_s: np.ndarray,
                     vt_e: np.ndarray, eid: np.ndarray, n: int) -> _Direction:
    order = np.lexsort((eid, vt_s, key))
    counts = np.bincount(key, minlength=n)
    offsets = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(counts, out=offsets[1:])
    return _Direction(offsets, other[order].astype(np.int64),
                      vt_s[order].astype(np.int64), vt_e[order].astype(np.int64),
                      order.astype(np.int64))


class TemporalCSR:
    def __init__(self, out_dir: _Direction, in_dir: _Direction, n_entities: int,
                 n_edges: int) -> None:
        self.out = out_dir
        self.inn = in_dir
        self.n_entities = n_entities
        self.n_edges = n_edges

    @classmethod
    def build(cls, cols: dict[str, np.ndarray], n_entities: int) -> "TemporalCSR":
        src, dst = cols["src_id"], cols["dst_id"]
        vt_s, vt_e, eid = cols["vt_s"], cols["vt_e"], cols["eid"]
        return cls(_build_direction(src, dst, vt_s, vt_e, eid, n_entities),
                   _build_direction(dst, src, vt_s, vt_e, eid, n_entities),
                   n_entities, len(src))

    def neighbors(self, u: int, direction: str = "out",
                  t_max: int | None = None) -> tuple[np.ndarray, ...]:
        """(nbr, vt_s, vt_e, row) slices for node u; if t_max is given, only
        edges with vt_s < t_max (binary search — slices are vt_s-sorted)."""
        d = self.out if direction == "out" else self.inn
        lo, hi = int(d.offsets[u]), int(d.offsets[u + 1])
        if t_max is not None:
            hi = lo + int(np.searchsorted(d.vt_s[lo:hi], t_max, side="left"))
        return d.nbr[lo:hi], d.vt_s[lo:hi], d.vt_e[lo:hi], d.row[lo:hi]

    def save(self, path: str | Path) -> None:
        np.savez(path, n_entities=self.n_entities, n_edges=self.n_edges,
                 **{f"{p}_{f}": getattr(getattr(self, a), f)
                    for p, a in (("out", "out"), ("in", "inn"))
                    for f in ("offsets", "nbr", "vt_s", "vt_e", "row")})

    @classmethod
    def load(cls, path: str | Path, mmap: bool = True) -> "TemporalCSR":
        z = np.load(path, mmap_mode="r" if mmap else None)
        dirs = {p: _Direction(*(z[f"{p}_{f}"]
                                for f in ("offsets", "nbr", "vt_s", "vt_e", "row")))
                for p in ("out", "in")}
        return cls(dirs["out"], dirs["in"], int(z["n_entities"]), int(z["n_edges"]))
