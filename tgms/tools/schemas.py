"""Tool schema generation (WP1.5).

One source of truth: the operator registry (tgms/temporal/algebra.py).
This module derives (a) Anthropic/OpenAI function-calling JSON and (b) MCP
tool definitions from it. Tool *descriptions* are part of the research
artifact — they are the operator manual the planner reads — so they live in
a reviewed YAML (configs/tool_manual.yaml) keyed by operator name; the
registry description is the fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tgms.temporal.algebra import REGISTRY, ensure_all_registered

MANUAL_PATH = Path(__file__).resolve().parents[2] / "configs" / "tool_manual.yaml"


def load_manual() -> dict[str, str]:
    if MANUAL_PATH.exists():
        with open(MANUAL_PATH) as f:
            manual = yaml.safe_load(f) or {}
        return {k: v["description"] if isinstance(v, dict) else str(v)
                for k, v in manual.items()}
    return {}


def tool_description(name: str) -> str:
    manual = load_manual()
    desc = manual.get(name, REGISTRY[name].description)
    # authoritative output-field list: planners must not invent output paths
    fields = ", ".join(REGISTRY[name].output_fields)
    return f"{desc.rstrip()}\nOutput fields: {fields}."


def anthropic_tools(exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Anthropic tool-use format (OpenAI-compatible modulo the wrapper key)."""
    ensure_all_registered()
    return [
        {
            "name": name,
            "description": tool_description(name),
            "input_schema": spec.args_schema,
        }
        for name, spec in sorted(REGISTRY.items())
        if name not in exclude
    ]


def openai_tools(exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [
        {"type": "function",
         "function": {"name": t["name"], "description": t["description"],
                      "parameters": t["input_schema"]}}
        for t in anthropic_tools(exclude)
    ]


def mcp_tool_definitions(exclude: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [
        {"name": t["name"], "description": t["description"],
         "inputSchema": t["input_schema"]}
        for t in anthropic_tools(exclude)
    ]
