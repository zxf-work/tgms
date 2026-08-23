# Equivalence receipt — compiled `version_history` vs the leaf

**2026-08-22.** Comparator:
`canonical_json(payload_of(envelope))` — the same function the oracle family
uses (`tgms/tgir/equiv.py`, M2 plan §8.1).

| | |
|---|---|
| git SHA | `0148b41a80bc` |
| backends | native, duckdb |
| cases | 16 (16 byte-identical) |
| corpus | multi-version identities, a carving `correct`, a `retract`, a pinned read, a paged read |
| verdict | byte-identical on every case, both backends |

| backend | case | leaf rows | result |
|---|---|---|---|
| native | version_history/node | 8 | **identical** |
| native | version_history/edge | 4 | **identical** |
| native | version_history/paged | 2 | **identical** |
| native | version_history/superseded | 2 | **identical** |
| native | version_history/all | 10 | **identical** |
| native | version_history/pinned | 6 | **identical** |
| native | version_history/rel_types | 3 | **identical** |
| native | version_history/narrow | 4 | **identical** |
| duckdb | version_history/node | 8 | **identical** |
| duckdb | version_history/edge | 4 | **identical** |
| duckdb | version_history/paged | 2 | **identical** |
| duckdb | version_history/superseded | 2 | **identical** |
| duckdb | version_history/all | 10 | **identical** |
| duckdb | version_history/pinned | 6 | **identical** |
| duckdb | version_history/rel_types | 3 | **identical** |
| duckdb | version_history/narrow | 4 | **identical** |

## Scope diff (non-gating)

```json
{
 "op": "version_history",
 "leaf_terms": [
  {
   "kinds": "*",
   "targets": "*",
   "rel_types": "*",
   "vt": "*",
   "vt_mode": "overlap",
   "props": "*"
  }
 ],
 "carve_reachable": true,
 "note": "the compiled form reads the same versions under the same \u03a3 and emits no column the leaf did not, so its Level-0 scope is the leaf's \u2014 \u00a76 #4's precision cost applies to `diff_snapshots`, whose compiled form emits vt_s/vt_e unconditionally, and not to these two"
}
```

## Rollout

`COMPILE_MODE["version_history"] = "leaf"`. This receipt is the evidence a promotion to
`shadow` would cite; the promotion itself needs a D-entry, and — for
`entity_history` — a ruling on the two payload fields §2.1's scan schema cannot
express.
