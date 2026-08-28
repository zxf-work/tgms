"""M4's import-boundary gate (M4_IMPLEMENTATION_PLAN §3.1, "non-negotiable"),
extended by M5 design memo §7.1 (`docs/design/M5_DESIGN.md`).

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

**M5 §7.1: the allowlist is now per-module, not one global set.** A single
global `ALLOWED` would let a change meant to permit `tgms/artifact/witness.py`
to import `tgms.tgir.level1` silently also permit `tgms/tgir/check.py` to
import it — the exact thing §6.2 forbids ("`level1` lives strictly
*downstream* of `check`... `check.py` gains no import"). `check.py`'s own
entry below is unchanged from before M5: this script is the thing that says
so, rather than a comment someone has to remember to re-read.

Five new modules join the guarded set (§7.1): `tgms/tgir/scan_region.py` and
`tgms/tgir/level1.py` (P1.3, not yet landed in this tree — this script
already handles a guarded module that does not exist yet, see below, so the
rule is in force before the code is), plus `tgms/artifact/{record,registry,
lookup,witness}.py` (P1.2, this package). `tgms/artifact/refresh.py`
**deliberately does not join** — refresh recomputes, so a module that opens a
store and runs a kernel cannot carry this allowlist's claim (§2.2).

P2.2 (`docs/design/M5_EXECUTION_PLAN_2026-08-27.md` §5, the two-hop
propagation demo) adds a sixth: `tgms/artifact/propagate.py` reads only the
registry's own in-memory fold (`Registry.current_generations`,
`Registry.current`) to answer "who names this artifact among their
`parents`, at a generation the registry has since left behind" — no store,
no event log, not even `tgms.tgir.check`'s machinery. It joins as a sibling
of the four P1.2 modules.

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

#: The four modules M4's plan named. Anything else under `tgms.` is a
#: violation for a module whose per-module allowlist below does not add it.
OUTWARD: frozenset[str] = frozenset({
    "tgms.core.model",
    "tgms.core.errors",
    "tgms.storage.eventlog",
    "tgms.tgir.depscope",
})

#: M5 §7.1's shared inward set for `tgms/artifact/{record,registry,lookup,
#: witness}.py`: the freshness-core modules those four may read from, plus
#: `tgms.tgir.scan_region` / `tgms.tgir.level1` (P1.3) ahead of their landing
#: — the same "add the guard before the module exists" posture this script
#: already takes (see the `does not exist yet` branch in `main`).
_ARTIFACT_INWARD: frozenset[str] = frozenset({
    "tgms.tgir.footprint",
    "tgms.tgir.check",
    "tgms.tgir.explain",
    "tgms.tgir.scan_region",
    "tgms.tgir.level1",
})

#: The four artifact modules may import each other (not `refresh` — §2.2).
_ARTIFACT_SIBLINGS: frozenset[str] = frozenset({
    "tgms.artifact.record",
    "tgms.artifact.registry",
    "tgms.artifact.lookup",
    "tgms.artifact.witness",
})

_ARTIFACT_ALLOWED: frozenset[str] = OUTWARD | _ARTIFACT_INWARD | _ARTIFACT_SIBLINGS

#: Per-module allowlist (§7.1's "the allowlist becomes per-module"). Order
#: matches the boundary table: M4's original three, then P1.3's two new
#: freshness-core modules, then P1.2's `tgms/artifact/` package.
ALLOWED: dict[str, frozenset[str]] = {
    # M4 — unchanged. `check.py` gains no import in M5 (§7.1 rule 1); this
    # entry is the thing that enforces that sentence.
    "tgms/tgir/footprint.py": OUTWARD,
    "tgms/tgir/check.py": OUTWARD | frozenset({"tgms.tgir.footprint"}),
    "tgms/tgir/explain.py": OUTWARD | frozenset({"tgms.tgir.check"}),

    # P1.3 (§6.1, §6.2) — not yet landed; guarded ahead of time.
    "tgms/tgir/scan_region.py": frozenset({
        "tgms.core.model", "tgms.core.errors", "tgms.tgir.depscope",
    }),
    "tgms/tgir/level1.py": OUTWARD | frozenset({
        "tgms.tgir.footprint", "tgms.tgir.check", "tgms.tgir.scan_region",
    }),

    # P1.2 — this package. §7.1 grants all four the same inward set; which
    # subset each actually uses today is a matter of current code, not of
    # what is permitted to change without touching this file again.
    "tgms/artifact/record.py": _ARTIFACT_ALLOWED,
    "tgms/artifact/registry.py": _ARTIFACT_ALLOWED,
    "tgms/artifact/lookup.py": _ARTIFACT_ALLOWED,
    "tgms/artifact/witness.py": _ARTIFACT_ALLOWED,
    "tgms/artifact/__init__.py": _ARTIFACT_SIBLINGS,

    # P2.2 — the parent-edge recheck. Reads only `record.py`/`registry.py`;
    # granted the same sibling set as `__init__.py` rather than a bespoke
    # narrower one, so a future addition to what it reads (still within the
    # artifact package) does not need a second edit here.
    "tgms/artifact/propagate.py": _ARTIFACT_SIBLINGS,
}

GUARDED: tuple[str, ...] = tuple(ALLOWED)

#: Guarded so the diagnosis names the actual hazard rather than "not allowed".
FORBIDDEN_HINTS: dict[str, str] = {
    "tgms.store": "the Store — D13.20 forbids the checker reading live store state",
    "tgms.storage.base": "a storage adapter — same reason",
    "tgms.storage.duckdb_adapter": "a storage adapter — same reason",
    "tgms.storage.native_adapter": "a storage adapter — same reason",
    "tgms.temporal": "an operator kernel — the checker never recomputes",
}


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
        allowed = ALLOWED[rel]
        bad = [(line, mod) for line, mod in imports_of(path) if mod not in allowed]
        if not bad:
            print(f"ok   {rel}")
            continue
        for line, mod in bad:
            hint = next((h for prefix, h in FORBIDDEN_HINTS.items()
                         if mod.startswith(prefix)), "not on this module's allowlist")
            failures.append(f"{rel}:{line} imports {mod} — {hint}")
            print(f"FAIL {rel}:{line} imports {mod}")

    if failures:
        print(f"\n{len(failures)} import-boundary violation(s):")
        for f in failures:
            print(f"  - {f}")
        print("\nper-module allowlists:")
        for rel in GUARDED:
            print(f"  {rel}: " + ", ".join(sorted(ALLOWED[rel])))
        return 1
    if not checked:
        print("no guarded module exists yet — nothing to check")
        return 0
    print(f"\n{checked} freshness module(s) stay within their own allowlist — "
          f"a checker can run against a foreign log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
