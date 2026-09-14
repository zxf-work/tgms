"""`tgms store backup` / `tgms store restore` (Lane A EXP-A6).

Backup is deliberately thin: the event log is the authoritative artifact
(`docs/STABILITY.md` §1), so a backup is a byte-for-byte copy of it plus a
manifest record identifying exactly what was copied (`store_identity`, the
source generation and its `manifest_sha`, the copy's own sha256/record
count, and the tooling that made it). Restore is `tgms replay`'s existing
path (copy the log into a fresh store, replay it, thread the cursor) plus a
verification step: the restored store's identity and logical digest must
match what the backup manifest recorded, printed as PASS/FAIL with a
nonzero exit on any mismatch — including a tampered backed-up log, caught
by a sha256 check before replay ever runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

import tgms  # noqa: E402
from tgms.cli import BACKUP_MANIFEST_NAME, main  # noqa: E402


def build(root: Path):
    from tgms.core.model import EntityRef

    store = tgms.open(root, backend="native")
    for i in range(5):
        store.assert_node(f"n{i}", "N", {"i": i}, vt_s=0, vt_e=100)
    store.assert_edge("n0", "n1", "R", {"w": 1}, vt_s=0, vt_e=100)
    store.correct(EntityRef(kind="node", uid="n0"), {"i": 999}, vt_s=0, vt_e=100)
    return store


def test_backup_then_restore_into_clean_dir_matches_digest(tmp_path):
    src = tmp_path / "src"
    store = build(src)
    identity = store.store_identity
    digest = store.digest()
    store.close()

    backup_dir = tmp_path / "backup"
    assert main(["store", "backup", "--store", str(src),
                 "--dest", str(backup_dir)]) == 0

    manifest_path = backup_dir / BACKUP_MANIFEST_NAME
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["store_identity"] == identity
    assert manifest["logical_digest"] == digest
    assert manifest["log_records"] > 0
    assert (backup_dir / "eventlog.jsonl").read_bytes() == \
        (src / "eventlog.jsonl").read_bytes()

    restored = tmp_path / "restored"
    assert main(["store", "restore", "--dest", str(backup_dir),
                 "--store", str(restored)]) == 0

    re = tgms.open(restored, backend="native")
    assert re.store_identity == identity
    assert re.digest() == digest
    v = re.adapter.verify()
    assert not v.get("problems"), v["problems"]
    re.close()


def test_backup_does_not_mutate_or_recover_the_source(tmp_path):
    """`store backup` opens the source `read_only=True`: it must never run
    recovery or publish a generation of its own (`Store._recover` is a
    writer's act) — the source's generation must be unchanged afterwards."""
    src = tmp_path / "src"
    store = build(src)
    gen_before = store.adapter.generation
    store.close()

    backup_dir = tmp_path / "backup"
    assert main(["store", "backup", "--store", str(src),
                 "--dest", str(backup_dir)]) == 0

    reopened = tgms.open(src, backend="native")
    assert reopened.adapter.generation == gen_before
    reopened.close()


def test_restore_of_a_tampered_backup_fails_loudly(tmp_path):
    src = tmp_path / "src"
    build(src).close()

    backup_dir = tmp_path / "backup"
    assert main(["store", "backup", "--store", str(src),
                 "--dest", str(backup_dir)]) == 0

    # flip one byte inside the backed-up log — the manifest's log_sha256 no
    # longer matches what is actually on disk.
    log_path = backup_dir / "eventlog.jsonl"
    raw = bytearray(log_path.read_bytes())
    raw[-5] ^= 0xFF
    log_path.write_bytes(bytes(raw))

    restored = tmp_path / "restored"
    assert main(["store", "restore", "--dest", str(backup_dir),
                 "--store", str(restored)]) != 0
    # the sha256 check happens before the target store is ever touched: no
    # half-restored directory is left behind that could look legitimate.
    assert not restored.exists()


def test_restore_reports_fail_on_manifest_mismatch(tmp_path, capsys):
    """A restore that replays cleanly but disagrees with the manifest
    (rather than failing the sha256 pre-check) must still print FAIL and
    exit nonzero — the loud-failure contract covers both routes."""
    src = tmp_path / "src"
    build(src).close()

    backup_dir = tmp_path / "backup"
    assert main(["store", "backup", "--store", str(src),
                 "--dest", str(backup_dir)]) == 0

    manifest_path = backup_dir / BACKUP_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["logical_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    restored = tmp_path / "restored"
    assert main(["store", "restore", "--dest", str(backup_dir),
                 "--store", str(restored)]) == 1
    out = capsys.readouterr().out
    assert '"verdict": "FAIL"' in out


def test_restore_requires_dest(tmp_path):
    assert main(["store", "restore", "--store", str(tmp_path / "x")]) == 2


def test_backup_requires_dest(tmp_path):
    src = tmp_path / "src"
    build(src).close()
    assert main(["store", "backup", "--store", str(src)]) == 2
