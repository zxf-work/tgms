"""`tgms store backup` / `restore` / `verify` on a store that is not native.

`docs/STABILITY.md` section 7: a backup is the event log plus a manifest, and
a restore replays that log into a fresh native store and compares its
`store_identity` and logical digest with the manifest. The logical digest is
`StorageAdapter.store_digest`, one definition for every backend, so a DuckDB
store's backup is checked by the same restore.

The released v0.8.0 opened every store as a native one. Given a DuckDB store,
its `store backup` created an empty native store under `<store>/native/` and
recorded that empty store's digest, so the restore printed FAIL. Such a
DuckDB store, with an empty `native/` beside `store.duckdb`, is still a DuckDB
store: backup, restore and verify must treat it as one.

The stores here are built from the v0.8.0 fixture log
(`tests/fixtures/upgrade_from_0_8_0/`), so their answers can be compared with
the values `expected.json` keeps. Which backend the round trip uses follows
`TGMS_TEST_BACKEND` (default `duckdb`, as in `tests/conftest.py`); the tests
whose subject is a DuckDB store run in the `duckdb` suite only.
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
from tgms.cli import BACKUP_MANIFEST_NAME, main  # noqa: E402
from tgms.storage.native import NativeAdapter  # noqa: E402
from tgms.tools.server import ToolRouter  # noqa: E402

BACKEND = os.environ.get("TGMS_TEST_BACKEND", "duckdb")
FIXTURE = Path(__file__).parent / "fixtures" / "upgrade_from_0_8_0"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text())
duckdb_only = pytest.mark.skipif(BACKEND != "duckdb",
                                 reason="a DuckDB store is the subject; runs in the duckdb suite")


def checksums(root: Path) -> dict[str, str]:
    """Every path under `root`, files with their sha256 and directories marked,
    so a new empty directory counts as a change."""
    return {str(p.relative_to(root)):
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
            for p in sorted(root.rglob("*"))}


def scrub(o):
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k != "semantic_identity"}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    return o


def answers(store_dir: Path) -> dict[str, str]:
    store = tgms.open(store_dir, read_only=True)
    try:
        router = ToolRouter(store.adapter, tt_source=store)
        out = {}
        for c in EXPECTED["calls"]:
            r = router.call(c["op"], c["args"], request_id=c["id"])
            text = json.dumps(scrub(r), sort_keys=True, separators=(",", ":"), default=str)
            out[c["id"]] = hashlib.sha256(text.encode()).hexdigest()
        return out
    finally:
        store.close()


def expected_answers(backend: str) -> dict[str, str]:
    return {c["id"]: c["digest"][backend] for c in EXPECTED["calls"]}


def cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    rc = main(list(argv))
    got = capsys.readouterr()
    return (0 if rc is None else rc), got.out, got.err


def store_from_fixture_log(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                           backend: str) -> Path:
    log = tmp_path / "fixture-eventlog.jsonl"
    shutil.copyfile(FIXTURE / "store" / "eventlog.jsonl", log)
    dst = tmp_path / f"src-{backend}"
    rc, _, _ = cli(capsys, "replay", str(log), "--store", str(dst), "--backend", backend)
    assert rc == 0
    return dst


def touch_like_0_8_0(store_dir: Path) -> None:
    """What v0.8.0's `store backup` left inside a DuckDB store: an empty native
    store (generation 0) under `native/`, next to `store.duckdb`."""
    NativeAdapter(store_dir / "native").close()
    assert (store_dir / "store.duckdb").is_file()
    assert sorted(p.name for p in (store_dir / "native" / "manifests").iterdir()) == [
        "00000000000000000000.json"]
    assert not any((store_dir / "native" / "seg").iterdir())


def backup_and_restore(capsys: pytest.CaptureFixture[str], tmp_path: Path, src: Path) -> dict:
    before = checksums(src)
    backup = tmp_path / "backup"
    rc, out, err = cli(capsys, "store", "backup", "--store", str(src), "--dest", str(backup))
    assert rc == 0, err
    assert checksums(src) == before  # no file changed, none added, no directory created
    manifest = json.loads((backup / BACKUP_MANIFEST_NAME).read_text())
    assert json.loads(out) == manifest
    assert (backup / "eventlog.jsonl").read_bytes() == (src / "eventlog.jsonl").read_bytes()
    assert manifest["store_identity"] == EXPECTED["store_identity"]
    assert manifest["logical_digest"] == EXPECTED["logical_digest"][BACKEND]

    restored = tmp_path / "restored"
    rc, out, err = cli(capsys, "store", "restore", "--dest", str(backup), "--store", str(restored))
    report = json.loads(out)
    assert (rc, report["verdict"]) == (0, "PASS"), (out, err)
    assert report["store_identity"] == EXPECTED["store_identity"]
    assert report["logical_digest"] == manifest["logical_digest"]
    # restore builds a native store; its answers are the native answers
    got = answers(restored)
    assert [k for k, v in expected_answers("native").items() if got.get(k) != v] == []
    return manifest


def test_backup_of_a_store_of_the_selected_backend(capsys: pytest.CaptureFixture[str],
                                                   tmp_path: Path) -> None:
    src = store_from_fixture_log(capsys, tmp_path, BACKEND)
    src_answers = answers(src)
    assert [k for k, v in expected_answers(BACKEND).items() if src_answers.get(k) != v] == []
    manifest = backup_and_restore(capsys, tmp_path, src)
    if BACKEND == "native":
        assert isinstance(manifest["generation"], int)
        assert isinstance(manifest["manifest_sha"], str)
    else:
        # a store without native generations has neither value to record
        assert manifest["generation"] is None and manifest["manifest_sha"] is None


@duckdb_only
def test_backup_of_a_duckdb_store_touched_by_0_8_0(capsys: pytest.CaptureFixture[str],
                                                    tmp_path: Path) -> None:
    src = store_from_fixture_log(capsys, tmp_path, "duckdb")
    touch_like_0_8_0(src)
    manifest = backup_and_restore(capsys, tmp_path, src)
    assert manifest["generation"] is None and manifest["manifest_sha"] is None


def test_native_backup_manifest_keys_and_values(capsys: pytest.CaptureFixture[str],
                                                tmp_path: Path) -> None:
    """A native store's backup manifest keeps every key, in order, with the
    source generation and `manifest_sha`, written as `json.dumps(indent=1)`."""
    src = store_from_fixture_log(capsys, tmp_path, "native")
    store = tgms.open(src, read_only=True)
    try:
        generation = store.adapter.generation
        manifest_sha = store.adapter._store.manifest_sha()
    finally:
        store.close()
    backup = tmp_path / "backup"
    rc, _, _ = cli(capsys, "store", "backup", "--store", str(src), "--dest", str(backup))
    assert rc == 0
    raw = (backup / BACKUP_MANIFEST_NAME).read_text()
    manifest = json.loads(raw)
    assert list(manifest) == ["store_identity", "generation", "manifest_sha", "log_sha256",
                              "log_records", "logical_digest", "tgms_version", "commit"]
    assert raw == json.dumps(manifest, indent=1) + "\n"
    assert (manifest["generation"], manifest["manifest_sha"]) == (generation, manifest_sha)
    assert manifest["log_sha256"] == EXPECTED["eventlog_sha256"]
    assert manifest["log_records"] == EXPECTED["backup_manifest"]["log_records"]
    assert manifest["logical_digest"] == EXPECTED["logical_digest"]["native"]


@duckdb_only
@pytest.mark.parametrize("touched", [False, True], ids=["clean", "touched-by-0.8.0"])
def test_verify_refuses_a_duckdb_store(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                       touched: bool) -> None:
    """verify checks native stores only. On a DuckDB store it says so and exits
    2 ("not checked"), never 0 — also when 0.8.0 left an empty `native/`."""
    src = store_from_fixture_log(capsys, tmp_path, "duckdb")
    if touched:
        touch_like_0_8_0(src)
    before = checksums(src)
    want = (f"{src} is a duckdb store; store verify checks native stores only, "
            f"so this store was not checked")

    rc, out, err = cli(capsys, "store", "verify", "--store", str(src))
    assert (rc, out, err) == (2, "", f"store verify: {want}\n")

    rc, out, err = cli(capsys, "store", "verify", "--store", str(src), "--full", "--json")
    assert (rc, err) == (2, "")
    assert json.loads(out) == {"store": str(src), "mode": "full", "readable": False,
                               "backend": "duckdb", "reason": want}

    rc, out, err = cli(capsys, "check", str(src))
    assert rc == 2 and want in err
    assert checksums(src) == before
