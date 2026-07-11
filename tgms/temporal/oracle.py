"""Brute-force reference implementations (WP1.6).

Correct-by-inspection Python over full version lists; O(n!)-ish acceptable —
used only on graphs <= 5k edge events. Every function takes the same filled
args dict as the engine operator and returns the same payload structure, so
property tests compare canonical JSON directly.

Python loops are explicitly allowed here (spec 7.1).
"""

from __future__ import annotations

from collections import deque
from typing import Any

from tgms.core.model import OPEN_END, EdgeVersion, NodeVersion
from tgms.temporal.algebra import paginate


class Oracle:
    def __init__(self, node_versions: list[NodeVersion],
                 edge_versions: list[EdgeVersion]) -> None:
        self.nv = list(node_versions)
        self.ev = list(edge_versions)

    # ------------------------------------------------------------------ #
    # primitive filters                                                   #
    # ------------------------------------------------------------------ #

    def believed_nodes(self, as_of: int) -> list[NodeVersion]:
        return [v for v in self.nv if v.believed_at(as_of)]

    def believed_edges(self, as_of: int) -> list[EdgeVersion]:
        return [v for v in self.ev if v.believed_at(as_of)]

    def nodes_at(self, t: int, as_of: int) -> dict[str, NodeVersion]:
        return {v.uid: v for v in self.believed_nodes(as_of) if v.valid_at(t)}

    def edges_at(self, t: int, as_of: int,
                 rel_types: list[str] | None = None) -> dict[str, EdgeVersion]:
        return {v.eid: v for v in self.believed_edges(as_of)
                if v.valid_at(t) and (rel_types is None or v.rel_type in rel_types)}

    def events_in(self, t_a: int, t_b: int, as_of: int) -> list[EdgeVersion]:
        """Edge versions as events at vt_s, ordered by the (t, eid) total order."""
        evs = [v for v in self.believed_edges(as_of) if t_a <= v.vt_s < t_b]
        return sorted(evs, key=lambda v: (v.vt_s, v.eid))

    @staticmethod
    def _edge_row(v: EdgeVersion) -> dict[str, Any]:
        return {"eid": v.eid, "vid": v.vid, "src": v.src, "dst": v.dst,
                "rel_type": v.rel_type, "vt_s": v.vt_s, "vt_e": v.vt_e}

    def _bfs(self, edges: dict[str, EdgeVersion], seeds: list[str],
             valid_uids: set[str], hops: int) -> dict[str, int]:
        dist = {u: 0 for u in seeds if u in valid_uids}
        frontier = sorted(dist)
        for h in range(1, hops + 1):
            nxt = set()
            for v in edges.values():
                if v.src in frontier and v.dst not in dist and v.dst in valid_uids:
                    nxt.add(v.dst)
                if v.dst in frontier and v.src not in dist and v.src in valid_uids:
                    nxt.add(v.src)
            for u in nxt:
                dist[u] = h
            frontier = sorted(nxt)
        return dist

    # ------------------------------------------------------------------ #
    # O1 entity_history                                                    #
    # ------------------------------------------------------------------ #

    def entity_history(self, args: dict[str, Any]) -> dict[str, Any]:
        as_of = args["as_of_tt"]
        versions = sorted((v for v in self.believed_nodes(as_of) if v.uid == args["uid"]),
                          key=lambda v: (v.vt_s, v.vid))
        rows = [v.to_json() for v in versions]
        for r in rows:  # censor post-as_of belief closure, like the engine
            if r["tt_e"] > as_of:
                r["tt_e"] = OPEN_END
        out = paginate(rows, args["limit"], args["cursor"])
        if args["include_edges"]:
            inc = sorted((v for v in self.believed_edges(as_of)
                          if args["uid"] in (v.src, v.dst)),
                         key=lambda v: (v.vt_s, v.vid))
            out["edges"] = [self._edge_row(v) for v in inc[: args["limit"]]]
            out["edges_truncated"] = len(inc) > args["limit"]
        return out

    # ------------------------------------------------------------------ #
    # O2 snapshot_subgraph                                                 #
    # ------------------------------------------------------------------ #

    def snapshot_subgraph(self, args: dict[str, Any]) -> dict[str, Any]:
        t, as_of = args["t_valid"], args["as_of_tt"]
        edges = self.edges_at(t, as_of, args["rel_types"])
        nodes = self.nodes_at(t, as_of)
        dist = self._bfs(edges, args["seeds"], set(nodes), args["hops"])
        induced = sorted((v for v in edges.values()
                          if v.src in dist and v.dst in dist),
                         key=lambda v: (v.vt_s, v.vid))
        node_rows = sorted(({"uid": u, "label": nodes[u].label, "hop": h}
                            for u, h in dist.items()),
                           key=lambda r: (r["hop"], r["uid"]))
        out = paginate([self._edge_row(v) for v in induced],
                       args["limit"], args["cursor"])
        out["nodes"] = node_rows[: args["limit"]]
        out["nodes_total"] = len(node_rows)
        out["nodes_truncated"] = len(node_rows) > args["limit"]
        out["truncated"] = out["truncated"] or out["nodes_truncated"]
        return out

    # ------------------------------------------------------------------ #
    # O3 diff_snapshots                                                    #
    # ------------------------------------------------------------------ #

    def diff_snapshots(self, args: dict[str, Any]) -> dict[str, Any]:
        t1, t2, as_of, limit = args["t1"], args["t2"], args["as_of_tt"], args["limit"]
        n1, n2 = self.nodes_at(t1, as_of), self.nodes_at(t2, as_of)
        e1, e2 = self.edges_at(t1, as_of), self.edges_at(t2, as_of)

        allowed: set[str] | None = None
        if args["scope"] is not None:
            seeds, hops = args["scope"]["seeds"], args["scope"]["hops"]
            d1 = self._bfs(e1, seeds, set(n1), hops)
            d2 = self._bfs(e2, seeds, set(n2), hops)
            allowed = set(d1) | set(d2)

        def nok(u: str) -> bool:
            return allowed is None or u in allowed

        def eok(v: EdgeVersion) -> bool:
            return allowed is None or (v.src in allowed and v.dst in allowed)

        nodes_added = sorted(u for u in n2 if u not in n1 and nok(u))
        nodes_removed = sorted(u for u in n1 if u not in n2 and nok(u))
        edges_added = sorted(e for e in e2 if e not in e1 and eok(e2[e]))
        edges_removed = sorted(e for e in e1 if e not in e2 and eok(e1[e]))

        changed = []
        for u in sorted(u for u in n1 if u in n2 and nok(u)):
            a, b = n1[u], n2[u]
            if a.vid != b.vid and (a.props != b.props or a.label != b.label):
                changed.append({"kind": "node", "id": u,
                                "from": {"label": a.label, "props": a.props},
                                "to": {"label": b.label, "props": b.props}})
        for e in sorted(e for e in e1 if e in e2 and eok(e1[e])):
            a, b = e1[e], e2[e]
            if a.vid != b.vid and a.props != b.props:
                changed.append({"kind": "edge", "id": e,
                                "from": {"props": a.props},
                                "to": {"props": b.props}})

        def desc(v: EdgeVersion) -> dict[str, Any]:
            return {"eid": v.eid, "src": v.src, "dst": v.dst, "rel_type": v.rel_type}

        out = {
            "nodes_added": nodes_added[:limit], "nodes_added_total": len(nodes_added),
            "nodes_removed": nodes_removed[:limit],
            "nodes_removed_total": len(nodes_removed),
            "edges_added": [desc(e2[e]) for e in edges_added[:limit]],
            "edges_added_total": len(edges_added),
            "edges_removed": [desc(e1[e]) for e in edges_removed[:limit]],
            "edges_removed_total": len(edges_removed),
            "props_changed": changed[:limit], "props_changed_total": len(changed),
        }
        out["truncated"] = any(
            out[f"{k}_total"] > limit
            for k in ("nodes_added", "nodes_removed", "edges_added",
                      "edges_removed", "props_changed"))
        return out

    # ------------------------------------------------------------------ #
    # O4 temporal_reachability                                             #
    # ------------------------------------------------------------------ #

    def temporal_reachability(self, args: dict[str, Any]) -> dict[str, Any]:
        t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
        delta = args["delta_max_wait"]
        src_uid = args["src"]
        edges = [v for v in self.believed_edges(args["as_of_tt"])
                 if v.vt_e > t_a and v.vt_s < t_b]
        hops: list[tuple[str, str, int, int]] = []
        for v in edges:
            hops.append((v.src, v.dst, v.vt_s, v.vt_e))
            if args["direction"] == "both":
                hops.append((v.dst, v.src, v.vt_s, v.vt_e))
        if args["direction"] == "in":
            hops = [(d, s, a, b) for s, d, a, b in hops]

        # exhaustive state search over (node, arrival)
        labels: dict[str, set[int]] = {src_uid: {t_a}}
        queue = deque([(src_uid, t_a)])
        while queue:
            u, a = queue.popleft()
            for s, d, vs, ve in hops:
                if s != u:
                    continue
                tau = max(a, vs)
                if tau >= ve or tau >= t_b:
                    continue
                if delta is not None and not (u == src_uid and a == t_a) \
                        and tau - a > delta:
                    continue
                if tau not in labels.setdefault(d, set()):
                    labels[d].add(tau)
                    queue.append((d, tau))
        rows = [{"uid": u, "earliest_arrival": min(ls)}
                for u, ls in labels.items() if u != src_uid]
        rows.sort(key=lambda r: (r["earliest_arrival"], r["uid"]))
        return paginate(rows, args["limit"], args["cursor"])

    # ------------------------------------------------------------------ #
    # O5 temporal_paths                                                    #
    # ------------------------------------------------------------------ #

    def temporal_paths(self, args: dict[str, Any]) -> dict[str, Any]:
        t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
        edges = sorted((v for v in self.believed_edges(args["as_of_tt"])
                        if v.vt_e > t_a and v.vt_s < t_b),
                       key=lambda v: (v.vt_s, v.eid))
        found: list[tuple[int, int, tuple, list[EdgeVersion]]] = []

        def dfs(node: str, arrival: int, visited: set[str],
                trail: list[EdgeVersion]) -> None:
            if node == args["dst"] and trail:
                key = tuple((v.vt_s, v.eid) for v in trail)
                found.append((arrival, len(trail), key, list(trail)))
                return
            if len(trail) == args["max_hops"]:
                return
            for v in edges:
                if v.src != node or v.dst in visited:
                    continue
                tau = max(arrival, v.vt_s)
                if tau >= v.vt_e or tau >= t_b:
                    continue
                visited.add(v.dst)
                trail.append(v)
                dfs(v.dst, tau, visited, trail)
                trail.pop()
                visited.remove(v.dst)

        dfs(args["src"], t_a, {args["src"]}, [])
        found.sort(key=lambda p: (p[0], p[1], p[2]))
        rows = [{"arrival": a, "hops": h,
                 "edges": [{"src": v.src, "dst": v.dst, "rel_type": v.rel_type,
                            "eid": v.eid, "t": v.vt_s} for v in trail]}
                for a, h, _, trail in found[: args["k"]]]
        return {"rows": rows, "rows_total": len(found),
                "truncated": len(found) > args["k"], "cursor": None}

    # ------------------------------------------------------------------ #
    # O6 / O7 motifs                                                       #
    # ------------------------------------------------------------------ #

    _MOTIF_CHECK = {
        "M_triangle_cyclic": lambda a, b, c: (
            b.src == a.dst and c.src == b.dst and c.dst == a.src
            and len({a.src, a.dst, b.dst}) == 3),
        "M_triangle_acyclic_1": lambda a, b, c: (
            b.src == a.src and c.src == a.dst and c.dst == b.dst
            and len({a.src, a.dst, b.dst}) == 3),
        "M_2node_pingpong": lambda a, b, c: (
            b.src == a.dst and b.dst == a.src and c.src == a.src
            and c.dst == a.dst and a.src != a.dst),
        "M_star_out_3": lambda a, b, c: (
            b.src == a.src and c.src == a.src
            and len({a.src, a.dst, b.dst, c.dst}) == 4),
        "M_path_3": lambda a, b, c: (
            b.src == a.dst and c.src == b.dst
            and len({a.src, a.dst, b.dst, c.dst}) == 4),
    }

    def _motif_instances(self, args: dict[str, Any]) -> list[tuple]:
        t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
        events = self.events_in(t_a, t_b, args["as_of_tt"])
        if args["node_filter"] is not None:
            f = set(args["node_filter"])
            events = [v for v in events if v.src in f and v.dst in f]
        check = self._MOTIF_CHECK[args["motif"]]
        delta = args["delta"]
        out = []
        n = len(events)
        for i in range(n):
            for j in range(i + 1, n):
                if events[j].vt_s - events[i].vt_s > delta:
                    break
                for k in range(j + 1, n):
                    if events[k].vt_s - events[i].vt_s > delta:
                        break
                    a, b, c = events[i], events[j], events[k]
                    if check(a, b, c):
                        out.append((a, b, c))
        out.sort(key=lambda t: tuple((v.vt_s, v.eid) for v in t))
        return out

    def count_temporal_motifs(self, args: dict[str, Any]) -> dict[str, Any]:
        t_a, t_b = args["window"]["t_a"], args["window"]["t_b"]
        events = self.events_in(t_a, t_b, args["as_of_tt"])
        if args["node_filter"] is not None:
            f = set(args["node_filter"])
            events = [v for v in events if v.src in f and v.dst in f]
        return {"count": len(self._motif_instances(args)),
                "n_events_in_window": len(events), "truncated": False}

    def find_temporal_motif_instances(self, args: dict[str, Any]) -> dict[str, Any]:
        rows = [{"edges": [{"src": v.src, "dst": v.dst, "t": v.vt_s,
                            "eid": v.eid, "rel_type": v.rel_type}
                           for v in inst]}
                for inst in self._motif_instances(args)]
        return paginate(rows, args["limit"], args["cursor"])

    # ------------------------------------------------------------------ #
    # O8 graph_metric_timeseries                                           #
    # ------------------------------------------------------------------ #

    def _bucket_bounds(self, args: dict[str, Any]) -> list[tuple[int, int]]:
        t_a, t_b, stride = args["window"]["t_a"], args["window"]["t_b"], args["stride"]
        return [(bs, min(bs + stride, t_b)) for bs in range(t_a, t_b, stride)]

    def graph_metric_timeseries(self, args: dict[str, Any]) -> dict[str, Any]:
        as_of = args["as_of_tt"]
        metric = args["metric"]
        nodes = self.believed_nodes(as_of)
        edges = self.believed_edges(as_of)
        rows = []
        for bs, be in self._bucket_bounds(args):
            if metric == "node_count":
                val = sum(1 for v in nodes if v.valid_at(bs))
            elif metric == "edge_event_count":
                val = sum(1 for v in edges if bs <= v.vt_s < be)
            elif metric == "active_edge_count":
                val = sum(1 for v in edges if v.valid_at(bs))
            elif metric == "mean_out_degree":
                act = sum(1 for v in edges if v.valid_at(bs))
                nod = sum(1 for v in nodes if v.valid_at(bs))
                val = act / nod if nod > 0 else 0.0
            elif metric == "new_node_rate":
                births: dict[str, int] = {}
                for v in nodes:
                    births[v.uid] = min(births.get(v.uid, OPEN_END), v.vt_s)
                val = sum(1 for t in births.values() if bs <= t < be)
            else:  # reciprocity
                pairs = {(v.src, v.dst) for v in edges if bs <= v.vt_s < be}
                val = (sum(1 for (s, d) in pairs if (d, s) in pairs) / len(pairs)
                       if pairs else 0.0)
            rows.append({"t_a": bs, "t_b": be,
                         "value": float(val) if isinstance(val, float) else int(val)})
        out = paginate(rows, args["limit"], args["cursor"])
        out["n_buckets"] = len(rows)
        return out

    # ------------------------------------------------------------------ #
    # O9 burst_detection                                                   #
    # ------------------------------------------------------------------ #

    def burst_detection(self, args: dict[str, Any]) -> dict[str, Any]:
        tgt = args["target"]
        edges = self.believed_edges(args["as_of_tt"])
        if tgt.get("rel_type"):
            edges = [v for v in edges if v.rel_type == tgt["rel_type"]]
        if tgt["kind"] == "node_activity":
            edges = [v for v in edges if tgt["uid"] in (v.src, v.dst)]
        bounds = self._bucket_bounds(args)
        series = [float(sum(1 for v in edges if bs <= v.vt_s < be))
                  for bs, be in bounds]
        w = args["params"].get("w", 10)
        flagged = []
        for i, ((bs, be), x) in enumerate(zip(bounds, series)):
            hist = series[max(0, i - w): i]
            if not hist:
                continue
            if args["method"] == "zscore":
                mean = sum(hist) / len(hist)
                var = sum((h - mean) ** 2 for h in hist) / len(hist)
                std = var ** 0.5
                score = abs(x - mean) / std if std > 0 else (0.0 if x == mean else 1e9)
                score = round(score, 9)  # quantized threshold, same as engine
                flag = score >= args["params"].get("z", 3.0)
            else:
                srt = sorted(hist)
                m = len(srt)
                med = srt[m // 2] if m % 2 else (srt[m // 2 - 1] + srt[m // 2]) / 2
                score = x / med if med > 0 else (1e9 if x > 0 else 0.0)
                score = round(score, 9)
                flag = score >= args["params"].get("r", 3.0)
            if flag:
                flagged.append({"t_a": bs, "t_b": be, "value": x,
                                "score": float(score)})
        out = paginate(flagged, args["limit"], args["cursor"])
        out["n_buckets"] = len(series)
        return out

    # ------------------------------------------------------------------ #
    # O10 neighborhood_evolution                                           #
    # ------------------------------------------------------------------ #

    def neighborhood_evolution(self, args: dict[str, Any]) -> dict[str, Any]:
        uid, t1, t2, as_of = args["uid"], args["t1"], args["t2"], args["as_of_tt"]
        stride = args["stride"] or max(1, (t2 - t1) // 20)

        def neighbors(t: int) -> set[str]:
            out = set()
            for v in self.edges_at(t, as_of).values():
                if v.src == uid and v.dst != uid:
                    out.add(v.dst)
                if v.dst == uid and v.src != uid:
                    out.add(v.src)
            return out

        n1, n2 = neighbors(t1), neighbors(t2)
        gained, lost = sorted(n2 - n1), sorted(n1 - n2)
        incident = [v for v in self.believed_edges(as_of) if uid in (v.src, v.dst)]
        series = [{"t": bs,
                   "degree": sum(1 for v in incident if v.valid_at(bs))}
                  for bs in range(t1, t2, stride)]
        limit = args["limit"]
        return {
            "neighbors_gained": gained[:limit], "neighbors_gained_total": len(gained),
            "neighbors_lost": lost[:limit], "neighbors_lost_total": len(lost),
            "degree_series": series, "stride": stride,
            "truncated": len(gained) > limit or len(lost) > limit,
        }

    # ------------------------------------------------------------------ #
    # O11 co_active                                                        #
    # ------------------------------------------------------------------ #

    def co_active(self, args: dict[str, Any]) -> dict[str, Any]:
        as_of = args["as_of_tt"]

        def select(spec: dict[str, Any]) -> list[EdgeVersion]:
            return [v for v in self.believed_edges(as_of)
                    if (not spec.get("src") or v.src == spec["src"])
                    and (not spec.get("dst") or v.dst == spec["dst"])
                    and (not spec.get("rel_type") or v.rel_type == spec["rel_type"])]

        rel = args["allen_relation"]["relation"]
        gap = args["allen_relation"].get("gap")

        def ok(a: EdgeVersion, b: EdgeVersion) -> bool:
            if rel == "overlaps":
                return a.vt_s < b.vt_s < a.vt_e < b.vt_e
            if rel == "during":
                return b.vt_s < a.vt_s and a.vt_e < b.vt_e
            if rel == "meets":
                return a.vt_e == b.vt_s
            return 0 < b.vt_s - a.vt_e <= gap  # before

        def desc(v: EdgeVersion) -> dict[str, Any]:
            return {"eid": v.eid, "vid": v.vid, "src": v.src, "dst": v.dst,
                    "rel_type": v.rel_type, "vt_s": v.vt_s, "vt_e": v.vt_e}

        rows = [{"a": desc(a), "b": desc(b)}
                for a in select(args["a_spec"]) for b in select(args["b_spec"])
                if a.vid != b.vid and ok(a, b)]
        rows.sort(key=lambda r: (r["a"]["vt_s"], r["a"]["vid"],
                                 r["b"]["vt_s"], r["b"]["vid"]))
        return paginate(rows, args["limit"], args["cursor"])

    # ------------------------------------------------------------------ #
    # O12 resolve_entities                                                 #
    # ------------------------------------------------------------------ #

    def resolve_entities(self, args: dict[str, Any]) -> dict[str, Any]:
        q, ql, as_of = args["query"], args["query"].lower(), args["as_of_tt"]
        latest: dict[str, NodeVersion] = {}
        matched: dict[str, int] = {}
        for v in self.believed_nodes(as_of):
            cur = latest.get(v.uid)
            if cur is None or v.vt_s > cur.vt_s:
                latest[v.uid] = v
            name = str(v.props.get("name", ""))
            if v.uid == q:
                score = 0
            elif ql in v.uid.lower():
                score = 1
            elif name and ql in name.lower():
                score = 2
            else:
                continue
            matched[v.uid] = min(matched.get(v.uid, 9), score)
        rows = []
        for uid, score in matched.items():
            v = latest[uid]
            if args["label"] is not None and v.label != args["label"]:
                continue
            rows.append({"uid": uid, "label": v.label,
                         "name": v.props.get("name"), "match": score})
        rows.sort(key=lambda r: (r["match"], r["uid"]))
        return paginate(rows, args["limit"], args["cursor"])
