"""`scripts/build_control_batches.py`: the builder of the per-batch and
per-check record files of a `tgms-control` loop. Pins, on a small synthetic
campaign built in `tmp_path`, the four output names, the row counts, the
SOURCES line formats, that values are copied through unchanged, and that two
runs give identical bytes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "2099-01-01-fake"


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_control_batches", ROOT / "scripts" / "build_control_batches.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["build_control_batches"] = mod
    spec.loader.exec_module(mod)
    return mod


def _row(i: int, salt: float) -> dict:
    def arm(x: float) -> dict:
        return {"check_wall_ms": x + 0.1, "false_fresh_count": 0,
                "false_stale_count": 0, "invalidated_count": i,
                "refresh_wall_ms": x + 0.2, "ttf_ms": x + 0.3,
                "extra_field_not_copied": "x"}

    return {"batch_index": i, "candidate_survivors": 10 + i,
            "intersects_calls": 100 + i, "global_recompute_wall_ms": salt + i,
            "ttf_mode": "off",
            "arms": {"tgms-L0": arm(salt + i), "tgms-L1": arm(2 * salt + i),
                     "global-recompute": arm(3 * salt + i)}}


def _campaign(root: Path) -> Path:
    camp = root / "campaign"
    for cell, n, salt in (("beta-n10-s0", 3, 5.5), ("alpha-n10-s0", 4, 1.25)):
        d = camp / cell
        d.mkdir(parents=True)
        (d / "storm-fake-0.json").write_text("{}")
        (d / "storm-fake-0-rows.jsonl").write_text(
            "".join(json.dumps(_row(i, salt)) + "\n" for i in range(n)))
    (camp / "PROGRESS.log").write_text("not a cell\n")
    return camp


def _build(mod, camp: Path, out: Path) -> dict[str, bytes]:
    assert mod.main(["--campaign-dir", str(camp), "--label", LABEL,
                     "--out-dir", str(out)]) == 0
    return {p.name: p.read_bytes() for p in sorted(out.iterdir())}


def test_outputs_names_counts_and_values(tmp_path):
    mod = _load()
    camp = _campaign(tmp_path)
    got = _build(mod, camp, tmp_path / "out")
    assert sorted(got) == sorted([
        f"tgms-control-{LABEL}-batches.jsonl",
        f"tgms-control-{LABEL}-batches.SOURCES.txt",
        f"tgms-control-checks-{LABEL}.jsonl",
        f"tgms-control-checks-{LABEL}.SOURCES.txt"])
    lines = {k: v.decode().splitlines() for k, v in got.items()}
    batches = [json.loads(x) for x in lines[f"tgms-control-{LABEL}-batches.jsonl"]]
    checks = [json.loads(x) for x in lines[f"tgms-control-checks-{LABEL}.jsonl"]]
    assert len(batches) == len(checks) == 7
    # cells in sorted order, rows in file order
    assert [b["cell_id"] for b in batches] == ["alpha-n10-s0"] * 4 + ["beta-n10-s0"] * 3
    assert [b["batch_index"] for b in batches] == [0, 1, 2, 3, 0, 1, 2]
    b = batches[1]
    assert set(b) == {"arms", "batch_index", "cell_id",
                      "global_recompute_wall_ms", "ttf_mode"}
    assert set(b["arms"]["tgms-L1"]) == set(mod.ARM_FIELDS)
    assert b["arms"]["tgms-L1"]["check_wall_ms"] == 2 * 1.25 + 1 + 0.1
    c = checks[5]
    assert c == {"batch_index": 1, "candidate_survivors": 11,
                 "cell_id": "beta-n10-s0", "control": LABEL,
                 "intersects_calls": 101,
                 "tgms_l0_check_wall_ms": 5.5 + 1 + 0.1,
                 "tgms_l1_check_wall_ms": 2 * 5.5 + 1 + 0.1}
    assert got[f"tgms-control-{LABEL}-batches.jsonl"].endswith(b"\n")


def test_sources_line_format(tmp_path):
    mod = _load()
    camp = _campaign(tmp_path)
    got = _build(mod, camp, tmp_path / "out")
    bsrc = got[f"tgms-control-{LABEL}-batches.SOURCES.txt"].decode().splitlines()
    csrc = got[f"tgms-control-checks-{LABEL}.SOURCES.txt"].decode().splitlines()
    assert len(bsrc) == len(csrc) == 2
    for cell, b, c in zip(("alpha-n10-s0", "beta-n10-s0"), bsrc, csrc):
        rows = camp / cell / "storm-fake-0-rows.jsonl"
        sha = hashlib.sha256(rows.read_bytes()).hexdigest()
        assert b == f"{sha}  {cell}  {rows}"
        assert c == f"{sha}  {LABEL}  {cell}  {rows}"


def test_deterministic(tmp_path):
    mod = _load()
    camp = _campaign(tmp_path)
    first = _build(mod, camp, tmp_path / "out1")
    second = _build(mod, camp, tmp_path / "out2")
    assert first == second
