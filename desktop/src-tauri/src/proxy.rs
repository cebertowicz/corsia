use std::{
    convert::Infallible,
    error::Error,
    future::Future,
    net::SocketAddr,
    pin::Pin,
    sync::Arc,
    time::Duration,
};

use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::{
    body::{Bytes, Incoming},
    header::{HeaderValue, HOST},
    service::service_fn,
    Request, Response, StatusCode, Uri,
};
use hyper_util::rt::TokioIo;
use tokio::{net::{TcpListener, TcpStream}, sync::oneshot};
use tokio_rustls::TlsConnector;

use crate::{remote_config::Locator, trust::{self, Trust}};

type ProxyBody = BoxBody<Bytes, Box<dyn Error + Send + Sync>>;
type ResolveFuture = Pin<Box<dyn Future<Output = Result<(String, u16), ProxyError>> + Send>>;
type Resolver = Arc<dyn Fn(Locator) -> ResolveFuture + Send + Sync>;

#[derive(Debug)]
pub enum ProxyError {
    Bind(String),
    Resolve(String),
    Unreachable(crate::classify::Unreachable),
    Transport(String),
}

pub struct ProxyHandle {
    pub port: u16,
    shutdown: Option<oneshot::Sender<()>>,
}

impl ProxyHandle {
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

pub async fn start(
    locator: Locator,
    trust: Trust,
    preferred_port: u16,
    on_key_changed: impl Fn() + Send + Sync + 'static,
) -> Result<ProxyHandle, ProxyError> {
    start_with_resolver(
        locator,
        trust,
        preferred_port,
        on_key_changed,
        Arc::new(|locator| Box::pin(default_resolve(locator))),
    )
    .await
}

pub async fn start_with_resolver(
    locator: Locator,
    trust: Trust,
    preferred_port: u16,
    on_key_changed: impl Fn() + Send + Sync + 'static,
    resolver: Resolver,
) -> Result<ProxyHandle, ProxyError> {
    let listener = bind_loopback(preferred_port).await?;
    let port = listener.local_addr().map_err(|error| ProxyError::Bind(error.to_string()))?.port();
    let (shutdown_send, mut shutdown_receive) = oneshot::channel();
    let state = Arc::new(ProxyState {
        locator,
        trust,
        local_port: port,
        on_key_changed: Arc::new(on_key_changed),
        resolver,
    });
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
    Ok(ProxyHandle { port, shutdown: Some(shutdown_send) })
}

struct ProxyState {
    locator: Locator,
    trust: Trust,
    local_port: u16,
    on_key_changed: Arc<dyn Fn() + Send + Sync>,
    resolver: Resolver,
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

async fn forward_trusted(
    mut request: Request<Incoming>,
    state: Arc<ProxyState>,
) -> Result<Response<ProxyBody>, String> {
    let (host, port) = (state.resolver)(state.locator.clone()).await.map_err(|error| match error {
        ProxyError::Resolve(message) => message,
        ProxyError::Unreachable(crate::classify::Unreachable::Network) => "remote host is unreachable on the network".into(),
        ProxyError::Unreachable(crate::classify::Unreachable::Server) => "remote CORSIA server is unavailable".into(),
        other => format!("{other:?}"),
    })?;
    let stream = TcpStream::connect((host.as_str(), port)).await.map_err(|error| error.to_string())?;
    let config = trust::client_config(&state.trust).map_err(|error| error.to_string())?;
    let name = rustls::pki_types::ServerName::try_from(host.clone()).map_err(|error| error.to_string())?;
    let tls = TlsConnector::from(Arc::new(config)).connect(name, stream).await.map_err(|error| {
        if matches!(state.trust, Trust::Pinned { .. }) && error.to_string().contains("SPKI") {
            (state.on_key_changed)();
        }
        error.to_string()
    })?;
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(tls))
        .await
        .map_err(|error| error.to_string())?;
    tokio::spawn(async move { let _ = connection.with_upgrades().await; });

    let client_upgrade = is_upgrade_request(&request).then(|| hyper::upgrade::on(&mut request));
    rewrite_request(&mut request, &host, port)?;
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
                crate::discovery::DiscoveryError::Timeout => ProxyError::Unreachable(crate::classify::Unreachable::Network),
                other => ProxyError::Resolve(format!("DNS-SD resolution failed: {other}")),
            }),
    }
}

fn rewrite_request(request: &mut Request<Incoming>, host: &str, port: u16) -> Result<(), String> {
    request.headers_mut().remove("forwarded");
    request.headers_mut().remove("x-forwarded-for");
    request.headers_mut().remove("x-forwarded-host");
    request.headers_mut().remove("x-forwarded-proto");
    request.headers_mut().insert(
        HOST,
        HeaderValue::from_str(&format!("{host}:{port}")).map_err(|error| error.to_string())?,
    );
    let path_and_query = request.uri().path_and_query().map(|value| value.as_str()).unwrap_or("/");
    *request.uri_mut() = Uri::builder()
        .scheme("https")
        .authority(format!("{host}:{port}"))
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
    use hyper::{body::Frame, header::{CONNECTION, UPGRADE}, upgrade};
    use tokio::{io::{AsyncReadExt, AsyncWriteExt}, task::JoinHandle};

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
        start_with_resolver(
            Locator::Address { host: "127.0.0.1".into(), port },
            Trust::Pinned { spki_sha256: pin },
            0,
            on_key_changed,
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
}
