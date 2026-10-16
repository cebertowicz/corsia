//! Explicit window, menu, and programmatic-exit routing.
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::Manager;

pub const QUIT_MENU_ID: &str = "corsia-quit";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Input {
    CloseMain,
    Reopen,
    Quit,
    ExitRequested(Option<i32>),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Hide,
    ShowAndFocus,
    CheckQuit,
    KeepAlive,
    AllowExit,
}

pub fn route(input: Input, quitting: bool) -> Action {
    match input {
        Input::CloseMain => Action::Hide,
        Input::Reopen => Action::ShowAndFocus,
        Input::Quit => Action::CheckQuit,
        Input::ExitRequested(None) => Action::KeepAlive,
        Input::ExitRequested(Some(_)) if quitting => Action::AllowExit,
        Input::ExitRequested(Some(_)) => Action::CheckQuit,
    }
}

fn quit_if_unlocked(
    operation_state: impl FnOnce() -> Result<bool, String>,
    unregister: impl FnOnce() -> Result<(), String>,
) -> Result<bool, String> {
    if super::should_prevent_quit(operation_state()) {
        return Ok(false);
    }
    unregister()?;
    Ok(true)
}

fn quit_and_mark_cleanup(
    attempted: &AtomicBool,
    operation_state: impl FnOnce() -> Result<bool, String>,
    unregister: impl FnOnce() -> Result<(), String>,
) -> Result<bool, String> {
    let quit = quit_if_unlocked(operation_state, unregister)?;
    if quit {
        attempted.store(true, Ordering::Release);
    }
    Ok(quit)
}

#[derive(Debug, PartialEq, Eq)]
enum QuitOutcome {
    Exit,
    KeepWindow,
}

const RETRY_LOCKED_ERROR: &str = "CORSIA is still completing an operation";

fn resolve_quit(
    attempted: &AtomicBool,
    operation_state: impl FnOnce() -> Result<bool, String>,
    unregister: impl FnOnce() -> Result<(), String>,
) -> Result<QuitOutcome, String> {
    match quit_and_mark_cleanup(attempted, operation_state, unregister)? {
        true => Ok(QuitOutcome::Exit),
        false => Ok(QuitOutcome::KeepWindow),
    }
}

pub fn show_main(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn quit_failure_url(dev_url: Option<&tauri::Url>) -> tauri::Url {
    let mut url = dev_url.cloned().unwrap_or_else(|| {
        tauri::Url::parse("tauri://localhost/index.html").expect("static shell URL is valid")
    });
    url.set_fragment(Some("quit-failure"));
    url
}

fn show_quit_failure(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        // The main webview may already be on the backend UI. Return to the
        // bundled failure surface rather than evaluating a startup-only hook.
        let dev_url = if tauri::is_dev() { app.config().build.dev_url.as_ref() } else { None };
        if let Err(error) = window.navigate(quit_failure_url(dev_url)) {
            log::error!("could not show the background-service stop failure: {error}");
        }
    }
    show_main(app);
}

#[tauri::command]
pub fn retry_quit(app: tauri::AppHandle) -> Result<(), String> {
    let outcome = quit_outcome(&app)?;
    retry_quit_result(outcome)?;
    finish_quit(&app);
    Ok(())
}

pub(super) fn service_port(app: &tauri::AppHandle, service: &super::AppServiceState) -> u16 {
    service.app_dir.lock().ok().and_then(|dir| dir.clone())
        .or_else(|| super::default_app_dir(app).ok())
        .as_deref().and_then(super::first_run::recorded_port)
        .unwrap_or(super::DEFAULT_SERVICE_PORT)
}

#[derive(Debug, PartialEq, Eq)]
enum ExitCleanup {
    AlreadyHandled,
    OperationLocked,
    Unregistered,
}

fn native_exit_cleanup(
    attempted: &AtomicBool,
    operation_state: impl FnOnce() -> Result<bool, String>,
    unregister: impl FnOnce() -> Result<(), String>,
) -> Result<ExitCleanup, String> {
    if attempted.swap(true, Ordering::AcqRel) {
        return Ok(ExitCleanup::AlreadyHandled);
    }
    if super::should_prevent_quit(operation_state()) {
        return Ok(ExitCleanup::OperationLocked);
    }
    unregister()?;
    Ok(ExitCleanup::Unregistered)
}

pub fn on_exit(app: &tauri::AppHandle) {
    let Some(service) = app.try_state::<super::AppServiceState>() else { return; };
    match native_exit_cleanup(
        &service.exit_cleanup_attempted,
        || super::operation_is_active(service_port(app, &service)),
        super::unregister_service,
    ) {
        Ok(ExitCleanup::OperationLocked) => log::info!("CORSIA is exiting with an active operation; background service remains registered and running."),
        Err(error) => log::error!("could not unregister background service during native Exit: {error}"),
        _ => {}
    }
}

pub fn request_quit(app: &tauri::AppHandle) {
    let service = app.state::<super::AppServiceState>();
    if service.adoption_in_progress.load(Ordering::Acquire) {
        service.adoption_cancel_requested.store(true, Ordering::Release);
        return;
    }
    let app = app.clone();
    std::thread::spawn(move || {
        if let Err(error) = quit(&app) {
            log::error!("could not quit CORSIA cleanly: {error}");
            show_quit_failure(&app);
        }
    });
}

fn quit_outcome(app: &tauri::AppHandle) -> Result<QuitOutcome, String> {
    let service = app.state::<super::AppServiceState>();
    let port = service_port(app, &service);
    resolve_quit(
        &service.exit_cleanup_attempted,
        || super::operation_is_active(port),
        super::unregister_service,
    )
}

fn finish_quit(app: &tauri::AppHandle) {
    let service = app.state::<super::AppServiceState>();
    service.quitting.store(true, Ordering::Relaxed);
    app.exit(0);
}

fn retry_quit_result(outcome: QuitOutcome) -> Result<(), String> {
    match outcome {
        QuitOutcome::Exit => Ok(()),
        QuitOutcome::KeepWindow => Err(RETRY_LOCKED_ERROR.into()),
    }
}

fn quit(app: &tauri::AppHandle) -> Result<(), String> {
    match quit_outcome(app)? {
        QuitOutcome::Exit => {
            finish_quit(app);
            Ok(())
        }
        QuitOutcome::KeepWindow => {
            show_main(app);
            Ok(())
        }
    }
}

#[cfg(target_os = "macos")]
pub fn menu(app: &tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    use tauri::menu::{Menu, MenuItem};
    let menu = Menu::default(app)?;
    // Locked Tauri 2.12.3 puts the native Quit last in its first (app) submenu.
    // Preserve all default menus, replacing only the terminate: action.
    let items = menu.items()?;
    let app_menu = items
        .first()
        .and_then(|item| item.as_submenu())
        .ok_or_else(|| tauri::Error::AssetNotFound("application menu".into()))?;
    app_menu.remove_at(app_menu.items()?.len() - 1)?;
    app_menu.append(&MenuItem::with_id(
        app,
        QUIT_MENU_ID,
        "Quit CORSIA",
        true,
        Some("Cmd+Q"),
    )?)?;
    Ok(menu)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[test]
    fn close_reopen_and_exit_have_distinct_routes() {
        assert_eq!(route(Input::CloseMain, false), Action::Hide);
        assert_eq!(route(Input::Reopen, false), Action::ShowAndFocus);
        assert_eq!(route(Input::Quit, false), Action::CheckQuit);
        for quitting in [false, true] {
            assert_eq!(route(Input::ExitRequested(None), quitting), Action::KeepAlive);
        }
        assert_eq!(
            route(Input::ExitRequested(Some(0)), false),
            Action::CheckQuit
        );
        assert_eq!(route(Input::ExitRequested(Some(7)), true), Action::AllowExit);
    }

    #[test]
    fn native_exit_unregisters_unlocked_but_retains_locked_service() {
        let unlocked = AtomicBool::new(false);
        let calls = RefCell::new(Vec::new());
        assert_eq!(native_exit_cleanup(&unlocked,
            || { calls.borrow_mut().push("lock"); Ok(false) },
            || { calls.borrow_mut().push("unregister"); Ok(()) }), Ok(ExitCleanup::Unregistered));
        assert_eq!(*calls.borrow(), ["lock", "unregister"]);
        let locked = AtomicBool::new(false);
        assert_eq!(native_exit_cleanup(&locked, || Ok(true), || panic!("retain service")), Ok(ExitCleanup::OperationLocked));
        assert_eq!(native_exit_cleanup(&locked, || panic!("only once"), || panic!("only once")), Ok(ExitCleanup::AlreadyHandled));
    }

    #[test]
    fn native_exit_does_not_unregister_again_after_command_quit() {
        let attempted = AtomicBool::new(false);
        assert_eq!(quit_and_mark_cleanup(&attempted, || Ok(false), || Ok(())), Ok(true));
        assert_eq!(native_exit_cleanup(&attempted, || panic!("already checked"), || panic!("already unregistered")), Ok(ExitCleanup::AlreadyHandled));
    }

    #[test]
    fn failed_command_quit_leaves_native_exit_cleanup_available() {
        let attempted = AtomicBool::new(false);
        assert_eq!(
            quit_and_mark_cleanup(&attempted, || Ok(false), || Err("refused".into())),
            Err("refused".into())
        );
        assert_eq!(
            native_exit_cleanup(&attempted, || Ok(false), || Ok(())),
            Ok(ExitCleanup::Unregistered)
        );
    }

    #[test]
    fn quit_failure_returns_to_the_local_recoverable_failure_surface() {
        let url = quit_failure_url(None);
        assert_eq!(url.as_str(), "tauri://localhost/index.html#quit-failure");
        let dev = tauri::Url::parse("http://localhost:5273/").unwrap();
        assert_eq!(quit_failure_url(Some(&dev)).as_str(), "http://localhost:5273/#quit-failure");
        let shell = include_str!("../../dist-shell/index.html");
        assert!(shell.contains("CORSIA couldn’t stop its background service."));
        assert!(shell.contains("invoke('retry_quit')"));
    }

    #[test]
    fn quit_checks_operation_lock_before_unregistering() {
        let calls = RefCell::new(Vec::new());
        assert_eq!(
            quit_if_unlocked(
                || {
                    calls.borrow_mut().push("lock");
                    Ok(false)
                },
                || {
                    calls.borrow_mut().push("unregister");
                    Ok(())
                },
            ),
            Ok(true)
        );
        assert_eq!(*calls.borrow(), ["lock", "unregister"]);
        assert_eq!(
            quit_if_unlocked(|| Ok(true), || panic!("must retain service")),
            Ok(false)
        );
        assert_eq!(
            quit_if_unlocked(|| Ok(false), || Err("refused".into())),
            Err("refused".into())
        );
    }

    #[test]
    fn locked_command_quit_keeps_the_window_and_retains_the_service() {
        let attempted = AtomicBool::new(false);
        assert_eq!(
            resolve_quit(&attempted, || Ok(true), || panic!("must retain service")),
            Ok(QuitOutcome::KeepWindow)
        );
        assert!(!attempted.load(Ordering::Acquire));
    }

    #[test]
    fn locked_retry_returns_an_error_so_the_failure_shell_is_retryable() {
        let attempted = AtomicBool::new(false);
        let outcome = resolve_quit(&attempted, || Ok(true), || panic!("must retain service"));

        assert_eq!(
            outcome.and_then(retry_quit_result),
            Err(RETRY_LOCKED_ERROR.into())
        );
        assert!(!attempted.load(Ordering::Acquire));
    }
}
