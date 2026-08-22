"""Double-keyed gold for the seven scoreable independent rows.

Each answer is computed **two independent ways** — once in SQL over the store's
DuckDB file, once in pure Python over the columnar edge list — and the two must
agree before the number is gold. That is the discipline
`scripts/independent_questions.py` already applies to its three gold functions,
and the reason is that a single implementation is a hypothesis, not a reference:
an error in the one that also wrote the plan would be invisible.

**The wording rulings are the frozen ones**, quoted from `forecast.yaml`'s
`gold_notes` at each function, never re-derived. They are the part a reader has
to trust, so they are the part that cites its source.

bo41 is **excluded from scoring in advance** (spec §8.13: the canonical
bitcoin-otc store carries zero corrections), so it has no gold here.

    uv run python scripts/tgir_gold.py [--only bo33] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
BITCOINOTC = ROOT / "stores/bitcoinotc"
COLLEGEMSG = ROOT / "stores/collegemsg"
DAY = 86_400_000_000
HOUR = 3_600_000_000
MINUTE = 60_000_000


# ---------------------------------------------------------------------------
# substrate
# ---------------------------------------------------------------------------

def edges(store_path: Path, rel_type: str,
          positive_only: bool = False) -> list[tuple[str, str, int]]:
    """`(src, dst, vt_s)` for every believed edge version, Python key."""
    import tgms
    from tgms.temporal.props import parse_props

    store = tgms.open(store_path, read_only=True)
    try:
        adapter = store.adapter
        cols = adapter.edges_columnar(columns=("src_id", "dst_id", "vt_s",
                                               "rel_type", "props"))
        uids = adapter.uids_for(list(range(adapter.num_entities())))
        out = []
        for i in range(len(cols["src_id"])):
            if cols["rel_type"][i] != rel_type:
                continue
            if positive_only:
                rating = parse_props(cols["props"][i]).get("rating")
                if not (isinstance(rating, (int, float))
                        and not isinstance(rating, bool) and rating > 0):
                    continue
            out.append((uids[cols["src_id"][i]], uids[cols["dst_id"][i]],
                        int(cols["vt_s"][i])))
        return out
    finally:
        store.close()


def sql(store_path: Path, query: str, params: tuple = ()) -> Any:
    """The SQL key: straight at the store's DuckDB file, sharing no code with
    the Python key or with the evaluator."""
    import duckdb

    connection = duckdb.connect(str(store_path / "store.duckdb"), read_only=True)
    try:
        return connection.execute(query, list(params)).fetchall()
    finally:
        connection.close()


POSITIVE_SQL = """
SELECT e.src, e.dst, e.vt_s
FROM edge_versions e
WHERE e.rel_type = 'TRUST' AND e.tt_e >= 4611686018427387904
  AND CAST(json_extract(e.props, '$.rating') AS DOUBLE) > 0
"""

TRUST_ANY_SQL = """
SELECT DISTINCT e.src, e.dst FROM edge_versions e
WHERE e.rel_type = 'TRUST' AND e.tt_e >= 4611686018427387904
"""

MSG_SQL = """
SELECT e.src, e.dst, e.vt_s FROM edge_versions e
WHERE e.rel_type = 'MSG' AND e.tt_e >= 4611686018427387904
"""


# ---------------------------------------------------------------------------
# the seven
# ---------------------------------------------------------------------------

def gold_bo31() -> dict[str, Any]:
    """"Count distinct ordered triples (A,B,C) of three different accounts such
    that A rated B positively and B rated C positively, and A has no TRUST edge
    to C at all (any rating, any time)."

    `A != C must be forced explicitly because a 2-path does not force it`
    (merged.yaml) — so the count excludes A == C rather than relying on the
    absent-chord test to do it.
    """
    positive = {(s, d) for s, d, _ in edges(BITCOINOTC, "TRUST", True)}
    any_trust = {(s, d) for s, d, _ in edges(BITCOINOTC, "TRUST")}
    out = defaultdict(set)
    for src, dst in positive:
        out[src].add(dst)
    python_key = sum(
        1
        for a in out
        for b in out[a]
        for c in out.get(b, ())
        if c != a and b != a and (a, c) not in any_trust)

    rows = sql(BITCOINOTC, POSITIVE_SQL)
    any_rows = sql(BITCOINOTC, TRUST_ANY_SQL)
    pos = {(r[0], r[1]) for r in rows}
    anye = {(r[0], r[1]) for r in any_rows}
    adj = defaultdict(set)
    for s, d in pos:
        adj[s].add(d)
    sql_key = sum(1 for a in adj for b in adj[a] for c in adj.get(b, ())
                  if c != a and b != a and (a, c) not in anye)
    return {"python": python_key, "sql": sql_key}


def gold_bo33() -> dict[str, Any]:
    """"Count distinct accounts X for which there exist at least three raters
    that each rated X positively and that all rated each other positively."

    `'rated each other' is read as MUTUAL (both directions positive)` — the
    reading that collapses "at least 3" to a positive 3-clique. Node
    distinctness among r1,r2,r3,x is **derived** on this store: one TRUST
    identity per ordered pair means x = r1 would force two edge variables onto
    one eid, which identity-isomorphism forbids.
    """
    def count(pairs: set[tuple[str, str]]) -> int:
        out, mutual = defaultdict(set), defaultdict(set)
        for src, dst in pairs:
            out[src].add(dst)
        for src, dst in pairs:
            if (dst, src) in pairs:
                mutual[src].add(dst)
        targets = set()
        for u in sorted(mutual):
            for v in mutual[u]:
                if v <= u:
                    continue
                for w in mutual[u] & mutual[v]:
                    if w <= v:
                        continue
                    for x in out[u] & out[v] & out[w]:
                        if len({(u, v), (v, u), (u, w), (w, u), (v, w), (w, v),
                                (u, x), (v, x), (w, x)}) == 9:
                            targets.add(x)
        return len(targets)

    python_key = count({(s, d) for s, d, _ in edges(BITCOINOTC, "TRUST", True)})
    sql_key = count({(r[0], r[1]) for r in sql(BITCOINOTC, POSITIVE_SQL)})
    return {"python": python_key, "sql": sql_key}


def gold_bo35() -> dict[str, Any]:
    """"Count distinct accounts X such that there are accounts Z and Y with X
    rating Z positively, Z rating Y positively, and Y rating X positively,
    while X never rated Y at all" — a positive 3-cycle carrying one absent
    chord."""
    def count(positive: set[tuple[str, str]],
              any_trust: set[tuple[str, str]]) -> int:
        out = defaultdict(set)
        for src, dst in positive:
            out[src].add(dst)
        found = set()
        for x in out:
            for z in out[x]:
                for y in out.get(z, ()):
                    if x in out.get(y, ()) and (x, y) not in any_trust \
                            and len({x, y, z}) == 3:
                        found.add(x)
        return len(found)

    python_key = count({(s, d) for s, d, _ in edges(BITCOINOTC, "TRUST", True)},
                       {(s, d) for s, d, _ in edges(BITCOINOTC, "TRUST")})
    sql_key = count({(r[0], r[1]) for r in sql(BITCOINOTC, POSITIVE_SQL)},
                    {(r[0], r[1]) for r in sql(BITCOINOTC, TRUST_ANY_SQL)})
    return {"python": python_key, "sql": sql_key}


def gold_bo37() -> dict[str, Any]:
    """"Decide whether three accounts A,B,C exist with A→B, B→C, C→A all
    positive and the three rating events in that order in time."

    `the ordering constraint is read as STRICT ON vt_s ALONE` — the question is
    silent on simultaneous ratings, and this gold does not break ties by eid
    even though the existing engine's motif ordering does.
    """
    def decide(events: list[tuple[str, str, int]]) -> bool:
        by_src = defaultdict(list)
        for src, dst, t in events:
            by_src[src].append((t, dst))
        for a, first in by_src.items():
            for t1, b in first:
                for t2, c in by_src.get(b, ()):
                    if t2 <= t1 or c == a or c == b:
                        continue
                    for t3, back in by_src.get(c, ()):
                        if t3 > t2 and back == a:
                            return True
        return False

    python_key = decide(edges(BITCOINOTC, "TRUST", True))
    sql_key = decide([(r[0], r[1], int(r[2])) for r in sql(BITCOINOTC, POSITIVE_SQL)])
    return {"python": python_key, "sql": sql_key}


def gold_cm13() -> dict[str, Any]:
    """"Count distinct account pairs {A,B} for which some message A→B and some
    message B→A occurred within one hour of each other."

    `pairs are UNORDERED (the exchange is one phenomenon, counted once)` and
    `'within the same hour' is a 60-MINUTE DELTA IN EITHER DIRECTION, not a
    shared clock-hour bucket`.
    """
    def count(events: list[tuple[str, str, int]]) -> int:
        by_pair = defaultdict(list)
        for src, dst, t in events:
            by_pair[(src, dst)].append(t)
        found = set()
        for (a, b), times in by_pair.items():
            reverse = by_pair.get((b, a))
            if not reverse or a == b:
                continue
            for t in times:
                if any(abs(t - u) <= HOUR for u in reverse):
                    found.add((a, b) if a < b else (b, a))
                    break
        return len(found)

    python_key = count(edges(COLLEGEMSG, "MSG"))
    sql_key = count([(r[0], r[1], int(r[2])) for r in sql(COLLEGEMSG, MSG_SQL)])
    return {"python": python_key, "sql": sql_key}


def gold_cm19() -> dict[str, Any]:
    """"Decide whether a1,a2,a3 exist such that a1 messaged a2 and, within 10
    minutes afterwards, a2 messaged a3, with both messages on the same calendar
    day" — three distinct accounts, a **forward** 10-minute window."""
    def decide(events: list[tuple[str, str, int]]) -> bool:
        by_src = defaultdict(list)
        for src, dst, t in events:
            by_src[src].append((t, dst))
        for a1, first in by_src.items():
            for t1, a2 in first:
                if a2 == a1:
                    continue
                for t2, a3 in by_src.get(a2, ()):
                    if t1 < t2 <= t1 + 10 * MINUTE and a3 not in (a1, a2) \
                            and t1 // DAY == t2 // DAY:
                        return True
        return False

    python_key = decide(edges(COLLEGEMSG, "MSG"))
    sql_key = decide([(r[0], r[1], int(r[2])) for r in sql(COLLEGEMSG, MSG_SQL)])
    return {"python": python_key, "sql": sql_key}


def gold_cm39() -> dict[str, Any]:
    """"Group all messages by (unordered account pair, UTC calendar day), sum
    both directions into one count per cell, and return the pair and day of the
    largest cell."

    `ties for the maximum cell are UNSPECIFIED by the question` — this gold
    reports the cell its stated tie-break selects (largest count, then the
    lexicographically smallest `(pair, day)`), and a differing tie choice is
    not scored as a miss.
    """
    def top(events: list[tuple[str, str, int]]) -> dict[str, Any]:
        cells: dict[tuple, int] = defaultdict(int)
        for src, dst, t in events:
            pair = (src, dst) if src < dst else (dst, src)
            cells[(pair, t // DAY)] += 1
        best = max(cells.items(), key=lambda kv: (kv[1], [-ord(c) for c in
                                                          str(kv[0])]))
        (pair, day), count = best
        ties = sum(1 for v in cells.values() if v == count)
        return {"pair": list(pair), "day": int(day), "count": int(count),
                "tied_cells": ties}

    python_key = top(edges(COLLEGEMSG, "MSG"))
    sql_key = top([(r[0], r[1], int(r[2])) for r in sql(COLLEGEMSG, MSG_SQL)])
    return {"python": python_key, "sql": sql_key}


GOLDS: dict[str, Callable[[], dict[str, Any]]] = {
    "bo31": gold_bo31, "bo33": gold_bo33, "bo35": gold_bo35, "bo37": gold_bo37,
    "cm13": gold_cm13, "cm19": gold_cm19, "cm39": gold_cm39,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "benchmarks/tgir-v1/gold.json"))
    args = ap.parse_args()

    wanted = args.only or sorted(GOLDS)
    results: dict[str, Any] = {}
    disagreements = []
    for row_id in wanted:
        store = BITCOINOTC if row_id.startswith("bo") else COLLEGEMSG
        if not store.exists():
            results[row_id] = {"status": "no-substrate"}
            continue
        keys = GOLDS[row_id]()
        agrees = keys["python"] == keys["sql"]
        results[row_id] = {**keys, "agrees": agrees,
                           "gold": keys["python"] if agrees else None}
        if not agrees:
            disagreements.append(row_id)
        print(f"{row_id:6} python={str(keys['python'])[:40]:42} "
              f"sql={str(keys['sql'])[:40]:42} {'AGREE' if agrees else 'DISAGREE'}")

    if args.json:
        print(json.dumps(results, indent=1))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
    print(f"\nwrote {args.out}")
    if disagreements:
        print(f"DISAGREEMENT on {disagreements} — neither key is gold until "
              f"they agree")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
