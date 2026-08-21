# Give TGMS to an agent

TGMS exposes its operator toolbox over MCP (Model Context Protocol): point
any MCP-capable agent at a store, and it gets a fixed set of typed,
bounded, deterministic, read-only tools — no arbitrary code execution, no
free-form query language, no write access. This walks through standing the
server up, seeing exactly what an agent discovers when it connects, making
one tool call by hand, and what to expect once a real agent is driving.

This tutorial assumes you already have a store — follow [Bring your own
data](bring-your-own-data.md) first if you don't, or generate a throwaway
one with `tgms synth /tmp/demo --nodes 50 --events 500`.

## 1. Start the server

The library form (works against any local install):

```
tgms serve --store my_store
```

This blocks, speaking MCP over stdio — it's meant to be launched *by* an
MCP client (an agent runtime), not run interactively. `--readonly` is on
by default and currently the only mode: the MCP surface never exposes
`ingest`/`correct`/`retract`, only the query operators. If you want an
agent to write to the store, that's out of scope for MCP today — see the
"rough edge" note in [Bring your own data](bring-your-own-data.md).

### The published launch shape, and a gotcha in it

[`server.json`](../../server.json) is what the MCP Registry entry points
agents at. Its documented launch command is:

```
uvx tgms serve --store /path/to/store
```

As tested against the current PyPI release, **this fails on its own**:

```
$ uvx tgms serve --store my_store
...
ModuleNotFoundError: No module named 'fastmcp'
```

`fastmcp` (and `litellm`) ship under the optional `agent` extra, not the
base package (`pyproject.toml`: `agent = ["litellm>=1.48", "fastmcp>=2.0"]`),
so the base `tgms` install `uvx` resolves doesn't have what `serve` needs.
The command that actually works:

```
uvx --from 'tgms[agent]' tgms serve --store /path/to/store
```

That was verified end-to-end against the real PyPI package (a fresh MCP
client connected over the subprocess's stdio and listed 15 tools).
`server.json` in the repo now declares the `--from tgms[agent]` runtime
argument, so registry listings published from it assemble the working
command; if a client you use still launches the bare form, use the
`[agent]` form above by hand.

## 2. Client config

For a generic MCP client (Claude Desktop / Claude Code style
`mcpServers` config):

```json
{
  "mcpServers": {
    "tgms": {
      "command": "uvx",
      "args": ["--from", "tgms[agent]", "tgms", "serve", "--store", "/path/to/store"]
    }
  }
}
```

Swap `/path/to/store` for an absolute path to a store directory you've
already ingested data into.

## 3. What an agent discovers: tool listing

You don't need a live LLM to see this — any MCP client library can connect
and list tools. This is real output from connecting a client to a `tgms
serve` subprocess over stdio against the store built in the data
tutorial:

```
N tools: 15
['aggregate_events', 'burst_detection', 'co_active', 'compute',
 'count_temporal_motifs', 'diff_snapshots', 'entity_history',
 'find_temporal_motif_instances', 'graph_metric_timeseries',
 'neighborhood_evolution', 'resolve_entities', 'snapshot_subgraph',
 'temporal_paths', 'temporal_reachability', 'version_history']
```

Each tool's `description` is generated from the same operator registry
that backs schema validation — an agent reads the real contract, not a
hand-maintained blurb. Two examples, verbatim:

> **`temporal_reachability`** — Earliest-arrival time per node reachable
> from `src` via time-respecting paths inside `window` (source excluded).
> `direction='in'` answers "who could reach src".

> **`aggregate_events`** — Grouped aggregation over edge events (believed
> edge versions with `t_a <= vt_s < t_b`). Dimensions: `time_bucket` (with
> stride), `calendar_unit` … [truncated — this one runs to several hundred
> words; every dimension, aggregate, and edge case is spelled out, because
> planners get no other documentation]

Every tool's `inputSchema` has exactly one top-level property, `args` — an
object matching that operator's real JSON Schema (the same one `tgms call`
validates against). This is a FastMCP wrapping detail: **a tool call must
nest its arguments one level deeper than you might expect.**

## 4. Make one tool call by hand

Confirmed by an actual round trip against a running `tgms serve`
subprocess:

```python
result = await client.call_tool(
    "temporal_reachability",
    {"args": {"src": "svc-gateway",
              "window": {"t_a": 1767225600000000, "t_b": 1767228000000001}}},
)
```

Real response payload:

```python
{'op': 'temporal_reachability',
 'args_echo': {'src': 'svc-gateway', 'window': {...}, 'as_of_tt': 4611686018427387904, 'direction': 'out', ...},
 'dataset_extent': {'vt_min': 1767225600000000, 'vt_max': 1767228000000001},
 'truncated': False,
 'rows': [{'uid': 'svc-auth', 'earliest_arrival': 1767225600000000},
          {'uid': 'svc-orders', 'earliest_arrival': 1767225720000000},
          {'uid': 'svc-notify', 'earliest_arrival': 1767225900000000}],
 'rows_total': 3,
 'result_digest': 'b6033562a3e9fe9f9db3d507ac562e3260a806b558dfaa35c3069ef2bdfd413b'}
```

Note what's *not* here: no partial credit, no silent guessing. If your
`args` fail schema validation, or the query would exceed the cost
guardrail, the tool returns a structured error (`{"error": "E_...",
"message": ..., "details": {...}}`) instead of raising — an agent's repair
loop can read `error`/`message` and retry with corrected arguments, which
is exactly what the real planner (`tgms ask`) does.

## 5. A minimal agent interaction

The shape of every agent turn against TGMS, whether it's a full LLM loop
(`tgms ask`) or a hand-rolled MCP client:

1. **List tools** (once, or cache) — the agent gets the operator catalog
   and argument schemas shown above.
2. **Call an operator** with structured, schema-valid arguments.
3. **Read the self-describing envelope back** — `op`, `args_echo`,
   `dataset_extent`, `truncated`, `result_digest`, plus the operator's own
   fields (`rows`, `value`, ...). Every field needed to reason about what
   was actually computed rides along with the result; nothing is implicit.
4. **Chain steps** by referencing a prior step's output (`resolve_entities`
   → get a uid → feed it into `temporal_reachability` → feed *those* rows
   into `compute`) — this is what a real plan DAG looks like, and it's
   exactly what [Audit an answer](audit-an-answer.md) walks through with a
   full multi-step example.
5. **Repair on error** — a schema violation or a cost-guardrail refusal
   comes back as data (`{"error": ...}`), not a crash; the agent's next
   message can fix its arguments and retry.

## 6. What a good trace looks like

A single tool call already returns a self-describing result (`result_digest`,
`truncated`, `dataset_extent`, `args_echo` — steps 3–4 above). Once an
agent chains several calls into an answer, TGMS additionally builds a
**plan trace**: one record per step (`step_id`, `op`, `resolved_args_sha`,
`status`, `result_digest`, `wall_ms`, `truncated`, `upstream_truncated`),
plus a claim-level verification report that checks every number and
entity id in the final answer text against the cited trace steps. That's
the subject of the next tutorial — it walks through a real trace end to
end, including what happens when evidence is truncated.

## Where to go next

- [Audit an answer](audit-an-answer.md) — the full trace format, what gets
  verified, and what TGMS explicitly does not guarantee.
- [Bring your own data](bring-your-own-data.md) — build the store this
  tutorial's examples query against.
- [`docs/design/EVIDENCE_MODEL.md`](../design/EVIDENCE_MODEL.md) — the
  formal model behind the verification layer, if you want the full
  precision (written for a reviewer, not a first read).
