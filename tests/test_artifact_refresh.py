"""P2.1 — the selective-refresh executor (`docs/design/M5_EXECUTION_PLAN_2026-08-27.md`
§5 P2.1; `docs/design/M5_DESIGN.md` §5.5, §1, §2.2).

Discharges P2.1's own test obligations: the end-to-end arc on a fixture
store (register -> correct -> `affected()`/`check_artifact` flags it ->
refresh -> new generation FRESH, old generation still POSSIBLY_STALE and
byte-identical on disk -> a second correction round works on the new
generation), the refusal paths (unknown plan_format, name not found,
generation mismatch, plus a few more of `refresh.py`'s own closed
taxonomy), the determinism replay pair, an opaque-leaf ("operator" kind)
refresh, and a CLI smoke of the full arc. `scripts/check_freshness_boundary.py`
staying green is `tests/test_artifact_check.py::test_boundary_script_is_green`
already — re-asserted here too since this file is the one that would catch
`refresh.py` accidentally growing an import onto the guarded allowlist.

Every fixture store here is built without going through `Store`'s own
write API (`assert_node`/`correct`/...): those tick a `HybridLogicalClock`,
which is wall-clock-seeded and therefore not reproducible across two
independent `tempfile.mkdtemp()` directories built "the same way" a few
milliseconds apart. §2.4's determinism obligation is about exactly that
reproducibility, so every write below goes through `_apply`/`_write`
instead — an explicit `tt`, applied to the log and the live backend
directly, mirroring what `Store._write_locked` does minus the clock.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog, extend_chain, replay
from tgms.tgir.depscope import DependencyScope
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import dump
from tgms.tgir.node import NodeScan
from tgms.tgir.plan import Plan

from tgms.artifact.lookup import affected
from tgms.artifact.record import ArtifactId, ArtifactRecord
from tgms.artifact.refresh import REFUSAL_REASONS, RefreshRefused, refresh, resolve_current
from tgms.artifact.registry import Registry
from tgms.artifact.witness import RefreshHandle, check_artifact

#: `TGMS_TEST_BACKEND` overrides, matching `tests/conftest.py::fresh_adapter`'s
#: env var — but the *default* here is `"native"`, not `conftest`'s
#: `"duckdb"`. This file's fixtures register artifacts from a real
#: `run_plan`/`ToolRouter.call` execution against a real backend and then
#: assert FRESH immediately afterward, which needs the registered scope's
#: `tt_q_verified` to be `True`. DuckDB has no replay cursor at all
#: (`hasattr(DuckDBAdapter(...), "note_event_cursor")` is `False`), so
#: `Store._seed_frontier` always falls back to `frontier_verified = False`
#: for it, which rides into every scope this file would build as
#: `tt_q_verified: false` — and an unverified `tt_q` *widens rather than
#: refuses* (`check.py`'s own rule), which flags even the write that
#: created an artifact's own data as a possible revision of it. That is a
#: real, structural property of the DuckDB adapter, not a `refresh.py` bug
#: — `TGMS_TEST_BACKEND=native pytest` is this file's primary verification
#: path for exactly that reason, and is the default here so a bare
#: `pytest tests/test_artifact_refresh.py` matches it too.
BACKEND = os.environ.get("TGMS_TEST_BACKEND", "native")

NODE_A = make_op("assert_node", uid="A", label="N", props={}, vt_s=0, vt_e=100,
                 source="ingest", provenance_ref=None)


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------


def _store_dir() -> Path:
    return Path(tempfile.mkdtemp())


def _correct_a(props: dict[str, Any]) -> dict[str, Any]:
    return make_op("correct", ref={"kind": "node", "uid": "A"}, props=props,
                   vt_s=0, vt_e=100, source="ingest", provenance_ref=None)


def _apply(store: Any, log: EventLog, tt: int, *ops: dict[str, Any]) -> None:
    """A deterministic write: append at an explicit `tt`, then apply the
    same ops to the live adapter directly (see module docstring) —
    mirroring `Store._write_locked` exactly, including the replay-cursor
    bookkeeping, minus the wall-clock `tick()`. Skipping the cursor update
    (a first draft of this helper did) leaves the backend's applied state
    ahead of what its own cursor claims, so a **later, independent** open
    of the same directory (`_recover`) reapplies the batch a second time —
    exactly what the CLI smoke test below does when it reopens the store
    for `artifact refresh`, and exactly what produced a spurious
    POSSIBLY_STALE the first time this helper was written without it.
    """
    batch = list(ops)
    _batch_id, end_offset, record = log.append(tt, batch)
    note_cursor = getattr(store.adapter, "note_event_cursor", None)
    if note_cursor is not None:
        if store._chain is None:
            store._chain = log.chain_of_prefix(end_offset - len(record))
        store._chain = extend_chain(store._chain, record)
    store.adapter.begin()
    try:
        store.adapter.apply_ops(batch, tt)
    except Exception:
        store.adapter.rollback()
        raise
    if note_cursor is not None:
        note_cursor(end_offset, store._chain)
    store.adapter.commit()


def _open_and_replay(store_dir: Path) -> tgms.Store:
    """`eventlog.jsonl` must already exist under `store_dir` (built with
    `EventLog(...).append`) before this is called — it opens a live store
    over it and applies the whole log to the backend."""
    store = tgms.open(store_dir, backend=BACKEND)
    replay(store_dir / "eventlog.jsonl", store.adapter, thread_cursor=True)
    return store


def _register_plan_artifact(store_dir: Path, store: Any, registry: Registry,
                            name: str = "wmc") -> ArtifactRecord:
    """Register a real, re-executable `"tgir_plan"` artifact: a `NodeScan`
    over uid `"A"`, its own blob under `plans/`, and a record built from
    exactly the `run_plan` envelope shape `refresh._publish` itself
    consumes — the fixture exercises the production seam, not a
    hand-typed stand-in for it."""
    scan = NodeScan("p", uids=("A",))
    env = run_plan(Plan(scan), store.adapter, tt_source=store)
    tgir = env["tgir"]
    plans_dir = store_dir / "plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / f"{tgir['plan_digest']}.json").write_text(json.dumps(dump(scan)))

    dependency = DependencyScope.from_json(env["dependency"])
    return registry.register(
        name=name, kind="query_result",
        plan={"plan_digest": tgir["plan_digest"], "node_digest": tgir["node_digest"],
              "plan_format": 1, "plan_ref": f"plans/{tgir['plan_digest']}.json"},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
               "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": tgir.get("completeness", "unknown"),
               "exactness": tgir.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "tgir_plan", "ref": f"plans/{tgir['plan_digest']}.json",
                "basis_policy": "open"},
        dependency=dependency,
    )


# ---------------------------------------------------------------------------
# 1 — the end-to-end arc
# ---------------------------------------------------------------------------


def test_end_to_end_arc() -> None:
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    assert gen0.generation == 0
    gen0_bytes = gen0.to_json()

    # nothing written since registration: FRESH
    verdict0 = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    assert verdict0.actionable_fresh

    # a correction inside the scope
    _apply(store, log, 20, _correct_a({"x": 1}))

    # `affected()` flags it
    reader = EventLog(store_dir / "eventlog.jsonl")
    batch = list(reader.batches())[-1]
    result = affected(batch, registry)
    assert [r.name for r in result.affected] == ["wmc"]

    # `check_artifact` flags it too, and hands back a usable refresh handle
    verdict1 = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    assert not verdict1.actionable_fresh
    assert verdict1.refresh is not None

    gen1 = refresh(gen0, verdict1.refresh, store, registry)
    assert gen1.name == "wmc"
    assert gen1.generation == 1
    assert gen1.supersedes == gen0.id

    # old generation: untouched, byte-identical on disk
    assert registry.at("wmc", 0).to_json() == gen0_bytes

    # new generation FRESH, old generation still POSSIBLY_STALE
    reader = EventLog(store_dir / "eventlog.jsonl")
    assert check_artifact(gen1, reader).actionable_fresh
    assert not check_artifact(registry.at("wmc", 0), reader).actionable_fresh

    # the new generation's basis is the fresh execution's, not inherited
    assert gen1.basis["tt_q"] == 20
    assert gen0.basis["tt_q"] == 10

    # a second correction round works on the new generation
    _apply(store, log, 30, _correct_a({"x": 2}))
    reader = EventLog(store_dir / "eventlog.jsonl")
    verdict2 = check_artifact(gen1, reader)
    assert not verdict2.actionable_fresh
    gen2 = refresh(gen1, verdict2.refresh, store, registry)
    assert gen2.generation == 2
    assert gen2.supersedes == gen1.id
    assert gen2.basis["tt_q"] == 30
    reader = EventLog(store_dir / "eventlog.jsonl")
    assert check_artifact(gen2, reader).actionable_fresh
    # generation 0 and 1 are still exactly as they were
    assert registry.at("wmc", 0).to_json() == gen0_bytes
    assert registry.at("wmc", 1).to_json() == gen1.to_json()
    store.close()


# ---------------------------------------------------------------------------
# 2 — refusal paths
# ---------------------------------------------------------------------------


def test_refuses_unknown_name() -> None:
    store_dir = _store_dir()
    EventLog(store_dir / "eventlog.jsonl").append(10, [NODE_A])
    registry = Registry(store_dir)
    with pytest.raises(RefreshRefused) as ei:
        resolve_current(registry, "nope", None)
    assert ei.value.reason == "not-found"


def test_refuses_generation_mismatch_at_resolve() -> None:
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    _register_plan_artifact(store_dir, store, registry)
    _apply(store, log, 20, _correct_a({"x": 1}))
    gen0 = registry.at("wmc", 0)
    verdict = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    refresh(gen0, verdict.refresh, store, registry)  # now current is generation 1

    with pytest.raises(RefreshRefused) as ei:
        resolve_current(registry, "wmc", 0)  # 0 is no longer current
    assert ei.value.reason == "generation-mismatch"
    # asking for the actual current generation still works
    assert resolve_current(registry, "wmc", 1).generation == 1
    store.close()


def test_refuses_generation_mismatch_inside_refresh_too() -> None:
    """The same defense lives inside `refresh()` itself, not only in
    `resolve_current` — a caller holding a stale `record` object (fetched
    before some other refresh already ran) must not be able to publish a
    duplicate generation."""
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    _apply(store, log, 20, _correct_a({"x": 1}))
    verdict = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    refresh(gen0, verdict.refresh, store, registry)  # publishes generation 1

    with pytest.raises(RefreshRefused) as ei:
        refresh(gen0, verdict.refresh, store, registry)  # gen0 is stale now
    assert ei.value.reason == "generation-mismatch"
    store.close()


def test_refuses_no_refresh_handle() -> None:
    """§1.4: `check_artifact` already hands back `refresh=None` for an
    unrecognized `plan_format`; `refresh()` refuses that too rather than
    treating `None` as "nothing to do"."""
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    with pytest.raises(RefreshRefused) as ei:
        refresh(gen0, None, store, registry)
    assert ei.value.reason == "no-refresh-handle"
    store.close()


def test_refuses_unrecognized_plan_format_in_the_blob_itself() -> None:
    """Defense in depth beyond `witness.py`'s own check (already covered by
    `tests/test_artifact_check.py`): the handle's `plan_format` was
    recognized when `check_artifact` built it, but the blob on disk
    disagrees — refresh never guesses (§1.4) and refuses independently."""
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    # corrupt the blob: a document whose own plan_format is unrecognized
    ref = gen0.refresh["ref"]
    (store_dir / ref).write_text(json.dumps({"plan_format": 99, "root": {"op": "NodeScan"}}))

    handle = RefreshHandle(gen0.id, "tgir_plan", ref, 1, "open")
    with pytest.raises(RefreshRefused) as ei:
        refresh(gen0, handle, store, registry)
    assert ei.value.reason == "unknown-plan-format"
    # nothing was published
    assert registry.current("wmc").generation == 0
    store.close()


def test_refuses_ref_not_found() -> None:
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    (store_dir / gen0.refresh["ref"]).unlink()

    verdict = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    with pytest.raises(RefreshRefused) as ei:
        refresh(gen0, verdict.refresh, store, registry)
    assert ei.value.reason == "ref-not-found"
    assert registry.current("wmc").generation == 0
    store.close()


def test_refuses_handle_mismatch() -> None:
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    bad_handle = RefreshHandle(ArtifactId("someone-else", 5), "tgir_plan",
                               gen0.refresh["ref"], 1, "open")
    with pytest.raises(RefreshRefused) as ei:
        refresh(gen0, bad_handle, store, registry)
    assert ei.value.reason == "handle-mismatch"
    store.close()


def test_refuses_unknown_refresh_kind() -> None:
    """Defensive only — `ArtifactRecord.__post_init__` already closes
    `refresh.kind` to `REFRESH_KINDS`, so a live record can never carry
    anything else; this exercises `refresh()`'s own closed match against a
    hand-built `RefreshHandle`, which carries no such constructor check."""
    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)
    bad_handle = RefreshHandle(gen0.id, "bogus", gen0.refresh["ref"], None, "open")
    with pytest.raises(RefreshRefused) as ei:
        refresh(gen0, bad_handle, store, registry)
    assert ei.value.reason == "unknown-refresh-kind"
    store.close()


def test_refusal_reasons_are_a_closed_taxonomy() -> None:
    assert set(REFUSAL_REASONS) == {
        "not-found", "generation-mismatch", "no-refresh-handle", "handle-mismatch",
        "unknown-refresh-kind", "ref-not-found", "unknown-plan-format",
        "execution-refused",
    }


# ---------------------------------------------------------------------------
# 3 — determinism replay (§2.4 / P2.1's own obligation)
# ---------------------------------------------------------------------------


def _build_and_refresh_twice(store_dir: Path) -> tuple[bytes, bytes]:
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)
    gen0 = _register_plan_artifact(store_dir, store, registry)

    _apply(store, log, 20, _correct_a({"x": 1}))
    verdict1 = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    gen1 = refresh(gen0, verdict1.refresh, store, registry)

    _apply(store, log, 30, _correct_a({"x": 2}))
    verdict2 = check_artifact(gen1, EventLog(store_dir / "eventlog.jsonl"))
    refresh(gen1, verdict2.refresh, store, registry)
    store.close()

    return ((store_dir / "artifacts.jsonl").read_bytes(),
            (store_dir / "eventlog.jsonl").read_bytes())


def test_deterministic_replay_identical_registry_bytes() -> None:
    """Replaying the same log, corrections and refresh calls into two fresh
    directories yields byte-identical `artifacts.jsonl` files — in
    particular identical `record_digest`s and an identical chain, since the
    chain is `extend_chain` folded over exactly these bytes."""
    d1, d2 = _store_dir(), _store_dir()
    artifacts1, eventlog1 = _build_and_refresh_twice(d1)
    artifacts2, eventlog2 = _build_and_refresh_twice(d2)

    assert eventlog1 == eventlog2  # sanity: the inputs really did match
    assert artifacts1 == artifacts2

    reg1, reg2 = Registry(d1), Registry(d2)
    assert reg1.checkpoint() == reg2.checkpoint()
    for name in reg1.names():
        for rec1, rec2 in zip(reg1.history(name), reg2.history(name)):
            assert rec1.record_digest == rec2.record_digest


# ---------------------------------------------------------------------------
# 4 — an opaque-leaf ("operator" kind) refresh
# ---------------------------------------------------------------------------


def test_operator_kind_refresh_end_to_end() -> None:
    from tgms.tools.server import ToolRouter

    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    registry = Registry(store_dir)

    router = ToolRouter(store.adapter, tt_source=store)
    args = {"kind": "node", "window": {"t_a": 0, "t_b": 1000}}
    env = router.call("version_history", args)
    assert "error" not in env
    meta = router.leaf_meta("version_history", env)

    ref = "ops/version_history.json"
    (store_dir / "ops").mkdir()
    (store_dir / ref).write_text(json.dumps({"op": "version_history", "args": args}))

    dependency = DependencyScope.from_json(env["dependency"])
    gen0 = registry.register(
        name="vh", kind="query_result",
        plan={"plan_digest": meta.get("plan_digest"), "node_digest": meta.get("node_digest"),
              "plan_format": None},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
               "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": meta.get("completeness", "unknown"),
               "exactness": meta.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "operator", "ref": ref, "basis_policy": "open"},
        dependency=dependency,
    )

    verdict0 = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    assert verdict0.actionable_fresh
    assert verdict0.refresh is not None
    assert verdict0.refresh.kind == "operator"
    assert verdict0.refresh.plan_format is None

    _apply(store, log, 20, _correct_a({"x": 1}))
    verdict1 = check_artifact(gen0, EventLog(store_dir / "eventlog.jsonl"))
    assert not verdict1.actionable_fresh

    gen1 = refresh(gen0, verdict1.refresh, store, registry)
    assert gen1.generation == 1
    assert gen1.supersedes == gen0.id
    assert gen1.basis["tt_q"] == 20

    reader = EventLog(store_dir / "eventlog.jsonl")
    assert check_artifact(gen1, reader).actionable_fresh
    assert not check_artifact(registry.at("vh", 0), reader).actionable_fresh
    store.close()


# ---------------------------------------------------------------------------
# 5 — CLI smoke of the full arc
# ---------------------------------------------------------------------------


def test_cli_smoke_full_arc(capsys: pytest.CaptureFixture[str]) -> None:
    from tgms.cli import main

    store_dir = _store_dir()
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [NODE_A])
    store = _open_and_replay(store_dir)
    scan = NodeScan("p", uids=("A",))
    env = run_plan(Plan(scan), store.adapter, tt_source=store)
    tgir = env["tgir"]
    (store_dir / "plans").mkdir()
    (store_dir / "plans" / f"{tgir['plan_digest']}.json").write_text(json.dumps(dump(scan)))
    record_doc = {
        "name": "wmc", "kind": "query_result",
        "plan": {"plan_digest": tgir["plan_digest"], "node_digest": tgir["node_digest"],
                "plan_format": 1, "plan_ref": f"plans/{tgir['plan_digest']}.json"},
        "basis": {"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
                  "tt_q_verified": True},
        "state": {"completeness": tgir.get("completeness", "unknown"),
                  "exactness": tgir.get("exactness", "exact"), "refusal": None},
        "refresh": {"kind": "tgir_plan", "ref": f"plans/{tgir['plan_digest']}.json",
                   "basis_policy": "open"},
        "dependency": env["dependency"],
    }
    record_path = store_dir / "record.json"
    record_path.write_text(json.dumps(record_doc))
    store.close()

    rc = main(["artifact", "register", str(store_dir), "--record-json", str(record_path)])
    assert rc in (0, None)
    capsys.readouterr()

    rc = main(["artifact", "check", str(store_dir), "--name", "wmc"])
    assert rc == 0  # nothing written since registration
    capsys.readouterr()

    # a correction, applied live (through our own handle, then closed —
    # the CLI's `refresh` opens its own handle on the same directory next)
    store = tgms.open(store_dir, backend=BACKEND)
    _apply(store, log, 20, _correct_a({"x": 1}))
    store.close()

    rc = main(["artifact", "check", str(store_dir), "--name", "wmc", "--json"])
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "possibly-stale"
    assert rc == 1

    rc = main(["artifact", "refresh", str(store_dir), "--name", "wmc", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["name"] == "wmc"
    assert out["generation"] == 1
    assert out["supersedes"] == ["wmc", 0]

    rc = main(["artifact", "check", str(store_dir), "--name", "wmc"])
    out = capsys.readouterr().out
    assert "wmc@1" in out
    assert rc == 0  # the new generation is fresh

    rc = main(["artifact", "list", str(store_dir), "--name", "wmc", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert [r["generation"] for r in listed] == [0, 1]


def test_cli_refresh_refuses_unknown_name(capsys: pytest.CaptureFixture[str]) -> None:
    from tgms.cli import main

    store_dir = _store_dir()
    EventLog(store_dir / "eventlog.jsonl").append(10, [NODE_A])
    Registry(store_dir)  # creates artifacts.jsonl's header

    rc = main(["artifact", "refresh", str(store_dir), "--name", "nope", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["details"]["reason"] == "not-found"


def test_cli_refresh_needs_a_name() -> None:
    from tgms.cli import main

    store_dir = _store_dir()
    EventLog(store_dir / "eventlog.jsonl").append(10, [NODE_A])
    with pytest.raises(SystemExit):
        main(["artifact", "refresh", str(store_dir)])


# ---------------------------------------------------------------------------
# 6 — the boundary gate stays green
# ---------------------------------------------------------------------------


def test_boundary_script_is_green() -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_freshness_boundary.py")],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_refresh_module_is_not_on_the_guarded_allowlist() -> None:
    """§2.2's own point: `refresh.py` deliberately does not join
    `scripts/check_freshness_boundary.py`'s `GUARDED` set — it opens a
    store and runs a kernel, the opposite of every guarded module's claim."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_freshness_boundary",
        Path(__file__).resolve().parent.parent / "scripts" / "check_freshness_boundary.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "tgms/artifact/refresh.py" not in mod.GUARDED
