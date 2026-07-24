"""Experiment harness (WP2.6): config-driven matrix run
(task_family x dataset x system x model x seed), resumable, cached, emits
tidy CSV + markdown tables with determinism receipts (spec §8.4).

Systems:
  ours        planner -> executor -> reporter -> verifier; unsupported claims
              are gated out of the final answer (the C2 mechanism)
  ours-noverify (B3)  same, verifier off — reporter output goes out raw
  ours-nomem    (B4)  same as ours, no memory notes injected
  b1 / b2 / b5  baselines (eval/baselines.py), same answer contract

Frozen-split discipline (spec §8.3): runs on split="test" are logged to
<out_dir>/runs_log.jsonl; a second run with the same config sha refuses to
proceed without force=<reason>, and every force use is logged.

Fairness rules (WP2.6) are carried by the config: identical models,
temperature 0, identical seeds and repair budgets across systems; B1's k is
tuned on dev via its own config key.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from tgms.core.model import canonical_json, sha256_hex
from tgms.eval.metrics import extract_pred, rates, score_answer
from tgms.store import Store

OURS_SYSTEMS = ("ours", "ours-noverify", "ours-nomem")
BASELINE_SYSTEMS = ("b1", "b2", "b5", "b6")


# --------------------------------------------------------------------------- #
# per-system runners                                                           #
# --------------------------------------------------------------------------- #

def _task_window(task: dict[str, Any]) -> tuple[int, int] | None:
    for step in task["oracle_plan"]["steps"]:
        w = step["args"].get("window")
        if isinstance(w, dict) and "t_a" in w and not any(
                isinstance(v, dict) for v in w.values()):
            return w["t_a"], w["t_b"]
    return None


def run_task_ours(system: str, task: dict[str, Any], store: Store,
                  model: str, llm_fn: Callable[..., str], seed: int,
                  memory=None, guided: bool = False,
                  ablate_output_contracts: bool = False,
                  ablate_truncation_taint: bool = False) -> dict[str, Any]:
    from tgms.agent.agent import Agent
    from tgms.agent.reporter import Reporter
    from tgms.agent.verifier import ClaimVerifier

    notes: list[str] = []
    if memory is not None and system != "ours-nomem":
        w = _task_window(task)
        if w is not None:
            notes = [n["text"] for n in memory.retrieve(w[0], w[1], k=3)]

    agent = Agent(store, model=model, llm_fn=llm_fn, seed=seed, guided=guided,
                  ablate_output_contracts=ablate_output_contracts)
    t0 = time.perf_counter()
    out = agent.ask(task["question_text"],
                    task_input_uids=set(task["input_uids"]),
                    memory_notes=notes)
    trace = out["trace"]
    row: dict[str, Any] = {
        "first_emission_valid": out["plan_result"].first_emission_valid,
        "executed_ok": float(bool(trace and trace.ok)),
        "n_llm_calls": len(out["plan_result"].calls),
        "wall_s": round(time.perf_counter() - t0, 3),
    }
    pred = trace.answer if trace is not None else None
    scores = score_answer(task["answer_kind"], task["gold"],
                          extract_pred(task["answer_kind"], pred))
    row.update(scores)

    # reporter + (optionally) verifier gating
    if trace is not None and out["plan_result"].plan is not None:
        reporter = Reporter(model, llm_fn=llm_fn, seed=seed, guided=guided)
        answer_obj = reporter.report(task["question_text"],
                                     out["plan_result"].plan, trace,
                                     agent.executor.results)
        if system == "ours-noverify":
            # B3 ablation: emit the raw reporter answer; the verifier runs as
            # a measurement instrument only (no gating) so end-to-end UCR is
            # directly comparable with the gated system (C2 contrast)
            verifier = ClaimVerifier(trace, agent.executor.results,
                                     store.adapter,
                                     honor_truncation=not ablate_truncation_taint)
            report = verifier.verify(answer_obj)
            row["ucr"] = report["metrics"].get("ucr")
            row["coverage"] = report["metrics"].get("coverage")
            row["answer_object"] = answer_obj
        else:
            verifier = ClaimVerifier(trace, agent.executor.results,
                                     store.adapter,
                                     honor_truncation=not ablate_truncation_taint)
            report = verifier.verify(answer_obj)
            row["ucr_pre_gate"] = report["metrics"].get("ucr")
            # E2 readout: supported claims whose evidence was truncated or
            # tainted — with taint honored this is 0 by construction; with
            # the ablation it counts incomplete-evidence claims passing
            row["supported_incomplete"] = sum(
                1 for c, r in zip(answer_obj["claims"], report["claims"])
                if r["verdict"] == "supported"
                and verifier._evidence_payloads(c["evidence"])[2])
            kept = [c for c, r in zip(answer_obj["claims"], report["claims"])
                    if r["verdict"] != "unsupported"]
            gated = {**answer_obj, "claims": kept}
            regate = verifier.verify(gated)
            row["ucr"] = regate["metrics"].get("ucr")
            row["coverage"] = regate["metrics"].get("coverage")
            row["answer_object"] = gated
        row["verifiable_claims"] = system != "ours-noverify"
    return row


def run_task_baseline(system: str, task: dict[str, Any], baseline: Any,
                      seed: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    out = baseline.answer(task["question_text"], task["input_uids"])
    pred = extract_pred(task["answer_kind"], out["answer_object"])
    row = {
        "first_emission_valid": None,
        "executed_ok": None,
        "verifiable_claims": False,  # no trace to check against (B5 contrast)
        "wall_s": round(time.perf_counter() - t0, 3),
        "answer_object": out["answer_object"],
        "meta": out["meta"],
    }
    row.update(score_answer(task["answer_kind"], task["gold"], pred))
    return row


# --------------------------------------------------------------------------- #
# matrix runner                                                                #
# --------------------------------------------------------------------------- #

def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "n/a"


def _receipts(cfg: dict[str, Any], suite: dict[str, Any]) -> dict[str, Any]:
    return {
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "config_sha": sha256_hex(canonical_json(cfg)),
        "test_split_sha": suite.get("test_split_sha"),
        "dataset": suite.get("dataset"),
        "suite_seed": suite.get("seed"),
    }


def _frozen_guard(out_dir: Path, cfg_sha: str, split: str,
                  force: str | None) -> None:
    """spec §8.3: the frozen test split is evaluated once per config; every
    override is logged with a reason."""
    log = out_dir / "runs_log.jsonl"
    if split == "test" and log.exists():
        prior = [json.loads(line) for line in log.read_text().splitlines()
                 if line]
        if any(r["config_sha"] == cfg_sha and r["split"] == "test"
               for r in prior):
            if not force:
                raise RuntimeError(
                    "test split already evaluated for this config; pass "
                    "force='<reason>' (logged) to rerun (spec §8.3)")
    with open(log, "a") as f:
        f.write(json.dumps({"ts": time.time(), "config_sha": cfg_sha,
                            "split": split, "force": force or None}) + "\n")


def build_systems(cfg: dict[str, Any], store: Store, model: str,
                  llm_fn: Callable[..., str],
                  embed_fn: Callable | None, seed: int) -> dict[str, Any]:
    """Instantiate requested baseline systems once per (model, seed)."""
    out: dict[str, Any] = {}
    for system in cfg["systems"]:
        if system in OURS_SYSTEMS:
            out[system] = None  # built per task (cheap; needs fresh agent)
        elif system == "b1":
            from tgms.eval.baselines import VectorRAG
            out[system] = VectorRAG(store, llm_fn, model,
                                    k=cfg.get("b1_k", 20),
                                    chunk_events=cfg.get("b1_chunk_events", 256),
                                    embed_fn=embed_fn, seed=seed)
        elif system == "b2":
            from tgms.eval.baselines import StaticGraphRAG
            out[system] = StaticGraphRAG(store, llm_fn, model,
                                         max_edges=cfg.get("b2_max_edges", 2_000),
                                         seed=seed)
        elif system == "b5":
            from tgms.eval.baselines import TextToCypher, build_vanilla_kuzu
            vk_path = Path(cfg["out_dir"]) / f"vanilla-kuzu-{suite_tag(cfg)}"
            if not vk_path.exists():
                from tgms.data.loaders import load
                events = load(cfg["b5_events_dataset"], cfg["data_dir"]) \
                    if cfg.get("b5_events_dataset") else _events_from_store(store)
                vdb, conn = build_vanilla_kuzu(events, vk_path)
                # release the writer handles: query execution runs in
                # hard-bounded child processes that need to open the file
                for closer in (conn.close, vdb.close):
                    try:
                        closer()
                    except Exception:
                        pass
            out[system] = TextToCypher(None, llm_fn, model,
                                       db_path=str(vk_path),
                                       max_repairs=cfg.get("max_repairs", 3),
                                       seed=seed)
        elif system == "b6":
            from tgms.eval.baselines import BiTemporalSQL
            src_db = Path(cfg["store_path"]) / "store.duckdb"
            bt_path = Path(cfg["out_dir"]) / f"bitemporal-{suite_tag(cfg)}.duckdb"
            if not bt_path.exists():
                # snapshot copy: the harness holds the live store's write
                # connection, and DuckDB allows read-only opens only when no
                # writer holds the file
                import shutil
                bt_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_db, bt_path)
            out[system] = BiTemporalSQL(llm_fn, model, db_path=bt_path,
                                        max_repairs=cfg.get("max_repairs", 3),
                                        seed=seed)
        else:
            raise ValueError(f"unknown system {system}")
    return out


def _events_from_store(store: Store):
    e = store.adapter.edges_columnar()
    src = store.adapter.uids_for(e["src_id"])
    dst = store.adapter.uids_for(e["dst_id"])
    for s, d, r, t in zip(src, dst, e["rel_type"], e["vt_s"]):
        yield {"src": s, "dst": d, "rel_type": r, "vt_s": int(t)}


def suite_tag(cfg: dict[str, Any]) -> str:
    return sha256_hex(cfg["suite_path"])[:8]


def run_matrix(cfg: dict[str, Any], llm_fn: Callable[..., str],
               embed_fn: Callable | None = None,
               force: str | None = None,
               usage_log: list | None = None) -> list[dict[str, Any]]:
    """cfg keys: suite_path, store_path, out_dir, systems, models, seeds,
    split ('dev'|'test'), optional b1_k / max_repairs / memory_db."""
    import tgms

    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    suite = json.loads(Path(cfg["suite_path"]).read_text())
    split = cfg.get("split", "dev")
    cfg_sha = sha256_hex(canonical_json(cfg))
    _frozen_guard(out_dir, cfg_sha, split, force)
    tasks = suite[split]

    store = tgms.open(cfg["store_path"])
    memory = None
    if cfg.get("memory_db"):
        from tgms.agent.memory import EvolutionMemory
        memory = EvolutionMemory(cfg["memory_db"])

    cache_dir = out_dir / "results"
    cache_dir.mkdir(exist_ok=True)
    rows: list[dict[str, Any]] = []
    for model in cfg["models"]:
        for seed in cfg["seeds"]:
            systems = build_systems(cfg, store, model, llm_fn, embed_fn, seed)
            for system in cfg["systems"]:
                for task in tasks:
                    key = sha256_hex(canonical_json(
                        [task["id"], system, model, seed, split]))
                    cache_file = cache_dir / f"{key}.json"
                    cached = (json.loads(cache_file.read_text())
                              if cache_file.exists() else None)
                    if cached is not None and "task_error" not in cached:
                        row = cached
                    else:
                        # infrastructure-failure rows (dead server, network)
                        # are never treated as results — recompute them
                        u0 = len(usage_log) if usage_log is not None else 0
                        try:
                            if system in OURS_SYSTEMS:
                                row = run_task_ours(
                                    system, task, store, model, llm_fn, seed,
                                    memory=memory,
                                    guided=bool(cfg.get("llm_guided")),
                                    ablate_output_contracts=bool(
                                        cfg.get("ablate_output_contracts")),
                                    ablate_truncation_taint=bool(
                                        cfg.get("ablate_truncation_taint")))
                            else:
                                row = run_task_baseline(system, task,
                                                        systems[system], seed)
                        except Exception as e:  # one bad task must not kill
                            row = {"first_emission_valid": None,   # the matrix
                                   "executed_ok": 0.0, "em": 0.0, "f1": 0.0,
                                   "task_error": f"{type(e).__name__}: "
                                                 f"{str(e)[:300]}"}
                        if usage_log is not None:
                            new = usage_log[u0:]
                            row["tokens_in"] = sum(x["tokens_in"] for x in new)
                            row["tokens_out"] = sum(x["tokens_out"] for x in new)
                        row.update(task_id=task["id"], family=task["family"],
                                   dataset=task["dataset"], system=system,
                                   model=model, seed=seed)
                        cache_file.write_text(canonical_json(row))
                    rows.append(row)
    store.close()
    _emit_tables(cfg, suite, rows, out_dir)
    return rows


def _emit_tables(cfg: dict[str, Any], suite: dict[str, Any],
                 rows: list[dict[str, Any]], out_dir: Path) -> None:
    import pandas as pd

    df = pd.DataFrame([{k: v for k, v in r.items()
                        if k not in ("answer_object", "meta")} for r in rows])
    df.to_csv(out_dir / "results.csv", index=False)

    receipts = _receipts(cfg, suite)
    lines = ["# TGMS matrix results",
             "", "receipts — " + "; ".join(f"{k}: `{v}`"
                                           for k, v in receipts.items()), "",
             "| system | model | family | n | PVR | ESR | EM | F1 | UCR |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for (system, model, family), grp in sorted(
            df.groupby(["system", "model", "family"])):
        r = rates(grp.to_dict("records"))
        lines.append(
            f"| {system} | {model} | {family} | {r['n']} | "
            f"{r['pvr']:.2f} | {r['esr']:.2f} | {r['em']:.2f} | "
            f"{r['f1']:.2f} | {r['ucr']:.3f} |")
    (out_dir / "results.md").write_text("\n".join(lines) + "\n")
