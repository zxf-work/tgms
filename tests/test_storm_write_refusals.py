"""Regression tests for `storm-harness-swallows-write-refusals`
(`ops/failure_ledger.jsonl`).

`Storm.run_batch`'s write path used to catch every `TgmsError` from
`Storm._write` uniformly and return `None`, exactly like the pre-existing
"mix starved" path (`self.mix(...)` had nothing realizable this draw). For
a store whose native engine backend has already committed past the
transaction time this harness computes from the store's own event log
(`Storm._tt`, seeded from `store.eventlog.last_tt()`) -- an engine-or-setup
error, never a starved mix -- that made *every* correction write raise the
engine's monotonic-tt `StateError`, which the harness silently absorbed
batch after batch: `Storm.run`/the CLI returned/wrote `batches_realized: 0`
at exit 0, with no indication anything was wrong.

This module: (1) builds a store that reproduces that exact mismatch and
checks `Storm.run`/`run_batch` now raise a clear `RuntimeError` instead of
swallowing it; (2) checks a properly built store still runs to completion
unchanged; (3) checks the pre-existing "mix starved" path is still a silent,
non-raising `None` -- never confused with a write refusal.
"""

from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path

import pytest

import tgms
from tgms.core.errors import StateError, TgmsError
from tgms.eval.storm import MAX_CONSECUTIVE_WRITE_REFUSALS, Storm
from tgms.storage.base import make_op

BACKEND = "native"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _build_fixture(store_dir: Path, *, n_nodes: int = 24, n_events: int = 240,
                   seed: int = 1) -> None:
    """A small, ordinary event-stream store, built the normal way (through
    `Store`'s public write API) -- mirrors `tests/test_storm.py`'s own
    fixture helper. Its own `tt`s are wall-clock and do not need to be
    deterministic; only the storm batches written on top of it do."""
    store = tgms.open(store_dir, backend=BACKEND)
    rng = random.Random(seed)
    uids = [f"n{i}" for i in range(n_nodes)]
    rel_types = ["FOLLOWS", "MESSAGES", "CITES"]
    events = []
    for i in range(n_events):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst, "rel_type": rng.choice(rel_types),
                       "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("storm-fixture")
    _build_fixture(base)
    return base


def _copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _build_mis_built_store(store_dir: Path, *, n_commits: int = 5) -> None:
    """A store whose native engine backend (`native/`) has committed past
    what the python-side event log / replay cursor ever recorded: ops
    applied straight to `store.adapter` -- never through `log.append` and
    never through `note_event_cursor` -- so the cursor stays at its
    "nothing applied yet" seed and `eventlog.jsonl` stays empty.

    Reopening this store succeeds *silently*: `Store._recover`'s own cursor
    check sees `offset(0) == size(0)` ("clean shutdown: nothing to do") and
    returns without complaint, even though the engine's internal
    `created_tt` is already `n_commits` ahead of anything `last_tt()` or
    `store.clock.last_tt` can ever report from Python. This is the exact
    engine-or-setup mismatch `storm-harness-swallows-write-refusals`
    describes -- a store "copied, rebuilt, or replayed inconsistently",
    not a hand-crafted pathology specific to this test. `Storm.__init__`
    (store open + substrate probe + artifact registration, all read-only)
    completes over it exactly as it would over a consistent store; only a
    write -- a correction batch -- ever discovers the mismatch.
    """
    store = tgms.open(store_dir, backend=BACKEND)
    for i in range(n_commits):
        ops = [make_op("assert_node", uid=f"x{i}", label="N", props={}, vt_s=0, vt_e=10,
                        source="ingest", provenance_ref=None)]
        store.adapter.begin()
        store.adapter.apply_ops(ops, i + 1)
        store.adapter.commit()
    store.close()


# ---------------------------------------------------------------------------
# 1 -- the mis-built-store case raises, loudly, naming the problem
# ---------------------------------------------------------------------------

def test_storm_raises_on_a_store_whose_engine_committed_past_its_own_log(
    tmp_path: Path,
) -> None:
    store_dir = tmp_path / "store"
    _build_mis_built_store(store_dir)

    storm = Storm(store_dir, n_artifacts=5, seed=0, backend=BACKEND)
    try:
        # Storm.__init__ (open + substrate probe + registration) is
        # read-only and completes exactly as it would over a consistent
        # store -- the mismatch is invisible until a write is attempted.
        assert len(storm.artifacts) > 0
        assert storm.n_registration_skipped == 0

        with pytest.raises(RuntimeError) as exc_info:
            storm.run(2)

        message = str(exc_info.value)
        assert "storm-harness-swallows-write-refusals" in message
        assert str(store_dir) in message
        assert "StateError" in message
        assert "transaction time must advance" in message
        # both tt sources this harness can see from Python, named per the
        # task's own "last_tt vs the clock" instruction
        assert "store.eventlog.last_tt()" in message or "Storm._tt" in message
        assert "store.clock.last_tt" in message

        assert storm.n_batches == 0
        assert storm.write_refusals >= 1
        assert storm.first_write_refusal is not None
        assert "StateError" in storm.first_write_refusal
    finally:
        storm.close()


def test_run_batch_raises_after_max_consecutive_refusals_without_exhausting_attempts(
    tmp_path: Path,
) -> None:
    """The fast-fail path: `run_batch` itself raises once consecutive
    refusals reach `MAX_CONSECUTIVE_WRITE_REFUSALS`, rather than waiting
    for the caller's full attempt budget to be silently spent."""
    store_dir = tmp_path / "store"
    _build_mis_built_store(store_dir)
    storm = Storm(store_dir, n_artifacts=5, seed=0, backend=BACKEND)
    try:
        for _ in range(MAX_CONSECUTIVE_WRITE_REFUSALS - 1):
            assert storm.run_batch(0) is None
        with pytest.raises(RuntimeError, match="storm-harness-swallows-write-refusals"):
            storm.run_batch(0)
        assert storm.write_refusals == MAX_CONSECUTIVE_WRITE_REFUSALS
        assert storm._consecutive_write_refusals == MAX_CONSECUTIVE_WRITE_REFUSALS
    finally:
        storm.close()


def test_run_raises_at_end_of_run_even_when_refusals_never_run_three_in_a_row(
    pristine_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop: refusals interspersed with a different, uncounted
    refusal kind never reach `MAX_CONSECUTIVE_WRITE_REFUSALS` in a row, but
    the run still realizes zero batches -- `Storm.run`'s own end-of-run
    check must still raise rather than return an empty, silent success."""
    storm = Storm(_copy(pristine_store), n_artifacts=5, seed=0, backend=BACKEND)
    try:
        calls = {"n": 0}

        def flaky_write(tt, ops):
            calls["n"] += 1
            if calls["n"] % 2 == 1:
                raise StateError("transaction time must advance: synthetic test refusal")
            raise TgmsError("synthetic non-state refusal, unrelated to tt")

        monkeypatch.setattr(storm, "_write", flaky_write)

        with pytest.raises(RuntimeError, match="storm-harness-swallows-write-refusals"):
            storm.run(1, max_attempts_factor=4)

        assert storm.n_batches == 0
        assert storm.write_refusals >= 1
        assert storm._consecutive_write_refusals < MAX_CONSECUTIVE_WRITE_REFUSALS
    finally:
        storm.close()


# ---------------------------------------------------------------------------
# 2 -- a properly built store runs to completion, unchanged
# ---------------------------------------------------------------------------

def test_a_properly_built_store_runs_unchanged(pristine_store: Path) -> None:
    storm = Storm(_copy(pristine_store), n_artifacts=20, seed=1, backend=BACKEND)
    try:
        results = storm.run(5)
        assert len(results) == 5
        assert storm.n_batches == 5
        assert storm.write_refusals == 0
        assert storm.first_write_refusal is None
    finally:
        storm.close()


# ---------------------------------------------------------------------------
# 3 -- a genuinely starved mix is still a silent, non-raising None
# ---------------------------------------------------------------------------

def test_a_genuinely_starved_mix_is_recorded_without_raising(pristine_store: Path) -> None:
    def _never_realizable(store, sub, target, rng):
        return []

    storm = Storm(_copy(pristine_store), n_artifacts=5, seed=0, backend=BACKEND,
                 mix=_never_realizable)
    try:
        results = storm.run(2, max_attempts_factor=3)
        assert results == []
        assert storm.n_batches == 0
        assert storm.n_mix_starved > 0
        # the whole point: a starved mix must never be counted or reported
        # as an engine write refusal, and must never raise.
        assert storm.write_refusals == 0
        assert storm.first_write_refusal is None
    finally:
        storm.close()
