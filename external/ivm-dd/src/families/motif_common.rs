//! Shared matching logic for F9 `count_temporal_motifs` and F10
//! `find_temporal_motif_instances`, motif `M_2node_pingpong` only (the one
//! grid-registered motif). Semantics (`ops_motifs.py` module
//! docstring): an event is an edge version at `t = vt_s` inside
//! `[window.t_a, window.t_b)`; motif edges are strictly ordered by
//! `(t, eid)`; `rel_type` is ignored; `u<>v`.
//!
//! `M_2node_pingpong`: `a: u->v`, `b: v->u`, `c: u->v`, with
//! `(b.t,b.eid) > (a.t,a.eid)`, `(c.t,c.eid) > (b.t,b.eid)`, and both
//! `b.t - a.t <= delta` and `c.t - a.t <= delta`.
//!
//! `pingpong_triples` is the reference enumeration (every matching triple,
//! in `(a, b, c)` order). The maintained dataflow (`views.rs`) partitions
//! the events by unordered endpoint pair -- all three events of a
//! `M_2node_pingpong` instance lie on one pair {u, v} -- and runs
//! `pingpong_count` / `pingpong_first` over one pair's events at a time;
//! `tests/maintained_tests.rs` pins both against `pingpong_triples`.

use crate::model::VersionRow;
use serde::{Deserialize, Serialize};

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

/// An owned motif event (one believed edge version at `t = vt_s`), the
/// record the maintained dataflow carries per (artifact, endpoint pair).
/// Field order gives the derived `Ord` the `(t, eid)` event order.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct MEv {
    pub t: i64,
    pub eid: String,
    pub src: String,
    pub dst: String,
    pub rel_type: String,
}

impl MEv {
    pub fn from_row(e: &VersionRow) -> MEv {
        MEv {
            t: e.vt_s,
            eid: e.eid.clone().unwrap_or_default(),
            src: e.src.clone().unwrap_or_default(),
            dst: e.dst.clone().unwrap_or_default(),
            rel_type: e.rel_type.clone().unwrap_or_default(),
        }
    }
    pub fn as_event(&self) -> Event<'_> {
        Event { t: self.t, eid: &self.eid, src: &self.src, dst: &self.dst, rel_type: &self.rel_type }
    }
    /// The unordered endpoint pair this event belongs to.
    pub fn pair(&self) -> (String, String) {
        if self.src <= self.dst {
            (self.src.clone(), self.dst.clone())
        } else {
            (self.dst.clone(), self.src.clone())
        }
    }
}

/// One `M_2node_pingpong` instance. The derived `Ord` is the reference's
/// output order: `(a.t, a.eid, b.t, b.eid, c.t, c.eid)`.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Inst {
    pub a: MEv,
    pub b: MEv,
    pub c: MEv,
}

/// `pingpong_triples(events, delta).len()` without enumerating: for each
/// first event `a` (u -> v), the candidates for `b` and `c` are the events
/// strictly after `a` in `(t, eid)` order with `t - a.t <= delta` (a
/// contiguous run, since `events` is sorted); every `c` (u -> v) in that run
/// pairs with each earlier `b` (v -> u) in it. O(n * run length).
pub fn pingpong_count(events: &[Event], delta: i64) -> u64 {
    let mut total = 0u64;
    for (ia, a) in events.iter().enumerate() {
        if a.src == a.dst {
            continue;
        }
        let (u, v) = (a.src, a.dst);
        let mut n_b = 0u64;
        for e in &events[ia + 1..] {
            if (e.t, e.eid) <= (a.t, a.eid) {
                continue;
            }
            if e.t - a.t > delta {
                break;
            }
            if e.src == u && e.dst == v {
                total += n_b;
            } else if e.src == v && e.dst == u {
                n_b += 1;
            }
        }
    }
    total
}

/// The first `k` triples of `pingpong_triples(events, delta)` (same order),
/// stopping as soon as `k` are found.
pub fn pingpong_first(events: &[Event], delta: i64, k: usize) -> Vec<(usize, usize, usize)> {
    let mut out = Vec::new();
    if k == 0 {
        return out;
    }
    for (ia, a) in events.iter().enumerate() {
        if a.src == a.dst {
            continue;
        }
        let (u, v) = (a.src, a.dst);
        for (ib, b) in events.iter().enumerate().skip(ia + 1) {
            if (b.t, b.eid) <= (a.t, a.eid) {
                continue;
            }
            if b.t - a.t > delta {
                break;
            }
            if !(b.src == v && b.dst == u) {
                continue;
            }
            for (ic, c) in events.iter().enumerate().skip(ib + 1) {
                if (c.t, c.eid) <= (b.t, b.eid) {
                    continue;
                }
                if c.t - a.t > delta {
                    break;
                }
                if c.src == u && c.dst == v {
                    out.push((ia, ib, ic));
                    if out.len() == k {
                        return out;
                    }
                }
            }
        }
    }
    out
}
