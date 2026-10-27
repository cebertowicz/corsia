//! Recoverable in-place adoption of a source install by the packaged app.
//!
//! The source install's own lifecycle owner, `<checkout>/scripts/corsia`,
//! stops it and, on rollback, starts it again. The app owns only what changes
//! hands: the backup, the service definition and the journal that makes each
//! step recoverable. The state root never moves.

use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use serde::{Deserialize, Serialize};

const JOURNAL: &str = "adoption.json";
/// The launchd label of the source install's service.
pub const SOURCE_LABEL: &str = "com.corsia.prod";
const PLIST: &str = "com.corsia.prod.plist";
/// How often a restart in flight is looked at again. How long to wait is the request's own age
/// (see `RestartLook`), never a limit of the app's.
const RESTART_POLL: Duration = Duration::from_secs(2);
const ORPHAN_POLL: Duration = Duration::from_secs(1);

// The person-facing lines for a source install that cannot be taken over yet, as the
// owner worded them. Each is a whole line with its own next step.
pub const NOT_RUNNING: &str =
    "Your existing CORSIA isn’t running. Start it, and this app will take it over.";
const STUCK_RESTARTING: &str = "Your existing CORSIA is stuck restarting.";
/// The loaded job is the saved settings' own folder but an older copy of them. The checkout's
/// script refuses to stop or restart it, so only launchd reloading the plist (a restart of the
/// computer) recovers it, and no button is offered.
const OLDER_SETTINGS: &str =
    "Your existing CORSIA is running older settings than the ones saved for it. \
     Restart this computer, and this app will take it over.";

/// The loaded job runs from one folder and its settings name another. No restart
/// fixes this: the checkout's own script refuses unless the installed definition names it.
pub fn wrong_folder_line(running: Option<&str>, checkout: &str) -> String {
    format!(
        "Your existing CORSIA is running from {}, but its settings name {checkout}. \
         Nothing was changed.",
        running.unwrap_or("an unknown folder")
    )
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct SourceInstall {
    pub plist: PathBuf,
    pub state: PathBuf,
    pub workspace: PathBuf,
    pub port: u16,
    /// The plist's `WorkingDirectory`: where the loaded job runs from.
    pub working_directory: PathBuf,
    /// The plist's `CORSIA_SERVICE_GENERATION`: what `launchctl print` echoes for the job
    /// bootstrapped from this plist.
    #[serde(default)]
    pub generation: Option<String>,
    /// The checkout's `scripts/corsia`: the only thing that stops or starts the source service.
    pub cli: PathBuf,
    pub env_file: PathBuf,
    pub plist_variable_names: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    pub source: SourceInstall,
    pub app_dir: PathBuf,
}

/// Read the owned source service's rendered plist. Directory names are not a
/// substitute for this contract: the plist supplies the roots and port the old
/// job actually used, and its checkout must still hold the lifecycle owner.
pub fn source_from_plist(home: &Path, plist_path: &Path) -> Result<SourceInstall, String> {
    let value = plist::Value::from_file(plist_path)
        .map_err(|error| format!("could not read source service plist: {error}"))?;
    let root = value
        .as_dictionary()
        .ok_or("source service plist is not a dictionary")?;
    if root.get("Label").and_then(plist::Value::as_string) != Some(SOURCE_LABEL) {
        return Err("source service plist is not the CORSIA production service".into());
    }
    let environment = root
        .get("EnvironmentVariables")
        .and_then(plist::Value::as_dictionary)
        .ok_or("source service plist has no environment")?;
    let working_directory = root
        .get("WorkingDirectory")
        .and_then(plist::Value::as_string)
        .map(PathBuf::from)
        .ok_or("source service plist has no working directory")?;
    let text = |name: &str| {
        environment
            .get(name)
            .and_then(plist::Value::as_string)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
    };
    let recorded = bootstrap_roots(&home.join(".corsia/config.json"));
    let workspace = text("CORSIA_WORKSPACE")
        .or_else(|| recorded.as_ref().map(|(workspace, _)| workspace.clone()))
        .ok_or("source service plist has no workspace")?;
    let state = text("CORSIA_STATE_DIR")
        .or_else(|| recorded.map(|(_, state)| state))
        .ok_or("source service plist has no state directory")?;
    let port = environment
        .get("BACKEND_PORT")
        .and_then(plist::Value::as_string)
        .and_then(|value| value.parse().ok())
        .filter(|port: &u16| *port != 0)
        .ok_or("source service plist has no valid port")?;
    let cli = working_directory
        .parent()
        .ok_or("source service backend has no checkout root")?
        .join("scripts/corsia");
    if !cli.is_file() {
        return Err(format!("the source checkout has no {}", cli.display()));
    }
    Ok(SourceInstall {
        plist: plist_path.to_path_buf(),
        generation: text("CORSIA_SERVICE_GENERATION")
            .map(|value| value.to_string_lossy().into_owned()),
        cli,
        working_directory,
        env_file: home.join(".corsia/.env"),
        plist_variable_names: environment.keys().cloned().collect(),
        state,
        workspace,
        port,
    })
}

/// The roots the source install's bootstrap config records: a v2 file names
/// both, an older one names only the workspace and keeps its state in
/// `<workspace>/.corsia`.
pub(crate) fn bootstrap_roots(config: &Path) -> Option<(PathBuf, PathBuf)> {
    let value: serde_json::Value = serde_json::from_str(&fs::read_to_string(config).ok()?).ok()?;
    match value.get("workspace")? {
        serde_json::Value::String(workspace) => {
            let workspace = PathBuf::from(workspace);
            let state = workspace.join(".corsia");
            Some((workspace, state))
        }
        serde_json::Value::Object(workspace) => Some((
            PathBuf::from(workspace.get("root")?.as_str()?),
            PathBuf::from(value.get("state_root")?.as_str()?),
        )),
        _ => None,
    }
}

impl Plan {
    pub fn adopted_dir(&self) -> PathBuf {
        self.app_dir.join("adopted")
    }

    /// All refusal conditions run before the source service is touched.
    pub fn preflight(&self) -> Result<(), String> {
        if !self.source.state.join("corsia.db").is_file() {
            return Err("the existing install has no corsia.db".into());
        }
        Ok(())
    }

    /// Every variable name the source install sets, for the backend to decide
    /// which of them are provider keys and compare those with what it has
    /// stored. Names only: the backend owns the rule, so this never filters.
    pub fn variable_names(&self) -> Result<BTreeSet<String>, String> {
        source_variable_names(&self.source.env_file, &self.source.plist_variable_names)
    }
}

/// The work the window is showing while adoption runs. Each phase has one
/// approved line of copy; the shell picks it from this, never from free text.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum Phase {
    /// The source service is draining its running work and stopping. `running`
    /// is a display-only count: `None` when it cannot be read, so the window
    /// shows no count line at all rather than a wrong one.
    Waiting {
        running: Option<u32>,
    },
    /// The source install is partway through restarting itself; nothing has been asked of it.
    WaitingRestart,
    BackingUp,
    /// The app's own service is starting.
    Starting,
    /// Start fresh is moving the earlier install into its backup folder.
    MovingToBackup,
    /// A rollback is putting the existing install back.
    StartingExisting,
    /// The one-time move of an adopted install's data to `CORSIA/state`, as one
    /// percentage for the whole move (`state_move`).
    MovingData {
        percent: u8,
    },
    /// The move stopped after the window showed it; the line says why and what
    /// the person can do, then CORSIA starts on the old folder.
    MoveSkipped {
        #[serde(flatten)]
        told: crate::state_move::Told,
    },
}

/// Production runs the source's lifecycle owner and the app's bundled backend.
/// Unit tests provide a fake, which makes the order observable without
/// reaching launchd or a real home folder.
pub trait Lifecycle {
    /// Whether adoption can finish, decided before the source service is
    /// touched: the source job is loaded from this checkout, no self-restart
    /// is in flight, every provider key among the given variable names is
    /// stored (names compared, nothing else read) and the disk holds the
    /// backup plus a reserve.
    fn check_ready(
        &mut self,
        state: &Path,
        variable_names: &BTreeSet<String>,
    ) -> Result<(), ReadyError>;
    /// A read-only look at the source's self-restart requests. What counts as
    /// stuck comes from the request's own age, never from how long this app has waited.
    fn look_for_restart(&mut self, _state: &Path) -> RestartLook {
        RestartLook::Settled
    }
    /// How often to look while a restart is in flight.
    fn restart_poll(&self) -> Duration {
        RESTART_POLL
    }
    /// `<checkout>/scripts/corsia stop`, which holds new work and drains the
    /// running work for up to `CORSIA_DRAIN_MAX_WAIT`, then proof, whatever its
    /// exit status, that the source has let go of everything adoption touches
    /// next: launchd no longer lists the job, the backend port does not answer,
    /// the process launchd ran is gone and the database opens exclusively
    /// (`release_proof`). While it runs, `progress` receives a
    /// read-only count of running turns about once a second, `None` when the
    /// count could not be read.
    fn stop_old(
        &mut self,
        progress: &mut dyn FnMut(Option<u32>),
        spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
    ) -> Result<(), String>;
    /// Whether this `corsia stop` process, left by an earlier run of the app, is still draining.
    fn stop_is_running(&mut self, _stop: &StopProcess) -> bool {
        false
    }
    /// How often to look at a stop left draining.
    fn orphan_poll(&self) -> Duration {
        ORPHAN_POLL
    }
    /// Proof, right before the app's service starts, that the source job is
    /// still gone and its port still free. Someone may have started it again.
    fn old_is_down(&mut self) -> Result<(), String>;
    /// Snapshot the whole state-root set before the app backend can migrate it.
    fn backup(&mut self, state: &Path) -> Result<PathBuf, String>;
    /// Register and prove the new backend is healthy (and hence any migration
    /// has run). This is one transaction phase.
    fn start_new(&mut self, state: &Path, port: u16, app_dir: &Path) -> Result<(), String>;
    /// Boot out the app job and prove it released the state and port.
    fn stop_new(&mut self, state: &Path, port: u16) -> Result<(), String>;
    /// Restore the snapshot before the source service is started again.
    fn restore_backup(&mut self, state: &Path, snapshot: &Path) -> Result<(), String>;
    /// `<checkout>/scripts/corsia start`, then wait for the source to answer.
    /// Safe to repeat, and safe when the source never stopped.
    fn start_old(&mut self) -> Result<(), String>;
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
enum Step {
    /// The stop was requested: the source service may be down.
    Stopping,
    BackedUp,
    MetadataMoved,
    ServiceWritten,
    Registered,
    /// Every file is back where it was; only the source start is left.
    Reverted,
}

/// The `corsia stop` process an adoption started: its pid and the start time
/// that pid had then, so a reused pid is never mistaken for it. Force Quit kills
/// only the app, and the stop keeps draining on its own, so a relaunch has to
/// wait for it before it starts the source again. It is never killed: a killed
/// stop leaves the source's hold set for good.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct StopProcess {
    pub pid: u32,
    pub started: String,
}

#[derive(Debug)]
struct Failure {
    step: String,
    message: String,
}

#[derive(Deserialize, Serialize)]
struct Journal {
    step: Step,
    source: SourceInstall,
    snapshot: Option<PathBuf>,
    #[serde(default)]
    stop: Option<StopProcess>,
}

fn read_journal(plan: &Plan) -> Result<Option<Journal>, String> {
    let raw = match fs::read_to_string(journal_path(plan)) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io(error)),
    };
    serde_json::from_str(&raw)
        .map(Some)
        .map_err(|error| format!("adoption journal is unreadable: {error}"))
}

fn clear_journal(plan: &Plan) -> Result<(), String> {
    match fs::remove_file(journal_path(plan)) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(io(error)),
    }
    File::open(plan.adopted_dir())
        .and_then(|folder| folder.sync_all())
        .map_err(io)
}

fn journal_path(plan: &Plan) -> PathBuf {
    plan.adopted_dir().join(JOURNAL)
}

/// Preserve rollback detail alongside the restored install that can act on it.
fn write_failure_trail(plan: &Plan, failure: &Failure) {
    let path = plan.source.state.join("backend.log");
    let result = (|| -> Result<(), std::io::Error> {
        let mut file = OpenOptions::new().create(true).append(true).open(path)?;
        writeln!(file, "adoption rollback: {}: {}", failure.step, failure.message)?;
        file.sync_all()
    })();
    if let Err(error) = result {
        log::warn!("could not write adoption rollback detail: {error}");
    }
}

/// Replace `path` so a crash leaves either the old contents or the new, never a
/// partial file: a temp file beside it is synced, renamed over it, and the
/// folder is synced. A temp file left by an earlier crash is discarded first,
/// so it can never block a retry.
pub(crate) fn write_atomically(path: &Path, contents: &[u8]) -> Result<(), String> {
    let directory = path.parent().ok_or("path has no folder")?;
    fs::create_dir_all(directory).map_err(io)?;
    let mut temporary = path.as_os_str().to_owned();
    temporary.push(".tmp");
    let temporary = PathBuf::from(temporary);
    match fs::remove_file(&temporary) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(io(error)),
    }
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .map_err(io)?;
    file.write_all(contents)
        .and_then(|_| file.sync_all())
        .map_err(io)?;
    fs::rename(&temporary, path).map_err(io)?;
    File::open(directory)
        .and_then(|folder| folder.sync_all())
        .map_err(io)
}

fn write_journal(plan: &Plan, step: Step, snapshot: Option<&Path>) -> Result<(), String> {
    write_journal_with(plan, step, snapshot, None)
}

/// The Stopping step again, now naming the stop process that is draining.
fn write_stopping(plan: &Plan, stop: &StopProcess) -> Result<(), String> {
    write_journal_with(plan, Step::Stopping, None, Some(stop.clone()))
}

fn write_journal_with(
    plan: &Plan,
    step: Step,
    snapshot: Option<&Path>,
    stop: Option<StopProcess>,
) -> Result<(), String> {
    let journal = Journal {
        step,
        source: plan.source.clone(),
        snapshot: snapshot.map(Path::to_path_buf),
        stop,
    };
    write_atomically(
        &journal_path(plan),
        serde_json::to_string(&journal)
            .map_err(|error| error.to_string())?
            .as_bytes(),
    )
}

fn move_to(source: &Path, destination: &Path) -> Result<(), String> {
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent).map_err(io)?;
    }
    fs::rename(source, destination).map_err(io)
}

fn write_service(plan: &Plan) -> Result<(), String> {
    let payload = serde_json::json!({
        "state_dir": plan.source.state,
        "workspace": plan.source.workspace,
        "port": plan.source.port,
    });
    write_atomically(
        &plan.app_dir.join("service.json"),
        payload.to_string().as_bytes(),
    )
}

fn skipped_env_names(path: &Path) -> Result<Vec<String>, String> {
    let contents = match fs::read_to_string(path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(error) => return Err(io(error)),
    };
    Ok(contents
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            line.split_once('=')
                .map(|(name, _)| name.trim())
                .filter(|name| !name.is_empty())
                .map(str::to_owned)
        })
        .collect())
}

fn source_variable_names(path: &Path, plist_names: &[String]) -> Result<BTreeSet<String>, String> {
    let mut names: BTreeSet<String> = plist_names.iter().cloned().collect();
    names.extend(skipped_env_names(path)?);
    Ok(names)
}

/// What state the existing install is in when adoption stops. The window words
/// its first line from this, never from the reason text.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    /// It was never stopped.
    Untouched,
    /// It was stopped, then started again from its own files.
    Restored,
    /// Not known to be running.
    Unconfirmed,
    /// Adoption finished and the app's service is registered, but it has not
    /// answered yet. The existing install is gone on purpose.
    Switched,
}

/// The one action the app can run to put an unconfirmed install back.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Recovery {
    /// Every file is where it was: start the service again. Safe to repeat.
    StartAgain,
    /// Files may be part-way between homes. Relaunching runs the journal's
    /// undo before anything else starts.
    Reopen,
    /// Nothing was stopped, but the existing install is not running: start it, then take it over.
    StartIt,
    /// It has been stuck restarting: restart it, then look again.
    RestartIt,
    /// Starting or restarting it did not work: the only honest next step is to look again.
    TryAgain,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AdoptionError {
    pub message: String,
    pub outcome: Outcome,
    /// The action the window offers: with `Outcome::Unconfirmed` the one that puts the install
    /// back, or with a standalone `Outcome::Untouched` line the one that fixes what the line says.
    pub recovery: Option<Recovery>,
    /// The message is a whole sentence written for the person, with what to do
    /// next: the window shows it as the line. Otherwise it is detail for the log.
    pub advice: bool,
    /// The message is the whole line: no lead about the existing install is put before it.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub standalone: bool,
    /// With `Outcome::Untouched`: the existing install was just proven loaded from this
    /// checkout, so the window may say it is still running. Without it the window says only
    /// that nothing was changed.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub running: bool,
}

impl AdoptionError {
    pub fn untouched(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Untouched, None, false)
    }

    /// An untouched refusal whose message tells the person what to do.
    pub fn refusal(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Untouched, None, true).confirmed_running()
    }

    /// Marks an untouched failure that came after the install was proven loaded.
    pub fn confirmed_running(mut self) -> Self {
        self.running = true;
        self
    }

    /// A refusal that is a whole line, with the one action that fixes its cause.
    pub fn standalone(message: impl Into<String>, recovery: Option<Recovery>) -> Self {
        let mut error = Self::with(message, Outcome::Untouched, recovery, true);
        error.standalone = true;
        error
    }

    /// Starting or restarting the existing install did not work.
    pub fn could_not_start(message: impl Into<String>) -> Self {
        Self::with(
            message,
            Outcome::Unconfirmed,
            Some(Recovery::TryAgain),
            false,
        )
    }

    pub fn restored(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Restored, None, false)
    }

    pub fn switched(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Switched, None, false)
    }

    pub fn start_again(message: impl Into<String>) -> Self {
        Self::with(
            message,
            Outcome::Unconfirmed,
            Some(Recovery::StartAgain),
            false,
        )
    }

    pub fn reopen(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Unconfirmed, Some(Recovery::Reopen), false)
    }

    #[cfg(test)]
    pub fn message(&self) -> &str {
        &self.message
    }

    fn with(
        message: impl Into<String>,
        outcome: Outcome,
        recovery: Option<Recovery>,
        advice: bool,
    ) -> Self {
        Self {
            message: message.into(),
            outcome,
            recovery,
            advice,
            standalone: false,
            running: false,
        }
    }
}

/// A plain error claims nothing about the old install, and the one recovery
/// that is right whatever the journal holds is a relaunch: with no journal it
/// simply starts, with one it finishes the undo first.
impl From<String> for AdoptionError {
    fn from(message: String) -> Self {
        Self::reopen(message)
    }
}

impl From<&str> for AdoptionError {
    fn from(message: &str) -> Self {
        message.to_string().into()
    }
}

/// Why the source install could not be declared ready to switch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReadyError {
    /// A whole sentence for the person, with what to do next.
    Refused(String),
    /// Anything else that went wrong while checking: detail for the log.
    Failed(String),
    /// The job is not loaded.
    NotRunning,
    /// The job runs from `running` (unknown when launchd printed none), but its
    /// settings name `checkout`. Both are for display.
    WrongFolder {
        running: Option<String>,
        checkout: String,
    },
    /// The loaded job runs from the folder its settings name, but was loaded from an older
    /// generation of them (the plist was rewritten without a reload).
    OlderSettings,
    /// A self-restart is in flight: wait for it, then look again.
    Restarting,
}

/// What a read-only look at the source's restart requests found.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RestartLook {
    /// No request is in flight.
    Settled,
    /// One began recently enough that it may still be draining: wait for it.
    Young,
    /// One is older than the longest drain plus a teardown margin: nothing is
    /// protecting running work any more, so restarting it is safe.
    Stuck,
    /// The look could not be read.
    Unknown,
}

/// Forget a journal whose work is finished. A leftover one is harmless: the
/// next launch recovers from it by repeating steps that are safe to repeat.
fn forget_journal(plan: &Plan) {
    if let Err(error) = clear_journal(plan) {
        log::warn!("adoption journal could not be cleared: {error}");
    }
}

fn requested(cancel: &AtomicBool) -> bool {
    cancel.load(Ordering::Acquire)
}

/// The one step that waits for running work. `corsia stop` holds new work
/// (it queues and is delivered after the switch) and drains what is running
/// for up to `CORSIA_DRAIN_MAX_WAIT`; the count shown meanwhile is for the
/// window only and never decides anything.
fn stop_source(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    phase: &mut dyn FnMut(Phase),
) -> Result<(), String> {
    phase(Phase::Waiting { running: None });
    // Without the record a relaunch cannot know a stop is still draining, so a stop that
    // cannot be recorded is let to finish and then undone (see `stop_old`).
    lifecycle.stop_old(
        &mut |running| phase(Phase::Waiting { running }),
        &mut |stop| write_stopping(plan, &stop),
    )
}

/// A relaunch after Force Quit: the `corsia stop` the last run started may still
/// be draining. Until it exits the source install is not down yet and starting it
/// would do nothing, so wait (the Waiting phase) and only then undo. Never kill it.
fn await_orphaned_stop(
    journal: &Journal,
    lifecycle: &mut impl Lifecycle,
    phase: &mut dyn FnMut(Phase),
) {
    let Some(stop) = &journal.stop else { return };
    while lifecycle.stop_is_running(stop) {
        phase(Phase::Waiting { running: None });
        std::thread::sleep(lifecycle.orphan_poll());
    }
}

/// Ask until the source install can be taken over. A restart in flight is waited
/// for (a read-only look every poll, up to the wait), because it settles by
/// itself; every other cause is a refusal, worded for the person, that changes
/// nothing. Quit ends the wait without touching anything.
fn wait_until_ready(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    variable_names: &BTreeSet<String>,
    cancel: &AtomicBool,
    phase: &mut dyn FnMut(Phase),
) -> Result<(), AdoptionError> {
    let poll = lifecycle.restart_poll();
    loop {
        match lifecycle.check_ready(&plan.source.state, variable_names) {
            Ok(()) => return Ok(()),
            Err(ReadyError::Refused(message)) => return Err(AdoptionError::refusal(message)),
            Err(ReadyError::Failed(message)) => return Err(AdoptionError::untouched(message)),
            Err(ReadyError::NotRunning) => {
                return Err(AdoptionError::standalone(
                    NOT_RUNNING,
                    Some(Recovery::StartIt),
                ))
            }
            Err(ReadyError::WrongFolder { running, checkout }) => {
                return Err(AdoptionError::standalone(
                    wrong_folder_line(running.as_deref(), &checkout),
                    None,
                ))
            }
            Err(ReadyError::OlderSettings) => {
                return Err(AdoptionError::standalone(OLDER_SETTINGS, None))
            }
            Err(ReadyError::Restarting) => loop {
                if requested(cancel) {
                    return Err(AdoptionError::untouched(
                        "adoption cancelled; the source service is still running",
                    ));
                }
                // The request's own age decides, so a restart that is draining as
                // long as it is allowed to is never bounced, however long this waits.
                match lifecycle.look_for_restart(&plan.source.state) {
                    RestartLook::Stuck => {
                        return Err(AdoptionError::standalone(
                            STUCK_RESTARTING,
                            Some(Recovery::RestartIt),
                        ))
                    }
                    RestartLook::Young => {
                        phase(Phase::WaitingRestart);
                        std::thread::sleep(poll);
                    }
                    // Anything else sends the question back to the full check,
                    // which says what is wrong if something is.
                    RestartLook::Unknown => {
                        std::thread::sleep(poll);
                        break;
                    }
                    RestartLook::Settled => break,
                }
            },
        }
    }
}

/// Applies the durable adoption order. The source state root is never renamed:
/// it is pinned in service.json and opened by the packaged backend in place.
/// Quit (`cancel`) is honoured at each step boundary: before the stop it
/// changes nothing, after it the steps taken are undone. A Quit during the
/// drain waits for the stop to return.
pub fn adopt(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    cancel: &AtomicBool,
    phase: &mut dyn FnMut(Phase),
) -> Result<(), AdoptionError> {
    // Up to the stop, nothing has been asked of the old install.
    plan.preflight().map_err(|error| {
        AdoptionError::untouched(format!("adoption refused before changes: {error}"))
    })?;
    let variable_names = plan.variable_names().map_err(AdoptionError::untouched)?;
    // The backend's refusal is a whole sentence that tells the person what to
    // do, so it is shown as written.
    wait_until_ready(plan, lifecycle, &variable_names, cancel, phase)?;
    if requested(cancel) {
        return Err(AdoptionError::untouched(
            "adoption cancelled; the source service is still running",
        )
        .confirmed_running());
    }
    // Rollback records are written only after the source files are restored.
    // From the stop request on the source may be down, so the journal says so
    // first and every failure after it ends in the old install's start.
    write_journal(plan, Step::Stopping, None)
        .map_err(|error| AdoptionError::untouched(error).confirmed_running())?;
    if let Err(error) = stop_source(plan, lifecycle, phase) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "stop_old".into(),
                message: error,
            },
            None,
            false,
        );
    }
    if requested(cancel) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "cancelled".into(),
                message: "adoption cancelled".into(),
            },
            None,
            false,
        );
    }
    phase(Phase::BackingUp);
    let snapshot = match lifecycle.backup(&plan.source.state) {
        Ok(snapshot) => snapshot,
        Err(error) => return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "backup".into(),
                message: error,
            },
            None,
            false,
        ),
    };
    if let Err(error) = write_journal(plan, Step::BackedUp, Some(&snapshot)) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "record_backup".into(),
                message: error,
            },
            Some(&snapshot),
            false,
        );
    }
    if requested(cancel) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "cancelled".into(),
                message: "adoption cancelled".into(),
            },
            Some(&snapshot),
            false,
        );
    }
    if let Err(error) = move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "move_plist".into(),
                message: error,
            },
            Some(&snapshot),
            false,
        );
    }
    // The bootstrap config stays in place: it holds the workspace's identity
    // (name and id) and setup choices, which the app's backend reads from it.
    if plan.source.env_file.exists() {
        let names = match skipped_env_names(&plan.source.env_file) {
            Ok(names) => names,
            Err(error) => return rollback(
                plan,
                lifecycle,
                phase,
                Failure {
                    step: "read_environment".into(),
                    message: error,
                },
                Some(&snapshot),
                false,
            ),
        };
        if let Err(error) = move_to(
            &plan.source.env_file,
            &plan.adopted_dir().join("service.env"),
        ) {
            return rollback(
                plan,
                lifecycle,
                phase,
                Failure {
                    step: "move_environment".into(),
                    message: error,
                },
                Some(&snapshot),
                false,
            );
        }
        if !names.is_empty() {
            log::info!("adoption skipped source .env keys: {}", names.join(", "));
        }
    }
    if let Err(error) = write_journal(plan, Step::MetadataMoved, Some(&snapshot)) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "record_metadata".into(),
                message: error,
            },
            Some(&snapshot),
            false,
        );
    }
    // Nothing of the app's is registered yet, so if the source came back the
    // backup is deleted because it is an untouched-state copy.
    if let Err(error) = lifecycle.old_is_down() {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "old_is_down".into(),
                message: error,
            },
            Some(&snapshot),
            false,
        );
    }
    if let Err(error) = write_service(plan) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "write_service".into(),
                message: error,
            },
            Some(&snapshot),
            false,
        );
    }
    if let Err(error) = write_journal(plan, Step::ServiceWritten, Some(&snapshot)) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "record_service".into(),
                message: error,
            },
            Some(&snapshot),
            true,
        );
    }
    phase(Phase::Starting);
    if let Err(error) = lifecycle.start_new(&plan.source.state, plan.source.port, &plan.app_dir) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "start_new".into(),
                message: error,
            },
            Some(&snapshot),
            true,
        );
    }
    // The switch is made: a Quit from here finishes it and then exits.
    if let Err(error) = write_journal(plan, Step::Registered, Some(&snapshot)) {
        return rollback(
            plan,
            lifecycle,
            phase,
            Failure {
                step: "record_registered".into(),
                message: error,
            },
            Some(&snapshot),
            true,
        );
    }
    Ok(())
}

fn restore_files(plan: &Plan) -> Result<(), String> {
    // The app's claim goes first: the source's own start refuses to run on a
    // state root that service.json names.
    let service = plan.app_dir.join("service.json");
    if service.exists() {
        fs::remove_file(service).map_err(io)?;
    }
    let old_plist = plan.adopted_dir().join(PLIST);
    if old_plist.exists() {
        move_to(&old_plist, &plan.source.plist)?;
    }
    let old_env = plan.adopted_dir().join("service.env");
    if old_env.exists() {
        move_to(&old_env, &plan.source.env_file)?;
    }
    Ok(())
}

/// Recover an interrupted adoption before first-run selection. A possible app
/// start is always stopped and restored before the source service starts.
pub fn interrupted_plan(app_dir: &Path) -> Result<Option<Plan>, String> {
    let path = app_dir.join("adopted").join(JOURNAL);
    let raw = match fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io(error)),
    };
    let journal: Journal = serde_json::from_str(&raw)
        .map_err(|error| format!("adoption journal is unreadable: {error}"))?;
    if journal.step == Step::Registered {
        Ok(None)
    } else {
        Ok(Some(Plan {
            source: journal.source,
            app_dir: app_dir.to_path_buf(),
        }))
    }
}

/// Undo an interrupted or failed adoption from its journal, before first-run
/// selection. Every step is safe to repeat. A possible app start is always
/// stopped and restored before the source service starts.
#[cfg(test)]
pub fn recover(plan: &Plan, lifecycle: &mut impl Lifecycle) -> Result<(), AdoptionError> {
    recover_showing(plan, lifecycle, &mut |_| {})
}

/// `recover`, telling the window what it is waiting for.
pub fn recover_showing(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    phase: &mut dyn FnMut(Phase),
) -> Result<(), AdoptionError> {
    let Some(journal) = read_journal(plan)? else {
        return Ok(());
    };
    await_orphaned_stop(&journal, lifecycle, phase);
    match journal.step {
        Step::Registered => Ok(()),
        // Nothing was moved: only the source start is left.
        Step::Stopping | Step::Reverted => start_source(plan, lifecycle),
        step => {
            let snapshot = journal.snapshot.as_deref();
            let restore_snapshot = step >= Step::ServiceWritten;
            if restore_snapshot && snapshot.is_none() {
                return Err(AdoptionError::reopen("interrupted adoption has no backup snapshot"));
            }
            match rollback(
                plan,
                lifecycle,
                &mut |_| {},
                Failure {
                    step: "recover_interrupted_adoption".into(),
                    message: "recovering interrupted adoption".into(),
                },
                snapshot,
                restore_snapshot,
            ) {
                Err(error) if error.outcome == Outcome::Restored => Ok(()),
                other => other,
            }
        }
    }
}

/// At launch, before first-run selection: finish an interrupted adoption's undo.
/// `Ok(Some(error))` is an undo that could not finish, carrying the outcome and
/// the recovery the person is offered; `Err` is a journal that could not be read.
pub fn recover_on_launch<L: Lifecycle>(
    app_dir: &Path,
    lifecycle_for: impl FnOnce(&Plan) -> L,
    phase: &mut dyn FnMut(Phase),
) -> Result<Option<AdoptionError>, String> {
    let Some(interrupted) = interrupted_plan(app_dir)? else {
        return Ok(None);
    };
    Ok(recover_showing(&interrupted, &mut lifecycle_for(&interrupted), phase).err())
}

/// The "Start it again" action: only for a journal whose files are all where
/// they were. Anything further along needs the whole undo, which is `recover`.
pub fn start_again(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    phase: &mut dyn FnMut(Phase),
) -> Result<(), AdoptionError> {
    let Some(journal) = read_journal(plan)? else {
        return Ok(());
    };
    await_orphaned_stop(&journal, lifecycle, phase);
    match journal.step {
        Step::Registered => Ok(()),
        Step::Stopping | Step::Reverted => start_source(plan, lifecycle),
        _ => Err(AdoptionError::reopen(
            "the existing install's files are part-way between homes",
        )),
    }
}

/// With every file where it was, the source only needs to be running.
fn start_source(plan: &Plan, lifecycle: &mut impl Lifecycle) -> Result<(), AdoptionError> {
    lifecycle.start_old().map_err(|error| {
        AdoptionError::start_again(format!(
            "the existing install could not be started: {error}"
        ))
    })?;
    clear_journal(plan).map_err(AdoptionError::start_again)
}

fn rollback(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    phase: &mut dyn FnMut(Phase),
    failure: Failure,
    snapshot: Option<&Path>,
    restore_snapshot: bool,
) -> Result<(), AdoptionError> {
    phase(Phase::StartingExisting);
    // Any `?` before the journal says Reverted may leave files part-way
    // between homes, which converts to Recovery::Reopen.
    if restore_snapshot {
        let snapshot = snapshot.ok_or("rollback needs its backup snapshot")?;
        lifecycle
            .stop_new(&plan.source.state, plan.source.port)
            .map_err(|error| format!("{}; app service could not be stopped: {error}", failure.message))?;
        lifecycle.restore_backup(&plan.source.state, snapshot)
            .map_err(|error| format!("{}; backup could not be restored, so the old install was not restarted: {error}", failure.message))?;
    }
    restore_files(plan)?;
    write_failure_trail(plan, &failure);
    if let Some(snapshot) = snapshot.filter(|_| !restore_snapshot) {
        if let Err(error) = fs::remove_dir_all(snapshot) {
            if error.kind() != std::io::ErrorKind::NotFound {
                log::warn!("could not remove the disposable adoption snapshot: {error}");
            }
        }
    }
    write_journal_with(
        plan,
        Step::Reverted,
        snapshot.filter(|_| restore_snapshot),
        None,
    )?;
    // From here every file is back, so starting the source again is safe to repeat.
    if let Err(error) = lifecycle.start_old() {
        let failure = Failure {
            step: "start_old".into(),
            message: format!("{}; old install could not be restored: {error}", failure.message),
        };
        write_failure_trail(plan, &failure);
        return Err(AdoptionError::start_again(failure.message));
    }
    forget_journal(plan);
    Err(AdoptionError::restored(format!(
        "{}; the old install was restored",
        failure.message
    )))
}

fn io(error: std::io::Error) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tempfile::TempDir;

    struct Fake {
        ready_asked: Vec<BTreeSet<String>>,
        events: Vec<&'static str>,
        fail: Option<&'static str>,
        also_fail: Option<&'static str>,
        real_state: bool,
        snapshot_is_file: bool,
        /// Sets Quit's flag when the named step runs, as the menu would.
        quit_at: Option<(&'static str, Arc<AtomicBool>)>,
        service_json: PathBuf,
        plist: PathBuf,
        /// What `start_old` found on disk: (service.json present, plist present).
        start_old_saw: Vec<(bool, bool)>,
        /// The counts the window is told about while the stop drains.
        counts: Vec<Option<u32>>,
        /// What `check_ready` answers, one per ask, before it answers by `fail`.
        ready_script: Vec<ReadyError>,
        /// What each read-only look for a restart in flight finds, in order.
        restart_looks: Vec<RestartLook>,
        restart_forever: Option<RestartLook>,
        /// The `corsia stop` process the fake reports as soon as it exists.
        stop_process: Option<StopProcess>,
        /// The journal's recorded stop, read while the fake stop was draining.
        journal_in_stop: Option<Option<StopProcess>>,
        plan: Plan,
        /// A stop left by an earlier run keeps draining for this many looks.
        stop_alive_for: u32,
        stop_looks: u32,
    }
    impl Fake {
        fn fails(&self, step: &str) -> bool {
            self.fail == Some(step) || self.also_fail == Some(step)
        }

        fn step(&mut self, name: &'static str, error: &str) -> Result<(), String> {
            self.events.push(name);
            if let Some((at, flag)) = &self.quit_at {
                if *at == name {
                    flag.store(true, Ordering::Release);
                }
            }
            if self.fails(name) {
                Err(error.into())
            } else {
                Ok(())
            }
        }
    }

    fn copy_state(from: &Path, to: &Path) {
        fs::create_dir_all(to).unwrap();
        for entry in fs::read_dir(from).unwrap() {
            let path = entry.unwrap().path();
            let name = path.file_name().unwrap().to_owned();
            if name == "backups" {
                continue;
            }
            if path.is_dir() {
                copy_state(&path, &to.join(&name));
            } else {
                fs::copy(&path, to.join(&name)).unwrap();
            }
        }
    }
    impl Lifecycle for Fake {
        fn check_ready(
            &mut self,
            _: &Path,
            variable_names: &BTreeSet<String>,
        ) -> Result<(), ReadyError> {
            self.ready_asked.push(variable_names.clone());
            if !self.ready_script.is_empty() {
                Err(self.ready_script.remove(0))
            } else if self.fail == Some("check_ready") {
                Err(ReadyError::Refused(
                    "OPENAI_API_KEY is not stored in CORSIA.".into(),
                ))
            } else if self.fail == Some("check_ready_failed") {
                Err(ReadyError::Failed("could not check".into()))
            } else {
                Ok(())
            }
        }
        fn look_for_restart(&mut self, _: &Path) -> RestartLook {
            if let Some(look) = self.restart_forever {
                look
            } else if self.restart_looks.is_empty() {
                RestartLook::Settled
            } else {
                self.restart_looks.remove(0)
            }
        }
        fn restart_poll(&self) -> Duration {
            Duration::from_millis(1)
        }
        fn stop_old(
            &mut self,
            progress: &mut dyn FnMut(Option<u32>),
            spawned: &mut dyn FnMut(StopProcess) -> Result<(), String>,
        ) -> Result<(), String> {
            for count in self.counts.clone() {
                progress(count);
            }
            if let Some(process) = self.stop_process.clone() {
                spawned(process)?;
                self.journal_in_stop = Some(
                    read_journal(&self.plan)
                        .unwrap()
                        .and_then(|journal| journal.stop),
                );
            }
            self.step("stop_old", "stop failed")
        }
        fn stop_is_running(&mut self, _: &StopProcess) -> bool {
            self.stop_looks += 1;
            self.stop_looks <= self.stop_alive_for
        }
        fn orphan_poll(&self) -> Duration {
            Duration::from_millis(1)
        }
        fn old_is_down(&mut self) -> Result<(), String> {
            self.step("old_is_down", "source is running again")
        }
        fn backup(&mut self, state: &Path) -> Result<PathBuf, String> {
            self.step("backup", "backup failed")?;
            let snapshot = state.join("backups/fake-adoption");
            if self.snapshot_is_file {
                fs::create_dir_all(snapshot.parent().unwrap()).unwrap();
                fs::write(&snapshot, "not a directory").unwrap();
            } else if self.real_state {
                copy_state(state, &snapshot);
            }
            Ok(snapshot)
        }
        fn start_new(&mut self, state: &Path, _: u16, _: &Path) -> Result<(), String> {
            if self.real_state {
                fs::write(state.join("counts.txt"), "migrated").unwrap();
            }
            self.step("start_new", "new start failed")
        }
        fn stop_new(&mut self, _: &Path, _: u16) -> Result<(), String> {
            self.step("stop_new", "new stop failed")
        }
        fn restore_backup(&mut self, state: &Path, snapshot: &Path) -> Result<(), String> {
            self.step("restore_backup", "restore failed")?;
            if self.real_state {
                copy_state(snapshot, state);
            }
            Ok(())
        }
        fn start_old(&mut self) -> Result<(), String> {
            self.start_old_saw
                .push((self.service_json.exists(), self.plist.exists()));
            self.step("start_old", "old start failed")
        }
    }

    fn fake_for(plan: &Plan, fail: Option<&'static str>) -> Fake {
        Fake {
            ready_asked: vec![],
            events: vec![],
            fail,
            also_fail: None,
            real_state: false,
            snapshot_is_file: false,
            quit_at: None,
            service_json: plan.app_dir.join("service.json"),
            plist: plan.source.plist.clone(),
            start_old_saw: vec![],
            counts: vec![],
            ready_script: vec![],
            restart_looks: vec![],
            restart_forever: None,
            stop_process: None,
            journal_in_stop: None,
            plan: plan.clone(),
            stop_alive_for: 0,
            stop_looks: 0,
        }
    }

    /// Runs `adopt` with no Quit and records the phases shown.
    fn adopt_quietly(plan: &Plan, fake: &mut Fake) -> (Result<(), AdoptionError>, Vec<Phase>) {
        let mut phases = vec![];
        let result = adopt(plan, fake, &AtomicBool::new(false), &mut |p| phases.push(p));
        (result, phases)
    }

    fn plan(legacy_v1: bool) -> (TempDir, Plan) {
        let home = tempfile::Builder::new()
            .prefix("corsia-adoption-")
            .tempdir_in("/private/tmp")
            .unwrap();
        let source = home.path().join("source");
        let state = if legacy_v1 {
            source.join("workspace/.corsia")
        } else {
            home.path()
                .join("Library/Application Support/com.cebertowicz.corsia/state")
        };
        fs::create_dir_all(state.join("keys")).unwrap();
        fs::write(state.join("keys/provider"), "stored").unwrap();
        let mut database = vec![0; 64];
        database[..16].copy_from_slice(b"SQLite format 3\0");
        database[60..64].copy_from_slice(&1u32.to_be_bytes());
        fs::write(state.join("corsia.db"), database).unwrap();
        let plist = source.join("com.corsia.prod.plist");
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::write(&plist, "plist").unwrap();
        let app_dir = home.path().join("Library/Application Support/CORSIA");
        let env_file = home.path().join(".corsia/.env");
        (
            home,
            Plan {
                source: SourceInstall {
                    plist,
                    state,
                    workspace: source.join("workspace"),
                    port: 47_895,
                    working_directory: source.join("backend"),
                    generation: None,
                    cli: source.join("scripts/corsia"),
                    env_file,
                    plist_variable_names: vec![],
                },
                app_dir,
            },
        )
    }

    fn journal_step(plan: &Plan) -> Option<Step> {
        read_journal(plan).unwrap().map(|journal| journal.step)
    }

    /// The journal and the moved plist a crash at `step` leaves behind.
    fn interrupted_at(plan: &Plan, step: Step) {
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        if step >= Step::MetadataMoved && step != Step::Reverted {
            move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)).unwrap();
        }
        write_journal(
            plan,
            step,
            Some(&plan.source.state.join("backups/fake-adoption")),
        )
        .unwrap();
    }

    #[test]
    fn adopts_v2_in_place_and_records_the_exact_state_root() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        result.unwrap();
        let service: serde_json::Value =
            serde_json::from_slice(&fs::read(plan.app_dir.join("service.json")).unwrap()).unwrap();
        assert_eq!(
            service["state_dir"],
            plan.source.state.to_string_lossy().as_ref()
        );
        assert!(plan.source.state.join("corsia.db").exists());
        assert!(plan.adopted_dir().join(PLIST).exists());
        assert_eq!(
            fake.events,
            ["stop_old", "backup", "old_is_down", "start_new"]
        );
        assert_eq!(
            phases,
            [
                Phase::Waiting { running: None },
                Phase::BackingUp,
                Phase::Starting
            ],
            "each step names itself with one approved line"
        );
        assert_eq!(journal_step(&plan), Some(Step::Registered));
        assert!(interrupted_plan(&plan.app_dir).unwrap().is_none());
    }

    #[test]
    fn every_variable_name_reaches_the_backend_check_unfiltered() {
        let (_home, mut plan) = plan(false);
        fs::create_dir_all(plan.source.env_file.parent().unwrap()).unwrap();
        fs::write(
            &plan.source.env_file,
            "# note\nTUNING_SWITCH=1\nOPENAI_API_KEY=x\nANY_OTHER_NAME = y\n",
        )
        .unwrap();
        plan.source.plist_variable_names = vec!["BACKEND_PORT".into()];
        let mut fake = fake_for(&plan, None);
        adopt_quietly(&plan, &mut fake).0.unwrap();
        let asked: Vec<&str> = fake.ready_asked[0].iter().map(String::as_str).collect();
        // Which of these are provider keys is the backend's rule, not the app's.
        assert_eq!(
            asked,
            [
                "ANY_OTHER_NAME",
                "BACKEND_PORT",
                "OPENAI_API_KEY",
                "TUNING_SWITCH"
            ]
        );
    }

    #[test]
    fn legacy_state_is_also_pinned_in_place() {
        let (_home, plan) = plan(true);
        let mut fake = fake_for(&plan, None);
        adopt_quietly(&plan, &mut fake).0.unwrap();
        assert!(plan.source.state.join("corsia.db").exists());
        assert!(!plan.app_dir.join("state").exists());
    }

    #[test]
    fn t9_mac_studio_shape_keeps_counts_state_path_and_sign_in_through_adoption() {
        let (_home, plan) = plan(false);
        fs::write(
            plan.source.state.join("counts.txt"),
            "threads=12\nplans=3\nagents=5",
        )
        .unwrap();
        fs::create_dir_all(plan.source.state.join("auth")).unwrap();
        fs::write(plan.source.state.join("auth/session"), "signed-in").unwrap();
        let mut fake = fake_for(&plan, None);
        fake.real_state = true;
        adopt_quietly(&plan, &mut fake).0.unwrap();
        let service: serde_json::Value =
            serde_json::from_slice(&fs::read(plan.app_dir.join("service.json")).unwrap()).unwrap();
        assert_eq!(
            service["state_dir"],
            plan.source.state.to_string_lossy().as_ref()
        );
        assert_eq!(
            fs::read_to_string(plan.source.state.join("auth/session")).unwrap(),
            "signed-in"
        );
        assert!(
            !plan.source.plist.exists(),
            "the old service definition moved only after adoption"
        );
    }

    #[test]
    fn t9b_a_failure_at_each_step_restores_the_old_install_and_it_starts_again() {
        let original = "threads=12\nplans=3\nagents=5";
        for (failing, expected) in [
            ("stop_old", vec!["stop_old", "start_old"]),
            ("backup", vec!["stop_old", "backup", "start_old"]),
            (
                "start_new",
                vec![
                    "stop_old",
                    "backup",
                    "old_is_down",
                    "start_new",
                    "stop_new",
                    "restore_backup",
                    "start_old",
                ],
            ),
        ] {
            let (_home, plan) = plan(false);
            fs::write(plan.source.state.join("counts.txt"), original).unwrap();
            fs::create_dir_all(plan.source.state.join("auth")).unwrap();
            fs::write(plan.source.state.join("auth/session"), "signed-in").unwrap();
            let mut fake = fake_for(&plan, Some(failing));
            fake.real_state = true;
            let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
            assert_eq!(error.outcome, Outcome::Restored, "{failing}");
            assert_eq!(fake.events, expected, "{failing}");
            assert_eq!(
                fs::read_to_string(plan.source.state.join("counts.txt")).unwrap(),
                original,
                "{failing}: the migrated state is put back"
            );
            assert_eq!(
                fs::read_to_string(plan.source.state.join("auth/session")).unwrap(),
                "signed-in",
                "{failing}"
            );
            assert_eq!(
                fake.start_old_saw,
                [(false, true)],
                "{failing}: the source's start runs only once the app's claim is gone and its plist is back"
            );
            assert!(!plan.app_dir.join("service.json").exists(), "{failing}");
            assert_eq!(
                journal_step(&plan),
                None,
                "{failing}: nothing left to repeat"
            );
        }
    }

    #[test]
    fn a_refused_stop_changes_nothing_and_its_cause_reaches_the_person() {
        // The stop is the first effect: before it only the app's own journal exists.
        let (_home, plan) = plan(false);
        let plist = fs::read(&plan.source.plist).unwrap();
        let state_before = fs::read(plan.source.state.join("corsia.db")).unwrap();
        let mut fake = fake_for(&plan, Some("stop_old"));
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(error.outcome, Outcome::Restored);
        assert!(
            error.message().contains("stop failed"),
            "{}",
            error.message()
        );
        assert_eq!(fake.events, ["stop_old", "start_old"]);
        assert_eq!(fs::read(&plan.source.plist).unwrap(), plist);
        assert_eq!(
            fs::read(plan.source.state.join("corsia.db")).unwrap(),
            state_before
        );
        assert!(!plan.app_dir.join("service.json").exists());
        assert!(!plan.adopted_dir().join(PLIST).exists());
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn a_backup_that_cannot_be_put_back_never_starts_the_old_service() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("start_new"));
        fake.also_fail = Some("restore_backup");
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert!(error.message().contains("not restarted"));
        assert_eq!(error.outcome, Outcome::Unconfirmed);
        assert_eq!(error.recovery, Some(Recovery::Reopen));
        assert!(!fake.events.contains(&"start_old"));
    }

    #[test]
    fn an_app_service_that_will_not_stop_never_restores_or_starts_the_old_service() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("start_new"));
        fake.also_fail = Some("stop_new");
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(error.recovery, Some(Recovery::Reopen));
        assert!(!fake.events.contains(&"restore_backup"));
        assert!(!fake.events.contains(&"start_old"));
    }

    #[test]
    fn quit_before_the_stop_changes_nothing() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        let cancel = AtomicBool::new(true);
        let error = adopt(&plan, &mut fake, &cancel, &mut |_| {}).unwrap_err();
        assert_eq!(error.outcome, Outcome::Untouched);
        // The install was proven loaded just before the Quit was seen, so the line may say so.
        assert!(error.running);
        assert!(
            fake.events.is_empty(),
            "nothing is asked of the old install"
        );
        assert!(plan.source.plist.exists() && journal_step(&plan).is_none());
    }

    #[test]
    fn quit_after_a_step_undoes_the_steps_taken_before_it_ends() {
        for (at, expected) in [
            ("stop_old", vec!["stop_old", "start_old"]),
            ("backup", vec!["stop_old", "backup", "start_old"]),
        ] {
            let (_home, plan) = plan(false);
            let mut fake = fake_for(&plan, None);
            let cancel = Arc::new(AtomicBool::new(false));
            fake.quit_at = Some((at, cancel.clone()));
            let error = adopt(&plan, &mut fake, &cancel, &mut |_| {}).unwrap_err();
            assert_eq!(error.outcome, Outcome::Restored, "{at}");
            assert!(error.message().contains("cancelled"), "{at}");
            assert_eq!(fake.events, expected, "{at}");
            assert!(plan.source.plist.exists(), "{at}");
            assert_eq!(journal_step(&plan), None, "{at}");
        }
    }

    #[test]
    fn quit_once_the_switch_is_made_lets_it_finish() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        let cancel = Arc::new(AtomicBool::new(false));
        fake.quit_at = Some(("start_new", cancel.clone()));
        adopt(&plan, &mut fake, &cancel, &mut |_| {}).unwrap();
        assert_eq!(journal_step(&plan), Some(Step::Registered));
    }

    #[test]
    fn an_undo_is_shown_as_starting_the_existing_install() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("backup"));
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        result.unwrap_err();
        assert_eq!(
            phases,
            [
                Phase::Waiting { running: None },
                Phase::BackingUp,
                Phase::StartingExisting
            ]
        );
    }

    #[test]
    fn early_rollback_removes_its_untouched_snapshot_but_late_rollback_keeps_one() {
        let (_home, early_plan) = plan(false);
        let mut early = fake_for(&early_plan, Some("old_is_down"));
        early.real_state = true;
        assert_eq!(adopt_quietly(&early_plan, &mut early).0.unwrap_err().outcome, Outcome::Restored);
        assert!(!early_plan.source.state.join("backups/fake-adoption").exists());

        let (_home, late_plan) = plan(false);
        let mut late = fake_for(&late_plan, Some("start_new"));
        late.real_state = true;
        assert_eq!(adopt_quietly(&late_plan, &mut late).0.unwrap_err().outcome, Outcome::Restored);
        assert!(late_plan.source.state.join("backups/fake-adoption").is_dir());
    }

    #[test]
    fn an_early_rollback_restarts_the_source_when_snapshot_removal_fails() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("old_is_down"));
        fake.snapshot_is_file = true;
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(error.outcome, Outcome::Restored);
        assert_eq!(fake.events, ["stop_old", "backup", "old_is_down", "start_old"]);
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn rollback_writes_failure_detail_to_the_restored_install_log() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("start_new"));
        fake.also_fail = Some("start_old");
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(error.recovery, Some(Recovery::StartAgain));
        assert_eq!(read_journal(&plan).unwrap().unwrap().step, Step::Reverted);
        let log = fs::read_to_string(plan.source.state.join("backend.log")).unwrap();
        assert!(log.contains("start_new"));
        assert!(log.contains("start_old"));
    }

    #[test]
    fn recovery_after_service_write_restores_before_starting_old() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::ServiceWritten);
        let mut fake = fake_for(&plan, None);
        recover(&plan, &mut fake).unwrap();
        assert_eq!(fake.events, ["stop_new", "restore_backup", "start_old"]);
        assert!(plan.source.plist.exists());
        assert!(
            interrupted_plan(&plan.app_dir).unwrap().is_none(),
            "a finished undo leaves no journal to repeat"
        );
    }

    #[test]
    fn every_interrupted_step_ends_with_one_old_start_and_restored_files() {
        for step in [
            Step::Stopping,
            Step::BackedUp,
            Step::MetadataMoved,
            Step::ServiceWritten,
            Step::Reverted,
        ] {
            let (_home, plan) = plan(false);
            interrupted_at(&plan, step);
            let mut fake = fake_for(&plan, None);
            recover(&plan, &mut fake).unwrap();
            let expected: &[&str] = if step == Step::ServiceWritten {
                &["stop_new", "restore_backup", "start_old"]
            } else {
                &["start_old"]
            };
            assert_eq!(fake.events, expected, "{step:?}");
            assert!(plan.source.plist.exists(), "{step:?}");
            assert_eq!(fake.start_old_saw, [(false, true)], "{step:?}");
            assert_eq!(journal_step(&plan), None, "{step:?}");
        }
    }

    #[test]
    fn a_finished_switch_is_not_undone_on_launch() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::Registered);
        let untouched = recover_on_launch(
            &plan.app_dir,
            |_| -> Fake { panic!("a registered switch has nothing to undo") },
            &mut |_| {},
        );
        assert_eq!(untouched, Ok(None));
    }

    #[test]
    fn a_failed_undo_at_launch_reaches_the_person_with_its_outcome_and_recovery() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::ServiceWritten);
        // The old install's files are back but it will not start: the cause picks the button.
        let fake = fake_for(&plan, Some("start_old"));
        let failure = recover_on_launch(&plan.app_dir, move |_| fake, &mut |_| {})
            .unwrap()
            .expect("the undo could not finish");
        assert_eq!(failure.outcome, Outcome::Unconfirmed);
        assert_eq!(failure.recovery, Some(Recovery::StartAgain));
        assert!(failure
            .message
            .contains("old install could not be restored"));
        assert!(
            interrupted_plan(&plan.app_dir).unwrap().is_some(),
            "a failed undo keeps its journal for the recovery to finish"
        );
    }

    #[test]
    fn a_failed_undo_that_left_files_part_way_offers_a_reopen() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::ServiceWritten);
        let fake = fake_for(&plan, Some("stop_new"));
        let failure = recover_on_launch(&plan.app_dir, move |_| fake, &mut |_| {})
            .unwrap()
            .expect("the undo could not finish");
        assert_eq!(failure.outcome, Outcome::Unconfirmed);
        assert_eq!(failure.recovery, Some(Recovery::Reopen));
    }

    #[test]
    fn launch_without_an_interrupted_adoption_has_nothing_to_undo() {
        let (_home, plan) = plan(false);
        let untouched = recover_on_launch(
            &plan.app_dir,
            |_| -> Fake { panic!("nothing to undo") },
            &mut |_| {},
        );
        assert_eq!(untouched, Ok(None));
    }

    #[test]
    fn a_source_that_would_not_start_after_the_undo_is_started_again() {
        let (_home, plan) = plan(false);
        let mut broken = fake_for(&plan, Some("start_new"));
        broken.also_fail = Some("start_old");
        let error = adopt_quietly(&plan, &mut broken).0.unwrap_err();
        assert_eq!(error.recovery, Some(Recovery::StartAgain));
        assert!(plan.source.plist.exists());
        assert!(!plan.app_dir.join("service.json").exists());
        assert_eq!(journal_step(&plan), Some(Step::Reverted));

        let mut working = fake_for(&plan, None);
        start_again(&plan, &mut working, &mut |_| {}).unwrap();
        assert_eq!(working.events, ["start_old"]);
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn a_stop_that_failed_and_could_not_be_undone_is_started_again() {
        let (_home, plan) = plan(false);
        let mut broken = fake_for(&plan, Some("stop_old"));
        broken.also_fail = Some("start_old");
        let error = adopt_quietly(&plan, &mut broken).0.unwrap_err();
        assert_eq!(error.outcome, Outcome::Unconfirmed);
        assert_eq!(error.recovery, Some(Recovery::StartAgain));
        assert!(
            plan.source.plist.exists(),
            "the reboot line needs the plist on disk"
        );
        assert_eq!(journal_step(&plan), Some(Step::Reverted));
    }

    #[test]
    fn a_start_that_still_fails_stays_unconfirmed_and_keeps_its_journal() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::Reverted);
        let mut still_broken = fake_for(&plan, Some("start_old"));
        let error = start_again(&plan, &mut still_broken, &mut |_| {}).unwrap_err();
        assert_eq!(error.recovery, Some(Recovery::StartAgain));
        assert_eq!(journal_step(&plan), Some(Step::Reverted));
    }

    #[test]
    fn start_again_never_runs_the_undo_for_files_still_part_way() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::MetadataMoved);
        let mut fake = fake_for(&plan, None);
        let error = start_again(&plan, &mut fake, &mut |_| {}).unwrap_err();
        assert_eq!(error.recovery, Some(Recovery::Reopen));
        assert!(fake.events.is_empty());
    }

    #[test]
    fn a_stalled_undo_is_finished_through_the_journal_after_reopening() {
        let (_home, plan) = plan(false);
        let mut stalled = fake_for(&plan, Some("start_new"));
        stalled.also_fail = Some("stop_new");
        let error = adopt_quietly(&plan, &mut stalled).0.unwrap_err();
        assert_eq!(error.recovery, Some(Recovery::Reopen));
        assert!(
            !plan.source.plist.exists(),
            "files are part-way between homes"
        );

        // The relaunch knows only the journal.
        let interrupted = interrupted_plan(&plan.app_dir)
            .unwrap()
            .expect("the unfinished undo is found on launch");
        let mut launch = fake_for(&plan, None);
        recover(&interrupted, &mut launch).unwrap();
        assert_eq!(launch.events, ["stop_new", "restore_backup", "start_old"]);
        assert!(plan.source.plist.exists());
        assert!(interrupted_plan(&plan.app_dir).unwrap().is_none());
    }

    #[test]
    fn a_torn_journal_temp_beside_a_valid_journal_still_recovers() {
        let (_home, plan) = plan(false);
        interrupted_at(&plan, Step::ServiceWritten);
        fs::write(
            plan.adopted_dir().join("adoption.json.tmp"),
            b"{\"step\": \"serv",
        )
        .unwrap();
        let interrupted = interrupted_plan(&plan.app_dir)
            .unwrap()
            .expect("the valid journal is still found");
        let mut fake = fake_for(&plan, None);
        recover(&interrupted, &mut fake).unwrap();
        assert_eq!(fake.events, ["stop_new", "restore_backup", "start_old"]);
        assert!(plan.source.plist.exists());
    }

    #[test]
    fn stale_temp_files_from_a_crash_never_block_a_retry() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        fs::write(plan.app_dir.join("service.json.tmp"), b"partial").unwrap();
        fs::write(plan.adopted_dir().join("adoption.json.tmp"), b"partial").unwrap();
        write_service(&plan).unwrap();
        write_journal(&plan, Step::Stopping, None).unwrap();
        let service: serde_json::Value =
            serde_json::from_slice(&fs::read(plan.app_dir.join("service.json")).unwrap()).unwrap();
        assert_eq!(service["port"], plan.source.port);
        assert!(
            !plan.app_dir.join("service.json.tmp").exists()
                && !plan.adopted_dir().join("adoption.json.tmp").exists()
        );
        assert!(interrupted_plan(&plan.app_dir).unwrap().is_some());
    }

    #[test]
    fn missing_provider_key_refuses_before_the_stop_without_reading_values() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.source.env_file.parent().unwrap()).unwrap();
        fs::write(&plan.source.env_file, "OPENAI_API_KEY=never-log-this\n").unwrap();
        let mut fake = fake_for(&plan, Some("check_ready"));
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        // The backend is asked by name and its refusal stops adoption before
        // the source service is touched.
        assert_eq!(
            fake.ready_asked,
            vec![BTreeSet::from(["OPENAI_API_KEY".to_string()])]
        );
        assert_eq!(error.message(), "OPENAI_API_KEY is not stored in CORSIA.");
        assert_eq!(error.outcome, Outcome::Untouched);
        assert!(error.advice);
        // The backend answered, so the install is known to be loaded from this checkout.
        assert!(error.running);
        assert!(!error.message().contains("never-log-this"));
        assert!(fake.events.is_empty());
    }

    #[test]
    fn a_check_that_could_not_run_is_untouched_detail_for_the_log() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("check_ready_failed"));
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(error.outcome, Outcome::Untouched);
        assert!(!error.advice);
        // Nothing proved it loaded, so the line says only that nothing was changed.
        assert!(!error.running);
        assert!(fake.events.is_empty());
    }

    #[test]
    fn skipped_env_audit_contains_names_never_values() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.source.env_file.parent().unwrap()).unwrap();
        fs::write(
            &plan.source.env_file,
            "TUNING=true\nAPI_KEY=secret-value\n# comment\n",
        )
        .unwrap();
        let names = skipped_env_names(&plan.source.env_file).unwrap();
        let logged = format!("adoption skipped source .env keys: {}", names.join(", "));
        assert_eq!(names, ["TUNING", "API_KEY"]);
        assert!(!logged.contains("secret-value") && !logged.contains("true"));
    }

    #[test]
    fn the_failure_reaches_the_window_as_an_outcome_not_as_text_to_match() {
        let json = serde_json::to_value(AdoptionError::untouched("x")).unwrap();
        assert_eq!(
            json,
            serde_json::json!({ "message": "x", "outcome": "untouched", "recovery": null, "advice": false })
        );
        let json = serde_json::to_value(AdoptionError::refusal("x")).unwrap();
        assert_eq!(json["advice"], true);
        assert_eq!(json["running"], true);
        let json = serde_json::to_value(AdoptionError::restored("y")).unwrap();
        assert_eq!(json["outcome"], "restored");
        let json = serde_json::to_value(AdoptionError::switched("s")).unwrap();
        assert_eq!(json["outcome"], "switched");
        assert_eq!(json["recovery"], serde_json::Value::Null);
        let json = serde_json::to_value(AdoptionError::start_again("a")).unwrap();
        assert_eq!(json["outcome"], "unconfirmed");
        assert_eq!(json["recovery"], "start_again");
        // A plain error claims nothing and offers the one recovery that is always safe.
        let json = serde_json::to_value(AdoptionError::from("z".to_string())).unwrap();
        assert_eq!(
            json,
            serde_json::json!({ "message": "z", "outcome": "unconfirmed", "recovery": "reopen", "advice": false })
        );
    }

    #[test]
    fn phases_reach_the_window_as_names_not_as_text() {
        for (phase, name) in [
            (Phase::WaitingRestart, "waiting_restart"),
            (Phase::BackingUp, "backing_up"),
            (Phase::Starting, "starting"),
            (Phase::StartingExisting, "starting_existing"),
            (Phase::MovingToBackup, "moving_to_backup"),
        ] {
            assert_eq!(
                serde_json::to_value(phase).unwrap(),
                serde_json::json!({ "phase": name })
            );
        }
    }

    /// A source checkout holding the lifecycle owner, and a plist pointing at it.
    fn installed_plist(home: &Path, environment: &str) -> PathBuf {
        let cli = home.join("checkout/scripts/corsia");
        fs::create_dir_all(cli.parent().unwrap()).unwrap();
        fs::write(&cli, "#!/bin/sh\n").unwrap();
        let plist = home.join("Library/LaunchAgents/com.corsia.prod.plist");
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::write(
            &plist,
            format!(
                r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>com.corsia.prod</string><key>WorkingDirectory</key><string>{}</string><key>EnvironmentVariables</key><dict>{environment}</dict></dict></plist>"#,
                home.join("checkout/backend").display()
            ),
        )
        .unwrap();
        plist
    }

    fn scratch_home(prefix: &str) -> TempDir {
        tempfile::Builder::new()
            .prefix(prefix)
            .tempdir_in("/private/tmp")
            .unwrap()
    }

    #[test]
    fn source_plist_controls_the_adopted_paths() {
        let home = scratch_home("corsia-adoption-plist-");
        fs::create_dir_all(home.path().join(".corsia")).unwrap();
        fs::write(
            home.path().join(".corsia/config.json"),
            "{\"workspace\":\"/work\"}",
        )
        .unwrap();
        let plist = installed_plist(
            home.path(),
            "<key>CORSIA_WORKSPACE</key><string>/work</string><key>CORSIA_STATE_DIR</key><string>/state</string><key>BACKEND_PORT</key><string>47895</string><key>OPENAI_API_KEY</key><string>not-exposed</string>",
        );
        let source = source_from_plist(home.path(), &plist).unwrap();
        assert_eq!(source.workspace, PathBuf::from("/work"));
        assert_eq!(source.state, PathBuf::from("/state"));
        assert_eq!(source.port, 47_895);
        assert_eq!(source.cli, home.path().join("checkout/scripts/corsia"));
        assert!(source
            .plist_variable_names
            .contains(&"OPENAI_API_KEY".to_string()));
        assert!(source
            .plist_variable_names
            .contains(&"BACKEND_PORT".to_string()));
    }

    #[test]
    fn a_checkout_without_its_lifecycle_owner_is_refused() {
        let home = scratch_home("corsia-adoption-nocli-");
        let plist = installed_plist(
            home.path(),
            "<key>CORSIA_WORKSPACE</key><string>/work</string><key>CORSIA_STATE_DIR</key><string>/state</string><key>BACKEND_PORT</key><string>47895</string>",
        );
        fs::remove_file(home.path().join("checkout/scripts/corsia")).unwrap();
        let reason = source_from_plist(home.path(), &plist).unwrap_err();
        assert!(reason.contains("scripts/corsia"), "{reason}");
    }

    #[test]
    fn roots_missing_from_the_plist_come_from_the_config_that_records_them() {
        let home = scratch_home("corsia-adoption-roots-");
        fs::create_dir_all(home.path().join(".corsia")).unwrap();
        let plist = installed_plist(home.path(), "<key>BACKEND_PORT</key><string>47895</string>");
        let config = home.path().join(".corsia/config.json");

        // A v2 config names both roots, so the state is the recorded one.
        fs::write(
            &config,
            r#"{"version":2,"workspace":{"id":"w","name":"n","root":"/work"},"state_root":"/state"}"#,
        )
        .unwrap();
        let source = source_from_plist(home.path(), &plist).unwrap();
        assert_eq!(
            (source.workspace, source.state),
            (PathBuf::from("/work"), PathBuf::from("/state"))
        );

        // An older config names only the workspace; its state is beside it.
        fs::write(&config, r#"{"workspace":"/work"}"#).unwrap();
        let source = source_from_plist(home.path(), &plist).unwrap();
        assert_eq!(source.state, PathBuf::from("/work/.corsia"));

        // Nothing records the roots: the install is refused, not guessed.
        fs::write(&config, "{}").unwrap();
        assert!(source_from_plist(home.path(), &plist)
            .unwrap_err()
            .contains("no workspace"));
    }

    #[test]
    fn the_window_is_told_the_count_while_the_stop_drains() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.counts = vec![Some(2), Some(1), None];
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        result.unwrap();
        assert_eq!(
            &phases[..4],
            [
                Phase::Waiting { running: None },
                Phase::Waiting { running: Some(2) },
                Phase::Waiting { running: Some(1) },
                Phase::Waiting { running: None },
            ],
            "a count that could not be read is no count, never zero"
        );
    }

    #[test]
    fn the_count_reaches_the_window_as_a_number_or_nothing() {
        assert_eq!(
            serde_json::to_value(Phase::Waiting { running: Some(3) }).unwrap(),
            serde_json::json!({ "phase": "waiting", "running": 3 })
        );
        assert_eq!(
            serde_json::to_value(Phase::Waiting { running: None }).unwrap(),
            serde_json::json!({ "phase": "waiting", "running": null })
        );
    }

    fn error_json(error: &AdoptionError) -> serde_json::Value {
        serde_json::to_value(error).unwrap()
    }

    #[test]
    fn an_existing_install_that_is_not_running_offers_to_start_it_and_changes_nothing() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::NotRunning];
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(
            error.message(),
            "Your existing CORSIA isn’t running. Start it, and this app will take it over."
        );
        assert_eq!(error.outcome, Outcome::Untouched);
        assert_eq!(error.recovery, Some(Recovery::StartIt));
        assert!(error.standalone);
        assert!(fake.events.is_empty());
        assert_eq!(journal_step(&plan), None);
        let shown = error_json(&error);
        assert_eq!(shown["standalone"], true);
        assert_eq!(shown["recovery"], "start_it");
    }

    #[test]
    fn a_failed_start_or_restart_is_unconfirmed_with_try_again() {
        let error = AdoptionError::could_not_start("`corsia start` exited 1");
        assert_eq!(error.outcome, Outcome::Unconfirmed);
        assert_eq!(error.recovery, Some(Recovery::TryAgain));
        assert!(!error.standalone);
        assert!(error_json(&error).get("standalone").is_none());
    }

    #[test]
    fn a_job_running_from_another_folder_names_both_folders_and_offers_no_button() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::WrongFolder {
            running: Some("~/old/corsia".into()),
            checkout: "~/new/corsia".into(),
        }];
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(
            error.message(),
            "Your existing CORSIA is running from ~/old/corsia, but its settings name ~/new/corsia. Nothing was changed."
        );
        assert_eq!(error.recovery, None);
        assert!(error.standalone);
        assert!(fake.events.is_empty());

        fake.ready_script = vec![ReadyError::WrongFolder {
            running: None,
            checkout: "~/new/corsia".into(),
        }];
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert!(error.message().contains("running from an unknown folder"));
    }

    #[test]
    fn settings_older_than_the_saved_ones_say_to_restart_the_computer_and_offer_no_restart_button()
    {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::OlderSettings];
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(
            error.message(),
            "Your existing CORSIA is running older settings than the ones saved for it. Restart this computer, and this app will take it over."
        );
        assert_eq!(error.recovery, None);
        assert_eq!(error.outcome, Outcome::Untouched);
        assert!(error.standalone);
        assert!(fake.events.is_empty());
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn a_restart_in_flight_is_waited_for_and_adoption_then_goes_on() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::Restarting];
        fake.restart_looks = vec![RestartLook::Young, RestartLook::Young, RestartLook::Settled];
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        result.unwrap();
        assert_eq!(
            phases,
            [
                Phase::WaitingRestart,
                Phase::WaitingRestart,
                Phase::Waiting { running: None },
                Phase::BackingUp,
                Phase::Starting
            ]
        );
        assert_eq!(
            fake.ready_asked.len(),
            2,
            "the full check runs again once the restart settled"
        );
        assert_eq!(
            fake.events,
            ["stop_old", "backup", "old_is_down", "start_new"]
        );
    }

    #[test]
    fn a_restart_look_that_cannot_be_read_goes_back_to_the_full_check() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::Restarting];
        fake.restart_looks = vec![RestartLook::Unknown];
        adopt_quietly(&plan, &mut fake).0.unwrap();
        assert_eq!(fake.ready_asked.len(), 2);
    }

    #[test]
    fn a_request_older_than_the_longest_drain_is_stuck_at_once_and_offers_to_restart_it() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::Restarting];
        fake.restart_looks = vec![RestartLook::Stuck];
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        let error = result.unwrap_err();
        assert_eq!(error.message(), "Your existing CORSIA is stuck restarting.");
        assert_eq!(error.outcome, Outcome::Untouched);
        assert_eq!(error.recovery, Some(Recovery::RestartIt));
        assert!(error.standalone);
        assert!(phases.is_empty(), "an old request is not waited for first");
        assert!(fake.events.is_empty());
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn a_request_that_ages_out_while_waiting_becomes_stuck() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::Restarting];
        fake.restart_looks = vec![RestartLook::Young, RestartLook::Stuck];
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        assert_eq!(result.unwrap_err().recovery, Some(Recovery::RestartIt));
        assert_eq!(phases, [Phase::WaitingRestart]);
    }

    #[test]
    fn a_young_request_is_waited_for_however_long_it_takes_and_never_bounced() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::Restarting];
        fake.restart_looks = [vec![RestartLook::Young; 200], vec![RestartLook::Settled]].concat();
        let (result, phases) = adopt_quietly(&plan, &mut fake);
        result.unwrap();
        assert_eq!(
            phases
                .iter()
                .filter(|phase| **phase == Phase::WaitingRestart)
                .count(),
            200
        );
    }

    #[test]
    fn quit_ends_the_wait_for_a_restart_without_touching_anything() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.ready_script = vec![ReadyError::Restarting];
        fake.restart_forever = Some(RestartLook::Young);
        let error = adopt(&plan, &mut fake, &AtomicBool::new(true), &mut |_| {}).unwrap_err();
        assert_eq!(error.outcome, Outcome::Untouched);
        assert!(!error.standalone);
        assert!(fake.events.is_empty());
    }

    fn a_stop() -> StopProcess {
        StopProcess {
            pid: 4242,
            started: "Sat Oct 17 05:50:00 2026".into(),
        }
    }

    #[test]
    fn the_journal_names_the_stop_process_while_it_drains() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        fake.stop_process = Some(a_stop());
        adopt_quietly(&plan, &mut fake).0.unwrap();
        assert_eq!(fake.journal_in_stop, Some(Some(a_stop())));
    }

    #[test]
    fn a_relaunch_waits_for_a_stop_still_draining_before_it_starts_the_old_install() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        write_journal(&plan, Step::Stopping, None).unwrap();
        write_stopping(&plan, &a_stop()).unwrap();
        let mut fake = fake_for(&plan, None);
        fake.stop_alive_for = 3;
        let mut phases = vec![];
        recover_showing(&plan, &mut fake, &mut |phase| phases.push(phase)).unwrap();
        assert_eq!(phases, vec![Phase::Waiting { running: None }; 3]);
        assert_eq!(
            fake.stop_looks, 4,
            "it looks again until the process is gone"
        );
        assert_eq!(
            fake.events,
            ["start_old"],
            "the stop is waited for, never killed or repeated"
        );
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn a_journal_without_a_recorded_stop_starts_the_old_install_at_once() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        write_journal(&plan, Step::Stopping, None).unwrap();
        let mut fake = fake_for(&plan, None);
        fake.stop_alive_for = 99;
        recover(&plan, &mut fake).unwrap();
        assert_eq!(fake.stop_looks, 0);
        assert_eq!(fake.events, ["start_old"]);
    }

    #[test]
    fn start_again_also_waits_for_a_stop_left_draining() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        write_stopping(&plan, &a_stop()).unwrap();
        let mut fake = fake_for(&plan, None);
        fake.stop_alive_for = 2;
        let mut phases = vec![];
        start_again(&plan, &mut fake, &mut |phase| phases.push(phase)).unwrap();
        assert_eq!(phases.len(), 2);
        assert_eq!(fake.events, ["start_old"]);
    }

    #[test]
    fn a_journal_from_before_the_stop_was_recorded_still_reads() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        write_journal(&plan, Step::Stopping, None).unwrap();
        let mut value: serde_json::Value =
            serde_json::from_slice(&fs::read(journal_path(&plan)).unwrap()).unwrap();
        value.as_object_mut().unwrap().remove("stop");
        fs::write(journal_path(&plan), serde_json::to_vec(&value).unwrap()).unwrap();
        assert_eq!(read_journal(&plan).unwrap().unwrap().stop, None);
    }

    #[test]
    fn quit_during_the_drain_is_honoured_when_the_stop_returns() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, None);
        let cancel = Arc::new(AtomicBool::new(false));
        fake.quit_at = Some(("stop_old", cancel.clone()));
        let error = adopt(&plan, &mut fake, &cancel, &mut |_| {}).unwrap_err();
        assert_eq!(error.outcome, Outcome::Restored);
        assert_eq!(fake.events, ["stop_old", "start_old"]);
        assert!(plan.source.plist.exists());
        assert_eq!(journal_step(&plan), None);
    }

    #[test]
    fn a_source_that_came_back_is_not_overwritten_with_the_backup() {
        let (_home, plan) = plan(false);
        let mut fake = fake_for(&plan, Some("old_is_down"));
        let error = adopt_quietly(&plan, &mut fake).0.unwrap_err();
        assert_eq!(error.outcome, Outcome::Restored);
        assert_eq!(
            fake.events,
            ["stop_old", "backup", "old_is_down", "start_old"],
            "no app service was started, so there is nothing to stop or restore"
        );
        assert_eq!(fake.start_old_saw, [(false, true)]);
        assert!(!plan.app_dir.join("service.json").exists());
        assert_eq!(journal_step(&plan), None);
    }
}
