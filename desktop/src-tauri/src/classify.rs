//! The one place a connection failure is named.
//!
//! The shell owns this distinction because it has the connection-level facts
//! that the hosted UI cannot observe. Every layer (the TCP and TLS classifiers
//! below, pairing, the proxy, the launch state, the pairing response and the
//! shell page) reports and reads `ConnectFailure`, so a new cause is added here
//! and no other layer needs its own copy of the mapping.

/// Why a remote could not be used, in the words the person is shown.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnectFailure {
    /// Nothing answered: the network path is the likely fault.
    Network,
    /// The host answered, but CORSIA is not serving there.
    Server,
    /// This computer could not finish (app folder, saved config, capability grant, window, local service).
    Local,
    /// The host presented a key other than the one this computer paired with.
    KeyChanged,
    /// The host and this app run versions that cannot talk to each other. `host` is unknown for a host too old to say.
    Incompatible { app: String, host: Option<String> },
    TooOld,
}

impl ConnectFailure {
    /// The reason the pairing page switches on. Pairing never reports a key change to it: it becomes `Local` first
    /// (see `remote_pair_error`).
    pub fn reason(&self) -> &'static str {
        match self {
            Self::Network => "network",
            Self::Server => "server",
            Self::Local => "local",
            Self::KeyChanged => "key_changed",
            Self::Incompatible { .. } => "incompatible",
            Self::TooOld => "too_old",
        }
    }
}

/// Classify a failed TCP connection according to the approved r4 rules.
///
/// A refusal proves that a reachable host rejected the connection, so it is a
/// server failure. Timeouts and routing/address failures do not prove that a
/// CORSIA service was reached and stay network failures. Other I/O errors are
/// conservatively network failures; callers with protocol evidence use the
/// dedicated helpers below.
pub fn classify_connect_error(error: &std::io::Error) -> ConnectFailure {
    if error.kind() == std::io::ErrorKind::ConnectionRefused {
        ConnectFailure::Server
    } else {
        ConnectFailure::Network
    }
}

/// A TLS handshake against a plain-HTTP endpoint means the host was reached,
/// but its CORSIA devices listener is not serving TLS.
pub fn classify_tls_against_plain_http() -> ConnectFailure {
    ConnectFailure::Server
}

#[cfg(test)]
mod tests {
    use super::{classify_connect_error, classify_tls_against_plain_http, ConnectFailure};
    use std::io::{Error, ErrorKind};

    #[test]
    fn r4_classifies_every_approved_connect_error_rule() {
        let cases = [
            (Error::from(ErrorKind::TimedOut), ConnectFailure::Network),
            (Error::from_raw_os_error(libc::EHOSTUNREACH), ConnectFailure::Network),
            (Error::from_raw_os_error(libc::ENETUNREACH), ConnectFailure::Network),
            (Error::from(ErrorKind::AddrNotAvailable), ConnectFailure::Network),
            (Error::from(ErrorKind::NotConnected), ConnectFailure::Network),
            (Error::from(ErrorKind::ConnectionRefused), ConnectFailure::Server),
        ];

        for (error, expected) in cases {
            assert_eq!(classify_connect_error(&error), expected, "{error:?}");
        }
    }

    #[test]
    fn r4_classifies_tls_against_plain_http_as_server() {
        assert_eq!(classify_tls_against_plain_http(), ConnectFailure::Server);
    }

    #[test]
    fn every_failure_has_a_distinct_reason() {
        let all = [
            ConnectFailure::Network,
            ConnectFailure::Server,
            ConnectFailure::Local,
            ConnectFailure::KeyChanged,
            ConnectFailure::Incompatible { app: "0.4.1".into(), host: None },
            ConnectFailure::TooOld,
        ];
        let reasons: std::collections::BTreeSet<_> = all.iter().map(ConnectFailure::reason).collect();
        assert_eq!(reasons.len(), all.len());
    }
}
