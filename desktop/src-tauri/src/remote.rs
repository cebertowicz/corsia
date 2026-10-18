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
    /// How this computer's own start last answered (ready, approval, an install to adopt, an undo that did not
    /// finish); the shell draws `startup` like a start it ran itself.
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

    /// A local start ended with `ended` (`ServiceFailed` or `LocalStartup`). It is kept only while the window is on
    /// this computer's own start; anything begun meanwhile (pairing, a remote) is newer.
    pub fn local_start_ended(&mut self, ended: LaunchState) {
        if self.is_local_start() {
            self.state = ended;
        }
    }

    /// The person acted on the last answer: the start running now owns the window until it answers.
    pub fn local_start_began(&mut self) {
        if self.is_local_start() {
            self.state = LaunchState::Local;
        }
    }

    fn is_local_start(&self) -> bool {
        matches!(self.state, LaunchState::Local | LaunchState::ServiceFailed { .. } | LaunchState::LocalStartup { .. })
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
    fn a_service_failure_survives_the_end_of_a_pairing_until_a_start_answers() {
        let mut connection = RemoteConnection::default();
        connection.begin_pairing();
        connection.service_failed("did not start");
        connection.end_pairing();
        assert_eq!(connection.state(), &LaunchState::ServiceFailed { reason: "did not start".into() });
        let ready = LaunchState::LocalStartup { startup: serde_json::json!({ "state": "ready", "url": "http://127.0.0.1:47895" }) };
        connection.local_start_ended(ready.clone());
        assert_eq!(connection.state(), &ready);
        connection.connected("Studio");
        connection.local_start_ended(ready);
        assert_eq!(connection.state(), &LaunchState::Remote { name: "Studio".into() });
    }

    #[test]
    fn a_start_answer_lasts_until_the_person_acts_on_it_and_never_replaces_what_they_began_since() {
        let offered = LaunchState::LocalStartup { startup: serde_json::json!({ "state": "adoption_offered", "workspace": "~/corsia" }) };
        let mut connection = RemoteConnection::default();
        connection.local_start_ended(offered.clone());
        assert_eq!(connection.state(), &offered);
        connection.local_start_began();
        assert_eq!(connection.state(), &LaunchState::Local, "an adoption offer outlived the adoption it started");

        connection.begin_pairing();
        connection.local_start_ended(LaunchState::ServiceFailed { reason: "refused".into() });
        assert_eq!(connection.state(), &LaunchState::Pairing, "a late start answer replaced a pairing");
        connection.local_start_began();
        assert_eq!(connection.state(), &LaunchState::Pairing);
    }
}
