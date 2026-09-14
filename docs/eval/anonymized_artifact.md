# Anonymized artifact bundle (`scripts/anonymize_artifact.py`)

Committed benchmark records and READMEs carry real provenance: cluster
hostnames, the university domain, the Slurm partition name, the GitHub
organisation, and the PI's identity. By ruling, records keep that
provenance in the repository — a double-blind submission ships a separate,
scrubbed *copy* instead. `scripts/anonymize_artifact.py` builds that copy.

## Usage

```sh
# Build the bundle
scripts/anonymize_artifact.py --src . --out /tmp/tgms-artifact

# Preview only: print per-file, per-pattern counts, write nothing
scripts/anonymize_artifact.py --src . --out /tmp/tgms-artifact --dry-run

# Pull in a file that's excluded by default (e.g. the failure ledger)
scripts/anonymize_artifact.py --src . --out /tmp/tgms-artifact \
    --include ops/failure_ledger.jsonl

# Name the manifest file explicitly (default: bundle-manifest.json)
scripts/anonymize_artifact.py --src . --out /tmp/tgms-artifact \
    --manifest manifest.json
```

Default include set: `benchmarks/**/*.json`, `benchmarks/**/*.jsonl`,
`benchmarks/**/*.md`, `benchmarks/**/*.yaml`, `benchmarks/schema/**`,
`docs/system_invariants.md`, `docs/STABILITY.md`, `docs/eval/*.md`,
`scripts/osdi_paper_macros.py`, `scripts/osdi_paper_figures.py`,
`README.md`, `LICENSE`. Always excluded, regardless of `--include`:
`docs/design/**`, `paper/**`, `docs/DECISIONS.md`, `.git`, anything
gitignored. `ops/failure_ledger.jsonl` is excluded unless named explicitly
via `--include`.

Non-dry-run mode writes the bundle, a `bundle-README.md` explaining that
digests were not recomputed, and a manifest recording every file's
before/after sha256 and a per-pattern replacement count (never the
original matched text). It then re-scans the *entire* written bundle —
file content, file and directory names, and the manifest itself, with no
exclusions — for every table pattern plus a short list of hard-coded
sentinel substrings drawn from the same real identifiers the table
targets, and exits non-zero if anything survived.

## Replacement table

Applied to whole file text for `.md`/`.py`/`.yaml`/`README.md`/`LICENSE`;
to JSON string values and to JSON object keys for `.json`/`.jsonl`
(walking the parsed structure so a match nested inside a longer string —
e.g. a kernel `platform` string — still lands); and to each file's own
relative path, so a directory or file name that matches gets renamed
inside the bundle. A key or number that matches nothing is left exactly
as it was — this table only ever *replaces a match*, never restructures
around one. Longer/more specific patterns run before shorter/more general
ones so a substring one rule consumes can't also trip a later rule.

| pattern | what it targets |
|---|---|
| `cluster_login_fqdn` | the compute cluster's login-node fully-qualified domain name |
| `cluster_node_name` | cluster compute-node names (a letters prefix + 2-digit number), fused into longer identifiers or not |
| `cluster_name` | the cluster's own name, bare, in prose or fused into a longer identifier |
| `gpu_server_fqdn` | the GPU server's fully-qualified domain name |
| `gpu_server_host` | the GPU server's bare hostname |
| `personal_workstation` | the PI's personal machine hostname |
| `slurm_partition` | the Slurm partition name |
| `github_identity` | the GitHub organisation, and, where paired in a URL, the repo |
| `pi_full_name` | the PI's full name |
| `pi_first_name` | the PI's given name, standing alone |
| `pi_email` | the PI's email address |
| `github_noreply_email` | the PI's GitHub no-reply commit-author email |
| `github_handle` | the PI's GitHub handle |
| `university_domain` | the university's web/email domain, any remaining bare mention |
| `university_name` | the university's name in prose |
| `path_project_cluster_user` | the cluster username's home directory under `/project` |
| `path_home_cluster_user` | the cluster username's home directory under `/home` |
| `path_mnt_project_gpu_user` | the GPU server username's home directory under `/mnt/project` |
| `path_users_pi_laptop` | the PI's laptop home directory |
| `user_token_cluster_user` | the cluster username, outside a `/project` or `/home` path |
| `user_token_gpu_user` | the GPU server username, outside a `/mnt/project` path |

Every pattern above was built from the real repo — grepped, not guessed
(see the task's own report for what was found and where).

### Renamed files and rewritten keys

A file whose relative path (directory component or basename) matches one
of the patterns above is renamed inside the bundle — the manifest's
`renames` list records the new path plus a sha256 of the *original*
relative path (never the original path text: an author holding the real
tree can hash each candidate original name and match it against the
recorded digest, without the manifest itself ever carrying anything a
reviewer could read back). A JSON object key that matches one of the
patterns is rewritten the same way and logged under `rewritten_keys`
(file, new key, and a sha256 of the original key text). Everything that
doesn't match — the overwhelming majority of keys, and every number — is
left untouched.

## Verification

The final pass re-scans everything the bundle contains — every file's
content, every file's and directory's name, and the manifest and
`bundle-README.md` themselves — for every pattern in the table above, plus
a short list of hard-coded sentinel substrings kept independent of the
table (so verification doesn't depend on the table being complete or
correctly ordered). Nothing is exempted: because renaming and key-rewriting
are real operations done before the manifest is written, the manifest's
own content is expected to come up exactly as clean as everything else.

## Dry-run summary (2026-09-14, this worktree)

`scripts/anonymize_artifact.py --src . --out /tmp/… --dry-run`: 347 files
scanned. Counts per pattern, per top-level directory (originals never
shown — only counts):

| directory | pattern | count |
|---|---|---:|
| `README.md` | `github_identity` | 4 |
| `benchmarks/` | `cluster_name` | 44 |
| `benchmarks/` | `cluster_node_name` | 954 |
| `benchmarks/` | `gpu_server_host` | 89 |
| `benchmarks/` | `path_mnt_project_gpu_user` | 49 |
| `benchmarks/` | `path_project_cluster_user` | 144 |
| `benchmarks/` | `path_users_pi_laptop` | 3 |
| `benchmarks/` | `personal_workstation` | 8 |
| `benchmarks/` | `slurm_partition` | 8 |
| `docs/` | `cluster_name` | 7 |
| `docs/` | `gpu_server_host` | 3 |
| `docs/` | `slurm_partition` | 1 |
| `scripts/` | `gpu_server_host` | 1 |

Totals (content + keys): `cluster_name` 51, `cluster_node_name` 954,
`github_identity` 4, `gpu_server_host` 93, `path_mnt_project_gpu_user` 49,
`path_project_cluster_user` 144, `path_users_pi_laptop` 3,
`personal_workstation` 8, `slurm_partition` 9.

Path (file/directory name) components: 2 files renamed, both under
`benchmarks/results-v1/`, both via `cluster_name` (2 hits). JSON keys
rewritten: 6, all in one file under `benchmarks/results-v1/` (4 via
`cluster_name`, 2 via `gpu_server_host`).

A real (non-dry-run) build of this worktree wrote 347 files and its
verification pass came back clean: zero survivors, scanning file content,
file and directory names, and the manifest itself, for every table
pattern and for the sentinel list (a case-insensitive substring check for
each of: the cluster's login domain, the cluster's own name, the GPU
server's hostname and domain, the Slurm partition name, the GitHub
organisation, and the PI's given name, GitHub handle, and email prefix).

## What this script cannot fix mechanically

- **Prose that describes the cluster's shape without naming it.** GPU
  counts, quota sizes, node counts, and similar numbers in `docs/eval/*.md`
  are left alone even where they're suggestive, because they're ordinary
  numbers this table can't pattern-match without risking collateral damage
  to unrelated figures.
- **Two different real names that anonymize to the identical string.**
  `dedupe_path` disambiguates a bundled-path collision deterministically
  (a short hash of the original path is appended), so two files never
  silently overwrite each other, but the resulting bundled name is then a
  hash fragment rather than something meaningful on its own — expected to
  be rare in practice (no such collision occurs in this repo today).
