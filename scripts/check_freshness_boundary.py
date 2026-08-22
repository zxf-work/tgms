"""M4's import-boundary gate (M4_IMPLEMENTATION_PLAN §3.1, "non-negotiable").

D13.20's whole point is that **a checker runs against a log it did not
produce**: a `CorrectionFootprint` is built from one logged op record alone, and
`check` reads a `DependencyScope` and an `EventLog` and nothing else. An import
of `Store` or of any storage adapter makes that claim untestable — the code
would still pass its tests while quietly depending on live store state, and the
first time anyone pointed a checker at a foreign log it would fail for a reason
no test could have found.

So the rule is enforced mechanically rather than by review:

> `tgms/tgir/footprint.py` and `tgms/tgir/check.py` may import
> `tgms.core.model`, `tgms.core.errors`, `tgms.storage.eventlog` and
> `tgms.tgir.depscope`, and **nothing else** from `tgms`. In particular they may
> not import `Store` or any adapter.

`tgms/tgir/explain.py` renders a witness as user-facing text and is held to the
same boundary — it is downstream of `check` and has even less business touching
a store.

Checked by walking the AST rather than by grepping lines, so a deferred import
inside a function body — the usual way this rule gets broken while looking
clean — is caught too.

    uv run python scripts/check_freshness_boundary.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The four modules the plan names. Anything else under `tgms.` is a violation.
OUTWARD: frozenset[str] = frozenset({
    "tgms.core.model",
    "tgms.core.errors",
    "tgms.storage.eventlog",
    "tgms.tgir.depscope",
})

#: The guarded set may import **itself**: `check` is built on `footprint`, and
#: `explain` renders what `check` returns. The plan's §3.1 lists the four
#: outward imports without saying so, which reads as forbidding `check` from
#: importing the footprint builder it is the consumer of — plainly not the
#: intent. The rule with teeth is the outward one: no store, no adapter, no
#: operator kernel. An inward edge changes nothing about whether a checker can
#: run against a log it did not produce.
INWARD: frozenset[str] = frozenset({
    "tgms.tgir.footprint",
    "tgms.tgir.check",
    "tgms.tgir.explain",
})

ALLOWED: frozenset[str] = OUTWARD | INWARD

#: Guarded so the diagnosis names the actual hazard rather than "not allowed".
FORBIDDEN_HINTS: dict[str, str] = {
    "tgms.store": "the Store — D13.20 forbids the checker reading live store state",
    "tgms.storage.base": "a storage adapter — same reason",
    "tgms.storage.duckdb_adapter": "a storage adapter — same reason",
    "tgms.storage.native_adapter": "a storage adapter — same reason",
    "tgms.temporal": "an operator kernel — the checker never recomputes",
}

GUARDED: tuple[str, ...] = ("tgms/tgir/footprint.py", "tgms/tgir/check.py",
                            "tgms/tgir/explain.py")


def imports_of(path: Path) -> list[tuple[int, str]]:
    """Every `tgms.*` module this file imports, at any nesting depth."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tgms"):
                    out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import can reach anywhere; refuse outright
                out.append((node.lineno, f"<relative import, level {node.level}>"))
            elif node.module and node.module.startswith("tgms"):
                out.append((node.lineno, node.module))
    return out


def main() -> int:
    failures: list[str] = []
    checked = 0
    for rel in GUARDED:
        path = ROOT / rel
        if not path.exists():
            # The phase that creates it has not landed yet. Say so rather than
            # passing silently — a boundary check that vacuously passes is how
            # a module gets written before its rule is in force.
            print(f"--   {rel} does not exist yet")
            continue
        checked += 1
        bad = [(line, mod) for line, mod in imports_of(path) if mod not in ALLOWED]
        if not bad:
            print(f"ok   {rel}")
            continue
        for line, mod in bad:
            hint = next((h for prefix, h in FORBIDDEN_HINTS.items()
                         if mod.startswith(prefix)), "not on the allowlist")
            failures.append(f"{rel}:{line} imports {mod} — {hint}")
            print(f"FAIL {rel}:{line} imports {mod}")

    if failures:
        print(f"\n{len(failures)} import-boundary violation(s):")
        for f in failures:
            print(f"  - {f}")
        print("\noutward, the allowlist is:")
        for mod in sorted(OUTWARD):
            print(f"  {mod}")
        print("plus the guarded set itself: " + ", ".join(sorted(INWARD)))
        return 1
    if not checked:
        print("no guarded module exists yet — nothing to check")
        return 0
    print(f"\n{checked} freshness module(s) reach outward only to "
          f"{', '.join(sorted(OUTWARD))} — a checker can run against a foreign log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
