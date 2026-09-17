"""`scripts/longevity_run.py::ReaderErrorDetail` and its wiring into
`child_reader` — bounded capture of exception *message* text alongside the
existing class-only `reader_errors_total` counter.

Motivated by `ops/failure_ledger.jsonl`'s
`D-088-reader-osstorm-root-cause-not-established`: P-SOAK2's 8 readers
accumulated 150.28M `OSError` + 197,792 `StateError` events, but pyo3's
default `std::io::Error -> PyErr` conversion drops the errno, and
`reader_errors_total`/the reader's progress file only ever carried
`type(e).__name__` — so "OSError" could not be told apart from EMFILE vs.
ENOENT vs. anything else, and the storm's root cause could not be
diagnosed from the soak's own data
(docs/design/READER_FD_STORM_DIAGNOSIS_2026-09-17.md).

Two things are covered:

1. `ReaderErrorDetail` in isolation — dedup by `(class, message[:120])`,
   the write-once-per-distinct-pair contract, the hard cap at 20 distinct
   pairs, and message truncation.
2. `child_reader` end to end against a fake, deterministically failing
   store (`tgms.open`/`call_operator` monkeypatched — no real engine, no
   real store on disk) — asserting the bounded detail actually reaches
   `longevity_ledger.jsonl` as `reader_op_error` events and the reader's
   progress file, and that 150M-style repetition does not turn into
   150M ledger lines.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import longevity_run as LR  # noqa: E402


# --------------------------------------------------------------------------- #
# ReaderErrorDetail in isolation                                              #
# --------------------------------------------------------------------------- #


def test_first_distinct_pair_is_reported_once_then_only_counted() -> None:
    d = LR.ReaderErrorDetail()
    first = d.record(OSError("Errno 24: Too many open files"), ts=1.0)
    assert first == {"class": "OSError",
                     "message": "Errno 24: Too many open files",
                     "first_seen_ts": 1.0, "count_at_first_seen": 1}

    # Same (class, message) again — no new ledger fields, just a count bump.
    assert d.record(OSError("Errno 24: Too many open files"), ts=2.0) is None
    snap = d.snapshot()
    assert len(snap) == 1
    assert snap[0]["count"] == 2
    assert snap[0]["first_seen_ts"] == 1.0  # unchanged by later occurrences


def test_distinct_messages_of_the_same_class_are_tracked_separately() -> None:
    d = LR.ReaderErrorDetail()
    a = d.record(OSError("Errno 24: Too many open files"), ts=1.0)
    b = d.record(OSError("Errno 2: No such file or directory"), ts=2.0)
    assert a is not None and b is not None
    assert a["message"] != b["message"]
    assert {row["message"] for row in d.snapshot()} == {
        "Errno 24: Too many open files", "Errno 2: No such file or directory"}


def test_message_is_truncated_to_120_chars_before_dedup() -> None:
    d = LR.ReaderErrorDetail()
    long_msg = "X" * 300
    reported = d.record(OSError(long_msg), ts=1.0)
    assert len(reported["message"]) == 120
    # A second exception differing only past character 120 dedups with the
    # first — the truncated text is the dedup key, not the raw message.
    long_msg2 = "X" * 120 + "Y" * 180
    assert d.record(OSError(long_msg2), ts=2.0) is None
    assert len(d.snapshot()) == 1
    assert d.snapshot()[0]["count"] == 2


def test_capture_is_bounded_at_20_distinct_pairs_no_per_event_growth() -> None:
    """The core D-088 requirement: 150M raw events must not become 150M (or
    even thousands of) ledger lines. Feed far more than the cap in distinct
    messages, plus a very large number of repeats of the first few, and
    confirm both the reported (write-once) count and the retained snapshot
    stay bounded at `cap`."""
    d = LR.ReaderErrorDetail(cap=20)
    reported = 0
    for i in range(1000):
        # Every i produces a distinct message the first 1000 times, so this
        # exercises far more than 20 distinct pairs, plus repeats of the
        # early ones to check the counters keep incrementing past the cap.
        msg = f"synthetic failure #{i % 50}"
        out = d.record(OSError(msg), ts=float(i))
        if out is not None:
            reported += 1
    assert reported == 20
    snap = d.snapshot()
    assert len(snap) == 20
    # Messages #0..#19 were each the *first* occurrence of a new pair and
    # got captured; message #0 recurs at i=50,100,...,950 (20 times total)
    # and its counter must reflect every recurrence, not just the first.
    by_message = {row["message"]: row["count"] for row in snap}
    assert by_message["synthetic failure #0"] == 20
    # #20..#49 never got a detail slot (cap already full when first seen).
    assert "synthetic failure #49" not in by_message


# --------------------------------------------------------------------------- #
# child_reader end to end, against a fake failing store                      #
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    pass


class _FakeStore:
    def __init__(self) -> None:
        self.adapter = _FakeAdapter()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _fake_call_operator_factory(fail_pattern):
    """Returns a `call_operator(adapter, name, args)` stand-in that raises
    from `fail_pattern` (an iterable of exceptions, cycled) forever — a
    store where every operator call fails uniformly once the reader is "in
    the state", exactly the soak's own symptom description."""
    pattern = list(fail_pattern)
    counter = {"i": 0}

    def _call(adapter, name, args):
        exc = pattern[counter["i"] % len(pattern)]
        counter["i"] += 1
        raise exc
    return _call


def test_child_reader_captures_bounded_error_detail_from_a_fake_failing_store(
        tmp_path, monkeypatch) -> None:
    import tgms
    import tgms.temporal.algebra as algebra

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    fake_store = _FakeStore()

    monkeypatch.setattr(tgms, "open", lambda *a, **k: fake_store)
    monkeypatch.setattr(algebra, "ensure_all_registered", lambda: None)
    # A fixed, small repertoire of distinct failures — mirrors "every
    # operator failing once a reader is in the state" from the soak, using
    # the project's own StateError alongside a plain OSError so the test
    # matches the soak's actual exception classes.
    from tgms.core.errors import StateError
    monkeypatch.setattr(algebra, "call_operator", _fake_call_operator_factory([
        OSError("[Errno 24] Too many open files: 'seg-000123.tgs'"),
        StateError("manifest generation 4082 missing from segment map"),
    ]))

    mix = [{"id": "q1", "op": "noop", "args": {}},
          {"id": "q2", "op": "noop", "args": {}}]
    progress_path = tmp_path / "reader-0-progress.json"
    metrics_path = tmp_path / "metrics.jsonl"
    cfg = {
        "idx": 0,
        "store": str(store_dir),
        "mix": mix,
        "metrics_path": str(metrics_path),
        "progress_path": str(progress_path),
        "report_every_s": 0.05,
        "reopen_every_s": 0,  # no reopen churn — isolate error capture only
        "end_at": time.time() + 0.25,
    }

    LR.child_reader(cfg)

    ledger_path = store_dir.parent / "longevity_ledger.jsonl"
    assert ledger_path.exists()
    entries = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    op_errors = [e for e in entries if e["event"] == "reader_op_error"]

    # Exactly two distinct (class, message) pairs ever occur here, so both
    # get a write-once ledger entry and no more — never one line per raw
    # error event (many raw errors occurred over the run's duration).
    assert len(op_errors) == 2
    classes = {e["class"] for e in op_errors}
    assert classes == {"OSError", "StateError"}
    for e in op_errors:
        assert e["reader"] == 0
        assert "first_seen_ts" in e and e["count_at_first_seen"] == 1

    final = json.loads(progress_path.read_text())
    assert final["final"] is True
    detail_classes = {row["class"] for row in final["error_details"]}
    assert detail_classes == {"OSError", "StateError"}
    # Both classes' counts must have grown well past 1 (many raw
    # occurrences over the run), proving the counter keeps incrementing
    # after the one-time ledger write instead of being dropped.
    assert all(row["count"] > 1 for row in final["error_details"])
