//! "Start fresh": set an earlier install aside so first run can begin again.
//!
//! Every piece of the earlier install that first run detects (and the folders its
//! settings name, where that is safe) is renamed into
//! `~/CORSIA backups/<YYYY-MM-DD HH-MM>/`, keeping its place relative to the home
//! folder. Nothing is copied or deleted, so each move is undone by renaming it
//! back. A journal in the app folder lists every move before the first one, and
//! any failure, including the app closing partway, puts everything back.
//!
//! A source install's code folder is never moved: when `~/.corsia` is a Git
//! checkout only its `config.json` is set aside.

use std::{
    collections::BTreeSet,
    fs,
    io::ErrorKind,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    process::Command,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};

use crate::{
    adoption::{self, Phase},
    first_run::{self, FirstRun},
    release_proof,
    source_lifecycle::SourceLifecycle,
};

const JOURNAL: &str = "start-fresh.json";
pub const BACKUPS_FOLDER: &str = "CORSIA backups";
const PLIST: &str = "Library/LaunchAgents/com.corsia.prod.plist";
const STOP_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_POLL: Duration = Duration::from_millis(100);
/// Top-level home folders that hold far more than CORSIA. A folder an earlier
/// install's settings name is moved only when it is none of these.
const HOME_FOLDERS: [&str; 9] = [
    "Applications", "Desktop", "Documents", "Downloads", "Library", "Movies", "Music", "Pictures", "Public",
];

/// One rename, from where the earlier install had it to its place in the backup.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
struct Move {
    from: PathBuf,
    to: PathBuf,
}

#[derive(Deserialize, Serialize)]
struct Journal {
    backup: PathBuf,
    moves: Vec<Move>,
    /// The earlier install's service was stopped by this run, so undoing it starts it again.
    restart_old: bool,
}

/// What Start fresh would move, decided by reading only.
#[derive(Debug, PartialEq, Eq)]
pub struct Inventory {
    home: PathBuf,
    /// Home-folder paths to move, outermost only, in the order they are moved.
    items: Vec<PathBuf>,
    /// Code folders that stay where they are.
    checkouts: Vec<PathBuf>,
}

/// What the confirmation shows: every item that moves and the code folder that stays.
#[derive(Debug, PartialEq, Eq, Serialize)]
pub struct Preview {
    pub moves: Vec<String>,
    pub stays: Vec<String>,
    pub backups: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct StartFreshError {
    /// The person-facing line: what happened and where everything is now.
    pub message: String,
    /// The technical reason, shown under Details.
    pub details: String,
    /// Everything is where it was before Start fresh ran.
    pub restored: bool,
}

impl StartFreshError {
    pub(crate) fn untouched(details: impl Into<String>) -> Self {
        Self {
            message: "CORSIA didn’t set your earlier install aside. Nothing was changed.".into(),
            details: details.into(),
            restored: true,
        }
    }

    /// The step stopped partway for a reason that says nothing about the files. The journal
    /// decides what the next launch puts back, so quitting and opening CORSIA again is safe.
    pub(crate) fn unfinished(details: impl Into<String>) -> Self {
        Self {
            message: "CORSIA stopped partway. Quit and open CORSIA again, and it will put everything back.".into(),
            details: details.into(),
            restored: false,
        }
    }
}

/// The earlier install's service. Production asks launchd and the checkout's own
/// script; unit tests use a fake, so no test reaches launchd.
pub trait OldService {
    /// Stop the service if it runs and prove it let go. `true` when this call stopped it,
    /// so undoing Start fresh starts it again. `progress` receives the running-work count.
    fn stop(&mut self, progress: &mut dyn FnMut(Option<u32>)) -> Result<bool, String>;
    /// Whether the service runs now.
    fn running(&mut self) -> Result<bool, String>;
    /// Start the service again. Safe when it already runs.
    fn start(&mut self) -> Result<(), String>;
}

/// A path as the window shows it, with the home folder as `~`.
pub(crate) fn shown(home: &Path, path: &Path) -> String {
    let home = fs::canonicalize(home).unwrap_or_else(|_| home.to_path_buf());
    match path.strip_prefix(&home) {
        Ok(relative) => format!("~/{}", relative.display()),
        Err(_) => path.display().to_string(),
    }
}

/// A path with its parent resolved, so a symbolic link is itself and not where it points.
fn resolved(path: &Path) -> Option<PathBuf> {
    let parent = fs::canonicalize(path.parent()?).ok()?;
    Some(parent.join(path.file_name()?))
}

fn overlaps(a: &Path, b: &Path) -> bool {
    a.starts_with(b) || b.starts_with(a)
}

/// The roots and checkout a rendered source plist names, read loosely: a plist
/// adoption refuses still says where its pieces are.
struct PlistRoots {
    checkout: Option<PathBuf>,
    workspace: Option<PathBuf>,
    state: Option<PathBuf>,
    port: Option<u16>,
}

fn plist_roots(plist: &Path) -> PlistRoots {
    let value = plist::Value::from_file(plist).ok();
    let root = value.as_ref().and_then(plist::Value::as_dictionary);
    let environment = root
        .and_then(|root| root.get("EnvironmentVariables"))
        .and_then(plist::Value::as_dictionary);
    let text = |name: &str| {
        environment
            .and_then(|environment| environment.get(name))
            .and_then(plist::Value::as_string)
            .filter(|value| !value.is_empty())
    };
    PlistRoots {
        checkout: root
            .and_then(|root| root.get("WorkingDirectory"))
            .and_then(plist::Value::as_string)
            .map(PathBuf::from)
            .and_then(|backend| backend.parent().map(Path::to_path_buf)),
        workspace: text("CORSIA_WORKSPACE").map(PathBuf::from),
        state: text("CORSIA_STATE_DIR").map(PathBuf::from),
        port: text("BACKEND_PORT").and_then(|port| port.parse().ok()).filter(|port| *port != 0),
    }
}

fn is_checkout(path: &Path) -> bool {
    path.join(".git").exists()
}

/// Everything Start fresh would move, or why it must not run. Reads only.
pub fn inventory(home: &Path, app_dir: &Path) -> Result<Inventory, String> {
    let home = fs::canonicalize(home).map_err(|error| format!("could not read {}: {error}", home.display()))?;
    let home_device = fs::metadata(&home)
        .map_err(|error| format!("could not read {}: {error}", home.display()))?
        .dev();
    let present = |path: &Path| first_run::present(path);

    let plist = home.join(PLIST);
    let plist_roots = if present(&plist)? { Some(plist_roots(&plist)) } else { None };
    let dot_corsia = home.join(".corsia");
    let config = dot_corsia.join("config.json");

    let mut checkouts = BTreeSet::new();
    if is_checkout(&dot_corsia) {
        checkouts.insert(dot_corsia.clone());
    }
    if let Some(checkout) = plist_roots.as_ref().and_then(|roots| roots.checkout.as_ref()) {
        checkouts.insert(fs::canonicalize(checkout).unwrap_or_else(|_| checkout.clone()));
    }

    // What first run detects: each one must move, or Start fresh cannot run.
    let mut markers = Vec::new();
    if plist_roots.is_some() {
        markers.push(plist.clone());
    }
    if present(&config)? {
        markers.push(if checkouts.contains(&dot_corsia) { config.clone() } else { dot_corsia.clone() });
    }
    let legacy = home.join("Library").join("Application Support").join(first_run::LEGACY_STATE_FOLDER);
    if present(&legacy)? {
        markers.push(legacy);
    }
    for entry in fs::read_dir(&home).map_err(|error| format!("could not read {}: {error}", home.display()))? {
        let entry = entry.map_err(|error| format!("could not read {}: {error}", home.display()))?;
        if !entry.file_name().to_string_lossy().eq_ignore_ascii_case(first_run::WORKSPACE_FOLDER) {
            continue;
        }
        let path = entry.path();
        if !path.is_dir() || !first_run::workspace_is_fresh(&path)? {
            markers.push(path);
        }
    }
    for marker in &markers {
        let metadata = fs::symlink_metadata(marker)
            .map_err(|error| format!("could not read {}: {error}", marker.display()))?;
        if metadata.dev() != home_device {
            return Err(format!(
                "{} is on another disk, so it can’t be moved into the backup folder",
                shown(&home, marker)
            ));
        }
        let resolved = resolved(marker).unwrap_or_else(|| marker.clone());
        for checkout in &checkouts {
            if checkout.starts_with(&resolved) || (resolved.starts_with(checkout) && resolved != config) {
                return Err(format!(
                    "{} holds the code folder {}, which Start fresh never moves",
                    shown(&home, marker),
                    shown(&home, checkout)
                ));
            }
        }
    }

    // The folders the earlier install's settings name, where moving them is safe.
    // Anything else stays where it is: first run does not look at it.
    let mut extras = Vec::new();
    if let Some(roots) = &plist_roots {
        extras.extend(roots.workspace.iter().chain(roots.state.iter()).cloned());
    }
    if let Some((workspace, state)) = adoption::bootstrap_roots(&config) {
        extras.extend([workspace, state]);
    }
    let app_dir = fs::canonicalize(app_dir).unwrap_or_else(|_| app_dir.to_path_buf());
    let backups = home.join(BACKUPS_FOLDER);
    let mut items = markers;
    for extra in extras {
        let Some(path) = resolved(&extra) else { continue };
        let Ok(metadata) = fs::symlink_metadata(&path) else { continue };
        let Ok(relative) = path.strip_prefix(&home) else { continue };
        let top = relative.components().next().map(|part| part.as_os_str().to_string_lossy().into_owned());
        let depth = relative.components().count();
        // `~/Library/<folder>` is the system's (Preferences, Caches); only an app's own folder
        // inside one of those, such as `~/Library/Application Support/<app>`, can be CORSIA's.
        let deep_enough = match top.as_deref() {
            Some("Library") => depth >= 3,
            Some(top) if HOME_FOLDERS.contains(&top) => depth >= 2,
            _ => depth >= 1,
        };
        let safe = deep_enough
            && metadata.dev() == home_device
            && !app_dir.starts_with(&path)
            && !overlaps(&path, &backups)
            && !checkouts.iter().any(|checkout| overlaps(&path, checkout));
        if safe {
            items.push(path);
        }
    }

    // Outermost first; one rename covers everything inside it and each file moves once.
    let mut seen = BTreeSet::new();
    items.retain(|path| {
        fs::symlink_metadata(path).is_ok_and(|metadata| seen.insert((metadata.dev(), metadata.ino())))
    });
    items.sort_by_key(|path| path.components().count());
    let mut kept: Vec<PathBuf> = Vec::new();
    for item in items {
        if !kept.iter().any(|outer| item.starts_with(outer)) {
            kept.push(item);
        }
    }
    Ok(Inventory { home, items: kept, checkouts: checkouts.into_iter().collect() })
}

pub fn preview(home: &Path, app_dir: &Path) -> Result<Preview, String> {
    let inventory = inventory(home, app_dir)?;
    Ok(Preview {
        moves: inventory.items.iter().map(|item| shown(&inventory.home, item)).collect(),
        stays: inventory.checkouts.iter().map(|checkout| shown(&inventory.home, checkout)).collect(),
        backups: shown(&inventory.home, &inventory.home.join(BACKUPS_FOLDER)),
    })
}

/// The local date and minute, as the backup folder's name.
fn local_stamp(now: SystemTime) -> String {
    let seconds = now.duration_since(UNIX_EPOCH).map(|since| since.as_secs()).unwrap_or(0) as libc::time_t;
    // SAFETY: `localtime_r` writes only the `tm` it is given.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::localtime_r(&seconds, &mut tm) };
    format!(
        "{:04}-{:02}-{:02} {:02}-{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min
    )
}

/// A new, empty backup folder; a second run in the same minute gets ` (2)`.
fn create_backup(home: &Path, now: SystemTime) -> Result<PathBuf, String> {
    let backups = home.join(BACKUPS_FOLDER);
    fs::create_dir_all(&backups).map_err(|error| format!("could not create {}: {error}", backups.display()))?;
    let stamp = local_stamp(now);
    for attempt in 1..=99 {
        let name = if attempt == 1 { stamp.clone() } else { format!("{stamp} ({attempt})") };
        let backup = backups.join(name);
        match fs::create_dir(&backup) {
            Ok(()) => return Ok(backup),
            Err(error) if error.kind() == ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("could not create {}: {error}", backup.display())),
        }
    }
    Err(format!("could not find a free backup folder name in {}", backups.display()))
}

fn journal_path(app_dir: &Path) -> PathBuf {
    app_dir.join(JOURNAL)
}

fn write_journal(app_dir: &Path, journal: &Journal) -> Result<(), String> {
    fs::create_dir_all(app_dir).map_err(|error| format!("could not create {}: {error}", app_dir.display()))?;
    let contents = serde_json::to_vec_pretty(journal).map_err(|error| error.to_string())?;
    adoption::write_atomically(&journal_path(app_dir), &contents)
}

/// Put every move back, last first. Safe to repeat: a move already back is skipped.
/// The journal is removed only once everything is back.
fn roll_back(app_dir: &Path, journal: &Journal) -> Result<(), String> {
    let mut failures = Vec::new();
    for step in journal.moves.iter().rev() {
        let at_backup = first_run::present(&step.to);
        let at_home = first_run::present(&step.from);
        match (at_home, at_backup) {
            (Ok(false), Ok(true)) => {
                if let Err(error) = fs::rename(&step.to, &step.from) {
                    failures.push(format!("could not move {} back: {error}", step.from.display()));
                }
            }
            (Ok(true), Ok(false)) | (Ok(false), Ok(false)) => {}
            (Ok(true), Ok(true)) => failures.push(format!(
                "{} is both back in place and in the backup",
                step.from.display()
            )),
            (Err(error), _) | (_, Err(error)) => failures.push(error),
        }
    }
    remove_empty_folders(&journal.backup);
    if let Some(backups) = journal.backup.parent() {
        let _ = fs::remove_dir(backups);
    }
    if !failures.is_empty() {
        return Err(failures.join("; "));
    }
    match fs::remove_file(journal_path(app_dir)) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("could not remove the Start fresh journal: {error}")),
    }
}

/// The backup folder as the window shows it. It is always `<home>/CORSIA backups/<stamp>`.
fn backup_shown(backup: &Path) -> String {
    match backup.parent().and_then(Path::parent) {
        Some(home) => shown(home, backup),
        None => backup.display().to_string(),
    }
}

/// The folders made inside the backup to hold moved items, removed once empty.
fn remove_empty_folders(folder: &Path) {
    if let Ok(entries) = fs::read_dir(folder) {
        for entry in entries.flatten() {
            if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                remove_empty_folders(&entry.path());
            }
        }
    }
    let _ = fs::remove_dir(folder);
}

fn undo(app_dir: &Path, journal: &Journal, old: &mut dyn OldService, details: String) -> StartFreshError {
    match roll_back(app_dir, journal) {
        Ok(()) => {
            if journal.restart_old {
                if let Err(error) = old.start() {
                    log::warn!("the earlier install did not start again after Start fresh was undone: {error}");
                }
            }
            StartFreshError {
                message: "CORSIA couldn’t move everything into the backup folder, so it put it all back where it was."
                    .into(),
                details,
                restored: true,
            }
        }
        Err(error) => StartFreshError {
            message: format!(
                "CORSIA couldn’t put everything back. Nothing was deleted: what it moved is in {}.",
                backup_shown(&journal.backup)
            ),
            details: format!("{details}; then {error}"),
            restored: false,
        },
    }
}

/// Stop the earlier install, move every piece of it into a new backup folder and
/// prove first run now finds nothing. Any failure puts everything back. Returns the backup folder.
pub fn start_fresh(
    home: &Path,
    app_dir: &Path,
    old: &mut dyn OldService,
    progress: &mut dyn FnMut(Phase),
    now: SystemTime,
) -> Result<PathBuf, StartFreshError> {
    match first_run::plan(home, app_dir).map_err(StartFreshError::untouched)? {
        FirstRun::ExistingInstall { .. } | FirstRun::Adoptable { .. } => {}
        FirstRun::Configured | FirstRun::Fresh { .. } => {
            return Err(StartFreshError::untouched("there is no earlier install to set aside"))
        }
    }
    if first_run::present(&journal_path(app_dir)).map_err(StartFreshError::untouched)? {
        return Err(StartFreshError::untouched("an earlier Start fresh has not been undone yet"));
    }
    let inventory = inventory(home, app_dir).map_err(StartFreshError::untouched)?;

    progress(Phase::Waiting { running: None });
    let restart_old = match old.stop(&mut |running| progress(Phase::Waiting { running })) {
        Ok(stopped) => stopped,
        Err(error) => {
            // A stop that failed partway may have left it down; starting it is safe either way.
            if let Err(start) = old.start() {
                log::warn!("the earlier install did not start again after its stop failed: {start}");
            }
            return Err(StartFreshError {
                message: "CORSIA couldn’t stop your earlier install, so it left everything where it was.".into(),
                details: error,
                restored: true,
            });
        }
    };

    progress(Phase::MovingToBackup);
    let restart = |old: &mut dyn OldService| {
        if restart_old {
            if let Err(error) = old.start() {
                log::warn!("the earlier install did not start again: {error}");
            }
        }
    };
    let backup = match create_backup(&inventory.home, now) {
        Ok(backup) => backup,
        Err(error) => {
            restart(old);
            return Err(StartFreshError::untouched(error));
        }
    };
    let moves: Vec<Move> = inventory
        .items
        .iter()
        .map(|from| Move {
            to: backup.join(from.strip_prefix(&inventory.home).unwrap_or(from)),
            from: from.clone(),
        })
        .collect();
    let journal = Journal { backup: backup.clone(), moves, restart_old };
    if let Err(error) = write_journal(app_dir, &journal) {
        let _ = fs::remove_dir(&backup);
        let _ = fs::remove_dir(inventory.home.join(BACKUPS_FOLDER));
        restart(old);
        return Err(StartFreshError::untouched(error));
    }

    for step in &journal.moves {
        let moved = step
            .to
            .parent()
            .map_or(Ok(()), fs::create_dir_all)
            .and_then(|()| fs::rename(&step.from, &step.to));
        if let Err(error) = moved {
            progress(Phase::StartingExisting);
            let details = format!("could not move {}: {error}", shown(&inventory.home, &step.from));
            return Err(undo(app_dir, &journal, old, details));
        }
    }

    // Done only when the earlier service is still stopped and first run finds nothing.
    let finished = match old.running() {
        Ok(true) => Err("the earlier install started again while its files were being moved".to_string()),
        Ok(false) => match first_run::plan(home, app_dir) {
            Ok(FirstRun::Fresh { .. }) => Ok(()),
            Ok(other) => Err(format!("first run still finds an earlier install: {other:?}")),
            Err(error) => Err(error),
        },
        Err(error) => Err(error),
    };
    if let Err(details) = finished {
        progress(Phase::StartingExisting);
        return Err(undo(app_dir, &journal, old, details));
    }
    if let Err(error) = fs::remove_file(journal_path(app_dir)) {
        // A journal left behind would undo the finished move on the next launch.
        progress(Phase::StartingExisting);
        return Err(undo(app_dir, &journal, old, format!("could not finish: {error}")));
    }
    Ok(backup)
}

/// A Start fresh the app did not finish (it quit or crashed partway) is undone
/// before launch decides anything, so the earlier install is found whole again.
pub fn recover_on_launch(app_dir: &Path, start_old: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
    let path = journal_path(app_dir);
    let contents = match fs::read(&path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("could not read the Start fresh journal: {error}")),
    };
    let journal: Journal = serde_json::from_slice(&contents)
        .map_err(|error| format!("could not read the Start fresh journal: {error}"))?;
    roll_back(app_dir, &journal).map_err(|error| {
        format!(
            "CORSIA couldn’t finish putting your earlier install back ({error}). Nothing was deleted: \
             what it moved is in {}.",
            backup_shown(&journal.backup)
        )
    })?;
    if journal.restart_old {
        if let Err(error) = start_old() {
            log::warn!("the earlier install did not start again after an unfinished Start fresh: {error}");
        }
    }
    Ok(())
}

/// The earlier install's service as launchd runs it. Its own `scripts/corsia stop`
/// drains it when the install is one this app could adopt; otherwise launchd boots it out.
pub struct LaunchdService {
    source: Option<SourceLifecycle>,
    plist: PathBuf,
    port: Option<u16>,
    databases: Vec<PathBuf>,
    python: PathBuf,
}

impl LaunchdService {
    pub fn new(home: &Path, source: Option<SourceLifecycle>, python: PathBuf) -> Self {
        let plist = home.join(PLIST);
        let roots = plist_roots(&plist);
        let mut states: Vec<PathBuf> = roots.state.into_iter().collect();
        if let Some((_, state)) = adoption::bootstrap_roots(&home.join(".corsia").join("config.json")) {
            states.push(state);
        }
        states.push(
            home.join("Library")
                .join("Application Support")
                .join(first_run::LEGACY_STATE_FOLDER)
                .join("state"),
        );
        Self {
            source,
            plist,
            port: roots.port,
            databases: states.into_iter().map(|state| state.join("corsia.db")).collect(),
            python,
        }
    }

    fn target() -> String {
        format!("gui/{}/{}", unsafe { libc::geteuid() }, adoption::SOURCE_LABEL)
    }

    fn loaded(&self) -> Result<bool, String> {
        Command::new("/bin/launchctl")
            .args(["print", &Self::target()])
            .output()
            .map(|output| output.status.success())
            .map_err(|error| format!("could not ask launchd about the earlier install: {error}"))
    }

    fn released(&mut self) -> Result<(), String> {
        if self.running()? {
            return Err("the earlier install is still running".into());
        }
        for database in self.databases.iter().filter(|database| database.is_file()) {
            release_proof::database_released(&self.python, database)?;
        }
        Ok(())
    }
}

impl OldService for LaunchdService {
    fn stop(&mut self, progress: &mut dyn FnMut(Option<u32>)) -> Result<bool, String> {
        if !self.running()? {
            return Ok(false);
        }
        if let Some(source) = self.source.as_mut() {
            use adoption::Lifecycle;
            match source.stop_old(progress, &mut |_| Ok(())) {
                Ok(()) => return Ok(true),
                Err(error) => log::warn!("`corsia stop` did not stop the earlier install, so launchd stops it: {error}"),
            }
        }
        progress(None);
        let status = Command::new("/bin/launchctl")
            .args(["bootout", &Self::target()])
            .status()
            .map_err(|error| format!("could not ask launchd to stop the earlier install: {error}"))?;
        if !status.success() {
            log::warn!("`launchctl bootout` exited {status}; checking whether the earlier install stopped");
        }
        let deadline = Instant::now() + STOP_TIMEOUT;
        loop {
            match self.released() {
                Ok(()) => return Ok(true),
                Err(error) if Instant::now() >= deadline => {
                    return Err(format!("the earlier install did not stop: {error}"))
                }
                Err(_) => thread::sleep(STOP_POLL),
            }
        }
    }

    fn running(&mut self) -> Result<bool, String> {
        Ok(self.loaded()? || self.port.is_some_and(|port| !release_proof::port_refuses(port)))
    }

    fn start(&mut self) -> Result<(), String> {
        if let Some(source) = self.source.as_mut() {
            use adoption::Lifecycle;
            return source.start_old();
        }
        if self.loaded()? || !self.plist.is_file() {
            return Ok(());
        }
        let status = Command::new("/bin/launchctl")
            .arg("bootstrap")
            .arg(format!("gui/{}", unsafe { libc::geteuid() }))
            .arg(&self.plist)
            .status()
            .map_err(|error| format!("could not ask launchd to start the earlier install: {error}"))?;
        status
            .success()
            .then_some(())
            .ok_or_else(|| format!("`launchctl bootstrap` exited {status}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Home(PathBuf);

    impl Home {
        fn new(label: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "corsia-start-fresh-{label}-{}-{:?}",
                std::process::id(),
                Instant::now()
            ));
            fs::create_dir_all(&root).unwrap();
            Home(fs::canonicalize(root).unwrap())
        }

        fn app_dir(&self) -> PathBuf {
            crate::app_paths::app_dir_for_home(&self.0)
        }

        fn write(&self, relative: &str, contents: &str) {
            let path = self.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, contents).unwrap();
        }

        /// Every file under the home with its contents, leaving out the app folder.
        fn files(&self) -> Vec<(String, String)> {
            fn walk(base: &Path, dir: &Path, skip: &Path, out: &mut Vec<(String, String)>) {
                for entry in fs::read_dir(dir).unwrap().flatten() {
                    let path = entry.path();
                    if path == skip {
                        continue;
                    }
                    if path.is_dir() {
                        walk(base, &path, skip, out);
                    } else {
                        let relative = path.strip_prefix(base).unwrap().to_string_lossy().into_owned();
                        out.push((relative, fs::read_to_string(&path).unwrap_or_default()));
                    }
                }
            }
            let mut out = Vec::new();
            walk(&self.0, &self.0, &self.app_dir(), &mut out);
            out.sort();
            out
        }

        fn backups(&self) -> Vec<PathBuf> {
            match fs::read_dir(self.0.join(BACKUPS_FOLDER)) {
                Ok(entries) => entries.flatten().map(|entry| entry.path()).collect(),
                Err(_) => Vec::new(),
            }
        }
    }

    impl Drop for Home {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[derive(Default)]
    struct FakeService {
        running: bool,
        refuse_stop: bool,
        /// Starts again on its own once stopped, as a job someone restarts would.
        comes_back: bool,
        stops: u32,
        starts: u32,
    }

    impl OldService for FakeService {
        fn stop(&mut self, progress: &mut dyn FnMut(Option<u32>)) -> Result<bool, String> {
            if self.refuse_stop {
                return Err("it would not stop".into());
            }
            progress(Some(2));
            let was_running = self.running;
            self.running = self.comes_back;
            self.stops += 1;
            Ok(was_running)
        }

        fn running(&mut self) -> Result<bool, String> {
            Ok(self.running)
        }

        fn start(&mut self) -> Result<(), String> {
            self.starts += 1;
            self.running = true;
            Ok(())
        }
    }

    fn plist(home: &Home, checkout: &Path, workspace: &Path, state: &Path) {
        home.write(
            PLIST,
            &format!(
                r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>com.corsia.prod</string><key>WorkingDirectory</key><string>{}</string><key>EnvironmentVariables</key><dict><key>CORSIA_WORKSPACE</key><string>{}</string><key>CORSIA_STATE_DIR</key><string>{}</string><key>BACKEND_PORT</key><string>47895</string></dict></dict></plist>"#,
                checkout.join("backend").display(),
                workspace.display(),
                state.display()
            ),
        );
    }

    /// A source install as `install.sh` leaves it: a checkout at `~/.corsia` with its
    /// bootstrap file, a loaded plist, a workspace and the legacy state folder.
    fn source_install(home: &Home) {
        home.write(".corsia/.git/HEAD", "ref: refs/heads/main");
        home.write(".corsia/scripts/corsia", "#!/bin/sh\n");
        home.write(".corsia/backend/main.py", "code");
        home.write(".corsia/.env", "KEY=value");
        let state = home.0.join("Library/Application Support/com.cebertowicz.corsia/state");
        home.write(
            ".corsia/config.json",
            &format!(r#"{{"workspace":{{"root":"{}"}},"state_root":"{}"}}"#, home.0.join("Corsia").display(), state.display()),
        );
        home.write("Corsia/projects/notes.md", "mine");
        home.write("Library/Application Support/com.cebertowicz.corsia/state/corsia.db", "db");
        plist(home, &home.0.join(".corsia"), &home.0.join("Corsia"), &state);
    }

    fn run(home: &Home, service: &mut FakeService) -> Result<PathBuf, StartFreshError> {
        run_at(home, service, SystemTime::now())
    }

    fn run_at(home: &Home, service: &mut FakeService, now: SystemTime) -> Result<PathBuf, StartFreshError> {
        let mut phases = Vec::new();
        let result = start_fresh(&home.0, &home.app_dir(), service, &mut |phase| phases.push(phase), now);
        if result.is_ok() {
            assert_eq!(phases.last(), Some(&Phase::MovingToBackup));
        }
        result
    }

    #[test]
    fn a_source_install_is_set_aside_whole_and_its_code_stays() {
        let home = Home::new("source");
        source_install(&home);
        let before = home.files();
        let mut service = FakeService { running: true, ..Default::default() };

        let backup = run(&home, &mut service).unwrap();

        assert_eq!(service.stops, 1);
        assert_eq!(service.starts, 0, "a finished Start fresh leaves the earlier install stopped");
        assert!(backup.starts_with(home.0.join(BACKUPS_FOLDER)));
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Fresh { .. }));
        // The code folder stays; only its bootstrap file moves.
        for kept in [".corsia/.git/HEAD", ".corsia/scripts/corsia", ".corsia/backend/main.py", ".corsia/.env"] {
            assert!(home.0.join(kept).is_file(), "{kept} stays in place");
        }
        assert!(!home.0.join(".corsia/config.json").exists());
        assert!(backup.join(".corsia/config.json").is_file());
        assert!(backup.join(PLIST).is_file());
        assert!(backup.join("Corsia/projects/notes.md").is_file());
        assert!(backup.join("Library/Application Support/com.cebertowicz.corsia/state/corsia.db").is_file());
        assert!(!journal_path(&home.app_dir()).exists());
        // Nothing was deleted or changed: every file is in place or in the backup, with its contents.
        let prefix = backup.strip_prefix(&home.0).unwrap().to_string_lossy().into_owned() + "/";
        let mut after: Vec<(String, String)> = home
            .files()
            .into_iter()
            .map(|(path, contents)| (path.strip_prefix(&prefix).map(str::to_string).unwrap_or(path), contents))
            .collect();
        after.sort();
        assert_eq!(after, before);
    }

    #[test]
    fn the_backup_folder_does_not_trip_the_detector_and_a_second_run_gets_its_own_folder() {
        let home = Home::new("twice");
        // Both runs in the same minute.
        let now = UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        home.write("CORSIA/notes.md", "first");
        let first = run_at(&home, &mut FakeService::default(), now).unwrap();
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Fresh { .. }));

        home.write("CORSIA/notes.md", "second");
        let second = run_at(&home, &mut FakeService::default(), now).unwrap();
        assert_ne!(first, second);
        assert_eq!(home.backups().len(), 2);
        assert!(second.to_string_lossy().ends_with(" (2)"));
        assert_eq!(fs::read_to_string(first.join("CORSIA/notes.md")).unwrap(), "first");
        assert_eq!(fs::read_to_string(second.join("CORSIA/notes.md")).unwrap(), "second");
    }

    #[test]
    fn each_detected_install_is_set_aside() {
        type Setup = fn(&Home);
        let cases: [(&str, Setup); 5] = [
            ("bootstrap-only", |home| home.write(".corsia/config.json", "{}")),
            ("legacy", |home| home.write("Library/Application Support/com.cebertowicz.corsia/state/corsia.db", "db")),
            ("workspace", |home| home.write("corsia/notes.md", "mine")),
            ("workspace-file", |home| home.write("CORSIA", "a file")),
            ("bad-plist", |home| home.write(PLIST, "not a plist")),
        ];
        for (label, setup) in cases {
            let home = Home::new(label);
            setup(&home);
            assert!(
                matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::ExistingInstall { .. }),
                "{label} is detected"
            );
            let backup = run(&home, &mut FakeService::default()).unwrap_or_else(|error| panic!("{label}: {error:?}"));
            assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Fresh { .. }), "{label}");
            assert!(fs::read_dir(&backup).unwrap().next().is_some(), "{label} moved something");
        }
    }

    #[test]
    fn an_adoptable_install_is_set_aside() {
        let home = Home::new("adoptable");
        source_install(&home);
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Adoptable { .. }));
        run(&home, &mut FakeService { running: true, ..Default::default() }).unwrap();
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Fresh { .. }));
    }

    #[test]
    fn a_failed_move_puts_everything_back_and_starts_the_earlier_install() {
        let home = Home::new("rollback");
        source_install(&home);
        let before = home.files();
        let mut service = FakeService { running: true, ..Default::default() };
        // The legacy folder moves after the shallower items; a read-only parent makes its rename fail.
        fs::create_dir_all(home.app_dir()).unwrap();
        let parent = home.0.join("Library/Application Support");
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o555)).unwrap();
        let result = run(&home, &mut service);
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o755)).unwrap();

        let error = result.unwrap_err();
        assert!(error.restored, "{error:?}");
        assert!(error.message.contains("put it all back"));
        assert!(error.details.contains("com.cebertowicz.corsia"), "{}", error.details);
        assert_eq!(home.files(), before, "the earlier install is whole again");
        assert!(home.backups().is_empty(), "the empty backup folder is removed");
        assert!(!journal_path(&home.app_dir()).exists());
        assert_eq!(service.starts, 1, "the stopped install is started again");
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Adoptable { .. }));
    }

    #[test]
    fn an_install_that_starts_again_midway_is_put_back() {
        let home = Home::new("came-back");
        source_install(&home);
        let before = home.files();
        let mut service = FakeService { running: true, comes_back: true, ..Default::default() };

        let error = run(&home, &mut service).unwrap_err();

        assert!(error.restored);
        assert!(error.details.contains("started again"));
        assert_eq!(home.files(), before);
    }

    #[test]
    fn a_stop_that_fails_changes_nothing() {
        let home = Home::new("no-stop");
        source_install(&home);
        let before = home.files();
        let mut service = FakeService { running: true, refuse_stop: true, ..Default::default() };

        let error = run(&home, &mut service).unwrap_err();

        assert!(error.restored);
        assert!(error.message.contains("couldn’t stop"));
        assert_eq!(home.files(), before);
        assert!(home.backups().is_empty());
        assert!(!journal_path(&home.app_dir()).exists());
    }

    #[test]
    fn an_unfinished_start_fresh_is_undone_on_the_next_launch() {
        let home = Home::new("relaunch");
        source_install(&home);
        let before = home.files();
        // The app closed after the first of its moves: the journal lists them all.
        let inventory = inventory(&home.0, &home.app_dir()).unwrap();
        let backup = create_backup(&inventory.home, SystemTime::now()).unwrap();
        let moves: Vec<Move> = inventory
            .items
            .iter()
            .map(|from| Move { to: backup.join(from.strip_prefix(&inventory.home).unwrap()), from: from.clone() })
            .collect();
        write_journal(&home.app_dir(), &Journal { backup: backup.clone(), moves: moves.clone(), restart_old: true })
            .unwrap();
        fs::create_dir_all(moves[0].to.parent().unwrap()).unwrap();
        fs::rename(&moves[0].from, &moves[0].to).unwrap();

        let mut started = false;
        recover_on_launch(&home.app_dir(), || {
            started = true;
            Ok(())
        })
        .unwrap();

        assert!(started);
        assert_eq!(home.files(), before);
        assert!(home.backups().is_empty());
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Adoptable { .. }));
        // Nothing left to undo: a second launch changes nothing.
        recover_on_launch(&home.app_dir(), || panic!("no journal, no start")).unwrap();
    }

    #[test]
    fn a_finished_start_fresh_plans_a_fresh_first_run_on_relaunch() {
        let home = Home::new("finished");
        source_install(&home);
        run(&home, &mut FakeService { running: true, ..Default::default() }).unwrap();
        recover_on_launch(&home.app_dir(), || panic!("nothing to undo")).unwrap();
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Fresh { .. }));
    }

    #[test]
    fn a_workspace_holding_the_code_folder_is_refused_before_anything_stops() {
        let home = Home::new("code-inside");
        let checkout = home.0.join("Corsia/source/corsia");
        home.write("Corsia/source/corsia/.git/HEAD", "ref");
        home.write("Corsia/source/corsia/scripts/corsia", "#!/bin/sh\n");
        plist(&home, &checkout, &home.0.join("Corsia"), &home.0.join("state"));
        let before = home.files();
        let mut service = FakeService { running: true, ..Default::default() };

        let error = run(&home, &mut service).unwrap_err();

        assert!(error.details.contains("code folder"), "{}", error.details);
        assert_eq!(service.stops, 0);
        assert_eq!(home.files(), before);
        assert!(home.backups().is_empty());
    }

    #[test]
    fn folders_the_settings_name_move_only_when_safe() {
        let home = Home::new("extras");
        home.write("work/notes.md", "mine");
        home.write("Documents/keep.md", "theirs");
        home.write("Library/Preferences/keep.plist", "system");
        home.write(
            PLIST,
            &format!(
                r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>com.corsia.prod</string><key>EnvironmentVariables</key><dict><key>CORSIA_STATE_DIR</key><string>{}</string></dict></dict></plist>"#,
                home.0.join("Library/Preferences").display()
            ),
        );
        home.write(
            ".corsia/config.json",
            &format!(r#"{{"workspace":{{"root":"{}"}},"state_root":"{}"}}"#, home.0.join("work").display(), home.0.join("Documents").display()),
        );
        let preview = preview(&home.0, &home.app_dir()).unwrap();
        assert_eq!(
            preview.moves,
            vec!["~/.corsia".to_string(), "~/work".to_string(), "~/Library/LaunchAgents/com.corsia.prod.plist".to_string()]
        );
        assert!(preview.stays.is_empty());
        assert_eq!(preview.backups, format!("~/{BACKUPS_FOLDER}"));
        run(&home, &mut FakeService::default()).unwrap();
        assert!(home.0.join("Documents/keep.md").is_file(), "a standard home folder never moves");
        assert!(home.0.join("Library/Preferences/keep.plist").is_file(), "a system folder in Library never moves");
    }

    #[test]
    fn a_configured_or_fresh_computer_is_never_touched() {
        let home = Home::new("nothing");
        let error = run(&home, &mut FakeService::default()).unwrap_err();
        assert!(error.restored);
        assert!(home.files().is_empty());
        assert!(home.backups().is_empty());
    }

    #[test]
    fn the_preview_names_the_code_folder_that_stays() {
        let home = Home::new("preview");
        source_install(&home);
        let preview = preview(&home.0, &home.app_dir()).unwrap();
        assert_eq!(
            preview.moves,
            vec![
                "~/Corsia".to_string(),
                "~/.corsia/config.json".to_string(),
                "~/Library/LaunchAgents/com.corsia.prod.plist".to_string(),
                "~/Library/Application Support/com.cebertowicz.corsia".to_string(),
            ]
        );
        assert_eq!(preview.stays, vec!["~/.corsia".to_string()]);
    }

    #[test]
    fn a_backup_is_named_from_the_home_folder() {
        let home = Home::new("shown");
        let backup = home.0.join(BACKUPS_FOLDER).join("2026-10-22 13-05");
        assert_eq!(backup_shown(&backup), format!("~/{BACKUPS_FOLDER}/2026-10-22 13-05"));
    }

    #[test]
    fn the_stamp_is_the_local_date_and_minute() {
        let stamp = local_stamp(SystemTime::now());
        assert_eq!(stamp.len(), "2026-10-22 13-05".len());
        assert_eq!(&stamp[4..5], "-");
        assert_eq!(&stamp[10..11], " ");
    }
}
