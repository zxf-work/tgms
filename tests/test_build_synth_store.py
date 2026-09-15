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


def _baseline_peak_kb(tmp_path: Path) -> int:
    """Peak RSS of a subprocess that does nothing but import `tgms` and open
    (then close) an empty native store at a fresh path -- the interpreter +
    import + engine-load footprint any `build_synth_store.py` subprocess
    pays before it ingests a single event, independent of `--n-entities`.

    Computed with the exact same platform-aware `ru_maxrss` handling as
    `build_synth_store.py::_rss_kb` (kB on Linux, bytes on macOS) so it is
    directly comparable to `build_info.peak_rss.maxrss_kb` from a real build.
    """
    out = tmp_path / "baseline_store"
    script = (
        "import sys, json, resource\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import tgms\n"
        f"s = tgms.open({str(out)!r}, backend='native')\n"
        "s.close()\n"
        "ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "maxrss_kb = ru // 1024 if sys.platform == 'darwin' else ru\n"
        "print(json.dumps({'maxrss_kb': maxrss_kb}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])["maxrss_kb"]


def test_peak_rss_bounded_not_by_n_times_batches(tmp_path: Path):
    """`ru_maxrss` (`getrusage(2)`) is a **process-lifetime** high-water
    mark, not a per-call measurement. Reading `result["rss"]` from an
    in-process `B.build()` call -- as this test used to -- measures the
    peak RSS of whatever else ran earlier in the same process, not of this
    build: on the GitHub Actions runner, running this file after other
    tests in the same pytest process, that was observed at ~1.64 GB (from
    earlier tests) against a true build peak of ~200 MB. Measuring in a
    fresh `subprocess` per build fixes this -- `ru_maxrss` there really is
    that build's own peak -- and the sidecar's `build_info.peak_rss.
    maxrss_kb` (populated by the driver's own `_rss_kb()`) is exactly that
    figure, correct precisely because `_rss_kb()` always runs inside a
    single build's own process.

    That subprocess fix was not enough by itself, though: on the GitHub
    Actions Linux runner the *floor* one subprocess pays just importing
    `tgms` (loading the compiled `_engine` extension, duckdb, numpy, ...)
    was observed at ~1.6 GB for both N=10k and N=50k -- an absolute 400 MB
    ceiling on `peak_rss` fails there even though the 2.5x N-scaling
    assertion (the actual regression this test guards against) passes
    cleanly. On macOS the same builds peak at ~80 MB / ~200 MB, i.e. the
    import floor there is small enough that the old absolute-ceiling
    version happened to pass. `_rss_kb()`'s unit handling (`ru_maxrss` is
    kB on Linux, bytes on macOS -- `build_synth_store.py`'s own
    `_rss_kb` already divides by 1024 only under `sys.platform ==
    "darwin"`) was checked and is correct on both platforms; the ~1.6 GB
    is a real, CI-specific import baseline, not a units bug.

    So the ceiling is now measured *over that baseline*
    (`_baseline_peak_kb`, a third subprocess that only imports `tgms` and
    opens an empty store) rather than as an absolute figure, which makes it
    portable across the two platforms' very different import footprints.
    Bound checked two ways: peak-over-baseline at N=50k (400 MB, generous
    headroom over the ~165 MB measured for a 50k/batch=250 build over its
    own baseline on the authoring laptop -- 200 MB peak minus ~35 MB
    baseline), and the actual property under test -- that peak RSS is a
    flat constant tied to store size at a given N, not to N times the
    number of batches (the regression this guards against is
    `build_dataset`'s own bug, `[_event(i, scale) for i in range(scale)]`
    fully materialized before any commit, whose cost grows with N). Both
    builds use the real `--batch 250` dispatch shape (SCALE_BUILD_FORECAST
    §1b) -- not the single-chunk `--batch 50000` `equivalence_pair` uses,
    which holds *more* in memory at once, not less, so it cannot stand in
    for this property.
    """
    def _build_peak_kb(n_entities: int, name: str) -> int:
        out = tmp_path / name
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_synth_store.py"),
             "--n-entities", str(n_entities), "--seed", "0", "--batch", "250",
             "--compact-every", "1000000", "--backend", "native", "--out", str(out)],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        record = json.loads((out / "build-record.json").read_text())
        return record["build_info"]["peak_rss"]["maxrss_kb"]

    baseline_kb = _baseline_peak_kb(tmp_path)
    peak_10k = _build_peak_kb(10_000, "rss10k")
    peak_50k = _build_peak_kb(50_000, "rss50k")

    assert peak_10k > 0
    assert peak_50k > 0
    assert peak_50k - baseline_kb < 400_000, (
        f"peak RSS over baseline exceeds the 400 MB bound: baseline={baseline_kb} kB, "
        f"peak_10k={peak_10k} kB, peak_50k={peak_50k} kB, "
        f"over_baseline={peak_50k - baseline_kb} kB")
    assert peak_50k <= 2.5 * peak_10k + 100_000, (
        f"peak RSS scales with N: baseline={baseline_kb} kB, 10k={peak_10k} kB, "
        f"50k={peak_50k} kB (expected 50k <= 2.5x 10k + 100 MB)")


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


#: The task's own digest-mode test scale -- big enough to force several
#: bulk-ingest batches and several `store_digest_streaming` spill chunks
#: (with a small `--batch`/`chunk_rows`), small enough to stay a laptop-scale
#: gate.
N_DIGEST = 20_000


def test_digest_mode_full_and_streaming_agree(tmp_path: Path):
    """`--digest full` and `--digest streaming`, run through the driver's own
    `--digest` plumbing at a scale where the bulk phase spans several
    batches, must land on the same `store.digest()` value for the *same*
    store.

    Building it twice independently (once per `digest_mode`) would not test
    this: `store.digest()` is tt-derived from the wall-clock
    `HybridLogicalClock`, so two independent ingests of identical logical
    content legitimately produce different digests regardless of
    `digest_mode` (D-023, this module's own "Digest equivalence" docstring
    section, and `tests/test_build_synth_store.py::
    test_equivalence_with_old_generator` above). So this builds once with
    `digest_mode="none"` (cheap -- no digest pass at build time) and then
    computes both digests directly on that one store afterward, which is
    exactly the byte-identity claim `store_digest_streaming` makes
    (`tests/test_store_digest_streaming.py` proves it in isolation; this
    reconfirms it through the driver's own call path)."""
    out = tmp_path / "once"
    result = B.build(out, N_DIGEST, 0, 2_000, 1_000_000, "native", False, None, "none")
    assert result["complete"] and result["digest_mode"] == "none"

    store = tgms.open(out, backend="native")
    full = store.adapter.store_digest()
    streaming = store.adapter.store_digest_streaming(chunk_rows=500)
    store.close()

    assert streaming == full


def test_digest_mode_none_writes_null_with_mode_recorded(tmp_path: Path):
    out = tmp_path / "none"
    result = B.build(out, N_DIGEST, 0, 2_000, 1_000_000, "native", False, None, "none")
    assert result["complete"]
    assert result["digest_mode"] == "none"
    assert result["store_digest"] is None
    assert result["content_digest"] is None

    record = json.loads((out / "build-record.json").read_text())
    assert record["config"]["digest_mode"] == "none"
    assert record["build_info"]["digest_mode"] == "none"
    assert record["build_info"]["store_digest"] is None
    assert record["build_info"]["content_digest"] is None

    # The schema's dataset.digest/result_digest are required non-empty
    # strings unconditionally -- "none" still owes the sidecar a real, cheap
    # (O(1)) identity rather than skipping the field outright.
    assert record["dataset"]["digest_kind"] == "manifest"
    assert isinstance(record["dataset"]["digest"], str) and record["dataset"]["digest"]
    assert isinstance(record["result_digest"], str) and record["result_digest"]

    schema = json.loads((ROOT / "benchmarks/schema/result_manifest.schema.json").read_text())
    jsonschema.validate(record, schema)


def test_finalisation_phases_present_on_a_20k_build(tmp_path: Path):
    """B7b (`SCALE_BUILD_FORECAST_2026-09-15.md` addendum 2): the sidecar
    records RSS-before/after and wall time for each of the four finalisation
    phases -- compact, gc, stats, digest -- separately, so a real run at
    30M/100M can show which one actually spiked instead of one lump
    60s-cadence RSS sample spanning all of them (which is what made the
    digest and the compact()+gc()+stats() sequence indistinguishable at
    P-SF1 scale in the first place)."""
    out = tmp_path / "finalisation"
    result = B.build(out, N_DIGEST, 0, 2_000, 1_000_000, "native", False, None, "streaming")
    assert result["complete"]

    phases = result["finalisation_phases"]
    for name in ("compact", "gc", "stats", "digest"):
        assert name in phases, f"missing finalisation phase {name!r}: {sorted(phases)}"
        entry = phases[name]
        assert set(entry) == {"rss_kb_before", "rss_kb_after", "wall_s"}
        assert isinstance(entry["rss_kb_before"], dict) and entry["rss_kb_before"]
        assert isinstance(entry["rss_kb_after"], dict) and entry["rss_kb_after"]
        assert isinstance(entry["wall_s"], (int, float)) and entry["wall_s"] >= 0

    record = json.loads((out / "build-record.json").read_text())
    assert record["build_info"]["finalisation_phases"] == phases

    schema = json.loads((ROOT / "benchmarks/schema/result_manifest.schema.json").read_text())
    jsonschema.validate(record, schema)


def test_digest_mode_default_scales_with_n_entities():
    assert B.default_digest_mode(0) == "full"
    assert B.default_digest_mode(B.DIGEST_AUTO_THRESHOLD - 1) == "full"
    assert B.default_digest_mode(B.DIGEST_AUTO_THRESHOLD) == "none"
    assert B.default_digest_mode(B.DIGEST_AUTO_THRESHOLD * 10) == "none"


def test_resume_parameter_mismatch_refuses(tmp_path: Path):
    out = tmp_path / "s"
    r = B.build(out, N_SMALL, 0, 20, 1_000_000, "native", False, stop_at_ops=30)
    assert not r["complete"]
    with pytest.raises(SystemExit):
        B.build(out, N_SMALL, seed=1, batch=20, compact_every=1_000_000,
               backend="native", resume=True, stop_at_ops=None)
