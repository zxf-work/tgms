# Maintain derived results

You have a saved result you built once and want to keep — a daily rollup,
a report, anything that took real work to compute — and you don't want to
choose between "trust it forever" and "recompute it from scratch on every
question." This walks through the whole arc: register a result as a named
*artifact*, check whether it's still fresh, refresh it when it isn't, and
watch that refresh propagate one hop to whatever else was built on top of
it — even when the dependent's own scope was never touched.

Every command below was run against a real TGMS store while writing this
page. Your own digests and transaction times will differ — `tt` is a hybrid
logical clock seeded from wall time, not a counter — but the shapes and the
verdicts will match.

## 0. A tiny store

```
tgms ingest events.jsonl --store my_store --backend native
```

with `events.jsonl`:

```jsonl
{"src": "svc-gateway", "dst": "svc-auth", "rel_type": "CALLS", "vt_s": 1767225600000000}
{"src": "svc-auth", "dst": "svc-orders", "rel_type": "CALLS", "vt_s": 1767225720000000}
{"src": "svc-gateway", "dst": "svc-orders", "rel_type": "CALLS", "vt_s": 1767226200000000}
```

(Same shape [Bring your own data](bring-your-own-data.md) walks through in
more detail — start there first if this is unfamiliar.) Three nodes get
created implicitly: `svc-gateway`, `svc-auth`, `svc-orders`.

## 1. What "artifact" means here

`tgms artifact register/list/check/refresh` manages a **registered result**:
a named, generation-numbered record (`name@generation`) stored beside the
event log in `<store_path>/artifacts.jsonl`. Every registration carries the
same freshness machinery `tgms trace check` uses — a dependency scope and a
`tt_q` — plus a `refresh` handle that says how to recompute it, and
optionally a `parents` list naming other artifacts it was built from. `check`
is the identical `FRESH` / `POSSIBLY_STALE` / `UNDECIDABLE` verdict
[Audit an answer](audit-an-answer.md) §6 already covers, applied to a name
instead of a bare trace record.

We'll register two: `gateway-daily`, scanning `svc-gateway` and `svc-auth`,
and `weekly-report`, scanning `svc-gateway` alone and declaring
`gateway-daily` as its parent — a report built partly from the daily rollup.

## 2. Register the first artifact

Registering a `query_result` artifact needs a *plan digest* — a
content-addressed identity for exactly what will be re-run on refresh. For
the 15 operators (`temporal_reachability`, `aggregate_events`, ...), `tgms
call` already prints one in every envelope's `tgir` field, so the record
you register is copy-pasteable from real command output. A precise, per-uid
scan like the one below is a compositional-core primitive (`NodeScan`)
instead, and today that has no CLI form — building its plan digest needs one
short internal-API call. This is a real gap, stated plainly, the same way
[Bring your own data](bring-your-own-data.md) states the missing
`tgms correct` verb: everything *after* this step is ordinary CLI.

```python
import json
import tgms
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import dump
from tgms.tgir.node import NodeScan
from tgms.tgir.plan import Plan

store = tgms.open("my_store")

def register_blob(name, uids, parents=()):
    scan = NodeScan("p", uids=tuple(uids))
    env = run_plan(Plan(scan), store.adapter, tt_source=store)
    tgir = env["tgir"]
    open(f"my_store/plans/{tgir['plan_digest']}.json", "w").write(json.dumps(dump(scan)))
    doc = {
        "name": name, "kind": "query_result",
        "plan": {"plan_digest": tgir["plan_digest"], "node_digest": tgir["node_digest"],
                 "plan_format": 1, "plan_ref": f"plans/{tgir['plan_digest']}.json"},
        "basis": {"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
                  "tt_q_verified": env["dependency"].get("tt_q_verified", True)},
        "state": {"completeness": tgir.get("completeness", "unknown"),
                  "exactness": tgir.get("exactness", "exact"), "refusal": None},
        "refresh": {"kind": "tgir_plan", "ref": f"plans/{tgir['plan_digest']}.json",
                    "basis_policy": "open"},
        "dependency": env["dependency"],
        "parents": [list(p) for p in parents],
    }
    json.dump(doc, open(f"my_store/{name}.record.json", "w"), indent=1)

import os
os.makedirs("my_store/plans", exist_ok=True)
register_blob("gateway-daily", ["svc-gateway", "svc-auth"])
register_blob("weekly-report", ["svc-gateway"], parents=[("gateway-daily", 0)])
store.close()
```

That writes `my_store/gateway-daily.record.json` — real output (digests
shortened here; yours will differ):

```json
{
 "name": "gateway-daily", "kind": "query_result",
 "plan": {"plan_digest": "0066e488...618bd", "node_digest": "ba938f91...5616e70",
          "plan_format": 1, "plan_ref": "plans/0066e488...618bd.json"},
 "basis": {"tt_q": 1788098250285365, "pinned": false, "clamped": false, "tt_q_verified": true},
 "state": {"completeness": "complete", "exactness": "exact", "refusal": null},
 "refresh": {"kind": "tgir_plan", "ref": "plans/0066e488...618bd.json", "basis_policy": "open"},
 "dependency": {
  "schema": "tgms-depscope", "version": 1, "store": "cbd0852a...204afd4e2",
  "tt_q": 1788098250285365, "pinned": false, "clamped": false,
  "checkpoints": [[453, "fa8fffa0cf1b1085"]],
  "terms": [{"kinds": "*",
             "targets": {"nodes": ["svc-gateway", "svc-auth"],
                         "incident": {"role": "either", "uids": ["svc-gateway", "svc-auth"]}},
             "rel_types": "*", "vt": [[0, 4611686018427387904]],
             "vt_mode": "overlap", "props": "*"}]
 },
 "parents": []
}
```

`weekly-report.record.json` is the same shape, scoped to `["svc-gateway"]`
alone and `"parents": [["gateway-daily", 0]]`. That `targets.nodes` field is
the whole point: a `NodeScan`'s dependency term names the *specific* uids it
read, unlike the fifteen built-in operators, which today record the coarser
"matches anything on this store" term ([Audit an answer](audit-an-answer.md)
§6 calls this out directly). A precise term is what lets `weekly-report`
stay unbothered by a correction that never touches `svc-gateway`, two steps
from now.

Now the ordinary part — real CLI, both artifacts:

```
$ tgms artifact register my_store --record-json my_store/gateway-daily.record.json
{
 "name": "gateway-daily",
 "generation": 0,
 "record_digest": "7d7daccb39aae59ede3b640e4cc14682a8fc0f2624459bcc0d805e2593648915"
}
$ tgms artifact register my_store --record-json my_store/weekly-report.record.json
{
 "name": "weekly-report",
 "generation": 0,
 "record_digest": "5c0ee5399a4fe576700b7edaf56d53d8aa5c45943e21127b64e51a33f1b514e3"
}
$ tgms artifact list my_store
gateway-daily@0  query_result  7d7daccb39aa
weekly-report@0  query_result  5c0ee5399a4f
```

## 3. Check, correct, check again

Both start `FRESH` — nothing has been written since either was registered:

```
$ tgms artifact check my_store --name gateway-daily
gateway-daily@0
This answer was produced on 2026-08-30 13:57:30 UTC. Nothing written since could have changed it.
$ echo $?
0
```

Now land a correction on `svc-auth` — inside `gateway-daily`'s scope,
outside `weekly-report`'s. No CLI verb for `correct` exists yet (the same
gap [Bring your own data](bring-your-own-data.md) §5 names), so this step
is Python too:

```python
import tgms
from tgms.core.model import OPEN_END, EntityRef

store = tgms.open("my_store")
ref = EntityRef(kind="node", uid="svc-auth")
tt = store.correct(ref, {"owner": "payments-team"}, vt_s=0, vt_e=OPEN_END)
print("corrected at tt =", tt)
store.close()
```

Check both again — real output:

```
$ tgms artifact check my_store --name gateway-daily
gateway-daily@0
This answer may be stale.
  plan: A write received on 2026-08-30 13:58:06 UTC corrected node svc-auth over a valid-time region this computation read. Reconsider.
$ echo $?
1

$ tgms artifact check my_store --name weekly-report
weekly-report@0
This answer was produced on 2026-08-30 13:57:30 UTC. Nothing written since could have changed it.
$ echo $?
0
```

`gateway-daily` flips to `POSSIBLY_STALE`; `weekly-report` — which never
scanned `svc-auth` — stays `FRESH`, exactly because its dependency term
named `svc-gateway` alone. That precision is real, not asserted: it's the
same soundness direction `tgms trace check` promises, just narrower.

## 4. Refresh it

```
$ tgms artifact refresh my_store --name gateway-daily
gateway-daily@1 (supersedes @0)  bc1054e99a78
$ tgms artifact list my_store --name gateway-daily
gateway-daily@0  query_result  7d7daccb39aa
gateway-daily@1  query_result  bc1054e99a78
```

Generation 0 is untouched on disk — `refresh` never edits a prior
generation, it only ever appends a new one and records what it supersedes.

## 5. Propagate to the dependent

`weekly-report` is still `FRESH` by its own scope. But it named
`gateway-daily` as a parent, and that parent just advanced — this is exactly
the case `tgms artifact check` alone cannot see, because a scope check only
ever asks "did a write touch what *I* read," never "did a write touch what
something I depend on read." `tgms.artifact.propagate.parent_recheck`
answers the second question. It has no CLI verb yet either — named here as
the gap it is, not smoothed over:

```python
from tgms.artifact.propagate import parent_recheck
from tgms.artifact.registry import Registry

registry = Registry("my_store")
a1 = registry.current("gateway-daily")
for c in parent_recheck(a1.id, registry).candidates:
    print(f"{c.record.name}@{c.record.generation} flagged:",
          [(t.reason, t.parent.to_json(), t.parent_current.to_json()) for t in c.threats])
```

Real output:

```
weekly-report@0 flagged: [('parent-generation-advanced', ['gateway-daily', 0], ['gateway-daily', 1])]
```

The reason is `parent-generation-advanced`, never a footprint hit — a
genuinely different signal from step 3's scope check. Refreshing on it is
the same CLI call as before:

```
$ tgms artifact refresh my_store --name weekly-report
weekly-report@1 (supersedes @0)  cd3dcc8c2cb7
$ tgms artifact check my_store --name weekly-report
weekly-report@1
This answer was produced on 2026-08-30 13:58:06 UTC. Nothing written since could have changed it.
$ echo $?
0
```

`weekly-report@1`'s own record now names `parents: [["gateway-daily", 1]]`
— the propagation is itself auditable: `tgms artifact list my_store --name
weekly-report --json` shows both generations, each with the parent
generation it was actually built against, not just the parent's name.

## 6. The selective half

Land a second correction that touches neither artifact's scope
(`svc-orders`, which neither `gateway-daily` nor `weekly-report` ever
scanned):

```
$ tgms artifact check my_store --name gateway-daily
gateway-daily@1
This answer was produced on 2026-08-30 13:58:06 UTC. Nothing written since could have changed it.
$ tgms artifact check my_store --name weekly-report
weekly-report@1
This answer was produced on 2026-08-30 13:58:06 UTC. Nothing written since could have changed it.
```

Both still `FRESH`, zero recomputation. This is the other half of the
argument this tutorial exists to make: refresh is selective. It doesn't
walk the whole registry and recompute anything that *might* be affected —
it acts only where a scope check or a propagation flag actually names a
reason. Measured on a real maintenance campaign rather than this toy store,
that selectivity holds up at scale: **99.0%** of a live workload's
propagation decisions resolved to "still fresh" without recomputing
anything, with **0** false-safe decisions and **0** false-fresh verdicts
across the whole campaign.

## Rough edges, stated plainly

- **No CLI verb for `correct`, `register`ing a `NodeScan`-based artifact, or
  `parent_recheck`.** All three are one-time or infrequent internal-API
  calls today, shown above exactly as run. Everything you'd do *often* —
  `check`, `refresh`, `list` — is ordinary CLI.
- **The fifteen built-in operators record a coarser dependency term** than
  the `NodeScan` example above (`"targets": "*"` instead of a specific uid
  list) — an `operator`-kind artifact wrapping one of them (`refresh: {"kind":
  "operator", "ref": "ops/<name>.json", ...}`, `ref` naming a file shaped
  `{"op": ..., "args": {...}}`) is registerable purely from a `tgms call`
  envelope with no internal API at all, but it will flag on *any* correction
  in the store, not just ones that actually matter to it. Precision, not
  soundness, is what's still coarse here — see
  [`docs/STABILITY.md`](../STABILITY.md) §6.
- **`refresh` never asks "should I."** It re-executes whatever you name,
  whether or not a preceding `check` said `FRESH`. Deciding *when* to
  refresh — on a schedule, on every flagged artifact, only on ones a human
  reviewed — is a policy this tutorial's two manual calls stand in for, not
  something TGMS imposes for you.

## Where to go next

- [Audit an answer](audit-an-answer.md) §6 — the `FRESH` / `POSSIBLY_STALE`
  / `UNDECIDABLE` contract this tutorial reuses, explained from first
  principles.
- [Bring your own data](bring-your-own-data.md) — the event format and the
  `correct()` call this tutorial's step 3 uses.
- [`docs/STABILITY.md`](../STABILITY.md) §6 — exactly what the artifact
  registry promises across upgrades, and what it explicitly does not yet.
