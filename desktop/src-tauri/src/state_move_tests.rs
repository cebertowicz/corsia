use super::*;
use std::{os::unix::fs::PermissionsExt, process::Child};

/// An adopted install as the source left it: state under the old identifier,
/// pinned by service.json, the bootstrap config and the adopted launchd files.
fn install(home: &Path) -> Paths {
    let app_dir = home.join("Library/Application Support/CORSIA");
    let paths = Paths::for_home(home, &app_dir);
    fs::create_dir_all(&paths.legacy).unwrap();
    fs::write(paths.legacy.join("corsia.db"), b"db").unwrap();
    fs::create_dir_all(paths.legacy.join("workspace")).unwrap();
    fs::write(paths.legacy.join("workspace/notes.md"), b"kept").unwrap();
    fs::write(paths.legacy_parent().join("person-file.txt"), b"theirs").unwrap();
    pin(home, &paths);
    paths
}

/// The files outside the state root that name it, as adoption leaves them.
fn pin(home: &Path, paths: &Paths) {
    let app_dir = &paths.app_dir;
    fs::create_dir_all(app_dir).unwrap();
    let legacy = paths.legacy.to_str().unwrap();
    fs::write(
        app_dir.join(SERVICE_FILE),
        serde_json::to_vec(&serde_json::json!({"state_dir": legacy, "port": 47895})).unwrap(),
    )
    .unwrap();
    fs::create_dir_all(paths.bootstrap_config.parent().unwrap()).unwrap();
    // A version 2 bootstrap, the only kind that names a state root outside the workspace.
    fs::write(
        &paths.bootstrap_config,
        serde_json::to_vec(&serde_json::json!({
            "version": 2,
            "workspace": {"id": "w1", "name": "Corsia", "root": home.join("Corsia")},
            "state_root": legacy,
        }))
        .unwrap(),
    )
    .unwrap();
    let adopted = app_dir.join("adopted");
    fs::create_dir_all(&adopted).unwrap();
    fs::write(adopted.join("com.corsia.prod.plist"), format!("<string>{legacy}</string>\n<string>{legacy}2</string>\n")).unwrap();
    fs::write(adopted.join("service.env"), format!("CORSIA_STATE_ROOT={legacy}\n")).unwrap();
    // A finished adoption's journal, as `adoption.rs` leaves it.
    let journal = serde_json::json!({
        "step": "registered",
        "source": {
            "plist": home.join("Library/LaunchAgents/com.corsia.prod.plist"),
            "state": legacy,
            "workspace": format!("{legacy}/workspace"),
            "port": 47895,
            "working_directory": "/src/corsia",
            "cli": "/src/corsia/scripts/corsia",
            "env_file": home.join(".corsia/service.env"),
            "plist_variable_names": ["CORSIA_STATE_ROOT"],
        },
        "snapshot": null,
    });
    fs::write(adopted.join("adoption.json"), serde_json::to_vec(&journal).unwrap()).unwrap();
    let runtime = paths.old_cache.join("runtime/bin");
    fs::create_dir_all(&runtime).unwrap();
    fs::write(runtime.join("python3"), b"").unwrap();
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o555)).unwrap();
}

#[derive(Default)]
struct Fake {
    calls: Vec<&'static str>,
    active: bool,
    stop_fails: bool,
    /// Scan the real process table rather than report no holders.
    real_scan: bool,
    /// Holders to report instead of none.
    held_by: Vec<Holder>,
    /// Run during `prove_released`, after the recorded-process stop.
    on_release: Option<Box<dyn FnMut()>>,
    on_recorded: Option<Box<dyn FnMut()>>,
}

impl Service for Fake {
    fn operation_active(&mut self) -> Result<bool, String> {
        self.calls.push("operation_active");
        Ok(self.active)
    }
    fn stop(&mut self) -> Result<(), String> {
        self.calls.push("stop");
        if self.stop_fails { Err("stop-local-model failed".into()) } else { Ok(()) }
    }
    fn unregister(&mut self) -> Result<(), String> {
        self.calls.push("unregister");
        Ok(())
    }
    fn stop_recorded_processes(&mut self, _state: &Path) -> Result<(), String> {
        self.calls.push("stop_recorded_processes");
        if let Some(hook) = self.on_recorded.as_mut() {
            hook();
        }
        Ok(())
    }
    fn prove_released(&mut self, _state: &Path) -> Result<(), String> {
        self.calls.push("prove_released");
        if let Some(hook) = self.on_release.as_mut() {
            hook();
        }
        Ok(())
    }
    fn holders(&mut self, state: &Path) -> Result<Vec<Holder>, String> {
        if self.real_scan { holders(state) } else { Ok(self.held_by.clone()) }
    }
}

fn run_with(paths: &Paths, fake: &mut Fake) -> (Result<Outcome, MoveError>, Vec<u8>) {
    let mut seen = Vec::new();
    let outcome = run(paths, fake, &mut |percent| seen.push(percent));
    (outcome, seen)
}

fn read(path: &Path) -> String {
    fs::read_to_string(path).unwrap()
}

fn assert_moved(paths: &Paths) {
    let (legacy, target) = (paths.legacy.to_str().unwrap(), paths.target.to_str().unwrap());
    assert!(!exists(paths.legacy_parent()), "the old folder is gone");
    assert_eq!(read(&paths.target.join("corsia.db")), "db");
    assert_eq!(read(&paths.target.join("workspace/notes.md")), "kept");
    assert_eq!(read(&paths.app_dir.join("earlier-app-folder/person-file.txt")), "theirs");
    assert!(!paths.journal().exists());
    let service: serde_json::Value = serde_json::from_str(&read(&paths.app_dir.join(SERVICE_FILE))).unwrap();
    assert_eq!(service["state_dir"], target);
    assert_eq!(service["port"], 47895);
    let config: serde_json::Value = serde_json::from_str(&read(&paths.bootstrap_config)).unwrap();
    assert_eq!(config["state_root"], target);
    assert_eq!(config["version"], 2);
    assert_eq!(config["workspace"]["root"], home_of(paths).join("Corsia").to_str().unwrap());
    let adopted = paths.app_dir.join("adopted");
    // Only a whole path segment is rewritten; `…/state2` is another folder.
    assert_eq!(
        read(&adopted.join("com.corsia.prod.plist")),
        format!("<string>{target}</string>\n<string>{legacy}2</string>\n")
    );
    assert_eq!(read(&adopted.join("service.env")), format!("CORSIA_STATE_ROOT={target}\n"));
    assert!(read(&adopted.join("adoption.json")).contains(target));
}

fn home_of(paths: &Paths) -> PathBuf {
    paths.bootstrap_config.parent().and_then(Path::parent).unwrap().to_path_buf()
}

fn assert_unchanged(paths: &Paths) {
    assert_eq!(read(&paths.legacy.join("corsia.db")), "db");
    assert!(!paths.journal().exists(), "a skip clears the journal so the launcher starts");
    assert!(service_file_names(paths, &paths.legacy).unwrap());
}

fn wait_for_cache_removal(paths: &Paths) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while exists(&paths.old_cache) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    assert!(!exists(&paths.old_cache), "the old read-only cache is deleted");
}

#[test]
fn moves_an_adopted_install_and_repoints_every_file_that_names_it() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let mut fake = Fake::default();

    let (outcome, seen) = run_with(&paths, &mut fake);

    assert_eq!(outcome, Ok(Outcome::Moved));
    assert_eq!(fake.calls, ["operation_active", "stop", "unregister", "stop_recorded_processes", "prove_released"]);
    assert!(seen.windows(2).all(|pair| pair[0] <= pair[1]), "progress only rises: {seen:?}");
    assert_eq!(seen.last(), Some(&95));
    assert_moved(&paths);
    // T11: this resolver is unchanged from the build before the move, so an older
    // build opened on a moved install starts on the new folder too.
    assert_eq!(app_paths::state_dir_with_override(&paths.app_dir, None), Ok(paths.target.clone()));
    wait_for_cache_removal(&paths);
    assert_eq!(check_legacy_gone(&paths), Ok(()));
    assert_eq!(run_with(&paths, &mut Fake::default()).0, Ok(Outcome::NotNeeded), "a second launch has nothing to do");
}

#[test]
fn a_fresh_or_already_moved_install_needs_no_move() {
    let home = tempfile::tempdir().unwrap();
    let app_dir = home.path().join("Library/Application Support/CORSIA");
    fs::create_dir_all(&app_dir).unwrap();
    let paths = Paths::for_home(home.path(), &app_dir);
    let mut fake = Fake::default();
    assert_eq!(run_with(&paths, &mut fake).0, Ok(Outcome::NotNeeded));

    fs::write(app_dir.join(SERVICE_FILE), format!(r#"{{"state_dir":"{}"}}"#, paths.target.display())).unwrap();
    assert_eq!(run_with(&paths, &mut fake).0, Ok(Outcome::NotNeeded));
    assert!(fake.calls.is_empty(), "nothing is stopped when nothing moves");
}

#[test]
fn an_unfinished_start_fresh_goes_first() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    fs::write(paths.app_dir.join("start-fresh.json"), b"{}").unwrap();
    let mut fake = Fake::default();
    assert_eq!(run_with(&paths, &mut fake).0, Ok(Outcome::NotNeeded));
    assert!(fake.calls.is_empty());
    assert_eq!(read(&paths.legacy.join("corsia.db")), "db");
}

// T4: a target that already holds files is never merged into or replaced.
#[test]
fn a_target_that_holds_files_skips_the_move_and_changes_nothing() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    fs::create_dir_all(&paths.target).unwrap();
    fs::write(paths.target.join("corsia.db"), b"other").unwrap();
    let mut fake = Fake::default();

    let (outcome, seen) = run_with(&paths, &mut fake);

    assert!(matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains("already holds files")), "{outcome:?}");
    assert!(fake.calls.is_empty(), "the service is not stopped for a move that can't happen");
    assert!(seen.is_empty(), "no move window for a move that can't happen: {seen:?}");
    assert_unchanged(&paths);
    assert_eq!(read(&paths.target.join("corsia.db")), "other");
}

#[test]
fn an_empty_target_folder_is_replaced_by_the_move() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    fs::create_dir_all(&paths.target).unwrap();
    assert_eq!(run_with(&paths, &mut Fake::default()).0, Ok(Outcome::Moved));
    assert_moved(&paths);
}

#[test]
fn a_target_filled_after_the_check_is_not_overwritten() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let target = paths.target.clone();
    let mut fake = Fake {
        on_release: Some(Box::new(move || {
            fs::create_dir_all(&target).unwrap();
            fs::write(target.join("raced"), b"x").unwrap();
        })),
        ..Fake::default()
    };

    let (outcome, _) = run_with(&paths, &mut fake);

    assert!(matches!(outcome, Ok(Outcome::Skipped(_))), "{outcome:?}");
    assert_unchanged(&paths);
    assert_eq!(read(&paths.target.join("raced")), "x");
}

// T5: a linked old folder stays put; following the link would move the wrong thing.
#[test]
fn a_linked_old_folder_skips_the_move() {
    let home = tempfile::tempdir().unwrap();
    let elsewhere = home.path().join("elsewhere");
    let paths = install(&elsewhere);
    let app_dir = home.path().join("Library/Application Support/CORSIA");
    fs::create_dir_all(app_dir.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(elsewhere.join("Library/Application Support/CORSIA"), &app_dir).unwrap();
    std::os::unix::fs::symlink(
        paths.legacy_parent(),
        home.path().join("Library/Application Support").join(first_run::LEGACY_STATE_FOLDER),
    )
    .unwrap();
    let linked = Paths::for_home(home.path(), &app_dir);
    fs::write(
        app_dir.join(SERVICE_FILE),
        format!(r#"{{"state_dir":"{}"}}"#, linked.legacy.display()),
    )
    .unwrap();
    let mut fake = Fake::default();

    let (outcome, _) = run_with(&linked, &mut fake);

    assert!(matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains("link")), "{outcome:?}");
    assert!(fake.calls.is_empty());
    assert_eq!(read(&paths.legacy.join("corsia.db")), "db");
}

/// A sandbox that refuses the process list can't show a holder; the refusal itself is tested below.
fn can_list_processes() -> bool {
    let listed = holders(Path::new("/")).is_ok();
    if !listed {
        eprintln!("skipped: this sandbox refuses the process list");
    }
    listed
}

#[test]
fn a_refused_process_list_skips_the_move() {
    if can_list_processes() {
        return;
    }
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let (outcome, _) = run_with(&paths, &mut Fake { real_scan: true, ..Fake::default() });
    assert!(matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains("could not check")), "{outcome:?}");
    assert_unchanged(&paths);
}

fn hold(dir: &Path, script: &str) -> Child {
    let child = Command::new("/bin/sh").arg("-c").arg(script).current_dir(dir).spawn().unwrap();
    // Wait until the shell has opened what it holds.
    let deadline = Instant::now() + Duration::from_secs(5);
    while holders(dir).unwrap().iter().all(|holder| holder.pid != child.id() as i32) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    child
}

// T6: an unknown process with the folder open, or as its working folder, refuses the move.
#[test]
fn a_process_holding_the_old_folder_refuses_the_move_and_is_named() {
    if !can_list_processes() {
        return;
    }
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let workspace = paths.legacy.join("workspace");
    for script in ["exec 3<>../corsia.db-wal; exec sleep 30", "exec sleep 30"] {
        let mut child = hold(&workspace, script);
        let mut fake = Fake { real_scan: true, ..Fake::default() };

        let (outcome, _) = run_with(&paths, &mut fake);

        child.kill().unwrap();
        child.wait().unwrap();
        assert!(
            matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains("sleep") && skip.detail.contains(&child.id().to_string())),
            "{script}: {outcome:?}"
        );
        assert_unchanged(&paths);
    }
}

#[test]
fn a_recorded_managed_process_is_stopped_before_the_holder_check() {
    if !can_list_processes() {
        return;
    }
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let child = std::rc::Rc::new(std::cell::RefCell::new(hold(&paths.legacy, "exec sleep 30")));
    let stopped = child.clone();
    let mut fake = Fake {
        on_recorded: Some(Box::new(move || {
            let mut child = stopped.borrow_mut();
            child.kill().unwrap();
            child.wait().unwrap();
        })),
        real_scan: true,
        ..Fake::default()
    };

    assert_eq!(run_with(&paths, &mut fake).0, Ok(Outcome::Moved));
    assert_moved(&paths);
    drop(child);
}

// T14: an update holding the lock, or an operation in flight, waits for a later launch.
#[test]
fn a_held_update_lock_or_an_active_operation_skips_the_move() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());

    let held = app_paths::try_lock_update_in_existing(&paths.legacy).unwrap().unwrap();
    let mut fake = Fake::default();
    let (outcome, seen) = run_with(&paths, &mut fake);
    assert!(matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains("update")), "{outcome:?}");
    assert!(fake.calls.is_empty());
    // Nothing was stopped, so the move window never showed.
    assert!(seen.is_empty(), "{seen:?}");
    drop(held);

    let mut fake = Fake { active: true, ..Fake::default() };
    let (outcome, seen) = run_with(&paths, &mut fake);
    assert!(matches!(outcome, Ok(Outcome::Skipped(_))), "{outcome:?}");
    assert_eq!(fake.calls, ["operation_active"]);
    assert!(seen.is_empty(), "{seen:?}");
    assert_unchanged(&paths);
}

// An unknown port or bundle is never guessed: the move skips before it stops anything.
#[test]
fn an_unknown_port_or_bundle_skips_the_move_before_stopping_anything() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    for (mut service, said) in [
        (AppService::new(Ok(home.path().join("resources")), None), "port"),
        (AppService::new(Err("no resource folder".into()), Some(1956)), "bundled files"),
    ] {
        let outcome = run(&paths, &mut service, &mut |_| {});
        assert!(matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains(said)), "{outcome:?}");
        assert_unchanged(&paths);
        assert!(!paths.app_dir.join(JOURNAL).exists());
    }
}

// A skip the person watched start tells them why: a terminal, a named or unnamed app, or only that it
// tries again; past the recorded-process stop it also says background jobs stopped.
#[test]
fn a_skip_after_the_stop_says_why_and_one_before_it_says_nothing() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let held = |holders: &[(&str, Option<&str>)]| -> Vec<Holder> {
        holders
            .iter()
            .enumerate()
            .map(|(pid, (name, app))| Holder { pid: pid as i32 + 100, name: name.to_string(), app: app.map(str::to_string) })
            .collect()
    };
    let finder = Some("Finder");
    for (held_by, reason, app) in [
        (held(&[("zsh", None)]), SkipReason::Terminal, None),
        (held(&[("zsh", None), ("bash", None)]), SkipReason::Terminal, None),
        (held(&[("Finder", finder)]), SkipReason::OtherApp, Some("Finder")),
        (held(&[("zsh", None), ("Finder", finder), ("Finder", finder)]), SkipReason::OtherApp, Some("Finder")),
        (held(&[("Finder", finder), ("Preview", Some("Preview"))]), SkipReason::OtherApp, None),
        (held(&[("python3", None)]), SkipReason::OtherApp, None),
    ] {
        let (outcome, _) = run_with(&paths, &mut Fake { held_by, ..Fake::default() });
        let expected = Told { reason, app: app.map(str::to_string), jobs_stopped: true };
        assert!(matches!(outcome, Ok(Outcome::Skipped(Skip { tell: Some(ref t), .. })) if *t == expected), "{outcome:?}");
        assert_unchanged(&paths);
    }

    // A failed stop leaves the service, and so every background job, running.
    let (outcome, _) = run_with(&paths, &mut Fake { stop_fails: true, ..Fake::default() });
    let expected = Told { reason: SkipReason::Other, app: None, jobs_stopped: false };
    assert!(matches!(outcome, Ok(Outcome::Skipped(Skip { tell: Some(ref t), .. })) if *t == expected), "{outcome:?}");

    let (outcome, _) = run_with(&paths, &mut Fake { active: true, ..Fake::default() });
    assert!(matches!(outcome, Ok(Outcome::Skipped(Skip { tell: None, .. }))), "{outcome:?}");
}

#[test]
fn an_app_is_named_from_its_bundle_folder() {
    let name = |path: &str| bundle_name(Path::new(path));
    assert_eq!(name("/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder").as_deref(), Some("Finder"));
    assert_eq!(
        name("/Applications/Google Chrome.app/Contents/Frameworks/Helpers/Google Chrome Helper.app/Contents/MacOS/x").as_deref(),
        Some("Google Chrome")
    );
    assert_eq!(name("/opt/homebrew/bin/python3"), None);
    assert_eq!(name("/Users/you/.app/bin/tool"), None);
}

// T16 (shell side is in test_service_scripts.py): a failed stop leaves everything in place.
#[test]
fn a_failed_stop_skips_the_move_without_unregistering() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    let mut fake = Fake { stop_fails: true, ..Fake::default() };

    let (outcome, _) = run_with(&paths, &mut fake);

    assert!(matches!(outcome, Ok(Outcome::Skipped(ref skip)) if skip.detail.contains("stop-local-model")), "{outcome:?}");
    assert_eq!(fake.calls, ["operation_active", "stop"]);
    assert_unchanged(&paths);
}

#[test]
fn data_in_both_folders_refuses_to_start() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    fs::create_dir_all(&paths.target).unwrap();
    fs::write(paths.target.join("corsia.db"), b"other").unwrap();
    write_journal(&paths, Step::Stopped).unwrap();

    let outcome = run_with(&paths, &mut Fake::default()).0;

    assert!(matches!(outcome, Err(ref error) if error.line == TWO_PLACES && error.detail.contains("two places")), "{outcome:?}");
    assert_eq!(read(&paths.legacy.join("corsia.db")), "db");
    assert_eq!(read(&paths.target.join("corsia.db")), "other");
}

#[test]
fn a_recreated_old_folder_is_reported_by_count_only() {
    let home = tempfile::tempdir().unwrap();
    let paths = install(home.path());
    assert_eq!(run_with(&paths, &mut Fake::default()).0, Ok(Outcome::Moved));
    fs::create_dir_all(&paths.legacy).unwrap();
    fs::write(paths.legacy.join("private-name.db"), b"").unwrap();

    let message = check_legacy_gone(&paths).unwrap_err();

    assert!(message.contains("(1 items)"), "{message}");
    assert!(!message.contains("private-name"));
}

const KILL_HOME: &str = "CORSIA_STATE_MOVE_KILL_HOME";

/// The child half of the kill test: run the move in the given home until the
/// checkpoint named in `CORSIA_STATE_MOVE_KILL_AT` kills this process.
#[test]
fn kill_child_runs_the_move() {
    let Some(home) = std::env::var_os(KILL_HOME) else { return };
    let home = PathBuf::from(home);
    let paths = Paths::for_home(&home, &home.join("Library/Application Support/CORSIA"));
    let _ = run(&paths, &mut Fake::default(), &mut |_| {});
    panic!("the move ran past its kill point");
}

// T3: a kill at any step leaves an install the next launch finishes, never one split in two.
#[test]
fn a_kill_at_any_step_is_finished_by_the_next_launch() {
    use std::os::unix::process::ExitStatusExt;

    for point in ["stopping", "stopped", "renamed", "moved", "committed", "repointed", "tidied"] {
        let home = tempfile::tempdir().unwrap();
        let paths = install(home.path());
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "state_move::tests::kill_child_runs_the_move", "--test-threads=1", "--nocapture"])
            .env(KILL_HOME, home.path())
            .env("CORSIA_STATE_MOVE_KILL_AT", point)
            .status()
            .unwrap();
        assert_eq!(status.signal(), Some(libc::SIGKILL), "{point}: the child died at its kill point");
        assert!(paths.journal().exists(), "{point}: the journal makes the launcher wait");

        let (outcome, _) = run_with(&paths, &mut Fake::default());

        assert!(matches!(outcome, Ok(Outcome::Moved) | Ok(Outcome::Skipped(_))), "{point}: {outcome:?}");
        if outcome == Ok(Outcome::Moved) {
            assert_moved(&paths);
        } else {
            // Killed before the rename: the next launch starts on the old folder and moves later.
            assert_unchanged(&paths);
            assert_eq!(run_with(&paths, &mut Fake::default()).0, Ok(Outcome::Moved), "{point}");
            assert_moved(&paths);
        }
    }
}

#[test]
fn rebased_replaces_only_whole_path_segments() {
    let old = "/h/com.cebertowicz.corsia/state";
    assert_eq!(rebased(&format!("{old}/x {old}2 \"{old}\""), old, "/h/CORSIA/state"), "/h/CORSIA/state/x /h/com.cebertowicz.corsia/state2 \"/h/CORSIA/state\"");
}

/// T12 scale run. Point `CORSIA_T12_HOME` at a disposable HOME whose legacy
/// root holds real data and `CORSIA_T12_PYTHON` at a Python; prints step times.
#[test]
#[ignore = "scale run: needs CORSIA_T12_HOME with real data"]
fn moves_a_large_install_in_constant_time() {
    let home = PathBuf::from(std::env::var("CORSIA_T12_HOME").unwrap());
    let python = PathBuf::from(std::env::var("CORSIA_T12_PYTHON").unwrap());
    let paths = Paths::for_home(&home, &home.join("Library/Application Support/CORSIA"));
    pin(&home, &paths);
    let db = paths.legacy.join("corsia.db");
    let released = std::rc::Rc::new(std::cell::Cell::new(Duration::ZERO));
    let seen = released.clone();
    let mut fake = Fake {
        on_release: Some(Box::new(move || {
            let start = Instant::now();
            crate::release_proof::database_released(&python, &db).unwrap();
            seen.set(start.elapsed());
        })),
        ..Fake::default()
    };
    let start = Instant::now();
    let (outcome, _) = run_with(&paths, &mut fake);
    let total = start.elapsed();
    assert!(outcome.is_ok(), "{outcome:?}");
    assert!(!exists(paths.legacy_parent()) && paths.target.join("corsia.db").exists());
    println!("T12 release proof {:?}, whole move {:?}", released.get(), total);
    wait_for_cache_removal(&paths);
}

// The window reads these exact keys from the phase; renaming one would show the wrong line.
#[test]
fn a_skipped_phase_carries_what_the_window_reads() {
    let told = Told { reason: SkipReason::OtherApp, app: Some("Finder".into()), jobs_stopped: true };
    let json = serde_json::to_value(crate::adoption::Phase::MoveSkipped { told }).unwrap();
    assert_eq!(
        json,
        serde_json::json!({ "phase": "move_skipped", "reason": "other_app", "app": "Finder", "jobs_stopped": true })
    );
}
