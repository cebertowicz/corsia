// CORSIA desktop shell.
//
// The bundle owns one local backend for the lifetime of its window. The
// static shell shows startup progress until the bundled service passes its
// loopback health check, then the WebView navigates to the local CORSIA UI.

use std::{
    env,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use serde::Serialize;
use tauri::Manager;

mod first_run;
mod service_management;

pub fn service_command_from_environment() -> Option<i32> {
    service_management::command_from_environment()
}

const DEFAULT_SERVICE_PORT: u16 = 47_895;
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);

#[derive(Default)]
struct AppServiceState {
    state_dir: Mutex<Option<PathBuf>>,
    quitting: AtomicBool,
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

fn default_state_dir_for_home(home: &Path) -> PathBuf {
    home.join("Library")
        .join("Application Support")
        .join("CORSIA")
}

fn default_state_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("CORSIA_STATE_DIR").filter(|path| !path.is_empty()) {
        return Ok(PathBuf::from(path));
    }

    // Keep the shell's default aligned with backend/infra/platform_paths.py.
    app.path()
        .home_dir()
        .map(|home| default_state_dir_for_home(&home))
        .map_err(|error| format!("could not resolve the CORSIA state directory: {error}"))
}

fn service_log_path(state_dir: &PathBuf) -> PathBuf {
    state_dir.join("logs").join("service.log")
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

fn probe_health(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .is_ok_and(|_| response.starts_with("HTTP/1.1 200"))
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

fn operation_is_active(port: u16) -> Result<bool, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(250))
        .map_err(|_| "the CORSIA service is unavailable")?;
    stream
        .set_read_timeout(Some(Duration::from_millis(500)))
        .map_err(|error| format!("could not read CORSIA operation state: {error}"))?;
    stream
        .set_write_timeout(Some(Duration::from_millis(500)))
        .map_err(|error| format!("could not read CORSIA operation state: {error}"))?;
    stream
        .write_all(b"GET /api/system/operation-active HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .map_err(|_| "the CORSIA service is unavailable")?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|_| "the CORSIA service is unavailable")?;
    operation_is_active_response(&response)
}

fn unregister_service() -> Result<(), String> {
    let executable = env::current_exe()
        .map_err(|error| format!("could not resolve CORSIA executable: {error}"))?;
    let status = Command::new(executable)
        .args(["--service", "unregister"])
        .status()
        .map_err(|error| format!("could not unregister the CORSIA service: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("CORSIA service unregistration exited {status}"))
    }
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

fn wait_for_service(port: u16) -> Result<(), String> {
    let deadline = Instant::now() + HEALTH_TIMEOUT;
    while Instant::now() < deadline {
        if probe_health(port) {
            return Ok(());
        }
        thread::sleep(HEALTH_POLL_INTERVAL);
    }
    Err("the CORSIA service did not become ready in time".into())
}

fn register_or_report_status() -> Result<service_management::ServiceStatus, String> {
    match service_management::service_status()? {
        service_management::ServiceStatus::NotRegistered => {
            service_management::register_from_app()?;
            service_management::service_status()
        }
        status => Ok(status),
    }
}

fn start_service(
    app: &tauri::AppHandle,
    service: &AppServiceState,
) -> Result<ServiceStartup, String> {
    let state_dir = default_state_dir(app)?;
    // Decide about the workspace before anything is written: a refusal leaves
    // the computer exactly as it was.
    let first_run = if first_run::environment_names_the_roots() {
        first_run::FirstRun::Configured
    } else {
        let home = app
            .path()
            .home_dir()
            .map_err(|error| format!("could not resolve the home folder: {error}"))?;
        first_run::plan(&home, &state_dir)?
    };
    if let first_run::FirstRun::ExistingInstall { reason } = first_run {
        return Err(reason);
    }
    let port = select_port(first_run::recorded_port(&state_dir))?;
    if let first_run::FirstRun::Fresh { workspace } = first_run {
        let resources = bundled_resource_dir(app).ok();
        first_run::apply(&workspace, &state_dir, port, |path| {
            first_run::git_init(
                path,
                resources.as_deref(),
                first_run::command_line_tools_installed,
            )
        })?;
    }
    *service
        .state_dir
        .lock()
        .map_err(|_| "service state is unavailable")? = Some(state_dir);

    match register_or_report_status()? {
        service_management::ServiceStatus::Enabled => {
            wait_for_service(port)?;
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
    let state_dir = service
        .state_dir
        .lock()
        .map_err(|_| "service state is unavailable")?
        .clone()
        .unwrap_or(default_state_dir(&app)?);
    Ok(service_log_path(&state_dir).display().to_string())
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
            open_login_items,
            get_service_log_path,
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
        .on_window_event(|window, event| {
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
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            let tauri::RunEvent::ExitRequested { api, .. } = event else {
                return;
            };
            let service = app_handle.state::<AppServiceState>();
            if service.quitting.load(Ordering::Relaxed) {
                return;
            }
            api.prevent_exit();
            let app_handle = app_handle.clone();
            thread::spawn(move || {
                let service = app_handle.state::<AppServiceState>();
                let state_dir = service
                    .state_dir
                    .lock()
                    .ok()
                    .and_then(|state_dir| state_dir.clone())
                    .or_else(|| default_state_dir(&app_handle).ok());
                let port = state_dir
                    .as_deref()
                    .and_then(first_run::recorded_port)
                    .unwrap_or(DEFAULT_SERVICE_PORT);
                if should_prevent_quit(operation_is_active(port)) {
                    if let Some(window) = app_handle.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                    return;
                }
                match unregister_service() {
                    Ok(()) => {
                        service.quitting.store(true, Ordering::Relaxed);
                        app_handle.exit(0);
                    }
                    Err(error) => {
                        log::error!("could not quit CORSIA cleanly: {error}");
                        if let Some(window) = app_handle.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                }
            });
        });
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;

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
    fn macos_default_state_dir_uses_corsia_not_the_legacy_identifier() {
        let home = Path::new("/tmp/corsia-desktop-state-test");
        let state_dir = default_state_dir_for_home(home);

        assert_eq!(
            state_dir,
            home.join("Library")
                .join("Application Support")
                .join("CORSIA")
        );
        assert!(!state_dir
            .to_string_lossy()
            .contains("com.cebertowicz.corsia"));
    }

}
