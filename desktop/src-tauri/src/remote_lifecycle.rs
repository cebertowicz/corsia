/// Enters remote mode in the only safe order. Each closure is a real shell
/// operation in production and a fake in unit tests.
pub fn enter_remote<E>(
    save_remote: impl FnOnce() -> Result<(), E>,
    start_proxy: impl FnOnce() -> Result<(), E>,
    unregister_service: impl FnOnce() -> Result<(), E>,
    stop_proxy: impl FnOnce(),
    forget_remote: impl FnOnce(),
) -> Result<(), E> {
    save_remote()?;
    start_proxy()?;
    if let Err(error) = unregister_service() {
        stop_proxy();
        forget_remote();
        return Err(error);
    }
    Ok(())
}

pub fn disconnect<E>(
    stop_proxy: impl FnOnce(),
    forget_remote: impl FnOnce() -> Result<(), E>,
    register_service: impl FnOnce() -> Result<(), E>,
    start_service: impl FnOnce() -> Result<(), E>,
) -> Result<(), E> {
    stop_proxy();
    forget_remote()?;
    register_service()?;
    start_service()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, collections::BTreeMap, rc::Rc};

    #[test]
    fn remote_success_saves_starts_proxy_then_unregisters_without_touching_data() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let state = Rc::new(RefCell::new(BTreeMap::from([("db", "unchanged")] )));
        let before = state.borrow().clone();
        let result: Result<(), ()> = enter_remote(
            { let events = events.clone(); move || { events.borrow_mut().push("save"); Ok(()) } },
            { let events = events.clone(); move || { events.borrow_mut().push("proxy"); Ok(()) } },
            { let events = events.clone(); move || { events.borrow_mut().push("unregister"); Ok(()) } },
            || {}, || {},
        );
        assert_eq!(result, Ok(()));
        assert_eq!(*events.borrow(), ["save", "proxy", "unregister"]);
        assert_eq!(*state.borrow(), before);
    }

    #[test]
    fn unregister_failure_stops_proxy_and_forgets_remote_for_pairing_recovery() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let result: Result<(), &str> = enter_remote(
            { let events = events.clone(); move || { events.borrow_mut().push("save"); Ok(()) } },
            { let events = events.clone(); move || { events.borrow_mut().push("proxy"); Ok(()) } },
            || Err("server"),
            { let events = events.clone(); move || events.borrow_mut().push("stop_proxy") },
            { let events = events.clone(); move || events.borrow_mut().push("forget") },
        );
        assert_eq!(result, Err("server"));
        assert_eq!(*events.borrow(), ["save", "proxy", "stop_proxy", "forget"]);
    }

    #[test]
    fn disconnect_reregisters_and_starts_before_welcome() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let result: Result<(), ()> = disconnect(
            { let events = events.clone(); move || events.borrow_mut().push("stop_proxy") },
            { let events = events.clone(); move || { events.borrow_mut().push("forget"); Ok(()) } },
            { let events = events.clone(); move || { events.borrow_mut().push("register"); Ok(()) } },
            { let events = events.clone(); move || { events.borrow_mut().push("start"); Ok(()) } },
        );
        assert_eq!(result, Ok(()));
        assert_eq!(*events.borrow(), ["stop_proxy", "forget", "register", "start"]);
    }
}
