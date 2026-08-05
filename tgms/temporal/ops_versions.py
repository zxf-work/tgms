"""O15 `version_history` (D-058): the belief log as a queryable population.

Every operator before this one answers *what did we believe* at some
`as_of_tt`. None answered *what did we revise, and when* — so a store built
around keeping corrections could not count them, which the independent
question study noticed before we did (bo-Q40, bo-Q45, bo-Q41).

This is `entity_history` without the uid: the same version rows, the same
bi-temporal censoring, over the whole store instead of one identity.

THE CONTRACT, and the part an implementation gets wrong first. Everything is
evaluated **as of `as_of_tt`**, including the question of what counts as
superseded:

  * a version whose `tt_s` is after `as_of_tt` **does not exist yet** and
    appears in no mode;
  * a version whose belief ended after `as_of_tt` had not ended yet, so it
    is *current* at that `as_of` and its `tt_e` is reported as `OPEN_END` —
    the same censoring O1 applies, for the same reason;
  * therefore `belief: "superseded"` as of a transaction time before a
    correction is honestly an **empty page**.

Without that, a pinned result leaks knowledge the caller's belief state does
not have, and bi-temporal immutability stops being a property of the system.

WHAT "HOW MANY CORRECTIONS" MEANS HERE. A count of **superseded** versions:
beliefs that were revised. A store nobody has corrected answers 0; a
`correct` that carves one version into three closes exactly one belief and
so counts once. Counting the *replacements* instead would count the
fragments, which are the same belief re-expressed rather than a revision.

Rows are ordered by `(tt_s, vid)` — the belief log's own order, not valid
time. That ordering is the difference between this operator and a snapshot.
Props are deliberately not returned: this reports the *shape* of the log,
and a props column would put a JSON blob on every row of a whole-store scan.
"""

from __future__ import annotations

import numpy as np

from typing import Any

from tgms.core.errors import InvalidArgError
from tgms.core.model import OPEN_END, clamp_tt
from tgms.storage.base import StorageAdapter
from tgms.temporal.algebra import (
    AS_OF_TT,
    CURSOR,
    LIMIT,
    WINDOW,
    check_window,
    operator,
    paginate,
    required,
)

def _version_cost(args: dict[str, Any], stats: dict[str, Any]) -> dict[str, int]:
    """This operator is in a different cost class from the rest of the
    algebra, and the guardrail has to know it.

    `all_*_versions` materializes one Python object per version; every other
    operator reads packed columns. Measured at 10,000,000 versions: 153.9 s
    and 13.3 GB peak RSS, against 1.3 s and 2.3 GB for a columnar count over
    the same population — 116x the time, 54x the bytes per row (D-058).

    Two departures from `scan_estimate`, both of them the truth rather than
    a safety margin. **No window pruning**, because there is no pushdown:
    the window filters the output, not the work, so a narrow window must not
    make the call look cheap. And the per-row cost is charged against
    `expansions_est`, because per-row allocation is what that ceiling is
    for — which puts the refusal at around 5M versions, on the right side of
    a call that would otherwise run for two and a half minutes.
    """
    n = int(stats.get("n_edge_versions", 0)) + int(stats.get("n_node_versions", 0))
    return {"rows_scanned_est": n, "expansions_est": n}


#: Columns dropped from the version row. `props` is a blob and this
#: operator is a whole-store scan; `source`/`provenance_ref` are reserved
#: for Phase-3 write-back and are constant for every row written so far.


def _version_validators(args: dict[str, Any]) -> None:
    check_window(args)
    if args["kind"] == "node" and args.get("rel_types") is not None:
        raise InvalidArgError(
            "'rel_types' is only meaningful with kind 'edge'; node versions "
            "have a label, not a relationship type")


@operator(
    "version_history",
    {
        "kind": required({"type": "string", "enum": ["node", "edge"]}),
        "window": required(WINDOW),
        "belief": {"type": "string",
                   "enum": ["current", "superseded", "all"],
                   "default": "current",
                   "description": "which beliefs, AS OF as_of_tt: current = "
                                  "still believed then, superseded = revised "
                                  "by then, all = written by then"},
        "rel_types": {"type": ["array", "null"], "items": {"type": "string"},
                      "default": None},
        "as_of_tt": AS_OF_TT,
        "limit": LIMIT,
        "cursor": CURSOR,
    },
    "The belief log itself: version rows of nodes or edges whose valid-time "
    "interval overlaps `window`, with BOTH clocks on every row (vt_s, vt_e, "
    "tt_s, tt_e) and ordered by transaction time. `belief` selects which "
    "beliefs, always as of `as_of_tt`: 'current' = still believed then, "
    "'superseded' = already revised by then, 'all' = everything written by "
    "then. **How many corrections exist is a count of `superseded` rows.** "
    "A version written after as_of_tt does not appear, and a belief that "
    "ended after as_of_tt is reported as still open — so a pinned result "
    "never leaks a revision the caller's belief state has not seen. Props "
    "are not returned; use entity_history for one identity's full rows. "
    "COST: this materializes the whole version log — no columnar version "
    "scan exists — so it is refused by the cost guardrail on stores past a "
    "few million versions, and the window prunes the answer rather than the "
    "work.",
    cost_fn=_version_cost,
    validators=[_version_validators],
    output_fields=("rows", "rows_total", "truncated", "cursor"),
)
def version_history(adapter: StorageAdapter, args: dict[str, Any]) -> dict[str, Any]:
    as_of = clamp_tt(args["as_of_tt"])
    t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
    belief, kind = args["belief"], args["kind"]
    rel_types = set(args["rel_types"]) if args["rel_types"] is not None else None

    # Columns, not objects: the censoring and the window are masks, the
    # ordering is a lexsort over two of them, and only the page is ever
    # built into rows. The old path materialized the whole population —
    # 64 s of object construction at 10M — to return at most `limit` rows
    # (D-069). `rows_total` is still exact because the mask counts.
    cols = adapter.versions_columnar(kind)
    tt_s, tt_e, vt_s, vt_e = (cols["tt_s"], cols["tt_e"],
                              cols["vt_s"], cols["vt_e"])
    superseded = tt_e <= as_of
    keep = (tt_s <= as_of) & (vt_s < t_b) & (t_a < vt_e)
    if belief == "current":
        keep &= ~superseded
    elif belief == "superseded":
        keep &= superseded
    if rel_types is not None:
        keep &= np.isin(cols["rel_type"], list(rel_types))
    idx = np.flatnonzero(keep)
    # (tt_s, vid) — the last key to lexsort is the primary one
    idx = idx[np.lexsort((cols["vid"][idx], tt_s[idx]))]

    names = [c for c in adapter.VERSION_COLS[kind]]
    page = paginate(idx.tolist(), args["limit"], args["cursor"])
    page["rows"] = [
        {**{c: (int(cols[c][i]) if c in adapter.VERSION_INT_COLS
                else cols[c][i]) for c in names},
         "tt_e": int(tt_e[i]) if superseded[i] else OPEN_END}
        for i in page["rows"]
    ]
    return page
