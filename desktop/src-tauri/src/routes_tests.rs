use std::{
    convert::Infallible,
    sync::atomic::{AtomicU16, AtomicUsize, Ordering::SeqCst},
};

use hyper::{body::Incoming, service::service_fn, Method, Response, StatusCode};
use ring::signature::{EcdsaKeyPair, ECDSA_P256_SHA256_ASN1_SIGNING};
use rustls::pki_types::CertificateDer;
use tokio::{net::TcpListener, task::JoinHandle};

use super::*;
use crate::{
    proxy::{self, ProxyHandle},
    remote_config::{Locator, RemoteTrust},
    test_tls::{self, HostKey},
};

#[test]
fn local_failure_moves_to_an_unproven_outside() {
    let mut routes = RouteSelector::new();
    assert_eq!(routes.current(), Route::Local);
    routes.local_failed(Instant::now());
    assert_eq!(routes.current(), Route::Outside);
    assert!(!routes.is_proven(Route::Outside, &[1; 32]), "outside proves itself afresh after a switch");
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
    routes.local_proven([1; 32]);
    assert_eq!(routes.current(), Route::Local);
    assert!(routes.is_proven(Route::Local, &[1; 32]));
    assert!(!routes.is_proven(Route::Local, &[2; 32]), "a changed TLS certificate proves itself again");
    assert!(!routes.should_probe_local(start + Duration::from_secs(90), RouteSelector::LOCAL_REPROBE), "no probe while on local");
}

/// How a test host answers an identity proof request.
#[derive(Clone)]
struct Prover {
    pkcs8: Vec<u8>,
    spki: Vec<u8>,
    /// Signs this authority instead of the one asked: a proof captured on one route and replayed on another.
    replayed_for: Option<String>,
}

impl Prover {
    fn honest(key: &HostKey) -> Self {
        Self { pkcs8: key.pkcs8.clone(), spki: key.spki.clone(), replayed_for: None }
    }

    fn answer(&self, body: &[u8]) -> String {
        let asked: serde_json::Value = serde_json::from_slice(body).unwrap();
        let nonce = BASE64.decode(asked["nonce"].as_str().unwrap()).unwrap();
        let authority = self.replayed_for.clone().unwrap_or_else(|| asked["authority"].as_str().unwrap().to_owned());
        let rng = SystemRandom::new();
        let key = EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_ASN1_SIGNING, &self.pkcs8, &rng).unwrap();
        let signature = key.sign(&rng, &[IDENTITY_CONTEXT, &nonce, b"\0", authority.as_bytes()].concat()).unwrap();
        serde_json::json!({ "spki": BASE64.encode(&self.spki), "signature": BASE64.encode(signature.as_ref()) }).to_string()
    }
}

/// A TLS host on loopback that names itself in every forwarded answer and counts what reached it.
struct TestHost {
    port: u16,
    forwarded: Arc<AtomicUsize>,
    proofs: Arc<AtomicUsize>,
    task: JoinHandle<()>,
}

impl Drop for TestHost {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn serve(config: Arc<rustls::ServerConfig>, name: &'static str, prover: Option<Prover>) -> TestHost {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (forwarded, proofs) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
    let (seen_forwarded, seen_proofs) = (Arc::clone(&forwarded), Arc::clone(&proofs));
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let (config, prover, forwarded, proofs) = (Arc::clone(&config), prover.clone(), Arc::clone(&seen_forwarded), Arc::clone(&seen_proofs));
            tokio::spawn(async move {
                let Ok(tls) = tokio_rustls::TlsAcceptor::from(config).accept(stream).await else { return };
                let service = service_fn(move |request: Request<Incoming>| {
                    let (prover, forwarded, proofs) = (prover.clone(), Arc::clone(&forwarded), Arc::clone(&proofs));
                    async move {
                        let path = request.uri().path().to_owned();
                        let is_proof = request.method() == Method::POST && path == IDENTITY_PATH;
                        let body = request.into_body().collect().await.unwrap().to_bytes();
                        let (status, text) = match (&prover, is_proof) {
                            (Some(prover), true) => {
                                proofs.fetch_add(1, SeqCst);
                                (StatusCode::OK, prover.answer(&body))
                            }
                            (None, true) => (StatusCode::NOT_FOUND, String::new()),
                            (_, false) => {
                                forwarded.fetch_add(1, SeqCst);
                                let text = match path.as_str() {
                                    "/api/devices/outside-address" => serde_json::json!({"outside_address": format!("127.0.0.1:{port}")}).to_string(),
                                    "/api/version" => serde_json::json!({"app": {"version": "0.4.1", "shell_protocol": crate::version_check::SHELL_PROTOCOL}}).to_string(),
                                    _ => name.to_owned(),
                                };
                                (StatusCode::OK, text)
                            }
                        };
                        Ok::<_, Infallible>(Response::builder().status(status).body(Full::new(Bytes::from(text))).unwrap())
                    }
                });
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(tls), service).await;
            });
        }
    });
    TestHost { port, forwarded, proofs, task }
}

/// A port nothing listens on.
async fn dead_port() -> u16 {
    TcpListener::bind(("127.0.0.1", 0)).await.unwrap().local_addr().unwrap().port()
}

/// The app's pinned TLS, and system trust through the test CA alone.
fn client_tls(ca: Option<CertificateDer<'static>>) -> TlsConfig {
    Arc::new(move |trust: &Trust| match trust {
        Trust::System => {
            let mut roots = rustls::RootCertStore::empty();
            if let Some(ca) = &ca {
                roots.add(ca.clone()).unwrap();
            }
            Ok(rustls::ClientConfig::builder().with_root_certificates(roots).with_no_client_auth())
        }
        pinned => trust::client_config(pinned),
    })
}

/// A paired record's routes: local pinned to the host, and an outside route at `outside` when given.
fn paired(host: &HostKey, outside: Option<(u16, RemoteTrust)>, ca: Option<CertificateDer<'static>>, refusals: &Arc<AtomicUsize>) -> RouteConfig {
    let refusals = Arc::clone(refusals);
    RouteConfig {
        trust: Trust::Pinned { spki_sha256: host.tls.pin },
        identity: Some(host.tls.pin),
        outside: outside.map(|(port, trust)| OutsideRoute { url: format!("https://127.0.0.1:{port}"), trust }),
        on_key_changed: Arc::new(move || {
            refusals.fetch_add(1, SeqCst);
        }),
        tls: client_tls(ca),
        reprobe: RouteSelector::LOCAL_REPROBE,
    }
}

/// A proxy whose local route resolves to whatever `local` holds at each resolve.
async fn proxy_for(config: RouteConfig, local: Arc<AtomicU16>) -> ProxyHandle {
    proxy::start_routes(
        Locator::Address { host: "127.0.0.1".into(), port: 0 },
        config,
        0,
        Arc::new(move |_| {
            let port = local.load(SeqCst);
            Box::pin(async move { Ok(("127.0.0.1".into(), port)) })
        }),
    )
    .await
    .unwrap()
}

async fn get(proxy: &ProxyHandle) -> (StatusCode, String) {
    let response = reqwest::get(format!("http://127.0.0.1:{}/api/health", proxy.port)).await.unwrap();
    (response.status(), response.text().await.unwrap())
}

fn system() -> RemoteTrust {
    RemoteTrust::System
}

#[tokio::test]
async fn a_pinned_local_route_is_proven_by_its_handshake_alone() {
    let host = test_tls::generated_p256_host();
    assert_eq!(Sha256::digest(&host.spki).as_slice(), host.tls.pin, "the identity is the listener key's fingerprint");
    let local = serve(Arc::clone(&host.tls.config), "local", None).await;
    let refusals = Arc::new(AtomicUsize::new(0));
    let config = paired(&host, Some((dead_port().await, system())), None, &refusals);
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(local.port))).await;

    assert_eq!(get(&proxy).await, (StatusCode::OK, "local".into()));
    assert_eq!(local.proofs.load(SeqCst), 0, "a pinned route asks for no signature");
    assert_eq!(refusals.load(SeqCst), 0);
}

#[path = "routes_proxy_tests.rs"]
mod terminating_proxy;

/// Every refusal ends forwarding through both routes, tells the owner once, and never reaches the refused route.
async fn assert_refused(proxy: &ProxyHandle, refused: &TestHost, refusals: &AtomicUsize) {
    assert_eq!(get(proxy).await.0, StatusCode::BAD_GATEWAY);
    assert!(proxy.key_changed());
    assert_eq!(get(proxy).await, (StatusCode::BAD_GATEWAY, KEY_CHANGED_MESSAGE.into()));
    assert_eq!(refusals.load(SeqCst), 1);
    assert_eq!(refused.forwarded.load(SeqCst), 0, "nothing is forwarded to a route that failed its proof");
}

#[tokio::test]
async fn a_pinned_relay_presenting_another_key_is_refused() {
    let (host, relay_key) = (test_tls::generated_p256_host(), test_tls::generated_self_signed_identity());
    let relay = serve(relay_key.config, "relay", None).await;
    let refusals = Arc::new(AtomicUsize::new(0));
    let pinned = RemoteTrust::Pinned { spki_sha256: BASE64.encode(host.tls.pin) };
    let config = paired(&host, Some((relay.port, pinned)), None, &refusals);
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(dead_port().await))).await;

    assert_refused(&proxy, &relay, &refusals).await;
}

#[tokio::test]
async fn a_forwarder_signing_with_another_key_is_refused() {
    let (host, impostor, forwarder) = (test_tls::generated_p256_host(), test_tls::generated_p256_host(), test_tls::generated_ca_forwarder());
    let outside = serve(forwarder.config, "outside", Some(Prover::honest(&impostor))).await;
    let refusals = Arc::new(AtomicUsize::new(0));
    let config = paired(&host, Some((outside.port, system())), Some(forwarder.ca), &refusals);
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(dead_port().await))).await;

    assert_refused(&proxy, &outside, &refusals).await;
    assert_eq!(outside.proofs.load(SeqCst), 1);
}

#[tokio::test]
async fn a_proof_signed_for_another_address_is_refused() {
    let (host, forwarder) = (test_tls::generated_p256_host(), test_tls::generated_ca_forwarder());
    let replayed = Prover { replayed_for: Some("studio.example:443".into()), ..Prover::honest(&host) };
    let outside = serve(forwarder.config, "outside", Some(replayed)).await;
    let refusals = Arc::new(AtomicUsize::new(0));
    let config = paired(&host, Some((outside.port, system())), Some(forwarder.ca), &refusals);
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(dead_port().await))).await;

    assert_refused(&proxy, &outside, &refusals).await;
}

#[tokio::test]
async fn a_local_route_that_never_answers_falls_back_after_its_budget() {
    let host = test_tls::generated_p256_host();
    let outside = serve(Arc::clone(&host.tls.config), "outside", Some(Prover::honest(&host))).await;
    // Accepts TCP and never speaks TLS: a saved address that now leads nowhere useful.
    let silent = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let silent_port = silent.local_addr().unwrap().port();
    let held = tokio::spawn(async move {
        let mut streams = Vec::new();
        while let Ok((stream, _)) = silent.accept().await {
            streams.push(stream);
        }
    });
    let refusals = Arc::new(AtomicUsize::new(0));
    let config = paired(&host, Some((outside.port, system())), Some(host.tls.certificate.clone()), &refusals);
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(silent_port))).await;

    let started = Instant::now();
    assert_eq!(get(&proxy).await, (StatusCode::OK, "outside".into()));
    let waited = started.elapsed();
    assert!(waited >= RouteSelector::LOCAL_BUDGET && waited < Duration::from_secs(5), "{waited:?}");
    let started = Instant::now();
    assert_eq!(get(&proxy).await, (StatusCode::OK, "outside".into()));
    assert!(started.elapsed() < Duration::from_secs(1), "once on outside, a request does not wait for local");
    held.abort();
}

#[tokio::test]
async fn outside_hands_back_to_local_once_local_proves_itself() {
    let host = test_tls::generated_p256_host();
    let outside = serve(Arc::clone(&host.tls.config), "outside", Some(Prover::honest(&host))).await;
    let local = serve(Arc::clone(&host.tls.config), "local", None).await;
    let refusals = Arc::new(AtomicUsize::new(0));
    let config = RouteConfig { reprobe: Duration::ZERO, ..paired(&host, Some((outside.port, system())), Some(host.tls.certificate.clone()), &refusals) };
    let local_port = Arc::new(AtomicU16::new(dead_port().await));
    let proxy = proxy_for(config, Arc::clone(&local_port)).await;

    assert_eq!(get(&proxy).await, (StatusCode::OK, "outside".into()));
    local_port.store(local.port, SeqCst); // The person is home again.
    assert_eq!(get(&proxy).await, (StatusCode::OK, "outside".into()), "outside serves at once while local is probed");
    let mut answered = String::new();
    for _ in 0..50 {
        answered = get(&proxy).await.1;
        if answered == "local" {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(answered, "local", "a proven local route carries requests again");
    assert_eq!(refusals.load(SeqCst), 0);
}

#[tokio::test]
async fn a_local_success_refresh_makes_the_outside_route_available_in_the_current_session() {
    let host = test_tls::generated_p256_host();
    let local = serve(Arc::clone(&host.tls.config), "host", Some(Prover::honest(&host))).await;
    let mut saved = RemoteRecord { locator: Locator::Address { host: "127.0.0.1".into(), port: local.port },
        trust: RemoteTrust::Pinned { spki_sha256: BASE64.encode(host.tls.pin) }, name: "Studio".into(), proxy_port: 0,
        identity: Some(BASE64.encode(host.tls.pin)), outside: None };
    crate::refresh_outside_address(&mut saved, "127.0.0.1", local.port).await;
    assert_eq!(saved.outside.as_ref().unwrap().url, format!("https://127.0.0.1:{}", local.port));
    let mut config = RouteConfig::from_record(&saved, || {}).unwrap();
    config.tls = client_tls(Some(host.tls.certificate.clone()));
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(dead_port().await))).await;
    assert_eq!(get(&proxy).await, (StatusCode::OK, "host".into()));
}

#[tokio::test]
async fn an_established_outside_launch_does_not_wait_for_the_local_refresh() {
    let host = test_tls::generated_p256_host();
    let outside = serve(Arc::clone(&host.tls.config), "outside", Some(Prover::honest(&host))).await;
    let silent = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
    let port = silent.local_addr().unwrap().port();
    let held = tokio::spawn(async move {
        let mut streams = Vec::new();
        while let Ok((stream, _)) = silent.accept().await { streams.push(stream); }
    });
    let saved = RemoteRecord { locator: Locator::Address { host: "127.0.0.1".into(), port },
        trust: RemoteTrust::Pinned { spki_sha256: BASE64.encode(host.tls.pin) }, name: "Studio".into(), proxy_port: 0,
        identity: Some(BASE64.encode(host.tls.pin)),
        outside: Some(OutsideRoute { url: format!("https://127.0.0.1:{}", outside.port), trust: system() }) };
    let directory = tempfile::tempdir().unwrap();
    crate::remote_config::save_remote(directory.path(), saved.clone()).unwrap();
    let refresh = tokio::spawn(crate::refresh_outside_after_launch(directory.path().to_owned(), saved.clone()));
    let mut config = RouteConfig::from_record(&saved, || {}).unwrap();
    config.tls = client_tls(Some(host.tls.certificate.clone()));
    let proxy = proxy_for(config, Arc::new(AtomicU16::new(port))).await;
    let version = tokio::time::timeout(Duration::from_secs(5), crate::version_check::check_through_proxy(proxy.port, "0.4.1")).await.unwrap().unwrap();
    assert_eq!(version, crate::version_check::Compat::Ok);
    assert!(!refresh.is_finished(), "launch waited for the incidental local refresh");
    refresh.abort();
    held.abort();
}

#[tokio::test]
async fn a_trusted_legacy_local_connection_learns_the_identity_and_enables_the_saved_outside_route() {
    let host = test_tls::generated_p256_host();
    let local = serve(Arc::clone(&host.tls.config), "local", Some(Prover::honest(&host))).await;
    let port = local.port;
    let dial: LocalDial = Arc::new(move || Box::pin(async move { super::dial("127.0.0.1", port).await }));
    let config = RouteConfig { tls: client_tls(Some(host.tls.certificate.clone())), ..RouteConfig::single(Trust::System, || {}) };
    let routes = Routes::new(config, dial).unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let legacy = RemoteRecord { locator: Locator::Address { host: "127.0.0.1".into(), port }, trust: RemoteTrust::System,
        name: "Studio".into(), proxy_port: 0, identity: None, outside: None };
    crate::remote_config::save_remote(temporary.path(), legacy.clone()).unwrap();
    let mut saved = crate::remote_config::load_remote(temporary.path()).unwrap().unwrap();
    saved.identity = Some(BASE64.encode(routes.learn_local_identity().await.unwrap()));
    assert_eq!(saved.identity, Some(BASE64.encode(host.tls.pin)));
    saved.outside = crate::outside_route(Some("home.example.com:8443"), saved.identity.is_some());
    crate::remote_config::save_refreshed_remote(temporary.path(), &legacy, saved.clone()).unwrap();
    let loaded = crate::remote_config::load_remote(temporary.path()).unwrap().unwrap();
    assert_eq!(loaded, saved);
    assert!(RouteConfig::from_record(&loaded, || {}).unwrap().outside.is_some());
    assert_eq!(local.proofs.load(SeqCst), 1);
}

#[tokio::test]
async fn a_record_from_an_earlier_build_connects_on_its_single_route() {
    let host = test_tls::generated_self_signed_identity();
    let local = serve(Arc::clone(&host.config), "local", None).await;
    let saved = format!(
        r#"{{"locator":{{"kind":"address","host":"127.0.0.1","port":{}}},"trust":{{"kind":"pinned","spki_sha256":"{}"}},"name":"Studio","proxy_port":0}}"#,
        local.port,
        BASE64.encode(host.pin),
    );
    let record: RemoteRecord = serde_json::from_str(&saved).unwrap();
    assert_eq!((&record.identity, &record.outside), (&None, &None));
    let proxy = proxy::start_record(&record, 0, || panic!("a matching key never reports a change")).await.unwrap();

    assert_eq!(get(&proxy).await, (StatusCode::OK, "local".into()));
    assert_eq!(local.proofs.load(SeqCst), 0, "a single route asks for no identity proof");
}

#[tokio::test]
async fn an_outside_route_without_a_paired_identity_is_refused_at_start() {
    let host = test_tls::generated_p256_host();
    let config = RouteConfig { identity: None, ..paired(&host, Some((443, system())), None, &Arc::new(AtomicUsize::new(0))) };
    let started = proxy::start_routes(
        Locator::Address { host: "127.0.0.1".into(), port: 1 },
        config,
        0,
        Arc::new(|_| Box::pin(async { Ok(("127.0.0.1".into(), 1)) })),
    )
    .await;
    assert!(started.is_err());
}
