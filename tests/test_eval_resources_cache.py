"""The `scripts/eval_resources.py` store cache: hit/stale/legacy behavior.

Real incident (2026-09-13/14, see docs/eval_resources.md): a 10M native
store cached under `$TMPDIR/tgms-eval-resources` was six weeks stale — the
`.ok` marker carried no code identity, so `ensure_store()` happily served a
store built before commit f851d69 changed the eventlog cursor bookkeeping,
and every mixed-writer trial failed with a cursor/record-boundary
StateError. `ensure_dataset`/`ensure_store` now key their cache markers on
`format_fingerprint()`, a content hash of the format-relevant paths
(FORMAT_PATHS), and rebuild automatically on a mismatch or a legacy marker.

These tests never build a real dataset or store: `H.build_dataset` and
`H.load_store` are monkeypatched to tiny fakes, and everything runs inside
pytest's `tmp_path`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load("eval_resources")


# --------------------------------------------------------------------------- #
# fakes for H.build_dataset / H.load_store
# --------------------------------------------------------------------------- #

def _fake_build_dataset(mod, calls, tmp_path):
    def build_dataset(scale):
        calls["dataset"] += 1
        log = tmp_path / f"raw-eventlog-{calls['dataset']}.jsonl"
        log.write_text('{"header": true}\n{"tt": 1, "ops": []}\n')
        return mod.H.Dataset(log=log, scale=scale, tt_epoch1=1, t0=0, t1=1000,
                             filter_uids=("n1", "n2"), name="synth")
    return build_dataset


def _fake_load_store(calls):
    def load_store(path, backend, log):
        calls["store"] += 1
        path.mkdir(parents=True)
        (path / "segment0").write_text(f"backend={backend} log={log.name}\n")
    return load_store


def _fp(value):
    """A stand-in fingerprint function that always returns `value`."""
    def f(root=None):
        return value
    return f


# --------------------------------------------------------------------------- #
# a. first call builds and writes JSON markers with the expected fields
# --------------------------------------------------------------------------- #

def test_first_call_builds_and_writes_json_markers(mod, monkeypatch, tmp_path):
    calls = {"dataset": 0, "store": 0}
    monkeypatch.setattr(mod.H, "build_dataset", _fake_build_dataset(mod, calls, tmp_path))
    monkeypatch.setattr(mod.H, "load_store", _fake_load_store(calls))
    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-aaaa"))

    workdir = tmp_path / "work"
    data, d = mod.ensure_dataset(workdir, 1000)
    assert calls["dataset"] == 1

    meta = json.loads((d / "meta.json").read_text())
    identity = meta["identity"]
    assert identity["fingerprint"] == "fp-aaaa"
    assert "commit" in identity and "built_at" in identity

    store = mod.ensure_store(d, "native", data.log)
    assert calls["store"] == 1
    marker = json.loads((d / "store-native.ok").read_text())
    assert marker["fingerprint"] == "fp-aaaa"
    assert "commit" in marker and "built_at" in marker
    assert marker["backend"] == "native"
    assert isinstance(marker["replay_s"], float)
    assert marker["dataset_fingerprint"] == "fp-aaaa"
    assert store == d / "store-native"
    assert (store / "segment0").exists()


# --------------------------------------------------------------------------- #
# b. same fingerprint on a second call is a hit
# --------------------------------------------------------------------------- #

def test_same_fingerprint_is_a_hit(mod, monkeypatch, tmp_path):
    calls = {"dataset": 0, "store": 0}
    monkeypatch.setattr(mod.H, "build_dataset", _fake_build_dataset(mod, calls, tmp_path))
    monkeypatch.setattr(mod.H, "load_store", _fake_load_store(calls))
    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-aaaa"))

    workdir = tmp_path / "work"
    data, d = mod.ensure_dataset(workdir, 1000)
    store1 = mod.ensure_store(d, "native", data.log)

    data2, d2 = mod.ensure_dataset(workdir, 1000)
    store2 = mod.ensure_store(d2, "native", data2.log)

    assert calls["dataset"] == 1
    assert calls["store"] == 1
    assert store1 == store2 == d / "store-native"


# --------------------------------------------------------------------------- #
# c. changed fingerprint -> ensure_store rebuilds
# --------------------------------------------------------------------------- #

def test_changed_fingerprint_rebuilds_store(mod, monkeypatch, tmp_path, capsys):
    calls = {"dataset": 0, "store": 0}
    monkeypatch.setattr(mod.H, "build_dataset", _fake_build_dataset(mod, calls, tmp_path))
    monkeypatch.setattr(mod.H, "load_store", _fake_load_store(calls))

    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-aaaa"))
    workdir = tmp_path / "work"
    data, d = mod.ensure_dataset(workdir, 1000)
    mod.ensure_store(d, "native", data.log)
    old_marker = json.loads((d / "store-native.ok").read_text())
    (d / "store-native" / "old-only-file").write_text("stale contents\n")

    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-bbbb"))
    capsys.readouterr()
    store = mod.ensure_store(d, "native", data.log)

    assert calls["store"] == 2
    assert not (store / "old-only-file").exists()
    new_marker = json.loads((d / "store-native.ok").read_text())
    assert new_marker["fingerprint"] == "fp-bbbb"
    assert new_marker != old_marker
    out = capsys.readouterr().out
    assert "cache stale" in out


# --------------------------------------------------------------------------- #
# d. legacy text marker -> rebuild
# --------------------------------------------------------------------------- #

def test_legacy_text_marker_triggers_rebuild(mod, monkeypatch, tmp_path, capsys):
    calls = {"dataset": 0, "store": 0}
    monkeypatch.setattr(mod.H, "build_dataset", _fake_build_dataset(mod, calls, tmp_path))
    monkeypatch.setattr(mod.H, "load_store", _fake_load_store(calls))
    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-aaaa"))

    workdir = tmp_path / "work"
    data, d = mod.ensure_dataset(workdir, 1000)
    store_dir = d / "store-native"
    store_dir.mkdir()
    (store_dir / "junk").write_text("old\n")
    (d / "store-native.ok").write_text("replayed in 1.0s\n")

    capsys.readouterr()
    store = mod.ensure_store(d, "native", data.log)

    assert calls["store"] == 1
    assert not (store / "junk").exists()
    marker = json.loads((d / "store-native.ok").read_text())
    assert marker["fingerprint"] == "fp-aaaa"
    out = capsys.readouterr().out
    assert "legacy" in out


# --------------------------------------------------------------------------- #
# e. changed fingerprint -> ensure_dataset rebuilds and wipes stores under d
# --------------------------------------------------------------------------- #

def test_changed_fingerprint_rebuilds_dataset_and_wipes_stores(mod, monkeypatch, tmp_path, capsys):
    calls = {"dataset": 0, "store": 0}
    monkeypatch.setattr(mod.H, "build_dataset", _fake_build_dataset(mod, calls, tmp_path))
    monkeypatch.setattr(mod.H, "load_store", _fake_load_store(calls))

    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-aaaa"))
    workdir = tmp_path / "work"
    data, d = mod.ensure_dataset(workdir, 1000)
    mod.ensure_store(d, "native", data.log)
    assert (d / "store-native").exists()
    assert (d / "store-native.ok").exists()

    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-cccc"))
    capsys.readouterr()
    data2, d2 = mod.ensure_dataset(workdir, 1000)

    assert calls["dataset"] == 2
    assert d2 == d  # same workdir + scale -> same cache directory
    assert not (d / "store-native").exists()
    assert not (d / "store-native.ok").exists()
    meta = json.loads((d2 / "meta.json").read_text())
    assert meta["identity"]["fingerprint"] == "fp-cccc"
    out = capsys.readouterr().out
    assert "wiped" in out


# --------------------------------------------------------------------------- #
# f. legacy meta.json without "identity" -> rebuild
# --------------------------------------------------------------------------- #

def test_legacy_meta_without_identity_triggers_rebuild(mod, monkeypatch, tmp_path, capsys):
    calls = {"dataset": 0, "store": 0}
    monkeypatch.setattr(mod.H, "build_dataset", _fake_build_dataset(mod, calls, tmp_path))
    monkeypatch.setattr(mod.H, "load_store", _fake_load_store(calls))
    monkeypatch.setattr(mod, "format_fingerprint", _fp("fp-aaaa"))

    workdir = tmp_path / "work"
    d = workdir / "scale-1000"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "scale": 1000, "tt_epoch1": 1, "t0": 0, "t1": 1000,
        "filter_uids": ["n1"], "name": "synth"}) + "\n")
    (d / "eventlog.jsonl").write_text('{"header": true}\n')

    capsys.readouterr()
    data, d2 = mod.ensure_dataset(workdir, 1000)

    assert calls["dataset"] == 1
    meta = json.loads((d2 / "meta.json").read_text())
    assert meta["identity"]["fingerprint"] == "fp-aaaa"
    out = capsys.readouterr().out
    assert "legacy" in out


# --------------------------------------------------------------------------- #
# g. format_fingerprint on a synthetic non-git tree
# --------------------------------------------------------------------------- #

def _make_format_tree(root: Path) -> None:
    (root / "tgms").mkdir(parents=True)
    (root / "tgms" / "store.py").write_text("# store v1\n")
    (root / "tgms" / "__pycache__").mkdir()
    (root / "tgms" / "__pycache__" / "store.cpython-312.pyc").write_bytes(b"junk")
    (root / "Cargo.lock").write_text("# lock v1\n")


def test_format_fingerprint_is_deterministic(mod, tmp_path):
    root = tmp_path / "tree"
    _make_format_tree(root)

    mod._fingerprint_cache.clear()
    fp1 = mod.format_fingerprint(root)
    mod._fingerprint_cache.clear()
    fp2 = mod.format_fingerprint(root)

    assert fp1 == fp2
    assert len(fp1) == 16
    assert all(c in "0123456789abcdef" for c in fp1)


def test_format_fingerprint_changes_with_file_bytes(mod, tmp_path):
    root = tmp_path / "tree"
    _make_format_tree(root)
    mod._fingerprint_cache.clear()
    fp1 = mod.format_fingerprint(root)

    (root / "tgms" / "store.py").write_text("# store v2 — changed\n")
    mod._fingerprint_cache.clear()
    fp2 = mod.format_fingerprint(root)

    assert fp1 != fp2


def test_format_fingerprint_changes_on_rename(mod, tmp_path):
    root = tmp_path / "tree"
    _make_format_tree(root)
    mod._fingerprint_cache.clear()
    fp1 = mod.format_fingerprint(root)

    (root / "tgms" / "store.py").rename(root / "tgms" / "store2.py")
    mod._fingerprint_cache.clear()
    fp2 = mod.format_fingerprint(root)

    assert fp1 != fp2


def test_format_fingerprint_ignores_pycache_and_pyc(mod, tmp_path):
    root_a = tmp_path / "tree_a"
    _make_format_tree(root_a)
    mod._fingerprint_cache.clear()
    fp_with_pycache = mod.format_fingerprint(root_a)

    root_b = tmp_path / "tree_b"
    _make_format_tree(root_b)
    import shutil
    shutil.rmtree(root_b / "tgms" / "__pycache__")
    mod._fingerprint_cache.clear()
    fp_without_pycache = mod.format_fingerprint(root_b)

    assert fp_with_pycache == fp_without_pycache


def test_format_fingerprint_is_cached_per_root(mod, tmp_path):
    root = tmp_path / "tree"
    _make_format_tree(root)
    mod._fingerprint_cache.clear()
    fp1 = mod.format_fingerprint(root)

    # mutate the tree without clearing the cache: cached value must stick
    (root / "tgms" / "store.py").write_text("# mutated after first hash\n")
    fp2 = mod.format_fingerprint(root)
    assert fp1 == fp2


# --------------------------------------------------------------------------- #
# h. format_fingerprint on the real checkout exercises the git path
# --------------------------------------------------------------------------- #

def test_format_fingerprint_real_checkout(mod):
    mod._fingerprint_cache.clear()
    fp = mod.format_fingerprint(mod.ROOT)
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)
