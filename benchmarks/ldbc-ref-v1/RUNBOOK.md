# LDBC reference-correctness run — xzgpu runbook (lane D1-prep)

**Status: prep only. Nothing in this file has been executed.** This is a
dispatch package for the lane that runs it on xzgpu: every command below is
written so that lane can go straight from `git pull` to a submitted run
without redesigning anything. The design is
`docs/design/LDBC_REFERENCE_CORRECTNESS_DESIGN_2026-09-13.md` (D1, cited
throughout as "the design memo") and its correction
`docs/design/BI6_TRIAGE_2026-09-14.md` (D2). Read both before deviating from
anything here. The frozen predictions live in `campaign.yaml` next to this
file — this runbook is the *how*, that file is the *what was predicted*.

No experiment, ssh session, download, or Neo4j instance was run to produce
this document. Every number labelled **estimated** is a projection from the
design memo's own §5 table or from the D1-local campaign record
(`benchmarks/results-v1/ldbc-sf1-campaign.json`), not a new measurement.

---

## 0. What this run is not

Per `scripts/ldbc_reference_run.py` and `scripts/ldbc_compare.py`'s own
module docstrings: **this is not an LDBC Benchmark, does not implement an
LDBC Benchmark, and produces no LDBC Benchmark Result.** It is a
correctness comparison between TGIR's output and an independently loaded,
unmodified-query Neo4j reference, over the 24 templates TGIR can express.
LDBC material is used under CC-BY 4.0.

---

## 1. Where things live

| what | path | notes |
|---|---|---|
| this runbook + frozen predictions | `benchmarks/ldbc-ref-v1/` | this directory |
| vendored LDBC BI Cypher + import scripts | `external_workloads/ldbc/bi/neo4j/` | on `main` — see §1.1 |
| vendored LDBC Interactive Cypher | `external_workloads/ldbc/interactive_v1/cypher/` | on `main` — see §1.1 |
| pins for both vendored trees | `external_workloads/MANIFEST.yaml:59-72` | bi commit `47dd38b40844ecdb0e42e5a610c369535304786d`, interactive_v1 commit `11db98cc2ba14c33492f6c0c34e68c8be7e22e5f` |
| per-template contract classes (41 rows, copied) | `external_workloads/ldbc/coverage_annotation.jsonl` and `tests/fixtures/ldbc_ref/contracts.json` | **is** on `main` |
| TGIR plan artifacts | `benchmarks/tgir-v1/plans/*.json` | 25 files feed the 24 templates (BI6 has two: `BI6.json` evidence-only, `BI6.v2.json` the one to run — §6) |
| parameter binder | `scripts/ldbc_snb_params.py` | `export_bindings()` writes the two-sided `params.json` |
| Neo4j reference runner | `scripts/ldbc_reference_run.py` | runs vendored Cypher verbatim over the `neo4j` driver |
| compare | `scripts/ldbc_compare.py` | normalizes, matches, classifies disagreements |
| D1-local (fixture-scale) evidence this run supersedes for correctness (not for expressiveness) | `benchmarks/results-v1/ldbc-sf1-campaign.json` | 21/24 executed at SF1 already, 0 compared against any reference — this is the run that closes that gap |

### 1.1 The vendored Cypher is on `main` (lane D1-fix, 2026-09-14)

`external_workloads/ldbc/bi/neo4j/` and `external_workloads/ldbc/interactive_v1/cypher/`
are now vendored on this repo's `main` (query/schema/script text only, at the
pinned commits above; see `external_workloads/ldbc/README.md` for the
provenance note and the upstream `LICENSE.txt`/`NOTICE.txt` files carried
alongside each tree). This closes the gap this section used to document — a
plain `git pull` on the xzgpu checkout is now sufficient, no separate fetch,
rsync, or clone step before step 3. Confirm the paths resolve after pulling:

```bash
test -f external_workloads/ldbc/bi/neo4j/queries/bi-3.cypher && echo ok
test -f external_workloads/ldbc/interactive_v1/cypher/queries/interactive-short-1.cypher && echo ok
```

(Only query/schema/script text was vendored — no data files, no generated
CSVs, nothing over 1 MB. `initial_snapshot/` CSVs are still fetched fresh
per §4 for the actual import; their absence here changes nothing about the
run.)

**Do not edit any `.cypher` file.** The design memo's whole independence
argument (§1) is that these queries run unmodified; an edited query is an
edit to the reference, not a finding about TGIR.

---

## 2. Neo4j: version, install, config

### 2.1 Version pin: **Neo4j Community 5.26.0**

Two vendored trees name *different* Neo4j majors:

- `external_workloads/ldbc/bi/neo4j/scripts/vars.sh:9` —
  `export NEO4J_VERSION=${NEO4J_VERSION:-5.20.0}`. The BI importer also uses
  `neo4j-admin database import full` (`scripts/import.sh:67`), a 5.x-only
  subcommand name (4.x spells it `neo4j-admin import`).
- `external_workloads/ldbc/interactive_v1/cypher/scripts/vars.sh:8-9` —
  `NEO4J_DRIVER_VERSION=4.4.11`, `NEO4J_VERSION=4.4.24`.

These cannot both be satisfied by one running instance, and this run needs
exactly one instance (both BI and Interactive templates query the same
loaded SF1 snapshot). Resolution: **load and query on 5.26.0.** Reasons:

1. The substrate import needs `neo4j-admin database import full`, which
   requires a 5.x server — this is not optional, it is how the composite-fk
   CSVs get in at all (§4).
2. The Interactive queries used here (`interactive-short-{1..7}.cypher`,
   `interactive-complex-{2,5,6,8,9,11,12}.cypher`) were checked
   (`grep -il apoc`, this lane) and use no APOC calls and no
   version-gated syntax — plain `MATCH`/`OPTIONAL MATCH`/`ORDER BY`/`LIMIT`,
   portable across 4.4 and 5.x. The 4.4.24 pin in that tree's `vars.sh` is
   this vendor's own Docker default, not a documented syntax requirement.
3. **5.26.0 is already the validated pin on this exact host** for a
   different Neo4j instance (`docs/eval/SYSTEM_CONFIGS.md:12`,
   `docs/eval/REPRODUCE.md:33-37`: "Neo4j Community 5.26.0, tarball +
   Temurin JDK 21, no root"). Reusing the version (not the instance —
   see §2.3) means the install procedure is already proven on this host and
   `pyproject.toml`'s `neo4j>=5.20` Python driver pin already covers it.

**Do not reuse the existing Neo4j instance or its data directory.** That
instance carries TGMS's own `(:Entity)/(:NodeVersion)` schema
(`scripts/neo4j_baseline.py`) — the design memo (§1) is explicit that this
loader "is unusable here and must not be extended for this purpose", because
a reference built from TGMS's own event log would share TGMS's mapping code.
This run gets its own fresh install and its own data directory (§2.3).

### 2.2 Obtaining it without root

No root and no Docker are assumed available for this run (the design memo's
own BI import script defaults to Docker; that default is **not** used here —
see §4). Tarball install, same pattern as the existing pin:

```bash
mkdir -p /mnt/project/xzhang/neo4j
cd /mnt/project/xzhang/neo4j

# Temurin JDK 21 (Neo4j 5.26 requires Java 17 or 21; match the existing pin)
curl -sSLO https://github.com/adoptium/temurin21-binaries/releases/latest/download/OpenJDK21U-jdk_x64_linux_hotspot.tar.gz
tar xzf OpenJDK21U-jdk_x64_linux_hotspot.tar.gz
export JAVA_HOME=/mnt/project/xzhang/neo4j/jdk-21*
export PATH=$JAVA_HOME/bin:$PATH

# Neo4j Community 5.26.0
curl -sSLO https://dist.neo4j.org/deb/neo4j-community-5.26.0-unix.tar.gz
tar xzf neo4j-community-5.26.0-unix.tar.gz
export NEO4J_HOME=/mnt/project/xzhang/neo4j/neo4j-community-5.26.0

$NEO4J_HOME/bin/neo4j-admin dbms set-initial-password neo4j
```

The design memo's `neo4j-admin` invocations assume the Docker image's
`/import`/`/headers`/`/data` layout; with a tarball install those are just
`$NEO4J_HOME/import`, a `--headers` argument pointed straight at the vendored
`headers/` directory, and `$NEO4J_HOME/data` — no path translation needed.
`scripts/import.sh` is a Docker wrapper around one `neo4j-admin database
import full` invocation; §4 below gives the tarball-native equivalent
directly rather than wrapping the Docker script.

### 2.3 Config for a 93 GB host with nothing else running at that time

`$NEO4J_HOME/conf/neo4j.conf`:

```
server.directories.data=/mnt/project/xzhang/neo4j/data-ldbc-ref
server.directories.logs=/mnt/project/xzhang/neo4j/logs-ldbc-ref
server.bolt.listen_address=127.0.0.1:7687
dbms.security.auth_enabled=false
server.memory.heap.initial_size=8g
server.memory.heap.max_size=8g
server.memory.pagecache.size=16g
db.transaction.timeout=600s
```

`heap 8g` / `pagecache 16g` reuses the figure the design memo (§2) already
judged **adequate** for this exact scale ("SF1 is ~3M nodes / 17.2M
relationships... the store is a few GB"), which is also this host's existing
validated Neo4j pin (`docs/eval/SYSTEM_CONFIGS.md:12`). A separate data
directory (`data-ldbc-ref`, distinct from the existing eval instance's data
dir) keeps this run from touching that instance. The host has slack for more
if the import proves memory-bound (93 GB total, nothing else scheduled in
this window per the task brief) — raise `pagecache.size` before raising
`heap`, since page cache is what the bulk import and BI10's traversal spend
most of their memory on. `db.transaction.timeout=600s` is the per-query
ceiling (§7).

### 2.4 APOC (BI10's dependency)

`bi-10.cypher` calls `apoc.path.subgraphNodes` (checked: the only one of the
24 templates' queries that calls into APOC or GDS — `bi/neo4j/README.md:4`
names both libraries as sometimes-needed; a `grep -l "apoc\.\|gds\."` over
the ten BI query files this run touches finds it only in `bi-10.cypher`).
No root is needed to add a plugin to a tarball install:

```bash
curl -sSLO https://github.com/neo4j/apoc/releases/download/5.26.0/apoc-5.26.0-core.jar
cp apoc-5.26.0-core.jar $NEO4J_HOME/plugins/
echo 'dbms.security.procedures.unrestricted=apoc.*' >> $NEO4J_HOME/conf/neo4j.conf
```

Pin the APOC minor to the server minor (`5.26.0-core.jar` for a `5.26.0`
server) — APOC's own compatibility table refuses to load across a minor
mismatch, failing loudly at startup rather than silently, so this is
self-checking.

---

## 3. The 24 templates: ids, cypher files, plan artifacts, contracts

Recomputed from `tests/fixtures/ldbc_ref/contracts.json` and
`scripts/ldbc_reference_run.py`'s own `_default_cypher_name` /
`_IS_NUM` / `_IC_NUM` naming convention (BI: `bi-<n>.cypher`; IS:
`interactive-short-<n>.cypher`; IC: `interactive-complex-<n>.cypher`) —
this table is what `campaign.yaml`'s `templates:` block also carries, and
`tests/test_ldbc_ref_v1_freeze.py` pins it mechanically rather than by hand.

| id | vendored cypher | TGIR plan to run | contract |
|---|---|---|---|
| BI3  | `bi/neo4j/queries/bi-3.cypher`  | `BI3.json`  | REQUIRES_TOP_K |
| BI4  | `bi/neo4j/queries/bi-4.cypher`  | `BI4.json`  | REQUIRES_TOP_K |
| BI6  | `bi/neo4j/queries/bi-6.cypher`  | **`BI6.v2.json`** (not `BI6.json` — §6) | REQUIRES_TOP_K |
| BI7  | `bi/neo4j/queries/bi-7.cypher`  | `BI7.json`  | REQUIRES_TOP_K |
| BI9  | `bi/neo4j/queries/bi-9.cypher`  | `BI9.json`  | REQUIRES_TOP_K |
| BI10 | `bi/neo4j/queries/bi-10.cypher` | `BI10.json` | REQUIRES_TOP_K (needs APOC, §2.4) |
| BI11 | `bi/neo4j/queries/bi-11.cypher` | `BI11.json` | CURRENT_ECQR_FRAGMENT |
| BI12 | `bi/neo4j/queries/bi-12.cypher` | `BI12.json` | REQUIRES_ORDERED_RESULT |
| BI17 | `bi/neo4j/queries/bi-17.cypher` | `BI17.json` | REQUIRES_TOP_K |
| BI18 | `bi/neo4j/queries/bi-18.cypher` | `BI18.json` | REQUIRES_TOP_K |
| IC2  | `interactive_v1/cypher/queries/interactive-complex-2.cypher`  | `IC2.json`  | REQUIRES_TOP_K |
| IC5  | `interactive_v1/cypher/queries/interactive-complex-5.cypher`  | `IC5.json`  | REQUIRES_TOP_K |
| IC6  | `interactive_v1/cypher/queries/interactive-complex-6.cypher`  | `IC6.json`  | REQUIRES_TOP_K |
| IC8  | `interactive_v1/cypher/queries/interactive-complex-8.cypher`  | `IC8.json`  | REQUIRES_TOP_K |
| IC9  | `interactive_v1/cypher/queries/interactive-complex-9.cypher`  | `IC9.json`  | REQUIRES_TOP_K |
| IC11 | `interactive_v1/cypher/queries/interactive-complex-11.cypher` | `IC11.json` | REQUIRES_TOP_K |
| IC12 | `interactive_v1/cypher/queries/interactive-complex-12.cypher` | `IC12.json` | REQUIRES_TOP_K |
| IS1  | `interactive_v1/cypher/queries/interactive-short-1.cypher` | `IS1.json` | CURRENT_ECQR_FRAGMENT |
| IS2  | `interactive_v1/cypher/queries/interactive-short-2.cypher` | `IS2.json` | REQUIRES_TOP_K |
| IS3  | `interactive_v1/cypher/queries/interactive-short-3.cypher` | `IS3.json` | REQUIRES_ORDERED_RESULT |
| IS4  | `interactive_v1/cypher/queries/interactive-short-4.cypher` | `IS4.json` | CURRENT_ECQR_FRAGMENT |
| IS5  | `interactive_v1/cypher/queries/interactive-short-5.cypher` | `IS5.json` | CURRENT_ECQR_FRAGMENT |
| IS6  | `interactive_v1/cypher/queries/interactive-short-6.cypher` | `IS6.json` | CURRENT_ECQR_FRAGMENT |
| IS7  | `interactive_v1/cypher/queries/interactive-short-7.cypher` | `IS7.json` | REQUIRES_ORDERED_RESULT |

24 rows: 5 `CURRENT_ECQR_FRAGMENT`, 3 `REQUIRES_ORDERED_RESULT`,
16 `REQUIRES_TOP_K` — matches the design memo §0 and
`tests/test_ldbc_compare.py::test_the_24_expressible_split_matches_the_design_memo`.

### 3.1 Closed code gap: `IS1`/`IS4`/`IS5` are now in `ldbc_reference_run.py`'s naming table

`scripts/ldbc_reference_run.py`'s `_IS_NUM` used to read `{"IS2": 2, "IS3": 3,
"IS6": 6, "IS7": 7}` and did not carry `IS1`, `IS4`, or `IS5` — they were
added to the *parameter* binder (`scripts/ldbc_snb_params.py::IV_SOURCES`,
Lane D2, 2026-09-14) but the reference runner's filename table was not
updated to match at the time. Calling `run_all()` for those three ids raised
`KeyError: no known vendored filename convention for 'IS1'`
(`_default_cypher_name`, `ldbc_reference_run.py:201`).

Commit `8b46159` (2026-09-14, lane D1-fix, "ldbc: close the IS1/IS4/IS5
_IS_NUM gap, add a mechanical sort_keys generator (Claim C9)") closed this
gap on `main`. `_IS_NUM` is now `{"IS1": 1, "IS2": 2, "IS3": 3, "IS4": 4,
"IS5": 5, "IS6": 6, "IS7": 7}` — all seven `IS` ids resolve. No pre-flight
patch to `_IS_NUM` and no `cypher_name=` override is needed any more before
running step 5.3.

---

## 4. Loading the SF1 BI snapshot

### 4.1 Fetch `composite-projected-fk`, not `composite-merged-fk`

The BI importer needs `composite-projected-fk` layout (design memo §2,
option A — recommended): the merged-fk layout this repo already has at
`/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/...`
(`benchmarks/results-v1/ldbc-sf1-campaign.json`'s `csv_root`) is what TGMS's
own loader (`tgms/data/snb_loader.py`) reads, and re-deriving Neo4j's
CSV shape from it would put ~200 lines of this project's own code between
LDBC's data and LDBC's queries — reimporting the M3 judgement call into the
reference (design memo §2, option B, the fallback only).

```bash
mkdir -p /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk
cd /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk
curl -sSLO https://datasets.ldbcouncil.org/bi-pre-audit/bi-sf1-composite-projected-fk.tar.zst
curl -sSLO https://datasets.ldbcouncil.org/bi-pre-audit/bi-composite-projected-fk-md5sums.tar.zst
zstd -d bi-sf1-composite-projected-fk.tar.zst -o bi-sf1-composite-projected-fk.tar
tar xf bi-sf1-composite-projected-fk.tar
tar xf bi-composite-projected-fk-md5sums.tar.zst   # md5sums.txt lands alongside
md5sum -c md5sums.txt --ignore-missing            # abort the whole run on any FAIL
```

(URLs from `external_workloads/ldbc/bi/snb-bi-pre-generated-data-sets.md:41-61`
via the D1 design memo's own citation, §2.)

### 4.2 The same-data gate (mandatory, before any query runs)

Both `composite-merged-fk` (already on disk, the source TGMS itself was
loaded from) and `composite-projected-fk` (just fetched) are the *same*
generated SF1 dataset in two serializations. Prove that before trusting a
single comparison — design memo §2, "any mismatch aborts":

```bash
uv run python scripts/build_snb_store.py --csv-root \
    /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk/graphs/csv/bi/composite-projected-fk/initial_snapshot \
    --dry-run-fidelity   # per-node-type count + sorted-id-list sha256; per-rel-type count
```

Use `tgms.data.snb_loader.fidelity()`/`store_label_counts()` (already used
by the existing SF1 loader path) against both CSV roots and diff the two
reports. Required to match: **2,997,352 nodes / 17,196,776 edges**
(design memo §2, citing `PAPER_A_EVIDENCE_FREEZE.md` §E addendum 2). **Any
mismatch aborts the whole campaign** — the two sides would no longer be
reading the same graph, and every subsequent comparison would be
meaningless. Record the gate's own output (both per-type counts and the two
sha256 digests) under `benchmarks/ldbc-ref-v1/same-data-gate.json` before
proceeding; this file's own digest is what `dataset.digest` (§9) points at.

### 4.3 CSV import: which files, which M-rules apply where

This is a **static snapshot import, no update streams**, per M8
(design memo §1, `PAPER_A_EVIDENCE_FREEZE.md:117-118`). Only
`initial_snapshot/` is ever read:

```bash
$NEO4J_HOME/bin/neo4j-admin database import full \
    --id-type=INTEGER --ignore-empty-strings=true --bad-tolerance=0 \
    --delimiter '|' \
    --nodes=Place="<headers>/static/Place.csv,<csv>/initial_snapshot/static/Place/part-*.csv" \
    --nodes=Organisation="<headers>/static/Organisation.csv,<csv>/initial_snapshot/static/Organisation/part-*.csv" \
    --nodes=TagClass="<headers>/static/TagClass.csv,<csv>/initial_snapshot/static/TagClass/part-*.csv" \
    --nodes=Tag="<headers>/static/Tag.csv,<csv>/initial_snapshot/static/Tag/part-*.csv" \
    --nodes=Forum="<headers>/dynamic/Forum.csv,<csv>/initial_snapshot/dynamic/Forum/part-*.csv" \
    --nodes=Person="<headers>/dynamic/Person.csv,<csv>/initial_snapshot/dynamic/Person/part-*.csv" \
    --nodes=Message:Comment="<headers>/dynamic/Comment.csv,<csv>/initial_snapshot/dynamic/Comment/part-*.csv" \
    --nodes=Message:Post="<headers>/dynamic/Post.csv,<csv>/initial_snapshot/dynamic/Post/part-*.csv" \
    --relationships=IS_PART_OF="<headers>/static/Place_isPartOf_Place.csv,<csv>/initial_snapshot/static/Place_isPartOf_Place/part-*.csv" \
    --relationships=IS_SUBCLASS_OF="<headers>/static/TagClass_isSubclassOf_TagClass.csv,..." \
    --relationships=IS_LOCATED_IN="<headers>/static/Organisation_isLocatedIn_Place.csv,..." \
    --relationships=HAS_TYPE="<headers>/static/Tag_hasType_TagClass.csv,..." \
    --relationships=HAS_CREATOR="<headers>/dynamic/Comment_hasCreator_Person.csv,..." \
    --relationships=IS_LOCATED_IN="<headers>/dynamic/Comment_isLocatedIn_Country.csv,..." \
    --relationships=REPLY_OF="<headers>/dynamic/Comment_replyOf_Comment.csv,..." \
    --relationships=REPLY_OF="<headers>/dynamic/Comment_replyOf_Post.csv,..." \
    --relationships=CONTAINER_OF="<headers>/dynamic/Forum_containerOf_Post.csv,..." \
    --relationships=HAS_MEMBER="<headers>/dynamic/Forum_hasMember_Person.csv,..." \
    --relationships=HAS_MODERATOR="<headers>/dynamic/Forum_hasModerator_Person.csv,..." \
    --relationships=HAS_TAG="<headers>/dynamic/Forum_hasTag_Tag.csv,..." \
    --relationships=HAS_INTEREST="<headers>/dynamic/Person_hasInterest_Tag.csv,..." \
    --relationships=IS_LOCATED_IN="<headers>/dynamic/Person_isLocatedIn_City.csv,..." \
    --relationships=KNOWS="<headers>/dynamic/Person_knows_Person.csv,..." \
    --relationships=LIKES="<headers>/dynamic/Person_likes_Comment.csv,..." \
    --relationships=LIKES="<headers>/dynamic/Person_likes_Post.csv,..." \
    --relationships=HAS_CREATOR="<headers>/dynamic/Post_hasCreator_Person.csv,..." \
    --relationships=HAS_TAG="<headers>/dynamic/Comment_hasTag_Tag.csv,..." \
    --relationships=HAS_TAG="<headers>/dynamic/Post_hasTag_Tag.csv,..." \
    --relationships=IS_LOCATED_IN="<headers>/dynamic/Post_isLocatedIn_Country.csv,..." \
    --relationships=STUDY_AT="<headers>/dynamic/Person_studyAt_University.csv,..." \
    --relationships=WORK_AT="<headers>/dynamic/Person_workAt_Company.csv,..." \
    2>&1 | tee /mnt/project/xzhang/neo4j/import.log
sha256sum /mnt/project/xzhang/neo4j/import.log   # this is the "import log digest" (§9)
```

(Node/relationship list transcribed verbatim from
`external_workloads/ldbc/bi/neo4j/scripts/import.sh:67-97` with the Docker
path prefixes and `find`-based file globbing removed for the tarball
install — `<headers>` is the vendored `bi/neo4j/headers/` directory,
`<csv>` is `.../bi-sf1-composite-projected-fk/graphs/csv/bi/composite-projected-fk`.
**Do not fork `import.sh`** — this is the same command it issues, run
directly against a tarball server instead of inside Docker.)

Then indices (design memo §2, "a further 5-15 min"):

```bash
$NEO4J_HOME/bin/neo4j start
cat external_workloads/ldbc/bi/neo4j/ddl/indices.cypher | $NEO4J_HOME/bin/cypher-shell -u neo4j -p neo4j
```

**Mapping rules by phase** (design memo §1's table, sorted into "acts at
import" vs "acts at compare/query time" — nothing here is a new rule, this
is the existing table read for *when* each line fires):

| rule | acts at | what happens |
|---|---|---|
| **M8** (static snapshot) | **import** | only `initial_snapshot/` is ever passed to `neo4j-admin`; `deletes/`/`inserts/` directories are never referenced by the command above — record that they were not touched, alongside the import log |
| **M6** (no materialized `Message` label) | **import** | the compound labels `--nodes=Message:Comment=...` / `--nodes=Message:Post=...` are exactly how Neo4j's `(:Message)` pattern comes to match both hierarchies — no separate step |
| **M3** (8 FK-merged edge types carry no `creationDate`) | **import** (absence) + **query time** (assertion) | those 8 relationship types' header files carry no `creationDate` column by construction; before running any query, `grep -L creationDate` the 8 corresponding files under `bi/neo4j/headers/{static,dynamic}/` and confirm none of the 24 vendored `.cypher` files reference `.creationDate` on `HAS_CREATOR`/`IS_LOCATED_IN`/`REPLY_OF`/`CONTAINER_OF`/`HAS_MODERATOR`/`HAS_TAG`/`HAS_TYPE`/`IS_SUBCLASS_OF`/`IS_PART_OF` |
| **M4** (classYear/workFrom are ints) | **import** | the CSV headers already type them as plain ints; nothing to convert |
| **M7** (KNOWS undirected) | **query time** | the vendored Cypher's `-[:KNOWS]-` pattern is already undirected; nothing to do at import, but any KNOWS-count doubling in a result is a defect to report, not a normalization to apply |
| **M5/A4** (`uid = ldbc_id*8 + tag`) | **compare time only** | Neo4j never sees an encoded uid — it is loaded with raw LDBC ids throughout. `ldbc_compare.py`'s `uid` column kind decodes the *TGMS* side before comparing; the reference side is already correct |
| **unit** (µs vs ms) | **compare time only** | `ldbc_compare.py`'s `ts` column kind multiplies the reference side by 1000; nothing to do at import or query time |
| **M10** (untruncated strings) | **compare time only** | NFC-normalized, byte comparison; nothing at import |
| **M1/M2/M9/M11/M12** | **not applicable to Neo4j** | these are TGIR-side binding/replay rules (`sigma` window, replay order, frozen params); the Neo4j side has no analogue — a static snapshot has no replay order and no `sigma` |

---

## 5. Running the 24 templates

### 5.1 Parameters — one export, both sides

```bash
uv run python scripts/ldbc_snb_params.py --params <ldbc-params-root> --sf sf1 --plan all \
    > /dev/null   # writes benchmarks/ldbc-ref-v1/params.json via export_bindings()
```

`<ldbc-params-root>` is the extracted
`ldbc-snb-bi-parameters-sf1-to-sf30000.zip` (BI rows) plus
`validation_params-sf1.csv` (Interactive rows) — both named in
`external_workloads/ldbc/bi/snb-bi-pre-generated-data-sets.md:31`.
This binds the **same** first-tuple-in-file-order parameters
(`scripts/ldbc_snb_params.py`'s `BI_SOURCES`/`IV_SOURCES`, campaign seed
`5724984519806421702`) the D1-local SF1 campaign already used
(`benchmarks/results-v1/ldbc-sf1-campaign.json` manifest
`campaign_seed`), so a disagreement can never be attributed to a parameter
drawn differently between the two runs being compared.

Add the `BI6.v2` row before running (already present in this checkout's
`scripts/ldbc_snb_params.py::BI_SOURCES`, confirmed:
`"BI6.v2": ("bi-6", {"tagName": "tag"})` — binds to the same source file,
column, and first-tuple rule as `BI6`, per `docs/design/BI6_TRIAGE_2026-09-14.md`).

### 5.2 TGMS side (`--emit-rows`)

```bash
uv run python scripts/tgir_ldbc_sf1.py \
    --store stores/snb-sf1 --params benchmarks/ldbc-ref-v1/params.json \
    --sf sf1 --plan all \
    --emit-rows benchmarks/ldbc-ref-v1/tgms-rows \
    --out benchmarks/ldbc-ref-v1/tgms-campaign.json
```

This reuses `scripts/tgir_ldbc_sf1.py` with the guardrail bypass on (9 of
the 10 BI rows needed it at D1-local scale, design memo §5) and
`--emit-rows` writing `tgms-<ID>.json` per plan (design memo §6, item 4).
Also run `BI6.json` on its own (`--plan BI6`) alongside `BI6.v2` — keep both:
`BI6.json`'s record is the evidence the v1 defect still reproduces at SF1
(it should still `ERRORED`, per the BI6 triage §7: "the v1 record stands");
`BI6.v2.json`'s record (`--plan BI6.v2`) is the one compared against the
reference in §6.

### 5.3 Reference side (Neo4j, verbatim)

```bash
uv run --extra eval python scripts/ldbc_reference_run.py \
    --params benchmarks/ldbc-ref-v1/params.json \
    --cypher-dir external_workloads/ldbc/bi/neo4j/queries \
    --out benchmarks/ldbc-ref-v1/ref-rows \
    --plan BI3 --plan BI4 --plan BI6 --plan BI7 --plan BI9 --plan BI10 \
    --plan BI11 --plan BI12 --plan BI17 --plan BI18

uv run --extra eval python scripts/ldbc_reference_run.py \
    --params benchmarks/ldbc-ref-v1/params.json \
    --cypher-dir external_workloads/ldbc/interactive_v1/cypher/queries \
    --out benchmarks/ldbc-ref-v1/ref-rows \
    --plan IC2 --plan IC5 --plan IC6 --plan IC8 --plan IC9 --plan IC11 --plan IC12 \
    --plan IS1 --plan IS2 --plan IS3 --plan IS4 --plan IS5 --plan IS6 --plan IS7
```

Two invocations because BI and Interactive queries live in different
vendored directories and `--cypher-dir` is one directory per run. The
second invocation runs as-is — §3.1 is closed, so `IS1`/`IS4`/`IS5` no
longer raise `KeyError`.

`ref-BI6.json` (from the first invocation) is the reference answer for the
`BI6` LDBC template — the same one `BI6.v2.json`'s TGIR output is compared
against (§6). The `--uri`/`--user`/`--password` flags default to
`bolt://127.0.0.1:7687` / `neo4j`/`neo4j` (auth is off per §2.3, but the
bolt handshake still wants a credential pair — `ldbc_reference_run.py`'s own
comment on `DEFAULT_AUTH`).

---

## 6. BI6 / BI6.v2: comparing the repaired plan against the one reference answer

`ldbc_compare.py` matches files by plan id (`tgms-<pid>.json` against
`ref-<pid>.json`). The reference side only ever runs under the id `BI6`
(that is the only id `_default_cypher_name` resolves to `bi-6.cypher`;
§3.1's `_IS_NUM` fix does not touch the `BI` table, and `BI6` has no
`BI6.v2` special case there either — it does not need one).
Before comparing, make the reference file available under both names —
it is the same query, same parameters, same answer either way:

```bash
cp benchmarks/ldbc-ref-v1/ref-rows/ref-BI6.json benchmarks/ldbc-ref-v1/ref-rows/ref-BI6.v2.json
```

Then compare `BI6.v2` (not `BI6`) as the template's row in the final
report — `BI6`'s own comparison (`tgms-BI6.json`, which should still show
`ERRORED`/no rows) is kept as a second, explicitly-labelled row showing the
v1 defect did not silently vanish. `campaign.yaml`'s `templates:` list
carries `BI6` with `plan_artifact: BI6.v2.json` for exactly this reason —
report the repaired plan as the id's row, and keep the v1 error as a footnote
alongside it, not the other way around.

---

## 7. Timeouts

`db.transaction.timeout=600s` (§2.3) matches the TGMS-side
`bypass_ceiling_s: 600` already used for the D1-local SF1 campaign
(`benchmarks/results-v1/ldbc-sf1-campaign.json` manifest) — same ceiling on
both sides, so a "the reference took too long" outcome is symmetric with
"TGMS refused/bypassed", not an artifact of one side being given more
rope. BI10 (APOC subgraph traversal, unbounded by any LIMIT below the final
one) is the template most likely to approach it; if it times out, record the
timeout itself as the row's outcome (`compared: 0`, note: "reference-side
timeout at 600s") — never raise the ceiling mid-run to make one row finish.

---

## 8. Compare invocation and what it produces

```bash
uv run python scripts/ldbc_compare.py \
    --tgms-dir benchmarks/ldbc-ref-v1/tgms-rows \
    --ref-dir benchmarks/ldbc-ref-v1/ref-rows \
    --contracts tests/fixtures/ldbc_ref/contracts.json \
    --sort-keys benchmarks/ldbc-ref-v1/sort_keys.yaml \
    --plan BI3 --plan BI4 --plan BI6.v2 --plan BI7 --plan BI9 --plan BI10 \
    --plan BI11 --plan BI12 --plan BI17 --plan BI18 \
    --plan IC2 --plan IC5 --plan IC6 --plan IC8 --plan IC9 --plan IC11 --plan IC12 \
    --plan IS1 --plan IS2 --plan IS3 --plan IS4 --plan IS5 --plan IS6 --plan IS7 \
    --out benchmarks/ldbc-ref-v1/compare-<date>.json
```

Produces `compare-<date>.json` (+ `.md`) with, per template, **never a
single ratio** (design memo §4): `attempted / bound / both_sides_completed /
compared / agreeing / tie_ambiguous / disagreeing`, plus one
`{cause, detail}` per disagreeing row drawn from the closed set
`MAPPING-RULE / ORDERING/TIE / TGIR-SEMANTIC-GAP / ENGINE-DEFECT /
REFERENCE-SIDE-QUIRK`, or `UNTRIAGED` if none fits.

**Tie-break policy** (design memo §4, `ldbc_compare.py`'s own
`compare_topk`): for the 16 `REQUIRES_TOP_K` templates, compare the top-k
**set** after the template's own `ORDER BY`; if the boundary row's sort key
ties across both sides, that row is `TIE-AMBIGUOUS` — reported as its own
outcome, neither agreement nor disagreement. This requires `sort_keys` per
plan; that table is now generated, not hand-built (lane D1-fix, 2026-09-14):
`benchmarks/ldbc-ref-v1/sort_keys.yaml`, produced from the vendored `.cypher`
files' own final `ORDER BY` clauses by `scripts/ldbc_sort_keys.py` (re-run it
if the vendored trees are ever re-pinned to a newer commit — it is
mechanical, not a one-time hand transcription), and `--sort-keys` above
passes it straight to `ldbc_compare.py`. It is keyed by the 24 LDBC template
ids (`BI6`, not `BI6.v2` — `lookup_sort_keys` strips the `.v2` suffix before
looking up, since the sort key is a property of the query, answered by
either plan artifact). One entry, `IC5`, carries an `unmapped: true` column
(`forum.id`, which that query's `ORDER BY` sorts by but never projects) — a
named limitation, not silently guessed at; any resulting top-k residual on
that boundary is reported `UNTRIAGED` rather than misclassified as a tie.

For the 3 `REQUIRES_ORDERED_RESULT` templates (BI12, IS3, IS7): compared as
sequences; a positional mismatch where the multisets still agree is
`ORDERING/TIE`. For the 5 `CURRENT_ECQR_FRAGMENT` templates: exact multiset
equality, no top-k allowance.

---

## 9. What to record, and the manifest layout

Record, per template: Neo4j version (5.26.0, fixed for the whole run),
Neo4j config (§2.3's `neo4j.conf`, unmodified — record its sha256), the
import log digest (§4.3, `sha256sum import.log`), and per-template wall
time (`ldbc_reference_run.py`'s own `wall_s` field in each `ref-<ID>.json`,
already emitted).

None of the repo's existing LDBC-related JSON files validate against
`benchmarks/schema/result_manifest.schema.json` today
(`benchmarks/schema/README.md`'s audit: 157/157 fail, including
`ldbc-sf1-campaign.json` itself) — that schema was written to apply going
forward, not to retrofit old records. This run's own top-level manifest
**should** validate, since nothing forces the ad hoc shape and the schema
is cheap to satisfy. Write it as
`benchmarks/ldbc-ref-v1/manifest-<date>.json`:

```json
{
  "schema_version": "1.0.0",
  "git_commit": "<git rev-parse --short=12 HEAD, on the xzgpu checkout>",
  "timestamp_utc": "<run start, ISO 8601>",
  "machine": {"host": "xzgpu", "platform": "<uname -a equivalent>",
              "cpus": 40, "ram_gb": 93},
  "config": {
    "neo4j_version": "5.26.0",
    "neo4j_conf_sha256": "<sha256 of the neo4j.conf actually used>",
    "apoc_version": "5.26.0-core",
    "params_file": "benchmarks/ldbc-ref-v1/params.json",
    "contracts_file": "tests/fixtures/ldbc_ref/contracts.json"
  },
  "seed": {"value": 5724984519806421702, "reason": null},
  "dataset": {
    "name": "ldbc-sf1-bi-composite-projected-fk",
    "digest": "<sha256 recorded by the same-data gate, benchmarks/ldbc-ref-v1/same-data-gate.json>",
    "digest_kind": "manifest"
  },
  "result_digest": "<sha256 over the sorted verdicts list from compare-<date>.json>",
  "protocol": {"warmups": 0, "reps": 1,
               "ceilings": {"query_timeout_s": 600}},
  "record": "benchmarks/ldbc-ref-v1/compare-<date>.json"
}
```

`protocol.reps: 1` because this is a correctness comparison, not a timing
run — one execution per side per template, not repeated reps (contrast
with the six-system timing harness's 5 warmups / 30 reps,
`docs/eval/REPRODUCE.md`, which is a different kind of measurement).
Validate before calling the run complete:

```bash
uv run python scripts/check_result_manifest.py benchmarks/ldbc-ref-v1/manifest-<date>.json
```

---

## 10. Launch hygiene (long-running, on a shared host)

- **One working copy.** Do not run this alongside another checkout's
  long-running job on the same host (`docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md`:
  a prior lane's relaunch-in-place habit cost 38 GB deleted twice). Use one
  dedicated worktree for this run and nothing else in it at the same time.
- **`TMPDIR`.** Set `TMPDIR=/mnt/project/xzhang/tmp` (or another
  project-local, not-`/tmp`, disk with room for the import's scratch files)
  before the import — the default `/tmp` on a shared host is small and
  shared across every user's session.
- **No `pgrep -f` as launch evidence.** Per `docs/eval/REPRODUCE.md:82-85`:
  launch under `nohup` from a script file that prints `RUN_STARTED
  commit=<sha>` into its own log first, never an inline `nohup` on the ssh
  command line, and never kill-and-relaunch in one ssh call.
- **Background with `nohup`, short ssh calls.** e.g.:

  ```bash
  nohup bash -c '
    echo "RUN_STARTED commit=$(git rev-parse --short=12 HEAD) $(date -u +%FT%TZ)"
    bash run_ldbc_ref_campaign.sh
  ' > /mnt/project/xzhang/neo4j/ldbc-ref-run.log 2>&1 &
  disown
  ```

  then reconnect later with a short `ssh xzgpu 'tail -n 40
  /mnt/project/xzhang/neo4j/ldbc-ref-run.log'` rather than holding the
  session open across the whole two-day window.
- **Stop Neo4j between phases.** The design memo (§2) is explicit: "Neo4j is
  normally stopped and must be stopped between campaigns" — a resident JVM
  would corrupt any timing measurement sharing the window, even though this
  run's own protocol is not itself timing-sensitive (§9), other lanes on the
  same host may be.

---

## 11. Budget (all figures **estimated**, from the design memo §5 and the D1-local record; none measured by this lane)

| step | estimate |
|---|---|
| download + decompress `composite-projected-fk` SF1 | 1-2 h |
| same-data gate | 30 min |
| `neo4j-admin database import full` + indices | 0.5-1 h |
| 24 templates × one Cypher execution each (no reps, §9) | 1-2 h; BI-class queries at SF1 on Neo4j run seconds to minutes per the design memo, and this run does not repeat warmups |
| TGMS side, `--emit-rows`, guard bypassed | ~1.5 h (D1-local campaign wall was 4233 s for 21/24; `benchmarks/results-v1/ldbc-sf1-campaign.json` manifest `wall_s`) |
| compare + triage (`sort_keys.yaml` is generated, §8 — this no longer includes building it by hand) | 0.5-1 h |
| **total** | **~1.5-2 working days with slack** |

Disk: **~25 GB estimated** (design memo §2) — CSVs (`composite-projected-fk`,
compressed + decompressed) plus the Neo4j store (a few GB) plus row exports
(JSON, low hundreds of MB at SF1's per-template row counts, per the D1-local
record). Clean up the decompressed CSV tree after a successful same-data
gate and import if disk pressure appears; keep the compressed `.tar.zst`
and the import log until the manifest (§9) is written and checked in.

---

## 12. Order of operations (checklist)

1. §1.1 — confirm both vendored trees (now on `main`) are present at their pinned commits (`git pull` is enough).
2. §3.1 — confirm the checkout includes `8b46159` so `_IS_NUM` carries all
   seven `IS` ids before any Interactive reference run.
3. §2 — install Neo4j 5.26.0 + APOC, write `neo4j.conf`, do **not** start
   it against the existing eval instance's data directory.
4. §4.1-4.2 — fetch `composite-projected-fk`, run the same-data gate,
   **abort on any mismatch**.
5. §4.3 — import, then indices; keep the import log and its sha256.
6. §5.1 — export `params.json` (one binding, both sides).
7. §5.2 — run the TGMS side with `--emit-rows` (`BI6.json` and
   `BI6.v2.json` both).
8. §5.3 — run the Neo4j side (two `--cypher-dir` invocations).
9. §6 — duplicate `ref-BI6.json` to `ref-BI6.v2.json`.
10. §8 — run `ldbc_compare.py` with `--sort-keys benchmarks/ldbc-ref-v1/sort_keys.yaml`
    (already generated and checked in; re-run `scripts/ldbc_sort_keys.py` only
    if the vendored trees are ever re-pinned).
11. §9 — write and validate `manifest-<date>.json`.
12. Compare the result against `campaign.yaml`'s pre-registered predictions
    and gates; report PASS/REFUTED per the frozen bar, not a new one chosen
    after seeing the numbers.
