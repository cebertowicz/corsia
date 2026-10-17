//! The production side of in-place adoption: the source install's own
//! `scripts/corsia` stops and starts its service, launchd and the loopback
//! port prove what it did, and the app's bundled backend and Python make the
//! backup. Nothing here reads the CLI's output to decide anything.

use std::{
    collections::BTreeSet,
    ffi::OsString,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use crate::{
    adoption::{self, ReadyError, StopProcess},
    probe_health, release_proof, run_service_command, unregister_service, wait_for_service,
    APP_SERVICE_LABEL, HEALTH_POLL_INTERVAL, HEALTH_TIMEOUT,
};

/// The source's own drain wait, in seconds: its cap, and the longest a switch waits for running work.
const DRAIN_MAX_WAIT: &str = "1800";
/// The variables of the app's own environment the CLI is given.
const INHERITED: [&str; 5] = ["HOME", "PATH", "USER", "LANG", "TMPDIR"];
const COUNT_INTERVAL: Duration = Duration::from_secs(1);
const PROOF_TIMEOUT: Duration = Duration::from_secs(15);

/// What the source backend's drain counts as running (`turn_core.active_turn_count`).
/// Read only, over `mode=ro` and never immutable: the database is live.
const COUNT_PROGRAM: &str = "import sqlite3, sys; from pathlib import Path; c = sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True, timeout=1); print(c.execute(\"SELECT COUNT(*) FROM active_turns WHERE status='running'\").fetchone()[0])";
const PREFLIGHT_PROGRAM: &str = "import sys; sys.path.insert(0, sys.argv[1]); from infra.adoption_snapshot import preflight_main; sys.exit(preflight_main(sys.argv[2:]))";
const RESTART_PROGRAM: &str = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from infra.adoption_snapshot import restart_in_flight; sys.exit(0 if restart_in_flight(Path(sys.argv[2])) else 1)";
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
            .env("CORSIA_DRAIN_MAX_WAIT", DRAIN_MAX_WAIT)
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

    fn wait_for_source(&self) -> Result<(), String> {
        let deadline = Instant::now() + HEALTH_TIMEOUT;
        loop {
            if self.loaded_job()?.is_some() && probe_health(self.source.port) {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err("the existing install did not answer after it was started".into());
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

/// The `working directory = ` line of `launchctl print`.
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
        let printed = self
            .loaded_job()
            .map_err(ReadyError::Failed)?
            .ok_or(ReadyError::NotRunning)?;
        // The checkout that would stop it must be the one the loaded job runs
        // from. A print without the line cannot prove it, so it is refused the same way.
        let running = printed_working_directory(&printed);
        if running.map(Path::new) != Some(self.source.working_directory.as_path()) {
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

    fn restart_in_flight(&mut self, state: &Path) -> Option<bool> {
        let output = self
            .python(RESTART_PROGRAM)
            .arg(&self.bundled.backend)
            .arg(state)
            .output()
            .inspect_err(|error| log::warn!("could not look for a restart: {error}"))
            .ok()?;
        match output.status.code() {
            Some(0) => Some(true),
            Some(1) => Some(false),
            _ => {
                log::warn!("could not look for a restart: exited {}", output.status);
                None
            }
        }
    }

    fn stop_old(
        &mut self,
        progress: &mut dyn FnMut(Option<u32>),
        spawned: &mut dyn FnMut(StopProcess),
    ) -> Result<(), String> {
        let mut child = self
            .cli("stop")
            .spawn()
            .map_err(|error| format!("could not stop the existing install: {error}"))?;
        // Recorded as soon as it exists, so a Force Quit leaves the journal naming it.
        if let Some(started) = process_started(child.id()) {
            spawned(StopProcess {
                pid: child.id(),
                started,
            });
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
        if !status.success() {
            return Err(format!("`corsia stop` exited {status}"));
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
            &format!("gui/{}/{APP_SERVICE_LABEL}", unsafe { libc::geteuid() }),
            since,
        )
    }

    fn stop_new(&mut self, state: &Path, port: u16) -> Result<(), String> {
        let pid = release_proof::launchd_pid(&format!("gui/{}/{APP_SERVICE_LABEL}", unsafe {
            libc::geteuid()
        }));
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

    fn free_port() -> u16 {
        TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
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
                "[ -e '{loaded}' ] || exit 113\necho 'working directory = {wd}'",
                loaded = loaded.display(),
                wd = base.join("checkout/backend").display()
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
            env_file: base.join(".env"),
            plist_variable_names: vec![],
        };
        let mut lifecycle =
            SourceLifecycle::new(source, Bundled::in_resources(&base.join("resources")));
        lifecycle.launchctl = base.join("launchctl");
        lifecycle.env = vec![("HOME", base.into()), ("PATH", "/usr/bin:/bin".into())];
        lifecycle.proof_timeout = Duration::from_millis(300);
        lifecycle.count_interval = Duration::from_millis(30);
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
    fn stop_and_start_run_in_the_checkout_with_exactly_the_basics_and_the_drain_cap() {
        let mut home = home(
            "env | grep -v '^\\(PWD\\|SHLVL\\|_\\|OLDPWD\\)=' | sort > \"$HOME/stop.env\"; pwd -P > \"$HOME/stop.cwd\"; rm -f LOADED",
            "env | grep -v '^\\(PWD\\|SHLVL\\|_\\|OLDPWD\\)=' | sort > \"$HOME/start.env\"; touch LOADED",
            "exit 0",
        );
        home.lifecycle.stop_old(&mut quiet, &mut |_| {}).unwrap();
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
            .stop_old(&mut quiet, &mut |_| {})
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
            .stop_old(&mut quiet, &mut |_| {})
            .unwrap_err()
            .contains("exited"));
    }

    #[test]
    fn a_stop_that_leaves_the_port_answering_is_not_a_stop() {
        let mut home = home("rm -f LOADED", "exit 0", "exit 0");
        let _held = TcpListener::bind(("127.0.0.1", home.lifecycle.source.port)).unwrap();
        let error = home
            .lifecycle
            .stop_old(&mut quiet, &mut |_| {})
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
            .stop_old(&mut |count| seen.push(count), &mut |_| {})
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
            .stop_old(&mut |count| seen.push(count), &mut |_| {})
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
    fn a_job_that_is_not_loaded_is_not_running_before_anything_is_asked() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        fs::remove_file(home.root.path().join("loaded")).unwrap();
        assert_eq!(
            home.lifecycle
                .check_ready(Path::new("/state"), &BTreeSet::new()),
            Err(ReadyError::NotRunning)
        );
    }

    #[test]
    fn a_job_running_from_another_folder_names_both_folders() {
        let mut home = home("exit 0", "exit 0", "exit 0");
        let base = home.root.path().to_path_buf();
        home.lifecycle.source.working_directory = base.join("elsewhere/backend");
        // The fake launchctl prints `<base>/checkout/backend`.
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
    }

    #[test]
    fn a_restart_in_flight_is_read_from_the_backend_and_an_unreadable_answer_is_unknown() {
        let look = |python: &str| {
            let mut home = home("exit 0", "exit 0", python);
            home.lifecycle.restart_in_flight(Path::new("/state"))
        };
        assert_eq!(look("exit 0"), Some(true));
        assert_eq!(look("exit 1"), Some(false));
        assert_eq!(look("exit 7"), None);
    }

    #[test]
    fn the_restart_program_asks_the_backends_own_read_only_look() {
        assert!(RESTART_PROGRAM.contains("restart_in_flight"));
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
            .stop_old(&mut |_| {}, &mut |process| recorded.push(process))
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
