"""Shared validation per burst (failure ledger `check-fingerprint-per-check`,
pre-registered engine change P-ENG2).

A `ChainCache` hit re-digests the whole cached log prefix (D-162), ~10 ms of
the ~12 ms a check cost on the 4 MB campaign logs, and the store identity
memo re-reads the 3.4 MB genesis record on every check and twice per
publish. Inside `with cache.burst():` / `with registry.burst():` the first
call per log state does the full validation and the rest of the block shares
it, re-checking only the stat key (and, for the chain cache, the log's last
bytes). This file pins:

1. the sharing: one whole-prefix digest and one identity validation per log
   state per burst, however many checks; outside a burst, nothing changes;
2. the invalidation: an append inside a burst, or a rewrite of the tail with
   every stat field frozen, sends the next check back to the full path;
3. D-162 kept where it was promised: outside a burst, and at the start of
   every burst, a same-size same-tick in-place rewrite of the middle of the
   log is still caught — and the one case a burst gives up (that rewrite
   *during* a burst) is written down as a test, not left implicit;
4. the identity memo and the registry: one prefix compare per log state per
   burst, appends re-compare, a genesis rewrite before the burst is caught,
   and the published bytes are identical with and without a burst.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import tgms.tgir.check as check_mod
import tgms.tgir.depscope as depscope_mod
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import ChainCache, check
from tgms.tgir.depscope import (
    Checkpoint, DependencyScope, ScopeTerm, StoreIdentityMemo, Targets, store_identity,
)

from tgms.artifact.record import StepDependency
from tgms.artifact.registry import Registry

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100)
MID = make_op("assert_node", uid="M", label="N", props={"tier": "silv"},
              vt_s=0, vt_e=100)
OP0 = make_op("assert_node", uid="A", label="Node", props={"tier": "gold"},
              vt_s=0, vt_e=1000)
T_A = ScopeTerm(targets=Targets(nodes=("A",)))


def _log(*batches: tuple[int, list[dict[str, Any]]], store: Path | None = None) -> EventLog:
    log = EventLog((store or Path(tempfile.mkdtemp())) / "eventlog.jsonl")
    for tt, ops in batches:
        log.append(tt, ops)
    return log


def _identity(log: EventLog) -> str:
    return store_identity(log.header(), log.first_batch())


def _checkpointed(log: EventLog) -> DependencyScope:
    """`tt_q=1600` with a checkpoint at the end of the second record, so a
    rewrite of that record is `log-rewritten` and the third is the suffix."""
    ends = [e for _b, e, _r in log.batches_from(0)]
    return DependencyScope(store=_identity(log), tt_q=1600, terms=(T_A,),
                           checkpoints=(Checkpoint(ends[1], log.chain_of_prefix(ends[1])),))


def _three(*, pad: bool = False) -> EventLog:
    """`pad` appends a record longer than the burst's tail window, so the
    second record lies outside the bytes a burst re-reads."""
    batches = [(900, [NODE_A]), (1500, [MID]), (2500, [OP0])]
    if pad:
        big = "x" * (2 * check_mod._TAIL_BYTES)
        batches.append((3000, [make_op("assert_node", uid="P", label="N",
                                       props={"pad": big}, vt_s=0, vt_e=100)]))
    return _log(*batches)


def _rewrite(log: EventLog, line: int, old: bytes, new: bytes) -> None:
    raw = log.path.read_bytes().split(b"\n")
    assert old in raw[line] and len(old) == len(new)
    raw[line] = raw[line].replace(old, new)
    size = log.path.stat().st_size
    log.path.write_bytes(b"\n".join(raw))
    assert log.path.stat().st_size == size


# ---------------------------------------------------------------------------
# 1. the sharing
# ---------------------------------------------------------------------------

def test_a_burst_digests_the_whole_prefix_once_per_log_state():
    log = _three()
    scope = _checkpointed(log)
    plain = check(scope, log).to_json()
    cache = ChainCache()
    with cache.burst():
        got = [check(scope, log, chain_cache=cache).to_json() for _ in range(6)]
    assert got == [plain] * 6
    assert (cache.misses, cache.hits, cache.fingerprints) == (1, 5, 1)


def test_outside_a_burst_every_hit_still_digests():
    """D-162 unchanged by default: no burst, one digest per hit."""
    log = _three()
    scope = _checkpointed(log)
    cache = ChainCache()
    for _ in range(4):
        check(scope, log, chain_cache=cache)
    assert (cache.misses, cache.hits, cache.fingerprints) == (1, 3, 4)


def test_a_burst_shares_the_store_identity_from_the_second_check(monkeypatch):
    log = _three()
    scope = _checkpointed(log)
    calls = []
    real = check_mod._store_identity_of
    monkeypatch.setattr(check_mod, "_store_identity_of",
                        lambda lg: calls.append(1) or real(lg))
    cache = ChainCache()
    with cache.burst():
        for _ in range(6):
            check(scope, log, chain_cache=cache)
    # the first check computes it before its walk validates the state, and
    # the walk adopts it (same stat key); the other five are served it
    assert len(calls) == 1
    calls.clear()
    for _ in range(3):                      # outside the burst: every check asks
        check(scope, log, chain_cache=cache)
    assert len(calls) == 3


def test_bursts_nest_and_the_state_dies_with_the_outermost():
    log = _three()
    scope = _checkpointed(log)
    cache = ChainCache()
    with cache.burst():
        check(scope, log, chain_cache=cache)
        with cache.burst():
            check(scope, log, chain_cache=cache)
        check(scope, log, chain_cache=cache)
        assert cache.fingerprints == 1
    check(scope, log, chain_cache=cache)
    assert cache.fingerprints == 2


# ---------------------------------------------------------------------------
# 2. the invalidation
# ---------------------------------------------------------------------------

def test_an_append_inside_a_burst_is_a_new_log_state():
    log = _log((900, [NODE_A]))
    scope = DependencyScope(store=_identity(log), tt_q=1000, terms=(T_A,))
    cache = ChainCache()
    with cache.burst():
        assert check(scope, log, chain_cache=cache).actionable_fresh
        log.append(2000, [OP0])
        assert check(scope, log, chain_cache=cache).state == "possibly-stale"
        assert check(scope, log, chain_cache=cache).state == "possibly-stale"
    # the append is a new state (a miss), and the check after it in the
    # same burst is served that state (a hit)
    assert (cache.misses, cache.hits) == (2, 1)


def test_a_tail_rewrite_with_every_stat_field_frozen_is_caught_inside_a_burst(monkeypatch):
    """The tail compare is the burst's own tripwire: freezing `_stat` (the
    D-162 aliasing) and rewriting the last record same-size still sends the
    check back to the full path, where the digest mismatch re-walks."""
    log = _three()
    scope = _checkpointed(log)
    cache = ChainCache()
    frozen = os.stat(log.path)
    monkeypatch.setattr(check_mod, "_stat", lambda p: frozen)
    with cache.burst():
        before = check(scope, log, chain_cache=cache)
        _rewrite(log, 3, b'"gold"', b'"lead"')       # the suffix record, in the tail
        after = check(scope, log, chain_cache=cache)
    assert cache.misses == 2
    assert before.state == after.state == "possibly-stale"


# ---------------------------------------------------------------------------
# 3. what D-162 keeps, and the one case a burst gives up
# ---------------------------------------------------------------------------

def test_a_mid_log_rewrite_before_a_burst_is_caught_by_its_first_check(monkeypatch):
    """D-162's case, across a burst boundary: the cache was populated, the
    middle of the checkpointed prefix is rewritten in place inside one tick,
    and the next burst's first check re-digests and refuses."""
    log = _three()
    scope = _checkpointed(log)
    cache = ChainCache()
    with cache.burst():
        assert check(scope, log, chain_cache=cache).state == "possibly-stale"
    frozen = os.stat(log.path)
    _rewrite(log, 2, b'"silv"', b'"lead"')
    monkeypatch.setattr(check_mod, "_stat", lambda p: frozen)
    with cache.burst():
        assert check(scope, log, chain_cache=cache).reason == "log-rewritten"
        assert check(scope, log, chain_cache=cache).reason == "log-rewritten"
    assert cache.misses == 2


def test_a_mid_log_same_tick_rewrite_during_a_burst_is_the_case_it_gives_up(monkeypatch):
    """Recorded rather than papered over: inside a burst, a same-size
    in-place rewrite of the *middle* of the log that leaves every stat field
    and the tail untouched is served the burst's state — the caller vouched
    that nothing rewrites the log during the block. The block's end restores
    D-162: the next check outside it re-digests and refuses."""
    log = _three(pad=True)
    scope = _checkpointed(log)
    ends = [e for _b, e, _r in log.batches_from(0)]
    assert log.path.stat().st_size - ends[1] > check_mod._TAIL_BYTES
    cache = ChainCache()
    frozen = os.stat(log.path)
    monkeypatch.setattr(check_mod, "_stat", lambda p: frozen)
    with cache.burst():
        assert check(scope, log, chain_cache=cache).state == "possibly-stale"
        _rewrite(log, 2, b'"silv"', b'"lead"')
        assert check(scope, log, chain_cache=cache).state == "possibly-stale"
    assert check(scope, log, chain_cache=cache).reason == "log-rewritten"


# ---------------------------------------------------------------------------
# 4. the identity memo and the registry
# ---------------------------------------------------------------------------

def _counting_open(monkeypatch) -> list[str]:
    opened: list[str] = []

    def _open(path, *args, **kw):
        opened.append(str(path))
        return open(path, *args, **kw)

    monkeypatch.setattr(depscope_mod, "open", _open, raising=False)
    return opened


def test_the_identity_memo_compares_the_prefix_once_per_burst(monkeypatch):
    log = _three()
    memo = StoreIdentityMemo()
    want = _identity(log)
    opened = _counting_open(monkeypatch)
    assert memo.identity(log) == want              # miss: computes, keeps the prefix
    with memo.burst():
        for _ in range(5):
            assert memo.identity(log) == want
    # the miss read the prefix once; the burst compared it once, then served
    assert len(opened) == 2
    opened.clear()
    for _ in range(3):                              # outside: a compare per call
        memo.identity(log)
    assert len(opened) == 3


def test_an_append_inside_an_identity_burst_re_compares(monkeypatch):
    log = _three()
    memo = StoreIdentityMemo()
    want = _identity(log)
    memo.identity(log)
    opened = _counting_open(monkeypatch)
    with memo.burst():
        memo.identity(log)
        memo.identity(log)
        log.append(3000, [MID])
        assert memo.identity(log) == want           # the genesis did not change
        memo.identity(log)
    assert len(opened) == 2


def test_a_genesis_rewrite_before_an_identity_burst_is_caught():
    log = _log((900, [NODE_A]), (1500, [MID]))
    memo = StoreIdentityMemo()
    before = memo.identity(log)
    _rewrite(log, 1, b'"N"', b'"Q"')                  # the genesis record, same size
    with memo.burst():
        after = memo.identity(log)
    assert after != before
    assert after == _identity(log)


def _fields(name: str, log: EventLog) -> dict[str, Any]:
    scope = DependencyScope(store=_identity(log), tt_q=1000, terms=(T_A,))
    return dict(
        name=name, kind="query_result",
        plan={"plan_digest": "pd", "node_digest": "nd", "plan_format": 1,
              "plan_ref": "plans/pd.json"},
        basis={"tt_q": 1000, "pinned": False, "clamped": False, "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope)],
    )


def test_a_registry_burst_publishes_the_same_bytes_with_fewer_reads(monkeypatch):
    stores = [Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())]
    logs = [_log((900, [NODE_A]), store=s) for s in stores]
    for log in logs[1:]:
        # same history, so the same identity and the same record bytes
        assert log.path.read_bytes() == logs[0].path.read_bytes()
    plain, bursty = Registry(stores[0]), Registry(stores[1])
    for i in range(4):
        plain.register(**_fields(f"a{i}", logs[0]))
    opened = _counting_open(monkeypatch)
    with bursty.burst():
        for i in range(4):
            bursty.register(**_fields(f"a{i}", logs[1]))
    assert bursty.path.read_bytes() == plain.path.read_bytes()
    # register and append each ask; inside the burst the prefix is compared
    # once for the whole block, against twice per publish without it
    assert len([p for p in opened if p.endswith("eventlog.jsonl")]) <= 2
