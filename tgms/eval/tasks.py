"""Task-suite generation T1–T4 + correction probes (WP2.5).

Every task carries a program-computed gold answer: the oracle plan is
executed by the engine (never LLM-labeled). Exception: T2 gold comes from the
synthetic generator's planted-structure manifest — also program-computed.

Task = {id, family, dataset, question_text, oracle_plan, answer_kind,
gold, gold_answer_object, input_uids, difficulty}.

Split: 20% dev / 80% test, seeded shuffle; the test split is hashed
(sha256 of canonical JSON) — record the hash in docs/DECISIONS.md when the
real evaluation suite is frozen (spec §8.3).

Correction probes: corrections are injected into the store *before* any gold
is computed (they become part of the belief history); probes then ask
"as of tt just before the correction" vs. "now" — answers differ by
construction, which is the bi-temporal differentiator no baseline can answer.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from tgms.agent.executor import Executor
from tgms.agent.ir import Plan
from tgms.agent.reporter import mechanical_answer
from tgms.agent.verifier import validate_static
from tgms.core.model import EntityRef, canonical_json, sha256_hex
from tgms.store import Store
from tgms.tools.server import ToolRouter

MICROS_PER_DAY = 86_400_000_000
MICROS_PER_WEEK = 7 * MICROS_PER_DAY


@dataclass
class Task:
    id: str
    family: str  # t1 | t2 | t3 | t4 | probe
    dataset: str
    question_text: str
    oracle_plan: dict[str, Any]
    answer_kind: str
    gold: Any
    gold_answer_object: dict[str, Any]
    input_uids: list[str] = field(default_factory=list)
    difficulty: str = "easy"
    gold_source: str = "oracle_plan"  # or "manifest" (T2)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1e6, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def fmt_t(ts: int) -> str:
    """Timestamps in question text carry both forms so no system needs date
    arithmetic: human-readable + exact microseconds."""
    return f"{iso(ts)} ({ts})"


def fmt_w(t_a: int, t_b: int) -> str:
    return f"from {fmt_t(t_a)} to {fmt_t(t_b)}"


# --------------------------------------------------------------------------- #
# generation context                                                           #
# --------------------------------------------------------------------------- #

@dataclass
class Ctx:
    store: Store
    dataset: str
    t0: int
    t1: int
    uids: list[str]  # active nodes, degree-biased sample

    @property
    def span(self) -> int:
        return self.t1 - self.t0


def build_context(store: Store, dataset: str, rng: random.Random,
                  n_uids: int = 100) -> Ctx:
    stats = store.stats()
    e = store.adapter.edges_columnar(columns=("src_id", "dst_id"))
    deg = np.bincount(e["src_id"], minlength=store.adapter.num_entities()) + \
        np.bincount(e["dst_id"], minlength=store.adapter.num_entities())
    order = np.argsort(-deg, kind="stable")
    top = [int(i) for i in order[: n_uids // 2] if deg[i] > 0]
    active = [int(i) for i in np.flatnonzero(deg > 0)]
    extra = rng.sample(active, min(len(active), n_uids - len(top)))
    ids = list(dict.fromkeys(top + extra))
    return Ctx(store=store, dataset=dataset, t0=stats["vt_min"],
               t1=stats["vt_max"], uids=store.adapter.uids_for(ids))


def _window(rng: random.Random, ctx: Ctx, min_frac: float = 0.05,
            max_frac: float = 0.5) -> tuple[int, int]:
    frac = rng.uniform(min_frac, max_frac)
    length = max(2, int(ctx.span * frac))
    start = ctx.t0 + rng.randrange(max(1, ctx.span - length))
    return start, start + length


def _stride_for(t_a: int, t_b: int, target_buckets: int = 20) -> int:
    return max(1, (t_b - t_a) // target_buckets)


# --------------------------------------------------------------------------- #
# gold computation (the engine executes the oracle plan)                       #
# --------------------------------------------------------------------------- #

def compute_gold(store: Store, plan_json: dict[str, Any],
                 input_uids: list[str]) -> tuple[Any, dict[str, Any]] | None:
    verdict = validate_static(plan_json, adapter=store.adapter,
                              task_input_uids=set(input_uids))
    if not verdict["valid"]:
        raise ValueError(f"oracle plan invalid: {verdict['violations']}")
    plan = Plan.from_json(plan_json)
    trace = Executor(ToolRouter(store.adapter)).run(plan)
    if not trace.ok or trace.answer is None:
        return None
    return trace.answer, mechanical_answer(plan, trace)


# --------------------------------------------------------------------------- #
# T1 — temporal QA templates (10 templates x 3 hand-written paraphrases)       #
# --------------------------------------------------------------------------- #

def _p(plan_id: str, steps: list[dict], kind: str, src: str,
       question: str) -> dict[str, Any]:
    return {"plan_id": plan_id, "question": question, "steps": steps,
            "answer_spec": {"kind": kind, "from": src}}


def t1_reach_count(rng: random.Random, ctx: Ctx, q: str):
    x = rng.choice(ctx.uids)
    t_a, t_b = _window(rng, ctx)
    question = q.format(x=x, w=fmt_w(t_a, t_b))
    steps = [
        # gold reads rows_total (exact even when the row page truncates)
        {"id": "s1", "op": "temporal_reachability",
         "args": {"src": x, "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
         "depends_on": []},
    ]
    return question, _p("t1-reach-count", steps, "count", "s1.rows_total",
                        question), [x]


T1_TEMPLATES: list[dict[str, Any]] = [
    {"name": "reach_count", "difficulty": "easy", "build": t1_reach_count,
     "paraphrases": [
         "How many distinct nodes could be reached from {x} via time-respecting paths {w}?",
         "Count the nodes reachable from {x} {w} (edges must be traversed in time order).",
         "{x} starts spreading a message {w}; how many other nodes can it reach?"]},
]


def _register_t1(name: str, difficulty: str, paraphrases: list[str]):
    def deco(fn: Callable):
        T1_TEMPLATES.append({"name": name, "difficulty": difficulty,
                             "build": fn, "paraphrases": paraphrases})
        return fn
    return deco


@_register_t1("reach_first", "easy", [
    "Which node became reachable from {x} at the earliest time {w}?",
    "Starting from {x} {w}, which node is reached first along time-respecting paths?",
    "Of everything {x} can reach {w}, name the earliest-reached node."])
def t1_reach_first(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t_a, t_b = _window(rng, ctx)
    question = q.format(x=x, w=fmt_w(t_a, t_b))
    steps = [
        {"id": "s1", "op": "temporal_reachability",
         "args": {"src": x, "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
         "depends_on": []},
    ]
    return question, _p("t1-reach-first", steps, "value", "s1.rows[0].uid",
                        question), [x]


@_register_t1("neighbors_at", "medium", [
    "Who were {x}'s direct neighbors (either direction) at {t}?",
    "List the nodes connected to {x} by an active edge at {t}.",
    "At {t}, which nodes was {x} directly linked to?"])
def t1_neighbors_at(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t = ctx.t0 + rng.randrange(ctx.span)
    question = q.format(x=x, t=fmt_t(t))
    steps = [
        {"id": "s1", "op": "snapshot_subgraph",
         "args": {"seeds": [x], "hops": 1, "t_valid": t, "limit": 10000},
         "depends_on": []},
        {"id": "s2", "op": "compute",
         "args": {"fn": "filter", "input": {"$ref": "s1.nodes"},
                  "field": "hop", "cmp": "eq", "value": 1, "limit": 10000},
         "depends_on": ["s1"]},
    ]
    return question, _p("t1-neighbors-at", steps, "entity_set", "s2.rows",
                        question), [x]


@_register_t1("degree_at", "easy", [
    "How many active edges were incident to {x} at {t}?",
    "What was {x}'s degree (incident active edges) at {t}?",
    "At {t}, count the edges touching {x}."])
def t1_degree_at(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t = ctx.t0 + rng.randrange(max(1, ctx.span - 2))
    question = q.format(x=x, t=fmt_t(t))
    steps = [
        {"id": "s1", "op": "neighborhood_evolution",
         "args": {"uid": x, "t1": t, "t2": t + 2, "stride": 2}, "depends_on": []},
    ]
    return question, _p("t1-degree-at", steps, "value",
                        "s1.degree_series[0].degree", question), [x]


@_register_t1("event_count", "easy", [
    "How many edge events occurred {w}?",
    "Count all events (edge occurrences) {w}.",
    "What is the total number of interactions recorded {w}?"])
def t1_event_count(rng, ctx, q):
    t_a, t_b = _window(rng, ctx)
    question = q.format(w=fmt_w(t_a, t_b))
    steps = [
        {"id": "s1", "op": "graph_metric_timeseries",
         "args": {"metric": "edge_event_count",
                  "window": {"t_a": t_a, "t_b": t_b}, "stride": t_b - t_a},
         "depends_on": []},
    ]
    return question, _p("t1-event-count", steps, "value", "s1.rows[0].value",
                        question), []


@_register_t1("busiest_bucket", "medium", [
    "Which interval of length {stride_days} days had the most edge events {w}?",
    "Split {w} into {stride_days}-day buckets: which bucket saw peak activity?",
    "Find the {stride_days}-day period with the highest event count {w}."])
def t1_busiest_bucket(rng, ctx, q):
    t_a, t_b = _window(rng, ctx, min_frac=0.2, max_frac=0.9)
    stride = max(MICROS_PER_DAY, _stride_for(t_a, t_b, 14))
    question = q.format(w=fmt_w(t_a, t_b),
                        stride_days=round(stride / MICROS_PER_DAY, 1))
    steps = [
        {"id": "s1", "op": "graph_metric_timeseries",
         "args": {"metric": "edge_event_count",
                  "window": {"t_a": t_a, "t_b": t_b}, "stride": stride,
                  "limit": 2000},
         "depends_on": []},
        {"id": "s2", "op": "compute",
         "args": {"fn": "topk", "input": {"$ref": "s1.rows"},
                  "field": "value", "k": 1},
         "depends_on": ["s1"]},
    ]
    return question, _p("t1-busiest", steps, "interval", "s2.rows[0]",
                        question), []


@_register_t1("new_nodes", "easy", [
    "How many nodes made their first-ever appearance {w}?",
    "Count the nodes whose first activity falls {w}.",
    "How many newcomers joined the graph {w}?"])
def t1_new_nodes(rng, ctx, q):
    t_a, t_b = _window(rng, ctx)
    question = q.format(w=fmt_w(t_a, t_b))
    steps = [
        {"id": "s1", "op": "graph_metric_timeseries",
         "args": {"metric": "new_node_rate",
                  "window": {"t_a": t_a, "t_b": t_b}, "stride": t_b - t_a},
         "depends_on": []},
    ]
    return question, _p("t1-new-nodes", steps, "value", "s1.rows[0].value",
                        question), []


@_register_t1("reciprocity", "medium", [
    "What fraction of directed communication pairs {w} was reciprocated?",
    "Among ordered (sender, receiver) pairs active {w}, what share also saw the reverse direction?",
    "Compute the reciprocity of interactions {w}."])
def t1_reciprocity(rng, ctx, q):
    t_a, t_b = _window(rng, ctx, min_frac=0.2)
    question = q.format(w=fmt_w(t_a, t_b))
    steps = [
        {"id": "s1", "op": "graph_metric_timeseries",
         "args": {"metric": "reciprocity",
                  "window": {"t_a": t_a, "t_b": t_b}, "stride": t_b - t_a},
         "depends_on": []},
    ]
    return question, _p("t1-reciprocity", steps, "value", "s1.rows[0].value",
                        question), []


@_register_t1("earliest_arrival_at", "medium", [
    "What is the earliest time {y} can be reached from {x} {w}?",
    "Starting at {x} {w}, when does a time-respecting path first arrive at {y}?",
    "Give the earliest arrival time at {y} for information leaving {x} {w}."])
def t1_earliest_arrival(rng, ctx, q):
    x, y = rng.sample(ctx.uids, 2)
    t_a, t_b = _window(rng, ctx, min_frac=0.3)
    question = q.format(x=x, y=y, w=fmt_w(t_a, t_b))
    steps = [
        {"id": "s1", "op": "temporal_reachability",
         "args": {"src": x, "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
         "depends_on": []},
        {"id": "s2", "op": "compute",
         "args": {"fn": "filter", "input": {"$ref": "s1.rows"},
                  "field": "uid", "cmp": "eq", "value": y},
         "depends_on": ["s1"]},
    ]
    return question, _p("t1-earliest", steps, "value",
                        "s2.rows[0].earliest_arrival", question), [x, y]


@_register_t1("burst_buckets", "medium", [
    "How many {stride_days}-day buckets {w} were flagged as activity bursts (z-score >= 3 vs the trailing 10 buckets)?",
    "Using a rolling z-score (threshold 3, window 10), count bursty {stride_days}-day buckets {w}.",
    "Count the {stride_days}-day periods {w} whose event rate was a >=3-sigma outlier vs the preceding ten periods."])
def t1_burst_buckets(rng, ctx, q):
    t_a, t_b = _window(rng, ctx, min_frac=0.3, max_frac=0.9)
    stride = max(MICROS_PER_DAY, _stride_for(t_a, t_b, 30))
    question = q.format(w=fmt_w(t_a, t_b),
                        stride_days=round(stride / MICROS_PER_DAY, 1))
    steps = [
        {"id": "s1", "op": "burst_detection",
         "args": {"target": {"kind": "edge_event_rate"},
                  "window": {"t_a": t_a, "t_b": t_b}, "stride": stride,
                  "limit": 2000},
         "depends_on": []},
        {"id": "s2", "op": "compute",
         "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
         "depends_on": ["s1"]},
    ]
    return question, _p("t1-bursts", steps, "count", "s2.value", question), []


@_register_t1("history_count", "easy", [
    "How many belief versions does node {x} currently have?",
    "Count the currently-believed versions of {x} in the store.",
    "Under current beliefs, how many versions make up {x}'s history?"])
def t1_history_count(rng, ctx, q):
    x = rng.choice(ctx.uids)
    question = q.format(x=x)
    steps = [
        {"id": "s1", "op": "entity_history", "args": {"uid": x, "limit": 10000},
         "depends_on": []},
        {"id": "s2", "op": "compute",
         "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
         "depends_on": ["s1"]},
    ]
    return question, _p("t1-history", steps, "count", "s2.value", question), [x]


# --------------------------------------------------------------------------- #
# T3 — evolution QA                                                            #
# --------------------------------------------------------------------------- #

T3_TEMPLATES: list[dict[str, Any]] = []


def _register_t3(name: str, difficulty: str, paraphrases: list[str]):
    def deco(fn: Callable):
        T3_TEMPLATES.append({"name": name, "difficulty": difficulty,
                             "build": fn, "paraphrases": paraphrases})
        return fn
    return deco


@_register_t3("nodes_added", "easy", [
    "How many nodes were present at {t2} but absent at {t1}?",
    "Between the snapshots at {t1} and {t2}, how many nodes were added?",
    "Count the nodes that exist at {t2} but did not exist at {t1}."])
def t3_nodes_added(rng, ctx, q):
    t_a, t_b = _window(rng, ctx, min_frac=0.2)
    question = q.format(t1=fmt_t(t_a), t2=fmt_t(t_b))
    steps = [
        {"id": "s1", "op": "diff_snapshots",
         "args": {"t1": t_a, "t2": t_b, "limit": 10000}, "depends_on": []},
    ]
    return question, _p("t3-added", steps, "value", "s1.nodes_added_total",
                        question), []


@_register_t3("neighbors_gained", "medium", [
    "Which neighbors did {x} gain between {t1} and {t2}?",
    "List the nodes that were connected to {x} at {t2} but not at {t1}.",
    "Whom did {x} become linked to between {t1} and {t2}?"])
def t3_neighbors_gained(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t_a, t_b = _window(rng, ctx, min_frac=0.2)
    question = q.format(x=x, t1=fmt_t(t_a), t2=fmt_t(t_b))
    steps = [
        {"id": "s1", "op": "neighborhood_evolution",
         "args": {"uid": x, "t1": t_a, "t2": t_b, "limit": 10000},
         "depends_on": []},
    ]
    return question, _p("t3-gained", steps, "entity_set",
                        "s1.neighbors_gained", question), [x]


@_register_t3("first_burst", "medium", [
    "In which {stride_days}-day period did the edge-event rate first burst {w}?",
    "Find the earliest {stride_days}-day bucket flagged as a burst {w}.",
    "When (which {stride_days}-day interval) did activity first spike {w}?"])
def t3_first_burst(rng, ctx, q):
    t_a, t_b = _window(rng, ctx, min_frac=0.4, max_frac=1.0)
    stride = max(MICROS_PER_DAY, _stride_for(t_a, t_b, 30))
    question = q.format(w=fmt_w(t_a, t_b),
                        stride_days=round(stride / MICROS_PER_DAY, 1))
    steps = [
        {"id": "s1", "op": "burst_detection",
         "args": {"target": {"kind": "edge_event_rate"},
                  "window": {"t_a": t_a, "t_b": t_b}, "stride": stride,
                  "limit": 2000},
         "depends_on": []},
    ]
    return question, _p("t3-burst", steps, "interval", "s1.rows[0]",
                        question), []


# --------------------------------------------------------------------------- #
# T4 — multi-step analytical (oracle plans with >= 3 dependent steps)          #
# --------------------------------------------------------------------------- #

T4_TEMPLATES: list[dict[str, Any]] = []


def _register_t4(name: str, paraphrases: list[str]):
    def deco(fn: Callable):
        T4_TEMPLATES.append({"name": name, "difficulty": "hard",
                             "build": fn, "paraphrases": paraphrases})
        return fn
    return deco


@_register_t4("reach_motifs", [
    "Among the nodes reachable from {x} {w}, how many cyclic temporal triangles completed within {delta_h} hours?",
    "Restrict to nodes {x} can reach {w}: count three-edge cycles (u->v->w->u, time-ordered) spanning at most {delta_h} hours.",
    "Count delta-temporal cyclic triangles (delta = {delta_h}h) {w} among accounts reachable from {x}."])
def t4_reach_motifs(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t_a, t_b = _window(rng, ctx, min_frac=0.3)
    delta = max(3_600_000_000, ctx.span // 20)
    question = q.format(x=x, w=fmt_w(t_a, t_b),
                        delta_h=round(delta / 3_600_000_000, 1))
    steps = [
        {"id": "s1", "op": "resolve_entities", "args": {"query": x},
         "depends_on": []},
        {"id": "s2", "op": "temporal_reachability",
         "args": {"src": {"$ref": "s1.rows[0].uid"},
                  "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
         "depends_on": ["s1"]},
        {"id": "s3", "op": "count_temporal_motifs",
         "args": {"motif": "M_triangle_cyclic", "delta": delta,
                  "window": {"t_a": t_a, "t_b": t_b},
                  "node_filter": {"$ref": "s2.rows[*].uid"}},
         "depends_on": ["s2"]},
    ]
    return question, _p("t4-reach-motifs", steps, "count", "s3.count",
                        question), [x]


@_register_t4("first_reached_history", [
    "Take the node first reached from {x} {w}: how many belief versions does it have?",
    "Find the earliest node reachable from {x} {w}, then count its currently-believed versions.",
    "For the first node {x} reaches {w}, report the size of its version history."])
def t4_first_reached_history(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t_a, t_b = _window(rng, ctx, min_frac=0.3)
    question = q.format(x=x, w=fmt_w(t_a, t_b))
    steps = [
        {"id": "s1", "op": "temporal_reachability",
         "args": {"src": x, "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
         "depends_on": []},
        {"id": "s2", "op": "entity_history",
         "args": {"uid": {"$ref": "s1.rows[0].uid"}, "limit": 10000},
         "depends_on": ["s1"]},
        {"id": "s3", "op": "compute",
         "args": {"fn": "count", "input": {"$ref": "s2.rows"}},
         "depends_on": ["s2"]},
    ]
    return question, _p("t4-first-history", steps, "count", "s3.value",
                        question), [x]


@_register_t4("motif_top_participant", [
    "Among nodes reachable from {x} {w}, find cyclic temporal triangles within {delta_h} hours and name the source of the first edge of the earliest one.",
    "Restrict to {x}'s reachable set {w}: of the time-ordered three-cycles spanning <= {delta_h}h, who initiates the earliest instance?",
    "Who fired the opening edge of the earliest cyclic triangle (delta = {delta_h}h) {w} among accounts reachable from {x}?"])
def t4_motif_participant(rng, ctx, q):
    x = rng.choice(ctx.uids)
    t_a, t_b = _window(rng, ctx, min_frac=0.3)
    delta = max(3_600_000_000, ctx.span // 10)
    question = q.format(x=x, w=fmt_w(t_a, t_b),
                        delta_h=round(delta / 3_600_000_000, 1))
    steps = [
        {"id": "s1", "op": "temporal_reachability",
         "args": {"src": x, "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
         "depends_on": []},
        {"id": "s2", "op": "find_temporal_motif_instances",
         "args": {"motif": "M_triangle_cyclic", "delta": delta,
                  "window": {"t_a": t_a, "t_b": t_b},
                  "node_filter": {"$ref": "s1.rows[*].uid"}},
         "depends_on": ["s1"]},
    ]
    return question, _p("t4-participant", steps, "value",
                        "s2.rows[0].edges[0].src", question), [x]


@_register_t4("coactive_count", [
    "How many ordered pairs of {x}-outgoing edge intervals were separated by a gap of at most {gap_h} hours (first ends, then the next starts)?",
    "Count before-related pairs (gap <= {gap_h}h) among the valid intervals of edges leaving {x}.",
    "Among {x}'s outgoing edges, count interval pairs where one ends and another starts within {gap_h} hours."])
def t4_coactive_count(rng, ctx, q):
    x = rng.choice(ctx.uids)
    gap = max(3_600_000_000, ctx.span // 20)
    question = q.format(x=x, gap_h=round(gap / 3_600_000_000, 1))
    steps = [
        {"id": "s1", "op": "resolve_entities", "args": {"query": x},
         "depends_on": []},
        {"id": "s2", "op": "co_active",
         "args": {"a_spec": {"src": {"$ref": "s1.rows[0].uid"}},
                  "b_spec": {"src": {"$ref": "s1.rows[0].uid"}},
                  "allen_relation": {"relation": "before", "gap": gap},
                  "limit": 10000},
         "depends_on": ["s1"]},
        {"id": "s3", "op": "compute",
         "args": {"fn": "count", "input": {"$ref": "s2.rows"}},
         "depends_on": ["s2"]},
    ]
    return question, _p("t4-coactive", steps, "count", "s3.value",
                        question), [x]


# --------------------------------------------------------------------------- #
# correction probes                                                            #
# --------------------------------------------------------------------------- #

PROBE_NOTE = "correction probe"


def _recover_corrections(store: Store) -> list[dict[str, Any]]:
    """Previously injected probe corrections, recovered from the event log —
    makes generate_suite idempotent: rerunning on the same store reuses them
    instead of mutating the store again."""
    records = []
    prev_tt = 0
    for batch in store.eventlog.batches():
        for op in batch["ops"]:
            if op["op"] == "correct" and \
                    op.get("props", {}).get("note") == PROBE_NOTE:
                records.append({"uid": op["ref"]["uid"], "tt_before": prev_tt,
                                "tt_after": batch["tt"], "vt_s": op["vt_s"],
                                "vt_e": op["vt_e"]})
        prev_tt = batch["tt"]
    return records


def inject_corrections(store: Store, ctx: Ctx, rng: random.Random,
                       n: int) -> list[dict[str, Any]]:
    """Apply n corrections to random active nodes; returns records with the
    tt boundary just before each correction (probes pin as_of_tt there).
    Idempotent: if probe corrections already exist in the event log, they are
    reused and the store is not written again."""
    existing = _recover_corrections(store)
    if existing:
        return existing
    records = []
    for uid in rng.sample(ctx.uids, min(n, len(ctx.uids))):
        vt_s = ctx.t0 + rng.randrange(max(1, ctx.span // 2))
        vt_e = vt_s + max(2, ctx.span // 10)
        tt_before = store.clock.last_tt
        tt = store.correct(EntityRef(kind="node", uid=uid),
                           {"revised": True, "note": PROBE_NOTE},
                           vt_s=vt_s, vt_e=vt_e)
        records.append({"uid": uid, "tt_before": tt_before, "tt_after": tt,
                        "vt_s": vt_s, "vt_e": vt_e})
    return records


PROBE_PARAPHRASES = {
    "before": [
        "As of transaction time {tt}, how many belief versions of {x} existed?",
        "Pin the belief state to transaction time {tt}: how many versions of {x} were believed then?",
        "Before any later revisions — at transaction time {tt} — how many versions did {x}'s history contain?"],
    "now": [
        "Under current beliefs, how many versions of {x} exist?",
        "How many belief versions does {x} have now (latest transaction state)?",
        "Count {x}'s versions in the present belief state."],
}


def gen_probes(store: Store, ctx: Ctx, records: list[dict[str, Any]],
               rng: random.Random) -> list[Task]:
    tasks = []
    for i, rec in enumerate(records):
        for mode in ("before", "now"):
            as_of = rec["tt_before"] if mode == "before" else None
            q = PROBE_PARAPHRASES[mode][i % 3].format(
                x=rec["uid"], tt=rec.get("tt_before"))
            args: dict[str, Any] = {"uid": rec["uid"], "limit": 10000}
            if as_of is not None:
                args["as_of_tt"] = as_of
            plan = _p(f"probe-{mode}-{i}", [
                {"id": "s1", "op": "entity_history", "args": args,
                 "depends_on": []},
                {"id": "s2", "op": "compute",
                 "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
                 "depends_on": ["s1"]},
            ], "count", "s2.value", q)
            g = compute_gold(store, plan, [rec["uid"]])
            if g is None:
                continue
            gold, gold_obj = g
            tasks.append(Task(
                id=f"{ctx.dataset}-probe-{mode}-{i:03d}", family="probe",
                dataset=ctx.dataset, question_text=q, oracle_plan=plan,
                answer_kind="count", gold=gold, gold_answer_object=gold_obj,
                input_uids=[rec["uid"]], difficulty="hard"))
    return tasks


# --------------------------------------------------------------------------- #
# T2 — planted-pattern mining (synthetic only; gold from the manifest)         #
# --------------------------------------------------------------------------- #

T2_PARAPHRASES = [
    "Which accounts took part in a cyclic temporal flow (three-edge cycle completing within {delta_h} hours) {w}?",
    "Find every node involved in a time-ordered three-cycle spanning at most {delta_h} hours {w}.",
    "Name the accounts forming closed triangles in time order (span <= {delta_h}h) {w}."]


def gen_t2(ctx: Ctx, manifest: dict[str, Any]) -> list[Task]:
    tasks = []
    rings = [p for p in manifest.get("planted", [])
             if p["kind"] == "triangle_ring"]
    for i, ring in enumerate(rings):
        delta = ring["delta"]
        t_a = min(ring["times"]) - delta
        t_b = max(ring["times"]) + delta
        q = T2_PARAPHRASES[i % 3].format(
            w=fmt_w(t_a, t_b), delta_h=round(delta / 3_600_000_000, 1))
        # gold: every planted ring fully inside the asked window
        gold = sorted({n for r in rings
                       if min(r["times"]) >= t_a and max(r["times"]) < t_b
                       and max(r["times"]) - min(r["times"]) <= delta
                       for n in r["nodes"]})
        plan = _p(f"t2-rings-{i}", [
            {"id": "s1", "op": "find_temporal_motif_instances",
             "args": {"motif": "M_triangle_cyclic", "delta": delta,
                      "window": {"t_a": t_a, "t_b": t_b}, "limit": 10000},
             "depends_on": []},
        ], "entity_set", "s1.rows", q)
        tasks.append(Task(
            id=f"{ctx.dataset}-t2-{i:03d}", family="t2", dataset=ctx.dataset,
            question_text=q, oracle_plan=plan, answer_kind="entity_set",
            gold=gold,
            gold_answer_object={"text": f"Accounts: {', '.join(gold)}.",
                                "claims": [{"id": "c1", "type": "entity",
                                            "uids": gold, "evidence": ["s1"]}]},
            input_uids=[], difficulty="medium", gold_source="manifest"))
    return tasks


# --------------------------------------------------------------------------- #
# suite assembly                                                               #
# --------------------------------------------------------------------------- #

def _gen_family(store: Store, ctx: Ctx, rng: random.Random,
                templates: list[dict], family: str, n: int,
                max_tries: int = 6) -> list[Task]:
    tasks: list[Task] = []
    i = 0
    while len(tasks) < n and i < n * max_tries:
        tpl = templates[i % len(templates)]
        para = tpl["paraphrases"][(i // len(templates)) % 3]
        i += 1
        built = tpl["build"](rng, ctx, para)
        if built is None:
            continue
        question, plan, input_uids = built
        try:
            g = compute_gold(store, plan, input_uids)
        except ValueError:
            raise  # invalid oracle plan is a generator bug — fail loudly
        if g is None:
            continue
        gold, gold_obj = g
        if plan["answer_spec"]["kind"] == "entity_set" and not gold:
            continue  # skip trivial empty-set tasks
        tasks.append(Task(
            id=f"{ctx.dataset}-{family}-{tpl['name']}-{len(tasks):03d}",
            family=family, dataset=ctx.dataset, question_text=question,
            oracle_plan=plan, answer_kind=plan["answer_spec"]["kind"],
            gold=gold, gold_answer_object=gold_obj, input_uids=input_uids,
            difficulty=tpl["difficulty"]))
    return tasks


def generate_suite(store: Store, dataset: str, seed: int = 0,
                   sizes: dict[str, int] | None = None,
                   manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full pipeline: inject corrections -> generate all families with
    engine-executed gold -> 20/80 dev/test split -> hash the frozen test
    split. Deterministic given (store content, dataset, seed, sizes)."""
    sizes = sizes or {"t1": 60, "t3": 24, "t4": 16, "probes": 8}
    rng = random.Random(seed)
    ctx = build_context(store, dataset, rng)

    # corrections first: they become part of the belief history all gold
    # answers are computed under
    records = inject_corrections(store, ctx, rng, sizes.get("probes", 8))
    ctx = build_context(store, dataset, random.Random(seed))  # extent may grow

    tasks: list[Task] = []
    tasks += _gen_family(store, ctx, rng, T1_TEMPLATES, "t1", sizes["t1"])
    tasks += _gen_family(store, ctx, rng, T3_TEMPLATES, "t3", sizes["t3"])
    tasks += _gen_family(store, ctx, rng, T4_TEMPLATES, "t4", sizes["t4"])
    tasks += gen_probes(store, ctx, records, rng)
    if manifest is not None:
        tasks += gen_t2(ctx, manifest)

    # deterministic 20/80 split, stratified by family
    dev, test = [], []
    by_family: dict[str, list[Task]] = {}
    for t in tasks:
        by_family.setdefault(t.family, []).append(t)
    split_rng = random.Random(seed ^ 0xDEADBEEF)
    for fam in sorted(by_family):
        fam_tasks = by_family[fam]
        split_rng.shuffle(fam_tasks)
        cut = max(1, len(fam_tasks) // 5)
        dev.extend(fam_tasks[:cut])
        test.extend(fam_tasks[cut:])
    dev.sort(key=lambda t: t.id)
    test.sort(key=lambda t: t.id)

    test_json = [t.to_json() for t in test]
    return {
        "dataset": dataset,
        "seed": seed,
        "n_dev": len(dev),
        "n_test": len(test),
        "dev": [t.to_json() for t in dev],
        "test": test_json,
        "test_split_sha": sha256_hex(canonical_json(test_json)),
    }
