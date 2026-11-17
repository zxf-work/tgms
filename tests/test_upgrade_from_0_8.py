"""A store written by the released tgms 0.8.0, used by this tree.

`docs/STABILITY.md` section 1 makes the event log the stable artifact: the
derived store may be rebuilt across versions, `store_identity` is derived
from the log, and there is no downgrade. This file checks that promise
against a store the released 0.8.0 wheel actually wrote, vendored under
`tests/fixtures/upgrade_from_0_8_0/` the way `format1_store` and
`format2_store` are vendored for `tests/test_manifest_format2.py`.

**How the fixture was produced.** With tgms 0.8.0 installed from PyPI
(`tgms-0.8.0-cp312-cp312-manylinux_2_28_x86_64.whl`, native engine 0.8.0,
manifest format 3, duckdb 1.5.4) in a clean virtual environment, from a
working directory outside any git checkout::

    python tests/fixtures/upgrade_from_0_8_0/generate.py tests/fixtures/upgrade_from_0_8_0

`generate.py`'s docstring lists every step. In short: one deterministic
twelve-batch history (several loads, corrections and a retraction that
change what was believed about earlier valid times), written once per
backend; a `tgms store compact` of the native store; five artifacts
registered with `tgms artifact register`; `tgms artifact check --json`
recorded right after registration (state 1) and after one more correction
that overlaps some artifacts and not others (state 2); a `tgms store backup`
of the native store; and the digest of each answer of a fixed call list
covering all fifteen 0.8.0 operators, per backend. A rerun reproduces the
files byte for byte, because transaction times come from a constant clock.

**What is vendored.** `store/` is the native store 0.8.0 wrote (event log,
artifact registry, operator blobs, derived store). `backup/` is its backup.
`duckdb_artifacts.jsonl` is the registry 0.8.0 wrote against the DuckDB store
of the same log; the DuckDB database file itself (about 3 MB) is not kept.
`expected.json` holds 0.8.0's values. `duckdb_backup_manifest.json` is the
manifest 0.8.0's `tgms store backup` wrote for a DuckDB store of the same log
(0.8.0's `tgms replay --backend duckdb` of `store/eventlog.jsonl`, then
`tgms store backup`); its event log is `store/eventlog.jsonl` byte for byte,
so only the manifest is kept.

**What is asserted, per `TGMS_TEST_BACKEND`** (default `duckdb`, as in
`tests/conftest.py`):

- (a) native only: opening 0.8.0's derived store with read-only entry points
  (the call list, `tgms store verify --fast` and `--full`) succeeds and
  changes no file. Under `duckdb` there is no vendored DuckDB store to open,
  so this is skipped.
- (b) the store identity, and every artifact's `record_digest`, equal 0.8.0's.
- (c) the call list's answers equal 0.8.0's answers on the same backend: on
  `native` from 0.8.0's own derived store, on `duckdb` from a store this tree
  replays from the 0.8.0 log (the only DuckDB store the fixture can give).
- (d) `tgms replay` of the 0.8.0 log into a fresh store of the selected
  backend gives the same identity, logical digest and answers.
- (e) `tgms artifact check --json` on the 0.8.0 registry gives 0.8.0's verdict
  and the same verdict document, in both states.
- (g) `tgms store restore` of the 0.8.0 backup prints PASS and gives the same
  identity and answers. Restore builds a native store in 0.8.0 and in this
  tree alike, so it is compared with the native answers under either value.
- (g2) 0.8.0's backup of a DuckDB store recorded the logical digest of an
  empty native store, so its restore prints FAIL here as it does in 0.8.0.
  The restored store still has the right identity and answers. The remedy is
  to take the backup again (`docs/STABILITY.md` section 7); restore does not
  special-case the old manifest.

An answer's digest leaves out one field, `semantic_identity`
(`tgms/<package version>`): it is a version stamp, so it differs between any
two versions by construction. Everything else in the envelope is compared.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

import tgms  # noqa: E402
from tgms.cli import main  # noqa: E402
from tgms.tools.server import ToolRouter  # noqa: E402

BACKEND = os.environ.get("TGMS_TEST_BACKEND", "duckdb")
FIXTURE = Path(__file__).parent / "fixtures" / "upgrade_from_0_8_0"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text())


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def scrub(o):
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k != "semantic_identity"}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    return o


def digest(o) -> str:
    return hashlib.sha256(canon(scrub(o)).encode()).hexdigest()


def checksums(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    rc = main(list(argv))
    return (0 if rc is None else rc), capsys.readouterr().out


def answers(store_dir: Path) -> dict[str, str]:
    store = tgms.open(store_dir, read_only=True)
    try:
        router = ToolRouter(store.adapter, tt_source=store)
        return {c["id"]: digest(router.call(c["op"], c["args"], request_id=c["id"]))
                for c in EXPECTED["calls"]}
    finally:
        store.close()


def expected_answers(backend: str) -> dict[str, str]:
    return {c["id"]: c["digest"][backend] for c in EXPECTED["calls"]}


def assert_same_answers(got: dict[str, str], backend: str) -> None:
    want = expected_answers(backend)
    assert [k for k in want if got.get(k) != want[k]] == []


def replayed(capsys: pytest.CaptureFixture[str], tmp_path: Path, backend: str) -> Path:
    """A fresh store this tree builds from the 0.8.0 event log with `tgms replay`."""
    log = tmp_path / "old-eventlog.jsonl"
    shutil.copyfile(FIXTURE / "store" / "eventlog.jsonl", log)
    dst = tmp_path / f"replayed-{backend}"
    rc, _ = cli(capsys, "replay", str(log), "--store", str(dst), "--backend", backend)
    assert rc == 0
    return dst


def old_store_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "old-native"
    shutil.copytree(FIXTURE / "store", dst)
    return dst


def store_for_backend(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> Path:
    return old_store_copy(tmp_path) if BACKEND == "native" else replayed(capsys, tmp_path, BACKEND)


def test_fixture_is_the_one_expected_json_describes() -> None:
    log = (FIXTURE / "store" / "eventlog.jsonl").read_bytes()
    assert hashlib.sha256(log).hexdigest() == EXPECTED["eventlog_sha256"]
    assert (FIXTURE / "backup" / "eventlog.jsonl").read_bytes() == log
    assert EXPECTED["produced_by"]["tgms"] == "0.8.0"
    assert {c["op"] for c in EXPECTED["calls"]} == {
        "compute", "graph_metric_timeseries", "burst_detection", "co_active",
        "aggregate_events", "count_temporal_motifs", "find_temporal_motif_instances",
        "temporal_reachability", "temporal_paths", "entity_history", "snapshot_subgraph",
        "diff_snapshots", "neighborhood_evolution", "resolve_entities", "version_history"}


# --- (a) ------------------------------------------------------------------ #

@pytest.mark.skipif(BACKEND != "native", reason="no vendored DuckDB store (see docstring)")
def test_a_read_only_open_of_the_old_store_changes_no_file(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    store_dir = old_store_copy(tmp_path)
    before = checksums(store_dir)
    assert_same_answers(answers(store_dir), "native")
    for mode in ("--fast", "--full"):
        rc, out = cli(capsys, "store", "verify", "--store", str(store_dir), mode, "--json")
        report = json.loads(out)
        assert rc == 0 and report["healthy"] and report["findings"] == []
        assert report["manifest_format"] == 3
    assert checksums(store_dir) == before


# --- (b) ------------------------------------------------------------------ #

def test_b_store_identity(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    store = tgms.open(store_for_backend(capsys, tmp_path), read_only=True)
    try:
        assert store.store_identity == EXPECTED["store_identity"]
        assert store.store_identity == EXPECTED["backup_manifest"]["store_identity"]
    finally:
        store.close()


def registry_dir(tmp_path: Path, backend: str, log_bytes: int | None = None) -> Path:
    """The 0.8.0 log (optionally cut back to state 1), registry and operator blobs:
    everything `tgms artifact` reads. It never opens the derived store."""
    d = tmp_path / f"registry-{backend}-{log_bytes}"
    d.mkdir()
    log = (FIXTURE / "store" / "eventlog.jsonl").read_bytes()
    (d / "eventlog.jsonl").write_bytes(log if log_bytes is None else log[:log_bytes])
    src = (FIXTURE / "store" / "artifacts.jsonl" if backend == "native"
           else FIXTURE / "duckdb_artifacts.jsonl")
    shutil.copyfile(src, d / "artifacts.jsonl")
    shutil.copytree(FIXTURE / "store" / "ops", d / "ops")
    return d


def test_b_artifact_identities(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, out = cli(capsys, "artifact", "list", str(registry_dir(tmp_path, BACKEND)), "--json")
    assert rc == 0
    got = sorted([r["name"], r["generation"], r["record_digest"]] for r in json.loads(out))
    assert got == sorted(EXPECTED["artifacts"][BACKEND])


# --- (c) and (d) ---------------------------------------------------------- #

def test_c_answers_on_the_old_store(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert_same_answers(answers(store_for_backend(capsys, tmp_path)), BACKEND)


def test_d_replay_of_the_old_log(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    dst = replayed(capsys, tmp_path, BACKEND)
    store = tgms.open(dst, read_only=True)
    try:
        assert store.backend == BACKEND
        assert store.store_identity == EXPECTED["store_identity"]
        assert store.digest() == EXPECTED["logical_digest"][BACKEND]
    finally:
        store.close()
    assert_same_answers(answers(dst), BACKEND)


# --- (e) ------------------------------------------------------------------ #

@pytest.mark.parametrize("state", ["state1", "state2"])
def test_e_freshness_verdicts(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                              state: str) -> None:
    cut = EXPECTED["state1_log_bytes"] if state == "state1" else None
    d = registry_dir(tmp_path, BACKEND, cut)
    want = EXPECTED["freshness"][BACKEND][state]
    for name, w in want.items():
        rc, out = cli(capsys, "artifact", "check", str(d), "--name", name, "--json")
        verdict = json.loads(out)
        assert (name, rc, verdict["verdict"]) == (name, w["rc"], w["verdict"])
        assert digest(verdict) == w["digest"], name


# --- (g) ------------------------------------------------------------------ #

def test_g_restore_of_the_old_backup(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    shutil.copytree(FIXTURE / "backup", backup)
    dst = tmp_path / "restored"
    rc, out = cli(capsys, "store", "restore", "--dest", str(backup), "--store", str(dst))
    report = json.loads(out)
    assert rc == 0 and report["verdict"] == "PASS"
    assert report["store_identity"] == EXPECTED["store_identity"]
    assert report["logical_digest"] == EXPECTED["backup_manifest"]["logical_digest"]
    assert_same_answers(answers(dst), "native")


def test_g2_restore_of_the_old_duckdb_backup(capsys: pytest.CaptureFixture[str],
                                             tmp_path: Path) -> None:
    manifest = json.loads((FIXTURE / "duckdb_backup_manifest.json").read_text())
    assert manifest["log_sha256"] == EXPECTED["eventlog_sha256"]
    assert manifest["store_identity"] == EXPECTED["store_identity"]
    assert manifest["logical_digest"] != EXPECTED["logical_digest"]["duckdb"]
    backup = tmp_path / "backup"
    backup.mkdir()
    shutil.copyfile(FIXTURE / "duckdb_backup_manifest.json", backup / "backup_manifest.json")
    shutil.copyfile(FIXTURE / "store" / "eventlog.jsonl", backup / "eventlog.jsonl")
    dst = tmp_path / "restored"
    rc, out = cli(capsys, "store", "restore", "--dest", str(backup), "--store", str(dst))
    report = json.loads(out)
    assert rc == 1 and report["verdict"] == "FAIL"
    assert report["store_identity"] == EXPECTED["store_identity"]
    assert report["expected_logical_digest"] == manifest["logical_digest"]
    assert report["logical_digest"] == EXPECTED["logical_digest"]["native"]
    assert_same_answers(answers(dst), "native")
