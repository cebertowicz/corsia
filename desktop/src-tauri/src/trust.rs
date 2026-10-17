use std::sync::{Arc, Mutex};

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, Error, SignatureScheme};
use sha2::{Digest, Sha256};
use x509_parser::prelude::{FromDer, X509Certificate};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Trust {
    Pinned { spki_sha256: [u8; 32] },
    System,
}

/// The remote presented a key other than the saved pin. It rides inside the TLS handshake error so a caller can
/// tell a changed key from a network failure without reading error text; see `is_pin_mismatch`.
#[derive(Debug)]
pub struct PinMismatch;

impl std::fmt::Display for PinMismatch {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("remote certificate SPKI does not match the saved pin")
    }
}

impl std::error::Error for PinMismatch {}

/// Whether this error, or anything in its source chain, is a pin mismatch. rustls errors have no source, and
/// tokio-rustls wraps them in an `io::Error` whose payload is not its source, so both are looked into.
pub fn is_pin_mismatch(error: &(dyn std::error::Error + 'static)) -> bool {
    let mut current = Some(error);
    while let Some(error) = current {
        if error.is::<PinMismatch>() {
            return true;
        }
        if let Some(Error::InvalidCertificate(rustls::CertificateError::Other(other))) = error.downcast_ref::<Error>() {
            if other.0.is::<PinMismatch>() {
                return true;
            }
        }
        if let Some(inner) = error.downcast_ref::<std::io::Error>().and_then(std::io::Error::get_ref) {
            if is_pin_mismatch(inner) {
                return true;
            }
        }
        current = error.source();
    }
    false
}

#[derive(Clone, Debug)]
pub struct PinnedVerifier {
    expected: [u8; 32],
    signature_algorithms: rustls::crypto::WebPkiSupportedAlgorithms,
}

impl PinnedVerifier {
    pub fn new(expected: [u8; 32]) -> Result<Self, Error> {
        Ok(Self {
            expected,
            signature_algorithms: rustls::crypto::ring::default_provider().signature_verification_algorithms,
        })
    }
}

impl ServerCertVerifier for PinnedVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, Error> {
        let actual = spki_sha256(end_entity)?;
        if actual != self.expected {
            return Err(Error::InvalidCertificate(rustls::CertificateError::Other(rustls::OtherError(Arc::new(PinMismatch)))));
        }
        // A pin intentionally survives the self-signed certificate's validity period.
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.signature_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.signature_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.signature_algorithms.supported_schemes()
    }
}

pub fn spki_sha256(certificate: &CertificateDer<'_>) -> Result<[u8; 32], Error> {
    let (_, parsed) = X509Certificate::from_der(certificate.as_ref())
        .map_err(|error| Error::General(format!("remote certificate is not DER X.509: {error}")))?;
    let digest = Sha256::digest(parsed.tbs_certificate.subject_pki.raw);
    Ok(digest.into())
}

fn install_crypto_provider() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}

pub fn pinned_client_config(pin: [u8; 32]) -> Result<ClientConfig, Error> {
    install_crypto_provider();
    Ok(ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(PinnedVerifier::new(pin)?))
        .with_no_client_auth())
}

/// Captures one peer SPKI while continuing to verify the TLS handshake
/// signature. It is safe only for the first PAKE request: the transcript binds
/// this exact observed key, so a relay's key cannot complete pairing.
#[derive(Clone, Debug)]
pub struct CaptureVerifier {
    observed_spki: Arc<Mutex<Option<[u8; 32]>>>,
    signature_algorithms: rustls::crypto::WebPkiSupportedAlgorithms,
}

impl CaptureVerifier {
    pub fn new() -> Result<Self, Error> {
        Ok(Self {
            observed_spki: Arc::new(Mutex::new(None)),
            signature_algorithms: rustls::crypto::ring::default_provider().signature_verification_algorithms,
        })
    }

    pub fn observed_spki(&self) -> Result<[u8; 32], Error> {
        self.observed_spki
            .lock()
            .map_err(|_| Error::General("peer certificate capture lock poisoned".into()))?
            .ok_or_else(|| Error::General("TLS completed without a peer certificate".into()))
    }
}

impl ServerCertVerifier for CaptureVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, Error> {
        let pin = spki_sha256(end_entity)?;
        *self
            .observed_spki
            .lock()
            .map_err(|_| Error::General("peer certificate capture lock poisoned".into()))? = Some(pin);
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.signature_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.signature_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.signature_algorithms.supported_schemes()
    }
}

pub fn capture_client_config(verifier: CaptureVerifier) -> ClientConfig {
    install_crypto_provider();
    ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(verifier))
        .with_no_client_auth()
}

pub fn system_client_config() -> Result<ClientConfig, Error> {
    use rustls_platform_verifier::ConfigVerifierExt;

    install_crypto_provider();
    ClientConfig::with_platform_verifier()
}


pub fn client_config(trust: &Trust) -> Result<ClientConfig, Error> {
    match trust {
        Trust::Pinned { spki_sha256 } => pinned_client_config(*spki_sha256),
        Trust::System => system_client_config(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::time::Duration;

    fn verify_at(verifier: &PinnedVerifier, certificate: &CertificateDer<'_>, seconds: u64) -> Result<ServerCertVerified, Error> {
        let name = ServerName::try_from("127.0.0.1").expect("literal address is a server name");
        verifier.verify_server_cert(certificate, &[], &name, &[], UnixTime::since_unix_epoch(Duration::from_secs(seconds)))
    }

    #[test]
    fn malformed_certificate_cannot_be_pinned() {
        let certificate = CertificateDer::from(vec![1, 2, 3]);
        assert!(spki_sha256(&certificate).is_err());
    }

    #[test]
    fn pinned_verifier_accepts_the_pinned_key_before_and_after_the_certificate_dates() {
        let host = crate::test_tls::generated_self_signed_identity();
        let verifier = PinnedVerifier::new(host.pin).expect("verifier builds");
        // The generated certificate is valid for one day from now: 1_000_000 s is
        // before it was issued and 4_000_000_000 s is long after it expired.
        assert!(verify_at(&verifier, &host.certificate, 1_000_000).is_ok());
        assert!(verify_at(&verifier, &host.certificate, 4_000_000_000).is_ok());
    }

    #[test]
    fn pinned_verifier_refuses_a_relay_presenting_another_certificate() {
        let host = crate::test_tls::generated_self_signed_identity();
        let relay = crate::test_tls::generated_self_signed_identity();
        assert_ne!(host.pin, relay.pin, "two generated identities must have different keys");
        let verifier = PinnedVerifier::new(host.pin).expect("verifier builds");
        let refusal = verify_at(&verifier, &relay.certificate, 1_000_000).unwrap_err();
        assert!(is_pin_mismatch(&refusal), "{refusal}");
        assert!(!is_pin_mismatch(&Error::General("remote certificate SPKI does not match the saved pin".into())), "only the typed signal counts, never text");
        let wrapped = std::io::Error::new(std::io::ErrorKind::InvalidData, refusal);
        assert!(is_pin_mismatch(&wrapped), "tokio-rustls carries the refusal inside an io::Error");
    }
}
