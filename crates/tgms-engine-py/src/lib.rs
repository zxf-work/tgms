//! PyO3 bindings for the TGMS native storage engine.
//!
//! This crate is deliberately thin: it owns the Python boundary (type
//! conversion, buffer ownership, error mapping) and nothing else. All engine
//! logic lives in `tgms-engine-core`, which knows nothing about Python. If a
//! decision has to be made here, it belongs one layer down.
//!
//! **Boundary rule** (implementation spec §6): calls are coarse — one
//! crossing per batch, never per row. Every method that moves rows takes or
//! returns a *dict of columns*, not a list of records, so a 50,000-event
//! ingest chunk is one call rather than fifty thousand.
//!
//! Deviation from spec §6 worth noting: reads live on `NativeStore` rather
//! than on a separate `Snapshot` handle. The store's reads already union
//! rows staged in the open batch, which is what `apply_ops` needs, and a
//! single-writer store has exactly one live view. A pinned-generation
//! `Snapshot` becomes meaningful when concurrent readers arrive; adding it
//! now would be a second way to say the same thing.

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::{PyIOError, PyKeyError, PyOverflowError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use tgms_engine_core::derive::Id96;
use tgms_engine_core::error::{Category, EngineError};
use tgms_engine_core::read::{EdgeVersionOut, NodeVersionOut};
use tgms_engine_core::row::{EdgeRow, NodeRow, RowKind};
use tgms_engine_core::scan::{ScanRequest, ScanSet, ScanTarget};
use tgms_engine_core::segment::MmapSource;
use tgms_engine_core::store::{segment_id_of, NativeStore as CoreStore};
use tgms_engine_core::OPEN_END;

/// Map an engine error onto a Python exception.
///
/// The category is kept as the message prefix (`not_found: ...`) so the
/// Python adapter can translate into `tgms.core.errors` precisely rather
/// than pattern-matching prose.
fn err(e: EngineError) -> PyErr {
    let msg = e.to_string();
    match e.category {
        Category::NotFound => PyKeyError::new_err(msg),
        Category::Capacity => PyOverflowError::new_err(msg),
        Category::Io => PyIOError::new_err(msg),
        Category::Corrupt | Category::Invariant => PyRuntimeError::new_err(msg),
    }
}

type Res<T> = Result<T, PyErr>;

fn kind_of(kind: &str) -> Res<RowKind> {
    match kind {
        "node" => Ok(RowKind::Node),
        "edge" => Ok(RowKind::Edge),
        other => Err(PyRuntimeError::new_err(format!(
            "unknown row kind {other:?}; expected 'node' or 'edge'"
        ))),
    }
}

fn id96(hex: &str) -> Res<Id96> {
    Id96::from_hex(hex).map_err(err)
}

/// Columns in, columns out. Node versions as a dict of equal-length lists.
fn nodes_to_py(py: Python<'_>, rows: Vec<NodeVersionOut>) -> Res<Py<PyDict>> {
    let d = PyDict::new(py);
    d.set_item("vid", rows.iter().map(|r| r.vid.clone()).collect::<Vec<_>>())?;
    d.set_item("uid", rows.iter().map(|r| r.uid.clone()).collect::<Vec<_>>())?;
    d.set_item("label", rows.iter().map(|r| r.label.clone()).collect::<Vec<_>>())?;
    d.set_item("vt_s", rows.iter().map(|r| r.vt_s).collect::<Vec<_>>())?;
    d.set_item("vt_e", rows.iter().map(|r| r.vt_e).collect::<Vec<_>>())?;
    d.set_item("tt_s", rows.iter().map(|r| r.tt_s).collect::<Vec<_>>())?;
    d.set_item("tt_e", rows.iter().map(|r| r.tt_e).collect::<Vec<_>>())?;
    d.set_item("props", rows.iter().map(|r| r.props.clone()).collect::<Vec<_>>())?;
    d.set_item("source", rows.iter().map(|r| r.source.clone()).collect::<Vec<_>>())?;
    d.set_item(
        "provenance_ref",
        rows.iter().map(|r| r.provenance_ref.clone()).collect::<Vec<_>>(),
    )?;
    Ok(d.into())
}

fn edges_to_py(py: Python<'_>, rows: Vec<EdgeVersionOut>) -> Res<Py<PyDict>> {
    let d = PyDict::new(py);
    d.set_item("eid", rows.iter().map(|r| r.eid.clone()).collect::<Vec<_>>())?;
    d.set_item("vid", rows.iter().map(|r| r.vid.clone()).collect::<Vec<_>>())?;
    d.set_item("src", rows.iter().map(|r| r.src.clone()).collect::<Vec<_>>())?;
    d.set_item("dst", rows.iter().map(|r| r.dst.clone()).collect::<Vec<_>>())?;
    d.set_item(
        "rel_type",
        rows.iter().map(|r| r.rel_type.clone()).collect::<Vec<_>>(),
    )?;
    d.set_item("disc", rows.iter().map(|r| r.disc.clone()).collect::<Vec<_>>())?;
    d.set_item("vt_s", rows.iter().map(|r| r.vt_s).collect::<Vec<_>>())?;
    d.set_item("vt_e", rows.iter().map(|r| r.vt_e).collect::<Vec<_>>())?;
    d.set_item("tt_s", rows.iter().map(|r| r.tt_s).collect::<Vec<_>>())?;
    d.set_item("tt_e", rows.iter().map(|r| r.tt_e).collect::<Vec<_>>())?;
    d.set_item("props", rows.iter().map(|r| r.props.clone()).collect::<Vec<_>>())?;
    d.set_item("source", rows.iter().map(|r| r.source.clone()).collect::<Vec<_>>())?;
    d.set_item(
        "provenance_ref",
        rows.iter().map(|r| r.provenance_ref.clone()).collect::<Vec<_>>(),
    )?;
    Ok(d.into())
}

/// Columnar arguments for one staged batch of edge versions.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct EdgeCols {
    vid: Vec<String>,
    src: Vec<String>,
    dst: Vec<String>,
    rel_type: Vec<String>,
    disc: Vec<String>,
    vt_s: Vec<i64>,
    vt_e: Vec<i64>,
    tt_s: Vec<i64>,
    props: Vec<String>,
    source: Vec<String>,
    provenance_ref: Vec<Option<String>>,
}

#[derive(FromPyObject)]
#[pyo3(from_item_all)]
struct NodeCols {
    vid: Vec<String>,
    uid: Vec<String>,
    label: Vec<String>,
    vt_s: Vec<i64>,
    vt_e: Vec<i64>,
    tt_s: Vec<i64>,
    props: Vec<String>,
    source: Vec<String>,
    provenance_ref: Vec<Option<String>>,
}

#[pyclass(module = "tgms._engine")]
pub struct NativeStore {
    inner: CoreStore,
}

#[pymethods]
impl NativeStore {
    #[new]
    fn new(path: &str) -> Res<Self> {
        Ok(Self {
            inner: CoreStore::open(path).map_err(err)?,
        })
    }

    fn close(&mut self) {
        // segments are mmap'd read-only and dropped with the store; there is
        // no writer handle to flush, because a commit is already durable
    }

    fn generation(&self) -> u64 {
        self.inner.generation()
    }

    // --- batch lifecycle ------------------------------------------------ //

    fn begin(&mut self, tt: i64) -> Res<()> {
        self.inner.begin(tt).map_err(err)
    }

    fn commit(&mut self) -> Res<u64> {
        // the event-log position is owned by the Python `Store`, which has
        // already appended and fsynced before calling us; wiring the offset
        // through is a WP-N5 concern (suffix replay)
        self.inner
            .commit(tgms_engine_core::manifest::EventLogRef::default())
            .map_err(err)
    }

    fn rollback(&mut self) -> Res<()> {
        self.inner.rollback().map_err(err)
    }

    fn in_batch(&self) -> bool {
        self.inner.in_batch()
    }

    // --- dictionary ----------------------------------------------------- //

    fn ensure_entities(&mut self, uids: Vec<String>, labels: Vec<String>) -> Res<()> {
        if uids.len() != labels.len() {
            return Err(PyRuntimeError::new_err(
                "ensure_entities: uids and labels differ in length",
            ));
        }
        for (u, l) in uids.iter().zip(&labels) {
            self.inner.ensure_entity(u, l).map_err(err)?;
        }
        Ok(())
    }

    fn dense_ids<'py>(&self, py: Python<'py>, uids: Vec<String>) -> Res<Bound<'py, PyArray1<i64>>> {
        let mut out = Vec::with_capacity(uids.len());
        for u in &uids {
            match self.inner.dict().dense_id(u) {
                Some(id) => out.push(id as i64),
                None => return Err(PyKeyError::new_err(format!("unknown uid: {u}"))),
            }
        }
        Ok(out.into_pyarray(py))
    }

    fn uids_for(&self, ids: Vec<i64>) -> Res<Vec<String>> {
        ids.iter()
            .map(|&i| {
                self.inner
                    .dict()
                    .uid(i as u32)
                    .map(str::to_string)
                    .ok_or_else(|| PyKeyError::new_err(format!("unknown dense id: {i}")))
            })
            .collect()
    }

    fn num_entities(&self) -> u32 {
        self.inner.dict().len()
    }

    // --- staging (one call per insert batch) ----------------------------- //

    fn stage_edges(&mut self, cols: EdgeCols) -> Res<()> {
        let n = cols.vid.len();
        for i in 0..n {
            let src_id = self
                .inner
                .dict()
                .dense_id(&cols.src[i])
                .ok_or_else(|| PyKeyError::new_err(format!("unknown uid: {}", cols.src[i])))?;
            let dst_id = self
                .inner
                .dict()
                .dense_id(&cols.dst[i])
                .ok_or_else(|| PyKeyError::new_err(format!("unknown uid: {}", cols.dst[i])))?;
            self.inner
                .stage_edge(EdgeRow {
                    vid: id96(&cols.vid[i])?,
                    src_id,
                    dst_id,
                    rel_type: cols.rel_type[i].clone(),
                    disc: cols.disc[i].clone(),
                    vt_s: cols.vt_s[i],
                    vt_e: cols.vt_e[i],
                    tt_s: cols.tt_s[i],
                    props: cols.props[i].clone(),
                    source: cols.source[i].clone(),
                    provenance_ref: cols.provenance_ref[i].clone(),
                })
                .map_err(err)?;
        }
        Ok(())
    }

    fn stage_nodes(&mut self, cols: NodeCols) -> Res<()> {
        for i in 0..cols.vid.len() {
            let uid_id = self
                .inner
                .dict()
                .dense_id(&cols.uid[i])
                .ok_or_else(|| PyKeyError::new_err(format!("unknown uid: {}", cols.uid[i])))?;
            self.inner
                .stage_node(NodeRow {
                    vid: id96(&cols.vid[i])?,
                    uid_id,
                    label: cols.label[i].clone(),
                    vt_s: cols.vt_s[i],
                    vt_e: cols.vt_e[i],
                    tt_s: cols.tt_s[i],
                    props: cols.props[i].clone(),
                    source: cols.source[i].clone(),
                    provenance_ref: cols.provenance_ref[i].clone(),
                })
                .map_err(err)?;
        }
        Ok(())
    }

    fn stage_closes(&mut self, kind: &str, vids: Vec<String>, tt_e: i64) -> Res<()> {
        let k = kind_of(kind)?;
        for v in &vids {
            self.inner.close_version(k, id96(v)?, tt_e).map_err(err)?;
        }
        Ok(())
    }

    // --- reads ----------------------------------------------------------- //

    fn all_versions(&self, py: Python<'_>, kind: &str) -> Res<Py<PyDict>> {
        match kind_of(kind)? {
            RowKind::Edge => edges_to_py(py, self.inner.all_edge_versions().map_err(err)?),
            RowKind::Node => nodes_to_py(py, self.inner.all_node_versions().map_err(err)?),
        }
    }

    #[pyo3(signature = (kind, identity, as_of_tt = OPEN_END))]
    fn believed(
        &self,
        py: Python<'_>,
        kind: &str,
        identity: &str,
        as_of_tt: i64,
    ) -> Res<Py<PyDict>> {
        match kind_of(kind)? {
            RowKind::Edge => edges_to_py(
                py,
                self.inner
                    .believed_edge_versions(identity, as_of_tt)
                    .map_err(err)?,
            ),
            RowKind::Node => nodes_to_py(
                py,
                self.inner
                    .believed_node_versions(identity, as_of_tt)
                    .map_err(err)?,
            ),
        }
    }

    #[pyo3(signature = (uids, as_of_tt = OPEN_END))]
    fn believed_any(&self, uids: Vec<String>, as_of_tt: i64) -> Res<Vec<String>> {
        let set = self
            .inner
            .nodes_with_believed_versions(&uids, as_of_tt)
            .map_err(err)?;
        let mut out: Vec<String> = set.into_iter().collect();
        out.sort();
        Ok(out)
    }

    /// Entity resolution (O12). Returns `(uid, score, label, props)` for the
    /// latest believed version of each match, already ordered by
    /// `(score, uid)`. The caller builds the rows, because the output `name`
    /// comes from `props` and may be a non-string the typed column does not
    /// index.
    #[pyo3(signature = (query, as_of_tt = OPEN_END))]
    fn resolve_entities(
        &self,
        query: &str,
        as_of_tt: i64,
    ) -> Res<Vec<(String, u8, String, String)>> {
        self.inner.resolve_entities(query, as_of_tt).map_err(err)
    }

    fn props_for_vids(&self, kind: &str, vids: Vec<String>) -> Res<HashMap<String, String>> {
        self.inner
            .props_for_vids(kind_of(kind)?, &vids)
            .map_err(err)
    }

    /// Windowed columnar scan — the workhorse `edges_columnar` serves.
    #[pyo3(signature = (as_of_tt = OPEN_END, vt_min = None, vt_max = None,
                        rel_types = None, touching_ids = None, touching_both = false,
                        limit = None, columns = None))]
    #[allow(clippy::too_many_arguments)]
    fn scan_edges<'py>(
        &self,
        py: Python<'py>,
        as_of_tt: i64,
        vt_min: Option<i64>,
        vt_max: Option<i64>,
        rel_types: Option<Vec<String>>,
        touching_ids: Option<Vec<i64>>,
        touching_both: bool,
        limit: Option<usize>,
        columns: Option<Vec<String>>,
    ) -> Res<Bound<'py, PyDict>> {
        // a current-only store must refuse a past-belief scan rather than
        // silently answer it from the surviving rows
        self.inner.assert_full_belief(as_of_tt).map_err(err)?;
        let m = self.inner.manifest();
        let mut files = Vec::new();
        for (lane, entries) in [
            (
                tgms_engine_core::row::Lane::Event,
                &m.edge_lanes.event,
            ),
            (
                tgms_engine_core::row::Lane::Interval,
                &m.edge_lanes.interval,
            ),
        ] {
            for e in entries {
                files.push((lane, e.file.clone()));
            }
        }
        let mut segs = Vec::with_capacity(files.len());
        for (lane, file) in &files {
            // via the store so checksums are walked on first touch
            segs.push((
                self.inner.open_segment(file).map_err(err)?,
                *lane,
                segment_id_of(file),
            ));
        }
        let targets: Vec<ScanTarget<'_, MmapSource>> = segs
            .iter()
            .map(|(segment, lane, id)| ScanTarget {
                segment,
                lane: *lane,
                id: *id,
            })
            .collect();
        let set = ScanSet::new(targets).with_closes(self.inner.close_index().map_err(err)?);

        let req = ScanRequest {
            as_of_tt,
            vt_min,
            vt_max,
            rel_types,
            touching_ids: touching_ids.map(|v| {
                let mut ids: Vec<u32> = v.into_iter().map(|i| i as u32).collect();
                ids.sort_unstable();
                ids.dedup();
                ids
            }),
            touching_both,
            limit,
            // eid is derived from rel_type and disc, so asking for it implies
            // materializing those two even when the caller does not want them
            // back. The adapter drops the extras on the way out.
            columns: columns.map(|mut c| {
                if c.iter().any(|x| x == "eid") {
                    for dep in ["rel_type", "disc"] {
                        if !c.iter().any(|x| x == dep) {
                            c.push(dep.to_string());
                        }
                    }
                }
                c
            }),
        };
        let (cols, stats) = set.materialize_edges(&req).map_err(err)?;

        // eid is derived, never stored (D-028 #2). This layer owns the
        // dictionary, so it is the one place that can turn dense ids back
        // into the uids the identity hash is defined over. Done before any
        // column is moved into NumPy.
        let want_eid = req
            .columns
            .as_ref()
            .map(|c| c.iter().any(|x| x == "eid"))
            .unwrap_or(true);
        let n_ids = if want_eid { cols.len() } else { 0 };
        let mut eids = Vec::with_capacity(n_ids);
        for i in 0..n_ids {
            let src = self.inner.dict().uid(cols.src_id[i]).ok_or_else(|| {
                PyKeyError::new_err(format!("dense id {} vanished", cols.src_id[i]))
            })?;
            let dst = self.inner.dict().uid(cols.dst_id[i]).ok_or_else(|| {
                PyKeyError::new_err(format!("dense id {} vanished", cols.dst_id[i]))
            })?;
            // never index blindly across the boundary: a projection bug
            // should surface as an error, not a panic inside Python
            let (rel, disc) = match (cols.rel_type.get(i), cols.disc.get(i)) {
                (Some(r), Some(d)) => (r, d),
                _ => {
                    return Err(PyRuntimeError::new_err(
                        "scan_edges: eid requested without rel_type/disc materialized",
                    ))
                }
            };
            eids.push(tgms_engine_core::derive::edge_eid(src, dst, rel, disc).to_hex());
        }

        let d = PyDict::new(py);
        if want_eid {
            d.set_item("eid", PyList::new(py, &eids)?)?;
        }
        d.set_item("vt_s", cols.vt_s.into_pyarray(py))?;
        d.set_item("vt_e", cols.vt_e.into_pyarray(py))?;
        d.set_item(
            "src_id",
            cols.src_id
                .iter()
                .map(|&v| v as i64)
                .collect::<Vec<_>>()
                .into_pyarray(py),
        )?;
        d.set_item(
            "dst_id",
            cols.dst_id
                .iter()
                .map(|&v| v as i64)
                .collect::<Vec<_>>()
                .into_pyarray(py),
        )?;
        // Only pay for hex when the caller asked for it — decided from the
        // request, like eid above, never from the result. The previous test
        // (`!cols.vid.is_empty()`) conflated "not requested" with "zero rows
        // matched", so a projected scan over an empty window returned a dict
        // with no vid key and the adapter raised KeyError.
        let want_vid = req
            .columns
            .as_ref()
            .map(|c| c.iter().any(|x| x == "vid"))
            .unwrap_or(true);
        if want_vid {
            d.set_item("vid", PyList::new(py, cols.vid.iter().map(|v| v.to_hex()))?)?;
        }
        d.set_item("rel_type", PyList::new(py, &cols.rel_type)?)?;
        d.set_item("disc", PyList::new(py, &cols.disc)?)?;
        d.set_item("props", PyList::new(py, &cols.props)?)?;
        // pruning counters, so effectiveness is observable rather than assumed
        d.set_item("segments_total", stats.segments_total)?;
        d.set_item("segments_pruned", stats.segments_pruned)?;
        d.set_item("rows_examined", stats.rows_examined)?;
        Ok(d)
    }

    // --- maintenance ------------------------------------------------------ //

    /// Node versions overlapping a window, sorted by `(vt_s, vid)`.
    ///
    /// Nodes are few and identity-clustered (D-028 #15), so this filters the
    /// full listing rather than driving the segment cursor — the cursor's
    /// pruning machinery buys nothing at |V| scale.
    #[pyo3(signature = (as_of_tt = OPEN_END, vt_min = None, vt_max = None))]
    fn scan_nodes<'py>(
        &self,
        py: Python<'py>,
        as_of_tt: i64,
        vt_min: Option<i64>,
        vt_max: Option<i64>,
    ) -> Res<Bound<'py, PyDict>> {
        self.inner.assert_full_belief(as_of_tt).map_err(err)?;
        let mut rows: Vec<NodeVersionOut> = self
            .inner
            .all_node_versions()
            .map_err(err)?
            .into_iter()
            .filter(|r| tgms_engine_core::believed_at(r.tt_s, r.tt_e, as_of_tt))
            .filter(|r| vt_min.is_none_or(|t| r.vt_e > t))
            .filter(|r| vt_max.is_none_or(|t| r.vt_s < t))
            .collect();
        rows.sort_by(|a, b| (a.vt_s, &a.vid).cmp(&(b.vt_s, &b.vid)));

        let d = PyDict::new(py);
        d.set_item(
            "uid_id",
            rows.iter()
                .map(|r| self.inner.dict().dense_id(&r.uid).unwrap_or(0) as i64)
                .collect::<Vec<_>>()
                .into_pyarray(py),
        )?;
        d.set_item(
            "vt_s",
            rows.iter().map(|r| r.vt_s).collect::<Vec<_>>().into_pyarray(py),
        )?;
        d.set_item(
            "vt_e",
            rows.iter().map(|r| r.vt_e).collect::<Vec<_>>().into_pyarray(py),
        )?;
        d.set_item("uid", PyList::new(py, rows.iter().map(|r| r.uid.clone()))?)?;
        d.set_item("vid", PyList::new(py, rows.iter().map(|r| r.vid.clone()))?)?;
        d.set_item("label", PyList::new(py, rows.iter().map(|r| r.label.clone()))?)?;
        Ok(d)
    }

    /// Statistics for the cost guardrails, computed exactly.
    ///
    /// Counts cover *all* stored rows, not just believed ones, matching what
    /// the DuckDB adapter reports — the two backends have to agree here or
    /// `estimate_cost` would diverge between them. This walks the store; the
    /// incremental maintenance that makes it O(1) rides with the WP-N4
    /// indexes.
    fn stats(&self, py: Python<'_>) -> Res<Py<PyDict>> {
        let acc = self.inner.stats_accum().map_err(err)?;
        let d = PyDict::new(py);
        d.set_item("n_entities", self.inner.dict().len())?;
        d.set_item("n_node_versions", acc.n_node_versions)?;
        d.set_item("n_edge_versions", acc.n_edge_versions)?;
        d.set_item("vt_min", acc.vt_min)?;
        d.set_item("vt_max", acc.vt_max)?;
        d.set_item("rel_type_counts", &acc.rel_type_counts)?;
        d.set_item("max_out_degree", acc.max_out_degree())?;
        d.set_item("generation", self.inner.generation())?;
        Ok(d.into())
    }

    /// Walk every referenced file, checking magic, checksums, completion
    /// markers, and cross-references. Returns a report rather than raising,
    /// so an operator sees every problem at once.
    fn verify(&self, py: Python<'_>) -> Res<Py<PyDict>> {
        let r = self.inner.verify().map_err(err)?;
        let d = PyDict::new(py);
        d.set_item("generation", r.generation)?;
        d.set_item("segments_checked", r.segments_checked)?;
        d.set_item("close_runs_checked", r.close_runs_checked)?;
        d.set_item("rows", r.rows)?;
        d.set_item("closes", r.closes)?;
        d.set_item("dict_records", r.dict_records)?;
        d.set_item("problems", PyList::new(py, &r.problems)?)?;
        d.set_item("healthy", r.is_healthy())?;
        Ok(d.into())
    }

    fn needs_compaction(&self) -> bool {
        self.inner.needs_compaction()
    }

    fn compact(&mut self, py: Python<'_>) -> Res<Py<PyDict>> {
        let r = self.inner.compact().map_err(err)?;
        let d = PyDict::new(py);
        d.set_item("segments_before", r.segments_before)?;
        d.set_item("segments_after", r.segments_after)?;
        d.set_item("closes_folded", r.closes_folded)?;
        d.set_item("edge_rows", r.edge_rows)?;
        d.set_item("node_rows", r.node_rows)?;
        Ok(d.into())
    }

    /// The §13 stripped configuration: rewrite the store keeping only the
    /// currently believed rows and stamp it `CURRENT_ONLY`. Experimental —
    /// the store refuses past-belief queries and corrections afterwards.
    /// `closes_folded` reports the superseded versions *dropped*.
    fn compact_current_only(&mut self, py: Python<'_>) -> Res<Py<PyDict>> {
        let r = self.inner.compact_current_only().map_err(err)?;
        let d = PyDict::new(py);
        d.set_item("segments_before", r.segments_before)?;
        d.set_item("segments_after", r.segments_after)?;
        d.set_item("versions_dropped", r.closes_folded)?;
        d.set_item("edge_rows", r.edge_rows)?;
        d.set_item("node_rows", r.node_rows)?;
        Ok(d.into())
    }

    /// Whether this store is the stripped current-only configuration.
    fn current_only(&self) -> bool {
        self.inner.current_only()
    }

    /// Collect superseded generations: manifests older than the retention
    /// window, then any segment or close-run file no retained manifest
    /// references (superseded by compaction, or orphaned by a crash).
    #[pyo3(signature = (keep_last = tgms_engine_core::defaults::GC_KEEP_GENERATIONS))]
    fn gc(&mut self, py: Python<'_>, keep_last: u64) -> Res<Py<PyDict>> {
        let r = self.inner.gc(keep_last).map_err(err)?;
        let d = PyDict::new(py);
        d.set_item("manifests_removed", r.manifests_removed)?;
        d.set_item("segments_removed", r.segments_removed)?;
        d.set_item("close_runs_removed", r.close_runs_removed)?;
        d.set_item("bytes_reclaimed", r.bytes_reclaimed)?;
        d.set_item("generations_retained", r.generations_retained)?;
        Ok(d.into())
    }
}

/// δ-temporal motif matching (O6/O7).
///
/// Takes the window's events as columns and returns `(count, instances)`,
/// where each instance is a triple of row indices into those columns — the
/// caller already holds the strings, so there is no reason to send them back
/// across the boundary. `collect=False` counts without materializing, which
/// is all O6 needs.
#[pyfunction]
#[pyo3(signature = (motif, src, dst, t, eid, delta, collect = false))]
#[allow(clippy::too_many_arguments)]
fn motif_match(
    py: Python<'_>,
    motif: &str,
    src: PyReadonlyArray1<'_, i64>,
    dst: PyReadonlyArray1<'_, i64>,
    t: PyReadonlyArray1<'_, i64>,
    eid: Vec<String>,
    delta: i64,
    collect: bool,
) -> Res<(u64, Vec<[u32; 3]>)> {
    let kind = tgms_engine_core::motif::Motif::parse(motif).map_err(err)?;
    // The three integer columns are borrowed from NumPy rather than copied
    // into Vecs. Building those Vecs cost 4.4 ms of an 11 ms call at 14.5k
    // events — first in Python materializing lists, then again in PyO3
    // converting them element by element. `eid` still has to be copied: it is
    // an object array of Python strings, and there is no buffer to borrow.
    let (src, dst, t) = (src.as_slice()?, dst.as_slice()?, t.as_slice()?);
    // pure Rust over borrowed data: nothing here touches Python
    py.detach(|| {
        let events = tgms_engine_core::motif::Events {
            src,
            dst,
            t,
            eid: &eid,
        };
        tgms_engine_core::motif::match_motifs(kind, &events, delta, collect).map_err(err)
    })
}

/// Interval-join pair enumeration (O11 `co_active`).
///
/// Takes the per-row candidate ranges the caller already computed by binary
/// search and walks them. Returns `(a_row, b_row)` pairs in input order,
/// which is the order the operator contract promises.
#[pyfunction]
fn interval_pairs(
    py: Python<'_>,
    lo: Vec<u32>,
    hi: Vec<u32>,
    a_vt_e: Vec<i64>,
    b_vt_e: Vec<i64>,
    require_b_end_after_a_end: bool,
) -> Res<Vec<(u32, u32)>> {
    py.detach(|| {
        tgms_engine_core::interval::interval_pairs(
            &lo,
            &hi,
            &a_vt_e,
            &b_vt_e,
            require_b_end_after_a_end,
        )
        .map_err(err)
    })
}

/// Round-trip probe (WP-N0 acceptance): proves the extension is importable,
/// the core crate is linked, and arrays cross into NumPy without a copy.
#[pyfunction]
#[pyo3(signature = (n = 8))]
fn ping(py: Python<'_>, n: usize) -> Bound<'_, PyArray1<i64>> {
    let v: Vec<i64> = (0..n as i64).collect();
    v.into_pyarray(py)
}

/// Version of the Rust core, for determinism receipts (spec §1.4).
#[pyfunction]
fn core_version() -> &'static str {
    tgms_engine_core::VERSION
}

/// Engine constants are exported so the Python side can *assert* agreement
/// with `tgms.core.model` rather than trusting two copies of one number.
#[pymodule]
fn _engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeStore>()?;
    m.add_function(wrap_pyfunction!(interval_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(motif_match, m)?)?;
    m.add_function(wrap_pyfunction!(ping, m)?)?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add("FORMAT_VERSION", tgms_engine_core::FORMAT_VERSION)?;
    m.add("OPEN_END", tgms_engine_core::OPEN_END)?;
    m.add("BLOCK_ROWS", tgms_engine_core::defaults::BLOCK_ROWS)?;
    m.add(
        "SEGMENT_TARGET_BYTES",
        tgms_engine_core::defaults::SEGMENT_TARGET_BYTES,
    )?;
    Ok(())
}
