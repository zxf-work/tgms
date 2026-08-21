# Bring your own temporal graph data

You have a stream of timestamped events — service calls, messages, sensor
readings, trades, anything with a "who/what happened, and when" shape — and
you want it in TGMS so you can ask time-aware questions about it, including
questions about corrections to the record. This walks through the whole
arc: write a tiny event file, ingest it, run a query, correct a mistake in
the data, and query the belief state before and after the correction.

Every command below was run against a real TGMS store while writing this
page. Your own timestamps and transaction-time (`tt`) numbers will differ
from the ones shown — `tt` is a hybrid logical clock seeded from wall time,
not a counter — but the shapes and the relationships between the numbers
will match.

## 0. Install

```
pip install tgms
```

That gives you the CLI (`tgms`) and the `tgms` Python package. Ingestion
needs no extra backend — the default store engine (`native`) ships in the
base package. (There is also an optional `duckdb` backend, `pip install
"tgms[duckdb]"`, kept around for A/B comparisons and pre-existing DuckDB
stores; you don't need it for this tutorial. See the aside below.)

> **Aside — on tgms 0.6.0, pass `--backend native` explicitly.** In the
> 0.6.0 release the `tgms ingest` CLI flag `--backend` defaults to
> `duckdb` even though the library's own default is `native` (D-028), and
> a plain `pip install tgms` does *not* include the `duckdb` package — so
> omitting `--backend` on a fresh 0.6.0 install fails with `ImportError:
> this store uses the duckdb backend, which is now an optional extra`.
> This is fixed in the release after 0.6.0 (the flag now defers to the
> store's own backend detection, so no flag means a native store). The
> commands below pass `--backend native` explicitly, which is correct on
> every version.

## 1. The event format

`tgms ingest` reads a JSON Lines file: one event object per line. This is
the schema the ingest path (`Store.ingest_events`) actually reads:

| field | type | required | meaning |
|---|---|---|---|
| `src` | string | yes | source node uid |
| `dst` | string | yes | destination node uid |
| `rel_type` | string | yes | edge label, e.g. `"CALLS"`, `"MSG"`, `"TRUST"` |
| `vt_s` | integer | yes | valid-time start, **int64 epoch microseconds, UTC** |
| `vt_e` | integer | no | valid-time end (half-open); defaults to `vt_s + 1`, i.e. the event is treated as instantaneous |
| `props` | object | no | arbitrary JSON properties on the edge (e.g. `{"latency_ms": 42}`) |
| `disc` | string | no | discriminator that names this specific logical edge; defaults to the event's position in the batch (`"#3"`, `"#4"`, …) |

Nodes are created implicitly the first time their uid appears (label
`"Node"`, no properties) — you don't declare nodes up front. Every event
becomes its own edge, disjoint from every other event with the same
`(src, dst, rel_type)`; that's what `disc` is for. Set `disc` explicitly on
any event you might want to correct later, so you have a stable handle on
it — the auto-assigned `"#N"` form depends on the event's position in the
file, which is fragile to depend on.

## 2. Build a tiny example

A four-service call graph, one call every few minutes, twelve events total.
One of them (`call-09`) was logged with a bogus latency reading — the kind
of thing a monitoring glitch produces — that we'll correct in step 5.

Save this as `events.jsonl`:

```jsonl
{"src": "svc-gateway", "dst": "svc-auth", "rel_type": "CALLS", "vt_s": 1767225600000000}
{"src": "svc-auth", "dst": "svc-orders", "rel_type": "CALLS", "vt_s": 1767225720000000}
{"src": "svc-orders", "dst": "svc-notify", "rel_type": "CALLS", "vt_s": 1767225900000000}
{"src": "svc-gateway", "dst": "svc-orders", "rel_type": "CALLS", "vt_s": 1767226200000000}
{"src": "svc-orders", "dst": "svc-notify", "rel_type": "CALLS", "vt_s": 1767226320000000}
{"src": "svc-auth", "dst": "svc-notify", "rel_type": "CALLS", "vt_s": 1767226500000000}
{"src": "svc-gateway", "dst": "svc-auth", "rel_type": "CALLS", "vt_s": 1767226800000000}
{"src": "svc-auth", "dst": "svc-orders", "rel_type": "CALLS", "vt_s": 1767226920000000}
{"src": "svc-orders", "dst": "svc-notify", "rel_type": "CALLS", "disc": "call-09", "vt_s": 1767227100000000, "props": {"latency_ms": 9999}}
{"src": "svc-notify", "dst": "svc-gateway", "rel_type": "CALLS", "vt_s": 1767227400000000}
{"src": "svc-gateway", "dst": "svc-auth", "rel_type": "CALLS", "vt_s": 1767227700000000}
{"src": "svc-auth", "dst": "svc-orders", "rel_type": "CALLS", "vt_s": 1767228000000000}
```

(`vt_s` values are `2026-01-01T00:00:00Z` plus a few minutes each — any
timestamps work, these just need to be int64 epoch microseconds. Compute
your own with `int(dt.timestamp() * 1_000_000)`.)

## 3. Ingest it

```
tgms ingest events.jsonl --store my_store --backend native
```

Real output:

```json
{"last_tt": 1787323156530996, "stats": {"n_entities": 4, "n_node_versions": 4, "n_edge_versions": 12, "vt_min": 1767225600000000, "vt_max": 1767228000000001, "rel_type_counts": {"CALLS": 12}, "max_out_degree": 4}}
```

`last_tt` is the transaction time this ingest was committed at — the belief
timestamp you'd pin a query to if you wanted "what did the store know right
after this ingest, and nothing since." Keep it; you'll need something like
it in step 6.

## 4. Run a first query

Every TGMS operator is callable from the CLI as `tgms call --store STORE
OP ARGS_JSON`. Let's ask a real temporal question: which services can
`svc-gateway` reach, and how fast, via time-respecting call chains?

```
tgms call --store my_store temporal_reachability '{"src": "svc-gateway", "window": {"t_a": 1767225600000000, "t_b": 1767228000000001}}'
```

Real output (trimmed):

```json
{
 "op": "temporal_reachability",
 "args_echo": { "src": "svc-gateway", "window": {"t_a": 1767225600000000, "t_b": 1767228000000001}, "as_of_tt": 4611686018427387904, "direction": "out", "limit": 100, "cursor": null },
 "dataset_extent": {"vt_min": 1767225600000000, "vt_max": 1767228000000001},
 "truncated": false,
 "rows": [
  {"uid": "svc-auth", "earliest_arrival": 1767225600000000},
  {"uid": "svc-orders", "earliest_arrival": 1767225720000000},
  {"uid": "svc-notify", "earliest_arrival": 1767225900000000}
 ],
 "rows_total": 3,
 "result_digest": "b6033562a3e9fe9f9db3d507ac562e3260a806b558dfaa35c3069ef2bdfd413b"
}
```

`earliest_arrival` is the first time a time-respecting path (each hop's
`vt_s` at or after the previous hop's) reaches that node — a genuinely
temporal answer, not just "is there an edge." `as_of_tt:
4611686018427387904` is `OPEN_END`, TGMS's sentinel for "current beliefs" —
every operator is bi-temporal by default even when you don't ask for
history. `result_digest` is a content hash of the result; you'll see it
again in [Audit an answer](audit-an-answer.md).

Run `tgms call --store my_store <op> '{}'` with a bad or empty args object
to see the full arg schema echoed back in the error — every operator
validates its arguments against a JSON Schema before running.

## 5. Correct the bad reading

The CLI doesn't yet have an `ingest`-time notion of "this event was wrong"
— bulk `ingest_events` is assert-only, by design, for fast bulk loading.
Corrections go through the write API directly, one call at a time, which
today means a few lines of Python rather than a CLI flag (a rough edge —
see the note at the end of this tutorial):

```python
import tgms
from tgms.core.model import EntityRef

store = tgms.open("my_store")

ref = EntityRef(kind="edge", src="svc-orders", dst="svc-notify",
                rel_type="CALLS", disc="call-09")
after_tt = store.correct(ref, {"latency_ms": 42},
                         vt_s=1767227100000000, vt_e=1767227100000001)
print("corrected at tt =", after_tt)
store.close()
```

Real output: `corrected at tt = 1787323191377217`.

`vt_s`/`vt_e` here must match the *valid-time interval you're correcting*
(the original event's `[vt_s, vt_s + 1)`, since it was instantaneous) —
`correct()` supersedes whatever believed version(s) overlap that interval
and writes new properties for it. The old belief isn't deleted: it's
marked superseded as of the new `tt`, which is exactly what makes the next
step possible.

## 6. Query the belief before vs. after the correction

`aggregate_events` takes `as_of_tt` like every other operator — pin it to
the `tt` from step 3 (before the correction existed) and you get the old
belief; leave it out (or pass the current one) and you get the corrected
belief:

```
tgms call --store my_store aggregate_events '{"group_by": [], "aggregates": [{"agg": "max", "of": "prop", "prop": "latency_ms"}], "window": {"t_a": 1767225600000000, "t_b": 1767228000000001}, "as_of_tt": 1787323156530996}'
```

```json
{ "rows": [ { "max_prop_latency_ms": 9999 } ], "prop_coercion": {"latency_ms": 11} }
```

Same query, no `as_of_tt` (current beliefs, i.e. after the correction):

```
tgms call --store my_store aggregate_events '{"group_by": [], "aggregates": [{"agg": "max", "of": "prop", "prop": "latency_ms"}], "window": {"t_a": 1767225600000000, "t_b": 1767228000000001}}'
```

```json
{ "rows": [ { "max_prop_latency_ms": 42 } ], "prop_coercion": {"latency_ms": 11} }
```

Same store, same query, same valid-time window — the only thing that
changed is which belief state you asked under, and the answer changed from
`9999` to `42`. That's the core bi-temporal move: TGMS never overwrites
history in place, so "what did we believe as of last Tuesday" stays
answerable forever, even after this Tuesday's correction lands.
(`prop_coercion: {"latency_ms": 11}` just means 11 of the 12 events don't
carry a `latency_ms` property at all and were excluded from the
aggregate — expected, since only `call-09` has one.)

## Rough edge, stated plainly

There is no `tgms correct` / `tgms retract` CLI subcommand yet — only
`ingest` (bulk, assert-only) and the read-only operator surface (`call`,
`ask`, `serve`) are exposed from the command line. Issuing a correction or
a retraction today requires the Python `Store` API shown in step 5. If you
need this from a shell script rather than Python, that's a real gap worth
filing.

## Where to go next

- [Give TGMS to an agent](agent-setup.md) — put this store behind MCP so an
  LLM agent can query it itself.
- [Audit an answer](audit-an-answer.md) — what a trace looks like, and what
  TGMS does and doesn't guarantee about an answer.
- [`docs/STABILITY.md`](../STABILITY.md) — what's durable across upgrades
  (event logs) vs. what isn't (on-disk store format).
