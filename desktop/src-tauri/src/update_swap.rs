// `CORSIA --update-swap <operation>`: put a staged update in the installed
// app's place.
//
// The staged copy's signature is checked again against the installed app,
// then one `renamex_np(RENAME_SWAP)` exchanges the two, so the installed path
// always holds a runnable app. The swapped-out app becomes
// `CORSIA.app.previous`, which the post-swap handoff deletes once the new
// version is proven. The service is stopped by the caller first; nothing here
// touches operational state.

use std::{
    fs,
    path::{Path, PathBuf},
};

use crate::{app_paths, update_stage};

pub const SWAPPED: i32 = 0;
pub const SWAP_FAILED: i32 = 1;
pub const IDENTITY_FAILED: i32 = 42;
pub const UPDATE_BUSY: i32 = 75;

#[derive(Debug)]
enum SwapError {
    Identity(String),
    Swap(String),
}

pub fn run(_operation_id: &str) -> i32 {
    let state_dir = match app_paths::app_dir() {
        Ok(app_dir) => app_paths::state_dir_for(&app_dir),
        Err(reason) => {
            eprintln!("update swap failed: {reason}");
            return SWAP_FAILED;
        }
    };
    let _lock = match app_paths::try_lock_update(&state_dir) {
        Ok(Some(lock)) => lock,
        Ok(None) => {
            eprintln!("update swap refused: another update is in progress");
            return UPDATE_BUSY;
        }
        Err(reason) => {
            eprintln!("update swap failed: {reason}");
            return SWAP_FAILED;
        }
    };
    let installed = match update_stage::running_bundle() {
        Ok(installed) => installed,
        Err(reason) => {
            eprintln!("update swap failed: {reason}");
            return SWAP_FAILED;
        }
    };
    let staged = update_stage::staged_path(&installed);
    match swap_installed(&installed, &staged, update_stage::verify_bundle_identity, exchange) {
        Ok(previous) => {
            println!("{}", previous.display());
            SWAPPED
        }
        Err(SwapError::Identity(reason)) => {
            eprintln!("update swap refused the staged app: {reason}");
            IDENTITY_FAILED
        }
        Err(SwapError::Swap(reason)) => {
            eprintln!("update swap failed: {reason}");
            SWAP_FAILED
        }
    }
}

/// Put the app retained after a successful swap back at the installed path.
/// The failed update remains at `.previous` until its caller proves the old
/// service is healthy and deletes it.
pub fn rollback(_operation_id: &str) -> i32 {
    let state_dir = match app_paths::app_dir() {
        Ok(app_dir) => app_paths::state_dir_for(&app_dir),
        Err(reason) => {
            eprintln!("update rollback failed: {reason}");
            return SWAP_FAILED;
        }
    };
    let _lock = match app_paths::try_lock_update(&state_dir) {
        Ok(Some(lock)) => lock,
        Ok(None) => {
            eprintln!("update rollback refused: another update is in progress");
            return UPDATE_BUSY;
        }
        Err(reason) => {
            eprintln!("update rollback failed: {reason}");
            return SWAP_FAILED;
        }
    };
    let installed = match update_stage::running_bundle() {
        Ok(installed) => installed,
        Err(reason) => {
            eprintln!("update rollback failed: {reason}");
            return SWAP_FAILED;
        }
    };
    let previous = update_stage::previous_path(&installed);
    match rollback_installed(&installed, &previous, exchange) {
        Ok(()) => SWAPPED,
        Err(SwapError::Swap(reason)) | Err(SwapError::Identity(reason)) => {
            eprintln!("update rollback failed: {reason}");
            SWAP_FAILED
        }
    }
}

fn swap_installed<V, X>(
    installed: &Path,
    staged: &Path,
    verify_identity: V,
    exchange: X,
) -> Result<PathBuf, SwapError>
where
    V: FnOnce(&Path, &Path) -> Result<(), String>,
    X: Fn(&Path, &Path) -> Result<(), String>,
{
    if !staged.is_dir() {
        return Err(SwapError::Swap(format!(
            "no staged update at {}",
            staged.display()
        )));
    }
    let previous = update_stage::previous_path(installed);
    if previous.exists() {
        return Err(SwapError::Swap(format!(
            "{} is still present from an earlier update",
            previous.display()
        )));
    }
    if let Err(reason) = verify_identity(installed, staged) {
        // An untrusted copy is never left where the next update would try it.
        let _ = fs::remove_dir_all(staged);
        return Err(SwapError::Identity(reason));
    }
    exchange(staged, installed).map_err(SwapError::Swap)?;
    // The staged path now holds the old app. Keep it as `.previous`; if that
    // rename fails, exchange back so the caller restarts the old service on
    // the app it stopped.
    if let Err(error) = fs::rename(staged, &previous) {
        return Err(SwapError::Swap(match exchange(staged, installed) {
            Ok(()) => format!("could not keep the previous app ({error}); swapped back"),
            Err(back) => format!(
                "could not keep the previous app ({error}) and could not swap back ({back})"
            ),
        }));
    }
    Ok(previous)
}

fn rollback_installed<X>(
    installed: &Path,
    previous: &Path,
    exchange: X,
) -> Result<(), SwapError>
where
    X: Fn(&Path, &Path) -> Result<(), String>,
{
    if !previous.is_dir() {
        return Err(SwapError::Swap(format!(
            "no previous app at {}",
            previous.display()
        )));
    }
    exchange(installed, previous).map_err(SwapError::Swap)
}

#[cfg(target_os = "macos")]
fn exchange(first: &Path, second: &Path) -> Result<(), String> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};

    let path = |path: &Path| {
        CString::new(path.as_os_str().as_bytes()).map_err(|error| error.to_string())
    };
    let (first_c, second_c) = (path(first)?, path(second)?);
    // SAFETY: both pointers are NUL-terminated strings that outlive the call.
    if unsafe { libc::renamex_np(first_c.as_ptr(), second_c.as_ptr(), libc::RENAME_SWAP) } == 0 {
        Ok(())
    } else {
        Err(format!(
            "could not exchange {} and {}: {}",
            first.display(),
            second.display(),
            std::io::Error::last_os_error()
        ))
    }
}

#[cfg(not(target_os = "macos"))]
fn exchange(_first: &Path, _second: &Path) -> Result<(), String> {
    Err("an atomic app exchange is only available on macOS".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fake_app(path: &Path, marker: &str) {
        fs::create_dir_all(path.join("Contents")).unwrap();
        fs::write(path.join("Contents").join("marker"), marker).unwrap();
    }

    fn marker(path: &Path) -> String {
        fs::read_to_string(path.join("Contents").join("marker")).unwrap()
    }

    fn layout() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let temp = tempfile::tempdir().unwrap();
        let installed = temp.path().join("CORSIA.app");
        let staged = update_stage::staged_path(&installed);
        fake_app(&installed, "old");
        fake_app(&staged, "new");
        (temp, installed, staged)
    }

    #[test]
    fn verified_swap_installs_the_staged_app_and_keeps_the_old_as_previous() {
        let (_temp, installed, staged) = layout();

        let previous = swap_installed(&installed, &staged, |_, _| Ok(()), exchange).unwrap();

        assert_eq!(previous, update_stage::previous_path(&installed));
        assert_eq!(marker(&installed), "new");
        assert_eq!(marker(&previous), "old");
        assert!(!staged.exists());
    }

    #[test]
    fn rollback_restores_the_previous_app_and_keeps_the_failed_update() {
        let (_temp, installed, staged) = layout();
        let previous = swap_installed(&installed, &staged, |_, _| Ok(()), exchange).unwrap();

        rollback_installed(&installed, &previous, exchange).unwrap();

        assert_eq!(marker(&installed), "old");
        assert_eq!(marker(&previous), "new");
    }

    #[test]
    fn identity_failure_leaves_the_installed_app_and_discards_the_staged_copy() {
        let (_temp, installed, staged) = layout();

        let result = swap_installed(&installed, &staged, |_, _| Err("unsigned".into()), |_, _| {
            panic!("an unverified copy must never be exchanged")
        });

        assert!(matches!(result, Err(SwapError::Identity(reason)) if reason == "unsigned"));
        assert_eq!(marker(&installed), "old");
        assert!(!staged.exists());
        assert!(!update_stage::previous_path(&installed).exists());
    }

    #[test]
    fn rollback_without_a_previous_app_refuses_before_an_exchange() {
        let (_temp, installed, _staged) = layout();

        let result = rollback_installed(
            &installed,
            &update_stage::previous_path(&installed),
            |_, _| -> Result<(), String> { panic!("exchanged") },
        );

        assert!(matches!(result, Err(SwapError::Swap(reason)) if reason.contains("no previous app")));
    }

    #[test]
    fn missing_stage_or_leftover_previous_refuses_before_any_exchange() {
        let (_temp, installed, staged) = layout();
        let previous = update_stage::previous_path(&installed);
        fake_app(&previous, "older");
        let never = |_: &Path, _: &Path| -> Result<(), String> { panic!("exchanged") };

        assert!(matches!(
            swap_installed(&installed, &staged, |_, _| Ok(()), never),
            Err(SwapError::Swap(_))
        ));
        fs::remove_dir_all(&previous).unwrap();
        fs::remove_dir_all(&staged).unwrap();
        assert!(matches!(
            swap_installed(&installed, &staged, |_, _| Ok(()), never),
            Err(SwapError::Swap(_))
        ));
        assert_eq!(marker(&installed), "old");
    }

    #[test]
    fn failing_to_keep_the_previous_app_swaps_back() {
        let (temp, installed, staged) = layout();
        // A file where `.previous` must go makes the rename fail after the exchange.
        let previous = update_stage::previous_path(&installed);
        let blocker = temp.path().join("blocker");
        fs::create_dir(&blocker).unwrap();
        fs::write(blocker.join("child"), "x").unwrap();
        let exchanges = std::cell::Cell::new(0);
        let counted = |first: &Path, second: &Path| {
            exchanges.set(exchanges.get() + 1);
            if exchanges.get() == 1 {
                // Plant a non-empty directory at `.previous` after the existence
                // check, so only the post-exchange rename can trip on it.
                fs::rename(&blocker, &previous).unwrap();
            }
            exchange(first, second)
        };

        let result = swap_installed(&installed, &staged, |_, _| Ok(()), counted);

        assert!(matches!(result, Err(SwapError::Swap(reason)) if reason.contains("swapped back")));
        assert_eq!(exchanges.get(), 2);
        assert_eq!(marker(&installed), "old");
        assert_eq!(marker(&staged), "new");
    }

    #[test]
    fn exit_codes_are_stable() {
        assert_eq!(
            (SWAPPED, SWAP_FAILED, IDENTITY_FAILED, UPDATE_BUSY),
            (0, 1, 42, 75)
        );
    }
}
