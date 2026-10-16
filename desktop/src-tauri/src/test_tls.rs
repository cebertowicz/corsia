use std::{fs, process::Command, sync::Arc};

use rustls::{pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer}, ServerConfig};

pub(crate) struct TlsIdentity {
    pub(crate) config: Arc<ServerConfig>,
    pub(crate) pin: [u8; 32],
    pub(crate) certificate: CertificateDer<'static>,
}

pub(crate) fn generated_self_signed_identity() -> TlsIdentity {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let temporary = tempfile::tempdir().expect("create temporary certificate directory");
    let key_pem = temporary.path().join("key.pem");
    let certificate_pem = temporary.path().join("certificate.pem");
    let certificate_der = temporary.path().join("certificate.der");
    let key_der = temporary.path().join("key.der");

    run_openssl(&[
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        key_pem.to_str().expect("temporary path is UTF-8"),
        "-out",
        certificate_pem.to_str().expect("temporary path is UTF-8"),
        "-days",
        "1",
        "-subj",
        "/CN=127.0.0.1",
        "-addext",
        "subjectAltName=IP:127.0.0.1",
    ]);
    run_openssl(&[
        "x509",
        "-in",
        certificate_pem.to_str().expect("temporary path is UTF-8"),
        "-outform",
        "DER",
        "-out",
        certificate_der.to_str().expect("temporary path is UTF-8"),
    ]);
    run_openssl(&[
        "pkcs8",
        "-topk8",
        "-nocrypt",
        "-in",
        key_pem.to_str().expect("temporary path is UTF-8"),
        "-outform",
        "DER",
        "-out",
        key_der.to_str().expect("temporary path is UTF-8"),
    ]);

    let certificate = CertificateDer::from(fs::read(certificate_der).expect("read generated certificate"));
    let pin = crate::trust::spki_sha256(&certificate).expect("generated certificate has a SPKI");
    let private_key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(
        fs::read(key_der).expect("read generated private key"),
    ));
    let config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![certificate.clone()], private_key)
        .expect("generated certificate and key are compatible");

    TlsIdentity {
        config: Arc::new(config),
        pin,
        certificate,
    }
}

fn run_openssl(arguments: &[&str]) {
    let output = Command::new("openssl")
        .args(arguments)
        .output()
        .expect("openssl must be available for generated TLS test certificates");
    assert!(
        output.status.success(),
        "openssl {:?} failed: {}",
        arguments,
        String::from_utf8_lossy(&output.stderr)
    );
}
