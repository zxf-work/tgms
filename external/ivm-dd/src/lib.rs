//! `ivm-dd`: the P-EXT2 differential-dataflow IVM configuration
//! (`docs/design/EXTERNAL_BASELINES_DESIGN_2026-10-02.md` §3). Library
//! surface so `tests/` can exercise the changelog encoder and the family
//! payload functions directly, independent of the `main.rs` CLI.

pub mod changelog;
pub mod dataflow;
pub mod digest;
pub mod export;
pub mod families;
pub mod model;
pub mod record;
pub mod withheld;
