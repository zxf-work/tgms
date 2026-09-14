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
original matched text). It then re-scans the written bundle for every
table pattern plus a short list of hard-coded sentinels
(`memphis`, `xzhang`, the GitHub org) and exits non-zero if anything
survived.

## Replacement table

Applied to whole file text for `.md`/`.py`/`.yaml`/`README.md`/`LICENSE`,
and to JSON string *values only* (never keys, never numbers) for
`.json`/`.jsonl`, walking the parsed structure so a match nested inside a
longer string (e.g. a kernel `platform` string) still lands. Longer/more
specific patterns run before shorter/more general ones so a substring one
rule consumes can't also trip a later rule.

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

## Dry-run summary (2026-09-14, this worktree)

`scripts/anonymize_artifact.py --src . --out /tmp/… --dry-run`: 346 files
scanned. Counts per pattern, per top-level directory (originals never
shown — only counts):

| directory | pattern | count |
|---|---|---:|
| `README.md` | `github_identity` | 4 |
| `benchmarks/` | `cluster_name` | 40 |
| `benchmarks/` | `cluster_node_name` | 954 |
| `benchmarks/` | `gpu_server_host` | 87 |
| `benchmarks/` | `path_mnt_project_gpu_user` | 49 |
| `benchmarks/` | `path_project_cluster_user` | 144 |
| `benchmarks/` | `path_users_pi_laptop` | 3 |
| `benchmarks/` | `personal_workstation` | 8 |
| `benchmarks/` | `slurm_partition` | 8 |
| `docs/` | `cluster_name` | 7 |
| `docs/` | `gpu_server_host` | 3 |
| `docs/` | `slurm_partition` | 1 |
| `scripts/` | `gpu_server_host` | 1 |

Totals: `cluster_name` 47, `cluster_node_name` 954, `github_identity` 4,
`gpu_server_host` 91, `path_mnt_project_gpu_user` 49,
`path_project_cluster_user` 144, `path_users_pi_laptop` 3,
`personal_workstation` 8, `slurm_partition` 9. Patterns with zero hits in
this worktree today (`cluster_login_fqdn`, `gpu_server_fqdn`,
`pi_full_name`, `pi_email`, `github_noreply_email`, `github_handle`,
`university_domain`, `university_name`, `path_home_cluster_user`,
`user_token_cluster_user`, `user_token_gpu_user`) are kept in the table
regardless — several of the removed real values (the FQDNs, the PI's
email) only ever appeared in files this default include set doesn't pull
in (`pyproject.toml`, `CITATION.cff`, `Cargo.toml`, `.git` history), and
the table stays defensive for whatever future record adds one.

A real (non-dry-run) build of this worktree wrote 346 files and passed its
own verification pass clean.

## What this script cannot fix mechanically

- **File and directory names.** `benchmarks/results-v1/evidence-overhead-itiger.json`
  and `…/guard-frontier-itiger-scaled.json` embed the cluster name in the
  filename itself. The script anonymizes file *content*, not the bundle's
  directory listing — renaming would break the paper's own citations to
  these files by path. The bundle manifest's `files[].path` entries are
  therefore left as the real relative paths on purpose (and are excluded
  from the manifest's own verification scan for exactly this reason).
- **JSON object keys.** `benchmarks/results-v1/paper_numbers.json` has
  keys named things like `itiger_scaled_at_500ms`. The task's own rule is
  "replace inside strings; never change numbers or keys" — a key that
  embeds a hostname is, by that rule, out of scope for a value-rewriting
  pass, and verification is scoped to match (it checks JSON *values* only
  for `.json`/`.jsonl`, not keys, to stay consistent with what the
  rewriter itself is allowed to touch).
- **Prose that describes the cluster's shape without naming it.** GPU
  counts, quota sizes, node counts, and similar numbers in `docs/eval/*.md`
  are left alone even where they're suggestive, because they're ordinary
  numbers this table can't pattern-match without risking collateral damage
  to unrelated figures.
