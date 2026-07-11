"""Agent facade (spec 7.3.1): `tgms.Agent(store, model=...)` — the interface
researchers touch. Ties Planner -> static Verifier -> Executor with the
runtime repair loop (E_COST / E_NOT_FOUND return control to the planner).
The claim-level reporter/verifier (WP2.3b) attaches at M5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from tgms.agent.executor import Executor, ResultStore, Trace
from tgms.agent.planner import Planner, PlanResult
from tgms.store import Store
from tgms.tools.server import ToolRouter

REPAIRABLE = {"E_COST", "E_NOT_FOUND"}


def dataset_card(store: Store) -> dict[str, Any]:
    s = store.stats()
    return {
        "extent": {"vt_min": s["vt_min"], "vt_max": s["vt_max"]},
        "n_entities": s["n_entities"],
        "n_edge_versions": s["n_edge_versions"],
        "rel_types": sorted(s["rel_type_counts"]),
    }


class Agent:
    def __init__(self, store: Store, model: str,
                 llm_fn: Callable[..., str] | None = None,
                 cache_dir: str | Path | None = None,
                 max_repairs: int = 3, seed: int = 0,
                 guided: bool = False) -> None:
        from tgms.tools.schemas import tool_description

        self.store = store
        self.router = ToolRouter(store.adapter)
        manual = "\n".join(f"### {name}\n{tool_description(name)}"
                           for name in self.router.tools())
        self.planner = Planner(model=model, tool_manual=manual, llm_fn=llm_fn,
                               cache_dir=cache_dir, max_repairs=max_repairs,
                               seed=seed, guided=guided)
        self.executor = Executor(self.router,
                                 ResultStore(Path(store.path) / "results")
                                 if store.path else None)

    def ask(self, question: str,
            task_input_uids: set[str] | None = None,
            memory_notes: list[str] | None = None) -> dict[str, Any]:
        """Plan -> execute, with runtime repairs sharing the repair budget.
        Returns {plan_result, trace, answer}."""
        card = dataset_card(self.store)
        result: PlanResult = self.planner.plan(
            question, card, adapter=self.store.adapter,
            task_input_uids=task_input_uids, memory_notes=memory_notes)
        trace: Trace | None = None
        while result.plan is not None:
            trace = self.executor.run(result.plan)
            runtime_error = next(
                (s["error"] for s in trace.steps
                 if s.get("error", {}).get("error") in REPAIRABLE), None)
            if runtime_error is None or \
                    len(result.attempts) > self.planner.max_repairs:
                break
            result = self.planner.plan(
                question, card, adapter=self.store.adapter,
                task_input_uids=task_input_uids, memory_notes=memory_notes,
                runtime_error=runtime_error, prior=result)
        return {
            "plan_result": result,
            "trace": trace,
            "answer": trace.answer if trace is not None else None,
        }
