use std::time::Duration;

#[derive(Clone, Debug)]
pub enum DiscoveryError {
    Unavailable,
}

pub async fn resolve(_instance: &str, _timeout: Duration) -> Result<(String, u16), DiscoveryError> {
    Err(DiscoveryError::Unavailable)
}
