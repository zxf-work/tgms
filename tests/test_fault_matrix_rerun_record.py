"""[tests] The F1-8 re-run addendum record (benchmarks/faults-v1, 2026-10-12).

The committed manifest and rows file must be exactly what
scripts/fault_matrix_rerun_record.py derives from the committed raw driver
record, the raw record's counts and result_digest must recompute from its
own trial rows, and every committed file must match its checksum line.
The earlier campaign's F1-8 records stay exactly as recorded (0 trials):
the addendum adds the cell, it does not edit them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAULTS = ROOT / "benchmarks" / "faults-v1"
RERUN_DIR = FAULTS / "f1-8-rerun-2026-10-12"
RAW = RERUN_DIR / "F1-8-collegemsg.json"
MANIFEST = FAULTS / "faults-f1-8-rerun-2026-10-12.json"
ROWS = FAULTS / "faults-f1-8-rerun-2026-10-12-rows.jsonl"
SUMS = FAULTS / "SHA256SUMS-f1-8-rerun-2026-10-12.txt"


def _load_builder():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "fault_matrix_rerun_record", ROOT / "scripts" / "fault_matrix_rerun_record.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canonical_digest(obj) -> str:
    # tgms.core.model.canonical_json / sha256_hex, restated so this test
    # needs no engine
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_raw_record_counts_and_digest_recompute_from_its_own_rows():
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    rows = raw["per_case"]
    assert raw["cell"] == "F1-8" and raw["strict_gate"] is False and raw["seed"] == 0
    assert raw["n_requested"] == 300 and raw["n_cases"] == len(rows) == 300
    counts = {k: 0 for k in ("correct", "safe-refusal", "explicit-failure", "silent-violation")}
    for r in rows:
        counts[r["outcome"]] += 1
    assert counts == raw["outcomes"]["counts"]
    assert counts == {"correct": 0, "safe-refusal": 272, "explicit-failure": 28,
                      "silent-violation": 0}
    assert _canonical_digest(rows) == raw["result_digest"]


def test_committed_manifest_and_rows_are_what_the_builder_derives(tmp_path):
    builder = _load_builder()
    meta = json.loads((RERUN_DIR / "run-meta.json").read_text(encoding="utf-8"))
    out, rows_out = tmp_path / MANIFEST.name, tmp_path / ROWS.name
    builder.build(RAW, meta, out, rows_out, f"benchmarks/faults-v1/{MANIFEST.name}")
    assert out.read_bytes() == MANIFEST.read_bytes()
    assert rows_out.read_bytes() == ROWS.read_bytes()


def test_manifest_states_the_addendum_totals():
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert m["total_trials"] == 300 and m["total_trials_requested"] == 300
    assert m["n_records"] == 1 and m["shortfalls"] == []
    assert m["silent_violations"] == []
    assert m["headline_silent_violation_count"] == 0
    assert m["non_headline_silent_violation_count"] == 0
    assert m["outcome_reasons"] == {"explicit-failure/E_ANSWER": 28,
                                    "safe-refusal/incompleteness": 272}
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    assert m["result_digest"] == raw["result_digest"]
    assert [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines()] \
        == raw["per_case"]


def test_the_earlier_records_still_say_zero_trials():
    """The addendum corrects by addition: the two earlier F1-8 raw records
    keep their recorded 0/300, and the defect reproduction at the
    campaign-commit driver is byte-identical in its digest to the first."""
    first = json.loads((FAULTS / "F1-8-collegemsg.json").read_text(encoding="utf-8"))
    d160 = json.loads((FAULTS / "d160-raw" / "F1-8-collegemsg.json").read_text(encoding="utf-8"))
    repro = json.loads((RERUN_DIR / "defect-repro-F1-8-collegemsg-old-driver.json")
                       .read_text(encoding="utf-8"))
    for rec in (first, d160, repro):
        assert rec["n_cases"] == 0 and rec["per_case"] == []
    assert first["n_requested"] == d160["n_requested"] == 300
    assert repro["result_digest"] == first["result_digest"] == d160["result_digest"]


def test_every_committed_file_matches_its_checksum_line():
    lines = re.findall(r"^([0-9a-f]{64})  (\S+)$", SUMS.read_text(encoding="utf-8"),
                       re.MULTILINE)
    assert len(lines) == 6
    for digest, rel in lines:
        assert hashlib.sha256((FAULTS / rel).read_bytes()).hexdigest() == digest, rel
