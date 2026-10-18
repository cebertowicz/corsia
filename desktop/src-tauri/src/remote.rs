use crate::{classify::ConnectFailure, remote_config::RemoteRecord};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LaunchState {
    Local,
    Pairing,
    Connecting { name: String },
    Remote { name: String },
    /// The remote could not be opened; the shell draws the card for `failure` with its actions.
    Failed { name: String, failure: ConnectFailure },
    /// This computer's own service could not be brought back; the shell draws its failure card with the action.
    ServiceFailed { reason: String },
    /// This computer's own service stopped at a screen the person answers (approval, an install to adopt, an undo
    /// that did not finish); `startup` is how it answered, and the shell draws it like a start it ran itself.
    LocalStartup { startup: serde_json::Value },
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

    pub fn service_failed(&mut self, reason: impl Into<String>) {
        self.state = LaunchState::ServiceFailed { reason: reason.into() };
    }

    /// The launch-time start ended on a failure or a screen the person answers. Only a shell still showing the start
    /// takes it; anything begun meanwhile (pairing, say) is newer.
    pub fn launch_ended(&mut self, ended: LaunchState) {
        if self.state == LaunchState::Local {
            self.state = ended;
        }
    }

    /// A service that came up ends its failure or the screen it was waiting on; any other state is left as it is.
    pub fn service_recovered(&mut self) {
        if matches!(self.state, LaunchState::ServiceFailed { .. } | LaunchState::LocalStartup { .. }) {
            self.state = LaunchState::Local;
        }
    }

    /// A pairing that did not connect returns to local; a failure recorded meanwhile stays.
    pub fn end_pairing(&mut self) {
        if self.state == LaunchState::Pairing {
            self.state = LaunchState::Local;
        }
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

    #[test]
    fn a_service_failure_survives_the_end_of_a_pairing_until_the_service_comes_back() {
        let mut connection = RemoteConnection::default();
        connection.begin_pairing();
        connection.service_failed("did not start");
        connection.end_pairing();
        assert_eq!(connection.state(), &LaunchState::ServiceFailed { reason: "did not start".into() });
        connection.service_recovered();
        assert_eq!(connection.state(), &LaunchState::Local);
        connection.begin_pairing();
        connection.end_pairing();
        assert_eq!(connection.state(), &LaunchState::Local);
        connection.connected("Studio");
        connection.service_recovered();
        assert_eq!(connection.state(), &LaunchState::Remote { name: "Studio".into() });
    }

    #[test]
    fn a_launch_outcome_lasts_until_the_service_comes_back_and_never_replaces_what_the_person_began_since() {
        let offered = LaunchState::LocalStartup { startup: serde_json::json!({ "state": "requires_approval" }) };
        let mut connection = RemoteConnection::default();
        connection.launch_ended(offered.clone());
        assert_eq!(connection.state(), &offered);
        connection.service_recovered();
        assert_eq!(connection.state(), &LaunchState::Local);

        connection.begin_pairing();
        connection.launch_ended(LaunchState::ServiceFailed { reason: "refused".into() });
        assert_eq!(connection.state(), &LaunchState::Pairing, "a late launch outcome replaced a pairing");
        connection.connected("Studio");
        connection.launch_ended(offered);
        assert_eq!(connection.state(), &LaunchState::Remote { name: "Studio".into() });
    }
}
