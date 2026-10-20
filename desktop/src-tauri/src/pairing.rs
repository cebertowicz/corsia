use std::{error::Error, net::SocketAddr, time::Duration};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use hmac::{Hmac, Mac};
use hkdf::Hkdf;
use reqwest::{header::{HeaderMap, HeaderValue, HOST}, Client, ClientBuilder, StatusCode};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use spake2::{Ed25519Group, Identity, Password, Spake2};

use crate::{
    classify::{classify_connect_error, classify_tls_against_plain_http, ConnectFailure},
    remote_host::RemoteHost,
    trust::{self, Trust},
};

const PAIRING_IDENTITY: &[u8] = b"corsia-pair-v1";
type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Paired {
    pub name: String,
    pub trust: Trust,
    pub host: String,
    pub port: u16,
    /// Stable host identity, confirmed by every successful new pairing.
    pub identity: Option<[u8; 32]>,
    /// Optional bare host[:port] learned while this local pinned connection was established.
    pub outside_address: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PairError {
    WrongCode,
    Expired,
    TooManyTries,
    Paused,
    Busy,
    NotLocal,
    Unreachable(ConnectFailure),
    TooOld,
    /// The host presented a key other than the pinned one: not a network failure.
    KeyChanged,
    Protocol(String),
}

impl PairError {
    /// The connection failure behind this error, or `None` for a refusal of the code itself (wrong, expired, paused,
    /// busy), which is about the pairing and not about reaching the host.
    pub fn connect_failure(&self) -> Option<ConnectFailure> {
        match self {
            Self::Unreachable(failure) => Some(failure.clone()),
            Self::TooOld => Some(ConnectFailure::TooOld),
            Self::KeyChanged => Some(ConnectFailure::KeyChanged),
            Self::Protocol(_) => Some(ConnectFailure::Server),
            Self::WrongCode | Self::Expired | Self::TooManyTries | Self::Paused | Self::Busy | Self::NotLocal => None,
        }
    }
}

impl From<reqwest::Error> for PairError {
    fn from(error: reqwest::Error) -> Self {
        if trust::is_pin_mismatch(&error) {
            return Self::KeyChanged;
        }
        if error.is_timeout() {
            return Self::Unreachable(ConnectFailure::Network);
        }
        if error.is_connect() {
            let message = error.to_string().to_ascii_lowercase();
            let debug = format!("{error:?}").to_ascii_lowercase();
            // Rustls reports a TLS ClientHello answered by HTTP only in the
            // reqwest error's debug chain as InvalidContentType. A listener
            // answered, so this is the remote-server recovery state, not a
            // network outage.
            if debug.contains("invalidcontenttype") {
                return Self::Unreachable(classify_tls_against_plain_http());
            }
            let mut source = error.source();
            let mut io_classification = None;
            while let Some(cause) = source {
                if let Some(io) = cause.downcast_ref::<std::io::Error>() {
                    io_classification = Some(classify_connect_error(io));
                }
                source = cause.source();
            }
            if let Some(classification) = io_classification {
                return Self::Unreachable(classification);
            }
            if message.contains("plain http") || message.contains("wrong version") {
                return Self::Unreachable(classify_tls_against_plain_http());
            }
            return Self::Unreachable(ConnectFailure::Network);
        }
        Self::Protocol(error.to_string())
    }
}

#[derive(Serialize)]
struct StartRequest<'a> {
    mode: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    host: Option<&'a str>,
    msg_c: String,
}

#[derive(Deserialize)]
struct StartResponse {
    exchange_id: String,
    msg_s: String,
}

#[derive(Serialize)]
struct FinishRequest<'a> {
    exchange_id: &'a str,
    confirm_c: String,
}

#[derive(Deserialize)]
struct FinishResponse {
    confirm_s: String,
    name: String,
    #[serde(default)]
    spki_sha256: Option<String>,
    #[serde(default)]
    confirm_identity: Option<String>,
    #[serde(default)]
    outside_address: Option<String>,
}

#[derive(Deserialize)]
struct OutsideAddressResponse {
    outside_address: Option<String>,
}

#[derive(Deserialize)]
struct ErrorBody {
    detail: Option<ErrorDetail>,
}

#[derive(Deserialize)]
struct ErrorDetail {
    reason: Option<String>,
}

struct Exchange {
    start: StartResponse,
    msg_c: Vec<u8>,
    spake: Spake2<Ed25519Group>,
}


/// Where a pairing or version request goes. reqwest's URL type cannot carry an
/// IPv6 zone, so a link-local address is addressed by a placeholder name that
/// the client resolves to the scoped socket address.
#[derive(Clone, Debug)]
pub(crate) struct Endpoint {
    pub(crate) host: String,
    pub(crate) port: u16,
    remote: RemoteHost,
    #[cfg(test)]
    redirect: Option<SocketAddr>,
}

const LINK_LOCAL_PLACEHOLDER: &str = "link-local.invalid";

impl Endpoint {
    pub(crate) fn new(host: &str, port: u16) -> Result<Self, PairError> {
        let remote = RemoteHost::parse(host).map_err(PairError::Protocol)?;
        Ok(Self {
            host: host.to_owned(),
            port,
            remote,
            #[cfg(test)]
            redirect: None,
        })
    }

    pub(crate) fn url(&self, path: &str) -> String {
        match &self.remote {
            RemoteHost::Scoped { .. } => format!("https://{LINK_LOCAL_PLACEHOLDER}:{}{path}", self.port),
            remote => format!("https://{}{path}", remote.authority(self.port)),
        }
    }

    /// Apply the dial and Host-header overrides a link-local address needs.
    pub(crate) fn configure(&self, builder: ClientBuilder) -> Result<ClientBuilder, PairError> {
        let RemoteHost::Scoped { .. } = &self.remote else {
            return Ok(builder);
        };
        let dial = self.dial_address().expect("a scoped host has an address");
        let mut headers = HeaderMap::new();
        headers.insert(
            HOST,
            HeaderValue::from_str(&self.remote.authority(self.port)).map_err(|error| PairError::Protocol(error.to_string()))?,
        );
        Ok(builder.resolve(LINK_LOCAL_PLACEHOLDER, dial).default_headers(headers))
    }

    #[cfg(not(test))]
    fn dial_address(&self) -> Option<SocketAddr> {
        self.remote.socket_addr(self.port)
    }

    #[cfg(test)]
    fn dial_address(&self) -> Option<SocketAddr> {
        self.redirect.or_else(|| self.remote.socket_addr(self.port))
    }

    /// Test seam: dial `address` instead of the link-local address, which a
    /// test host cannot listen on. reqwest keeps the URL's port, not this one's.
    #[cfg(test)]
    pub(crate) fn redirected_to(mut self, address: SocketAddr) -> Self {
        self.redirect = Some(address);
        self
    }
}

pub async fn pair(host: &str, port: u16, code: &str) -> Result<Paired, PairError> {
    if code.len() != 4 {
        return Err(PairError::WrongCode);
    }
    pair_endpoint(Endpoint::new(host, port)?, code).await
}

async fn pair_endpoint(endpoint: Endpoint, code: &str) -> Result<Paired, PairError> {
    // An IPv6 literal has no certificate the platform verifier can trust, so
    // it pairs through a captured pin directly.
    let Some(canonical_host) = canonical_verified_host(&endpoint.host)? else {
        return pair_pinned(&endpoint, code).await;
    };

    // Prefer the platform verifier. A certificate that is not system-trusted
    // gets one capture attempt: PAKE then binds the captured SPKI before the
    // app records it as the pin. A network failure still fails when capture
    // attempts the same endpoint.
    let system_client = client_for(Trust::System, &endpoint)?;
    match start(&system_client, &endpoint, "verified", Some(&canonical_host), code).await {
        Ok(exchange) => {
            finish_exchange(
                system_client,
                &endpoint,
                Trust::System,
                transcript_prefix("verified", canonical_host.as_bytes()),
                exchange,
            )
            .await
        }
        Err(PairError::Unreachable(_)) => pair_pinned(&endpoint, code).await,
        Err(error) => Err(error),
    }
}

async fn pair_pinned(endpoint: &Endpoint, code: &str) -> Result<Paired, PairError> {
    let capture = trust::CaptureVerifier::new().map_err(|error| PairError::Protocol(error.to_string()))?;
    let client = endpoint
        .configure(
            Client::builder()
                .use_preconfigured_tls(trust::capture_client_config(capture.clone()))
                .https_only(true)
                .timeout(Duration::from_secs(15)),
        )?
        .build()
        .map_err(|error| PairError::Protocol(error.to_string()))?;
    let exchange = start(&client, endpoint, "pinned", None, code).await?;
    let pin = capture
        .observed_spki()
        .map_err(|error| PairError::Protocol(error.to_string()))?;
    finish_exchange(
        client_for(Trust::Pinned { spki_sha256: pin }, endpoint)?,
        endpoint,
        Trust::Pinned { spki_sha256: pin },
        transcript_prefix("pinned", &pin),
        exchange,
    )
    .await
}

fn client_for(trust: Trust, endpoint: &Endpoint) -> Result<Client, PairError> {
    let tls = trust::client_config(&trust).map_err(|error| PairError::Protocol(error.to_string()))?;
    endpoint
        .configure(
            Client::builder()
                .use_preconfigured_tls(tls)
                .https_only(true)
                .timeout(Duration::from_secs(15)),
        )?
        .build()
        .map_err(|error| PairError::Protocol(error.to_string()))
}

async fn start(
    client: &Client,
    endpoint: &Endpoint,
    mode: &str,
    verified_host: Option<&str>,
    code: &str,
) -> Result<Exchange, PairError> {
    let (msg_c, spake) = start_spake(code)?;
    let request = StartRequest {
        mode,
        host: verified_host,
        msg_c: BASE64.encode(&msg_c),
    };
    let response = client
        .post(endpoint.url("/api/devices/pair/start"))
        .json(&request)
        .send()
        .await?;
    if response.status() == StatusCode::NOT_FOUND {
        return Err(PairError::TooOld);
    }
    if !response.status().is_success() {
        return Err(pair_status(response).await);
    }
    let start: StartResponse = response
        .json()
        .await
        .map_err(|error| PairError::Protocol(format!("invalid pair/start response: {error}")))?;
    Ok(Exchange { start, msg_c, spake })
}

async fn finish_exchange(
    client: Client,
    endpoint: &Endpoint,
    trust: Trust,
    prefix: Vec<u8>,
    exchange: Exchange,
) -> Result<Paired, PairError> {
    let msg_s = BASE64
        .decode(&exchange.start.msg_s)
        .map_err(|_| PairError::Protocol("pair/start returned a non-base64 server SPAKE message".into()))?;
    if msg_s == exchange.msg_c {
        return Err(PairError::Protocol("reflected server SPAKE message".into()));
    }
    let shared = exchange
        .spake
        .finish(&msg_s)
        .map_err(|error| PairError::Protocol(format!("invalid server SPAKE message: {error}")))?;
    let (client_key, server_key) = confirmation_keys(&shared)?;
    let mut transcript = prefix;
    transcript.extend_from_slice(&exchange.msg_c);
    transcript.extend_from_slice(&msg_s);
    let confirm_c = confirmation(&client_key, &transcript);
    let response = client
        .post(endpoint.url("/api/devices/pair/finish"))
        .json(&FinishRequest {
            exchange_id: &exchange.start.exchange_id,
            confirm_c: BASE64.encode(confirm_c),
        })
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(pair_status(response).await);
    }
    let finish: FinishResponse = response
        .json()
        .await
        .map_err(|error| PairError::Protocol(format!("invalid pair/finish response: {error}")))?;
    validate_name(&finish.name)?;
    let confirm_s = BASE64
        .decode(&finish.confirm_s)
        .map_err(|_| PairError::Protocol("pair/finish returned a non-base64 server confirmation".into()))?;
    transcript.extend_from_slice(b"name:");
    transcript.extend_from_slice(finish.name.as_bytes());
    verify_confirmation(&server_key, &transcript, &confirm_s)?;
    let identity = match (finish.spki_sha256, finish.confirm_identity) {
        (Some(fingerprint), Some(confirmation)) => confirmed_identity(&server_key, transcript, &fingerprint, &confirmation)?,
        _ => return Err(PairError::Protocol("pair/finish returned an incomplete identity confirmation".into())),
    };
    Ok(Paired {
        name: finish.name,
        trust,
        host: endpoint.host.clone(),
        port: endpoint.port,
        identity: Some(identity),
        outside_address: finish.outside_address,
    })
}

/// Read the endpoint only over the paired local route, whose TLS key is pinned.
pub async fn outside_address(host: &str, port: u16, trust: Trust) -> Result<Option<String>, PairError> {
    let endpoint = Endpoint::new(host, port)?;
    let response = client_for(trust, &endpoint)?
        .get(endpoint.url("/api/devices/outside-address"))
        .send()
        .await?;
    if !response.status().is_success() {
        return Err(pair_status(response).await);
    }
    response
        .json::<OutsideAddressResponse>()
        .await
        .map(|response| response.outside_address)
        .map_err(|error| PairError::Protocol(format!("invalid outside-address response: {error}")))
}

/// The host's identity fingerprint, accepted only under its own confirmation over the name-bound transcript.
fn confirmed_identity(server_key: &[u8; 32], mut transcript: Vec<u8>, fingerprint: &str, confirmation: &str) -> Result<[u8; 32], PairError> {
    let fingerprint: [u8; 32] = BASE64
        .decode(fingerprint)
        .map_err(|_| PairError::Protocol("pair/finish returned a non-base64 identity".into()))?
        .try_into()
        .map_err(|_| PairError::Protocol("pair/finish identity must contain exactly 32 bytes".into()))?;
    let confirmation = BASE64
        .decode(confirmation)
        .map_err(|_| PairError::Protocol("pair/finish returned a non-base64 identity confirmation".into()))?;
    transcript.extend_from_slice(b"spki:");
    transcript.extend_from_slice(&fingerprint);
    verify_confirmation(server_key, &transcript, &confirmation)?;
    Ok(fingerprint)
}

fn start_spake(code: &str) -> Result<(Vec<u8>, Spake2<Ed25519Group>), PairError> {
    let password = pairing_password(code);
    let (state, message) = Spake2::<Ed25519Group>::start_symmetric(
        &Password::new(&password),
        &Identity::new(PAIRING_IDENTITY),
    );
    Ok((message, state))
}

fn pairing_password(code: &str) -> [u8; 32] {
    Sha256::digest([PAIRING_IDENTITY, b"\0", code.as_bytes()].concat()).into()
}

/// The host bound into a verified pairing transcript, or `None` when the host cannot have a system-trusted
/// certificate: an address, a `.local` or `.localhost` name, or a name without a dot. Those pair through the
/// host's pin; letting them verify would let whoever answers for the address (DNS-SD is unauthenticated)
/// pair as a relay with a certificate it owns.
fn canonical_verified_host(host: &str) -> Result<Option<String>, PairError> {
    match RemoteHost::parse(host.trim_end_matches('.')).map_err(PairError::Protocol)? {
        RemoteHost::Name(name) => {
            let name = name.to_ascii_lowercase();
            Ok(can_hold_a_public_certificate(&name).then_some(name))
        }
        RemoteHost::Ip(_) | RemoteHost::Scoped { .. } => Ok(None),
    }
}

fn can_hold_a_public_certificate(name: &str) -> bool {
    name.contains('.') && !name.ends_with(".local") && !name.ends_with(".localhost")
}

fn transcript_prefix(mode: &str, binding: &[u8]) -> Vec<u8> {
    let mut prefix = Vec::with_capacity(PAIRING_IDENTITY.len() + mode.len() + binding.len() + 2);
    prefix.extend_from_slice(PAIRING_IDENTITY);
    prefix.extend_from_slice(mode.as_bytes());
    prefix.push(b':');
    prefix.extend_from_slice(binding);
    prefix.push(0);
    prefix
}

async fn pair_status(response: reqwest::Response) -> PairError {
    let status = response.status();
    if status == StatusCode::TOO_MANY_REQUESTS {
        return PairError::Busy;
    }
    let reason = response
        .json::<ErrorBody>()
        .await
        .ok()
        .and_then(|body| body.detail.and_then(|detail| detail.reason));
    match reason.as_deref() {
        Some("wrong_code") => PairError::WrongCode,
        Some("expired") => PairError::Expired,
        Some("too_many_tries") => PairError::TooManyTries,
        Some("paused") => PairError::Paused,
        Some("not_on_this_network") => PairError::NotLocal,
        _ => PairError::Protocol(format!("pairing request failed with HTTP {status}")),
    }
}

fn confirmation_keys(shared_key: &[u8]) -> Result<([u8; 32], [u8; 32]), PairError> {
    let mut material = [0u8; 64];
    Hkdf::<Sha256>::new(None, shared_key)
        .expand(PAIRING_IDENTITY, &mut material)
        .map_err(|_| PairError::Protocol("could not derive pairing confirmations".into()))?;
    let mut client = [0u8; 32];
    let mut server = [0u8; 32];
    client.copy_from_slice(&material[..32]);
    server.copy_from_slice(&material[32..]);
    Ok((client, server))
}

fn confirmation(key: &[u8; 32], transcript: &[u8]) -> [u8; 32] {
    let mut mac = HmacSha256::new_from_slice(key).expect("SHA-256 accepts any key length");
    mac.update(transcript);
    mac.finalize().into_bytes().into()
}

fn verify_confirmation(key: &[u8; 32], transcript: &[u8], actual: &[u8]) -> Result<(), PairError> {
    let mut mac = HmacSha256::new_from_slice(key).expect("SHA-256 accepts any key length");
    mac.update(transcript);
    mac.verify_slice(actual)
        .map_err(|_| PairError::Protocol("server pairing confirmation did not match".into()))
}

fn validate_name(name: &str) -> Result<(), PairError> {
    if name.is_empty() || name.len() > 63 {
        return Err(PairError::Protocol("pair/finish returned an invalid computer name".into()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{convert::Infallible, sync::{Arc, Mutex}};

    use super::*;
    use http_body_util::BodyExt;
    use hyper::{body::{Bytes, Incoming}, service::service_fn, Request, Response};
    use hyper_util::rt::TokioIo;
    use rand_chacha::{rand_core::SeedableRng, ChaCha20Rng};
    use tokio::net::TcpListener;

    fn decode_hex(value: &str) -> Vec<u8> {
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).unwrap())
            .collect()
    }

    fn json_response(value: serde_json::Value) -> Response<http_body_util::Full<Bytes>> {
        Response::new(http_body_util::Full::new(Bytes::from(serde_json::to_vec(&value).unwrap())))
    }

    async fn start_pairing_server(
        config: Arc<rustls::ServerConfig>,
        pin: [u8; 32],
        requests: Arc<Mutex<Vec<String>>>,
        hosts: Arc<Mutex<Vec<String>>>,
        connections: usize,
    ) -> (u16, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let exchange = Arc::new(Mutex::new(None));
        let task = tokio::spawn(async move {
            let mut served = Vec::new();
            for _ in 0..connections {
            let (stream, _) = listener.accept().await.unwrap();
            let config = Arc::clone(&config);
            let exchange = Arc::clone(&exchange);
            let requests = Arc::clone(&requests);
            let hosts = Arc::clone(&hosts);
            served.push(tokio::spawn(async move {
            let tls = tokio_rustls::TlsAcceptor::from(config).accept(stream).await.unwrap();
            let service = service_fn(move |request: Request<Incoming>| {
                let exchange = Arc::clone(&exchange);
                let requests = Arc::clone(&requests);
                let hosts = Arc::clone(&hosts);
                async move {
                    let path = request.uri().path().to_owned();
                    if let Some(host) = request.headers().get("host") {
                        hosts.lock().unwrap().push(host.to_str().unwrap().to_owned());
                    }
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let body_text = String::from_utf8(body.to_vec()).unwrap();
                    requests.lock().unwrap().push(body_text.clone());
                    let request: serde_json::Value = serde_json::from_str(&body_text).unwrap();
                    let response = if path == "/api/devices/pair/start" {
                        let message = BASE64.decode(request["msg_c"].as_str().unwrap()).unwrap();
                        let password = pairing_password("K7QX");
                        let (spake, reply) = Spake2::<Ed25519Group>::start_symmetric(
                            &Password::new(&password),
                            &Identity::new(PAIRING_IDENTITY),
                        );
                        *exchange.lock().unwrap() = Some((spake, message, reply.clone()));
                        serde_json::json!({"exchange_id": "test-exchange", "msg_s": BASE64.encode(reply)})
                    } else {
                        let (spake, msg_c, msg_s) = exchange.lock().unwrap().take().unwrap();
                        let shared = spake.finish(&msg_c).unwrap();
                        let (client_key, server_key) = confirmation_keys(&shared).unwrap();
                        let mut transcript = transcript_prefix("pinned", &pin);
                        transcript.extend_from_slice(&msg_c);
                        transcript.extend_from_slice(&msg_s);
                        assert_eq!(
                            BASE64.decode(request["confirm_c"].as_str().unwrap()).unwrap(),
                            confirmation(&client_key, &transcript),
                        );
                        transcript.extend_from_slice(b"name:Studio");
                        serde_json::json!({
                            "confirm_s": BASE64.encode(confirmation(&server_key, &transcript)),
                            "name": "Studio",
                            "spki_sha256": BASE64.encode(pin),
                            "confirm_identity": BASE64.encode(confirmation(&server_key, &[transcript.as_slice(), b"spki:", &pin].concat())),
                        })
                    };
                    Ok::<_, Infallible>(json_response(response))
                }
            });
            hyper::server::conn::http1::Builder::new()
                .serve_connection(TokioIo::new(tls), service)
                .await
                .unwrap();
            }));
            }
            for connection in served {
                connection.await.unwrap();
            }
        });
        (port, task)
    }

    #[tokio::test]
    async fn same_network_refusal_reaches_the_frontend_reason() {
        let response = hyper::Response::builder().status(403)
            .body(include_str!("../../../testdata/pairing-not-local.json")).unwrap();
        assert_eq!(pair_status(reqwest::Response::from(response)).await, PairError::NotLocal);
        assert!(matches!(crate::remote_pair_error(PairError::NotLocal), crate::RemotePairResponse::Failure { reason, .. } if reason == "not_local"));
    }

    #[test]
    fn password_is_bound_to_the_protocol_identity() {
        assert_eq!(
            pairing_password("K7QX"),
            [
                0xe2, 0xdf, 0x18, 0x8b, 0x39, 0xf2, 0xa6, 0xec, 0x96, 0x38, 0xbd, 0xec,
                0x0a, 0x1b, 0xd0, 0x97, 0x14, 0xa1, 0x5e, 0xcf, 0x9f, 0xee, 0x4a, 0x1f,
                0xf9, 0xf5, 0xd9, 0xd0, 0x8b, 0x28, 0x08, 0xf1,
            ]
        );
    }

    #[test]
    fn verified_host_is_canonicalized_like_the_host() {
        assert_eq!(canonical_verified_host("Studio.Example.COM.").unwrap(), Some("studio.example.com".to_owned()));
    }

    #[test]
    fn addresses_and_local_names_are_never_verified_but_are_accepted() {
        for host in ["::1", "[::1]", "fe80::1%4", "[fe80::1%254]", "2001:db8::7", "192.168.1.20", "203.0.113.9", "Studio.LOCAL.", "studio", "printer.localhost"] {
            assert_eq!(canonical_verified_host(host).unwrap(), None, "{host}");
        }
        assert_eq!(canonical_verified_host("Studio.Tail1234.TS.net.").unwrap(), Some("studio.tail1234.ts.net".to_owned()));
    }

    #[test]
    fn a_host_with_a_port_is_still_refused() {
        for host in ["studio.local:47901", "", "[studio.local]"] {
            assert!(matches!(canonical_verified_host(host), Err(PairError::Protocol(_))), "{host:?}");
        }
    }

    #[test]
    fn identity_is_accepted_only_under_its_confirmation() {
        let (key, transcript, fingerprint) = ([3_u8; 32], b"transcript".to_vec(), [9_u8; 32]);
        let signed = confirmation(&key, &[transcript.as_slice(), b"spki:", &fingerprint].concat());
        let accept = |fingerprint: &[u8], signed: &[u8]| confirmed_identity(&key, transcript.clone(), &BASE64.encode(fingerprint), &BASE64.encode(signed));
        assert_eq!(accept(&fingerprint, &signed).unwrap(), fingerprint);
        assert!(accept(&[8_u8; 32], &signed).is_err(), "a swapped fingerprint");
        assert!(accept(&fingerprint, &confirmation(&key, &transcript)).is_err(), "the name confirmation reflected");
        assert!(accept(&fingerprint[..31], &signed).is_err(), "a short fingerprint");
    }

    #[test]
    fn link_local_endpoints_use_a_placeholder_url_and_a_bracketed_host_header() {
        let endpoint = Endpoint::new("fe80::1%4", 47901).unwrap();
        assert_eq!(endpoint.url("/p"), "https://link-local.invalid:47901/p");
        assert_eq!(Endpoint::new("::1", 47901).unwrap().url("/p"), "https://[::1]:47901/p");
        assert_eq!(Endpoint::new("studio.local", 47901).unwrap().url("/p"), "https://studio.local:47901/p");
    }

    #[tokio::test]
    async fn an_aaaa_only_link_local_answer_pairs_through_a_bracketed_host() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let hosts = Arc::new(Mutex::new(Vec::new()));
        // The pinned capture takes one connection for pair/start and a second,
        // pin-verified one for pair/finish.
        let (port, server) = start_pairing_server(
            identity.config,
            identity.pin,
            Arc::clone(&requests),
            Arc::clone(&hosts),
            2,
        )
        .await;
        // What discovery returns for an AAAA-only host on interface index 4.
        let host = "fe80::1%4";
        let endpoint = Endpoint::new(host, port)
            .unwrap()
            .redirected_to(SocketAddr::from(([127, 0, 0, 1], port)));

        let paired = pair_endpoint(endpoint, "K7QX").await.unwrap();

        assert_eq!(paired.name, "Studio");
        assert_eq!((paired.host.as_str(), paired.port), (host, port));
        assert!(matches!(paired.trust, Trust::Pinned { spki_sha256 } if spki_sha256 == identity.pin));
        server.await.unwrap();
        assert_eq!(*hosts.lock().unwrap(), vec![format!("[fe80::1]:{port}"); 2]);
        let first: serde_json::Value = serde_json::from_str(&requests.lock().unwrap()[0]).unwrap();
        assert_eq!(first["mode"], "pinned");
        assert!(first.get("host").is_none(), "an IPv6 literal is never bound as a verified host");
    }

    #[test]
    fn connect_failures_keep_the_r4_cause() {
        assert_eq!(
            PairError::Unreachable(classify_connect_error(&std::io::Error::from(std::io::ErrorKind::TimedOut))),
            PairError::Unreachable(ConnectFailure::Network)
        );
        assert_eq!(
            PairError::Unreachable(classify_connect_error(&std::io::Error::from(std::io::ErrorKind::ConnectionRefused))),
            PairError::Unreachable(ConnectFailure::Server)
        );
        assert_eq!(
            PairError::Unreachable(classify_tls_against_plain_http()),
            PairError::Unreachable(ConnectFailure::Server)
        );
    }

    #[test]
    fn invalid_names_are_never_trusted() {
        assert!(validate_name("").is_err());
        assert!(validate_name(&"a".repeat(64)).is_err());
        assert!(validate_name("Studio").is_ok());
    }

    #[test]
    fn confirmation_rejects_a_reflected_or_changed_value() {
        let key = [7; 32];
        let transcript = b"test transcript";
        let expected = confirmation(&key, transcript);
        assert!(verify_confirmation(&key, transcript, &expected).is_ok());
        assert!(verify_confirmation(&key, transcript, &confirmation(&key, b"different")).is_err());
    }

    #[test]
    fn committed_spake2_vector_reproduces_the_rust_message_and_shared_key() {
        let password = Password::new(&pairing_password("K7QX"));
        let identity = Identity::new(PAIRING_IDENTITY);
        let mut rng = ChaCha20Rng::from_seed([0xaa; 32]);
        let (state, message) = Spake2::<Ed25519Group>::start_symmetric_with_rng(
            &password,
            &identity,
            &mut rng,
        );
        assert_eq!(
            message,
            decode_hex("53f3beca55cf3041ceddcd8ac9aa5c1cad9e1734a4a5093eb9934e76c328db91d1")
        );
        let shared = state.finish(&decode_hex("531412a5eceda93c395e55c8fb26e83274a036606807f3e41c2154175b54e42e76")).unwrap();
        assert_eq!(shared, decode_hex("d1cff6e3453c5becbc814318a44bdca35ed9b8366ed26d7b0501c70ec009dfa2"));
        let (_, server_key) = confirmation_keys(&shared).unwrap();
        let mut transcript = transcript_prefix("verified", b"studio.local");
        transcript.extend_from_slice(&decode_hex("531412a5eceda93c395e55c8fb26e83274a036606807f3e41c2154175b54e42e76"));
        transcript.extend_from_slice(&message);
        transcript.extend_from_slice(b"name:Studio");
        assert_eq!(
            confirmation(&server_key, &transcript).as_slice(),
            decode_hex("944a40f6df11c32fd42a3a2652610d84113da85571c5d6f62a9e8867423be7e0")
        );
    }

    #[test]
    fn committed_spake2_vector_reproduces_the_rust_first_message_and_shared_key() {
        let password = pairing_password("K7QX");
        let identity = Identity::new(PAIRING_IDENTITY);
        let mut rng = ChaCha20Rng::from_seed([0x22; 32]);
        let (state, message) = Spake2::<Ed25519Group>::start_symmetric_with_rng(
            &Password::new(&password),
            &identity,
            &mut rng,
        );
        assert_eq!(
            message,
            decode_hex("532d48ad759c38f68782de779208ac0440d28191e2ecbdaaae158894a05788b1f2")
        );
        let shared = state.finish(&decode_hex("53d8cb46e1104bd78d9cc5b6ffaefe49f1f8f80673b790f7bfff29da99d98539bd")).unwrap();
        assert_eq!(shared, decode_hex("005071fab558c21bb23c1806278cf833f953e5f762aea6b50a25be14b8c1e321"));
    }

    #[tokio::test]
    async fn pairing_wire_bodies_never_carry_the_code_or_its_hash() {
        let identity = crate::test_tls::generated_self_signed_identity();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let (port, server) = start_pairing_server(
            identity.config,
            identity.pin,
            Arc::clone(&requests),
            Arc::new(Mutex::new(Vec::new())),
            1,
        )
        .await;
        let endpoint = Endpoint::new("127.0.0.1", port).unwrap();
        let client = client_for(Trust::Pinned { spki_sha256: identity.pin }, &endpoint).unwrap();
        let exchange = start(&client, &endpoint, "pinned", None, "K7QX").await.unwrap();
        let paired = finish_exchange(
            client,
            &endpoint,
            Trust::Pinned { spki_sha256: identity.pin },
            transcript_prefix("pinned", &identity.pin),
            exchange,
        )
        .await
        .unwrap();
        assert_eq!(paired.name, "Studio");
        assert_eq!(paired.identity, Some(identity.pin), "a fresh pairing always confirms the host identity");
        server.await.unwrap();

        let requests = requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        let password_hash = pairing_password("K7QX");
        let password_hash_base64 = BASE64.encode(password_hash);
        let password_hash_hex = password_hash.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
        for request in requests.iter() {
            assert!(!request.contains("K7QX"));
            assert!(!request.contains(&password_hash_base64));
            assert!(!request.contains(&password_hash_hex));
        }
        let start: serde_json::Value = serde_json::from_str(&requests[0]).unwrap();
        let finish: serde_json::Value = serde_json::from_str(&requests[1]).unwrap();
        assert_eq!(start["mode"], "pinned");
        assert!(start.get("msg_c").is_some());
        assert!(finish.get("exchange_id").is_some());
        assert!(finish.get("confirm_c").is_some());
    }

    #[tokio::test]
    #[ignore = "requires CORSIA_E2E_PORT and CORSIA_E2E_CODE from a disposable real-TLS host"]
    async fn disposable_tls_host_pairs_checks_version_and_proxies_health() {
        let port = std::env::var("CORSIA_E2E_PORT").unwrap().parse().unwrap();
        let main_port = std::env::var("CORSIA_E2E_MAIN_PORT").unwrap().parse().unwrap();
        let code = std::env::var("CORSIA_E2E_CODE").unwrap();
        assert_eq!(
            pair("127.0.0.1", main_port, "0000").await,
            Err(PairError::Unreachable(ConnectFailure::Server))
        );
        let paired = pair("127.0.0.1", port, &code).await.unwrap();
        assert_eq!(
            crate::version_check::check("127.0.0.1", port, &paired.trust, "0.4.1-dev.230").await.unwrap(),
            crate::version_check::Compat::Ok
        );
        let proxy = crate::proxy::start(
            crate::remote_config::Locator::Address { host: "127.0.0.1".into(), port },
            paired.trust.clone(),
            0,
            || panic!("a matching disposable host key must not trigger key_changed"),
        )
        .await
        .unwrap();
        let response = reqwest::get(format!("http://127.0.0.1:{}/api/health", proxy.port))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        proxy.stop();

        let mut wrong_trust = paired.trust;
        let crate::trust::Trust::Pinned { spki_sha256 } = &mut wrong_trust else {
            panic!("the disposable self-signed host must pair through its captured pin");
        };
        spki_sha256[0] ^= 1;
        let key_changed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let observed_key_change = std::sync::Arc::clone(&key_changed);
        let wrong_proxy = crate::proxy::start(
            crate::remote_config::Locator::Address { host: "127.0.0.1".into(), port },
            wrong_trust,
            0,
            move || observed_key_change.store(true, std::sync::atomic::Ordering::SeqCst),
        )
        .await
        .unwrap();
        let response = reqwest::get(format!("http://127.0.0.1:{}/api/health", wrong_proxy.port))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
        assert!(key_changed.load(std::sync::atomic::Ordering::SeqCst));
        wrong_proxy.stop();
    }

    #[tokio::test]
    #[ignore = "requires CORSIA_E2E_MAIN_PORT from a disposable host's plain-HTTP main listener"]
    async fn disposable_plain_http_listener_is_classified_as_server() {
        let port = std::env::var("CORSIA_E2E_MAIN_PORT").unwrap().parse().unwrap();
        assert_eq!(
            pair("127.0.0.1", port, "0000").await,
            Err(PairError::Unreachable(ConnectFailure::Server))
        );
    }

    #[tokio::test]
    #[ignore = "requires CORSIA_E2E_PORT after its disposable real-TLS host has stopped"]
    async fn stopped_disposable_tls_host_is_classified_as_server() {
        let port = std::env::var("CORSIA_E2E_PORT").unwrap().parse().unwrap();
        assert_eq!(
            pair("127.0.0.1", port, "0000").await,
            Err(PairError::Unreachable(ConnectFailure::Server))
        );
    }
}
