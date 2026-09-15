//! Forwards the two build-script-only env vars Cargo sets (`PROFILE`,
//! `OPT_LEVEL`) into compile-time env vars the crate itself can read via
//! `env!()` — normal compilation of `src/lib.rs` never sees either one
//! directly, only a build script does.
//!
//! What this is for: `build_info()`'s `profile`/`opt_level` fields
//! (`lib.rs`). The 2026-09 engine-commit A/B diagnosis found the untimed
//! residual absorbing all of the treatment's decile growth, and a
//! debug-assertions build — which runs `publish`'s O(segments)
//! `debug_assert_eq!` every commit — was the top candidate. Every future
//! timing record should be able to say which build profile it ran.

fn main() {
    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "unknown".to_string());
    let opt_level = std::env::var("OPT_LEVEL").unwrap_or_else(|_| "unknown".to_string());
    println!("cargo:rustc-env=TGMS_BUILD_PROFILE={profile}");
    println!("cargo:rustc-env=TGMS_BUILD_OPT_LEVEL={opt_level}");
}
