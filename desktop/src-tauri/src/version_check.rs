use std::time::Duration;

use reqwest::{Client, StatusCode};
use semver::Version;
use serde::Deserialize;

use crate::pairing::PairError;
use crate::trust::{self, Trust};

pub const SHELL_PROTOCOL: u32 = 1;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Compat {
    Ok,
    Incompatible { app: String, host: Option<String> },
    TooOld,
}

#[derive(Deserialize)]
struct VersionResponse {
    app: Option<HostApp>,
}

#[derive(Deserialize)]
struct HostApp {
    version: Option<String>,
    shell_protocol: Option<u32>,
}

pub async fn check(host: &str, port: u16, trust: &Trust) -> Result<Compat, PairError> {
    let config = trust::client_config(trust).map_err(|error| PairError::Protocol(error.to_string()))?;
    let client = Client::builder()
        .use_preconfigured_tls(config)
        .https_only(true)
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| PairError::Protocol(error.to_string()))?;
    let response = client
        .get(format!("https://{host}:{port}/api/version"))
        .send()
        .await
        .map_err(PairError::from)?;
    if response.status() == StatusCode::NOT_FOUND {
        return Ok(Compat::TooOld);
    }
    if !response.status().is_success() {
        return Err(PairError::Protocol(format!(
            "version check failed with HTTP {}",
            response.status()
        )));
    }
    let payload: VersionResponse = response
        .json()
        .await
        .map_err(|error| PairError::Protocol(format!("invalid version response: {error}")))?;
    let Some(app) = payload.app else {
        return Ok(Compat::TooOld);
    };
    let Some(protocol) = app.shell_protocol else {
        return Ok(Compat::TooOld);
    };
    if protocol == SHELL_PROTOCOL {
        return Ok(Compat::Ok);
    }
    Ok(Compat::Incompatible {
        app: env!("CARGO_PKG_VERSION").to_owned(),
        host: app.version,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_protocol_matches_the_backend_source_constant() {
        let backend = include_str!("../../../backend/api/version.py");
        assert!(backend.contains(&format!("\"shell_protocol\": {SHELL_PROTOCOL}")));
    }

    #[test]
    fn semver_can_order_the_versions_displayed_on_incompatibility() {
        assert!(Version::parse("1.2.0").unwrap() > Version::parse("1.1.9").unwrap());
    }
}
