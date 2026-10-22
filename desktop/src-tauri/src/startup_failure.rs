//! Why a starting service never answered, with launcher refusals kept visible.
use std::{fmt, fs::File, io::{Read, Seek, SeekFrom}, path::Path};

/// Each way a startup wait ends without health. Callers branch on the variant,
/// never on the wording.
#[derive(Debug, PartialEq)]
pub enum StartupFailure {
    /// launchd reported a different PID: the service restarted while starting.
    Restarted,
    /// The PID launchd reported is gone and launchd started no other, or
    /// launchd showed no process at all. A launcher refusal exits 0, which
    /// `KeepAlive` does not restart.
    Exited { refusal: Option<String> },
    /// The cap passed without health.
    TimedOut { refusal: Option<String> },
    /// Asking launchd for the service's PID failed.
    PidUnreadable(String),
}

impl StartupFailure {
    pub fn exited(log_path: Option<&Path>) -> Self {
        Self::Exited { refusal: log_path.and_then(refusal_line) }
    }

    pub fn timed_out(log_path: Option<&Path>) -> Self {
        Self::TimedOut { refusal: log_path.and_then(refusal_line) }
    }
}

impl fmt::Display for StartupFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (summary, refusal) = match self {
            Self::Restarted => ("the CORSIA service restarted while it was starting", &None),
            Self::Exited { refusal } => ("the CORSIA service stopped while it was starting", refusal),
            Self::TimedOut { refusal } => ("the CORSIA service did not become ready in time", refusal),
            Self::PidUnreadable(error) => return f.write_str(error),
        };
        match refusal {
            Some(line) => write!(f, "{summary}: {line}"),
            None => f.write_str(summary),
        }
    }
}

/// The log's last line when it is a launcher refusal. A refusal is the
/// launcher's final write before it exits, so anything a later launch wrote
/// after an older refusal means the refusal is not this launch's cause.
fn refusal_line(log_path: &Path) -> Option<String> {
    let mut file = File::open(log_path).ok()?;
    let size = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(size.saturating_sub(4_096))).ok()?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).ok()?;
    String::from_utf8_lossy(&bytes)
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .filter(|line| line.contains("CORSIA bundled backend refused to start:"))
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_failures_display_a_refusal_only_when_it_ends_the_log() {
        let root = tempfile::tempdir().unwrap();
        let log = root.path().join("service.log");
        std::fs::write(&log, "CORSIA bundled backend refused to start: old\nnoise\nCORSIA bundled backend refused to start: foreign CORSIA service com.corsia.prod owns this Mac\n\n").unwrap();
        for error in [StartupFailure::timed_out(Some(&log)), StartupFailure::exited(Some(&log))] {
            let error = error.to_string();
            assert!(error.contains("foreign CORSIA service com.corsia.prod owns this Mac"));
            assert!(!error.contains("old"));
        }
        std::fs::write(&log, "CORSIA bundled backend refused to start: stale\nmigration failed: disk full\n").unwrap();
        assert_eq!(
            StartupFailure::exited(Some(&log)).to_string(),
            "the CORSIA service stopped while it was starting"
        );
        assert_eq!(
            StartupFailure::timed_out(Some(&root.path().join("missing"))).to_string(),
            "the CORSIA service did not become ready in time"
        );
        assert_eq!(StartupFailure::exited(None), StartupFailure::Exited { refusal: None });
    }
}
