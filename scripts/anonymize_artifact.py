#!/usr/bin/env python3
"""Build an anonymized artifact bundle for a double-blind submission.

Committed benchmark records and READMEs carry real provenance — cluster
hostnames, the university domain, the Slurm partition name, the GitHub
organisation, and the PI's name/email. The *repository* keeps that
provenance (records must stay attributable in place); this script produces
a separate, scrubbed *copy* under ``--out`` for the submission.

Usage:
    scripts/anonymize_artifact.py --src . --out /tmp/tgms-artifact
    scripts/anonymize_artifact.py --src . --out /tmp/x --dry-run
    scripts/anonymize_artifact.py --src . --out /tmp/x --include ops/failure_ledger.jsonl

What it does:

1. Walks a default include set (``benchmarks/**/*.json``, ``*.jsonl``,
   ``*.md``, ``*.yaml``; ``benchmarks/schema/**``; the paper-cited
   ``docs/*.md`` pages; the two ``osdi_paper_*`` scripts; ``README.md``;
   ``LICENSE``), minus a hard-excluded set (``docs/design/**``,
   ``paper/**``, ``docs/DECISIONS.md``, ``.git``, anything gitignored) and
   minus ``ops/failure_ledger.jsonl`` unless ``--include`` names it.
2. Applies a fixed, deterministic replacement table (see
   ``build_replacement_table``) to file text; to JSON string *values*;
   and to JSON object *keys* that themselves match a pattern (a key that
   matches nothing, and every number, is left exactly as-is). The same
   table is also applied to each file's own relative path, so a file name
   or directory component that embeds a hostname gets renamed inside the
   bundle, not just its content.
3. Logs every replacement into a manifest (file, per-pattern count, and
   before/after sha256) — never the original matched text. Renamed files
   are logged under ``renames`` (new path + a sha256 of the *original*
   relative path, so an author holding the real tree can confirm which
   bundle file a citation maps to without the manifest itself carrying the
   original name). Rewritten JSON keys are logged the same way under
   ``rewritten_keys``.
4. Re-parses every JSON/JSONL file after replacement to confirm it is
   still valid.
5. In real (non-dry-run) mode, re-scans the *entire* written bundle —
   file content, file names, and the manifest itself, with no exclusions
   — for every pattern in the table plus a list of hard-coded sentinels,
   and fails (exit 1) if anything survived.

``--dry-run`` prints the same per-file, per-pattern counts and writes
nothing (no bundle directory, no manifest).

Known mechanical limit (see docs/eval/anonymized_artifact.md): prose
describing the cluster's shape by number alone (GPU counts, quota sizes)
is left alone even where it's suggestive, because a bare number isn't
something this table can pattern-match without risking collateral damage
to unrelated figures.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------
# Default include / exclude sets
# --------------------------------------------------------------------------

DEFAULT_INCLUDE_GLOBS = [
    "benchmarks/**/*.json",
    "benchmarks/**/*.jsonl",
    "benchmarks/**/*.md",
    "benchmarks/**/*.yaml",
    "benchmarks/schema/**",
    "docs/system_invariants.md",
    "docs/STABILITY.md",
    "docs/eval/*.md",
    "scripts/osdi_paper_macros.py",
    "scripts/osdi_paper_figures.py",
    "README.md",
    "LICENSE",
]

# Unconditional: no --include can pull a path back in under these.
HARD_EXCLUDE_GLOBS = [
    "docs/design/**",
    "paper/**",
    "docs/DECISIONS.md",
    ".git/**",
    ".git",
]

# Excluded unless a user --include glob explicitly names it.
CONDITIONAL_EXCLUDE = "ops/failure_ledger.jsonl"

JSON_SUFFIXES = {".json"}
JSONL_SUFFIXES = {".jsonl"}


# --------------------------------------------------------------------------
# Replacement table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    repl: Callable[[re.Match], str] | str
    description: str


def _github_identity_repl(m: re.Match) -> str:
    has_io = m.group(1) is not None
    has_repo = m.group(2) is not None
    base = "anon-org.github.io" if has_io else "ANON-ORG"
    return base + ("/ANON-REPO" if has_repo else "")


def _node_name_repl(m: re.Match) -> str:
    return f"H-array-{m.group(1)}"


def build_replacement_table() -> list[Rule]:
    """The deterministic replacement table.

    Order matters: more specific (longer) patterns run first so a
    substring they consume can't also trip a later, more general pattern
    and get double-mangled. Every entry here was checked against the repo
    with grep before being added (see the report this script's task
    produced) — nothing is a guess.
    """
    # NOTE: descriptions are paraphrased and never contain the literal
    # secret token a rule matches — they get written into the manifest,
    # which the verification pass re-scans, and the manifest must come up
    # as clean as the rest of the bundle.
    # Every bare-identifier pattern below anchors only its *leading* edge
    # (`\b<token>`, no trailing `\b`). A trailing boundary looks safer but
    # isn't: JSON keys glue these tokens directly onto more identifier
    # text with an underscore (`xzgpu_at_2s`, `itiger_scaled_at_500ms`) --
    # and `_` counts as a word character, so `\btoken\b` silently refuses
    # to match right where the token is followed by `_`. None of these
    # tokens is a prefix of any ordinary word, so dropping the trailing
    # anchor costs nothing and catches the fused-key/fused-identifier
    # case the leading-only anchor is meant for. Slash-delimited path
    # patterns keep a trailing boundary: a path segment is not glued to
    # more identifier text past its own username, so the extra precision
    # (not eating into a different, longer username) is worth keeping.
    return [
        Rule(
            "cluster_login_fqdn",
            re.compile(r"\bitiger\.memphis\.edu"),
            "cluster.example.edu",
            "the compute cluster's login-node fully-qualified domain name",
        ),
        Rule(
            "cluster_node_name",
            re.compile(r"\bitiger(\d{2})"),
            _node_name_repl,
            "compute-cluster node names (a letters prefix + 2-digit number)",
        ),
        Rule(
            "cluster_name",
            re.compile(r"\bitiger", re.IGNORECASE),
            "Cluster-A",
            "the compute cluster's own name, in prose or fused into a longer identifier",
        ),
        Rule(
            "gpu_server_fqdn",
            re.compile(r"\bxzgpu\.uom\.memphis\.edu"),
            "H-server.example.edu",
            "the GPU server's fully-qualified domain name",
        ),
        Rule(
            "gpu_server_host",
            re.compile(r"\bxzgpu"),
            "H-server",
            "the GPU server's bare hostname",
        ),
        Rule(
            "personal_workstation",
            re.compile(r"\bXiaofeis-Mac-mini(?:\.local)?"),
            "H-workstation.local",
            "the PI's personal machine hostname",
        ),
        Rule(
            "slurm_partition",
            re.compile(r"\bbigTiger"),
            "partition-A",
            "the Slurm partition name",
        ),
        Rule(
            "github_identity",
            re.compile(r"\bzxf-work(\.github\.io)?(/tgms\b)?"),
            _github_identity_repl,
            "the GitHub organisation (and, where paired in a URL, the repo)",
        ),
        Rule(
            "pi_full_name",
            re.compile(r"\bXiaofei\s+Zhang"),
            "ANON-AUTHOR",
            "the PI's full name",
        ),
        Rule(
            "pi_first_name",
            re.compile(r"\bXiaofei"),
            "ANON-AUTHOR",
            "the PI's given name, standing alone",
        ),
        Rule(
            "pi_email",
            re.compile(re.escape("zxfhkust@gmail.com")),
            "anon@example.com",
            "the PI's email address",
        ),
        Rule(
            "github_noreply_email",
            re.compile(re.escape("115059980+DPG-LMS@users.noreply.github.com")),
            "anon@users.noreply.github.com",
            "the PI's GitHub no-reply commit-author email",
        ),
        Rule(
            "github_handle",
            re.compile(r"\bDPG-LMS"),
            "ANON-HANDLE",
            "the PI's GitHub handle",
        ),
        Rule(
            "university_domain",
            re.compile(r"\bmemphis\.edu", re.IGNORECASE),
            "example.edu",
            "the university's web/email domain, any remaining bare mention",
        ),
        Rule(
            "university_name",
            re.compile(r"\bUniversity of Memphis"),
            "Anonymous University",
            "the university's name in prose",
        ),
        Rule(
            "path_project_cluster_user",
            re.compile(r"/project/xzhang12\b"),
            "/project/ANON",
            "the cluster username's home directory under /project",
        ),
        Rule(
            "path_home_cluster_user",
            re.compile(r"/home/xzhang12\b"),
            "/home/ANON",
            "the cluster username's home directory under /home",
        ),
        Rule(
            "path_mnt_project_gpu_user",
            re.compile(r"/mnt/project/xzhang\b"),
            "/mnt/project/ANON",
            "the GPU server username's home directory under /mnt/project",
        ),
        Rule(
            "path_users_pi_laptop",
            re.compile(r"/Users/xz(?=/)"),
            "/Users/ANON",
            "the PI's laptop home directory",
        ),
        Rule(
            "user_token_cluster_user",
            re.compile(r"\bxzhang12"),
            "ANON",
            "the cluster username, outside a /project or /home path",
        ),
        Rule(
            "user_token_gpu_user",
            re.compile(r"\bxzhang"),
            "ANON",
            "the GPU server username, outside a /mnt/project path",
        ),
    ]


#: hard-coded sentinels the final verification pass also checks for,
#: independent of whether any table pattern happens to match them. Several
#: of these are redundant with a table pattern (e.g. "itiger" is a strict
#: substring of what `cluster_name` matches) -- kept anyway so verification
#: doesn't depend on the table being complete or correctly ordered.
VERIFICATION_SENTINELS = [
    "memphis",
    "xzhang",
    "itiger",
    "xzgpu",
    "zxf-work",
    "bigtiger",
    "uom.memphis",
    "xiaofei",
    "zxfhkust",
    "dpg-lms",
]


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def _glob_matches(root: Path, pattern: str) -> set[Path]:
    if pattern.endswith("/**"):
        base = pattern[: -len("/**")]
        matches = {p for p in root.glob(pattern) if p.is_file()}
        base_path = root / base
        if base_path.is_dir():
            matches |= {p for p in base_path.rglob("*") if p.is_file()}
        return matches
    return {p for p in root.glob(pattern) if p.is_file()}


def _hard_excluded(rel: str) -> bool:
    parts = Path(rel).parts
    if ".git" in parts:
        return True
    for pattern in HARD_EXCLUDE_GLOBS:
        if pattern in (".git/**", ".git"):
            continue
        base = pattern[:-3] if pattern.endswith("/**") else pattern
        if rel == base or rel.startswith(base + "/") or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def gitignored_set(src: Path, rel_paths: list[str]) -> set[str]:
    """Paths (of rel_paths) that git considers ignored. Empty set if src
    isn't a git working tree or git isn't available — anonymization still
    runs, just without that particular filter."""
    if not rel_paths or not (src / ".git").exists():
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(src), "check-ignore", "--stdin"],
            input="\n".join(rel_paths) + "\n",
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return set()
    if proc.returncode not in (0, 1):
        return set()
    return {line for line in proc.stdout.splitlines() if line}


def discover_files(src: Path, extra_include_globs: list[str]) -> list[str]:
    """Return sorted repo-relative paths to anonymize."""
    all_globs = DEFAULT_INCLUDE_GLOBS + list(extra_include_globs)
    candidates: set[Path] = set()
    for pattern in all_globs:
        candidates |= _glob_matches(src, pattern)

    rels = sorted({str(p.relative_to(src)) for p in candidates})
    rels = [r for r in rels if not _hard_excluded(r)]

    ignored = gitignored_set(src, rels)
    rels = [r for r in rels if r not in ignored]

    explicitly_included = set()
    for pattern in extra_include_globs:
        explicitly_included |= {str(p.relative_to(src)) for p in _glob_matches(src, pattern)}

    rels = [
        r
        for r in rels
        if r != CONDITIONAL_EXCLUDE or r in explicitly_included
    ]

    return sorted(rels)


# --------------------------------------------------------------------------
# Replacement application
# --------------------------------------------------------------------------


def _apply_rules_to_str(value: str, table: list[Rule], counts: Counter) -> str:
    for rule in table:
        value, n = rule.pattern.subn(rule.repl, value)
        if n:
            counts[rule.name] += n
    return value


def _walk_json(value, table: list[Rule], counts: Counter, *, rel: str, rewritten_keys: list[dict]):
    """Rewrite string leaf values, and any dict key that itself matches a
    pattern; numbers/bool/None and non-matching keys are untouched.

    `rel` is the file's *bundled* (already-renamed) relative path, used
    only to label `rewritten_keys` entries -- it's guaranteed clean since
    it already went through the same table (see `anonymize_path`).
    """
    if isinstance(value, str):
        return _apply_rules_to_str(value, table, counts)
    if isinstance(value, list):
        return [_walk_json(v, table, counts, rel=rel, rewritten_keys=rewritten_keys) for v in value]
    if isinstance(value, dict):
        new_dict = {}
        for k, v in value.items():
            key_counts: Counter = Counter()
            new_key = _apply_rules_to_str(k, table, key_counts)
            if key_counts:
                counts.update(key_counts)
                rewritten_keys.append(
                    {
                        "file": rel,
                        "old_key_sha256": sha256_hex(k.encode("utf-8")),
                        "new_key": new_key,
                        "patterns": sorted(key_counts.keys()),
                    }
                )
            new_dict[new_key] = _walk_json(v, table, counts, rel=rel, rewritten_keys=rewritten_keys)
        return new_dict
    return value  # numbers, bool, None


class AnonymizeError(RuntimeError):
    pass


def process_text(content: str, table: list[Rule]) -> tuple[str, Counter]:
    counts: Counter = Counter()
    content = _apply_rules_to_str(content, table, counts)
    return content, counts


def process_json(content: str, table: list[Rule], *, rel: str, bundled_rel: str) -> tuple[str, Counter, list[dict]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise AnonymizeError(f"{rel}: not valid JSON before anonymizing: {e}") from e
    counts: Counter = Counter()
    rewritten_keys: list[dict] = []
    new_data = _walk_json(data, table, counts, rel=bundled_rel, rewritten_keys=rewritten_keys)
    new_content = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    try:
        json.loads(new_content)
    except json.JSONDecodeError as e:  # pragma: no cover - should be unreachable
        raise AnonymizeError(f"{rel}: became invalid JSON after anonymizing: {e}") from e
    return new_content, counts, rewritten_keys


def process_jsonl(content: str, table: list[Rule], *, rel: str, bundled_rel: str) -> tuple[str, Counter, list[dict]]:
    counts: Counter = Counter()
    rewritten_keys: list[dict] = []
    out_lines = []
    for i, line in enumerate(content.split("\n")):
        if line.strip() == "":
            out_lines.append(line)
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise AnonymizeError(f"{rel}:{i + 1}: not valid JSON before anonymizing: {e}") from e
        new_data = _walk_json(data, table, counts, rel=bundled_rel, rewritten_keys=rewritten_keys)
        new_line = json.dumps(new_data, ensure_ascii=False)
        try:
            json.loads(new_line)
        except json.JSONDecodeError as e:  # pragma: no cover
            raise AnonymizeError(f"{rel}:{i + 1}: became invalid JSON after anonymizing: {e}") from e
        out_lines.append(new_line)
    return "\n".join(out_lines), counts, rewritten_keys


def process_file(rel: str, bundled_rel: str, raw: bytes, table: list[Rule]) -> tuple[bytes, Counter, bool, list[dict]]:
    """Returns (new_bytes, per-pattern content counts, was_text, rewritten_keys)."""
    suffix = Path(rel).suffix
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, Counter(), False, []

    if suffix in JSON_SUFFIXES:
        new_text, counts, rewritten_keys = process_json(text, table, rel=rel, bundled_rel=bundled_rel)
    elif suffix in JSONL_SUFFIXES:
        new_text, counts, rewritten_keys = process_jsonl(text, table, rel=rel, bundled_rel=bundled_rel)
    else:
        new_text, counts = process_text(text, table)
        rewritten_keys = []
    return new_text.encode("utf-8"), counts, True, rewritten_keys


def anonymize_path(rel: str, table: list[Rule]) -> tuple[str, Counter]:
    """Apply the same replacement table to a relative file path -- a
    directory or file name can embed a hostname just like any other
    string. Returns (new_relative_path, per-pattern counts)."""
    counts: Counter = Counter()
    new_rel = _apply_rules_to_str(rel, table, counts)
    return new_rel, counts


def dedupe_path(candidate: str, original_rel: str, taken: dict[str, str]) -> str:
    """If `candidate` (a proposed bundled path) is already used by a
    *different* original file, disambiguate deterministically by suffixing
    a short hash of the original path -- collisions are expected to be
    rare (two different real names anonymizing to the identical string)
    but must never silently overwrite one file with another."""
    if candidate not in taken or taken[candidate] == original_rel:
        return candidate
    suffix = sha256_hex(original_rel.encode("utf-8"))[:8]
    p = Path(candidate)
    if p.suffix:
        disambiguated = str(p.with_name(f"{p.stem}-{suffix}{p.suffix}"))
    else:
        disambiguated = f"{candidate}-{suffix}"
    # astronomically unlikely to collide again, but don't loop forever
    return disambiguated if disambiguated not in taken else f"{disambiguated}-{suffix}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sum_counters(counters) -> Counter:
    total: Counter = Counter()
    for c in counters:
        total.update(c)
    return total


# --------------------------------------------------------------------------
# Verification pass
# --------------------------------------------------------------------------


def verify_bundle(out: Path, table: list[Rule], sentinels: list[str], manifest_name: str = "bundle-manifest.json") -> list[str]:
    """Re-scan the *entire* written bundle -- file content, file/directory
    names, and the manifest itself -- for every table pattern and every
    sentinel. No exclusions: a key that carries a token is rewritten (see
    `_walk_json`) and a file whose name carries one is renamed (see
    `anonymize_path`), so by construction nothing scanned here should
    still match; anything that does is a real leftover, not a documented
    limit. (Numbers are never at risk: none of the table's patterns can
    match a bare run of digits, so scanning raw JSON text -- keys, values,
    and numbers all serialized together -- cannot produce a false
    positive off a number alone.)

    Returns a list of human-readable violations (file + what survived,
    but never the full offending text beyond what's needed to locate it)
    -- empty means clean.
    """
    violations: list[str] = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(out))
        for rule in table:
            if rule.pattern.search(rel):
                violations.append(f"{rel}: pattern '{rule.name}' still matches in the file name")
        for sentinel in sentinels:
            if sentinel.lower() in rel.lower():
                violations.append(f"{rel}: sentinel '{sentinel}' still present in the file name")

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for rule in table:
            if rule.pattern.search(text):
                violations.append(f"{rel}: pattern '{rule.name}' still matches")
        for sentinel in sentinels:
            if sentinel.lower() in text.lower():
                violations.append(f"{rel}: sentinel '{sentinel}' still present")
    return violations


# --------------------------------------------------------------------------
# Bundle README
# --------------------------------------------------------------------------

BUNDLE_README = """\
# Anonymized artifact bundle

This bundle was produced by `scripts/anonymize_artifact.py` for double-blind
review. It is a scrubbed *copy*: the source repository keeps the real
provenance (hostnames, university domain, Slurm partition, GitHub org,
author identity) on the ruling that committed benchmark records must stay
attributable in place.

**Digests were not recomputed.** Every `result_digest` and any other
`record`-style hash in these files was computed over the *original* rows,
before this bundle rewrote host/user/domain fields inside them. A digest
here should be read as "this is the digest the real run produced," not as a
checksum of the bytes sitting next to it in this bundle.

See `bundle-manifest.json` for exactly what changed: per file, the count of
replacements made by each pattern, and the before/after sha256 of the file
so a reviewer can confirm nothing else moved. Files whose *name* embedded
an identifier were renamed inside this bundle -- see `renames` (new name
plus a sha256 of the original relative path, not the original name
itself, so an author holding the real tree can confirm a mapping without
this manifest carrying it). JSON object keys that themselves embedded an
identifier were rewritten the same way -- see `rewritten_keys`.
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_dry_run_summary(
    file_counts: dict[str, Counter],
    path_counts: dict[str, Counter],
    rewritten_key_counts: dict[str, int],
) -> None:
    by_dir: dict[str, Counter] = {}
    totals: Counter = Counter()
    for rel, counts in sorted(file_counts.items()):
        top = rel.split("/", 1)[0]
        by_dir.setdefault(top, Counter())
        for name, n in counts.items():
            by_dir[top][name] += n
            totals[name] += n

    path_by_dir: dict[str, Counter] = {}
    path_totals: Counter = Counter()
    renamed_files = 0
    for rel, counts in sorted(path_counts.items()):
        if not counts:
            continue
        renamed_files += 1
        top = rel.split("/", 1)[0]
        path_by_dir.setdefault(top, Counter())
        for name, n in counts.items():
            path_by_dir[top][name] += n
            path_totals[name] += n

    print("anonymize_artifact: DRY RUN (nothing written)\n")
    print(f"{len(file_counts)} file(s) scanned\n")

    print("Per top-level directory, per pattern (file content + JSON keys):")
    for top in sorted(by_dir):
        dir_counts = by_dir[top]
        if not dir_counts:
            continue
        print(f"  {top}/")
        for name, n in sorted(dir_counts.items()):
            print(f"    {name}: {n}")

    print("\nTotals (content + keys):")
    if not totals:
        print("  (no replacements)")
    for name, n in sorted(totals.items()):
        print(f"  {name}: {n}")

    print(f"\nFile/directory names that would be renamed: {renamed_files}")
    print("Per top-level directory, per pattern (path components only):")
    for top in sorted(path_by_dir):
        dir_counts = path_by_dir[top]
        if not dir_counts:
            continue
        print(f"  {top}/")
        for name, n in sorted(dir_counts.items()):
            print(f"    {name}: {n}")
    if path_totals:
        print("Path totals:")
        for name, n in sorted(path_totals.items()):
            print(f"  {name}: {n}")

    total_rewritten_keys = sum(rewritten_key_counts.values())
    print(f"\nJSON keys that would be rewritten: {total_rewritten_keys}")

    print("\nPer file (nonzero content/key counts only):")
    for rel, counts in sorted(file_counts.items()):
        if not counts:
            continue
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"  {rel}: {parts}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", required=True, help="repo root to read from")
    parser.add_argument("--out", required=True, help="directory to write the anonymized bundle to")
    parser.add_argument("--manifest", default="bundle-manifest.json", help="manifest filename, written under --out")
    parser.add_argument("--dry-run", action="store_true", help="print counts, write nothing")
    parser.add_argument("--include", action="append", default=[], help="extra glob(s), relative to --src, to include")
    args = parser.parse_args(argv)

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()

    if not src.is_dir():
        print(f"anonymize_artifact: --src {src} is not a directory", file=sys.stderr)
        return 2

    table = build_replacement_table()
    rels = discover_files(src, args.include)

    # First pass: compute each file's bundled (possibly renamed) path,
    # disambiguating collisions deterministically.
    bundled_rel: dict[str, str] = {}
    path_counts: dict[str, Counter] = {}
    taken: dict[str, str] = {}
    for rel in sorted(rels):
        candidate, p_counts = anonymize_path(rel, table)
        candidate = dedupe_path(candidate, rel, taken)
        taken[candidate] = rel
        bundled_rel[rel] = candidate
        path_counts[rel] = p_counts

    file_counts: dict[str, Counter] = {}
    rewritten_keys_all: list[dict] = []
    new_bytes_by_rel: dict[str, bytes] = {}
    sha_before: dict[str, str] = {}
    sha_after: dict[str, str] = {}
    text_flag: dict[str, bool] = {}

    for rel in rels:
        raw = (src / rel).read_bytes()
        sha_before[rel] = sha256_hex(raw)
        try:
            new_raw, counts, was_text, rewritten_keys = process_file(rel, bundled_rel[rel], raw, table)
        except AnonymizeError as e:
            print(f"anonymize_artifact: {e}", file=sys.stderr)
            return 1
        new_bytes_by_rel[rel] = new_raw
        sha_after[rel] = sha256_hex(new_raw)
        file_counts[rel] = counts
        text_flag[rel] = was_text
        rewritten_keys_all.extend(rewritten_keys)

    rewritten_key_counts = {rel: len([k for k in rewritten_keys_all if k["file"] == bundled_rel[rel]]) for rel in rels}

    if args.dry_run:
        _print_dry_run_summary(file_counts, path_counts, rewritten_key_counts)
        return 0

    out.mkdir(parents=True, exist_ok=True)
    renames: list[dict] = []
    for rel in rels:
        dest_rel = bundled_rel[rel]
        dest = out / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(new_bytes_by_rel[rel])
        if dest_rel != rel:
            renames.append(
                {
                    "to": dest_rel,
                    "old_path_sha256": sha256_hex(rel.encode("utf-8")),
                    "patterns": sorted(path_counts[rel].keys()),
                }
            )

    (out / "bundle-README.md").write_text(BUNDLE_README, encoding="utf-8")

    # The --src/--out paths are local operator detail, not bundle content,
    # but they can themselves carry a home-directory username (e.g.
    # /Users/<user>/...) — scrub them the same way as file content instead
    # of assuming they're safe just because they're metadata.
    manifest_src, _ = process_text(str(src), table)
    manifest_out, _ = process_text(str(out), table)
    manifest = {
        "src": manifest_src,
        "out": manifest_out,
        "replacement_patterns": [
            {"name": rule.name, "description": rule.description} for rule in table
        ],
        "files": [
            {
                "path": bundled_rel[rel],
                "renamed": bundled_rel[rel] != rel,
                "sha256_before": sha_before[rel],
                "sha256_after": sha_after[rel],
                "text": text_flag[rel],
                "replacements": dict(sorted(file_counts[rel].items())),
            }
            for rel in rels
        ],
        "renames": renames,
        "rewritten_keys": rewritten_keys_all,
        "totals": dict(sorted(_sum_counters(list(file_counts.values()) + list(path_counts.values())).items())),
    }
    manifest_path = out / args.manifest
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # No exclusions: file content, file/directory names, and the manifest
    # (and bundle-README.md) are all checked the same way. By construction
    # nothing here should still match -- see verify_bundle's docstring.
    violations = verify_bundle(out, table, VERIFICATION_SENTINELS, manifest_name=args.manifest)
    if violations:
        print("anonymize_artifact: VERIFICATION FAILED — provenance survived in the bundle:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1

    print(f"anonymize_artifact: wrote {len(rels)} file(s) to {out}")
    print(f"anonymize_artifact: manifest at {manifest_path}")
    print("anonymize_artifact: verification pass clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
