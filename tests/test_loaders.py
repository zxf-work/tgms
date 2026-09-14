"""Loader contract tests for the multi-file (typed-edge) datasets.

Network-free: raw files are tiny gzip fixtures written per test. What is
pinned here is the loader *contract* the scale ladder depends on —
fixed file order (the recorded event log's determinism), per-file
rel_type assignment, microsecond mapping, and the SHA manifest refusing
a changed raw.
"""

from __future__ import annotations

import gzip

import pytest

from tgms.data.loaders import DATASETS, _fetch_pinned, load


def _write_gz(path, lines):
    with gzip.open(path, "wt") as f:
        f.write("\n".join(lines) + "\n")


def _stub_multifile(tmp_path, monkeypatch):
    """A two-file dataset in the sx shape, registered under a stub name."""
    _write_gz(tmp_path / "x-a2q.txt.gz", ["# comment", "1 2 100", "2 3 200"])
    _write_gz(tmp_path / "x-c2q.txt.gz", ["3 1 150"])
    monkeypatch.setitem(DATASETS, "x-stub", {
        "files": [
            {"url": "unused://a2q", "raw": "x-a2q.txt.gz", "rel_type": "A2Q"},
            {"url": "unused://c2q", "raw": "x-c2q.txt.gz", "rel_type": "C2Q"},
        ],
    })


def test_multifile_streams_in_spec_order_with_per_file_rel_type(
        tmp_path, monkeypatch):
    _stub_multifile(tmp_path, monkeypatch)
    events = list(load("x-stub", tmp_path))
    assert [e["rel_type"] for e in events] == ["A2Q", "A2Q", "C2Q"]
    # file order wins over time order: the C2Q event's vt_s interleaves
    assert [e["vt_s"] for e in events] == [100_000_000, 200_000_000,
                                           150_000_000]
    assert events[0]["src"] == "n1" and events[0]["dst"] == "n2"


def test_multifile_writes_one_manifest_per_raw(tmp_path, monkeypatch):
    _stub_multifile(tmp_path, monkeypatch)
    list(load("x-stub", tmp_path))
    assert (tmp_path / "x-a2q.txt.gz.sha256").exists()
    assert (tmp_path / "x-c2q.txt.gz.sha256").exists()


def test_changed_raw_fails_the_pin(tmp_path):
    raw = tmp_path / "d.txt.gz"
    _write_gz(raw, ["1 2 100"])
    _fetch_pinned("unused://d", raw, "d")          # first fetch pins
    _write_gz(raw, ["1 2 100", "9 9 900"])          # raw silently changes
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        _fetch_pinned("unused://d", raw, "d")


def test_ladder_datasets_are_registered():
    for name in ("sx-mathoverflow", "sx-superuser", "wiki-talk"):
        spec = DATASETS[name]
        if "files" in spec:
            assert [f["rel_type"] for f in spec["files"]] == \
                ["A2Q", "C2Q", "C2A"]
        else:
            assert spec["rel_type"] == "TALK"
