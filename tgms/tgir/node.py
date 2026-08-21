"""TGIR plan nodes: the twelve compositional core operators (TGIR_SPEC §2) and
the `OpaqueLeaf` that carries the fifteen existing operators (R7).

Every node is a **frozen dataclass**, and every node satisfies §2.0's common
shape: `node ::= Op(inputs…, args…) @ Σ`. Of §2.0's six obligations, M2.0
discharges the ones that are data:

1. *signature* — `out_schema`, propagated per §4.2, validated at construction;
2. *bi-temporal evaluation rule under Σ* — `sigma`, with §3.5's rule that an
   input subtree may narrow `T_v` and may never widen it, checked here;
3. *canonical output order* — recorded per node in `canonical_order`, as
   documentation of the contract rather than as an implementation;
4. *cost-guard hook* — **not** M2.0's (§2.13 admission stays where it is);
5. *metadata propagation* — `tgms.tgir.metadata`;
6. *dependency-scope rule or an explicit `∅`* — `reads_store` here, the
   derivation in `tgms.tgir.scope_of`, the adapter-withholding guard in
   `tgms.tgir.guard`.

**No evaluators.** Nothing in this module reads a store, and nothing in the
package is wired into `call_operator`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, fields
from typing import Any, Literal

from tgms.core.errors import InvalidArgError
from tgms.core.model import digest
from tgms.tgir.expr import CMP_OPS, Expr
from tgms.tgir.types import (
    DEFAULT_SIGMA, EDGE_COLUMNS, NODE_COLUMNS, PATTERN_NODE_COLUMNS, Column,
    Schema, Sigma, T_FLOAT, T_INT, Tau, check_belief, check_vt_mode,
    edge_schema, node_schema,
)

JoinType = Literal["inner", "left_outer", "anti"]
JOIN_TYPES: frozenset[str] = frozenset({"inner", "left_outer", "anti"})

Direction = Literal["out", "in", "both"]
DIRECTIONS: frozenset[str] = frozenset({"out", "in", "both"})

AggFn = Literal["count", "count_distinct", "sum", "min", "max", "mean"]
AGG_FNS: frozenset[str] = frozenset({"count", "count_distinct", "sum", "min", "max", "mean"})

SortDir = Literal["asc", "desc"]
NullsOrder = Literal["nulls_first", "nulls_last"]

#: §6 #15 / FRESHNESS_SEMANTICS D13.11: `compute` is the only one of the
#: fifteen whose kernel never touches the adapter, so it is the only `∅`
#: leaf. See `tgms.tgir.guard` for the check that makes the classification
#: falsifiable rather than decorative.
EMPTY_SCOPE_OPS: frozenset[str] = frozenset({"compute"})


class Node(abc.ABC):
    """A node in a plan DAG. Subclasses are frozen dataclasses."""

    __slots__ = ()

    #: §2.0 obligation 6 — does this node read store state? `False` is the `∅`
    #: classification and is a *checkable* property (`tgms.tgir.guard`).
    reads_store: bool = False

    #: §2.0 obligation 3 — the total order the node's output is defined to
    #: have, so plans are deterministic without an explicit `Order`.
    canonical_order: str = ""

    @property
    @abc.abstractmethod
    def sigma(self) -> Sigma: ...

    @property
    def inputs(self) -> tuple["Node", ...]:
        return ()

    @property
    @abc.abstractmethod
    def out_schema(self) -> Schema: ...

    @property
    def op(self) -> str:
        return type(self).__name__

    # -- validation ------------------------------------------------------
    def _check_inputs(self) -> None:
        """§3.5: a subtree may narrow `T_v`, never widen it, and `T_b` is
        plan-global in v1."""
        for i in self.inputs:
            if not self.sigma.covers(i.sigma):
                raise InvalidArgError(
                    "an input's Σ widens its consumer's — no node may widen T_v, "
                    "and T_b is plan-global in v1",
                    node=self.op, outer=self.sigma.to_json(), inner=i.sigma.to_json(),
                )

    # -- provenance ------------------------------------------------------
    def canonical_args(self) -> dict[str, Any]:
        """The node's bound arguments, canonically. Inputs and Σ are excluded —
        `node_digest` carries them separately (§5.4)."""
        out: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            if f.name in ("sigma",):
                continue
            value = getattr(self, f.name)
            if isinstance(value, Node) or _is_node_seq(value):
                continue
            out[f.name] = _jsonify(value)
        return out

    @property
    def node_digest(self) -> str:
        """§5.4: a content digest over `(op, canonical args with parameters
        bound, Σ, the input nodes' digests)` — a Merkle digest of the plan
        subtree. Every component is *data*, which is why the opaque leaf holds
        bound args rather than a kernel callable."""
        return digest({
            "op": self.op,
            "args": self.canonical_args(),
            "sigma": self.sigma.to_json(),
            "inputs": [i.node_digest for i in self.inputs],
        })


def _is_node_seq(value: Any) -> bool:
    return isinstance(value, tuple) and bool(value) and all(isinstance(v, Node) for v in value)


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "to_json"):
        return value.to_json()
    if isinstance(value, (tuple, list)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    raise InvalidArgError(f"non-canonicalizable argument: {type(value).__name__}")


# ---------------------------------------------------------------------------
# §2.3's hop forms (R6)
# ---------------------------------------------------------------------------

class HopSpec(abc.ABC):
    """`exact(k) | bounded(a, b) | unbounded(a)`.

    The two families are **different relations** and the spec keeps them apart
    (§2.3, adjudication §8.7): `exact(k)` is a *walk* relation —
    multiplicity-preserving and edge-bindable — while `bounded`/`unbounded` are
    *node* relations, deduplicated at minimum depth. `bounded(k, k)` is
    therefore **not** silently normalized to `exact(k)`; an implementation must
    not rewrite one into the other.
    """

    __slots__ = ()

    #: Variable-length forms add `<into>.depth` and bind no edge variable.
    variable_length: bool = True

    @abc.abstractmethod
    def to_json(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class Exact(HopSpec):
    k: int
    variable_length = False

    def __post_init__(self) -> None:
        if self.k < 0:
            raise InvalidArgError(f"exact(k) needs k >= 0, got {self.k}")

    def to_json(self) -> Any:
        return {"exact": self.k}


@dataclass(frozen=True, slots=True)
class Bounded(HopSpec):
    a: int
    b: int

    def __post_init__(self) -> None:
        if not (0 <= self.a <= self.b):
            raise InvalidArgError(f"bounded(a, b) needs 0 <= a <= b, got ({self.a}, {self.b})")

    def to_json(self) -> Any:
        return {"bounded": [self.a, self.b]}


@dataclass(frozen=True, slots=True)
class Unbounded(HopSpec):
    a: int

    def __post_init__(self) -> None:
        if self.a not in (0, 1):
            raise InvalidArgError(f"unbounded(a) needs a in {{0, 1}}, got {self.a}")

    def to_json(self) -> Any:
        return {"unbounded": self.a}


# ---------------------------------------------------------------------------
# §2.1 / §2.2 — the two scans
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NodeScan(Node):
    """`NodeScan(as, labels?, uids?, belief) → as.(uid, vid, label, vt_s, vt_e,
    tt_s, tt_e, props)`.

    `labels` is a **plain union list, not a hierarchy** (§2.1, adjudication
    §8.17): a version matches when its label is any member. It is sugar for a
    union rather than subtyping — TGIR-v1 has no subtype relation.
    """

    as_: str
    labels: tuple[str, ...] | None = None
    uids: tuple[str, ...] | None = None
    belief: str = "current"
    vt_mode: str = "overlap"
    sigma_: Sigma = DEFAULT_SIGMA

    reads_store = True

    def __post_init__(self) -> None:
        if not self.as_:
            raise InvalidArgError("NodeScan.as is required — it binds the scan variable")
        check_belief(self.belief)
        check_vt_mode(self.vt_mode)
        if self.labels is not None and not self.labels:
            raise InvalidArgError("labels = [] matches nothing; omit it for an unrestricted scan")
        if self.uids is not None and not self.uids:
            raise InvalidArgError("uids = [] matches nothing; omit it for an unrestricted scan")

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def out_schema(self) -> Schema:
        return node_schema(self.as_)

    @property
    def canonical_order(self) -> str:
        return "(tt_s, vid)" if self.belief != "current" else "(vt_s, vid)"


@dataclass(frozen=True, slots=True)
class EdgeScan(Node):
    """`EdgeScan(as, rel_types?, endpoints?, belief) → as.(eid, vid, src, dst,
    rel_type, disc, vt_s, vt_e, tt_s, tt_e, props)`.

    `endpoints` is an incidence pushdown and **the only cohort restriction in
    the algebra that is not a `Join`** (§2.2): a `Join` against a uid list would
    multiply rows where an edge has both endpoints in the cohort, while
    `endpoints` selects each matching edge version exactly once.
    """

    as_: str
    rel_types: tuple[str, ...] | None = None
    endpoints: "Endpoints | None" = None
    belief: str = "current"
    vt_mode: str = "overlap"
    sigma_: Sigma = DEFAULT_SIGMA

    reads_store = True

    def __post_init__(self) -> None:
        if not self.as_:
            raise InvalidArgError("EdgeScan.as is required — it binds the scan variable")
        check_belief(self.belief)
        check_vt_mode(self.vt_mode)
        if self.rel_types is not None and not self.rel_types:
            raise InvalidArgError("rel_types = [] matches nothing; omit it instead")

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def out_schema(self) -> Schema:
        return edge_schema(self.as_)

    @property
    def canonical_order(self) -> str:
        return "(tt_s, vid)" if self.belief != "current" else "(vt_s, vid)"


@dataclass(frozen=True, slots=True)
class Endpoints:
    """`{role, uids}` — the storage layer's own scan signature (§2.2). All four
    roles are real; `both` is the one genuine narrowing the enum offers, and
    omitting it was a gate finding (FRESHNESS_SEMANTICS D13.3/FF-8)."""

    role: str
    uids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.role not in INCIDENT_ROLES:
            raise InvalidArgError(f"unknown endpoint role: {self.role!r}",
                                  allowed=sorted(INCIDENT_ROLES))
        if not self.uids:
            raise InvalidArgError("endpoints.uids = [] matches nothing")

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "uids": list(self.uids)}


#: §2.2 and FRESHNESS_SEMANTICS D13.3 — one enum, four values, shared with the
#: dependency wire format's `incident` arm.
INCIDENT_ROLES: frozenset[str] = frozenset({"src", "dst", "either", "both"})


# ---------------------------------------------------------------------------
# §2.3 — Expand
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Expand(Node):
    """`Expand(input, from, rel_type?, dir, hops, into, edge_var?)`.

    Two restrictions are enforced here because they are structural:

    - **No edge bindings for variable-length forms** (§2.3): a variable number
      of edges cannot bind into a fixed row schema without list values, and v1
      has no list type. This is precisely why the path family stays outside the
      core.
    - **Structural closure only** — every hop is evaluated under one Σ, with no
      constraint that hop *i+1*'s `vt_s` exceed hop *i*'s. R6's unbounded
      `Expand` is therefore **not** the reachability operator;
      `temporal_reachability` imposes the ordering constraint and an
      earliest-arrival semiring and stays an opaque leaf (§6).
    """

    input: Node
    from_: str
    into: str
    hops: HopSpec
    rel_type: str | None = None
    dir: str = "out"
    edge_var: str | None = None
    sigma_: Sigma = DEFAULT_SIGMA

    reads_store = True

    def __post_init__(self) -> None:
        if self.dir not in DIRECTIONS:
            raise InvalidArgError(f"unknown direction: {self.dir!r}", allowed=sorted(DIRECTIONS))
        self.from_column  # noqa: B018 - resolves `from`, or fails at construction
        if self.edge_var is not None:
            if self.hops.variable_length:
                raise InvalidArgError(
                    "bounded/unbounded Expand binds `into` and `<into>.depth` only — "
                    "a variable number of edges cannot bind into a fixed row schema, "
                    "and v1 has no list type")
            if isinstance(self.hops, Exact) and self.hops.k == 0:
                raise InvalidArgError("exact(0) traverses no edge, so it binds no edge_var")
        self._check_inputs()
        self.out_schema  # noqa: B018 - fail at construction, not at first read

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def from_column(self) -> str:
        """The node-identity column `from` names. §2.3 writes `from: var`;
        L13.1's anchor rule is per *column* and its worked trap expands from
        `EdgeScan`'s `src`, so both spellings resolve here."""
        from tgms.tgir.anchor import uid_column  # local import: anchor reads nodes
        return uid_column(self.input, self.from_)

    @property
    def out_schema(self) -> Schema:
        out = self.input.out_schema.concat(Schema(NODE_COLUMNS).prefixed(self.into))
        if self.edge_var is not None:
            out = out.concat(Schema(EDGE_COLUMNS).prefixed(self.edge_var))
        if self.hops.variable_length:
            # `<into>.depth` is prefixed like every other column the operator
            # adds, so two variable-length expansions in one plan do not
            # collide (§2.3, §4.2).
            out = out.concat(Schema.of(Column(f"{self.into}.depth", T_INT)))
        return out

    @property
    def canonical_order(self) -> str:
        if self.hops.variable_length:
            return "(input row position, into.depth, into)"
        return "(input row position, (vt_s, vid))"


# ---------------------------------------------------------------------------
# §2.4 – §2.6 — the selections
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Filter(Node):
    """`Filter(input, pred) → input.schema`.

    `Filter` narrows the result's declared **domain** (§5.2); it does not narrow
    Σ, and therefore never narrows a dependency scope (§3.5's C3 ruling,
    FRESHNESS_SEMANTICS D13.12). A later correction can make a row that was
    filtered *out* pass the predicate, and that row was inside the scan's region
    all along.
    """

    input: Node
    pred: Expr
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        t = self.pred.tau(self.input.out_schema)
        if t.base not in ("bool", "json"):
            raise InvalidArgError(f"Filter.pred must be boolean, got {t.to_json()}")
        self._check_inputs()

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def out_schema(self) -> Schema:
        return self.input.out_schema

    @property
    def canonical_order(self) -> str:
        return "the input's, restricted"


@dataclass(frozen=True, slots=True)
class PropertyPredicate(Node):
    """`PropertyPredicate(input, var, prop, cmp, value) → input.schema`.

    Named rather than sugar for `Filter` because of the **D-052 type-fit rule**:
    a value participates only if its JSON type fits the comparison, and rows
    excluded *by type mismatch* are counted and reported as `prop_coercion`
    metadata. An answer must not rest on a shrunken denominator without saying
    so.
    """

    input: Node
    var: str
    prop: str
    cmp: str
    value: Any
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if self.cmp not in CMP_OPS:
            raise InvalidArgError(f"unknown comparison: {self.cmp!r}", allowed=sorted(CMP_OPS))
        if self.var not in self.input.out_schema.vars():
            raise InvalidArgError(f"PropertyPredicate names an unbound variable: {self.var!r}",
                                  bound=list(self.input.out_schema.vars()))
        if not self.prop:
            raise InvalidArgError("PropertyPredicate needs a property key")
        self._check_inputs()

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def out_schema(self) -> Schema:
        return self.input.out_schema

    @property
    def canonical_order(self) -> str:
        return "the input's, restricted"


@dataclass(frozen=True, slots=True)
class TypeConstraint(Node):
    """`TypeConstraint(input, var, labels)` for a node variable, or
    `TypeConstraint(input, var, rel_type)` for an edge variable.

    A node's label is a property of the *version* valid in Σ, not of the
    identity. `labels` is a union list and **no label hierarchy exists**
    (§2.6, adjudication §8.17): LDBC's `Message` is compiled at bind time as
    `labels: ["Post", "Comment"]`, so the pushdown survives without the IR
    carrying subtyping.
    """

    input: Node
    var: str
    labels: tuple[str, ...] | None = None
    rel_type: str | None = None
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if (self.labels is None) == (self.rel_type is None):
            raise InvalidArgError(
                "TypeConstraint takes labels (node variable) or rel_type (edge variable), "
                "exactly one")
        if self.labels is not None and not self.labels:
            raise InvalidArgError("labels = [] matches nothing")
        if self.var not in self.input.out_schema.vars():
            raise InvalidArgError(f"TypeConstraint names an unbound variable: {self.var!r}",
                                  bound=list(self.input.out_schema.vars()))
        self._check_inputs()

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def out_schema(self) -> Schema:
        return self.input.out_schema

    @property
    def canonical_order(self) -> str:
        return "the input's, restricted"


# ---------------------------------------------------------------------------
# §2.7 — Project
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Project(Node):
    """`Project(input, bindings, keep) → (listed names) [⧺ input.schema if
    keep = all]`.

    One restriction is statically checked here and it is exactly R2's boundary:
    a `Project` directly above an `Aggregate` may reference **at most one**
    aggregate output column per expression, plus constants. Combining two or
    more aggregate output columns is B1's `arithmetic-over-aggregates` — 8 rows,
    all `partial-rows` at every rung — and is beyond v1.
    """

    input: Node
    bindings: tuple[tuple[str, Expr], ...]
    keep: str = "listed"
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if self.keep not in ("all", "listed"):
            raise InvalidArgError(f"Project.keep must be 'all' or 'listed', got {self.keep!r}")
        if not self.bindings:
            raise InvalidArgError("Project needs at least one binding")
        in_schema = self.input.out_schema
        seen: set[str] = set()
        for name, e in self.bindings:
            if not name:
                raise InvalidArgError("a projected binding needs a name")
            if name in seen:
                raise InvalidArgError(f"duplicate projected name: {name!r}")
            seen.add(name)
            e.tau(in_schema)
        if isinstance(self.input, Aggregate):
            agg_cols = set(self.input.aggregate_columns)
            for name, e in self.bindings:
                used = [c for c in e.columns() if c in agg_cols]
                if len(used) > 1:
                    raise InvalidArgError(
                        "an expression may reference at most one aggregate output column "
                        "(arithmetic-over-aggregates is beyond v1)",
                        binding=name, aggregates=sorted(used))
        self._check_inputs()
        self.out_schema  # noqa: B018 - collisions surface at construction

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def out_schema(self) -> Schema:
        in_schema = self.input.out_schema
        listed = Schema(tuple(Column(n, e.tau(in_schema)) for n, e in self.bindings))
        return in_schema.concat(listed) if self.keep == "all" else listed

    @property
    def canonical_order(self) -> str:
        return "the input's"


# ---------------------------------------------------------------------------
# §2.8 — Join
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Join(Node):
    """`Join(left, right, on, type ∈ {inner, left_outer, anti})` (R3).

    Two rulings ride on this node:

    - **Duplicate probe keys** (§8.4, CLOSED): `anti` accepts them (duplicates
      cannot change an absence test) and `left_outer` accepts them *with
      multiplication*. Rejecting instead would turn two `yes` rows into
      refusals on any corrected store, since every correction produces several
      believed versions per identity.
    - **The completeness precondition** (§2.8, §5.3): `left_outer` and `anti`
      derive rows from *absence* on the right and refuse (`E_INCOMPLETE`)
      unless the right input is execution-complete. That is a runtime refusal —
      truncation is not knowable at plan time — so M2.0 records it in
      `tgms.tgir.metadata` rather than checking it here.
    """

    left: Node
    right: Node
    on: tuple[tuple[str, str], ...]
    join_type: str = "inner"
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if self.join_type not in JOIN_TYPES:
            raise InvalidArgError(f"unknown join type: {self.join_type!r}",
                                  allowed=sorted(JOIN_TYPES))
        if not self.on:
            raise InvalidArgError("Join needs at least one key pair")
        ls, rs = self.left.out_schema, self.right.out_schema
        for lcol, rcol in self.on:
            lt, rt = ls.tau_of(lcol), rs.tau_of(rcol)
            if not lt.same_base(rt):
                raise InvalidArgError(
                    "join key types are incompatible",
                    left=[lcol, lt.to_json()], right=[rcol, rt.to_json()])
        self._check_inputs()
        self.out_schema  # noqa: B018 - collisions surface at construction

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.left, self.right)

    @property
    def out_schema(self) -> Schema:
        if self.join_type == "anti":
            # the right relation contributes no columns; it is a *probe*
            return self.left.out_schema
        right = self.right.out_schema
        if self.join_type == "left_outer":
            right = right.nullable()
        return self.left.out_schema.concat(right)

    @property
    def canonical_order(self) -> str:
        return "left row position, then right row position"


# ---------------------------------------------------------------------------
# §2.9 — PatternMatch
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NodePat:
    """`(v : Label?)`."""

    var: str
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.var:
            raise InvalidArgError("a node pattern needs a variable")

    def to_json(self) -> dict[str, Any]:
        return {"var": self.var, "label": self.label}


@dataclass(frozen=True, slots=True)
class EdgePat:
    """`(u) -[ e : RelType? ]-> (w)` or its undirected form."""

    var: str
    src: str
    dst: str
    rel_type: str | None = None
    directed: bool = True

    def __post_init__(self) -> None:
        if not self.var:
            raise InvalidArgError("an edge pattern needs a variable")

    def to_json(self) -> dict[str, Any]:
        return {"var": self.var, "src": self.src, "dst": self.dst,
                "rel_type": self.rel_type, "directed": self.directed}


@dataclass(frozen=True, slots=True)
class Pattern:
    node_pats: tuple[NodePat, ...]
    edge_pats: tuple[EdgePat, ...]

    def __post_init__(self) -> None:
        if not self.node_pats:
            raise InvalidArgError("a pattern needs at least one node")
        if not self.edge_pats:
            raise InvalidArgError("a pattern needs at least one edge")
        names = [p.var for p in self.node_pats] + [p.var for p in self.edge_pats]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise InvalidArgError(f"duplicate pattern variable(s): {dupes}")
        node_vars = {p.var for p in self.node_pats}
        for e in self.edge_pats:
            missing = [v for v in (e.src, e.dst) if v not in node_vars]
            if missing:
                raise InvalidArgError(f"edge {e.var!r} names undeclared node(s): {missing}")

    def to_json(self) -> dict[str, Any]:
        return {"nodes": [p.to_json() for p in self.node_pats],
                "edges": [p.to_json() for p in self.edge_pats]}


@dataclass(frozen=True, slots=True)
class Source:
    """`sources: {v → r}` or `{v → (r, col)}`.

    `sources` **rebinds, it does not match prefixes** (§2.9): an edge variable
    takes `r`'s edge identity column and a node variable takes `r`'s node
    identity column, and if `r` carries more than one column of that kind the
    binding **must name it**, because silently picking one would be a
    schema-dependent answer.
    """

    var: str
    relation: Node
    column: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"var": self.var, "column": self.column,
                "relation": self.relation.node_digest}


@dataclass(frozen=True, slots=True)
class PatternMatch(Node):
    """`PatternMatch(sources?, pattern) @ Σ` — general multi-way matching with
    edge-time bindings, **fixed length only** (R1).

    Matching discipline is **edge-isomorphism over identities, not versions**
    (§8.5, CLOSED): no two edge variables bind versions of the same `eid`, and
    node variables are *not* implicitly distinct. Under a version-based rule a
    correction that changed no property value would manufacture pattern
    instances no uncorrected store has.
    """

    pattern: Pattern
    sources: tuple[Source, ...] = ()
    sigma_: Sigma = DEFAULT_SIGMA

    reads_store = True

    def __post_init__(self) -> None:
        declared = {p.var for p in self.pattern.node_pats} | {p.var for p in self.pattern.edge_pats}
        for s in self.sources:
            if s.var not in declared:
                raise InvalidArgError(f"sources names a variable not in the pattern: {s.var!r}")
            if s.column is not None and s.column not in s.relation.out_schema:
                raise InvalidArgError(f"sources column not in the relation: {s.column!r}")
            candidates = self._identity_columns(s)
            if s.column is None and len(candidates) > 1:
                raise InvalidArgError(
                    "the source relation carries more than one identity column of that kind; "
                    "the binding must name it",
                    var=s.var, candidates=candidates)
            if s.column is None and not candidates:
                raise InvalidArgError(
                    "the source relation carries no identity column of that kind", var=s.var)
        seen = [s.var for s in self.sources]
        if len(set(seen)) != len(seen):
            raise InvalidArgError("a pattern variable may take at most one source")
        self._check_inputs()
        self.out_schema  # noqa: B018 - collisions surface at construction

    def _identity_columns(self, s: Source) -> list[str]:
        wanted = "eid" if any(p.var == s.var for p in self.pattern.edge_pats) else "uid"
        return [c.name for c in s.relation.out_schema if c.tau.base == wanted]

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        """§2.9: `PatternMatch` takes no `input` relation — its inputs are the
        `sources` relations, which is what `scope_of`'s `⊎ ins` ranges over."""
        return tuple(s.relation for s in self.sources)

    @property
    def out_schema(self) -> Schema:
        out = Schema()
        for p in self.pattern.node_pats:
            out = out.concat(Schema(PATTERN_NODE_COLUMNS).prefixed(p.var))
        for e in self.pattern.edge_pats:
            out = out.concat(Schema(EDGE_COLUMNS).prefixed(e.var))
        return out

    @property
    def canonical_order(self) -> str:
        return ("lexicographic over bound edge (vt_s, vid) in pattern declaration order, "
                "then bound node uid in declaration order")


# ---------------------------------------------------------------------------
# §2.10 — Aggregate
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Agg:
    """`(fn, of?, alias)`.

    **`mean` is an *atomic* aggregate** (§8.3, confirmed): defined as
    `sum/count` but computed as one aggregate rather than composed from two
    aggregate columns — written the composed way it would be
    `arithmetic-over-aggregates` and beyond v1. Two consequences are normative:
    an empty group emits no row at all, so `mean` **never yields NaN and never
    raises a division error**, and its value is formed under §2.7's blessed
    arithmetic rule.
    """

    fn: str
    alias: str
    of: Expr | None = None

    def __post_init__(self) -> None:
        if self.fn not in AGG_FNS:
            raise InvalidArgError(f"unknown aggregate: {self.fn!r}", allowed=sorted(AGG_FNS))
        if not self.alias:
            raise InvalidArgError("an aggregate needs an alias")
        if self.fn == "count" and self.of is not None:
            raise InvalidArgError("count takes no `of`")
        if self.fn != "count" and self.of is None:
            raise InvalidArgError(f"{self.fn} needs an `of` expression")

    def tau(self, in_schema: Schema) -> Tau:
        if self.fn in ("count", "count_distinct"):
            if self.of is not None:
                self.of.tau(in_schema)
            return T_INT
        assert self.of is not None
        inner = self.of.tau(in_schema)
        if self.fn == "mean":
            # §4.2 asks for "int-or-float under the blessed rule"; §4.1 has no
            # union type, so the wider of the two is the static answer.
            return T_FLOAT
        return inner  # sum / min / max keep the input column's type

    def to_json(self) -> dict[str, Any]:
        return {"fn": self.fn, "alias": self.alias,
                "of": self.of.to_json() if self.of is not None else None}


@dataclass(frozen=True, slots=True)
class Aggregate(Node):
    """`Aggregate(input, group_by, aggregates)`.

    - **Group-by arity is unrestricted** — today's two-slot cap is an
      operator-boundary artifact, not a semantic gap. `group_by = []` yields
      exactly one row; `aggregates = []` is `DISTINCT` over the key.
    - **Non-empty groups only.** There is no densified group axis and no bucket
      generator in v1, which is exactly why `graph_metric_timeseries`,
      `burst_detection` and `neighborhood_evolution`'s degree series stay
      opaque: their series are *dense*, zeros included.
    - **Precondition.** The input must be execution-complete over its declared
      domain or the `Aggregate` refuses (`E_INCOMPLETE`). `Aggregate` consumes
      *relations*, never *pages* — a page cut belongs at the plan's output
      boundary (§2.12), so a page-cut `Limit` beneath an `Aggregate` is a
      defect and is rejected here.
    """

    input: Node
    group_by: tuple[tuple[str, Expr], ...] = ()
    aggregates: tuple[Agg, ...] = ()
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        in_schema = self.input.out_schema
        names = [n for n, _ in self.group_by] + [a.alias for a in self.aggregates]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise InvalidArgError(f"duplicate output name(s): {dupes}")
        for _, e in self.group_by:
            e.tau(in_schema)
        for a in self.aggregates:
            a.tau(in_schema)
        if isinstance(self.input, Limit) and not self.input.is_top_k:
            raise InvalidArgError(
                "Aggregate consumes relations, never pages: a page-cut Limit narrows "
                "delivery rather than the domain, so aggregating it would answer for the "
                "page while claiming the population (§2.12, §5.3 rule 3)")
        if isinstance(self.input, Aggregate):
            raise InvalidArgError("aggregate over aggregate is beyond v1 (§4.3)")
        self._check_inputs()
        self.out_schema  # noqa: B018 - collisions surface at construction

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def aggregate_columns(self) -> tuple[str, ...]:
        return tuple(a.alias for a in self.aggregates)

    @property
    def out_schema(self) -> Schema:
        in_schema = self.input.out_schema
        keys = Schema(tuple(Column(n, e.tau(in_schema)) for n, e in self.group_by))
        aggs = Schema(tuple(Column(a.alias, a.tau(in_schema)) for a in self.aggregates))
        return keys.concat(aggs)

    @property
    def canonical_order(self) -> str:
        return "by group key values — numeric for numeric keys, code point for strings, nulls first"


# ---------------------------------------------------------------------------
# §2.11 / §2.12 — Order and Limit
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SortKey:
    key: Expr
    direction: str = "asc"
    nulls: str = "nulls_last"

    def __post_init__(self) -> None:
        if self.direction not in ("asc", "desc"):
            raise InvalidArgError(f"unknown sort direction: {self.direction!r}")
        if self.nulls not in ("nulls_first", "nulls_last"):
            raise InvalidArgError(f"unknown nulls order: {self.nulls!r}")

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key.to_json(), "dir": self.direction, "nulls": self.nulls}


@dataclass(frozen=True, slots=True)
class Order(Node):
    """`Order(input, keys)`.

    **A total order is required**: the declared keys are extended with the
    input's own canonical order as a final tiebreak, so the canonical result
    hash is well defined. `@OrderedBy(f)` is *reserved, not implemented* — a
    TGIR result may carry the ordering key in its provenance, but v1 certifies
    no ordering claim from it.
    """

    input: Node
    keys: tuple[SortKey, ...]
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if not self.keys:
            raise InvalidArgError("Order needs at least one key")
        for k in self.keys:
            k.key.tau(self.input.out_schema)
        self._check_inputs()

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def out_schema(self) -> Schema:
        return self.input.out_schema

    @property
    def canonical_order(self) -> str:
        return "the declared keys, tiebroken by the input's own canonical order"


@dataclass(frozen=True, slots=True)
class Limit(Node):
    """`Limit(input, n, offset|cursor)`.

    Two distinct uses, and they produce different metadata (§2.12, §5.3):
    directly above an `Order` it is **top-k** — the declared domain narrows to
    "the `n` greatest rows under the recorded ranking key" — and otherwise it is
    a **page cut**, where delivery is incomplete and execution is not.
    `is_top_k` is exactly that syntactic test.
    """

    input: Node
    n: int
    offset: int | None = None
    cursor: str | None = None
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if self.n < 1:
            raise InvalidArgError(f"Limit.n must be >= 1, got {self.n}")
        if self.offset is not None and self.cursor is not None:
            raise InvalidArgError("Limit takes an offset or a cursor, not both")
        if self.offset is not None and self.offset < 0:
            raise InvalidArgError(f"Limit.offset must be >= 0, got {self.offset}")
        self._check_inputs()

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def inputs(self) -> tuple[Node, ...]:
        return (self.input,)

    @property
    def is_top_k(self) -> bool:
        return isinstance(self.input, Order)

    @property
    def out_schema(self) -> Schema:
        return self.input.out_schema

    @property
    def canonical_order(self) -> str:
        return "the input's, cut"


# ---------------------------------------------------------------------------
# R7 — the opaque leaf
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OpaqueLeaf(Node):
    """One of the fifteen existing operators, as a plan node (R7, plan §5).

    The opaque leaf is a **plan-node kind**, not a registry annotation and not a
    wrapper object around the kernel. Both alternatives were considered and
    rejected for reasons that are properties of this class:

    - Σ and `out_schema` are **per call**, not per operator —
      `entity_history`'s `edges` list exists only when `include_edges`, and Σ
      comes from `as_of_tt` / `window` / `t_valid` / `t1` / `t2`. An annotation
      has no instance to carry them.
    - §2.0 obligation 6 requires the `∅` classification to be a *checkable*
      property: "an `∅`-classified kernel must not receive a live storage
      adapter". The adapter is passed **per call**, so only a plan node can
      carry the decision to withhold it — `withhold_adapter`, here.
    - It is not a wrapper holding the kernel callable: that would make the
      leaf's identity depend on a Python function object and break
      `node_digest`'s content addressing (§5.4 digests `(op, canonical args, Σ,
      input digests)` — all data).

    `withhold_adapter` may be widened (withheld where the classification does
    not require it) but never narrowed on a known-`∅` operator; constructing
    `compute` with `withhold_adapter = False` is refused rather than accepted
    quietly, because the whole point of the classification is that a
    misclassification fails loudly at the first read instead of rotting into
    silent unsoundness.
    """

    op_name: str
    bound_args: tuple[tuple[str, Any], ...]
    out_fields: tuple[str, ...]
    vt_mode: str = "overlap"
    withhold_adapter: bool | None = None
    sigma_: Sigma = DEFAULT_SIGMA

    def __post_init__(self) -> None:
        if not self.op_name:
            raise InvalidArgError("an opaque leaf names an operator")
        check_vt_mode(self.vt_mode)
        if not self.out_fields:
            raise InvalidArgError("an opaque leaf carries its operator's output_fields")
        keys = [k for k, _ in self.bound_args]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise InvalidArgError(f"duplicate bound argument(s): {dupes}")
        required = self.op_name in EMPTY_SCOPE_OPS
        if self.withhold_adapter is None:
            object.__setattr__(self, "withhold_adapter", required)
        elif required and not self.withhold_adapter:
            raise InvalidArgError(
                f"{self.op_name!r} is ∅-classified: its kernel never touches the adapter, "
                "so the adapter must be withheld (§2.0 obligation 6)")

    @staticmethod
    def build(op: str, bound_args: dict[str, Any], out_fields: tuple[str, ...], *,
              sigma: Sigma = DEFAULT_SIGMA, vt_mode: str = "overlap") -> "OpaqueLeaf":
        """Construct from post-`validate_args`, post-`_fill_defaults` arguments —
        the shape `call_operator` already has at `algebra.py:145`."""
        return OpaqueLeaf(op, tuple(sorted(bound_args.items())), tuple(out_fields),
                          vt_mode, None, sigma)

    @property
    def sigma(self) -> Sigma:
        return self.sigma_

    @property
    def op(self) -> str:
        return self.op_name

    @property
    def args(self) -> dict[str, Any]:
        return dict(self.bound_args)

    @property
    def reads_store(self) -> bool:  # type: ignore[override]
        return not self.withhold_adapter

    @property
    def out_schema(self) -> Schema:
        """Derived from `OperatorSpec.output_fields`. The leaf is opaque, so
        the payload's *field* names are the contract (C4) and their types are
        not modelled: an operator's list field is `json`, and the two scalar
        pagination fields are typed because §5.3 reads them."""
        cols = []
        for f in self.out_fields:
            if f == "truncated":
                tau = Tau("bool")
            elif f.endswith("_total") or f == "rows_total":
                tau = T_INT
            elif f == "cursor":
                tau = Tau("str").optional()
            else:
                tau = Tau("json")
            cols.append(Column(f, tau))
        return Schema(tuple(cols))

    @property
    def canonical_order(self) -> str:
        return "inherited unchanged from the kernel; the leaf asserts nothing new"


#: The twelve compositional core node types of §2, in spec order.
CORE_NODE_TYPES: tuple[type[Node], ...] = (
    NodeScan, EdgeScan, Expand, Filter, PropertyPredicate, TypeConstraint,
    Project, Join, PatternMatch, Aggregate, Order, Limit,
)

#: §2.0's `∅` classification of the core: four store-reading, eight pure.
STORE_READING_CORE: tuple[type[Node], ...] = (NodeScan, EdgeScan, Expand, PatternMatch)


__all__ = [
    "AGG_FNS", "Agg", "AggFn", "Aggregate", "Bounded", "CORE_NODE_TYPES",
    "DIRECTIONS", "Direction", "EMPTY_SCOPE_OPS", "EdgePat", "EdgeScan",
    "Endpoints", "Exact", "Expand", "Filter", "HopSpec", "INCIDENT_ROLES",
    "JOIN_TYPES", "Join", "JoinType", "Limit", "Node", "NodePat", "NodeScan",
    "NullsOrder", "OpaqueLeaf", "Order", "Pattern", "PatternMatch", "Project",
    "PropertyPredicate", "STORE_READING_CORE", "SortDir", "SortKey", "Source",
    "TypeConstraint", "Unbounded",
]
