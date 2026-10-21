//! The production side of in-place adoption: the source install's own
//! `scripts/corsia` stops and starts its service, launchd and the loopback
//! port prove what it did, and the app's bundled backend and Python make the
//! backup. Nothing here reads the CLI's output to decide anything.

use std::{
    collections::BTreeSet,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use crate::{
    adoption::{self, ReadyError, RestartLook, StopProcess},
    probe_health, release_proof, run_service_command,
    service_management::service_label,
    unregister_service, wait_for_service, HEALTH_POLL_INTERVAL,
};

/// The source's own drain wait, in seconds: its cap, and the longest a switch waits for running work.
const DRAIN_MAX_WAIT_SECONDS: u64 = 1800;
/// A self-restart request is stuck only once it is older than the longest drain
/// plus a margin for the teardown and boot after it.
const STUCK_AFTER: Duration = Duration::from_secs(DRAIN_MAX_WAIT_SECONDS + 300);
/// The variables of the app's own environment the CLI is given.
const INHERITED: [&str; 5] = ["HOME", "PATH", "USER", "LANG", "TMPDIR"];
const COUNT_INTERVAL: Duration = Duration::from_secs(1);
const PROOF_TIMEOUT: Duration = Duration::from_secs(15);
/// The verified production startup took 99 seconds on a 22 GB state root; two
/// quiet minutes identifies a wedged start without masking an active migration.
const START_STALL_WINDOW: Duration = Duration::from_secs(120);
/// A startup with continuous backend progress may run for a long migration, but
/// never indefinitely.
const START_ABSOLUTE_CAP: Duration = Duration::from_secs(1_800);

/// What the source backend's drain counts as running (`turn_core.active_turn_count`).
/// Read only, over `mode=ro` and never immutable: the database is live.
const COUNT_PROGRAM: &str = "import sqlite3, sys; from pathlib import Path; c = sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True, timeout=1); print(c.execute(\"SELECT COUNT(*) FROM active_turns WHERE status='running'\").fetchone()[0])";
const PREFLIGHT_PROGRAM: &str = "import sys; sys.path.insert(0, sys.argv[1]); from infra.adoption_snapshot import preflight_main; sys.exit(preflight_main(sys.argv[2:]))";
const RESTART_PROGRAM: &str = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from infra.adoption_snapshot import restart_in_flight_age; age = restart_in_flight_age(Path(sys.argv[2])); print('none' if age is None else age)";
const SNAPSHOT_PROGRAM: &str = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from infra.adoption_snapshot import create_adoption_snapshot; print(create_adoption_snapshot(Path(sys.argv[2])))";
const RESTORE_PROGRAM: &str = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from infra.adoption_snapshot import restore_adoption_snapshot; restore_adoption_snapshot(Path(sys.argv[2]), Path(sys.argv[3]))";

/// The app's bundled backend and the Python that runs it.
#[derive(Clone, Debug)]
pub struct Bundled {
    pub python: PathBuf,
    pub backend: PathBuf,
    pub packages: PathBuf,
}

impl Bundled {
    pub fn in_resources(resources: &Path) -> Self {
        Self {
            python: resources.join("python/bin/python3"),
            backend: resources.join("backend"),
            packages: resources.join("python/backend-packages"),
        }
    }
}

pub struct SourceLifecycle {
    source: adoption::SourceInstall,
    bundled: Bundled,
    launchctl: PathBuf,
    env: Vec<(&'static str, OsString)>,
    proof_timeout: Duration,
    count_interval: Duration,
    start_stall_window: Duration,
    start_absolute_cap: Duration,
    /// Reads a process's start time; replaced in tests.
    stop_clock: fn(u32) -> Option<String>,
}

impl SourceLifecycle {
    pub fn new(source: adoption::SourceInstall, bundled: Bundled) -> Self {
        Self {
            source,
            bundled,
            launchctl: PathBuf::from("/bin/launchctl"),
            env: INHERITED
                .iter()
                .filter_map(|name| std::env::var_os(name).map(|value| (*name, value)))
                .collect(),
            proof_timeout: PROOF_TIMEOUT,
            count_interval: COUNT_INTERVAL,
            start_stall_window: START_STALL_WINDOW,
            start_absolute_cap: START_ABSOLUTE_CAP,
            stop_clock: process_started,
        }
    }

    fn target() -> String {
        format!(
            "gui/{}/{}",
            unsafe { libc::geteuid() },
            adoption::SOURCE_LABEL
        )
    }

    /// The checkout's CLI, run in the checkout with nothing but the basics.
    fn cli(&self, verb: &str) -> Command {
        let mut command = Command::new(&self.source.cli);
        command
            .arg(verb)
            .env_clear()
            .envs(self.env.iter().map(|(name, value)| (name, value)))
            .env("CORSIA_DRAIN_MAX_WAIT", DRAIN_MAX_WAIT_SECONDS.to_string())
            .current_dir(
                self.source
                    .cli
                    .parent()
                    .and_then(Path::parent)
                    .unwrap_or(Path::new("/")),
            )
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command
    }

    /// The bundled Python, as the app's own service runs it.
    fn python(&self, program: &str) -> Command {
        let mut command = Command::new(&self.bundled.python);
        command
            .args(["-B", "-c", program])
            .env_clear()
            .envs(self.env.iter().map(|(name, value)| (name, value)))
            .env("CORSIA_WORKSPACE", &self.source.workspace)
            .env("CORSIA_STATE_DIR", &self.source.state)
            .env("PYTHONPATH", &self.bundled.packages)
            .env("PYTHONNOUSERSITE", "1")
            .current_dir(&self.bundled.backend);
        command
    }

    /// launchd's own account of the source job: `Some(print output)` while it is loaded.
    fn loaded_job(&self) -> Result<Option<String>, String> {
        let output = Command::new(&self.launchctl)
            .args(["print", &Self::target()])
            .output()
            .map_err(|error| {
                format!("could not ask launchd about the existing install: {error}")
            })?;
        Ok(output
            .status
            .success()
            .then(|| String::from_utf8_lossy(&output.stdout).into_owned()))
    }

    fn wait_until(&self, mut done: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + self.proof_timeout;
        while !done() {
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(Duration::from_millis(50));
        }
        true
    }

    fn is_down(&self) -> Result<(), String> {
        if self.loaded_job()?.is_some() {
            return Err("launchd still lists the existing install".into());
        }
        if !release_proof::port_refuses(self.source.port) {
            return Err(format!(
                "port {} still accepts connections",
                self.source.port
            ));
        }
        Ok(())
    }

    /// Running turns, read-only; `None` when it cannot be read, which the
    /// window shows as no count and never as zero.
    fn running_turns(&self) -> Option<u32> {
        let output = self
            .python(COUNT_PROGRAM)
            .arg(self.source.state.join("corsia.db"))
            .output()
            .inspect_err(|error| log::warn!("could not count running work: {error}"))
            .ok()?;
        if !output.status.success() {
            log::warn!("could not count running work: exited {}", output.status);
            return None;
        }
        String::from_utf8_lossy(&output.stdout).trim().parse().ok()
    }

    /// A path as the person sees it: the home folder written `~`.
    fn shown(&self, path: &Path) -> String {
        let home = self
            .env
            .iter()
            .find(|(name, _)| *name == "HOME")
            .map(|(_, value)| PathBuf::from(value));
        match home.and_then(|home| path.strip_prefix(&home).ok().map(Path::to_path_buf)) {
            Some(rest) if rest.as_os_str().is_empty() => "~".into(),
            Some(rest) => format!("~/{}", rest.display()),
            None => path.display().to_string(),
        }
    }

    /// The checkout a backend folder belongs to (`<checkout>/backend`).
    fn checkout_of<'a>(&self, backend: &'a Path) -> &'a Path {
        match (backend.file_name(), backend.parent()) {
            (Some(name), Some(parent)) if name == "backend" => parent,
            _ => backend,
        }
    }

    /// `<checkout>/scripts/corsia restart`, then the source answers again. The
    /// source's own script decides what a restart means; this only proves it.
    pub fn restart_existing(&mut self) -> Result<(), String> {
        let status = self
            .cli("restart")
            .status()
            .map_err(|error| format!("could not restart the existing install: {error}"))?;
        if !status.success() {
            return Err(format!("`corsia restart` exited {status}"));
        }
        self.wait_for_source()
    }

    /// Wait for the source to become healthy while it is demonstrably alive and
    /// writing startup progress. A fixed health deadline falsely reports a
    /// large, migrating state root as stopped.
    fn wait_for_source(&self) -> Result<(), String> {
        let began = Instant::now();
        let mut last_progress = began;
        let mut marker = log_marker(&self.source.state.join("logs/service.log"));
        let mut process = None;
        loop {
            let listing = self
                .loaded_job()?
                .ok_or("the existing install unloaded while it was starting")?;
            let pid = release_proof::pid_from_launchctl_print(&listing)
                .ok_or("the existing install has no running process while it was starting")?;
            let started = process_started(pid)
                .ok_or("the existing install process exited while it was starting")?;
            if let Some((expected_pid, expected_started)) = &process {
                if *expected_pid != pid || *expected_started != started {
                    return Err("the existing install process restarted while it was starting".into());
                }
            } else {
                process = Some((pid, started));
            }
            if probe_health(self.source.port) {
                return Ok(());
            }
            let next_marker = log_marker(&self.source.state.join("logs/service.log"));
            if next_marker != marker {
                marker = next_marker;
                last_progress = Instant::now();
            }
            let now = Instant::now();
            if now.duration_since(began) >= self.start_absolute_cap {
                return Err("the existing install exceeded its startup safety cap".into());
            }
            if now.duration_since(last_progress) >= self.start_stall_window {
                return Err("the existing install stopped making startup progress".into());
            }
            thread::sleep(HEALTH_POLL_INTERVAL);
        }
    }

    fn start_cli(&self) -> Result<(), String> {
        let status = self
            .cli("start")
            .status()
            .map_err(|error| format!("could not start the existing install: {error}"))?;
        status
            .success()
            .then_some(())
            .ok_or_else(|| format!("`corsia start` exited {status}"))
    }
}

/// The backend's answer to the restart look: `none`, or the age in seconds of
/// the youngest in-flight request.
fn classify_restart(answer: &str) -> RestartLook {
    if answer == "none" {
        return RestartLook::Settled;
    }
    match answer.parse::<f64>() {
        Ok(age) if age.is_finite() && age > STUCK_AFTER.as_secs_f64() => RestartLook::Stuck,
        Ok(age) if age.is_finite() => RestartLook::Young,
        _ => {
            log::warn!("could not read the age of a restart request");
            RestartLook::Unknown
        }
    }
}

/// A log marker changes when the backend writes a new startup line, without
/// copying log contents into diagnostics where they could contain private data.
fn log_marker(path: &Path) -> Option<(std::time::SystemTime, u64)> {
    let metadata = fs::metadata(path).ok()?;
    Some((metadata.modified().ok()?, metadata.len()))
}

/// When the kernel says a process started, as `seconds.microseconds`; `None`
/// when no such process exists. A pid alone can be reused, the pair cannot.
fn process_started(pid: u32) -> Option<String> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let written = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            std::ptr::addr_of_mut!(info).cast(),
            size,
        )
    };
    (written == size).then(|| format!("{}.{:06}", info.pbi_start_tvsec, info.pbi_start_tvusec))
}

/// A fresh, private file for one `corsia stop`'s output.
fn stop_record() -> Option<(std::fs::File, PathBuf)> {
    use std::os::unix::fs::OpenOptionsExt;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_nanos());
    let path = std::env::temp_dir().join(format!(
        "corsia-adopt-stop-{}-{nanos}.log",
        std::process::id()
    ));
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .ok()
        .map(|file| (file, path))
}

/// The last line the checkout's CLI wrote, without its colour codes and marker glyph: its own
/// words for why it refused. Shown to the person, never matched.
fn refusal_line(path: &Path) -> Option<String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = std::fs::File::open(path).ok()?;
    let length = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(length.saturating_sub(4096)))
        .ok()?;
    let mut tail = Vec::new();
    file.read_to_end(&mut tail).ok()?;
    let text = String::from_utf8_lossy(&tail);
    let mut plain = String::new();
    let mut characters = text.chars().peekable();
    while let Some(character) = characters.next() {
        if character == '\u{1b}' {
            // An ANSI escape: `ESC [ ... <letter>`.
            for next in characters.by_ref() {
                if next.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            plain.push(character);
        }
    }
    let line = plain
        .lines()
        .map(|line| {
            line.trim_start_matches(|c: char| !c.is_alphanumeric())
                .trim()
        })
        .rfind(|line| !line.is_empty())?;
    Some(line.chars().take(400).collect())
}

/// The `CORSIA_SERVICE_GENERATION => <value>` line of `launchctl print`.
fn printed_generation(printed: &str) -> Option<&str> {
    printed
        .lines()
        .find_map(|line| line.trim().strip_prefix("CORSIA_SERVICE_GENERATION => "))
}

/// The `working directory = ` line of `launchctl print`, used only to name the folder in the refusal.
fn printed_working_directory(printed: &str) -> Option<&str> {
    printed
        .lines()
        .find_map(|line| line.trim().strip_prefix("working directory = "))
}

impl adoption::Lifecycle for SourceLifecycle {
    fn check_ready(
        &mut self,
        state: &Path,
        variable_names: &BTreeSet<String>,
    ) -> Result<(), ReadyError> {
        let Some(printed) = self.loaded_job().map_err(ReadyError::Failed)? else {
            // The source's own restart unloads the job and bootstraps it again, so an unloaded
            // job with a restart in flight is restarting, not stopped: wait for it (or call it
            // stuck) rather than offering a start that would race the restart's own.
            return Err(match self.look_for_restart(state) {
                RestartLook::Young | RestartLook::Stuck => ReadyError::Restarting,
                RestartLook::Settled => ReadyError::NotRunning,
                // A look that could not be read proves neither state: never claim one.
                RestartLook::Unknown => ReadyError::Failed(
                    "could not tell whether the existing install is restarting".into(),
                ),
            });
        };
        // The checkout that would stop it must be the one the loaded job was bootstrapped
        // from. This is the narrowed copy of the checkout's own rule
        // (`service_loaded_definition_matches` in scripts/corsia.d/service.sh), kept only
        // because no released checkout exposes that check read-only: the plist's generation
        // must be the one the loaded job reports, and anything else (no generation in the
        // plist, none in the print) cannot be proven, exactly as `corsia stop` refuses it.
        // Port, interpreter, workspace and state-directory mismatches are left to that stop.
        let running = printed_working_directory(&printed);
        let proven = matches!(
            (self.source.generation.as_deref(), printed_generation(&printed)),
            (Some(expected), Some(loaded)) if expected == loaded
        );
        // Both generations present and different, from the very folder the settings name: the
        // plist was rewritten without a reload, which no folder wording describes.
        let older_settings = matches!(
            (self.source.generation.as_deref(), printed_generation(&printed)),
            (Some(expected), Some(loaded)) if expected != loaded
        ) && running.map(Path::new)
            == Some(self.source.working_directory.as_path());
        if older_settings {
            return Err(ReadyError::OlderSettings);
        }
        if !proven {
            return Err(ReadyError::WrongFolder {
                running: running
                    .map(|directory| self.shown(self.checkout_of(Path::new(directory)))),
                checkout: self.shown(self.checkout_of(&self.source.working_directory)),
            });
        }
        let output = self
            .python(PREFLIGHT_PROGRAM)
            .arg(&self.bundled.backend)
            .arg(state)
            .args(variable_names)
            .output()
            .map_err(|error| {
                ReadyError::Failed(format!("could not check the existing install: {error}"))
            })?;
        match output.status.code() {
            Some(0) => Ok(()),
            // Exit 4: the source is partway through restarting itself.
            Some(4) => Err(ReadyError::Restarting),
            // Exit 5: the data is from a newer schema than the bundled backend opens (the
            // backend owns the version, so the app holds no copy of it).
            Some(5) => Err(ReadyError::Failed(
                "adoption refused before changes: your data is newer than this app".into(),
            )),
            // Exit 3 carries a refusal written to be shown: names and byte counts only.
            Some(3) => Err(ReadyError::Refused(
                String::from_utf8_lossy(&output.stderr).trim().to_string(),
            )),
            _ => Err(ReadyError::Failed(format!(
                "could not check the existing install: exited {}",
                output.status
            ))),
        }
    }

    fn look_for_restart(&mut self, state: &Path) -> RestartLook {
        let output = match self
            .python(RESTART_PROGRAM)
            .arg(&self.bundled.backend)
            .arg(state)
            .output()
        {
            Ok(output) if output.status.success() => output,
            Ok(output) => {
                log::warn!("could not look for a restart: exited {}", output.status);
                return RestartLook::Unknown;
            }
            Err(error) => {
                log::warn!("could not look for a restart: {error}");
                return RestartLook::Unknown;
            }
        };
        classify_restart(String::from_utf8_lossy(&output.stdout).trim())
    }

    fn stop_old(
        &mut self,
        progress: &mut dyn FnMut(Option<u32>),
        spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
    ) -> Result<(), String> {
        // The stop's output goes to a file, never a pipe: a pipe closed by an app that quit
        // would kill the stop mid-drain. It is read only after the stop has failed, to show
        // the checkout's own refusal; no decision is made from it.
        let record = stop_record();
        let mut command = self.cli("stop");
        if let Some((file, _)) = &record {
            if let Ok(file) = file.try_clone() {
                command.stdout(Stdio::from(file));
            }
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("could not stop the existing install: {error}"))?;
        // Recorded as soon as it exists, so a Force Quit leaves the journal naming it. A
        // running stop that cannot be named (its start time unreadable, or the journal write
        // failing) is still never killed: it is let to finish and the switch is then undone,
        // because a Force Quit now could not be recovered from.
        let mut unrecorded = None::<String>;
        match (self.stop_clock)(child.id()) {
            Some(started) => {
                let stop = StopProcess {
                    pid: child.id(),
                    started,
                };
                if let Err(error) = spawned(stop) {
                    unrecorded = Some(format!("the journal could not name it: {error}"));
                }
            }
            None => match child.try_wait() {
                // Already gone: there is nothing left to wait for, so nothing to name.
                Ok(Some(_)) => {}
                _ => unrecorded = Some("its start time could not be read".into()),
            },
        }
        let mut counted = None::<Instant>;
        let status = loop {
            if let Some(status) = child.try_wait().map_err(|error| {
                format!("could not wait for the existing install to stop: {error}")
            })? {
                break status;
            }
            if counted.map_or(true, |at| at.elapsed() >= self.count_interval) {
                progress(self.running_turns());
                counted = Some(Instant::now());
            }
            thread::sleep(Duration::from_millis(50));
        };
        let refusal = record.and_then(|(_, path)| {
            let line = refusal_line(&path);
            let _ = std::fs::remove_file(&path);
            line
        });
        if !status.success() {
            return Err(match refusal {
                Some(line) => format!("`corsia stop` exited {status}: {line}"),
                None => format!("`corsia stop` exited {status}"),
            });
        }
        if let Some(why) = unrecorded {
            log::warn!("the stop process could not be recorded: {why}");
            return Err(format!(
                "the stop could not be recorded ({why}), so the switch is being undone"
            ));
        }
        // An exit status is a report, not proof: ask launchd and the port.
        let mut last = String::new();
        if self.wait_until(|| match self.is_down() {
            Ok(()) => true,
            Err(error) => {
                last = error;
                false
            }
        }) {
            Ok(())
        } else {
            Err(format!("the existing install did not stop: {last}"))
        }
    }

    fn stop_is_running(&mut self, stop: &StopProcess) -> bool {
        process_started(stop.pid).as_deref() == Some(stop.started.as_str())
    }

    fn old_is_down(&mut self) -> Result<(), String> {
        self.is_down()
            .map_err(|error| format!("the existing install is running again: {error}"))
    }

    fn backup(&mut self, state: &Path) -> Result<PathBuf, String> {
        let output = self
            .python(SNAPSHOT_PROGRAM)
            .arg(&self.bundled.backend)
            .arg(state)
            .output()
            .map_err(|error| format!("could not start the state backup: {error}"))?;
        if !output.status.success() {
            return Err(format!("the state backup exited {}", output.status));
        }
        let snapshot = String::from_utf8(output.stdout)
            .map_err(|_| "the state backup returned a non-UTF-8 path")?;
        let snapshot = PathBuf::from(snapshot.trim());
        if !snapshot.is_dir() {
            return Err("the state backup did not create a snapshot".into());
        }
        Ok(snapshot)
    }

    fn start_new(&mut self, _state: &Path, port: u16, app_dir: &Path) -> Result<(), String> {
        let since = std::time::SystemTime::now();
        run_service_command("register")?;
        wait_for_service(port, app_dir)?;
        release_proof::replaced_since(
            &format!("gui/{}/{}", unsafe { libc::geteuid() }, service_label()),
            since,
        )
    }

    fn stop_new(&mut self, state: &Path, port: u16) -> Result<(), String> {
        let pid = release_proof::launchd_pid(&format!(
            "gui/{}/{}",
            unsafe { libc::geteuid() },
            service_label()
        ));
        unregister_service()?;
        release_proof::prove_released(pid, port, &self.bundled.python, &state.join("corsia.db"))
    }

    fn restore_backup(&mut self, state: &Path, snapshot: &Path) -> Result<(), String> {
        let status = self
            .python(RESTORE_PROGRAM)
            .arg(&self.bundled.backend)
            .arg(state)
            .arg(snapshot)
            .status()
            .map_err(|error| format!("could not restore the state backup: {error}"))?;
        status
            .success()
            .then_some(())
            .ok_or_else(|| format!("the state restore exited {status}"))
    }

    /// `corsia start` is a no-op for a job that is already loaded, so this also
    /// holds when the install never stopped. Done is launchd listing the job and
    /// the backend answering.
    fn start_old(&mut self) -> Result<(), String> {
        self.start_cli()?;
        self.wait_for_source()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adoption::Lifecycle;
    use std::{
        fs,
        io::{Read, Write},
        net::TcpListener,
        os::unix::fs::PermissionsExt,
    };

    /// A temp home holding a fake checkout CLI, a fake `launchctl` and a fake
    /// bundled Python, none of which touches the real machine.
    struct Home {
        root: tempfile::TempDir,
        lifecycle: SourceLifecycle,
    }

    fn script(path: &Path, body: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// A port that is free now, from below the range the kernel hands out for `bind(0)`, so
    /// no other test that binds port 0 can be given the same number while this one waits to use it.
    fn free_port() -> u16 {
        static NEXT: std::sync::atomic::AtomicU16 = std::sync::atomic::AtomicU16::new(41_000);
        loop {
            let port = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            assert!(port < 49_000, "ran out of test ports");
            if TcpListener::bind(("127.0.0.1", port)).is_ok() {
                return port;
            }
        }
    }

    /// `loaded` exists while the fake job is loaded; the CLI creates and removes it.
    fn home(stop: &str, start: &str, python: &str) -> Home {
        let root = tempfile::Builder::new()
            .prefix("corsia-source-lifecycle-")
            .tempdir_in("/private/tmp")
            .unwrap();
        let base = root.path();
        let loaded = base.join("loaded");
        let counts = base.join("counts");
        let counts = counts.to_str().unwrap();
        script(
            &base.join("checkout/scripts/corsia"),
            &format!(
                "case \"$1\" in stop) {stop};; start) {start};; esac",
                stop = stop
                    .replace("LOADED", loaded.to_str().unwrap())
                    .replace("COUNTS", counts),
                start = start.replace("LOADED", loaded.to_str().unwrap()),
            ),
        );
        script(
            &base.join("launchctl"),
            &format!(
                "[ -e '{loaded}' ] || exit 113\necho 'working directory = {wd}'\necho 'CORSIA_SERVICE_GENERATION => gen-1'\necho 'pid = {pid}'",
                loaded = loaded.display(),
                wd = base.join("checkout/backend").display(),
                pid = std::process::id()
            ),
        );
        script(
            &base.join("resources/python/bin/python3"),
            &python.replace("COUNTS", counts),
        );
        fs::create_dir_all(base.join("resources/backend")).unwrap();
        let source = adoption::SourceInstall {
            plist: base.join("com.corsia.prod.plist"),
            state: base.join("state"),
            workspace: base.join("work"),
            port: free_port(),
            cli: base.join("checkout/scripts/corsia"),
            working_directory: base.join("checkout/backend"),
            generation: Some("gen-1".into()),
            env_file: base.join(".env"),
            plist_variable_names: vec![],
        };
        let mut lifecycle =
            SourceLifecycle::new(source, Bundled::in_resources(&base.join("resources")));
        lifecycle.launchctl = base.join("launchctl");
        lifecycle.env = vec![("HOME", base.into()), ("PATH", "/usr/bin:/bin".into())];
        lifecycle.proof_timeout = Duration::from_millis(300);
        lifecycle.count_interval = Duration::from_millis(30);
        lifecycle.start_stall_window = Duration::from_millis(300);
        lifecycle.start_absolute_cap = Duration::from_secs(2);
        fs::write(&loaded, "").unwrap();
        Home { root, lifecycle }
    }

    fn quiet(_: Option<u32>) {}

    /// Answers every request on the port with a 200 until the thread is dropped with the process.
    fn serve_health(port: u16) {
        let listener = TcpListener::bind(("127.0.0.1", port)).unwrap();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let mut request = [0_u8; 512];
                let _ = stream.read(&mut request);
                let _ = stream.write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok");
            }
        });
    }

    #[test]
    fn bundled_python_receives_the_source_roots() {
        let home = home("rm -f LOADED", "touch LOADED", "env | sort");
        let output = home.lifecycle.python("ignored").output().unwrap();
        assert!(output.status.success());
        let environment = String::from_utf8(output.stdout).unwrap();
        assert!(environment.contains(&format!(
            "CORSIA_WORKSPACE={}\n",
            home.lifecycle.source.workspace.display()
        )));
        assert!(environment.contains(&format!(
            "CORSIA_STATE_DIR={}\n",
            home.lifecycle.source.state.display()
        )));
    }

    #[test]
    fn real_size_snapshot_and_restore_preserve_migration_backups() {
        if std::env::var("CORSIA_ADOPTION_REAL_SIZE").as_deref() != Ok("1") {
            return;
        }
        let state = PathBuf::from(std::env::var("CORSIA_ADOPTION_REHEARSAL_STATE").unwrap());
        let python = PathBuf::from(std::env::var("CORSIA_ADOPTION_REHEARSAL_PYTHON").unwrap());
        let backend = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend");
        let workspace = state.parent().unwrap().join("workspace");
        let source = adoption::SourceInstall {
            plist: state.join("unused.plist"), state: state.clone(), workspace,
            port: 47_896, working_directory: backend.clone(), cli: state.join("unused-corsia"),
            generation: None, env_file: state.join("unused.env"), plist_variable_names: vec![],
        };
        let mut lifecycle = SourceLifecycle::new(source, Bundled { python, backend: backend.clone(), packages: backend });
        let before = lifecycle.python("import hashlib, sqlite3, sys; c=sqlite3.connect(sys.argv[1]); rows=c.execute(\"select count(*) from messages\").fetchone()[0]; sample=''.join(row[0] for row in c.execute(\"select content from messages order by rowid limit 100\")); print(f'{rows}:{hashlib.sha256(sample.encode()).hexdigest()}')").arg(state.join("corsia.db")).output().unwrap();
        assert!(before.status.success());
        let before = String::from_utf8(before.stdout).unwrap();
        let backups: Vec<_> = fs::read_dir(&state).unwrap().filter_map(Result::ok).map(|entry| entry.path()).filter(|path| path.file_name().unwrap().to_string_lossy().contains(".pre-migration-")).collect();
        assert_eq!(backups.len(), 2);
        let backup_metadata: Vec<_> = backups.iter().map(|path| { use std::os::unix::fs::MetadataExt; let metadata = fs::metadata(path).unwrap(); (metadata.ino(), metadata.mtime()) }).collect();
        let snapshot_started = Instant::now();
        let snapshot = lifecycle.backup(&state).unwrap();
        let snapshot_elapsed = snapshot_started.elapsed();
        assert!(backups.iter().all(|path| !snapshot.join("files").join(path.file_name().unwrap()).exists()));
        let mutate = lifecycle.python("import sqlite3, sys; from pathlib import Path; c=sqlite3.connect(Path(sys.argv[1])/'corsia.db'); c.execute(\"insert into messages (id, thread_id, role, content, created_at) values ('mutated','rehearsal-thread','assistant','changed','now')\"); c.commit(); (Path(sys.argv[1])/'changed.txt').write_text('changed')").arg(&state).status().unwrap();
        assert!(mutate.success());
        let restore_started = Instant::now();
        lifecycle.restore_backup(&state, &snapshot).unwrap();
        let restore_elapsed = restore_started.elapsed();
        let verification_started = Instant::now();
        let after = lifecycle.python("import hashlib, sqlite3, sys; from pathlib import Path; c=sqlite3.connect(Path(sys.argv[1])/'corsia.db'); assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'; rows=c.execute(\"select count(*) from messages\").fetchone()[0]; sample=''.join(row[0] for row in c.execute(\"select content from messages order by rowid limit 100\")); print(f'{rows}:{hashlib.sha256(sample.encode()).hexdigest()}')").arg(&state).output().unwrap();
        assert!(after.status.success());
        assert_eq!(String::from_utf8(after.stdout).unwrap(), before);
        assert!(!state.join("changed.txt").exists());
        for (path, expected) in backups.iter().zip(backup_metadata) { use std::os::unix::fs::MetadataExt; let metadata = fs::metadata(path).unwrap(); assert_eq!((metadata.ino(), metadata.mtime()), expected); }
        eprintln!("rehearsal snapshot={snapshot_elapsed:?} restore={restore_elapsed:?} verify={:?}", verification_started.elapsed());
    }

    #[test]
    fn startup_past_the_former_health_timeout_succeeds_while_the_log_advances() {
        let mut home = home("rm -f LOADED", "exit 0", "exit 0");
        let port = home.lifecycle.source.port;
        let log = home.lifecycle.source.state.join("logs/service.log");
        home.lifecycle.start_stall_window = Duration::from_millis(80);
        thread::spawn(move || {
            for _ in 0..3 {
                thread::sleep(Duration::from_millis(10));
                fs::create_dir_all(log.parent().unwrap()).unwrap();
                fs::write(&log, format!("{} progress\n", std::time::Instant::now().elapsed().as_nanos())).unwrap();
            }
            thread::sleep(Duration::from_millis(10));
            serve_health(port);
        });
        let former_timeout = Duration::from_millis(30);
        let started = Instant::now();
        home.lifecycle.start_old().unwrap();
        assert!(started.elapsed() > former_timeout);
    }

    #[test]
    fn a_startup_stall_or_liveness_loss_fails_before_the_safety_cap() {
        let mut stalled = home("rm -f LOADED", "exit 0", "exit 0");
        stalled.lifecycle.start_stall_window = Duration::from_millis(30);
        stalled.lifecycle.start_absolute_cap = Duration::from_secs(1);
        assert!(stalled.lifecycle.start_old().unwrap_err().contains("stopped making startup progress"));

        let mut unloaded = home("rm -f LOADED", "rm -f LOADED", "exit 0");
        assert!(unloaded.lifecycle.start_old().unwrap_err().contains("unloaded"));
        let mut exited = home("rm -f LOADED", "exit 0", "exit 0");
        script(&exited.root.path().join("launchctl"), "echo 'pid = 999999'");
        assert!(exited.lifecycle.start_old().unwrap_err().contains("process exited"));
    }

    #[test]
    fn stop_and_start_run_in_the_checkout_with_exactly_the_basics_and_the_drain_cap() {
        let mut home = home(
            "env | grep -v '^\\(PWD\\|SHLVL\\|_\\|OLDPWD\\)=' | sort > \"$HOME/stop.env\"; pwd -P > \"$HOME/stop.cwd\"; rm -f LOADED",
            "env | grep -v '^\\(PWD\\|SHLVL\\|_\\|OLDPWD\\)=' | sort > \"$HOME/start.env\"; touch LOADED",
            "exit 0",
        );
        home.lifecycle
            .stop_old(&mut quiet, &mut |_| Ok(()))
            .unwrap();
        let base = home.root.path().canonicalize().unwrap();
        let expected = format!(
            "CORSIA_DRAIN_MAX_WAIT=1800\nHOME={}\nPATH=/usr/bin:/bin\n",
            home.root.path().display()
        );
        assert_eq!(fs::read_to_string(base.join("stop.env")).unwrap(), expected);
        assert_eq!(
            fs::read_to_string(base.join("stop.cwd")).unwrap().trim(),
            base.join("checkout").to_str().unwrap()
        );
        serve_health(home.lifecycle.source.port);
        home.lifecycle.start_old().unwrap();
        assert_eq!(
            fs::read_to_string(base.join("start.env")).unwrap(),
            expected
        );
    }

    #[test]
    fn a_stop_is_judged_by_its_exit_status_and_launchd_never_by_its_output() {
        // Exit 0 and a loud message, but the job is still loaded.
        let mut home = home("echo 'stopped OK'", "exit 0", "exit 0");
        let error = home
            .lifecycle
            .stop_old(&mut quiet, &mut |_| Ok(()))
            .unwrap_err();
        assert!(error.contains("did not stop"), "{error}");
        assert!(error.contains("launchd still lists"), "{error}");
        // A failing exit status is a failure whatever it printed.
        let mut home = self::home(
            "rm -f LOADED; echo 'stopped OK'; exit 1",
            "exit 0",
            "exit 0",
        );
        assert!(home
            .lifecycle
            .stop_old(&mut quiet, &mut |_| Ok(()))
            .unwrap_err()
            .contains("exited"));
    }

    #[test]
    fn a_refused_stop_shows_the_checkouts_own_line_and_leaves_the_job_loaded() {
        // `fail` in scripts/corsia writes a coloured, marked line to stdout and returns 1.
        let mut home = home(
            "printf '  \\033[0;31m\u{274c} Installed x does not describe this checkout\\033[0m\\n'; exit 1",
            "exit 0",
            "exit 0",
        );
        let error = home
            .lifecycle
            .stop_old(&mut quiet, &mut |_| Ok(()))
            .unwrap_err();
        assert!(
            error.ends_with(": Installed x does not describe this checkout"),
            "{error:?}"
        );
        assert!(
            home.root.path().join("loaded").exists(),
            "nothing was stopped"
        );
    }

    #[test]
    fn a_stop_that_leaves_the_port_answering_is_not_a_stop() {
        let mut home = home("rm -f LOADED", "exit 0", "exit 0");
        let _held = TcpListener::bind(("127.0.0.1", home.lifecycle.source.port)).unwrap();
        let error = home
            .lifecycle
            .stop_old(&mut quiet, &mut |_| Ok(()))
            .unwrap_err();
        assert!(error.contains("still accepts connections"), "{error}");
    }

    #[test]
    fn the_count_is_polled_while_the_stop_drains_and_an_unreadable_one_is_no_count() {
        // The drain ends only once the count has been read three times, so the number of
        // readings never depends on how fast this machine starts processes.
        let drains = "n=0; while [ $n -lt 500 ] && [ \"$(wc -l < COUNTS 2>/dev/null || echo 0)\" -lt 3 ]; do sleep 0.02; n=$((n+1)); done; rm -f LOADED";
        let mut home = home(drains, "exit 0", "echo x >> COUNTS; echo 2");
        let mut seen = vec![];
        home.lifecycle
            .stop_old(&mut |count| seen.push(count), &mut |_| Ok(()))
            .unwrap();
        assert!(seen.len() >= 3, "{seen:?}");
        assert!(seen.iter().all(|count| *count == Some(2)), "{seen:?}");

        let mut broken = self::home(
            "n=0; while [ $n -lt 500 ] && [ ! -e COUNTS ]; do sleep 0.02; n=$((n+1)); done; rm -f LOADED",
            "exit 0",
            "echo x >> COUNTS; exit 1",
        );
        let mut seen = vec![];
        broken
            .lifecycle
            .stop_old(&mut |count| seen.push(count), &mut |_| Ok(()))
            .unwrap();
        assert!(!seen.is_empty());
        assert!(
            seen.iter().all(Option::is_none),
            "a failed read is never shown as zero: {seen:?}"
        );
    }

    #[test]
    fn the_count_reads_the_database_read_only_and_not_as_immutable() {
        assert!(COUNT_PROGRAM.contains("?mode=ro"));
        assert!(!COUNT_PROGRAM.contains("immutable"));
        assert!(COUNT_PROGRAM.contains("status='running'"));
    }

    #[test]
    fn a_job_that_is_not_loaded_and_has_no_restart_in_flight_is_not_running() {
        let mut home = home("exit 0", "exit 0", "echo none");
        fs::remove_file(home.root.path().join("loaded")).unwrap();
        assert_eq!(
            home.lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::NotRunning)
        );
    }

    #[test]
    fn an_unloaded_job_with_a_restart_in_flight_is_restarting_not_stopped() {
        // The source's own restart boots the job out and back in: young or old, it is a restart.
        for age in ["5", "99999"] {
            let mut home = home("exit 0", "exit 0", &format!("echo {age}"));
            fs::remove_file(home.root.path().join("loaded")).unwrap();
            assert_eq!(
                home.lifecycle
                    .check_ready(Path::new("/state"), &BTreeSet::new()),
                Err(ReadyError::Restarting),
                "request {age} s old"
            );
        }
    }

    #[test]
    fn an_unloaded_job_with_an_unreadable_restart_look_is_not_called_stopped() {
        let mut home = home("exit 0", "exit 0", "exit 7");
        fs::remove_file(home.root.path().join("loaded")).unwrap();
        assert!(matches!(
            home.lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::Failed(_))
        ));
    }

    #[test]
    fn a_job_running_from_another_folder_names_both_folders() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path().to_path_buf();
        home.lifecycle.source.working_directory = base.join("elsewhere/backend");
        home.lifecycle.source.generation = Some("gen-elsewhere".into());
        // The fake launchctl prints `<base>/checkout/backend` and generation `gen-1`.
        assert_eq!(
            home.lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::WrongFolder {
                running: Some("~/checkout".into()),
                checkout: "~/elsewhere".into(),
            })
        );
    }

    #[test]
    fn a_print_without_a_working_directory_line_is_adopted_when_the_generation_matches() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path().to_path_buf();
        script(
            &base.join("launchctl"),
            &format!(
                "[ -e '{}' ] || exit 113\necho 'CORSIA_SERVICE_GENERATION => gen-1'",
                base.join("loaded").display()
            ),
        );
        let answer = home
            .lifecycle
            .check_ready(Path::new("/state"), &BTreeSet::new());
        assert!(
            !matches!(answer, Err(ReadyError::WrongFolder { .. })),
            "a matching generation proves the folder: {answer:?}"
        );
    }

    #[test]
    fn a_generation_that_differs_or_is_missing_from_the_print_is_refused_as_a_different_folder() {
        for printed in [
            "echo 'CORSIA_SERVICE_GENERATION => gen-2'",
            "echo 'state = running'",
        ] {
            let mut home = home("exit 0", "exit 0", "exit 0");
            let base = home.root.path().to_path_buf();
            script(
                &base.join("launchctl"),
                &format!(
                    "[ -e '{}' ] || exit 113\n{printed}",
                    base.join("loaded").display()
                ),
            );
            assert!(matches!(
                home.lifecycle
                    .check_ready(Path::new("/state"), &BTreeSet::new()),
                Err(ReadyError::WrongFolder { running: None, .. })
            ));
        }
    }

    #[test]
    fn a_different_generation_from_the_same_folder_is_older_settings_not_a_different_folder() {
        // The fake launchctl prints `<base>/checkout/backend` and generation `gen-1`.
        let mut first = home("exit 0", "exit 0", "exit 0");
        first.lifecycle.source.generation = Some("gen-2".into());
        assert_eq!(
            first
                .lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::OlderSettings)
        );
        // Another folder with a different generation, or a print with no generation or no
        // folder, keeps the folder refusal.
        let base = first.root.path().to_path_buf();
        first.lifecycle.source.working_directory = base.join("elsewhere/backend");
        assert!(matches!(
            first
                .lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::WrongFolder { .. })
        ));
        for printed in [
            "echo 'state = running'",
            "echo 'CORSIA_SERVICE_GENERATION => gen-1'",
        ] {
            let mut home = home("exit 0", "exit 0", "exit 0");
            home.lifecycle.source.generation = Some("gen-2".into());
            let base = home.root.path().to_path_buf();
            script(
                &base.join("launchctl"),
                &format!(
                    "[ -e '{}' ] || exit 113\n{printed}",
                    base.join("loaded").display()
                ),
            );
            assert!(matches!(
                home.lifecycle
                    .check_ready(Path::new("/state"), &BTreeSet::new()),
                Err(ReadyError::WrongFolder { .. })
            ));
        }
    }

    #[test]
    fn a_plist_without_a_generation_is_refused_like_the_checkouts_own_stop_refuses_it() {
        // service.sh `service_loaded_definition_matches` returns 1 when the plist carries no
        // generation, so `corsia stop` would refuse after a passing preflight. The folder
        // wording still comes from the print.
        // The fake launchctl prints `<base>/checkout/backend` and generation `gen-1`.
        let mut home = home("exit 0", "exit 0", "exit 0");
        home.lifecycle.source.generation = None;
        let same = home
            .lifecycle
            .check_ready(Path::new("/state"), &BTreeSet::new());
        assert!(
            matches!(
                same,
                Err(ReadyError::WrongFolder {
                    running: Some(_),
                    ..
                })
            ),
            "the same folder is still refused without a generation: {same:?}"
        );
        let base = home.root.path().to_path_buf();
        home.lifecycle.source.working_directory = base.join("elsewhere/backend");
        assert_eq!(
            home.lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::WrongFolder {
                running: Some("~/checkout".into()),
                checkout: "~/elsewhere".into(),
            })
        );
    }

    #[test]
    fn a_print_without_a_working_directory_is_refused_as_a_different_folder() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path().to_path_buf();
        script(
            &base.join("launchctl"),
            &format!(
                "[ -e '{}' ] || exit 113\necho 'state = running'",
                base.join("loaded").display()
            ),
        );
        let Err(ReadyError::WrongFolder { running, .. }) = home
            .lifecycle
            .check_ready(Path::new("/state"), &BTreeSet::new())
        else {
            panic!("a print that cannot prove the folder fails closed");
        };
        assert_eq!(running, None);
    }

    #[test]
    fn folders_are_shown_with_the_home_folder_as_a_tilde() {
        let home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path();
        assert_eq!(home.lifecycle.shown(&base.join("a/b")), "~/a/b");
        assert_eq!(home.lifecycle.shown(Path::new("/opt/x")), "/opt/x");
    }

    #[test]
    fn the_backends_preflight_answer_decides_whether_adoption_may_begin() {
        let ready = |python: &str| {
            let mut home = home("exit 0", "exit 0", python);
            home.lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new())
        };
        assert_eq!(ready("exit 0"), Ok(()));
        assert_eq!(
            ready("echo 'These provider keys aren’t stored in CORSIA yet: X.' >&2; exit 3"),
            Err(ReadyError::Refused(
                "These provider keys aren’t stored in CORSIA yet: X.".into()
            ))
        );
        assert!(matches!(ready("exit 1"), Err(ReadyError::Failed(_))));
        assert_eq!(ready("exit 4"), Err(ReadyError::Restarting));
        // Data from a newer schema: the backend owns the version, the app only maps its answer.
        assert_eq!(
            ready("exit 5"),
            Err(ReadyError::Failed(
                "adoption refused before changes: your data is newer than this app".into()
            ))
        );
    }

    #[test]
    fn a_restart_request_is_judged_by_its_own_age_and_an_unreadable_answer_is_unknown() {
        let look = |python: &str| {
            let mut home = home("exit 0", "exit 0", python);
            home.lifecycle.look_for_restart(Path::new("/state"))
        };
        assert_eq!(look("echo none"), RestartLook::Settled);
        assert_eq!(look("echo 12.5"), RestartLook::Young);
        assert_eq!(look("echo 0.0"), RestartLook::Young);
        assert_eq!(look("exit 7"), RestartLook::Unknown);
        assert_eq!(look("echo garbage"), RestartLook::Unknown);
        assert_eq!(look("echo nan"), RestartLook::Unknown);
    }

    #[test]
    fn a_request_is_stuck_only_past_the_longest_drain_and_the_teardown_margin() {
        assert_eq!(STUCK_AFTER.as_secs(), 2100, "1800 s of drain plus 300 s");
        assert_eq!(classify_restart("1799.0"), RestartLook::Young);
        assert_eq!(classify_restart("2099.9"), RestartLook::Young);
        assert_eq!(classify_restart("2100.0"), RestartLook::Young);
        assert_eq!(classify_restart("2100.1"), RestartLook::Stuck);
        assert_eq!(classify_restart("86400"), RestartLook::Stuck);
    }

    #[test]
    fn the_restart_program_asks_the_backends_own_read_only_look() {
        assert!(RESTART_PROGRAM.contains("restart_in_flight_age"));
    }

    #[test]
    fn restart_runs_the_checkouts_script_with_the_basics_and_proves_the_source_answers() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path().canonicalize().unwrap();
        script(
            &base.join("checkout/scripts/corsia"),
            "env | grep -v '^\\(PWD\\|SHLVL\\|_\\|OLDPWD\\)=' | sort > \"$HOME/$1.env\"; touch \"$HOME/loaded\"",
        );
        serve_health(home.lifecycle.source.port);
        home.lifecycle.restart_existing().unwrap();
        assert_eq!(
            fs::read_to_string(base.join("restart.env")).unwrap(),
            format!(
                "CORSIA_DRAIN_MAX_WAIT=1800\nHOME={}\nPATH=/usr/bin:/bin\n",
                home.root.path().display()
            )
        );
    }

    #[test]
    fn a_failed_restart_is_a_failure() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path().to_path_buf();
        script(&base.join("checkout/scripts/corsia"), "exit 5");
        assert!(home
            .lifecycle
            .restart_existing()
            .unwrap_err()
            .contains("exited"));
    }

    #[test]
    fn the_stop_process_is_reported_as_soon_as_it_exists_and_is_known_while_it_lives() {
        let mut home = home("sleep 1; rm -f LOADED", "exit 0", "exit 0");
        let mut recorded = vec![];
        home.lifecycle
            .stop_old(&mut |_| {}, &mut |process| {
                recorded.push(process);
                Ok(())
            })
            .unwrap();
        assert_eq!(recorded.len(), 1, "{recorded:?}");
        let process = recorded.remove(0);
        // The stop returned, so it no longer runs; the pair was real while it did.
        assert!(!home.lifecycle.stop_is_running(&process));
        let this = StopProcess {
            pid: std::process::id(),
            started: process_started(std::process::id()).unwrap(),
        };
        assert!(home.lifecycle.stop_is_running(&this));
        // A reused pid has a different start time and is not the stop.
        let reused = StopProcess {
            started: "0.000000".into(),
            ..this
        };
        assert!(!home.lifecycle.stop_is_running(&reused));
    }

    /// A `corsia stop` that waits for `$HOME/go`, which the test creates from its progress
    /// callback (called only after the app has looked at the running stop), then finishes.
    const WAITS_FOR_GO: &str = "n=0; while [ $n -lt 500 ] && [ ! -e \"$HOME/go\" ]; do sleep 0.02; n=$((n+1)); done; touch \"$HOME/stopped\"; rm -f LOADED";

    #[test]
    fn a_stop_the_journal_cannot_name_is_let_to_finish_and_then_undone() {
        let mut home = home(WAITS_FOR_GO, "exit 0", "exit 0");
        let base = home.root.path().to_path_buf();
        let error = home
            .lifecycle
            .stop_old(
                &mut |_| fs::write(base.join("go"), "").unwrap(),
                &mut |_| Err("disk full".into()),
            )
            .unwrap_err();
        assert!(error.contains("could not be recorded"), "{error}");
        assert!(error.contains("disk full"), "{error}");
        assert!(
            base.join("stopped").exists(),
            "the stop ran to its end, it was not killed"
        );
    }

    #[test]
    fn a_running_stop_whose_start_time_cannot_be_read_is_let_to_finish_and_then_undone() {
        let mut home = home(WAITS_FOR_GO, "exit 0", "exit 0");
        home.lifecycle.stop_clock = |_| None;
        let base = home.root.path().to_path_buf();
        let mut named = false;
        let error = home
            .lifecycle
            .stop_old(
                &mut |_| fs::write(base.join("go"), "").unwrap(),
                &mut |_| {
                    named = true;
                    Ok(())
                },
            )
            .unwrap_err();
        assert!(error.contains("start time could not be read"), "{error}");
        assert!(!named);
        assert!(base.join("stopped").exists());
    }

    /// Waits until the child has exited (without reaping it), then reports no start time:
    /// a stop that finished before it could be looked at.
    fn unreadable_after_exit(pid: u32) -> Option<String> {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        unsafe {
            libc::waitid(
                libc::P_PID,
                pid as libc::id_t,
                &mut info,
                libc::WEXITED | libc::WNOWAIT,
            )
        };
        None
    }

    #[test]
    fn a_stop_that_already_exited_needs_no_record() {
        let mut home = home("rm -f LOADED", "exit 0", "exit 0");
        home.lifecycle.stop_clock = unreadable_after_exit;
        home.lifecycle
            .stop_old(&mut quiet, &mut |_| panic!("nothing to name"))
            .unwrap();
    }

    #[test]
    fn a_source_that_is_running_again_is_caught_before_the_app_starts() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        assert!(home
            .lifecycle
            .old_is_down()
            .unwrap_err()
            .contains("running again"));
        fs::remove_file(home.root.path().join("loaded")).unwrap();
        home.lifecycle.old_is_down().unwrap();
        let _held = TcpListener::bind(("127.0.0.1", home.lifecycle.source.port)).unwrap();
        assert!(home.lifecycle.old_is_down().is_err());
    }
}
