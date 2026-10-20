//! Which route carries the proxy's next connection, and the proof that the
//! route reaches the paired host. A record with no outside address keeps the
//! single saved route and trust exactly as before. With one, local is tried
//! first within `LOCAL_BUDGET`, outside otherwise, and either route carries a
//! request only once it has proven the pairing's identity: a pinned route by
//! its TLS handshake, a system-trusted route by the host's signature. A route
//! that answers with another key ends forwarding (`key_changed`); it is never
//! a reason to try the other route.

use std::{
    future::Future,
    pin::Pin,
    sync::{atomic::{AtomicBool, Ordering}, Arc, Mutex},
    time::{Duration, Instant},
};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use http_body_util::{BodyExt, Full, LengthLimitError, Limited};
use hyper::{body::Bytes, client::conn::http1::SendRequest, header::{CONTENT_TYPE, HOST}, Request};
use hyper_util::rt::TokioIo;
use ring::{rand::{SecureRandom, SystemRandom}, signature::{UnparsedPublicKey, ECDSA_P256_SHA256_ASN1}};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;
use x509_parser::prelude::{FromDer, SubjectPublicKeyInfo};

use crate::{proxy::ProxyBody, remote_config::{OutsideRoute, RemoteRecord}, remote_host::RemoteHost, trust::{self, Trust}};

pub(crate) const KEY_CHANGED_MESSAGE: &str = "the host's key changed, so nothing is forwarded to it";
const IDENTITY_PATH: &str = "/api/devices/identity";
const IDENTITY_CONTEXT: &[u8] = b"corsia-identity-v1\0";
const MAX_PROOF_BYTES: usize = 4096;

/// A TCP connection to the host and port it reached.
pub(crate) struct Dialed {
    pub(crate) host: RemoteHost,
    pub(crate) port: u16,
    pub(crate) stream: TcpStream,
}

pub(crate) type LocalDial = Arc<dyn Fn() -> Pin<Box<dyn Future<Output = Result<Dialed, String>> + Send>> + Send + Sync>;
pub(crate) type TlsConfig = Arc<dyn Fn(&Trust) -> Result<rustls::ClientConfig, rustls::Error> + Send + Sync>;

/// A connection that has proven the paired host, ready for a forwarded request.
pub(crate) struct Upstream {
    pub(crate) sender: SendRequest<ProxyBody>,
    pub(crate) authority: String,
}

#[derive(Debug)]
pub(crate) enum RouteError {
    /// The route answered with a key other than the paired one; nothing is forwarded from now on.
    KeyChanged(String),
    Unreachable(String),
}

impl RouteError {
    pub(crate) fn into_message(self) -> String {
        match self {
            Self::KeyChanged(message) | Self::Unreachable(message) => message,
        }
    }
}

/// How a proxy reaches its paired host.
pub(crate) struct RouteConfig {
    pub(crate) trust: Trust,
    pub(crate) identity: Option<[u8; 32]>,
    pub(crate) outside: Option<OutsideRoute>,
    pub(crate) on_key_changed: Arc<dyn Fn() + Send + Sync>,
    pub(crate) tls: TlsConfig,
    pub(crate) reprobe: Duration,
}

impl RouteConfig {
    /// The saved trust alone, as every record had before outside addresses.
    pub(crate) fn single(trust: Trust, on_key_changed: impl Fn() + Send + Sync + 'static) -> Self {
        Self {
            trust,
            identity: None,
            outside: None,
            on_key_changed: Arc::new(on_key_changed),
            tls: Arc::new(trust::client_config),
            reprobe: RouteSelector::LOCAL_REPROBE,
        }
    }

    pub(crate) fn from_record(record: &RemoteRecord, on_key_changed: impl Fn() + Send + Sync + 'static) -> Result<Self, String> {
        let identity = record.identity.as_deref().map(decode_identity).transpose()?;
        Ok(Self {
            identity,
            outside: record.outside.clone(),
            ..Self::single(Trust::try_from(&record.trust)?, on_key_changed)
        })
    }
}

fn decode_identity(encoded: &str) -> Result<[u8; 32], String> {
    BASE64
        .decode(encoded)
        .map_err(|_| "saved remote identity is not valid base64".to_owned())?
        .try_into()
        .map_err(|_| "saved remote identity must contain exactly 32 bytes".to_owned())
}

struct Outside {
    host: String,
    port: u16,
    trust: Trust,
}

/// The one owner of opening a proven upstream connection for the proxy.
pub(crate) struct Routes {
    local: LocalDial,
    local_trust: Trust,
    /// With an outside route, the identity both routes must prove. `None` only for a single route.
    paired: Option<([u8; 32], Outside)>,
    tls: TlsConfig,
    reprobe: Duration,
    selector: Mutex<RouteSelector>,
    key_changed: AtomicBool,
    on_key_changed: Arc<dyn Fn() + Send + Sync>,
}

impl Routes {
    pub(crate) fn new(config: RouteConfig, local: LocalDial) -> Result<Self, String> {
        let paired = match (config.outside, config.identity) {
            (None, _) => None,
            (Some(_), None) => return Err("an outside route needs the paired host's identity".into()),
            (Some(outside), Some(identity)) => {
                let (host, port) = crate::remote_host::parse_https_address(&outside.url)?;
                Some((identity, Outside { host, port, trust: Trust::try_from(&outside.trust)? }))
            }
        };
        Ok(Self {
            local,
            local_trust: config.trust,
            paired,
            tls: config.tls,
            reprobe: config.reprobe,
            selector: Mutex::new(RouteSelector::new()),
            key_changed: AtomicBool::new(false),
            on_key_changed: config.on_key_changed,
        })
    }

    pub(crate) fn key_changed(&self) -> bool {
        self.key_changed.load(Ordering::SeqCst)
    }

    /// A proven connection for one forwarded request. Once a route has refused the identity, every call refuses.
    pub(crate) async fn connect(self: &Arc<Self>) -> Result<Upstream, RouteError> {
        if self.key_changed() {
            return Err(RouteError::KeyChanged(KEY_CHANGED_MESSAGE.into()));
        }
        let connected = match &self.paired {
            // Today's single route: no budget and no identity proof beyond its saved trust.
            None => match (self.local)().await {
                Ok(dialed) => self.open(&self.local_trust, dialed).await,
                Err(error) => Err(RouteError::Unreachable(error)),
            },
            Some((identity, outside)) => self.connect_paired(identity, outside).await,
        };
        if matches!(connected, Err(RouteError::KeyChanged(_))) {
            self.refuse();
        }
        connected
    }

    async fn connect_paired(self: &Arc<Self>, identity: &[u8; 32], outside: &Outside) -> Result<Upstream, RouteError> {
        if self.selector().current() == Route::Local {
            match tokio::time::timeout(RouteSelector::LOCAL_BUDGET, self.prove_local(identity)).await {
                Ok(Err(RouteError::Unreachable(_))) | Err(_) => self.selector().local_failed(Instant::now()),
                Ok(proven) => return proven,
            }
        } else {
            self.probe_local();
        }
        let dialed = dial(&outside.host, outside.port).await.map_err(RouteError::Unreachable)?;
        self.prove(Route::Outside, &outside.trust, identity, dialed).await
    }

    async fn prove_local(&self, identity: &[u8; 32]) -> Result<Upstream, RouteError> {
        let dialed = (self.local)().await.map_err(RouteError::Unreachable)?;
        self.prove(Route::Local, &self.local_trust, identity, dialed).await
    }

    /// While on outside, try local in the background at most once per `reprobe`; no request waits for it. Local
    /// carries requests again only after it has proven the identity.
    fn probe_local(self: &Arc<Self>) {
        if !self.selector().should_probe_local(Instant::now(), self.reprobe) {
            return;
        }
        let routes = Arc::clone(self);
        tokio::spawn(async move {
            let Some((identity, _)) = &routes.paired else { return };
            match tokio::time::timeout(RouteSelector::LOCAL_BUDGET, routes.prove_local(identity)).await {
                Ok(Ok(_proven)) => routes.selector().local_proven(),
                Ok(Err(RouteError::KeyChanged(_))) => routes.refuse(),
                Ok(Err(RouteError::Unreachable(_))) | Err(_) => {}
            }
        });
    }

    async fn prove(&self, route: Route, trust: &Trust, identity: &[u8; 32], dialed: Dialed) -> Result<Upstream, RouteError> {
        if matches!(trust, Trust::Pinned { spki_sha256 } if spki_sha256 != identity) {
            return Err(RouteError::KeyChanged("the route's pin is not the paired host's identity".into()));
        }
        let mut upstream = self.open(trust, dialed).await?;
        if *trust == Trust::System && !self.selector().is_proven(route) {
            prove_identity(&mut upstream, identity).await?;
            self.selector().proven(route);
        }
        Ok(upstream)
    }

    async fn open(&self, trust: &Trust, dialed: Dialed) -> Result<Upstream, RouteError> {
        let config = (self.tls)(trust).map_err(|error| RouteError::Unreachable(error.to_string()))?;
        let name = dialed.host.server_name().map_err(RouteError::Unreachable)?;
        let tls = TlsConnector::from(Arc::new(config)).connect(name, dialed.stream).await.map_err(|error| {
            if matches!(trust, Trust::Pinned { .. }) && trust::is_pin_mismatch(&error) {
                RouteError::KeyChanged(error.to_string())
            } else {
                RouteError::Unreachable(error.to_string())
            }
        })?;
        let (sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(tls))
            .await
            .map_err(|error| RouteError::Unreachable(error.to_string()))?;
        tokio::spawn(async move { let _ = connection.with_upgrades().await; });
        Ok(Upstream { sender, authority: dialed.host.authority(dialed.port) })
    }

    /// Fail closed: the first refusal ends forwarding, and the owner hears of it once.
    fn refuse(&self) {
        if !self.key_changed.swap(true, Ordering::SeqCst) {
            (self.on_key_changed)();
        }
    }

    fn selector(&self) -> std::sync::MutexGuard<'_, RouteSelector> {
        self.selector.lock().expect("route selector mutex is not poisoned")
    }
}

/// Open a TCP connection to `host`, a name or an address with an optional IPv6 zone.
pub(crate) async fn dial(host: &str, port: u16) -> Result<Dialed, String> {
    let remote = RemoteHost::parse(host)?;
    let stream = match remote.socket_addr(port) {
        Some(address) => TcpStream::connect(address).await,
        None => TcpStream::connect((host, port)).await,
    }
    .map_err(|error| error.to_string())?;
    Ok(Dialed { host: remote, port, stream })
}

#[derive(Deserialize)]
struct IdentityProof {
    spki: String,
    signature: String,
}

/// Prove a system-trusted route reaches the paired host: the host signs a fresh nonce and the authority this app
/// dialed with its stable key. Anything but a valid proof is a refusal; only a transport failure is not.
async fn prove_identity(upstream: &mut Upstream, identity: &[u8; 32]) -> Result<(), RouteError> {
    let authority = upstream.authority.to_ascii_lowercase();
    let mut nonce = [0_u8; 32];
    SystemRandom::new()
        .fill(&mut nonce)
        .map_err(|_| RouteError::Unreachable("could not draw an identity nonce".into()))?;
    let body = serde_json::json!({ "nonce": BASE64.encode(nonce), "authority": authority }).to_string();
    let request = Request::post(IDENTITY_PATH)
        .header(HOST, upstream.authority.as_str())
        .header(CONTENT_TYPE, "application/json")
        .body(Full::new(Bytes::from(body)).map_err(|never| match never {}).boxed())
        .map_err(|error| RouteError::Unreachable(error.to_string()))?;
    let response = upstream
        .sender
        .send_request(request)
        .await
        .map_err(|error| RouteError::Unreachable(error.to_string()))?;
    let refused = |reason: &str| RouteError::KeyChanged(format!("the route did not prove the paired host: {reason}"));
    if !response.status().is_success() {
        return Err(refused(&format!("identity answered HTTP {}", response.status())));
    }
    let bytes = match Limited::new(response.into_body(), MAX_PROOF_BYTES).collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(error) if error.is::<LengthLimitError>() => return Err(refused("the identity answer is too large")),
        Err(error) => return Err(RouteError::Unreachable(error.to_string())),
    };
    let proof: IdentityProof = serde_json::from_slice(&bytes).map_err(|_| refused("malformed identity answer"))?;
    let spki = BASE64.decode(proof.spki).map_err(|_| refused("the key is not base64"))?;
    let signature = BASE64.decode(proof.signature).map_err(|_| refused("the signature is not base64"))?;
    verify_identity_proof(identity, &nonce, &authority, &spki, &signature).map_err(refused)
}

fn verify_identity_proof(identity: &[u8; 32], nonce: &[u8; 32], authority: &str, spki: &[u8], signature: &[u8]) -> Result<(), &'static str> {
    if Sha256::digest(spki).as_slice() != identity {
        return Err("the key is not the paired host's");
    }
    let (rest, key) = SubjectPublicKeyInfo::from_der(spki).map_err(|_| "the key is not a public key")?;
    if !rest.is_empty() {
        return Err("the key has trailing bytes");
    }
    let message = [IDENTITY_CONTEXT, nonce.as_slice(), b"\0", authority.as_bytes()].concat();
    UnparsedPublicKey::new(&ECDSA_P256_SHA256_ASN1, key.subject_public_key.data.as_ref())
        .verify(&message, signature)
        .map_err(|_| "the signature does not match this nonce and address")
}

/// The route a connection uses, proven before it carries a request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Route {
    Local,
    Outside,
}

/// Route transition policy for a paired local route and its outside fallback. Networking and proof stay with
/// `Routes`; a refused proof never reaches this policy, so it can never become a retry.
pub(crate) struct RouteSelector {
    current: Route,
    /// Whether `current` has proven the identity since the proxy switched to it.
    proven: bool,
    last_local_probe: Option<Instant>,
}

impl RouteSelector {
    pub(crate) const LOCAL_BUDGET: Duration = Duration::from_secs(2);
    pub(crate) const LOCAL_REPROBE: Duration = Duration::from_secs(30);

    pub(crate) fn new() -> Self {
        Self { current: Route::Local, proven: false, last_local_probe: None }
    }

    pub(crate) fn current(&self) -> Route {
        self.current
    }

    fn is_proven(&self, route: Route) -> bool {
        self.current == route && self.proven
    }

    fn proven(&mut self, route: Route) {
        if self.current == route {
            self.proven = true;
        }
    }

    /// Local did not connect in time: switch to outside, which proves itself afresh, and wait a full interval before
    /// probing local again.
    pub(crate) fn local_failed(&mut self, now: Instant) {
        self.current = Route::Outside;
        self.proven = false;
        self.last_local_probe = Some(now);
    }

    /// Whether a background probe of local is due; at most one per `interval`, and only while on outside.
    pub(crate) fn should_probe_local(&mut self, now: Instant, interval: Duration) -> bool {
        if self.current != Route::Outside || self.last_local_probe.is_some_and(|last| now.duration_since(last) < interval) {
            return false;
        }
        self.last_local_probe = Some(now);
        true
    }

    /// A probe connected to local and proved the identity.
    pub(crate) fn local_proven(&mut self) {
        self.current = Route::Local;
        self.proven = true;
    }
}

#[cfg(test)]
#[path = "routes_tests.rs"]
mod tests;
