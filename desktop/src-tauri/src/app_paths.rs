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
const STATE_DIRECTORY: &str = "state";
const UPDATE_LOCK_FILE: &str = "update.lock";

pub fn app_dir_for_home(home: &Path) -> PathBuf {
    home.join("Library")
        .join("Application Support")
        .join(APP_FOLDER)
}

fn state_dir_with_override(app_dir: &Path, state_override: Option<OsString>) -> PathBuf {
    state_override
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| app_dir.join(STATE_DIRECTORY))
}

/// The operational state root for an app folder.
pub fn state_dir_for(app_dir: &Path) -> PathBuf {
    state_dir_with_override(app_dir, env::var_os("CORSIA_STATE_DIR"))
}

/// The app folder of the person running this process, from `HOME`.
pub fn app_dir() -> Result<PathBuf, String> {
    env::var_os("HOME")
        .filter(|home| !home.is_empty())
        .map(|home| app_dir_for_home(Path::new(&home)))
        .ok_or_else(|| "could not resolve the CORSIA app folder: HOME is not set".into())
}

/// Holds the update lock until dropped; the kernel releases it if the process
/// dies, so a crashed update never wedges the next one.
pub struct UpdateLock {
    _file: File,
}

/// Take the one update lock in the state root without waiting. `Ok(None)`
/// means another stage or swap holds it.
pub fn try_lock_update(state_dir: &Path) -> Result<Option<UpdateLock>, String> {
    use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd};

    fs::create_dir_all(state_dir)
        .map_err(|error| format!("could not create {}: {error}", state_dir.display()))?;
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
        return Ok(Some(UpdateLock { _file: file }));
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
        Ok(None)
    } else {
        Err(format!("could not lock {}: {error}", path.display()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_folder_is_corsia_with_a_state_child_never_the_legacy_identifier() {
        let home = Path::new("/tmp/corsia-desktop-state-test");
        let app_dir = app_dir_for_home(home);

        assert_eq!(
            app_dir,
            home.join("Library").join("Application Support").join("CORSIA")
        );
        assert_eq!(state_dir_with_override(&app_dir, None), app_dir.join("state"));
        assert!(!app_dir.to_string_lossy().contains("com.cebertowicz.corsia"));
    }

    #[test]
    fn state_override_is_exact_and_an_empty_one_is_ignored() {
        let app_dir = Path::new("/tmp/app");
        assert_eq!(
            state_dir_with_override(app_dir, Some("/tmp/elsewhere".into())),
            PathBuf::from("/tmp/elsewhere")
        );
        assert_eq!(
            state_dir_with_override(app_dir, Some(OsString::new())),
            app_dir.join("state")
        );
    }

    #[test]
    fn update_lock_admits_one_holder_and_frees_on_drop() {
        // A flock lives until the last copy of its descriptor closes, and a
        // concurrent test's fork briefly holds a copy, so the check runs in a
        // test process of its own.
        const NAME: &str = "app_paths::tests::update_lock_admits_one_holder_and_frees_on_drop";
        if std::env::var_os("CORSIA_UPDATE_LOCK_ALONE").is_none() {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", NAME, "--test-threads=1"])
                .env("CORSIA_UPDATE_LOCK_ALONE", "1")
                .output()
                .unwrap();
            let stdout = String::from_utf8_lossy(&output.stdout);
            assert!(
                output.status.success() && stdout.contains("1 passed"),
                "{stdout}{}",
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");

        let held = try_lock_update(&state).unwrap().expect("first holder");
        assert!(try_lock_update(&state).unwrap().is_none());
        drop(held);
        assert!(try_lock_update(&state).unwrap().is_some());
    }
}
