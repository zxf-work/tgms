"""Reporter (WP2.3b): the second LLM call that turns question + trace
summaries into an AnswerObject under the answer contract. Every number or
uid the text asserts must be covered by a claim citing evidence steps.

If the LLM output fails schema validation twice, we fall back to the
mechanical AnswerObject derived from answer_spec — numbers-only, always
verifiable (mirrors the memory-digest fallback rule in WP2.4).
"""

from __future__ import annotations

import json
from typing import Any, Callable

import jsonschema

from tgms.core.model import canonical_json
from tgms.agent.executor import ResultStore, Trace
from tgms.agent.ir import Plan
from tgms.agent.planner import strip_fences
from tgms.agent.verifier import ANSWER_SCHEMA

REPORTER_SYSTEM = """You are a reporting assistant for TGMS. You receive an \
analytical question and the execution trace of a verified query plan. Write \
the final answer as ONE JSON AnswerObject and nothing else:

{"text": "<the prose answer>",
 "claims": [{"id": "c1", "type": "count|value|entity|ordering|temporal_pattern",
             ..., "evidence": ["s3"]}]}

Rules:
- Every number and every entity id mentioned in `text` MUST be covered by a
  claim; claims cite the trace steps ("s1", "s2", ...) that ground them.
- count/value claims carry {"value": <number>} and optionally
  {"from": "sN.<field>"} naming the exact trace field.
- entity claims carry {"uids": [...]}.
- ordering claims carry a, b (intervals {"start","end"} or {"t": ts}) and
  "relation" (Allen name, or {"within": micros}).
- temporal_pattern claims carry "assertion" (burst|concentration|trend) and
  "interval" {"t_a","t_b"}.
- Do not assert anything the trace does not show.
"""


def trace_summary(plan: Plan, trace: Trace, results: ResultStore | None,
                  max_rows: int = 5) -> str:
    parts = []
    for rec in trace.steps:
        line = {"step": rec["step_id"], "op": rec["op"], "status": rec["status"]}
        if rec.get("status") == "ok" and results is not None:
            payload = results.get(rec["result_digest"])
            excerpt = {k: v for k, v in payload.items()
                       if k in ("count", "value", "rows_total", "truncated",
                                "n_buckets")}
            rows = payload.get("rows")
            if isinstance(rows, list):
                excerpt["rows_head"] = rows[:max_rows]
            line["result"] = excerpt
        elif rec.get("error"):
            line["error"] = rec["error"].get("error")
        parts.append(canonical_json(line))
    parts.append(canonical_json({"final_answer": trace.answer,
                                 "answer_spec": plan.answer_spec}))
    return "\n".join(parts)


def mechanical_answer(plan: Plan, trace: Trace) -> dict[str, Any]:
    """Deterministic numbers-only AnswerObject from answer_spec — the
    fallback, and the gold-answer generator for the task suite."""
    src = plan.answer_spec["from"]
    step_id = src.split(".")[0]
    kind = plan.answer_spec["kind"]
    ans = trace.answer
    claim: dict[str, Any] = {"id": "c1", "evidence": [step_id]}
    if kind == "count" or (kind == "value" and isinstance(ans, (int, float))):
        claim.update(type="count" if kind == "count" else "value", value=ans)
        if "." in src:
            claim["from"] = src
        text = f"The answer is {ans}."
    elif kind == "entity_set":
        uids = [r["uid"] if isinstance(r, dict) else r for r in (ans or [])]
        claim.update(type="entity", uids=uids)
        text = f"The entities are: {', '.join(uids) if uids else '(none)'}."
    elif kind == "interval" and isinstance(ans, dict):
        iv = {"t_a": ans.get("t_a"), "t_b": ans.get("t_b")}
        claim.update(type="value", value=iv.get("t_a"), **({"from": src + ".t_a"}
                     if "." not in src else {}))
        text = f"The interval is [{iv['t_a']}, {iv['t_b']})."
    else:
        claim.update(type="value", value=None)
        text = json.dumps(ans)[:400] if ans is not None else "No answer."
        return {"text": text, "claims": []}
    return {"text": text, "claims": [claim]}


class Reporter:
    def __init__(self, model: str, llm_fn: Callable[..., str],
                 temperature: float = 0.0, seed: int = 0) -> None:
        self.model = model
        self.llm_fn = llm_fn
        self.temperature = temperature
        self.seed = seed

    def report(self, question: str, plan: Plan, trace: Trace,
               results: ResultStore | None) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": REPORTER_SYSTEM},
            {"role": "user", "content":
                f"QUESTION: {question}\n\nTRACE:\n"
                f"{trace_summary(plan, trace, results)}\n\nANSWER OBJECT:"},
        ]
        for _ in range(2):
            raw = self.llm_fn(self.model, messages, self.temperature, self.seed)
            try:
                obj = json.loads(strip_fences(raw))
                jsonschema.validate(obj, ANSWER_SCHEMA)
                return obj
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                                 f"Invalid AnswerObject ({e}). Emit corrected "
                                 "JSON only."})
        return mechanical_answer(plan, trace)
