use std::time::{Duration, Instant};

/// The route a connection may use only after the caller has completed its
/// relevant TLS or identity proof.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Route {
    Local,
    Outside,
}

/// Bounded policy for a paired local route and its optional outside fallback.
/// Networking and proof deliberately remain with the proxy; this module owns
/// only the route transition policy so an invalid proof cannot become a retry.
pub(crate) struct RouteSelector {
    outside_available: bool,
    current: Route,
    last_local_probe: Option<Instant>,
}

impl RouteSelector {
    pub(crate) const LOCAL_BUDGET: Duration = Duration::from_secs(2);
    pub(crate) const LOCAL_REPROBE: Duration = Duration::from_secs(30);

    pub(crate) fn new(outside_available: bool) -> Self {
        Self { outside_available, current: Route::Local, last_local_probe: None }
    }

    pub(crate) fn current(&self) -> Route {
        self.current
    }

    /// A local connection failure uses outside only when it was configured.
    pub(crate) fn local_failed(&mut self) -> Route {
        if self.outside_available {
            self.current = Route::Outside;
        }
        self.current
    }

    /// Outside requests must never wait for this probe. The caller schedules a
    /// background probe only when this returns true.
    pub(crate) fn should_probe_local(&mut self, now: Instant) -> bool {
        if self.current != Route::Outside || self.last_local_probe.is_some_and(|last| now.duration_since(last) < Self::LOCAL_REPROBE) {
            return false;
        }
        self.last_local_probe = Some(now);
        true
    }

    /// Only a successful identity-proven local connection can switch back.
    pub(crate) fn local_proven(&mut self) {
        self.current = Route::Local;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_failure_falls_back_only_when_outside_is_configured() {
        let mut paired = RouteSelector::new(true);
        assert_eq!(paired.current(), Route::Local);
        assert_eq!(paired.local_failed(), Route::Outside);

        let mut legacy = RouteSelector::new(false);
        assert_eq!(legacy.local_failed(), Route::Local);
    }

    #[test]
    fn outside_reprobe_is_bounded_and_switches_back_only_after_proof() {
        let mut routes = RouteSelector::new(true);
        routes.local_failed();
        let start = Instant::now();
        assert!(routes.should_probe_local(start));
        assert!(!routes.should_probe_local(start + Duration::from_secs(29)));
        // A request can continue on outside while this separate probe is pending.
        assert_eq!(routes.current(), Route::Outside);
        routes.local_proven();
        assert_eq!(routes.current(), Route::Local);
    }
}
