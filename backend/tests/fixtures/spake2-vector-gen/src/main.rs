//! Deterministic SPAKE2 interoperability vector for device pairing.
//!
//! From the repository root:
//!
//! ```text
//! cargo run --locked --quiet \
//!     --manifest-path backend/tests/fixtures/spake2-vector-gen/Cargo.toml \
//!     > backend/tests/fixtures/spake2-rust-0.4-vector.json
//! ```
//!
//! Each exchange has a Rust peer and a Python peer. The Rust peer draws its
//! scalar from `ChaCha20Rng::from_seed(rust_seed)`. The Python peer is the
//! backend's `spake2` 0.9 `SPAKE2_Symmetric`, whose `entropy_f` returns
//! `python_entropy`: the first 64 bytes of `ChaCha20Rng::from_seed(python_seed)`.
//!
//! Python reduces those 64 bytes as a big-endian integer mod L, while
//! curve25519-dalek's `Scalar::random` reduces 64 bytes little-endian mod L.
//! Serving dalek the reversed bytes gives it the Python peer's exact scalar,
//! so this generator records the message Python must reproduce. The derived
//! key is the Rust peer's `finish(python_message)`; the backend test asserts
//! that Python's own `finish(rust_message)` equals it.

use rand_chacha::ChaCha20Rng;
use rand_core::{CryptoRng, RngCore, SeedableRng};
use sha2::{Digest, Sha256};
use spake2::{Ed25519Group, Identity, Password, Spake2};

/// `core.device_pairing.PAIRING_IDENTITY`.
const IDENTITY: &[u8] = b"corsia-pair-v1";
const CODE: &str = "K7QX";
const WRONG_CODE: &str = "ZZZZ";

/// One exchange. `python_first` names the peer that starts first and whose
/// message sorts first in the symmetric transcript, so the two exchanges
/// cover both branches of that sort.
struct Exchange {
    name: &'static str,
    python_first: bool,
    python_seed: [u8; 32],
    rust_seed: [u8; 32],
}

const EXCHANGES: [Exchange; 2] = [
    Exchange {
        name: "python_first",
        python_first: true,
        python_seed: [0x99; 32],
        rust_seed: [0xaa; 32],
    },
    Exchange {
        name: "rust_first",
        python_first: false,
        python_seed: [0x11; 32],
        rust_seed: [0x22; 32],
    },
];

/// Serves the Python peer's entropy to `Scalar::random`, byte-reversed so its
/// little-endian reduction equals Python's big-endian one. Any other draw
/// would silently change the scalar, so it panics instead.
struct PythonScalarRng {
    entropy: [u8; 64],
    drawn: bool,
}

impl RngCore for PythonScalarRng {
    fn next_u32(&mut self) -> u32 {
        unreachable!("Scalar::random draws through fill_bytes")
    }

    fn next_u64(&mut self) -> u64 {
        unreachable!("Scalar::random draws through fill_bytes")
    }

    fn fill_bytes(&mut self, dest: &mut [u8]) {
        assert!(
            !self.drawn && dest.len() == self.entropy.len(),
            "Scalar::random must draw exactly 64 bytes once"
        );
        for (out, byte) in dest.iter_mut().zip(self.entropy.iter().rev()) {
            *out = *byte;
        }
        self.drawn = true;
    }

    fn try_fill_bytes(&mut self, dest: &mut [u8]) -> Result<(), rand_core::Error> {
        self.fill_bytes(dest);
        Ok(())
    }
}

// Test-vector generation only: the bytes are fixed and public.
impl CryptoRng for PythonScalarRng {}

/// `DevicePairing.issue_code`'s SPAKE2 password for a code.
fn pairing_secret(code: &str) -> Vec<u8> {
    let mut hash = Sha256::new();
    hash.update(IDENTITY);
    hash.update(b"\0");
    hash.update(code.as_bytes());
    hash.finalize().to_vec()
}

fn python_entropy(seed: [u8; 32]) -> [u8; 64] {
    let mut entropy = [0u8; 64];
    ChaCha20Rng::from_seed(seed).fill_bytes(&mut entropy);
    entropy
}

fn python_peer(code: &str, entropy: [u8; 64]) -> (Spake2<Ed25519Group>, Vec<u8>) {
    Spake2::<Ed25519Group>::start_symmetric_with_rng(
        &Password::new(pairing_secret(code)),
        &Identity::new(IDENTITY),
        PythonScalarRng { entropy, drawn: false },
    )
}

fn rust_peer(seed: [u8; 32]) -> (Spake2<Ed25519Group>, Vec<u8>) {
    Spake2::<Ed25519Group>::start_symmetric_with_rng(
        &Password::new(pairing_secret(CODE)),
        &Identity::new(IDENTITY),
        ChaCha20Rng::from_seed(seed),
    )
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn exchange_json(exchange: &Exchange) -> String {
    let entropy = python_entropy(exchange.python_seed);
    let (python_message, rust_message, python_key, rust_key) = if exchange.python_first {
        let (python, python_message) = python_peer(CODE, entropy);
        let (rust, rust_message) = rust_peer(exchange.rust_seed);
        let rust_key = rust.finish(&python_message).expect("rust finish");
        let python_key = python.finish(&rust_message).expect("python-peer finish");
        (python_message, rust_message, python_key, rust_key)
    } else {
        let (rust, rust_message) = rust_peer(exchange.rust_seed);
        let (python, python_message) = python_peer(CODE, entropy);
        let python_key = python.finish(&rust_message).expect("python-peer finish");
        let rust_key = rust.finish(&python_message).expect("rust finish");
        (python_message, rust_message, python_key, rust_key)
    };
    assert_eq!(python_key, rust_key, "{}: peers disagree", exchange.name);
    assert_eq!(
        python_message[1..] < rust_message[1..],
        exchange.python_first,
        "{}: seeds do not give the intended transcript order",
        exchange.name
    );
    format!(
        concat!(
            "  \"{}\": {{\n",
            "    \"python_seed_hex\": \"{}\",\n",
            "    \"python_entropy_hex\": \"{}\",\n",
            "    \"rust_seed_hex\": \"{}\",\n",
            "    \"python_message_hex\": \"{}\",\n",
            "    \"rust_message_hex\": \"{}\",\n",
            "    \"derived_key_hex\": \"{}\"\n",
            "  }}"
        ),
        exchange.name,
        hex(&exchange.python_seed),
        hex(&entropy),
        hex(&exchange.rust_seed),
        hex(&python_message),
        hex(&rust_message),
        hex(&rust_key),
    )
}

/// The Python peer of the first exchange with the wrong code, against the
/// same Rust peer: Python must reproduce this message, and neither side's key
/// may match the other's or the right-code key.
fn wrong_code_json() -> String {
    let exchange = &EXCHANGES[0];
    let (python, python_message) = python_peer(WRONG_CODE, python_entropy(exchange.python_seed));
    let (rust, rust_message) = rust_peer(exchange.rust_seed);
    let rust_key = rust.finish(&python_message).expect("rust finish");
    let python_key = python.finish(&rust_message).expect("python-peer finish");
    assert_ne!(python_key, rust_key, "wrong code must not agree");
    format!(
        concat!(
            "  \"wrong_code\": {{\n",
            "    \"code\": \"{}\",\n",
            "    \"exchange\": \"{}\",\n",
            "    \"python_message_hex\": \"{}\",\n",
            "    \"rust_derived_key_hex\": \"{}\"\n",
            "  }}"
        ),
        WRONG_CODE,
        exchange.name,
        hex(&python_message),
        hex(&rust_key),
    )
}

fn main() {
    let mut sections = vec![
        "  \"producer\": \"rust spake2 0.4.0 (spake2-vector-gen)\"".to_string(),
        "  \"rng\": \"rand_chacha 0.3 ChaCha20Rng::from_seed\"".to_string(),
        format!("  \"code\": \"{CODE}\""),
        format!("  \"pw_hex\": \"{}\"", hex(&pairing_secret(CODE))),
        format!("  \"identity_hex\": \"{}\"", hex(IDENTITY)),
    ];
    sections.extend(EXCHANGES.iter().map(exchange_json));
    sections.push(wrong_code_json());
    println!("{{\n{}\n}}", sections.join(",\n"));
}
