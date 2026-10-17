use std::future::Future;

/// Enters remote mode: the one definition of the order, used by pairing and by
/// every launch that connects a saved remote.
///
/// The order is proxy, save, grant, unregister, and each step is chosen so a
/// failure never strands state:
///
/// 1. The proxy starts first because the saved record names its port; it only
///    listens, and nothing can reach it until a capability names its origin.
/// 2. The remote is saved before its origin is granted any command, so no
///    capability (which cannot be revoked) ever exists for a remote the next
///    launch would not know about.
/// 3. The local service is unregistered last, only once the remote is saved
///    and its proxy is up, so a person is never left with neither a local
///    service nor a reachable remote. A failure at this step, or at the grant,
///    stops the proxy and forgets the remote that this call saved, and the
///    local service is untouched.
///
/// Each closure is a real shell operation in production and a fake in unit
/// tests. A launch that reconnects an already saved remote passes no-ops for
/// `unregister_service` (done when the remote was first entered) and
/// `forget_remote` (a saved pairing outlives a transient failure).
pub async fn enter_remote<E, P, Fut>(
    start_proxy: impl FnOnce() -> Fut,
    save_remote: impl FnOnce(&P) -> Result<(), E>,
    grant_origin: impl FnOnce(&P) -> Result<(), E>,
    unregister_service: impl FnOnce() -> Result<(), E>,
    stop_proxy: impl FnOnce(P),
    forget_remote: impl FnOnce(),
) -> Result<P, E>
where
    Fut: Future<Output = Result<P, E>>,
{
    let proxy = start_proxy().await?;
    if let Err(error) = save_remote(&proxy) {
        stop_proxy(proxy);
        return Err(error);
    }
    if let Err(error) = grant_origin(&proxy).and_then(|()| unregister_service()) {
        stop_proxy(proxy);
        forget_remote();
        return Err(error);
    }
    Ok(proxy)
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
/// stop the proxy and bring the local service back. After the forget the
/// person is local whatever happens next, and a later launch starts the local
/// service on its own.
pub fn disconnect<E, S>(
    forget_remote: impl FnOnce() -> Result<(), E>,
    stop_proxy: impl FnOnce(),
    register_service: impl FnOnce() -> Result<(), E>,
    start_service: impl FnOnce() -> Result<S, E>,
) -> Result<S, DisconnectError<E>> {
    forget_remote().map_err(DisconnectError::NotForgotten)?;
    stop_proxy();
    register_service().map_err(DisconnectError::Forgotten)?;
    start_service().map_err(DisconnectError::Forgotten)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};

    type Events = Rc<RefCell<Vec<&'static str>>>;

    fn log(events: &Events, name: &'static str) {
        events.borrow_mut().push(name);
    }

    /// Runs `enter_remote` with fakes that record their order and fail at `failing_step`.
    fn enter(failing_step: Option<&'static str>) -> (Result<&'static str, &'static str>, Vec<&'static str>) {
        let events: Events = Rc::default();
        let step = |name: &'static str| {
            let events = events.clone();
            move || -> Result<(), &'static str> {
                log(&events, name);
                if failing_step == Some(name) { Err(name) } else { Ok(()) }
            }
        };
        let start = step("proxy");
        let save = step("save");
        let grant = step("grant");
        let unregister = step("unregister");
        let result = block_on(enter_remote(
            move || async move { start().map(|()| "proxy-handle") },
            |_: &&str| save(),
            |_: &&str| grant(),
            unregister,
            { let events = events.clone(); move |_| log(&events, "stop_proxy") },
            { let events = events.clone(); move || log(&events, "forget") },
        ));
        let recorded = events.borrow().clone();
        (result, recorded)
    }

    fn block_on<T>(future: impl Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread().build().unwrap().block_on(future)
    }

    #[test]
    fn remote_success_starts_proxy_saves_grants_then_unregisters() {
        let (result, events) = enter(None);
        assert_eq!(result, Ok("proxy-handle"));
        assert_eq!(events, ["proxy", "save", "grant", "unregister"]);
    }

    #[test]
    fn proxy_failure_changes_nothing() {
        let (result, events) = enter(Some("proxy"));
        assert_eq!(result, Err("proxy"));
        assert_eq!(events, ["proxy"]);
    }

    #[test]
    fn save_failure_stops_the_proxy_before_any_origin_is_granted() {
        let (result, events) = enter(Some("save"));
        assert_eq!(result, Err("save"));
        assert_eq!(events, ["proxy", "save", "stop_proxy"]);
    }

    #[test]
    fn grant_failure_stops_the_proxy_and_forgets_without_touching_the_local_service() {
        let (result, events) = enter(Some("grant"));
        assert_eq!(result, Err("grant"));
        assert_eq!(events, ["proxy", "save", "grant", "stop_proxy", "forget"]);
    }

    #[test]
    fn unregister_failure_stops_proxy_and_forgets_remote_for_pairing_recovery() {
        let (result, events) = enter(Some("unregister"));
        assert_eq!(result, Err("unregister"));
        assert_eq!(events, ["proxy", "save", "grant", "unregister", "stop_proxy", "forget"]);
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
        let result = disconnect(
            forget,
            { let events = events.clone(); move || log(&events, "stop_proxy") },
            register,
            move || start().map(|()| "ready"),
        );
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
}
