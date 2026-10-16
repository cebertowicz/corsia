//! Reachability classification for the remote-connect r4 state.
//!
//! The shell owns this distinction because it has the connection-level facts
//! that the hosted UI cannot observe. Keep the mapping deliberately small:
//! unknown transport errors are network failures, while an endpoint that
//! answered negatively is a server failure.

/// The r4 recovery copy distinguishes a network path from a host that is not
/// serving CORSIA correctly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Unreachable {
    Network,
    Server,
}

/// Classify a failed TCP connection according to the approved r4 rules.
///
/// A refusal proves that a reachable host rejected the connection, so it is a
/// server failure. Timeouts and routing/address failures do not prove that a
/// CORSIA service was reached and stay network failures. Other I/O errors are
/// conservatively network failures; callers with protocol evidence use the
/// dedicated helpers below.
pub fn classify_connect_error(error: &std::io::Error) -> Unreachable {
    if error.kind() == std::io::ErrorKind::ConnectionRefused {
        Unreachable::Server
    } else {
        Unreachable::Network
    }
}

/// A TLS handshake against a plain-HTTP endpoint means the host was reached,
/// but its CORSIA devices listener is not serving TLS.
pub fn classify_tls_against_plain_http() -> Unreachable {
    Unreachable::Server
}

/// `/api/health` is part of the remote-connect endpoint contract. Any status
/// other than 200 proves that the endpoint is not a usable CORSIA host.
pub fn classify_health_status(status: u16) -> Result<(), Unreachable> {
    if status == 200 {
        Ok(())
    } else {
        Err(Unreachable::Server)
    }
}

#[cfg(test)]
mod tests {
    use super::{classify_connect_error, classify_health_status, classify_tls_against_plain_http, Unreachable};
    use std::io::{Error, ErrorKind};

    #[test]
    fn r4_classifies_every_approved_connect_error_rule() {
        let cases = [
            (Error::from(ErrorKind::TimedOut), Unreachable::Network),
            (Error::from_raw_os_error(libc::EHOSTUNREACH), Unreachable::Network),
            (Error::from_raw_os_error(libc::ENETUNREACH), Unreachable::Network),
            (Error::from(ErrorKind::AddrNotAvailable), Unreachable::Network),
            (Error::from(ErrorKind::NotConnected), Unreachable::Network),
            (Error::from(ErrorKind::ConnectionRefused), Unreachable::Server),
        ];

        for (error, expected) in cases {
            assert_eq!(classify_connect_error(&error), expected, "{error:?}");
        }
    }

    #[test]
    fn r4_classifies_tls_against_plain_http_as_server() {
        assert_eq!(classify_tls_against_plain_http(), Unreachable::Server);
    }

    #[test]
    fn r4_classifies_all_non_ok_health_responses_as_server() {
        for status in [100, 199, 201, 301, 400, 404, 500, 599] {
            assert_eq!(classify_health_status(status), Err(Unreachable::Server), "{status}");
        }
        assert_eq!(classify_health_status(200), Ok(()));
    }
}
