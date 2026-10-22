//! Where the main window opens. `tauri-plugin-window-state` restores the size and position the person last
//! left it at; this adds the cases it leaves to the OS: a first launch fills the screen's usable area, and a
//! saved place that no connected screen shows falls back to the same fill.

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

/// Fill the usable area of the main window's screen when there is no saved place to restore.
pub fn place_main_window(app: &tauri::App, state_dir: &Path) {
    let Some(window) = app.get_webview_window(MAIN) else { return };
    let Ok(monitors) = window.available_monitors() else { return };
    let screens: Vec<Rect> = monitors
        .iter()
        .map(|monitor| {
            let area = monitor.work_area();
            Rect { x: area.position.x, y: area.position.y, width: area.size.width, height: area.size.height }
        })
        .collect();
    let visible = saved_place_is_visible(saved_main_window(state_dir), &screens);
    if visible {
        if let (Ok(size), Ok(position)) = (window.outer_size(), window.outer_position()) {
            log::info!("main window {}x{} at ({}, {}) restored from the saved place", size.width, size.height, position.x, position.y);
        }
    } else if let Some(monitor) = window.current_monitor().ok().flatten().or_else(|| window.primary_monitor().ok().flatten()) {
        let area = monitor.work_area();
        log::info!("main window: no saved place on a connected screen; filling the usable area {}x{}", area.size.width, area.size.height);
        if let Err(error) = window.set_position(PhysicalPosition::new(area.position.x, area.position.y)) {
            log::warn!("could not move the main window to the usable area: {error}");
        }
        if let Err(error) = window.set_size(PhysicalSize::new(area.size.width, area.size.height)) {
            log::warn!("could not size the main window to the usable area: {error}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCREEN: Rect = Rect { x: 0, y: 0, width: 2560, height: 1415 };
    const SECOND: Rect = Rect { x: 2560, y: 0, width: 1920, height: 1055 };

    fn saved(x: i32, y: i32) -> Option<Rect> {
        Some(Rect { x, y, width: 1000, height: 700 })
    }

    #[test]
    fn a_first_launch_has_no_saved_place_so_it_fills_the_screen() {
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
    fn a_saved_place_on_a_screen_no_longer_connected_falls_back_to_the_fill() {
        assert!(!saved_place_is_visible(saved(2800, 100), &[SCREEN]));
        assert!(!saved_place_is_visible(saved(-3000, 0), &[SCREEN]));
    }
}
