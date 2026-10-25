//! Bringing this computer's service back during an update, from the startup shell's update screen.
//!
//! The app's full start plans the first run, adopts, puts back a Start fresh and repairs registration;
//! none of that belongs in the middle of an update. A recovery start only starts the agent this app already has
//! registered, under the update lock, and proves it with the ordinary startup proof. An automatic one needs the
//! replacement this process's own observer saw (`ShellState::observe_install`); a page can ask, never arm it.

use std::{
    path::Path,
    process::Command,
    sync::{Mutex, PoisonError},
};

use serde::{Deserialize, Serialize};

use crate::{
    app_paths,
    service_management::{self, ServiceStatus},
    shell_state::ShellState,
};

/// Why the startup shell asks: its own timer after a restart ran long, or the person pressing Start CORSIA.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RecoveryIntent {
    Automatic,
    Manual,
}

/// A recovery start's answer. Only `Ready` follows a proven service; the others leave the update screen where it is.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "recovery", rename_all = "snake_case")]
pub(crate) enum RecoveryAnswer {
    /// The service passed its startup proof at this URL.
    Ready { url: String },
    /// An update or another start holds the update lock: nothing was done, and an automatic start is still unspent.
    Deferred,
    /// Automatic recovery cannot start, but a current manual request may pass the same narrow admission. Nothing was
    /// started or consumed.
    ManualAvailable,
    /// No recovery start applies here (not this computer's service, a stale or mismatched observed replacement, or an
    /// agent that is not exactly this app's): nothing was done.
    Unavailable,
    /// The one start ran and the service did not prove itself.
    NotStarted,
}

/// What a recovery start reads and does outside this module; tests replace it, so they never reach launchd.
pub(crate) trait RecoveryHost {
    /// The generation of the app installed at `bundle`, read from disk now.
    fn installed_generation(&self, bundle: &Path) -> Option<String>;
    /// ServiceManagement's status for the agent and launchd's record of the canonical job.
    fn registered_agent(&self) -> Result<(ServiceStatus, String), String>;
    /// Whether launchd reports a process for the job.
    fn running(&self) -> bool;
    /// `launchctl kickstart` without `-k`: starts the loaded job, never kills a running one.
    fn kickstart(&self) -> Result<(), String>;
    /// The ordinary startup proof (`wait_for_service`).
    fn wait_for_startup(&self) -> Result<(), String>;
}

/// Runs one recovery start. Every refusal comes before any effect; the update lock, taken without waiting, is held
/// until the startup proof answers.
pub(crate) fn recover(
    intent: RecoveryIntent,
    shell: &Mutex<ShellState>,
    bundle: &Path,
    state_dir: &Path,
    local_session: bool,
    url: &str,
    host: &impl RecoveryHost,
) -> RecoveryAnswer {
    let shell_state = || shell.lock().unwrap_or_else(PoisonError::into_inner);
    if !local_session {
        return RecoveryAnswer::Unavailable;
    }
    // Take one short snapshot of the shell state. Calling `shell_state` again while this guard is live would deadlock.
    let (automatic_recovery, manual_allowed, manual_offer_allowed) = {
        let state = shell_state();
        (
            state.automatic_recovery().cloned(),
            state.allows_manual_recovery(),
            state.allows_manual_recovery_offer(),
        )
    };
    let (expected, manual_available) = match intent {
        RecoveryIntent::Automatic => match automatic_recovery {
            Some(replacement) if replacement.bundle == bundle => (Some(replacement), false),
            Some(_) => return RecoveryAnswer::Unavailable,
            None if manual_offer_allowed => (None, true),
            None => return RecoveryAnswer::Unavailable,
        },
        RecoveryIntent::Manual if manual_allowed => (None, false),
        RecoveryIntent::Manual => return RecoveryAnswer::Unavailable,
    };
    let still_installed = || {
        expected.as_ref().is_none_or(|replacement| {
            host.installed_generation(bundle).as_deref() == Some(replacement.generation.as_str())
        })
    };
    if !still_installed() {
        return RecoveryAnswer::Unavailable;
    }
    let _update = match app_paths::try_lock_update_in_existing(state_dir) {
        Ok(Some(lock)) => lock,
        Ok(None) => return RecoveryAnswer::Deferred,
        Err(error) => {
            log::error!("a recovery start could not take the update lock: {error}");
            return RecoveryAnswer::Unavailable;
        }
    };
    // A missing, foreign or mismatched agent ends the automatic start for good, decided under the lock.
    match host.registered_agent() {
        Ok((ServiceStatus::Enabled, details)) if service_management::is_exactly_this_agent(&details, bundle) => {}
        Ok((status, _)) => {
            log::error!("a recovery start refused: the agent is {status:?} or is not exactly this app's");
            shell_state().invalidate_automatic_recovery();
            return RecoveryAnswer::Unavailable;
        }
        Err(error) => {
            log::error!("a recovery start refused: the agent could not be read: {error}");
            shell_state().invalidate_automatic_recovery();
            return RecoveryAnswer::Unavailable;
        }
    }
    // The automatic no-observation answer rechecks manual eligibility under the canonical lock and admission before
    // inviting an explicit request; it never consumes the automatic attempt or reaches a start effect.
    if manual_available {
        return if shell_state().allows_manual_recovery_offer() {
            RecoveryAnswer::ManualAvailable
        } else {
            RecoveryAnswer::Unavailable
        };
    }
    // Checked again now the lock is held, and the automatic start is spent before its one effect.
    let admitted = still_installed() && {
        let mut state = shell_state();
        match &expected {
            Some(replacement) => state.consume_automatic_recovery(replacement),
            None => state.allows_manual_recovery(),
        }
    };
    if !admitted {
        return RecoveryAnswer::Unavailable;
    }
    if !host.running() {
        if let Err(error) = host.kickstart() {
            log::error!("a recovery start could not start the service: {error}");
            return RecoveryAnswer::NotStarted;
        }
    }
    match host.wait_for_startup() {
        Ok(()) => RecoveryAnswer::Ready { url: url.to_owned() },
        Err(error) => {
            log::error!("a recovery start did not pass its startup proof: {error}");
            RecoveryAnswer::NotStarted
        }
    }
}

/// The real host: this app's agent through ServiceManagement and launchd, and the service's own startup proof.
pub(crate) struct SystemRecovery<'a> {
    pub(crate) port: u16,
    pub(crate) app_dir: &'a Path,
}

fn job_target() -> String {
    format!("gui/{}/{}", unsafe { libc::geteuid() }, service_management::service_label())
}

impl RecoveryHost for SystemRecovery<'_> {
    fn installed_generation(&self, bundle: &Path) -> Option<String> {
        crate::bundle_generation(bundle)
    }

    fn registered_agent(&self) -> Result<(ServiceStatus, String), String> {
        service_management::registered_agent()
    }

    fn running(&self) -> bool {
        crate::release_proof::launchd_pid(&job_target()).is_some()
    }

    fn kickstart(&self) -> Result<(), String> {
        let status = Command::new("/bin/launchctl")
            .args(["kickstart", &job_target()])
            .status()
            .map_err(|error| format!("could not run launchctl kickstart: {error}"))?;
        if status.success() { Ok(()) } else { Err(format!("launchctl kickstart exited {status}")) }
    }

    fn wait_for_startup(&self) -> Result<(), String> {
        crate::wait_for_service(self.port, self.app_dir)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        cell::{Cell, RefCell},
        fs,
        path::PathBuf,
    };

    const LAUNCHED: &str = "0.4.0+40";
    const INSTALLED: &str = "0.4.1+41";

    struct Fixture {
        _root: tempfile::TempDir,
        bundle: PathBuf,
        state: PathBuf,
    }

    fn fixture() -> Fixture {
        let root = tempfile::tempdir().unwrap();
        let bundle = root.path().join("Applications/CORSIA.app");
        let state = root.path().join("state");
        fs::create_dir_all(&bundle).unwrap();
        fs::create_dir_all(&state).unwrap();
        Fixture { bundle, state, _root: root }
    }

    fn agent(bundle: &Path) -> String {
        format!(
            "gui/501/ai.corsia.service = {{\n\tpath = {}/Contents/Library/LaunchAgents/ai.corsia.service.plist\n\tprogram identifier = Contents/Resources/bin/corsia-service (mode: 2)\n\tenvironment = {{\n\t\tprogram = /elsewhere\n\t}}\n}}\n",
            bundle.display()
        )
    }

    /// Records every effect; `kickstarts` and `proofs` are the start effects a refusal must never reach.
    struct Spy<'a> {
        installed: RefCell<Option<String>>,
        /// What is installed after the first reading, for a swap that lands between the check and the lock.
        then_installed: RefCell<Option<Option<String>>>,
        status: ServiceStatus,
        details: Result<String, String>,
        running: bool,
        proof: Result<(), String>,
        kickstarts: Cell<u32>,
        proofs: Cell<u32>,
        state: &'a Path,
        held_at_first_effect: Cell<Option<bool>>,
        held_through_proof: Cell<Option<bool>>,
    }

    fn spy<'a>(fixture: &'a Fixture) -> Spy<'a> {
        Spy {
            installed: RefCell::new(Some(INSTALLED.into())),
            then_installed: RefCell::new(None),
            status: ServiceStatus::Enabled,
            details: Ok(agent(&fixture.bundle)),
            running: false,
            proof: Ok(()),
            kickstarts: Cell::new(0),
            proofs: Cell::new(0),
            state: &fixture.state,
            held_at_first_effect: Cell::new(None),
            held_through_proof: Cell::new(None),
        }
    }

    impl Spy<'_> {
        fn effects(&self) -> u32 {
            self.kickstarts.get() + self.proofs.get()
        }
    }

    impl RecoveryHost for Spy<'_> {
        fn installed_generation(&self, _bundle: &Path) -> Option<String> {
            let now = self.installed.borrow().clone();
            if let Some(then) = self.then_installed.borrow_mut().take() {
                *self.installed.borrow_mut() = then;
            }
            now
        }
        fn registered_agent(&self) -> Result<(ServiceStatus, String), String> {
            self.details.clone().map(|details| (self.status, details))
        }
        fn running(&self) -> bool {
            self.running
        }
        fn kickstart(&self) -> Result<(), String> {
            self.kickstarts.set(self.kickstarts.get() + 1);
            // An independent opener is refused before the first start effect, not only while its proof runs.
            let busy = app_paths::try_lock_update_in_existing(self.state).unwrap().is_none();
            self.held_at_first_effect.set(Some(busy));
            Ok(())
        }
        fn wait_for_startup(&self) -> Result<(), String> {
            self.proofs.set(self.proofs.get() + 1);
            // An independent opener of the same lock file is refused while the proof runs.
            let busy = app_paths::try_lock_update_in_existing(self.state).unwrap().is_none();
            self.held_through_proof.set(Some(busy));
            self.proof.clone()
        }
    }

    /// The shell state after the observer saw `installed` replace the launched app during a local session.
    fn observed(fixture: &Fixture, installed: Option<&str>) -> Mutex<ShellState> {
        let mut shell = ShellState::default();
        shell.observe_install(&fixture.bundle, LAUNCHED, installed, true);
        Mutex::new(shell)
    }

    fn run(intent: RecoveryIntent, shell: &Mutex<ShellState>, fixture: &Fixture, host: &Spy) -> RecoveryAnswer {
        recover(intent, shell, &fixture.bundle, &fixture.state, true, "http://127.0.0.1:47895", host)
    }

    /// A test that takes the update lock runs in a process of its own, as `app_paths`' lock tests do: a concurrent
    /// test's fork briefly holds a copy of a dropped lock's descriptor, which reads as another holder.
    fn alone(name: &str) -> bool {
        app_paths::tests::alone(&format!("update_recovery::tests::{name}"))
    }

    #[test]
    fn an_observed_replacement_allows_exactly_one_non_killing_start_held_under_the_lock_through_its_proof() {
        if !alone("an_observed_replacement_allows_exactly_one_non_killing_start_held_under_the_lock_through_its_proof") {
            return;
        }
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let host = spy(&fixture);
        assert_eq!(
            run(RecoveryIntent::Automatic, &shell, &fixture, &host),
            RecoveryAnswer::Ready { url: "http://127.0.0.1:47895".into() }
        );
        assert_eq!((host.kickstarts.get(), host.proofs.get()), (1, 1));
        assert_eq!(host.held_at_first_effect.get(), Some(true), "the update lock was not held at the first start effect");
        assert_eq!(host.held_through_proof.get(), Some(true), "the update lock was not held through the startup proof");
        assert!(app_paths::try_lock_update_in_existing(&fixture.state).unwrap().is_some(), "the lock outlived the start");

        // The same replacement seen again, a repeat request, a reload or Back: the attempt is spent.
        shell.lock().unwrap().observe_install(&fixture.bundle, LAUNCHED, Some(INSTALLED), true);
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        assert_eq!(host.effects(), 2);
    }

    #[test]
    fn a_running_service_is_proved_without_a_kickstart() {
        if !alone("a_running_service_is_proved_without_a_kickstart") {
            return;
        }
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let host = Spy { running: true, ..spy(&fixture) };
        assert!(matches!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Ready { .. }));
        assert_eq!((host.kickstarts.get(), host.proofs.get()), (0, 1));
    }

    #[test]
    fn a_failed_proof_spends_the_automatic_start_and_says_so() {
        if !alone("a_failed_proof_spends_the_automatic_start_and_says_so") {
            return;
        }
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let host = Spy { proof: Err("timed out".into()), ..spy(&fixture) };
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::NotStarted);
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        assert_eq!(host.kickstarts.get(), 1);
    }

    #[test]
    fn a_held_update_lock_defers_without_an_effect_or_spending_the_attempt() {
        if !alone("a_held_update_lock_defers_without_an_effect_or_spending_the_attempt") {
            return;
        }
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let host = spy(&fixture);
        let updater = app_paths::try_lock_update_in_existing(&fixture.state).unwrap().unwrap();
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Deferred);
        assert_eq!(run(RecoveryIntent::Manual, &shell, &fixture, &host), RecoveryAnswer::Deferred);
        assert_eq!(host.effects(), 0);
        drop(updater);
        assert!(matches!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Ready { .. }));
        assert_eq!(host.kickstarts.get(), 1);
    }

    #[test]
    fn without_a_native_observation_automatic_recovery_only_offers_an_exact_manual_start() {
        if !alone("without_a_native_observation_automatic_recovery_only_offers_an_exact_manual_start") {
            return;
        }
        let fixture = fixture();
        let host = spy(&fixture);
        // A page that arrives with a forged snapshot or fragment reaches native code only as this request: nothing
        // it carries can arm the start. The native preflight may only offer the existing narrow manual request.
        let untouched = Mutex::new(ShellState::default());
        assert_eq!(run(RecoveryIntent::Automatic, &untouched, &fixture, &host), RecoveryAnswer::ManualAvailable);
        // The observer saw only the launched app, or could not read the install: neither is an automatic candidate.
        assert_eq!(run(RecoveryIntent::Automatic, &observed(&fixture, Some(LAUNCHED)), &fixture, &host), RecoveryAnswer::ManualAvailable);
        assert_eq!(run(RecoveryIntent::Automatic, &observed(&fixture, None), &fixture, &host), RecoveryAnswer::ManualAvailable);
        assert_eq!(host.effects(), 0);
    }

    #[test]
    fn a_replacement_seen_outside_an_initialized_local_session_does_not_arm() {
        let fixture = fixture();
        let mut shell = ShellState::default();
        shell.observe_install(&fixture.bundle, LAUNCHED, Some(INSTALLED), false);
        let host = spy(&fixture);
        assert_eq!(run(RecoveryIntent::Automatic, &Mutex::new(shell), &fixture, &host), RecoveryAnswer::Unavailable);
        let armed = observed(&fixture, Some(INSTALLED));
        assert_eq!(
            recover(RecoveryIntent::Automatic, &armed, &fixture.bundle, &fixture.state, false, "u", &host),
            RecoveryAnswer::Unavailable
        );
        assert_eq!(host.effects(), 0);
    }

    #[test]
    fn a_stale_or_mismatched_replacement_starts_nothing() {
        if !alone("a_stale_or_mismatched_replacement_starts_nothing") {
            return;
        }
        let fixture = fixture();
        let host = spy(&fixture);
        // Rolled back since: the launched app is installed again, so the observation is invalid for good.
        let shell = observed(&fixture, Some(INSTALLED));
        shell.lock().unwrap().observe_install(&fixture.bundle, LAUNCHED, Some(LAUNCHED), true);
        shell.lock().unwrap().observe_install(&fixture.bundle, LAUNCHED, Some(INSTALLED), true);
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        // Disk changed before the observer's next reading.
        let shell = observed(&fixture, Some(INSTALLED));
        *host.installed.borrow_mut() = Some("0.4.2+42".into());
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        *host.installed.borrow_mut() = None;
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        // A mismatch is not deferred behind a held lock as if a start were still owed.
        *host.installed.borrow_mut() = Some(LAUNCHED.into());
        let updater = app_paths::try_lock_update_in_existing(&fixture.state).unwrap().unwrap();
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        drop(updater);
        // Observed at another path than this app's.
        *host.installed.borrow_mut() = Some(INSTALLED.into());
        let mut elsewhere = ShellState::default();
        elsewhere.observe_install(Path::new("/Applications/Other.app"), LAUNCHED, Some(INSTALLED), true);
        assert_eq!(run(RecoveryIntent::Automatic, &Mutex::new(elsewhere), &fixture, &host), RecoveryAnswer::Unavailable);
        assert_eq!(host.effects(), 0);
        // The unspent observation still starts once it matches again.
        assert!(matches!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Ready { .. }));
    }

    #[test]
    fn a_swap_that_lands_while_the_lock_is_taken_starts_nothing() {
        if !alone("a_swap_that_lands_while_the_lock_is_taken_starts_nothing") {
            return;
        }
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let host = Spy { then_installed: RefCell::new(Some(Some(LAUNCHED.into()))), ..spy(&fixture) };
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        assert_eq!(host.effects(), 0);
    }

    #[test]
    fn a_remote_window_never_recovers_and_its_observation_does_not_come_back() {
        let fixture = fixture();
        let host = spy(&fixture);
        let shell = observed(&fixture, Some(INSTALLED));
        shell.lock().unwrap().connected("Studio");
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        assert_eq!(run(RecoveryIntent::Manual, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        shell.lock().unwrap().local();
        shell.lock().unwrap().observe_install(&fixture.bundle, LAUNCHED, Some(INSTALLED), true);
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Unavailable);
        let pairing = observed(&fixture, Some(INSTALLED));
        pairing.lock().unwrap().begin_pairing();
        assert_eq!(run(RecoveryIntent::Automatic, &pairing, &fixture, &host), RecoveryAnswer::Unavailable);
        pairing.lock().unwrap().end_pairing();
        pairing.lock().unwrap().observe_install(&fixture.bundle, LAUNCHED, Some(INSTALLED), true);
        assert_eq!(run(RecoveryIntent::Automatic, &pairing, &fixture, &host), RecoveryAnswer::Unavailable);
        assert_eq!(host.effects(), 0);
    }

    #[test]
    fn only_exactly_this_apps_registered_agent_is_started() {
        if !alone("only_exactly_this_apps_registered_agent_is_started") {
            return;
        }
        let fixture = fixture();
        let foreign = format!(
            "gui/501/ai.corsia.service = {{\n\tprogram = {}/Contents/Resources/bin/corsia-service\n}}\n",
            fixture.bundle.parent().unwrap().join("Old CORSIA.app").display()
        );
        // Inside this bundle, which registration repair accepts, but not the agent's program.
        let other_program = format!(
            "gui/501/ai.corsia.service = {{\n\tprogram = {}/Contents/MacOS/CORSIA\n}}\n",
            fixture.bundle.display()
        );
        let contradictory = format!(
            "{}\tprogram = /usr/local/bin/corsia-service\n}}\n",
            agent(&fixture.bundle).trim_end().trim_end_matches('}')
        );
        let plist_only = format!(
            "gui/501/ai.corsia.service = {{\n\tpath = {}/Contents/Library/LaunchAgents/ai.corsia.service.plist\n}}\n",
            fixture.bundle.display()
        );
        let refused: Vec<(ServiceStatus, Result<String, String>)> = vec![
            (ServiceStatus::Enabled, Err("Could not find service".into())),
            (ServiceStatus::Enabled, Ok(foreign)),
            (ServiceStatus::Enabled, Ok(other_program)),
            (ServiceStatus::Enabled, Ok(contradictory)),
            (ServiceStatus::Enabled, Ok(plist_only)),
            (ServiceStatus::Enabled, Ok(String::new())),
            (ServiceStatus::NotRegistered, Ok(agent(&fixture.bundle))),
            (ServiceStatus::RequiresApproval, Ok(agent(&fixture.bundle))),
            (ServiceStatus::NotFound, Ok(agent(&fixture.bundle))),
        ];
        let good = spy(&fixture);
        for (status, details) in refused {
            for intent in [RecoveryIntent::Automatic, RecoveryIntent::Manual] {
                let shell = observed(&fixture, Some(INSTALLED));
                let host = Spy { status, details: details.clone(), ..spy(&fixture) };
                assert_eq!(run(intent, &shell, &fixture, &host), RecoveryAnswer::Unavailable, "{intent:?} {status:?} {details:?}");
                assert_eq!(host.effects(), 0, "{intent:?} {status:?} {details:?}");
                // A missing, foreign or mismatched agent invalidates the automatic start for good: the same
                // replacement seen again, or an agent that later reads right, never brings it back.
                assert!(shell.lock().unwrap().automatic_recovery().is_none(), "{intent:?} {status:?} {details:?}");
                shell.lock().unwrap().observe_install(&fixture.bundle, LAUNCHED, Some(INSTALLED), true);
                assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &good), RecoveryAnswer::Unavailable);
                assert_eq!(good.effects(), 0);
            }
        }
    }

    #[test]
    fn automatic_manual_availability_still_refuses_a_foreign_unregistered_or_unapproved_agent() {
        if !alone("automatic_manual_availability_still_refuses_a_foreign_unregistered_or_unapproved_agent") {
            return;
        }
        let fixture = fixture();
        let foreign = format!(
            "gui/501/ai.corsia.service = {{\n\tprogram = {}/Contents/Resources/bin/corsia-service\n}}\n",
            fixture.bundle.parent().unwrap().join("Old CORSIA.app").display()
        );
        for (status, details) in [
            (ServiceStatus::Enabled, Ok(foreign)),
            (ServiceStatus::NotRegistered, Ok(agent(&fixture.bundle))),
            (ServiceStatus::RequiresApproval, Ok(agent(&fixture.bundle))),
        ] {
            for intent in [RecoveryIntent::Automatic, RecoveryIntent::Manual] {
                let shell = Mutex::new(ShellState::default());
                let host = Spy { status, details: details.clone(), ..spy(&fixture) };
                assert_eq!(run(intent, &shell, &fixture, &host), RecoveryAnswer::Unavailable, "{intent:?} {status:?}");
                assert_eq!(host.effects(), 0, "{intent:?} {status:?}");
            }
        }
    }

    #[test]
    fn a_refusal_before_the_lock_leaves_the_automatic_start_armed() {
        if !alone("a_refusal_before_the_lock_leaves_the_automatic_start_armed") {
            return;
        }
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let _held = app_paths::try_lock_update_in_existing(&fixture.state).unwrap().unwrap();
        // An unreadable agent is not judged while another update holds the lock: that answer is Deferred.
        let host = Spy { details: Err("Could not find service".into()), ..spy(&fixture) };
        assert_eq!(run(RecoveryIntent::Automatic, &shell, &fixture, &host), RecoveryAnswer::Deferred);
        assert!(shell.lock().unwrap().automatic_recovery().is_some());
        assert_eq!(host.effects(), 0);
    }

    #[test]
    fn a_missing_state_folder_refuses_and_is_never_made() {
        let fixture = fixture();
        let shell = observed(&fixture, Some(INSTALLED));
        let host = spy(&fixture);
        let missing = fixture.state.join("gone");
        assert_eq!(
            recover(RecoveryIntent::Automatic, &shell, &fixture.bundle, &missing, true, "u", &host),
            RecoveryAnswer::Unavailable
        );
        assert!(!missing.exists());
        assert_eq!(host.effects(), 0);
    }

    #[test]
    fn a_manual_start_needs_no_observation_but_is_still_narrow() {
        if !alone("a_manual_start_needs_no_observation_but_is_still_narrow") {
            return;
        }
        let fixture = fixture();
        let host = spy(&fixture);
        let shell = Mutex::new(ShellState::default());
        assert!(matches!(run(RecoveryIntent::Manual, &shell, &fixture, &host), RecoveryAnswer::Ready { .. }));
        assert!(matches!(run(RecoveryIntent::Manual, &shell, &fixture, &host), RecoveryAnswer::Ready { .. }));
        assert_eq!(host.kickstarts.get(), 2, "each press is one attempt");
        // A manual start never spends the automatic one.
        let armed = observed(&fixture, Some(INSTALLED));
        let _ = run(RecoveryIntent::Manual, &armed, &fixture, &host);
        assert!(armed.lock().unwrap().automatic_recovery().is_some());
    }

    #[test]
    fn the_recovery_start_reaches_no_full_startup_step() {
        let source = include_str!("update_recovery.rs").split("#[cfg(test)]").next().unwrap();
        for forbidden in [
            "start_service", "first_run", "adoption", "start_fresh", "unfinished_setup", "register(", "ensure_registered",
            "kickstart\", \"-k", "\"-k\"", "unregister", "bootstrap",
        ] {
            assert!(!source.contains(forbidden), "the recovery start reaches {forbidden}");
        }
    }

    #[test]
    fn the_answers_the_shell_reads_are_typed() {
        assert_eq!(serde_json::to_value(RecoveryAnswer::Deferred).unwrap(), serde_json::json!({ "recovery": "deferred" }));
        assert_eq!(serde_json::to_value(RecoveryAnswer::ManualAvailable).unwrap(), serde_json::json!({ "recovery": "manual_available" }));
        assert_eq!(
            serde_json::to_value(RecoveryAnswer::Ready { url: "http://127.0.0.1:1".into() }).unwrap(),
            serde_json::json!({ "recovery": "ready", "url": "http://127.0.0.1:1" })
        );
        assert_eq!(serde_json::from_value::<RecoveryIntent>(serde_json::json!("manual")).unwrap(), RecoveryIntent::Manual);
        assert!(serde_json::from_value::<RecoveryIntent>(serde_json::json!("full")).is_err());
    }
}
