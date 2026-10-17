use std::time::Duration;

use reqwest::{Client, StatusCode};
use semver::Version;
use serde::Deserialize;

use crate::pairing::{Endpoint, PairError};
use crate::trust::{self, Trust};

pub const SHELL_PROTOCOL: u32 = 1;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Compat {
    Ok,
    Incompatible { app: String, host: Option<String> },
    TooOld,
}

#[derive(Deserialize)]
struct VersionResponse {
    app: Option<HostApp>,
}

#[derive(Deserialize)]
struct HostApp {
    version: Option<String>,
    shell_protocol: Option<u32>,
}

pub async fn check(host: &str, port: u16, trust: &Trust) -> Result<Compat, PairError> {
    check_endpoint(&Endpoint::new(host, port)?, trust).await
}

async fn check_endpoint(endpoint: &Endpoint, trust: &Trust) -> Result<Compat, PairError> {
    let config = trust::client_config(trust).map_err(|error| PairError::Protocol(error.to_string()))?;
    let client = endpoint
        .configure(
            Client::builder()
                .use_preconfigured_tls(config)
                .https_only(true)
                .timeout(Duration::from_secs(15)),
        )?
        .build()
        .map_err(|error| PairError::Protocol(error.to_string()))?;
    let response = client
        .get(endpoint.url("/api/version"))
        .send()
        .await
        .map_err(PairError::from)?;
    if response.status() == StatusCode::NOT_FOUND {
        return Ok(Compat::TooOld);
    }
    if !response.status().is_success() {
        return Err(PairError::Protocol(format!(
            "version check failed with HTTP {}",
            response.status()
        )));
    }
    let payload: VersionResponse = response
        .json()
        .await
        .map_err(|error| PairError::Protocol(format!("invalid version response: {error}")))?;
    let Some(app) = payload.app else {
        return Ok(Compat::TooOld);
    };
    let Some(protocol) = app.shell_protocol else {
        return Ok(Compat::TooOld);
    };
    if protocol == SHELL_PROTOCOL {
        return Ok(Compat::Ok);
    }
    Ok(Compat::Incompatible {
        app: env!("CARGO_PKG_VERSION").to_owned(),
        host: app.version,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_protocol_matches_the_backend_source_constant() {
        let backend = include_str!("../../../backend/api/version.py");
        assert!(backend.contains(&format!("SHELL_PROTOCOL: int = {SHELL_PROTOCOL}\n")));
    }

    #[tokio::test]
    async fn a_link_local_host_is_checked_through_a_bracketed_host_header() {
        use std::{convert::Infallible, sync::{Arc, Mutex}};

        use http_body_util::Full;
        use hyper::{body::{Bytes, Incoming}, service::service_fn, Request, Response};
        use hyper_util::rt::TokioIo;

        let identity = crate::test_tls::generated_self_signed_identity();
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let hosts = Arc::new(Mutex::new(Vec::new()));
        let seen = Arc::clone(&hosts);
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let tls = tokio_rustls::TlsAcceptor::from(identity.config).accept(stream).await.unwrap();
            let service = service_fn(move |request: Request<Incoming>| {
                let seen = Arc::clone(&seen);
                async move {
                    seen.lock().unwrap().push(request.headers()["host"].to_str().unwrap().to_owned());
                    let body = format!("{{\"app\":{{\"version\":\"1.0.0\",\"shell_protocol\":{SHELL_PROTOCOL}}}}}");
                    Ok::<_, Infallible>(Response::new(Full::new(Bytes::from(body))))
                }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .serve_connection(TokioIo::new(tls), service)
                .await;
        });
        let endpoint = Endpoint::new("fe80::1%4", port)
            .unwrap()
            .redirected_to(std::net::SocketAddr::from(([127, 0, 0, 1], port)));
        assert_eq!(endpoint.url("/api/version"), format!("https://link-local.invalid:{port}/api/version"));

        let compat = check_endpoint(&endpoint, &Trust::Pinned { spki_sha256: identity.pin }).await.unwrap();

        assert_eq!(compat, Compat::Ok);
        assert_eq!(*hosts.lock().unwrap(), vec![format!("[fe80::1]:{port}")]);
        server.abort();
    }

    #[test]
    fn a_global_ipv6_host_gets_a_bracketed_url() {
        assert_eq!(
            Endpoint::new("2001:db8::7", 47901).unwrap().url("/api/version"),
            "https://[2001:db8::7]:47901/api/version"
        );
    }

    #[test]
    fn semver_can_order_the_versions_displayed_on_incompatibility() {
        assert!(Version::parse("1.2.0").unwrap() > Version::parse("1.1.9").unwrap());
    }
}
