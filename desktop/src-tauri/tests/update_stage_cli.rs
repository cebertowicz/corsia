use std::{
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

fn run_with_timeout(args: &[&str]) -> Output {
    let temp = tempfile::tempdir().expect("create isolated subprocess roots");
    let mut child = Command::new(env!("CARGO_BIN_EXE_corsia-desktop"))
        .args(args)
        .env_clear()
        .env("HOME", temp.path())
        .env("TMPDIR", temp.path())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start update-stage binary");
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if child.try_wait().expect("poll update-stage binary").is_some() {
            return child.wait_with_output().expect("collect update-stage output");
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("update-stage binary exceeded its 30 second timeout");
        }
        thread::sleep(Duration::from_millis(10));
    }
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
fn update_stage_without_an_operation_id_returns_usage_error() {
    let output = run_with_timeout(&["--update-stage"]);

    assert_eq!(output.status.code(), Some(64));
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr).contains("usage error"));
}
