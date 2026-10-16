use std::{
    fs::{self, File},
    os::unix::io::AsRawFd,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

fn run_binary(binary: &Path, home: &Path, args: &[&str]) -> Output {
    let mut child = Command::new(binary)
        .args(args)
        .env_clear()
        .env("HOME", home)
        .env("TMPDIR", home)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start app binary");
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if child.try_wait().expect("poll app binary").is_some() {
            return child.wait_with_output().expect("collect app binary output");
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("app binary exceeded its 30 second timeout");
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn run_with_timeout(args: &[&str]) -> Output {
    let temp = tempfile::tempdir().expect("create isolated subprocess roots");
    run_binary(Path::new(env!("CARGO_BIN_EXE_corsia-desktop")), temp.path(), args)
}

fn state_dir(home: &Path) -> PathBuf {
    home.join("Library/Application Support/CORSIA/state")
}

/// Hold the update lock the way a running stage or swap does.
fn hold_update_lock(home: &Path) -> File {
    let state = state_dir(home);
    fs::create_dir_all(&state).unwrap();
    let file = File::create(state.join("update.lock")).unwrap();
    // SAFETY: flock only reads the descriptor, which `file` keeps open.
    let held = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    assert_eq!(held, 0, "test could not take the update lock");
    file
}

/// The built binary inside a fake installed app, beside a staged copy.
fn fake_installed_app(root: &Path) -> (PathBuf, PathBuf, PathBuf) {
    let installed = root.join("CORSIA.app");
    let macos = installed.join("Contents/MacOS");
    fs::create_dir_all(&macos).unwrap();
    let binary = macos.join("CORSIA");
    let built = Path::new(env!("CARGO_BIN_EXE_corsia-desktop"));
    if fs::hard_link(built, &binary).is_err() {
        fs::copy(built, &binary).unwrap();
    }
    fs::write(installed.join("Contents/marker"), "installed").unwrap();
    let staged = root.join("CORSIA.app.staged");
    fs::create_dir_all(staged.join("Contents")).unwrap();
    fs::write(staged.join("Contents/marker"), "staged").unwrap();
    (installed, staged, binary)
}

#[test]
fn update_stage_outside_a_bundle_returns_build_failure() {
    let output = run_with_timeout(&["--update-stage", "op-test"]);

    assert_eq!(output.status.code(), Some(42));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("update staging failed"));
    assert!(stderr.contains("CORSIA.app bundle"));
}

#[test]
fn update_modes_without_an_operation_id_return_usage_error() {
    for mode in ["--update-stage", "--update-swap"] {
        let output = run_with_timeout(&[mode]);

        assert_eq!(output.status.code(), Some(64), "{mode}");
        assert!(output.stdout.is_empty(), "{mode}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("usage error"), "{mode}");
    }
}

#[test]
fn update_modes_refuse_while_another_update_holds_the_lock() {
    for mode in ["--update-stage", "--update-swap"] {
        let home = tempfile::tempdir().unwrap();
        let _held = hold_update_lock(home.path());

        let output = run_binary(
            Path::new(env!("CARGO_BIN_EXE_corsia-desktop")),
            home.path(),
            &[mode, "op-test"],
        );

        assert_eq!(output.status.code(), Some(75), "{mode}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("another update is in progress"),
            "{mode}"
        );
    }
}

#[test]
fn update_swap_outside_a_bundle_returns_swap_failure() {
    let output = run_with_timeout(&["--update-swap", "op-test"]);

    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("CORSIA.app bundle"));
}

#[test]
fn update_swap_refuses_an_unsigned_staged_app_and_leaves_the_installed_app() {
    // Same volume as the built binary, so the fake app can hard-link it.
    let root = tempfile::tempdir_in(env!("CARGO_TARGET_TMPDIR")).unwrap();
    let home = tempfile::tempdir().unwrap();
    let (installed, staged, binary) = fake_installed_app(root.path());

    let output = run_binary(&binary, home.path(), &["--update-swap", "op-test"]);

    assert_eq!(output.status.code(), Some(42));
    assert!(String::from_utf8_lossy(&output.stderr).contains("refused the staged app"));
    assert_eq!(
        fs::read_to_string(installed.join("Contents/marker")).unwrap(),
        "installed"
    );
    assert!(installed.join("Contents/MacOS/CORSIA").is_file());
    assert!(!staged.exists());
    assert!(!root.path().join("CORSIA.app.previous").exists());
}
