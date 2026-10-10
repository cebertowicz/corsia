// CORSIA desktop shell.
//
// Thin Tauri wrapper that hosts the corsia web UI inside a native macOS
// window with vibrancy + traffic-lights overlay. The UI ships from the
// corsia backend (HTTP); this binary only handles window chrome,
// remembering which server URL to load, and the eventual auto-update path.
//
// First launch: load `dist-shell/index.html` (a tiny URL picker). User
// pastes their server URL, we persist it to
// ~/Library/Application Support/com.cebertowicz.corsia/config.json, and navigate the
// window to that URL. Subsequent launches skip the picker and load
// straight to the saved URL.

use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use tauri::Manager;

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

#[derive(Default, Serialize, Deserialize)]
struct Config {
    server_url: Option<String>,
}

fn config_path(app: &tauri::AppHandle) -> PathBuf {
    let dir = app
        .path()
        .app_config_dir()
        .expect("no app config dir");
    fs::create_dir_all(&dir).ok();
    dir.join("config.json")
}

fn load_config(app: &tauri::AppHandle) -> Config {
    fs::read_to_string(config_path(app))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_config(app: &tauri::AppHandle, cfg: &Config) -> Result<(), String> {
    let json = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    fs::write(config_path(app), json).map_err(|e| e.to_string())
}

/// Persists the user's server URL. The picker's JS handles the actual
/// navigation via window.location after this resolves — we only store
/// the URL so subsequent launches skip the picker.
#[tauri::command]
fn set_server_url(app: tauri::AppHandle, url: String) -> Result<(), String> {
    // Validate the URL is at least parseable before persisting.
    let _: url::Url = url.parse().map_err(|e: url::ParseError| e.to_string())?;
    let mut cfg = load_config(&app);
    cfg.server_url = Some(url);
    save_config(&app, &cfg)
}

/// Returns the saved URL (if any) so the picker page can prefill.
#[tauri::command]
fn get_server_url(app: tauri::AppHandle) -> Option<String> {
    load_config(&app).server_url
}

/// Clears the saved URL — next launch shows the picker again.
#[tauri::command]
fn reset_server_url(app: tauri::AppHandle) -> Result<(), String> {
    save_config(&app, &Config::default())
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
        .plugin(tauri_plugin_log::Builder::default().build())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            set_server_url,
            get_server_url,
            reset_server_url,
            start_window_drag,
        ])
        .on_page_load(|webview, _payload| {
            // Inject the drag handler on every page load. Idempotent —
            // the script self-guards via __corsia_drag_handler_installed
            // so multiple injections don't double-bind.
            let _ = webview.eval(DRAG_INIT_SCRIPT);
            // Re-apply traffic-light position after each navigation —
            // the picker → corsia redirect rebuilds the view tree and
            // can reset button positions on some macOS versions.
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
            // The default window loads dist-shell/index.html (the picker).
            // The picker's JS reads the saved URL via get_server_url and,
            // if set, immediately redirects via window.location.href —
            // which the WebView handles natively without hitting Tauri 2's
            // external-URL navigation guards.
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
