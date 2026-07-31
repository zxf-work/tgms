#!/usr/bin/env python3
"""Spec §8.1 enforcement: tests/ and tgms/temporal/oracle.py are human-owned
ground truth. Any commit touching both (tests/ or the oracle) and
implementation code under tgms/ fails this check.

Usage: check_commit_hygiene.py [BASE_REF]
Checks every commit in BASE_REF..HEAD (default base: the marker commit that
adopted spec §8 — see docs/DECISIONS.md D-010). Exits non-zero on violation.
"""

from __future__ import annotations

import subprocess
import sys

# commit that introduced docs/DECISIONS.md D-010 (set once, then frozen);
# empty string means "first commit that contains scripts/check_commit_hygiene.py"
ADOPTION_MARKER_FILE = "docs/DECISIONS.md"

GROUND_TRUTH = ("tests/", "tgms/temporal/oracle.py")


def sh(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=True).stdout


def default_base() -> str:
    line = sh("log", "--diff-filter=A", "--format=%H", "--",
              ADOPTION_MARKER_FILE).strip().splitlines()
    if not line:
        print("hygiene: adoption marker not committed yet; nothing to check")
        sys.exit(0)
    return line[-1]  # the commit that added DECISIONS.md


#: Commits where mixing ground truth with implementation was the point,
#: sanctioned case by case. The check exists to stop an implementer from
#: bending the oracle to fit the code; these are the opposite event — the
#: oracle itself being corrected under an explicit decision, where landing
#: the semantics change atomically across every implementation is what
#: keeps the tree consistent at each commit.
SANCTIONED = {
    # D-031: resolve_entities — the oracle's str() name coercion matched
    # text the store never contained ("None", "42"); PI-directed semantic
    # correction applied to oracle, fallback, and kernel in one commit so
    # no intermediate commit has them disagreeing.
    "3b02ba097f7ca33dd5937903ba893acb0644cb7b",
}


def is_ground_truth(path: str) -> bool:
    return path.startswith(GROUND_TRUTH[0]) or path == GROUND_TRUTH[1]


def is_implementation(path: str) -> bool:
    return (path.startswith("tgms/") and path != GROUND_TRUTH[1])


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else default_base()
    commits = sh("rev-list", f"{base}..HEAD").split()
    bad = []
    for c in commits:
        if c in SANCTIONED:
            continue
        files = [f for f in sh("show", "--name-only", "--format=", c).split("\n")
                 if f.strip()]
        gt = sorted(f for f in files if is_ground_truth(f))
        impl = sorted(f for f in files if is_implementation(f))
        if gt and impl:
            subject = sh("log", "-1", "--format=%s", c).strip()
            bad.append((c[:10], subject, gt, impl))
    if bad:
        print("SPEC §8.1 VIOLATION — commits mixing ground-truth "
              "(tests/oracle) with implementation:")
        for sha, subject, gt, impl in bad:
            print(f"\n  {sha}  {subject}")
            print(f"    ground truth: {', '.join(gt[:5])}"
                  + (" …" if len(gt) > 5 else ""))
            print(f"    implementation: {', '.join(impl[:5])}"
                  + (" …" if len(impl) > 5 else ""))
        print("\nSplit into separate commits; label test commits [tests] "
              "with justification (docs/DECISIONS.md D-010).")
        return 1
    print(f"hygiene: {len(commits)} commit(s) since {base[:10]} clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
