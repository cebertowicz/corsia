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
            return Err(Error::General("remote certificate SPKI does not match the saved pin".into()));
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

pub fn pinned_client_config(pin: [u8; 32]) -> Result<ClientConfig, Error> {
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
    ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(verifier))
        .with_no_client_auth()
}

pub fn system_client_config() -> Result<ClientConfig, Error> {
    use rustls_platform_verifier::ConfigVerifierExt;

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

    #[test]
    fn malformed_certificate_cannot_be_pinned() {
        let certificate = CertificateDer::from(vec![1, 2, 3]);
        assert!(spki_sha256(&certificate).is_err());
    }
}
