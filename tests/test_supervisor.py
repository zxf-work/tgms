"""[tests] The out-of-process wall-clock supervisor (`scripts/tgms_supervise.py`,
Lane B5/F2): it tails a child's JSON request log, and a request whose
`"started"` line has no `"finished"` line inside `max_wall_s` gets the child
SIGKILLed and restarted, with the kill recorded on the metrics sink and in
`ops/failure_ledger.jsonl`'s required-field schema.

Loaded by file path (`importlib.util.spec_from_file_location`), the
convention `tests/test_artifact_check.py` already uses for exercising a
`scripts/*.py` module directly, since `scripts/` is not a package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_supervisor_module():
    spec = importlib.util.spec_from_file_location(
        "tgms_supervise", ROOT / "scripts" / "tgms_supervise.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SUP = _load_supervisor_module()

FAKE_CHILD_SRC = '''
import json, os, sys, time

log_path = os.environ["TGMS_LOG_PATH"]

def emit(event, **fields):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\\n")

emit("started", request_id="r1", tool="fake_slow_op")
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 5.0)
emit("finished", request_id="r1", tool="fake_slow_op", outcome="ok", wall_ms=0)
'''

FAKE_CHILD_INSTANT_SRC = '''
import json, os, time

log_path = os.environ["TGMS_LOG_PATH"]
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": time.time(), "event": "started",
                        "request_id": "r1", "tool": "fake_fast_op"}) + "\\n")
    f.write(json.dumps({"ts": time.time(), "event": "finished",
                        "request_id": "r1", "tool": "fake_fast_op",
                        "outcome": "ok", "wall_ms": 1}) + "\\n")
'''


def test_log_tail_reads_only_complete_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3')  # last line has no newline
    tail = SUP.LogTail(path)
    recs = tail.read_new()
    assert [r["a"] for r in recs] == [1, 2]
    with open(path, "a") as f:
        f.write('}\n')  # complete the third line
    recs2 = tail.read_new()
    assert [r["a"] for r in recs2] == [3]


def test_supervisor_kills_hung_child_and_restarts(tmp_path):
    fake = tmp_path / "fake_child.py"
    fake.write_text(FAKE_CHILD_SRC)
    log_path = tmp_path / "child.jsonl"
    ledger_path = tmp_path / "ledger.jsonl"
    metrics_path = tmp_path / "metrics.jsonl"

    from tgms.telemetry.metrics import Metrics

    sup = SUP.Supervisor(
        [sys.executable, str(fake), "5"], log_path, max_wall_s=0.3,
        poll_interval_s=0.05, metrics=Metrics(metrics_path),
        ledger_path=ledger_path, max_restarts=1)
    start = time.time()
    sup.run(max_iterations=400)
    elapsed = time.time() - start

    assert sup.restarts == 1
    assert elapsed < 20, "the supervisor should have killed the hung child quickly"

    ledger_lines = [json.loads(line) for line in
                    ledger_path.read_text().splitlines() if line.strip()]
    assert len(ledger_lines) == 1
    entry = ledger_lines[0]
    required = ("id", "first_observed", "workload_or_seed", "symptom",
               "severity", "root_cause", "fix_commit", "regression_test",
               "decision_ref")
    for field in required:
        assert isinstance(entry[field], str) and entry[field], field
    assert "r1" in entry["symptom"]

    assert metrics_path.exists()
    metrics_lines = [json.loads(line) for line in
                     metrics_path.read_text().splitlines() if line.strip()]
    assert any(m["name"] == "wall_kill" for m in metrics_lines)


def test_supervisor_does_not_kill_a_child_that_finishes_in_time(tmp_path):
    fake = tmp_path / "fake_fast.py"
    fake.write_text(FAKE_CHILD_INSTANT_SRC)
    log_path = tmp_path / "child.jsonl"
    ledger_path = tmp_path / "ledger.jsonl"

    from tgms.telemetry.metrics import Metrics

    sup = SUP.Supervisor(
        [sys.executable, str(fake)], log_path, max_wall_s=5.0,
        poll_interval_s=0.05, metrics=Metrics(None),
        ledger_path=ledger_path, max_restarts=0)
    # the fake child exits almost immediately after writing both lines;
    # give the loop a handful of iterations to observe that and stop.
    sup.run(max_iterations=20)
    assert sup.restarts == 0
    assert not ledger_path.exists() or ledger_path.read_text().strip() == ""


def test_append_ledger_entry_conforms_to_check_failure_ledger_schema(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    SUP._append_ledger_entry(ledger_path, request_id="req-x", pid=1234,
                             max_wall_s=10.0, cmd=["tgms", "serve"])
    check_mod_spec = importlib.util.spec_from_file_location(
        "check_failure_ledger", ROOT / "scripts" / "check_failure_ledger.py")
    check_mod = importlib.util.module_from_spec(check_mod_spec)
    check_mod_spec.loader.exec_module(check_mod)
    problems = check_mod.check_line(1, ledger_path.read_text().splitlines()[0])
    assert problems == []
