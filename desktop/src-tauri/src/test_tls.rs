use std::{fs, process::Command, sync::Arc};

use rustls::{pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer}, ServerConfig};
use x509_parser::prelude::{FromDer, X509Certificate};

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

/// A paired host's stable P-256 key: its self-signed listener certificate and the PKCS#8 key that signs identity proofs.
pub(crate) struct HostKey {
    pub(crate) tls: TlsIdentity,
    pub(crate) pkcs8: Vec<u8>,
    pub(crate) spki: Vec<u8>,
}

pub(crate) fn generated_p256_host() -> HostKey {
    let temporary = tempfile::tempdir().expect("create temporary certificate directory");
    let file = |name: &str| temporary.path().join(name).to_str().expect("temporary path is UTF-8").to_owned();
    run_openssl(&["req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", &file("key.pem"),
        "-out", &file("cert.pem"), "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        "-addext", "basicConstraints=CA:FALSE"]);
    let (certificate, pkcs8) = der_pair(&file("cert.pem"), &file("key.pem"), &file);
    let (_, parsed) = X509Certificate::from_der(&certificate).expect("parse generated certificate");
    let spki = parsed.tbs_certificate.subject_pki.raw.to_vec();
    HostKey { tls: identity_for(certificate, &pkcs8), pkcs8, spki }
}

/// A forwarder's certificate signed by a test CA: a system-trusted route, trusted through `ca` alone.
pub(crate) struct Forwarder {
    pub(crate) config: Arc<ServerConfig>,
    pub(crate) ca: CertificateDer<'static>,
}

pub(crate) fn generated_ca_forwarder() -> Forwarder {
    let temporary = tempfile::tempdir().expect("create temporary certificate directory");
    let file = |name: &str| temporary.path().join(name).to_str().expect("temporary path is UTF-8").to_owned();
    let p256 = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"];
    let (ca_key, ca_pem, leaf_key, leaf_csr) = (file("ca.key"), file("ca.pem"), file("leaf.key"), file("leaf.csr"));
    run_openssl(&[&["req", "-x509"][..], &p256, &["-keyout", &ca_key, "-out", &ca_pem, "-days", "1", "-subj", "/CN=CORSIA test CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign"]].concat());
    run_openssl(&[&["req"][..], &p256, &["-keyout", &leaf_key, "-out", &leaf_csr, "-subj", "/CN=127.0.0.1"]].concat());
    fs::write(file("leaf.ext"), "subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n").expect("write extensions");
    run_openssl(&["x509", "-req", "-in", &file("leaf.csr"), "-CA", &file("ca.pem"), "-CAkey", &file("ca.key"), "-CAcreateserial",
        "-out", &file("leaf.pem"), "-days", "1", "-extfile", &file("leaf.ext")]);
    let (certificate, pkcs8) = der_pair(&file("leaf.pem"), &file("leaf.key"), &file);
    run_openssl(&["x509", "-in", &file("ca.pem"), "-outform", "DER", "-out", &file("ca.der")]);
    let ca = CertificateDer::from(fs::read(file("ca.der")).expect("read test CA"));
    Forwarder { config: identity_for(certificate, &pkcs8).config, ca }
}

/// A PEM certificate and key as DER: the certificate and the PKCS#8 key.
fn der_pair(certificate_pem: &str, key_pem: &str, file: &dyn Fn(&str) -> String) -> (CertificateDer<'static>, Vec<u8>) {
    let (certificate_der, key_der) = (file("pair-cert.der"), file("pair-key.der"));
    run_openssl(&["x509", "-in", certificate_pem, "-outform", "DER", "-out", &certificate_der]);
    run_openssl(&["pkcs8", "-topk8", "-nocrypt", "-in", key_pem, "-outform", "DER", "-out", &key_der]);
    (CertificateDer::from(fs::read(certificate_der).expect("read certificate")), fs::read(key_der).expect("read key"))
}

fn identity_for(certificate: CertificateDer<'static>, pkcs8: &[u8]) -> TlsIdentity {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let pin = crate::trust::spki_sha256(&certificate).expect("generated certificate has a SPKI");
    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(pkcs8.to_vec()));
    let config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(vec![certificate.clone()], key)
        .expect("generated certificate and key are compatible");
    TlsIdentity { config: Arc::new(config), pin, certificate }
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
