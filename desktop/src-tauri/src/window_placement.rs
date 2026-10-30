//! Where the main window opens. `tauri-plugin-window-state` restores the size and position the person last
//! left it at; this adds the cases it leaves to the OS: a first launch, and a saved place that no connected
//! screen shows, open at the default size centered on the screen, so setup is a normal-sized window that
//! fits its widest layout. When setup finishes, a window still at that default size grows to fill the
//! screen's usable area, so the app itself opens large.

use std::collections::HashMap;
use std::path::Path;

use serde::Deserialize;
use tauri::{Manager, PhysicalPosition, PhysicalSize};

const MAIN: &str = "main";
/// The plugin's default file name, in the app config directory.
const STATE_FILE: &str = ".window-state.json";

/// A rectangle in physical pixels.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
}

impl Rect {
    fn intersects(&self, other: &Rect) -> bool {
        let (ax, ay) = (i64::from(self.x), i64::from(self.y));
        let (bx, by) = (i64::from(other.x), i64::from(other.y));
        ax < bx + i64::from(other.width)
            && bx < ax + i64::from(self.width)
            && ay < by + i64::from(other.height)
            && by < ay + i64::from(self.height)
    }
}

#[derive(Deserialize)]
struct SavedWindow {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

/// The place the plugin saved for the main window, when it saved a real one.
pub fn saved_main_window(state_dir: &Path) -> Option<Rect> {
    let text = std::fs::read_to_string(state_dir.join(STATE_FILE)).ok()?;
    saved_from_json(&text)
}

fn saved_from_json(text: &str) -> Option<Rect> {
    let mut windows: HashMap<String, SavedWindow> = serde_json::from_str(text).ok()?;
    let saved = windows.remove(MAIN)?;
    (saved.width > 0 && saved.height > 0).then_some(Rect { x: saved.x, y: saved.y, width: saved.width, height: saved.height })
}

/// The saved place is kept only while some connected screen shows part of it.
pub fn saved_place_is_visible(saved: Option<Rect>, screens: &[Rect]) -> bool {
    saved.is_some_and(|rect| screens.iter().any(|screen| rect.intersects(screen)))
}

/// The main window's default size in logical pixels, read from `tauri.conf.json` so the config stays its one
/// home (its width is also the setup layout's widest breakpoint).
fn configured_default(config: &tauri::Config) -> Option<(f64, f64)> {
    let window = config.app.windows.iter().find(|window| window.label == MAIN)?;
    Some((window.width, window.height))
}

/// The default window, centered in a screen's usable area and shrunk to fit a smaller one.
pub fn centered_default(area: Rect, scale: f64, logical: (f64, f64)) -> Rect {
    let fit = |logical: f64, available: u32| ((logical * scale).round() as u32).min(available);
    let (width, height) = (fit(logical.0, area.width), fit(logical.1, area.height));
    Rect {
        x: area.x + ((area.width - width) / 2) as i32,
        y: area.y + ((area.height - height) / 2) as i32,
        width,
        height,
    }
}

/// Setup is over: a window the person hasn't resized (still the default size) fills the usable area. A
/// resized, maximized or full-screen window is left alone.
pub fn grow_after_setup(current: (u32, u32), default: Rect, area: Rect, maximized_or_full: bool) -> Option<Rect> {
    let untouched = current == (default.width, default.height);
    (untouched && !maximized_or_full && (default.width, default.height) != (area.width, area.height)).then_some(area)
}

fn work_area(monitor: &tauri::Monitor) -> Rect {
    let area = monitor.work_area();
    Rect { x: area.position.x, y: area.position.y, width: area.size.width, height: area.size.height }
}

/// Fill the usable area when setup finishes (the shell's `finish_setup_window` command).
pub fn expand_main_window_after_setup(window: &tauri::WebviewWindow) {
    let Some(monitor) = window.current_monitor().ok().flatten().or_else(|| window.primary_monitor().ok().flatten()) else {
        log::warn!("setup finished, but no screen was found; the main window keeps its size");
        return;
    };
    let (Ok(size), Ok(maximized), Ok(full)) = (window.inner_size(), window.is_maximized(), window.is_fullscreen()) else {
        log::warn!("setup finished, but the main window's size and state could not be read; it keeps its size");
        return;
    };
    let Some(logical) = configured_default(window.app_handle().config()) else {
        log::warn!("setup finished, but the config has no main window default size; the window keeps its size");
        return;
    };
    let area = work_area(&monitor);
    let default = centered_default(area, monitor.scale_factor(), logical);
    let Some(fill) = grow_after_setup((size.width, size.height), default, area, maximized || full) else {
        return;
    };
    log::info!("setup finished: the main window grows from {}x{} to the usable area {}x{}", size.width, size.height, fill.width, fill.height);
    if let Err(error) = window.set_position(PhysicalPosition::new(fill.x, fill.y)) {
        log::warn!("could not move the main window to the usable area: {error}");
    }
    if let Err(error) = window.set_size(PhysicalSize::new(fill.width, fill.height)) {
        log::warn!("could not size the main window to the usable area: {error}");
    }
}

/// Open the default window centered when there is no saved place to restore.
pub fn place_main_window(app: &tauri::App, state_dir: &Path) {
    let Some(window) = app.get_webview_window(MAIN) else {
        log::warn!("main window not found; leaving its placement to the OS");
        return;
    };
    let monitors = match window.available_monitors() {
        Ok(monitors) => monitors,
        Err(error) => {
            log::warn!("could not list the connected screens; leaving the main window's placement to the OS: {error}");
            return;
        }
    };
    let screens: Vec<Rect> = monitors
        .iter()
        .map(work_area)
        .collect();
    let visible = saved_place_is_visible(saved_main_window(state_dir), &screens);
    if visible {
        if let (Ok(size), Ok(position)) = (window.outer_size(), window.outer_position()) {
            log::info!("main window {}x{} at ({}, {}) restored from the saved place", size.width, size.height, position.x, position.y);
        }
    } else if let Some(monitor) = window.current_monitor().ok().flatten().or_else(|| window.primary_monitor().ok().flatten()) {
        let Some(logical) = configured_default(app.config()) else {
            log::warn!("config has no main window default size; leaving its placement to the OS");
            return;
        };
        let place = centered_default(work_area(&monitor), monitor.scale_factor(), logical);
        log::info!("main window: no saved place on a connected screen; opening {}x{} centered", place.width, place.height);
        if let Err(error) = window.set_size(PhysicalSize::new(place.width, place.height)) {
            log::warn!("could not size the main window to its default: {error}");
        }
        if let Err(error) = window.set_position(PhysicalPosition::new(place.x, place.y)) {
            log::warn!("could not center the main window: {error}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCREEN: Rect = Rect { x: 0, y: 0, width: 2560, height: 1415 };
    const SECOND: Rect = Rect { x: 2560, y: 0, width: 1920, height: 1055 };
    const DEFAULT: (f64, f64) = (1280.0, 900.0);

    #[test]
    fn the_default_size_is_the_one_in_the_shipped_config() {
        let raw = include_str!("../tauri.conf.json");
        let json: serde_json::Value = serde_json::from_str(raw).expect("tauri.conf.json parses");
        let config: tauri::Config = serde_json::from_value(json.clone()).expect("tauri.conf.json is a tauri config");
        let main = &json["app"]["windows"][0];
        assert_eq!(
            configured_default(&config),
            Some((main["width"].as_f64().unwrap(), main["height"].as_f64().unwrap()))
        );
        assert_eq!(configured_default(&config), Some(DEFAULT));
    }

    fn saved(x: i32, y: i32) -> Option<Rect> {
        Some(Rect { x, y, width: 1000, height: 700 })
    }

    #[test]
    fn a_first_launch_has_no_saved_place_so_it_opens_at_the_default() {
        assert!(!saved_place_is_visible(None, &[SCREEN]));
        assert_eq!(saved_from_json("{}"), None);
        assert_eq!(saved_from_json(r#"{"main":{"x":0,"y":0,"width":0,"height":0}}"#), None);
        assert_eq!(saved_main_window(Path::new("/nonexistent/corsia")), None);
    }

    #[test]
    fn a_saved_place_on_a_connected_screen_is_restored() {
        let json = r#"{"main":{"width":1000,"height":700,"x":120,"y":80,"prev_x":0,"prev_y":0,"maximized":false,"visible":true,"decorated":true,"fullscreen":false}}"#;
        assert_eq!(saved_from_json(json), saved(120, 80));
        assert!(saved_place_is_visible(saved(120, 80), &[SCREEN]));
        assert!(saved_place_is_visible(saved(2800, 100), &[SCREEN, SECOND]));
    }

    #[test]
    fn a_saved_place_on_a_screen_no_longer_connected_falls_back_to_the_default() {
        assert!(!saved_place_is_visible(saved(2800, 100), &[SCREEN]));
        assert!(!saved_place_is_visible(saved(-3000, 0), &[SCREEN]));
    }

    #[test]
    fn the_default_window_is_centered_at_the_setup_layouts_widest_width() {
        let retina = centered_default(Rect { x: 0, y: 38, width: 3000, height: 1900 }, 2.0, DEFAULT);
        assert_eq!(retina, Rect { x: 220, y: 88, width: 2560, height: 1800 });
        let one_x = centered_default(Rect { x: 0, y: 25, width: 1920, height: 1055 }, 1.0, DEFAULT);
        assert_eq!(one_x, Rect { x: 320, y: 25 + 77, width: 1280, height: 900 });
    }

    #[test]
    fn the_default_window_shrinks_to_a_screen_too_small_for_it() {
        let small = Rect { x: 0, y: 25, width: 1100, height: 700 };
        assert_eq!(centered_default(small, 1.0, DEFAULT), small);
    }

    #[test]
    fn setup_finishing_grows_only_a_window_still_at_its_default_size() {
        let area = Rect { x: 0, y: 25, width: 2560, height: 1415 };
        let default = centered_default(area, 1.0, DEFAULT);
        assert_eq!(grow_after_setup((1280, 900), default, area, false), Some(area));
        // Resized by the person, maximized, or full screen: left alone.
        assert_eq!(grow_after_setup((1500, 900), default, area, false), None);
        assert_eq!(grow_after_setup((1280, 900), default, area, true), None);
        // A screen that only fits the default has nothing to grow into.
        let snug = Rect { x: 0, y: 25, width: 1280, height: 900 };
        assert_eq!(grow_after_setup((1280, 900), centered_default(snug, 1.0, DEFAULT), snug, false), None);
    }
}
