#!/usr/bin/env python3
"""Regenerate `tests/fixtures/upgrade_from_0_8_0/` with the released tgms 0.8.0.

Run it with the interpreter of a clean virtual environment that holds the
released wheel and nothing from this tree::

    python -m venv /tmp/tgms080
    /tmp/tgms080/bin/pip install "tgms[duckdb]==0.8.0" duckdb==1.5.4
    cd /tmp && /tmp/tgms080/bin/python <repo>/tests/fixtures/upgrade_from_0_8_0/generate.py \\
        <repo>/tests/fixtures/upgrade_from_0_8_0

Run it from a directory that is not a git checkout: `tgms store backup` records
the `git rev-parse HEAD` of its working directory in `backup_manifest.json`.

It refuses to run under any version other than 0.8.0. Everything it writes is
deterministic, so a rerun reproduces the committed files byte for byte: the
data come from a fixed seed, and transaction times come from the store's own
public `clock` attribute, replaced by a `HybridLogicalClock` whose `now_fn`
is a constant (each batch then gets the previous batch's tt plus one).

What it does, using only entry points present in 0.8.0 (`tgms.open`,
`Store.ingest_events/assert_edge/assert_node/correct/retract`,
`ToolRouter.call/leaf_meta`, and the `tgms` CLI):

1. builds the same history twice, once per backend (native, duckdb): twelve
   batches -- node and event loads in several batches, two structural edges,
   a node, then corrections and a retraction that change what was believed
   about earlier valid times;
2. compacts the native store with `tgms store compact`;
3. registers five artifacts with `tgms artifact register` from real operator
   envelopes, and records `tgms artifact check --json` for each (state 1);
4. writes one more correction, which overlaps some artifacts and not others,
   and records the verdicts again (state 2);
5. takes `tgms store backup` of the native store;
6. records the store identity, logical digest, every artifact's
   `record_digest`, and the digest of each answer of a fixed call list that
   covers all fifteen 0.8.0 operators (some with `as_of_tt`), per backend.

The fixture keeps the native store (event log, artifact registry, operator
blobs, derived store), the backup, the DuckDB store's artifact registry, and
`expected.json`. The DuckDB database file (about 3 MB) is not kept.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SEED = 20261113
SEC = 1_000_000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
T0 = 1_600_000_000_000_000          # valid-time origin
TT_BASE = 1_767_225_600_000_000     # transaction-time origin (2026-01-01T00:00:00Z)
TGMS = str(Path(sys.executable).parent / "tgms")


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), default=str)


def scrub(o):
    """Drop `semantic_identity` ("tgms/<package version>"): a version stamp, which
    differs between any two versions by construction."""
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k != "semantic_identity"}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    return o


def answer_digest(env) -> str:
    return hashlib.sha256(canon(scrub(env)).encode()).hexdigest()


def run(*args: str, cwd: str | None = None) -> tuple[int, str]:
    p = subprocess.run([TGMS, *args], capture_output=True, text=True, cwd=cwd)
    if p.returncode not in (0, 1):
        raise SystemExit(f"tgms {' '.join(args)} failed ({p.returncode}): {p.stderr}")
    return p.returncode, p.stdout


def make_data():
    rng = random.Random(SEED)
    nodes = [{"uid": f"n{i:02d}", "label": "Person" if i % 2 == 0 else "Account",
              "props": {"name": f"node-{i:02d}", "score": rng.randint(0, 100)},
              "vt_s": T0 - DAY} for i in range(24)]
    uids = [n["uid"] for n in nodes]

    def events(k: int, n: int) -> list[dict]:
        out = []
        for i in range(n):
            src, dst = rng.sample(uids, 2)
            out.append({"src": src, "dst": dst, "rel_type": rng.choice(["PAYS", "MSG"]),
                        "vt_s": T0 + (k * 60 + i) * MIN + rng.randint(0, 30) * SEC,
                        "props": {"amount": rng.randint(1, 500)}, "disc": f"b{k}-{i}"})
        return out

    e1, e2, e3, e4 = events(0, 50), events(1, 50), events(2, 50), events(3, 30)
    m = T0 + 10 * MIN  # planted motifs
    e1 += [
        {"src": "n01", "dst": "n02", "rel_type": "MSG", "vt_s": m, "disc": "pp-1"},
        {"src": "n02", "dst": "n01", "rel_type": "MSG", "vt_s": m + 10 * SEC, "disc": "pp-2"},
        {"src": "n03", "dst": "n04", "rel_type": "PAYS", "vt_s": m + 20 * SEC, "disc": "tri-1"},
        {"src": "n04", "dst": "n05", "rel_type": "PAYS", "vt_s": m + 30 * SEC, "disc": "tri-2"},
        {"src": "n05", "dst": "n03", "rel_type": "PAYS", "vt_s": m + 40 * SEC, "disc": "tri-3"},
    ]
    return nodes, e1, e2, e3, e4


def log_tts(store_dir: Path) -> list[int]:
    with open(store_dir / "eventlog.jsonl") as f:
        return [r["tt"] for r in map(json.loads, f) if "tt" in r]


def call_plan(tts: list[int]) -> list[dict]:
    """The fixed call list; `as_of_tt` values name batches of this history."""
    pre = tts[6]   # after every load batch, before the first correction
    mid = tts[-3]  # between the corrections
    w = {"t_a": T0, "t_b": T0 + 5 * HOUR}
    we = {"t_a": T0, "t_b": T0 + 30 * MIN}
    calls = [
        ("entity_history", {"uid": "n03"}),
        ("entity_history", {"uid": "n03", "as_of_tt": pre}),
        ("entity_history", {"uid": "n07", "include_edges": True}),
        ("snapshot_subgraph", {"seeds": ["n00"], "hops": 2, "t_valid": T0 + 2 * HOUR}),
        ("snapshot_subgraph", {"seeds": ["n07"], "hops": 1, "t_valid": T0 + 6 * HOUR,
                               "as_of_tt": pre}),
        ("diff_snapshots", {"t1": T0 + 30 * MIN, "t2": T0 + 3 * HOUR}),
        ("diff_snapshots", {"t1": T0 + 30 * MIN, "t2": T0 + 3 * HOUR, "as_of_tt": pre}),
        ("neighborhood_evolution", {"uid": "n05", "t1": T0, "t2": T0 + 4 * HOUR,
                                    "stride": HOUR}),
        ("resolve_entities", {"query": "node-0"}),
        ("resolve_entities", {"query": "node", "label": "Person", "as_of_tt": pre}),
        ("version_history", {"kind": "node", "window": w, "belief": "superseded"}),
        ("version_history", {"kind": "edge", "window": we, "belief": "all",
                             "rel_types": ["KNOWS", "MSG"]}),
        ("version_history", {"kind": "node", "window": w, "belief": "current",
                             "as_of_tt": mid}),
        ("temporal_reachability", {"src": "n00", "window": w}),
        ("temporal_reachability", {"src": "n03", "window": w, "direction": "in",
                                   "delta_max_wait": 30 * MIN, "as_of_tt": pre}),
        ("temporal_paths", {"src": "n00", "dst": "n07", "window": w, "k": 3, "max_hops": 3}),
        ("count_temporal_motifs", {"motif": "M_2node_pingpong", "delta": 5 * MIN, "window": w}),
        ("count_temporal_motifs", {"motif": "M_triangle_cyclic", "delta": 5 * MIN,
                                   "window": w, "as_of_tt": pre}),
        ("find_temporal_motif_instances", {"motif": "M_path_3", "delta": 10 * MIN,
                                           "window": we}),
        ("graph_metric_timeseries", {"metric": "edge_event_count", "window": w,
                                     "stride": 30 * MIN}),
        ("graph_metric_timeseries", {"metric": "node_count", "window": w, "stride": HOUR,
                                     "as_of_tt": pre}),
        ("burst_detection", {"target": {"kind": "edge_event_rate"}, "window": w,
                             "stride": 10 * MIN, "params": {"w": 3, "z": 1.0}}),
        ("co_active", {"a_spec": {"rel_type": "KNOWS"}, "b_spec": {"rel_type": "PAYS"},
                       "allen_relation": {"relation": "during"}, "limit": 50}),
        ("aggregate_events", {"group_by": [{"dim": "rel_type"}],
                              "aggregates": [{"agg": "count"},
                                             {"agg": "count_distinct", "of": "src"}],
                              "window": w}),
        ("aggregate_events", {"group_by": [{"dim": "endpoint", "role": "src"}],
                              "aggregates": [{"agg": "max", "of": "prop", "prop": "amount"}],
                              "window": w, "as_of_tt": pre}),
        ("compute", {"fn": "sum", "input": [3, 4, 5]}),
        ("compute", {"fn": "topk", "input": [{"u": "a", "x": 1}, {"u": "b", "x": 7},
                                            {"u": "c", "x": 4}], "field": "x", "k": 2}),
    ]
    return [{"id": f"c{i:02d}-{op}", "op": op, "args": args}
            for i, (op, args) in enumerate(calls)]


ARTIFACTS = [
    ("ah_n03", "entity_history", {"uid": "n03"}),
    ("ah_n17", "entity_history", {"uid": "n17"}),
    ("snap_n10", "snapshot_subgraph", {"seeds": ["n10"], "hops": 1, "t_valid": T0 + 2 * HOUR}),
    ("vh_edges_early", "version_history",
     {"kind": "edge", "window": {"t_a": T0, "t_b": T0 + 20 * MIN}}),
    ("pinned_n03", "entity_history", {"uid": "n03"}),  # + as_of_tt: a pinned read
]


def open_fixed(path: Path, backend: str):
    import tgms
    from tgms.core.clock import HybridLogicalClock
    s = tgms.open(path, backend=backend)
    s.clock = HybridLogicalClock(last_tt=max(s.clock.last_tt, TT_BASE - 1),
                                 now_fn=lambda: TT_BASE)
    return s


def checks(store_dir: Path) -> dict:
    out = {}
    for n, _, _ in ARTIFACTS:
        rc, text = run("artifact", "check", str(store_dir), "--name", n, "--json")
        j = json.loads(text)
        out[n] = {"rc": rc, "verdict": j["verdict"], "digest": answer_digest(j)}
    return out


def build(store_dir: Path, backend: str) -> dict:
    from tgms.core.model import EntityRef
    from tgms.tools.server import ToolRouter
    import tgms

    nodes, e1, e2, e3, e4 = make_data()
    s = open_fixed(store_dir, backend)
    s.ingest_events(e1, nodes=nodes)
    s.ingest_events(e2)
    s.assert_edge("n00", "n07", "KNOWS", {"since": 2019}, vt_s=T0, vt_e=T0 + 10 * HOUR)
    s.assert_edge("n07", "n11", "KNOWS", {"since": 2020}, vt_s=T0 + HOUR, vt_e=T0 + 12 * HOUR)
    s.assert_node("n24", "Person", {"name": "node-24", "score": 5}, vt_s=T0)
    s.ingest_events(e3)
    s.close()
    if backend == "native":
        run("store", "compact", "--store", str(store_dir))
    s = open_fixed(store_dir, backend)
    s.correct(EntityRef(kind="node", uid="n03"), {"name": "node-03", "score": 999},
              vt_s=T0 + HOUR, vt_e=T0 + 2 * HOUR)
    s.correct(EntityRef(kind="edge", src="n00", dst="n07", rel_type="KNOWS"), {"since": 2018},
              vt_s=T0, vt_e=T0 + 3 * HOUR)
    s.retract(EntityRef(kind="edge", src="n07", dst="n11", rel_type="KNOWS"), T0 + 5 * HOUR)
    s.ingest_events(e4)
    s.close()

    tts = log_tts(store_dir)
    (store_dir / "ops").mkdir()
    s = tgms.open(store_dir, backend=backend, read_only=True)
    router = ToolRouter(s.adapter, tt_source=s)
    work = Path(tempfile.mkdtemp())
    for name, op, args in ARTIFACTS:
        args = dict(args)
        if name == "pinned_n03":
            args["as_of_tt"] = tts[6]
        env = router.call(op, args, request_id=f"register-{name}")
        meta = router.leaf_meta(op, env)
        ref = f"ops/{name}.json"
        (store_dir / ref).write_text(canon({"op": op, "args": args}))
        doc = {
            "name": name, "kind": "query_result",
            "plan": {"plan_digest": meta.get("plan_digest"),
                     "node_digest": meta.get("node_digest"), "plan_format": None},
            "basis": {"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
                      "tt_q_verified": env["dependency"].get("tt_q_verified", True)},
            "state": {"completeness": meta.get("completeness", "unknown"),
                      "exactness": meta.get("exactness", "exact"), "refusal": None},
            "refresh": {"kind": "operator", "ref": ref, "basis_policy": "open"},
            "dependency": env["dependency"],
        }
        (work / f"{name}.json").write_text(json.dumps(doc))
        run("artifact", "register", str(store_dir), "--record-json", str(work / f"{name}.json"))
    s.close()
    state1_bytes = (store_dir / "eventlog.jsonl").stat().st_size
    state1 = checks(store_dir)

    s = open_fixed(store_dir, backend)
    s.correct(EntityRef(kind="node", uid="n03"), {"name": "node-03", "score": 1234},
              vt_s=T0 + 3 * HOUR, vt_e=T0 + 4 * HOUR)
    s.close()
    state2 = checks(store_dir)

    plan = call_plan(log_tts(store_dir))
    s = tgms.open(store_dir, backend=backend, read_only=True)
    router = ToolRouter(s.adapter, tt_source=s)
    answers = {}
    sem = set()
    for c in plan:
        env = router.call(c["op"], c["args"], request_id=c["id"])
        assert "error" not in env, (c, env)
        answers[c["id"]] = answer_digest(env)
        sem.add(env["tgir"]["provenance"]["semantic_identity"])
    identity, logical = s.store_identity, s.digest()
    s.close()
    lines = (store_dir / "artifacts.jsonl").read_text().splitlines()
    registry = [json.loads(line) for line in lines if line.strip()]
    return {"plan": plan, "answers": answers, "semantic_identity": sorted(sem),
            "store_identity": identity, "logical_digest": logical,
            "state1_log_bytes": state1_bytes, "freshness": {"state1": state1, "state2": state2},
            "artifacts": [[r["name"], r["generation"], r["record_digest"]]
                          for r in registry if "name" in r]}


def main(out: Path) -> None:
    import tgms
    import tgms._engine as engine
    import duckdb

    if tgms.__version__ != "0.8.0":
        raise SystemExit(f"run this with the released tgms 0.8.0, not {tgms.__version__}")
    tmp = Path(tempfile.mkdtemp())
    res = {b: build(tmp / b, b) for b in ("native", "duckdb")}
    nat, duck = res["native"], res["duckdb"]
    assert nat["plan"] == duck["plan"]
    assert nat["store_identity"] == duck["store_identity"]
    native = tmp / "native"
    backup = tmp / "backup"
    run("store", "backup", "--store", str(native), "--dest", str(backup), cwd=str(tmp))
    rcf, _ = run("store", "verify", "--store", str(native), "--json")
    rcF, _ = run("store", "verify", "--store", str(native), "--full", "--json")

    if out.exists():
        for sub in ("store", "backup"):
            shutil.rmtree(out / sub, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(native, out / "store",
                    ignore=shutil.ignore_patterns("*.lock"))
    shutil.copytree(backup, out / "backup")
    shutil.copyfile(tmp / "duckdb" / "artifacts.jsonl", out / "duckdb_artifacts.jsonl")
    log = (native / "eventlog.jsonl").read_bytes()
    assert log == (tmp / "duckdb" / "eventlog.jsonl").read_bytes()
    expected = {
        "produced_by": {"tgms": tgms.__version__, "engine": engine.build_info(),
                        "duckdb": duckdb.__version__, "python": sys.version.split()[0],
                        "generator": "tests/fixtures/upgrade_from_0_8_0/generate.py"},
        "store_identity": nat["store_identity"],
        "logical_digest": {"native": nat["logical_digest"], "duckdb": duck["logical_digest"]},
        "eventlog_sha256": hashlib.sha256(log).hexdigest(),
        "state1_log_bytes": nat["state1_log_bytes"],
        "semantic_identity": nat["semantic_identity"],
        "verify_rc": {"fast": rcf, "full": rcF},
        "backup_manifest": json.loads((backup / "backup_manifest.json").read_text()),
        "artifacts": {"native": nat["artifacts"], "duckdb": duck["artifacts"]},
        "freshness": {"native": nat["freshness"], "duckdb": duck["freshness"]},
        "calls": [{**c, "digest": {"native": nat["answers"][c["id"]],
                                   "duckdb": duck["answers"][c["id"]]}} for c in nat["plan"]],
    }
    (out / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
