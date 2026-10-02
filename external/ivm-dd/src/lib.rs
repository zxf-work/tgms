//! `ivm-dd`: incremental view maintenance of TGMS's registered artifacts
//! with differential dataflow. Consumes one exported cell (epoch-0 version
//! table, registered artifacts, per-burst corrections, oracle digests),
//! maintains one dataflow per artifact family, and emits per-burst refresh
//! wall time and output digests for the external comparison. Library
//! surface so `tests/` can exercise the changelog encoder and the family
//! payload functions directly, independent of the `main.rs` CLI.

pub mod changelog;
pub mod dataflow;
pub mod digest;
pub mod export;
pub mod families;
pub mod model;
pub mod record;
pub mod views;
pub mod withheld;
