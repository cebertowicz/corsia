// First-run workspace setup for the packaged app.
//
// On a computer with no CORSIA, the first launch creates the default workspace
// folder, makes it a Git repository, and records the choice in `service.json`
// inside the app folder. The bundled service launcher reads that file, so a
// double-click starts the app with nothing else configured. The shell is the
// only writer of the file.
//
// An existing install (a source install's bootstrap, the legacy state folder,
// or a folder already holding files at the default workspace path) is never
// touched here: setup refuses before writing anything. Adopting it and
// setting it aside (`start_fresh`) are separate journeys the person chooses.

use std::{
    fs,
    io::{ErrorKind, Write},
    path::{Path, PathBuf},
    process::Command,
};

use serde::Serialize;

use crate::adoption;

pub const SERVICE_FILE: &str = "service.json";
pub(crate) const WORKSPACE_FOLDER: &str = "CORSIA";
pub(crate) const LEGACY_STATE_FOLDER: &str = "com.cebertowicz.corsia";
// Finder's view settings do not make a folder someone's data. A `.git`
// directory is ignored only when it has no branch refs or packed branch refs.
const IGNORED_ENTRIES: [&str; 1] = [".DS_Store"];

#[derive(Debug, PartialEq, Eq)]
pub enum FirstRun {
    /// `service.json` exists: not a first run, nothing to write.
    Configured,
    /// Nothing from an earlier install: create this workspace and record it.
    Fresh { workspace: PathBuf },
    /// An earlier install is present but does not meet the source-service
    /// contract required for adoption: write nothing and offer Start fresh with this reason.
    /// `details` carries the runtime cause, shown only behind Details.
    ExistingInstall { reason: String, details: String },
    /// A rendered source service plist identifies a moveable source install.
    Adoptable { source: adoption::SourceInstall },
}

#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum FolderClassification {
    Ready { path: String, folder_warning: Option<FolderWarning> },
    Unwritable { path: String, reason: String },
    Unreadable { path: String, reason: String },
}

#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum FolderWarning {
    PersonalFolder { folder: &'static str },
    ContainsPersonalFolder { folder: &'static str },
    ContainsFiles,
}

fn folder_warning(path: &Path, home: &Path) -> Result<Option<FolderWarning>, String> {
    if path == home {
        return Ok(Some(FolderWarning::PersonalFolder { folder: "Home" }));
    }

    for (personal, name) in [
        (home.join("Documents"), "Documents"),
        (home.join("Desktop"), "Desktop"),
        (home.join("Downloads"), "Downloads"),
        (
            home.join("Library")
                .join("Mobile Documents")
                .join("com~apple~CloudDocs"),
            "iCloud Drive",
        ),
    ] {
        if path == personal {
            return Ok(Some(FolderWarning::PersonalFolder { folder: name }));
        }
        if personal.starts_with(path) {
            return Ok(Some(FolderWarning::ContainsPersonalFolder { folder: name }));
        }
    }

    workspace_is_fresh(path).map(|fresh| (!fresh).then_some(FolderWarning::ContainsFiles))
}

pub fn classify_folder(
    path: &Path,
    home: &Path,
    check_writable: impl FnOnce(&Path) -> Result<(), String>,
) -> FolderClassification {
    let shown = path.display().to_string();
    let resolved_path = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    let resolved_home = home.canonicalize().unwrap_or_else(|_| home.to_path_buf());
    match check_writable(path) {
        Ok(()) => match folder_warning(&resolved_path, &resolved_home) {
            Ok(folder_warning) => FolderClassification::Ready { path: shown, folder_warning },
            Err(reason) => FolderClassification::Unreadable { path: shown, reason },
        },
        Err(reason) => FolderClassification::Unwritable { path: shown, reason },
    }
}

pub fn ensure_folder_writable(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path)
        .map_err(|error| format!("could not create {}: {error}", path.display()))?;
    let probe = path.join(format!(".corsia-write-check-{}", std::process::id()));
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)
        .and_then(|file| file.sync_all())
        .map_err(|error| format!("could not write {}: {error}", path.display()))?;
    fs::remove_file(&probe)
        .map_err(|error| format!("could not finish checking {}: {error}", path.display()))
}

fn directory_has_files(path: &Path) -> Result<bool, String> {
    for entry in fs::read_dir(path)
        .map_err(|error| format!("could not read {}: {error}", path.display()))?
    {
        let entry = entry.map_err(|error| format!("could not read {}: {error}", path.display()))?;
        if entry.path().is_dir() {
            if directory_has_files(&entry.path())? {
                return Ok(true);
            }
        } else {
            return Ok(true);
        }
    }
    Ok(false)
}

fn git_has_no_commits(git_dir: &Path) -> Result<bool, String> {
    let heads = git_dir.join("refs").join("heads");
    match fs::metadata(&heads) {
        Ok(metadata) => {
            if !metadata.is_dir() || directory_has_files(&heads)? {
                return Ok(false);
            }
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(format!("could not read {}: {error}", heads.display())),
    }
    let packed_refs = git_dir.join("packed-refs");
    let packed_refs = match fs::read_to_string(&packed_refs) {
        Ok(contents) => contents,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(true),
        Err(error) => return Err(format!("could not read {}: {error}", packed_refs.display())),
    };
    Ok(!packed_refs
        .lines()
        .filter_map(|line| line.split_whitespace().nth(1))
        .any(|reference| reference.starts_with("refs/heads/")))
}

pub(crate) fn workspace_is_fresh(path: &Path) -> Result<bool, String> {
    for entry in fs::read_dir(path)
        .map_err(|error| format!("could not read {}: {error}", path.display()))?
    {
        let entry =
            entry.map_err(|error| format!("could not read {}: {error}", path.display()))?;
        if IGNORED_ENTRIES.contains(&entry.file_name().to_string_lossy().as_ref()) {
            continue;
        }
        if entry.file_name() != ".git"
            || !entry.path().is_dir()
            || !git_has_no_commits(&entry.path())?
        {
            return Ok(false);
        }
    }
    Ok(true)
}

pub fn remove_default_folder_if_fresh(
    default_folder: &Path,
    chosen_folder: &Path,
) -> Result<bool, String> {
    if default_folder == chosen_folder || !default_folder.is_dir() {
        return Ok(false);
    }
    let mut entries = fs::read_dir(default_folder)
        .map_err(|error| format!("could not read {}: {error}", default_folder.display()))?;
    let Some(entry) = entries
        .next()
        .transpose()
        .map_err(|error| format!("could not read {}: {error}", default_folder.display()))?
    else {
        return Ok(false);
    };
    if entry.file_name() != ".git"
        || !entry.path().is_dir()
        || entries.next().is_some()
        || !git_has_no_commits(&entry.path())?
    {
        return Ok(false);
    }
    fs::remove_dir_all(default_folder)
        .map_err(|error| format!("could not remove {}: {error}", default_folder.display()))?;
    Ok(true)
}

/// The shell's own environment names the workspace or state folder (tests and
/// scripted runs). Those runs never get a default workspace created for them.
pub fn environment_names_the_roots() -> bool {
    ["CORSIA_WORKSPACE", "CORSIA_STATE_DIR"]
        .iter()
        .any(|name| std::env::var_os(name).is_some_and(|value| !value.is_empty()))
}

pub(crate) fn present(path: &Path) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!(
            "could not check whether {} exists: {error}",
            path.display()
        )),
    }
}

pub fn default_workspace(home: &Path) -> PathBuf {
    home.join(WORKSPACE_FOLDER)
}

fn existing_install(reason: String) -> Result<FirstRun, String> {
    Ok(FirstRun::ExistingInstall { reason, details: String::new() })
}

/// Decide what a launch must do about the workspace, reading only.
pub fn plan(home: &Path, app_dir: &Path) -> Result<FirstRun, String> {
    plan_trusting(home, app_dir, None)
}

/// `plan`, for a launch that just started an unfinished setup over: `trusted` is the work folder that setup
/// made, and a default folder at exactly that path is reused even though the backend left files in it. Nothing
/// else about the refusal rules changes.
pub fn plan_trusting(home: &Path, app_dir: &Path, trusted: Option<&Path>) -> Result<FirstRun, String> {
    if present(&app_dir.join(SERVICE_FILE))? {
        return Ok(FirstRun::Configured);
    }

    let source_plist = home.join("Library").join("LaunchAgents").join("com.corsia.prod.plist");
    if present(&source_plist)? {
        // A plist this app cannot trust is not adopted, and no other path is tried.
        return match adoption::source_from_plist(home, &source_plist) {
            Ok(source) => Ok(FirstRun::Adoptable { source }),
            Err(details) => Ok(FirstRun::ExistingInstall {
                reason: "CORSIA found an existing install on this computer, and this app can’t take it over.".into(),
                details,
            }),
        };
    }

    if present(&home.join(".corsia").join("config.json"))? {
        return existing_install(
            "CORSIA found an existing install on this computer (~/.corsia), \
             and this app can’t open it."
                .into(),
        );
    }
    let legacy = home
        .join("Library")
        .join("Application Support")
        .join(LEGACY_STATE_FOLDER);
    if present(&legacy)? {
        return existing_install(format!(
            "CORSIA found data from an earlier version (~/Library/Application Support/{LEGACY_STATE_FOLDER}), \
             and this app can’t open it."
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
        if trusted != Some(path.as_path()) && !workspace_is_fresh(&path)? {
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
pub fn recorded_port(app_dir: &Path) -> Option<u16> {
    let text = fs::read_to_string(app_dir.join(SERVICE_FILE)).ok()?;
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

fn write_service_file(app_dir: &Path, workspace: &Path, port: u16) -> Result<(), String> {
    let workspace = workspace
        .to_str()
        .ok_or("the workspace path is not valid UTF-8")?;
    let state_dir = app_dir.join("state").to_str().ok_or("the state path is not valid UTF-8")?.to_owned();
    let body = serde_json::to_vec_pretty(&serde_json::json!({
        "state_dir": state_dir,
        "workspace": workspace,
        "port": port,
    }))
    .map_err(|error| error.to_string())?;

    let target = app_dir.join(SERVICE_FILE);
    let staged = app_dir.join(format!("{SERVICE_FILE}.tmp"));
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
    app_dir: &Path,
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
    fs::create_dir_all(app_dir)
        .map_err(|error| format!("could not create {}: {error}", app_dir.display()))?;
    write_service_file(app_dir, workspace, port)
}

pub fn apply_chosen_folder(
    workspace: &Path,
    default_workspace: &Path,
    app_dir: &Path,
    port: u16,
    account_exists: impl FnOnce() -> Result<bool, String>,
    init_git: impl FnOnce(&Path) -> Result<(), String>,
    restart: impl FnOnce() -> Result<(), String>,
    wait_for_health: impl FnOnce() -> Result<(), String>,
) -> Result<(), String> {
    if account_exists()? {
        return Err("Your account is already set up, so CORSIA left its work folder unchanged.".into());
    }
    apply(workspace, app_dir, port, init_git)?;
    restart()?;
    wait_for_health()?;
    remove_default_folder_if_fresh(default_workspace, workspace)?;
    Ok(())
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

        fn app_dir(&self) -> PathBuf {
            crate::app_paths::app_dir_for_home(&self.0)
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
        match plan(&home.0, &home.app_dir()).expect("plan") {
            FirstRun::ExistingInstall { reason, .. } => {
                assert!(reason.contains(marker), "reason names the marker: {reason}");
                assert!(reason.ends_with('.') && !reason.contains("Nothing was changed"), "{reason}");
            }
            other => panic!("expected a refusal, got {other:?}"),
        }
        assert_eq!(home.listing(), before, "a refusal writes nothing");
    }

    #[test]
    fn fresh_computer_plans_the_default_workspace_and_writes_it_down() {
        let home = Home::new("fresh");
        assert_eq!(
            plan(&home.0, &home.app_dir()).unwrap(),
            FirstRun::Fresh {
                workspace: home.0.join("CORSIA")
            }
        );
        assert!(home.listing().is_empty(), "planning writes nothing");

        let workspace = home.0.join("CORSIA");
        let mut initialised = None;
        apply(&workspace, &home.app_dir(), 47_895, |path| {
            initialised = Some(path.to_path_buf());
            Ok(())
        })
        .unwrap();

        assert_eq!(initialised, Some(workspace.clone()));
        assert!(workspace.is_dir());
        let recorded: serde_json::Value =
            serde_json::from_slice(&fs::read(home.app_dir().join(SERVICE_FILE)).unwrap()).unwrap();
        assert_eq!(recorded["state_dir"], home.app_dir().join("state").to_str().unwrap());
        assert_eq!(recorded["workspace"], workspace.to_str().unwrap());
        assert_eq!(recorded["port"], 47_895);
        assert_eq!(
            recorded.as_object().unwrap().len(),
            3,
            "only state_dir, workspace and port are recorded"
        );
        assert_eq!(recorded_port(&home.app_dir()), Some(47_895));
        assert!(!home.app_dir().join("service.json.tmp").exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(home.app_dir().join(SERVICE_FILE))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o600);
        }
    }

    #[test]
    fn a_failed_git_init_still_records_the_workspace() {
        let home = Home::new("nogit");
        let workspace = home.0.join("CORSIA");

        apply(&workspace, &home.app_dir(), 47_895, |_| {
            Err("git is not installed".into())
        })
        .unwrap();

        assert!(home.app_dir().join(SERVICE_FILE).is_file());
    }

    #[test]
    fn git_is_not_spawned_without_a_bundled_git_or_command_line_tools() {
        let home = Home::new("nogit-skip");
        let workspace = home.0.join("CORSIA");
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
    fn a_source_plist_that_cannot_be_adopted_is_refused_with_nothing_changed() {
        let home = Home::new("bad-plist");
        let plist = home.0.join("Library/LaunchAgents/com.corsia.prod.plist");
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::write(&plist, "not a plist").unwrap();
        assert_refused(&home, "can’t take it over");
        // The parser's own words stay behind Details, never in the line.
        let FirstRun::ExistingInstall { reason, details } = plan(&home.0, &home.app_dir()).unwrap() else {
            panic!("expected a refusal");
        };
        assert!(!reason.contains('(') && !reason.contains("plist"), "{reason}");
        assert!(details.contains("plist"), "{details}");

        // A readable plist whose checkout has no lifecycle owner is refused the same way.
        fs::write(
            &plist,
            format!(
                r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>com.corsia.prod</string><key>WorkingDirectory</key><string>{}</string><key>EnvironmentVariables</key><dict><key>CORSIA_WORKSPACE</key><string>/work</string><key>CORSIA_STATE_DIR</key><string>/state</string><key>BACKEND_PORT</key><string>47895</string></dict></dict></plist>"#,
                home.0.join("checkout/backend").display()
            ),
        )
        .unwrap();
        assert_refused(&home, "can’t take it over");
        let FirstRun::ExistingInstall { details, .. } = plan(&home.0, &home.app_dir()).unwrap() else {
            panic!("expected a refusal");
        };
        assert!(details.contains("scripts/corsia"), "{details}");
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
    fn an_empty_or_no_commit_git_workspace_is_reused_under_its_own_name() {
        let home = Home::new("reuse");
        fs::create_dir_all(home.0.join("Corsia").join(".git")).unwrap();
        fs::write(home.0.join("Corsia").join(".DS_Store"), "").unwrap();

        assert_eq!(
            plan(&home.0, &home.app_dir()).unwrap(),
            FirstRun::Fresh {
                workspace: home.0.join("Corsia")
            }
        );
    }

    #[test]
    fn a_repository_with_branch_refs_is_an_existing_install() {
        let heads = Home::new("heads-ref");
        fs::create_dir_all(heads.0.join("corsia").join(".git").join("refs").join("heads")).unwrap();
        fs::write(heads.0.join("corsia").join(".git").join("refs").join("heads").join("main"), "abc").unwrap();
        assert_refused(&heads, "~/corsia");

        let packed = Home::new("packed-ref");
        fs::create_dir_all(packed.0.join("corsia").join(".git")).unwrap();
        fs::write(
            packed.0.join("corsia").join(".git").join("packed-refs"),
            "abc refs/heads/main\n",
        )
        .unwrap();
        assert_refused(&packed, "~/corsia");
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
        fs::create_dir_all(home.app_dir()).unwrap();
        fs::write(
            home.app_dir().join(SERVICE_FILE),
            r#"{"workspace": "/somewhere/else", "port": 5123}"#,
        )
        .unwrap();
        fs::create_dir_all(home.0.join(".corsia")).unwrap();
        fs::write(home.0.join(".corsia").join("config.json"), "{}").unwrap();
        fs::create_dir_all(home.0.join("corsia")).unwrap();
        fs::write(home.0.join("corsia").join("notes.md"), "mine").unwrap();
        let before = home.listing();

        assert_eq!(
            plan(&home.0, &home.app_dir()).unwrap(),
            FirstRun::Configured
        );
        assert_eq!(recorded_port(&home.app_dir()), Some(5123));
        assert_eq!(home.listing(), before);
    }

    #[test]
    fn chosen_folder_is_created_git_initialised_recorded_and_restarts_before_account() {
        let home = Home::new("chosen-folder");
        let default_folder = home.0.join("corsia");
        fs::create_dir_all(default_folder.join(".git")).unwrap();
        let chosen = home.0.join("Documents").join("corsia");
        let calls = std::cell::RefCell::new(Vec::new());

        apply_chosen_folder(
            &chosen,
            &default_folder,
            &home.app_dir(),
            47_895,
            || Ok(false),
            |workspace| {
                calls.borrow_mut().push(format!("git:{}", workspace.display()));
                fs::create_dir_all(workspace.join(".git")).map_err(|error| error.to_string())
            },
            || { calls.borrow_mut().push("restart".into()); Ok(()) },
            || { calls.borrow_mut().push("health".into()); Ok(()) },
        )
        .unwrap();

        assert!(chosen.join(".git").is_dir());
        assert_eq!(
            *calls.borrow(),
            [
                format!("git:{}", chosen.display()),
                "restart".to_string(),
                "health".to_string(),
            ]
        );
        let recorded: serde_json::Value =
            serde_json::from_slice(&fs::read(home.app_dir().join(SERVICE_FILE)).unwrap()).unwrap();
        assert_eq!(recorded["workspace"], chosen.to_str().unwrap());
        assert!(!default_folder.exists(), "only the fresh default Git folder is removed");
    }

    #[test]
    fn chosen_folder_is_refused_after_an_account_exists() {
        let home = Home::new("chosen-folder-account");
        let chosen = home.0.join("Documents").join("corsia");
        let result = apply_chosen_folder(
            &chosen,
            &home.0.join("corsia"),
            &home.app_dir(),
            47_895,
            || Ok(true),
            |_| panic!("must not initialise after account creation"),
            || panic!("must not restart after account creation"),
            || panic!("must not wait after account creation"),
        );

        assert!(result.unwrap_err().contains("already set up"));
        assert!(!chosen.exists());
        assert!(!home.app_dir().join(SERVICE_FILE).exists());
    }

    #[test]
    fn folder_warnings_and_unavailable_folder_choices_are_classified() {
        let home = Home::new("folder-classification");
        let documents = home.0.join("Documents");
        let empty_in_documents = documents.join("corsia");
        let desktop = home.0.join("Desktop");
        let downloads = home.0.join("Downloads");
        let library = home.0.join("Library");
        let cloud_docs = library
            .join("Mobile Documents")
            .join("com~apple~CloudDocs");
        for path in [&empty_in_documents, &desktop, &downloads, &cloud_docs] {
            fs::create_dir_all(path).unwrap();
        }
        let documents_with_trailing_separator = PathBuf::from(format!("{}/", documents.display()));
        let home_parent = home.0.parent().unwrap().to_path_buf();

        for (path, folder_warning) in [
            (&home.0, FolderWarning::PersonalFolder { folder: "Home" }),
            (&documents, FolderWarning::PersonalFolder { folder: "Documents" }),
            (&documents_with_trailing_separator, FolderWarning::PersonalFolder { folder: "Documents" }),
            (&desktop, FolderWarning::PersonalFolder { folder: "Desktop" }),
            (&downloads, FolderWarning::PersonalFolder { folder: "Downloads" }),
            (&cloud_docs, FolderWarning::PersonalFolder { folder: "iCloud Drive" }),
            (&library, FolderWarning::ContainsPersonalFolder { folder: "iCloud Drive" }),
            (
                &home_parent,
                FolderWarning::ContainsPersonalFolder { folder: "Documents" },
            ),
        ] {
            assert_eq!(
                classify_folder(path, &home.0, |_| Ok(())),
                FolderClassification::Ready {
                    path: path.display().to_string(),
                    folder_warning: Some(folder_warning),
                }
            );
        }

        assert_eq!(
            classify_folder(&empty_in_documents, &home.0, |_| Ok(())),
            FolderClassification::Ready {
                path: empty_in_documents.display().to_string(),
                folder_warning: None,
            }
        );
        fs::write(empty_in_documents.join(".DS_Store"), "").unwrap();
        fs::create_dir_all(empty_in_documents.join(".git")).unwrap();
        assert_eq!(
            classify_folder(&empty_in_documents, &home.0, |_| Ok(())),
            FolderClassification::Ready {
                path: empty_in_documents.display().to_string(),
                folder_warning: None,
            }
        );

        fs::write(empty_in_documents.join("personal-note.txt"), "private").unwrap();
        assert_eq!(
            classify_folder(&empty_in_documents, &home.0, |_| Ok(())),
            FolderClassification::Ready {
                path: empty_in_documents.display().to_string(),
                folder_warning: Some(FolderWarning::ContainsFiles),
            }
        );

        let unwritable = home.0.join("read-only");
        assert_eq!(
            classify_folder(&unwritable, &home.0, |_| Err("the disk is read-only".into())),
            FolderClassification::Unwritable {
                path: unwritable.display().to_string(),
                reason: "the disk is read-only".into(),
            }
        );

        let unreadable = home.0.join("not-a-folder");
        fs::write(&unreadable, "not a folder").unwrap();
        let FolderClassification::Unreadable { path, reason } = classify_folder(&unreadable, &home.0, |_| Ok(())) else {
            panic!("a folder that cannot be inspected must not look ready");
        };
        assert_eq!(path, unreadable.display().to_string());
        assert!(reason.contains("could not read"), "{reason}");
    }

    #[cfg(unix)]
    #[test]
    fn folder_warning_classification_resolves_symlinks() {
        let home = Home::new("folder-classification-symlink");
        let documents = home.0.join("Documents");
        fs::create_dir_all(&documents).unwrap();
        let alias = home.0.join("documents-alias");
        std::os::unix::fs::symlink(&documents, &alias).unwrap();

        assert_eq!(
            classify_folder(&alias, &home.0, |_| Ok(())),
            FolderClassification::Ready {
                path: alias.display().to_string(),
                folder_warning: Some(FolderWarning::PersonalFolder { folder: "Documents" }),
            }
        );
    }

    #[test]
    fn only_a_git_only_default_folder_is_removed_after_a_new_choice() {
        let home = Home::new("remove-default");
        let default_folder = home.0.join("corsia");
        let chosen = home.0.join("Documents").join("corsia");
        fs::create_dir_all(default_folder.join(".git")).unwrap();

        assert!(remove_default_folder_if_fresh(&default_folder, &chosen).unwrap());
        assert!(!default_folder.exists());

        fs::create_dir_all(default_folder.join(".git").join("refs").join("heads")).unwrap();
        fs::write(default_folder.join(".git").join("refs").join("heads").join("main"), "abc").unwrap();
        assert!(!remove_default_folder_if_fresh(&default_folder, &chosen).unwrap());
        assert!(default_folder.exists());

        fs::remove_dir_all(&default_folder).unwrap();
        fs::create_dir_all(default_folder.join(".git")).unwrap();
        fs::write(default_folder.join(".git").join("packed-refs"), "abc refs/heads/main\n").unwrap();
        assert!(!remove_default_folder_if_fresh(&default_folder, &chosen).unwrap());
        assert!(default_folder.exists());
    }

    #[test]
    fn recorded_port_ignores_a_damaged_file() {
        let home = Home::new("damaged");
        fs::create_dir_all(home.app_dir()).unwrap();
        for text in ["", "not json", r#"{"port": 0}"#, r#"{"port": 70000}"#, r#"{"port": "80"}"#] {
            fs::write(home.app_dir().join(SERVICE_FILE), text).unwrap();
            assert_eq!(recorded_port(&home.app_dir()), None, "{text}");
        }
    }
}
