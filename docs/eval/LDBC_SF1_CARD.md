# LDBC SNB SF1 dataset card

> **This is not an LDBC Benchmark, this is not an implementation of an LDBC
> Benchmark, and nothing in this document is an LDBC Benchmark Result.** LDBC
> material is used under CC-BY 4.0.

## Source

LDBC SNB's pre-generated **BI** data sets, artifact `bi-sf1-composite-merged-fk`
(the composite, FK-merged CSV serialization) — the **initial data set** only,
no update/delete streams applied (freeze `docs/design/PAPER_A_EVIDENCE_FREEZE.md`
§A2, §A6, §A8). **The Spark datagen tool is not used**; the artifact is
acquired as pre-generated CSVs, per the freeze's §A2 (citing LDBC's own
`snb-bi-pre-generated-data-sets.md` at `ldbc_snb_bi @ 47dd38b4`, an upstream
LDBC document, not vendored in this repository) and its §E addendum 1 ("the PI
reviewed the licensing question on the pre-generated LDBC SF1 datasets… and
authorized the download").

## Version / pins

Per `external_workloads/MANIFEST.yaml`'s `ldbc:` block:

| | |
|---|---|
| `ldbc_snb_bi` (BI paramgen queries, artifact spec) | commit `47dd38b40844ecdb0e42e5a610c369535304786d` |
| `ldbc_snb_docs` (spec text, id-space rule, published SF1 counts) | commit `b2269610f433da72e7c97041f01680aae369a903` |
| `ldbc_snb_interactive_v1_impls` (Interactive `validation_params-sf1.csv` + Neo4j reference results) | commit `11db98cc2ba14c33492f6c0c34e68c8be7e22e5f` |

## License

CC-BY 4.0. The disclaimer above is reproduced verbatim from
`docs/design/PAPER_A_EVIDENCE_REPORT.md` lines 13-15 and must accompany every
artifact, result record, and figure caption drawn from this substrate (freeze
§A11): the word "benchmark" is not used for this campaign — it is a
substrate.

## Mapping rules

Frozen as M1-M12 in `docs/design/PAPER_A_EVIDENCE_FREEZE.md` §A3 (verbatim
from `PAPER_A_EVIDENCE_PLAN.md` §1.4); the governing principle is that the
mapping is charitable to LDBC and never to TGMS. Two rules were amended
**before any data was ingested**, each recorded as a dated addendum rather
than a silent edit:

- **M5 (identity), amended (freeze §A4, §E addendum 2).** LDBC guarantees id
  disjointness only *within* one inheritance hierarchy, not across the seven
  independent hierarchies (Person, Forum, Message, Tag, TagClass, Place,
  Organisation), so a flat per-label offset was replaced by a **per-hierarchy
  interleave**: `uid = id * 8 + hierarchy_tag`, with the seven tags 0-6
  assigned in frozen alphabetical order (Forum 0, Message 1, Organisation 2,
  Person 3, Place 4, Tag 5, TagClass 6). Collision-free by construction at any
  id magnitude, strictly monotone within a hierarchy, and `Post`/`Comment`
  share the `Message` tag since IS2/IS6/IS7 traverse them as one population.
- **M1/M3 boundary, restated (freeze §E addendum 2).** From the actual
  composite-merged-fk serialization: seven edge types carry their own
  `creationDate` and take M1 (own-timestamp) — `KNOWS`, `LIKES`, `HAS_MEMBER`,
  `HAS_TAG`, `HAS_INTEREST`, `STUDY_AT`, `WORK_AT`; eight FK-merged types carry
  none and take M3 (inherited from the FK-carrying row at the moment the
  relationship is recorded) — `HAS_CREATOR`, `IS_LOCATED_IN`, `REPLY_OF`,
  `CONTAINER_OF`, `HAS_MODERATOR`, `HAS_TYPE`, `IS_SUBCLASS_OF`, `IS_PART_OF`.

## Sizes

| | |
|---|---|
| nodes | 2,997,352 |
| edge versions (incl. M7 `KNOWS` doubling) | 17,369,790 (17,196,776 published + 173,014; `tgms/tgir/eval/pattern.py` binds `src`/`dst` positionally and never consults `directed`, freeze §A5) |
| records streamed | 20,367,142 |
| mapping-fidelity gate (freeze §A7, blocking G2) | **27 of 27 exact** — every per-type node/edge count reproduces LDBC's published SF1 figures exactly, doubling declared as the one exception (`docs/design/PAPER_A_EVIDENCE_REPORT.md` §1.2) |
| store size, 2026-08-24 build (`stores/snb-sf1`) | 3,711 MB (event log 3,074 · segments 513 · dict 79 · manifests 1; `docs/design/PAPER_A_EVIDENCE_REPORT.md:57`) |
| store size, 2026-09-15 rebuild (`stores/snb-sf1-xz-a6b3e94`) | 4.4 GB (`benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09.README.md`; deleted from xzgpu after the record was verified) |

## Acquisition checksums

Two committed per-file manifests of the SF1 `initial_snapshot` directory (55
files each, format `relative_path byte_size mtime_utc_iso sha256`, sorted by
path) — the checksums promised in the freeze's §A2 and never previously
recorded:

| manifest | file sha256 |
|---|---|
| `benchmarks/results-v1/b1-manifest-v2-ab-2026-09-logs/dataset-manifest.txt` | `8b4168aa72042193a13648a003acd308f23c59922c8a0cf39b79a31246d0d590` |
| `benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09-csv-manifest.txt` | `9b0373fa78b2be5dba12a8181115f064ab52735d7b4641e396df9622f1bda7ae` |

Both manifests list the **same 55 paths with identical per-file sha256, byte
size and mtime** (all `2022-04-26T18:13:0*Z`); the two file-level digests
above differ only because one manifest prefixes paths with `./` and separates
fields with spaces where the other uses a bare relative path and tabs. Total
size 165,693,985 bytes (≈166 MB compressed CSV).

The original 2026-08-24 campaign (`ldbc-sf1-campaign.json`) recorded only the
`csv_root` path string
(`/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/graphs/csv/bi/composite-merged-fk/initial_snapshot`)
— **"same corpus" for that run rests on that path string and the unchanged
2022-04-26 mtimes, not on a digest taken at the time.** The tar.zst / zip
artifact md5 bundles named in the freeze's §A2 were **never recorded
anywhere** — not recorded here either, rather than invented.

## Templates (21 frozen plans, `benchmarks/tgir-v1/plans/`)

- **10 BI:** BI3, BI4, BI6, BI7, BI9, BI10, BI11, BI12, BI17, BI18
- **11 Interactive:** IC2, IC5, IC6, IC8, IC9, IC11, IC12, IS2, IS3, IS6, IS7
- **2026-09-15 additions** (`benchmarks/tgir-v1/plans/`, landed on public main
  at `c403ba5`): IS1, IS4, IS5 (three previously-unrun Interactive Short
  templates) and **BI6.v2** (see "Known BI6 defect" below).

The 21-row list matches `benchmarks/paper-a-v1/forecast.yaml`'s
`derived_admission.rows` and the campaign records in `benchmarks/results-v1/`.

## The two arms

Per the campaign manifest (`ldbc-sf1-campaign.json` / `ldbc-sf1-campaign-fmt3-2026-09.json`, `manifest.arms`), **never summed**:

- **`scored-bi`** — the 10 BI rows, bound to LDBC's own SF1 parameters (paramgen
  files). The only arm that carries a third-party-parameter claim.
- **`characterization-interactive`** — the 11 Interactive rows. LDBC assigns
  ids independently per workload, and this campaign's substrate is the BI SF1
  snapshot, not the separately generated Interactive SF1 dataset, so these
  rows' LDBC-native parameters name no entity here (freeze §E addendum 4).
  Anchors are instead **seeded draws from the BI store corpus itself** —
  execution-at-scale characterization only, excluded from every
  third-party-parameter claim. Seed `5724984519806421702` =
  `sha256("paper-a-v1")[:8]` (`manifest.campaign_seed` /
  `campaign_seed_source`).

## The interactive-arm anchor-binding rule and its `--csv` requirement

`scripts/tgir_ldbc_sf1.py --csv <initial_snapshot>` is what enables the
characterization-interactive arm at all. Its help text
(`scripts/tgir_ldbc_sf1.py:418-423`): *"Enables the §E addendum 4
characterization arm: the Interactive rows, whose LDBC parameters name
entities of a different dataset, get seeded anchors sampled from this corpus.
Omit and they fail as BIND_FAILED."*

The binder, `bind()` in `scripts/ldbc_snb_params.py:338-419`: only when
`csv_root is not None` (checked at line 392) does it overwrite each
Interactive row's id-valued parameters with `sample_anchor()` draws guaranteed
to exist in the store's own corpus (lines 392-398). Without `--csv`, the
Interactive rows bind the **raw** ids from `validation_params-sf1.csv` (the
Interactive dataset's own reference-parameter file, read via `read_iv_first`),
which by construction name entities of the separately generated Interactive
dataset, not the BI snapshot this campaign loads — measured, 2 of 98
`personId` values in that file exist in the BI store, versus 50 of 50 for the
BI paramgen ids (freeze §E addendum 4). When `adapter` is passed, every bound
id is checked against the store (`verify_anchors`, lines 401-402); a miss
raises `PhantomAnchor` (lines 403-408), which the runner records as
`outcome: BIND_FAILED, phantom_anchor: true` (`scripts/tgir_ldbc_sf1.py:329-340`).

**This is exactly what voided the 2026-09-15 rerun's interactive arm.**
`ldbc-sf1-campaign-fmt3-2026-09.json`'s `manifest.csv_root` is `""` (empty —
`--csv` was omitted on that invocation), and its companion
`ldbc-sf1-campaign-fmt3-2026-09.README.md` documents the consequence: all 14
characterization-interactive records (the 11 original IC/IS rows plus the 3
new IS1/IS4/IS5 rows) came back `BIND_FAILED` with `phantom_anchor: true`.
Each such record in the committed JSON carries only four keys — `error`,
`outcome`, `phantom_anchor`, `plan_id` — no `arm` or `rows` key (unlike a
`COMPLETED`/`ERRORED` record, which is bound and executed). The scored-bi arm
is unaffected: it does not consult `--csv` at all.

## Known BI6 defect and BI6.v2

**BI6 (v1).** The plan feeds the first outer join's null-filled `likerId`
column into the second join's left key. TGIR §2.8 makes a null join key an
**error**, never a non-match. On the 2026-08-24 SF1 campaign, BI6 **ERRORED**
at 535 null rows (`error_detail`: `"null join key 'likerId' on the left side
at 535 row(s) — §2.8 makes a null key an error, never a non-match"`,
`ldbc-sf1-campaign.json`); the 2026-09-15 rerun reproduces the same error
class (`ldbc-sf1-campaign-fmt3-2026-09.json`, `outcome: ERRORED`).

**BI6.v2** re-associates the two joins so no join key column is nullable in
its own input. Landed on public main at commit `c403ba5`
(`benchmarks/tgir-v1/plans/BI6.v2.json`). In the 2026-09-15 rerun's record
(`ldbc-sf1-campaign-fmt3-2026-09.json`, `plan_id: "BI6.v2"`): `outcome:
COMPLETED`, `derived_admission: refuse` (over the `time_est_ms` ceiling,
`estimate.time_est_ms 230072`), `bypassed: true` — so the plan ran to
completion under the campaign's 600 s bypass ceiling rather than being
refused outright — `rows: 100`, `ms: 291970.8707332611` (≈292 s of measured
plan time), `wall_s: 638.398`, `param_source: "bi-6.csv row 1"`. (The
README's per-plan table reports this same `ms` value, rounded, in its
"new ms" column — 291,971 is a measured-time figure in milliseconds, not a
row count; the row count is 100.)

## Gold arm: UNAVAILABLE

Per freeze §A10 / §E addendum 3: the Interactive `validation_params-sf1.csv`
carries Neo4j-produced reference results, but its **first operation is an
update**, and this campaign's substrate is the static initial snapshot (M8) —
no read in that file precedes the first update, so no expected result is
comparable to a read against the unmodified snapshot. The gold arm is
reported unavailable by name, per the pre-registered caveat, rather than
scored on a mismatched basis.

## Pending: P-SF1b

A rerun (store rebuild + the interactive arm invoked correctly with `--csv`
and `--emit-rows`) is scheduled on xzgpu after REPLAY-2 — scheduled, not run.
