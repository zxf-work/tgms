"""`scripts/axes_standing_state.py tgms`: the TGMS side of the standing-state
measurement (registry bytes and entries after registration, the harness's
and a separate check process's resident memory).

Pins that the numbers are read off the right objects: the registry bytes
equal the sum of its line lengths, the check process folds exactly one
entry per registered artifact (the registry as the burst found it), and the
check pass runs over the burst's own batch. Linux-only (`/proc`).
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

import tgms

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not Path("/proc/self/status").exists(),
                                reason="reads /proc/<pid>/status")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "axes_standing_state", ROOT / "scripts" / "axes_standing_state.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["axes_standing_state"] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_fixture(store_dir: Path) -> None:
    store = tgms.open(store_dir, backend="native")
    rng = random.Random(1)
    uids = [f"n{i}" for i in range(24)]
    events = []
    for i in range(240):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst,
                       "rel_type": rng.choice(["FOLLOWS", "MESSAGES", "CITES"]),
                       "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


def test_tgms_standing_state_reads_the_registry_and_the_check_process(tmp_path: Path) -> None:
    mod = _load_script()
    src = tmp_path / "src"
    _build_fixture(src)
    out = tmp_path / "tgms.json"
    rc = mod.main(["tgms", "--store", str(src), "--n-artifacts", "12", "--measure-ttf", "sum",
                   "--work", str(tmp_path / "work"), "--out", str(out), "--interval", "0.2",
                   "--keep-work"])
    assert rc == 0
    doc = json.loads(out.read_text())
    reg = doc["registration"]
    n = reg["n_registered"]
    assert n >= 1
    assert reg["registry_record_lines"] == n == reg["fold"]["entries"] == reg["fold"]["names"]
    reg_file = tmp_path / "work" / "check" / "artifacts.jsonl"
    assert reg_file.stat().st_size == reg["registry_bytes"]
    lines = [len(x) + 1 for x in reg_file.read_bytes().split(b"\n") if x]
    assert lines[0] == reg["registry_header_bytes"]  # the format header line
    assert sum(lines[1:]) == reg["registry_bytes"] - reg["registry_header_bytes"]
    assert reg["registry_record_bytes_mean"] == pytest.approx(sum(lines[1:]) / n)
    # one burst republished every artifact: one more entry per name
    assert doc["burst_1"]["fold"]["entries"] == 2 * n
    check = doc["check_process"]
    assert check["fold"] == {"names": n, "entries": n}
    assert check["checks"] == 2 * check["candidate_survivors"]
    for key in ("after_imports", "after_registry_load", "after_check_pass"):
        assert isinstance(check[key]["vm_rss_kb"], int) and check[key]["vm_rss_kb"] > 0
        assert check[key]["vm_hwm_kb"] >= check[key]["vm_rss_kb"]
    for key in ("after_registration", "after_burst_1"):
        assert doc["harness_process"][key]["vm_rss_kb"] > 0
