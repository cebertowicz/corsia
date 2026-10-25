//! Moves an install's data out of the folder named for the old app identifier.
//!
//! An install adopted from a source checkout keeps its state where the source
//! left it, `~/Library/Application Support/com.cebertowicz.corsia/state`, and
//! `service.json` pins that path. At launch, before anything attaches to or
//! starts the service, this stops the service and CORSIA's own processes,
//! proves nothing else holds the folder, renames it to `CORSIA/state` in one
//! `renamex_np(RENAME_EXCL)`, and repoints every file that names it. The rename
//! is one metadata operation on the same volume, whatever the data's size; it
//! never copies. The journal `CORSIA/state-move.json` makes every step safe to
//! resume, and the launcher refuses to start while it exists. The commit point
//! is the `service.json` rewrite: before it a failure puts the folder back,
//! after it the move only goes forward.

use serde::{Deserialize, Serialize};
use std::{
    ffi::CString,
    fs, io,
    os::unix::{ffi::OsStrExt, fs::MetadataExt},
    path::{Path, PathBuf},
    process::Command,
    thread,
    time::{Duration, Instant},
};

use crate::{adoption, app_paths, first_run, release_proof};

pub const JOURNAL: &str = "state-move.json";
const SERVICE_FILE: &str = "service.json";
/// Where entries the person left beside the old state folder are kept.
const EARLIER_FOLDER: &str = "earlier-app-folder";
/// `renamex_np` and every check run on paths this long at most.
const DRAIN_CAP: Duration = Duration::from_secs(1800);
const RELEASE_CAP: Duration = Duration::from_secs(55);

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Step {
    /// The stop was asked for: the service may be down and unregistered.
    Stopping,
    /// Everything is stopped and proven released; the rename is next.
    Stopped,
    /// The folder is at its new path. From here the move only goes forward.
    Moved,
}

#[derive(Debug, Deserialize, Serialize)]
struct Journal {
    from: PathBuf,
    to: PathBuf,
    step: Step,
}

/// The folders and files a move reads and writes, all from one home folder.
pub struct Paths {
    pub app_dir: PathBuf,
    /// `…/com.cebertowicz.corsia/state`.
    pub legacy: PathBuf,
    /// `…/CORSIA/state`, the packaged app's default state root.
    pub target: PathBuf,
    pub bootstrap_config: PathBuf,
    pub old_cache: PathBuf,
}

impl Paths {
    pub fn for_home(home: &Path, app_dir: &Path) -> Self {
        let support = home.join("Library").join("Application Support");
        Self {
            app_dir: app_dir.to_path_buf(),
            legacy: support.join(first_run::LEGACY_STATE_FOLDER).join(app_paths::STATE_DIRECTORY),
            target: app_dir.join(app_paths::STATE_DIRECTORY),
            bootstrap_config: home.join(".corsia").join("config.json"),
            old_cache: home.join("Library").join("Caches").join(first_run::LEGACY_STATE_FOLDER),
        }
    }

    fn journal(&self) -> PathBuf {
        self.app_dir.join(JOURNAL)
    }

    fn legacy_parent(&self) -> &Path {
        self.legacy.parent().unwrap_or(&self.legacy)
    }
}

/// The running service, as the move needs it. Production drives launchd and the
/// bundled CLI; tests provide a fake so every step can be interrupted.
pub trait Service {
    /// Whether an update or restart is in flight. A service that is not
    /// answering has none in flight: the update lock covers the rest.
    fn operation_active(&mut self) -> Result<bool, String>;
    /// Drain, stop the local model, then boot the service out (`corsia stop
    /// --stop-local-model`). A failure leaves the service running.
    fn stop(&mut self) -> Result<(), String>;
    fn unregister(&mut self) -> Result<(), String>;
    /// Stop CORSIA's recorded managed processes by fingerprint-checked group.
    fn stop_recorded_processes(&mut self, state: &Path) -> Result<(), String>;
    /// The service process is gone, its port refuses and its database is free.
    fn prove_released(&mut self, state: &Path) -> Result<(), String>;
    /// Every process still holding `state`, from the kernel's process table.
    fn holders(&mut self, state: &Path) -> Result<Vec<(i32, String)>, String> {
        holders(state)
    }
}

/// Gets the move's whole-run percentage; ticked from a timer thread during long steps.
pub type Progress<'a> = dyn FnMut(u8) + Send + 'a;

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Nothing to move: a fresh, source or already-moved install.
    NotNeeded,
    Moved,
    /// The move did not run this launch and nothing was changed; the service
    /// starts on the old folder and the next launch tries again.
    Skipped(String),
}

/// Run the move if this install needs one. `progress` gets one percentage for
/// the whole move. An `Err` is a state the app must not start on (the data in
/// two places); everything else either moves or leaves the install as it was.
pub fn run(paths: &Paths, service: &mut dyn Service, progress: &mut Progress<'_>) -> Result<Outcome, String> {
    let journal = read_journal(paths)?;
    if journal.is_none() && !service_file_names(paths, &paths.legacy)? {
        return Ok(Outcome::NotNeeded);
    }
    // An unfinished adoption or Start fresh recovers first; the move waits for a later launch.
    if adoption::interrupted_plan(&paths.app_dir)?.is_some() || paths.app_dir.join("start-fresh.json").exists() {
        log::info!("data folder move: waiting for an unfinished adoption or Start fresh to finish first");
        return Ok(Outcome::NotNeeded);
    }
    match journal.map(|journal| journal.step) {
        Some(Step::Moved) => {
            progress(1);
            finish(paths, progress)
        }
        Some(_) if !exists(&paths.legacy) && holds_data(&paths.target) => {
            // Renamed, but stopped before the journal said so: go on from the commit.
            progress(1);
            write_journal(paths, Step::Moved)?;
            finish(paths, progress)
        }
        Some(_) if exists(&paths.legacy) && holds_data(&paths.target) => Err(two_places(paths)),
        _ => move_now(paths, service, progress),
    }
}

fn move_now(paths: &Paths, service: &mut dyn Service, progress: &mut Progress<'_>) -> Result<Outcome, String> {
    if let Err(reason) = preflight(paths) {
        return skip(paths, reason);
    }
    // Held to the end: an update can't swap or restart the service mid-move,
    // and flock follows the folder through the rename.
    let _lock = match app_paths::try_lock_update_in_existing(&paths.legacy) {
        Ok(Some(lock)) => lock,
        Ok(None) => return skip(paths, "an update is running".into()),
        Err(error) => return skip(paths, format!("could not take the update lock: {error}")),
    };
    match service.operation_active() {
        Ok(false) => {}
        Ok(true) => return skip(paths, "an update or restart is in progress".into()),
        Err(error) => return skip(paths, format!("could not read whether an update is running: {error}")),
    }
    // The move window, and its line that background jobs stop, appear only once the stop is next.
    progress(1);
    write_journal(paths, Step::Stopping)?;
    checkpoint("stopping");
    progress(2);
    if let Err(error) = timed(progress, 2, 70, DRAIN_CAP, || service.stop()) {
        return skip(paths, format!("could not stop the service: {error}"));
    }
    if let Err(error) = service.unregister() {
        return skip(paths, format!("could not unregister the service: {error}"));
    }
    if let Err(error) = service.stop_recorded_processes(&paths.legacy) {
        log::warn!("data folder move: recorded processes: {error}");
    }
    if let Err(error) = timed(progress, 70, 85, RELEASE_CAP, || service.prove_released(&paths.legacy)) {
        return skip(paths, format!("the service did not let go of its data: {error}"));
    }
    let holders = match service.holders(&paths.legacy) {
        Ok(holders) => holders,
        Err(error) => return skip(paths, error),
    };
    if !holders.is_empty() {
        let named: Vec<String> = holders.iter().map(|(pid, name)| format!("{name} (pid {pid})")).collect();
        log::warn!("data folder move: {} process(es) hold the old folder: {}", named.len(), named.join(", "));
        return skip(paths, format!("{} still has CORSIA’s old data folder open", named.join(", ")));
    }
    write_journal(paths, Step::Stopped)?;
    checkpoint("stopped");
    progress(88);
    if let Err(error) = rename(paths) {
        return skip(paths, error);
    }
    checkpoint("renamed");
    if let Err(error) = write_journal(paths, Step::Moved) {
        // Before the commit a failure puts the folder back where it was.
        return match rename_excl(&paths.target, &paths.legacy) {
            Ok(()) => skip(paths, format!("could not record the move, so it was undone: {error}")),
            Err(undo) => Err(format!("CORSIA moved its data but could not record it ({error}) or undo it ({undo})")),
        };
    }
    finish(paths, progress)
}

/// Steps 5 to 8. Each is idempotent, so a relaunch repeats them safely.
fn finish(paths: &Paths, progress: &mut Progress<'_>) -> Result<Outcome, String> {
    checkpoint("moved");
    rebase_json(&paths.app_dir.join(SERVICE_FILE), &paths.legacy, &paths.target)?;
    if !service_file_names(paths, &paths.target)? {
        return Err("CORSIA moved its data but could not point service.json at the new folder".into());
    }
    checkpoint("committed");
    progress(92);
    rebase_json(&paths.bootstrap_config, &paths.legacy, &paths.target)?;
    let adopted = paths.app_dir.join("adopted");
    rebase_text(&adopted.join("com.corsia.prod.plist"), &paths.legacy, &paths.target)?;
    rebase_text(&adopted.join("service.env"), &paths.legacy, &paths.target)?;
    rebase_json(&adopted.join("adoption.json"), &paths.legacy, &paths.target)?;
    checkpoint("repointed");
    tidy(paths);
    checkpoint("tidied");
    clear_journal(paths)?;
    progress(95);
    log::info!("data folder move: CORSIA's data is now in {}", paths.target.display());
    Ok(Outcome::Moved)
}

/// After the moved service is ready: the old folder must stay gone. Something
/// that recreated it would split the data, so it is reported, by count only.
pub fn check_legacy_gone(paths: &Paths) -> Result<(), String> {
    let Ok(entries) = fs::read_dir(&paths.legacy) else {
        return Ok(());
    };
    let count = entries.count();
    log::error!("data folder move: the old data folder reappeared with {count} entries");
    Err(format!(
        "CORSIA moved its data to its new folder, but something recreated the old one ({count} items). \
         Quit any other copy of CORSIA, then reopen it."
    ))
}

fn skip(paths: &Paths, reason: String) -> Result<Outcome, String> {
    log::warn!("data folder move skipped this launch: {reason}");
    clear_journal(paths)?;
    Ok(Outcome::Skipped(reason))
}

fn two_places(paths: &Paths) -> String {
    log::error!(
        "data folder move: data in both {} and {}",
        paths.legacy.display(),
        paths.target.display()
    );
    format!(
        "CORSIA found its data in two places, so it changed neither: {} and {}",
        paths.legacy.display(),
        paths.target.display()
    )
}

fn preflight(paths: &Paths) -> Result<(), String> {
    let parent = fs::symlink_metadata(paths.legacy_parent()).map_err(|error| format!("could not read the old folder: {error}"))?;
    let legacy = fs::symlink_metadata(&paths.legacy).map_err(|error| format!("could not read the old folder: {error}"))?;
    if parent.file_type().is_symlink() || legacy.file_type().is_symlink() {
        return Err("the old data folder is a link, so CORSIA's data stays where it is".into());
    }
    if !paths.legacy.join("corsia.db").is_file() {
        return Err("the old data folder holds no database".into());
    }
    let app = fs::metadata(&paths.app_dir).map_err(|error| format!("could not read the app folder: {error}"))?;
    if app.dev() != legacy.dev() || parent.dev() != legacy.dev() {
        return Err("the old data folder is on another disk, so CORSIA's data stays where it is".into());
    }
    match fs::read_dir(&paths.target) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("could not read {}: {error}", paths.target.display())),
        Ok(mut entries) => {
            if entries.next().is_none() {
                return Ok(());
            }
            let listing = |path: &Path| fs::read_dir(path).map(|entries| {
                entries.filter_map(Result::ok).map(|entry| entry.file_name().to_string_lossy().into_owned()).collect::<Vec<_>>()
            });
            log::warn!(
                "data folder move: target already holds {:?}; old folder holds {:?}",
                listing(&paths.target).unwrap_or_default(),
                listing(&paths.legacy).unwrap_or_default()
            );
            Err(format!("{} already holds files", paths.target.display()))
        }
    }
}

fn rename(paths: &Paths) -> Result<(), String> {
    // An empty target goes first; rmdir fails if anything appeared in it.
    match fs::remove_dir(&paths.target) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(format!("could not clear the empty {}: {error}", paths.target.display())),
    }
    rename_excl(&paths.legacy, &paths.target)
}

/// One atomic rename that never replaces anything at `to`.
fn rename_excl(from: &Path, to: &Path) -> Result<(), String> {
    let c = |path: &Path| CString::new(path.as_os_str().as_bytes()).map_err(|_| format!("{} has a NUL byte", path.display()));
    let (source, destination) = (c(from)?, c(to)?);
    // SAFETY: both are NUL-terminated paths that outlive the call.
    if unsafe { libc::renamex_np(source.as_ptr(), destination.as_ptr(), libc::RENAME_EXCL) } != 0 {
        return Err(format!("could not move {} to {}: {}", from.display(), to.display(), io::Error::last_os_error()));
    }
    for folder in [from.parent(), to.parent()].into_iter().flatten() {
        fs::File::open(folder).and_then(|folder| folder.sync_all()).map_err(|error| error.to_string())?;
    }
    Ok(())
}

/// Step 7: what the person kept beside the old state folder is kept, under
/// `CORSIA/earlier-app-folder`; the old cache goes in the background.
fn tidy(paths: &Paths) {
    let parent = paths.legacy_parent();
    if let Ok(entries) = fs::read_dir(parent) {
        let names: Vec<_> = entries.filter_map(Result::ok).map(|entry| entry.file_name()).collect();
        let kept: Vec<_> = names.iter().filter(|name| name.as_bytes() != b".DS_Store").collect();
        if kept.is_empty() {
            let _ = fs::remove_file(parent.join(".DS_Store"));
        } else {
            let earlier = paths.app_dir.join(EARLIER_FOLDER);
            let _ = fs::create_dir_all(&earlier);
            for name in &kept {
                if let Err(error) = rename_excl(&parent.join(name), &earlier.join(name)) {
                    log::warn!("data folder move: kept {} in place: {error}", name.to_string_lossy());
                }
            }
            log::info!(
                "data folder move: kept {:?} in {}",
                kept.iter().map(|name| name.to_string_lossy()).collect::<Vec<_>>(),
                earlier.display()
            );
        }
        if let Err(error) = fs::remove_dir(parent) {
            log::warn!("data folder move: left {}: {error}", parent.display());
        }
    }
    let cache = paths.old_cache.clone();
    if exists(&cache) {
        thread::spawn(move || remove_cache(&cache));
    }
}

fn remove_cache(cache: &Path) {
    // Bundled runtimes are read-and-execute only, so each folder is made writable first.
    fn writable(path: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let Ok(metadata) = fs::symlink_metadata(path) else { return };
        if metadata.is_dir() {
            let _ = fs::set_permissions(path, fs::Permissions::from_mode(metadata.mode() | 0o700));
            for entry in fs::read_dir(path).into_iter().flatten().flatten() {
                writable(&entry.path());
            }
        }
    }
    writable(cache);
    if let Err(error) = fs::remove_dir_all(cache) {
        log::warn!("data folder move: could not delete the old cache {}: {error}", cache.display());
    }
}

fn service_file_names(paths: &Paths, state: &Path) -> Result<bool, String> {
    let recorded = match fs::read(paths.app_dir.join(SERVICE_FILE)) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(format!("could not read service.json: {error}")),
    };
    let value: serde_json::Value = serde_json::from_slice(&recorded).unwrap_or_default();
    let Some(recorded) = value.get("state_dir").and_then(serde_json::Value::as_str) else {
        return Ok(false);
    };
    let canonical = |path: &Path| fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    Ok(canonical(Path::new(recorded)) == canonical(state))
}

fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

fn holds_data(state: &Path) -> bool {
    state.join("corsia.db").is_file()
}

fn read_journal(paths: &Paths) -> Result<Option<Journal>, String> {
    let raw = match fs::read(paths.journal()) {
        Ok(raw) => raw,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("could not read the data folder move journal: {error}")),
    };
    let journal: Journal =
        serde_json::from_slice(&raw).map_err(|error| format!("the data folder move journal is unreadable: {error}"))?;
    if journal.from != paths.legacy || journal.to != paths.target {
        return Err("the data folder move journal names other folders".into());
    }
    Ok(Some(journal))
}

fn write_journal(paths: &Paths, step: Step) -> Result<(), String> {
    let journal = Journal { from: paths.legacy.clone(), to: paths.target.clone(), step };
    let bytes = serde_json::to_vec(&journal).map_err(|error| error.to_string())?;
    adoption::write_atomically(&paths.journal(), &bytes)
}

fn clear_journal(paths: &Paths) -> Result<(), String> {
    match fs::remove_file(paths.journal()) {
        Ok(()) => fs::File::open(&paths.app_dir)
            .and_then(|folder| folder.sync_all())
            .map_err(|error| error.to_string()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("could not clear the data folder move journal: {error}")),
    }
}

/// `value` with `old` replaced by `new` where it is the whole path or a prefix
/// that ends at a separator; any other text is left alone.
fn rebased(value: &str, old: &str, new: &str) -> String {
    let mut out = String::with_capacity(value.len());
    let mut rest = value;
    while let Some(at) = rest.find(old) {
        let after = &rest[at + old.len()..];
        let ends = after.chars().next().is_none_or(|next| matches!(next, '/' | '<' | '"' | '\'' | '\n' | '\r' | ' ' | '\t'));
        out.push_str(&rest[..at]);
        out.push_str(if ends { new } else { old });
        rest = after;
    }
    out.push_str(rest);
    out
}

fn rebase_value(value: &mut serde_json::Value, old: &str, new: &str) {
    match value {
        serde_json::Value::String(text) => *text = rebased(text, old, new),
        serde_json::Value::Array(items) => items.iter_mut().for_each(|item| rebase_value(item, old, new)),
        serde_json::Value::Object(map) => map.values_mut().for_each(|item| rebase_value(item, old, new)),
        _ => {}
    }
}

fn path_str(path: &Path) -> Result<&str, String> {
    path.to_str().ok_or_else(|| format!("{} is not UTF-8", path.display()))
}

fn rebase_json(file: &Path, old: &Path, new: &Path) -> Result<(), String> {
    let Some(text) = read_if_present(file)? else { return Ok(()) };
    let (old, new) = (path_str(old)?, path_str(new)?);
    if !text.contains(old) {
        return Ok(());
    }
    let mut value: serde_json::Value =
        serde_json::from_str(&text).map_err(|error| format!("{} is unreadable: {error}", file.display()))?;
    rebase_value(&mut value, old, new);
    let bytes = serde_json::to_vec_pretty(&value).map_err(|error| error.to_string())?;
    adoption::write_atomically(file, &bytes)
}

fn rebase_text(file: &Path, old: &Path, new: &Path) -> Result<(), String> {
    let Some(text) = read_if_present(file)? else { return Ok(()) };
    let healed = rebased(&text, path_str(old)?, path_str(new)?);
    if healed == text {
        return Ok(());
    }
    adoption::write_atomically(file, healed.as_bytes())
}

fn read_if_present(file: &Path) -> Result<Option<String>, String> {
    match fs::read_to_string(file) {
        Ok(text) => Ok(Some(text)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("could not read {}: {error}", file.display())),
    }
}

/// Run a bounded step on this thread, advancing `progress` from `from` toward
/// `to` by elapsed time over its cap; it jumps to `to` once the step finishes.
fn timed<T>(progress: &mut Progress<'_>, from: u8, to: u8, cap: Duration, work: impl FnOnce() -> T) -> T {
    let started = Instant::now();
    let (done, finished) = std::sync::mpsc::channel::<()>();
    let result = thread::scope(|scope| {
        let tick = &mut *progress;
        let ticker = scope.spawn(move || {
            while finished.recv_timeout(Duration::from_millis(250)).is_err() {
                let share = started.elapsed().as_secs_f64() / cap.as_secs_f64();
                tick(from + ((f64::from(to - from) * share.min(1.0)) as u8).min(to - from - 1));
            }
        });
        let result = work();
        let _ = done.send(());
        let _ = ticker.join();
        result
    });
    progress(to);
    result
}

/// Every other process with a file or its working folder under `root`. A
/// process list the system refuses is an error, never an empty list: an
/// unseen holder would keep writing to a folder that has moved.
pub fn holders(root: &Path) -> Result<Vec<(i32, String)>, String> {
    let root = fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let me = std::process::id() as i32;
    // SAFETY: a null buffer asks only for the count.
    let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    if count <= 0 {
        return Err(format!(
            "could not check which programs have CORSIA’s data open: {}",
            io::Error::last_os_error()
        ));
    }
    let mut pids = vec![0i32; count as usize + 64];
    // SAFETY: the buffer is as large as the size passed.
    let listed = unsafe {
        libc::proc_listallpids(pids.as_mut_ptr().cast(), (pids.len() * std::mem::size_of::<i32>()) as libc::c_int)
    };
    pids.truncate(listed.max(0) as usize);
    Ok(pids
        .into_iter()
        .filter(|pid| *pid > 0 && *pid != me && holds(*pid, &root))
        .map(|pid| (pid, process_name(pid)))
        .collect())
}

/// `struct vnode_fdinfowithpath`: a `proc_fileinfo` (24 bytes) then the path.
#[repr(C)]
struct VnodeFdInfoWithPath {
    _file: [u64; 3],
    path: libc::vnode_info_path,
}
const PROC_PIDFDVNODEPATHINFO: libc::c_int = 2;

fn holds(pid: i32, root: &Path) -> bool {
    let under = |raw: &[[libc::c_char; 32]; 32]| {
        // SAFETY: the kernel fills a NUL-terminated path into this fixed buffer.
        let bytes: &[u8] = unsafe { std::slice::from_raw_parts(raw.as_ptr().cast(), 32 * 32) };
        let end = bytes.iter().position(|byte| *byte == 0).unwrap_or(bytes.len());
        Path::new(std::ffi::OsStr::from_bytes(&bytes[..end])).starts_with(root)
    };
    // SAFETY: each call writes at most the size it is given into zeroed structs it owns.
    unsafe {
        let mut cwd: libc::proc_vnodepathinfo = std::mem::zeroed();
        let size = std::mem::size_of::<libc::proc_vnodepathinfo>() as libc::c_int;
        if libc::proc_pidinfo(pid, libc::PROC_PIDVNODEPATHINFO, 0, (&raw mut cwd).cast(), size) == size
            && under(&cwd.pvi_cdir.vip_path)
        {
            return true;
        }
        let bytes = libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, std::ptr::null_mut(), 0);
        if bytes <= 0 {
            return false;
        }
        let entry = std::mem::size_of::<libc::proc_fdinfo>();
        let mut fds: Vec<libc::proc_fdinfo> = vec![std::mem::zeroed(); bytes as usize / entry + 16];
        let got = libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, fds.as_mut_ptr().cast(), (fds.len() * entry) as libc::c_int);
        fds.truncate(got.max(0) as usize / entry);
        fds.iter().filter(|fd| fd.proc_fdtype == libc::PROX_FDTYPE_VNODE as u32).any(|fd| {
            let mut info: VnodeFdInfoWithPath = std::mem::zeroed();
            let size = std::mem::size_of::<VnodeFdInfoWithPath>() as libc::c_int;
            libc::proc_pidfdinfo(pid, fd.proc_fd, PROC_PIDFDVNODEPATHINFO, (&raw mut info).cast(), size) > 0
                && under(&info.path.vip_path)
        })
    }
}

fn process_name(pid: i32) -> String {
    let mut name = [0u8; 256];
    // SAFETY: the buffer is as large as the size passed.
    let length = unsafe { libc::proc_name(pid, name.as_mut_ptr().cast(), name.len() as u32) };
    if length <= 0 {
        return "a process".into();
    }
    String::from_utf8_lossy(&name[..length as usize]).into_owned()
}

/// Test hook: a test process that names this point in `CORSIA_STATE_MOVE_KILL_AT`
/// is killed here, the way Force Quit or a power cut would stop it.
fn checkpoint(_point: &str) {
    #[cfg(test)]
    if std::env::var("CORSIA_STATE_MOVE_KILL_AT").is_ok_and(|at| at == _point) {
        // SAFETY: the process signals itself.
        unsafe { libc::kill(libc::getpid(), libc::SIGKILL) };
        // Delivery to a process with other threads is not immediate; never run the next step.
        loop {
            thread::park();
        }
    }
}

/// The installed app's service, through launchd and the bundled CLI.
pub struct AppService {
    pub resources: PathBuf,
    pub port: u16,
    pid: Option<u32>,
    /// Why the move can't check or stop the service; it skips before stopping anything.
    unknown: Option<String>,
}

impl AppService {
    pub fn new(resources: Result<PathBuf, String>, port: Option<u16>) -> Self {
        let unknown = match (&resources, port) {
            (Err(error), _) => Some(format!("could not find the app's bundled files: {error}")),
            (Ok(_), None) => Some("service.json records no port for the service".to_string()),
            _ => None,
        };
        Self { resources: resources.unwrap_or_default(), port: port.unwrap_or_default(), pid: None, unknown }
    }

    fn python(&self) -> PathBuf {
        self.resources.join("python/bin/python3")
    }
}

impl Service for AppService {
    fn operation_active(&mut self) -> Result<bool, String> {
        if let Some(reason) = &self.unknown {
            return Err(reason.clone());
        }
        if !crate::probe_health(self.port) {
            return Ok(false);
        }
        crate::operation_is_active(self.port)
    }

    fn stop(&mut self) -> Result<(), String> {
        let target = format!("gui/{}/{}", unsafe { libc::geteuid() }, crate::service_management::service_label());
        self.pid = release_proof::launchd_pid(&target);
        // Stopping managed processes ends an agent's running job; the progress copy says so.
        let output = Command::new(self.resources.join("scripts/corsia"))
            .args(["stop", "--stop-local-model"])
            .output()
            .map_err(|error| format!("could not run the stop: {error}"))?;
        log::info!("data folder move: stop: {}", String::from_utf8_lossy(&output.stdout).trim());
        if output.status.success() {
            Ok(())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).trim().lines().last().unwrap_or("the stop failed").to_string())
        }
    }

    fn unregister(&mut self) -> Result<(), String> {
        crate::unregister_service()
    }

    fn stop_recorded_processes(&mut self, state: &Path) -> Result<(), String> {
        let output = Command::new(self.python())
            .args(["-I", "-B"])
            .arg(self.resources.join("backend/automation/stop_recorded_processes.py"))
            .arg(state)
            .output()
            .map_err(|error| format!("could not run the process stop: {error}"))?;
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            log::info!("data folder move: recorded process {line}");
        }
        if output.status.success() {
            Ok(())
        } else {
            Err("a recorded process could not be proven stopped".into())
        }
    }

    fn prove_released(&mut self, state: &Path) -> Result<(), String> {
        release_proof::prove_released(self.pid, self.port, &self.python(), &state.join("corsia.db"))
    }
}

#[cfg(test)]
#[path = "state_move_tests.rs"]
mod tests;
