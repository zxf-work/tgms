"""The three selections and `Project` — §2.4, §2.5, §2.6, §2.7.

All four preserve their input's order by construction: boolean-mask indexing
keeps rows in input order, and a projection touches no row at all. §3.4's rule
that "only four node kinds ever sort" is what makes eleven of the twelve
operators free of an `n log n`, and these are four of the eleven.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.errors import InvalidArgError
from tgms.temporal import props as props_module
from tgms.tgir.eval.expr_eval import eval_expr, eval_predicate
from tgms.tgir.eval.masks import mask_in
from tgms.tgir.node import Filter, Project, PropertyPredicate, TypeConstraint
from tgms.tgir.relation import Relation, array_for
from tgms.tgir.types import Column, Schema

#: §2.7's comparison spellings → `tgms/temporal/props.py`'s vocabulary. The
#: property module is the single source of D-052's type-fit rule, shared by the
#: kernel, the oracle and the SQL twins, so `PropertyPredicate` translates into
#: its vocabulary rather than reimplementing the rule.
CMP_TO_PROPS = {"=": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}


def eval_filter(node: Filter, rel: Relation) -> Relation:
    """§2.4. Keeps rows where the predicate is **true** — a null result is not
    true. `Filter` narrows the declared *domain*, never Σ, and therefore never a
    dependency scope (D13.12: a later correction can make a row that was
    filtered out pass the predicate, and that row was inside the scan's region
    all along)."""
    return rel.filter(eval_predicate(node.pred, rel))


def eval_type_constraint(node: TypeConstraint, rel: Relation) -> Relation:
    """§2.6. A node's label is a property of the *version* valid in Σ, not of
    the identity — so this reads the label column the scan already produced
    under Σ, and needs no second read.

    `labels` is a plain union list and there is **no hierarchy** (§8.17): LDBC's
    `Message` is compiled at bind time as `labels: ["Post", "Comment"]`.
    """
    if node.labels is not None:
        column = f"{node.var}.label"
        wanted = np.array(node.labels, dtype=object)
    else:
        column = f"{node.var}.rel_type"
        wanted = np.array([node.rel_type], dtype=object)
    values = rel.column(column)
    keep = mask_in(values, wanted)
    mask = rel.null_mask(column)
    if mask is not None:
        keep &= ~mask
    return rel.filter(keep)


def eval_property_predicate(node: PropertyPredicate,
                            rel: Relation) -> tuple[Relation, dict[str, Any]]:
    """§2.5, and the reason it is a named operator rather than sugar for
    `Filter`: the **coercion accounting**.

    A value participates only if its JSON type fits the comparison — text is
    never parsed into a number, and a boolean is not one (D-052). Rows excluded
    *by type mismatch* are counted separately from rows excluded by the
    comparison, because an answer must not rest on a shrunken denominator
    without saying so. The counts ride out as `prop_coercion` metadata.
    """
    column = f"{node.var}.props"
    bags = rel.column(column)
    bag_nulls = rel.null_mask(column)
    cmp_name = CMP_TO_PROPS[node.cmp]

    keep = np.zeros(rel.n, dtype=bool)
    skipped = 0
    for i in range(rel.n):
        if bag_nulls is not None and bag_nulls[i]:
            skipped += 1
            continue
        verdict = props_module.matches(bags[i], node.prop, cmp_name, node.value)
        if verdict is props_module.SKIP:
            skipped += 1
        elif verdict:
            keep[i] = True
    coercion = {"prop": node.prop, "considered": int(rel.n), "skipped": int(skipped),
                "matched": int(keep.sum())}
    return rel.filter(keep), coercion


def eval_project(node: Project, rel: Relation) -> Relation:
    """§2.7. Each binding's τ was inferred at construction, so this evaluates
    and attaches; the schema is not re-derived here.

    `keep = "all"` prefixes the input schema — and a collision between an input
    column and a projected name is a static plan error the node layer already
    refused (§4.2: "name collisions are a static plan error, never silently
    resolved").
    """
    columns, values, nulls = [], {}, {}
    for name, expr in node.bindings:
        tau = expr.tau(rel.schema)
        got, null_mask = eval_expr(expr, rel)
        columns.append(Column(name, tau))
        values[name] = array_for(tau, got)
        if null_mask is not None:
            nulls[name] = null_mask
    listed = Relation(Schema(tuple(columns)), values, rel.n, nulls)
    if node.keep == "all":
        return rel.with_columns(listed.schema, listed.cols, listed.nulls)
    return listed


def check_join_keys(rel: Relation, names: tuple[str, ...], side: str) -> None:
    """§2.8: **join keys must be non-null; a null key is an error, not a
    non-match.** Checked before the build, so the failure names the key rather
    than surfacing as a missing row."""
    for name in names:
        if rel.has_nulls(name):
            count = int(rel.is_null(name).sum())
            raise InvalidArgError(
                f"null join key {name!r} on the {side} side at {count} row(s) — "
                f"§2.8 makes a null key an error, never a non-match",
                column=name, rows=count)


__all__ = ["CMP_TO_PROPS", "check_join_keys", "eval_filter", "eval_project",
           "eval_property_predicate", "eval_type_constraint"]
