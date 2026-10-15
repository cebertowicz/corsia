// First-run workspace setup for the packaged app.
//
// On a computer with no CORSIA, the first launch creates the default workspace
// folder, makes it a Git repository, and records the choice in `service.json`
// inside the state folder. The bundled service launcher reads that file, so a
// double-click starts the app with nothing else configured. The shell is the
// only writer of the file.
//
// An existing install (a source install's bootstrap, the legacy state folder,
// or a folder already holding files at the default workspace path) is never
// touched: setup refuses before writing anything, and adopting it is a
// separate journey.

use std::{
    fs,
    io::{ErrorKind, Write},
    path::{Path, PathBuf},
    process::Command,
};

pub const SERVICE_FILE: &str = "service.json";
const WORKSPACE_FOLDER: &str = "corsia";
const LEGACY_STATE_FOLDER: &str = "com.cebertowicz.corsia";
// Entries that do not make a folder "someone's data": the repository a
// previous interrupted first run left behind, and Finder's view settings.
const IGNORED_ENTRIES: [&str; 2] = [".git", ".DS_Store"];

#[derive(Debug, PartialEq, Eq)]
pub enum FirstRun {
    /// `service.json` exists: not a first run, nothing to write.
    Configured,
    /// Nothing from an earlier install: create this workspace and record it.
    Fresh { workspace: PathBuf },
    /// An earlier install is present: write nothing and show this reason.
    ExistingInstall { reason: String },
}

/// The shell's own environment names the workspace or state folder (tests and
/// scripted runs). Those runs never get a default workspace created for them.
pub fn environment_names_the_roots() -> bool {
    ["CORSIA_WORKSPACE", "CORSIA_STATE_DIR"]
        .iter()
        .any(|name| std::env::var_os(name).is_some_and(|value| !value.is_empty()))
}

fn present(path: &Path) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!(
            "could not check whether {} exists: {error}",
            path.display()
        )),
    }
}

fn existing_install(reason: String) -> Result<FirstRun, String> {
    Ok(FirstRun::ExistingInstall {
        reason: format!("{reason} Nothing was changed."),
    })
}

/// Decide what a launch must do about the workspace, reading only.
pub fn plan(home: &Path, state_dir: &Path) -> Result<FirstRun, String> {
    if present(&state_dir.join(SERVICE_FILE))? {
        return Ok(FirstRun::Configured);
    }

    if present(&home.join(".corsia").join("config.json"))? {
        return existing_install(
            "CORSIA found an existing install on this computer (~/.corsia). \
             This app can't open it yet."
                .into(),
        );
    }
    let legacy = home
        .join("Library")
        .join("Application Support")
        .join(LEGACY_STATE_FOLDER);
    if present(&legacy)? {
        return existing_install(format!(
            "CORSIA found data from an earlier version (~/Library/Application Support/{LEGACY_STATE_FOLDER}). \
             This app can't open it yet."
        ));
    }

    // Find the default workspace by name, ignoring case: on a case-insensitive
    // volume `~/corsia` and `~/Corsia` are one folder, and a case-sensitive one
    // can hold a differently cased twin that is just as much someone's work.
    let entries = fs::read_dir(home)
        .map_err(|error| format!("could not read {}: {error}", home.display()))?;
    for entry in entries {
        let entry = entry.map_err(|error| format!("could not read {}: {error}", home.display()))?;
        let name = entry.file_name();
        if !name.to_string_lossy().eq_ignore_ascii_case(WORKSPACE_FOLDER) {
            continue;
        }
        let path = entry.path();
        let shown = format!("~/{}", name.to_string_lossy());
        if !path.is_dir() {
            return existing_install(format!(
                "A file named {shown} is in the way of CORSIA's default workspace folder."
            ));
        }
        let holds_files = fs::read_dir(&path)
            .map_err(|error| format!("could not read {}: {error}", path.display()))?
            .any(|child| {
                child.map_or(true, |child| {
                    !IGNORED_ENTRIES.contains(&child.file_name().to_string_lossy().as_ref())
                })
            });
        if holds_files {
            return existing_install(format!(
                "The folder {shown} already has files in it, so CORSIA left it alone."
            ));
        }
        return Ok(FirstRun::Fresh { workspace: path });
    }
    Ok(FirstRun::Fresh {
        workspace: home.join(WORKSPACE_FOLDER),
    })
}

/// The port an earlier launch recorded, so the app keeps one local address.
pub fn recorded_port(state_dir: &Path) -> Option<u16> {
    let text = fs::read_to_string(state_dir.join(SERVICE_FILE)).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let port = u16::try_from(value.get("port")?.as_u64()?).ok()?;
    (port != 0).then_some(port)
}

/// Whether `xcode-select -p` finds the Command Line Tools. Without them
/// `/usr/bin/git` is Apple's stub, and running it opens the system dialog that
/// offers to install developer tools.
pub fn command_line_tools_installed() -> bool {
    Command::new("/usr/bin/xcode-select")
        .arg("-p")
        .output()
        .is_ok_and(|output| output.status.success())
}

/// `git init` with the same PATH the bundled service runs with. Git is only
/// spawned when the bundle ships one or `command_line_tools` (the probe)
/// reports the Command Line Tools. Without Git the backend still serves and
/// creates the repository on its first guidance save, so a skipped or failed
/// init is a warning for the caller to log, not a startup failure.
pub fn git_init(
    workspace: &Path,
    resources: Option<&Path>,
    command_line_tools: impl FnOnce() -> bool,
) -> Result<(), String> {
    if workspace.join(".git").exists() {
        return Ok(());
    }
    let bundled = resources.is_some_and(|resources| resources.join("bin").join("git").exists());
    if !bundled && !command_line_tools() {
        return Err(
            "git is not available (no bundled git and the Command Line Tools are not installed)"
                .into(),
        );
    }
    let mut path = String::new();
    if let Some(resources) = resources {
        path.push_str(&resources.join("bin").to_string_lossy());
        path.push(':');
    }
    path.push_str("/usr/bin:/bin:/usr/sbin:/sbin");
    let output = Command::new("git")
        .arg("-C")
        .arg(workspace)
        .args(["init", "--quiet"])
        .env("PATH", path)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .output()
        .map_err(|error| format!("could not run git: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "git init exited with {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

fn write_service_file(state_dir: &Path, workspace: &Path, port: u16) -> Result<(), String> {
    let workspace = workspace
        .to_str()
        .ok_or("the workspace path is not valid UTF-8")?;
    let body = serde_json::to_vec_pretty(&serde_json::json!({
        "workspace": workspace,
        "port": port,
    }))
    .map_err(|error| error.to_string())?;

    let target = state_dir.join(SERVICE_FILE);
    let staged = state_dir.join(format!("{SERVICE_FILE}.tmp"));
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&staged)
        .map_err(|error| format!("could not write {}: {error}", staged.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("could not restrict {}: {error}", staged.display()))?;
    }
    file.write_all(&body)
        .and_then(|()| file.sync_all())
        .map_err(|error| format!("could not write {}: {error}", staged.display()))?;
    fs::rename(&staged, &target)
        .map_err(|error| format!("could not write {}: {error}", target.display()))
}

/// Create the workspace, make it a repository, then record it. The record is
/// written last, so an interrupted first run leaves no `service.json` and the
/// next launch plans again from the same markers.
pub fn apply(
    workspace: &Path,
    state_dir: &Path,
    port: u16,
    init_git: impl FnOnce(&Path) -> Result<(), String>,
) -> Result<(), String> {
    fs::create_dir_all(workspace)
        .map_err(|error| format!("could not create {}: {error}", workspace.display()))?;
    if let Err(error) = init_git(workspace) {
        log::warn!(
            "could not make {} a Git repository: {error}",
            workspace.display()
        );
    }
    fs::create_dir_all(state_dir)
        .map_err(|error| format!("could not create {}: {error}", state_dir.display()))?;
    write_service_file(state_dir, workspace, port)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Home(PathBuf);

    impl Home {
        fn new(label: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "corsia-first-run-{label}-{}-{:?}",
                std::process::id(),
                std::time::Instant::now()
            ));
            fs::create_dir_all(&root).expect("create throwaway home");
            Home(root)
        }

        fn state(&self) -> PathBuf {
            self.0
                .join("Library")
                .join("Application Support")
                .join("CORSIA")
        }

        /// Everything under the home, as relative paths, to prove nothing was written.
        fn listing(&self) -> Vec<String> {
            fn walk(base: &Path, dir: &Path, out: &mut Vec<String>) {
                for entry in fs::read_dir(dir).unwrap().flatten() {
                    out.push(
                        entry
                            .path()
                            .strip_prefix(base)
                            .unwrap()
                            .to_string_lossy()
                            .into_owned(),
                    );
                    if entry.path().is_dir() {
                        walk(base, &entry.path(), out);
                    }
                }
            }
            let mut out = Vec::new();
            walk(&self.0, &self.0, &mut out);
            out.sort();
            out
        }
    }

    impl Drop for Home {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn assert_refused(home: &Home, marker: &str) {
        let before = home.listing();
        match plan(&home.0, &home.state()).expect("plan") {
            FirstRun::ExistingInstall { reason } => {
                assert!(reason.contains(marker), "reason names the marker: {reason}");
                assert!(reason.ends_with("Nothing was changed."));
            }
            other => panic!("expected a refusal, got {other:?}"),
        }
        assert_eq!(home.listing(), before, "a refusal writes nothing");
    }

    #[test]
    fn fresh_computer_plans_the_default_workspace_and_writes_it_down() {
        let home = Home::new("fresh");
        assert_eq!(
            plan(&home.0, &home.state()).unwrap(),
            FirstRun::Fresh {
                workspace: home.0.join("corsia")
            }
        );
        assert!(home.listing().is_empty(), "planning writes nothing");

        let workspace = home.0.join("corsia");
        let mut initialised = None;
        apply(&workspace, &home.state(), 47_895, |path| {
            initialised = Some(path.to_path_buf());
            Ok(())
        })
        .unwrap();

        assert_eq!(initialised, Some(workspace.clone()));
        assert!(workspace.is_dir());
        let recorded: serde_json::Value =
            serde_json::from_slice(&fs::read(home.state().join(SERVICE_FILE)).unwrap()).unwrap();
        assert_eq!(recorded["workspace"], workspace.to_str().unwrap());
        assert_eq!(recorded["port"], 47_895);
        assert_eq!(
            recorded.as_object().unwrap().len(),
            2,
            "only workspace and port are recorded"
        );
        assert_eq!(recorded_port(&home.state()), Some(47_895));
        assert!(!home.state().join("service.json.tmp").exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(home.state().join(SERVICE_FILE))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o600);
        }
    }

    #[test]
    fn a_failed_git_init_still_records_the_workspace() {
        let home = Home::new("nogit");
        let workspace = home.0.join("corsia");

        apply(&workspace, &home.state(), 47_895, |_| {
            Err("git is not installed".into())
        })
        .unwrap();

        assert!(home.state().join(SERVICE_FILE).is_file());
    }

    #[test]
    fn git_is_not_spawned_without_a_bundled_git_or_command_line_tools() {
        let home = Home::new("nogit-skip");
        let workspace = home.0.join("corsia");
        fs::create_dir_all(&workspace).unwrap();
        let resources = home.0.join("Resources");
        fs::create_dir_all(resources.join("bin")).unwrap();

        let result = git_init(&workspace, Some(&resources), || false);

        assert!(result.unwrap_err().contains("not available"));
        assert!(!workspace.join(".git").exists());
    }

    #[cfg(unix)]
    #[test]
    fn a_bundled_git_runs_without_asking_the_command_line_tools() {
        use std::os::unix::fs::PermissionsExt;
        let home = Home::new("bundled");
        let workspace = home.0.join("corsia");
        let resources = home.0.join("Resources");
        fs::create_dir_all(&workspace).unwrap();
        fs::create_dir_all(resources.join("bin")).unwrap();
        // A stand-in for the bundled git: it records that it ran as `init`.
        let fake = resources.join("bin").join("git");
        fs::write(&fake, "#!/bin/sh\nmkdir -p \"$2/.git\"\n").unwrap();
        fs::set_permissions(&fake, fs::Permissions::from_mode(0o755)).unwrap();

        git_init(&workspace, Some(&resources), || {
            panic!("the probe must not run when git is bundled")
        })
        .unwrap();

        assert!(workspace.join(".git").is_dir());
    }

    #[test]
    fn an_existing_repository_needs_no_git() {
        let home = Home::new("has-git");
        let workspace = home.0.join("corsia");
        fs::create_dir_all(workspace.join(".git")).unwrap();

        git_init(&workspace, None, || panic!("no probe expected")).unwrap();
    }

    #[test]
    fn source_install_bootstrap_is_an_existing_install() {
        let home = Home::new("bootstrap");
        fs::create_dir_all(home.0.join(".corsia")).unwrap();
        fs::write(home.0.join(".corsia").join("config.json"), "{}").unwrap();

        assert_refused(&home, "~/.corsia");
    }

    #[test]
    fn legacy_state_folder_is_an_existing_install() {
        let home = Home::new("legacy");
        fs::create_dir_all(
            home.0
                .join("Library")
                .join("Application Support")
                .join(LEGACY_STATE_FOLDER),
        )
        .unwrap();

        assert_refused(&home, LEGACY_STATE_FOLDER);
    }

    #[test]
    fn non_empty_default_workspace_is_an_existing_install() {
        let home = Home::new("workspace");
        fs::create_dir_all(home.0.join("corsia")).unwrap();
        fs::write(home.0.join("corsia").join("notes.md"), "mine").unwrap();

        assert_refused(&home, "~/corsia");
    }

    #[test]
    fn differently_cased_workspace_is_the_same_folder() {
        // `~/Corsia` is a live workspace on a case-insensitive volume, and
        // the same name to us wherever the volume is case-sensitive.
        let home = Home::new("case");
        fs::create_dir_all(home.0.join("Corsia").join("projects")).unwrap();

        assert_refused(&home, "~/Corsia");
    }

    #[test]
    fn an_empty_or_git_only_workspace_is_reused_under_its_own_name() {
        let home = Home::new("reuse");
        fs::create_dir_all(home.0.join("Corsia").join(".git")).unwrap();
        fs::write(home.0.join("Corsia").join(".DS_Store"), "").unwrap();

        assert_eq!(
            plan(&home.0, &home.state()).unwrap(),
            FirstRun::Fresh {
                workspace: home.0.join("Corsia")
            }
        );
    }

    #[test]
    fn a_file_in_the_workspace_name_is_refused() {
        let home = Home::new("file");
        fs::write(home.0.join("corsia"), "not a folder").unwrap();

        assert_refused(&home, "~/corsia");
    }

    #[test]
    fn an_existing_service_file_wins_over_every_marker() {
        let home = Home::new("configured");
        fs::create_dir_all(home.state()).unwrap();
        fs::write(
            home.state().join(SERVICE_FILE),
            r#"{"workspace": "/somewhere/else", "port": 5123}"#,
        )
        .unwrap();
        fs::create_dir_all(home.0.join(".corsia")).unwrap();
        fs::write(home.0.join(".corsia").join("config.json"), "{}").unwrap();
        fs::create_dir_all(home.0.join("corsia")).unwrap();
        fs::write(home.0.join("corsia").join("notes.md"), "mine").unwrap();
        let before = home.listing();

        assert_eq!(
            plan(&home.0, &home.state()).unwrap(),
            FirstRun::Configured
        );
        assert_eq!(recorded_port(&home.state()), Some(5123));
        assert_eq!(home.listing(), before);
    }

    #[test]
    fn recorded_port_ignores_a_damaged_file() {
        let home = Home::new("damaged");
        fs::create_dir_all(home.state()).unwrap();
        for text in ["", "not json", r#"{"port": 0}"#, r#"{"port": 70000}"#, r#"{"port": "80"}"#] {
            fs::write(home.state().join(SERVICE_FILE), text).unwrap();
            assert_eq!(recorded_port(&home.state()), None, "{text}");
        }
    }
}
