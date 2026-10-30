use std::{collections::BTreeSet, path::{Path, PathBuf}};

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
    /// How the app's own start at launch answered (ready, approval, an install to adopt, an undo that did not
    /// finish); the shell draws `startup` like a start it ran itself.
    LocalStartup { startup: crate::ServiceStartup },
    Disconnecting,
}

/// The only owner of what the native startup shell draws: the remote connection, and this computer's own service
/// when its start failed or answered at launch. Transport modules and starts report outcomes here; they never select
/// shell pages themselves.
pub struct ShellState {
    state: LaunchState,
    local_corsia_configured: bool,
    /// An update this process's own observer saw replace the app under the open window; it allows one automatic
    /// recovery start. Only `observe_install` arms it: nothing a page sends can.
    replacement: Option<Replacement>,
    /// Replaced generations whose automatic start was used or invalidated; seeing one again never re-arms it.
    spent: BTreeSet<String>,
}

/// The installed app the observer saw replace the one this process launched from: its path and generation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Replacement {
    pub bundle: PathBuf,
    pub generation: String,
}

impl Default for ShellState {
    fn default() -> Self {
        Self::launch(None)
    }
}
impl ShellState {
    pub fn launch(saved: Option<&RemoteRecord>) -> Self {
        let state = saved.map_or(LaunchState::Local, |remote| LaunchState::Connecting {
            name: remote.name.clone(),
        });
        Self { state, local_corsia_configured: false, replacement: None, spent: BTreeSet::new() }
    }

    /// One reading of the installed app by the replacement observer. A generation other than the launched one, seen
    /// while this computer's own service session is up, arms the automatic recovery start once; the launched
    /// generation coming back, a different replacement, or leaving local mode invalidates it for good. An install that
    /// can't be read changes nothing: the start re-reads it before any effect.
    pub fn observe_install(&mut self, bundle: &Path, launched: &str, installed: Option<&str>, local_session: bool) {
        if !self.is_local_start() {
            self.forget_replacement();
            return;
        }
        let Some(installed) = installed else { return };
        if installed == launched {
            self.forget_replacement();
            return;
        }
        if self.replacement.as_ref().is_some_and(|seen| seen.bundle == bundle && seen.generation == installed) {
            return;
        }
        self.forget_replacement();
        if local_session && !self.spent.contains(installed) {
            self.replacement = Some(Replacement { bundle: bundle.to_path_buf(), generation: installed.to_owned() });
        }
    }

    /// The armed replacement, while the window is on this computer's own service.
    pub fn automatic_recovery(&self) -> Option<&Replacement> {
        self.replacement.as_ref().filter(|_| self.is_local_start())
    }

    /// Spends the automatic start for `expected` before its effect; false when it is no longer the armed one.
    pub fn consume_automatic_recovery(&mut self, expected: &Replacement) -> bool {
        if self.automatic_recovery() != Some(expected) {
            return false;
        }
        self.forget_replacement();
        true
    }

    /// Ends the armed automatic start without running it, as when the registered agent is not exactly this app's.
    pub fn invalidate_automatic_recovery(&mut self) {
        self.forget_replacement();
    }

    /// Whether a manual recovery start may run: only while the window is on this computer's own service.
    pub fn allows_manual_recovery(&self) -> bool {
        self.is_local_start()
    }

    fn forget_replacement(&mut self) {
        if let Some(replacement) = self.replacement.take() {
            self.spent.insert(replacement.generation);
        }
    }

    pub fn state(&self) -> &LaunchState {
        &self.state
    }

    pub fn local_corsia_configured(&self) -> bool {
        self.local_corsia_configured
    }

    pub fn set_local_corsia_configured(&mut self, configured: bool) {
        self.local_corsia_configured = configured;
    }

    pub fn begin_pairing(&mut self) {
        self.forget_replacement();
        self.state = LaunchState::Pairing;
    }

    pub fn connected(&mut self, name: impl Into<String>) {
        self.forget_replacement();
        self.state = LaunchState::Remote { name: name.into() };
    }

    pub fn failed(&mut self, name: impl Into<String>, failure: ConnectFailure) {
        self.forget_replacement();
        self.state = LaunchState::Failed { name: name.into(), failure };
    }

    pub fn retry(&mut self, name: impl Into<String>) {
        self.forget_replacement();
        self.state = LaunchState::Connecting { name: name.into() };
    }

    pub fn service_failed(&mut self, reason: impl Into<String>) {
        self.state = LaunchState::ServiceFailed { reason: reason.into() };
    }

    /// The app's own start at launch answered: an answer is `LocalStartup`, a start that failed `ServiceFailed`. No page
    /// awaits that start, so its answer is kept here for the shell to draw. It is kept only while the window is on this
    /// computer's own start; anything begun meanwhile (pairing, a remote) is newer.
    pub fn launch_answered(&mut self, outcome: Result<crate::ServiceStartup, String>) {
        if self.is_local_start() {
            self.state = match outcome {
                Ok(startup) => LaunchState::LocalStartup { startup },
                Err(reason) => LaunchState::ServiceFailed { reason },
            };
        }
    }

    /// The window ran a local start itself. The page that awaits it draws its answer, so a launch answer kept here is
    /// no longer what the window shows. A failure stays until the start answers: if it fails too, a later shell load
    /// still draws the failure with its Retry.
    pub fn local_start_began(&mut self) {
        if matches!(self.state, LaunchState::LocalStartup { .. }) {
            self.state = LaunchState::Local;
        }
    }

    /// A start the window ran answered, and its page draws that answer, so a kept failure no longer holds.
    pub fn service_recovered(&mut self) {
        if matches!(self.state, LaunchState::ServiceFailed { .. }) {
            self.state = LaunchState::Local;
        }
    }

    /// The window finished loading a page other than the startup shell. A Ready launch answer has then been followed,
    /// so a later shell load (a quit failure, a return to the shell) must not follow it again.
    pub fn left_shell(&mut self) {
        if matches!(self.state, LaunchState::LocalStartup { startup: crate::ServiceStartup::Ready { .. } }) {
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
        self.forget_replacement();
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
            identity: None,
            outside: None,
        }
    }

    #[test]
    fn launch_without_a_saved_remote_starts_local() {
        assert_eq!(ShellState::launch(None).state(), &LaunchState::Local);
    }

    #[test]
    fn launch_with_saved_remote_connects_before_any_local_service_starts() {
        assert_eq!(
            ShellState::launch(Some(&record())).state(),
            &LaunchState::Connecting { name: "Studio".into() }
        );
    }

    #[test]
    fn launch_failures_keep_the_paired_name_and_recovery_transition() {
        let mut connection = ShellState::launch(Some(&record()));
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
    fn a_service_failure_survives_the_end_of_a_pairing_until_the_launch_answers() {
        let mut connection = ShellState::default();
        connection.begin_pairing();
        connection.service_failed("did not start");
        connection.end_pairing();
        assert_eq!(connection.state(), &LaunchState::ServiceFailed { reason: "did not start".into() });
        let ready = crate::ServiceStartup::Ready { url: "http://127.0.0.1:47895".into() };
        connection.launch_answered(Ok(ready.clone()));
        assert_eq!(connection.state(), &LaunchState::LocalStartup { startup: ready.clone() });
        connection.connected("Studio");
        connection.launch_answered(Ok(ready));
        assert_eq!(connection.state(), &LaunchState::Remote { name: "Studio".into() });
    }

    #[test]
    fn a_launch_answer_lasts_until_the_window_runs_a_start_and_never_replaces_what_the_person_began_since() {
        let offered = crate::ServiceStartup::AdoptionOffered { workspace: "~/corsia".into() };
        let mut connection = ShellState::default();
        connection.launch_answered(Ok(offered.clone()));
        assert_eq!(connection.state(), &LaunchState::LocalStartup { startup: offered });
        connection.local_start_began();
        assert_eq!(connection.state(), &LaunchState::Local, "an adoption offer outlived the adoption it started");

        connection.begin_pairing();
        connection.launch_answered(Err("refused".into()));
        assert_eq!(connection.state(), &LaunchState::Pairing, "a late start answer replaced a pairing");
        connection.local_start_began();
        assert_eq!(connection.state(), &LaunchState::Pairing);
    }

    #[test]
    fn a_service_failure_outlasts_a_start_the_window_runs_until_one_answers() {
        let mut connection = ShellState::default();
        connection.launch_answered(Err("did not start".into()));
        connection.local_start_began();
        assert_eq!(
            connection.state(),
            &LaunchState::ServiceFailed { reason: "did not start".into() },
            "a retry cleared the failure, so a reload after it fails again has no card to draw"
        );
        connection.service_recovered();
        assert_eq!(connection.state(), &LaunchState::Local);
    }
}
