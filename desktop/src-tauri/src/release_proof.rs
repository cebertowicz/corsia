//! Proofs that a stopped service has really let go of the install's state.
//!
//! A bootout that returns is not release: the process can linger, the port can
//! stay bound and the database can stay locked. Adoption backs up and swaps
//! state only after all four proofs hold, for the source service and, on
//! rollback, for the app's own service.

use std::{
    io::ErrorKind,
    net::TcpStream,
    path::Path,
    process::Command,
    thread,
    time::{Duration, Instant, SystemTime},
};

const PROOF_TIMEOUT: Duration = Duration::from_secs(15);
const PROOF_POLL: Duration = Duration::from_millis(100);

/// The process id launchd reports for a loaded job, or none when it has none.
pub fn launchd_pid(target: &str) -> Option<u32> {
    let output = Command::new("/bin/launchctl")
        .args(["print", target])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    pid_from_launchctl_print(&String::from_utf8_lossy(&output.stdout))
}

pub fn pid_from_launchctl_print(text: &str) -> Option<u32> {
    text.lines()
        .find_map(|line| line.trim().strip_prefix("pid = ")?.trim().parse().ok())
}

/// True once the kernel no longer knows the process (kill(pid, 0) fails with ESRCH).
pub fn pid_is_gone(pid: u32) -> bool {
    let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
    result == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}

/// True once nothing accepts connections on the loopback port.
pub fn port_refuses(port: u16) -> bool {
    matches!(
        TcpStream::connect_timeout(&std::net::SocketAddr::from(([127, 0, 0, 1], port)), Duration::from_millis(250)),
        Err(error) if error.kind() == ErrorKind::ConnectionRefused
    )
}

/// Open the database exclusively and truncate its WAL; fails while any process holds it.
pub fn database_released(python: &Path, database: &Path) -> Result<(), String> {
    let program = "import sqlite3, sys; c = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None); c.execute('BEGIN EXCLUSIVE'); c.execute('ROLLBACK'); busy, _, _ = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); sys.exit(1 if busy else 0)";
    let status = Command::new(python)
        .args(["-I", "-c", program])
        .arg(database)
        .status()
        .map_err(|error| format!("could not check the database lock: {error}"))?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| "the database is still held open".into())
}

fn wait_until(mut done: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + PROOF_TIMEOUT;
    loop {
        if done() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(PROOF_POLL);
    }
}

/// Run the four proofs in order; the first one that does not hold names itself.
pub fn prove_released(
    pid: Option<u32>,
    port: u16,
    python: &Path,
    database: &Path,
) -> Result<(), String> {
    if let Some(pid) = pid {
        if !wait_until(|| pid_is_gone(pid)) {
            return Err(format!("process {pid} is still running after bootout"));
        }
    }
    if !wait_until(|| port_refuses(port)) {
        return Err(format!(
            "port {port} still accepts connections after bootout"
        ));
    }
    let mut last = String::new();
    if wait_until(|| match database_released(python, database) {
        Ok(()) => true,
        Err(error) => {
            last = error;
            false
        }
    }) {
        Ok(())
    } else {
        Err(last)
    }
}

/// Whole seconds a process has been running, from `ps -o etime=` ([[dd-]hh:]mm:ss).
pub fn process_age_seconds(pid: u32) -> Option<u64> {
    let output = Command::new("/bin/ps")
        .args(["-o", "etime=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    parse_etime(String::from_utf8_lossy(&output.stdout).trim())
}

pub fn parse_etime(text: &str) -> Option<u64> {
    let (days, clock) = match text.split_once('-') {
        Some((days, clock)) => (days.parse::<u64>().ok()?, clock),
        None => (0, text),
    };
    let parts = clock
        .split(':')
        .map(|part| part.parse::<u64>().ok())
        .collect::<Option<Vec<_>>>()?;
    let seconds = match parts.as_slice() {
        [m, s] => m * 60 + s,
        [h, m, s] => h * 3600 + m * 60 + s,
        _ => return None,
    };
    Some(days * 86_400 + seconds)
}

/// Proof that the process behind a launchd job started after `since`, so a
/// survivor of the old job cannot pass for the replacement.
pub fn replaced_since(target: &str, since: SystemTime) -> Result<(), String> {
    let elapsed = SystemTime::now()
        .duration_since(since)
        .unwrap_or_default()
        .as_secs()
        + 1;
    let pid = launchd_pid(target).ok_or("the restarted service has no running process")?;
    let age =
        process_age_seconds(pid).ok_or("could not read the restarted process's start time")?;
    if started_after(age, elapsed) {
        Ok(())
    } else {
        Err(format!(
            "{target} is still the process that started before the restart"
        ))
    }
}

/// A process that has run no longer than the time since the restart began started after it.
pub fn started_after(age_seconds: u64, seconds_since_restart: u64) -> bool {
    age_seconds <= seconds_since_restart
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn reads_the_pid_launchd_reports() {
        assert_eq!(
            pid_from_launchctl_print("gui/501/x = {\n\tstate = running\n\tpid = 4242\n}"),
            Some(4242)
        );
        assert_eq!(
            pid_from_launchctl_print("gui/501/x = {\n\tstate = waiting\n}"),
            None
        );
    }

    #[test]
    fn a_reaped_child_is_gone_and_the_live_test_process_is_not() {
        let mut child = Command::new("/usr/bin/true").spawn().unwrap();
        let pid = child.id();
        child.wait().unwrap();
        assert!(pid_is_gone(pid));
        assert!(!pid_is_gone(std::process::id()));
    }

    #[test]
    fn a_bound_port_does_not_refuse_and_a_closed_one_does() {
        // A port below the kernel's ephemeral range, so another test binding port 0 cannot be
        // handed this number between the drop and the check.
        let (listener, port) = (39_000..40_000)
            .find_map(|port| {
                TcpListener::bind(("127.0.0.1", port))
                    .ok()
                    .map(|l| (l, port))
            })
            .expect("a free test port");
        assert!(!port_refuses(port));
        drop(listener);
        assert!(port_refuses(port));
    }

    #[test]
    fn parses_ps_elapsed_time() {
        assert_eq!(parse_etime("00:07"), Some(7));
        assert_eq!(parse_etime("12:07"), Some(727));
        assert_eq!(parse_etime("01:02:03"), Some(3723));
        assert_eq!(parse_etime("2-00:00:01"), Some(172_801));
        assert_eq!(parse_etime("nonsense"), None);
    }

    #[test]
    fn a_survivor_older_than_the_restart_does_not_count_as_the_replacement() {
        assert!(started_after(3, 10));
        assert!(started_after(10, 10));
        assert!(!started_after(11, 10));
    }

    #[test]
    fn a_held_database_is_not_released_and_a_free_one_is() {
        let root = tempfile::Builder::new()
            .prefix("corsia-release-proof-")
            .tempdir_in("/private/tmp")
            .unwrap();
        let database = root.path().join("corsia.db");
        let holder = Command::new("/usr/bin/python3")
            .args(["-I", "-c", "import sqlite3, sys, time; c = sqlite3.connect(sys.argv[1], isolation_level=None); c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE t (x)'); c.execute('BEGIN EXCLUSIVE'); print('held', flush=True); time.sleep(30)"])
            .arg(&database).stdout(std::process::Stdio::piped()).spawn();
        let Ok(mut holder) = holder else { return };
        let mut line = String::new();
        std::io::BufRead::read_line(
            &mut std::io::BufReader::new(holder.stdout.take().unwrap()),
            &mut line,
        )
        .unwrap();
        assert_eq!(line.trim(), "held");
        assert!(database_released(Path::new("/usr/bin/python3"), &database).is_err());
        holder.kill().unwrap();
        holder.wait().unwrap();
        assert!(database_released(Path::new("/usr/bin/python3"), &database).is_ok());
    }
}
