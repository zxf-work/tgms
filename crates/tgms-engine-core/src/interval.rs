//! Interval-join pair enumeration — the inner loop of O11 `co_active`.
//!
//! The candidate range for each `a` row is already a pair of binary searches
//! over `b`'s starts, which vectorizes fine. What did not was walking those
//! ranges: a Python double loop over ~5.3 s at 1M events, the hotspot the
//! scan probe ranked first. That loop is all this module is.
//!
//! Deliberately narrow. Allen-relation choice, the cost cap, and the
//! self-pair check stay on the Python side: the first two are cheap and
//! vectorized, and the third only has to look at pairs that survive — which
//! the cap already bounds — so none of them belongs in a hot loop, and
//! keeping them out means this kernel needs no identities and no strings.

use crate::error::{EngineError, Result};

/// Enumerate `(a_row, b_row)` candidate pairs.
///
/// `lo[i]..hi[i]` is the candidate range in `b` for `a` row `i`, as produced
/// by the relation's binary searches. `require_b_end_after_a_end` carries the
/// one predicate the range bounds cannot express — `overlaps` and `during`
/// both additionally need `b.vt_e > a.vt_e`.
///
/// Pairs come back in `(i, j)` order. Both inputs arrive in `(vt_s, vid)`
/// order from the columnar scan, so that is already the order the operator
/// contract promises, and no sort is needed.
pub fn interval_pairs(
    lo: &[u32],
    hi: &[u32],
    a_vt_e: &[i64],
    b_vt_e: &[i64],
    require_b_end_after_a_end: bool,
) -> Result<Vec<(u32, u32)>> {
    if lo.len() != hi.len() || lo.len() != a_vt_e.len() {
        return Err(EngineError::invariant(
            "interval join: lo, hi and a_vt_e must have one entry per a-row",
        ));
    }
    let nb = b_vt_e.len() as u32;
    let mut out = Vec::new();
    for i in 0..lo.len() {
        let (start, end) = (lo[i], hi[i].min(nb));
        if start >= end {
            continue;
        }
        let a_end = a_vt_e[i];
        if require_b_end_after_a_end {
            for j in start..end {
                if b_vt_e[j as usize] > a_end {
                    out.push((i as u32, j));
                }
            }
        } else {
            // the range bounds already decided every pair
            out.reserve((end - start) as usize);
            for j in start..end {
                out.push((i as u32, j));
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn walks_each_candidate_range() {
        let pairs = interval_pairs(&[0, 2], &[2, 4], &[10, 10], &[0, 0, 0, 0], false).unwrap();
        assert_eq!(pairs, vec![(0, 0), (0, 1), (1, 2), (1, 3)]);
    }

    #[test]
    fn empty_and_inverted_ranges_yield_nothing() {
        assert!(interval_pairs(&[3], &[3], &[1], &[0, 0, 0, 0], false).unwrap().is_empty());
        assert!(interval_pairs(&[3], &[1], &[1], &[0, 0, 0, 0], false).unwrap().is_empty());
        assert!(interval_pairs(&[], &[], &[], &[], false).unwrap().is_empty());
    }

    #[test]
    fn end_predicate_filters_only_when_requested() {
        // b ends: 5, 20 — only the second outlives a's end of 10
        let b_end = [5i64, 20];
        let filtered = interval_pairs(&[0], &[2], &[10], &b_end, true).unwrap();
        assert_eq!(filtered, vec![(0, 1)]);
        let unfiltered = interval_pairs(&[0], &[2], &[10], &b_end, false).unwrap();
        assert_eq!(unfiltered, vec![(0, 0), (0, 1)]);
    }

    #[test]
    fn a_range_running_past_b_is_clamped() {
        // a defensive clamp: a malformed hi must not read out of bounds
        let pairs = interval_pairs(&[0], &[99], &[1], &[0, 0], false).unwrap();
        assert_eq!(pairs, vec![(0, 0), (0, 1)]);
    }

    #[test]
    fn pairs_come_back_in_input_order() {
        let lo = [0u32, 0, 1];
        let hi = [1u32, 3, 3];
        let pairs = interval_pairs(&lo, &hi, &[0, 0, 0], &[0, 0, 0], false).unwrap();
        assert!(pairs.windows(2).all(|w| w[0] < w[1]), "must be ascending");
    }

    #[test]
    fn mismatched_inputs_are_rejected() {
        assert!(interval_pairs(&[0, 1], &[1], &[1, 2], &[0], false).is_err());
        assert!(interval_pairs(&[0], &[1], &[1, 2], &[0], false).is_err());
    }
}
