/// The shell operations that enter remote mode. They are real operations in production and fakes in unit tests, and
/// pairing and every launch that connects a saved remote implement the same trait, so there is one order and one
/// rollback.
pub trait EnterRemote {
    type Proxy;
    type Error;
    fn save_remote(&mut self, proxy: &Self::Proxy) -> Result<(), Self::Error>;
    fn grant_origin(&mut self, proxy: &Self::Proxy) -> Result<(), Self::Error>;
    fn unregister_service(&mut self) -> Result<(), Self::Error>;
    /// Puts the remote in front of the person: records the connected state and navigates the window.
    fn show(&mut self, proxy: &Self::Proxy) -> Result<(), Self::Error>;
    fn stop_proxy(&mut self, proxy: Self::Proxy);
    fn forget_remote(&mut self);
    /// Brings the local service back after `unregister_service` ran.
    fn restore_service(&mut self);
}

/// Enters remote mode with an already started proxy: the one definition of the order and of the rollback, used by
/// pairing and by every launch that connects a saved remote.
///
/// The order is save, grant, unregister, show, and each step is chosen so a failure never strands state:
///
/// 1. The remote is saved before its origin is granted any command, so no capability (which cannot be revoked) ever
///    exists for a remote the next launch would not know about.
/// 2. The local service is unregistered only once the remote is saved and its proxy is up, so a person is never left
///    with neither a local service nor a reachable remote.
/// 3. The window is shown last. A failure there comes after the local service was unregistered, so it is rolled
///    back like every other failure.
///
/// A failure at any step runs the whole rollback: the proxy stops, the remote this call saved is forgotten, and the
/// local service comes back if the call had reached the step that stops it. A launch that reconnects an already
/// saved remote implements `forget_remote` and `restore_service` as no-ops: the saved pairing outlives a transient
/// failure (Retry needs it) and the local service was stopped when the remote was first entered.
pub fn enter_remote<E: EnterRemote>(entry: &mut E, proxy: E::Proxy) -> Result<E::Proxy, E::Error> {
    let mut service_touched = false;
    match run_steps(entry, &proxy, &mut service_touched) {
        Ok(()) => Ok(proxy),
        Err(error) => {
            entry.stop_proxy(proxy);
            entry.forget_remote();
            if service_touched {
                entry.restore_service();
            }
            Err(error)
        }
    }
}

fn run_steps<E: EnterRemote>(entry: &mut E, proxy: &E::Proxy, service_touched: &mut bool) -> Result<(), E::Error> {
    entry.save_remote(proxy)?;
    entry.grant_origin(proxy)?;
    *service_touched = true;
    entry.unregister_service()?;
    entry.show(proxy)
}

/// Where a failed disconnect stopped, so the caller knows whether the person is
/// still paired.
#[derive(Debug, PartialEq, Eq)]
pub enum DisconnectError<E> {
    /// The saved remote is still saved and the proxy is untouched.
    NotForgotten(E),
    /// The remote is forgotten and its proxy is stopped; only the local service failed to come up.
    Forgotten(E),
}

/// Leaves remote mode: forget the remote first, so a failure to write the
/// config changes nothing and the page keeps its working proxy; only then
/// stop the proxy and bring the local service back. The disk and service
/// steps may wait off the window's event executor; the state handoff stays
/// between them. After the forget the person is local whatever happens next,
/// and a later launch starts the local service on its own.
pub async fn disconnect<
    E,
    S,
    Forget,
    ForgetFuture,
    Register,
    RegisterFuture,
    Start,
    StartFuture,
>(
    forget_remote: Forget,
    stop_proxy: impl FnOnce(),
    register_service: Register,
    start_service: Start,
) -> Result<S, DisconnectError<E>>
where
    Forget: FnOnce() -> ForgetFuture,
    ForgetFuture: std::future::Future<Output = Result<(), E>>,
    Register: FnOnce() -> RegisterFuture,
    RegisterFuture: std::future::Future<Output = Result<(), E>>,
    Start: FnOnce() -> StartFuture,
    StartFuture: std::future::Future<Output = Result<S, E>>,
{
    forget_remote().await.map_err(DisconnectError::NotForgotten)?;
    stop_proxy();
    register_service().await.map_err(DisconnectError::Forgotten)?;
    start_service().await.map_err(DisconnectError::Forgotten)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};

    type Events = Rc<RefCell<Vec<&'static str>>>;

    fn log(events: &Events, name: &'static str) {
        events.borrow_mut().push(name);
    }

    /// Records each step and fails at `failing_step`.
    struct Fake {
        events: Events,
        failing_step: Option<&'static str>,
    }

    impl Fake {
        fn step(&mut self, name: &'static str) -> Result<(), &'static str> {
            log(&self.events, name);
            if self.failing_step == Some(name) { Err(name) } else { Ok(()) }
        }
    }

    impl EnterRemote for Fake {
        type Proxy = &'static str;
        type Error = &'static str;
        fn save_remote(&mut self, _: &&'static str) -> Result<(), &'static str> { self.step("save") }
        fn grant_origin(&mut self, _: &&'static str) -> Result<(), &'static str> { self.step("grant") }
        fn unregister_service(&mut self) -> Result<(), &'static str> { self.step("unregister") }
        fn show(&mut self, _: &&'static str) -> Result<(), &'static str> { self.step("show") }
        fn stop_proxy(&mut self, _: &'static str) { log(&self.events, "stop_proxy") }
        fn forget_remote(&mut self) { log(&self.events, "forget") }
        fn restore_service(&mut self) { log(&self.events, "restore_service") }
    }

    fn enter(failing_step: Option<&'static str>) -> (Result<&'static str, &'static str>, Vec<&'static str>) {
        let events: Events = Rc::default();
        let mut fake = Fake { events: events.clone(), failing_step };
        let result = enter_remote(&mut fake, "proxy-handle");
        let recorded = events.borrow().clone();
        (result, recorded)
    }

    #[test]
    fn remote_success_saves_grants_unregisters_then_shows() {
        let (result, events) = enter(None);
        assert_eq!(result, Ok("proxy-handle"));
        assert_eq!(events, ["save", "grant", "unregister", "show"]);
    }

    #[test]
    fn save_failure_rolls_back_before_any_origin_is_granted_and_leaves_the_service_alone() {
        let (result, events) = enter(Some("save"));
        assert_eq!(result, Err("save"));
        assert_eq!(events, ["save", "stop_proxy", "forget"]);
    }

    #[test]
    fn grant_failure_leaves_the_local_service_alone() {
        let (result, events) = enter(Some("grant"));
        assert_eq!(result, Err("grant"));
        assert_eq!(events, ["save", "grant", "stop_proxy", "forget"]);
    }

    // A skipped step strands a running proxy, a saved remote nobody can use, or a stopped local service, so each
    // one has to appear after every failure that comes once the service was touched.
    #[test]
    fn unregister_failure_stops_the_proxy_forgets_the_remote_and_restores_the_service() {
        let (result, events) = enter(Some("unregister"));
        assert_eq!(result, Err("unregister"));
        assert_eq!(events, ["save", "grant", "unregister", "stop_proxy", "forget", "restore_service"]);
    }

    #[test]
    fn a_failure_to_show_the_remote_runs_the_whole_rollback() {
        let (result, events) = enter(Some("show"));
        assert_eq!(result, Err("show"));
        assert_eq!(events, ["save", "grant", "unregister", "show", "stop_proxy", "forget", "restore_service"]);
    }

    fn leave(failing_step: Option<&'static str>) -> (Result<&'static str, DisconnectError<&'static str>>, Vec<&'static str>) {
        let events: Events = Rc::default();
        let step = |name: &'static str| {
            let events = events.clone();
            move || -> Result<(), &'static str> {
                log(&events, name);
                if failing_step == Some(name) { Err(name) } else { Ok(()) }
            }
        };
        let (forget, register, start) = (step("forget"), step("register"), step("start"));
        let result = tauri::async_runtime::block_on(disconnect(
            move || async move { forget() },
            { let events = events.clone(); move || log(&events, "stop_proxy") },
            move || async move { register() },
            move || async move { start().map(|()| "ready") },
        ));
        let recorded = events.borrow().clone();
        (result, recorded)
    }

    #[test]
    fn disconnect_forgets_then_reregisters_and_starts_before_welcome() {
        let (result, events) = leave(None);
        assert_eq!(result, Ok("ready"));
        assert_eq!(events, ["forget", "stop_proxy", "register", "start"]);
    }

    #[test]
    fn disconnect_that_cannot_forget_leaves_the_proxy_and_the_remote_alone() {
        let (result, events) = leave(Some("forget"));
        assert_eq!(result, Err(DisconnectError::NotForgotten("forget")));
        assert_eq!(events, ["forget"]);
    }

    #[test]
    fn disconnect_that_cannot_bring_the_local_service_back_reports_the_remote_as_forgotten() {
        for failing in ["register", "start"] {
            let (result, _) = leave(Some(failing));
            assert_eq!(result, Err(DisconnectError::Forgotten(failing)));
        }
    }

    #[test]
    fn pending_disconnect_worker_keeps_the_async_runtime_responsive() {
        let (entered, waiting) = std::sync::mpsc::sync_channel(1);
        let (release, continue_work) = std::sync::mpsc::sync_channel(0);
        let pending = tauri::async_runtime::spawn(async move {
            disconnect(
                move || async move {
                    tauri::async_runtime::spawn_blocking(move || {
                        entered.send(()).unwrap();
                        continue_work.recv().unwrap();
                        Ok::<_, &'static str>(())
                    })
                    .await
                    .unwrap()
                },
                || {},
                || async { Ok(()) },
                || async { Ok("ready") },
            )
            .await
        });
        waiting
            .recv_timeout(std::time::Duration::from_secs(1))
            .expect("the disconnect worker started");
        let responsive = tauri::async_runtime::block_on(async {
            tauri::async_runtime::spawn(async { 42_u8 }).await.unwrap()
        });
        assert_eq!(responsive, 42, "the pending disconnect held the async runtime");
        release.send(()).unwrap();
        assert_eq!(
            tauri::async_runtime::block_on(pending).unwrap(),
            Ok("ready")
        );
    }
}
