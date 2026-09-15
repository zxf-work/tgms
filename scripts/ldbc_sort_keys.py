"""Mechanically derive `benchmarks/ldbc-ref-v1/sort_keys.yaml` from each of the
24 campaign templates' *vendored, unmodified* `.cypher` file (RUNBOOK.md §8 —
"this repository does not vendor the `.cypher` files' `ORDER BY` clauses [as
data]"; it now vendors the files themselves, `external_workloads/ldbc/README.md`,
and this script reads their `ORDER BY` clause directly rather than a hand-typed
table).

**This is not an LDBC Benchmark and nothing produced here is an LDBC Benchmark
Result.** LDBC material is used under CC-BY 4.0.

What "mechanical" means here: for each template, take the **final** `ORDER BY`
clause in the query text (the one that governs the query's own output order —
`interactive-short-2.cypher` has an earlier one on a mid-query `WITH`, which
this script skips), split its comma-separated keys, and resolve each key
expression to the RETURN clause's own output column name:

* an unaliased key that already matches a RETURN column verbatim (Neo4j names
  an unaliased `RETURN` expression after its own text, e.g. `forum.id`) is
  used as-is;
* a key that matches the *pre-alias* expression of an aliased RETURN column
  (e.g. `ORDER BY ... person.id ASC` where `RETURN person.id AS personId`)
  resolves to that column's alias — this is the common LDBC pattern where the
  sort key is written against the bound variable, not the projected name;
* a key wrapped in a single-argument cast used only to fix sort order
  (`toInteger(personId)`, LDBC's own workaround for comparing numeric ids
  stored as strings) is unwrapped and the inner expression resolved the same
  way;
* a key that resolves to neither (e.g. `interactive-complex-5.cypher`'s
  `ORDER BY postCount DESC, forum.id ASC` — `forum.id` is never projected at
  all) is emitted as-is with `"unmapped": true`, a real limitation named
  rather than silently guessed at (RUNBOOK.md §8's own stance on this class of
  gap).

A template with no `ORDER BY` at all (the 5 `CURRENT_ECQR_FRAGMENT` ids plus
BI11, whose `RETURN count(*) AS count` is a single scalar row) gets an empty
`order_by: []` and `limit: null`.

    uv run python scripts/ldbc_sort_keys.py --campaign benchmarks/ldbc-ref-v1/campaign.yaml \\
        --out benchmarks/ldbc-ref-v1/sort_keys.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

_WS_RE = re.compile(r"\s+")


def _collapse_ws(s: str) -> str:
    return _WS_RE.sub(" ", s.strip())


#: Matches `ORDER BY <keys...>`, non-greedy, stopping before the next
#: top-level clause keyword or end of string. Cypher's grammar only allows
#: an optional `SKIP`/`LIMIT` between an `ORDER BY` and the next clause, so
#: this lookahead is exhaustive over the 24 vendored queries (checked by
#: hand against every file this script reads).
_ORDER_BY_RE = re.compile(
    r"ORDER\s+BY\s+(?P<keys>.*?)"
    r"(?=\n\s*(?:LIMIT|SKIP|RETURN|WITH|UNWIND|OPTIONAL\s+MATCH|MATCH|WHERE|"
    r"CALL|UNION)\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_LIMIT_RE = re.compile(r"\A\s*LIMIT\s+(?P<n>\d+)", re.IGNORECASE)
_RETURN_RE = re.compile(r"\bRETURN\b", re.IGNORECASE)
_AS_ALIAS_RE = re.compile(r"^(?P<expr>.*?)\s+AS\s+(?P<alias>[A-Za-z_]\w*)\s*$",
                          re.IGNORECASE | re.DOTALL)
_KEY_DIR_RE = re.compile(r"^(?P<expr>.*?)(?:\s+(?P<dir>ASC|DESC))?\s*$",
                         re.IGNORECASE | re.DOTALL)
_WRAP_FN_RE = re.compile(r"^[A-Za-z_]\w*\(\s*(?P<inner>[^()]+?)\s*\)$")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` at paren-depth 0 only — a `count(DISTINCT message)` or
    `CASE ... END` argument list must not be split internally."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _return_columns(cypher: str, order_by_start: int) -> tuple[set[str], dict[str, str]]:
    """`(final_names, expr_to_alias)` for the `RETURN` clause nearest before
    `order_by_start` (or, if there is no `ORDER BY`, the caller passes
    `len(cypher)` and this reads the last `RETURN` in the file). `final_names`
    is every name the query's own output actually carries (an alias, or an
    unaliased expression's own text, Neo4j's default column name).
    `expr_to_alias` maps a *pre-alias* expression to the alias it was given,
    for keys written against the bound variable rather than the projection."""
    return_matches = list(_RETURN_RE.finditer(cypher, 0, order_by_start))
    if not return_matches:
        return set(), {}
    start = return_matches[-1].end()
    # up to the next ORDER BY/LIMIT or end of the slice we were given
    tail = cypher[start:order_by_start]
    limit_cut = re.search(r"\bLIMIT\b", tail, re.IGNORECASE)
    if limit_cut:
        tail = tail[:limit_cut.start()]
    final_names: set[str] = set()
    expr_to_alias: dict[str, str] = {}
    for col in _split_top_level(tail):
        col = _collapse_ws(col)
        if not col:
            continue
        m = _AS_ALIAS_RE.match(col)
        if m:
            expr = _collapse_ws(m.group("expr"))
            alias = m.group("alias")
            final_names.add(alias)
            expr_to_alias[expr] = alias
        else:
            final_names.add(col)
    return final_names, expr_to_alias


def _resolve(expr: str, final_names: set[str], expr_to_alias: dict[str, str]
            ) -> tuple[str, bool]:
    """One `ORDER BY` key expression -> `(output_column_name, unmapped)`."""
    candidates = [expr]
    m = _WRAP_FN_RE.match(expr)
    if m:
        candidates.append(_collapse_ws(m.group("inner")))
    for cand in candidates:
        if cand in final_names:
            return cand, False
        if cand in expr_to_alias:
            return expr_to_alias[cand], False
    # nothing matched: best-effort raw text, flagged rather than guessed at
    return candidates[-1], True


def parse_order_by(cypher: str) -> dict[str, Any]:
    """The final `ORDER BY` in `cypher`, resolved against its own `RETURN`
    clause. Returns `{"order_by": [{"column", "direction", "unmapped"?}, ...],
    "limit": int | None}`."""
    matches = list(_ORDER_BY_RE.finditer(cypher))
    if not matches:
        return {"order_by": [], "limit": None}
    last = matches[-1]
    final_names, expr_to_alias = _return_columns(cypher, last.start())

    limit = None
    rest = cypher[last.end():]
    m = _LIMIT_RE.match(rest)
    if m:
        limit = int(m.group("n"))

    order_by: list[dict[str, Any]] = []
    for raw_key in _split_top_level(last.group("keys")):
        raw_key = _collapse_ws(raw_key)
        if not raw_key:
            continue
        km = _KEY_DIR_RE.match(raw_key)
        expr = _collapse_ws(km.group("expr"))
        direction = (km.group("dir") or "ASC").upper()
        column, unmapped = _resolve(expr, final_names, expr_to_alias)
        entry: dict[str, Any] = {"column": column, "direction": direction}
        if unmapped:
            entry["unmapped"] = True
        order_by.append(entry)
    return {"order_by": order_by, "limit": limit}


def build_sort_keys(campaign: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    templates: dict[str, Any] = {}
    for t in campaign["templates"]:
        pid = t["id"]
        cypher_rel = t["cypher"]
        cypher_path = root / "external_workloads" / "ldbc" / cypher_rel
        text = cypher_path.read_text()
        parsed = parse_order_by(text)
        templates[pid] = {"cypher": cypher_rel, **parsed}
    return {
        "generated_by": "scripts/ldbc_sort_keys.py",
        "source_note": ("mechanically parsed from each vendored query's own "
                        "final ORDER BY clause (RUNBOOK.md §8); "
                        "column names are resolved against that query's own "
                        "RETURN clause, not hand-transcribed"),
        "templates": templates,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", default=str(ROOT / "benchmarks" / "ldbc-ref-v1"
                                              / "campaign.yaml"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "ldbc-ref-v1"
                                        / "sort_keys.yaml"))
    args = ap.parse_args()

    campaign = yaml.safe_load(Path(args.campaign).read_text())
    doc = build_sort_keys(campaign)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    print(f"wrote {out} ({len(doc['templates'])} templates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
