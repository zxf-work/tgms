"""A TGIR plan: a DAG of nodes, its record, and its digest.

In M2.2 every operator call *is* a plan — a single-node one, whose root is the
`OpaqueLeaf` for that call. That is not ceremony: it is what makes the leaf's
identity content-addressed (`plan_digest` over data only), what gives the
executor a plan record to write, and what M3 grows into when the root becomes a
compositional subtree instead of a leaf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.core.model import digest
from tgms.tgir.node import Node, OpaqueLeaf


@dataclass(frozen=True, slots=True)
class Plan:
    """A plan DAG, identified by its root."""

    root: Node
    plan_id: str = ""

    @staticmethod
    def of(leaf: OpaqueLeaf, plan_id: str = "") -> "Plan":
        """The single-leaf plan every operator call becomes in M2.2."""
        return Plan(leaf, plan_id)

    def nodes(self) -> tuple[Node, ...]:
        """Every node, inputs before consumers, each appearing once.

        Deduplication is by `node_digest`, so a DAG that reaches one subtree
        twice reports it once — which is what makes a plan a DAG rather than a
        tree, and what keeps `⊎` from double-counting a shared scan's scope
        (harmless for a union, but misleading in a record).
        """
        seen: dict[str, Node] = {}
        order: list[Node] = []

        def walk(node: Node) -> None:
            key = node.node_digest
            if key in seen:
                return
            for i in node.inputs:
                walk(i)
            seen[key] = node
            order.append(node)

        walk(self.root)
        return tuple(order)

    @property
    def plan_digest(self) -> str:
        """§4.3: taken over the plan **with parameters bound**, which is what
        makes a `RefusalCertificate` auditable by re-running the estimator."""
        return digest({"plan": [n.node_digest for n in self.nodes()]})

    @property
    def out_schema(self) -> Any:
        return self.root.out_schema

    def to_json(self) -> dict[str, Any]:
        """The plan record. Node arguments are *not* restated here — they are
        already inside each `node_digest`, and a record that repeated them
        would invite the two copies to disagree."""
        return {
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "nodes": [{"op": n.op, "node_digest": n.node_digest,
                       "sigma": n.sigma.to_json(),
                       "inputs": [i.node_digest for i in n.inputs]}
                      for n in self.nodes()],
        }


__all__ = ["Plan"]
