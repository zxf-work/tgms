"""A chain-cache miss caused by an append extends the newest walk (failure
ledger `chain-cache-rewalks-whole-log-on-append`, P-ENG2 addendum).

The first check after each correction used to miss the cache and re-walk the
whole log (~95 ms on the 4.1 MB campaign logs, whose genesis is one 4.1 MB
line). Now the cache keeps the newest state's bytes per path; a miss on the
same inode grown past them compares the prefix byte for byte and walks only
the appended records. This file pins:

1. the extension: same chains, starts, overlaps, fingerprint and verdicts as
   a walk from byte 0, without calling one;
2. the invalidation: a prefix rewritten alongside an append, a shrink, a
   replaced file (new inode), and a torn appended record all take the full
   path and report what the full walk reports;
3. D-162 kept: a same-size same-tick in-place rewrite after an extension is
   still caught;
4. the harness: with `check_cache="chain"`, every batch's first check is an
   extension, and nothing in the rows changes.
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
import tgms.tgir.check as check_mod
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import ChainCache, _fingerprint, _walk, check
from tgms.tgir.depscope import (
    Checkpoint, DependencyScope, ScopeTerm, Targets, store_identity,
)

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)
MID = make_op("assert_node", uid="M", label="N", props={"tier": "silv"},
              vt_s=0, vt_e=100)
OP0 = make_op("assert_node", uid="A", label="Node", props={"tier": "gold"},
              vt_s=0, vt_e=1000)
T_A = ScopeTerm(targets=Targets(nodes=("A",)))


def _log(*batches: tuple[int, list[dict[str, Any]]]) -> EventLog:
    log = EventLog(Path(tempfile.mkdtemp()) / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _scope(log: EventLog) -> DependencyScope:
    """`tt_q=1600`, checkpointed at the end of the second record."""
    ends = [e for _b, e, _r in log.batches_from(0)]
    return DependencyScope(store=store_identity(log.header(), log.first_batch()),
                           tt_q=1600, terms=(T_A,),
                           checkpoints=(Checkpoint(ends[1], log.chain_of_prefix(ends[1])),))


def _two() -> EventLog:
    return _log((900, [NODE_A]), (1500, [MID]))


# ---------------------------------------------------------------------------
# 1. the extension
# ---------------------------------------------------------------------------

def test_an_append_extends_the_walk_without_re_walking(monkeypatch):
    log = _two()
    scope = _scope(log)
    cache = ChainCache()
    assert check(scope, log, chain_cache=cache).actionable_fresh
    log.append(2500, [OP0])
    want_walk = _walk(log)
    want = check(scope, log).to_json()

    def _no_full_walk(_log: Any) -> Any:
        raise AssertionError("an append must not re-walk from byte 0")

    monkeypatch.setattr(check_mod, "_walk", _no_full_walk)
    got = check(scope, log, chain_cache=cache).to_json()
    assert got == want
    assert got["verdict"] == "possibly-stale"
    assert (cache.misses, cache.extensions, cache.fingerprints) == (2, 1, 1)
    walked = cache.walk(log)
    assert walked.chains == want_walk.chains
    assert walked.starts == want_walk.starts
    assert walked.overlaps == want_walk.overlaps


def test_the_extended_fingerprint_is_the_digest_of_the_file(monkeypatch):
    """So the D-162 hit path compares it against a fresh digest unchanged."""
    log = _two()
    cache = ChainCache()
    cache.walk(log)
    for tt in (2500, 2600, 2700):
        log.append(tt, [OP0])
        cache.walk(log)
    size = log.path.stat().st_size
    (entry,) = [e for k, e in cache._entries.items() if k[1] == size]
    assert entry.fingerprint == _fingerprint(log.path, size)
    assert cache.extensions == 3
    hits = cache.hits
    cache.walk(log)                           # a plain hit: re-digests and matches
    assert cache.hits == hits + 1


def test_an_appended_overlap_is_found_by_the_extension():
    log = _log((900, [make_op("ingest_events", events=[
        {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10}])]))
    cache = ChainCache()
    assert cache.walk(log).overlaps == ()
    log.append(2000, [make_op("ingest_events", events=[
        {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10, "disc": "#0"}])])
    assert len(cache.walk(log).overlaps) == 1
    assert cache.extensions == 1


# ---------------------------------------------------------------------------
# 2. the invalidation
# ---------------------------------------------------------------------------

def _rewrite(log: EventLog, line: int, old: bytes, new: bytes) -> None:
    raw = log.path.read_bytes().split(b"\n")
    assert old in raw[line] and len(old) == len(new)
    raw[line] = raw[line].replace(old, new)
    log.path.write_bytes(b"\n".join(raw))


def test_a_prefix_rewritten_alongside_an_append_re_walks_and_refuses():
    log = _two()
    scope = _scope(log)
    cache = ChainCache()
    assert check(scope, log, chain_cache=cache).actionable_fresh
    _rewrite(log, 2, b'"silv"', b'"lead"')     # inside the checkpointed prefix
    log.append(2500, [OP0])
    assert check(scope, log, chain_cache=cache).reason == "log-rewritten"
    assert cache.extensions == 0 and cache.misses == 2


def test_a_shrink_re_walks():
    log = _log((900, [NODE_A]), (1500, [MID]), (2500, [OP0]))
    cache = ChainCache()
    cache.walk(log)
    raw = log.path.read_bytes()
    log.path.write_bytes(raw[:raw.rstrip(b"\n").rfind(b"\n") + 1])   # drop the last record
    assert cache.walk(log).chains == _walk(log).chains
    assert cache.extensions == 0


def test_a_replaced_file_re_walks_even_if_it_extends_the_old_bytes():
    log = _two()
    cache = ChainCache()
    cache.walk(log)
    grown = _log((900, [NODE_A]), (1500, [MID]), (2500, [OP0]))
    tmp = log.path.with_name("replacement.jsonl")
    shutil.copy(grown.path, tmp)
    os.replace(tmp, log.path)                  # same path, new inode
    assert cache.walk(log).chains == _walk(log).chains
    assert cache.extensions == 0


def test_a_torn_appended_record_refuses_as_the_full_walk_does():
    log = _two()
    scope = _scope(log)
    cache = ChainCache()
    check(scope, log, chain_cache=cache)
    with open(log.path, "ab") as fh:
        fh.write(b'{"batch_id": "torn", "tt": 2500, "ops": [')   # no newline, no close
    want = check(scope, log)
    assert want.reason == "log-unreadable"
    assert check(scope, log, chain_cache=cache).to_json() == want.to_json()


# ---------------------------------------------------------------------------
# 3. D-162
# ---------------------------------------------------------------------------

def test_a_same_tick_same_size_tamper_after_an_extension_is_caught(monkeypatch):
    log = _two()
    scope = _scope(log)
    cache = ChainCache()
    check(scope, log, chain_cache=cache)
    log.append(2500, [OP0])
    assert check(scope, log, chain_cache=cache).state == "possibly-stale"
    assert cache.extensions == 1
    frozen = os.stat(log.path)
    _rewrite(log, 2, b'"silv"', b'"lead"')
    assert log.path.stat().st_size == frozen.st_size
    monkeypatch.setattr(check_mod, "_stat", lambda p: frozen)
    assert check(scope, log, chain_cache=cache).reason == "log-rewritten"


# ---------------------------------------------------------------------------
# 4. the harness
# ---------------------------------------------------------------------------

BACKEND = "native"


@pytest.fixture(scope="module")
def pristine_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("cache-extension-fixture")
    store = tgms.open(base, backend=BACKEND)
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
    return base


def test_every_batch_first_check_is_an_extension(pristine_store: Path) -> None:
    from tgms.eval.storm import Storm

    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine_store, work)
    storm = Storm(work, n_artifacts=15, seed=4, backend=BACKEND, check_cache="chain")
    try:
        assert storm.chain_cache is not None
        assert (storm.chain_cache.misses, storm.chain_cache.extensions) == (1, 0)  # warmed
        rows = [r.to_json() for r in storm.run(4)]
        # one new log state per batch that ran a check, every one extended
        misses = sum(r["check_cache_misses"] for r in rows)
        assert misses >= 1
        assert all(r["check_cache_misses"] in (0, 1) for r in rows)
        assert storm.chain_cache.extensions == misses
        assert storm.chain_cache.fingerprints == 1     # the warm walk's, and no other
    finally:
        storm.close()
