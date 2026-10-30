//! "Start fresh": set an earlier install aside so first run can begin again.
//!
//! Every piece of the earlier install that first run detects (and the state folders
//! its settings name) is renamed into `~/CORSIA backups/<YYYY-MM-DD HH-MM>/`,
//! keeping its place relative to the home folder. Nothing is copied or deleted, so
//! each move is undone by renaming it back. A journal in the app folder lists every
//! move before the earlier install is stopped, and any failure, including the app
//! closing partway, puts everything back and starts the earlier install again.
//!
//! A source install's code folder is never moved: when `~/.corsia` is a Git
//! checkout only its `config.json` is set aside. A work folder the settings name
//! somewhere else stays where it is: it may hold the person's other work.

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
    adoption::{self, Phase, StopProcess},
    app_paths,
    first_run::{self, FirstRun},
    release_proof,
    source_lifecycle::SourceLifecycle,
};

const JOURNAL: &str = "start-fresh.json";
pub const BACKUPS_FOLDER: &str = "CORSIA backups";
const PLIST: &str = "Library/LaunchAgents/com.corsia.prod.plist";
const STOP_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_POLL: Duration = Duration::from_millis(100);

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
    /// The earlier install's service ran when this run began, so undoing it starts it again.
    restart_old: bool,
    /// The checkout's own `corsia stop`, recorded as soon as it exists: a relaunch after
    /// Force Quit waits for it to finish draining before starting the earlier install.
    #[serde(default)]
    stop: Option<StopProcess>,
}

/// What Start fresh would move, decided by reading only.
#[derive(Debug, PartialEq, Eq)]
pub struct Inventory {
    home: PathBuf,
    /// Home-folder paths to move, outermost only, in the order they are moved.
    items: Vec<PathBuf>,
    /// Code folders that stay where they are.
    checkouts: Vec<PathBuf>,
    /// Work folders the settings name outside the detected pieces, which stay too.
    work: Vec<PathBuf>,
}

/// What the confirmation shows: every item that moves and the folders that stay.
#[derive(Debug, PartialEq, Eq, Serialize)]
pub struct Preview {
    pub moves: Vec<String>,
    pub code: Vec<String>,
    pub work: Vec<String>,
    pub backups: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct StartFreshError {
    /// The person-facing line: what happened and where everything is now.
    pub message: String,
    /// The technical reason, shown under Details.
    pub details: String,
    /// Every file is where it was before Start fresh ran, so Retry starts over.
    pub restored: bool,
    /// The earlier install ran before Start fresh and isn't running now. The window must
    /// say so, so the app stays open even when Quit was asked meanwhile.
    pub left_stopped: bool,
    /// No earlier install is left to set aside, so setup goes on instead of Retry.
    pub nothing_to_move: bool,
}

impl StartFreshError {
    fn new(message: impl Into<String>, details: impl Into<String>, restored: bool) -> Self {
        Self { message: message.into(), details: details.into(), restored, left_stopped: false, nothing_to_move: false }
    }

    pub(crate) fn untouched(details: impl Into<String>) -> Self {
        Self::new("CORSIA couldn’t set your earlier install aside. Nothing was changed.", details, true)
    }

    /// Start fresh can't run on this computer as it is: `message` says why and what the person
    /// can do, and `details` how, when that takes more than the line can say.
    fn refused(message: String, details: String) -> Self {
        Self::new(message, details, true)
    }

    /// The earlier install's pieces are gone, or this app is already set up.
    fn nothing_to_move(details: impl Into<String>) -> Self {
        Self {
            nothing_to_move: true,
            ..Self::new(
                "Your earlier install is no longer on this computer, so there’s nothing to move. \
                 Choose Set up CORSIA to go on.",
                details,
                true,
            )
        }
    }

    /// Every file is back, but the earlier install that ran before did not start again.
    fn not_started(details: String) -> Self {
        Self { left_stopped: true, ..Self::new(NOT_STARTED, details, true) }
    }

    /// The step stopped partway for a reason that says nothing about the files. The journal
    /// decides what the next launch puts back, so quitting and opening CORSIA again is safe.
    pub(crate) fn unfinished(details: impl Into<String>) -> Self {
        Self::new("CORSIA stopped partway. Quit and open CORSIA again, and it will put everything back.", details, false)
    }

    /// Some moves could not be put back. The journal stays, so the next launch tries again.
    fn not_put_back(journal: &Journal, details: String) -> Self {
        let message = format!(
            "CORSIA couldn’t put everything back, but nothing was deleted: what it moved is in {}. \
             Reopen CORSIA to try again.",
            backup_shown(&journal.backup)
        );
        Self::new(message, details, false)
    }
}

/// Said after the files are back when the earlier install did not start again, whatever
/// failed first; Details says what that was.
const NOT_STARTED: &str = "Everything is back where it was, but CORSIA couldn’t start your earlier install \
                           again, so it isn’t running. Choose Retry to try Start fresh again.";

/// The earlier install's service. Production asks launchd and the checkout's own
/// script; unit tests use a fake, so no test reaches launchd.
pub trait OldService {
    /// Stop the service if it runs and prove it let go. `progress` receives the running-work
    /// count; `spawned` receives the checkout's own `corsia stop` process as soon as it exists.
    fn stop(
        &mut self,
        progress: &mut dyn FnMut(Option<u32>),
        spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
    ) -> Result<(), String>;
    /// Whether the service runs now.
    fn running(&mut self) -> Result<bool, String>;
    /// Start the service again once `stop`, a `corsia stop` an earlier run left, has exited.
    /// Safe when it already runs.
    fn start(&mut self, stop: Option<&StopProcess>) -> Result<(), String>;
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
pub fn inventory(home: &Path, app_dir: &Path) -> Result<Inventory, StartFreshError> {
    match first_run::plan(home, app_dir).map_err(StartFreshError::untouched)? {
        FirstRun::ExistingInstall { .. } | FirstRun::Adoptable { .. } => {}
        FirstRun::Configured | FirstRun::Fresh { .. } => {
            return Err(StartFreshError::nothing_to_move("first run finds no earlier install"))
        }
    }
    let unreadable = |path: &Path, error: std::io::Error| {
        StartFreshError::untouched(format!("could not read {}: {error}", path.display()))
    };
    let home = fs::canonicalize(home).map_err(|error| unreadable(home, error))?;
    let home_device = fs::metadata(&home).map_err(|error| unreadable(&home, error))?.dev();
    let present = |path: &Path| first_run::present(path).map_err(StartFreshError::untouched);

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
    for entry in fs::read_dir(&home).map_err(|error| unreadable(&home, error))? {
        let entry = entry.map_err(|error| unreadable(&home, error))?;
        if !entry.file_name().to_string_lossy().eq_ignore_ascii_case(first_run::WORKSPACE_FOLDER) {
            continue;
        }
        let path = entry.path();
        if !path.is_dir() || !first_run::workspace_is_fresh(&path).map_err(StartFreshError::untouched)? {
            markers.push(path);
        }
    }
    for marker in &markers {
        let metadata = fs::symlink_metadata(marker).map_err(|error| unreadable(marker, error))?;
        let name = shown(&home, marker);
        if metadata.dev() != home_device {
            return Err(StartFreshError::refused(format!(
                "{name} is on another disk, so CORSIA can’t move it into the backup folder. \
                 Rename it or move it off that disk yourself, then choose Retry."
            ), String::new()));
        }
        let resolved = resolved(marker).unwrap_or_else(|| marker.clone());
        for checkout in &checkouts {
            let holds = checkout.starts_with(&resolved);
            if holds || (resolved.starts_with(checkout) && resolved != config) {
                let code = shown(&home, checkout);
                return Err(StartFreshError::refused(
                    format!(
                        "{name} is {} CORSIA’s code folder, {code}, and Start fresh never moves code. \
                         Stop your earlier install and move {name} yourself, then choose Retry.",
                        if holds { "holding" } else { "inside" }
                    ),
                    format!("To stop it, run {code}/scripts/corsia stop in Terminal."),
                ));
            }
        }
    }

    // A state folder the earlier install's settings name moves when it holds that install's
    // database. A work folder named there stays: it may hold the person's other work, and
    // first run does not look at it.
    let mut states = Vec::new();
    let mut workspaces = Vec::new();
    if let Some(roots) = &plist_roots {
        states.extend(roots.state.iter().cloned());
        workspaces.extend(roots.workspace.iter().cloned());
    }
    if let Some((workspace, state)) = adoption::bootstrap_roots(&config) {
        states.push(state);
        workspaces.push(workspace);
    }
    let app_dir = fs::canonicalize(app_dir).unwrap_or_else(|_| app_dir.to_path_buf());
    let backups = home.join(BACKUPS_FOLDER);
    let mut items = markers;
    for state in states {
        let Some(path) = resolved(&state) else { continue };
        let Ok(metadata) = fs::symlink_metadata(&path) else { continue };
        let movable = metadata.is_dir()
            && path.join("corsia.db").is_file()
            && path.starts_with(&home)
            && metadata.dev() == home_device
            && !overlaps(&path, &app_dir)
            && !overlaps(&path, &backups)
            && !checkouts.iter().any(|checkout| overlaps(&path, checkout));
        if movable {
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
    if kept.is_empty() {
        return Err(StartFreshError::nothing_to_move("none of the earlier install's pieces are left"));
    }
    let mut work: Vec<PathBuf> = workspaces
        .iter()
        .filter_map(|workspace| resolved(workspace))
        .filter(|workspace| workspace.is_dir() && !kept.iter().any(|item| workspace.starts_with(item)))
        .collect();
    work.sort();
    work.dedup();
    Ok(Inventory { home, items: kept, checkouts: checkouts.into_iter().collect(), work })
}

pub fn preview(home: &Path, app_dir: &Path) -> Result<Preview, StartFreshError> {
    let inventory = inventory(home, app_dir)?;
    let shown_all = |paths: &[PathBuf]| paths.iter().map(|path| shown(&inventory.home, path)).collect();
    Ok(Preview {
        moves: shown_all(&inventory.items),
        code: shown_all(&inventory.checkouts),
        work: shown_all(&inventory.work),
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
/// The journal stays: `put_back` removes it once the earlier install is started too.
fn roll_back(journal: &Journal) -> Result<(), String> {
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
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("; "))
    }
}

/// How putting an unfinished Start fresh back went.
enum PutBack {
    /// Every file is back and the earlier install runs again if it ran before.
    Done,
    /// Every file is back, but the earlier install did not start again.
    NotStarted(String),
    /// Some files are still in the backup; the journal stays for the next launch.
    Files(String),
}

/// Put every move back, then start the earlier install again if it ran before. The journal
/// is removed last, so the app closing partway repeats both on the next launch.
fn put_back(
    app_dir: &Path,
    journal: &Journal,
    start_old: impl FnOnce(Option<&StopProcess>) -> Result<(), String>,
) -> PutBack {
    if let Err(error) = roll_back(journal) {
        return PutBack::Files(error);
    }
    let started = if journal.restart_old { start_old(journal.stop.as_ref()) } else { Ok(()) };
    match fs::remove_file(journal_path(app_dir)) {
        Err(error) if error.kind() != ErrorKind::NotFound => {
            PutBack::Files(format!("could not remove the Start fresh journal: {error}"))
        }
        _ => match started {
            Ok(()) => PutBack::Done,
            Err(error) => PutBack::NotStarted(error),
        },
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

/// Undo this run's moves and say what happened. `back` is the line for when everything is
/// where it was and the earlier install runs again.
fn undo(app_dir: &Path, journal: &Journal, old: &mut dyn OldService, back: &str, details: String) -> StartFreshError {
    match put_back(app_dir, journal, |stop| old.start(stop)) {
        PutBack::Done => StartFreshError::new(back, details, true),
        PutBack::NotStarted(error) => StartFreshError::not_started(format!("{details}; then {error}")),
        PutBack::Files(error) => StartFreshError::not_put_back(journal, format!("{details}; then {error}")),
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
    if first_run::present(&journal_path(app_dir)).map_err(StartFreshError::untouched)? {
        return Err(StartFreshError::untouched("an earlier Start fresh has not been undone yet"));
    }
    let inventory = inventory(home, app_dir)?;
    let restart_old = old.running().map_err(StartFreshError::untouched)?;

    // The journal is written before the stop, so a relaunch after the app closes at any
    // point from here puts the files back and starts the earlier install again.
    let backup = create_backup(&inventory.home, now).map_err(StartFreshError::untouched)?;
    let moves: Vec<Move> = inventory
        .items
        .iter()
        .map(|from| Move {
            to: backup.join(from.strip_prefix(&inventory.home).unwrap_or(from)),
            from: from.clone(),
        })
        .collect();
    let mut journal = Journal { backup: backup.clone(), moves, restart_old, stop: None };
    if let Err(error) = write_journal(app_dir, &journal) {
        let _ = fs::remove_dir(&backup);
        let _ = fs::remove_dir(inventory.home.join(BACKUPS_FOLDER));
        return Err(StartFreshError::untouched(error));
    }

    progress(Phase::Waiting { running: None });
    let stopped = old.stop(&mut |running| progress(Phase::Waiting { running }), &mut |stop| {
        journal.stop = Some(stop);
        write_journal(app_dir, &journal)
    });
    if let Err(error) = stopped {
        progress(Phase::StartingExisting);
        let back = "CORSIA couldn’t finish stopping your earlier install, so it left everything where it was.";
        return Err(undo(app_dir, &journal, old, back, error));
    }

    progress(Phase::MovingToBackup);
    let not_moved = "CORSIA couldn’t move everything into the backup folder, so it put it all back where it was.";
    for step in &journal.moves {
        let moved = step
            .to
            .parent()
            .map_or(Ok(()), fs::create_dir_all)
            .and_then(|()| fs::rename(&step.from, &step.to));
        if let Err(error) = moved {
            progress(Phase::StartingExisting);
            let details = format!("could not move {}: {error}", shown(&inventory.home, &step.from));
            return Err(undo(app_dir, &journal, old, not_moved, details));
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
        return Err(undo(app_dir, &journal, old, not_moved, details));
    }
    if let Err(error) = fs::remove_file(journal_path(app_dir)) {
        // A journal left behind would undo the finished move on the next launch.
        progress(Phase::StartingExisting);
        return Err(undo(app_dir, &journal, old, not_moved, format!("could not finish: {error}")));
    }
    Ok(backup)
}

/// Whether a Quit asked for during Start fresh can now be carried out: once everything is
/// in one known place, and never while the earlier install it stopped isn't running, which
/// the person has to see first.
pub fn quit_after(quit_asked: bool, result: &Result<PathBuf, StartFreshError>) -> bool {
    quit_asked && result.as_ref().map_or_else(|error| error.restored && !error.left_stopped, |_| true)
}

/// A Start fresh the app did not finish (it quit or crashed partway) is undone
/// before launch decides anything, so the earlier install is found whole again.
/// `start_old` starts the earlier install once its files are back.
///
/// An undo that can't finish here sets the journal aside, so the next launch goes on to
/// first run with whatever is in place instead of failing the same way every time.
pub fn recover_on_launch(
    app_dir: &Path,
    start_old: impl FnOnce(Option<&StopProcess>) -> Result<(), String>,
) -> Result<(), StartFreshError> {
    let path = journal_path(app_dir);
    let read = match fs::read(&path) {
        Ok(contents) => serde_json::from_slice::<Journal>(&contents).map_err(|error| error.to_string()),
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()),
        Err(error) => Err(error.to_string()),
    };
    let (journal, details, kept) = match read {
        Err(error) => {
            let details = format!("could not read the Start fresh journal: {error}");
            let (details, kept) = set_journal_aside(app_dir, details);
            let message = format!(
                "CORSIA couldn’t read its record of what Start fresh moved, but nothing was deleted: \
                 anything it moved is in ~/{BACKUPS_FOLDER}. {}",
                next_launch(kept)
            );
            return Err(StartFreshError::new(message, details, false));
        }
        Ok(journal) => match put_back(app_dir, &journal, start_old) {
            PutBack::Done => return Ok(()),
            PutBack::NotStarted(details) => return Err(StartFreshError::not_started(details)),
            PutBack::Files(details) => {
                let (details, kept) = set_journal_aside(app_dir, details);
                (journal, details, kept)
            }
        },
    };
    let message = format!(
        "CORSIA couldn’t put everything back, but nothing was deleted: what it moved is still in {}. {}",
        backup_shown(&journal.backup),
        next_launch(kept)
    );
    Err(StartFreshError::new(message, details, false))
}

/// What the person does after a launch-time undo that couldn't finish.
fn next_launch(journal_kept: bool) -> &'static str {
    if journal_kept {
        "Reopen CORSIA to try again."
    } else {
        "Move back anything you need from there, then reopen CORSIA."
    }
}

/// Rename the journal beside itself so the next launch stops undoing it, keeping it as a
/// record. Returns the details to show and whether the journal is still in place.
fn set_journal_aside(app_dir: &Path, details: String) -> (String, bool) {
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |since| since.as_secs());
    let aside = app_dir.join(format!("start-fresh-set-aside-{stamp}.json"));
    match fs::rename(journal_path(app_dir), &aside) {
        Ok(()) => (format!("{details}; the record of the moves is now {}", aside.display()), false),
        Err(error) => (format!("{details}; could not set the Start fresh journal aside: {error}"), true),
    }
}

/// What `LaunchdService` asks of launchd, the earlier install's port and its databases.
/// Production asks the real ones (`SystemLaunchd`); unit tests script the answers.
pub trait Launchd {
    /// launchd lists the earlier install's job.
    fn loaded(&mut self) -> Result<bool, String>;
    /// Something answers on the earlier install's backend port.
    fn port_answers(&mut self) -> bool;
    /// Ask launchd to stop the job. Only for an install this app can't adopt.
    fn bootout(&mut self) -> Result<(), String>;
    /// Ask launchd to run the job from its plist again, when the plist is still there.
    fn bootstrap(&mut self) -> Result<(), String>;
    /// Every existing `corsia.db` of the earlier install opens exclusively.
    fn databases_released(&mut self) -> Result<(), String>;
}

/// launchd and the earlier install's files as this computer has them.
pub struct SystemLaunchd {
    plist: PathBuf,
    port: Option<u16>,
    databases: Vec<PathBuf>,
    python: PathBuf,
}

impl SystemLaunchd {
    fn target() -> String {
        format!("gui/{}/{}", unsafe { libc::geteuid() }, adoption::SOURCE_LABEL)
    }
}

impl Launchd for SystemLaunchd {
    fn loaded(&mut self) -> Result<bool, String> {
        Command::new("/bin/launchctl")
            .args(["print", &Self::target()])
            .output()
            .map(|output| output.status.success())
            .map_err(|error| {
                log::warn!("could not check the earlier install: {error}");
                "CORSIA couldn’t check whether the earlier install is running".to_string()
            })
    }

    fn port_answers(&mut self) -> bool {
        self.port.is_some_and(|port| !release_proof::port_refuses(port))
    }

    fn bootout(&mut self) -> Result<(), String> {
        release_proof::launchctl_bootout(Path::new("/bin/launchctl"), &Self::target())
            .map_err(|error| format!("could not ask launchd to stop the earlier install: {error}"))
    }

    fn bootstrap(&mut self) -> Result<(), String> {
        if !self.plist.is_file() {
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

    fn databases_released(&mut self) -> Result<(), String> {
        for database in self.databases.iter().filter(|database| database.is_file()) {
            release_proof::database_released(&self.python, database)
                .map_err(|error| format!("{}: {error}", database.display()))?;
        }
        Ok(())
    }
}

/// The earlier install's service as launchd runs it. Its own `scripts/corsia stop`
/// drains it when the install is one this app could adopt, and a stop that refuses is
/// the answer; launchd boots out only an install this app can't adopt.
pub struct LaunchdService<L = SourceLifecycle, D = SystemLaunchd> {
    source: Option<L>,
    launchd: D,
    timeout: Duration,
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
        // Where the data folder move puts an adopted install's data.
        states.push(app_paths::app_dir_for_home(home).join(app_paths::STATE_DIRECTORY));
        let databases = states.into_iter().map(|state| state.join("corsia.db")).collect();
        let launchd = SystemLaunchd { plist, port: roots.port, databases, python };
        Self { source, launchd, timeout: STOP_TIMEOUT }
    }
}

impl<L: adoption::Lifecycle, D: Launchd> LaunchdService<L, D> {
    fn released(&mut self) -> Result<(), String> {
        if self.running()? {
            return Err("the earlier install is still running".into());
        }
        self.launchd.databases_released()
    }

    /// Wait until `check` passes, or say why it never did.
    fn wait_for(&mut self, check: impl FnMut(&mut Self) -> Result<(), String>) -> Result<(), String> {
        self.wait_within(self.timeout, check)
    }

    fn wait_within(
        &mut self,
        timeout: Duration,
        mut check: impl FnMut(&mut Self) -> Result<(), String>,
    ) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        loop {
            match check(self) {
                Ok(()) => return Ok(()),
                Err(error) if Instant::now() >= deadline => {
                    return Err(error)
                }
                Err(_) => thread::sleep(STOP_POLL),
            }
        }
    }
}

impl<L: adoption::Lifecycle, D: Launchd> OldService for LaunchdService<L, D> {
    fn stop(
        &mut self,
        progress: &mut dyn FnMut(Option<u32>),
        spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
    ) -> Result<(), String> {
        if self.running()? {
            if let Some(source) = self.source.as_mut() {
                source.stop_old(progress, spawned)?;
            } else {
                progress(None);
                self.launchd.bootout()?;
                // A plain bootout returns at once and launchd lists the job until its process
                // has exited, which a source job's `ExitTimeOut` allows 120 s for.
                return self.wait_within(release_proof::BOOTOUT_TIMEOUT, Self::released);
            }
        }
        // Whatever stopped it, and even when launchd never ran it, a backend still holding a
        // database (one started by hand, say) means its folders must not move.
        self.wait_for(|service| service.launchd.databases_released())
    }

    fn running(&mut self) -> Result<bool, String> {
        Ok(self.launchd.loaded()? || self.launchd.port_answers())
    }

    fn start(&mut self, stop: Option<&StopProcess>) -> Result<(), String> {
        if let Some(source) = self.source.as_mut() {
            // Until a stop left draining exits, starting would do nothing. It is never killed.
            if let Some(stop) = stop {
                while source.stop_is_running(stop) {
                    thread::sleep(source.orphan_poll());
                }
            }
            return source.start_old();
        }
        if self.launchd.loaded()? {
            return Ok(());
        }
        self.launchd.bootstrap()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_release_check_covers_a_moved_install_s_database() {
        let home = Path::new("/tmp/corsia-release-list");
        let service = LaunchdService::new(home, None, PathBuf::from("python3"));
        assert!(service
            .launchd
            .databases
            .contains(&home.join("Library/Application Support/CORSIA/state/corsia.db")));
    }

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
        /// Starting it again fails.
        start_fails: bool,
        stops: u32,
        starts: u32,
        /// Where the journal is read while the stop drains.
        app_dir: Option<PathBuf>,
        journal_during_stop: Option<String>,
    }

    fn a_stop() -> StopProcess {
        StopProcess { pid: 4242, started: "Thu Oct 22 13:05:00 2026".into() }
    }

    impl OldService for FakeService {
        fn stop(
            &mut self,
            progress: &mut dyn FnMut(Option<u32>),
            spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
        ) -> Result<(), String> {
            spawned(a_stop())?;
            if let Some(app_dir) = &self.app_dir {
                self.journal_during_stop = fs::read_to_string(journal_path(app_dir)).ok();
            }
            if self.refuse_stop {
                return Err("it would not stop".into());
            }
            progress(Some(2));
            self.running = self.comes_back;
            self.stops += 1;
            Ok(())
        }

        fn running(&mut self) -> Result<bool, String> {
            Ok(self.running)
        }

        fn start(&mut self, _stop: Option<&StopProcess>) -> Result<(), String> {
            self.starts += 1;
            if self.start_fails {
                return Err("it would not start".into());
            }
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
        assert!(!error.message.contains("isn’t running"), "{}", error.message);
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
        assert!(!error.left_stopped, "it runs again");
        assert!(error.message.contains("couldn’t finish stopping"));
        assert_eq!(home.files(), before);
        assert!(home.backups().is_empty());
        assert!(!journal_path(&home.app_dir()).exists());
        assert_eq!(service.starts, 1, "a stop that failed partway may have left it down");
    }

    #[test]
    fn the_journal_and_the_stop_are_recorded_before_the_stop_drains() {
        let home = Home::new("recorded");
        source_install(&home);
        let mut service = FakeService { running: true, app_dir: Some(home.app_dir()), ..Default::default() };

        run(&home, &mut service).unwrap();

        let journal: Journal = serde_json::from_str(service.journal_during_stop.as_deref().unwrap()).unwrap();
        assert!(journal.restart_old);
        assert_eq!(journal.stop, Some(a_stop()));
        assert_eq!(journal.moves.len(), 4);
    }

    #[test]
    fn an_install_that_does_not_start_again_is_reported_stopped() {
        let home = Home::new("not-started");
        source_install(&home);
        let before = home.files();
        let mut service = FakeService { running: true, refuse_stop: true, start_fails: true, ..Default::default() };

        let error = run(&home, &mut service).unwrap_err();

        assert!(error.restored, "the files are where they were");
        assert!(error.left_stopped, "the app stays open to say so, even after Quit");
        assert_eq!(error.message, NOT_STARTED, "one line, whatever failed first");
        assert!(error.details.contains("it would not stop"), "{}", error.details);
        assert!(error.details.contains("it would not start"), "{}", error.details);
        assert_eq!(home.files(), before);
        assert!(!journal_path(&home.app_dir()).exists(), "Retry starts over instead of undoing again");
    }

    #[test]
    fn quit_during_start_fresh_waits_until_the_person_can_see_the_earlier_install_is_down() {
        let outcome = |service: &mut FakeService| {
            let home = Home::new("quit");
            source_install(&home);
            run(&home, service)
        };
        let done = outcome(&mut FakeService { running: true, ..Default::default() });
        let put_back = outcome(&mut FakeService { running: true, refuse_stop: true, ..Default::default() });
        let down = outcome(&mut FakeService { running: true, refuse_stop: true, start_fails: true, ..Default::default() });
        assert!(quit_after(true, &done));
        assert!(!quit_after(false, &done));
        assert!(quit_after(true, &put_back));
        assert!(!quit_after(true, &down), "{down:?}");
        assert!(!quit_after(true, &Err(StartFreshError::unfinished("x"))));
    }

    #[test]
    fn an_unfinished_start_fresh_is_undone_on_the_next_launch() {
        let home = Home::new("relaunch");
        source_install(&home);
        let before = home.files();
        // The app closed after the first of its moves: the journal lists them all.
        let (backup, moves) = journal_for(&home, Some(a_stop()));
        fs::create_dir_all(moves[0].to.parent().unwrap()).unwrap();
        fs::rename(&moves[0].from, &moves[0].to).unwrap();

        let mut waited_for = None;
        recover_on_launch(&home.app_dir(), |stop| {
            waited_for = stop.cloned();
            Ok(())
        })
        .unwrap();

        assert_eq!(waited_for, Some(a_stop()), "the stop left draining is waited for before the start");
        assert_eq!(home.files(), before);
        assert!(!backup.exists() && home.backups().is_empty());
        assert!(matches!(first_run::plan(&home.0, &home.app_dir()).unwrap(), FirstRun::Adoptable { .. }));
        // Nothing left to undo: a second launch changes nothing.
        recover_on_launch(&home.app_dir(), |_| panic!("no journal, no start")).unwrap();
    }

    /// A journal listing every move of a source install, as `start_fresh` writes it before the stop.
    fn journal_for(home: &Home, stop: Option<StopProcess>) -> (PathBuf, Vec<Move>) {
        let inventory = inventory(&home.0, &home.app_dir()).unwrap();
        let backup = create_backup(&inventory.home, SystemTime::now()).unwrap();
        let moves: Vec<Move> = inventory
            .items
            .iter()
            .map(|from| Move { to: backup.join(from.strip_prefix(&inventory.home).unwrap()), from: from.clone() })
            .collect();
        let journal = Journal { backup: backup.clone(), moves: moves.clone(), restart_old: true, stop };
        write_journal(&home.app_dir(), &journal).unwrap();
        (backup, moves)
    }

    #[test]
    fn a_launch_that_cannot_start_the_earlier_install_says_so() {
        let home = Home::new("relaunch-not-started");
        source_install(&home);
        let before = home.files();
        // The app closed while the stop was draining: nothing had moved yet.
        journal_for(&home, Some(a_stop()));

        let error = recover_on_launch(&home.app_dir(), |_| Err("it would not start".into())).unwrap_err();

        assert!(error.restored);
        assert!(error.left_stopped);
        assert_eq!(error.message, NOT_STARTED);
        assert_eq!(error.details, "it would not start");
        assert_eq!(home.files(), before);
        assert!(!journal_path(&home.app_dir()).exists(), "the next launch goes on as usual");
    }

    #[test]
    fn a_launch_that_cannot_put_everything_back_sets_the_journal_aside() {
        let home = Home::new("relaunch-stuck");
        source_install(&home);
        let (backup, moves) = journal_for(&home, None);
        // Moved, and something new now sits where it was.
        fs::create_dir_all(moves[0].to.parent().unwrap()).unwrap();
        fs::rename(&moves[0].from, &moves[0].to).unwrap();
        home.write(moves[0].from.strip_prefix(&home.0).unwrap().join("new.md").to_str().unwrap(), "new");

        let error = recover_on_launch(&home.app_dir(), |_| panic!("files first")).unwrap_err();

        assert!(!error.restored);
        assert!(error.message.contains(&format!("~/{BACKUPS_FOLDER}/")), "{}", error.message);
        assert!(error.message.contains("then reopen CORSIA"), "{}", error.message);
        assert!(!error.message.contains(&home.0.display().to_string()), "no full path in the line");
        assert!(error.details.contains("both back in place and in the backup"), "{}", error.details);
        assert!(moves[0].to.exists() && backup.exists(), "nothing is deleted");
        // The record is kept beside the journal, and the next launch goes on to first run.
        assert!(!journal_path(&home.app_dir()).exists());
        assert!(error.details.contains("start-fresh-set-aside-"), "{}", error.details);
        recover_on_launch(&home.app_dir(), |_| panic!("set aside, not undone again")).unwrap();
    }

    #[test]
    fn an_unreadable_journal_is_set_aside_so_the_next_launch_goes_on() {
        let home = Home::new("relaunch-unreadable");
        fs::create_dir_all(home.app_dir()).unwrap();
        fs::write(journal_path(&home.app_dir()), "{ not json").unwrap();

        let error = recover_on_launch(&home.app_dir(), |_| panic!("nothing to start")).unwrap_err();

        assert!(!error.restored);
        assert!(error.message.contains("then reopen CORSIA"), "{}", error.message);
        assert!(error.details.contains("could not read the Start fresh journal"), "{}", error.details);
        assert!(!journal_path(&home.app_dir()).exists());
        let aside: Vec<_> = fs::read_dir(home.app_dir())
            .unwrap()
            .flatten()
            .filter(|entry| entry.file_name().to_string_lossy().starts_with("start-fresh-set-aside-"))
            .collect();
        assert_eq!(aside.len(), 1, "the record is kept");
        recover_on_launch(&home.app_dir(), |_| panic!("set aside, not read again")).unwrap();
    }

    #[test]
    fn a_finished_start_fresh_plans_a_fresh_first_run_on_relaunch() {
        let home = Home::new("finished");
        source_install(&home);
        run(&home, &mut FakeService { running: true, ..Default::default() }).unwrap();
        recover_on_launch(&home.app_dir(), |_| panic!("nothing to undo")).unwrap();
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

        assert!(error.restored);
        assert_eq!(
            error.message,
            "~/Corsia is holding CORSIA’s code folder, ~/Corsia/source/corsia, and Start fresh never moves code. \
             Stop your earlier install and move ~/Corsia yourself, then choose Retry."
        );
        assert_eq!(error.details, "To stop it, run ~/Corsia/source/corsia/scripts/corsia stop in Terminal.");
        assert_eq!(preview(&home.0, &home.app_dir()).unwrap_err(), error, "the confirmation says the same");
        assert_eq!(service.stops, 0);
        assert_eq!(home.files(), before);
        assert!(home.backups().is_empty());
    }

    #[test]
    fn settings_name_state_folders_that_move_and_work_folders_that_stay() {
        let home = Home::new("extras");
        home.write("work/notes.md", "mine");
        home.write("work/.corsia/corsia.db", "db");
        home.write("Developer/app/main.rs", "theirs");
        home.write("Library/Preferences/keep.plist", "system");
        home.write("Library/Mobile Documents/com~apple~CloudDocs/keep.md", "synced");
        // A state folder holding the earlier install's database moves; the iCloud Drive root
        // named as state holds none, so it stays, as does every work folder.
        home.write(
            PLIST,
            &format!(
                r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>com.corsia.prod</string><key>EnvironmentVariables</key><dict><key>CORSIA_WORKSPACE</key><string>{}</string><key>CORSIA_STATE_DIR</key><string>{}</string></dict></dict></plist>"#,
                home.0.join("Developer").display(),
                home.0.join("Library/Mobile Documents/com~apple~CloudDocs").display()
            ),
        );
        home.write(
            ".corsia/config.json",
            &format!(r#"{{"workspace":"{}"}}"#, home.0.join("work").display()),
        );

        let preview = preview(&home.0, &home.app_dir()).unwrap();

        assert_eq!(
            preview.moves,
            vec!["~/.corsia".to_string(), "~/work/.corsia".to_string(), "~/Library/LaunchAgents/com.corsia.prod.plist".to_string()]
        );
        assert!(preview.code.is_empty());
        assert_eq!(preview.work, vec!["~/Developer".to_string(), "~/work".to_string()]);
        assert_eq!(preview.backups, format!("~/{BACKUPS_FOLDER}"));
        run(&home, &mut FakeService::default()).unwrap();
        assert!(home.0.join("work/notes.md").is_file(), "a work folder stays");
        assert!(!home.0.join("work/.corsia").exists(), "its state moves");
        assert!(home.0.join("Developer/app/main.rs").is_file());
        assert!(home.0.join("Library/Preferences/keep.plist").is_file());
        assert!(home.0.join("Library/Mobile Documents/com~apple~CloudDocs/keep.md").is_file());
    }

    #[test]
    fn a_configured_or_fresh_computer_is_never_touched() {
        let home = Home::new("nothing");
        let error = run(&home, &mut FakeService::default()).unwrap_err();
        assert!(error.restored);
        assert!(error.nothing_to_move, "setup goes on instead of a Retry that fails the same way");
        assert_eq!(preview(&home.0, &home.app_dir()).unwrap_err(), error, "the confirmation says the same");
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
        assert_eq!(preview.code, vec!["~/.corsia".to_string()]);
        assert!(preview.work.is_empty(), "the work folder is the detected ~/Corsia, which moves");
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

    /// launchd, the port and the databases as a test scripts them.
    #[derive(Default)]
    struct FakeLaunchd {
        loaded: bool,
        port: bool,
        databases_held: bool,
        bootouts: u32,
        bootstraps: u32,
    }

    impl Launchd for FakeLaunchd {
        fn loaded(&mut self) -> Result<bool, String> {
            Ok(self.loaded)
        }

        fn port_answers(&mut self) -> bool {
            self.port
        }

        fn bootout(&mut self) -> Result<(), String> {
            self.bootouts += 1;
            self.loaded = false;
            self.port = false;
            Ok(())
        }

        fn bootstrap(&mut self) -> Result<(), String> {
            self.bootstraps += 1;
            self.loaded = true;
            Ok(())
        }

        fn databases_released(&mut self) -> Result<(), String> {
            if self.databases_held {
                return Err("database is locked".into());
            }
            Ok(())
        }
    }

    /// The adoptable install's own lifecycle: only the stop and start Start fresh uses.
    #[derive(Default)]
    struct FakeSource {
        refuse: bool,
        stops: u32,
        starts: u32,
        /// Looks for which a stop left by an earlier run is still draining.
        draining: u32,
    }

    impl adoption::Lifecycle for FakeSource {
        fn check_ready(&mut self, _state: &Path, _names: &BTreeSet<String>) -> Result<(), adoption::ReadyError> {
            unreachable!("Start fresh never adopts")
        }

        fn stop_old(
            &mut self,
            _progress: &mut dyn FnMut(Option<u32>),
            spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
        ) -> Result<(), String> {
            self.stops += 1;
            spawned(a_stop())?;
            if self.refuse {
                return Err("`corsia stop` refused: work is still running".into());
            }
            Ok(())
        }

        fn stop_is_running(&mut self, _stop: &StopProcess) -> bool {
            let draining = self.draining > 0;
            self.draining = self.draining.saturating_sub(1);
            draining
        }

        fn orphan_poll(&self) -> Duration {
            Duration::ZERO
        }

        fn old_is_down(&mut self) -> Result<(), String> {
            unreachable!("Start fresh never adopts")
        }

        fn backup(&mut self, _state: &Path) -> Result<PathBuf, String> {
            unreachable!("Start fresh never adopts")
        }

        fn start_new(&mut self, _state: &Path, _port: u16, _app_dir: &Path) -> Result<(), String> {
            unreachable!("Start fresh never adopts")
        }

        fn stop_new(&mut self, _state: &Path, _port: u16) -> Result<(), String> {
            unreachable!("Start fresh never adopts")
        }

        fn restore_backup(&mut self, _state: &Path, _snapshot: &Path) -> Result<(), String> {
            unreachable!("Start fresh never adopts")
        }

        fn start_old(&mut self) -> Result<(), String> {
            assert_eq!(self.draining, 0, "started while a stop was still draining");
            self.starts += 1;
            Ok(())
        }
    }

    fn launchd_service(source: Option<FakeSource>, launchd: FakeLaunchd) -> LaunchdService<FakeSource, FakeLaunchd> {
        LaunchdService { source, launchd, timeout: Duration::from_millis(300) }
    }

    fn stop(service: &mut LaunchdService<FakeSource, FakeLaunchd>) -> Result<(), String> {
        service.stop(&mut |_| {}, &mut |_| Ok(()))
    }

    #[test]
    fn an_unreadable_install_status_is_not_reported_as_a_confirmed_stop_failure() {
        let mut service = launchd_service(None, FakeLaunchd::default());
        service.timeout = Duration::ZERO;
        let cause = "CORSIA couldn’t check whether the earlier install is running";
        assert_eq!(service.wait_for(|_| Err(cause.into())).unwrap_err(), cause);
    }

    #[test]
    fn an_adoptable_install_is_stopped_only_by_its_own_stop() {
        let mut service = launchd_service(Some(FakeSource::default()), FakeLaunchd { loaded: true, ..Default::default() });
        stop(&mut service).unwrap();
        assert_eq!(service.source.as_ref().unwrap().stops, 1);
        assert_eq!(service.launchd.bootouts, 0, "running work is drained, never killed");
    }

    #[test]
    fn a_refused_stop_is_the_answer_and_nothing_is_booted_out() {
        let source = FakeSource { refuse: true, ..Default::default() };
        let mut service = launchd_service(Some(source), FakeLaunchd { loaded: true, ..Default::default() });
        let error = stop(&mut service).unwrap_err();
        assert!(error.contains("refused"), "{error}");
        assert_eq!(service.launchd.bootouts, 0);
    }

    #[test]
    fn an_install_this_app_cannot_adopt_is_booted_out_and_proven_released() {
        let mut service = launchd_service(None, FakeLaunchd { loaded: true, port: true, ..Default::default() });
        stop(&mut service).unwrap();
        assert_eq!(service.launchd.bootouts, 1);
        assert!(!service.running().unwrap());
    }

    #[test]
    fn a_held_database_keeps_everything_in_place_even_when_nothing_looks_running() {
        for source in [None, Some(FakeSource::default())] {
            let adoptable = source.is_some();
            let mut service = launchd_service(source, FakeLaunchd { databases_held: true, ..Default::default() });
            let error = stop(&mut service).unwrap_err();
            assert!(error.contains("locked"), "{error}");
            assert_eq!(service.launchd.bootouts, 0);
            assert_eq!(service.source.as_ref().map_or(0, |source| source.stops), 0, "adoptable {adoptable}");
        }
        // Stopped by its own stop, but something else still holds the database.
        let source = FakeSource::default();
        let mut service = launchd_service(Some(source), FakeLaunchd { loaded: true, databases_held: true, ..Default::default() });
        assert!(stop(&mut service).is_err());
    }

    #[test]
    fn the_earlier_install_starts_only_after_a_left_stop_drains() {
        let source = FakeSource { draining: 3, ..Default::default() };
        let mut service = launchd_service(Some(source), FakeLaunchd::default());
        service.start(Some(&a_stop())).unwrap();
        assert_eq!(service.source.as_ref().unwrap().starts, 1);
        assert_eq!(service.launchd.bootstraps, 0, "an adoptable install starts through its own checkout");
    }

    #[test]
    fn an_install_this_app_cannot_adopt_is_started_by_launchd_only_when_not_loaded() {
        let mut service = launchd_service(None, FakeLaunchd::default());
        service.start(None).unwrap();
        service.start(None).unwrap();
        assert_eq!(service.launchd.bootstraps, 1);
    }
}
