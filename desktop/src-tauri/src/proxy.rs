use std::{
    convert::Infallible,
    error::Error,
    future::Future,
    net::SocketAddr,
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};

use futures_util::future::{FutureExt, Shared};
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::{
    body::{Bytes, Incoming},
    header::{HeaderValue, HOST},
    service::service_fn,
    Request, Response, StatusCode, Uri,
};
use hyper_util::rt::TokioIo;
use tokio::{net::TcpListener, sync::oneshot};

use crate::{
    remote_config::{Locator, RemoteRecord},
    routes::{self, Dialed, LocalDial, RouteConfig, Routes, Upstream},
};

pub(crate) type ProxyBody = BoxBody<Bytes, Box<dyn Error + Send + Sync>>;
type ResolveFuture = Pin<Box<dyn Future<Output = Result<(String, u16), ProxyError>> + Send>>;
type Resolver = Arc<dyn Fn(Locator) -> ResolveFuture + Send + Sync>;
type SharedResolve = Shared<Pin<Box<dyn Future<Output = Result<Target, ProxyError>> + Send>>>;

#[derive(Debug, Clone)]
pub enum ProxyError {
    Bind(String),
    Resolve(String),
    Unreachable(crate::classify::ConnectFailure),
    Transport(String),
}

pub struct ProxyHandle {
    pub port: u16,
    shutdown: Option<oneshot::Sender<()>>,
    routes: Arc<Routes>,
}

impl ProxyHandle {
    /// Whether a route refused the paired identity, which ended forwarding.
    pub fn key_changed(&self) -> bool {
        self.routes.key_changed()
    }

    pub fn stop(mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
    }
}

impl Drop for ProxyHandle {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
    }
}

/// The proxy for a saved record: its local route, and its outside route when one is saved.
pub async fn start_record(
    record: &RemoteRecord,
    preferred_port: u16,
    on_key_changed: impl Fn() + Send + Sync + 'static,
) -> Result<ProxyHandle, ProxyError> {
    let config = RouteConfig::from_record(record, on_key_changed).map_err(ProxyError::Transport)?;
    start_routes(record.locator.clone(), config, preferred_port, Arc::new(|locator| Box::pin(default_resolve(locator)))).await
}

pub(crate) async fn start_routes(
    locator: Locator,
    config: RouteConfig,
    preferred_port: u16,
    resolver: Resolver,
) -> Result<ProxyHandle, ProxyError> {
    let local = Arc::new(LocalTarget { locator, resolver, resolved: Mutex::new(ResolveCache::default()) });
    let dial: LocalDial = Arc::new(move || {
        let local = Arc::clone(&local);
        Box::pin(async move { connect(&local).await })
    });
    let routes = Arc::new(Routes::new(config, dial).map_err(ProxyError::Transport)?);
    let listener = bind_loopback(preferred_port).await?;
    let port = listener.local_addr().map_err(|error| ProxyError::Bind(error.to_string()))?.port();
    let (shutdown_send, mut shutdown_receive) = oneshot::channel();
    let state = Arc::new(ProxyState { local_port: port, routes: Arc::clone(&routes) });
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut shutdown_receive => break,
                accepted = listener.accept() => match accepted {
                    Ok((stream, _)) => {
                        let state = Arc::clone(&state);
                        tokio::spawn(async move {
                            let service = service_fn(move |request| forward(request, Arc::clone(&state)));
                            let _ = hyper::server::conn::http1::Builder::new()
                                .serve_connection(TokioIo::new(stream), service)
                                .with_upgrades()
                                .await;
                        });
                    }
                    Err(_) => break,
                }
            }
        }
    });
    Ok(ProxyHandle { port, shutdown: Some(shutdown_send), routes })
}

struct ProxyState {
    local_port: u16,
    routes: Arc<Routes>,
}

/// The saved locator and what it last resolved to: the local route.
struct LocalTarget {
    locator: Locator,
    resolver: Resolver,
    resolved: Mutex<ResolveCache>,
}

/// Where the saved locator currently points.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Target {
    host: String,
    port: u16,
}

/// The resolved target, kept until a connection to it fails, and the resolve
/// in flight, which every request that needs a target shares.
#[derive(Default)]
struct ResolveCache {
    current: Option<Target>,
    flight: Option<(u64, SharedResolve)>,
    next_flight: u64,
}

/// The cached target. With `stale` set, the caller found that target
/// unreachable: it is dropped and resolved again, unless another request has
/// already replaced it. Concurrent callers share one resolve.
async fn resolve_target(state: &LocalTarget, stale: Option<&Target>) -> Result<Target, ProxyError> {
    let (id, flight) = {
        let mut cache = state.resolved.lock().expect("resolve cache mutex is not poisoned");
        if let Some(current) = &cache.current {
            if Some(current) != stale {
                return Ok(current.clone());
            }
        }
        cache.current = None;
        match &cache.flight {
            Some((id, flight)) => (*id, flight.clone()),
            None => {
                let id = cache.next_flight;
                cache.next_flight += 1;
                let resolving = (state.resolver)(state.locator.clone());
                let flight: SharedResolve = (Box::pin(async move {
                    resolving.await.map(|(host, port)| Target { host, port })
                }) as Pin<Box<dyn Future<Output = Result<Target, ProxyError>> + Send>>)
                    .shared();
                cache.flight = Some((id, flight.clone()));
                (id, flight)
            }
        }
    };
    let outcome = flight.await;
    let mut cache = state.resolved.lock().expect("resolve cache mutex is not poisoned");
    if matches!(&cache.flight, Some((current, _)) if *current == id) {
        cache.flight = None;
        if let Ok(target) = &outcome {
            cache.current = Some(target.clone());
        }
    }
    outcome
}

fn describe(error: ProxyError) -> String {
    match error {
        ProxyError::Resolve(message) => message,
        ProxyError::Unreachable(crate::classify::ConnectFailure::Network) => "remote host is unreachable on the network".into(),
        ProxyError::Unreachable(crate::classify::ConnectFailure::Server) => "remote CORSIA server is unavailable".into(),
        other => format!("{other:?}"),
    }
}

/// Open a connection to the remote host. A failed connection drops the cached
/// address and resolves once more: a DHCP change moves a saved DNS-SD host.
async fn connect(state: &LocalTarget) -> Result<Dialed, String> {
    let target = resolve_target(state, None).await.map_err(describe)?;
    match routes::dial(&target.host, target.port).await {
        Ok(dialed) => Ok(dialed),
        Err(first) => {
            let fresh = resolve_target(state, Some(&target)).await.map_err(describe)?;
            if fresh == target {
                return Err(first);
            }
            routes::dial(&fresh.host, fresh.port).await
        }
    }
}

async fn bind_loopback(preferred_port: u16) -> Result<TcpListener, ProxyError> {
    let preferred = SocketAddr::from(([127, 0, 0, 1], preferred_port));
    match TcpListener::bind(preferred).await {
        Ok(listener) => Ok(listener),
        Err(_) => TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
            .await
            .map_err(|error| ProxyError::Bind(error.to_string())),
    }
}

async fn forward(request: Request<Incoming>, state: Arc<ProxyState>) -> Result<Response<ProxyBody>, Infallible> {
    if !is_own_origin(&request, state.local_port) {
        return Ok(failure(StatusCode::MISDIRECTED_REQUEST, "proxy refuses requests for another Host"));
    }
    match forward_trusted(request, state).await {
        Ok(response) => Ok(response),
        Err(error) => Ok(failure(StatusCode::BAD_GATEWAY, &error)),
    }
}

fn is_own_origin<B>(request: &Request<B>, port: u16) -> bool {
    let expected = format!("127.0.0.1:{port}");
    request.headers().get(HOST).and_then(|host| host.to_str().ok()) == Some(expected.as_str())
}

fn is_upgrade_request<B>(request: &Request<B>) -> bool {
    request.headers().contains_key("upgrade")
        && request
            .headers()
            .get("connection")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value.split(',').any(|token| token.trim().eq_ignore_ascii_case("upgrade")))
}

/// Forward one request over a connection `Routes` has proven reaches the paired host.
async fn forward_trusted(
    mut request: Request<Incoming>,
    state: Arc<ProxyState>,
) -> Result<Response<ProxyBody>, String> {
    let Upstream { mut sender, authority, .. } = state.routes.connect().await.map_err(routes::RouteError::into_message)?;
    let client_upgrade = is_upgrade_request(&request).then(|| hyper::upgrade::on(&mut request));
    rewrite_request(&mut request, &authority)?;
    let request = request.map(|body| body.map_err(|error| Box::new(error) as Box<dyn Error + Send + Sync>).boxed());
    let mut response = sender.send_request(request).await.map_err(|error| error.to_string())?;
    if response.status() == StatusCode::SWITCHING_PROTOCOLS {
        if let Some(client_upgrade) = client_upgrade {
            let upstream_upgrade = hyper::upgrade::on(&mut response);
            tokio::spawn(async move {
                if let (Ok(client), Ok(upstream)) = (client_upgrade.await, upstream_upgrade.await) {
                    let (mut client, mut upstream) = (TokioIo::new(client), TokioIo::new(upstream));
                    let _ = tokio::io::copy_bidirectional(&mut client, &mut upstream).await;
                }
            });
        }
    }
    Ok(response.map(|body| body.map_err(|error| Box::new(error) as Box<dyn Error + Send + Sync>).boxed()))
}

async fn default_resolve(locator: Locator) -> Result<(String, u16), ProxyError> {
    match locator {
        Locator::Address { host, port } => Ok((host, port)),
        Locator::DnsSd { instance } => crate::discovery::resolve(&instance, Duration::from_secs(5))
            .await
            .map_err(|error| match error {
                crate::discovery::DiscoveryError::Timeout => ProxyError::Unreachable(crate::classify::ConnectFailure::Network),
                other => ProxyError::Resolve(format!("DNS-SD resolution failed: {other}")),
            }),
    }
}

fn rewrite_request<B>(request: &mut Request<B>, authority: &str) -> Result<(), String> {
    request.headers_mut().remove("forwarded");
    request.headers_mut().remove("x-forwarded-for");
    request.headers_mut().remove("x-forwarded-host");
    request.headers_mut().remove("x-forwarded-proto");
    request.headers_mut().insert(
        HOST,
        HeaderValue::from_str(authority).map_err(|error| error.to_string())?,
    );
    let path_and_query = request.uri().path_and_query().map(|value| value.as_str()).unwrap_or("/");
    *request.uri_mut() = Uri::builder()
        .scheme("https")
        .authority(authority)
        .path_and_query(path_and_query)
        .build()
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn failure(status: StatusCode, message: &str) -> Response<ProxyBody> {
    Response::builder()
        .status(status)
        .body(Full::new(Bytes::copy_from_slice(message.as_bytes()))
            .map_err(|never| match never {})
            .boxed())
        .expect("static response is valid")
}

#[cfg(test)]
mod tests {
    use std::{
        convert::Infallible,
        future::Future,
        sync::{atomic::{AtomicBool, Ordering}, Arc, Mutex},
        time::Duration,
    };

    use super::*;
    use crate::{remote_host::RemoteHost, routes::KEY_CHANGED_MESSAGE, trust::Trust};
    use hyper::{body::Frame, header::{CONNECTION, UPGRADE}, upgrade};
    use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::TcpStream, task::JoinHandle};

    fn test_body(body: impl Into<Bytes>) -> ProxyBody {
        Full::new(body.into()).map_err(|never| match never {}).boxed()
    }

    async fn start_tls_server<F, Fut>(
        config: Arc<rustls::ServerConfig>,
        handler: F,
    ) -> (u16, JoinHandle<()>)
    where
        F: Fn(Request<Incoming>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<Response<ProxyBody>, Infallible>> + Send + 'static,
    {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let task = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let Ok(tls) = tokio_rustls::TlsAcceptor::from(config).accept(stream).await else {
                return;
            };
            let service = service_fn(handler);
            hyper::server::conn::http1::Builder::new()
                .serve_connection(TokioIo::new(tls), service)
                .with_upgrades()
                .await
                .unwrap();
        });
        (port, task)
    }

    async fn proxy_for(port: u16, pin: [u8; 32], on_key_changed: impl Fn() + Send + Sync + 'static) -> ProxyHandle {
        start_routes(
            Locator::Address { host: "127.0.0.1".into(), port },
            RouteConfig::single(Trust::Pinned { spki_sha256: pin }, on_key_changed),
            0,
            Arc::new(move |_| Box::pin(async move { Ok(("127.0.0.1".into(), port)) })),
        )
        .await
        .unwrap()
    }

    #[test]
    fn foreign_host_is_refused_before_any_remote_dial() {
        let request = Request::builder().header(HOST, "attacker.invalid").body(()).unwrap();
        assert!(!is_own_origin(&request, 47911));
    }

    #[test]
    fn own_loopback_origin_is_required_exactly() {
        let request = Request::builder().header(HOST, "127.0.0.1:47911").body(()).unwrap();
        assert!(is_own_origin(&request, 47911));
    }

    #[tokio::test]
    async fn real_tls_proxy_rewrites_host_and_removes_forwarded_headers() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let seen = Arc::new(Mutex::new(None));
        let observed = Arc::clone(&seen);
        let (port, server) = start_tls_server(identity.config, move |request| {
            let observed = Arc::clone(&observed);
            async move {
                *observed.lock().unwrap() = Some(request.headers().clone());
                Ok(Response::new(test_body("ok")))
            }
        })
        .await;
        let proxy = proxy_for(port, identity.pin, || {}).await;
        let response = reqwest::Client::new()
            .get(format!("http://127.0.0.1:{}/headers", proxy.port))
            .header("forwarded", "for=198.51.100.1")
            .header("x-forwarded-for", "198.51.100.1")
            .header("x-forwarded-host", "attacker.invalid")
            .header("x-forwarded-proto", "http")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let headers = seen.lock().unwrap().take().unwrap();
        assert_eq!(headers.get(HOST).unwrap(), format!("127.0.0.1:{port}").as_str());
        for name in ["forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"] {
            assert!(headers.get(name).is_none(), "{name} leaked upstream");
        }
        proxy.stop();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn real_tls_proxy_streams_first_sse_event_before_upstream_finishes() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (release_send, release_receive) = oneshot::channel();
        let release = Arc::new(tokio::sync::Mutex::new(Some(release_receive)));
        let stream_release = Arc::clone(&release);
        let (port, server) = start_tls_server(identity.config, move |_| {
            let stream_release = Arc::clone(&stream_release);
            async move {
                let release = stream_release.lock().await.take().unwrap();
                let stream = futures_util::stream::unfold((false, release), |(sent, release)| async move {
                    if sent {
                        let _ = release.await;
                        None
                    } else {
                        Some((
                            Ok::<_, Infallible>(Frame::data(Bytes::from_static(b"data: first\n\n"))),
                            (true, release),
                        ))
                    }
                });
                Ok(Response::builder()
                    .header("content-type", "text/event-stream")
                    .body(http_body_util::StreamBody::new(stream).map_err(|never| match never {}).boxed())
                    .unwrap())
            }
        })
        .await;
        let proxy = proxy_for(port, identity.pin, || {}).await;
        let mut response = reqwest::get(format!("http://127.0.0.1:{}/events", proxy.port)).await.unwrap();
        let first = tokio::time::timeout(Duration::from_secs(1), response.chunk()).await.unwrap().unwrap().unwrap();
        assert_eq!(first, Bytes::from_static(b"data: first\n\n"));
        release_send.send(()).unwrap();
        assert!(response.chunk().await.unwrap().is_none());
        proxy.stop();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn real_tls_proxy_tunnels_websocket_frames_and_close() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (port, server) = start_tls_server(identity.config, move |mut request| async move {
            assert_eq!(request.headers().get(UPGRADE).unwrap(), "websocket");
            let upgraded = upgrade::on(&mut request);
            tokio::spawn(async move {
                let mut socket = TokioIo::new(upgraded.await.unwrap());
                let mut header = [0; 2];
                socket.read_exact(&mut header).await.unwrap();
                assert_eq!(header[0], 0x81);
                let size = usize::from(header[1] & 0x7f);
                let mut mask = [0; 4];
                socket.read_exact(&mut mask).await.unwrap();
                let mut payload = vec![0; size];
                socket.read_exact(&mut payload).await.unwrap();
                for (index, byte) in payload.iter_mut().enumerate() {
                    *byte ^= mask[index % mask.len()];
                }
                assert_eq!(payload, b"ping");
                socket.write_all(&[0x81, 4, b'p', b'o', b'n', b'g']).await.unwrap();
                socket.read_exact(&mut header).await.unwrap();
                assert_eq!(header[0], 0x88);
                socket.write_all(&[0x88, 0]).await.unwrap();
            });
            Ok(Response::builder()
                .status(StatusCode::SWITCHING_PROTOCOLS)
                .header(CONNECTION, "upgrade")
                .header(UPGRADE, "websocket")
                .header("sec-websocket-accept", "test")
                .body(test_body(Bytes::new()))
                .unwrap())
        })
        .await;
        let proxy = proxy_for(port, identity.pin, || {}).await;
        let mut client = TcpStream::connect(("127.0.0.1", proxy.port)).await.unwrap();
        client.write_all(format!(
            "GET /chat HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Key: test\r\nSec-WebSocket-Version: 13\r\n\r\n",
            proxy.port,
        ).as_bytes()).await.unwrap();
        let mut response = Vec::new();
        let mut byte = [0; 1];
        while !response.ends_with(b"\r\n\r\n") {
            client.read_exact(&mut byte).await.unwrap();
            response.push(byte[0]);
        }
        assert!(response.starts_with(b"HTTP/1.1 101"));
        client.write_all(&[0x81, 0x84, 1, 2, 3, 4, b'p' ^ 1, b'i' ^ 2, b'n' ^ 3, b'g' ^ 4]).await.unwrap();
        let mut frame = [0; 6];
        client.read_exact(&mut frame).await.unwrap();
        assert_eq!(&frame, &[0x81, 4, b'p', b'o', b'n', b'g']);
        client.write_all(&[0x88, 0x80, 5, 6, 7, 8]).await.unwrap();
        let mut close = [0; 2];
        client.read_exact(&mut close).await.unwrap();
        assert_eq!(close, [0x88, 0]);
        proxy.stop();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn real_tls_proxy_reports_pin_mismatch_on_a_proxied_request() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (port, server) = start_tls_server(identity.config, |_| async {
            Ok(Response::new(test_body("unreachable")))
        })
        .await;
        let mut wrong_pin = identity.pin;
        wrong_pin[0] ^= 1;
        let changed = Arc::new(AtomicBool::new(false));
        let callback = Arc::clone(&changed);
        let proxy = proxy_for(port, wrong_pin, move || callback.store(true, Ordering::SeqCst)).await;
        let response = reqwest::get(format!("http://127.0.0.1:{}/api/health", proxy.port)).await.unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
        assert!(changed.load(Ordering::SeqCst));
        proxy.stop();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn after_a_pin_mismatch_the_proxy_forwards_nothing_and_reports_the_change_once() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (port, server) = start_tls_server(identity.config, |_| async {
            Ok(Response::new(test_body("unreachable")))
        })
        .await;
        let mut wrong_pin = identity.pin;
        wrong_pin[0] ^= 1;
        let reports = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counted = Arc::clone(&reports);
        let proxy = proxy_for(port, wrong_pin, move || {
            counted.fetch_add(1, Ordering::SeqCst);
        })
        .await;
        let url = format!("http://127.0.0.1:{}/api/health", proxy.port);

        assert_eq!(reqwest::get(&url).await.unwrap().status(), StatusCode::BAD_GATEWAY);
        server.await.unwrap();
        // The host is gone now, so a second dial would fail with a connection error; the refusal names the key.
        let second = reqwest::get(&url).await.unwrap();

        assert_eq!(second.status(), StatusCode::BAD_GATEWAY);
        assert_eq!(second.text().await.unwrap(), KEY_CHANGED_MESSAGE);
        assert_eq!(reports.load(Ordering::SeqCst), 1);
        proxy.stop();
    }

    /// A TLS server that answers every connection until it is aborted.
    async fn start_tls_pool(config: Arc<rustls::ServerConfig>) -> (u16, JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let task = tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                let config = Arc::clone(&config);
                tokio::spawn(async move {
                    let Ok(tls) = tokio_rustls::TlsAcceptor::from(config).accept(stream).await else {
                        return;
                    };
                    let service = service_fn(|_| async { Ok::<_, Infallible>(Response::new(test_body("ok"))) });
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(TokioIo::new(tls), service)
                        .await;
                });
            }
        });
        (port, task)
    }

    /// A port nothing listens on.
    async fn dead_port() -> u16 {
        TcpListener::bind(("127.0.0.1", 0)).await.unwrap().local_addr().unwrap().port()
    }

    /// A resolver that counts its calls and answers each from `ports` in order
    /// (the last answer repeats) after `delay`.
    fn counting_resolver(ports: Vec<u16>, delay: Duration) -> (Resolver, Arc<std::sync::atomic::AtomicUsize>) {
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counted = Arc::clone(&calls);
        let resolver: Resolver = Arc::new(move |_| {
            let call = counted.fetch_add(1, Ordering::SeqCst);
            let port = ports[call.min(ports.len() - 1)];
            Box::pin(async move {
                tokio::time::sleep(delay).await;
                Ok(("127.0.0.1".into(), port))
            })
        });
        (resolver, calls)
    }

    async fn proxy_with(resolver: Resolver, pin: [u8; 32]) -> ProxyHandle {
        start_routes(
            Locator::DnsSd { instance: "Studio".into() },
            RouteConfig::single(Trust::Pinned { spki_sha256: pin }, || {}),
            0,
            resolver,
        )
        .await
        .unwrap()
    }

    async fn get_all(proxy: &ProxyHandle, count: usize) -> Vec<StatusCode> {
        let url = format!("http://127.0.0.1:{}/api/health", proxy.port);
        futures_util::future::join_all((0..count).map(|_| reqwest::get(url.clone())))
            .await
            .into_iter()
            .map(|response| response.unwrap().status())
            .collect()
    }

    #[tokio::test]
    async fn concurrent_proxied_requests_share_one_resolve_and_later_ones_reuse_it() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (port, server) = start_tls_pool(identity.config).await;
        let (resolver, calls) = counting_resolver(vec![port], Duration::from_millis(150));
        let proxy = proxy_with(resolver, identity.pin).await;

        assert_eq!(get_all(&proxy, 8).await, vec![StatusCode::OK; 8]);
        assert_eq!(calls.load(Ordering::SeqCst), 1, "eight concurrent requests resolve once");
        assert_eq!(get_all(&proxy, 2).await, vec![StatusCode::OK; 2]);
        assert_eq!(calls.load(Ordering::SeqCst), 1, "later requests reuse the cached address");
        proxy.stop();
        server.abort();
    }

    #[tokio::test]
    async fn a_connect_failure_drops_the_cache_and_resolves_again() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (port, server) = start_tls_pool(identity.config).await;
        let (resolver, calls) = counting_resolver(vec![dead_port().await, port], Duration::ZERO);
        let proxy = proxy_with(resolver, identity.pin).await;

        assert_eq!(get_all(&proxy, 1).await, vec![StatusCode::OK]);
        assert_eq!(calls.load(Ordering::SeqCst), 2, "the stale address is resolved once more");
        assert_eq!(get_all(&proxy, 1).await, vec![StatusCode::OK]);
        assert_eq!(calls.load(Ordering::SeqCst), 2, "the fresh address is cached");
        proxy.stop();
        server.abort();
    }

    #[tokio::test]
    async fn concurrent_connect_failures_share_one_re_resolve() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (port, server) = start_tls_pool(identity.config).await;
        let (resolver, calls) = counting_resolver(vec![dead_port().await, port], Duration::from_millis(150));
        let proxy = proxy_with(resolver, identity.pin).await;

        assert_eq!(get_all(&proxy, 6).await, vec![StatusCode::OK; 6]);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        proxy.stop();
        server.abort();
    }

    #[tokio::test]
    async fn an_unchanged_address_after_a_connect_failure_is_not_dialled_twice() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let (resolver, calls) = counting_resolver(vec![dead_port().await], Duration::ZERO);
        let proxy = proxy_with(resolver, identity.pin).await;

        assert_eq!(get_all(&proxy, 1).await, vec![StatusCode::BAD_GATEWAY]);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        proxy.stop();
    }

    #[test]
    fn rewrite_brackets_an_ipv6_authority_in_the_host_header_and_uri() {
        let mut request = Request::builder().uri("/api/health?x=1").body(()).unwrap();
        let authority = RemoteHost::parse("fe80::1%4").unwrap().authority(47901);
        rewrite_request(&mut request, &authority).unwrap();
        assert_eq!(request.headers().get(HOST).unwrap(), "[fe80::1]:47901");
        assert_eq!(request.uri().to_string(), "https://[fe80::1]:47901/api/health?x=1");
    }
}
