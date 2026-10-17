//! Recoverable in-place adoption of a source install by the packaged app.
//!
//! The state root never moves. The durable journal, source-service metadata,
//! and lifecycle seam make each irreversible step recoverable in tests without
//! reaching launchd or a real home folder.

use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::Duration,
};

use serde::{Deserialize, Serialize};

const JOURNAL: &str = "adoption.json";
const PLIST: &str = "com.corsia.prod.plist";

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct SourceInstall {
    pub plist: PathBuf,
    pub config: PathBuf,
    pub state: PathBuf,
    pub workspace: PathBuf,
    pub port: u16,
    pub lifecycle_script: PathBuf,
    pub python: PathBuf,
    pub env_file: PathBuf,
    pub plist_provider_key_names: Vec<String>,
    pub legacy_v1: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    pub source: SourceInstall,
    pub app_dir: PathBuf,
}

/// Read the owned source service's rendered plist. Directory names are not a
/// substitute for this contract: the plist supplies the roots and port the old
/// job actually used.
pub fn source_from_plist(home: &Path, plist_path: &Path) -> Result<SourceInstall, String> {
    let value = plist::Value::from_file(plist_path)
        .map_err(|error| format!("could not read source service plist: {error}"))?;
    let root = value
        .as_dictionary()
        .ok_or("source service plist is not a dictionary")?;
    if root.get("Label").and_then(plist::Value::as_string) != Some("com.corsia.prod") {
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
    let python = root
        .get("ProgramArguments")
        .and_then(plist::Value::as_array)
        .and_then(|arguments| arguments.first())
        .and_then(plist::Value::as_string)
        .map(PathBuf::from)
        .ok_or("source service plist has no Python executable")?;
    let text = |name: &str| {
        environment
            .get(name)
            .and_then(plist::Value::as_string)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
    };
    let workspace = text("CORSIA_WORKSPACE")
        .or_else(|| {
            fs::read_to_string(home.join(".corsia/config.json"))
                .ok()
                .and_then(|raw| {
                    serde_json::from_str::<serde_json::Value>(&raw)
                        .ok()?
                        .get("workspace")?
                        .as_str()
                        .map(PathBuf::from)
                })
        })
        .ok_or("source service plist has no workspace")?;
    let state = text("CORSIA_STATE_DIR").unwrap_or_else(|| workspace.join(".corsia"));
    let port = environment
        .get("BACKEND_PORT")
        .and_then(plist::Value::as_string)
        .and_then(|value| value.parse().ok())
        .filter(|port: &u16| *port != 0)
        .ok_or("source service plist has no valid port")?;
    let source_root = working_directory
        .parent()
        .ok_or("source service backend has no checkout root")?;
    Ok(SourceInstall {
        plist: plist_path.to_path_buf(),
        config: home.join(".corsia/config.json"),
        lifecycle_script: source_root.join("scripts/corsia"),
        python,
        env_file: home.join(".corsia/.env"),
        legacy_v1: state == workspace.join(".corsia"),
        plist_provider_key_names: environment
            .keys()
            .filter(|name| is_provider_key_name(name))
            .cloned()
            .collect(),
        state,
        workspace,
        port,
    })
}

impl Plan {
    pub fn adopted_dir(&self) -> PathBuf {
        self.app_dir.join("adopted")
    }

    /// All refusal conditions run before the source service starts draining.
    pub fn preflight(&self, supported_schema: u32) -> Result<(), String> {
        if !self.source.state.join("corsia.db").is_file() {
            return Err("the existing install has no corsia.db".into());
        }
        if schema_version(&self.source.state.join("corsia.db"))
            .is_some_and(|version| version > supported_schema)
        {
            return Err("your data is newer than this app".into());
        }
        Ok(())
    }

    /// The variable names the source install sets for provider access, for the
    /// backend to compare with what it has stored. Names only.
    pub fn expected_key_names(&self) -> Result<BTreeSet<String>, String> {
        provider_key_names(&self.source.env_file, &self.source.plist_provider_key_names)
    }
}

/// Production owns the launchd and database effects. Unit tests provide a
/// fake, which makes the rollback order observable without invoking them.
pub trait Lifecycle {
    /// Ask the source backend whether adoption can finish: every expected
    /// provider key is stored (names compared, nothing else read) and the disk
    /// holds the backup plus a reserve. Runs before anything is drained.
    fn check_ready(&mut self, state: &Path, key_names: &BTreeSet<String>) -> Result<(), String>;
    /// The id the drain will carry, so a failed start can still cancel it.
    fn new_operation_id(&mut self) -> String;
    /// Start the drain operation with that id. It has not released the
    /// source service yet; the caller owns polling and cancellation.
    fn begin_drain(&mut self, operation_id: &str) -> Result<DrainStatus, String>;
    /// Return the live count for the same operation, never a projection of a
    /// replacement operation.
    fn drain_status(&mut self, operation_id: &str) -> Result<DrainStatus, String>;
    /// Compensate only this operation. This is deliberately callable before
    /// the source service is booted out, so Quit leaves it running.
    fn cancel_drain(&mut self, operation_id: &str) -> Result<(), String>;
    /// Transfer the exact quiescent drain to the lifecycle owner.
    fn complete_drain(&mut self, operation_id: &str) -> Result<(), String>;
    /// The old launchd job may be released only after exact drain completion.
    fn bootout_old(&mut self) -> Result<(), String>;
    /// Snapshot the whole state-root set before the app backend can migrate it.
    fn backup(&mut self, state: &Path) -> Result<PathBuf, String>;
    /// Register and prove the new backend is healthy (and hence any migration
    /// has run). This is one transaction phase.
    fn start_new(&mut self, state: &Path, port: u16, app_dir: &Path) -> Result<(), String>;
    /// Boot out the app job and prove it released the state and port.
    fn stop_new(&mut self, state: &Path, port: u16) -> Result<(), String>;
    /// Restore the snapshot before the source service is restarted.
    fn restore_backup(&mut self, state: &Path, snapshot: &Path) -> Result<(), String>;
    fn start_old(&mut self) -> Result<(), String>;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DrainStatus {
    pub operation_id: String,
    pub active_processes: usize,
    pub active: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
enum Step {
    Stopped,
    BackedUp,
    MetadataMoved,
    ServiceWritten,
    NewStarted,
    Registered,
}

#[derive(Deserialize, Serialize)]
struct Journal {
    step: Step,
    source: SourceInstall,
    snapshot: Option<PathBuf>,
}

fn journal_path(plan: &Plan) -> PathBuf {
    plan.adopted_dir().join(JOURNAL)
}

/// Replace `path` so a crash leaves either the old contents or the new, never a
/// partial file: a temp file beside it is synced, renamed over it, and the
/// folder is synced. A temp file left by an earlier crash is discarded first,
/// so it can never block a retry.
fn write_atomically(path: &Path, contents: &[u8]) -> Result<(), String> {
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
    let journal = Journal {
        step,
        source: plan.source.clone(),
        snapshot: snapshot.map(Path::to_path_buf),
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

fn is_provider_key_name(name: &str) -> bool {
    name.ends_with("_API_KEY")
}

fn provider_key_names(path: &Path, plist_names: &[String]) -> Result<BTreeSet<String>, String> {
    let mut names: BTreeSet<String> = plist_names.iter().cloned().collect();
    names.extend(
        skipped_env_names(path)?
            .into_iter()
            .filter(|name| is_provider_key_name(name)),
    );
    Ok(names)
}

/// What state the existing install is in when adoption stops. The window words
/// its first line from this, never from the reason text.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    /// It was never stopped and holds no new restart operation.
    Untouched,
    /// It was stopped, then started again from its own files.
    Restored,
    /// Not known to be running, or still holding new work.
    Unconfirmed,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AdoptionError {
    pub message: String,
    pub outcome: Outcome,
}

impl AdoptionError {
    pub fn untouched(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Untouched)
    }

    pub fn restored(message: impl Into<String>) -> Self {
        Self::with(message, Outcome::Restored)
    }

    #[cfg(test)]
    pub fn message(&self) -> &str {
        &self.message
    }

    fn with(message: impl Into<String>, outcome: Outcome) -> Self {
        Self {
            message: message.into(),
            outcome,
        }
    }
}

/// A plain error claims nothing: the window says it could not confirm the old
/// install, the one reading that is never false.
impl From<String> for AdoptionError {
    fn from(message: String) -> Self {
        Self::with(message, Outcome::Unconfirmed)
    }
}

impl From<&str> for AdoptionError {
    fn from(message: &str) -> Self {
        message.to_string().into()
    }
}

/// Ends the held drain after `cause`. The old install is untouched only when
/// the source confirms the exact operation is cancelled; its restart hold
/// never expires, so an unconfirmed cancel may leave it holding every new turn.
fn abandon_drain(
    lifecycle: &mut impl Lifecycle,
    operation_id: &str,
    cause: String,
) -> AdoptionError {
    match lifecycle.cancel_drain(operation_id) {
        Ok(()) => AdoptionError::untouched(cause),
        Err(error) => AdoptionError::from(format!(
            "{cause}; the hold on new work could not be released: {error}"
        )),
    }
}

/// Applies the durable adoption order. The source state root is never renamed:
/// it is pinned in service.json and opened by the packaged backend in place.
pub fn adopt(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    supported_schema: u32,
    cancel_requested: &AtomicBool,
) -> Result<(), AdoptionError> {
    // Up to the drain, nothing has been asked of the old install.
    plan.preflight(supported_schema).map_err(|error| {
        AdoptionError::untouched(format!("adoption refused before changes: {error}"))
    })?;
    let key_names = plan
        .expected_key_names()
        .map_err(AdoptionError::untouched)?;
    // The backend's refusal is a whole sentence that tells the person what to
    // do, so it is shown as written.
    lifecycle
        .check_ready(&plan.source.state, &key_names)
        .map_err(AdoptionError::untouched)?;
    // From the drain request on, the source may hold new work until the exact
    // operation is cancelled, so every failure before the bootout cancels it.
    let operation_id = lifecycle.new_operation_id();
    let started = match lifecycle.begin_drain(&operation_id) {
        Ok(started) => started,
        Err(error) => return Err(abandon_drain(lifecycle, &operation_id, error)),
    };
    let mut status = started;
    loop {
        if cancel_requested.load(Ordering::Acquire) {
            return Err(abandon_drain(
                lifecycle,
                &operation_id,
                "adoption cancelled; the source service is still running".into(),
            ));
        }
        if status.operation_id != operation_id || !status.active {
            return Err(abandon_drain(
                lifecycle,
                &operation_id,
                "adoption drain changed before completion; the source service is still running"
                    .into(),
            ));
        }
        if status.active_processes == 0 {
            break;
        }
        thread::sleep(Duration::from_millis(250));
        status = match lifecycle.drain_status(&operation_id) {
            Ok(status) => status,
            Err(error) => return Err(abandon_drain(lifecycle, &operation_id, error)),
        };
    }
    if let Err(error) = lifecycle.complete_drain(&operation_id) {
        return Err(abandon_drain(lifecycle, &operation_id, error));
    }
    if let Err(error) = lifecycle.bootout_old() {
        return Err(match lifecycle.start_old() {
            Ok(()) => AdoptionError::restored(format!("{error}; the source service was restarted")),
            Err(restart_error) => AdoptionError::from(format!(
                "{error}; the source service could not be restarted: {restart_error}"
            )),
        });
    }
    write_journal(plan, Step::Stopped, None)?;
    let snapshot = match lifecycle.backup(&plan.source.state) {
        Ok(snapshot) => snapshot,
        Err(error) => return rollback(plan, lifecycle, error, None),
    };
    write_journal(plan, Step::BackedUp, Some(&snapshot))?;
    if let Err(error) = move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)) {
        return rollback(plan, lifecycle, error, None);
    }
    if plan.source.config.exists() {
        if let Err(error) = move_to(&plan.source.config, &plan.adopted_dir().join("config.json")) {
            return rollback(plan, lifecycle, error, None);
        }
    }
    if plan.source.env_file.exists() {
        let names = skipped_env_names(&plan.source.env_file)?;
        if let Err(error) = move_to(
            &plan.source.env_file,
            &plan.adopted_dir().join("service.env"),
        ) {
            return rollback(plan, lifecycle, error, None);
        }
        if !names.is_empty() {
            log::info!("adoption skipped source .env keys: {}", names.join(", "));
        }
    }
    write_journal(plan, Step::MetadataMoved, Some(&snapshot))?;
    if let Err(error) = write_service(plan) {
        return rollback(plan, lifecycle, error, None);
    }
    write_journal(plan, Step::ServiceWritten, Some(&snapshot))?;
    if let Err(error) = lifecycle.start_new(&plan.source.state, plan.source.port, &plan.app_dir) {
        return rollback(plan, lifecycle, error, Some(&snapshot));
    }
    if let Err(error) = write_journal(plan, Step::NewStarted, Some(&snapshot)) {
        return rollback(plan, lifecycle, error, Some(&snapshot));
    }
    if let Err(error) = write_journal(plan, Step::Registered, Some(&snapshot)) {
        return rollback(plan, lifecycle, error, Some(&snapshot));
    }
    Ok(())
}

fn restore_files(plan: &Plan) -> Result<(), String> {
    let service = plan.app_dir.join("service.json");
    if service.exists() {
        fs::remove_file(service).map_err(io)?;
    }
    let old_plist = plan.adopted_dir().join(PLIST);
    if old_plist.exists() {
        move_to(&old_plist, &plan.source.plist)?;
    }
    let old_config = plan.adopted_dir().join("config.json");
    if old_config.exists() {
        move_to(&old_config, &plan.source.config)?;
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

pub fn recover(plan: &Plan, lifecycle: &mut impl Lifecycle) -> Result<(), String> {
    let journal = fs::read_to_string(journal_path(plan)).map_err(io)?;
    let journal: Journal = serde_json::from_str(&journal)
        .map_err(|error| format!("adoption journal is unreadable: {error}"))?;
    if journal.step == Step::Registered {
        return Ok(());
    }
    let snapshot = if journal.step >= Step::ServiceWritten {
        Some(
            journal
                .snapshot
                .as_deref()
                .ok_or("interrupted adoption has no backup snapshot")?,
        )
    } else {
        None
    };
    rollback(
        plan,
        lifecycle,
        "recovering interrupted adoption".into(),
        snapshot,
    )
    .map_err(|error| error.message)
}

fn rollback(
    plan: &Plan,
    lifecycle: &mut impl Lifecycle,
    cause: String,
    snapshot: Option<&Path>,
) -> Result<(), AdoptionError> {
    // Any `?` below leaves the old install stopped on purpose or by failure,
    // which converts to Outcome::Unconfirmed.
    if let Some(snapshot) = snapshot {
        lifecycle
            .stop_new(&plan.source.state, plan.source.port)
            .map_err(|error| format!("{cause}; app service could not be stopped: {error}"))?;
        lifecycle.restore_backup(&plan.source.state, snapshot)
            .map_err(|error| format!("{cause}; backup could not be restored, so the old install was not restarted: {error}"))?;
    }
    restore_files(plan)?;
    lifecycle
        .start_old()
        .map_err(|error| format!("{cause}; old install could not be restored: {error}"))?;
    Err(AdoptionError::restored(format!(
        "{cause}; the old install was restored"
    )))
}

fn schema_version(database: &Path) -> Option<u32> {
    let bytes = fs::read(database).ok()?;
    if bytes.len() < 64 || &bytes[..16] != b"SQLite format 3\0" {
        return None;
    }
    Some(u32::from_be_bytes(bytes[60..64].try_into().ok()?))
}

fn io(error: std::io::Error) -> String {
    error.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use tempfile::TempDir;

    struct Fake {
        ready_asked: Vec<BTreeSet<String>>,
        events: Vec<&'static str>,
        fail: Option<&'static str>,
        also_fail: Option<&'static str>,
        statuses: VecDeque<DrainStatus>,
        real_state: bool,
    }
    impl Fake {
        fn fails(&self, step: &str) -> bool {
            self.fail == Some(step) || self.also_fail == Some(step)
        }

        fn status(&mut self) -> DrainStatus {
            self.statuses.pop_front().unwrap_or(DrainStatus {
                operation_id: "a".into(),
                active_processes: 0,
                active: true,
            })
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
        fn check_ready(&mut self, _: &Path, key_names: &BTreeSet<String>) -> Result<(), String> {
            self.ready_asked.push(key_names.clone());
            if self.fail == Some("check_ready") {
                Err("OPENAI_API_KEY is not stored in CORSIA.".into())
            } else {
                Ok(())
            }
        }
        fn new_operation_id(&mut self) -> String {
            "a".into()
        }
        fn begin_drain(&mut self, _: &str) -> Result<DrainStatus, String> {
            self.events.push("begin_drain");
            if self.fails("begin_drain") {
                return Err("begin failed".into());
            }
            Ok(self.status())
        }
        fn drain_status(&mut self, _: &str) -> Result<DrainStatus, String> {
            self.events.push("drain_status");
            if self.fails("drain_status") {
                return Err("status failed".into());
            }
            Ok(self.status())
        }
        fn cancel_drain(&mut self, _: &str) -> Result<(), String> {
            self.events.push("cancel_drain");
            if self.fails("cancel_drain") {
                return Err("cancel failed".into());
            }
            Ok(())
        }
        fn complete_drain(&mut self, _: &str) -> Result<(), String> {
            self.events.push("complete_drain");
            if self.fails("complete_drain") {
                return Err("complete failed".into());
            }
            Ok(())
        }
        fn bootout_old(&mut self) -> Result<(), String> {
            self.events.push("bootout_old");
            if self.fail == Some("bootout_old") {
                Err("bootout failed".into())
            } else {
                Ok(())
            }
        }
        fn backup(&mut self, state: &Path) -> Result<PathBuf, String> {
            self.events.push("backup");
            if self.fail == Some("backup") {
                return Err("backup failed".into());
            }
            let snapshot = state.join("backups/fake-adoption");
            if self.real_state {
                copy_state(state, &snapshot);
            }
            Ok(snapshot)
        }
        fn start_new(&mut self, state: &Path, _: u16, _: &Path) -> Result<(), String> {
            self.events.push("start_new");
            if self.real_state {
                fs::write(state.join("counts.txt"), "migrated").unwrap();
            }
            if matches!(
                self.fail,
                Some("start_new") | Some("start_new_restore_backup")
            ) {
                Err("new start failed".into())
            } else {
                Ok(())
            }
        }
        fn stop_new(&mut self, _: &Path, _: u16) -> Result<(), String> {
            self.events.push("stop_new");
            if self.fails("stop_new") {
                Err("new stop failed".into())
            } else {
                Ok(())
            }
        }
        fn restore_backup(&mut self, state: &Path, snapshot: &Path) -> Result<(), String> {
            self.events.push("restore_backup");
            if self.fails("restore_backup") || self.fails("start_new_restore_backup") {
                return Err("restore failed".into());
            }
            if self.real_state {
                copy_state(snapshot, state);
            }
            Ok(())
        }
        fn start_old(&mut self) -> Result<(), String> {
            self.events.push("start_old");
            if self.fails("start_old") {
                return Err("old start failed".into());
            }
            Ok(())
        }
    }

    fn fake(fail: Option<&'static str>) -> Fake {
        Fake {
            ready_asked: vec![],
            events: vec![],
            fail,
            also_fail: None,
            statuses: VecDeque::new(),
            real_state: false,
        }
    }
    fn uncancelled() -> AtomicBool {
        AtomicBool::new(false)
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
        let config = source.join("config.json");
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::write(&plist, "plist").unwrap();
        fs::write(&config, "{}").unwrap();
        let app_dir = home.path().join("Library/Application Support/CORSIA");
        let env_file = home.path().join(".corsia/.env");
        (
            home,
            Plan {
                source: SourceInstall {
                    plist,
                    config,
                    state,
                    workspace: source.join("workspace"),
                    port: 47_895,
                    lifecycle_script: source.join("scripts/corsia"),
                    python: PathBuf::from("/usr/bin/python3"),
                    env_file,
                    plist_provider_key_names: vec![],
                    legacy_v1,
                },
                app_dir,
            },
        )
    }

    #[test]
    fn adopts_v2_in_place_and_records_the_exact_state_root() {
        let (_home, plan) = plan(false);
        let mut fake = fake(None);
        let cancelled = uncancelled();
        adopt(&plan, &mut fake, 1, &cancelled).unwrap();
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
            [
                "begin_drain",
                "complete_drain",
                "bootout_old",
                "backup",
                "start_new"
            ]
        );
    }

    #[test]
    fn legacy_state_is_also_pinned_in_place() {
        let (_home, plan) = plan(true);
        let mut fake = fake(None);
        let cancelled = uncancelled();
        adopt(&plan, &mut fake, 1, &cancelled).unwrap();
        assert!(plan.source.state.join("corsia.db").exists());
        assert!(!plan.app_dir.join("state").exists());
    }

    #[test]
    fn post_start_failure_stops_new_restores_backup_before_old_start() {
        let (_home, plan) = plan(false);
        let mut fake = fake(Some("start_new"));
        let cancelled = uncancelled();
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("restored"));
        assert_eq!(
            fake.events,
            [
                "begin_drain",
                "complete_drain",
                "bootout_old",
                "backup",
                "start_new",
                "stop_new",
                "restore_backup",
                "start_old"
            ]
        );
        assert!(plan.source.plist.exists());
        assert!(plan.source.config.exists());
    }

    #[test]
    fn backup_restore_failure_never_starts_the_old_service() {
        let (_home, plan) = plan(false);
        let mut fake = fake(Some("start_new_restore_backup"));
        let cancelled = uncancelled();
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("not restarted"));
        assert_eq!(
            fake.events,
            [
                "begin_drain",
                "complete_drain",
                "bootout_old",
                "backup",
                "start_new",
                "stop_new",
                "restore_backup"
            ]
        );
    }

    #[test]
    fn recovery_after_service_write_restores_before_starting_old() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)).unwrap();
        write_journal(
            &plan,
            Step::ServiceWritten,
            Some(&plan.source.state.join("backups/fake-adoption")),
        )
        .unwrap();
        let mut fake = fake(None);
        assert!(recover(&plan, &mut fake).unwrap_err().contains("restored"));
        assert_eq!(fake.events, ["stop_new", "restore_backup", "start_old"]);
        assert!(plan.source.plist.exists());
    }

    #[test]
    fn a_torn_journal_temp_beside_a_valid_journal_still_recovers() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)).unwrap();
        write_journal(
            &plan,
            Step::ServiceWritten,
            Some(&plan.source.state.join("backups/fake-adoption")),
        )
        .unwrap();
        fs::write(
            plan.adopted_dir().join("adoption.json.tmp"),
            b"{\"step\": \"serv",
        )
        .unwrap();
        let interrupted = interrupted_plan(&plan.app_dir)
            .unwrap()
            .expect("the valid journal is still found");
        let mut fake = fake(None);
        assert!(recover(&interrupted, &mut fake)
            .unwrap_err()
            .contains("restored"));
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
        write_journal(&plan, Step::Stopped, None).unwrap();
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
    fn bootout_failure_restarts_the_old_service_and_never_snapshots() {
        let (_home, plan) = plan(false);
        let mut fake = fake(Some("bootout_old"));
        let cancelled = uncancelled();
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("restarted"));
        assert_eq!(
            fake.events,
            ["begin_drain", "complete_drain", "bootout_old", "start_old"]
        );
    }

    #[test]
    fn backup_failure_restores_files_and_restarts_old_without_touching_the_app() {
        let (_home, plan) = plan(false);
        let mut fake = fake(Some("backup"));
        let cancelled = uncancelled();
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("restored"));
        assert_eq!(
            fake.events,
            [
                "begin_drain",
                "complete_drain",
                "bootout_old",
                "backup",
                "start_old"
            ]
        );
        assert!(plan.source.plist.exists());
        assert!(!plan.app_dir.join("service.json").exists());
    }

    #[test]
    fn app_release_failure_never_restores_or_starts_the_old_service() {
        let (_home, plan) = plan(false);
        let mut fake = fake(Some("stop_new"));
        fs::create_dir_all(plan.adopted_dir()).unwrap();
        move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)).unwrap();
        write_journal(
            &plan,
            Step::NewStarted,
            Some(&plan.source.state.join("backups/fake-adoption")),
        )
        .unwrap();
        assert!(recover(&plan, &mut fake)
            .unwrap_err()
            .contains("could not be stopped"));
        assert_eq!(fake.events, ["stop_new"]);
    }

    #[test]
    fn every_interrupted_step_ends_with_one_old_start_and_restored_files() {
        for step in [
            Step::Stopped,
            Step::BackedUp,
            Step::MetadataMoved,
            Step::ServiceWritten,
            Step::NewStarted,
        ] {
            let (_home, plan) = plan(false);
            fs::create_dir_all(plan.adopted_dir()).unwrap();
            if step >= Step::MetadataMoved {
                move_to(&plan.source.plist, &plan.adopted_dir().join(PLIST)).unwrap();
            }
            write_journal(
                &plan,
                step,
                Some(&plan.source.state.join("backups/fake-adoption")),
            )
            .unwrap();
            let mut fake = fake(None);
            assert!(
                recover(&plan, &mut fake).unwrap_err().contains("restored"),
                "{step:?}"
            );
            let expected: &[&str] = if step >= Step::ServiceWritten {
                &["stop_new", "restore_backup", "start_old"]
            } else {
                &["start_old"]
            };
            assert_eq!(fake.events, expected, "{step:?}");
            assert!(plan.source.plist.exists(), "{step:?}");
        }
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
        let mut fake = fake(None);
        fake.real_state = true;
        let cancelled = uncancelled();
        adopt(&plan, &mut fake, 1, &cancelled).unwrap();
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
    fn t9b_failed_start_restores_counts_and_sign_in_before_the_old_service_starts() {
        let (_home, plan) = plan(false);
        let original = "threads=12\nplans=3\nagents=5";
        fs::write(plan.source.state.join("counts.txt"), original).unwrap();
        fs::create_dir_all(plan.source.state.join("auth")).unwrap();
        fs::write(plan.source.state.join("auth/session"), "signed-in").unwrap();
        let mut fake = fake(Some("start_new"));
        fake.real_state = true;
        let cancelled = uncancelled();
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("restored"));
        assert_eq!(
            fs::read_to_string(plan.source.state.join("counts.txt")).unwrap(),
            original
        );
        assert_eq!(
            fs::read_to_string(plan.source.state.join("auth/session")).unwrap(),
            "signed-in"
        );
        assert_eq!(fake.events.last(), Some(&"start_old"));
        let restore = fake
            .events
            .iter()
            .position(|event| *event == "restore_backup")
            .unwrap();
        assert!(restore < fake.events.len() - 1);
        assert!(plan.source.plist.exists() && !plan.app_dir.join("service.json").exists());
    }

    #[test]
    fn missing_provider_key_refuses_before_drain_without_reading_values() {
        let (_home, plan) = plan(false);
        fs::create_dir_all(plan.source.env_file.parent().unwrap()).unwrap();
        fs::write(&plan.source.env_file, "OPENAI_API_KEY=never-log-this\n").unwrap();
        let mut fake = fake(Some("check_ready"));
        let cancelled = uncancelled();
        let error = adopt(&plan, &mut fake, 1, &cancelled).unwrap_err();
        // The backend is asked by name and its refusal stops adoption before
        // the source service is drained.
        assert_eq!(
            fake.ready_asked,
            vec![BTreeSet::from(["OPENAI_API_KEY".to_string()])]
        );
        assert_eq!(error.message(), "OPENAI_API_KEY is not stored in CORSIA.");
        assert_eq!(error.outcome, Outcome::Untouched);
        assert!(!error.message().contains("never-log-this"));
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
        assert!(!logged.contains("true"));
        assert!(!logged.contains("secret-value"));
    }

    #[test]
    fn drain_waits_for_live_count_before_bootout() {
        let (_home, plan) = plan(false);
        let mut fake = fake(None);
        let cancelled = uncancelled();
        fake.statuses = VecDeque::from([
            DrainStatus {
                operation_id: "a".into(),
                active_processes: 1,
                active: true,
            },
            DrainStatus {
                operation_id: "a".into(),
                active_processes: 0,
                active: true,
            },
        ]);
        adopt(&plan, &mut fake, 1, &cancelled).unwrap();
        assert_eq!(
            fake.events,
            [
                "begin_drain",
                "drain_status",
                "complete_drain",
                "bootout_old",
                "backup",
                "start_new"
            ]
        );
    }

    #[test]
    fn replacement_drain_is_cancelled_without_booting_out_source() {
        let (_home, plan) = plan(false);
        let mut fake = fake(None);
        let cancelled = uncancelled();
        fake.statuses = VecDeque::from([
            DrainStatus {
                operation_id: "a".into(),
                active_processes: 1,
                active: true,
            },
            DrainStatus {
                operation_id: "different".into(),
                active_processes: 0,
                active: true,
            },
        ]);
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("changed"));
        assert_eq!(fake.events, ["begin_drain", "drain_status", "cancel_drain"]);
    }

    #[test]
    fn quit_cancels_exact_drain_without_booting_out_source() {
        let (_home, plan) = plan(false);
        let mut fake = fake(None);
        let cancelled = AtomicBool::new(true);
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("cancelled"));
        assert_eq!(fake.events, ["begin_drain", "cancel_drain"]);
    }

    /// Fresh plan and fake for one scenario; `statuses` lets a drain wait.
    fn scenario(
        fail: Option<&'static str>,
        also_fail: Option<&'static str>,
        waiting: bool,
    ) -> (TempDir, Plan, Fake) {
        let (home, plan) = plan(false);
        let mut fake = fake(fail);
        fake.also_fail = also_fail;
        fake.real_state = true;
        if waiting {
            fake.statuses.push_back(DrainStatus {
                operation_id: "a".into(),
                active_processes: 1,
                active: true,
            });
        }
        (home, plan, fake)
    }

    fn outcome_of(
        fail: Option<&'static str>,
        also_fail: Option<&'static str>,
        waiting: bool,
    ) -> Outcome {
        let (_home, plan, mut fake) = scenario(fail, also_fail, waiting);
        adopt(&plan, &mut fake, 1, &uncancelled())
            .unwrap_err()
            .outcome
    }

    #[test]
    fn refusals_before_the_drain_leave_the_install_untouched() {
        let (_home, plan) = plan(false);
        let mut database = fs::read(plan.source.state.join("corsia.db")).unwrap();
        database[60..64].copy_from_slice(&2u32.to_be_bytes());
        fs::write(plan.source.state.join("corsia.db"), database).unwrap();
        let newer = adopt(&plan, &mut fake(None), 1, &uncancelled()).unwrap_err();
        assert_eq!(newer.outcome, Outcome::Untouched);

        assert_eq!(
            outcome_of(Some("check_ready"), None, false),
            Outcome::Untouched
        );
    }

    #[test]
    fn a_drain_failure_is_untouched_only_when_the_cancel_is_confirmed() {
        // drain_status and complete_drain are the two steps a held drain can
        // fail at; begin_drain can fail after the source took the request.
        for step in ["begin_drain", "drain_status", "complete_drain"] {
            let waiting = step == "drain_status";
            let (_home, plan, mut cancelled) = scenario(Some(step), None, waiting);
            let error = adopt(&plan, &mut cancelled, 1, &uncancelled()).unwrap_err();
            assert_eq!(error.outcome, Outcome::Untouched, "{step}, cancel works");
            assert_eq!(cancelled.events.last(), Some(&"cancel_drain"), "{step}");
            assert!(!cancelled.events.contains(&"bootout_old"), "{step}");

            let (_home, plan, mut stuck) = scenario(Some(step), Some("cancel_drain"), waiting);
            let error = adopt(&plan, &mut stuck, 1, &uncancelled()).unwrap_err();
            assert_eq!(error.outcome, Outcome::Unconfirmed, "{step}, cancel fails");
            assert!(error.message().contains("could not be released"), "{step}");
            assert!(!stuck.events.contains(&"bootout_old"), "{step}");
        }
    }

    #[test]
    fn a_cancelled_wait_is_untouched_only_when_the_cancel_is_confirmed() {
        let (_home, plan, mut fake) = scenario(None, None, false);
        let error = adopt(&plan, &mut fake, 1, &AtomicBool::new(true)).unwrap_err();
        assert_eq!(error.outcome, Outcome::Untouched);

        let (_home, plan, mut fake) = scenario(None, Some("cancel_drain"), false);
        let error = adopt(&plan, &mut fake, 1, &AtomicBool::new(true)).unwrap_err();
        assert_eq!(error.outcome, Outcome::Unconfirmed);
    }

    #[test]
    fn a_failure_after_the_stop_is_restored_or_unconfirmed() {
        // The old install was stopped and came back from its own files.
        for step in ["bootout_old", "backup", "start_new"] {
            assert_eq!(
                outcome_of(Some(step), None, false),
                Outcome::Restored,
                "{step}"
            );
        }
        // It was stopped and could not be brought back, or was deliberately left stopped.
        assert_eq!(
            outcome_of(Some("bootout_old"), Some("start_old"), false),
            Outcome::Unconfirmed
        );
        assert_eq!(
            outcome_of(Some("backup"), Some("start_old"), false),
            Outcome::Unconfirmed
        );
        for step in ["stop_new", "restore_backup"] {
            let (_home, plan, mut fake) = scenario(Some("start_new"), Some(step), false);
            let error = adopt(&plan, &mut fake, 1, &uncancelled()).unwrap_err();
            assert_eq!(error.outcome, Outcome::Unconfirmed, "{step}");
            assert!(
                !fake.events.contains(&"start_old"),
                "{step} never restarts the old install"
            );
        }
    }

    #[test]
    fn the_failure_reaches_the_window_as_an_outcome_not_as_text_to_match() {
        let json = serde_json::to_value(AdoptionError::untouched("x")).unwrap();
        assert_eq!(
            json,
            serde_json::json!({ "message": "x", "outcome": "untouched" })
        );
        let json = serde_json::to_value(AdoptionError::restored("y")).unwrap();
        assert_eq!(
            json,
            serde_json::json!({ "message": "y", "outcome": "restored" })
        );
        // A plain error claims nothing.
        let json = serde_json::to_value(AdoptionError::from("z".to_string())).unwrap();
        assert_eq!(
            json,
            serde_json::json!({ "message": "z", "outcome": "unconfirmed" })
        );
    }

    #[test]
    fn future_schema_refuses_before_stop() {
        let (_home, plan) = plan(false);
        let mut database = fs::read(plan.source.state.join("corsia.db")).unwrap();
        database[60..64].copy_from_slice(&2u32.to_be_bytes());
        fs::write(plan.source.state.join("corsia.db"), database).unwrap();
        let mut fake = fake(None);
        let cancelled = uncancelled();
        assert!(adopt(&plan, &mut fake, 1, &cancelled)
            .unwrap_err()
            .message()
            .contains("newer"));
        assert!(fake.events.is_empty());
    }

    #[test]
    fn source_plist_controls_the_adopted_paths() {
        let home = tempfile::Builder::new()
            .prefix("corsia-adoption-plist-")
            .tempdir_in("/private/tmp")
            .unwrap();
        let plist = home
            .path()
            .join("Library/LaunchAgents/com.corsia.prod.plist");
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::create_dir_all(home.path().join(".corsia")).unwrap();
        fs::write(
            home.path().join(".corsia/config.json"),
            "{\"workspace\":\"/work\"}",
        )
        .unwrap();
        fs::write(&plist, r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>com.corsia.prod</string><key>WorkingDirectory</key><string>/source/backend</string><key>ProgramArguments</key><array><string>/source/.venv/bin/python</string></array><key>EnvironmentVariables</key><dict><key>CORSIA_WORKSPACE</key><string>/work</string><key>CORSIA_STATE_DIR</key><string>/state</string><key>BACKEND_PORT</key><string>47895</string><key>OPENAI_API_KEY</key><string>not-exposed</string></dict></dict></plist>"#).unwrap();
        let source = source_from_plist(home.path(), &plist).unwrap();
        assert_eq!(source.workspace, PathBuf::from("/work"));
        assert_eq!(source.state, PathBuf::from("/state"));
        assert_eq!(source.port, 47_895);
        assert_eq!(source.plist_provider_key_names, ["OPENAI_API_KEY"]);
    }
}
