"""Adjacency for `Expand` — a typed, Σ-pruned CSR, cached for one plan run.

`adapter.tcsr()` exists and `TemporalCSR.neighbors(u, direction, t_max)` is
exactly the primitive an expansion wants: it returns `(nbr, vt_s, vt_e, row)`
numpy slices, with `row` indexing back into the columnar arrays, so a hop costs
no copy. But the cached CSR is **current-belief, unwindowed and untyped** (the
native backend even swaps `eid`/`rel_type` out of `TCSR_COLS` for physical row
addresses), so it cannot serve a scan under a narrowed Σ or a `rel_type`.

So this module makes the same decision `ops_paths._csr_for` already makes, and
lifts that function rather than re-deriving it:

> the cached CSR **only** when `rel_type is None` and Σ is the default;
> otherwise a per-`(rel_type, Σ)` build from a pruned `edges_columnar`, cached
> for the duration of one plan execution.

The Σ mask is applied to the columns **before** the CSR is built, so every edge
in the index is one the scan would have returned — an expansion cannot traverse
an edge its own Σ excludes.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.model import OPEN_END, clamp_tt
from tgms.storage.tcsr import TemporalCSR
from tgms.tgir.eval.scan import _sigma_mask
from tgms.tgir.types import Sigma


class Adjacency:
    """One `(rel_type, Σ)` index plus the columnar arrays it was built from."""

    __slots__ = ("csr", "cols", "rel_type", "sigma")

    def __init__(self, csr: TemporalCSR, cols: dict[str, np.ndarray],
                 rel_type: str | None, sigma: Sigma) -> None:
        self.csr = csr
        self.cols = cols
        self.rel_type = rel_type
        self.sigma = sigma

    def step(self, u: int, direction: str) -> tuple[np.ndarray, np.ndarray]:
        """`(neighbour dense ids, edge row indices)` for one hop out of `u`.

        The slices are in the columnar scan's own `(vt_s, vid)` order, because
        `_build_direction` groups with a **stable** sort on the key alone — so
        each hop's edges arrive in the traversal order §2.3 declares, with no
        sort here.

        `direction="both"` merges the two directions and re-sorts by
        `(vt_s, vid)`: the union of two ordered slices is not itself ordered,
        and "the traversal's `(vt_s, vid)`" is a statement about the hop, not
        about each direction separately.
        """
        if direction in ("out", "in"):
            nbr, _vt_s, _vt_e, row = self.csr.neighbors(u, direction)
            return nbr, row
        out_nbr, _s1, _e1, out_row = self.csr.neighbors(u, "out")
        in_nbr, _s2, _e2, in_row = self.csr.neighbors(u, "in")
        nbr = np.concatenate([out_nbr, in_nbr])
        row = np.concatenate([out_row, in_row])
        if row.size:
            order = np.lexsort((self.cols["vid"][row] if "vid" in self.cols
                                else row, self.cols["vt_s"][row]))
            nbr, row = nbr[order], row[order]
        return nbr, row


class AdjacencyCache:
    """Per-plan-execution cache. Two `Expand` nodes over the same `(rel_type,
    Σ)` share one index, which is what keeps a multi-hop chain from rebuilding
    it per hop."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self._cache: dict[tuple[str | None, Any], Adjacency] = {}

    def get(self, rel_type: str | None, sigma: Sigma, *,
            need_identity: bool = False) -> Adjacency:
        """`need_identity` asks for an index carrying `vid`, which is what an
        `edge_var` binding needs to reach the columns the index itself does not
        carry (`disc`, `tt_s`, `tt_e`, `props`). The shared `adapter.tcsr()` is
        built from `TCSR_COLS` and has no `vid`, so asking for identity also
        means declining the shared index — the alternative was binding those
        columns as null, which would misreport a column the store holds."""
        key = (rel_type, (sigma.t_v, sigma.t_b), need_identity)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._build(rel_type, sigma, need_identity)
            self._cache[key] = hit
        return hit

    def _build(self, rel_type: str | None, sigma: Sigma,
               need_identity: bool = False) -> Adjacency:
        adapter = self.adapter
        if rel_type is None and _is_default(sigma) and not need_identity:
            # exactly `ops_paths._csr_for`'s condition: the cached index is
            # current-belief and unwindowed, so it is only substitutable when
            # the plan asks for neither
            csr, cols = adapter.tcsr()
            return Adjacency(csr, cols, rel_type, sigma)

        hull = sigma.hull
        columns = tuple(adapter.TCSR_COLS) + (
            ("vid",) if "vid" not in adapter.TCSR_COLS else ())
        cols = adapter.edges_columnar(
            as_of_tt=sigma.t_b, vt_min=hull.start, vt_max=hull.end,
            rel_types=[rel_type] if rel_type else None, columns=columns)
        # `Expand` carries no `vt_mode` of its own (it is a scan parameter, and
        # §2.3 gives the node none), so the widest reading — `overlap` — is the
        # one an expansion traverses under. Recorded rather than assumed: a
        # narrower mode here would drop edges the scans would have returned.
        keep = _sigma_mask(cols, sigma, "overlap")
        cols = {k: v[keep] for k, v in cols.items()}
        return Adjacency(TemporalCSR.build(cols, adapter.num_entities()), cols,
                         rel_type, sigma)


def _is_default(sigma: Sigma) -> bool:
    return (len(sigma.t_v) == 1 and sigma.t_v[0].start == 0
            and sigma.t_v[0].end == OPEN_END
            and clamp_tt(sigma.t_b) == clamp_tt(OPEN_END))


__all__ = ["Adjacency", "AdjacencyCache"]
