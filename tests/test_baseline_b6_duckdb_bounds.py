"""[tests] b6/b6e DuckDB resource bounds (2026-09-14 postmortem, job 211581).

An LLM-generated SQL statement (the b6/b6e arms) can be a pathological
cartesian product. DuckDB's default `temp_directory` sits next to the
database file; on the D-160 iTiger campaign that directory was
`/project`, a quota-limited shared filesystem, so an unbounded spill
there exhausted the whole account's quota and took unrelated jobs down
with it (independently confirmed: many concurrent Slurm array tasks
failed within the same second the spill peaked). `BiTemporalSQL` now
binds every subprocess-side DuckDB connection to a `temp_directory`
(defaulting to a subdirectory of the process's own `TMPDIR`, which is
node-local on the campaign's slurm scripts already),
`max_temp_directory_size`, and `memory_limit`, via DuckDB's own
`config=` connect-time option -- so a runaway statement hits a bounded,
node-local failure instead of an unbounded write to a shared quota.

These are fake-LLM, tmp_path-only unit tests: no network, no real model,
a tiny synth DuckDB store built the same way tests/test_b6e.py's `db`
fixture already does.
"""

from __future__ import annotations

import json
import subprocess as subprocess_mod

import pytest

import tgms
from tgms.eval.baselines import BiTemporalSQL


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    from tgms.data.synth import generate
    tmp = tmp_path_factory.mktemp("b6-bounds")
    generate(tmp / "synth", n_nodes=20, n_events=200, seed=3)
    store = tgms.open(tmp / "store", backend="duckdb")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    store.adapter.conn.execute("CHECKPOINT")
    store.close()
    return {"path": tmp / "store" / "store.duckdb"}


def _fake_llm(sql: str):
    def llm(model, messages, temperature, seed, **kw):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith(
                "You translate one question into ONE DuckDB SQL query."):
            return sql  # every attempt, including repairs, emits the same
                        # scripted query -- these tests are about the
                        # connection's resource bounds, not repair logic
        return json.dumps({"text": "n/a", "claims": []})
    return llm


def _magnitude(setting: str) -> float:
    """'6.5 GiB' / '95.4 MiB' -> a float in MiB, so callers compare in one
    unit regardless of which DuckDB picked for the value."""
    value, unit = setting.split()
    n = float(value)
    return n * 1024 if unit.upper().startswith("G") else n


def test_temp_dir_and_limits_are_honoured(db, tmp_path):
    temp_dir = str(tmp_path / "node-local-duckdb-temp")
    arm = BiTemporalSQL(_fake_llm("n/a"), "fake", db_path=db["path"],
                        seed=0, duckdb_temp_dir=temp_dir,
                        duckdb_max_temp_mb=100, duckdb_memory_limit_gb=8)
    rows, err = arm._run_sql(
        "SELECT current_setting('temp_directory'), "
        "current_setting('memory_limit'), "
        "current_setting('max_temp_directory_size')")
    assert err is None, err
    got_temp_dir, got_mem_limit, got_max_temp = rows[0]
    assert got_temp_dir == temp_dir
    # 8 GB requested -> ~7.45 GiB reported; 100 MB requested -> ~95.4 MiB
    # reported (DuckDB's own decimal->binary rounding) -- compare in one
    # unit (MiB) with generous tolerance rather than hardcoding a string.
    assert 6 * 1024 <= _magnitude(got_mem_limit) <= 8 * 1024
    assert 80 <= _magnitude(got_max_temp) <= 100


def test_default_temp_dir_is_under_process_tmpdir(db, monkeypatch, tmp_path):
    # tempfile.gettempdir() caches its answer on first call in the process,
    # so setting $TMPDIR alone would not be observed here if anything else
    # already resolved it this session -- patch the function itself,
    # which is what BiTemporalSQL.__init__ actually calls.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    arm = BiTemporalSQL(_fake_llm("n/a"), "fake", db_path=db["path"], seed=0)
    assert arm.duckdb_temp_dir.startswith(str(tmp_path))


def test_pathological_query_times_out_and_is_flagged(db, monkeypatch):
    # Exercise the existing subprocess-level wall-clock kill (already a
    # hard 30s+ floor -- too slow to actually wait out in a unit test) by
    # making the subprocess call raise the same exception a real timeout
    # raises, and check the NEW `timeout` meta flag this fix adds: a
    # timeout must be a countable baseline outcome, never a crash.
    def _raise(*a, **k):
        raise subprocess_mod.TimeoutExpired(cmd=["duckdb"], timeout=1)
    monkeypatch.setattr(subprocess_mod, "run", _raise)

    arm = BiTemporalSQL(_fake_llm("SELECT 1"), "fake", db_path=db["path"],
                        max_repairs=0, seed=0)
    out = arm.answer("anything", [])
    assert out["meta"]["failed"] is True
    assert out["meta"]["timeout"] is True
    # never crashes -- a normal (empty-claims) AnswerObject still comes back
    assert out["answer_object"] == {"text": "n/a", "claims": []}


def test_non_timeout_failure_is_not_flagged_as_timeout(db):
    arm = BiTemporalSQL(_fake_llm("SELECT * FROM no_such_table_at_all"),
                        "fake", db_path=db["path"], max_repairs=0, seed=0)
    out = arm.answer("anything", [])
    assert out["meta"]["failed"] is True
    assert out["meta"]["timeout"] is False
