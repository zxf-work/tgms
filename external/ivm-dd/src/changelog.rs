//! Changelog encoding: a version row is inserted (+1) at the
//! epoch of its `tt_s` and retracted (−1) at the epoch whose transaction
//! closed it (`tt_e`); a correction is exactly retraction of the superseded
//! believed version(s) + insertion of the corrected ones at their valid
//! time. DD logical time = epoch `u64` (0 = initial state, k = after burst
//! k).
//!
//! The export bundle's `deltas.jsonl` already carries this as a diff
//! (`closed`/`inserted`, computed by `export_storm_workload.py` diffing the
//! live version table before/after each batch) -- `closed` names only
//! `{kind, vid, tt_e}`, the *new* `tt_e`, not the row's other fields, so
//! turning it into a DD retraction needs the row's prior state. This module
//! replays the bundle forward through its own `live` table (mirroring what
//! the export script's `_diff_version_tables` did when it built the bundle)
//! to recover the exact old row to retract.

use crate::export::CellBundle;
use crate::model::{VersionRow, OPEN_END};
use std::collections::HashMap;

/// One closed version's valid-time span, for the `retracted_vt_span`
/// covariate (the retracted version's valid interval). `None`
/// when the believed version's valid-time end was itself still open
/// (`vt_e == OPEN_END`) -- reported as open, never summed as a number.
#[derive(Clone, Copy, Debug)]
pub struct RetractedSpan(pub Option<i64>);

pub struct EpochUpdate {
    pub epoch: u64,
    pub tt: i64,
    pub correction_class: String,
    pub generator: String,
    pub placement: String,
    /// Old row values to retract (weight −1) at this epoch -- includes both
    /// the "closed" (belief-superseded) rows and, for a `vid` that is
    /// *both* closed and immediately reinserted under the same `vid` (never
    /// happens in practice: `vid` is a fresh hash per `(identity, tt_s)`),
    /// nothing extra.
    pub retractions: Vec<VersionRow>,
    /// New row values to insert (weight +1) at this epoch: the corrected
    /// belief for every closed `vid` (same row, new `tt_e`) plus every
    /// freshly inserted `vid`.
    pub insertions: Vec<VersionRow>,
    pub retracted_spans: Vec<RetractedSpan>,
}

pub struct Changelog {
    pub cell_id: String,
    /// Epoch-0 insertions (weight +1 for every row in `versions-epoch0.jsonl`).
    pub epoch0: Vec<VersionRow>,
    pub updates: Vec<EpochUpdate>,
}

pub fn build(bundle: &CellBundle) -> Changelog {
    let mut live: HashMap<String, VersionRow> = HashMap::with_capacity(bundle.epoch0.len());
    for row in &bundle.epoch0 {
        live.insert(row.vid.clone(), row.clone());
    }

    let mut updates = Vec::with_capacity(bundle.deltas.len());
    for delta in &bundle.deltas {
        let mut retractions = Vec::with_capacity(delta.closed.len());
        let mut insertions = Vec::with_capacity(delta.closed.len() + delta.inserted.len());
        let mut retracted_spans = Vec::with_capacity(delta.closed.len());

        for closed in &delta.closed {
            let old = live.get(&closed.vid).cloned().unwrap_or_else(|| {
                panic!(
                    "{}: epoch {} closes vid {:?} with no prior state in the live table \
                     (export bundle is internally inconsistent)",
                    bundle.cell_id, delta.epoch, closed.vid
                )
            });
            let span = if old.vt_e == OPEN_END {
                RetractedSpan(None)
            } else {
                RetractedSpan(Some(old.vt_e - old.vt_s))
            };
            retracted_spans.push(span);

            let mut new_row = old.clone();
            new_row.tt_e = closed.tt_e;
            retractions.push(old);
            insertions.push(new_row.clone());
            live.insert(closed.vid.clone(), new_row);
        }

        for ins in &delta.inserted {
            insertions.push(ins.clone());
            live.insert(ins.vid.clone(), ins.clone());
        }

        updates.push(EpochUpdate {
            epoch: delta.epoch,
            tt: delta.tt,
            correction_class: delta.correction_class.clone(),
            generator: delta.generator.clone(),
            placement: delta.placement.clone(),
            retractions,
            insertions,
            retracted_spans,
        });
    }

    Changelog {
        cell_id: bundle.cell_id.clone(),
        epoch0: bundle.epoch0.clone(),
        updates,
    }
}

impl EpochUpdate {
    /// Sum of closed spans that are not open-ended, plus the count of
    /// open-ended ones -- the aggregate the record reports (the covariate
    /// is the span of history the retractions touch; summing an open span
    /// as a number would understate it).
    pub fn retracted_vt_span_summary(&self) -> (i64, usize, usize) {
        let mut sum = 0i64;
        let mut n_finite = 0usize;
        let mut n_open = 0usize;
        for s in &self.retracted_spans {
            match s.0 {
                Some(v) => {
                    sum += v;
                    n_finite += 1;
                }
                None => n_open += 1,
            }
        }
        (sum, n_finite, n_open)
    }
}
