use std::net::{IpAddr, Ipv6Addr, SocketAddr, SocketAddrV6};

use reqwest::Url;

/// A remote host as typed by a person or resolved by discovery: a DNS name, an
/// IP literal, or an IPv6 literal carrying a zone (`fe80::1%en0`), with or
/// without URL brackets.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum RemoteHost {
    Name(String),
    Ip(IpAddr),
    /// A link-local IPv6 address, which is only reachable through its interface.
    Scoped { address: Ipv6Addr, scope_id: u32 },
}

/// Parse a typed address: a bare host keeps its historic meaning and
/// `legacy_port`; an `https://` URL names its host and port (default 443).
pub(crate) fn parse_pairing_address(input: &str, legacy_port: u16) -> Result<(String, u16), String> {
    let input = input.trim();
    if has_scheme(input, "http://") {
        return Err("remote address must use https".into());
    }
    if has_scheme(input, "https://") {
        return parse_https_address(input);
    }
    RemoteHost::parse(input)?;
    Ok((input.into(), legacy_port))
}

/// Parse an `https://host[:port]` address. It is endpoint-only: credentials,
/// paths, queries and fragments are not configuration and are never silently
/// dropped. The host comes back lowercased and without IPv6 brackets.
pub(crate) fn parse_https_address(input: &str) -> Result<(String, u16), String> {
    let input = input.trim();
    if input.ends_with(':') {
        return Err("remote HTTPS address needs a port from 1 to 65535".into());
    }
    let url = Url::parse(input).map_err(|_| "remote address is not a valid https URL".to_owned())?;
    if url.scheme() != "https" {
        return Err("remote address must use https".into());
    }
    if !url.username().is_empty() || url.password().is_some() || url.path() != "/" || url.query().is_some() || url.fragment().is_some() {
        return Err("remote HTTPS address may contain only host and optional port".into());
    }
    let host = url.host_str().ok_or_else(|| "remote HTTPS address needs a host".to_owned())?;
    let host = host.strip_prefix('[').and_then(|inner| inner.strip_suffix(']')).unwrap_or(host);
    RemoteHost::parse(host)?;
    let port = url.port().unwrap_or(443);
    if port == 0 {
        return Err("remote HTTPS address needs a port from 1 to 65535".into());
    }
    Ok((host.to_owned(), port))
}

fn has_scheme(input: &str, scheme: &str) -> bool {
    input.get(..scheme.len()).is_some_and(|prefix| prefix.eq_ignore_ascii_case(scheme))
}

impl RemoteHost {
    pub(crate) fn parse(host: &str) -> Result<Self, String> {
        let host = host.trim();
        let (inner, bracketed) = match host.strip_prefix('[').and_then(|rest| rest.strip_suffix(']')) {
            Some(inner) => (inner, true),
            None => (host, false),
        };
        if let Some((address, zone)) = inner.split_once('%') {
            let address: Ipv6Addr = address.parse().map_err(|_| "remote host is not a valid IPv6 address".to_owned())?;
            // A URL writes the zone separator as %25.
            let zone = match zone.strip_prefix("25") {
                Some(rest) if bracketed && !rest.is_empty() => rest,
                _ => zone,
            };
            let scope_id = parse_scope(zone)?;
            return Ok(if scope_id == 0 { Self::Ip(IpAddr::V6(address)) } else { Self::Scoped { address, scope_id } });
        }
        if let Ok(address) = inner.parse::<IpAddr>() {
            return Ok(Self::Ip(address));
        }
        if inner.is_empty() || bracketed || inner.contains(':') {
            return Err("remote host must be a hostname or IP address without a port".into());
        }
        Ok(Self::Name(inner.to_owned()))
    }

    /// `host:port` as it belongs in a URL or Host header: IPv6 is bracketed and
    /// never carries its zone on the wire.
    pub(crate) fn authority(&self, port: u16) -> String {
        match self {
            Self::Name(name) => format!("{name}:{port}"),
            Self::Ip(IpAddr::V4(address)) => format!("{address}:{port}"),
            Self::Ip(IpAddr::V6(address)) | Self::Scoped { address, .. } => format!("[{address}]:{port}"),
        }
    }

    /// The socket address to dial, or `None` when a name must be looked up.
    pub(crate) fn socket_addr(&self, port: u16) -> Option<SocketAddr> {
        match self {
            Self::Name(_) => None,
            Self::Ip(address) => Some(SocketAddr::new(*address, port)),
            Self::Scoped { address, scope_id } => Some(SocketAddr::V6(SocketAddrV6::new(*address, port, 0, *scope_id))),
        }
    }

    /// The TLS server name: a name, or the address without its zone.
    pub(crate) fn server_name(&self) -> Result<rustls::pki_types::ServerName<'static>, String> {
        match self {
            Self::Name(name) => rustls::pki_types::ServerName::try_from(name.clone()).map_err(|error| error.to_string()),
            Self::Ip(address) => Ok(rustls::pki_types::ServerName::IpAddress((*address).into())),
            Self::Scoped { address, .. } => Ok(rustls::pki_types::ServerName::IpAddress(IpAddr::V6(*address).into())),
        }
    }
}

fn parse_scope(zone: &str) -> Result<u32, String> {
    if let Ok(index) = zone.parse::<u32>() {
        return Ok(index);
    }
    #[cfg(unix)]
    if let Ok(name) = std::ffi::CString::new(zone) {
        let index = unsafe { libc::if_nametoindex(name.as_ptr()) };
        if index != 0 {
            return Ok(index);
        }
    }
    Err(format!("unknown network interface {zone:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ipv6_authorities_are_bracketed_and_never_carry_the_zone() {
        let scoped = RemoteHost::parse("fe80::1%4").unwrap();
        assert_eq!(scoped, RemoteHost::Scoped { address: "fe80::1".parse().unwrap(), scope_id: 4 });
        assert_eq!(scoped.authority(47901), "[fe80::1]:47901");
        assert_eq!(
            scoped.socket_addr(47901).unwrap().to_string(),
            "[fe80::1%4]:47901"
        );
        assert_eq!(RemoteHost::parse("[fe80::1%254]").unwrap(), scoped, "URL form writes the zone as %25");
        assert_eq!(RemoteHost::parse("::1").unwrap().authority(1), "[::1]:1");
        assert_eq!(RemoteHost::parse("[::1]").unwrap().authority(1), "[::1]:1");
        assert_eq!(RemoteHost::parse("studio.local").unwrap().authority(1), "studio.local:1");
        assert_eq!(RemoteHost::parse("10.0.0.2").unwrap().authority(1), "10.0.0.2:1");
        assert!(RemoteHost::parse("fe80::1%no-such-interface0").is_err());
    }

    #[test]
    fn outside_address_contract_vectors_are_accepted_or_rejected_by_the_desktop_boundary() {
        let vectors: serde_json::Value = serde_json::from_str(include_str!("../../../testdata/outside-address-vectors.json")).unwrap();
        for vector in vectors["accepted"].as_array().unwrap() {
            // The API owns normalization; the desktop converts its saved endpoint.
            let normalized = vector["normalized"].as_str().unwrap();
            let (host, port) = normalized.rsplit_once(':').map(|(host, port)| (host, port.parse::<u16>().unwrap())).unwrap_or((normalized, 443));
            assert_eq!(parse_https_address(&format!("https://{normalized}")), Ok((host.to_ascii_lowercase(), port)), "{normalized}");
        }
        for value in vectors["rejected"].as_array().unwrap() {
            let input = value.as_str().unwrap();
            let url = if input.starts_with("http://") { input.to_owned() } else { format!("https://{input}") };
            assert!(parse_https_address(&url).is_err(), "{input}");
        }
    }

    #[test]
    fn pairing_address_accepts_only_endpoint_https_urls() {
        assert_eq!(parse_pairing_address("studio.local", 47901), Ok(("studio.local".into(), 47901)));
        assert_eq!(parse_pairing_address("https://outside.example", 47901), Ok(("outside.example".into(), 443)));
        assert_eq!(parse_pairing_address("https://outside.example:47901", 47901), Ok(("outside.example".into(), 47901)));
        assert_eq!(parse_pairing_address(" HTTPS://Outside.Example/ ", 47901), Ok(("outside.example".into(), 443)));
        assert_eq!(parse_https_address("https://[2001:db8::1]:8443"), Ok(("2001:db8::1".into(), 8443)));
        for invalid in ["HTTP://outside.example", "https://outside.example#x", "https://", "outside.example:443", "ftp://outside.example"] {
            assert!(parse_pairing_address(invalid, 47901).is_err(), "{invalid}");
        }
        assert!(parse_https_address("studio.local").is_err(), "an outside route is always an https URL");
        for invalid in ["http://outside.example", "https://outside.example/path", "https://user@outside.example", "https://outside.example?x=1"] {
            assert!(parse_pairing_address(invalid, 47901).is_err(), "{invalid}");
        }
    }
}
