"""Shared fixtures and hypothesis strategies for bi-temporal update sequences."""

from __future__ import annotations

import os
import tempfile
from typing import Any

from hypothesis import strategies as st

from tgms.storage.duckdb_adapter import DuckDBAdapter

UIDS = ["a", "b", "c", "d"]
RELS = ["R", "S"]

# small dense time domain so intervals collide often (stress for carving logic)
times = st.integers(min_value=0, max_value=50)
props = st.fixed_dictionaries({}, optional={"p": st.integers(0, 3), "q": st.booleans()})


def interval_args() -> st.SearchStrategy[tuple[int, int]]:
    return st.tuples(times, times).map(lambda t: (min(t), max(t) + 1))


@st.composite
def write_op(draw) -> dict[str, Any]:
    kind = draw(st.sampled_from(
        ["assert_node", "assert_edge", "assert_edge", "retract_node",
         "retract_edge", "correct_node", "correct_edge"]))
    uid = draw(st.sampled_from(UIDS))
    dst = draw(st.sampled_from(UIDS))
    rel = draw(st.sampled_from(RELS))
    vt_s, vt_e = draw(interval_args())
    p = draw(props)
    t = draw(times)
    if kind == "assert_node":
        return {"op": "assert_node", "uid": uid, "label": "N", "props": p,
                "vt_s": vt_s, "vt_e": vt_e}
    if kind == "assert_edge":
        return {"op": "assert_edge", "src": uid, "dst": dst, "rel_type": rel,
                "props": p, "vt_s": vt_s, "vt_e": vt_e, "disc": ""}
    if kind == "retract_node":
        return {"op": "retract", "ref": {"kind": "node", "uid": uid}, "t": t}
    if kind == "retract_edge":
        return {"op": "retract", "ref": {"kind": "edge", "src": uid, "dst": dst,
                "rel_type": rel, "disc": ""}, "t": t}
    if kind == "correct_node":
        return {"op": "correct", "ref": {"kind": "node", "uid": uid}, "props": p,
                "vt_s": vt_s, "vt_e": vt_e}
    return {"op": "correct", "ref": {"kind": "edge", "src": uid, "dst": dst,
            "rel_type": rel, "disc": ""}, "props": p, "vt_s": vt_s, "vt_e": vt_e}


op_sequences = st.lists(write_op(), min_size=1, max_size=25)


def fresh_adapter(paranoid: bool = True):
    """The adapter every suite runs against.

    Defaults to DuckDB, so nothing about an ordinary run changes. Setting
    TGMS_TEST_BACKEND=native points the *entire* suite — invariants, the
    500-case operator oracle, metamorphic, replay — at the native engine
    instead (D-028; ENGINE_IMPLEMENTATION_SPEC WP-N3). That is the whole
    acceptance argument for the new backend: it has to satisfy the same
    human-owned ground truth, unmodified, that DuckDB does.

    The native store is a directory rather than an in-memory handle, so each
    adapter gets a temporary one whose lifetime is tied to the adapter object.
    """
    backend = os.environ.get("TGMS_TEST_BACKEND", "duckdb")
    if backend == "duckdb":
        a = DuckDBAdapter(":memory:")
    elif backend == "native":
        from tgms.storage.native import NativeAdapter

        tmp = tempfile.TemporaryDirectory(prefix="tgms-test-native-")
        a = NativeAdapter(tmp.name)
        a._test_tmpdir = tmp  # keep the directory alive as long as the adapter
    else:
        raise ValueError(
            f"unknown TGMS_TEST_BACKEND {backend!r}; expected 'duckdb' or 'native'"
        )
    a.paranoid = paranoid
    return a
