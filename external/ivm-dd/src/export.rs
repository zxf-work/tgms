//! Loader for one exported cell bundle, as written by
//! `scripts/export_storm_workload.py` (see the module-level NOTE in that
//! script for the bundle's exact contents).
//!
//! Bundle layout, per `<export-root>/<cell_id>/`:
//!   `versions-epoch0.jsonl`   one `VersionRow` per line (epoch-0 state)
//!   `artifacts.jsonl`         `{name, op, args}` per registered artifact
//!   `deltas.jsonl`            one line per epoch k=1..batches:
//!                             `{epoch, tt, correction_class, generator,
//!                               placement, closed: [{kind,vid,tt_e}],
//!                               inserted: [VersionRow + vid]}`
//!   `oracle.jsonl`            one line per epoch k=0..batches:
//!                             `{epoch, digests: {name: digest|null},
//!                               refused: [name]}`
//!   `export-manifest.json`    cell_digest, config, file sha256s
//!
//! `eventlog-tail.jsonl` (raw log bytes) is not needed by this crate: the
//! changelog it needs is already the diffed `deltas.jsonl`.

use crate::model::{Kind, VersionRow};
use serde::Deserialize;
use std::fs;
use std::path::Path;

#[derive(Debug, Deserialize, Clone)]
pub struct ArtifactSpec {
    pub name: String,
    pub op: String,
    pub args: serde_json::Value,
}

#[derive(Debug, Deserialize)]
pub struct ClosedEntry {
    pub kind: Kind,
    pub vid: String,
    pub tt_e: i64,
}

#[derive(Debug, Deserialize)]
pub struct DeltaRow {
    pub epoch: u64,
    pub tt: i64,
    pub correction_class: String,
    pub generator: String,
    pub placement: String,
    pub closed: Vec<ClosedEntry>,
    pub inserted: Vec<VersionRow>,
}

#[derive(Debug, Deserialize)]
pub struct OracleRow {
    pub epoch: u64,
    pub digests: std::collections::BTreeMap<String, Option<String>>,
    pub refused: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct ExportManifest {
    pub cell_id: String,
    pub cell_digest: String,
    pub config: serde_json::Value,
    #[serde(default)]
    pub files: std::collections::BTreeMap<String, String>,
}

pub struct CellBundle {
    pub cell_id: String,
    pub epoch0: Vec<VersionRow>,
    pub artifacts: Vec<ArtifactSpec>,
    pub deltas: Vec<DeltaRow>,
    pub oracle: Vec<OracleRow>,
    pub manifest: ExportManifest,
}

fn read_jsonl<T: for<'de> Deserialize<'de>>(path: &Path) -> std::io::Result<Vec<T>> {
    let text = fs::read_to_string(path)?;
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let v: T = serde_json::from_str(line).map_err(|e| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!("{}:{}: {}", path.display(), i + 1, e),
            )
        })?;
        out.push(v);
    }
    Ok(out)
}

fn json_err(e: serde_json::Error) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, e)
}

impl CellBundle {
    pub fn load(cell_dir: &Path) -> std::io::Result<CellBundle> {
        let manifest: ExportManifest = serde_json::from_str(
            &fs::read_to_string(cell_dir.join("export-manifest.json"))?,
        )
        .map_err(json_err)?;
        let epoch0: Vec<VersionRow> = read_jsonl(&cell_dir.join("versions-epoch0.jsonl"))?;
        let artifacts: Vec<ArtifactSpec> = read_jsonl(&cell_dir.join("artifacts.jsonl"))?;
        let deltas: Vec<DeltaRow> = read_jsonl(&cell_dir.join("deltas.jsonl"))?;
        let oracle: Vec<OracleRow> = read_jsonl(&cell_dir.join("oracle.jsonl"))?;
        Ok(CellBundle {
            cell_id: manifest.cell_id.clone(),
            epoch0,
            artifacts,
            deltas,
            oracle,
            manifest,
        })
    }

    pub fn batches(&self) -> usize {
        self.deltas.len()
    }
}
