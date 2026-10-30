// CORSIA desktop shell.
//
// The bundle owns one local backend for the lifetime of its window. The
// static shell shows startup progress until the bundled service passes its
// loopback health check, then the WebView navigates to the local CORSIA UI.

use std::{
    env,
    ffi::OsString,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex, PoisonError,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use serde::{Deserialize, Serialize};
use service_management::service_label;
use source_lifecycle::{Bundled, SourceLifecycle};
use tauri::{ipc::CapabilityBuilder, Manager};

mod adoption;
mod app_paths;
mod cli_link;
#[cfg(all(test, unix))]
mod cli_link_tests;
mod classify;
mod discovery;
mod first_run;
mod lifecycle;
mod pairing;
mod proxy;
mod release_proof;
mod remote_config;
mod remote_host;
mod routes;
mod remote_lifecycle;
mod service_management;
mod shell_state;
mod source_lifecycle;
mod start_fresh;
mod startup_failure;
mod state_move;
#[cfg(test)]
mod test_tls;
use startup_failure::StartupFailure;
mod trust;
mod unfinished_setup;
mod version_check;
mod window_placement;
mod update_recovery;
mod update_stage;
mod update_swap;

const DEFAULT_SERVICE_PORT: u16 = 47_895;
const ACTIVE_OPERATION_ERROR: &str = "CORSIA is still completing an operation";
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);
/// The fixed cap for a service that publishes no startup progress (an existing
/// source install). A 20 GiB rehearsal root reached health in 71–74 s and the
/// October 20 cold start took about 99 s; six times the largest observed start
/// leaves room for a slow migration without concealing a hang indefinitely.
pub(crate) const STARTUP_CAP: Duration = Duration::from_secs(600);
/// The app's own service publishes startup progress, so it is held to the
/// update shell's rule (`scripts/corsia.d/runtime.sh`): a minute without
/// visible progress fails, and any start ends at the larger of 15 minutes and
/// a minute per GiB of database.
const STARTUP_STALL: Duration = Duration::from_secs(60);
const STARTUP_CEILING_FLOOR: Duration = Duration::from_secs(900);
const STARTUP_SECONDS_PER_GIB: u64 = 60;
/// How long launchd may show no process for a just-started service.
pub(crate) const NOT_RUNNING_WINDOW: Duration = Duration::from_secs(30);
const RELAUNCH_POLL_INTERVAL: Duration = Duration::from_secs(2);

#[derive(Default)]
struct AppServiceState {
    app_dir: Mutex<Option<PathBuf>>,
    /// The local service ports already granted their commands; a capability can't be removed, so each is added once.
    granted_local_ports: Mutex<std::collections::BTreeSet<u16>>,
    /// Set once this process has had its one chance to wipe an unfinished setup. A retry in the same process (after
    /// an approval prompt or a slow start) never wipes, so it can't delete what this process just started; what a
    /// retry may still do, planning a first run in the folder a finished wipe left, is read from disk.
    setup_checked: AtomicBool,
    quitting: AtomicBool,
    exit_cleanup_attempted: AtomicBool,
    adoption_in_progress: AtomicBool,
    adoption_cancel_requested: AtomicBool,
    /// What the adoption in progress is doing now, for the window to show.
    adoption_phase: Mutex<Option<adoption::Phase>>,
    /// The update screen the replaced app handed over when it reopened this one (`--update-screen`), carried once to
    /// the first Ready address this window follows. Presentation only: it starts, skips or grants nothing.
    relaunched_screen: Mutex<Option<String>>,
    /// A data folder move the launch start skipped because an update held the folder; it runs once that update is over.
    move_after_update: state_move::AfterUpdate,
}

#[derive(Default)]
struct RemoteRuntime {
    browser: Mutex<Option<discovery::Browser>>,
    shell: Mutex<shell_state::ShellState>,
    proxy: Mutex<Option<proxy::ProxyHandle>>,
}

/// The argument the replaced app reopens this one with, carrying its update screen's URL fragment.
const UPDATE_SCREEN_ARGUMENT: &str = "--update-screen=";
/// The update screen's fragment key and the longest fragment any page reads (`shellHandoff.ts`).
const UPDATE_SCREEN_KEY: &str = "corsia-update=";
const UPDATE_SCREEN_MAX_CHARS: usize = 2_048;

/// An update screen fragment as `encodeURIComponent` writes it, or nothing: percent escapes and the characters it leaves
/// unescaped. None of them can start a new URL part or an argument, and it only ever reaches a page as a JSON string or
/// a URL fragment; the page that reads it checks every field again.
fn update_screen_fragment(value: &str) -> Option<&str> {
    let encoded = value.strip_prefix(UPDATE_SCREEN_KEY)?;
    if value.len() > UPDATE_SCREEN_MAX_CHARS || encoded.is_empty() {
        return None;
    }
    let bytes = encoded.as_bytes();
    let mut at = 0;
    while at < bytes.len() {
        match bytes[at] {
            b'%' if bytes.get(at + 1..at + 3).is_some_and(|hex| hex.iter().all(u8::is_ascii_hexdigit)) => at += 3,
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'!' | b'*' | b'\'' | b'(' | b')' => at += 1,
            _ => return None,
        }
    }
    Some(value)
}

/// The update screen handed over on the command line: exactly one well-formed `--update-screen=` argument, or nothing.
fn relaunched_update_screen(args: &[OsString]) -> Option<String> {
    let mut screens = args.iter().filter_map(|arg| arg.to_str()?.strip_prefix(UPDATE_SCREEN_ARGUMENT));
    let screen = screens.next()?;
    if screens.next().is_some() {
        return None;
    }
    update_screen_fragment(screen).map(str::to_owned)
}

/// `open`'s arguments to reopen the replacement: the update screen goes with it when this window shows one, on the
/// shell's update page or on this computer's own service page (`local_origin`), which keeps its latest screen in its
/// address. Presentation only: the reopened app starts as it always does.
fn relaunch_arguments(bundle: &Path, window_url: Option<&str>, local_origin: &str) -> Vec<OsString> {
    let mut arguments = vec![OsString::from("-n"), bundle.as_os_str().to_owned()];
    let screen = window_url
        .and_then(|url| tauri::Url::parse(url).ok())
        .filter(|url| is_shell_update_page(url.as_str()) || url.origin().ascii_serialization() == local_origin)
        .and_then(|url| url.fragment().and_then(update_screen_fragment).map(str::to_owned));
    if let Some(screen) = screen {
        arguments.push("--args".into());
        arguments.push(format!("{UPDATE_SCREEN_ARGUMENT}{screen}").into());
    }
    arguments
}

/// The page a reopened app's window opens on: with a handed-over screen, the shell's update page drawing it while the
/// launch start runs exactly as it always does; with none, nothing changes.
fn relaunched_start_page(screen: Option<&str>) -> Option<tauri::Url> {
    tauri::Url::parse(&format!("tauri://localhost/update.html#{}", screen?)).ok()
}

/// The first Ready address this window follows carries the screen the replaced app handed over, once.
fn with_relaunched_screen<E>(slot: &Mutex<Option<String>>, answer: Result<ServiceStartup, E>) -> Result<ServiceStartup, E> {
    match answer {
        Ok(ServiceStartup::Ready { url }) => match slot.lock().unwrap_or_else(PoisonError::into_inner).take() {
            Some(screen) => Ok(ServiceStartup::Ready { url: format!("{}/#{screen}", url.trim_end_matches('/')) }),
            None => Ok(ServiceStartup::Ready { url }),
        },
        other => other,
    }
}

/// The app binary's command-line modes; anything else opens the window.
#[derive(Debug, PartialEq, Eq)]
enum CommandLineMode {
    Service,
    UpdateStage(String),
    UpdateSwap(String),
    UpdateRollback(String),
}

fn operation_argument(args: &[OsString], flag: &str) -> Result<Option<String>, String> {
    if args.len() == 2 && args[0] == flag && !args[1].is_empty() {
        return Ok(Some(args[1].to_string_lossy().into_owned()));
    }
    if args.iter().any(|arg| arg == flag) {
        return Err(format!(
            "{flag} requires exactly one operation ID and no other mode flags"
        ));
    }
    Ok(None)
}

fn command_line_mode(args: &[OsString]) -> Result<Option<CommandLineMode>, String> {
    if args.first().is_some_and(|arg| arg == "--service") {
        return Ok(Some(CommandLineMode::Service));
    }
    let stage = operation_argument(args, "--update-stage")?;
    let swap = operation_argument(args, "--update-swap")?;
    let rollback = operation_argument(args, "--update-rollback")?;
    Ok(match (stage, swap, rollback) {
        (Some(operation_id), None, None) => Some(CommandLineMode::UpdateStage(operation_id)),
        (None, Some(operation_id), None) => Some(CommandLineMode::UpdateSwap(operation_id)),
        (None, None, Some(operation_id)) => Some(CommandLineMode::UpdateRollback(operation_id)),
        _ => None,
    })
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
enum ServiceStartup {
    Ready { url: String },
    RequiresApproval,
    DiskImage,
    /// An existing install this app can adopt: the window asks the person first.
    AdoptionOffered { workspace: String },
    /// An earlier adoption's undo could not finish; the window offers the recovery the backend chose.
    AdoptionFailed { failure: adoption::AdoptionError },
    /// An earlier install this app can't open: the window offers to set it aside and start fresh.
    ExistingInstall { reason: String, details: String },
    /// Putting back a Start fresh the app did not finish went wrong; the window says what and offers the next step.
    StartFreshFailed { failure: start_fresh::StartFreshError },
}

/// Drag-region handler injected into every page (including external URLs
/// loaded into the WebView). Tauri 2's data-tauri-drag-region attribute
/// is only honored on pages served by the Tauri runtime (tauri:// origin),
/// and -webkit-app-region: drag CSS doesn't reliably work in WKWebView.
/// This script runs natively in the WebView and uses the macOS-only
/// WebKit `webkit.messageHandlers` channel to invoke the Tauri command
/// that calls `start_dragging` on the window.
const DRAG_INIT_SCRIPT: &str = r#"
(function () {
  if (window.__corsia_drag_handler_installed) return;
  window.__corsia_drag_handler_installed = true;
  window.__CORSIA_SHELL__ = window.__CORSIA_SHELL__ || { mode: 'local' };

  // Mark the document so CSS can apply Tauri-shell-only treatments
  // (e.g. left-pad chrome to clear macOS traffic lights). Apply on
  // documentElement (set early — fires before page styles). Re-apply
  // after DOMContentLoaded too in case the page re-mounts the html
  // node late.
  document.documentElement.dataset.shell = 'tauri';
  document.addEventListener('DOMContentLoaded', () => {
    document.documentElement.dataset.shell = 'tauri';
  });

  // Drag handler — clicks on data-tauri-drag-region trigger the Rust
  // command that calls window.start_dragging(). Skips clicks that
  // originate inside an interactive element.
  document.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    let el = e.target;
    while (el && el !== document) {
      if (el.hasAttribute && el.hasAttribute('data-tauri-drag-region')) {
        const t = e.target;
        if (t.closest && t.closest('button, input, select, textarea, a, [contenteditable="true"]')) return;
        try {
          window.__TAURI_INTERNALS__.invoke('start_window_drag');
        } catch (err) { console.warn('start_window_drag failed', err); }
        return;
      }
      el = el.parentNode;
    }
  }, true);
})();
"#;

/// Setup is over: a window the person left at its default size grows to fill the screen's usable area.
#[tauri::command]
fn finish_setup_window(window: tauri::WebviewWindow) {
    window_placement::expand_main_window_after_setup(&window);
}

#[tauri::command]
fn start_window_drag(window: tauri::Window) -> Result<(), String> {
    window.start_dragging().map_err(|e| e.to_string())
}

fn default_app_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    app.path()
        .home_dir()
        .map(|home| app_paths::app_dir_for_home(&home))
        .map_err(|error| format!("could not resolve the CORSIA app folder: {error}"))
}

/// Whether this desktop app has already configured its own local CORSIA. The existing first-run plan owns this
/// distinction; remote surfaces consume it only to choose their return-action label.
fn local_corsia_configured<R: tauri::Runtime>(app: &tauri::AppHandle<R>, app_dir: &Path) -> bool {
    if first_run::environment_names_the_roots() {
        return true;
    }
    let Ok(home) = app.path().home_dir() else {
        log::warn!("could not resolve the home folder while checking local CORSIA setup");
        return false;
    };
    local_plan_configured(&home, app_dir)
}

fn local_plan_configured(home: &Path, app_dir: &Path) -> bool {
    if matches!(unfinished_setup::at_launch(app_dir), unfinished_setup::Launch::Wipe(_) | unfinished_setup::Launch::Replan(_)) {
        return false;
    }
    match first_run::plan(home, app_dir) {
        Ok(first_run::FirstRun::Configured) => true,
        Ok(_) => false,
        Err(error) => {
            log::warn!("could not determine whether local CORSIA is configured: {error}");
            false
        }
    }
}

fn service_log_path(app_dir: &Path) -> Result<PathBuf, String> {
    Ok(app_paths::state_dir_for(app_dir)?
        .join("logs")
        .join("service.log"))
}

fn preferred_port(recorded: Option<u16>) -> u16 {
    env::var("CORSIA_DESKTOP_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .filter(|port| *port != 0)
        .or(recorded)
        .unwrap_or(DEFAULT_SERVICE_PORT)
}

fn select_port(recorded: Option<u16>) -> Result<u16, String> {
    let preferred = preferred_port(recorded);
    if TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], preferred))).is_ok() {
        return Ok(preferred);
    }

    TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
        .map_err(|error| format!("could not select a fallback loopback service port: {error}"))?
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| format!("could not determine the fallback service port: {error}"))
}

fn port_for_startup(first_run: &first_run::FirstRun, recorded: Option<u16>) -> Result<u16, String> {
    match first_run {
        first_run::FirstRun::Fresh { .. } => select_port(recorded),
        // A configured service may already be listening. Reconnect to its
        // recorded port rather than allocating an unwritten fallback.
        _ => Ok(recorded.unwrap_or_else(|| preferred_port(None))),
    }
}

/// The one request line the shell sends to its own service. The Host header
/// carries the port: the backend's loopback guard refuses a Host without the
/// port it is bound to.
fn local_request(method: &str, port: u16, path: &str) -> String {
    let body = if method == "POST" { "Content-Type: application/json\r\nContent-Length: 2\r\n" } else { "" };
    let tail = if method == "POST" { "{}" } else { "" };
    format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{body}Connection: close\r\n\r\n{tail}")
}

/// Send one request (a GET, or the quit-time POST) to a service on this computer and read the whole reply.
/// The error names the step that failed, for callers that report it.
fn local_exchange(
    method: &str,
    port: u16,
    path: &str,
    connect_timeout: Duration,
    io_timeout: Duration,
) -> Result<Vec<u8>, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, connect_timeout)
        .map_err(|error| format!("could not connect: {error}"))?;
    stream
        .set_read_timeout(Some(io_timeout))
        .and_then(|_| stream.set_write_timeout(Some(io_timeout)))
        .and_then(|_| stream.write_all(local_request(method, port, path).as_bytes()))
        .map_err(|error| format!("could not send the request: {error}"))?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("could not read the reply: {error}"))?;
    Ok(response)
}

fn local_get(port: u16, path: &str) -> Result<String, String> {
    let unavailable = || "the CORSIA service is unavailable".to_string();
    local_exchange("GET", port, path, Duration::from_millis(250), Duration::from_millis(500))
        .map_err(|_| unavailable())
        .and_then(|response| String::from_utf8(response).map_err(|_| unavailable()))
}

/// Asks the service to stop the local model CORSIA started. Stopping takes up to its SIGTERM grace, so the read waits.
fn stop_local_model(port: u16) -> Result<(), String> {
    let response = local_exchange("POST", port, "/api/system/stop-local-model", Duration::from_millis(250), Duration::from_secs(15))
        .and_then(|bytes| String::from_utf8(bytes).map_err(|error| error.to_string()))?;
    if response.starts_with("HTTP/1.1 200") {
        Ok(())
    } else {
        Err(format!("the service refused to stop the local model: {}", response.lines().next().unwrap_or("")))
    }
}

fn probe_health(port: u16) -> bool {
    local_get(port, "/api/health").is_ok_and(|response| response.starts_with("HTTP/1.1 200"))
}

fn operation_is_active_response(response: &str) -> Result<bool, String> {
    if !response.starts_with("HTTP/1.1 200") {
        return Err("could not read the current CORSIA operation state".into());
    }
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .ok_or("the CORSIA operation-state response was malformed")?;
    match body.trim() {
        "{\"active\":true}" => Ok(true),
        "{\"active\":false}" => Ok(false),
        _ => Err("the CORSIA operation-state response was malformed".into()),
    }
}

fn should_prevent_quit(operation_state: Result<bool, String>) -> bool {
    matches!(operation_state, Ok(true))
}

fn setup_account_exists_response(response: &str) -> Result<bool, String> {
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .ok_or("the CORSIA setup-status response was malformed")?;
    let body = serde_json::from_str::<serde_json::Value>(body)
        .map_err(|_| "the CORSIA setup-status response was malformed".to_string())?;
    let body = body
        .as_object()
        .ok_or("the CORSIA setup-status response was malformed")?;
    if response.starts_with("HTTP/1.1 409")
        && body.get("code").and_then(serde_json::Value::as_str) == Some("setup_complete")
    {
        return Ok(true);
    }
    if response.starts_with("HTTP/1.1 200") {
        return Ok(false);
    }
    Err("could not read whether a CORSIA account already exists".into())
}

fn setup_account_exists(port: u16) -> Result<bool, String> {
    setup_account_exists_response(&local_get(port, "/api/setup/status")?)
}

fn operation_is_active(port: u16) -> Result<bool, String> {
    operation_is_active_response(&local_get(port, "/api/system/operation-active")?)
}

fn running_from_disk_image() -> Result<bool, String> {
    let executable = env::current_exe()
        .map_err(|error| format!("could not resolve CORSIA executable: {error}"))?;
    Ok(service_management::is_transient_bundle_path(&executable))
}

fn service_command_is_allowed(action: &str, from_disk_image: bool) -> Result<(), String> {
    if action == "register" && from_disk_image {
        return Err("Drag CORSIA into your Applications folder, then open it from there.".into());
    }
    Ok(())
}

fn run_service_command(action: &str) -> Result<(), String> {
    // Every registration goes through here, so a copy on a disk image never leaves a service behind it.
    service_command_is_allowed(action, action == "register" && running_from_disk_image()?)?;
    let executable = env::current_exe()
        .map_err(|error| format!("could not resolve CORSIA executable: {error}"))?;
    let status = Command::new(executable)
        .args(["--service", action])
        .status()
        .map_err(|error| format!("could not {action} the CORSIA service: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("CORSIA service {action} exited {status}"))
    }
}

fn unregister_service() -> Result<(), String> {
    run_service_command("unregister")
}

/// An unfinished first-run setup leaves nothing: before anything is planned or started, a marker the first run
/// left (and the backend deletes when setup finishes) makes this launch wipe the state folder and what setup
/// created, so first run begins again at step 1. Without a marker, or with one that cannot be trusted, nothing
/// is touched. Starting over never stops a launch, and it is all or nothing for the old install: a wipe that
/// fails deletes nothing, so the marker is dropped and the install resumes where it stopped (the old behavior);
/// one that gets past its first step has removed the old install, so the launch plans a clean first run.
///
/// Returns the work folder that setup made, so planning the new first run reuses it.
fn start_over_if_unfinished(app_dir: &Path, may_wipe: bool) -> Option<PathBuf> {
    match unfinished_setup::at_launch(app_dir) {
        unfinished_setup::Launch::Nothing => None,
        unfinished_setup::Launch::Replan(workspace) => {
            unfinished_setup::finish_leftovers(app_dir);
            Some(workspace)
        }
        unfinished_setup::Launch::Damaged(reason) => {
            // Nothing can trust it, so it only gets in the way: this install is simply not marked.
            log::error!("a damaged unfinished-setup marker was removed: {reason}");
            drop_marker(app_dir);
            None
        }
        unfinished_setup::Launch::Untrusted(reason) => {
            log::error!("an unfinished-setup marker was left alone: {reason}");
            None
        }
        // This process has already started a service, so what is on disk is not an abandoned setup.
        unfinished_setup::Launch::Wipe(_) if !may_wipe => None,
        unfinished_setup::Launch::Wipe(marker) => {
            log::info!("setup was never finished: starting it over");
            // The service holds the state folder open, so it stops first.
            let wiped = unregister_service().and_then(|()| unfinished_setup::wipe(app_dir, &marker));
            match wiped {
                Ok(()) => Some(PathBuf::from(marker.workspace)),
                Err(error) => {
                    log::error!("could not start setup over, so it resumes where it stopped: {error}");
                    drop_marker(app_dir);
                    None
                }
            }
        }
    }
}

fn drop_marker(app_dir: &Path) {
    if let Err(error) = unfinished_setup::discard_marker(app_dir) {
        log::error!("could not remove the unfinished-setup marker: {error}");
    }
}

fn restart_service() -> Result<(), String> {
    unregister_service()?;
    run_service_command("register")
}

fn service_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

/// The commands the pages the local service serves call: first-run folder choice, the setup report and the window
/// growing when setup finishes, Connect to another computer, and the window-drag handler injected on every page. Never `disconnect_remote` (the remote page's
/// own command), the quit and login-item commands, or `start_local_service`, which runs migrations, recovery and
/// registration: only the startup shell starts the service.
const LOCAL_SERVICE_PERMISSIONS: &[&str] = &[
    "allow-choose-work-folder",
    "allow-configure-work-folder",
    "allow-finish-setup-window",
    "allow-read-startup-progress",
    "allow-remote-discovered",
    "allow-remote-pair",
    "allow-save-setup-report",
    "allow-start-window-drag",
];
const LOCAL_SERVICE_EXTERNAL_URL_SCOPES: &[&str] = &["http://*", "https://*"];

fn local_service_external_url_scopes() -> Vec<serde_json::Value> {
    LOCAL_SERVICE_EXTERNAL_URL_SCOPES
        .iter()
        .map(|url| serde_json::json!({ "url": url }))
        .collect()
}

/// Builds the capability for one local-service origin: its own app commands and only browser-safe external URLs.
fn local_service_capability(port: u16) -> CapabilityBuilder {
    LOCAL_SERVICE_PERMISSIONS
        .iter()
        .fold(
            CapabilityBuilder::new(format!("local-service-origin-{port}")).remote(service_url(port)).window("main"),
            |capability, permission| capability.permission(*permission),
        )
        .permission_scoped(
            "opener:allow-open-url",
            local_service_external_url_scopes(),
            Vec::<serde_json::Value>::new(),
        )
}

/// Lets the page the local service serves at this exact origin call its commands and open safe external links. The app's
/// command ACL refuses every origin it does not name, and the service page is not the startup shell, so without this the
/// first-run folder choice, Connect to another computer screens, and chat sign-in links are refused.
fn grant_local_service_origin<R: tauri::Runtime>(app: &tauri::AppHandle<R>, service: &AppServiceState, port: u16) -> Result<(), String> {
    let mut granted = service.granted_local_ports.lock().unwrap_or_else(PoisonError::into_inner);
    if granted.contains(&port) {
        return Ok(());
    }
    app.add_capability(local_service_capability(port)).map_err(|error| format!("could not grant local service permissions: {error}"))?;
    granted.insert(port);
    Ok(())
}

fn local_service_ready<R: tauri::Runtime>(app: &tauri::AppHandle<R>, service: &AppServiceState, port: u16) -> Result<ServiceStartup, String> {
    grant_local_service_origin(app, service, port)?;
    #[cfg(all(unix, not(test)))]
    {
        let app_dir = service.app_dir.lock().unwrap_or_else(PoisonError::into_inner).clone();
        if let (Ok(home), Ok(resources), Some(app_dir)) = (app.path().home_dir(), bundled_resource_dir(app), app_dir) {
            let retired = adoption::retired_cli(&app_dir).unwrap_or_else(|error| {
                log::warn!("could not inspect adoption record for CLI links: {error}");
                None
            });
            cli_link::repoint_existing(&home, &resources.join("bin/corsia"), cli_link::Repair::Launch(retired.as_deref()));
        }
    }
    Ok(ServiceStartup::Ready { url: service_url(port) })
}

fn bundled_resource_dir<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    match app.path().resource_dir() {
        Ok(path) => Ok(path),
        Err(path_error) => {
            let executable = env::current_exe()
                .map_err(|error| format!("could not resolve the app executable: {error}"))?;
            executable
                .parent()
                .and_then(|macos| macos.parent())
                .map(|contents| contents.join("Resources"))
                .filter(|path| path.is_dir())
                .ok_or_else(|| {
                    format!(
                        "could not resolve bundled resources through Tauri ({path_error}) or from executable {}",
                        executable.display()
                    )
                })
        }
    }
}

fn wait_for_service(port: u16, app_dir: &Path) -> Result<(), String> {
    let log = service_log_path(app_dir)?;
    let state = app_paths::state_dir_for(app_dir)?;
    let progress = state.join("runtime").join("startup-progress.json");
    let database_bytes = std::fs::metadata(state.join("corsia.db")).map_or(0, |metadata| metadata.len());
    let target = format!("gui/{}/{}", unsafe { libc::geteuid() }, service_label());
    wait_for_startup(
        port,
        Some(&log),
        || Ok(release_proof::launchd_pid(&target)),
        NOT_RUNNING_WINDOW,
        startup_ceiling(database_bytes),
        Some(ProgressWatch { file: &progress, stall: STARTUP_STALL }),
    )
    .map_err(|failure| failure.to_string())
}

fn startup_ceiling(database_bytes: u64) -> Duration {
    let gib = database_bytes.div_ceil(1 << 30);
    STARTUP_CEILING_FLOOR.max(Duration::from_secs(gib * STARTUP_SECONDS_PER_GIB))
}

/// The starting service's own progress file
/// (`backend/infra/startup_progress.py`), and how long it may go unchanged.
pub(crate) struct ProgressWatch<'a> {
    pub(crate) file: &'a Path,
    pub(crate) stall: Duration,
}

/// The PID-validated, redacted subset of the service's startup-progress file the UI can use; diagnostic error text and
/// database schema versions deliberately remain in the service file.
#[derive(Deserialize, Serialize, Debug, PartialEq)]
struct StartupProgress {
    schema: u8,
    pid: u32,
    seq: u64,
    phase: String,
    done: Option<u64>,
    total: Option<u64>,
}

fn read_startup_progress_file(file: &Path) -> Option<StartupProgress> {
    serde_json::from_slice(&std::fs::read(file).ok()?).ok()
}

/// A stale file can only describe another service process, never the one the screen follows.
fn current_startup_progress(file: &Path, service_pid: Option<u32>) -> Option<StartupProgress> {
    let pid = service_pid?;
    read_startup_progress_file(file).filter(|reading| reading.schema == 1 && reading.pid == pid)
}

/// Health is the readiness signal. A changed launchd PID proves the service
/// restarted while starting, and a PID the kernel no longer knows with none in
/// its place proves it exited. launchd showing no process for the whole
/// `not_running` window means it exited before it was seen or never started;
/// launchd's 10 s respawn throttle sits well inside the window. With a
/// `progress` watch, a file naming the launchd PID that reports `failed` ends
/// the wait at once, and a `seq` that stays unchanged (or no such file) for
/// `stall` fails it; `cap` bounds every start. `log` is searched only for
/// launcher refusals.
pub(crate) fn wait_for_startup(
    port: u16,
    log: Option<&Path>,
    mut service_pid: impl FnMut() -> Result<Option<u32>, String>,
    not_running: Duration,
    cap: Duration,
    progress: Option<ProgressWatch>,
) -> Result<(), StartupFailure> {
    let began = Instant::now();
    let mut initial_pid = None;
    let mut last_seq = None;
    let mut idle_since = began;
    loop {
        if probe_health(port) {
            return Ok(());
        }
        match (initial_pid, service_pid().map_err(StartupFailure::PidUnreadable)?) {
            (Some(expected), Some(actual)) if actual != expected => {
                return Err(StartupFailure::Restarted);
            }
            (Some(expected), None) if release_proof::pid_is_gone(expected) => {
                return Err(StartupFailure::exited(log));
            }
            (None, None) if began.elapsed() >= not_running => {
                return Err(StartupFailure::exited(log));
            }
            (None, Some(pid)) => initial_pid = Some(pid),
            _ => {}
        }
        if let (Some(watch), Some(pid)) = (&progress, initial_pid) {
            if let Some(reading) = current_startup_progress(watch.file, Some(pid)) {
                // A failed startup exits next; the log may name a refusal.
                if reading.phase == "failed" {
                    return Err(StartupFailure::exited(log));
                }
                if last_seq != Some(reading.seq) {
                    last_seq = Some(reading.seq);
                    idle_since = Instant::now();
                }
            }
        }
        let stalled = progress.as_ref().is_some_and(|watch| idle_since.elapsed() >= watch.stall);
        if stalled || began.elapsed() >= cap {
            return Err(StartupFailure::timed_out(log));
        }
        thread::sleep(HEALTH_POLL_INTERVAL);
    }
}

/// The bundle's generation as the launcher reports it to the service
/// (`CFBundleShortVersionString+CFBundleVersion`).
fn bundle_generation(bundle: &Path) -> Option<String> {
    let info = plist::Value::from_file(bundle.join("Contents").join("Info.plist")).ok()?;
    let info = info.as_dictionary()?;
    let short_version = info.get("CFBundleShortVersionString")?.as_string()?;
    let build = info.get("CFBundleVersion")?.as_string()?;
    Some(format!("{short_version}+{build}"))
}

/// An update swapped a new app into this window's path, and the new app's
/// service answers: the window belongs to the replaced app, so reopen it.
fn should_relaunch(launched: &str, installed: Option<&str>, healthy: impl FnOnce() -> bool) -> bool {
    installed.is_some_and(|installed| installed != launched) && healthy()
}

/// Whether the replacement opened, so this app is exiting.
fn relaunch(app: &tauri::AppHandle, bundle: &Path) -> bool {
    let service = app.state::<AppServiceState>();
    // The service now belongs to the new app: this exit must never unregister it.
    let cleanup_was_pending = !service.exit_cleanup_attempted.swap(true, Ordering::AcqRel);
    let window_url = app.get_webview_window("main").and_then(|window| window.url().ok()).map(|url| url.to_string());
    let local_origin = service_url(lifecycle::service_port(app, &service));
    match Command::new("/usr/bin/open").args(relaunch_arguments(bundle, window_url.as_deref(), &local_origin)).status() {
        Ok(status) if status.success() => {
            service.quitting.store(true, Ordering::Relaxed);
            app.exit(0);
            true
        }
        outcome => {
            log::error!("could not reopen the updated CORSIA app: {outcome:?}");
            if cleanup_was_pending {
                service.exit_cleanup_attempted.store(false, Ordering::Release);
            }
            false
        }
    }
}

/// Watches the installed app under this window. Each reading is also the only source of the update screen's automatic
/// recovery start (`ShellState::observe_install`), so nothing a page sends can arm one.
fn watch_for_swapped_app(app: tauri::AppHandle) {
    let Ok(bundle) = update_stage::running_bundle() else {
        return;
    };
    let Some(launched) = bundle_generation(&bundle) else {
        return;
    };
    thread::spawn(move || loop {
        thread::sleep(RELAUNCH_POLL_INTERVAL);
        let installed = bundle_generation(&bundle);
        let service = app.state::<AppServiceState>();
        let local_session = service.app_dir.lock().unwrap_or_else(PoisonError::into_inner).is_some();
        app.state::<RemoteRuntime>().shell.lock().unwrap_or_else(PoisonError::into_inner).observe_install(
            &bundle,
            &launched,
            installed.as_deref(),
            local_session,
        );
        let port = lifecycle::service_port(&app, &service);
        if should_relaunch(&launched, installed.as_deref(), || probe_health(port)) {
            let _ = relaunch(&app, &bundle);
            return;
        }
        // An install that can't be read changes nothing; the next reading decides.
        if let Some(installed) = installed.as_deref() {
            if service.move_after_update.take_when_over(installed == launched, || update_is_over(&app, &service, port)) {
                move_after_update(app.clone());
            }
        }
    });
}

/// The update is over by the update page's own reading (`update_page_ready_url`): this computer's service, already
/// served to this window, answers healthy with nothing in flight, and nothing holds the update lock.
fn update_is_over(app: &tauri::AppHandle, service: &AppServiceState, port: u16) -> bool {
    let Some(app_dir) = service.app_dir.lock().unwrap_or_else(PoisonError::into_inner).clone() else {
        return false;
    };
    let Ok(state) = app_paths::state_dir_for(&app_dir) else {
        return false;
    };
    let served = service.granted_local_ports.lock().unwrap_or_else(PoisonError::into_inner).contains(&port);
    let local = app.state::<RemoteRuntime>().shell.lock().unwrap_or_else(PoisonError::into_inner).allows_manual_recovery();
    update_page_ready_url(&READINESS_CHECK, served, local, port, probe_health, operation_is_active, || update_lock_is_free(&state))
        .is_some()
}

/// Runs the launch start again for the move an update held, and the service comes back on the moved folder the way a
/// launch brings it up. The window leaves the page the person is on only once the move is about to stop the service
/// (`start_service_in`); a move that skips before that leaves them where they are.
fn move_after_update(app: tauri::AppHandle) {
    thread::spawn(move || {
        log::info!("data folder move: the update is over, so the move runs now");
        local_start_began(&app);
        let service = app.state::<AppServiceState>();
        let outcome = start_service(&app, &service);
        let on_shell = app.get_webview_window("main").and_then(|window| window.url().ok()).is_some_and(|url| is_startup_shell_page(url.as_str()));
        match (&outcome, on_shell) {
            (_, true) => show_launch_outcome(&app, &outcome),
            // A skip left the person on their page: a Ready answer kept as shell state would be replayed by a later
            // shell load, so only a failure is shown, on the shell with its Retry.
            (Err(reason), false) => {
                log::error!("bundled CORSIA service did not start: {reason}");
                show_shell_failure(&app, reason);
            }
            (Ok(_), false) => {}
        }
    });
}

fn register_or_report_status(port: u16) -> Result<service_management::ServiceStatus, String> {
    service_management::ensure_registered_from_app(|| operation_is_active(port))
}

/// Attach to the recorded local service only while it proves an update operation is still active and it is healthy.
/// This path reads the existing record and service only; every unavailable or unproven case continues to the locked start.
fn attach_healthy_active_service<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    service: &AppServiceState,
    app_dir: &Path,
) -> Result<Option<ServiceStartup>, String> {
    let Some(port) = first_run::recorded_port(app_dir) else {
        return Ok(None);
    };
    if operation_is_active(port).ok() != Some(true) || !probe_health(port) {
        return Ok(None);
    }
    *service.app_dir.lock().map_err(|_| "service state is unavailable")? = Some(app_dir.to_path_buf());
    local_service_ready(app, service, port).map(Some)
}

fn start_service<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    service: &AppServiceState,
) -> Result<ServiceStartup, String> {
    if running_from_disk_image()? {
        return Ok(ServiceStartup::DiskImage);
    }
    start_service_in(app, service, default_app_dir(app)?)
}

/// Start from an already-resolved app folder. The public startup wrapper resolves that folder once before this path.
fn start_service_in<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    service: &AppServiceState,
    app_dir: PathBuf,
) -> Result<ServiceStartup, String> {
    let home = app
        .path()
        .home_dir()
        .map_err(|error| format!("could not resolve the home folder: {error}"))?;
    // An adopted install's data leaves the folder named for the old identifier before
    // anything attaches to or starts the service; a skip starts it where it is.
    let move_paths = state_move::Paths::for_home(&home, &app_dir);
    {
        let mut mover =
            state_move::AppService::new(bundled_resource_dir(app), first_run::recorded_port(&app_dir));
        let outcome = state_move::run(&move_paths, &mut mover, &mut |percent| {
            // The move window is the startup shell's; its first reading comes just before the service stops.
            if percent <= 1 {
                return_to_shell(app);
            }
            set_adoption_phase(service, Some(adoption::Phase::MovingData { percent }))
        });
        service.move_after_update.launched(&outcome);
        let outcome = outcome.map_err(|error| {
            log::error!("data folder move: {}", error.detail);
            error.line.to_string()
        });
        // A skip the person watched start says why before CORSIA starts where it was.
        if let Ok(state_move::Outcome::Skipped(state_move::Skip { tell: Some(told), .. })) = &outcome {
            // The line stays long enough to read before CORSIA starts on the old folder.
            set_adoption_phase(service, Some(adoption::Phase::MoveSkipped { told: told.clone() }));
            thread::sleep(told.hold());
        }
        set_adoption_phase(service, None);
        outcome?;
    }
    if let Some(startup) = attach_healthy_active_service(app, service, &app_dir)? {
        return Ok(startup);
    }
    // An update holds its lock before draining until the new app is proven or rolled back, so a start waits
    // for it instead of starting either app in the middle. A state root that can't be resolved refuses the start here,
    // before anything is recovered or written.
    let _update = app_paths::wait_for_update_lock(&app_paths::state_dir_for(&app_dir)?)?;
    // The undo finishes before anything else, then launch goes on as if
    // nothing had been tried: the restored install is offered for adoption again.
    let resources = bundled_resource_dir(app).unwrap_or_default();
    let make_lifecycle =
        |plan: &adoption::Plan| SourceLifecycle::new(plan.source.clone(), Bundled::in_resources(&resources));
    let recovered =
        adoption::recover_on_launch(&app_dir, make_lifecycle, &mut |phase| set_adoption_phase(service, Some(phase)))?;
    set_adoption_phase(service, None);
    if let Some(failure) = recovered {
        log::error!("interrupted adoption could not be undone: {}", failure.message);
        return Ok(ServiceStartup::AdoptionFailed { failure });
    }
    // A Start fresh the app did not finish is put back first, so the earlier install is found whole.
    let recovered = start_fresh::recover_on_launch(&app_dir, |stop| {
        use start_fresh::OldService;
        // A stop the last run left draining is waited for; the window shows the wait.
        if stop.is_some() {
            set_adoption_phase(service, Some(adoption::Phase::Waiting { running: None }));
        }
        let started = earlier_install_service(&home, &app_dir, &resources).0.start(stop);
        set_adoption_phase(service, None);
        started
    });
    if let Err(failure) = recovered {
        log::error!("an unfinished Start fresh could not be put back: {}", failure.details);
        return Ok(ServiceStartup::StartFreshFailed { failure });
    }
    let started_over = if first_run::environment_names_the_roots() {
        None
    } else {
        let may_wipe = !service.setup_checked.swap(true, Ordering::SeqCst);
        start_over_if_unfinished(&app_dir, may_wipe)
    };
    // Decide about the workspace before anything is written: a refusal leaves
    // the computer exactly as it was.
    let first_run = if first_run::environment_names_the_roots() {
        first_run::FirstRun::Configured
    } else {
        first_run::plan_trusting(&home, &app_dir, started_over.as_deref())?
    };
    if let first_run::FirstRun::ExistingInstall { reason, details } = first_run {
        return Ok(ServiceStartup::ExistingInstall { reason, details });
    }
    if let first_run::FirstRun::Adoptable { source } = &first_run {
        return Ok(ServiceStartup::AdoptionOffered {
            workspace: source.workspace.display().to_string(),
        });
    }
    let port = port_for_startup(&first_run, first_run::recorded_port(&app_dir))?;
    if let first_run::FirstRun::Fresh { workspace } = first_run {
        // Marked before anything is written, so a marker that can't be written refuses the launch and leaves the
        // computer as it was. Only a first run that makes the state folder marks it; `false` means it was not new.
        match unfinished_setup::mark(&app_dir, &workspace) {
            Ok(true) => {}
            Ok(false) => log::info!("the state folder was not new, so setup is not marked unfinished"),
            Err(error) => {
                log::error!("could not mark setup as unfinished: {error}");
                return Err("Couldn’t start setup. Quit and reopen CORSIA to try again.".into());
            }
        }
        let resources = bundled_resource_dir(app).ok();
        first_run::apply(&workspace, &app_dir, port, |path| {
            first_run::git_init(
                path,
                resources.as_deref(),
                first_run::command_line_tools_installed,
            )
        })?;
    }
    *service
        .app_dir
        .lock()
        .map_err(|_| "service state is unavailable")? = Some(app_dir.clone());

    match register_or_report_status(port)? {
        service_management::ServiceStatus::Enabled => {
            wait_for_service(port, &app_dir)?;
            local_service_ready(app, service, port)
        }
        service_management::ServiceStatus::RequiresApproval => Ok(ServiceStartup::RequiresApproval),
        service_management::ServiceStatus::NotRegistered => {
            Err("CORSIA service registration did not persist".into())
        }
        service_management::ServiceStatus::NotFound => {
            Err("CORSIA service definition is missing from this app bundle".into())
        }
    }
}

#[cfg(target_os = "macos")]
fn open_folder_dialog() -> Result<Option<PathBuf>, String> {
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSModalResponseOK, NSOpenPanel};

    let marker = MainThreadMarker::new().ok_or("the folder chooser must run on the main thread")?;
    let panel = NSOpenPanel::openPanel(marker);
    panel.setCanChooseDirectories(true);
    panel.setCanChooseFiles(false);
    panel.setAllowsMultipleSelection(false);
    panel.setCanCreateDirectories(true);
    if panel.runModal() != NSModalResponseOK {
        return Ok(None);
    }
    panel
        .URL()
        .and_then(|url| url.path())
        .map(|path| PathBuf::from(path.to_string()))
        .map(Some)
        .ok_or_else(|| "macOS did not return the selected folder".into())
}

#[cfg(not(target_os = "macos"))]
fn open_folder_dialog() -> Result<Option<PathBuf>, String> {
    Err("Choosing a work folder is only available in the macOS app".into())
}

#[tauri::command]
fn choose_work_folder(app: tauri::AppHandle) -> Result<Option<first_run::FolderClassification>, String> {
    let home = app
        .path()
        .home_dir()
        .map_err(|error| format!("could not resolve the home folder: {error}"))?;
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    app.run_on_main_thread(move || {
        let _ = sender.send(open_folder_dialog());
    })
    .map_err(|error| format!("could not open the folder chooser: {error}"))?;
    let Some(path) = receiver
        .recv()
        .map_err(|_| "the folder chooser closed unexpectedly")??
    else {
        return Ok(None);
    };
    Ok(Some(first_run::classify_folder(
        &path,
        &home,
        first_run::ensure_folder_writable,
    )))
}

#[tauri::command]
async fn configure_work_folder(app: tauri::AppHandle, path: PathBuf) -> Result<(), String> {
    let configuring = app.clone();
    let (app_dir, port) = tauri::async_runtime::spawn_blocking(move || {
        let app_dir = default_app_dir(&configuring)?;
        let home = configuring
            .path()
            .home_dir()
            .map_err(|error| format!("could not resolve the home folder: {error}"))?;
        first_run::ensure_folder_writable(&path)?;
        let port = first_run::recorded_port(&app_dir).unwrap_or(DEFAULT_SERVICE_PORT);
        let resources = bundled_resource_dir(&configuring).ok();
        first_run::apply_chosen_folder(
            &path,
            &first_run::default_workspace(&home),
            &app_dir,
            port,
            || setup_account_exists(port),
            |workspace| {
                first_run::git_init(
                    workspace,
                    resources.as_deref(),
                    first_run::command_line_tools_installed,
                )
            },
            restart_service,
            || wait_for_service(port, &app_dir),
        )?;
        Ok::<_, String>((app_dir, port))
    })
    .await
    .map_err(|error| {
        log::error!("configuring the work folder did not finish: {error}");
        "Couldn’t finish setting up the work folder. Quit CORSIA and open it again.".to_string()
    })??;
    let service = app.state::<AppServiceState>();
    grant_local_service_origin(&app, &service, port)?;
    *service
        .app_dir
        .lock()
        .map_err(|_| "service state is unavailable")? = Some(app_dir);
    Ok(())
}

fn local_shell_context(home: Option<&Path>, local_configured: bool) -> serde_json::Value {
    let Some(home) = home else { return serde_json::json!({ "mode": "local", "localConfigured": local_configured }); };
    let workspace = first_run::default_workspace(home);
    serde_json::json!({
        "mode": "local",
        "localConfigured": local_configured,
        "default_workspace": {
            "path": workspace.display().to_string(),
            "display": format!("~/{}", first_run::WORKSPACE_FOLDER),
        },
    })
}

fn remote_shell_context(connection: &shell_state::ShellState, home: Option<&Path>) -> serde_json::Value {
    match connection.state() {
        shell_state::LaunchState::Remote { name } => serde_json::json!({ "mode": "remote", "name": name, "localConfigured": connection.local_corsia_configured() }),
        _ => local_shell_context(home, connection.local_corsia_configured()),
    }
}

/// What a page load hands the window: the shell context, and the script drawing the shell state. A finished load of
/// any page but the startup shell means a Ready launch answer was followed, so it is not followed again.
fn page_load_state(connection: &mut shell_state::ShellState, url: &str, finished: bool, home: Option<&Path>) -> (serde_json::Value, Option<String>) {
    if finished && !is_shell_origin(url) {
        connection.left_shell();
    }
    (remote_shell_context(connection, home), shell_state_script(connection.state()))
}

fn remote_startup_state(state: &shell_state::LaunchState) -> Option<serde_json::Value> {
    match state {
        shell_state::LaunchState::Connecting { name } => Some(serde_json::json!({ "kind": "connecting", "name": name })),
        shell_state::LaunchState::Failed { name, failure } => Some(failure_shell_state(name, failure)),
        _ => None,
    }
}

/// The one mapping from a failure to the card the startup shell draws. Network, server and local share the
/// `unreachable` card and differ in its line; the others have a card of their own.
fn failure_shell_state(name: &str, failure: &classify::ConnectFailure) -> serde_json::Value {
    use classify::ConnectFailure;
    match failure {
        ConnectFailure::Network | ConnectFailure::Server | ConnectFailure::Local => serde_json::json!({ "kind": "unreachable", "name": name, "cause": failure.reason() }),
        ConnectFailure::KeyChanged => serde_json::json!({ "kind": "key_changed", "name": name }),
        ConnectFailure::Incompatible { app, host } => serde_json::json!({ "kind": "incompatible", "name": name, "app": app, "host": host }),
        ConnectFailure::TooOld => serde_json::json!({ "kind": "too_old", "name": name }),
    }
}

/// The script that draws a state on the startup shell: its failure card for the local service, or the remote card.
fn shell_state_script(state: &shell_state::LaunchState) -> Option<String> {
    match state {
        shell_state::LaunchState::ServiceFailed { reason } => Some(format!("window.__corsiaShowServiceFailure?.({});", serde_json::to_string(reason).unwrap_or_default())),
        shell_state::LaunchState::LocalStartup { startup } => match serde_json::to_string(startup) {
            Ok(startup) => Some(format!("window.__corsiaShowStartup?.({startup});")),
            Err(error) => {
                log::error!("could not hand the answer of the start to the window: {error}");
                None
            }
        },
        other => remote_startup_state(other).map(|payload| format!("window.__corsiaShowRemoteState?.({payload});")),
    }
}

fn retain_shell_state<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    update: impl FnOnce(&mut shell_state::ShellState),
) -> shell_state::LaunchState {
    let runtime = app.state::<RemoteRuntime>();
    // A poisoned lock still holds a whole state: every writer replaces it in one assignment.
    let mut connection = runtime.shell.lock().unwrap_or_else(PoisonError::into_inner);
    update(&mut connection);
    connection.state().clone()
}

fn update_shell_state<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    update: impl FnOnce(&mut shell_state::ShellState),
) -> Result<(), String> {
    let state = retain_shell_state(app, update);
    if let (Some(window), Some(script)) = (app.get_webview_window("main"), shell_state_script(&state)) {
        window.eval(script).map_err(|error| error.to_string())?;
    }
    Ok(())
}

/// The launch-time start can answer before the shell page has loaded, when a script sent to it is lost. No page awaits
/// it, so its answer is shell state: drawn now if the page is up and by the page-load hook if not. A start the window
/// runs itself is drawn by the page that awaits it, which is its only owner.
fn show_launch_outcome<R: tauri::Runtime>(app: &tauri::AppHandle<R>, outcome: &Result<ServiceStartup, String>) {
    if let Err(error) = outcome {
        log::error!("bundled CORSIA service did not start: {error}");
    }
    report_shell_state(app, |connection| connection.launch_answered(outcome.clone()));
}

/// The window began a local start of its own, so the launch answer is no longer what it shows. Nothing is drawn: the
/// page that runs the start draws its answer.
fn local_start_began<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    app.state::<RemoteRuntime>().shell.lock().unwrap_or_else(PoisonError::into_inner).local_start_began();
}

/// A start the window ran finished. Its page draws the answer; any answer (Ready, approval, adoption) also ends a kept
/// failure, which only a start that fails again leaves standing.
fn local_start_answered<R: tauri::Runtime, E>(app: &tauri::AppHandle<R>, answer: Result<ServiceStartup, E>) -> Result<ServiceStartup, E> {
    if answer.is_ok() {
        report_shell_state(app, shell_state::ShellState::service_recovered);
    }
    answer
}

/// Records a state the shell cannot act on. The state is already stored when drawing it fails, and the next page
/// load draws it, so the failure is logged and never dropped.
fn report_shell_state<R: tauri::Runtime>(app: &tauri::AppHandle<R>, update: impl FnOnce(&mut shell_state::ShellState)) {
    if let Err(error) = update_shell_state(app, update) {
        log::error!("could not show the startup shell state: {error}");
    }
}

fn grant_remote_proxy_origin<R: tauri::Runtime>(app: &tauri::AppHandle<R>, proxy_port: u16) -> Result<(), String> {
    app.add_capability(
        CapabilityBuilder::new("remote-proxy-origin")
            .remote(format!("http://127.0.0.1:{proxy_port}"))
            .window("main")
            .permission("allow-disconnect-remote")
            .permission("allow-start-window-drag")
            .permission_scoped("opener:allow-open-url", local_service_external_url_scopes(), Vec::<serde_json::Value>::new()),
    ).map_err(|error| format!("could not grant remote proxy commands: {error}"))
}

/// Swaps the running remote proxy, stopping the one it replaces.
fn replace_proxy(runtime: &RemoteRuntime, proxy: Option<proxy::ProxyHandle>) {
    let previous = std::mem::replace(&mut *runtime.proxy.lock().unwrap_or_else(PoisonError::into_inner), proxy);
    if let Some(previous) = previous {
        previous.stop();
    }
}

fn navigate_to_remote<R: tauri::Runtime>(app: &tauri::AppHandle<R>, name: &str, proxy_port: u16) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("the CORSIA window is unavailable")?;
    let name = serde_json::to_string(name).map_err(|error| error.to_string())?;
    window
        .eval(format!("window.__CORSIA_SHELL__ = {{ mode: 'remote', name: {name} }}; window.location.replace('http://127.0.0.1:{proxy_port}');"))
        .map_err(|error| error.to_string())
}

fn forget_remote_or_log(app_dir: &Path) {
    if let Err(error) = remote_config::forget_remote(app_dir) {
        log::error!("could not forget the remote after a failed connect: {error}");
    }
}

/// A failure on this computer (app folder, saved config, capability, window, local service). The shell has a card for
/// it of its own; the cause is logged for the person who has to fix it.
fn local_failure(step: &str, error: impl std::fmt::Display) -> classify::ConnectFailure {
    log::error!("could not connect the remote ({step}): {error}");
    classify::ConnectFailure::Local
}

fn proxy_failure(error: proxy::ProxyError) -> classify::ConnectFailure {
    match error {
        proxy::ProxyError::Unreachable(failure) => failure,
        other => local_failure("proxy", format!("{other:?}")),
    }
}

/// Brings this computer's own service back after a connect failed past the point that stopped it. When it cannot,
/// the window is left on a page whose service is gone, so the startup shell takes over with a card and its action.
fn restore_local_service<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    let service = app.state::<AppServiceState>();
    let restored = run_service_command("register").and_then(|()| start_service(app, &service).map(drop));
    if let Err(error) = restored {
        log::error!("could not bring the local service back after a failed connect: {error}");
        show_shell_failure(app, &error);
    }
}

/// The shell operations that enter a remote, for pairing and for every launch that connects a saved one. The two
/// differ only in what rolls back: pairing forgets the remote it just saved and brings the local service back, while a
/// launch keeps the saved pairing (Retry needs it) and the local service stays stopped as it was.
struct RemoteEntry<'a, R: tauri::Runtime, U, S> {
    app: &'a tauri::AppHandle<R>,
    app_dir: &'a Path,
    record: remote_config::RemoteRecord,
    forget_on_rollback: bool,
    unregister: U,
    restore: S,
}

impl<R: tauri::Runtime, U, S> RemoteEntry<'_, R, U, S> {
    /// Writes the remote with the port its proxy actually took.
    fn save(&self, proxy_port: u16) -> Result<(), classify::ConnectFailure> {
        let record = remote_config::RemoteRecord { proxy_port, ..self.record.clone() };
        remote_config::save_remote(self.app_dir, record).map_err(|error| local_failure("save", error))
    }
}

impl<R: tauri::Runtime, U: FnMut() -> Result<(), String>, S: FnMut()> remote_lifecycle::EnterRemote for RemoteEntry<'_, R, U, S> {
    type Proxy = proxy::ProxyHandle;
    type Error = classify::ConnectFailure;

    fn save_remote(&mut self, proxy: &proxy::ProxyHandle) -> Result<(), classify::ConnectFailure> {
        self.save(proxy.port)
    }

    fn grant_origin(&mut self, proxy: &proxy::ProxyHandle) -> Result<(), classify::ConnectFailure> {
        grant_remote_proxy_origin(self.app, proxy.port).map_err(|error| local_failure("grant", error))
    }

    fn unregister_service(&mut self) -> Result<(), classify::ConnectFailure> {
        (self.unregister)().map_err(|error| local_failure("service", error))
    }

    fn show(&mut self, proxy: &proxy::ProxyHandle) -> Result<(), classify::ConnectFailure> {
        let name = self.record.name.clone();
        update_shell_state(self.app, |connection| connection.connected(name.clone()))
            .and_then(|()| navigate_to_remote(self.app, &name, proxy.port))
            .map_err(|error| local_failure("show", error))
    }

    fn stop_proxy(&mut self, proxy: proxy::ProxyHandle) {
        proxy.stop();
    }

    fn forget_remote(&mut self) {
        if self.forget_on_rollback {
            forget_remote_or_log(self.app_dir);
        }
    }

    fn restore_service(&mut self) {
        (self.restore)();
    }
}

/// What the proxy does when the host presents a key other than the paired one. The proxy has already stopped
/// forwarding; this records the failure and puts the key-changed card in front of the person, who is otherwise
/// left on a remote page whose requests all fail.
fn key_changed_callback<R: tauri::Runtime>(app: &tauri::AppHandle<R>, name: &str) -> impl Fn() + Send + Sync + 'static {
    let app = app.clone();
    let name = name.to_owned();
    move || {
        report_shell_state(&app, |connection| connection.failed(name.clone(), classify::ConnectFailure::KeyChanged));
        return_to_shell(&app);
    }
}

/// Connects the saved remote at launch and on Retry. Whatever goes wrong ends in a failure state with actions,
/// never a silent return, so the shell never stays on "Connecting".
async fn connect_saved_remote<R: tauri::Runtime>(app: tauri::AppHandle<R>, saved: remote_config::RemoteRecord) {
    let name = saved.name.clone();
    report_shell_state(&app, |connection| connection.retry(name.clone()));
    if let Err(failure) = enter_saved_remote(&app, saved).await {
        report_shell_state(&app, |connection| connection.failed(name.clone(), failure));
    }
}

async fn enter_saved_remote<R: tauri::Runtime>(app: &tauri::AppHandle<R>, saved: remote_config::RemoteRecord) -> Result<(), classify::ConnectFailure> {
    // A retry can find the last attempt's proxy still up (a key change leaves it running); it owns the saved port.
    replace_proxy(&app.state::<RemoteRuntime>(), None);
    let app_dir = default_app_dir(app).map_err(|error| local_failure("app folder", error))?;
    let refresh = saved.clone();
    let started = check_through_routes(app, &saved).await?;
    let mut entry = RemoteEntry { app, app_dir: &app_dir, record: saved, forget_on_rollback: false, unregister: || Ok(()), restore: || {} };
    let proxy = remote_lifecycle::enter_remote(&mut entry, started)?;
    replace_proxy(&app.state::<RemoteRuntime>(), Some(proxy));
    tauri::async_runtime::spawn(refresh_outside_after_launch(app_dir, refresh));
    Ok(())
}

/// Convert the host-owned bare endpoint only at the desktop boundary. Invalid input never reaches the saved route.
fn outside_route(address: Option<&str>, has_identity: bool) -> Option<remote_config::OutsideRoute> {
    let address = address?;
    if !has_identity { return None; }
    let url = format!("https://{address}");
    if remote_host::parse_https_address(&url).is_err() {
        log::warn!("host returned an invalid outside address; ignoring it");
        return None;
    }
    Some(remote_config::OutsideRoute { url, trust: remote_config::RemoteTrust::System })
}

async fn refresh_outside_address(saved: &mut remote_config::RemoteRecord, host: &str, port: u16) {
    let Ok(trust) = trust::Trust::try_from(&saved.trust) else { return };
    if saved.identity.is_none() {
        match routes::local_identity(host, port, trust.clone()).await {
            Ok(identity) => saved.identity = Some(BASE64.encode(identity)),
            Err(error) => {
                log::warn!("could not learn local host identity: {}", error.into_message());
                return;
            }
        }
    }
    match pairing::outside_address(host, port, trust).await {
        Ok(address) => saved.outside = outside_route(address.as_deref(), saved.identity.is_some()),
        Err(error) => log::warn!("could not refresh outside address: {error:?}"),
    }
}

async fn refresh_outside_after_launch(app_dir: std::path::PathBuf, mut saved: remote_config::RemoteRecord) {
    let original = saved.clone();
    let local = match &saved.locator {
        remote_config::Locator::Address { host, port } => Some((host.clone(), *port)),
        remote_config::Locator::DnsSd { instance } => match discovery::resolve(instance, Duration::from_secs(15)).await {
            Ok(address) => Some(address),
            Err(error) => {
                log::warn!("could not resolve local host for outside address refresh: {error:?}");
                None
            }
        },
    };
    if let Some((host, port)) = local {
        refresh_outside_address(&mut saved, &host, port).await;
        if let Err(error) = remote_config::save_refreshed_remote(&app_dir, &original, saved) {
            log::warn!("could not save refreshed outside address: {error}");
        }
    }
}

/// A saved route set starts through the proxy, so the version check observes the same verified connection.
/// The set contains the local route and may also contain a saved outside route.
async fn check_through_routes<R: tauri::Runtime>(app: &tauri::AppHandle<R>, saved: &remote_config::RemoteRecord) -> Result<proxy::ProxyHandle, classify::ConnectFailure> {
    use classify::ConnectFailure;
    let started = proxy::start_record(saved, saved.proxy_port, key_changed_callback(app, &saved.name)).await.map_err(proxy_failure)?;
    match version_check::check_through_proxy(started.port, &version_check::app_version(app)).await {
        Ok(version_check::Compat::Ok) => Ok(started),
        Ok(version_check::Compat::Incompatible { app: app_version, host }) => Err(ConnectFailure::Incompatible { app: app_version, host }),
        Ok(version_check::Compat::TooOld) => Err(ConnectFailure::TooOld),
        Err(_) if started.key_changed() => Err(ConnectFailure::KeyChanged),
        Err(error) => Err(error.connect_failure().unwrap_or(ConnectFailure::Network)),
    }
}

/// Retry on a launch failure: connect the saved remote once more. It answers at once; the shell is told the
/// outcome through the same state pushes as a launch.
#[tauri::command]
fn retry_saved_remote(app: tauri::AppHandle) -> Result<(), String> {
    let saved = remote_config::load_remote(&default_app_dir(&app)?)?.ok_or("no remote is saved on this computer")?;
    tauri::async_runtime::spawn(connect_saved_remote(app, saved));
    Ok(())
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum RemoteTarget {
    DnsSd { instance: String },
    Address { host: String, port: Option<u16> },
}

#[derive(Serialize)]
struct RemoteNames {
    names: Vec<String>,
    denied: bool,
}

fn discovery_policy_denied(error: &discovery::DiscoveryError) -> bool {
    matches!(error, discovery::DiscoveryError::Service(-65_570))
}

#[derive(Serialize)]
#[serde(untagged)]
enum RemotePairResponse {
    Success { ok: bool, name: String },
    Failure { ok: bool, reason: String, app_version: Option<String>, host_version: Option<String> },
}

#[tauri::command]
fn remote_discovered(remote: tauri::State<'_, RemoteRuntime>) -> Result<RemoteNames, String> {
    let mut browser = remote.browser.lock().map_err(|_| "remote browser state is unavailable")?;
    if browser.is_none() {
        match discovery::Browser::start() {
            Ok(started) => *browser = Some(started),
            Err(error) if discovery_policy_denied(&error) => return Ok(RemoteNames { names: Vec::new(), denied: true }),
            Err(error) => return Err(format!("could not start discovery: {error:?}")),
        }
    }
    Ok(RemoteNames { names: browser.as_ref().expect("browser created").names(), denied: false })
}

fn failure_response(failure: classify::ConnectFailure) -> RemotePairResponse {
    let (app_version, host_version) = match &failure {
        classify::ConnectFailure::Incompatible { app, host } => (Some(app.clone()), host.clone()),
        _ => (None, None),
    };
    RemotePairResponse::Failure { ok: false, reason: failure.reason().into(), app_version, host_version }
}

fn remote_pair_error(error: pairing::PairError) -> RemotePairResponse {
    use classify::ConnectFailure;
    use pairing::PairError;
    let reason = match error {
        PairError::WrongCode => "wrong_code",
        PairError::Expired => "expired",
        PairError::TooManyTries => "too_many_tries",
        PairError::Paused => "paused",
        PairError::Busy => "busy",
        PairError::NotLocal => "not_local",
        // The host's key moved under a pairing that just trusted it. CORSIA is running there, so the server line would
        // be false; the local line is true and names the fix (a new code).
        PairError::KeyChanged => ConnectFailure::Local.reason(),
        other => return failure_response(other.connect_failure().unwrap_or(ConnectFailure::Local)),
    };
    RemotePairResponse::Failure { ok: false, reason: reason.into(), app_version: None, host_version: None }
}

/// Pairing leaves the shell in `Pairing` while it works. Anything but a connected remote ends back on `Local` (unless
/// the rollback recorded a failure), so a failed pairing never leaves the recorded state behind the real one.
fn leave_pairing_unless_connected<R: tauri::Runtime>(app: &tauri::AppHandle<R>, outcome: &Result<RemotePairResponse, String>) {
    if !matches!(outcome, Ok(RemotePairResponse::Success { .. })) {
        report_shell_state(app, |connection| connection.end_pairing());
    }
}

// The page calls this with `{ target, code }` (`remoteShell.ts`); Tauri matches each top-level key to the parameter of
// the same name, so these names are the IPC contract.
#[tauri::command]
async fn remote_pair<R: tauri::Runtime>(
    target: RemoteTarget,
    code: String,
    app: tauri::AppHandle<R>,
    remote: tauri::State<'_, RemoteRuntime>,
) -> Result<RemotePairResponse, String> {
    let outcome = pair_remote(target, &code, &app, &remote).await;
    if let Err(error) = &outcome {
        log::warn!("pairing failed on this computer: {error}");
    }
    leave_pairing_unless_connected(&app, &outcome);
    outcome
}

/// The page shows only the reason, so every failed pairing logs its cause here (never the code or key material).
fn pairing_failed(step: &str, cause: impl std::fmt::Debug, response: RemotePairResponse) -> Result<RemotePairResponse, String> {
    log::warn!("pairing failed at {step}: {cause:?}");
    Ok(response)
}

async fn pair_remote<R: tauri::Runtime>(target: RemoteTarget, code: &str, app: &tauri::AppHandle<R>, remote: &RemoteRuntime) -> Result<RemotePairResponse, String> {
    use classify::ConnectFailure;
    let app_dir = default_app_dir(app)?;
    let local_configured = local_corsia_configured(app, &app_dir);
    update_shell_state(app, |connection| {
        connection.set_local_corsia_configured(local_configured);
        connection.begin_pairing();
    })?;
    let locator = match target {
        RemoteTarget::DnsSd { instance } => remote_config::Locator::DnsSd { instance },
        RemoteTarget::Address { host, port } => {
            let (host, parsed_port) = remote_host::parse_pairing_address(&host, port.unwrap_or(47_901))
                .map_err(|_| "invalid remote address")?;
            remote_config::Locator::Address { host, port: parsed_port }
        },
    };
    let (host, port) = match &locator {
        remote_config::Locator::Address { host, port } => (host.clone(), *port),
        remote_config::Locator::DnsSd { instance } => match discovery::resolve(instance, Duration::from_secs(15)).await {
            Ok(address) => address,
            Err(error) => return pairing_failed("discovery", error, failure_response(ConnectFailure::Network)),
        },
    };
    let paired = match pairing::pair(&host, port, code).await {
        Ok(paired) => paired,
        Err(error) => return pairing_failed("pair", &error, remote_pair_error(error.clone())),
    };
    match version_check::check(&paired.host, paired.port, &paired.trust, &version_check::app_version(app)).await {
        Ok(version_check::Compat::Ok) => {}
        Ok(version_check::Compat::Incompatible { app: app_version, host }) => {
            let failure = ConnectFailure::Incompatible { app: app_version, host };
            return pairing_failed("version check", &failure, failure_response(failure.clone()));
        }
        Ok(version_check::Compat::TooOld) => return pairing_failed("version check", ConnectFailure::TooOld, failure_response(ConnectFailure::TooOld)),
        Err(error) => return pairing_failed("version check", &error, remote_pair_error(error.clone())),
    }
    let record = remote_config::RemoteRecord {
        locator,
        trust: remote_config::RemoteTrust::from(&paired.trust),
        name: paired.name.clone(),
        proxy_port: 0,
        identity: Some(BASE64.encode(paired.identity)),
        outside: outside_route(paired.outside_address.as_deref(), true),
    };
    let started = match proxy::start_record(&record, 0, key_changed_callback(app, &paired.name)).await {
        Ok(started) => started,
        Err(error) => return pairing_failed("proxy start", &error, failure_response(proxy_failure(error.clone()))),
    };
    let mut entry = RemoteEntry {
        app,
        app_dir: &app_dir,
        record,
        forget_on_rollback: true,
        unregister: || lifecycle::unregister_unless_locked(app),
        restore: || restore_local_service(app),
    };
    match remote_lifecycle::enter_remote(&mut entry, started) {
        Ok(proxy) => {
            replace_proxy(remote, Some(proxy));
            Ok(RemotePairResponse::Success { ok: true, name: paired.name })
        }
        Err(failure) => pairing_failed("entering the remote", &failure, failure_response(failure.clone())),
    }
}

fn saved_proxy_origin_allows(remote: Option<&remote_config::RemoteRecord>, actual: &str) -> bool {
    remote.is_some_and(|saved| actual.trim_end_matches('/') == format!("http://127.0.0.1:{}", saved.proxy_port))
}

fn disconnect_origin_allows(remote: Option<&remote_config::RemoteRecord>, actual: &str) -> bool {
    is_shell_origin(actual) || saved_proxy_origin_allows(remote, actual)
}

/// The startup shell's own page, the only caller besides the saved proxy origin: its launch-failure actions
/// (Set up this computer instead, Pair again) have to be able to forget the saved remote.
fn is_shell_origin(actual: &str) -> bool {
    tauri::Url::parse(actual).is_ok_and(|url| url.scheme() == "tauri" && url.host_str() == Some("localhost"))
}

/// Why `start_local_service` refuses this caller, if it does: only the startup shell's page starts the service, and a
/// caller whose page can't be read is not that page.
fn local_start_refusal(calling_origin: Option<&str>) -> Option<String> {
    if calling_origin.is_some_and(is_shell_origin) {
        None
    } else {
        Some("Only CORSIA's startup window can start CORSIA.".into())
    }
}

/// What a disconnect removes from the webview so the host's session token does not outlive the pairing: the remote
/// page's own token when the call comes from that page, or all of the webview's stored data when it comes from the
/// startup shell, which cannot reach the proxy origin's storage any other way. The second also signs the person out of
/// this computer's own CORSIA page, who is about to set it up again.
#[derive(Debug, PartialEq, Eq)]
enum RemoteSessionClear {
    ProxyToken,
    AllBrowsingData,
}

const CLEAR_REMOTE_TOKEN_SCRIPT: &str = "try { localStorage.removeItem('corsia_token'); } catch (_) {}";

fn remote_session_clear(calling_origin: &str) -> RemoteSessionClear {
    if is_shell_origin(calling_origin) { RemoteSessionClear::AllBrowsingData } else { RemoteSessionClear::ProxyToken }
}

fn clear_remote_session<R: tauri::Runtime>(app: &tauri::AppHandle<R>, clear: &RemoteSessionClear) {
    let Some(window) = app.get_webview_window("main") else { return };
    let cleared = match clear {
        RemoteSessionClear::ProxyToken => window.eval(CLEAR_REMOTE_TOKEN_SCRIPT),
        RemoteSessionClear::AllBrowsingData => window.clear_all_browsing_data(),
    };
    if let Err(error) = cleared {
        log::error!("could not clear the remote session from the window: {error}");
    }
}

/// Puts the startup shell back in front of the person with a failure card that carries its action. Used when the
/// page they are on has nothing left to recover with (its proxy is stopped and the local service did not come up).
/// The failure is retained before the sole navigation; the page-load hook draws it after its own script is ready.
fn show_shell_failure<R: tauri::Runtime>(app: &tauri::AppHandle<R>, reason: &str) {
    retain_shell_state(app, |connection| connection.service_failed(reason));
    return_to_shell(app);
}

fn is_startup_shell_page(actual: &str) -> bool {
    is_shell_origin(actual) && tauri::Url::parse(actual).is_ok_and(|url| matches!(url.path(), "/" | "/index.html"))
}

/// Navigates the window to the startup shell's index page unless it is already there.
fn return_to_shell<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    let navigated = app
        .get_webview_window("main")
        .ok_or_else(|| "the CORSIA window is unavailable".to_owned())
        .and_then(|window| {
            if window.url().is_ok_and(|url| is_startup_shell_page(url.as_str())) {
                return Ok(());
            }
            window.navigate(tauri::Url::parse("tauri://localhost/index.html").expect("a valid URL")).map_err(|error| error.to_string())
        });
    if let Err(error) = navigated {
        log::error!("could not return to the startup shell: {error}");
    }
}

/// A remote page cannot draw a local-start answer after its proxy is stopped. Keep that answer in the startup shell
/// and return there unless the local service is already ready to replace the page directly.
fn show_disconnected_startup<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    called_from_shell: bool,
    startup: &ServiceStartup,
) -> Result<(), String> {
    report_shell_state(app, |connection| connection.local());
    if let ServiceStartup::Ready { url } = startup {
        let window = app.get_webview_window("main").ok_or("the CORSIA window is unavailable")?;
        window.eval(format!("window.location.replace('{url}');")).map_err(|error| error.to_string())?;
    } else if !called_from_shell {
        show_launch_outcome(app, &Ok(startup.clone()));
        return_to_shell(app);
    }
    Ok(())
}

/// Forgets the saved remote and brings the local service back. Called by the remote page's Disconnect and by the
/// startup shell's launch-failure actions; it answers like `start_local_service`.
#[tauri::command]
async fn disconnect_remote(
    app: tauri::AppHandle,
    _service: tauri::State<'_, AppServiceState>,
    remote: tauri::State<'_, RemoteRuntime>,
    webview: tauri::Webview,
) -> Result<ServiceStartup, String> {
    // The invoking page can navigate while the service work waits, so its authority is fixed before the first await.
    let actual = webview
        .url()
        .map_err(|error| format!("could not read calling origin: {error}"))?;
    let app_dir = default_app_dir(&app)?;
    let saved = remote_config::load_remote(&app_dir)?;
    if !disconnect_origin_allows(saved.as_ref(), actual.as_str()) {
        return Err("disconnect_remote is allowed only from the startup shell or the saved proxy origin".into());
    }

    let starting = app.clone();
    let outcome = remote_lifecycle::disconnect(
        move || async move {
            tauri::async_runtime::spawn_blocking(move || remote_config::forget_remote(&app_dir))
                .await
                .map_err(|error| error.to_string())?
        },
        || {
            // Webview and window state stay on this task, after forgetting and before the
            // local service is brought back.
            clear_remote_session(&app, &remote_session_clear(actual.as_str()));
            replace_proxy(&remote, None);
            report_shell_state(&app, |connection| connection.begin_disconnect());
        },
        || async {
            tauri::async_runtime::spawn_blocking(|| run_service_command("register"))
                .await
                .map_err(|error| error.to_string())?
        },
        move || async move {
            tauri::async_runtime::spawn_blocking(move || {
                let service = starting.state::<AppServiceState>();
                start_service(&starting, &service)
            })
            .await
            .map_err(|error| error.to_string())?
        },
    )
    .await;
    match outcome {
        Ok(startup) => {
            show_disconnected_startup(&app, is_shell_origin(actual.as_str()), &startup)?;
            Ok(startup)
        }
        Err(remote_lifecycle::DisconnectError::NotForgotten(error)) => Err(error),
        Err(remote_lifecycle::DisconnectError::Forgotten(error)) => {
            report_shell_state(&app, |connection| connection.local());
            // The calling page's proxy is gone, so it cannot show the error with a way forward; the startup
            // shell can. A shell caller is already on it and draws the failure from this error itself.
            if !is_shell_origin(actual.as_str()) {
                show_shell_failure(&app, &error);
            }
            Err(error)
        }
    }
}

/// What adoption is doing now and whether a Quit is waiting on it.
#[derive(Debug, PartialEq, Eq, serde::Serialize)]
struct AdoptionProgress {
    #[serde(flatten)]
    phase: adoption::Phase,
    /// A Quit was asked for and will be carried out once the existing install
    /// is running again as before, which is only ever true while the stop is
    /// still draining, the backup is being made, or the existing install is being
    /// started again.
    quit_pending: bool,
}

impl AdoptionProgress {
    fn new(phase: adoption::Phase, quit_asked: bool) -> Self {
        let quit_pending = quit_asked
            && matches!(
                phase,
                adoption::Phase::Waiting { .. }
                    | adoption::Phase::BackingUp
                    | adoption::Phase::MovingToBackup
                    | adoption::Phase::StartingExisting
            );
        Self { phase, quit_pending }
    }
}

fn set_adoption_phase(service: &AppServiceState, phase: Option<adoption::Phase>) {
    if let Ok(mut shown) = service.adoption_phase.lock() {
        *shown = phase;
    }
}

/// What adoption is doing now: the window picks the line from the phase, never from text.
#[tauri::command]
fn adoption_progress(service: tauri::State<'_, AppServiceState>) -> Option<AdoptionProgress> {
    let phase = service.adoption_phase.lock().ok().and_then(|phase| phase.clone())?;
    Some(AdoptionProgress::new(
        phase,
        service.adoption_cancel_requested.load(Ordering::Acquire),
    ))
}

/// Run a long, blocking step off the main thread, so the window keeps handling
/// events (Quit, the live task count) while it works.
async fn off_main_thread<T, F>(work: F) -> Result<T, adoption::AdoptionError>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, adoption::AdoptionError> + Send + 'static,
{
    // A panic says nothing about the old install or the switch, so it is never
    // reported as either: the journal decides what a relaunch has to undo.
    tauri::async_runtime::spawn_blocking(work)
        .await
        .map_err(|error| adoption::AdoptionError::reopen(format!("the step did not finish: {error}")))?
}

/// Whether a Quit asked for during adoption can now be carried out. Never when
/// the old install may not be running: the person has to see that first.
fn quit_after_adoption(quit_asked: bool, adopted: &Result<(), adoption::AdoptionError>) -> bool {
    quit_asked
        && match adopted {
            Ok(()) => true,
            Err(error) => matches!(
                error.outcome,
                adoption::Outcome::Untouched | adoption::Outcome::Restored
            ),
        }
}

#[tauri::command]
async fn adopt_existing_install(app: tauri::AppHandle) -> Result<ServiceStartup, adoption::AdoptionError> {
    local_start_began(&app);
    let adopting = app.clone();
    local_start_answered(&app, off_main_thread(move || adopt_existing_install_blocking(&adopting)).await)
}

fn adopt_existing_install_blocking(app: &tauri::AppHandle) -> Result<ServiceStartup, adoption::AdoptionError> {
    let service = app.state::<AppServiceState>();
    // Nothing has been stopped until the plan below is running.
    let untouched = adoption::AdoptionError::untouched;
    let home = app.path().home_dir().map_err(|error| untouched(format!("could not resolve the home folder: {error}")))?;
    let app_dir = default_app_dir(app).map_err(untouched)?;
    let first_run = first_run::plan(&home, &app_dir).map_err(untouched)?;
    let first_run::FirstRun::Adoptable { source } = first_run else {
        return Err(untouched("there is no source install ready to adopt".to_string()));
    };
    let port = source.app_port(&app_dir);
    let plan = adoption::Plan { source: source.clone(), app_dir: app_dir.clone() };
    let resources = bundled_resource_dir(app).map_err(untouched)?;
    service.adoption_cancel_requested.store(false, Ordering::Release);
    service.adoption_in_progress.store(true, Ordering::Release);
    let mut lifecycle = SourceLifecycle::new(source, Bundled::in_resources(&resources));
    let adopted = adoption::adopt(
        &plan,
        &mut lifecycle,
        &service.adoption_cancel_requested,
        &mut |phase| set_adoption_phase(&service, Some(phase)),
    );
    service.adoption_in_progress.store(false, Ordering::Release);
    set_adoption_phase(&service, None);
    // Quit during adoption was asked to wait for a step boundary; the old
    // install is back (or the switch is made), so the app can now quit.
    let quit_asked = service.adoption_cancel_requested.load(Ordering::Acquire);
    if quit_after_adoption(quit_asked, &adopted) {
        lifecycle::request_quit(app);
    }
    adopted?;
    #[cfg(all(unix, not(test)))]
    match adoption::retired_cli(&app_dir) {
        Ok(Some(retired)) => cli_link::repoint_existing(&home, &resources.join("bin/corsia"), cli_link::Repair::Adoption(&retired)),
        Ok(None) => (),
        Err(error) => log::warn!("could not inspect adoption record for CLI links: {error}"),
    }
    // Adoption is done: from here a failure is the app's own service still coming up, never the old install.
    let switched = |message: &str| adoption::AdoptionError::switched(message);
    *service.app_dir.lock().map_err(|_| switched("service state is unavailable"))? = Some(app_dir.clone());
    wait_for_service(port, &app_dir).map_err(|error| switched(&error))?;
    local_service_ready(app, &service, port).map_err(|error| switched(&error))
}

/// The earlier install's service, stopped through its own checkout when this app could adopt it,
/// and that checkout's `scripts/corsia`, which the person's `corsia` links may still point at.
fn earlier_install_service(home: &Path, app_dir: &Path, resources: &Path) -> (start_fresh::LaunchdService, Option<PathBuf>) {
    let source = match first_run::plan(home, app_dir) {
        Ok(first_run::FirstRun::Adoptable { source }) => Some(source),
        _ => None,
    };
    let cli = source.as_ref().map(|source| source.cli.clone());
    let backend_dir = source.as_ref().map(|source| source.working_directory.clone());
    let lifecycle = source.map(|source| SourceLifecycle::new(source, Bundled::in_resources(resources)));
    (start_fresh::LaunchdService::new(home, lifecycle, backend_dir, Bundled::in_resources(resources).python), cli)
}

/// What Start fresh would move and which folders stay, for the confirmation, or why it can't run.
#[tauri::command]
fn start_fresh_preview(app: tauri::AppHandle) -> Result<start_fresh::Preview, start_fresh::StartFreshError> {
    let untouched = start_fresh::StartFreshError::untouched;
    let home = app.path().home_dir().map_err(|error| untouched(format!("could not resolve the home folder: {error}")))?;
    start_fresh::preview(&home, &default_app_dir(&app).map_err(untouched)?)
}

/// Set the earlier install aside in a backup folder. Answers that folder, as the window shows it.
#[tauri::command]
async fn start_fresh(app: tauri::AppHandle) -> Result<String, start_fresh::StartFreshError> {
    local_start_began(&app);
    let working = app.clone();
    tauri::async_runtime::spawn_blocking(move || start_fresh_blocking(&working))
        .await
        .map_err(|error| start_fresh::StartFreshError::unfinished(format!("the step did not finish: {error}")))?
}

fn start_fresh_blocking(app: &tauri::AppHandle) -> Result<String, start_fresh::StartFreshError> {
    let untouched = start_fresh::StartFreshError::untouched;
    let service = app.state::<AppServiceState>();
    let home = app.path().home_dir().map_err(|error| untouched(format!("could not resolve the home folder: {error}")))?;
    let app_dir = default_app_dir(app).map_err(untouched)?;
    let resources = bundled_resource_dir(app).map_err(untouched)?;
    let (mut old, retired) = earlier_install_service(&home, &app_dir, &resources);
    service.adoption_cancel_requested.store(false, Ordering::Release);
    service.adoption_in_progress.store(true, Ordering::Release);
    let result = start_fresh::start_fresh(
        &home,
        &app_dir,
        &mut old,
        &mut |phase| set_adoption_phase(&service, Some(phase)),
        std::time::SystemTime::now(),
    );
    service.adoption_in_progress.store(false, Ordering::Release);
    set_adoption_phase(&service, None);
    if let Err(error) = &result {
        log::warn!("Start fresh did not finish: {} ({})", error.message, error.details);
    }
    if start_fresh::quit_after(service.adoption_cancel_requested.load(Ordering::Acquire), &result) {
        lifecycle::request_quit(app);
    }
    // The checkout stays where it is, so a link to it still resolves: replace it as Take over does.
    #[cfg(all(unix, not(test)))]
    if let (Ok(_), Some(retired)) = (&result, &retired) {
        cli_link::repoint_existing(&home, &resources.join("bin/corsia"), cli_link::Repair::Adoption(retired));
    }
    #[cfg(any(not(unix), test))]
    let _ = &retired;
    result.map(|backup| start_fresh::shown(&home, &backup))
}

/// "Start it again": with every file where it was, start the existing install
/// if it is stopped and release its exact hold. Never repeats the adoption.
#[tauri::command]
async fn start_existing_again(app: tauri::AppHandle) -> Result<(), adoption::AdoptionError> {
    local_start_began(&app);
    off_main_thread(move || start_existing_again_blocking(&app)).await
}

fn start_existing_again_blocking(app: &tauri::AppHandle) -> Result<(), adoption::AdoptionError> {
    let service = app.state::<AppServiceState>();
    let app_dir = default_app_dir(app)?;
    let Some(plan) = adoption::interrupted_plan(&app_dir)? else { return Ok(()) };
    let resources = bundled_resource_dir(app).map_err(adoption::AdoptionError::reopen)?;
    let mut lifecycle = SourceLifecycle::new(plan.source.clone(), Bundled::in_resources(&resources));
    service.adoption_cancel_requested.store(false, Ordering::Release);
    service.adoption_in_progress.store(true, Ordering::Release);
    let started = adoption::start_again(&plan, &mut lifecycle, &mut |phase| {
        set_adoption_phase(&service, Some(phase))
    });
    service.adoption_in_progress.store(false, Ordering::Release);
    set_adoption_phase(&service, None);
    // A Quit asked for meanwhile only waited for the existing install to be running again.
    let quit_asked = service.adoption_cancel_requested.load(Ordering::Acquire);
    if quit_after_adoption(quit_asked, &started) {
        lifecycle::request_quit(app);
    }
    started
}

/// The lifecycle of the source install this app would adopt, for the two
/// actions that bring it back to a state adoption can take over.
fn existing_install_lifecycle(app: &tauri::AppHandle) -> Result<SourceLifecycle, adoption::AdoptionError> {
    let untouched = adoption::AdoptionError::untouched;
    let home = app.path().home_dir().map_err(|error| untouched(format!("could not resolve the home folder: {error}")))?;
    let app_dir = default_app_dir(app).map_err(untouched)?;
    let first_run::FirstRun::Adoptable { source } = first_run::plan(&home, &app_dir).map_err(untouched)? else {
        return Err(untouched("there is no source install ready to adopt".to_string()));
    };
    let resources = bundled_resource_dir(app).map_err(untouched)?;
    Ok(SourceLifecycle::new(source, Bundled::in_resources(&resources)))
}

/// "Start it": run the existing install's own `corsia start` and wait for it
/// to answer. The window then asks to take it over again; nothing is adopted here.
#[tauri::command]
async fn start_source_install(app: tauri::AppHandle) -> Result<(), adoption::AdoptionError> {
    off_main_thread(move || {
        let mut lifecycle = existing_install_lifecycle(&app)?;
        adoption::Lifecycle::start_old(&mut lifecycle).map_err(adoption::AdoptionError::could_not_start)
    })
    .await
}

/// "Restart it": the existing install's own `corsia restart`, for one that
/// has been stuck restarting. Again nothing is adopted here.
#[tauri::command]
async fn restart_source_install(app: tauri::AppHandle) -> Result<(), adoption::AdoptionError> {
    off_main_thread(move || {
        existing_install_lifecycle(&app)?
            .restart_existing()
            .map_err(adoption::AdoptionError::could_not_start)
    })
    .await
}

/// "Quit": the window's close button only hides it, so the shell's Quit asks
/// the app to quit, which stops the background service first.
#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    lifecycle::request_quit(&app);
}

/// "Reopen CORSIA": relaunch so the adoption journal finishes its undo on launch.
#[tauri::command]
fn reopen_app(app: tauri::AppHandle) -> Result<(), String> {
    let failed = || "CORSIA couldn’t reopen itself.".to_string();
    let bundle = update_stage::running_bundle().map_err(|_| failed())?;
    if relaunch(&app, &bundle) { Ok(()) } else { Err(failed()) }
}

/// "Check again": the app's service is registered; wait for it to answer.
#[tauri::command]
async fn check_service_started(app: tauri::AppHandle) -> Result<ServiceStartup, adoption::AdoptionError> {
    local_start_began(&app);
    let checking = app.clone();
    let answer = off_main_thread(move || {
        let service = checking.state::<AppServiceState>();
        let app_dir = default_app_dir(&checking).map_err(adoption::AdoptionError::switched)?;
        let port = lifecycle::service_port(&checking, &service);
        wait_for_service(port, &app_dir).map_err(adoption::AdoptionError::switched)?;
        local_service_ready(&checking, &service, port).map_err(adoption::AdoptionError::switched)
    })
    .await;
    local_start_answered(&app, answer)
}

/// What `start_local_service` answers: the full start's outcome, or a recovery start's.
#[derive(Debug, PartialEq, Eq, Serialize)]
#[serde(untagged)]
enum LocalStartAnswer {
    Startup(ServiceStartup),
    Recovery(update_recovery::RecoveryAnswer),
}

/// Records an exceptional update-recovery wrapper error through the startup shell's existing failure path.
fn show_recovery_failure<R: tauri::Runtime>(app: &tauri::AppHandle<R>, error: String) -> String {
    log::error!("update recovery did not finish: {error}");
    show_shell_failure(app, "");
    error
}

/// Flattens a recovery worker answer: either an inner recovery-wrapper error or a worker join error reaches the same
/// retained failure path, while typed recovery answers remain untouched.
fn finish_recovery<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    answer: Result<Result<update_recovery::RecoveryAnswer, String>, String>,
) -> Result<update_recovery::RecoveryAnswer, String> {
    answer.and_then(|answer| answer).map_err(|error| show_recovery_failure(app, error))
}

/// Uses the initialized service folder when present, otherwise the same canonical folder ordinary startup resolves.
/// The result only identifies the existing update lock and never initializes or repairs service state.
fn recovery_app_dir(
    current: Option<PathBuf>,
    resolve_default: impl FnOnce() -> Result<PathBuf, String>,
) -> Result<PathBuf, String> {
    current.map(Ok).unwrap_or_else(resolve_default)
}

/// A recovery start from the startup shell's update screen. Typed recovery answers leave the retained launch answer
/// alone. An exceptional wrapper error instead becomes the shell's retained failure and returns there, so its existing
/// page-load hook owns the failure card.
async fn recover_local_service(app: tauri::AppHandle, intent: update_recovery::RecoveryIntent) -> Result<update_recovery::RecoveryAnswer, String> {
    let recovering = app.clone();
    let answer = tauri::async_runtime::spawn_blocking(move || {
        let service = recovering.state::<AppServiceState>();
        let app_dir = recovery_app_dir(
            service.app_dir.lock().unwrap_or_else(PoisonError::into_inner).clone(),
            || default_app_dir(&recovering),
        )?;
        let bundle = update_stage::running_bundle()?;
        let port = lifecycle::service_port(&recovering, &service);
        let answer = update_recovery::recover(
            intent,
            &recovering.state::<RemoteRuntime>().shell,
            &bundle,
            &app_paths::state_dir_for(&app_dir)?,
            || service.app_dir.lock().unwrap_or_else(PoisonError::into_inner).is_some(),
            &service_url(port),
            &update_recovery::SystemRecovery { port, app_dir: &app_dir },
        );
        if matches!(answer, update_recovery::RecoveryAnswer::Ready { .. }) {
            grant_local_service_origin(&recovering, &service, port)?;
        }
        Ok::<_, String>(answer)
    })
    .await
    .map_err(|error| format!("bringing CORSIA back did not finish: {error}"));
    finish_recovery(&app, answer)
}

#[tauri::command]
async fn start_local_service(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
    webview: tauri::Webview,
    recovery: Option<update_recovery::RecoveryIntent>,
) -> Result<LocalStartAnswer, String> {
    // Capture the caller before the await: a local-service page cannot render typed non-ready answers, while the
    // startup shell awaits and renders its own result. A URL failure is deliberately not treated as local.
    let calling_origin = webview.url().ok().map(|url| url.to_string());
    // Refused before anything runs or the shell hears of a start: the capability already refuses other origins, and
    // this holds if a grant ever widens.
    if let Some(refusal) = local_start_refusal(calling_origin.as_deref()) {
        return Err(refusal);
    }
    // The update screen's recovery start never runs the full start below.
    if let Some(intent) = recovery {
        return recover_local_service(app, intent).await.map(LocalStartAnswer::Recovery);
    }
    // Finishing an interrupted adoption's undo can wait on the source service,
    // so it never runs on the thread that handles the window's events.
    local_start_began(&app);
    let started_app = app.clone();
    let answer = tauri::async_runtime::spawn_blocking(move || {
        let service = started_app.state::<AppServiceState>();
        start_service(&started_app, &service)
    })
    .await
    .map_err(|error| format!("starting CORSIA did not finish: {error}"))?;
    // Only the startup shell runs this start (refused above otherwise), and it follows the Ready address itself.
    let answer = with_relaunched_screen(&service.relaunched_screen, answer);
    let answer = local_start_answered(&app, answer);
    answer.map(LocalStartAnswer::Startup)
}

/// One progress reading: the service's record, and for the startup shell's update page the local service's address
/// once it answers. Without an address it serializes exactly as the bare record did.
#[derive(Serialize, Debug, PartialEq)]
struct ProgressReading {
    #[serde(flatten)]
    progress: Option<StartupProgress>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ready_url: Option<String>,
}

/// Whether the calling page is the startup shell's update page (`update.html`), the only page told the service is back.
fn is_shell_update_page(actual: &str) -> bool {
    is_shell_origin(actual) && tauri::Url::parse(actual).is_ok_and(|url| url.path() == "/update.html")
}

/// Set while one readiness check runs, so readings that overlap never stack their health waits.
static READINESS_CHECK: AtomicBool = AtomicBool::new(false);

/// The local service's address for the update page, only when this process already served that page (`served`), the
/// window is on this computer's own service, the service answers healthy, it reports no update or restart in flight,
/// and nothing holds the update lock: a rollback records its failed row before it stops the service and puts the old
/// app back under that lock, so a terminal row alone is not an idle updater. The lock is tried last, without waiting,
/// and let go at once. Each check is one bounded request; none starts, registers or grants anything.
fn update_page_ready_url(
    checking: &AtomicBool,
    served: bool,
    local: bool,
    port: u16,
    healthy: impl FnOnce(u16) -> bool,
    operation_active: impl FnOnce(u16) -> Result<bool, String>,
    update_lock_free: impl FnOnce() -> bool,
) -> Option<String> {
    if !served || !local || checking.swap(true, Ordering::AcqRel) {
        return None;
    }
    let ready = healthy(port) && operation_active(port) == Ok(false) && update_lock_free();
    checking.store(false, Ordering::Release);
    ready.then(|| service_url(port))
}

/// Whether no update, rollback or start holds the update lock now: taken without waiting and dropped at once.
fn update_lock_is_free(state_dir: &Path) -> bool {
    matches!(app_paths::try_lock_update_in_existing(state_dir), Ok(Some(_)))
}

/// Reads the current local service's own progress record while HTTP is unavailable.
/// Missing, malformed, stale-PID, and unsupported-schema records all return `None`; the raw
/// diagnostic and database-schema version fields never cross this command boundary.
#[tauri::command]
async fn read_startup_progress(
    app: tauri::AppHandle,
    _service: tauri::State<'_, AppServiceState>,
    webview: tauri::Webview,
) -> Result<Option<ProgressReading>, String> {
    let on_update_page = webview.url().is_ok_and(|url| is_shell_update_page(url.as_str()));
    let reading = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let service = reading.state::<AppServiceState>();
        let session_dir = service.app_dir.lock().map_err(|_| "could not read startup progress")?.clone();
        let local_session = session_dir.is_some();
        let app_dir = session_dir.unwrap_or(default_app_dir(&reading).map_err(|_| "could not read startup progress")?);
        let state = app_paths::state_dir_for(&app_dir).map_err(|_| "could not read startup progress")?;
        let target = format!("gui/{}/{}", unsafe { libc::geteuid() }, service_label());
        let pid = release_proof::launchd_pid(&target);
        let progress = current_startup_progress(&state.join("runtime").join("startup-progress.json"), pid);
        let ready_url = if on_update_page && local_session {
            let port = lifecycle::service_port(&reading, &service);
            let served = service.granted_local_ports.lock().unwrap_or_else(PoisonError::into_inner).contains(&port);
            let local = reading.state::<RemoteRuntime>().shell.lock().unwrap_or_else(PoisonError::into_inner).allows_manual_recovery();
            update_page_ready_url(&READINESS_CHECK, served, local, port, probe_health, operation_is_active, || update_lock_is_free(&state))
        } else {
            None
        };
        Ok((progress.is_some() || ready_url.is_some()).then_some(ProgressReading { progress, ready_url }))
    })
    .await
    .map_err(|_| "could not read startup progress")?
}

#[tauri::command]
fn open_login_items() {
    service_management::open_login_items();
}

#[tauri::command]
fn get_service_log_path(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
) -> Result<String, String> {
    let app_dir = service
        .app_dir
        .lock()
        .map_err(|_| "service state is unavailable")?
        .clone()
        .unwrap_or(default_app_dir(&app)?);
    Ok(service_log_path(&app_dir)?.display().to_string())
}

/// The most a setup report's details may hold; the service already bounds them far below this.
const SETUP_REPORT_DETAILS_CHARS: usize = 64 * 1024;

#[tauri::command]
fn save_setup_report(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
    details: Option<String>,
) -> Result<String, String> {
    let app_dir = service
        .app_dir
        .lock()
        .map_err(|_| "service state is unavailable")?
        .clone()
        .unwrap_or(default_app_dir(&app)?);
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "the computer clock is before 1970")?
        .as_secs();
    let report = app_dir.join(format!("corsia-setup-report-{stamp}.txt"));
    let log_path = service_log_path(&app_dir)?;
    // Setup may have handled credentials, so the log is never copied whole: the details
    // are the service's own allowlisted lines about the failed rows (POST /api/setup/report).
    let details = details.unwrap_or_default();
    let details: String = details.chars().take(SETUP_REPORT_DETAILS_CHARS).collect();
    std::fs::write(
        &report,
        format!(
            "CORSIA setup report\n\nGenerated: {stamp}\nApp version: {}\nSetup did not complete.\n\n{details}\nFull service log: {}\n",
            app.package_info().version,
            log_path.display(),
        ),
    )
    .map_err(|error| format!("could not write the setup report: {error}"))?;
    let revealed = Command::new("open")
        .arg("-R")
        .arg(&report)
        .status()
        .map_err(|error| format!("could not show the setup report in Finder: {error}"))?;
    if !revealed.success() {
        return Err("could not show the setup report in Finder".into());
    }
    Ok(report.display().to_string())
}

/// Macos traffic-light positioning workaround.
///
/// `tauri.conf.json` `trafficLightPosition` flows through to Tao's
/// `ViewState.traffic_light_inset` correctly — but Tao only APPLIES the
/// inset from inside `NSView::drawRect:` (see
/// `tao-0.34.8/src/platform_impl/macos/view.rs::draw_rect`). With our
/// `transparent: true` + `windowEffects: ["sidebar"]` config the
/// NSVisualEffectView replaces the standard view's drawing path and
/// `drawRect:` doesn't fire on initial display, leaving the buttons at
/// system-default position. We replicate Tao's `inset_traffic_lights`
/// by manipulating the standardWindowButton frames directly. This must run
/// only while the native window controls are visible: changing the title-bar
/// container during fullscreen reintroduces a full-width native chrome strip.
#[cfg(target_os = "macos")]
fn inset_traffic_lights(window: &tauri::WebviewWindow, x: f64, y: f64) {
    use objc2::msg_send;
    use objc2_app_kit::{NSWindow, NSWindowButton};
    use objc2_foundation::NSRect;

    let ns_window_ptr = match window.ns_window() {
        Ok(p) => p,
        Err(_) => return,
    };
    if ns_window_ptr.is_null() {
        return;
    }

    unsafe {
        let ns_window: &NSWindow = &*(ns_window_ptr as *const NSWindow);

        let close = match ns_window.standardWindowButton(NSWindowButton::CloseButton) {
            Some(b) => b,
            None => return,
        };
        let miniaturize = match ns_window.standardWindowButton(NSWindowButton::MiniaturizeButton) {
            Some(b) => b,
            None => return,
        };
        let zoom = match ns_window.standardWindowButton(NSWindowButton::ZoomButton) {
            Some(b) => b,
            None => return,
        };

        let close_frame: NSRect = msg_send![&*close, frame];
        let mini_frame: NSRect = msg_send![&*miniaturize, frame];
        let space_between = mini_frame.origin.x - close_frame.origin.x;

        let win_frame: NSRect = msg_send![ns_window, frame];

        // Reframe the title-bar container so it grows down to fit the
        // lowered buttons (matches Tao's algorithm). The new height is
        // close_h + y, so the bottom edge of the title bar (in Cocoa
        // bottom-up coords) lines up with the bottom edge of the buttons.
        if let Some(close_super) = close.superview() {
            if let Some(title_bar) = close_super.superview() {
                let mut tb_rect: NSRect = msg_send![&*title_bar, frame];
                tb_rect.size.height = close_frame.size.height + y;
                tb_rect.origin.y = win_frame.size.height - tb_rect.size.height;
                let _: () = msg_send![&*title_bar, setFrame: tb_rect];
            }
        }

        // Position each button. Tao's reference algorithm only sets x,
        // relying on drawRect: to re-center buttons in the resized
        // title bar — but drawRect: doesn't fire for transparent +
        // windowEffects windows, so we also pin origin.y to 0. The y
        // param is the inset above the button row inside the resized
        // title bar; with the 60px chat header and 32px shell-btn
        // icons centered at window-y=30, y=23 lines up the 12px traffic
        // lights' centers at window-y≈29.5 (verified pixel-precise via
        // screencapture + PIL). Re-tune by ±1 if the header height
        // changes.
        for (i, button) in [&close, &miniaturize, &zoom].iter().enumerate() {
            let mut frame: NSRect = msg_send![&***button, frame];
            frame.origin.x = x + (i as f64 * space_between);
            frame.origin.y = 0.0;
            let _: () = msg_send![&***button, setFrameOrigin: frame.origin];
        }
    }
}

/// Synchronize the browser's chrome layout with the native window mode.
/// The Tauri title-bar area environment variable is unavailable on the
/// externally served app, so normal-window traffic-light clearance must be
/// explicit. The same authoritative state prevents native title-bar mutation
/// after fullscreen hides those controls.
#[cfg(target_os = "macos")]
fn sync_window_chrome(window: &tauri::WebviewWindow) {
    let fullscreen = match window.is_fullscreen() {
        Ok(fullscreen) => fullscreen,
        Err(error) => {
            log::warn!("could not read native fullscreen state: {error}");
            return;
        }
    };
    let fullscreen_value = if fullscreen { "true" } else { "false" };
    if let Err(error) = window.eval(format!(
        "document.documentElement.dataset.nativeFullscreen = '{fullscreen_value}';"
    )) {
        log::warn!("could not synchronize native fullscreen state to the webview: {error}");
    }
    if !fullscreen {
        inset_traffic_lights(window, 23.0, 23.0);
    }
}

fn generated_context<R: tauri::Runtime>() -> tauri::Context<R> {
    tauri::generate_context!()
}

/// The shell's own log file in the app log folder (`~/Library/Logs/<identifier>/desktop-shell.log`). The background
/// service logs to its own `service.log` under the state folder.
const SHELL_LOG_FILE: &str = "desktop-shell";

/// Everything goes to stdout, as it always has. The shell's own lines (not its libraries') also go to `file`, so a
/// pairing or launch failure's cause outlives a packaged app's lost stdout. A log file that cannot be opened is
/// skipped rather than stopping the app from starting.
fn shell_logger<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    file: tauri_plugin_log::TargetKind,
) -> Result<(tauri::plugin::TauriPlugin<R>, log::LevelFilter, Box<dyn log::Log>), tauri_plugin_log::Error> {
    let stdout = || tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::Stdout);
    let shell_lines = tauri_plugin_log::Target::new(file).filter(|metadata| is_shell_log_target(metadata.target()));
    tauri_plugin_log::Builder::default()
        .clear_targets()
        .targets([stdout(), shell_lines])
        .split(app)
        .or_else(|error| {
            eprintln!("the shell log file could not be opened, so the shell logs to stdout only: {error}");
            tauri_plugin_log::Builder::default().clear_targets().target(stdout()).split(app)
        })
}

fn is_shell_log_target(target: &str) -> bool {
    let shell = module_path!();
    target == shell || target.strip_prefix(shell).is_some_and(|rest| rest.starts_with("::"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Every command-line mode is dispatched here, before any window exists.
    let arguments = env::args_os().skip(1).collect::<Vec<_>>();
    let mode = match command_line_mode(&arguments) {
        Ok(mode) => mode,
        Err(error) => {
            eprintln!("usage error: {error}");
            std::process::exit(update_stage::USAGE_ERROR);
        }
    };
    match &mode {
        Some(CommandLineMode::Service) => std::process::exit(
            service_management::command_from_environment().unwrap_or(update_stage::USAGE_ERROR),
        ),
        Some(CommandLineMode::UpdateSwap(operation_id)) => {
            std::process::exit(update_swap::run(operation_id))
        }
        Some(CommandLineMode::UpdateRollback(operation_id)) => {
            std::process::exit(update_swap::rollback(operation_id))
        }
        Some(CommandLineMode::UpdateStage(_)) | None => {}
    }
    let context = generated_context();
    if let Some(CommandLineMode::UpdateStage(operation_id)) = mode {
        std::process::exit(update_stage::run(context, operation_id));
    }
    // Only the window reads it; every command-line mode has already exited.
    let relaunched_screen = relaunched_update_screen(&arguments);

    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        // Remembers the window's size and position; `window_placement` covers what it leaves to the OS.
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .invoke_handler(tauri::generate_handler![
            start_local_service,
            read_startup_progress,
            adopt_existing_install,
            start_existing_again,
            start_fresh,
            start_fresh_preview,
            start_source_install,
            restart_source_install,
            reopen_app,
            quit_app,
            check_service_started,
            adoption_progress,
            remote_discovered,
            remote_pair,
            disconnect_remote,
            retry_saved_remote,
            choose_work_folder,
            configure_work_folder,
            lifecycle::retry_quit,
            open_login_items,
            get_service_log_path,
            save_setup_report,
            start_window_drag,
            finish_setup_window,
        ])
        .on_page_load(|webview, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) && is_shell_origin(payload.url().as_str()) {
            }
            // Inject the drag handler on every page load. Idempotent —
            // the script self-guards via __corsia_drag_handler_installed
            // so multiple injections don't double-bind.
            let finished = matches!(payload.event(), tauri::webview::PageLoadEvent::Finished);
            let home = webview.app_handle().path().home_dir().ok();
            let (shell_context, startup_state) = webview
                .app_handle()
                .state::<RemoteRuntime>()
                .shell
                .lock()
                .map(|mut connection| page_load_state(&mut connection, payload.url().as_str(), finished, home.as_deref()))
                .unwrap_or_else(|_| (local_shell_context(home.as_deref(), false), None));
            let show_state = startup_state.unwrap_or_default();
            let _ = webview.eval(format!("window.__CORSIA_SHELL__ = {shell_context};{DRAG_INIT_SCRIPT}{show_state}"));
            // Re-apply traffic-light position after each navigation —
            // the startup-shell → local-app redirect rebuilds the view tree
            // and can reset button positions on some macOS versions.
            #[cfg(target_os = "macos")]
            if let Some(window) = webview.window().get_webview_window(webview.label()) {
                sync_window_chrome(&window);
            }
        })
        .on_menu_event(|app, event| {
            if event.id().as_ref() == lifecycle::QUIT_MENU_ID
                && lifecycle::route(lifecycle::Input::Quit, false) == lifecycle::Action::CheckQuit
            {
                lifecycle::request_quit(app);
            }
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    if lifecycle::route(lifecycle::Input::CloseMain, false) == lifecycle::Action::Hide {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            }
            // Keep the DOM's native fullscreen state and normal-window control
            // frames synchronized after focus and resize transitions.
            #[cfg(target_os = "macos")]
            {
                use tauri::WindowEvent;
                if matches!(event, WindowEvent::Focused(_) | WindowEvent::Resized(_)) {
                    if let Some(wv) = window.get_webview_window(window.label()) {
                        sync_window_chrome(&wv);
                    }
                }
            }
        })
        .setup(|app| {
            let log_file = tauri_plugin_log::TargetKind::LogDir { file_name: Some(SHELL_LOG_FILE.into()) };
            let (log_plugin, level, logger) = shell_logger(app.handle(), log_file)?;
            app.handle().plugin(log_plugin)?;
            tauri_plugin_log::attach_logger(level, logger)?;
            let start_page = relaunched_start_page(relaunched_screen.as_deref());
            app.manage(AppServiceState { relaunched_screen: Mutex::new(relaunched_screen), ..AppServiceState::default() });
            app.manage(RemoteRuntime::default());
            watch_for_swapped_app(app.handle().clone());
            match app.path().app_config_dir() {
                Ok(config_dir) => window_placement::place_main_window(app, &config_dir),
                Err(error) => log::warn!("the window's saved place could not be read: {error}"),
            }
            #[cfg(target_os = "macos")]
            app.set_menu(lifecycle::menu(app.handle())?)?;
            // Apply traffic-light position once the main window is up.
            // The config-driven path is unreliable for our transparent +
            // windowEffects setup (see inset_traffic_lights doc comment).
            #[cfg(target_os = "macos")]
            {
                use tauri::Manager;
                if let Some(window) = app.get_webview_window("main") {
                    sync_window_chrome(&window);
                }
            }
            // A saved remote owns launch: do not register or start the local
            // service, whose login item was intentionally removed on pairing.
            if let Ok(app_dir) = default_app_dir(app.handle()) {
                if let Ok(Some(saved)) = remote_config::load_remote(&app_dir) {
                    let local_configured = local_corsia_configured(app.handle(), &app_dir);
                    if let Ok(mut connection) = app.state::<RemoteRuntime>().shell.lock() {
                        *connection = shell_state::ShellState::launch(Some(&saved));
                        connection.set_local_corsia_configured(local_configured);
                    }
                    let app_handle = app.handle().clone();
                    tauri::async_runtime::spawn(async move { connect_saved_remote(app_handle, saved).await; });
                    return Ok(());
                }
            }
            // A reopened app's handed-over update screen goes on drawing on the
            // shell's update page while the same launch start runs below; that
            // page follows its Ready answer and keeps any other on the screen.
            if let (Some(page), Some(window)) = (start_page, app.get_webview_window("main")) {
                if let Err(error) = window.navigate(page) {
                    log::error!("could not show the update screen the replaced app handed over: {error}");
                }
            }
            // The default window loads the static startup shell while Tauri
            // owns the first service launch. Retrying after a visible failure
            // stays available through start_local_service.
            let app_handle = app.handle().clone();
            thread::spawn(move || {
                let service = app_handle.state::<AppServiceState>();
                let outcome = with_relaunched_screen(&service.relaunched_screen, start_service(&app_handle, &service));
                show_launch_outcome(&app_handle, &outcome);
            });
            Ok(())
        })
        .build(context)
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            match event {
                #[cfg(target_os = "macos")]
                tauri::RunEvent::Reopen { .. } => {
                    if lifecycle::route(lifecycle::Input::Reopen, false) == lifecycle::Action::ShowAndFocus {
                        lifecycle::show_main(app_handle);
                    }
                }
                tauri::RunEvent::Exit => lifecycle::on_exit(app_handle),
                tauri::RunEvent::ExitRequested { api, code, .. } => {
                    let quitting = app_handle.state::<AppServiceState>().quitting.load(Ordering::Relaxed);
                    match lifecycle::route(lifecycle::Input::ExitRequested(code), quitting) {
                        lifecycle::Action::KeepAlive => api.prevent_exit(),
                        lifecycle::Action::CheckQuit => {
                            api.prevent_exit();
                            lifecycle::request_quit(app_handle);
                        }
                        _ => {}
                    }
                }
                _ => {}
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unfinished_local_setup_is_not_configured_for_remote_return() {
        let home = tempfile::tempdir().unwrap();
        let app_dir = home.path().join("app");
        let workspace = home.path().join("workspace");
        assert!(unfinished_setup::mark(&app_dir, &workspace).unwrap());
        std::fs::write(app_dir.join(first_run::SERVICE_FILE), "{}").unwrap();
        assert!(matches!(unfinished_setup::at_launch(&app_dir), unfinished_setup::Launch::Wipe(_)));
        assert!(matches!(first_run::plan(home.path(), &app_dir), Ok(first_run::FirstRun::Configured)));
        assert!(!local_plan_configured(home.path(), &app_dir));
    }
    use base64::Engine;
    use std::fs;

    fn closed_port() -> u16 {
        TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
    }

    fn write_progress(file: &Path, pid: u32, seq: u64, phase: &str) {
        let staged = file.with_extension("tmp");
        fs::write(
            &staged,
            format!(r#"{{"schema": 1, "pid": {pid}, "seq": {seq}, "phase": "{phase}", "done": null, "total": null}}"#),
        )
        .unwrap();
        fs::rename(staged, file).unwrap();
    }

    /// Waits on this process as the service, with a 300 ms stall and the cap given.
    fn wait_on_progress(file: &Path, cap: Duration) -> (Result<(), StartupFailure>, Duration) {
        let began = Instant::now();
        let outcome = wait_for_startup(
            closed_port(),
            None,
            || Ok(Some(std::process::id())),
            Duration::from_secs(30),
            cap,
            Some(ProgressWatch { file, stall: Duration::from_millis(300) }),
        );
        (outcome, began.elapsed())
    }

    /// Rewrites the progress file with a growing `seq` until the wait returns.
    fn keep_beating(file: PathBuf, pid: u32) -> (std::sync::Arc<AtomicBool>, thread::JoinHandle<()>) {
        let done = std::sync::Arc::new(AtomicBool::new(false));
        let stop = done.clone();
        let beater = thread::spawn(move || {
            let mut seq = 1;
            while !stop.load(Ordering::Relaxed) {
                write_progress(&file, pid, seq, "migrating");
                seq += 1;
                thread::sleep(Duration::from_millis(50));
            }
        });
        (done, beater)
    }

    #[test]
    fn startup_progress_returns_only_the_current_pid_and_redacts_diagnostics() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("startup-progress.json");
        fs::write(
            &file,
            r#"{"schema":1,"pid":42,"seq":7,"phase":"migrating","done":3,"total":9,"from_version":48,"to_version":49,"error":"private diagnostic"}"#,
        )
        .unwrap();

        assert_eq!(current_startup_progress(&file, Some(41)), None, "stale PID is ignored");
        let current = current_startup_progress(&file, Some(42)).unwrap();
        assert_eq!(current, StartupProgress { schema: 1, pid: 42, seq: 7, phase: "migrating".into(), done: Some(3), total: Some(9) });
        let response = serde_json::to_value(current).unwrap();
        assert!(response.get("error").is_none());
        assert!(response.get("from_version").is_none());
        assert!(response.get("to_version").is_none());
    }

    #[test]
    fn a_progress_reading_without_an_address_is_the_bare_record() {
        let progress = StartupProgress { schema: 1, pid: 42, seq: 7, phase: "migrating".into(), done: Some(3), total: None };
        let bare = serde_json::to_value(&progress).unwrap();
        let reading = ProgressReading { progress: Some(progress), ready_url: None };
        assert_eq!(serde_json::to_value(&reading).unwrap(), bare);
        let ready = ProgressReading { progress: None, ready_url: Some(service_url(47_895)) };
        assert_eq!(serde_json::to_value(&ready).unwrap(), serde_json::json!({ "ready_url": "http://127.0.0.1:47895" }));
    }

    #[test]
    fn only_the_shell_update_page_is_told_the_service_is_back() {
        assert!(is_shell_update_page("tauri://localhost/update.html"));
        assert!(is_shell_update_page("tauri://localhost/update.html#corsia-update=x"));
        for other in [
            "tauri://localhost/index.html", "tauri://localhost", "http://127.0.0.1:47895/update.html",
            "tauri://evil.example/update.html", "tauri://localhost/update.html/x", "",
        ] {
            assert!(!is_shell_update_page(other), "{other}");
        }
    }

    #[test]
    fn the_update_page_hears_ready_only_from_a_served_local_healthy_and_idle_service() {
        let guard = AtomicBool::new(false);
        let probes = std::cell::Cell::new(0);
        let healthy = |answer: bool| {
            let probes = &probes;
            move |_: u16| {
                probes.set(probes.get() + 1);
                answer
            }
        };
        let ready = |served: bool, local: bool, healthy: &dyn Fn(u16) -> bool, active: &dyn Fn(u16) -> Result<bool, String>| {
            update_page_ready_url(&guard, served, local, 47_895, healthy, active, || true)
        };
        assert_eq!(ready(true, true, &healthy(true), &|_| Ok(false)), Some(service_url(47_895)));
        // The update lock is asked last, and only of a healthy, idle service: it is never held through a health wait.
        assert_eq!(update_page_ready_url(&guard, true, true, 47_895, |_| false, |_| Ok(false), || panic!("asked the lock of a down service")), None);
        assert_eq!(update_page_ready_url(&guard, true, true, 47_895, |_| true, |_| Ok(true), || panic!("asked the lock of a busy service")), None);
        assert_eq!(update_page_ready_url(&guard, true, true, 47_895, |_| true, |_| Ok(false), || false), None);
        // A busy update or restart, or an answer that can't be read, is never ready.
        assert_eq!(ready(true, true, &healthy(true), &|_| Ok(true)), None);
        assert_eq!(ready(true, true, &healthy(true), &|_| Err("unreadable".into())), None);
        assert_eq!(ready(true, true, &healthy(false), &|_| panic!("asked a down service")), None);
        probes.set(0);
        // A port this process never served, or a remote or pairing window, isn't even probed.
        assert_eq!(ready(false, true, &healthy(true), &|_| Ok(false)), None);
        assert_eq!(ready(true, false, &healthy(true), &|_| Ok(false)), None);
        assert_eq!(probes.get(), 0);
        assert!(!guard.load(Ordering::Acquire), "a check left the guard set");
    }

    #[test]
    fn a_held_update_lock_keeps_the_update_page_waiting_even_after_a_terminal_row() {
        if !app_paths::tests::alone("tests::a_held_update_lock_keeps_the_update_page_waiting_even_after_a_terminal_row") {
            return;
        }
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        std::fs::create_dir_all(&state).unwrap();
        let guard = AtomicBool::new(false);
        // The service answers healthy and its row is terminal (no update in flight), as a rollback records its failure
        // before it stops the service and puts the old app back, still holding the update lock.
        let ready = || update_page_ready_url(&guard, true, true, 47_895, |_| true, |_| Ok(false), || update_lock_is_free(&state));
        let rollback = app_paths::try_lock_update_in_existing(&state).unwrap().expect("the rollback's lock");
        assert_eq!(ready(), None, "a held update lock was called ready");
        assert_eq!(ready(), None);
        drop(rollback);
        assert_eq!(ready(), Some(service_url(47_895)));
        // The check held the lock only for its own instant.
        assert!(app_paths::try_lock_update_in_existing(&state).unwrap().is_some());
        // A state folder that can't be read is never free.
        assert!(!update_lock_is_free(&temp.path().join("missing")));
    }

    #[test]
    fn overlapping_readiness_checks_never_stack_their_health_waits() {
        let guard = AtomicBool::new(false);
        let inner = std::cell::Cell::new(None);
        let outer = update_page_ready_url(
            &guard,
            true,
            true,
            47_895,
            |port| {
                inner.set(Some(update_page_ready_url(&guard, true, true, port, |_| panic!("a second health wait ran"), |_| Ok(false), || true)));
                true
            },
            |_| Ok(false),
            || true,
        );
        assert_eq!(inner.take(), Some(None), "the overlapping reading waited");
        assert_eq!(outer, Some(service_url(47_895)));
        // The guard is released after each check.
        assert_eq!(update_page_ready_url(&guard, true, true, 1, |_| true, |_| Ok(false), || true), Some(service_url(1)));
    }

    #[test]
    fn a_startup_that_keeps_progressing_waits_past_the_stall_until_the_ceiling() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("startup-progress.json");
        let (done, beater) = keep_beating(file.clone(), std::process::id());
        let (outcome, elapsed) = wait_on_progress(&file, Duration::from_millis(1_200));
        done.store(true, Ordering::Relaxed);
        beater.join().unwrap();
        assert_eq!(outcome.unwrap_err(), StartupFailure::TimedOut { refusal: None });
        assert!(elapsed >= Duration::from_millis(1_200));
    }

    #[test]
    fn a_startup_whose_progress_stops_fails_at_the_stall() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("startup-progress.json");
        write_progress(&file, std::process::id(), 7, "migrating");
        let (outcome, elapsed) = wait_on_progress(&file, Duration::from_secs(10));
        assert_eq!(outcome.unwrap_err(), StartupFailure::TimedOut { refusal: None });
        assert!(elapsed >= Duration::from_millis(300) && elapsed < Duration::from_secs(3));
    }

    #[test]
    fn progress_from_another_process_does_not_count() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("startup-progress.json");
        let (done, beater) = keep_beating(file.clone(), std::process::id() + 1);
        let (outcome, elapsed) = wait_on_progress(&file, Duration::from_secs(10));
        done.store(true, Ordering::Relaxed);
        beater.join().unwrap();
        assert_eq!(outcome.unwrap_err(), StartupFailure::TimedOut { refusal: None });
        assert!(elapsed < Duration::from_secs(3));
    }

    #[test]
    fn a_startup_that_reports_failed_ends_the_wait_at_once() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("startup-progress.json");
        write_progress(&file, std::process::id(), 3, "failed");
        let (outcome, elapsed) = wait_on_progress(&file, Duration::from_secs(10));
        assert_eq!(outcome.unwrap_err(), StartupFailure::Exited { refusal: None });
        assert!(elapsed < Duration::from_millis(300));
    }

    #[test]
    fn the_startup_ceiling_grows_a_minute_per_gib_past_fifteen_minutes() {
        const GIB: u64 = 1 << 30;
        assert_eq!(startup_ceiling(0), Duration::from_secs(900));
        assert_eq!(startup_ceiling(15 * GIB), Duration::from_secs(900));
        assert_eq!(startup_ceiling(15 * GIB + 1), Duration::from_secs(960));
        assert_eq!(startup_ceiling(24 * GIB), Duration::from_secs(1_440));
        assert_eq!(startup_ceiling(100 * GIB), Duration::from_secs(6_000));
    }

    const SHELL_COMMANDS: &[&str] = &[
        "start_local_service",
        "read_startup_progress",
        "adopt_existing_install",
        "adoption_progress",
        "check_service_started",
        "quit_app",
        "reopen_app",
        "restart_source_install",
        "start_existing_again",
        "start_fresh",
        "start_fresh_preview",
        "start_source_install",
        "remote_discovered",
        "remote_pair",
        "disconnect_remote",
        "retry_saved_remote",
        "choose_work_folder",
        "configure_work_folder",
        "finish_setup_window",
        "retry_quit",
        "open_login_items",
        "get_service_log_path",
        "save_setup_report",
        "start_window_drag",
    ];

    fn acl_test_app() -> tauri::App<tauri::test::MockRuntime> {
        tauri::test::mock_builder()
            .invoke_handler(|invoke| {
                if SHELL_COMMANDS.contains(&invoke.message.command()) {
                    invoke.resolver.resolve(serde_json::Value::Null);
                    true
                } else {
                    false
                }
            })
            .build(generated_context())
            .unwrap()
    }

    fn invoke_from(webview: &tauri::WebviewWindow<tauri::test::MockRuntime>, command: &str, url: &str) -> Result<tauri::ipc::InvokeResponseBody, serde_json::Value> {
        tauri::test::get_ipc_response(webview, tauri::webview::InvokeRequest {
            cmd: command.into(),
            callback: tauri::ipc::CallbackFn(0),
            error: tauri::ipc::CallbackFn(1),
            url: url.parse().unwrap(),
            body: tauri::ipc::InvokeBody::default(),
            headers: Default::default(),
            invoke_key: tauri::test::INVOKE_KEY.to_string(),
        })
    }

    #[test]
    fn remote_only_commands_are_refused_from_remote_and_non_shell_origins() {
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        for command in ["disconnect_remote", "start_window_drag"] {
            for origin in ["https://studio.example", "https://untrusted.example"] {
                let refusal = invoke_from(&webview, command, origin).unwrap_err();
                assert!(refusal.to_string().contains("not allowed"), "{command} was not ACL-refused from {origin}: {refusal}");
            }
        }
    }

    #[test]
    fn info_plist_declares_the_local_network_prompt_and_the_bonjour_service() {
        let plist = include_str!("../Info.plist");
        assert!(plist.contains("<key>NSLocalNetworkUsageDescription</key>"));
        assert!(plist.contains("<key>NSBonjourServices</key>"));
        assert!(plist.contains("<string>_corsia._tcp</string>"));
    }

    #[test]
    fn no_app_command_is_allowed_from_a_foreign_http_or_https_origin() {
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        for command in SHELL_COMMANDS {
            for origin in ["https://studio.example", "http://studio.example", "https://127.0.0.1:47911", "http://127.0.0.1:47911"] {
                let refusal = invoke_from(&webview, command, origin).unwrap_err();
                assert!(refusal.to_string().contains("not allowed"), "{command} was not ACL-refused from {origin}: {refusal}");
            }
        }
    }

    #[test]
    fn remote_only_commands_reach_their_handlers_from_the_shell_origin() {
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        assert!(invoke_from(&webview, "disconnect_remote", "tauri://localhost").is_ok());
        assert!(invoke_from(&webview, "start_window_drag", "tauri://localhost").is_ok());
    }

    #[test]
    fn runtime_proxy_capability_allows_only_its_exact_origin_and_commands() {
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        let proxy_origin = "http://127.0.0.1:47911";
        grant_remote_proxy_origin(app.handle(), 47911).unwrap();

        for command in ["disconnect_remote", "start_window_drag"] {
            assert!(invoke_from(&webview, command, proxy_origin).is_ok(), "{command} was refused from the granted proxy origin");
            assert!(invoke_from(&webview, command, "http://127.0.0.1:47912").is_err(), "{command} was allowed from an ungranted proxy port");
        }
        for command in SHELL_COMMANDS {
            if ["disconnect_remote", "start_window_drag"].contains(command) {
                continue;
            }
            assert!(invoke_from(&webview, command, proxy_origin).is_err(), "{command} was allowed from the proxy origin");
        }
        let command = "plugin:opener|open_url";
        let error = invoke_from(&webview, command, proxy_origin).unwrap_err();
        assert!(!error.to_string().contains("not allowed"), "opener was ACL-refused: {error}");
        assert!(invoke_from(&webview, command, "http://127.0.0.1:47912").unwrap_err().to_string().contains("not allowed"));
    }

    #[test]
    fn discovery_policy_denial_is_reported_to_the_remote_connect_ui() {
        assert!(discovery_policy_denied(&discovery::DiscoveryError::Service(-65_570)));
        assert!(!discovery_policy_denied(&discovery::DiscoveryError::Service(-65_571)));
        assert!(!discovery_policy_denied(&discovery::DiscoveryError::Timeout));
    }

    #[test]
    fn opener_allows_only_the_exact_local_network_system_settings_url() {
        let capability: serde_json::Value = serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
        let opener = capability["permissions"].as_array().unwrap().iter().find(|permission| permission["identifier"] == "opener:allow-open-url").unwrap();
        assert_eq!(opener["allow"], serde_json::json!([
            { "url": "http://*" },
            { "url": "https://*" },
            { "url": "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork" }
        ]));
    }

    #[test]
    fn shell_context_is_remote_only_after_the_connection_is_live() {
        let saved = remote_config::RemoteRecord {
            locator: remote_config::Locator::Address { host: "192.168.1.20".into(), port: 47901 },
            trust: remote_config::RemoteTrust::System,
            name: "Studio".into(),
            proxy_port: 47911,
            identity: None,
            outside: None,
        };
        let mut connection = shell_state::ShellState::launch(Some(&saved));
        let home = Path::new("/fixture");
        assert_eq!(remote_shell_context(&connection, Some(home)), serde_json::json!({ "mode": "local", "localConfigured": false, "default_workspace": { "path": "/fixture/CORSIA", "display": "~/CORSIA" } }));
        connection.connected("Studio");
        assert_eq!(remote_shell_context(&connection, Some(home)), serde_json::json!({ "mode": "remote", "name": "Studio", "localConfigured": false }));
        connection.set_local_corsia_configured(true);
        assert_eq!(remote_shell_context(&connection, Some(home)), serde_json::json!({ "mode": "remote", "name": "Studio", "localConfigured": true }));
        connection.failed("Studio", classify::ConnectFailure::Network);
        assert_eq!(remote_shell_context(&connection, Some(home)), serde_json::json!({ "mode": "local", "localConfigured": true, "default_workspace": { "path": "/fixture/CORSIA", "display": "~/CORSIA" } }));
    }

    #[test]
    fn disconnect_accepts_only_the_current_saved_proxy_origin() {
        let remote = remote_config::RemoteRecord {
            locator: remote_config::Locator::Address { host: "192.168.1.20".into(), port: 47901 },
            trust: remote_config::RemoteTrust::System,
            name: "Studio".into(),
            proxy_port: 47911,
            identity: None,
            outside: None,
        };
        assert!(disconnect_origin_allows(Some(&remote), "tauri://localhost"));
        assert!(disconnect_origin_allows(Some(&remote), "http://127.0.0.1:47911"));
        assert!(!disconnect_origin_allows(Some(&remote), "http://127.0.0.1:47912"));
        assert!(!disconnect_origin_allows(Some(&remote), "https://127.0.0.1:47911"));
        assert!(!disconnect_origin_allows(Some(&remote), "http://127.0.0.1:47895"));
        assert!(!disconnect_origin_allows(None, "http://127.0.0.1:47911"));
    }


    #[test]
    fn disconnect_also_accepts_the_startup_shell_so_its_launch_failures_can_forget_the_remote() {
        assert!(is_shell_origin("tauri://localhost"));
        assert!(is_shell_origin("tauri://localhost/index.html"));
        for other in ["http://127.0.0.1:47911", "https://localhost", "tauri://studio.example", "http://tauri.localhost", "not a url", ""] {
            assert!(!is_shell_origin(other), "{other} was taken for the startup shell");
        }
    }

    /// The commands the startup shell page calls by name: invoke with a literal, optional-call invoke, and startLocal.
    fn shell_page_commands() -> std::collections::BTreeSet<&'static str> {
        let page = include_str!("../../dist-shell/index.html");
        let mut commands = std::collections::BTreeSet::new();
        for call in ["invoke('", "invoke?.('", "startLocal('"] {
            for part in page.split(call).skip(1) {
                commands.insert(&part[..part.find('\'').unwrap()]);
            }
        }
        commands
    }

    #[test]
    fn every_command_the_startup_shell_page_calls_is_registered_and_granted_to_the_shell_origin() {
        let commands = shell_page_commands();
        for needed in ["start_local_service", "disconnect_remote", "retry_saved_remote"] {
            assert!(commands.contains(needed), "the startup shell no longer calls {needed}: {commands:?}");
        }
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        for command in commands {
            assert!(SHELL_COMMANDS.contains(&command), "{command} is not a registered app command");
            assert!(invoke_from(&webview, command, "tauri://localhost").is_ok(), "{command} is not granted to the startup shell");
        }
    }

    /// Every command the frontend calls by name through the shell bridge: `invoke('x'`, outside tests and design pages.
    fn frontend_commands() -> std::collections::BTreeSet<String> {
        fn walk(folder: &Path, found: &mut std::collections::BTreeSet<String>) {
            for entry in fs::read_dir(folder).unwrap() {
                let path = entry.unwrap().path();
                let name = path.file_name().unwrap().to_string_lossy().into_owned();
                if path.is_dir() {
                    if name != "_design" && name != "node_modules" {
                        walk(&path, found);
                    }
                } else if (name.ends_with(".ts") || name.ends_with(".svelte")) && !name.contains(".test.") {
                    let source = fs::read_to_string(&path).unwrap();
                    for part in source.split("invoke('").skip(1) {
                        found.insert(part[..part.find('\'').unwrap()].to_owned());
                    }
                }
            }
        }
        let mut found = std::collections::BTreeSet::new();
        walk(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../../frontend/src"), &mut found);
        found
    }

    /// The commands only the remote page calls (Settings > Disconnect): its own origin gets it, the local service's does not.
    const REMOTE_PAGE_ONLY: &[&str] = &["disconnect_remote"];

    /// The commands the frontend calls that only the startup shell's page may: the local service's page is refused.
    const SHELL_PAGE_ONLY: &[&str] = &["start_local_service"];

    fn service_origin_app(port: u16) -> (tauri::App<tauri::test::MockRuntime>, tauri::WebviewWindow<tauri::test::MockRuntime>) {
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        grant_local_service_origin(app.handle(), &AppServiceState::default(), port).unwrap();
        (app, webview)
    }

    #[test]
    fn the_local_service_origin_is_allowed_every_command_its_pages_call() {
        let called = frontend_commands();
        assert_eq!(
            called.iter().map(String::as_str).collect::<Vec<_>>(),
            ["choose_work_folder", "configure_work_folder", "disconnect_remote", "finish_setup_window", "read_startup_progress", "remote_discovered", "remote_pair", "save_setup_report", "start_local_service"],
            "the frontend's commands changed: grant each page its commands, or add it to the remote-page-only or shell-page-only list"
        );
        let (_app, webview) = service_origin_app(47895);
        let origin = service_url(47895);
        for command in called.iter().map(String::as_str).chain(["start_window_drag"]) {
            let allowed = invoke_from(&webview, command, &origin).is_ok();
            let refused = REMOTE_PAGE_ONLY.contains(&command) || SHELL_PAGE_ONLY.contains(&command);
            assert_eq!(allowed, !refused, "{command} from {origin}");
        }
    }

    #[derive(Clone, Copy, Debug)]
    enum ProofReply { Active, Inactive, Unknown, Unhealthy }

    fn proof_server(reply: ProofReply) -> (u16, std::sync::Arc<AtomicBool>, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let stopped = std::sync::Arc::new(AtomicBool::new(false));
        let stop = stopped.clone();
        let worker = thread::spawn(move || while !stop.load(Ordering::Acquire) {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let mut request = [0; 512];
                    let read = stream.read(&mut request).unwrap_or(0);
                    let operation = String::from_utf8_lossy(&request[..read]).contains("/api/system/operation-active");
                    let (status, body) = if operation {
                        match reply {
                            ProofReply::Active | ProofReply::Unhealthy => ("200 OK", r#"{"active":true}"#),
                            ProofReply::Inactive => ("200 OK", r#"{"active":false}"#),
                            ProofReply::Unknown => ("200 OK", "{}"),
                        }
                    } else if matches!(reply, ProofReply::Unhealthy) {
                        ("503 Service Unavailable", "")
                    } else {
                        ("200 OK", "")
                    };
                    let response = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                    let _ = stream.write_all(response.as_bytes());
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(1)),
                Err(error) => panic!("proof server failed: {error}"),
            }
        });
        (port, stopped, worker)
    }

    fn recorded_service(folder: &Path, state: &Path, port: u16) {
        fs::create_dir_all(folder).unwrap();
        fs::create_dir_all(state).unwrap();
        fs::write(folder.join("service.json"), format!(r#"{{"state_dir":"{}","port":{port}}}"#, state.display())).unwrap();
    }

    fn test_app() -> tauri::App<tauri::test::MockRuntime> {
        tauri::test::mock_builder().build(generated_context()).unwrap()
    }

    /// The held lock and server live in the parent test process. The child starts with a disposable HOME so MockRuntime
    /// resolves the production wrapper's default app folder there, while its bounded timeout prevents an old lock-first
    /// implementation from hanging the suite.
    fn alone_at_home(name: &str, home: &Path) -> bool {
        if std::env::var_os("CORSIA_START_SERVICE_ALONE").is_some() {
            return true;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", name, "--test-threads=1"])
            .env("CORSIA_START_SERVICE_ALONE", "1")
            .env("HOME", home)
            .env_remove(app_paths::UPDATE_LOCK_FD_VARIABLE)
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(output.status.success() && stdout.contains("1 passed"), "{stdout}{}", String::from_utf8_lossy(&output.stderr));
        false
    }

    #[test]
    fn a_healthy_active_service_attaches_before_the_held_update_lock_without_startup_mutation() {
        if std::env::var_os("CORSIA_START_SERVICE_ALONE").is_none() {
            let temp = tempfile::tempdir().unwrap();
            let app_dir = app_paths::app_dir_for_home(temp.path());
            let state = temp.path().join("state");
            let (port, stopped, server) = proof_server(ProofReply::Active);
            recorded_service(&app_dir, &state, port);
            let update = app_paths::try_lock_update(&state).unwrap().expect("the updater lock");
            assert!(!alone_at_home("tests::a_healthy_active_service_attaches_before_the_held_update_lock_without_startup_mutation", temp.path()));
            drop(update);
            stopped.store(true, Ordering::Release);
            server.join().unwrap();
            return;
        }
        let home = PathBuf::from(std::env::var_os("HOME").unwrap());
        let app_dir = app_paths::app_dir_for_home(&home);
        let state = home.join("state");
        let port = first_run::recorded_port(&app_dir).unwrap();
        let app = test_app();
        let service = std::sync::Arc::new(AppServiceState::default());
        let (sent, received) = std::sync::mpsc::channel();
        let starting = app.handle().clone();
        let state_for_start = service.clone();
        thread::spawn(move || { let _ = sent.send(start_service(&starting, &state_for_start)); });
        let outcome = received.recv_timeout(Duration::from_millis(300)).expect("healthy active service waited for update lock").unwrap();
        assert_eq!(outcome, ServiceStartup::Ready { url: service_url(port) });
        assert_eq!(*service.app_dir.lock().unwrap(), Some(app_dir));
        assert!(app_paths::try_lock_update_in_existing(&state).unwrap().is_none(), "attachment took the updater lock");
    }

    #[test]
    fn inactive_unknown_or_unhealthy_service_stays_excluded_while_the_update_lock_is_held() {
        if !app_paths::tests::alone("tests::inactive_unknown_or_unhealthy_service_stays_excluded_while_the_update_lock_is_held") {
            return;
        }
        let mut held = Vec::new();
        let mut roots = Vec::new();
        let mut apps = Vec::new();
        let mut servers = Vec::new();
        for reply in [ProofReply::Inactive, ProofReply::Unknown, ProofReply::Unhealthy] {
            let temp = tempfile::tempdir().unwrap();
            let app_dir = temp.path().join("app");
            let state = temp.path().join("state");
            let (port, stopped, server) = proof_server(reply);
            recorded_service(&app_dir, &state, port);
            held.push(app_paths::try_lock_update(&state).unwrap().expect("the updater lock"));
            let app = test_app();
            let service = std::sync::Arc::new(AppServiceState::default());
            let (sent, received) = std::sync::mpsc::channel();
            let starting = app.handle().clone();
            let state_for_start = service.clone();
            thread::spawn(move || { let _ = sent.send(start_service_in(&starting, &state_for_start, app_dir)); });
            assert!(received.recv_timeout(Duration::from_millis(300)).is_err(), "{reply:?} bypassed the update lock");
            assert!(service.app_dir.lock().unwrap().is_none(), "{reply:?} created a local session before lock admission");
            servers.push((stopped, server));
            roots.push(temp);
            apps.push(app);
        }
        for (stopped, server) in servers {
            stopped.store(true, Ordering::Release);
            server.join().unwrap();
        }
        assert_eq!((held.len(), roots.len(), apps.len()), (3, 3, 3));
    }

    #[test]
    fn recovery_without_an_initialized_session_uses_the_ordinary_canonical_folder() {
        let canonical = PathBuf::from("/Applications/CORSIA.app/Contents/Resources/corsia");
        assert_eq!(recovery_app_dir(None, || Ok(canonical.clone())).unwrap(), canonical);

        let initialized = PathBuf::from("/existing/corsia");
        assert_eq!(
            recovery_app_dir(Some(initialized.clone()), || Err("the default folder was read".into())).unwrap(),
            initialized
        );
    }

    #[test]
    fn only_the_startup_shell_can_start_the_local_service() {
        let (_app, webview) = service_origin_app(47895);
        for origin in [service_url(47895), "http://localhost:47895".into(), "http://127.0.0.1:47911".into()] {
            let refusal = invoke_from(&webview, "start_local_service", &origin).unwrap_err();
            assert!(refusal.to_string().contains("not allowed"), "start_local_service was not ACL-refused from {origin}: {refusal}");
        }
        assert!(invoke_from(&webview, "start_local_service", "tauri://localhost").is_ok());

        // The command refuses any other caller itself, before its first effect, should a grant ever widen.
        let start = include_str!("lib.rs").split("async fn start_local_service(").nth(1).unwrap();
        let refusal = start.find("local_start_refusal(").unwrap();
        assert!(refusal < start.find("local_start_began(").unwrap() && refusal < start.find("start_service(").unwrap());
        assert_eq!(local_start_refusal(Some("tauri://localhost")), None);
        assert_eq!(local_start_refusal(Some("tauri://localhost/index.html")), None);
        for caller in [Some("http://127.0.0.1:47895/"), Some("http://localhost:47895/"), Some("https://tauri.localhost/"), Some("tauri://evil.example"), None] {
            assert!(local_start_refusal(caller).is_some(), "{caller:?}");
        }
    }

    #[test]
    fn a_recovery_start_is_routed_after_the_caller_check_and_before_any_full_start_step() {
        let start = include_str!("lib.rs").split("async fn start_local_service(").nth(1).unwrap();
        let recovery = start.find("recover_local_service(").expect("the recovery route");
        assert!(start.find("local_start_refusal(").unwrap() < recovery);
        for full in ["local_start_began(", "start_service(", "local_start_answered("] {
            assert!(recovery < start.find(full).unwrap(), "{full} runs before the recovery route");
        }
        let route = include_str!("lib.rs").split("async fn recover_local_service(").nth(1).unwrap();
        let route = &route[..route.find("\n}\n").unwrap()];
        for forbidden in ["start_service(", "local_start_began(", "local_start_answered(", "show_launch_outcome(", "report_shell_state(", "register"] {
            assert!(!route.contains(forbidden), "the recovery route reaches {forbidden}");
        }
        assert!(route.contains("spawn_blocking"), "the recovery start runs on the window's event thread");
    }

    #[test]
    fn the_local_service_grant_is_for_that_exact_origin_and_those_commands_only() {
        let (_app, webview) = service_origin_app(47895);
        for command in SHELL_COMMANDS {
            let local = LOCAL_SERVICE_PERMISSIONS.contains(&format!("allow-{}", command.replace('_', "-")).as_str());
            assert_eq!(invoke_from(&webview, command, &service_url(47895)).is_ok(), local, "{command} from the local service origin");
            for other in ["http://127.0.0.1:47896", "http://localhost:47895", "https://127.0.0.1:47895", "http://studio.example:47895", "http://127.0.0.1"] {
                let refusal = invoke_from(&webview, command, other).unwrap_err();
                assert!(refusal.to_string().contains("not allowed"), "{command} was not ACL-refused from {other}: {refusal}");
            }
        }
    }

    #[test]
    fn every_local_ready_path_uses_the_shared_grant_step() {
        let source = include_str!("lib.rs");
        for function in ["fn attach_healthy_active_service<", "fn start_service_in<", "fn adopt_existing_install_blocking(", "async fn check_service_started("] {
            let body = source.split(function).nth(1).unwrap().split("\n}\n").next().unwrap();
            assert!(body.contains("local_service_ready("), "{function} does not use the shared ready step");
            assert!(!body.contains("ServiceStartup::Ready {"), "{function} bypasses the shared ready step");
        }
    }

    #[test]
    fn local_service_ready_grants_opener_to_its_exact_origin() {
        let app = acl_test_app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        let port = 47895;
        assert_eq!(local_service_ready(app.handle(), &AppServiceState::default(), port).unwrap(), ServiceStartup::Ready { url: service_url(port) });
        let command = "plugin:opener|open_url";
        let error = invoke_from(&webview, command, &service_url(port)).unwrap_err();
        assert!(!error.to_string().contains("not allowed"), "opener was ACL-refused: {error}");
        assert!(invoke_from(&webview, command, &service_url(port + 1)).unwrap_err().to_string().contains("not allowed"));
    }

    #[test]
    fn the_local_service_origin_can_open_http_links_but_no_other_remote_origin_can() {
        let (_app, webview) = service_origin_app(47895);
        let command = "plugin:opener|open_url";
        let local_error = invoke_from(&webview, command, &service_url(47895)).unwrap_err();
        assert!(
            !local_error.to_string().contains("not allowed"),
            "{command} was ACL-refused from the local service origin: {local_error}"
        );
        for other in ["http://127.0.0.1:47896", "http://localhost:47895", "https://127.0.0.1:47895", "http://studio.example:47895", "http://127.0.0.1"] {
            let refusal = invoke_from(&webview, command, other).unwrap_err();
            assert!(refusal.to_string().contains("not allowed"), "{command} was not ACL-refused from {other}: {refusal}");
        }
    }

    #[derive(Debug, serde::Deserialize)]
    struct TestOpenerScope { url: String }

    #[tauri::command]
    fn open_url(command_scope: tauri::ipc::CommandScope<TestOpenerScope>, url: String) -> bool {
        command_scope.allows().iter().any(|scope| scope.url.strip_suffix('*').is_some_and(|prefix| url.starts_with(prefix)))
    }

    #[test]
    fn local_and_proxy_opener_receive_http_scopes() {
        let plugin = tauri::plugin::Builder::<tauri::test::MockRuntime>::new("opener")
            .invoke_handler(tauri::generate_handler![open_url]).build();
        let app = tauri::test::mock_builder().plugin(plugin).build(generated_context()).unwrap();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        local_service_ready(app.handle(), &AppServiceState::default(), 47895).unwrap();
        grant_remote_proxy_origin(app.handle(), 47911).unwrap();
        for origin in [service_url(47895), service_url(47911)] {
            for (target, expected) in [("https://example.com", true), ("file:///etc/hosts", false)] {
                let response = tauri::test::get_ipc_response(&webview, tauri::webview::InvokeRequest {
                    cmd: "plugin:opener|open_url".into(), callback: tauri::ipc::CallbackFn(0), error: tauri::ipc::CallbackFn(1),
                    url: origin.parse().unwrap(), body: tauri::ipc::InvokeBody::Json(serde_json::json!({ "url": target })),
                    headers: Default::default(), invoke_key: tauri::test::INVOKE_KEY.to_string(),
                }).unwrap();
                assert_eq!(response.deserialize::<bool>().unwrap(), expected, "{target} from {origin}");
            }
        }
    }

    #[test]
    fn local_and_proxy_opener_reject_file_urls() {
        let app = tauri::test::mock_builder().plugin(tauri_plugin_opener::init()).build(generated_context()).unwrap();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        local_service_ready(app.handle(), &AppServiceState::default(), 47895).unwrap();
        grant_remote_proxy_origin(app.handle(), 47911).unwrap();
        for origin in [service_url(47895), service_url(47911)] {
            let result = tauri::test::get_ipc_response(&webview, tauri::webview::InvokeRequest {
                cmd: "plugin:opener|open_url".into(), callback: tauri::ipc::CallbackFn(0), error: tauri::ipc::CallbackFn(1),
                url: origin.parse().unwrap(), body: tauri::ipc::InvokeBody::Json(serde_json::json!({ "url": "file:///etc/hosts" })),
                headers: Default::default(), invoke_key: tauri::test::INVOKE_KEY.to_string(),
            });
            let error = result.unwrap_err().to_string();
            assert!(error.contains("Not allowed to open url file:///etc/hosts"), "unexpected refusal from {origin}: {error}");
        }
    }

    #[test]
    fn the_local_service_opener_scope_allows_only_http_and_https_urls() {
        assert_eq!(LOCAL_SERVICE_EXTERNAL_URL_SCOPES, ["http://*", "https://*"]);
        assert!(!LOCAL_SERVICE_EXTERNAL_URL_SCOPES.iter().any(|scope| scope.starts_with("file:") || scope.starts_with("javascript:")));
        assert_eq!(
            local_service_external_url_scopes(),
            LOCAL_SERVICE_EXTERNAL_URL_SCOPES
                .iter()
                .map(|url| serde_json::json!({ "url": url }))
                .collect::<Vec<_>>(),
            "the capability must receive the same URL scope that the allowlist declares"
        );
    }

    #[test]
    fn the_proxy_origin_keeps_only_its_two_commands_beside_the_local_service_grant() {
        let (app, webview) = service_origin_app(47895);
        grant_remote_proxy_origin(app.handle(), 47911).unwrap();
        for command in SHELL_COMMANDS {
            let allowed = invoke_from(&webview, command, "http://127.0.0.1:47911").is_ok();
            assert_eq!(allowed, ["disconnect_remote", "start_window_drag"].contains(command), "{command} from the proxy origin");
        }
    }

    /// The arguments the page sends `remote_pair` (`remoteShell.ts`; the frontend test pins the same file).
    fn remote_pair_requests() -> Vec<serde_json::Value> {
        serde_json::from_str(include_str!("../../../testdata/remote-pair-requests.json")).unwrap()
    }

    /// The real command, invoked over IPC with the page's own arguments: a refused argument shape rejects the call
    /// before any pairing runs, which the page can only show as a local failure.
    #[test]
    fn remote_pair_accepts_the_arguments_the_page_sends() {
        let app = tauri::test::mock_builder()
            .invoke_handler(tauri::generate_handler![remote_pair])
            .build(generated_context())
            .unwrap();
        app.manage(RemoteRuntime::default());
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        for request in remote_pair_requests() {
            serde_json::from_value::<RemoteTarget>(request["target"].clone()).unwrap();
            if request["target"]["kind"] != "address" {
                continue;
            }
            let response = tauri::test::get_ipc_response(&webview, tauri::webview::InvokeRequest {
                cmd: "remote_pair".into(),
                callback: tauri::ipc::CallbackFn(0),
                error: tauri::ipc::CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: tauri::ipc::InvokeBody::Json(request.clone()),
                headers: Default::default(),
                invoke_key: tauri::test::INVOKE_KEY.to_string(),
            })
            .unwrap_or_else(|error| panic!("remote_pair refused {request}: {error}"));
            // Nothing listens on port 1, so the pairing itself ran and answered with a connection failure.
            let response: serde_json::Value = response.deserialize().unwrap();
            assert_eq!(response["ok"], false, "{response}");
            assert_ne!(response["reason"], "local", "{response}");
        }
    }

    /// The only test that installs the process-wide logger: the real failure path's line lands in the shell log file,
    /// and a library's line stays on stdout.
    #[test]
    fn the_shell_log_file_keeps_a_pairing_failure_cause() {
        let app = tauri::test::mock_builder().build(generated_context()).unwrap();
        let logs = tempfile::tempdir().unwrap();
        let file = tauri_plugin_log::TargetKind::Folder { path: logs.path().into(), file_name: Some(SHELL_LOG_FILE.into()) };
        let (_, level, logger) = shell_logger(app.handle(), file).unwrap();
        tauri_plugin_log::attach_logger(level, logger).unwrap();

        pairing_failed("pair", pairing::PairError::WrongCode, remote_pair_error(pairing::PairError::WrongCode)).unwrap();
        log::warn!(target: "rustls::client", "a library line");
        log::logger().flush();

        let text = fs::read_to_string(logs.path().join("desktop-shell.log")).unwrap();
        assert!(text.contains("pairing failed at pair: WrongCode"), "{text}");
        assert!(!text.contains("a library line"), "{text}");
        // On an installed app the LogDir target resolves here.
        let installed = app.path().app_log_dir().unwrap();
        assert!(installed.ends_with("Library/Logs/ai.corsia.app"), "{}", installed.display());
    }

    #[test]
    fn an_unopenable_shell_log_file_leaves_stdout_logging() {
        let app = tauri::test::mock_builder().build(generated_context()).unwrap();
        let root = tempfile::tempdir().unwrap();
        let not_a_folder = root.path().join("logs");
        fs::write(&not_a_folder, "a file where the log folder belongs").unwrap();
        let file = tauri_plugin_log::TargetKind::Folder { path: not_a_folder, file_name: Some(SHELL_LOG_FILE.into()) };
        assert!(shell_logger(app.handle(), file).is_ok());
    }

    #[test]
    fn only_the_shells_own_lines_go_to_its_log_file() {
        assert!(is_shell_log_target("app_lib"));
        assert!(is_shell_log_target("app_lib::window_placement"));
        assert!(!is_shell_log_target("app_lib_other"));
        assert!(!is_shell_log_target("tauri::app"));
    }

    #[test]
    fn granting_the_local_service_origin_again_adds_nothing() {
        let app = acl_test_app();
        let service = AppServiceState::default();
        grant_local_service_origin(app.handle(), &service, 47895).unwrap();
        grant_local_service_origin(app.handle(), &service, 47895).unwrap();
        grant_local_service_origin(app.handle(), &service, 47896).unwrap();
        assert_eq!(*service.granted_local_ports.lock().unwrap(), std::collections::BTreeSet::from([47895, 47896]));
    }

    #[test]
    fn a_launch_outcome_reached_before_the_shell_page_loads_is_drawn_by_every_shell_load() {
        let app = launch_app();
        let refusal = "A file named ~/Corsia is in the way. Nothing was changed.";
        show_launch_outcome(app.handle(), &Err(refusal.into()));
        let failure = format!("window.__corsiaShowServiceFailure?.({});", serde_json::to_string(refusal).unwrap());
        assert_eq!(shell_state_script(&launch_state(&app)).as_deref(), Some(failure.as_str()));

        // A service already running answers Ready within milliseconds, before the page is up.
        let app = launch_app();
        show_launch_outcome(app.handle(), &Ok(ServiceStartup::Ready { url: service_url(47_895) }));
        assert_eq!(
            shell_state_script(&launch_state(&app)).as_deref(),
            Some(r#"window.__corsiaShowStartup?.({"state":"ready","url":"http://127.0.0.1:47895"});"#)
        );

        let app = launch_app();
        app.state::<RemoteRuntime>().shell.lock().unwrap().begin_pairing();
        show_launch_outcome(app.handle(), &Ok(ServiceStartup::RequiresApproval));
        assert_eq!(launch_state(&app), shell_state::LaunchState::Pairing, "a late launch outcome replaced a pairing");
    }

    #[test]
    fn a_ready_launch_answer_is_followed_once_and_never_replayed_by_a_later_shell_load() {
        let app = launch_app();
        show_launch_outcome(app.handle(), &Ok(ServiceStartup::Ready { url: service_url(47_895) }));
        let runtime = app.state::<RemoteRuntime>();
        let mut connection = runtime.shell.lock().unwrap();
        // The shell loads after the answer: it is drawn, and the window follows it to the service.
        let (_, script) = page_load_state(&mut connection, "tauri://localhost/index.html", true, Some(Path::new("/fixture")));
        assert!(script.is_some_and(|script| script.contains(r#""state":"ready""#)));
        page_load_state(&mut connection, "http://127.0.0.1:47895/", false, Some(Path::new("/fixture")));
        assert!(shell_state_script(connection.state()).is_some(), "a load still starting has not reached the service");
        page_load_state(&mut connection, "http://127.0.0.1:47895/", true, Some(Path::new("/fixture")));
        // A quit failure later sends the window back to the shell, which must stay on its own screen.
        let (_, script) = page_load_state(&mut connection, "tauri://localhost/index.html#quit-failure", true, Some(Path::new("/fixture")));
        assert_eq!(script, None);
    }

    #[test]
    fn a_start_the_window_runs_clears_the_launch_answer_so_a_reload_shows_no_stale_offer() {
        let app = launch_app();
        show_launch_outcome(app.handle(), &Ok(ServiceStartup::AdoptionOffered { workspace: "~/corsia".into() }));
        assert!(shell_state_script(&launch_state(&app)).is_some());
        local_start_began(app.handle());
        assert_eq!(launch_state(&app), shell_state::LaunchState::Local);
        assert_eq!(shell_state_script(&launch_state(&app)), None);
    }


    #[test]
    fn a_failure_card_survives_a_retry_that_fails_and_ends_when_a_start_answers() {
        for answer in [
            ServiceStartup::Ready { url: "http://127.0.0.1:47895".into() },
            ServiceStartup::RequiresApproval,
            ServiceStartup::AdoptionOffered { workspace: "~/corsia".into() },
        ] {
            let app = launch_app();
            show_shell_failure(app.handle(), "did not start");
            local_start_began(app.handle());
            let _ = local_start_answered(app.handle(), Err::<ServiceStartup, _>("did not start again".to_string()));
            assert_eq!(
                shell_state_script(&launch_state(&app)).as_deref(),
                Some("window.__corsiaShowServiceFailure?.(\"did not start\");"),
                "a reload after a failed retry must still draw the failure and its Retry"
            );
            assert_eq!(local_start_answered(app.handle(), Ok::<_, String>(answer.clone())), Ok(answer.clone()));
            assert_eq!(
                launch_state(&app),
                shell_state::LaunchState::Local,
                "the old failure would be drawn over the screen for {answer:?}"
            );
        }
    }

    #[test]
    fn every_exceptional_update_recovery_failure_is_retained_then_navigates_once_to_the_startup_shell() {
        let answers: [(Result<Result<update_recovery::RecoveryAnswer, String>, String>, &str); 2] = [
            (Ok(Err("the recovery state directory is unavailable".into())), "the recovery state directory is unavailable"),
            (Err("bringing CORSIA back did not finish: the recovery worker stopped".into()), "bringing CORSIA back did not finish: the recovery worker stopped"),
        ];
        for (answer, error) in answers {
            let app = launch_app();
            let window = app.get_webview_window("main").unwrap();
            window.navigate(tauri::Url::parse("tauri://localhost/update.html#corsia-update=fixture").unwrap()).unwrap();
            assert_eq!(finish_recovery(app.handle(), answer), Err(error.to_owned()));
            let script = shell_state_script(&launch_state(&app)).expect("the failure remains retained");
            assert_eq!(script, "window.__corsiaShowServiceFailure?.(\"\");");
            assert!(!script.contains(error), "the recovery diagnostic reached the retained script");
            assert_eq!(window.url().unwrap().as_str(), "tauri://localhost/index.html");
        }
    }

    #[test]
    fn a_disconnect_that_cannot_bring_the_local_service_back_returns_to_a_shell_card_with_an_action() {
        let app = launch_app();
        show_shell_failure(app.handle(), "CORSIA service registration did not persist");
        let connection = app.state::<RemoteRuntime>().shell.lock().unwrap().state().clone();
        assert_eq!(
            shell_state_script(&connection).as_deref(),
            Some("window.__corsiaShowServiceFailure?.(\"CORSIA service registration did not persist\");")
        );
        assert!(is_shell_origin(app.get_webview_window("main").unwrap().url().unwrap().as_str()), "the window did not return to the startup shell");
    }

    #[test]
    fn a_remote_disconnect_keeps_a_non_ready_startup_answer_for_the_startup_shell() {
        let app = launch_app();
        show_disconnected_startup(app.handle(), false, &ServiceStartup::RequiresApproval).unwrap();
        assert_eq!(launch_state(&app), shell_state::LaunchState::LocalStartup { startup: ServiceStartup::RequiresApproval });
        assert!(is_shell_origin(app.get_webview_window("main").unwrap().url().unwrap().as_str()), "the window did not return to the startup shell");
    }

    #[test]
    fn a_key_change_while_connected_records_the_failure_and_returns_to_the_startup_shell() {
        let app = launch_app();
        app.state::<RemoteRuntime>().shell.lock().unwrap().connected("Studio");
        key_changed_callback(app.handle(), "Studio")();
        let state = app.state::<RemoteRuntime>().shell.lock().unwrap().state().clone();
        assert_eq!(state, shell_state::LaunchState::Failed { name: "Studio".into(), failure: classify::ConnectFailure::KeyChanged });
        assert_eq!(remote_startup_state(&state), Some(serde_json::json!({ "kind": "key_changed", "name": "Studio" })));
        assert!(is_shell_origin(app.get_webview_window("main").unwrap().url().unwrap().as_str()));
    }

    fn saved_studio(locator: remote_config::Locator, trust: remote_config::RemoteTrust) -> remote_config::RemoteRecord {
        remote_config::RemoteRecord {
            locator,
            trust,
            name: "Studio".into(),
            proxy_port: 47911,
            identity: None,
            outside: None,
        }
    }

    fn launch_app() -> tauri::App<tauri::test::MockRuntime> {
        let app = tauri::test::mock_builder().build(generated_context()).unwrap();
        app.manage(RemoteRuntime::default());
        tauri::WebviewWindowBuilder::new(&app, "main", Default::default()).build().unwrap();
        app
    }

    fn launch_state(app: &tauri::App<tauri::test::MockRuntime>) -> shell_state::LaunchState {
        app.state::<RemoteRuntime>().shell.lock().unwrap().state().clone()
    }

    #[test]
    fn a_launch_that_cannot_use_the_saved_trust_ends_on_the_key_changed_card_not_on_connecting() {
        let app = launch_app();
        let saved = saved_studio(
            remote_config::Locator::Address { host: "127.0.0.1".into(), port: 1 },
            remote_config::RemoteTrust::Pinned { spki_sha256: "not base64".into() },
        );
        tauri::async_runtime::block_on(connect_saved_remote(app.handle().clone(), saved));
        assert_eq!(launch_state(&app), shell_state::LaunchState::Failed { name: "Studio".into(), failure: classify::ConnectFailure::KeyChanged });
    }

    #[test]
    fn a_launch_whose_host_key_differs_from_the_saved_pin_ends_on_the_key_changed_card_not_the_network_card() {
        let app = launch_app();
        let identity = test_tls::generated_self_signed_identity();
        let port = tauri::async_runtime::block_on(async {
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
            let port = listener.local_addr().unwrap().port();
            tauri::async_runtime::spawn(async move {
                // The client refuses the key mid-handshake, so the accept is expected to fail.
                if let Ok((stream, _)) = listener.accept().await {
                    let _ = tokio_rustls::TlsAcceptor::from(identity.config).accept(stream).await;
                }
            });
            port
        });
        let mut changed_pin = identity.pin;
        changed_pin[0] ^= 1;
        let saved = saved_studio(
            remote_config::Locator::Address { host: "127.0.0.1".into(), port },
            remote_config::RemoteTrust::from(&trust::Trust::Pinned { spki_sha256: changed_pin }),
        );
        tauri::async_runtime::block_on(connect_saved_remote(app.handle().clone(), saved));
        assert_eq!(launch_state(&app), shell_state::LaunchState::Failed { name: "Studio".into(), failure: classify::ConnectFailure::KeyChanged });
        assert!(app.state::<RemoteRuntime>().proxy.lock().unwrap().is_none());
    }

    #[test]
    fn outside_address_conversion_uses_https_system_trust_and_rejects_bad_values() {
        let route = outside_route(Some("home.example.com:8443"), true).unwrap();
        assert_eq!(route.url, "https://home.example.com:8443");
        assert_eq!(route.trust, remote_config::RemoteTrust::System);
        assert!(outside_route(Some("http://home.example.com"), true).is_none());
        assert!(outside_route(Some("home.example.com/path"), true).is_none());
        assert!(outside_route(Some("home.example.com"), false).is_none());
    }

    /// A saved record with an outside route at `outside_port`, pinned like its local route to `identity`.
    fn saved_with_outside(identity: [u8; 32], outside_port: u16) -> remote_config::RemoteRecord {
        let pinned = remote_config::RemoteTrust::from(&trust::Trust::Pinned { spki_sha256: identity });
        remote_config::RemoteRecord {
            proxy_port: 0,
            identity: Some(BASE64.encode(identity)),
            outside: Some(remote_config::OutsideRoute { url: format!("https://127.0.0.1:{outside_port}"), trust: pinned.clone() }),
            ..saved_studio(remote_config::Locator::Address { host: "127.0.0.1".into(), port: 1 }, pinned)
        }
    }

    #[test]
    fn a_launch_whose_outside_route_presents_another_key_ends_on_the_key_changed_card() {
        let app = launch_app();
        let relay = test_tls::generated_self_signed_identity();
        let port = tauri::async_runtime::block_on(async {
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
            let port = listener.local_addr().unwrap().port();
            tauri::async_runtime::spawn(async move {
                while let Ok((stream, _)) = listener.accept().await {
                    let _ = tokio_rustls::TlsAcceptor::from(std::sync::Arc::clone(&relay.config)).accept(stream).await;
                }
            });
            port
        });
        tauri::async_runtime::block_on(connect_saved_remote(app.handle().clone(), saved_with_outside([5; 32], port)));
        assert_eq!(launch_state(&app), shell_state::LaunchState::Failed { name: "Studio".into(), failure: classify::ConnectFailure::KeyChanged });
        assert!(app.state::<RemoteRuntime>().proxy.lock().unwrap().is_none());
    }

    #[test]
    fn a_launch_that_reaches_neither_route_ends_on_the_network_card() {
        let app = launch_app();
        tauri::async_runtime::block_on(connect_saved_remote(app.handle().clone(), saved_with_outside([5; 32], 1)));
        assert_eq!(launch_state(&app), shell_state::LaunchState::Failed { name: "Studio".into(), failure: classify::ConnectFailure::Network });
        assert!(app.state::<RemoteRuntime>().proxy.lock().unwrap().is_none());
    }

    #[test]
    fn a_pair_that_failed_after_the_code_was_used_reports_the_local_reason() {
        let RemotePairResponse::Failure { ok, reason, .. } = failure_response(classify::ConnectFailure::Local) else { panic!("a failure response") };
        assert!(!ok);
        assert_eq!(reason, "local");
    }

    #[test]
    fn a_host_key_that_changed_during_pairing_reports_the_local_reason_not_the_server_one() {
        let RemotePairResponse::Failure { ok, reason, .. } = remote_pair_error(pairing::PairError::KeyChanged) else { panic!("a failure response") };
        assert!(!ok);
        assert_eq!(reason, "local");
    }

    #[test]
    fn a_launch_that_gets_a_refused_connection_ends_on_the_server_card_not_on_connecting() {
        let app = launch_app();
        let identity = test_tls::generated_self_signed_identity();
        let pin = identity.pin;
        let port = tauri::async_runtime::block_on(async {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};

            let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
            let port = listener.local_addr().unwrap().port();
            tauri::async_runtime::spawn(async move {
                let (stream, _) = listener.accept().await.unwrap();
                let mut tls = tokio_rustls::TlsAcceptor::from(identity.config).accept(stream).await.unwrap();
                let mut request = [0; 1024];
                let _ = tls.read(&mut request).await.unwrap();
                tls.write_all(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n").await.unwrap();
            });
            port
        });
        let saved = saved_studio(
            remote_config::Locator::Address { host: "127.0.0.1".into(), port },
            remote_config::RemoteTrust::from(&trust::Trust::Pinned { spki_sha256: pin }),
        );
        tauri::async_runtime::block_on(connect_saved_remote(app.handle().clone(), saved));
        assert_eq!(
            launch_state(&app),
            shell_state::LaunchState::Failed { name: "Studio".into(), failure: classify::ConnectFailure::Server },
        );
        assert!(app.state::<RemoteRuntime>().proxy.lock().unwrap().is_none());
    }

    #[test]
    fn a_launch_that_cannot_save_the_remote_fails_to_the_local_card_not_to_a_claim_about_the_other_computer() {
        let folder = tempfile::tempdir().unwrap();
        let blocker = folder.path().join("not-a-folder");
        fs::write(&blocker, b"").unwrap();
        let saved = saved_studio(
            remote_config::Locator::Address { host: "192.168.1.20".into(), port: 47901 },
            remote_config::RemoteTrust::System,
        );
        let app = launch_app();
        let app_dir = blocker.join("CORSIA");
        let entry = RemoteEntry { app: app.handle(), app_dir: &app_dir, record: saved.clone(), forget_on_rollback: false, unregister: || Ok::<(), String>(()), restore: || {} };
        let failure = entry.save(47912).err().expect("the save must fail");
        assert_eq!(failure, classify::ConnectFailure::Local);
        let mut connection = shell_state::ShellState::launch(Some(&saved));
        connection.failed("Studio", failure);
        assert_eq!(remote_startup_state(connection.state()), Some(serde_json::json!({ "kind": "unreachable", "name": "Studio", "cause": "local" })));
    }

    #[test]
    fn every_launch_failure_is_a_state_the_shell_draws_with_actions() {
        use classify::ConnectFailure;
        let saved = saved_studio(
            remote_config::Locator::Address { host: "192.168.1.20".into(), port: 47901 },
            remote_config::RemoteTrust::System,
        );
        let failures = [
            ConnectFailure::KeyChanged,
            ConnectFailure::Network,
            ConnectFailure::Server,
            ConnectFailure::Incompatible { app: "0.4.1".into(), host: Some("0.3.0".into()) },
            ConnectFailure::TooOld,
            ConnectFailure::Local,
        ];
        for failure in failures {
            let mut connection = shell_state::ShellState::launch(Some(&saved));
            connection.failed("Studio", failure);
            let drawn = remote_startup_state(connection.state()).expect("a launch failure the shell cannot draw");
            assert_ne!(drawn["kind"], "connecting");
        }
    }

    /// A pairing whose window cannot be shown: the connect is rolled back on this computer, not left half-entered.
    #[test]
    fn a_pairing_that_cannot_show_the_remote_rolls_back_the_proxy_the_saved_remote_and_the_service() {
        use remote_lifecycle::EnterRemote;
        let app = tauri::test::mock_builder().build(generated_context()).unwrap();
        app.manage(RemoteRuntime::default());
        let folder = tempfile::tempdir().unwrap();
        let record = saved_studio(remote_config::Locator::Address { host: "127.0.0.1".into(), port: 1 }, remote_config::RemoteTrust::System);
        let (unregistered, restored) = (std::cell::Cell::new(0), std::cell::Cell::new(0));
        let proxy = tauri::async_runtime::block_on(proxy::start_record(&record, 0, || {})).expect("a proxy");
        let mut entry = RemoteEntry {
            app: app.handle(),
            app_dir: folder.path(),
            record,
            forget_on_rollback: true,
            unregister: || { unregistered.set(unregistered.get() + 1); Ok(()) },
            restore: || restored.set(restored.get() + 1),
        };
        // The mock app has no "main" window, so navigating to the remote fails after the service was unregistered.
        let failure = remote_lifecycle::enter_remote(&mut entry, proxy).err().expect("the window is gone");
        assert_eq!(failure, classify::ConnectFailure::Local);
        assert_eq!(unregistered.get(), 1);
        assert_eq!(restored.get(), 1, "the local service was not brought back");
        assert_eq!(remote_config::load_remote(folder.path()), Ok(None), "the remote stayed saved");
        // A launch keeps its saved pairing: the same rollback must not forget it.
        let kept = saved_studio(remote_config::Locator::Address { host: "127.0.0.1".into(), port: 1 }, remote_config::RemoteTrust::System);
        let mut launch = RemoteEntry { app: app.handle(), app_dir: folder.path(), record: kept.clone(), forget_on_rollback: false, unregister: || Ok(()), restore: || {} };
        launch.save(47911).unwrap();
        launch.forget_remote();
        assert_eq!(remote_config::load_remote(folder.path()), Ok(Some(kept)));
    }

    #[test]
    fn a_pairing_that_did_not_connect_leaves_the_pairing_state() {
        let app = launch_app();
        let outcomes: [Result<RemotePairResponse, String>; 3] = [
            Ok(failure_response(classify::ConnectFailure::Local)),
            Ok(remote_pair_error(pairing::PairError::WrongCode)),
            Err("the CORSIA window is unavailable".into()),
        ];
        for outcome in outcomes {
            update_shell_state(app.handle(), |connection| connection.begin_pairing()).unwrap();
            leave_pairing_unless_connected(app.handle(), &outcome);
            assert_eq!(launch_state(&app), shell_state::LaunchState::Local);
        }
        update_shell_state(app.handle(), |connection| connection.connected("Studio")).unwrap();
        leave_pairing_unless_connected(app.handle(), &Ok(RemotePairResponse::Success { ok: true, name: "Studio".into() }));
        assert_eq!(launch_state(&app), shell_state::LaunchState::Remote { name: "Studio".into() });
    }

    #[test]
    fn a_disconnect_clears_the_remote_session_from_wherever_it_was_called() {
        assert_eq!(remote_session_clear("http://127.0.0.1:47911/"), RemoteSessionClear::ProxyToken);
        assert_eq!(remote_session_clear("tauri://localhost"), RemoteSessionClear::AllBrowsingData);
        assert_eq!(CLEAR_REMOTE_TOKEN_SCRIPT, "try { localStorage.removeItem('corsia_token'); } catch (_) {}");
        // The page stores its session under the same key (frontend/src/lib/stores/auth.ts).
        assert!(include_str!("../../../frontend/src/lib/stores/auth.ts").contains("localStorage.setItem('corsia_token'"));
    }

    #[test]
    fn a_waiting_quit_is_reported_only_while_the_stop_the_backup_or_the_restart_runs() {
        use adoption::Phase::*;
        for (phase, quit_asked, pending) in [
            (Waiting { running: Some(2) }, true, true),
            (Waiting { running: None }, false, false),
            (BackingUp, true, true),
            (WaitingRestart, true, false),
            (Starting, true, false),
            (StartingExisting, true, true),
        ] {
            assert_eq!(AdoptionProgress::new(phase.clone(), quit_asked).quit_pending, pending, "{phase:?}");
        }
        assert_eq!(
            serde_json::to_value(AdoptionProgress::new(Waiting { running: Some(2) }, true)).unwrap(),
            serde_json::json!({ "phase": "waiting", "running": 2, "quit_pending": true })
        );
    }

    #[test]
    fn local_requests_name_the_port_the_backend_guard_requires() {
        // backend/api/setup.py _loopback_host refuses a Host without the bound port.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind a test port");
        let port = listener.local_addr().expect("test port").port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept the shell request");
            let mut request = [0_u8; 512];
            let read = stream.read(&mut request).expect("read the shell request");
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{\"active\":false}")
                .expect("answer the shell request");
            String::from_utf8_lossy(&request[..read]).into_owned()
        });

        assert_eq!(operation_is_active(port), Ok(false));
        let request = server.join().expect("test server");
        assert!(request.starts_with("GET /api/system/operation-active HTTP/1.1\r\n"));
        assert!(request.contains(&format!("\r\nHost: 127.0.0.1:{port}\r\n")));
    }

    #[test]
    fn quit_during_adoption_exits_only_once_the_install_is_known_to_be_settled() {
        use adoption::AdoptionError as E;
        assert!(quit_after_adoption(true, &Ok(())));
        assert!(!quit_after_adoption(false, &Ok(())));
        assert!(quit_after_adoption(true, &Err(E::restored("x"))));
        assert!(quit_after_adoption(true, &Err(E::untouched("x"))));
        assert!(!quit_after_adoption(true, &Err(E::start_again("x"))));
        assert!(!quit_after_adoption(true, &Err(E::switched("x"))));
    }

    #[test]
    fn a_panic_in_an_adoption_step_asks_for_a_reopen_not_a_switch() {
        let error = tauri::async_runtime::block_on(off_main_thread(|| -> Result<(), _> {
            panic!("boom")
        }))
        .unwrap_err();
        assert_eq!(error.outcome, adoption::Outcome::Unconfirmed);
        assert_eq!(error.recovery, Some(adoption::Recovery::Reopen));
    }

    #[test]
    fn updater_configuration_pins_the_release_verification_key() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json"))
            .expect("tauri configuration must be valid JSON");
        let updater = config
            .get("plugins")
            .and_then(|plugins| plugins.get("updater"))
            .expect("updater plugin configuration must be present");
        let pubkey = updater
            .get("pubkey")
            .and_then(serde_json::Value::as_str)
            .expect("updater public key must be a string");
        let minisign_bytes = base64::engine::general_purpose::STANDARD
            .decode(pubkey)
            .expect("updater public key must be base64");
        let minisign_text = std::str::from_utf8(&minisign_bytes)
            .expect("updater public key must decode to minisign text");
        let mut lines = minisign_text.lines();
        let untrusted_comment = lines
            .next()
            .expect("minisign text must include an untrusted comment");
        assert!(
            untrusted_comment.starts_with("untrusted comment: minisign public key: "),
            "minisign text must start with an untrusted public-key comment"
        );
        assert!(
            untrusted_comment.ends_with("2EA93E7893462EB7"),
            "minisign untrusted comment must identify the pinned key"
        );
        let key_bytes = base64::engine::general_purpose::STANDARD
            .decode(
                lines
                    .next()
                    .expect("minisign text must include an encoded public key"),
            )
            .expect("minisign public key must be base64");
        assert_eq!(key_bytes.len(), 42, "minisign public key has 42 bytes");
        assert_eq!(&key_bytes[..2], b"Ed", "minisign key algorithm is Ed");
        let key_id = u64::from_le_bytes(
            key_bytes[2..10]
                .try_into()
                .expect("minisign key must include an eight-byte key id"),
        );
        assert_eq!(key_id, 0x2EA93E7893462EB7);
        assert_eq!(
            updater
                .get("endpoints")
                .and_then(serde_json::Value::as_array)
                .expect("updater endpoints must be an array")
                .iter()
                .map(|endpoint| endpoint.as_str().expect("endpoint must be a string"))
                .collect::<Vec<_>>(),
            vec!["https://github.com/cebertowicz/corsia/releases/latest/download/latest.json"]
        );
        assert!(
            matches!(
                updater.get("createUpdaterArtifacts"),
                None | Some(serde_json::Value::Bool(false))
            ),
            "updater artifacts must remain disabled"
        );
    }

    #[test]
    fn configured_startup_reuses_the_recorded_port_while_service_is_listening() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert_eq!(port_for_startup(&first_run::FirstRun::Configured, Some(port)), Ok(port));
    }

    #[test]
    fn setup_status_response_reports_whether_an_account_exists() {
        assert_eq!(
            setup_account_exists_response(
                "HTTP/1.1 200 OK\r\n\r\n{\"apple_silicon\":true,\"macos_supported\":true,\"ram_tier\":\"eligible\",\"free_disk_gb\":123.0,\"local_eligible\":true,\"work_folder\":\"~/corsia\",\"required_username\":null,\"existing_omlx\":false,\"ready_ai\":null}",
            ),
            Ok(false)
        );
        assert_eq!(
            setup_account_exists_response("HTTP/1.1 409 Conflict\r\n\r\n{\"code\":\"setup_complete\"}"),
            Ok(true)
        );
        assert!(setup_account_exists_response("HTTP/1.1 409 Conflict\r\n\r\n{\"code\":\"other\"}").is_err());
        assert!(setup_account_exists_response("HTTP/1.1 500 Server Error\r\n\r\n").is_err());
    }

    #[test]
    fn disk_image_refuses_only_service_registration() {
        assert_eq!(
            service_command_is_allowed("register", true),
            Err("Drag CORSIA into your Applications folder, then open it from there.".into())
        );
        assert_eq!(service_command_is_allowed("register", false), Ok(()));
        assert_eq!(service_command_is_allowed("unregister", true), Ok(()));
    }

    #[test]
    fn operation_lock_response_distinguishes_a_held_operation() {
        assert_eq!(
            operation_is_active_response("HTTP/1.1 200 OK\r\n\r\n{\"active\":true}"),
            Ok(true)
        );
        assert_eq!(
            operation_is_active_response("HTTP/1.1 200 OK\r\n\r\n{\"active\":false}"),
            Ok(false)
        );
        assert!(operation_is_active_response("HTTP/1.1 500 Internal Server Error\r\n\r\n").is_err());
        assert!(should_prevent_quit(Ok(true)));
        assert!(!should_prevent_quit(Ok(false)));
        assert!(!should_prevent_quit(Err("the CORSIA service is unavailable".into())));
    }

    #[test]
    fn command_line_modes_take_exactly_one_operation_id() {
        let args = |values: &[&str]| values.iter().map(OsString::from).collect::<Vec<_>>();

        assert_eq!(
            command_line_mode(&args(&["--update-stage", "op-1"])),
            Ok(Some(CommandLineMode::UpdateStage("op-1".into())))
        );
        assert_eq!(
            command_line_mode(&args(&["--update-swap", "op-1"])),
            Ok(Some(CommandLineMode::UpdateSwap("op-1".into())))
        );
        assert_eq!(
            command_line_mode(&args(&["--update-rollback", "op-1"])),
            Ok(Some(CommandLineMode::UpdateRollback("op-1".into())))
        );
        assert_eq!(
            command_line_mode(&args(&["--service", "status"])),
            Ok(Some(CommandLineMode::Service))
        );
        assert_eq!(command_line_mode(&args(&[])), Ok(None));
        for refused in [
            &["--update-stage"][..],
            &["--update-swap"],
            &["--update-stage", "op-1", "--service"],
            &["--update-swap", "op-1", "--update-stage"],
            &["--update-rollback", "op-1", "--update-stage"],
        ] {
            assert!(command_line_mode(&args(refused)).is_err(), "{refused:?}");
        }
    }

    const SCREEN: &str = "corsia-update=%7B%22v%22%3A1%2C%22route%22%3A%22%2F%3Fproject%3Dabc%22%7D";

    #[test]
    fn update_screen_fragment_constants_match_the_renderer() {
        const HANDOFF: &str = include_str!("../../../frontend/src/lib/update/shellHandoff.ts");
        let key = UPDATE_SCREEN_KEY.strip_suffix('=').unwrap();
        assert!(HANDOFF.contains(&format!("export const HANDOFF_KEY = '{key}';")));
        let renderer_limit = HANDOFF
            .lines()
            .find_map(|line| line.strip_prefix("export const HANDOFF_MAX_CHARS = "))
            .and_then(|value| value.strip_suffix(';'))
            .expect("the renderer handoff limit")
            .replace('_', "")
            .parse::<usize>()
            .expect("a numeric renderer handoff limit");
        assert_eq!(renderer_limit, UPDATE_SCREEN_MAX_CHARS);
    }

    #[test]
    fn a_handed_over_update_screen_never_changes_a_command_line_mode() {
        let args = |values: &[&str]| values.iter().map(OsString::from).collect::<Vec<_>>();
        let screen = format!("--update-screen={SCREEN}");
        assert_eq!(command_line_mode(&args(&[&screen])), Ok(None), "the window opens as it always does");
        assert_eq!(command_line_mode(&args(&["--service", "status", &screen])), Ok(Some(CommandLineMode::Service)));
        assert!(command_line_mode(&args(&["--update-swap", "op-1", &screen])).is_err(), "a mode still takes nothing else");
    }

    #[test]
    fn only_one_well_formed_bounded_update_screen_is_read_from_the_command_line() {
        let args = |values: &[&str]| values.iter().map(OsString::from).collect::<Vec<_>>();
        assert_eq!(relaunched_update_screen(&args(&[&format!("--update-screen={SCREEN}")])), Some(SCREEN.to_owned()));
        let oversized = format!("--update-screen=corsia-update={}", "a".repeat(UPDATE_SCREEN_MAX_CHARS));
        for refused in [
            vec![],
            vec![format!("--update-screen={SCREEN}"), format!("--update-screen={SCREEN}")],
            vec![oversized],
            vec!["--update-screen=".into()],
            vec!["--update-screen=corsia-update=".into()],
            vec!["--update-screen=other=%7B%7D".into()],
            vec!["--update-screen=corsia-update=%7B\"%7D".into()],
            vec!["--update-screen=corsia-update=%7B<%7D".into()],
            vec!["--update-screen=corsia-update=%7B\\%7D".into()],
            vec!["--update-screen=corsia-update=%7B{%7D".into()],
            vec!["--update-screen=corsia-update=%7B\u{7}%7D".into()],
            vec!["--update-screen=corsia-update=%7B%2".into()],
            vec!["--update-screen=corsia-update=%zz".into()],
            vec!["--update-screen=corsia-update=a b".into()],
            vec!["--update-screen=corsia-update=a#b".into()],
            vec!["--update-screen corsia-update=abc".into()],
        ] {
            let args = refused.iter().map(OsString::from).collect::<Vec<_>>();
            assert_eq!(relaunched_update_screen(&args), None, "{refused:?}");
        }
    }

    /// A screen as `encodeURIComponent` writes it for a route holding every character it leaves unescaped.
    const SCREEN_UNESCAPED: &str = "corsia-update=%7B%22route%22%3A%22%2F%3Fq%3D(it's)!*~-_.%22%7D";

    #[test]
    fn every_character_the_page_leaves_unescaped_is_carried() {
        let args = vec![OsString::from(format!("--update-screen={SCREEN_UNESCAPED}"))];
        assert_eq!(relaunched_update_screen(&args), Some(SCREEN_UNESCAPED.to_owned()));
        let carried = relaunch_arguments(Path::new("/Applications/CORSIA.app"), Some(&format!("tauri://localhost/update.html#{SCREEN_UNESCAPED}")), &service_url(47_895));
        assert_eq!(carried.last(), Some(&OsString::from(format!("--update-screen={SCREEN_UNESCAPED}"))));
    }

    #[test]
    fn the_replacement_reopens_with_the_update_screen_of_the_shell_page_or_this_computers_service_page() {
        let bundle = Path::new("/Applications/CORSIA.app");
        let local = service_url(47_895);
        let plain = vec![OsString::from("-n"), OsString::from("/Applications/CORSIA.app")];
        let screen = [OsString::from("--args"), OsString::from(format!("--update-screen={SCREEN}"))];
        for window in [format!("tauri://localhost/update.html#{SCREEN}"), format!("http://127.0.0.1:47895/?project=abc#{SCREEN}"), format!("http://127.0.0.1:47895/#{SCREEN}")] {
            let carried = relaunch_arguments(bundle, Some(&window), &local);
            assert_eq!(carried[..2], plain[..], "{window}");
            assert_eq!(carried[2..], screen, "{window}");
        }
        for window in [
            None,
            Some("tauri://localhost/index.html".to_owned()),
            Some(format!("tauri://localhost/index.html#{SCREEN}")),
            Some(format!("http://127.0.0.1:47896/#{SCREEN}")),
            Some(format!("http://localhost:47895/#{SCREEN}")),
            Some(format!("https://127.0.0.1:47895/#{SCREEN}")),
            Some(format!("http://studio.example:47895/#{SCREEN}")),
            Some("http://127.0.0.1:47895/".to_owned()),
            Some("tauri://localhost/update.html".to_owned()),
            Some("tauri://localhost/update.html#corsia-update=%7B%zz".to_owned()),
            Some("http://127.0.0.1:47895/#corsia-update=%7B%zz".to_owned()),
            Some(format!("tauri://localhost/update.html#corsia-update={}", "a".repeat(UPDATE_SCREEN_MAX_CHARS))),
        ] {
            assert_eq!(relaunch_arguments(bundle, window.as_deref(), &local), plain, "{window:?}");
        }
    }

    #[test]
    fn a_handed_over_screen_opens_the_window_on_the_shell_update_page_and_nothing_else_does() {
        let page = relaunched_start_page(Some(SCREEN)).expect("the update page");
        assert_eq!(page.as_str(), format!("tauri://localhost/update.html#{SCREEN}"));
        assert!(is_shell_update_page(page.as_str()));
        assert_eq!(relaunched_start_page(None), None, "an ordinary launch opens the startup page as always");
    }

    #[test]
    fn the_first_ready_address_carries_the_handed_over_screen_once() {
        let slot = Mutex::new(Some(SCREEN.to_owned()));
        let answer = |startup: ServiceStartup| with_relaunched_screen::<String>(&slot, Ok(startup));
        // A start that is not Ready leaves it for the start that is.
        assert_eq!(answer(ServiceStartup::RequiresApproval), Ok(ServiceStartup::RequiresApproval));
        assert_eq!(with_relaunched_screen(&slot, Err::<ServiceStartup, _>("did not start".to_owned())), Err("did not start".into()));
        assert_eq!(
            answer(ServiceStartup::Ready { url: service_url(47_895) }),
            Ok(ServiceStartup::Ready { url: format!("http://127.0.0.1:47895/#{SCREEN}") })
        );
        assert_eq!(answer(ServiceStartup::Ready { url: service_url(47_895) }), Ok(ServiceStartup::Ready { url: service_url(47_895) }));
        let empty = Mutex::new(None);
        assert_eq!(
            with_relaunched_screen::<String>(&empty, Ok(ServiceStartup::Ready { url: service_url(1) })),
            Ok(ServiceStartup::Ready { url: service_url(1) })
        );
    }

    #[test]
    fn a_move_an_update_held_is_recorded_by_every_start_and_rerun_by_the_install_watcher_after_its_relaunch_check() {
        let source = include_str!("lib.rs");
        let start = source.split("fn start_service_in<").nth(1).unwrap();
        let moved = start.find("state_move::run(").unwrap();
        let recorded = start.find("move_after_update.launched(&outcome)").expect("every start records its move");
        assert!(moved < recorded && recorded < start.find("attach_healthy_active_service(").unwrap());
        let watch = source.split("fn watch_for_swapped_app(").nth(1).unwrap();
        let watch = &watch[..watch.find("\n}\n").unwrap()];
        // A swapped-in app relaunches first: the move never runs in a window that belongs to a replaced app.
        assert!(watch.find("relaunch(&app, &bundle)").unwrap() < watch.find("take_when_over(").unwrap());
        let rerun = source.split("fn move_after_update(").nth(1).unwrap();
        let rerun = &rerun[..rerun.find("\n}\n").unwrap()];
        assert!(rerun.contains("start_service(") && rerun.contains("show_launch_outcome("));
        // The window leaves the person's page only from the move's own first reading, never up front.
        assert!(!rerun.contains("return_to_shell("));
        // Its answer is shell state only once the window is on the shell; a skip that left the person on their page
        // keeps no Ready for a later shell load to replay.
        let kept = rerun.find("(_, true) => show_launch_outcome(").expect("the answer is kept only on the shell");
        assert!(rerun.find("is_startup_shell_page(").unwrap() < kept);
        assert!(rerun.contains("(Ok(_), false) => {}"));
        let progress = &start[moved..recorded];
        assert!(progress.find("return_to_shell(app)").unwrap() < progress.find("MovingData").unwrap());
    }

    #[test]
    fn a_saved_remote_still_owns_launch_before_any_handed_over_screen() {
        let setup = include_str!("lib.rs").split("// A saved remote owns launch").nth(1).unwrap();
        let remote_return = setup.find("return Ok(());").unwrap();
        let carried = setup.find("with_relaunched_screen(").unwrap();
        assert!(remote_return < carried, "the handed-over screen is applied before a saved remote takes launch");
    }

    #[test]
    fn relaunch_waits_for_a_different_installed_generation_and_a_healthy_service() {
        let unreachable = || -> bool { panic!("health is only probed after a swap") };

        assert!(!should_relaunch("0.4.0+1", Some("0.4.0+1"), unreachable));
        assert!(!should_relaunch("0.4.0+1", None, unreachable));
        assert!(!should_relaunch("0.4.0+1", Some("0.4.1+2"), || false));
        assert!(should_relaunch("0.4.0+1", Some("0.4.1+2"), || true));
        assert!(should_relaunch("0.4.1+2", Some("0.4.0+1"), || true));
    }

    #[test]
    fn bundle_generation_joins_short_version_and_build() {
        let temp = tempfile::tempdir().unwrap();
        let bundle = temp.path().join("CORSIA.app");
        fs::create_dir_all(bundle.join("Contents")).unwrap();
        fs::write(
            bundle.join("Contents").join("Info.plist"),
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict>\
             <key>CFBundleShortVersionString</key><string>0.4.0</string>\
             <key>CFBundleVersion</key><string>7</string></dict></plist>",
        )
        .unwrap();

        assert_eq!(bundle_generation(&bundle).as_deref(), Some("0.4.0+7"));
        assert_eq!(bundle_generation(temp.path()), None);
    }
}
