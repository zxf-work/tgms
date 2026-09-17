"""The campaign driver's failure handling.

A ceiling hit is an expected outcome of this campaign — `BYPASS_CEILING_S` is
there precisely so a plan that would run forever is recorded and moved past.
What must never happen is a ceiling hit taking the *campaign* with it: the
plans after it never run, and because the record is only written at the end,
the plans before it are lost too.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# the script imports its siblings (`ldbc_compare`, `ldbc_snb_params`) by name
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "tgir_ldbc_sf1", ROOT / "scripts" / "tgir_ldbc_sf1.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)


PRE = b'{"phase": "pre", "plan_id": "BI6.v2", "estimate": {"time_est_ms": 161120}}\n'


def test_last_json_reads_the_bytes_a_timeout_carries():
    """`TimeoutExpired.stdout` is bytes even when `subprocess.run` was given
    `text=True` — the decoding wrapper applies to the returned
    `CompletedProcess`, not to the exception raised on the way out."""
    got = T._last_json(PRE, phase="pre")
    assert got["plan_id"] == "BI6.v2"
    assert got["estimate"]["time_est_ms"] == 161120
    assert "phase" not in got


def test_last_json_still_reads_str():
    """The non-timeout path hands it `CompletedProcess.stdout`, already str."""
    assert T._last_json(PRE.decode(), phase="pre")["plan_id"] == "BI6.v2"


def test_last_json_survives_undecodable_output():
    assert T._last_json(b"\xff\xfe not json\n" + PRE, phase="pre")["plan_id"] \
        == "BI6.v2"


def test_a_ceiling_hit_is_one_recorded_row_not_a_dead_campaign(monkeypatch):
    """The regression that cost a run (ldbc-ref-v1, 2026-09-17): BI6.v2 hit
    the ceiling, the timeout handler raised `TypeError` parsing its own
    evidence, and the campaign died 9 plans in with nothing written."""
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1), output=PRE)

    monkeypatch.setattr(T.subprocess, "run", fake_run)

    rec = T.run_child("BI6.v2", "stores/snb-sf1", Path("/params"), "sf1")

    assert rec["outcome"] == "TIMEOUT"
    assert rec["plan_id"] == "BI6.v2"
    # the estimate the child managed to emit before being killed survives,
    # which is the whole reason it is emitted up front
    assert rec["estimate"]["time_est_ms"] == 161120
    assert str(T.BYPASS_CEILING_S) in rec["note"]


def test_run_child_reports_a_crashed_child_as_errored(monkeypatch):
    """The other failure shape: the child dies without a final record."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(T.subprocess, "run", fake_run)
    rec = T.run_child("BI3", "stores/snb-sf1", Path("/params"), "sf1")
    assert rec["outcome"] == "ERRORED"
    assert "boom" in rec["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
