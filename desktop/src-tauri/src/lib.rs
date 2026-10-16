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
        Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use serde::Serialize;
use tauri::Manager;

mod app_paths;
mod classify;
mod discovery;
mod first_run;
mod lifecycle;
mod remote;
mod remote_config;
mod service_management;
mod startup_failure;
mod update_stage;
mod update_swap;

const DEFAULT_SERVICE_PORT: u16 = 47_895;
const ACTIVE_OPERATION_ERROR: &str = "CORSIA is still completing an operation";
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);
const RELAUNCH_POLL_INTERVAL: Duration = Duration::from_secs(2);

#[derive(Default)]
struct AppServiceState {
    app_dir: Mutex<Option<PathBuf>>,
    quitting: AtomicBool,
    exit_cleanup_attempted: AtomicBool,
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

#[derive(Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
enum ServiceStartup {
    Ready { url: String },
    RequiresApproval,
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

fn default_app_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .home_dir()
        .map(|home| app_paths::app_dir_for_home(&home))
        .map_err(|error| format!("could not resolve the CORSIA app folder: {error}"))
}

fn service_log_path(app_dir: &Path) -> PathBuf {
    app_paths::state_dir_for(app_dir)
        .join("logs")
        .join("service.log")
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

fn local_get(port: u16, path: &str) -> Result<String, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(250))
        .map_err(|_| "the CORSIA service is unavailable")?;
    stream
        .set_read_timeout(Some(Duration::from_millis(500)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_millis(500))))
        .and_then(|_| stream.write_all(local_get_request(port, path).as_bytes()))
        .map_err(|_| "the CORSIA service is unavailable")?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|_| "the CORSIA service is unavailable")?;
    Ok(response)
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

fn run_service_command(action: &str) -> Result<(), String> {
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
    let deadline = Instant::now() + HEALTH_TIMEOUT;
    while Instant::now() < deadline {
        if probe_health(port) {
            return Ok(());
        }
        thread::sleep(HEALTH_POLL_INTERVAL);
    }
    Err(startup_failure::timeout_message(&service_log_path(app_dir)))
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

fn relaunch(app: &tauri::AppHandle, bundle: &Path) {
    let service = app.state::<AppServiceState>();
    // The service now belongs to the new app: this exit must never unregister it.
    let cleanup_was_pending = !service.exit_cleanup_attempted.swap(true, Ordering::AcqRel);
    match Command::new("/usr/bin/open").arg("-n").arg(bundle).status() {
        Ok(status) if status.success() => {
            service.quitting.store(true, Ordering::Relaxed);
            app.exit(0);
        }
        outcome => {
            log::error!("could not reopen the updated CORSIA app: {outcome:?}");
            if cleanup_was_pending {
                service.exit_cleanup_attempted.store(false, Ordering::Release);
            }
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
            relaunch(&app, &bundle);
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
    let app_dir = default_app_dir(app)?;
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
fn configure_work_folder(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
    path: PathBuf,
) -> Result<(), String> {
    let app_dir = default_app_dir(&app)?;
    let home = app
        .path()
        .home_dir()
        .map_err(|error| format!("could not resolve the home folder: {error}"))?;
    first_run::ensure_folder_writable(&path)?;
    let port = first_run::recorded_port(&app_dir).unwrap_or(DEFAULT_SERVICE_PORT);
    let resources = bundled_resource_dir(&app).ok();
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
    *service
        .app_dir
        .lock()
        .map_err(|_| "service state is unavailable")? = Some(app_dir);
    Ok(())
}

#[tauri::command]
fn start_local_service(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
) -> Result<ServiceStartup, String> {
    start_service(&app, &service)
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
    Ok(service_log_path(&app_dir).display().to_string())
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
    let log_path = service_log_path(&app_dir);
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
    let context = tauri::generate_context!();
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
        .invoke_handler(tauri::generate_handler![
            start_local_service,
            choose_work_folder,
            configure_work_folder,
            lifecycle::retry_quit,
            open_login_items,
            get_service_log_path,
            save_setup_report,
            start_window_drag,
        ])
        .on_page_load(|webview, _payload| {
            // Inject the drag handler on every page load. Idempotent —
            // the script self-guards via __corsia_drag_handler_installed
            // so multiple injections don't double-bind.
            let _ = webview.eval(DRAG_INIT_SCRIPT);
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
            watch_for_swapped_app(app.handle().clone());
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
            // The default window loads the static startup shell while Tauri
            // owns the first service launch. Retrying after a visible failure
            // stays available through start_local_service.
            let app_handle = app.handle().clone();
            thread::spawn(move || {
                let service = app_handle.state::<AppServiceState>();
                match start_service(&app_handle, &service) {
                    Ok(ServiceStartup::Ready { url }) => {
                        if let Some(window) = app_handle.get_webview_window("main") {
                            if let Err(error) = window.eval(format!("window.location.replace('{url}');")) {
                                log::error!(
                                    "could not navigate to the local CORSIA service: {error}"
                                );
                            }
                        }
                    }
                    Ok(ServiceStartup::RequiresApproval) => {
                        if let Some(window) = app_handle.get_webview_window("main") {
                            let _ = window.eval("window.__corsiaShowBackgroundApproval?.();");
                        }
                    }
                    Err(error) => {
                        log::error!("bundled CORSIA service did not start: {error}");
                        if let Some(window) = app_handle.get_webview_window("main") {
                            let reason = serde_json::to_string(&error).unwrap_or_default();
                            let _ = window.eval(format!(
                                "window.__corsiaShowServiceFailure?.({reason});"
                            ));
                        }
                    }
                }
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
                "HTTP/1.1 200 OK\r\n\r\n{\"apple_silicon\":true,\"macos_supported\":true,\"ram_tier\":\"eligible\",\"free_disk_gb\":123.0,\"app_location\":\"applications\",\"local_eligible\":true,\"work_folder\":\"~/corsia\",\"required_username\":null,\"existing_omlx\":false,\"ready_ai\":null}",
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
