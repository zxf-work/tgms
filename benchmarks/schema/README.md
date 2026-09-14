# Benchmark result manifest schema

`result_manifest.schema.json` (JSON Schema, draft 2020-12) formalizes the
metadata a TGMS benchmark result should carry, so a result can be checked,
reproduced, and compared to another run without reading the harness that
produced it. It **formalizes fields the repo's benchmark manifests already
carry under various names** — it does not invent a new reporting
requirement. It was derived from:

- `docs/site_facts.json` — the published snapshot/fact records.
- `benchmarks/results-v1/eval-10m-d047.json` — `manifest.{measurement_host,
  commit, scale_events, cpu_count, platform}`.
- `benchmarks/results-v1/ldbc-sf1-campaign.json` — `manifest.{host, commit,
  campaign_seed, csv_root, store, protocol, wall_s}`.
- `benchmarks/m5-v1/*.json` — `machine.{host, platform, cpus}`.
- `benchmarks/tgir-v1/measured.yaml` — the header block, including
  `measured_date`.
- `scripts/tgir_measure.py` — `measured_date` injection (`_measured_date()`).

## Required fields

| Field | Shape | Notes |
|---|---|---|
| `schema_version` | string `X.Y.Z` | version of this schema the manifest targets |
| `git_commit` | string | 7-40 hex chars, optional `-dirty` suffix |
| `timestamp_utc` | string | RFC 3339 / ISO 8601, UTC |
| `machine.host` | string | hostname or stable label (e.g. `xzgpu`, `itiger02`) |
| `machine.platform` | string | OS/kernel/arch string |
| `machine.cpus` | integer ≥ 1 | logical CPU count |
| `machine.ram_gb` | number > 0 | total/allotted RAM in GiB |
| `config` | string \| object | path to a config file, or the config inlined |
| `seed.value` | integer \| string \| null | the run's seed |
| `seed.reason` | string \| null | **required, non-empty, when `value` is null** |
| `dataset.name` | string | dataset identifier |
| `dataset.digest` | string | the digest value |
| `dataset.digest_kind` | enum | one of `manifest`, `store_digest`, `eventlog_sha` |
| `result_digest` | string | digest over the reported result/rows |
| `protocol.warmups` | integer ≥ 0 | warmup repetitions excluded from measurement |
| `protocol.reps` | integer ≥ 1 | measured repetitions |
| `protocol.ceilings` | object | any cost/time ceilings enforced (empty object if none) |
| `record` | string | path to the raw per-row/per-trial records |

`additionalProperties: true` throughout — a manifest may carry more than
this; the schema only pins down what must be present and how it is shaped.

## Validating a manifest

```sh
python scripts/check_result_manifest.py path/to/manifest.json
```

Exits 0 if the file conforms, 1 with the first schema violation printed
otherwise.

## Auditing the existing corpus

```sh
python scripts/check_result_manifest.py --audit
```

Walks every `benchmarks/**/*.json` file (excluding this schema directory
itself) and validates each against the schema, printing a pass/fail table.

### Audit result (run 2026-09-13, commit `feaab3f`)

**157 files checked, 0 pass.** This is the expected finding, not a defect
to fix in this task: every existing manifest predates this schema and was
written under one of several different ad hoc shapes —
`benchmarks/results-v1/*.json` nests machine info under
`manifest.{measurement_host, cpu_count, platform}` with no `schema_version`,
`result_digest`, `dataset.digest_kind`, or `record` field in the shape this
schema expects; `benchmarks/m5-v1/*.json` nests it under
`machine.{host, platform, cpus}` with no `ram_gb`; `benchmarks/tgir-v1/`
carries plan files and a gold-answer list, neither of which is a result
manifest at all (two of them are JSON arrays, not objects, and fail for
that reason instead); `benchmarks/independent-v1/classification.json` and
`benchmarks/ldbc-fit-v1/classification.json` are likewise top-level arrays.
No existing manifest is retrofitted by this task — this schema is meant to
apply going forward, to new manifests, starting from whatever P0.4's
follow-up work decides.

**By directory:**

| Directory | Files | Pass | Fail |
|---|---|---|---|
| `benchmarks/freshness-v1/` | 4 | 0 | 4 |
| `benchmarks/frozen-v1/` | 3 | 0 | 3 |
| `benchmarks/independent-v1/` | 1 | 0 | 1 |
| `benchmarks/ldbc-fit-v1/` | 1 | 0 | 1 |
| `benchmarks/m5-v1/` | 28 | 0 | 28 |
| `benchmarks/results-v1/` | 67 | 0 | 67 |
| `benchmarks/tgir-v1/` | 53 | 0 | 53 |
| **Total** | **157** | **0** | **157** |

**By failure reason:**

| Reason | Count |
|---|---|
| Missing `schema_version` (and 9 other required top-level fields) | 155 |
| Top level is a JSON array, not an object | 2 |

<details>
<summary>Full per-file audit table (157 rows, from <code>scripts/check_result_manifest.py --audit</code>)</summary>

| Status | File | Reason |
|---|---|---|
| FAIL | `benchmarks/freshness-v1/trials-fixture-run1-superseded.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/freshness-v1/trials-fixture.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/freshness-v1/trials-full-run1-superseded.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/freshness-v1/trials-full.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/frozen-v1/suite-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/frozen-v1/suite-emaileu.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/frozen-v1/suite-synth.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/independent-v1/classification.json` | top level is list, not an object |
| FAIL | `benchmarks/ldbc-fit-v1/classification.json` | top level is list, not an object |
| FAIL | `benchmarks/m5-v1/carve-arm-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/carve-arm-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/pattern-l1-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/pattern-l1-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/pattern-l1-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/pinned-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/pinned-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/pinned-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/propagation-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/propagation-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/propagation-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-carve-synth-iv-60k.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-carve2-synth-iv-60k.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-pattern-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-pattern-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-pattern-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-pattern2-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-pattern2-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-pattern2-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-propagation-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-propagation-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-propagation-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-propagation2-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-propagation2-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/topup-propagation2-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/zero-changed-ops-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/zero-changed-ops-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/m5-v1/zero-changed-ops-sx-mathoverflow.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bench-corrections-ci-d073.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bench-corrections-full-d073.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bench-corrections-full-d079.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bisect-2601d1a.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bisect-468ee32.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bisect-47379fc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bisect-c56ebbb.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/bisect-e5756a9.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p1-leaf-overhead-bitcoinotc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p1-leaf-overhead-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p2-compiled-10m-after.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p2-compiled-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p2-compiled-1m-after.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p2-compiled-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/e14-p3-frontier.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-10m-4sys.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-10m-agg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-10m-bitemporal-d081.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-10m-bitemporal.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-10m-d047.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-4sys.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-agg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal-closecache.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal-closecache5.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal-confirm5.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal-d081.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal-postfix5.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal-prefix5.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-bitemporal.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-d047.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m-gc.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k-4sys.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k-5sys.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k-6sys.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k-bitemporal-d072.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k-costfix.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k-graphs.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-200k.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-collegemsg-costfix.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-collegemsg.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-durability-injection.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-guardrail-frontier-d087.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-guardrail-frontier.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-coldwarm-10m-d082.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-coldwarm-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-coldwarm-1m-d082.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-coldwarm-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-memcap-10m-perquery.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-memcap-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-memcap-budget-10m-perquery.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-memcap-budget-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-memcap-smallbudget-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-readers-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-readers-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-threads-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-threads-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-threads-recal-10m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-resources-threads-recal-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-writes.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-xtdb-1m-20-final.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-xtdb-1m-5-final.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/eval-xtdb-footprints-1m.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/gate-1m-control.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/gate-1m-parallel.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/results-v1/ldbc-sf1-campaign.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/gold.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI1.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI10.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI11.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI12.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI13.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI14.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI15.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI16.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI17.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI18.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI19.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI2.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI20.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI3.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI4.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI5.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI6.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI7.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI8.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/BI9.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC1.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC10.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC11.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC12.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC13.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC14.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC2.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC3.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC4.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC5.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC6.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC7.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC8.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IC9.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IS2.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IS3.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IS6.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/IS7.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo31.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo32.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo33.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo34.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo35.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo37.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/bo41.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm13.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm14.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm19.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm24.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm31.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm39.json` | <root>: 'schema_version' is a required property (+9 more) |
| FAIL | `benchmarks/tgir-v1/plans/cm6.json` | <root>: 'schema_version' is a required property (+9 more) |

</details>

## Tests

`tests/test_result_manifest_schema.py` covers: a valid example manifest
passes; a manifest missing `machine` fails; an invalid `dataset.digest_kind`
value fails the enum constraint; a null `seed.value` without a `reason`
fails; a null `seed.value` with a `reason` passes.
