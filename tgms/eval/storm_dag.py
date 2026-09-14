"""The dependency-DAG generator and k-hop cascade driver (Lane C, task C5;
`docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md` §4).

**Built from real registrations, never a synthetic side graph.**
`build_dag` calls `tgms.artifact.registry.Registry.register` once per node,
exactly the `"operator"`-kind idiom `tgms/eval/storm.py::
Storm._register_operator_artifact` already uses (itself restated from
`bench_m5.py:1697-1727`) — a `ToolRouter` call, an `{"op","args"}` blob under
`ops/`, a registration built from that call's own envelope — and the
`parents` tuple on each non-root registration names its real parents' real
`ArtifactId`s, the same "dependency edge" `scripts/demo_propagation.py`
builds by hand for its base -> A -> B arc (`M5_DESIGN.md` §1.3).

**What makes a parent edge, not what a node computes.** Every node's
operator is the cheapest single-entity read (`entity_history`, the same
template `storm.py::_tmpl_entity_history` uses) over one uid the node was
assigned at build time. `demo_propagation.py` makes the same choice: B's own
plan (`NodeScan` over `U1`) never reads A's computed output at all — the
dependency edge is the declared `parents` field, not a data-flow one
(`refresh.py::_advance_parents`'s own docstring: "parents" is a snapshot of
*identity*, re-read from the registry's fold, never a value threaded through
the query). What a node's *uid* controls here is whether a correction to
that uid changes that node's own oracle-recomputed payload — which is what
lets `cascade`'s false-safe and "unnecessary invalidation" metrics mean
something: a node sharing the corrected uid genuinely changes on refresh: a
node with a disjoint uid does not, even though the parent edge still flags
it for recheck. Both are deliberately present in every shape below.

**Cascade stays out of `tgms/artifact/`** (§4, restated at
`M5_CAMPAIGN_FREEZE:707-709`): `parent_recheck` is one level, caller-driven,
no cascade — pinned by `tests/test_propagation.py::
test_walks_one_level_only_no_cascade`. `cascade()` below is the k-hop
worklist the design says belongs in the benchmark: it calls
`parent_recheck`, refreshes each flagged candidate, and re-calls with the
refreshed id — the exact two-call shape that test documents. No line in
`tgms/artifact/` changes for this file to exist.

**False-safe is computed by the oracle, per §4/§8-G-S2**: after the cascade
quiesces (or hits its `k` bound), every *other* currently-registered
artifact this call did not visit is itself refreshed once and compared
against its own pre-cascade payload; a payload that changed anyway is a
false-safe — a downstream artifact the cascade should have reached but did
not. This is the same "one real execution IS the ground truth" trick
`storm.py`'s own oracle uses, so it inherits the same cost model: this
oracle pass is `O(N)` re-executions over the *non-visited* population,
exactly the way `storm.py`'s own global-recompute arm is `O(N)` per batch —
acceptable per-cascade-call, not per-batch.
"""

from __future__ import annotations

import json
import random
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from tgms.core.errors import TgmsError
from tgms.eval.corrections import Substrate, probe_substrate
from tgms.tgir.depscope import DependencyScope

from tgms.artifact.propagate import parent_recheck
from tgms.artifact.record import ArtifactId, ArtifactRecord
from tgms.artifact.refresh import refresh
from tgms.artifact.registry import Registry
from tgms.artifact.witness import RefreshHandle

#: §4's five shapes.
SHAPES: tuple[str, ...] = ("chain", "tree", "diamond", "layered", "power-law")

#: §4's stated ranges — enforced here so a caller's typo does not silently
#: register a graph nobody asked for.
DEPTH_RANGE: tuple[int, int] = (1, 32)
FANOUT_RANGE: tuple[int, int] = (1, 1000)

#: A hard safety valve on total registered nodes, independent of `depth`/
#: `fanout`: `tree`/`layered`/`power-law` at the design's own extremes
#: (depth 32, fanout 1000) would ask for astronomically many real
#: registrations, each a real `ToolRouter` call and a real registry append —
#: nothing like R-18's own posting-list-index escape hatch exists for this
#: generator, so `build_dag` truncates each shape's growth once `max_nodes`
#: registrations have been planned, and reports the truncation rather than
#: silently building a smaller graph than the caller asked for
#: (`DagInfo.truncated`). This is this module's own documented deviation
#: from a literal reading of §4's ranges.
DEFAULT_MAX_NODES = 4000


# ---------------------------------------------------------------------------
# planning — pure, no registry/store access; separated so shape logic is
# testable without a store
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _PlannedNode:
    name: str
    level: int
    parent_names: tuple[str, ...]
    uid: str


def _child_uid(rng: random.Random, parent_uid: str, cold_pool: Sequence[str],
               *, p_inherit: float = 0.5) -> str:
    """Half the time a child shares its parent's uid (a real correction to
    that uid changes both on recompute — the cascade's "true positive"
    lineage); half the time it draws a different real uid from the
    substrate (`cold_pool`), so a correction upstream reaches it via the
    parent edge alone without changing its payload — the "unnecessary
    invalidation" case §4 asks to be measured, not merely possible."""
    if rng.random() < p_inherit or not cold_pool:
        return parent_uid
    return cold_pool[rng.randrange(len(cold_pool))]


def _plan_chain(depth: int, fanout: int, rng: random.Random, sub: Substrate,
                max_nodes: int, name_prefix: str) -> list[_PlannedNode]:
    """Fanout is not meaningful for a chain (fan-in/out is always 1 by
    definition) and is accepted but ignored — documented deviation."""
    hot = sub.uids[0]
    n = min(depth, max_nodes)
    plan: list[_PlannedNode] = []
    for i in range(n):
        name = f"{name_prefix}-chain-{i:04d}"
        parents = (plan[-1].name,) if plan else ()
        plan.append(_PlannedNode(name, i, parents, hot))
    return plan


def _plan_tree(depth: int, fanout: int, rng: random.Random, sub: Substrate,
              max_nodes: int, name_prefix: str) -> list[_PlannedNode]:
    hot = sub.uids[0]
    cold = list(sub.uids[1:]) or list(sub.uids)
    root = _PlannedNode(f"{name_prefix}-tree-0000-0", 0, (), hot)
    plan: list[_PlannedNode] = [root]
    current_level = [root]
    idx = 1
    for level in range(1, depth):
        next_level: list[_PlannedNode] = []
        for parent in current_level:
            for _c in range(fanout):
                if len(plan) >= max_nodes:
                    return plan
                name = f"{name_prefix}-tree-{idx:04d}-{level}"
                idx += 1
                uid = _child_uid(rng, parent.uid, cold)
                node = _PlannedNode(name, level, (parent.name,), uid)
                plan.append(node)
                next_level.append(node)
        if not next_level:
            break
        current_level = next_level
    return plan


def _plan_diamond(depth: int, fanout: int, rng: random.Random, sub: Substrate,
                  max_nodes: int, name_prefix: str) -> list[_PlannedNode]:
    """One diamond is root -> `fanout` mid nodes -> single sink (3 levels);
    `depth` stacks additional diamonds on top of the previous sink, each
    contributing 2 more levels (mid + sink) after the first. All nodes in
    one connected diamond stack share the root's uid — a diamond's whole
    point is the sink's *multiple* parents, best exercised when a single
    correction genuinely threatens every one of them at once."""
    hot = sub.uids[0]
    root = _PlannedNode(f"{name_prefix}-diamond-0000-0", 0, (), hot)
    plan: list[_PlannedNode] = [root]
    idx = 1
    level = 0
    current: _PlannedNode = root
    while level + 2 < depth + 1 and len(plan) < max_nodes:
        level += 1
        mids: list[_PlannedNode] = []
        for _c in range(fanout):
            if len(plan) >= max_nodes:
                break
            name = f"{name_prefix}-diamond-{idx:04d}-{level}"
            idx += 1
            node = _PlannedNode(name, level, (current.name,), hot)
            plan.append(node)
            mids.append(node)
        if not mids:
            break
        level += 1
        if len(plan) >= max_nodes:
            break
        sink_name = f"{name_prefix}-diamond-{idx:04d}-{level}"
        idx += 1
        sink = _PlannedNode(sink_name, level, tuple(m.name for m in mids), hot)
        plan.append(sink)
        current = sink
        if fanout <= 1:
            # a "diamond" with fanout 1 degenerates to a chain; one lap is
            # enough to demonstrate the shape without looping forever.
            break
    return plan


def _plan_layered(depth: int, fanout: int, rng: random.Random, sub: Substrate,
                  max_nodes: int, name_prefix: str) -> list[_PlannedNode]:
    """`fanout` nodes per layer; every node in layer L+1 depends on **every**
    node in layer L (capped at `min(fanout, 32)` parents per node so a
    `--dag-fanout 1000` run does not register nodes with 1,000-entry
    `parents` tuples) — the classic layered-DAG shape (neural-net-style
    layering), distinct from `tree`'s single-parent branching.

    Unlike every other shape, layer 0 here is `fanout` **independent**
    roots, not one — so each gets its own reserved uid (cycling through
    `sub.uids`), never the same value twice. Giving them all the same "hot"
    uid (this function's first cut) made two roots with no edge between
    them at all look, to the oracle, like one had "caused" the other's
    payload to change — a false-safe by construction, not a cascade bug;
    the fix is that a correction to one root's uid must never also be a
    different root's uid. `cold` (non-inheriting descendants) is drawn only
    from the remainder, so a descendant can never coincide with a root's
    reserved identity either.
    """
    n_roots = max(1, fanout)
    root_uids = [sub.uids[c % len(sub.uids)] for c in range(n_roots)]
    reserved = set(root_uids)
    cold = [u for u in sub.uids if u not in reserved] or list(sub.uids)
    cap = min(fanout, 32)
    layer0: list[_PlannedNode] = []
    for c in range(n_roots):
        if len(layer0) >= max_nodes:
            break
        name = f"{name_prefix}-layered-0000-{c}"
        layer0.append(_PlannedNode(name, 0, (), root_uids[c]))
    plan: list[_PlannedNode] = list(layer0)
    current = layer0
    idx = len(plan)
    for level in range(1, depth):
        next_level: list[_PlannedNode] = []
        parent_pool = current[:cap]
        for c in range(fanout):
            if len(plan) >= max_nodes:
                return plan
            name = f"{name_prefix}-layered-{idx:04d}-{level}"
            idx += 1
            uid = _child_uid(rng, parent_pool[c % len(parent_pool)].uid, cold)
            node = _PlannedNode(name, level, tuple(p.name for p in parent_pool), uid)
            plan.append(node)
            next_level.append(node)
        if not next_level:
            break
        current = next_level
    return plan


def _plan_power_law(depth: int, fanout: int, rng: random.Random, sub: Substrate,
                    max_nodes: int, name_prefix: str) -> list[_PlannedNode]:
    """A Barabasi-Albert-style preferential-attachment DAG: `n = depth *
    fanout` total nodes (capped at `max_nodes`) — `depth`/`fanout` do not
    map onto layers here (there are none), so this is the documented
    reinterpretation of those two knobs for this one shape: they set the
    population size, not a layer count/width. Node 0 is the hub (the
    "hot" uid); node `i >= 1` attaches to `min(fanout, i)` earlier nodes,
    each chosen with probability proportional to `1 + (children already
    attached)` — new nodes preferentially attach to already-popular ones,
    the mechanism that produces the power-law degree distribution."""
    hot = sub.uids[0]
    cold = list(sub.uids[1:]) or list(sub.uids)
    n = min(max(2, depth * fanout), max_nodes)
    root = _PlannedNode(f"{name_prefix}-pl-0000", 0, (), hot)
    plan: list[_PlannedNode] = [root]
    weight = [1]  # weight[j] tracks node j's current in-degree + 1
    for i in range(1, n):
        m = min(fanout, i)
        pool = list(range(i))
        chosen: list[int] = []
        pool_weights = list(weight)
        for _ in range(m):
            total = sum(pool_weights)
            r = rng.random() * total
            acc = 0.0
            pick = pool[-1]
            for j, w in zip(pool, pool_weights):
                acc += w
                if r <= acc:
                    pick = j
                    break
            chosen.append(pick)
            pos = pool.index(pick)
            pool.pop(pos)
            pool_weights.pop(pos)
            if not pool:
                break
        parent_names = tuple(plan[j].name for j in chosen)
        parent_uid = plan[chosen[0]].uid if chosen else hot
        uid = _child_uid(rng, parent_uid, cold)
        node = _PlannedNode(f"{name_prefix}-pl-{i:04d}", 0, parent_names, uid)
        plan.append(node)
        for j in chosen:
            weight[j] += 1
        weight.append(1)
    return plan


_SHAPE_BUILDERS: dict[str, Callable[..., list[_PlannedNode]]] = {
    "chain": _plan_chain, "tree": _plan_tree, "diamond": _plan_diamond,
    "layered": _plan_layered, "power-law": _plan_power_law,
}


# ---------------------------------------------------------------------------
# registration — real registry.register() calls, the "operator"-kind idiom
# ---------------------------------------------------------------------------

def _register_operator_artifact(store: Any, registry: Registry, name: str, op: str,
                                args: dict[str, Any],
                                parents: tuple[ArtifactId, ...]) -> ArtifactRecord:
    """Restated from `storm.py::Storm._register_operator_artifact`
    (itself `bench_m5.py:1697-1727`) rather than imported as a bound
    method — this module builds DAGs over any registry/store pair, with or
    without a live `Storm`, and the module docstring's "restated verbatim"
    reasoning (`storm.py::_payload_of`) applies the same way here.

    **One deliberate difference from `storm.py`'s own copy: `payload` is
    populated here, at registration, not left `None`.** `storm.py` gets
    away with a `None` initial payload because it keeps its own external
    `RegisteredArtifact.last_env` bookkeeping and never reads
    `record.payload` for generation 0. `cascade`'s false-safe oracle
    (below) has no such external table — it reads `record.payload` off
    whatever the registry hands back for *any* node, visited or not,
    including one `cascade` never touches at all. A `None` payload at
    generation 0 would make that first real refresh's digest look
    unconditionally "changed" for every never-yet-refreshed node in the
    registry, regardless of whether the correction under test actually
    reached it — a false-safe manufactured by this module's own
    bookkeeping gap, not a cascade defect. Mirroring `refresh._publish`'s
    own `ResultStore` write here (rather than reinventing a second table)
    closes that gap the same way the registry closes it for every
    subsequent generation.
    """
    from tgms.agent.executor import ResultStore
    from tgms.tools.server import ToolRouter

    router = ToolRouter(store.adapter, tt_source=store)
    env = router.call(op, args)
    if "error" in env:
        raise TgmsError(env.get("message", "operator call refused"), name=name, op=op)
    meta = router.leaf_meta(op, env)
    store_dir = Path(store.path)
    ops_dir = store_dir / "ops"
    ops_dir.mkdir(exist_ok=True)
    ref = f"ops/{name}.json"
    (store_dir / ref).write_text(json.dumps({"op": op, "args": args}))
    dependency = DependencyScope.from_json(env["dependency"])
    payload = None
    if env.get("result_digest") is not None:
        result_store = ResultStore(registry.store_dir / "results")
        d = result_store.put(env)
        payload = {"result_digest": d, "result_ref": f"results/{d}.json"}
    return registry.register(
        name=name, kind="query_result",
        plan={"plan_digest": meta.get("plan_digest"), "node_digest": meta.get("node_digest"),
             "plan_format": None},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
              "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": meta.get("completeness", "unknown"),
              "exactness": meta.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "operator", "ref": ref, "basis_policy": "open"},
        dependency=dependency, parents=parents, payload=payload,
    )


@dataclass(frozen=True, slots=True)
class DagNode:
    """One planned-and-registered node."""

    name: str
    level: int
    parents: tuple[str, ...]  # parent artifact *names* at build time
    uid: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level, "parents": list(self.parents),
                "uid": self.uid}


@dataclass(frozen=True, slots=True)
class DagInfo:
    """`build_dag`'s answer: every node, in registration order, plus the
    per-node registration cost (§4's "registration cost (ms and bytes)")."""

    shape: str
    depth: int
    fanout: int
    seed: int
    nodes: tuple[DagNode, ...]
    register_ms: dict[str, float]
    register_bytes: int
    truncated: bool

    @property
    def depth_achieved(self) -> int:
        return (max((n.level for n in self.nodes), default=-1)) + 1

    @property
    def fanout_achieved(self) -> int:
        """The maximum number of children any single node actually has —
        distinct from `parents`-per-node (`diamond`/`layered` sinks can have
        many parents; this is the branching-out direction)."""
        out_degree: dict[str, int] = {}
        for n in self.nodes:
            for p in n.parents:
                out_degree[p] = out_degree.get(p, 0) + 1
        return max(out_degree.values(), default=0)

    def to_json(self) -> dict[str, Any]:
        return {
            "shape": self.shape, "depth": self.depth, "fanout": self.fanout, "seed": self.seed,
            "n_nodes": len(self.nodes), "depth_achieved": self.depth_achieved,
            "fanout_achieved": self.fanout_achieved, "truncated": self.truncated,
            "register_ms_total": sum(self.register_ms.values()),
            "register_bytes": self.register_bytes,
            "nodes": [n.to_json() for n in self.nodes],
        }


def build_dag(registry: Registry, store: Any, shape: str, depth: int, fanout: int, seed: int,
             *, name_prefix: str = "dag", max_nodes: int = DEFAULT_MAX_NODES) -> DagInfo:
    """Register a real dependency DAG of the given `shape` (§4).

    Every non-root node's `parents` names its real, already-registered
    parents' `ArtifactId`s — nothing here is a synthetic side graph.
    """
    if shape not in SHAPES:
        raise ValueError(f"unknown DAG shape: {shape!r}; choose from {SHAPES}")
    if not (DEPTH_RANGE[0] <= depth <= DEPTH_RANGE[1]):
        raise ValueError(f"depth {depth} outside {DEPTH_RANGE} (design memo §4)")
    if not (FANOUT_RANGE[0] <= fanout <= FANOUT_RANGE[1]):
        raise ValueError(f"fanout {fanout} outside {FANOUT_RANGE} (design memo §4)")

    rng = random.Random(seed)
    sub = probe_substrate(store, rng=random.Random(seed))
    if not sub.uids:
        raise TgmsError("no uids available in this store to build a DAG over")

    planned = _SHAPE_BUILDERS[shape](depth, fanout, rng, sub, max_nodes, name_prefix)
    truncated = len(planned) >= max_nodes

    register_ms: dict[str, float] = {}
    id_by_name: dict[str, ArtifactId] = {}
    nodes: list[DagNode] = []
    for pn in planned:
        parent_ids = tuple(id_by_name[p] for p in pn.parent_names)
        t0 = time.perf_counter()
        record = _register_operator_artifact(
            store, registry, pn.name, "entity_history",
            {"uid": pn.uid, "include_edges": True}, parent_ids)
        register_ms[pn.name] = (time.perf_counter() - t0) * 1000
        id_by_name[pn.name] = record.id
        nodes.append(DagNode(name=pn.name, level=pn.level, parents=pn.parent_names, uid=pn.uid))

    return DagInfo(shape=shape, depth=depth, fanout=fanout, seed=seed, nodes=tuple(nodes),
                   register_ms=register_ms,
                   register_bytes=registry.path.stat().st_size, truncated=truncated)


# ---------------------------------------------------------------------------
# the k-hop cascade driver (§4's "caller-driven, no cascade in propagate.py")
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CascadeLevelStat:
    hop: int
    nodes_visited: int
    latency_ms: float

    def to_json(self) -> dict[str, Any]:
        return {"hop": self.hop, "nodes_visited": self.nodes_visited,
                "latency_ms": self.latency_ms}


def _rss_kb() -> dict[str, int]:
    """Restated from `scripts/eval_bitemporal.py::_rss_kb` (module note
    there: `ru_maxrss` is KB on Linux, bytes on macOS)."""
    out: dict[str, int] = {}
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                k, v = line.split(":")
                out[k.lower()] = int(v.split()[0])
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out["maxrss_kb"] = ru // 1024 if sys.platform == "darwin" else ru
    return out


@dataclass(frozen=True, slots=True)
class CascadeResult:
    """The k-hop cascade's answer, scored against the oracle (§4's metrics:
    nodes visited, per-hop latency, unnecessary invalidations, false-safe —
    which must be `()` for a correct `k >= the DAG's true depth from the
    refreshed root`, and is the intended way to *demonstrate* a false-safe
    by truncating `k` below that depth)."""

    seed_name: str
    k: int
    refreshed: tuple[str, ...]
    levels: tuple[CascadeLevelStat, ...]
    unnecessary_invalidations: tuple[str, ...]
    false_safe: tuple[str, ...]
    quiescent: bool
    peak_rss_kb: int | None

    @property
    def nodes_visited(self) -> int:
        return len(self.refreshed)

    def to_json(self) -> dict[str, Any]:
        return {
            "seed_name": self.seed_name, "k": self.k, "refreshed": list(self.refreshed),
            "nodes_visited": self.nodes_visited,
            "levels": [level.to_json() for level in self.levels],
            "unnecessary_invalidations": list(self.unnecessary_invalidations),
            "unnecessary_invalidations_count": len(self.unnecessary_invalidations),
            "false_safe": list(self.false_safe), "false_safe_count": len(self.false_safe),
            "quiescent": self.quiescent, "peak_rss_kb": self.peak_rss_kb,
        }


def _handle_for(record: ArtifactRecord) -> RefreshHandle:
    return RefreshHandle(record.id, record.refresh["kind"], record.refresh["ref"],
                         record.plan.get("plan_format"), record.refresh.get("basis_policy", "open"))


def cascade(registry: Registry, store: Any, refreshed: ArtifactId, k: int, *,
           track_rss: bool = True) -> CascadeResult:
    """The k-hop worklist §4 asks for: repeatedly call `parent_recheck`,
    `refresh()` each flagged candidate, and re-call with the refreshed id —
    `tests/test_propagation.py::test_walks_one_level_only_no_cascade`'s own
    two-call shape, generalized to `k` hops (`k=1` is exactly that test's
    single call).

    `store`/`registry` are threaded through explicitly rather than folded
    into `refreshed` because `refresh()` needs a live store — the same
    reason `Storm` and `demo_propagation.py` both carry `store` alongside
    their registries. This is the one deviation from the design memo's own
    written signature `cascade(registry, refreshed, k)` (§4): a cascade
    that cannot open a store cannot refresh anyone found, so `store` must be
    a parameter, not an implicit global.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    frontier: list[ArtifactId] = [refreshed]
    visited: list[str] = []
    visited_set: set[str] = {refreshed.name}
    unnecessary: list[str] = []
    levels: list[CascadeLevelStat] = []
    hop = 0
    while frontier and hop < k:
        hop += 1
        t0 = time.perf_counter()
        next_frontier: list[ArtifactId] = []
        hop_count = 0
        for fid in frontier:
            result = parent_recheck(fid, registry)
            for candidate in result.candidates:
                record = candidate.record
                if record.name in visited_set:
                    continue
                visited_set.add(record.name)
                old_digest = (record.payload or {}).get("result_digest")
                try:
                    new_record = refresh(record, _handle_for(record), store, registry)
                except TgmsError:
                    continue
                visited.append(record.name)
                hop_count += 1
                new_digest = (new_record.payload or {}).get("result_digest")
                if new_digest == old_digest:
                    unnecessary.append(record.name)
                next_frontier.append(new_record.id)
        levels.append(CascadeLevelStat(hop=hop, nodes_visited=hop_count,
                                       latency_ms=(time.perf_counter() - t0) * 1000))
        frontier = next_frontier

    # false-safe (§4/§8-G-S2): every OTHER currently-registered artifact this
    # walk did not visit is itself refreshed once (the oracle) and compared
    # against its own pre-cascade payload.
    false_safe: list[str] = []
    for record in registry.current_generations():
        if record.name in visited_set:
            continue
        old_digest = (record.payload or {}).get("result_digest")
        try:
            new_record = refresh(record, _handle_for(record), store, registry)
        except TgmsError:
            continue
        new_digest = (new_record.payload or {}).get("result_digest")
        if new_digest != old_digest:
            false_safe.append(record.name)

    peak_rss = _rss_kb().get("maxrss_kb") if track_rss else None
    return CascadeResult(
        seed_name=refreshed.name, k=k, refreshed=tuple(visited), levels=tuple(levels),
        unnecessary_invalidations=tuple(unnecessary), false_safe=tuple(sorted(false_safe)),
        quiescent=not frontier, peak_rss_kb=peak_rss)


__all__ = [
    "SHAPES", "DEPTH_RANGE", "FANOUT_RANGE", "DEFAULT_MAX_NODES",
    "DagNode", "DagInfo", "build_dag",
    "CascadeLevelStat", "CascadeResult", "cascade",
]
