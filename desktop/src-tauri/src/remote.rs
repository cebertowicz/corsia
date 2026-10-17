use crate::{classify::ConnectFailure, remote_config::RemoteRecord};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LaunchState {
    Local,
    Pairing,
    Connecting { name: String },
    Remote { name: String },
    /// The remote could not be opened; the shell draws the card for `failure` with its actions.
    Failed { name: String, failure: ConnectFailure },
    Disconnecting,
}

/// The only owner of the native shell's remote-connection state. Transport
/// modules report outcomes here; they never select shell pages themselves.
pub struct RemoteConnection {
    state: LaunchState,
}

impl Default for RemoteConnection {
    fn default() -> Self {
        Self::launch(None)
    }
}
impl RemoteConnection {
    pub fn launch(saved: Option<&RemoteRecord>) -> Self {
        let state = saved.map_or(LaunchState::Local, |remote| LaunchState::Connecting {
            name: remote.name.clone(),
        });
        Self { state }
    }

    pub fn state(&self) -> &LaunchState {
        &self.state
    }

    pub fn begin_pairing(&mut self) {
        self.state = LaunchState::Pairing;
    }

    pub fn connected(&mut self, name: impl Into<String>) {
        self.state = LaunchState::Remote { name: name.into() };
    }

    pub fn failed(&mut self, name: impl Into<String>, failure: ConnectFailure) {
        self.state = LaunchState::Failed { name: name.into(), failure };
    }

    pub fn retry(&mut self, name: impl Into<String>) {
        self.state = LaunchState::Connecting { name: name.into() };
    }

    pub fn begin_disconnect(&mut self) {
        self.state = LaunchState::Disconnecting;
    }

    pub fn local(&mut self) {
        self.state = LaunchState::Local;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::remote_config::{Locator, RemoteTrust};

    fn record() -> RemoteRecord {
        RemoteRecord {
            locator: Locator::Address { host: "192.168.1.20".into(), port: 47901 },
            trust: RemoteTrust::System,
            name: "Studio".into(),
            proxy_port: 47911,
        }
    }

    #[test]
    fn launch_without_a_saved_remote_starts_local() {
        assert_eq!(RemoteConnection::launch(None).state(), &LaunchState::Local);
    }

    #[test]
    fn launch_with_saved_remote_connects_before_any_local_service_starts() {
        assert_eq!(
            RemoteConnection::launch(Some(&record())).state(),
            &LaunchState::Connecting { name: "Studio".into() }
        );
    }

    #[test]
    fn launch_failures_keep_the_paired_name_and_recovery_transition() {
        let mut connection = RemoteConnection::launch(Some(&record()));
        connection.failed("Studio", ConnectFailure::Network);
        assert_eq!(
            connection.state(),
            &LaunchState::Failed { name: "Studio".into(), failure: ConnectFailure::Network }
        );
        connection.retry("Studio");
        assert_eq!(connection.state(), &LaunchState::Connecting { name: "Studio".into() });
        connection.failed("Studio", ConnectFailure::KeyChanged);
        assert_eq!(connection.state(), &LaunchState::Failed { name: "Studio".into(), failure: ConnectFailure::KeyChanged });
        connection.connected("Studio");
        connection.begin_disconnect();
        assert_eq!(connection.state(), &LaunchState::Disconnecting);
        connection.local();
        assert_eq!(connection.state(), &LaunchState::Local);
    }
}
