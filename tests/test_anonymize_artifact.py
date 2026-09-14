"""Tests for `scripts/anonymize_artifact.py` (OSDI campaign, lane W4).

The script builds a scrubbed copy of the artifact bundle for double-blind
review; the repository itself keeps the real provenance. These tests build
a tiny synthetic mini-repo under `tmp_path` (never the real repo — the real
tables/greps live in the task's own report, not here) that reproduces the
shapes the real repo actually has: a benchmark JSON record with a
`machine.host`-style field, a benchmark README mentioning a cluster node
name, the Slurm partition, the university domain, and a PI email, plus a
`/project/<user>/...` path buried in both a JSON string and prose.

Module loaded fresh via `importlib.util`, matching the pattern
`tests/test_osdi_paper_macros.py` already uses for `scripts/*.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("anonymize_artifact", ROOT / "scripts" / "anonymize_artifact.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclass's field-type resolution looks itself up in sys.modules by
    # __module__ name, so the module must be registered before exec runs.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


aa = _load()


RECORD_JSON = {
    "schema_version": 1,
    "machine": {
        "host": "itiger01,itiger02",
        "cpus": 4,
        "ram_gb": 8.0,
    },
    "note": "reached via xzgpu.uom.memphis.edu, staged at /project/xzhang12/store",
    "total_trials": 2000,
    "result_digest": "deadbeef1234cafe",
}

README_MD = """\
# sample-v1

Ran on itiger04 (partition `bigTiger`), cross-checked against
https://memphis.edu. Contact zxfhkust@gmail.com with questions.
Staged at /project/xzhang12/work before merging.
"""


def _make_repo(tmp_path: Path) -> Path:
    src = tmp_path / "repo"
    bench_dir = src / "benchmarks" / "sample-v1"
    bench_dir.mkdir(parents=True)
    (bench_dir / "record.json").write_text(json.dumps(RECORD_JSON, indent=2) + "\n", encoding="utf-8")
    (bench_dir / "README.md").write_text(README_MD, encoding="utf-8")
    return src


def _run(src: Path, out: Path, *, dry_run: bool = False, extra: list[str] | None = None) -> int:
    args = ["--src", str(src), "--out", str(out)]
    if dry_run:
        args.append("--dry-run")
    for pat in extra or []:
        args += ["--include", pat]
    return aa.main(args)


# --------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path, capsys):
    src = _make_repo(tmp_path)
    out = tmp_path / "bundle"
    rc = _run(src, out, dry_run=True)
    assert rc == 0
    assert not out.exists()
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    # dry-run output must report counts, never the original secret strings
    assert "itiger" not in captured.out
    assert "xzgpu" not in captured.out
    assert "xzhang" not in captured.out


def test_bundle_is_clean_and_manifest_records_counts(tmp_path):
    src = _make_repo(tmp_path)
    out = tmp_path / "bundle"
    rc = _run(src, out)
    assert rc == 0

    record_out = json.loads((out / "benchmarks" / "sample-v1" / "record.json").read_text())
    readme_out = (out / "benchmarks" / "sample-v1" / "README.md").read_text()

    # every real identifier is gone from the written bundle
    for secret in ("itiger01", "itiger02", "itiger04", "xzgpu", "bigTiger",
                   "memphis.edu", "zxfhkust@gmail.com", "xzhang12"):
        assert secret not in json.dumps(record_out)
        assert secret not in readme_out

    # numbers and keys are untouched
    assert record_out["machine"]["cpus"] == 4
    assert record_out["machine"]["ram_gb"] == 8.0
    assert record_out["total_trials"] == 2000
    assert record_out["result_digest"] == "deadbeef1234cafe"  # never recomputed
    assert set(record_out.keys()) == set(RECORD_JSON.keys())
    assert set(record_out["machine"].keys()) == set(RECORD_JSON["machine"].keys())

    # host field was rewritten, not deleted, and stays a comma-joined string
    new_host = record_out["machine"]["host"]
    assert new_host.count(",") == 1
    assert all(part.startswith("H-array-") for part in new_host.split(","))

    manifest = json.loads((out / "bundle-manifest.json").read_text())
    files_by_path = {f["path"]: f for f in manifest["files"]}
    record_entry = files_by_path["benchmarks/sample-v1/record.json"]
    readme_entry = files_by_path["benchmarks/sample-v1/README.md"]

    # manifest never carries the original strings
    manifest_text = json.dumps(manifest)
    for secret in ("itiger01", "itiger02", "itiger04", "xzgpu.uom.memphis.edu",
                   "bigTiger", "memphis.edu", "zxfhkust@gmail.com", "/project/xzhang12"):
        assert secret not in manifest_text

    # counts match what we planted
    assert record_entry["replacements"]["cluster_node_name"] == 2  # itiger01, itiger02
    assert record_entry["replacements"]["gpu_server_fqdn"] == 1  # xzgpu.uom.memphis.edu
    assert record_entry["replacements"]["path_project_cluster_user"] == 1

    assert readme_entry["replacements"]["cluster_node_name"] == 1  # itiger04
    assert readme_entry["replacements"]["slurm_partition"] == 1  # bigTiger
    assert readme_entry["replacements"]["university_domain"] == 1  # memphis.edu
    assert readme_entry["replacements"]["pi_email"] == 1
    assert readme_entry["replacements"]["path_project_cluster_user"] == 1

    assert manifest["totals"]["cluster_node_name"] == 3
    assert manifest["totals"]["path_project_cluster_user"] == 2

    # before/after sha256 recorded and actually differ (something changed)
    assert record_entry["sha256_before"] != record_entry["sha256_after"]
    assert len(record_entry["sha256_before"]) == 64
    assert len(record_entry["sha256_after"]) == 64

    # bundle-README.md explains digests weren't recomputed
    bundle_readme = (out / "bundle-README.md").read_text()
    assert "not" in bundle_readme.lower() and "recomputed" in bundle_readme.lower()


def test_json_still_parses_after_rewrite(tmp_path):
    src = _make_repo(tmp_path)
    out = tmp_path / "bundle"
    assert _run(src, out) == 0
    # would raise if invalid
    json.loads((out / "benchmarks" / "sample-v1" / "record.json").read_text())


def test_verification_fails_on_planted_leftover(tmp_path):
    src = _make_repo(tmp_path)
    out = tmp_path / "bundle"
    assert _run(src, out) == 0

    table = aa.build_replacement_table()
    clean = aa.verify_bundle(out, table, aa.VERIFICATION_SENTINELS)
    assert clean == []

    # plant a leftover identifier directly into a written bundle file,
    # bypassing the anonymizer entirely (as if a future bug let one slip)
    readme_path = out / "benchmarks" / "sample-v1" / "README.md"
    text = readme_path.read_text()
    readme_path.write_text(text + "\ncontact xzhang12 on itiger05 for details\n")

    violations = aa.verify_bundle(out, table, aa.VERIFICATION_SENTINELS)
    assert violations != []
    assert any("xzhang" in v.lower() or "cluster_node_name" in v or "user_token" in v for v in violations)


def test_conditional_exclude_ops_failure_ledger(tmp_path):
    src = _make_repo(tmp_path)
    (src / "ops").mkdir()
    (src / "ops" / "failure_ledger.jsonl").write_text('{"note": "itiger01 broke"}\n', encoding="utf-8")

    out1 = tmp_path / "bundle-default"
    assert _run(src, out1) == 0
    assert not (out1 / "ops").exists()

    out2 = tmp_path / "bundle-included"
    assert _run(src, out2, extra=["ops/failure_ledger.jsonl"]) == 0
    assert (out2 / "ops" / "failure_ledger.jsonl").exists()
    # and it was still anonymized like everything else
    assert "itiger01" not in (out2 / "ops" / "failure_ledger.jsonl").read_text()


def test_hard_excludes_cannot_be_reincluded(tmp_path):
    src = _make_repo(tmp_path)
    (src / "docs" / "design").mkdir(parents=True)
    (src / "docs" / "design" / "notes.md").write_text("itiger01 internals\n", encoding="utf-8")
    (src / "docs" / "DECISIONS.md").write_text("itiger01 decision log\n", encoding="utf-8")

    out = tmp_path / "bundle"
    assert _run(src, out, extra=["docs/design/**", "docs/DECISIONS.md"]) == 0
    assert not (out / "docs" / "design").exists()
    assert not (out / "docs" / "DECISIONS.md").exists()


def test_key_names_are_never_rewritten(tmp_path):
    """A JSON key that itself embeds a hostname-shaped token (not just a
    value) must be left exactly as-is -- process_file only ever touches
    string leaf *values*."""
    src = tmp_path / "repo"
    bench_dir = src / "benchmarks" / "keyed-v1"
    bench_dir.mkdir(parents=True)
    data = {"itiger_scaled_at_500ms": {"p50": 12.3, "note": "on itiger02"}}
    (bench_dir / "record.json").write_text(json.dumps(data), encoding="utf-8")

    out = tmp_path / "bundle"
    assert _run(src, out) == 0
    out_data = json.loads((out / "benchmarks" / "keyed-v1" / "record.json").read_text())

    assert "itiger_scaled_at_500ms" in out_data  # key untouched, even though it embeds a hostname token
    assert out_data["itiger_scaled_at_500ms"]["p50"] == 12.3
    assert "itiger02" not in out_data["itiger_scaled_at_500ms"]["note"]  # the value was still scrubbed


def test_gitignored_files_are_excluded(tmp_path):
    src = _make_repo(tmp_path)
    subprocess_run = __import__("subprocess").run
    subprocess_run(["git", "init", "-q"], cwd=src, check=True)
    (src / ".gitignore").write_text("benchmarks/sample-v1/README.md\n", encoding="utf-8")

    out = tmp_path / "bundle"
    assert _run(src, out) == 0
    assert not (out / "benchmarks" / "sample-v1" / "README.md").exists()
    assert (out / "benchmarks" / "sample-v1" / "record.json").exists()


def test_non_json_extension_treated_as_text(tmp_path):
    src = tmp_path / "repo"
    (src / "benchmarks" / "campaign-v1").mkdir(parents=True)
    (src / "benchmarks" / "campaign-v1" / "campaign.yaml").write_text(
        "partition: bigTiger\nhost: itiger03\n", encoding="utf-8"
    )
    out = tmp_path / "bundle"
    assert _run(src, out) == 0
    text = (out / "benchmarks" / "campaign-v1" / "campaign.yaml").read_text()
    assert "bigTiger" not in text
    assert "itiger03" not in text
    assert "partition: partition-A" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
