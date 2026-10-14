// CORSIA desktop shell.
//
// The bundle owns one local backend for the lifetime of its window. The
// static shell shows startup progress until the bundled service passes its
// loopback health check, then the WebView navigates to the local CORSIA UI.

use std::{
    env, fs,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use serde::Serialize;
use tauri::Manager;

const DEFAULT_SERVICE_PORT: u16 = 47_895;
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);

#[derive(Default)]
struct AppServiceState {
    service: Mutex<ServiceState>,
}

#[derive(Default)]
struct ServiceState {
    child: Option<Child>,
    port: Option<u16>,
    state_dir: Option<PathBuf>,
}

#[derive(Serialize)]
struct ServiceStartup {
    url: String,
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

fn default_state_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    if let Some(path) = env::var_os("CORSIA_STATE_DIR").filter(|path| !path.is_empty()) {
        return Ok(PathBuf::from(path));
    }

    // Keep the shell's default aligned with backend/infra/platform_paths.py.
    app.path()
        .home_dir()
        .map(|home| {
            home.join("Library")
                .join("Application Support")
                .join("com.cebertowicz.corsia")
                .join("state")
        })
        .map_err(|error| format!("could not resolve the CORSIA state directory: {error}"))
}

fn service_log_path(state_dir: &PathBuf) -> PathBuf {
    state_dir.join("logs").join("service.log")
}

fn preferred_port() -> u16 {
    env::var("CORSIA_DESKTOP_PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .filter(|port| *port != 0)
        .unwrap_or(DEFAULT_SERVICE_PORT)
}

fn select_port() -> Result<u16, String> {
    let preferred = preferred_port();
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

fn stop_child(state: &mut ServiceState) {
    if let Some(mut child) = state.child.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    state.port = None;
}

fn service_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

fn bundled_resource_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    match app.path().resource_dir() {
        Ok(path) => Ok(path),
        Err(path_error) => {
            // `resource_dir` is the canonical Tauri path. Fall back to the
            // app-bundle-relative location when macOS cannot resolve the
            // extracted bundle through its path APIs.
            let executable = env::current_exe()
                .map_err(|error| format!("could not resolve the app executable: {error}"))?;
            let resources = executable
                .parent()
                .and_then(|macos| macos.parent())
                .map(|contents| contents.join("Resources"))
                .filter(|path| path.is_dir())
                .ok_or_else(|| {
                    format!(
                        "could not resolve bundled resources through Tauri ({path_error}) or from executable {}",
                        executable.display()
                    )
                })?;
            Ok(resources)
        }
    }
}

fn wait_for_service(service: &AppServiceState, port: u16) -> Result<(), String> {
    let deadline = Instant::now() + HEALTH_TIMEOUT;
    loop {
        if probe_health(port) {
            return Ok(());
        }

        {
            let mut state = service
                .service
                .lock()
                .map_err(|_| "service state is unavailable")?;
            let exited = match state.child.as_mut() {
                Some(child) => child
                    .try_wait()
                    .map_err(|error| format!("could not inspect bundled service state: {error}"))?,
                None => return Err("the bundled service was stopped".into()),
            };
            if exited.is_some() {
                state.child = None;
                state.port = None;
                return Err("the bundled service exited before it was ready".into());
            }
        }

        if Instant::now() >= deadline {
            let mut state = service
                .service
                .lock()
                .map_err(|_| "service state is unavailable")?;
            stop_child(&mut state);
            return Err("the bundled service did not become ready in time".into());
        }
        thread::sleep(HEALTH_POLL_INTERVAL);
    }
}

fn start_service(
    app: &tauri::AppHandle,
    service: &AppServiceState,
) -> Result<ServiceStartup, String> {
    let state_dir = default_state_dir(app)?;
    fs::create_dir_all(state_dir.join("logs"))
        .map_err(|error| format!("could not create the CORSIA service log directory: {error}"))?;
    let port = select_port()?;

    {
        let mut state = service
            .service
            .lock()
            .map_err(|_| "service state is unavailable")?;
        if let Some(child) = state.child.as_mut() {
            if child
                .try_wait()
                .map_err(|error| error.to_string())?
                .is_none()
            {
                return Ok(ServiceStartup {
                    url: service_url(state.port.unwrap_or(port)),
                });
            }
        }
        stop_child(&mut state);
        let launcher = bundled_resource_dir(app)?.join("bin").join("corsia-service");
        state.child = Some(
            Command::new(launcher)
                .args(["--host", "127.0.0.1", "--port", &port.to_string()])
                .env("CORSIA_STATE_DIR", &state_dir)
                .spawn()
                .map_err(|error| format!("could not start the bundled CORSIA service: {error}"))?,
        );
        state.port = Some(port);
        state.state_dir = Some(state_dir);
    }

    wait_for_service(service, port)?;
    Ok(ServiceStartup {
        url: service_url(port),
    })
}

fn stop_service(app: &tauri::AppHandle) {
    let service = app.state::<AppServiceState>();
    if let Ok(mut state) = service.service.lock() {
        stop_child(&mut state);
    };
}

#[tauri::command]
fn start_local_service(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
) -> Result<ServiceStartup, String> {
    start_service(&app, &service)
}

#[tauri::command]
fn get_service_log_path(
    app: tauri::AppHandle,
    service: tauri::State<'_, AppServiceState>,
) -> Result<String, String> {
    let state_dir = service
        .service
        .lock()
        .map_err(|_| "service state is unavailable")?
        .state_dir
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
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            start_local_service,
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
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                stop_service(window.app_handle());
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
                    Ok(startup) => {
                        if let Some(window) = app_handle.get_webview_window("main") {
                            if let Err(error) =
                                window.eval(format!("window.location.replace('{}');", startup.url))
                            {
                                log::error!(
                                    "could not navigate to the local CORSIA service: {error}"
                                );
                            }
                        }
                    }
                    Err(error) => {
                        log::error!("bundled CORSIA service did not start: {error}");
                        if let Some(window) = app_handle.get_webview_window("main") {
                            let _ = window.eval("window.__corsiaShowServiceFailure?.();");
                        }
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
