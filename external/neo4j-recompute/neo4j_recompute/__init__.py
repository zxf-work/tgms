"""external-v1 Neo4j 5 recompute configuration for P-EXT1 (lane N1).

This package is deliberately self-contained: it does not import `tgms`.
Reproducing TGMS's canonicalization/digest algorithm independently (see
`canon.py`) rather than calling into the system under test is the same
posture as the P-EXT2 Rust crate (own `Cargo.toml`, "never joins the engine
workspace") — an external baseline that quietly imported the code it is
cross-checking would let a bug in that code cancel out of the comparison.
"""
