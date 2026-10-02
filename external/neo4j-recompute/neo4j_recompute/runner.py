"""Per-cell recompute protocol (design memo §2.5): load pristine dump ->
start -> indexes online -> epoch-0 pass (untimed) -> for k = 1..N: apply
burst (`apply_ms`) -> every registered artifact's query, serially, one
session, auto-commit read transactions, results fully consumed
(`recompute_ms`) -> untimed version-table check + digesting.

Timed quantity for predictions (a)/(b)/(c) is `recompute_ms` alone
(TGMS's global-recompute likewise excludes its write) — `apply_ms` and the
sum are reported alongside, never substituted for it. Ceiling:
`db.transaction.timeout=600s` per query; a timed-out artifact is recorded
`not_answered`, contributes 600s to `recompute_ms`, and is excluded from
the agreement count (§2.5, §2.6).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canon import digest
from .corrections import apply_burst
from .format import FORMATTERS
from .queries import (
    FAMILY_TEXT,
    NotExpressibleError,
    f1_params,
    f2_params,
    f3_params,
    f4_params,
    f5_params,
    f6_params,
    f7_params,
    f8_params,
    f9_params,
    f10_params,
    f11_window_params,
    f12_params,
    f12_query,
    f13_params,
)
from .reachability import run_reachability

#: Families with a single, directly-dispatchable (args, limit) -> params
#: builder. `temporal_reachability` (iterated), `temporal_paths` (its own
#: `k`, not `limit`) and `compute` (no limit at all) are special-cased in
#: `ArtifactRunner.recompute_one` instead.
PARAM_BUILDERS: dict[str, Callable[[dict, int], Any]] = {
    "entity_history": f1_params, "version_history": f2_params,
    "snapshot_subgraph": f3_params, "diff_snapshots": f4_params,
    "neighborhood_evolution": f5_params, "aggregate_events": f6_params,
    "graph_metric_timeseries": f7_params, "burst_detection": f8_params,
    "count_temporal_motifs": f9_params, "find_temporal_motif_instances": f10_params,
}

#: `db.transaction.timeout` (memo §2.5/§2.7); a `ClientError`/timeout from
#: the driver for a single artifact's query is caught and recorded as
#: `not_answered`, contributing this many ms to `recompute_ms`.
TX_TIMEOUT_S = 600


@dataclass
class ArtifactSpec:
    name: str
    op: str
    args: dict[str, Any]


@dataclass
class BurstRow:
    epoch: int
    apply_ms: float
    recompute_ms: float
    agree: int
    disagree: list[str] = field(default_factory=list)
    not_answered: list[str] = field(default_factory=list)
    oracle_refused: list[str] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch, "apply_ms": self.apply_ms,
            "recompute_ms": self.recompute_ms, "wall_ms": self.apply_ms + self.recompute_ms,
            "agree": self.agree, "disagree": self.disagree,
            "not_answered": self.not_answered, "oracle_refused": self.oracle_refused,
        }


def load_artifacts(artifacts_path: Path) -> list[ArtifactSpec]:
    specs = []
    with artifacts_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            specs.append(ArtifactSpec(row["name"], row["op"], row["args"]))
    return specs


def load_oracle(oracle_path: Path) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    with oracle_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[row["epoch"]] = {k: v for k, v in row.items() if k != "epoch"}
    return out


def load_deltas(deltas_path: Path) -> list[dict]:
    if not deltas_path.exists():
        return []
    rows = []
    with deltas_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("epoch", r.get("tt", 0)))
    return rows


class ArtifactRunner:
    """Wraps one Bolt `session` (auto-commit read transactions) to recompute
    every registered artifact once, in registration order, serially."""

    def __init__(self, session: Any, limit_default: int = 100) -> None:
        self.session = session
        self.limit_default = limit_default

    def _run_cypher(self, cypher: str, params: dict) -> list[dict]:
        result = self.session.run(cypher, params, timeout=TX_TIMEOUT_S)
        return [dict(r) for r in result]

    def recompute_one(self, spec: ArtifactSpec) -> tuple[dict | None, str | None]:
        """Returns `(payload, error)`; `error` is `"not_expressible"`,
        `"not_answered"`, or `None` on success."""
        try:
            if spec.op == "temporal_reachability":
                p = f11_window_params(spec.args)
                rows = run_reachability(self._run_cypher, spec.args["src"],
                                        p["t_a"], p["t_b"])
                limit = spec.args.get("limit", self.limit_default)
                payload = FORMATTERS["temporal_reachability"](rows, limit)
                return payload, None
            if spec.op == "temporal_paths":
                params, max_hops = f12_params(spec.args)
                cypher = FAMILY_TEXT["temporal_paths"] if max_hops == 4 else f12_query(max_hops)
                records = self._run_cypher(cypher, params)
                payload = FORMATTERS["temporal_paths"](records[0], params["k"])
                return payload, None
            if spec.op == "compute":
                params = f13_params(spec.args)
                records = self._run_cypher(FAMILY_TEXT["compute"], params)
                payload = FORMATTERS["compute"](records[0])
                return payload, None
            builder = PARAM_BUILDERS[spec.op]
            limit = spec.args.get("limit", self.limit_default)
            params = builder(spec.args, limit)
            records = self._run_cypher(FAMILY_TEXT[spec.op], params)
            payload = FORMATTERS[spec.op](records[0], limit)
            return payload, None
        except NotExpressibleError:
            return None, "not_expressible"
        except Exception as e:
            if _looks_like_timeout(e):
                return None, "not_answered"
            raise

    def recompute_all(self, specs: list[ArtifactSpec]
                      ) -> tuple[float, dict[str, dict | None], dict[str, str]]:
        """One pass over every artifact, serially, in one session. Returns
        (`recompute_ms`, payloads-by-name, errors-by-name)."""
        t0 = time.perf_counter()
        payloads: dict[str, dict | None] = {}
        errors: dict[str, str] = {}
        for spec in specs:
            payload, err = self.recompute_one(spec)
            if err is not None:
                errors[spec.name] = err
            else:
                payloads[spec.name] = payload
        recompute_ms = (time.perf_counter() - t0) * 1000.0
        return recompute_ms, payloads, errors


def _looks_like_timeout(e: Exception) -> bool:
    msg = str(e).lower()
    return "timeout" in msg or "timed out" in msg


def digest_payloads(payloads: dict[str, dict]) -> dict[str, str]:
    return {name: digest(payload) for name, payload in payloads.items()}


def score_against_oracle(digests: dict[str, str], errors: dict[str, str],
                         oracle_row: dict[str, str]) -> BurstRow:
    agree = 0
    disagree: list[str] = []
    not_answered: list[str] = []
    oracle_refused: list[str] = []
    for name, want in oracle_row.items():
        if want == "refused":
            oracle_refused.append(name)
            continue
        if name in errors:
            if errors[name] == "not_answered":
                not_answered.append(name)
            continue  # not_expressible: scoped out, not scored either way
        got = digests.get(name)
        if got == want:
            agree += 1
        else:
            disagree.append(name)
    return BurstRow(epoch=-1, apply_ms=0.0, recompute_ms=0.0, agree=agree,
                    disagree=disagree, not_answered=not_answered,
                    oracle_refused=oracle_refused, digests=digests)


def run_cell(session_factory: Callable[[], Any], cell_dir: Path, max_epochs: int | None,
            on_burst: Callable[[BurstRow], None] | None = None) -> list[BurstRow]:
    """Drive the full per-cell protocol. `session_factory()` returns a fresh
    Bolt session (so a burst's write transaction and the recompute pass's
    read session can be separate, matching "auto-commit read transactions"
    §2.5). `max_epochs=None` runs every delta in `deltas.jsonl`.
    """
    specs = load_artifacts(cell_dir / "artifacts.jsonl")
    oracle = load_oracle(cell_dir / "oracle.jsonl")
    deltas = load_deltas(cell_dir / "deltas.jsonl")
    if max_epochs is not None:
        deltas = deltas[:max_epochs]

    rows: list[BurstRow] = []

    # epoch 0: untimed correctness pass, warms plan cache/JIT/page cache.
    s0 = session_factory()
    try:
        runner0 = ArtifactRunner(s0)
        _, payloads0, errors0 = runner0.recompute_all(specs)
    finally:
        s0.close()
    digests0 = digest_payloads(payloads0)
    row0 = score_against_oracle(digests0, errors0, oracle.get(0, {}))
    row0.epoch = 0
    rows.append(row0)
    if on_burst:
        on_burst(row0)

    for i, delta in enumerate(deltas, start=1):
        epoch = delta.get("epoch", i)
        s_write = session_factory()
        try:
            t0 = time.perf_counter()
            apply_burst(lambda cy, pa, _s=s_write: _s.run(cy, pa).consume(), delta)
            apply_ms = (time.perf_counter() - t0) * 1000.0
        finally:
            s_write.close()

        s_read = session_factory()
        try:
            runner = ArtifactRunner(s_read)
            recompute_ms, payloads, errors = runner.recompute_all(specs)
        finally:
            s_read.close()

        digests = digest_payloads(payloads)
        row = score_against_oracle(digests, errors, oracle.get(epoch, {}))
        row.epoch = epoch
        row.apply_ms = apply_ms
        row.recompute_ms = recompute_ms
        rows.append(row)
        if on_burst:
            on_burst(row)

    return rows
