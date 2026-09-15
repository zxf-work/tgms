"""Tests for `scripts/build_synth_store.py` — the streaming, N-parametrised
synth-family build driver (`docs/design/SCALE_BUILD_FORECAST_2026-09-15.md`
B7a). Covers: content-digest equivalence with the old, unstreamed generator
(`scripts/eval_harness.py::build_dataset`) at the one scale/batch combination
where they are provably comparable; batch-size invariance of edge content;
resume correctness across a `--stop-at-ops` interruption; the `build-
record.json` sidecar's conformance to `benchmarks/schema/
result_manifest.schema.json`; and a peak-RSS bound that would catch a
regression back to materializing the whole event stream as a list.

Everything here runs at `--n-entities` 6,000 or 50,000 (never anything
resembling the real 30M/100M dispatch, which is xzgpu-only per
`docs/design/SCALE_BUILD_FORECAST_2026-09-15.md` and the "laptop builds
nothing larger than ~50k entities" rule this lane was scoped under) and the
whole file is a few tens of seconds.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, relpath: str) -> Any:
    # Both scripts do unqualified `import <other_script>` internally when run
    # as `python scripts/foo.py`; put scripts/ on sys.path so that resolves
    # the same way here (mirrors test_eval_concurrency_commitcost.py).
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load_module("build_synth_store", "scripts/build_synth_store.py")
H = _load_module("eval_harness", "scripts/eval_harness.py")

import tgms  # noqa: E402

#: The equivalence scale the task specifies. Also used for the RSS-bound and
#: sidecar-schema checks, sharing the one build via `equivalence_pair` below
#: rather than rebuilding a 50k store per test.
N_EQUIV = 50_000
#: Small scale for the batch-invariance and resume tests -- correctness of
#: those properties does not depend on N, only on there being more than one
#: batch's worth of events and corrections. Kept deliberately tiny: the
#: epoch-2/partial/retract phases commit one write batch per correction
#: (`build_dataset`'s own shape, ~30ms/commit measured), and their count is
#: `~min(N, 200)`-ish regardless of N (the `max(1, N // 200)` step floor), so
#: a bigger N here buys nothing but wall-clock -- it does not exercise a
#: different code path.
N_SMALL = 100


@pytest.fixture(scope="module")
def equivalence_pair(tmp_path_factory: pytest.TempPathFactory):
    """One `build_dataset(50000)` (old) and one `build_synth_store.build`
    at `--batch 50000` (new, single-chunk -- the one configuration where
    the two are provably comparable; see build_synth_store.py's module
    docstring, "Digest equivalence"). Built once, shared by the equivalence,
    sidecar-schema and RSS-bound tests."""
    old_ds = H.build_dataset(N_EQUIV)
    old_store = tgms.open(old_ds.log.parent, backend="native")

    out = tmp_path_factory.mktemp("synth50k") / "store"
    result = B.build(out, N_EQUIV, 0, N_EQUIV, 1_000_000, "native", False, None)
    new_store = tgms.open(out, backend="native")

    yield old_store, new_store, out, result

    old_store.close()
    new_store.close()


def test_equivalence_with_old_generator(equivalence_pair):
    """The one thing the task asks to prove: at N=50k, seed=0, --batch equal
    to N (matching `build_dataset`'s own single-chunk `ingest_events` call at
    this scale), the new driver's logical content is identical to the old
    generator's -- and `store.digest()` is *not*, which is expected (D-023,
    documented in both scripts and in docs/eval/DATASET_CARDS.md)."""
    old_store, new_store, out, result = equivalence_pair

    old_cd = B.content_digest(old_store)
    new_cd = B.content_digest(new_store)
    assert old_cd == new_cd
    assert result["content_digest"] == old_cd

    # store_digest() is tt-derived (HybridLogicalClock is wall-clock-seeded)
    # and is never expected to match between independent builds -- asserting
    # the difference pins that this is understood, not an oversight.
    assert old_store.digest() != new_store.digest()
    assert old_store.digest() != result["store_digest"]


def test_sidecar_conforms_to_result_manifest_schema(equivalence_pair):
    _, _, out, _ = equivalence_pair
    record_path = out / "build-record.json"
    assert record_path.exists()

    schema = json.loads((ROOT / "benchmarks/schema/result_manifest.schema.json").read_text())
    data = json.loads(record_path.read_text())
    jsonschema.validate(data, schema)

    assert "build_info" in data
    assert data["build_info"]["n_entities"] == N_EQUIV
    assert data["build_info"]["content_digest"] == data["result_digest"]

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_result_manifest.py"), str(record_path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_peak_rss_bounded_not_by_n_times_batches(equivalence_pair):
    """~200 MB measured for a 50k/batch=250 build on the authoring laptop
    (this fixture uses --batch 50000, one big commit, which if anything
    holds more in memory at once than the production --batch 250 shape).
    400 MB is a flat constant tied to the store's own size at this N, not to
    N or to the batch count -- the regression this guards against is
    `build_dataset`'s own bug, `[_event(i, scale) for i in range(scale)]`
    fully materialized before any commit, whose cost grows with N."""
    _, _, _, result = equivalence_pair
    maxrss_kb = result["rss"]["maxrss_kb"]
    assert maxrss_kb > 0
    assert maxrss_kb < 400_000, f"peak RSS {maxrss_kb} kB exceeds the 400 MB bound"


def test_determinism_per_batch(tmp_path: Path):
    """Edge-version content (identity, endpoints, rel_type, vt_s/vt_e, props)
    is fully --batch-invariant: disc is stamped explicitly per event
    (`f"#{i}"`), never left to `Store.ingest_events`'s own per-call offset
    default, which resets to 0 on every top-level call (see
    build_synth_store.py's module docstring for the bug this avoids)."""
    digests = set()
    for i, batch in enumerate((7, 23, N_SMALL)):
        out = tmp_path / f"b{i}"
        result = B.build(out, N_SMALL, 0, batch, 1_000_000, "native", False, None)
        store = tgms.open(out, backend="native")
        digests.add(B.content_digest(store, kinds=("edges",)))
        store.close()
        assert result["complete"]
    assert len(digests) == 1, f"edge content digest varies with --batch: {digests}"


def test_resume_matches_uninterrupted_build(tmp_path: Path):
    n, seed, batch = N_SMALL, 0, 20
    ref = B.build(tmp_path / "ref", n, seed, batch, 1_000_000, "native", False, None)
    assert ref["complete"]

    # ref["ops"] is ~237 at N_SMALL=100 (100 bulk + 1 + 100 corrections + 1 +
    # 10 partial + 25 retract) -- both stop points land mid-phase.
    out = tmp_path / "resumed"
    r1 = B.build(out, n, seed, batch, 1_000_000, "native", False, stop_at_ops=60)
    assert not r1["complete"] and 0 < r1["ops"] < ref["ops"]
    r2 = B.build(out, n, seed, batch, 1_000_000, "native", True, stop_at_ops=150)
    assert not r2["complete"] and r1["ops"] < r2["ops"] < ref["ops"]
    r3 = B.build(out, n, seed, batch, 1_000_000, "native", True, stop_at_ops=None)
    assert r3["complete"]

    assert r3["ops"] == ref["ops"]
    assert r3["content_digest"] == ref["content_digest"]


def test_resume_without_flag_refuses(tmp_path: Path):
    out = tmp_path / "s"
    r = B.build(out, N_SMALL, 0, 20, 1_000_000, "native", False, stop_at_ops=30)
    assert not r["complete"]
    with pytest.raises(SystemExit):
        B.build(out, N_SMALL, 0, 20, 1_000_000, "native", False, None)


def test_resume_parameter_mismatch_refuses(tmp_path: Path):
    out = tmp_path / "s"
    r = B.build(out, N_SMALL, 0, 20, 1_000_000, "native", False, stop_at_ops=30)
    assert not r["complete"]
    with pytest.raises(SystemExit):
        B.build(out, N_SMALL, seed=1, batch=20, compact_every=1_000_000,
               backend="native", resume=True, stop_at_ops=None)
