//! Engine error taxonomy (implementation spec §5.7).
//!
//! Every error carries enough context to name the offending file and
//! position: corruption must be diagnosable without a debugger, because the
//! prescribed remedy ("restore the previous generation, or `tgms replay`")
//! depends on knowing what is broken.

use std::fmt;
use std::path::PathBuf;

/// Error categories. The PyO3 layer maps these onto the existing
/// `tgms.core.errors` taxonomy, so the mapping must stay total.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    /// On-disk state failed a checksum, magic, or structural check.
    Corrupt,
    /// A requested identity, uid, or version does not exist.
    NotFound,
    /// A hard format limit was reached (e.g. > u32::MAX entities).
    Capacity,
    /// Underlying I/O failure.
    Io,
    /// An engine invariant was violated — always a bug in the engine.
    Invariant,
}

impl Category {
    pub const fn as_str(self) -> &'static str {
        match self {
            Category::Corrupt => "corrupt",
            Category::NotFound => "not_found",
            Category::Capacity => "capacity",
            Category::Io => "io",
            Category::Invariant => "invariant",
        }
    }
}

/// Where an error occurred. `None` fields are simply unknown at the raise
/// site; they are never filled with placeholders.
#[derive(Debug, Clone, Default)]
pub struct Location {
    pub file: Option<PathBuf>,
    pub offset: Option<u64>,
    pub row: Option<u32>,
}

impl fmt::Display for Location {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut parts = Vec::new();
        if let Some(p) = &self.file {
            parts.push(format!("file={}", p.display()));
        }
        if let Some(o) = self.offset {
            parts.push(format!("offset={o}"));
        }
        if let Some(r) = self.row {
            parts.push(format!("row={r}"));
        }
        if parts.is_empty() {
            Ok(())
        } else {
            write!(f, " [{}]", parts.join(" "))
        }
    }
}

#[derive(Debug, Clone)]
pub struct EngineError {
    pub category: Category,
    pub message: String,
    pub location: Location,
    /// Actionable remedy, surfaced verbatim to the user. Corruption errors
    /// must always carry one.
    pub remedy: Option<String>,
    /// The originating `io::Error`'s raw OS error code, when this was built
    /// `From<std::io::Error>`. `message` already renders as prose
    /// (`Display`, above) and that text must not change — other layers key
    /// off it — so this rides alongside purely for the pyo3 boundary to
    /// attach `.errno` to the raised `OSError` (D-088's reopen-on-ENOENT net
    /// needs to tell "a pinned reader's file vanished under gc" apart from
    /// any other I/O failure without parsing the message).
    pub raw_os_error: Option<i32>,
}

impl EngineError {
    pub fn new(category: Category, message: impl Into<String>) -> Self {
        Self {
            category,
            message: message.into(),
            location: Location::default(),
            remedy: None,
            raw_os_error: None,
        }
    }

    pub fn corrupt(message: impl Into<String>) -> Self {
        Self::new(Category::Corrupt, message).with_remedy(
            "restore the previous generation, or rebuild with `tgms replay` \
             from the event log",
        )
    }

    /// A refusal of a file written by a newer build (a manifest format or a
    /// segment header this build does not read). The category is `Corrupt`,
    /// as before, but the remedy is `segment::NEWER_WRITER_REMEDY` alone:
    /// restoring the previous generation does not help with a store a newer
    /// build wrote. `tgms store verify` recognises the refusal by that remedy.
    pub fn newer_writer(message: impl Into<String>) -> Self {
        Self::new(Category::Corrupt, message).with_remedy(crate::segment::NEWER_WRITER_REMEDY)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(Category::NotFound, message)
    }

    pub fn capacity(message: impl Into<String>) -> Self {
        Self::new(Category::Capacity, message)
    }

    pub fn invariant(message: impl Into<String>) -> Self {
        Self::new(Category::Invariant, message)
            .with_remedy("this is an engine bug; please report it with the store manifest")
    }

    pub fn at_file(mut self, path: impl Into<PathBuf>) -> Self {
        self.location.file = Some(path.into());
        self
    }

    pub fn at_offset(mut self, offset: u64) -> Self {
        self.location.offset = Some(offset);
        self
    }

    pub fn at_row(mut self, row: u32) -> Self {
        self.location.row = Some(row);
        self
    }

    pub fn with_remedy(mut self, remedy: impl Into<String>) -> Self {
        self.remedy = Some(remedy.into());
        self
    }
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}{}", self.category.as_str(), self.message, self.location)?;
        if let Some(r) = &self.remedy {
            write!(f, " — {r}")?;
        }
        Ok(())
    }
}

impl std::error::Error for EngineError {}

impl From<std::io::Error> for EngineError {
    fn from(e: std::io::Error) -> Self {
        let raw_os_error = e.raw_os_error();
        let mut err = EngineError::new(Category::Io, e.to_string());
        err.raw_os_error = raw_os_error;
        err
    }
}

pub type Result<T> = std::result::Result<T, EngineError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn corrupt_errors_always_carry_a_remedy() {
        let e = EngineError::corrupt("bad footer magic").at_file("seg/7.tgs").at_offset(4096);
        assert!(e.remedy.is_some());
        let s = e.to_string();
        assert!(s.contains("corrupt"));
        assert!(s.contains("seg/7.tgs"));
        assert!(s.contains("4096"));
    }

    #[test]
    fn a_newer_writer_refusal_names_only_its_own_remedy() {
        let e = EngineError::newer_writer("segment format 2 is not supported").at_file("seg/7.tgs");
        assert_eq!(e.category, Category::Corrupt);
        assert_eq!(e.remedy.as_deref(), Some(crate::segment::NEWER_WRITER_REMEDY));
        let s = e.to_string();
        assert!(s.ends_with(crate::segment::NEWER_WRITER_REMEDY), "{s}");
        assert!(!s.contains("restore the previous generation"), "{s}");
    }

    #[test]
    fn location_is_omitted_when_unknown() {
        let e = EngineError::not_found("unknown uid: n42");
        assert_eq!(e.to_string(), "not_found: unknown uid: n42");
    }
}
