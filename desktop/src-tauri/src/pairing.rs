use std::{error::Error, time::Duration};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use hmac::{Hmac, Mac};
use hkdf::Hkdf;
use reqwest::{Client, StatusCode};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use spake2::{Ed25519Group, Identity, Password, Spake2};

use crate::{
    classify::{classify_connect_error, classify_tls_against_plain_http, Unreachable},
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
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PairError {
    WrongCode,
    Expired,
    TooManyTries,
    Paused,
    Busy,
    Unreachable(Unreachable),
    TooOld,
    Protocol(String),
}

impl From<reqwest::Error> for PairError {
    fn from(error: reqwest::Error) -> Self {
        if error.is_timeout() {
            return Self::Unreachable(Unreachable::Network);
        }
        if error.is_connect() {
            let mut source = error.source();
            while let Some(cause) = source {
                if let Some(io) = cause.downcast_ref::<std::io::Error>() {
                    return Self::Unreachable(classify_connect_error(io));
                }
                source = cause.source();
            }
            let message = error.to_string().to_ascii_lowercase();
            if message.contains("plain http") || message.contains("wrong version") {
                return Self::Unreachable(classify_tls_against_plain_http());
            }
            return Self::Unreachable(Unreachable::Network);
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

pub async fn pair(host: &str, port: u16, code: &str) -> Result<Paired, PairError> {
    if code.len() != 4 {
        return Err(PairError::WrongCode);
    }
    let canonical_host = canonical_verified_host(host)?;

    // Prefer the platform verifier. A certificate that is not system-trusted
    // gets one capture attempt: PAKE then binds the captured SPKI before the
    // app records it as the pin. A network failure still fails when capture
    // attempts the same endpoint.
    let system_client = client_for(Trust::System)?;
    match start(&system_client, host, port, "verified", Some(&canonical_host), code).await {
        Ok(exchange) => {
            finish_exchange(
                system_client,
                host,
                port,
                Trust::System,
                transcript_prefix("verified", canonical_host.as_bytes()),
                exchange,
            )
            .await
        }
        Err(PairError::Unreachable(_)) => pair_pinned(host, port, code).await,
        Err(error) => Err(error),
    }
}

async fn pair_pinned(host: &str, port: u16, code: &str) -> Result<Paired, PairError> {
    let capture = trust::CaptureVerifier::new().map_err(|error| PairError::Protocol(error.to_string()))?;
    let client = Client::builder()
        .use_preconfigured_tls(trust::capture_client_config(capture.clone()))
        .https_only(true)
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| PairError::Protocol(error.to_string()))?;
    let exchange = start(&client, host, port, "pinned", None, code).await?;
    let pin = capture
        .observed_spki()
        .map_err(|error| PairError::Protocol(error.to_string()))?;
    finish_exchange(
        client_for(Trust::Pinned { spki_sha256: pin })?,
        host,
        port,
        Trust::Pinned { spki_sha256: pin },
        transcript_prefix("pinned", &pin),
        exchange,
    )
    .await
}

fn client_for(trust: Trust) -> Result<Client, PairError> {
    let tls = trust::client_config(&trust).map_err(|error| PairError::Protocol(error.to_string()))?;
    Client::builder()
        .use_preconfigured_tls(tls)
        .https_only(true)
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| PairError::Protocol(error.to_string()))
}

async fn start(
    client: &Client,
    host: &str,
    port: u16,
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
        .post(endpoint(host, port, "/api/devices/pair/start"))
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
    host: &str,
    port: u16,
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
        .post(endpoint(host, port, "/api/devices/pair/finish"))
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
    Ok(Paired {
        name: finish.name,
        trust,
        host: host.to_owned(),
        port,
    })
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

fn canonical_verified_host(host: &str) -> Result<String, PairError> {
    let canonical = host.trim_end_matches('.').to_ascii_lowercase();
    if canonical.is_empty() || canonical.contains(':') {
        return Err(PairError::Protocol("verified pairing requires a hostname without a port".into()));
    }
    Ok(canonical)
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

fn endpoint(host: &str, port: u16, path: &str) -> String {
    format!("https://{host}:{port}{path}")
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
    use super::*;
    use rand_chacha::{rand_core::SeedableRng, ChaCha20Rng};

    fn decode_hex(value: &str) -> Vec<u8> {
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).unwrap())
            .collect()
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
        assert_eq!(canonical_verified_host("Studio.LOCAL.").unwrap(), "studio.local");
    }

    #[test]
    fn connect_failures_keep_the_r4_cause() {
        assert_eq!(
            PairError::Unreachable(classify_connect_error(&std::io::Error::from(std::io::ErrorKind::TimedOut))),
            PairError::Unreachable(Unreachable::Network)
        );
        assert_eq!(
            PairError::Unreachable(classify_connect_error(&std::io::Error::from(std::io::ErrorKind::ConnectionRefused))),
            PairError::Unreachable(Unreachable::Server)
        );
        assert_eq!(
            PairError::Unreachable(classify_tls_against_plain_http()),
            PairError::Unreachable(Unreachable::Server)
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
            confirmation(&server_key, &transcript),
            decode_hex("944a40f6df11c32fd42a3a2652610d84113da85571c5d6f62a9e8867423be7e0")
        );
    }

    #[tokio::test]
    #[ignore = "requires CORSIA_E2E_PORT and CORSIA_E2E_CODE from a disposable real-TLS host"]
    async fn disposable_tls_host_pairs_checks_version_and_proxies_health() {
        let port = std::env::var("CORSIA_E2E_PORT").unwrap().parse().unwrap();
        let main_port = std::env::var("CORSIA_E2E_MAIN_PORT").unwrap().parse().unwrap();
        let code = std::env::var("CORSIA_E2E_CODE").unwrap();
        assert_eq!(
            pair("127.0.0.1", main_port, "0000").await,
            Err(PairError::Unreachable(Unreachable::Server))
        );
        let paired = pair("127.0.0.1", port, &code).await.unwrap();
        assert_eq!(
            crate::version_check::check("127.0.0.1", port, &paired.trust).await.unwrap(),
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
    #[ignore = "requires CORSIA_E2E_PORT after its disposable real-TLS host has stopped"]
    async fn stopped_disposable_tls_host_is_classified_as_server() {
        let port = std::env::var("CORSIA_E2E_PORT").unwrap().parse().unwrap();
        assert_eq!(
            pair("127.0.0.1", port, "0000").await,
            Err(PairError::Unreachable(Unreachable::Server))
        );
    }
}
