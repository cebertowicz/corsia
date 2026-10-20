use super::*;

#[test]
fn local_failure_moves_to_an_unproven_outside() {
    let mut routes = RouteSelector::new();
    assert_eq!(routes.current(), Route::Local);
    routes.local_failed(Instant::now());
    assert_eq!(routes.current(), Route::Outside);
    assert!(!routes.is_proven(Route::Outside), "outside proves itself afresh after a switch");
}

#[test]
fn outside_reprobe_is_bounded_and_switches_back_only_after_proof() {
    let mut routes = RouteSelector::new();
    let start = Instant::now();
    routes.local_failed(start);
    assert!(!routes.should_probe_local(start + Duration::from_secs(29), RouteSelector::LOCAL_REPROBE));
    assert!(routes.should_probe_local(start + Duration::from_secs(30), RouteSelector::LOCAL_REPROBE));
    assert!(!routes.should_probe_local(start + Duration::from_secs(31), RouteSelector::LOCAL_REPROBE));
    // A request can continue on outside while this separate probe is pending.
    assert_eq!(routes.current(), Route::Outside);
    routes.local_proven();
    assert_eq!(routes.current(), Route::Local);
    assert!(routes.is_proven(Route::Local));
    assert!(!routes.should_probe_local(start + Duration::from_secs(90), RouteSelector::LOCAL_REPROBE), "no probe while on local");
}
