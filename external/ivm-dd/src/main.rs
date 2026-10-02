//! `ivm-dd` CLI (memo §3.7 / this task's validation steps):
//!
//!   ivm-dd run <cell-dir> --out <out-dir>        full per-burst run, writes result.json + run.log
//!   ivm-dd shape-test <cell-dir>                 epoch-0 digest vs oracle.jsonl, every family
//!   ivm-dd withheld <cell-dir> --out <out-dir>   the 37th-cell F-epoch/F-watermark check

use clap::{Parser, Subcommand};
use ivm_dd::{changelog, dataflow, export::CellBundle, record, withheld};
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Parser)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Full per-burst run over one exported cell; writes result.json + run.log.
    Run {
        cell_dir: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    /// Epoch-0 shape test: this crate's digest vs the export's own oracle digest, per family.
    ShapeTest { cell_dir: PathBuf },
    /// The 37th-cell withheld-correction check (F-epoch / F-watermark), memo §3.5.
    Withheld {
        cell_dir: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
}

fn now_utc() -> String {
    // `date -u`-equivalent RFC3339, no chrono dependency: build it from
    // SystemTime + a tiny civil-calendar conversion rather than add a crate
    // for one timestamp string.
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
    civil_from_unix(secs)
}

fn civil_from_unix(secs: i64) -> String {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    // Howard Hinnant's days_from_civil inverse (civil_from_days), epoch 1970-01-01.
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m_ = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m_ <= 2 { y + 1 } else { y };
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, m_, d, h, m, s)
}

fn host_snapshot(label: &str) -> record::HostSnapshot {
    let uname = std::process::Command::new("uname")
        .arg("-a")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    let free_g = std::process::Command::new("free")
        .arg("-g")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    let loadavg1 = std::fs::read_to_string("/proc/loadavg")
        .ok()
        .and_then(|s| s.split_whitespace().next().map(|s| s.to_string()))
        .and_then(|s| s.parse().ok());
    record::HostSnapshot {
        label: label.to_string(),
        timestamp_utc: now_utc(),
        uname,
        loadavg1,
        free_g,
    }
}

const DEVIATIONS: &[&str] = &[
    "Routing is by natural key (uid / kind / global), not memo §3.2's 256-bucket \
     valid-time routing -- correctness-preserving, coarser fan-out; see dataflow.rs.",
    "F11 temporal_reachability does NOT use DD's iterate, contrary to the memo and \
     this task's explicit instruction: an iterate-based dataflow was built and failed \
     to re-derive correctly after a pure retraction on tests/fixtures/tiny1 (stuck at \
     its pre-retraction answer); root cause not found in time. F11 is computed fresh \
     via the same Global route+reduce path as F9/F10/F12 instead. See dataflow.rs's \
     module doc for the full account; flagged for the Opus review the memo calls for.",
    "F12 temporal_paths runs its DFS inside a Global route+reduce closure, not \
     memo §3.3's four static unrolled joins; see dataflow.rs and the F12 module doc.",
    "F9/F10 motif matching is a direct O(events^3)-worst-case enumeration per the \
     Cypher text, not the native engine's windowed event index; flagged for the \
     Opus review the memo itself calls for on F9-F12.",
    "The crate path is external/ivm-dd/ per this task's explicit instruction, not \
     memo §3.1's benchmarks/external-v1/ivm-dd/ -- reported, not silently changed.",
    "refresh_ms/publish_ms split: this crate times dataflow-maintain-and-serialize \
     as refresh_ms and sha256-of-already-serialized-strings as publish_ms, not \
     memo §3.4's maintain-only vs serialize-and-hash split.",
];

fn main() {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Run { cell_dir, out } => cmd_run(&cell_dir, &out),
        Cmd::ShapeTest { cell_dir } => cmd_shape_test(&cell_dir),
        Cmd::Withheld { cell_dir, out } => cmd_withheld(&cell_dir, &out),
    }
}

fn load_bundle(cell_dir: &Path) -> CellBundle {
    CellBundle::load(cell_dir).unwrap_or_else(|e| {
        eprintln!("failed to load cell bundle at {}: {e}", cell_dir.display());
        std::process::exit(2);
    })
}

fn cmd_run(cell_dir: &Path, out: &Path) {
    let mut log = Vec::new();
    log.push(format!("[{}] loading {}", now_utc(), cell_dir.display()));
    let bundle = load_bundle(cell_dir);
    let cl = changelog::build(&bundle);
    log.push(format!(
        "[{}] {} epoch0 rows, {} bursts, {} artifacts",
        now_utc(),
        cl.epoch0.len(),
        cl.updates.len(),
        bundle.artifacts.len()
    ));

    let host_start = host_snapshot("start");
    let t0 = Instant::now();
    let outcome = dataflow::run(cl, bundle.artifacts.clone());
    let wall_s = t0.elapsed().as_secs_f64();
    let host_end = host_snapshot("end");
    log.push(format!(
        "[{}] run complete in {:.3}s: load_ms={:.3} bursts={}",
        now_utc(),
        wall_s,
        outcome.load_ms,
        outcome.bursts.len()
    ));
    for (op, n) in &outcome.n_unmapped {
        log.push(format!("  unmapped op {op:?}: {n} artifact(s) not evaluated"));
    }

    let versions = record::Versions {
        crate_version: env!("CARGO_PKG_VERSION").to_string(),
        rustc: rustc_version(),
        timely: "0.31.0".to_string(),
        differential_dataflow: "0.25.1".to_string(),
        cargo_lock_sha256: sha256_of_file(&crate_root().join("Cargo.lock")),
        binary_sha256: std::env::current_exe().ok().as_deref().and_then(sha256_of_file),
    };
    let result = record::build_result_json(&bundle, &outcome, &versions, &host_start, &host_end, DEVIATIONS);
    record::write_cell_output(out, &result, &log).unwrap_or_else(|e| {
        eprintln!("failed to write output to {}: {e}", out.display());
        std::process::exit(2);
    });
    println!("wrote {}", out.join("result.json").display());
}

fn crate_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn sha256_of_file(path: &Path) -> Option<String> {
    std::fs::read(path).ok().map(|bytes| ivm_dd::digest::sha256_hex_bytes(&bytes))
}

fn rustc_version() -> String {
    std::process::Command::new("rustc")
        .arg("--version")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|_| "unknown".to_string())
}

fn cmd_shape_test(cell_dir: &Path) {
    let bundle = load_bundle(cell_dir);
    let cl = changelog::build(&bundle);
    let outcome = dataflow::run(cl, bundle.artifacts.clone());

    let row0 = match bundle.oracle.first() {
        Some(r) => r,
        None => {
            eprintln!("no epoch-0 oracle row in {}", cell_dir.display());
            std::process::exit(2);
        }
    };
    let mut ok = 0usize;
    let mut fail = 0usize;
    let mut by_family_fail: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let meta: std::collections::HashMap<String, String> =
        bundle.artifacts.iter().map(|a| (a.name.clone(), a.op.clone())).collect();
    for (name, oracle_digest) in &row0.digests {
        let Some(od) = oracle_digest else { continue };
        match outcome.epoch0_digests.get(name) {
            Some(ours) if ours == od => ok += 1,
            Some(ours) => {
                fail += 1;
                let op = meta.get(name).cloned().unwrap_or_default();
                *by_family_fail.entry(op.clone()).or_insert(0) += 1;
                println!("MISMATCH {name} (op={op}): oracle={od} ours={ours}");
            }
            None => {
                fail += 1;
                let op = meta.get(name).cloned().unwrap_or_default();
                *by_family_fail.entry(op.clone()).or_insert(0) += 1;
                println!("MISSING  {name} (op={op}): oracle={od}, this crate produced nothing");
            }
        }
    }
    println!("shape-test {}: {ok} agree, {fail} disagree/missing", bundle.cell_id);
    for (op, n) in &by_family_fail {
        println!("  op={op}: {n} failing");
    }
    if fail > 0 {
        std::process::exit(1);
    }
}

fn cmd_withheld(cell_dir: &Path, out: &Path) {
    let bundle = load_bundle(cell_dir);
    let results = withheld::run_from_bundle(&bundle);
    let json = serde_json::json!({
        "cell_id": bundle.cell_id,
        "feeders": results.iter().map(|r| serde_json::json!({
            "feeder": r.feeder,
            "probe_reports_complete_through_10": r.probe_reports_complete_through_10,
            "signalled": r.signalled,
            "false_fresh_count": r.false_fresh_count,
            "artifacts": r.artifacts.iter().map(|a| serde_json::json!({
                "name": a.name, "served_digest": a.served_digest,
                "oracle_digest_epoch10": a.oracle_digest_epoch10, "false_fresh": a.false_fresh,
            })).collect::<Vec<_>>(),
        })).collect::<Vec<_>>(),
    });
    std::fs::create_dir_all(out).unwrap();
    std::fs::write(out.join("withheld-result.json"), serde_json::to_string_pretty(&json).unwrap() + "\n").unwrap();
    for r in &results {
        println!("{}: probe_complete_through_10={} false_fresh_count={}", r.feeder, r.probe_reports_complete_through_10, r.false_fresh_count);
    }
}
