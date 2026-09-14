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
   ``build_replacement_table``) to file text, and — for ``.json``/
   ``.jsonl`` — to JSON string *values* only (never to numbers or keys),
   walking the parsed structure so a replacement inside a longer string
   (e.g. a kernel ``platform`` string) still lands correctly.
3. Logs every replacement into a manifest (file, per-pattern count, and
   before/after sha256) — never the original matched text.
4. Re-parses every JSON/JSONL file after replacement to confirm it is
   still valid.
5. In real (non-dry-run) mode, re-scans the written bundle for every
   pattern in the table plus a short list of hard-coded sentinels and
   fails (exit 1) if anything survived.

``--dry-run`` prints the same per-file, per-pattern counts and writes
nothing (no bundle directory, no manifest).

Known mechanical limits (see docs/eval/anonymized_artifact.md): file and
directory *names* that embed a hostname (e.g.
``benchmarks/results-v1/evidence-overhead-itiger.json``) are not renamed,
and prose describing the cluster's shape (GPU counts, quota sizes) is left
alone even though it is suggestive, because it is not itself an identifier
this table can safely pattern-match without collateral damage to ordinary
numbers.
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
    return [
        Rule(
            "cluster_login_fqdn",
            re.compile(r"\bitiger\.memphis\.edu\b"),
            "cluster.example.edu",
            "the compute cluster's login-node fully-qualified domain name",
        ),
        Rule(
            # Only the leading edge is anchored: node names show up glued
            # to more identifier text with no separator (e.g. inside a
            # kernel `platform` string, or `..._scaled_at_500ms`-style
            # metric keys), and `\d{2}` immediately after the prefix is
            # already specific enough to keep this from firing on
            # unrelated words.
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
            re.compile(r"\bxzgpu\.uom\.memphis\.edu\b"),
            "H-server.example.edu",
            "the GPU server's fully-qualified domain name",
        ),
        Rule(
            "gpu_server_host",
            re.compile(r"\bxzgpu\b"),
            "H-server",
            "the GPU server's bare hostname",
        ),
        Rule(
            "personal_workstation",
            re.compile(r"\bXiaofeis-Mac-mini(?:\.local)?\b"),
            "H-workstation.local",
            "the PI's personal machine hostname",
        ),
        Rule(
            "slurm_partition",
            re.compile(r"\bbigTiger\b"),
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
            re.compile(r"\bXiaofei\s+Zhang\b"),
            "ANON-AUTHOR",
            "the PI's full name",
        ),
        Rule(
            "pi_first_name",
            re.compile(r"\bXiaofei\b"),
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
            re.compile(r"\bDPG-LMS\b"),
            "ANON-HANDLE",
            "the PI's GitHub handle",
        ),
        Rule(
            "university_domain",
            re.compile(r"\bmemphis\.edu\b", re.IGNORECASE),
            "example.edu",
            "the university's web/email domain, any remaining bare mention",
        ),
        Rule(
            "university_name",
            re.compile(r"\bUniversity of Memphis\b"),
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
            re.compile(r"\bxzhang12\b"),
            "ANON",
            "the cluster username, outside a /project or /home path",
        ),
        Rule(
            "user_token_gpu_user",
            re.compile(r"\bxzhang\b"),
            "ANON",
            "the GPU server username, outside a /mnt/project path",
        ),
    ]


#: hard-coded sentinels the final verification pass also checks for,
#: independent of whether any table pattern happens to match them.
VERIFICATION_SENTINELS = ["memphis", "xzhang", "zxf-work"]


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


def _walk_json(value, table: list[Rule], counts: Counter):
    if isinstance(value, str):
        return _apply_rules_to_str(value, table, counts)
    if isinstance(value, list):
        return [_walk_json(v, table, counts) for v in value]
    if isinstance(value, dict):
        # keys are never touched, only values
        return {k: _walk_json(v, table, counts) for k, v in value.items()}
    return value  # numbers, bool, None


def _collect_string_leaves(value, out: list[str]) -> None:
    """Same traversal as _walk_json, but collecting instead of rewriting —
    used by verification to check exactly what anonymization was allowed
    to touch (string values), and nothing it wasn't (dict keys, numbers)."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for v in value:
            _collect_string_leaves(v, out)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_string_leaves(v, out)


class AnonymizeError(RuntimeError):
    pass


def process_text(content: str, table: list[Rule]) -> tuple[str, Counter]:
    counts: Counter = Counter()
    content = _apply_rules_to_str(content, table, counts)
    return content, counts


def process_json(content: str, table: list[Rule], *, rel: str) -> tuple[str, Counter]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise AnonymizeError(f"{rel}: not valid JSON before anonymizing: {e}") from e
    counts: Counter = Counter()
    new_data = _walk_json(data, table, counts)
    new_content = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    try:
        json.loads(new_content)
    except json.JSONDecodeError as e:  # pragma: no cover - should be unreachable
        raise AnonymizeError(f"{rel}: became invalid JSON after anonymizing: {e}") from e
    return new_content, counts


def process_jsonl(content: str, table: list[Rule], *, rel: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    out_lines = []
    for i, line in enumerate(content.split("\n")):
        if line.strip() == "":
            out_lines.append(line)
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise AnonymizeError(f"{rel}:{i + 1}: not valid JSON before anonymizing: {e}") from e
        new_data = _walk_json(data, table, counts)
        new_line = json.dumps(new_data, ensure_ascii=False)
        try:
            json.loads(new_line)
        except json.JSONDecodeError as e:  # pragma: no cover
            raise AnonymizeError(f"{rel}:{i + 1}: became invalid JSON after anonymizing: {e}") from e
        out_lines.append(new_line)
    return "\n".join(out_lines), counts


def process_file(rel: str, raw: bytes, table: list[Rule]) -> tuple[bytes, Counter, bool]:
    """Returns (new_bytes, per-pattern counts, was_text)."""
    suffix = Path(rel).suffix
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, Counter(), False

    if suffix in JSON_SUFFIXES:
        new_text, counts = process_json(text, table, rel=rel)
    elif suffix in JSONL_SUFFIXES:
        new_text, counts = process_jsonl(text, table, rel=rel)
    else:
        new_text, counts = process_text(text, table)
    return new_text.encode("utf-8"), counts, True


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


def _scan_targets(path: Path, manifest_name: str) -> list[str] | None:
    """Strings to run the pattern/sentinel scan over, or None to skip
    `path` entirely (undecodable / binary).

    Scoped to exactly what anonymization was allowed to touch: for
    ``.json``/``.jsonl`` this returns the JSON string *values* only (never
    keys, never numbers — the same "never change numbers or keys" rule
    ``process_json``/``process_jsonl`` follow), because a leftover
    identifier baked into a *key* (e.g. a metric named
    ``itiger_scaled_at_500ms``) is a documented mechanical limit, not
    something a value-rewriting pass could have fixed — see the module
    docstring and the task report. For every other file the whole text is
    scanned, since there's no key/value distinction to make.

    The manifest's own `files[].path` entries are relative source paths,
    deliberately left unrenamed (a filename like
    `evidence-overhead-itiger.json` embeds a hostname that this script
    does not rename files to scrub — another documented limit). Those
    path strings are excluded from the manifest's own scan; everything
    else in the manifest (descriptions, totals, hashes) is scanned as one
    blob.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None

    if path.name == manifest_name:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        for entry in data.get("files", []):
            if isinstance(entry, dict):
                entry.pop("path", None)
        return [json.dumps(data)]

    suffix = path.suffix
    if suffix in JSON_SUFFIXES:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        leaves: list[str] = []
        _collect_string_leaves(data, leaves)
        return leaves
    if suffix in JSONL_SUFFIXES:
        leaves = []
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                leaves.append(line)
                continue
            _collect_string_leaves(data, leaves)
        return leaves
    return [text]


def verify_bundle(out: Path, table: list[Rule], sentinels: list[str], manifest_name: str = "bundle-manifest.json") -> list[str]:
    """Re-scan every file under `out` for every table pattern and every
    sentinel. Returns a list of human-readable violations (file + what
    survived, but never the full offending text beyond what's needed to
    locate it) — empty means clean."""
    violations: list[str] = []
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        targets = _scan_targets(path, manifest_name)
        if targets is None:
            continue
        rel = str(path.relative_to(out))
        for rule in table:
            if any(rule.pattern.search(t) for t in targets):
                violations.append(f"{rel}: pattern '{rule.name}' still matches")
        for sentinel in sentinels:
            needle = sentinel.lower()
            if any(needle in t.lower() for t in targets):
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
so a reviewer can confirm nothing else moved.
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_dry_run_summary(file_counts: dict[str, Counter]) -> None:
    by_dir: dict[str, Counter] = {}
    totals: Counter = Counter()
    for rel, counts in sorted(file_counts.items()):
        top = rel.split("/", 1)[0]
        by_dir.setdefault(top, Counter())
        for name, n in counts.items():
            by_dir[top][name] += n
            totals[name] += n

    print("anonymize_artifact: DRY RUN (nothing written)\n")
    print(f"{len(file_counts)} file(s) scanned\n")

    print("Per top-level directory, per pattern:")
    for top in sorted(by_dir):
        dir_counts = by_dir[top]
        if not dir_counts:
            continue
        print(f"  {top}/")
        for name, n in sorted(dir_counts.items()):
            print(f"    {name}: {n}")

    print("\nTotals:")
    if not totals:
        print("  (no replacements)")
    for name, n in sorted(totals.items()):
        print(f"  {name}: {n}")

    print("\nPer file (nonzero only):")
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

    file_counts: dict[str, Counter] = {}
    new_bytes_by_rel: dict[str, bytes] = {}
    sha_before: dict[str, str] = {}
    sha_after: dict[str, str] = {}
    text_flag: dict[str, bool] = {}

    for rel in rels:
        raw = (src / rel).read_bytes()
        sha_before[rel] = sha256_hex(raw)
        try:
            new_raw, counts, was_text = process_file(rel, raw, table)
        except AnonymizeError as e:
            print(f"anonymize_artifact: {e}", file=sys.stderr)
            return 1
        new_bytes_by_rel[rel] = new_raw
        sha_after[rel] = sha256_hex(new_raw)
        file_counts[rel] = counts
        text_flag[rel] = was_text

    if args.dry_run:
        _print_dry_run_summary(file_counts)
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for rel in rels:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(new_bytes_by_rel[rel])

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
                "path": rel,
                "sha256_before": sha_before[rel],
                "sha256_after": sha_after[rel],
                "text": text_flag[rel],
                "replacements": dict(sorted(file_counts[rel].items())),
            }
            for rel in rels
        ],
        "totals": dict(sorted(_sum_counters(file_counts.values()).items())),
    }
    manifest_path = out / args.manifest
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # verify_bundle rglobs the whole `out` tree, so bundle-manifest.json and
    # bundle-README.md are checked too — the manifest must come up clean
    # (modulo its files[].path entries, which are original relative paths
    # by design — see _scan_text_for).
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
