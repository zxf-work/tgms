"""Temporal CSR index (spec 7.2.2, persistence per D-039).

Per-node adjacency arrays sorted by (node, vt_s, vid): `offsets: int64[V+1]`
into parallel `nbr / vt_s / vt_e / row` arrays, one structure per direction.
`row` indexes back into the columnar edge arrays the index was built from,
so per-row lookups stay zero-copy. Built lazily over *current* beliefs and
invalidated on write; time-respecting traversals (O4 multi-label, O5) run
over it.

Persistence stores the *permutation only* (blueprint C3.4: offsets plus
row perm, never duplicated nbr/vt_s/vt_e columns — those are gathered from
the base scan at load). The file is stamped with the generation and
manifest sha it was built from, and a stamp mismatch means the file is
ignored and rebuilt: a persisted index never survives a generation it was
not built for. The file is a disposable cache — any load problem falls
back to a rebuild, and saves are atomic (tmp + rename) so readers see old
bytes, new bytes, or nothing.

TODO(M3-bench): incremental rebuild per appended time bucket.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import numpy as np

#: Bump to orphan every persisted permutation written by older layouts.
PERM_FORMAT = 1


class _Direction:
    __slots__ = ("offsets", "nbr", "vt_s", "vt_e", "row")

    def __init__(self, offsets, nbr, vt_s, vt_e, row):
        self.offsets = offsets
        self.nbr = nbr
        self.vt_s = vt_s
        self.vt_e = vt_e
        self.row = row


def _build_direction(key: np.ndarray, other: np.ndarray, vt_s: np.ndarray,
                     vt_e: np.ndarray, n: int) -> _Direction:
    """Group edges by `key`, keeping each group in scan order.

    A *stable* sort on the grouping key alone. The columnar scan contract
    orders rows by `(vt_s, vid)`, so stability preserves that inside every
    group — which is all the traversal needs: `neighbors()` binary-searches
    on `vt_s`, and the identity only has to break ties deterministically.

    This was a four-key lexsort ending in the eid, costing ~750 ms per
    direction at 1M rows against ~85 ms here — and the sort was the entire
    build. The tiebreak is now vid rather than eid; the operator oracle is
    indifferent, and its fixtures do exercise the distinction (91 tied
    groups, 60 of them ordered differently by the two identities).
    """
    order = np.argsort(key, kind="stable")
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
        vt_s, vt_e = cols["vt_s"], cols["vt_e"]
        return cls(_build_direction(src, dst, vt_s, vt_e, n_entities),
                   _build_direction(dst, src, vt_s, vt_e, n_entities),
                   n_entities, len(src))

    @classmethod
    def from_permutation(cls, cols: dict[str, np.ndarray], n_entities: int,
                         out_offsets: np.ndarray, out_row: np.ndarray,
                         in_offsets: np.ndarray, in_row: np.ndarray) -> "TemporalCSR":
        """Reassemble from a persisted permutation plus the base scan.

        The gathers reproduce exactly what `build` computes: a stable sort
        is a permutation, and the permutation is all that was saved.
        """
        src, dst = cols["src_id"], cols["dst_id"]
        vt_s, vt_e = cols["vt_s"], cols["vt_e"]
        o = out_row.astype(np.int64)
        i = in_row.astype(np.int64)
        return cls(_Direction(out_offsets.astype(np.int64), dst[o], vt_s[o], vt_e[o], o),
                   _Direction(in_offsets.astype(np.int64), src[i], vt_s[i], vt_e[i], i),
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


def save_permutation(path: Path, csr: TemporalCSR,
                     generation: int, manifest_sha: str) -> None:
    """Persist the permutation form atomically; failure is non-fatal.

    The index is derived state: a store that cannot take the write (read-only
    mount, disk full) still answers queries, it just rebuilds next process.
    """
    tmp = path.with_name(f"{path.stem}.tmp{os.getpid()}.npz")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(tmp,
                 format=PERM_FORMAT,
                 generation=generation,
                 manifest_sha=manifest_sha,
                 n_entities=csr.n_entities,
                 n_edges=csr.n_edges,
                 out_offsets=csr.out.offsets,
                 out_row=csr.out.row.astype(np.uint32),
                 in_offsets=csr.inn.offsets,
                 in_row=csr.inn.row.astype(np.uint32))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def load_permutation(path: Path, cols: dict[str, np.ndarray], n_entities: int,
                     generation: int, manifest_sha: str) -> TemporalCSR | None:
    """The persisted TCSR for exactly this generation, else None.

    None means "rebuild": a missing file, a stamp from any other generation
    or manifest, a format bump, a shape that disagrees with the live scan,
    or a file damaged enough that NumPy refuses it. The stamp check is the
    correctness gate — a permutation is meaningless against rows it was not
    computed from — and the shape checks are corruption backstops behind it.
    """
    try:
        with np.load(path) as z:
            if int(z["format"]) != PERM_FORMAT \
                    or int(z["generation"]) != int(generation) \
                    or str(z["manifest_sha"]) != manifest_sha \
                    or int(z["n_entities"]) != int(n_entities) \
                    or int(z["n_edges"]) != len(cols["src_id"]):
                return None
            out_offsets, out_row = z["out_offsets"], z["out_row"]
            in_offsets, in_row = z["in_offsets"], z["in_row"]
        n_edges = len(cols["src_id"])
        for offsets, row in ((out_offsets, out_row), (in_offsets, in_row)):
            if len(offsets) != n_entities + 1 or len(row) != n_edges:
                return None
        return TemporalCSR.from_permutation(cols, n_entities,
                                            out_offsets, out_row,
                                            in_offsets, in_row)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
