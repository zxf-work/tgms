//! δ-temporal motif matching (Paranjape et al., WSDM 2017) — O6 and O7.
//!
//! This replaces a three-way non-equi self-join that DuckDB was executing over
//! an in-memory Arrow table, which was the engine's last third-party
//! dependency at runtime.
//!
//! Semantics are fixed by the operator contract and must not drift:
//!
//! * an *event* is an edge version at time `t = vt_s`, inside the window;
//! * motif edges are strictly increasing in the total order `(t, eid)`, which
//!   breaks timestamp ties deterministically;
//! * the span `t_last - t_first` is at most `delta`;
//! * motif node variables are pairwise distinct; `rel_type` is ignored.
//!
//! The structure that makes this cheap: in every catalogue motif, `e2`'s
//! endpoints are determined by `e1`, and `e3`'s by `e1` and `e2`. So neither
//! is a scan — both are lookups into an index built once per call. Three of
//! the five motifs pin *both* of `e3`'s endpoints, and their distinctness
//! constraints are decided entirely by `e1` and `e2`; for those the innermost
//! loop collapses into a range length, with no per-candidate work at all.
//!
//! `(t, eid)` is unique among believed versions — two versions of one logical
//! edge at the same valid-time start would violate the per-identity
//! disjointness invariant — so position in the sorted order *is* the total
//! order, and `p1 < p2 < p3` expresses the ordering constraint exactly.

use std::collections::HashMap;

use crate::error::{EngineError, Result};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Motif {
    /// u→v, v→w, w→u
    TriangleCyclic,
    /// u→v, u→w, v→w
    TriangleAcyclic1,
    /// u→v, v→u, u→v
    PingPong,
    /// u→a, u→b, u→c
    StarOut3,
    /// u→v→w→x
    Path3,
}

impl Motif {
    pub fn parse(name: &str) -> Result<Self> {
        Ok(match name {
            "M_triangle_cyclic" => Motif::TriangleCyclic,
            "M_triangle_acyclic_1" => Motif::TriangleAcyclic1,
            "M_2node_pingpong" => Motif::PingPong,
            "M_star_out_3" => Motif::StarOut3,
            "M_path_3" => Motif::Path3,
            other => {
                return Err(EngineError::invariant(format!(
                    "unknown motif {other:?}"
                )))
            }
        })
    }
}

/// Events, already restricted to the window and any node filter.
pub struct Events<'a> {
    pub src: &'a [i64],
    pub dst: &'a [i64],
    pub t: &'a [i64],
    /// Tie-breaker in the total order; the operator contract says `eid`.
    pub eid: &'a [String],
}

impl Events<'_> {
    fn len(&self) -> usize {
        self.t.len()
    }

    fn check(&self) -> Result<()> {
        if self.src.len() == self.dst.len() && self.dst.len() == self.t.len()
            && self.t.len() == self.eid.len()
        {
            Ok(())
        } else {
            Err(EngineError::invariant(
                "motif event columns have differing lengths",
            ))
        }
    }
}

/// One match, as row indices into the *input* arrays, in motif edge order.
pub type Match = [u32; 3];

struct Index {
    /// Input row index for each position in the `(t, eid)` total order.
    order: Vec<u32>,
    /// `t` by position, so range ends can be binary-searched.
    t_by_pos: Vec<i64>,
    src_by_pos: Vec<i64>,
    dst_by_pos: Vec<i64>,
    /// Ascending positions grouped by source node, and by endpoint pair.
    by_src: HashMap<i64, Vec<u32>>,
    by_pair: HashMap<(i64, i64), Vec<u32>>,
}

impl Index {
    fn build(ev: &Events<'_>) -> Self {
        let n = ev.len();
        let mut order: Vec<u32> = (0..n as u32).collect();
        order.sort_by(|&a, &b| {
            let (a, b) = (a as usize, b as usize);
            ev.t[a].cmp(&ev.t[b]).then_with(|| ev.eid[a].cmp(&ev.eid[b]))
        });

        let mut t_by_pos = Vec::with_capacity(n);
        let mut src_by_pos = Vec::with_capacity(n);
        let mut dst_by_pos = Vec::with_capacity(n);
        let mut by_src: HashMap<i64, Vec<u32>> = HashMap::new();
        let mut by_pair: HashMap<(i64, i64), Vec<u32>> = HashMap::new();
        for (pos, &row) in order.iter().enumerate() {
            let (s, d) = (ev.src[row as usize], ev.dst[row as usize]);
            t_by_pos.push(ev.t[row as usize]);
            src_by_pos.push(s);
            dst_by_pos.push(d);
            // inserted in position order, so every list stays ascending
            by_src.entry(s).or_default().push(pos as u32);
            by_pair.entry((s, d)).or_default().push(pos as u32);
        }
        Self {
            order,
            t_by_pos,
            src_by_pos,
            dst_by_pos,
            by_src,
            by_pair,
        }
    }

    /// Candidate positions strictly after `after` whose time is at most
    /// `t_max`. Both bounds are monotone in position, so both are binary
    /// searches and the result is one contiguous slice.
    fn window<'i>(&'i self, list: &'i [u32], after: u32, t_max: i64) -> &'i [u32] {
        let lo = list.partition_point(|&p| p <= after);
        let hi = list.partition_point(|&p| self.t_by_pos[p as usize] <= t_max);
        if hi <= lo {
            &[]
        } else {
            &list[lo..hi]
        }
    }

    fn src_list(&self, s: i64) -> &[u32] {
        self.by_src.get(&s).map(Vec::as_slice).unwrap_or(&[])
    }

    fn pair_list(&self, s: i64, d: i64) -> &[u32] {
        self.by_pair.get(&(s, d)).map(Vec::as_slice).unwrap_or(&[])
    }
}

/// Count matches, and optionally collect them.
///
/// `collect` drives whether instances are materialized: O6 only needs the
/// count, and for the three motifs whose third edge is fully determined that
/// lets the inner loop reduce to a range length.
pub fn match_motifs(
    motif: Motif,
    ev: &Events<'_>,
    delta: i64,
    collect: bool,
) -> Result<(u64, Vec<Match>)> {
    ev.check()?;
    if delta < 0 {
        return Err(EngineError::invariant("motif delta must be non-negative"));
    }
    let idx = Index::build(ev);
    let n = idx.order.len();
    let mut count: u64 = 0;
    let mut out: Vec<Match> = Vec::new();

    for p1 in 0..n as u32 {
        let i = p1 as usize;
        let (s1, d1, t1) = (idx.src_by_pos[i], idx.dst_by_pos[i], idx.t_by_pos[i]);
        let t_max = t1.checked_add(delta).unwrap_or(i64::MAX);

        // constraints on e1 alone
        match motif {
            Motif::TriangleCyclic
            | Motif::TriangleAcyclic1
            | Motif::PingPong
            | Motif::Path3 => {
                if s1 == d1 {
                    continue;
                }
            }
            Motif::StarOut3 => {
                if d1 == s1 {
                    continue;
                }
            }
        }

        // e2 candidates: endpoints determined by e1
        let e2_list = match motif {
            Motif::TriangleCyclic | Motif::Path3 => idx.src_list(d1),
            Motif::TriangleAcyclic1 | Motif::StarOut3 => idx.src_list(s1),
            Motif::PingPong => idx.pair_list(d1, s1),
        };

        for &p2 in idx.window(e2_list, p1, t_max) {
            let j = p2 as usize;
            let (s2, d2) = (idx.src_by_pos[j], idx.dst_by_pos[j]);

            // constraints decidable from e1 and e2
            let ok = match motif {
                Motif::TriangleCyclic => s2 != d2 && s1 != d2,
                Motif::TriangleAcyclic1 => d1 != d2 && s1 != d2,
                Motif::PingPong => true,
                Motif::StarOut3 => d1 != d2 && d2 != s1,
                Motif::Path3 => s1 != d2 && d1 != d2,
            };
            if !ok {
                continue;
            }

            // e3 candidates
            match motif {
                // third edge fully pinned, and distinctness is already
                // settled: the answer is a range length
                Motif::TriangleCyclic | Motif::TriangleAcyclic1 | Motif::PingPong => {
                    let (s3, d3) = match motif {
                        Motif::TriangleCyclic => (d2, s1),
                        Motif::TriangleAcyclic1 => (d1, d2),
                        _ => (s1, d1),
                    };
                    let hits = idx.window(idx.pair_list(s3, d3), p2, t_max);
                    count += hits.len() as u64;
                    if collect {
                        for &p3 in hits {
                            out.push([p1, p2, p3]);
                        }
                    }
                }
                // only the third edge's source is pinned; its target still
                // has to be distinct from the nodes already bound
                Motif::StarOut3 | Motif::Path3 => {
                    let (s3, forbidden) = match motif {
                        Motif::StarOut3 => (s1, [s1, d1, d2]),
                        _ => (d2, [s1, d1, d2]),
                    };
                    for &p3 in idx.window(idx.src_list(s3), p2, t_max) {
                        let d3 = idx.dst_by_pos[p3 as usize];
                        if forbidden.contains(&d3) {
                            continue;
                        }
                        count += 1;
                        if collect {
                            out.push([p1, p2, p3]);
                        }
                    }
                }
            }
        }
    }

    // The contract orders instances by the (t, eid) sequence of their edges.
    // Positions *are* that order, and the loops above walk them ascending, so
    // the result is already sorted — mapping back to input rows is the last
    // step, because input order is (vt_s, vid) and would sort differently.
    debug_assert!(out.windows(2).all(|w| w[0] < w[1]), "instances out of order");
    let out = out
        .into_iter()
        .map(|[a, b, c]| {
            [
                idx.order[a as usize],
                idx.order[b as usize],
                idx.order[c as usize],
            ]
        })
        .collect();
    Ok((count, out))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Data {
        src: Vec<i64>,
        dst: Vec<i64>,
        t: Vec<i64>,
        eid: Vec<String>,
    }

    impl Data {
        fn new() -> Self {
            Self {
                src: vec![],
                dst: vec![],
                t: vec![],
                eid: vec![],
            }
        }
        fn add(mut self, s: i64, d: i64, t: i64) -> Self {
            let n = self.t.len();
            self.src.push(s);
            self.dst.push(d);
            self.t.push(t);
            self.eid.push(format!("e{n:06}"));
            self
        }
        fn ev(&self) -> Events<'_> {
            Events {
                src: &self.src,
                dst: &self.dst,
                t: &self.t,
                eid: &self.eid,
            }
        }
    }

    /// The definition, transcribed directly — O(n^3) and obviously correct.
    fn brute(motif: Motif, d: &Data, delta: i64) -> u64 {
        let n = d.t.len();
        let key = |i: usize| (d.t[i], d.eid[i].clone());
        let mut c = 0u64;
        for i in 0..n {
            for j in 0..n {
                for k in 0..n {
                    if !(key(i) < key(j) && key(j) < key(k)) {
                        continue;
                    }
                    if d.t[k].max(d.t[i]) - d.t[i].min(d.t[k]) > delta {
                        continue;
                    }
                    let (s1, d1) = (d.src[i], d.dst[i]);
                    let (s2, d2) = (d.src[j], d.dst[j]);
                    let (s3, d3) = (d.src[k], d.dst[k]);
                    let ok = match motif {
                        Motif::TriangleCyclic => {
                            s2 == d1 && s3 == d2 && d3 == s1 && s1 != d1 && s2 != d2 && s1 != d2
                        }
                        Motif::TriangleAcyclic1 => {
                            s2 == s1 && s3 == d1 && d3 == d2 && d1 != d2 && s1 != d1 && s1 != d2
                        }
                        Motif::PingPong => {
                            s2 == d1 && d2 == s1 && s3 == s1 && d3 == d1 && s1 != d1
                        }
                        Motif::StarOut3 => {
                            s2 == s1
                                && s3 == s1
                                && d1 != d2
                                && d1 != d3
                                && d2 != d3
                                && d1 != s1
                                && d2 != s1
                                && d3 != s1
                        }
                        Motif::Path3 => {
                            s2 == d1
                                && s3 == d2
                                && s1 != d1
                                && s1 != d2
                                && s1 != d3
                                && d1 != d2
                                && d1 != d3
                                && d2 != d3
                        }
                    };
                    if ok {
                        c += 1;
                    }
                }
            }
        }
        c
    }

    const ALL: [Motif; 5] = [
        Motif::TriangleCyclic,
        Motif::TriangleAcyclic1,
        Motif::PingPong,
        Motif::StarOut3,
        Motif::Path3,
    ];

    #[test]
    fn finds_a_planted_cyclic_triangle() {
        let d = Data::new().add(0, 1, 10).add(1, 2, 20).add(2, 0, 30);
        let (c, m) = match_motifs(Motif::TriangleCyclic, &d.ev(), 100, true).unwrap();
        assert_eq!(c, 1);
        assert_eq!(m, vec![[0, 1, 2]]);
    }

    #[test]
    fn delta_bounds_the_span() {
        let d = Data::new().add(0, 1, 10).add(1, 2, 20).add(2, 0, 30);
        assert_eq!(match_motifs(Motif::TriangleCyclic, &d.ev(), 20, false).unwrap().0, 1);
        assert_eq!(match_motifs(Motif::TriangleCyclic, &d.ev(), 19, false).unwrap().0, 0);
    }

    #[test]
    fn edges_must_be_strictly_time_ordered() {
        // same three edges, but the cycle closes before it opens
        let d = Data::new().add(0, 1, 30).add(1, 2, 20).add(2, 0, 10);
        assert_eq!(match_motifs(Motif::TriangleCyclic, &d.ev(), 100, false).unwrap().0, 0);
    }

    #[test]
    fn ties_are_broken_by_eid_not_by_input_order() {
        // all at the same instant: the (t, eid) order decides, and eids here
        // ascend with input position, so exactly one arrangement matches
        let d = Data::new().add(0, 1, 5).add(1, 2, 5).add(2, 0, 5);
        assert_eq!(match_motifs(Motif::TriangleCyclic, &d.ev(), 0, false).unwrap().0, 1);
    }

    #[test]
    fn self_loops_and_repeated_nodes_are_excluded() {
        let d = Data::new().add(0, 0, 1).add(0, 0, 2).add(0, 0, 3);
        for m in ALL {
            assert_eq!(match_motifs(m, &d.ev(), 100, false).unwrap().0, 0, "{m:?}");
        }
    }

    #[test]
    fn pingpong_needs_the_third_edge_to_repeat_the_first() {
        let d = Data::new().add(0, 1, 1).add(1, 0, 2).add(0, 1, 3);
        assert_eq!(match_motifs(Motif::PingPong, &d.ev(), 10, false).unwrap().0, 1);
        let no = Data::new().add(0, 1, 1).add(1, 0, 2).add(0, 2, 3);
        assert_eq!(match_motifs(Motif::PingPong, &no.ev(), 10, false).unwrap().0, 0);
    }

    #[test]
    fn star_requires_three_distinct_targets() {
        let d = Data::new().add(0, 1, 1).add(0, 2, 2).add(0, 3, 3);
        assert_eq!(match_motifs(Motif::StarOut3, &d.ev(), 10, false).unwrap().0, 1);
        let dup = Data::new().add(0, 1, 1).add(0, 2, 2).add(0, 1, 3);
        assert_eq!(match_motifs(Motif::StarOut3, &dup.ev(), 10, false).unwrap().0, 0);
    }

    #[test]
    fn path_requires_four_distinct_nodes() {
        let d = Data::new().add(0, 1, 1).add(1, 2, 2).add(2, 3, 3);
        assert_eq!(match_motifs(Motif::Path3, &d.ev(), 10, false).unwrap().0, 1);
        let back = Data::new().add(0, 1, 1).add(1, 2, 2).add(2, 0, 3);
        assert_eq!(match_motifs(Motif::Path3, &back.ev(), 10, false).unwrap().0, 0);
    }

    #[test]
    fn agrees_with_brute_force_on_dense_random_graphs() {
        // small node set and tight times, so motifs actually occur
        let mut seed = 0x2026_0728_u64;
        let mut rng = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for case in 0..40 {
            let mut d = Data::new();
            let nodes = 4 + (case % 3) as i64;
            for _ in 0..30 {
                let s = (rng() % nodes as u64) as i64;
                let dd = (rng() % nodes as u64) as i64;
                let t = (rng() % 25) as i64;
                d = d.add(s, dd, t);
            }
            for delta in [0i64, 3, 10, 1_000] {
                for m in ALL {
                    let (fast, inst) = match_motifs(m, &d.ev(), delta, true).unwrap();
                    assert_eq!(
                        fast,
                        brute(m, &d, delta),
                        "case {case} delta {delta} motif {m:?}"
                    );
                    assert_eq!(inst.len() as u64, fast, "count and instances disagree");
                }
            }
        }
    }

    #[test]
    fn instances_come_back_in_a_deterministic_order() {
        let mut d = Data::new();
        for k in 0..6 {
            d = d.add(0, 1, k * 2).add(1, 0, k * 2 + 1);
        }
        let (_, m) = match_motifs(Motif::PingPong, &d.ev(), 100, true).unwrap();
        // every instance is strictly increasing in the (t, eid) total order
        let key = |r: u32| (d.t[r as usize], d.eid[r as usize].clone());
        for tri in &m {
            assert!(key(tri[0]) < key(tri[1]) && key(tri[1]) < key(tri[2]));
        }
        // and the instances themselves come back in that same order
        let keys: Vec<_> = m.iter().map(|t| t.map(key)).collect();
        assert!(keys.windows(2).all(|w| w[0] < w[1]), "instances must be ordered");
        let (_, again) = match_motifs(Motif::PingPong, &d.ev(), 100, true).unwrap();
        assert_eq!(m, again, "repeated calls must agree");
    }

    #[test]
    fn empty_and_degenerate_inputs_are_handled() {
        let d = Data::new();
        for m in ALL {
            assert_eq!(match_motifs(m, &d.ev(), 10, true).unwrap(), (0, vec![]));
        }
        let one = Data::new().add(0, 1, 1);
        assert_eq!(match_motifs(Motif::Path3, &one.ev(), 10, false).unwrap().0, 0);
    }

    #[test]
    fn mismatched_columns_and_negative_delta_are_rejected() {
        let src = [0i64];
        let dst = [1i64, 2];
        let t = [1i64];
        let eid = ["a".to_string()];
        let bad = Events { src: &src, dst: &dst, t: &t, eid: &eid };
        assert!(match_motifs(Motif::Path3, &bad, 1, false).is_err());

        let d = Data::new().add(0, 1, 1);
        assert!(match_motifs(Motif::Path3, &d.ev(), -1, false).is_err());
    }
}
