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
mod startup_failure;
#[cfg(test)]
mod test_tls;
use startup_failure::StartupFailure;
mod trust;
mod version_check;
mod window_placement;
mod update_stage;
mod update_swap;

const DEFAULT_SERVICE_PORT: u16 = 47_895;
const ACTIVE_OPERATION_ERROR: &str = "CORSIA is still completing an operation";
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);
/// A 20 GiB rehearsal root reached health in 71–74 s, the October 20 cold
/// start took about 99 s, and its longest silent phase was 65 s. Six times the
/// largest observed start leaves room for a slow migration without concealing
/// a hang indefinitely.
pub(crate) const STARTUP_CAP: Duration = Duration::from_secs(600);
/// How long launchd may show no process for a just-started service.
pub(crate) const NOT_RUNNING_WINDOW: Duration = Duration::from_secs(30);
const RELAUNCH_POLL_INTERVAL: Duration = Duration::from_secs(2);

#[derive(Default)]
struct AppServiceState {
    app_dir: Mutex<Option<PathBuf>>,
    /// The local service ports already granted their commands; a capability can't be removed, so each is added once.
    granted_local_ports: Mutex<std::collections::BTreeSet<u16>>,
    quitting: AtomicBool,
    exit_cleanup_attempted: AtomicBool,
    adoption_in_progress: AtomicBool,
    adoption_cancel_requested: AtomicBool,
    /// What the adoption in progress is doing now, for the window to show.
    adoption_phase: Mutex<Option<adoption::Phase>>,
}

#[derive(Default)]
struct RemoteRuntime {
    browser: Mutex<Option<discovery::Browser>>,
    shell: Mutex<shell_state::ShellState>,
    proxy: Mutex<Option<proxy::ProxyHandle>>,
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
fn local_get_request(port: u16, path: &str) -> String {
    format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
}

/// Send one GET to a service on this computer and read the whole reply.
/// The error names the step that failed, for callers that report it.
fn local_exchange(
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
        .and_then(|_| stream.write_all(local_get_request(port, path).as_bytes()))
        .map_err(|error| format!("could not send the request: {error}"))?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("could not read the reply: {error}"))?;
    Ok(response)
}

fn local_get(port: u16, path: &str) -> Result<String, String> {
    let unavailable = || "the CORSIA service is unavailable".to_string();
    local_exchange(port, path, Duration::from_millis(250), Duration::from_millis(500))
        .map_err(|_| unavailable())
        .and_then(|response| String::from_utf8(response).map_err(|_| unavailable()))
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

fn restart_service() -> Result<(), String> {
    unregister_service()?;
    run_service_command("register")
}

fn service_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

/// The commands the pages the local service serves call: first-run folder choice and the setup report, Connect to
/// another computer, and the window-drag handler injected on every page. Never `disconnect_remote` (the remote page's
/// own command), `start_local_service` or the quit and login-item commands (the startup shell's).
const LOCAL_SERVICE_PERMISSIONS: &[&str] = &[
    "allow-choose-work-folder",
    "allow-configure-work-folder",
    "allow-remote-discovered",
    "allow-remote-pair",
    "allow-save-setup-report",
    "allow-start-window-drag",
];

/// Lets the page the local service serves at this exact origin call its commands. The app's command ACL refuses every
/// origin it does not name, and the service page is not the startup shell, so without this the first-run folder
/// choice and the Connect to another computer screens are refused.
fn grant_local_service_origin<R: tauri::Runtime>(app: &tauri::AppHandle<R>, service: &AppServiceState, port: u16) -> Result<(), String> {
    let mut granted = service.granted_local_ports.lock().unwrap_or_else(PoisonError::into_inner);
    if granted.contains(&port) {
        return Ok(());
    }
    let capability = LOCAL_SERVICE_PERMISSIONS.iter().fold(
        CapabilityBuilder::new(format!("local-service-origin-{port}")).remote(service_url(port)).window("main"),
        |capability, permission| capability.permission(*permission),
    );
    app.add_capability(capability).map_err(|error| format!("could not grant local service commands: {error}"))?;
    granted.insert(port);
    Ok(())
}

fn bundled_resource_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
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
    let target = format!("gui/{}/{}", unsafe { libc::geteuid() }, service_label());
    wait_for_startup(
        port,
        Some(&log),
        || Ok(release_proof::launchd_pid(&target)),
        NOT_RUNNING_WINDOW,
        STARTUP_CAP,
    )
    .map_err(|failure| failure.to_string())
}

/// Health is the readiness signal. A changed launchd PID proves the service
/// restarted while starting, and a PID the kernel no longer knows with none in
/// its place proves it exited. launchd showing no process for the whole
/// `not_running` window means it exited before it was seen or never started;
/// launchd's 10 s respawn throttle sits well inside the window. Otherwise the
/// fixed cap bounds a hung startup. `log` is searched only for launcher
/// refusals.
pub(crate) fn wait_for_startup(
    port: u16,
    log: Option<&Path>,
    mut service_pid: impl FnMut() -> Result<Option<u32>, String>,
    not_running: Duration,
    cap: Duration,
) -> Result<(), StartupFailure> {
    let began = Instant::now();
    let mut initial_pid = None;
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
        if began.elapsed() >= cap {
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
    match Command::new("/usr/bin/open").arg("-n").arg(bundle).status() {
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
        let port = lifecycle::service_port(&app, &service);
        if should_relaunch(&launched, installed.as_deref(), || probe_health(port)) {
            let _ = relaunch(&app, &bundle);
            return;
        }
    });
}

fn register_or_report_status(port: u16) -> Result<service_management::ServiceStatus, String> {
    service_management::ensure_registered_from_app(|| operation_is_active(port))
}

fn start_service(
    app: &tauri::AppHandle,
    service: &AppServiceState,
) -> Result<ServiceStartup, String> {
    if running_from_disk_image()? {
        return Ok(ServiceStartup::DiskImage);
    }
    let app_dir = default_app_dir(app)?;
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
    // Decide about the workspace before anything is written: a refusal leaves
    // the computer exactly as it was.
    let first_run = if first_run::environment_names_the_roots() {
        first_run::FirstRun::Configured
    } else {
        let home = app
            .path()
            .home_dir()
            .map_err(|error| format!("could not resolve the home folder: {error}"))?;
        first_run::plan(&home, &app_dir)?
    };
    if let first_run::FirstRun::ExistingInstall { reason } = first_run {
        return Err(reason);
    }
    if let first_run::FirstRun::Adoptable { source } = &first_run {
        return Ok(ServiceStartup::AdoptionOffered {
            workspace: source.workspace.display().to_string(),
        });
    }
    let port = port_for_startup(&first_run, first_run::recorded_port(&app_dir))?;
    if let first_run::FirstRun::Fresh { workspace } = first_run {
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
            grant_local_service_origin(app, service, port)?;
            Ok(ServiceStartup::Ready {
                url: service_url(port),
            })
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

fn remote_shell_context(connection: &shell_state::ShellState) -> serde_json::Value {
    match connection.state() {
        shell_state::LaunchState::Remote { name } => serde_json::json!({ "mode": "remote", "name": name }),
        _ => serde_json::json!({ "mode": "local" }),
    }
}

/// What a page load hands the window: the shell context, and the script drawing the shell state. A finished load of
/// any page but the startup shell means a Ready launch answer was followed, so it is not followed again.
fn page_load_state(connection: &mut shell_state::ShellState, url: &str, finished: bool) -> (serde_json::Value, Option<String>) {
    if finished && !is_shell_origin(url) {
        connection.left_shell();
    }
    (remote_shell_context(connection), shell_state_script(connection.state()))
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

fn update_shell_state<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    update: impl FnOnce(&mut shell_state::ShellState),
) -> Result<(), String> {
    let runtime = app.state::<RemoteRuntime>();
    let state = {
        // A poisoned lock still holds a whole state: every writer replaces it in one assignment.
        let mut connection = runtime.shell.lock().unwrap_or_else(PoisonError::into_inner);
        update(&mut connection);
        connection.state().clone()
    };
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
            .permission("allow-start-window-drag"),
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
fn restore_local_service(app: &tauri::AppHandle) {
    let service = app.state::<AppServiceState>();
    let restored = run_service_command("register").and_then(|()| start_service(app, &service).map(drop));
    if let Err(error) = restored {
        log::error!("could not bring the local service back after a failed connect: {error}");
        show_shell_failure(app, &error);
    }
}

/// The shell operations that enter a remote, for pairing and for every launch that connects a saved one. The two
/// differ only in what rolls back: pairing forgets the remote it just saved and brings the local service back, while a
/// launch keeps the saved pairing (Try again needs it) and the local service stays stopped as it was.
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

/// Connects the saved remote at launch and on Try again. Whatever goes wrong ends in a failure state with actions,
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

/// Try again on a launch failure: connect the saved remote once more. It answers at once; the shell is told the
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

#[derive(Deserialize)]
struct RemotePairRequest {
    target: RemoteTarget,
    code: String,
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

#[tauri::command]
async fn remote_pair(
    request: RemotePairRequest,
    app: tauri::AppHandle,
    remote: tauri::State<'_, RemoteRuntime>,
) -> Result<RemotePairResponse, String> {
    let outcome = pair_remote(request, &app, &remote).await;
    leave_pairing_unless_connected(&app, &outcome);
    outcome
}

async fn pair_remote(request: RemotePairRequest, app: &tauri::AppHandle, remote: &RemoteRuntime) -> Result<RemotePairResponse, String> {
    use classify::ConnectFailure;
    update_shell_state(app, |connection| connection.begin_pairing())?;
    let app_dir = default_app_dir(app)?;
    let locator = match request.target {
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
            Err(_) => return Ok(failure_response(ConnectFailure::Network)),
        },
    };
    let paired = match pairing::pair(&host, port, &request.code).await {
        Ok(paired) => paired,
        Err(error) => return Ok(remote_pair_error(error)),
    };
    match version_check::check(&paired.host, paired.port, &paired.trust, &version_check::app_version(app)).await {
        Ok(version_check::Compat::Ok) => {}
        Ok(version_check::Compat::Incompatible { app: app_version, host }) => return Ok(failure_response(ConnectFailure::Incompatible { app: app_version, host })),
        Ok(version_check::Compat::TooOld) => return Ok(failure_response(ConnectFailure::TooOld)),
        Err(error) => return Ok(remote_pair_error(error)),
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
        Err(error) => return Ok(failure_response(proxy_failure(error))),
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
        Err(failure) => Ok(failure_response(failure)),
    }
}

fn saved_proxy_origin_allows(remote: Option<&remote_config::RemoteRecord>, actual: &str) -> bool {
    remote.is_some_and(|saved| actual.trim_end_matches('/') == format!("http://127.0.0.1:{}", saved.proxy_port))
}

/// The startup shell's own page, the only caller besides the saved proxy origin: its launch-failure actions
/// (Set up this computer instead, Pair again) have to be able to forget the saved remote.
fn is_shell_origin(actual: &str) -> bool {
    tauri::Url::parse(actual).is_ok_and(|url| url.scheme() == "tauri" && url.host_str() == Some("localhost"))
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
/// The failure is a state of the connection; the page-load hook draws it once the shell page's own script is running.
fn show_shell_failure<R: tauri::Runtime>(app: &tauri::AppHandle<R>, reason: &str) {
    report_shell_state(app, |connection| connection.service_failed(reason));
    return_to_shell(app);
}

/// Navigates the window to the startup shell unless it is already there.
fn return_to_shell<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    let navigated = app
        .get_webview_window("main")
        .ok_or_else(|| "the CORSIA window is unavailable".to_owned())
        .and_then(|window| {
            if window.url().is_ok_and(|url| is_shell_origin(url.as_str())) {
                return Ok(());
            }
            window.navigate(tauri::Url::parse("tauri://localhost").expect("a valid URL")).map_err(|error| error.to_string())
        });
    if let Err(error) = navigated {
        log::error!("could not return to the startup shell: {error}");
    }
}

/// Forgets the saved remote and brings the local service back. Called by the remote page's Disconnect and by the
/// startup shell's launch-failure actions; it answers like `start_local_service`.
#[tauri::command]
fn disconnect_remote(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
    remote: tauri::State<'_, RemoteRuntime>,
    webview: tauri::Webview,
) -> Result<ServiceStartup, String> {
    let app_dir = default_app_dir(&app)?;
    let saved = remote_config::load_remote(&app_dir)?;
    let actual = webview.url().map_err(|error| format!("could not read calling origin: {error}"))?;
    if !is_shell_origin(actual.as_str()) && !saved_proxy_origin_allows(saved.as_ref(), actual.as_str()) {
        return Err("disconnect_remote is allowed only from the startup shell or the saved proxy origin".into());
    }
    let outcome = remote_lifecycle::disconnect(
        || remote_config::forget_remote(&app_dir),
        || {
            clear_remote_session(&app, &remote_session_clear(actual.as_str()));
            replace_proxy(&remote, None);
            report_shell_state(&app, |connection| connection.begin_disconnect());
        },
        || run_service_command("register"),
        || start_service(&app, &service),
    );
    match outcome {
        Ok(startup) => {
            report_shell_state(&app, |connection| connection.local());
            if let ServiceStartup::Ready { url } = &startup {
                let window = app.get_webview_window("main").ok_or("the CORSIA window is unavailable")?;
                window.eval(format!("window.location.replace('{url}');")).map_err(|error| error.to_string())?;
            }
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
        Self {
            phase,
            quit_pending: quit_asked
                && matches!(
                    phase,
                    adoption::Phase::Waiting { .. }
                        | adoption::Phase::BackingUp
                        | adoption::Phase::StartingExisting
                ),
        }
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
    let phase = service.adoption_phase.lock().ok().and_then(|phase| *phase)?;
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
    let port = source.port;
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
    // Adoption is done: from here a failure is the app's own service still coming up, never the old install.
    let switched = |message: &str| adoption::AdoptionError::switched(message);
    *service.app_dir.lock().map_err(|_| switched("service state is unavailable"))? = Some(app_dir.clone());
    wait_for_service(port, &app_dir).map_err(|error| switched(&error))?;
    Ok(ServiceStartup::Ready { url: service_url(port) })
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
        Ok(ServiceStartup::Ready { url: service_url(port) })
    })
    .await;
    local_start_answered(&app, answer)
}

#[tauri::command]
async fn start_local_service(app: tauri::AppHandle) -> Result<ServiceStartup, String> {
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
    local_start_answered(&app, answer)
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

#[tauri::command]
fn save_setup_report(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
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
    // This is deliberately an allowlisted report: setup may have handled
    // credentials, so the service log is referenced but never copied.
    std::fs::write(
        &report,
        format!(
            "CORSIA setup report\n\nGenerated: {stamp}\nApp version: {}\nSetup did not complete.\n\nService log (not copied here): {}\n",
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Every command-line mode is dispatched here, before any window exists.
    let mode = match command_line_mode(&env::args_os().skip(1).collect::<Vec<_>>()) {
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

    tauri::Builder::default()
        // The backend owns persistent service logs under CORSIA_STATE_DIR. Keep
        // native diagnostics on stdout so an existing macOS app-log path cannot
        // prevent the packaged app from starting before the service does.
        .plugin(
            tauri_plugin_log::Builder::default()
                .clear_targets()
                .target(tauri_plugin_log::Target::new(
                    tauri_plugin_log::TargetKind::Stdout,
                ))
                .build(),
        )
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        // Remembers the window's size and position; `window_placement` covers what it leaves to the OS.
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .invoke_handler(tauri::generate_handler![
            start_local_service,
            adopt_existing_install,
            start_existing_again,
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
        ])
        .on_page_load(|webview, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) && is_shell_origin(payload.url().as_str()) {
            }
            // Inject the drag handler on every page load. Idempotent —
            // the script self-guards via __corsia_drag_handler_installed
            // so multiple injections don't double-bind.
            let finished = matches!(payload.event(), tauri::webview::PageLoadEvent::Finished);
            let (shell_context, startup_state) = webview
                .app_handle()
                .state::<RemoteRuntime>()
                .shell
                .lock()
                .map(|mut connection| page_load_state(&mut connection, payload.url().as_str(), finished))
                .unwrap_or_else(|_| (serde_json::json!({ "mode": "local" }), None));
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
            app.manage(AppServiceState::default());
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
                    if let Ok(mut connection) = app.state::<RemoteRuntime>().shell.lock() {
                        *connection = shell_state::ShellState::launch(Some(&saved));
                    }
                    let app_handle = app.handle().clone();
                    tauri::async_runtime::spawn(async move { connect_saved_remote(app_handle, saved).await; });
                    return Ok(());
                }
            }
            // The default window loads the static startup shell while Tauri
            // owns the first service launch. Retrying after a visible failure
            // stays available through start_local_service.
            let app_handle = app.handle().clone();
            thread::spawn(move || {
                let service = app_handle.state::<AppServiceState>();
                show_launch_outcome(&app_handle, &start_service(&app_handle, &service));
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
    use base64::Engine;
    use std::fs;

    const SHELL_COMMANDS: &[&str] = &[
        "start_local_service",
        "adopt_existing_install",
        "adoption_progress",
        "check_service_started",
        "quit_app",
        "reopen_app",
        "restart_source_install",
        "start_existing_again",
        "start_source_install",
        "remote_discovered",
        "remote_pair",
        "disconnect_remote",
        "retry_saved_remote",
        "choose_work_folder",
        "configure_work_folder",
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
        assert_eq!(remote_shell_context(&connection), serde_json::json!({ "mode": "local" }));
        connection.connected("Studio");
        assert_eq!(remote_shell_context(&connection), serde_json::json!({ "mode": "remote", "name": "Studio" }));
        connection.failed("Studio", classify::ConnectFailure::Network);
        assert_eq!(remote_shell_context(&connection), serde_json::json!({ "mode": "local" }));
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
        assert!(saved_proxy_origin_allows(Some(&remote), "http://127.0.0.1:47911"));
        assert!(!saved_proxy_origin_allows(Some(&remote), "http://127.0.0.1:47912"));
        assert!(!saved_proxy_origin_allows(Some(&remote), "https://127.0.0.1:47911"));
        assert!(!saved_proxy_origin_allows(Some(&remote), "http://127.0.0.1:47895"));
        assert!(!saved_proxy_origin_allows(None, "http://127.0.0.1:47911"));
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
            ["choose_work_folder", "configure_work_folder", "disconnect_remote", "remote_discovered", "remote_pair", "save_setup_report"],
            "the frontend's commands changed: grant each page its commands, or add it to the remote-page-only list"
        );
        let (_app, webview) = service_origin_app(47895);
        let origin = service_url(47895);
        for command in called.iter().map(String::as_str).chain(["start_window_drag"]) {
            let allowed = invoke_from(&webview, command, &origin).is_ok();
            assert_eq!(allowed, !REMOTE_PAGE_ONLY.contains(&command), "{command} from {origin}");
        }
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
    fn the_proxy_origin_keeps_only_its_two_commands_beside_the_local_service_grant() {
        let (app, webview) = service_origin_app(47895);
        grant_remote_proxy_origin(app.handle(), 47911).unwrap();
        for command in SHELL_COMMANDS {
            let allowed = invoke_from(&webview, command, "http://127.0.0.1:47911").is_ok();
            assert_eq!(allowed, ["disconnect_remote", "start_window_drag"].contains(command), "{command} from the proxy origin");
        }
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
        let (_, script) = page_load_state(&mut connection, "tauri://localhost/index.html", true);
        assert!(script.is_some_and(|script| script.contains(r#""state":"ready""#)));
        page_load_state(&mut connection, "http://127.0.0.1:47895/", false);
        assert!(shell_state_script(connection.state()).is_some(), "a load still starting has not reached the service");
        page_load_state(&mut connection, "http://127.0.0.1:47895/", true);
        // A quit failure later sends the window back to the shell, which must stay on its own screen.
        let (_, script) = page_load_state(&mut connection, "tauri://localhost/index.html#quit-failure", true);
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
                "a reload after a failed retry must still draw the failure and its Try again"
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
            assert_eq!(AdoptionProgress::new(phase, quit_asked).quit_pending, pending, "{phase:?}");
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
