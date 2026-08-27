# Equivalence receipt — compiled `entity_history` vs the leaf

**2026-08-24.** Comparator:
`canonical_json(payload_of(envelope))` — the same function the oracle family
uses (`tgms/tgir/equiv.py`, M2 plan §8.1).

| | |
|---|---|
| git SHA | `f8b278b90431` |
| backends | native, duckdb |
| cases | 10 (0 byte-identical) |
| corpus | multi-version identities, a carving `correct`, a `retract`, a pinned read, a paged read |
| verdict | byte-identical **except** the divergence predicted below |

| backend | case | leaf rows | result |
|---|---|---|---|
| native | entity_history/plain | 2 | differs in `source` |
| native | entity_history/edges | 2 | differs in `source` |
| native | entity_history/paged | 1 | differs in `source` |
| native | entity_history/pinned | 2 | differs in `source` |
| native | entity_history/corrected | 3 | differs in `provenance_ref, source` |
| duckdb | entity_history/plain | 2 | differs in `source` |
| duckdb | entity_history/edges | 2 | differs in `source` |
| duckdb | entity_history/paged | 1 | differs in `source` |
| duckdb | entity_history/pinned | 2 | differs in `source` |
| duckdb | entity_history/corrected | 3 | differs in `provenance_ref, source` |

## Scope diff (non-gating)

```json
{
 "op": "entity_history",
 "leaf_terms": [
  {
   "kinds": [
    "assert_node",
    "correct",
    "retract",
    "ingest_events"
   ],
   "targets": {
    "nodes": [
     "u1"
    ]
   },
   "rel_types": "*",
   "vt": "*",
   "vt_mode": "overlap",
   "props": "*"
  },
  {
   "kinds": [
    "assert_edge",
    "ingest_events"
   ],
   "targets": {
    "incident": {
     "role": "either",
     "uids": [
      "u1"
     ]
    }
   },
   "rel_types": "*",
   "vt": "*",
   "vt_mode": "overlap",
   "props": [
    "@identity"
   ]
  }
 ],
 "carve_reachable": true,
 "note": "the compiled form reads the same versions under the same \u03a3 and emits no column the leaf did not, so its Level-0 scope is the leaf's \u2014 \u00a76 #4's precision cost applies to `diff_snapshots`, whose compiled form emits vt_s/vt_e unconditionally, and not to these two"
}
```

## Rollout

`COMPILE_MODE["entity_history"] = "leaf"`. This receipt is the evidence a promotion to
`shadow` would cite; the promotion itself needs a D-entry, and — for
`entity_history` — a ruling on the two payload fields §2.1's scan schema cannot
express.
