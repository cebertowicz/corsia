// An unfinished first-run setup leaves nothing: the next launch starts again at step 1.
//
// Only a first run that made a brand-new state root marks it: `mark` writes `setup-unfinished` in the app
// folder, next to `service.json`, and refuses when the state folder already holds anything. The backend adds
// each work-folder file its setup creates (path and SHA-256) and deletes the marker before it commits the
// finished setup (`backend/infra/setup_marker.py`), so a finished install has no marker.
//
// While the marker exists, the next launch, before it starts the service, moves the state folder aside in one
// rename, keeps the downloads in it (`KEPT`), deletes the rest, removes `service.json` and the recorded files
// whose bytes still match, then plans a first run again in the folder the first run made. When the wipe
// finishes the marker is rewritten as `wiped`, which keeps that folder trusted on disk until the new first run
// is applied and `mark` replaces it, so a launch that fails in between still plans the first run there. An
// install the shell did not mark (adopted, existing, scripted, or already finished) is never touched. If the
// rename fails nothing was deleted: the caller drops the marker and the old install resumes whole. Once it
// succeeds the old install is gone and the launch goes on as a clean first run; a recorded file that will not
// go stays in the marker and is retried at the next launch (`finish_leftovers`). A marker the shell cannot
// trust is left alone.
//
// Two writers share the file: the shell owns `state_dir` and `workspace`, the backend owns `files_root` and
// `files` (see `backend/infra/setup_marker.py`).

use std::{
    collections::BTreeMap,
    fs,
    io::{ErrorKind, Write},
    path::{Component, Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{
    app_paths::{self, STATE_DIRECTORY},
    first_run::SERVICE_FILE,
};

/// Held equal to `MARKER_FILE` in `backend/infra/setup_marker.py` by a parity test.
pub const MARKER_FILE: &str = "setup-unfinished";
/// Where the state folder goes while it is deleted: one rename, so a state folder is never half there.
const DISCARDED_STATE: &str = "state.discarded";

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Marker {
    /// The state folder this marker may wipe: always the app folder's own `state` child.
    pub state_dir: String,
    /// The work folder the first run made, and the one a new first run may reuse after the wipe.
    pub workspace: String,
    /// The folder the recorded files are relative to: the work folder the person chose, when it is not `workspace`.
    #[serde(default)]
    pub files_root: Option<String>,
    /// Files the backend's setup created in that folder, by path relative to it, with their SHA-256.
    #[serde(default)]
    pub files: BTreeMap<String, String>,
    /// Set once the wipe has finished: the next first run is being planned in `workspace`, and nothing is left to
    /// delete. Kept on disk, so a launch that fails after the wipe (in this process or the next) still plans the
    /// first run in the folder the wiped setup made.
    #[serde(default)]
    pub wiped: bool,
}

impl Marker {
    fn files_folder(&self) -> &Path {
        Path::new(self.files_root.as_deref().unwrap_or(&self.workspace))
    }
}

/// What a launch does about an unfinished setup.
#[derive(Debug, PartialEq, Eq)]
pub enum Launch {
    /// No marker: the install is not the shell's to reset.
    Nothing,
    /// Setup was never finished: wipe it, then start from step 1.
    Wipe(Marker),
    /// The wipe already finished and the new first run is not marked yet: plan it in the folder the marker names.
    Replan(PathBuf),
    /// A marker the shell cannot trust: nothing is wiped, and the reason belongs in the log.
    Untrusted(String),
    /// A marker that is not valid JSON: nothing can use it, so the launch removes it (`discard_marker`) and the
    /// install goes on unmarked.
    Damaged(String),
}

fn state_dir_in(app_dir: &Path) -> PathBuf {
    app_dir.join(STATE_DIRECTORY)
}

/// Whether a path names an entry at all, a dangling link included.
fn exists(path: &Path) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!("could not check {}: {error}", path.display())),
    }
}

/// Mark the state root the first run just made, unless it already holds anything.
///
/// `Ok(false)` means no marker was written, which is the safe direction: the install is never wiped. An
/// existing state folder with content, or one that is a link, is not one this first run made.
pub fn mark(app_dir: &Path, workspace: &Path) -> Result<bool, String> {
    let state_dir = state_dir_in(app_dir);
    fs::create_dir_all(app_dir)
        .map_err(|error| format!("could not create {}: {error}", app_dir.display()))?;
    if exists(&state_dir)? {
        let metadata = fs::symlink_metadata(&state_dir)
            .map_err(|error| format!("could not check {}: {error}", state_dir.display()))?;
        let empty = metadata.is_dir() && holds_only_downloads(&state_dir, &state_dir);
        if !empty {
            return Ok(false);
        }
    }
    let marker = Marker {
        state_dir: state_dir
            .to_str()
            .ok_or("the state path is not valid UTF-8")?
            .to_owned(),
        workspace: workspace
            .to_str()
            .ok_or("the workspace path is not valid UTF-8")?
            .to_owned(),
        files_root: None,
        files: BTreeMap::new(),
        wiped: false,
    };
    write_marker(app_dir, &marker)?;
    Ok(true)
}

fn write_marker(app_dir: &Path, marker: &Marker) -> Result<(), String> {
    let body = serde_json::to_vec_pretty(marker).map_err(|error| error.to_string())?;
    let target = app_dir.join(MARKER_FILE);
    let staged = app_dir.join(format!("{MARKER_FILE}.tmp"));
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
    file.write_all(&body)
        .and_then(|()| file.sync_all())
        .map_err(|error| format!("could not write {}: {error}", staged.display()))?;
    fs::rename(&staged, &target)
        .map_err(|error| format!("could not write {}: {error}", target.display()))
}

/// Remove a marker nothing can use, so the backend never has to guess about it.
pub fn discard_marker(app_dir: &Path) -> Result<(), String> {
    remove_if_present(&app_dir.join(MARKER_FILE))
}

/// Decide what this launch does, reading only.
pub fn at_launch(app_dir: &Path) -> Launch {
    let path = app_dir.join(MARKER_FILE);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == ErrorKind::NotFound => return Launch::Nothing,
        Err(error) => return Launch::Untrusted(format!("{} could not be read: {error}", path.display())),
    };
    let marker: Marker = match serde_json::from_slice(&bytes) {
        Ok(marker) => marker,
        Err(error) => return Launch::Damaged(format!("{} is damaged: {error}", path.display())),
    };
    let expected = state_dir_in(app_dir);
    // The marker names the one folder it may wipe, and the install must still use exactly that folder: an
    // adopted install, or any service.json pointing elsewhere, is not this marker's to wipe.
    if Path::new(&marker.state_dir) != expected {
        return Launch::Untrusted(format!("{} names a state folder other than {}", path.display(), expected.display()));
    }
    match app_paths::state_dir_with_override(app_dir, None) {
        Ok(in_use) if in_use == expected => {}
        Ok(in_use) => {
            return Launch::Untrusted(format!(
                "{} is for {}, but this install uses {}",
                path.display(),
                expected.display(),
                in_use.display()
            ))
        }
        Err(reason) => return Launch::Untrusted(reason),
    }
    if !Path::new(&marker.workspace).is_absolute() || !marker.files_folder().is_absolute() {
        return Launch::Untrusted(format!("{} names a work folder that is not an absolute path", path.display()));
    }
    if marker.wiped || nothing_to_wipe(app_dir, &marker) {
        return Launch::Replan(PathBuf::from(marker.workspace));
    }
    Launch::Wipe(marker)
}

/// Whether a marker has nothing behind it: no service file, no recorded file, a state folder that is absent or
/// empty. That is a first run that stopped before it started anything, the same ground a finished wipe leaves.
fn nothing_to_wipe(app_dir: &Path, marker: &Marker) -> bool {
    let state_dir = state_dir_in(app_dir);
    let state_is_empty = match fs::read_dir(&state_dir) {
        Ok(_) => holds_only_downloads(&state_dir, &state_dir),
        Err(error) => error.kind() == ErrorKind::NotFound,
    };
    marker.files.is_empty() && state_is_empty && !matches!(exists(&app_dir.join(SERVICE_FILE)), Ok(true) | Err(_))
}

/// The file at `relative` under the work folder, when it is a plain file the marker may remove: a relative
/// path with no `..`, no link in it, still inside the work folder.
fn removable(workspace: &Path, relative: &str) -> Option<PathBuf> {
    let relative = Path::new(relative);
    if !relative.components().all(|part| matches!(part, Component::Normal(_))) {
        return None;
    }
    let path = workspace.join(relative);
    let metadata = fs::symlink_metadata(&path).ok()?;
    if !metadata.is_file() {
        return None;
    }
    let inside = path.canonicalize().ok()?.starts_with(workspace.canonicalize().ok()?);
    inside.then_some(path)
}

fn digest_of(path: &Path) -> Result<String, String> {
    let read_error = |error: std::io::Error| format!("could not read {}: {error}", path.display());
    let mut file = fs::File::open(path).map_err(read_error)?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher).map_err(read_error)?;
    Ok(hasher.finalize().iter().map(|byte| format!("{byte:02x}")).collect())
}

fn remove_if_present(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("could not remove {}: {error}", path.display())),
    }
}

/// Remove the folders a removed file leaves empty, up to but never including the work folder.
fn remove_empty_parents(file: &Path, workspace: &Path) {
    let mut folder = file.parent();
    while let Some(dir) = folder.filter(|dir| *dir != workspace && dir.starts_with(workspace)) {
        if fs::remove_dir(dir).is_err() {
            break;
        }
        folder = dir.parent();
    }
}

/// Wipe the unfinished setup `marker` names. The service must not be running.
///
/// All or nothing for the old install. The state folder is moved aside in one rename before anything else is
/// touched, so an `Err` means nothing was deleted and the caller resumes the old install whole. Once the rename
/// has happened the old install is gone and nothing below fails the wipe: the downloads in [`KEPT`] are moved
/// back into a new state folder and the rest of the old one is deleted, `service.json` goes, and then the
/// recorded work-folder files whose bytes still match. A file that will not go is written back into the marker
/// (`wiped`), and the next launch that plans a first run tries again. Errors are details for the log, not text
/// for the person.
pub fn wipe(app_dir: &Path, marker: &Marker) -> Result<(), String> {
    let state_dir = state_dir_in(app_dir);
    // A leftover from an earlier wipe that was cut short: its downloads come back before it is cleared.
    if exists(&app_dir.join(DISCARDED_STATE))? {
        settle_discarded(app_dir)?;
    }
    if exists(&state_dir)? {
        let metadata = fs::symlink_metadata(&state_dir)
            .map_err(|error| format!("could not check {}: {error}", state_dir.display()))?;
        if !metadata.is_dir() {
            return Err(format!("{} is not a plain folder", state_dir.display()));
        }
        fs::rename(&state_dir, app_dir.join(DISCARDED_STATE))
            .map_err(|error| format!("could not move {} aside: {error}", state_dir.display()))?;
    }
    // The old install is gone. Nothing from here may stop the launch.
    if let Err(error) = settle_discarded(app_dir) {
        log::warn!("could not finish clearing the old state folder: {error}");
    }
    if let Err(error) = remove_if_present(&app_dir.join(SERVICE_FILE)) {
        log::warn!("{error}");
    }
    let stuck = remove_recorded_files(marker.files_folder(), &marker.files);
    // The wipe is done. If this note can't be written, the old marker still describes an install with nothing
    // left, which `at_launch` also plans as a fresh first run.
    if let Err(error) = write_marker(app_dir, &wiped_marker(marker, stuck)) {
        log::warn!("could not note that the wipe finished: {error}");
    }
    Ok(())
}

/// The marker a finished wipe leaves: the first run is planned in `workspace`, and only files that would not go
/// are still recorded.
fn wiped_marker(marker: &Marker, stuck: BTreeMap<String, String>) -> Marker {
    Marker {
        state_dir: marker.state_dir.clone(),
        workspace: marker.workspace.clone(),
        files_root: (!stuck.is_empty()).then(|| marker.files_folder().to_string_lossy().into_owned()),
        files: stuck,
        wiped: true,
    }
}

/// Remove the recorded files whose bytes still match. Returns the ones that would not go, to try again. A file
/// the person changed, or one that is no longer a plain file, stays and is not retried.
fn remove_recorded_files(workspace: &Path, files: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    let mut stuck = BTreeMap::new();
    for (relative, digest) in files {
        let Some(path) = removable(workspace, relative) else { continue };
        let gone = match digest_of(&path) {
            Ok(found) if &found == digest => remove_if_present(&path).map(|()| remove_empty_parents(&path, workspace)),
            Ok(_) => Ok(()),
            Err(reason) => Err(reason),
        };
        if let Err(reason) = gone {
            log::warn!("{reason}");
            stuck.insert(relative.clone(), digest.clone());
        }
    }
    stuck
}

/// Try again to remove the files an earlier wipe could not. A launch that plans a first run again calls it.
pub fn finish_leftovers(app_dir: &Path) {
    let Ok(bytes) = fs::read(app_dir.join(MARKER_FILE)) else { return };
    let Ok(marker) = serde_json::from_slice::<Marker>(&bytes) else { return };
    if !marker.wiped || marker.files.is_empty() {
        return;
    }
    let stuck = remove_recorded_files(marker.files_folder(), &marker.files);
    if stuck.len() != marker.files.len() {
        if let Err(error) = write_marker(app_dir, &wiped_marker(&marker, stuck)) {
            log::warn!("could not note the files that are gone: {error}");
        }
    }
}

/// Downloads are not setup state, so a wipe keeps them. Relative to the state folder; held equal to the
/// backend's model store, classifier assets and logs folders by a test.
pub const KEPT: [&str; 3] = ["managed-omlx/models", "assets", "logs"];

/// Whether a state folder holds nothing but the folders in [`KEPT`] and the folders on the way to them. Its update
/// lock is not setup state either: a start makes it in a state folder that already exists.
fn holds_only_downloads(dir: &Path, within: &Path) -> bool {
    let Ok(entries) = fs::read_dir(dir) else { return false };
    entries.flatten().all(|entry| {
        let path = entry.path();
        let Ok(relative) = path.strip_prefix(within) else { return false };
        let relative = relative.to_string_lossy();
        KEPT.contains(&relative.as_ref())
            || (relative == crate::app_paths::UPDATE_LOCK_FILE
                && fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.is_file()))
            || (KEPT.iter().any(|kept| kept.starts_with(&format!("{relative}/")))
                && fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.is_dir())
                && holds_only_downloads(&path, within))
    })
}

/// Move the downloads out of the folder set aside back into the state folder, then delete what is left of it.
///
/// Nothing is deleted unless every download that was there has been moved back: one that cannot be restored, or
/// whose place is already taken, leaves the whole folder aside for the next wipe to settle.
fn settle_discarded(app_dir: &Path) -> Result<(), String> {
    let discarded = app_dir.join(DISCARDED_STATE);
    if !exists(&discarded)? {
        return Ok(());
    }
    let state_dir = state_dir_in(app_dir);
    for kept in KEPT {
        let from = discarded.join(kept);
        if !exists(&from)? {
            continue;
        }
        let to = state_dir.join(kept);
        if exists(&to)? {
            return Err(format!("{} is already there, so {} stays aside", to.display(), from.display()));
        }
        if let Some(parent) = to.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("could not create {}: {error}", parent.display()))?;
        }
        fs::rename(&from, &to).map_err(|error| format!("could not keep {}: {error}", from.display()))?;
    }
    fs::remove_dir_all(&discarded).map_err(|error| format!("could not delete {}: {error}", discarded.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Home(PathBuf);

    impl Home {
        fn new(label: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "corsia-unfinished-{label}-{}-{:?}",
                std::process::id(),
                std::time::Instant::now()
            ));
            fs::create_dir_all(&root).expect("create throwaway home");
            Home(root)
        }

        fn app_dir(&self) -> PathBuf {
            let dir = app_paths::app_dir_for_home(&self.0);
            fs::create_dir_all(&dir).unwrap();
            dir
        }

        fn workspace(&self) -> PathBuf {
            let dir = self.0.join("CORSIA");
            fs::create_dir_all(dir.join("context")).unwrap();
            dir
        }
    }

    impl Drop for Home {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const PROFILE: &str = "context/50-USER-PROFILE.md";

    /// A first run's disk: a service file, a state folder with a database, a work folder with the profile
    /// the backend recorded in the marker.
    fn unfinished(home: &Home) -> (PathBuf, PathBuf) {
        let app_dir = home.app_dir();
        let workspace = home.workspace();
        fs::write(app_dir.join(SERVICE_FILE), b"{}").unwrap();
        assert!(mark(&app_dir, &workspace).unwrap());
        fs::create_dir_all(app_dir.join("state")).unwrap();
        fs::write(app_dir.join("state").join("corsia.db"), b"db").unwrap();
        fs::write(workspace.join(PROFILE), b"# Profile\n").unwrap();
        let mut marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        marker.files.insert(PROFILE.into(), Sha256::digest(b"# Profile\n").iter().map(|b| format!("{b:02x}")).collect());
        fs::write(app_dir.join(MARKER_FILE), serde_json::to_vec(&marker).unwrap()).unwrap();
        (app_dir, workspace)
    }

    fn wipe_it(app_dir: &Path) -> Result<(), String> {
        match at_launch(app_dir) {
            Launch::Wipe(marker) => wipe(app_dir, &marker),
            other => panic!("expected a wipe, got {other:?}"),
        }
    }

    #[test]
    fn a_marker_with_no_finish_wipes_everything_setup_made() {
        let home = Home::new("wipe");
        let (app_dir, workspace) = unfinished(&home);
        wipe_it(&app_dir).unwrap();
        assert!(!app_dir.join("state").exists());
        assert!(!app_dir.join(SERVICE_FILE).exists());
        assert!(!workspace.join(PROFILE).exists());
        assert!(workspace.is_dir(), "the work folder itself stays");
        assert!(!app_dir.join("state.discarded").exists(), "the state folder is gone, not left aside");
        assert!(
            matches!(at_launch(&app_dir), Launch::Replan(ref folder) if *folder == workspace),
            "the marker now says the wipe is done, and that the first run is planned in the folder it made"
        );
    }

    #[test]
    fn the_state_folder_the_marker_names_is_the_one_the_app_uses() {
        let home = Home::new("constant");
        let app_dir = home.app_dir();
        assert_eq!(state_dir_in(&app_dir), app_paths::state_dir_with_override(&app_dir, None).unwrap());
        assert_eq!(STATE_DIRECTORY, "state", "the backend's default_state_root names the same folder");
    }

    #[test]
    fn a_first_run_that_stopped_before_it_started_anything_is_planned_again_from_disk() {
        let home = Home::new("retry");
        let app_dir = home.app_dir();
        let workspace = home.workspace();
        assert!(mark(&app_dir, &workspace).unwrap());
        assert!(
            matches!(at_launch(&app_dir), Launch::Replan(ref folder) if *folder == workspace),
            "no service file and no state: nothing to wipe, and the folder is the one this first run made"
        );
        fs::write(app_dir.join(SERVICE_FILE), b"{}").unwrap();
        fs::create_dir_all(app_dir.join("state")).unwrap();
        fs::write(app_dir.join("state").join("corsia.db"), b"db").unwrap();
        assert!(matches!(at_launch(&app_dir), Launch::Wipe(_)), "once it has a service and state it is a setup to wipe");
    }

    #[test]
    fn files_in_a_chosen_work_folder_go_while_the_first_runs_own_folder_is_still_the_one_trusted() {
        let home = Home::new("chosen");
        let (app_dir, workspace) = unfinished(&home);
        let chosen = home.0.join("chosen-folder");
        fs::create_dir_all(chosen.join("context")).unwrap();
        fs::write(chosen.join(PROFILE), b"# Profile\n").unwrap();
        let mut marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        marker.files_root = Some(chosen.to_string_lossy().into_owned());
        fs::write(app_dir.join(MARKER_FILE), serde_json::to_vec(&marker).unwrap()).unwrap();
        fs::remove_file(workspace.join(PROFILE)).unwrap();
        let Launch::Wipe(found) = at_launch(&app_dir) else { panic!("expected a wipe") };
        assert_eq!(found.workspace, workspace.to_string_lossy(), "the shell's own folder is not rewritten");
        wipe(&app_dir, &found).unwrap();
        assert!(!chosen.join(PROFILE).exists(), "the recorded file goes from the folder it was written in");
        assert!(chosen.is_dir());
    }

    #[test]
    fn a_wiped_first_run_plans_again_in_the_folder_it_made_even_with_the_backends_files_in_it() {
        use crate::first_run::{apply, plan, plan_trusting, FirstRun};
        let home = Home::new("cycle");
        let app_dir = home.app_dir();
        let workspace = match plan(&home.0, &app_dir).unwrap() {
            FirstRun::Fresh { workspace } => workspace,
            other => panic!("a new computer plans a first run, got {other:?}"),
        };
        apply(&workspace, &app_dir, 47_895, |path| fs::create_dir_all(path.join(".git")).map_err(|e| e.to_string())).unwrap();
        assert!(mark(&app_dir, &workspace).unwrap());
        // What the backend did before setup was left: its generated projection, and the profile.
        fs::create_dir_all(workspace.join(".corsia").join("context")).unwrap();
        fs::write(workspace.join(".corsia").join("README.md"), b"generated").unwrap();
        fs::create_dir_all(workspace.join("context")).unwrap();
        fs::write(workspace.join(PROFILE), b"# Profile\n").unwrap();
        fs::create_dir_all(app_dir.join("state")).unwrap();
        fs::write(app_dir.join("state").join("corsia.db"), b"db").unwrap();
        let mut marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        marker.files.insert(PROFILE.into(), Sha256::digest(b"# Profile\n").iter().map(|b| format!("{b:02x}")).collect());
        fs::write(app_dir.join(MARKER_FILE), serde_json::to_vec(&marker).unwrap()).unwrap();

        wipe_it(&app_dir).unwrap();
        assert!(!workspace.join("context").exists(), "the folder the profile left empty goes too");
        assert!(matches!(at_launch(&app_dir), Launch::Replan(_)), "a launch that fails after the wipe still plans the first run in that folder");
        assert!(matches!(plan(&home.0, &app_dir).unwrap(), FirstRun::ExistingInstall { .. }), "plain planning refuses a folder with files");
        assert_eq!(
            plan_trusting(&home.0, &app_dir, Some(&workspace)).unwrap(),
            FirstRun::Fresh { workspace: workspace.clone() },
            "but the folder the wiped setup made is reused"
        );
        let other = home.0.join("elsewhere");
        assert!(matches!(
            plan_trusting(&home.0, &app_dir, Some(&other)).unwrap(),
            FirstRun::ExistingInstall { .. }
        ), "trust names one folder only");
        // The new first run is applied and marks again: the stale marker is replaced by a clean one.
        apply(&workspace, &app_dir, 47_895, |path| fs::create_dir_all(path.join(".git")).map_err(|e| e.to_string())).unwrap();
        assert!(mark(&app_dir, &workspace).unwrap());
        let Launch::Wipe(fresh) = at_launch(&app_dir) else { panic!("the new first run is marked") };
        assert!(fresh.files.is_empty() && fresh.files_root.is_none());
    }

    #[test]
    fn no_marker_never_wipes() {
        let home = Home::new("nomarker");
        let app_dir = home.app_dir();
        let workspace = home.workspace();
        fs::write(app_dir.join(SERVICE_FILE), b"{}").unwrap();
        fs::create_dir_all(app_dir.join("state")).unwrap();
        fs::write(app_dir.join("state").join("corsia.db"), b"db").unwrap();
        fs::write(workspace.join(PROFILE), b"mine").unwrap();
        assert_eq!(at_launch(&app_dir), Launch::Nothing);
        assert!(app_dir.join("state").join("corsia.db").exists());
        assert!(app_dir.join(SERVICE_FILE).exists());
    }

    #[test]
    fn a_finished_install_has_no_marker_so_it_is_never_wiped() {
        // The backend deletes the marker before it commits the finished setup.
        let home = Home::new("finished");
        let (app_dir, workspace) = unfinished(&home);
        fs::remove_file(app_dir.join(MARKER_FILE)).unwrap();
        assert_eq!(at_launch(&app_dir), Launch::Nothing);
        assert!(app_dir.join("state").join("corsia.db").exists());
        assert!(workspace.join(PROFILE).exists());
    }

    #[test]
    fn an_existing_or_adopted_state_root_never_gets_a_marker() {
        let home = Home::new("existing");
        let app_dir = home.app_dir();
        let state = app_dir.join("state");
        fs::create_dir_all(&state).unwrap();
        fs::write(state.join("corsia.db"), b"their data").unwrap();
        assert!(!mark(&app_dir, &home.workspace()).unwrap());
        assert!(!app_dir.join(MARKER_FILE).exists());
        assert_eq!(at_launch(&app_dir), Launch::Nothing);

        let linked = Home::new("linked");
        let linked_app = linked.app_dir();
        let elsewhere = linked.0.join("elsewhere");
        fs::create_dir_all(&elsewhere).unwrap();
        std::os::unix::fs::symlink(&elsewhere, linked_app.join("state")).unwrap();
        assert!(!mark(&linked_app, &linked.workspace()).unwrap(), "a linked state folder is not one the shell made");
    }

    #[test]
    fn marking_a_computer_with_no_app_folder_yet_makes_the_folder_and_marks() {
        let home = Home::new("nofolder");
        let app_dir = home.0.join("fresh").join("app");
        assert!(mark(&app_dir, &home.workspace()).unwrap());
        assert!(app_dir.join(MARKER_FILE).is_file());
    }

    #[test]
    fn a_marker_that_cannot_be_written_is_an_error_not_a_silent_skip() {
        let home = Home::new("unwritable");
        let app_dir = home.0.join("blocked");
        fs::write(&app_dir, b"a file where the folder should be").unwrap();
        assert!(mark(&app_dir, &home.workspace()).is_err());
    }

    #[test]
    fn a_new_empty_state_root_is_marked() {
        let home = Home::new("empty");
        let app_dir = home.app_dir();
        assert!(mark(&app_dir, &home.workspace()).unwrap());
        assert!(app_dir.join(MARKER_FILE).is_file());
        fs::create_dir_all(app_dir.join("state")).unwrap();
        assert!(mark(&app_dir, &home.workspace()).unwrap(), "an empty state folder is still new");
        // The lock a start takes in an existing state folder is not setup state.
        drop(crate::app_paths::wait_for_update_lock(&app_dir.join("state")).unwrap().expect("the start's lock"));
        assert!(app_dir.join("state").join(crate::app_paths::UPDATE_LOCK_FILE).is_file());
        assert!(mark(&app_dir, &home.workspace()).unwrap(), "a state folder holding only its update lock is still new");
        fs::write(app_dir.join("state").join("corsia.db"), b"db").unwrap();
        assert!(!mark(&app_dir, &home.workspace()).unwrap(), "any other file still makes it an existing install");
    }

    #[test]
    fn a_file_the_person_changed_stays() {
        let home = Home::new("changed");
        let (app_dir, workspace) = unfinished(&home);
        fs::write(workspace.join(PROFILE), b"# Profile\n- Likes: short answers\n").unwrap();
        fs::write(workspace.join("context").join("other.md"), b"mine").unwrap();
        wipe_it(&app_dir).unwrap();
        assert_eq!(fs::read(workspace.join(PROFILE)).unwrap(), b"# Profile\n- Likes: short answers\n");
        assert!(workspace.join("context").join("other.md").exists());
        assert!(!app_dir.join("state").exists(), "the rest of the setup is still wiped");
    }

    #[test]
    fn a_linked_or_escaping_recorded_path_is_never_followed() {
        let home = Home::new("linked-file");
        let (app_dir, workspace) = unfinished(&home);
        let outside = home.0.join("outside.md");
        fs::write(&outside, b"# Profile\n").unwrap();
        fs::remove_file(workspace.join(PROFILE)).unwrap();
        std::os::unix::fs::symlink(&outside, workspace.join(PROFILE)).unwrap();
        let mut marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        marker.files.insert("../outside.md".into(), marker.files[PROFILE].clone());
        let elsewhere = home.0.join("elsewhere");
        fs::create_dir_all(&elsewhere).unwrap();
        fs::write(elsewhere.join("kept.md"), b"# Profile\n").unwrap();
        std::os::unix::fs::symlink(&elsewhere, workspace.join("linked")).unwrap();
        marker.files.insert("linked/kept.md".into(), marker.files[PROFILE].clone());
        fs::write(app_dir.join(MARKER_FILE), serde_json::to_vec(&marker).unwrap()).unwrap();
        wipe_it(&app_dir).unwrap();
        assert!(outside.exists(), "neither a link nor a path out of the work folder is followed");
        assert!(elsewhere.join("kept.md").exists(), "a linked folder inside the work folder is not followed either");
    }

    #[test]
    fn a_marker_for_another_state_folder_or_a_damaged_one_is_never_wiped() {
        let home = Home::new("untrusted");
        let (app_dir, _) = unfinished(&home);
        let mut marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        marker.state_dir = home.0.join("elsewhere").to_string_lossy().into_owned();
        fs::write(app_dir.join(MARKER_FILE), serde_json::to_vec(&marker).unwrap()).unwrap();
        assert!(matches!(at_launch(&app_dir), Launch::Untrusted(_)));
        fs::write(app_dir.join(MARKER_FILE), b"{ not json").unwrap();
        assert!(matches!(at_launch(&app_dir), Launch::Damaged(_)));
        discard_marker(&app_dir).unwrap();
        assert_eq!(at_launch(&app_dir), Launch::Nothing, "a damaged marker is removed and the install goes on unmarked");
        assert!(app_dir.join("state").join("corsia.db").exists());
    }

    #[test]
    fn an_install_whose_service_file_names_another_state_folder_is_never_wiped() {
        let home = Home::new("adopted");
        let (app_dir, _) = unfinished(&home);
        let elsewhere = home.0.join("adopted-state");
        fs::create_dir_all(&elsewhere).unwrap();
        fs::write(app_dir.join(SERVICE_FILE), format!("{{\"state_dir\": \"{}\"}}", elsewhere.display())).unwrap();
        assert!(matches!(at_launch(&app_dir), Launch::Untrusted(_)));
    }

    fn downloads(app_dir: &Path) {
        let state = app_dir.join("state");
        for (relative, body) in [
            ("managed-omlx/models/starter/weights.bin", "model"),
            ("managed-omlx/settings.json", "{}"),
            ("assets/classifier/asset.bin", "asset"),
            ("logs/service.log", "log"),
        ] {
            let path = state.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, body).unwrap();
        }
    }

    #[test]
    fn a_state_folder_that_cannot_be_moved_aside_deletes_nothing() {
        use std::os::unix::fs::PermissionsExt;
        let home = Home::new("stuck-state");
        let (app_dir, workspace) = unfinished(&home);
        downloads(&app_dir);
        fs::set_permissions(&app_dir, fs::Permissions::from_mode(0o500)).unwrap();
        let result = wipe_it(&app_dir);
        fs::set_permissions(&app_dir, fs::Permissions::from_mode(0o700)).unwrap();
        result.expect_err("a state folder that cannot be renamed is reported");
        assert!(app_dir.join("state").join("corsia.db").exists(), "never half deleted");
        assert!(workspace.join(PROFILE).exists(), "the profile is still there: the old install is whole");
        assert!(app_dir.join(SERVICE_FILE).exists());
        assert!(app_dir.join("state/managed-omlx/models/starter/weights.bin").exists());
        assert!(app_dir.join(MARKER_FILE).exists());
        discard_marker(&app_dir).unwrap();
        assert_eq!(at_launch(&app_dir), Launch::Nothing, "the caller drops the marker and the old install resumes");
    }

    #[test]
    fn a_file_that_will_not_go_never_stops_the_wipe_and_is_tried_again_at_the_next_launch() {
        use std::os::unix::fs::PermissionsExt;
        let home = Home::new("stuck-file");
        let (app_dir, workspace) = unfinished(&home);
        let context = workspace.join("context");
        fs::set_permissions(&context, fs::Permissions::from_mode(0o500)).unwrap();
        let wiped = wipe_it(&app_dir);
        wiped.expect("the old install is gone, so a stuck file cannot fail the wipe");
        assert!(!app_dir.join("state").exists());
        assert!(!app_dir.join(SERVICE_FILE).exists());
        assert!(workspace.join(PROFILE).exists(), "the file that would not go is still there");
        assert!(
            matches!(at_launch(&app_dir), Launch::Replan(ref folder) if *folder == workspace),
            "the launch plans a clean first run in the folder the wiped setup made"
        );
        let marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        assert!(marker.wiped);
        assert_eq!(marker.files.keys().collect::<Vec<_>>(), [PROFILE], "only the stuck file is still recorded");
        fs::set_permissions(&context, fs::Permissions::from_mode(0o700)).unwrap();
        finish_leftovers(&app_dir);
        assert!(!workspace.join(PROFILE).exists(), "the next launch removes it");
        let marker: Marker = serde_json::from_slice(&fs::read(app_dir.join(MARKER_FILE)).unwrap()).unwrap();
        assert!(marker.files.is_empty());
    }

    #[test]
    fn downloads_survive_the_wipe_and_the_next_first_run_is_still_marked() {
        let home = Home::new("downloads");
        let (app_dir, workspace) = unfinished(&home);
        downloads(&app_dir);
        wipe_it(&app_dir).unwrap();
        let state = app_dir.join("state");
        assert_eq!(fs::read(state.join("managed-omlx/models/starter/weights.bin")).unwrap(), b"model");
        assert_eq!(fs::read(state.join("assets/classifier/asset.bin")).unwrap(), b"asset");
        assert_eq!(fs::read(state.join("logs/service.log")).unwrap(), b"log");
        assert!(!state.join("corsia.db").exists(), "setup state goes");
        assert!(!state.join("managed-omlx/settings.json").exists(), "the managed oMLX settings are setup state too");
        assert!(!app_dir.join("state.discarded").exists());
        assert!(matches!(at_launch(&app_dir), Launch::Replan(_)));
        assert!(
            mark(&app_dir, &workspace).unwrap(),
            "a state folder holding only downloads is still new, so quitting the next setup wipes it too"
        );
        fs::write(state.join("corsia.db"), b"db").unwrap();
        assert!(!mark(&app_dir, &workspace).unwrap(), "any other file makes it an install this setup did not create");
    }

    #[test]
    fn a_folder_set_aside_by_a_wipe_that_was_cut_short_gives_its_downloads_back() {
        let home = Home::new("cut-short");
        let (app_dir, _) = unfinished(&home);
        downloads(&app_dir);
        fs::rename(app_dir.join("state"), app_dir.join("state.discarded")).unwrap();
        wipe_it(&app_dir).unwrap();
        assert!(app_dir.join("state/managed-omlx/models/starter/weights.bin").exists());
        assert!(!app_dir.join("state.discarded").exists());
    }

    #[test]
    fn a_download_that_cannot_be_put_back_keeps_the_folder_aside_instead_of_deleting_it() {
        let home = Home::new("conflict");
        let (app_dir, _) = unfinished(&home);
        downloads(&app_dir);
        fs::rename(app_dir.join("state"), app_dir.join("state.discarded")).unwrap();
        fs::create_dir_all(app_dir.join("state/logs")).unwrap();
        fs::write(app_dir.join("state/logs/new.log"), b"new").unwrap();
        assert!(settle_discarded(&app_dir).is_err());
        assert!(app_dir.join("state/managed-omlx/models/starter/weights.bin").exists(), "the others came back");
        assert!(app_dir.join("state.discarded/logs/service.log").exists(), "the one that could not stays aside");
        assert_eq!(fs::read(app_dir.join("state/logs/new.log")).unwrap(), b"new");
    }
}
