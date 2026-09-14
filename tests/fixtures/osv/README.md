# OSV test fixture (Lane F3)

20 real advisory records, carved from the four per-ecosystem `all.zip`
exports the design agent downloaded on 2026-09-13 from
`https://osv-vulnerabilities.storage.googleapis.com/<ECOSYSTEM>/all.zip`
(verified against `docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md` §3;
the zips themselves lived under the design session's scratchpad,
`.../scratchpad/{PyPI,Go,Maven,crates.io}.zip`, and are not checked in —
only the 20 extracted records are). Fields are untouched except where a file
is explicitly named a `.vN` **revision**, below.

## License and provenance

The OSV schema and the `osv.dev` tooling are Apache-2.0. The advisory
*content* itself is per-source, per `docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md`
§6: every record in this fixture is a **GHSA** (`GHSA-...`) or **OSV-native**
(`OSV-...`) id from the GitHub Advisory Database / OSS-Fuzz-style feeds, which
OSV lists as **CC-BY-4.0**. None of the four ecosystems' exports draw on a
CC-BY-SA or unaudited source, matching the live design's own selection
rationale.

## Layout

```
<Ecosystem>/<id>.json     -- one real record per file, as exported
revisions/<id>.vN.json    -- a later observation of <id>, N >= 2
```

`<Ecosystem>/` (`PyPI/`, `Go/`, `Maven/`, `crates.io/`) is what
`scripts/live_osv_poller.py --bootstrap tests/fixtures/osv` and
`tgms.data.osv_loader.iter_bootstrap_records` read directly: a flat directory
of `*.json` files per ecosystem, the same shape an extracted `all.zip` has —
`--bootstrap` names this directory itself, not a further subdirectory.
`revisions/` is not consumed by the bootstrap path at all (it is not an
ecosystem name, so `iter_bootstrap_records` never descends into it) — the
loader test (`tests/test_osv_loader.py`) reads `<eco>/<id>.json` as "what we
first believed" and `revisions/<id>.vN.json` as "what a later
`GET /v1/vulns/{id}` returned", and feeds the pair to `diff_to_ops`.

## Composition (20 advisories, 11 PyPI + 3 Go + 3 Maven + 3 crates.io)

| id | ecosystem | package | what it covers |
|---|---|---|---|
| `GHSA-22mf-97vh-x8rw` | PyPI | parso | **already withdrawn** at bootstrap (`withdrawn` present from the first observation) + a `last_affected` event with no `fixed` |
| `OSV-2021-1809` | PyPI | ujson | **a GIT range** (`type: "GIT"`, commit-hash `introduced`/two `fixed` events — a multi-event range, and neither hash is the `"0"` sentinel) |
| `GHSA-226f-f24g-524w` | PyPI | open-webui | **aliases across databases** (`CVE-2026-54008`, `PYSEC-2026-2690`) — both alias targets are outside our 4-ecosystem corpus, so the source data is **one-directional** (we never see a record naming this GHSA back); the loader writes both arms regardless (§1's "symmetric and transitive by schema") |
| `GHSA-22cc-w7xm-rfhx` | PyPI | mezzanine | a second **`last_affected` without `fixed`** example, with real CVE/PYSEC aliases |
| `GHSA-22c2-9gwg-mj59` | PyPI | langroid | plain publish, no revision |
| `GHSA-227r-w5j2-6243` | PyPI | invokeai | plain publish; base for two revisions (below) — also exercises the "ignore `affected[].versions`" rule (its real record carries ~140 enumerated versions the loader must never turn into nodes) |
| `GHSA-22cj-m4wf-fv2c` | PyPI | praisonai | plain publish; base for two revisions (below) |
| `GHSA-22gh-3r9q-xf38` | PyPI | mitmproxy | plain publish, no revision |
| `GHSA-22fp-mf44-f2mq` | PyPI | youtube-dl | plain publish; base for the **withdrawn transition** revision; also carries a real `related: [CVE-2024-38519]`, exercised by the loader's best-effort `withdraws`-edge heuristic (§7 risk 3 note in `tgms/data/osv_loader.py`) |
| `GHSA-22jm-p2vv-j2hc` | PyPI | plone | plain publish (two `affected` entries for the same package, different ranges); base for the **byte-identical no-op** revision |
| `MAL-2022-7421` | PyPI | ascii2text | **`MAL-` record that must be filtered** |
| `GHSA-2286-hxv5-cmp2`, `GHSA-228v-wc5r-j8m7`, `GHSA-22fx-6r9m-r8h9` | Go | sliver, OliveTin, libheif | ecosystem diversity, plain publish |
| `GHSA-223m-pgcq-f3xg`, `GHSA-2259-h742-5vr4`, `GHSA-2268-98wh-qfhf` | Maven | fortify plugin, jboss-ejb-client, jline-parent | ecosystem diversity, plain publish |
| `GHSA-2226-4v3c-cff8`, `GHSA-22q8-ghmq-63vf`, `GHSA-2326-pfpj-vx3h` | crates.io | rustc-serialize, libgit2-sys, lexical-core | ecosystem diversity, plain publish |

## Revisions (2 advisories with >= 2 modified revisions each)

| file | relative to | covers |
|---|---|---|
| `GHSA-227r-w5j2-6243.v2.json` | `PyPI/GHSA-227r-w5j2-6243.json` | **range edit**: `fixed` narrows from `5.3.0rc1` to `5.3.0` |
| `GHSA-227r-w5j2-6243.v3.json` | `.v2.json` | **severity change**: the CVSS vector's `I`/`C` components change |
| `GHSA-22cj-m4wf-fv2c.v2.json` | `PyPI/GHSA-22cj-m4wf-fv2c.json` | **reference-only bump**: one WEB reference added, nothing else |
| `GHSA-22cj-m4wf-fv2c.v3.json` | `.v2.json` | **alias gained**: `PYSEC-2026-9999` added — a synthetic id (the real record's export did not happen to gain a second alias in the observed window), used only to exercise the "aliases gains a member" op shape |
| `GHSA-22fp-mf44-f2mq.v2.json` | `PyPI/GHSA-22fp-mf44-f2mq.json` | **withdrawn transition**: `withdrawn` appears for the first time |
| `GHSA-22jm-p2vv-j2hc.v2.json` | `PyPI/GHSA-22jm-p2vv-j2hc.json` | **byte-identical no-op bump**: only `modified` changes |

Every other field in every `.vN.json` is byte-identical to its base except
where the table above says otherwise and except `modified`, which every
revision bumps (that bump is the poll trigger — §2 — and is deliberately
*not* by itself a reason to write anything).
