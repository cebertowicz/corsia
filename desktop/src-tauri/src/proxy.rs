use std::{convert::Infallible, error::Error, net::SocketAddr, sync::Arc};

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

#[derive(Debug)]
pub enum ProxyError {
    Bind(String),
    Resolve(String),
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
    let listener = bind_loopback(preferred_port).await?;
    let port = listener.local_addr().map_err(|error| ProxyError::Bind(error.to_string()))?.port();
    let (shutdown_send, mut shutdown_receive) = oneshot::channel();
    let state = Arc::new(ProxyState {
        locator,
        trust,
        local_port: port,
        on_key_changed: Arc::new(on_key_changed),
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

async fn forward_trusted(
    mut request: Request<Incoming>,
    state: Arc<ProxyState>,
) -> Result<Response<ProxyBody>, String> {
    let (host, port) = resolve_locator(&state.locator).await?;
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

    rewrite_request(&mut request, &host, port)?;
    let response = sender.send_request(request).await.map_err(|error| error.to_string())?;
    Ok(response.map(|body| body.map_err(|error| Box::new(error) as Box<dyn Error + Send + Sync>).boxed()))
}

async fn resolve_locator(locator: &Locator) -> Result<(String, u16), String> {
    match locator {
        Locator::Address { host, port } => Ok((host.clone(), *port)),
        Locator::DnsSd { instance } => Err(format!("DNS-SD locator {instance:?} requires the discovery resolver")),
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
    use super::*;

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
}
