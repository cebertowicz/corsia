// Where the packaged app keeps its metadata and its operational state.
//
// The app folder (`~/Library/Application Support/CORSIA`) holds app metadata
// such as `service.json`; operational state lives in its `state` child, the
// same rule as the backend's `default_state_root()`. `CORSIA_STATE_DIR` stays
// the one exact state-root override, for tests and scripted runs.

use std::{
    env,
    ffi::OsString,
    fs::{self, File, OpenOptions},
    io,
    path::{Path, PathBuf},
};

/// The app's folder under Application Support. Held equal to the backend's
/// `APP_FOLDER` and the launcher's `CORSIA_APP_FOLDER` by a parity test.
pub const APP_FOLDER: &str = "CORSIA";
pub(crate) const STATE_DIRECTORY: &str = "state";
pub(crate) const UPDATE_LOCK_FILE: &str = "update.lock";

pub fn app_dir_for_home(home: &Path) -> PathBuf {
    home.join("Library")
        .join("Application Support")
        .join(APP_FOLDER)
}

pub(crate) fn state_dir_with_override(
    app_dir: &Path,
    state_override: Option<OsString>,
) -> Result<PathBuf, String> {
    if let Some(exact) = state_override.filter(|path| !path.is_empty()) {
        return Ok(PathBuf::from(exact));
    }
    Ok(recorded_state_dir(app_dir)?.unwrap_or_else(|| app_dir.join(STATE_DIRECTORY)))
}

/// Whether `service.json` may name this folder as the state root: a string that
/// starts with `/` and holds no control character. Held equal to the backend's
/// `state_dir_is_valid` (`backend/infra/service_file.py`) by a parity test.
pub fn valid_state_dir(value: &str) -> bool {
    value.starts_with('/') && !value.chars().any(char::is_control)
}

/// The state root `service.json` records, as the backend reads it: an adopted
/// install keeps its data where it was, so the recorded folder, not the
/// `state` child, is the root. No file, or none naming a `state_dir`, means
/// the default. A file that cannot be read or a `state_dir` that is not valid
/// is refused: falling back would open a different, empty root.
fn recorded_state_dir(app_dir: &Path) -> Result<Option<PathBuf>, String> {
    let refused = |why: &str| {
        format!("CORSIA’s service.json {why}, so no other state folder was opened.")
    };
    let bytes = match fs::read(app_dir.join("service.json")) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(refused("could not be read")),
    };
    let value: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| refused("is damaged"))?;
    let Some(object) = value.as_object() else {
        return Err(refused("is damaged"));
    };
    match object.get("state_dir") {
        None => Ok(None),
        Some(recorded) => recorded
            .as_str()
            .filter(|path| valid_state_dir(path))
            .map(|path| Some(PathBuf::from(path)))
            .ok_or_else(|| refused("names a state folder that is not valid")),
    }
}

/// The operational state root for an app folder.
pub fn state_dir_for(app_dir: &Path) -> Result<PathBuf, String> {
    state_dir_with_override(app_dir, env::var_os("CORSIA_STATE_DIR"))
}

/// The operational state root of the person running this process.
pub fn state_dir() -> Result<PathBuf, String> {
    state_dir_for(&app_dir()?)
}

/// The app folder of the person running this process, from `HOME`.
pub fn app_dir() -> Result<PathBuf, String> {
    env::var_os("HOME")
        .filter(|home| !home.is_empty())
        .map(|home| app_dir_for_home(Path::new(&home)))
        .ok_or_else(|| "could not resolve the CORSIA app folder: HOME is not set".into())
}

/// Holds the update lock until dropped; the kernel releases it if the process
/// dies, so a crashed update never wedges the next one. An inherited lock has
/// no file of its own: it belongs to the parent's open descriptor, which this
/// process never closes.
pub struct UpdateLock {
    _file: Option<File>,
}

/// The descriptor of the update lock an update's shell holds and passes down.
pub const UPDATE_LOCK_FD_VARIABLE: &str = "CORSIA_UPDATE_LOCK_FD";

/// Take the update lock for a swap or rollback. The update's shell holds it
/// across the whole update and names its descriptor in `CORSIA_UPDATE_LOCK_FD`;
/// opening the file again would be refused by that same lock, so the named
/// descriptor is checked to be this state root's `update.lock` and locked
/// itself. Unset, the lock is taken as [`try_lock_update`] takes it.
/// `Ok(None)` means another holder has it.
pub fn lock_update_for_swap(state_dir: &Path) -> Result<Option<UpdateLock>, String> {
    match env::var_os(UPDATE_LOCK_FD_VARIABLE) {
        None => try_lock_update(state_dir),
        Some(descriptor) => lock_inherited_update(state_dir, &descriptor),
    }
}

fn lock_inherited_update(state_dir: &Path, descriptor: &std::ffi::OsStr) -> Result<Option<UpdateLock>, String> {
    use std::os::unix::fs::MetadataExt;

    let refused = |why: String| format!("the inherited update lock was refused: {why}");
    let fd: libc::c_int = descriptor
        .to_str()
        .and_then(|text| text.parse().ok())
        .filter(|fd| *fd >= 0)
        .ok_or_else(|| refused(format!("{UPDATE_LOCK_FD_VARIABLE} is not a descriptor number")))?;
    let path = state_dir.join(UPDATE_LOCK_FILE);
    let lock = fs::metadata(&path).map_err(|error| refused(format!("could not check {}: {error}", path.display())))?;
    // SAFETY: fstat only writes the zeroed struct it is given.
    let mut held: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut held) } != 0 {
        return Err(refused(format!("descriptor {fd} is not open: {}", io::Error::last_os_error())));
    }
    let regular = held.st_mode & libc::S_IFMT == libc::S_IFREG;
    if !regular || held.st_dev as u64 != lock.dev() || held.st_ino as u64 != lock.ino() {
        return Err(refused(format!("descriptor {fd} is not {}", path.display())));
    }
    // SAFETY: flock only reads the descriptor, which the parent keeps open.
    if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(Some(UpdateLock { _file: None }));
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
        Ok(None)
    } else {
        Err(format!("could not lock {}: {error}", path.display()))
    }
}

/// Wait for any update in this state root to finish, then hold its lock while
/// the app starts its service, so a start never runs into a swap. A state root
/// that does not exist yet is never created, so a first run still makes its own
/// and has no update to wait for; in one that exists, `update.lock` is made if
/// it is missing, so the start excludes an update that begins while it runs.
pub fn wait_for_update_lock(state_dir: &Path) -> Result<Option<UpdateLock>, String> {
    use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd};

    let path = state_dir.join(UPDATE_LOCK_FILE);
    let opened = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&path);
    let file = match opened {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("could not open {}: {error}", path.display())),
    };
    loop {
        // SAFETY: flock only reads the descriptor, which `file` keeps open.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0 {
            return Ok(Some(UpdateLock { _file: Some(file) }));
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(format!("could not lock {}: {error}", path.display()));
        }
    }
}

/// Take the one update lock in the state root without waiting. `Ok(None)`
/// means another stage or swap holds it.
pub fn try_lock_update(state_dir: &Path) -> Result<Option<UpdateLock>, String> {
    fs::create_dir_all(state_dir)
        .map_err(|error| format!("could not create {}: {error}", state_dir.display()))?;
    lock_update_now(state_dir)
}

/// Take the update lock without waiting for a recovery start, which only
/// brings back a service an install already has: a state root that does not
/// exist refuses rather than being made. `Ok(None)` means an update or a
/// start holds it.
pub fn try_lock_update_in_existing(state_dir: &Path) -> Result<Option<UpdateLock>, String> {
    if !state_dir.is_dir() {
        return Err(format!("{} does not exist", state_dir.display()));
    }
    lock_update_now(state_dir)
}

fn lock_update_now(state_dir: &Path) -> Result<Option<UpdateLock>, String> {
    use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd};

    let path = state_dir.join(UPDATE_LOCK_FILE);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&path)
        .map_err(|error| format!("could not open {}: {error}", path.display()))?;
    // SAFETY: flock only reads the descriptor, which `file` keeps open.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(Some(UpdateLock { _file: Some(file) }));
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
        Ok(None)
    } else {
        Err(format!("could not lock {}: {error}", path.display()))
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    #[test]
    fn app_folder_is_corsia_with_a_state_child_never_the_legacy_identifier() {
        let home = Path::new("/tmp/corsia-desktop-state-test");
        let app_dir = app_dir_for_home(home);

        assert_eq!(
            app_dir,
            home.join("Library").join("Application Support").join("CORSIA")
        );
        assert_eq!(state_dir_with_override(&app_dir, None), Ok(app_dir.join("state")));
        assert!(!app_dir.to_string_lossy().contains("com.cebertowicz.corsia"));
    }

    #[test]
    fn state_override_is_exact_and_an_empty_one_is_ignored() {
        let app_dir = Path::new("/tmp/app");
        assert_eq!(
            state_dir_with_override(app_dir, Some("/tmp/elsewhere".into())),
            Ok(PathBuf::from("/tmp/elsewhere"))
        );
        assert_eq!(
            state_dir_with_override(app_dir, Some(OsString::new())),
            Ok(app_dir.join("state"))
        );
    }

    #[test]
    fn recorded_state_dir_wins_over_the_default_but_not_the_override() {
        let temp = tempfile::tempdir().unwrap();
        let app_dir = temp.path();
        assert_eq!(state_dir_with_override(app_dir, None), Ok(app_dir.join("state")));

        fs::write(
            app_dir.join("service.json"),
            br#"{"state_dir": "/tmp/adopted-state", "port": 3001}"#,
        )
        .unwrap();
        assert_eq!(
            state_dir_with_override(app_dir, None),
            Ok(PathBuf::from("/tmp/adopted-state"))
        );
        assert_eq!(
            state_dir_with_override(app_dir, Some("/tmp/exact".into())),
            Ok(PathBuf::from("/tmp/exact"))
        );
        // A record with no state_dir keeps the default.
        fs::write(app_dir.join("service.json"), br#"{"port": 3001}"#).unwrap();
        assert_eq!(state_dir_with_override(app_dir, None), Ok(app_dir.join("state")));
    }

    #[test]
    fn a_damaged_or_invalid_record_is_refused_never_replaced_by_the_default() {
        let temp = tempfile::tempdir().unwrap();
        let app_dir = temp.path();
        for damaged in [
            &b"{not json"[..],
            b"[1]",
            br#"{"state_dir": "relative"}"#,
            br#"{"state_dir": 3}"#,
            "{\"state_dir\": \"/tmp/bad\\u0007name\"}".as_bytes(),
        ] {
            fs::write(app_dir.join("service.json"), damaged).unwrap();
            let refusal = state_dir_with_override(app_dir, None).unwrap_err();
            assert!(refusal.contains("no other state folder was opened"), "{refusal}");
            // An explicit override still wins, as it does in the launcher.
            assert_eq!(
                state_dir_with_override(app_dir, Some("/tmp/exact".into())),
                Ok(PathBuf::from("/tmp/exact"))
            );
        }
    }

    #[test]
    fn state_dir_validity_matches_the_backends_rule() {
        // Both sides are held to one table of values, so they cannot drift apart.
        let table = include_str!("../../../backend/tests/fixtures/state_dir_validity.txt");
        let mut cases = 0;
        for line in table.lines().filter(|line| !line.is_empty() && !line.starts_with('#')) {
            let (expected, value) = line.split_once('\t').expect("valid<TAB>value");
            let value = value.replace("\\u0007", "\u{7}").replace("\\n", "\n");
            assert_eq!(valid_state_dir(&value), expected == "valid", "{line:?}");
            cases += 1;
        }
        assert!(cases >= 6);
    }

    /// A flock lives until the last copy of its descriptor closes, and a
    /// concurrent test's fork briefly holds a copy, so lock checks run in a
    /// test process of their own. True in that process; in the outer one it
    /// runs the test there and asserts it passed.
    pub(crate) fn alone(name: &str) -> bool {
        if std::env::var_os("CORSIA_UPDATE_LOCK_ALONE").is_some() {
            return true;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", name, "--test-threads=1"])
            .env("CORSIA_UPDATE_LOCK_ALONE", "1")
            .env_remove(UPDATE_LOCK_FD_VARIABLE)
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            output.status.success() && stdout.contains("1 passed"),
            "{stdout}{}",
            String::from_utf8_lossy(&output.stderr)
        );
        false
    }

    fn open_lock(state: &Path) -> File {
        OpenOptions::new().read(true).write(true).open(state.join(UPDATE_LOCK_FILE)).unwrap()
    }

    fn flock_now(file: &File) -> bool {
        use std::os::unix::io::AsRawFd;
        unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) == 0 }
    }

    #[test]
    fn update_lock_admits_one_holder_and_frees_on_drop() {
        if !alone("app_paths::tests::update_lock_admits_one_holder_and_frees_on_drop") {
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");

        let held = try_lock_update(&state).unwrap().expect("first holder");
        assert!(try_lock_update(&state).unwrap().is_none());
        drop(held);
        assert!(try_lock_update(&state).unwrap().is_some());
    }

    #[test]
    fn a_swap_takes_the_lock_its_shell_holds_and_nothing_else() {
        use std::os::unix::io::AsRawFd;
        if !alone("app_paths::tests::a_swap_takes_the_lock_its_shell_holds_and_nothing_else") {
            return;
        }
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        drop(try_lock_update(&state).unwrap());

        // The shell's own open file, locked: the swap is admitted on it, and
        // an independent opener is still refused while the swap runs.
        let shell = open_lock(&state);
        assert!(flock_now(&shell));
        let fd = shell.as_raw_fd().to_string();
        assert!(lock_inherited_update(&state, fd.as_ref()).unwrap().is_some());
        // Opening the file again, as a swap without the descriptor would, is refused.
        assert!(try_lock_update(&state).unwrap().is_none());
        drop(shell);

        // A descriptor of the right file whose open file another holder has
        // locked is busy, never admitted.
        let other = try_lock_update(&state).unwrap().expect("another holder");
        let passed = open_lock(&state);
        assert!(lock_inherited_update(&state, passed.as_raw_fd().to_string().as_ref()).unwrap().is_none());
        drop(other);

        // Anything but this state root's update.lock is refused outright.
        let elsewhere = tempfile::tempdir().unwrap();
        let forged = try_lock_update(elsewhere.path()).unwrap();
        drop(forged);
        let forged = open_lock(elsewhere.path());
        let refusal = lock_inherited_update(&state, forged.as_raw_fd().to_string().as_ref()).err().unwrap();
        assert!(refusal.contains("is not"), "{refusal}");
        let directory = File::open(&state).unwrap();
        assert!(lock_inherited_update(&state, directory.as_raw_fd().to_string().as_ref()).is_err());
        for text in ["", "seven", "-1", "999999"] {
            assert!(lock_inherited_update(&state, text.as_ref()).is_err(), "{text:?}");
        }

        // The variable chooses the path: unset, the swap opens its own.
        std::env::remove_var(UPDATE_LOCK_FD_VARIABLE);
        assert!(lock_update_for_swap(&state).unwrap().is_some());
        let shell = open_lock(&state);
        assert!(flock_now(&shell));
        assert!(lock_update_for_swap(&state).unwrap().is_none());
        std::env::set_var(UPDATE_LOCK_FD_VARIABLE, shell.as_raw_fd().to_string());
        assert!(lock_update_for_swap(&state).unwrap().is_some());
        std::env::remove_var(UPDATE_LOCK_FD_VARIABLE);
    }

    #[test]
    fn a_start_waits_for_an_update_and_never_creates_the_state_folder() {
        if !alone("app_paths::tests::a_start_waits_for_an_update_and_never_creates_the_state_folder") {
            return;
        }
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");

        assert!(wait_for_update_lock(&state).unwrap().is_none());
        assert!(!state.exists(), "waiting created the state folder");

        // An existing state folder with no lock yet: the start holds a lock
        // an update that begins meanwhile is refused by.
        fs::create_dir(&state).unwrap();
        let start = wait_for_update_lock(&state).unwrap().expect("the start's lock");
        assert!(try_lock_update(&state).unwrap().is_none(), "an update ran into the start");
        drop(start);

        let update = try_lock_update(&state).unwrap().expect("the update");
        let (started, waited) = std::sync::mpsc::channel();
        let starting = state.clone();
        let start = std::thread::spawn(move || {
            let lock = wait_for_update_lock(&starting).unwrap();
            started.send(()).unwrap();
            lock.is_some()
        });
        assert!(waited.recv_timeout(std::time::Duration::from_millis(300)).is_err(), "the start ran into the update");
        drop(update);
        waited.recv_timeout(std::time::Duration::from_secs(10)).expect("the start went on once the update finished");
        assert!(start.join().unwrap());
    }
}
