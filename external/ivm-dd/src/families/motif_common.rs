//! Shared matching logic for F9 `count_temporal_motifs` and F10
//! `find_temporal_motif_instances`, motif `M_2node_pingpong` only (the one
//! grid-registered motif; memo table). Semantics (`ops_motifs.py` module
//! docstring): an event is an edge version at `t = vt_s` inside
//! `[window.t_a, window.t_b)`; motif edges are strictly ordered by
//! `(t, eid)`; `rel_type` is ignored; `u<>v`.
//!
//! `M_2node_pingpong`: `a: u->v`, `b: v->u`, `c: u->v`, with
//! `(b.t,b.eid) > (a.t,a.eid)`, `(c.t,c.eid) > (b.t,b.eid)`, and both
//! `b.t - a.t <= delta` and `c.t - a.t <= delta` (memo's Cypher text F9).
//!
//! **Not** memo §3.2's bucket routing or the native engine's windowed
//! event index (`tgms._engine.motif_match`) -- a direct O(pairs × events²)
//! enumeration per ordered pair, matching the Cypher text literally. This
//! is the part the memo flags for Opus review before any timed run (§3.7);
//! it is correct on the tiny fixture this crate's tests pin, and is the
//! obvious place to look first if a larger cell's motif digest disagrees.

use crate::model::VersionRow;

#[derive(Clone, Copy, Debug)]
pub struct Event<'a> {
    pub t: i64,
    pub eid: &'a str,
    pub src: &'a str,
    pub dst: &'a str,
    pub rel_type: &'a str,
}

pub fn events_in_window<'a>(
    edges: &'a [VersionRow],
    t_a: i64,
    t_b: i64,
    as_of: i64,
    node_filter: &Option<Vec<String>>,
) -> Vec<Event<'a>> {
    // `ops_motifs._events` reads `adapter.edges_columnar(as_of_tt=...)`,
    // which only ever returns *believed* edges -- a version whose belief
    // has been superseded (but which this crate's changelog keeps around
    // as a historical row, bitemporal immutability) must not become an
    // "event" just because its vt_s falls in the window. Omitting this
    // filter double-counts a corrected edge (old + new belief, same
    // `eid`/`vt_s`) for exactly one burst, which is the bug
    // `tests/fixtures/tiny1`'s shape test caught.
    let mut out: Vec<Event<'a>> = edges
        .iter()
        .filter(|e| e.believed_at(as_of))
        .filter(|e| e.vt_s >= t_a && e.vt_s < t_b)
        .filter(|e| match node_filter {
            None => true,
            Some(nf) => {
                let s = e.src.as_deref().unwrap_or("");
                let d = e.dst.as_deref().unwrap_or("");
                nf.iter().any(|x| x == s) && nf.iter().any(|x| x == d)
            }
        })
        .map(|e| Event {
            t: e.vt_s,
            eid: e.eid.as_deref().unwrap_or(""),
            src: e.src.as_deref().unwrap_or(""),
            dst: e.dst.as_deref().unwrap_or(""),
            rel_type: e.rel_type.as_deref().unwrap_or(""),
        })
        .collect();
    out.sort_by(|a, b| (a.t, a.eid).cmp(&(b.t, b.eid)));
    out
}

/// Every triple `(a, b, c)` (as indices into `events`) matching
/// `M_2node_pingpong` for ordered pair `(u, v)`. `events` need not be
/// pre-filtered to the pair -- this scans the whole window once per call,
/// which is the crate's deliberate, flagged simplification (see module
/// doc).
pub fn pingpong_triples(events: &[Event], delta: i64) -> Vec<(usize, usize, usize)> {
    let mut out = Vec::new();
    for (ia, a) in events.iter().enumerate() {
        if a.src == a.dst {
            continue; // u <> v
        }
        let (u, v) = (a.src, a.dst);
        for (ib, b) in events.iter().enumerate() {
            if !((b.t, b.eid) > (a.t, a.eid)) {
                continue;
            }
            if b.t - a.t > delta {
                break; // events sorted by t; no later b can satisfy either
            }
            if !(b.src == v && b.dst == u) {
                continue;
            }
            for (ic, c) in events.iter().enumerate() {
                if !((c.t, c.eid) > (b.t, b.eid)) {
                    continue;
                }
                if c.t - a.t > delta {
                    break;
                }
                if c.src == u && c.dst == v {
                    out.push((ia, ib, ic));
                }
            }
        }
    }
    out
}
