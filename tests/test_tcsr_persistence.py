"""The persisted TCSR permutation (D-039): correctness of the lifecycle.

The one invariant that matters: a persisted index must never survive a
generation it was not built for. Everything else — missing file, damaged
file, foreign stamp — must degrade to a rebuild, never to a wrong answer
or an error.

These run regardless of TGMS_TEST_BACKEND — persistence is native-engine
specific, not adapter conformance.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.storage.native import NativeAdapter  # noqa: E402

PATH_ARGS = {"src": "n0", "dst": "n3", "window": {"t_a": 0, "t_b": 100},
             "k": 5, "max_hops": 4}


def build(root: Path):
    import tgms

    store = tgms.open(root, backend="native")
    store.ingest_events([
        {"src": f"n{i % 7}", "dst": f"n{(i + 1) % 7}", "rel_type": "R",
         "vt_s": i, "vt_e": i + 5, "props": {"w": i}}
        for i in range(60)
    ])
    return store


def paths(adapter, **overrides):
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    out = call_operator(adapter, "temporal_paths", {**PATH_ARGS, **overrides})
    return out["rows"] if not overrides else out


def index_file(adapter) -> Path:
    return adapter.path / "index" / "tcsr.npz"


def csr_arrays(adapter):
    csr, _ = adapter.tcsr()
    return [np.asarray(getattr(d, f))
            for d in (csr.out, csr.inn)
            for f in ("offsets", "nbr", "vt_s", "vt_e", "row")]


def test_persisted_permutation_round_trips(tmp_path):
    store = build(tmp_path)
    first = paths(store.adapter)
    assert first, "the fixture graph must contain paths"
    idx = index_file(store.adapter)
    assert idx.exists(), "tcsr() outside a batch must persist the permutation"
    with np.load(idx) as z:
        assert int(z["generation"]) == store.adapter._store.generation()
        assert str(z["manifest_sha"]) == store.adapter._store.manifest_sha()

    built = csr_arrays(store.adapter)
    reopened = NativeAdapter(store.adapter.path)
    loaded = csr_arrays(reopened)
    for a, b in zip(built, loaded):
        assert np.array_equal(a, b), "loaded permutation must equal the build"
    assert paths(reopened) == first
    reopened.close()
    store.close()


def test_a_write_invalidates_and_restamps(tmp_path):
    store = build(tmp_path)
    before = paths(store.adapter, k=1)["rows_total"]
    gen_before = store.adapter._store.generation()

    store.assert_edge("n0", "n3", "R", {"w": 999}, vt_s=64, vt_e=70)
    after = paths(store.adapter, k=1)["rows_total"]
    assert store.adapter._store.generation() > gen_before
    assert after == before + 1, "the new 1-hop path must be visible to traversal"

    with np.load(index_file(store.adapter)) as z:
        assert int(z["generation"]) == store.adapter._store.generation(), \
            "the persisted index must be restamped for the new generation"
    store.close()


def test_a_foreign_stamp_is_ignored_not_trusted(tmp_path):
    store = build(tmp_path)
    idx = index_file(store.adapter)
    good = paths(store.adapter)

    # forge the file: right shapes, wrong permutation, wrong stamp — the
    # stamp alone must disqualify it before the content can mislead
    with np.load(idx) as z:
        forged = {k: z[k] for k in z.files}
    forged["generation"] = np.int64(int(forged["generation"]) + 7)
    forged["out_row"] = forged["out_row"][::-1].copy()
    np.savez(idx, **forged)

    fresh = NativeAdapter(store.adapter.path)
    assert paths(fresh) == good, "a foreign stamp must force a rebuild"
    with np.load(idx) as z:
        assert int(z["generation"]) == fresh._store.generation(), \
            "the rebuild must overwrite the forged file"
    fresh.close()
    store.close()


def test_a_damaged_file_degrades_to_rebuild(tmp_path):
    store = build(tmp_path)
    idx = index_file(store.adapter)
    good = paths(store.adapter)

    idx.write_bytes(b"not a zipfile at all")
    fresh = NativeAdapter(store.adapter.path)
    assert paths(fresh) == good, "corruption means rebuild, never an error"
    fresh.close()
    store.close()


def test_portable_backends_are_untouched(tmp_path):
    import tgms

    store = tgms.open(tmp_path, backend="duckdb")
    store.ingest_events([
        {"src": f"n{i % 7}", "dst": f"n{(i + 1) % 7}", "rel_type": "R",
         "vt_s": i, "vt_e": i + 5, "props": {"w": i}}
        for i in range(60)
    ])
    assert paths(store.adapter), "duckdb still answers through the base path"
    assert not (Path(tmp_path) / "index").exists()
    assert not (Path(tmp_path) / "native" / "index").exists()
    store.close()
