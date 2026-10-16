//! Keep launcher refusals visible in the existing startup failure surface.
use std::{fs::File, io::{Read, Seek, SeekFrom}, path::Path};

pub fn timeout_message(log_path: &Path) -> String {
    let fallback = "the CORSIA service did not become ready in time";
    let tail = (|| -> Option<String> {
        let mut file = File::open(log_path).ok()?;
        let size = file.metadata().ok()?.len();
        file.seek(SeekFrom::Start(size.saturating_sub(65_536))).ok()?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes).ok()?;
        Some(String::from_utf8_lossy(&bytes).into_owned())
    })().unwrap_or_default();
    tail.lines().rev().find(|line| line.contains("CORSIA bundled backend refused to start:"))
        .map_or_else(|| fallback.into(), |line| format!("{fallback}: {line}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_timeout_displays_the_last_launcher_refusal() {
        let root = tempfile::tempdir().unwrap();
        let log = root.path().join("service.log");
        std::fs::write(&log, "CORSIA bundled backend refused to start: old\nnoise\nCORSIA bundled backend refused to start: foreign CORSIA service com.corsia.prod owns this Mac\nmore noise\n").unwrap();
        let error = timeout_message(&log);
        assert!(error.contains("foreign CORSIA service com.corsia.prod owns this Mac"));
        assert!(!error.contains("old"));
        assert_eq!(timeout_message(&root.path().join("missing")), "the CORSIA service did not become ready in time");
    }
}
