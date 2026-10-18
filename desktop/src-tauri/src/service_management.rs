//! Windowless ServiceManagement operations for the packaged app.
//!
//! The trait boundary keeps unit tests away from macOS ServiceManagement and
//! launchd. Production code is the only implementation that reaches either.

use std::{
    env,
    path::{Path, PathBuf},
    process::Command,
};

pub const PLIST_NAME: &str = "ai.corsia.service.plist";
pub const STATUS_ENABLED_EXIT: i32 = 0;
pub const STATUS_REQUIRES_APPROVAL_EXIT: i32 = 2;
pub const STATUS_NOT_REGISTERED_EXIT: i32 = 3;
pub const STATUS_NOT_FOUND_EXIT: i32 = 4;
pub const FOREIGN_SERVICE_EXIT: i32 = 5;
pub const USAGE_EXIT: i32 = 64;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ServiceStatus {
    Enabled,
    RequiresApproval,
    NotRegistered,
    NotFound,
}

impl ServiceStatus {
    pub const fn exit_code(self) -> i32 {
        match self {
            Self::Enabled => STATUS_ENABLED_EXIT,
            Self::RequiresApproval => STATUS_REQUIRES_APPROVAL_EXIT,
            Self::NotRegistered => STATUS_NOT_REGISTERED_EXIT,
            Self::NotFound => STATUS_NOT_FOUND_EXIT,
        }
    }
}

pub trait ServiceManager {
    fn status(&self) -> Result<ServiceStatus, String>;
    fn register(&self) -> Result<(), String>;
    fn unregister(&self) -> Result<(), String>;
    fn open_login_items(&self);
}

pub trait LaunchctlReader {
    fn loaded_jobs(&self) -> Result<String, String>;
    fn job_details(&self, label: &str) -> Result<String, String>;
}

struct SystemLaunchctl;

fn launchctl_output(arguments: &[&str]) -> Result<String, String> {
    let output = Command::new("launchctl")
        .args(arguments)
        .output()
        .map_err(|error| format!("could not inspect loaded CORSIA services: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "could not inspect loaded CORSIA services (launchctl exited {})",
            output.status
        ));
    }
    String::from_utf8(output.stdout)
        .map_err(|_| "launchctl returned non-UTF-8 service metadata".into())
}

impl LaunchctlReader for SystemLaunchctl {
    fn loaded_jobs(&self) -> Result<String, String> {
        launchctl_output(&["list"])
    }

    fn job_details(&self, label: &str) -> Result<String, String> {
        let target = format!("gui/{}/{}", current_uid(), label);
        launchctl_output(&["print", &target])
    }
}

#[cfg(unix)]
fn current_uid() -> u32 {
    unsafe { libc::getuid() }
}

#[cfg(not(unix))]
fn current_uid() -> u32 {
    0
}

fn is_corsia_label(label: &str) -> bool {
    label.starts_with("com.corsia.") || label.starts_with("ai.corsia.")
}

pub(crate) fn service_label() -> &'static str {
    PLIST_NAME
        .strip_suffix(".plist")
        .expect("service plist name has its required suffix")
}

fn normalized(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => { result.pop(); }
            component => result.push(component.as_os_str()),
        }
    }
    result
}

/// Return CORSIA labels from the stable three-column `launchctl list` output.
///
/// A bootstrap-domain `launchctl print` is a summary, not a sequence of job
/// records. Each candidate therefore needs its own `print` call below before
/// its program path can establish ownership.
fn corsia_labels(output: &str) -> Vec<String> {
    output
        .lines()
        .filter_map(|line| line.split_whitespace().last())
        .filter(|label| is_corsia_label(label))
        .map(str::to_owned)
        .collect()
}

/// Read only top-level public job metadata; nested environment blocks may
/// contain secrets, even under keys that happen to match metadata names.
fn metadata_fields(output: &str) -> impl Iterator<Item = (&str, &str)> {
    let mut depth = 0usize;
    output.lines().filter_map(move |line| {
        let line = line.trim();
        if line == "}" {
            depth = depth.saturating_sub(1);
            return None;
        }
        if line.ends_with('{') {
            depth += 1;
            return None;
        }
        if depth != 1 {
            return None;
        }
        let (key, value) = line.split_once(" = ")?;
        matches!(key, "path" | "program" | "program identifier" | "state" | "pid")
            .then_some((key, value))
    })
}

/// Named fields are independent: `path` names the plist, not the program.
/// Lexical comparison also works for a stale registration whose files are gone.
fn owns_job(output: &str, bundle: &Path) -> bool {
    let bundle = normalized(bundle);
    metadata_fields(output).any(|(key, value)| {
        let value = value.split_once(" (mode: ").map_or(value, |(value, _)| value)
            .trim().trim_matches('"');
        if value.is_empty() { return false; }
        let path = Path::new(value);
        match key {
            "path" => normalized(path) == bundle.join("Contents/Library/LaunchAgents").join(PLIST_NAME),
            "program" | "program identifier" => {
                if key == "program" && !path.is_absolute() {
                    return false;
                }
                let program = if path.is_absolute() { path.to_path_buf() } else { bundle.join(path) };
                let program = normalized(&program);
                program != bundle && program.starts_with(&bundle)
            }
            _ => false,
        }
    })
}

fn ownership_diagnostic(label: &str, details: &str) -> String {
    let metadata = metadata_fields(details)
        .map(|(key, value)| format!("{key} = {value}"))
        .collect::<Vec<_>>()
        .join("\n");
    format!("CORSIA service ownership refused for {label}:\n{metadata}")
}

fn is_launchd_self(label: &str, xpc_service_name: Option<&str>, parent_pid: u32) -> bool {
    parent_pid == 1 && xpc_service_name == Some(label)
}

fn parent_pid() -> u32 {
    #[cfg(unix)]
    { unsafe { libc::getppid() as u32 } }
    #[cfg(not(unix))]
    { 0 }
}

fn foreign_corsia_job(
    reader: &impl LaunchctlReader,
    bundle: &Path,
) -> Result<Option<String>, String> {
    let xpc_service_name = env::var("XPC_SERVICE_NAME").ok();
    for label in corsia_labels(&reader.loaded_jobs()?) {
        if is_launchd_self(&label, xpc_service_name.as_deref(), parent_pid()) {
            continue;
        }
        let details = reader.job_details(&label)?;
        if !owns_job(&details, bundle) {
            eprintln!("{}", ownership_diagnostic(&label, &details));
            return Ok(Some(label));
        }
    }
    Ok(None)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CurrentService {
    Current,
    Stale,
}

/// Return the app bundle explicitly named by public job metadata, if any.
///
/// An enabled `SMAppService` can be absent from `launchctl list`, and
/// `launchctl print` can omit program metadata. Those observations cannot
/// establish that the enabled service belongs to an old bundle; only an
/// absolute path that actually names a different `.app` bundle can.
fn named_app_bundle(key: &str, value: &str) -> Option<PathBuf> {
    if !matches!(key, "path" | "program" | "program identifier") {
        return None;
    }
    let value = value
        .split_once(" (mode: ")
        .map_or(value, |(value, _)| value)
        .trim()
        .trim_matches('"');
    let path = Path::new(value);
    if !path.is_absolute() {
        return None;
    }

    let mut prefix = PathBuf::new();
    for component in path.components() {
        prefix.push(component.as_os_str());
        if prefix.extension().is_some_and(|extension| extension == "app") {
            return Some(normalized(&prefix));
        }
    }
    None
}

fn current_service(
    reader: &impl LaunchctlReader,
    bundle: &Path,
) -> Result<CurrentService, String> {
    let xpc_service_name = env::var("XPC_SERVICE_NAME").ok();
    let mut current = CurrentService::Current;
    let labels = match reader.loaded_jobs() {
        Ok(jobs) => corsia_labels(&jobs),
        // An enabled SMAppService is authoritative. Do not mutate it when
        // launchctl cannot supply corroborating metadata.
        Err(_) => return Ok(current),
    };
    for label in labels {
        if is_launchd_self(&label, xpc_service_name.as_deref(), parent_pid()) {
            continue;
        }
        let details = match reader.job_details(&label) {
            Ok(details) => details,
            // A canonical record with unavailable details is not evidence of
            // another bundle, so leave the enabled service alone.
            Err(_) if label == service_label() => continue,
            Err(error) => return Err(error),
        };
        if label == service_label() {
            if owns_job(&details, bundle) {
                current = CurrentService::Current;
            } else if metadata_fields(&details).any(|(key, value)| {
                named_app_bundle(key, value).is_some_and(|named| named != normalized(bundle))
            }) {
                current = CurrentService::Stale;
            }
            continue;
        }
        if !owns_job(&details, bundle) {
            eprintln!("{}", ownership_diagnostic(&label, &details));
            return Err(format!("another CORSIA service is already loaded: {label}"));
        }
    }
    Ok(current)
}

fn bundle_root(executable: &Path) -> Result<PathBuf, String> {
    executable
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .filter(|bundle| bundle.extension().is_some_and(|extension| extension == "app"))
        .map(Path::to_path_buf)
        .ok_or_else(|| {
            format!(
                "CORSIA must run from a macOS app bundle, not {}",
                executable.display()
            )
        })
}

pub fn refuse_foreign_corsia_job(
    reader: &impl LaunchctlReader,
    executable: &Path,
) -> Result<(), String> {
    let bundle = bundle_root(executable)?;
    if let Some(label) = foreign_corsia_job(reader, &bundle)? {
        return Err(format!("another CORSIA service is already loaded: {label}"));
    }
    Ok(())
}

fn unregister_if_registered(manager: &impl ServiceManager) -> Result<(), String> {
    if matches!(manager.status()?, ServiceStatus::NotRegistered | ServiceStatus::NotFound) {
        return Ok(());
    }
    manager.unregister().or_else(|error| {
        // Registration can disappear between the status read and unregister.
        match manager.status() {
            Ok(ServiceStatus::NotRegistered | ServiceStatus::NotFound) => Ok(()),
            _ => Err(error),
        }
    })
}

pub fn ensure_service_registered(
    manager: &impl ServiceManager,
    reader: &impl LaunchctlReader,
    executable: &Path,
    operation_state: impl FnOnce() -> Result<bool, String>,
) -> Result<ServiceStatus, String> {
    let status = manager.status()?;
    let bundle = bundle_root(executable)?;
    match status {
        ServiceStatus::Enabled => match current_service(reader, &bundle)? {
            CurrentService::Current => Ok(ServiceStatus::Enabled),
            CurrentService::Stale => {
                if super::should_prevent_quit(operation_state()) {
                    return Err(super::ACTIVE_OPERATION_ERROR.into());
                }
                unregister_if_registered(manager)?;
                manager.register()?;
                manager.status()
            }
        },
        ServiceStatus::RequiresApproval => {
            refuse_foreign_corsia_job(reader, executable)?;
            Ok(status)
        }
        // SMAppService reports NotFound for an agent that was never registered for this
        // user, so only register() can tell whether the bundle really lacks it.
        ServiceStatus::NotRegistered | ServiceStatus::NotFound => {
            refuse_foreign_corsia_job(reader, executable)?;
            manager.register()?;
            manager.status()
        }
    }
}

pub fn service_command(
    args: &[String],
    manager: &impl ServiceManager,
    reader: &impl LaunchctlReader,
    executable: &Path,
) -> Option<i32> {
    if args.first().is_none_or(|argument| argument != "--service") {
        return None;
    }
    let Some(operation) = args.get(1) else {
        eprintln!("usage: CORSIA --service register|unregister|status");
        return Some(USAGE_EXIT);
    };
    if args.len() != 2 {
        eprintln!("usage: CORSIA --service register|unregister|status");
        return Some(USAGE_EXIT);
    }
    match operation.as_str() {
        "status" => match manager.status() {
            Ok(status) => Some(status.exit_code()),
            Err(error) => {
                eprintln!("could not read CORSIA service status: {error}");
                Some(1)
            }
        },
        "register" => match refuse_foreign_corsia_job(reader, executable) {
            Ok(()) => match manager.register() {
                Ok(()) => Some(0),
                Err(error) => {
                    eprintln!("could not register CORSIA service: {error}");
                    Some(1)
                }
            },
            Err(error) => {
                eprintln!("{error}");
                Some(FOREIGN_SERVICE_EXIT)
            }
        },
        "unregister" => match unregister_if_registered(manager) {
            Ok(()) => Some(0),
            Err(error) => {
                eprintln!("could not unregister CORSIA service: {error}");
                Some(1)
            }
        },
        _ => {
            eprintln!("usage: CORSIA --service register|unregister|status");
            Some(USAGE_EXIT)
        }
    }
}

#[cfg(target_os = "macos")]
pub struct NativeServiceManager;

#[cfg(target_os = "macos")]
impl NativeServiceManager {
    fn service() -> objc2::rc::Retained<objc2_service_management::SMAppService> {
        use objc2_foundation::NSString;
        let name = NSString::from_str(PLIST_NAME);
        unsafe { objc2_service_management::SMAppService::agentServiceWithPlistName(&name) }
    }
}

#[cfg(target_os = "macos")]
impl ServiceManager for NativeServiceManager {
    fn status(&self) -> Result<ServiceStatus, String> {
        use objc2_service_management::SMAppServiceStatus;
        match unsafe { Self::service().status() } {
            SMAppServiceStatus::Enabled => Ok(ServiceStatus::Enabled),
            SMAppServiceStatus::RequiresApproval => Ok(ServiceStatus::RequiresApproval),
            SMAppServiceStatus::NotRegistered => Ok(ServiceStatus::NotRegistered),
            SMAppServiceStatus::NotFound => Ok(ServiceStatus::NotFound),
            value => Err(format!("unrecognized SMAppService status {}", value.0)),
        }
    }

    fn register(&self) -> Result<(), String> {
        unsafe { Self::service().registerAndReturnError() }.map_err(|error| error.to_string())
    }

    fn unregister(&self) -> Result<(), String> {
        unsafe { Self::service().unregisterAndReturnError() }.map_err(|error| error.to_string())
    }

    fn open_login_items(&self) {
        unsafe { objc2_service_management::SMAppService::openSystemSettingsLoginItems() }
    }
}


#[cfg(target_os = "macos")]
pub fn ensure_registered_from_app(
    operation_state: impl FnOnce() -> Result<bool, String>,
) -> Result<ServiceStatus, String> {
    let executable = env::current_exe()
        .map_err(|error| format!("could not resolve CORSIA executable: {error}"))?;
    ensure_service_registered(
        &NativeServiceManager,
        &SystemLaunchctl,
        &executable,
        operation_state,
    )
}

#[cfg(target_os = "macos")]
pub fn open_login_items() {
    NativeServiceManager.open_login_items();
}


#[cfg(not(target_os = "macos"))]
pub fn ensure_registered_from_app(
    _operation_state: impl FnOnce() -> Result<bool, String>,
) -> Result<ServiceStatus, String> {
    Err("CORSIA service management is only available on macOS".into())
}

#[cfg(not(target_os = "macos"))]
pub fn open_login_items() {}

#[cfg(target_os = "macos")]
pub fn command_from_environment() -> Option<i32> {
    let arguments = env::args().skip(1).collect::<Vec<_>>();
    let executable = match env::current_exe() {
        Ok(executable) => executable,
        Err(error) => {
            if arguments.first().is_some_and(|argument| argument == "--service") {
                eprintln!("could not resolve CORSIA executable: {error}");
                return Some(1);
            }
            return None;
        }
    };
    service_command(&arguments, &NativeServiceManager, &SystemLaunchctl, &executable)
}

#[cfg(not(target_os = "macos"))]
pub fn command_from_environment() -> Option<i32> {
    let arguments = env::args().skip(1).collect::<Vec<_>>();
    if arguments.first().is_some_and(|argument| argument == "--service") {
        eprintln!("CORSIA service management is only available on macOS");
        return Some(1);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, fs};

    #[derive(Default)]
    struct FakeManager {
        status: Cell<Option<ServiceStatus>>,
        registered_status: Cell<Option<ServiceStatus>>,
        registrations: Cell<u8>,
        unregistrations: Cell<u8>,
        unregister_error: Cell<Option<&'static str>>,
    }

    impl ServiceManager for FakeManager {
        fn status(&self) -> Result<ServiceStatus, String> {
            self.status.get().ok_or_else(|| "missing status".into())
        }
        fn register(&self) -> Result<(), String> {
            self.registrations.set(self.registrations.get() + 1);
            if let Some(status) = self.registered_status.get() {
                self.status.set(Some(status));
            }
            Ok(())
        }
        fn unregister(&self) -> Result<(), String> {
            self.unregistrations.set(self.unregistrations.get() + 1);
            self.unregister_error.get().map_or(Ok(()), |error| Err(error.into()))
        }
        fn open_login_items(&self) {}
    }

    struct FakeLaunchctl {
        jobs: String,
        details: String,
    }

    struct MappedLaunchctl {
        jobs: String,
        details: Vec<(String, String)>,
    }

    impl LaunchctlReader for FakeLaunchctl {
        fn loaded_jobs(&self) -> Result<String, String> {
            Ok(self.jobs.clone())
        }

        fn job_details(&self, _label: &str) -> Result<String, String> {
            Ok(self.details.clone())
        }
    }

    impl LaunchctlReader for MappedLaunchctl {
        fn loaded_jobs(&self) -> Result<String, String> {
            Ok(self.jobs.clone())
        }

        fn job_details(&self, label: &str) -> Result<String, String> {
            self.details
                .iter()
                .find(|(name, _)| name == label)
                .map(|(_, details)| details.clone())
                .ok_or_else(|| format!("missing details for {label}"))
        }
    }

    fn bundle_executable() -> (PathBuf, tempfile::TempDir) {
        let root = tempfile::tempdir().unwrap();
        let executable = root.path().join("CORSIA.app/Contents/MacOS/CORSIA");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, "").unwrap();
        (executable, root)
    }

    fn ensure_unlocked_service_registered(
        manager: &impl ServiceManager,
        reader: &impl LaunchctlReader,
        executable: &Path,
    ) -> Result<ServiceStatus, String> {
        ensure_service_registered(manager, reader, executable, || Ok(false))
    }

    #[test]
    fn shared_ownership_fixtures_cover_register_including_missing_files() {
        let fixtures: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/launchctl-ownership.json"
        )).unwrap();
        // Deliberately never create this bundle: stale metadata must be accepted.
        let root = tempfile::tempdir().unwrap();
        let bundle = root.path().join("CORSIA With Spaces.app");
        let executable = bundle.join("Contents/MacOS/CORSIA");
        for fixture in fixtures.as_array().unwrap() {
            let details = fixture["details"].as_str().unwrap().replace("@BUNDLE@", bundle.to_str().unwrap());
            let owned = fixture["owned"].as_bool().unwrap();
            if let Some(expected) = fixture["is_self"].as_bool() {
                assert_eq!(is_launchd_self("ai.corsia.service", fixture["xpc_service_name"].as_str(), fixture["parent_pid"].as_u64().unwrap() as u32), expected);
            }
            assert_eq!(owns_job(&details, &bundle), owned, "{}", fixture["name"]);
            for ignored in fixture["ignored_metadata"].as_array().into_iter().flatten() {
                let ignored = ignored
                    .as_str()
                    .unwrap()
                    .replace("@BUNDLE@", bundle.to_str().unwrap());
                assert!(
                    !ownership_diagnostic("ai.corsia.service", &details).contains(&ignored),
                    "{}",
                    fixture["name"]
                );
            }
            let manager = FakeManager::default();
            let reader = FakeLaunchctl { jobs: "123 0 ai.corsia.service".into(), details };
            let result = service_command(&["--service".into(), "register".into()], &manager, &reader, &executable);
            assert_eq!(result, Some(if owned { 0 } else { FOREIGN_SERVICE_EXIT }), "{}", fixture["name"]);
            assert_eq!(manager.registrations.get(), u8::from(owned), "{}", fixture["name"]);
        }
    }

    #[test]
    fn enabled_service_requires_current_bundle_identity() {
        let (executable, root) = bundle_executable();
        let bundle = root.path().join("CORSIA.app");
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        let reader = FakeLaunchctl {
            jobs: format!("123 0 {}", service_label()),
            details: format!(
                "{} = {{\n program identifier = Contents/Resources/bin/corsia-service (mode: 2)\n}}\n",
                service_label()
            ),
        };

        assert_eq!(
            ensure_unlocked_service_registered(&manager, &reader, &executable),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(manager.registrations.get(), 0);
        assert_eq!(manager.unregistrations.get(), 0);
        assert!(owns_job(&reader.details, &bundle));
    }

    #[test]
    fn enabled_stale_canonical_service_is_reregistered_when_unlocked() {
        let (executable, root) = bundle_executable();
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        manager.registered_status.set(Some(ServiceStatus::Enabled));
        let reader = FakeLaunchctl {
            jobs: format!("123 0 {}", service_label()),
            details: format!(
                "{} = {{\n program = {}/Old CORSIA.app/Contents/Resources/bin/corsia-service\n}}\n",
                service_label(),
                root.path().display()
            ),
        };

        assert_eq!(
            ensure_service_registered(&manager, &reader, &executable, || Ok(false)),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(manager.unregistrations.get(), 1);
        assert_eq!(manager.registrations.get(), 1);
    }

    #[test]
    fn enabled_stale_canonical_service_is_not_replaced_when_operation_is_locked() {
        let (executable, root) = bundle_executable();
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        let reader = FakeLaunchctl {
            jobs: format!("123 0 {}", service_label()),
            details: format!(
                "{} = {{\n program = {}/Old CORSIA.app/Contents/Resources/bin/corsia-service\n}}\n",
                service_label(),
                root.path().display()
            ),
        };

        assert_eq!(
            ensure_service_registered(&manager, &reader, &executable, || Ok(true)),
            Err(crate::ACTIVE_OPERATION_ERROR.into())
        );
        assert_eq!(manager.unregistrations.get(), 0);
        assert_eq!(manager.registrations.get(), 0);
    }

    #[test]
    fn enabled_service_does_not_reregister_when_canonical_record_is_absent() {
        let (executable, _root) = bundle_executable();
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        let reader = FakeLaunchctl { jobs: String::new(), details: String::new() };

        assert_eq!(
            ensure_unlocked_service_registered(&manager, &reader, &executable),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(manager.unregistrations.get(), 0);
        assert_eq!(manager.registrations.get(), 0);
    }

    #[test]
    fn enabled_service_does_not_reregister_when_canonical_metadata_is_undecidable() {
        let (executable, _root) = bundle_executable();
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        let reader = FakeLaunchctl {
            jobs: format!("123 0 {}", service_label()),
            details: format!("{} = {{\n state = running\n}}\n", service_label()),
        };

        assert_eq!(
            ensure_unlocked_service_registered(&manager, &reader, &executable),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(manager.unregistrations.get(), 0);
        assert_eq!(manager.registrations.get(), 0);
    }

    #[test]
    fn enabled_service_does_not_reregister_when_canonical_metadata_is_unavailable() {
        let (executable, _root) = bundle_executable();
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        let reader = MappedLaunchctl {
            jobs: format!("123 0 {}", service_label()),
            details: Vec::new(),
        };

        assert_eq!(
            ensure_unlocked_service_registered(&manager, &reader, &executable),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(manager.unregistrations.get(), 0);
        assert_eq!(manager.registrations.get(), 0);
    }

    #[test]
    fn enabled_service_refuses_a_different_foreign_corsia_label() {
        let (executable, _root) = bundle_executable();
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        let reader = MappedLaunchctl {
            jobs: "123 0 com.corsia.prod".into(),
            details: vec![(
                "com.corsia.prod".into(),
                "com.corsia.prod = {\n program = /foreign/service\n}\n".into(),
            )],
        };

        assert!(ensure_unlocked_service_registered(&manager, &reader, &executable).is_err());
        assert_eq!(manager.unregistrations.get(), 0);
        assert_eq!(manager.registrations.get(), 0);
    }

    #[test]
    fn non_enabled_statuses_preserve_approval_and_reregister_after_absence() {
        let (executable, _root) = bundle_executable();
        let reader = FakeLaunchctl { jobs: String::new(), details: String::new() };
        let approval = FakeManager::default();
        approval.status.set(Some(ServiceStatus::RequiresApproval));
        assert_eq!(
            ensure_unlocked_service_registered(&approval, &reader, &executable),
            Ok(ServiceStatus::RequiresApproval)
        );
        assert_eq!(approval.registrations.get(), 0);

        let absent = FakeManager::default();
        absent.status.set(Some(ServiceStatus::NotRegistered));
        absent.registered_status.set(Some(ServiceStatus::Enabled));
        assert_eq!(
            ensure_unlocked_service_registered(&absent, &reader, &executable),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(absent.registrations.get(), 1);
    }

    #[test]
    fn a_service_never_registered_for_this_user_is_registered_even_when_reported_not_found() {
        let (executable, _root) = bundle_executable();
        let reader = FakeLaunchctl { jobs: String::new(), details: String::new() };
        let fresh = FakeManager::default();
        fresh.status.set(Some(ServiceStatus::NotFound));
        fresh.registered_status.set(Some(ServiceStatus::Enabled));
        assert_eq!(
            ensure_unlocked_service_registered(&fresh, &reader, &executable),
            Ok(ServiceStatus::Enabled)
        );
        assert_eq!(fresh.registrations.get(), 1);
    }

    #[test]
    fn unregister_succeeds_when_already_absent_but_preserves_other_errors() {
        let (executable, _root) = bundle_executable();
        let reader = FakeLaunchctl { jobs: String::new(), details: String::new() };
        for status in [ServiceStatus::NotRegistered, ServiceStatus::NotFound] {
            let manager = FakeManager::default();
            manager.status.set(Some(status));
            manager.unregister_error.set(Some("must not unregister absent service"));
            assert_eq!(service_command(&["--service".into(), "unregister".into()], &manager, &reader, &executable), Some(0));
            assert_eq!(manager.unregistrations.get(), 0);
        }
        let manager = FakeManager::default();
        manager.status.set(Some(ServiceStatus::Enabled));
        manager.unregister_error.set(Some("permission refused"));
        assert_eq!(unregister_if_registered(&manager), Err("permission refused".into()));
        assert_eq!(manager.unregistrations.get(), 1);
    }

    #[test]
    fn self_identity_requires_launchd_parent_and_matching_xpc_label() {
        assert!(is_launchd_self("ai.corsia.service", Some("ai.corsia.service"), 1));
        assert!(!is_launchd_self("ai.corsia.service", Some("ai.corsia.service"), 123));
        assert!(!is_launchd_self("com.corsia.prod", Some("ai.corsia.service"), 1));
        assert!(!is_launchd_self("ai.corsia.service", None, 1));
    }

    #[test]
    fn ownership_diagnostic_does_not_log_environment_secrets() {
        let fixtures: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/launchctl-ownership.json"
        )).unwrap();
        let fixture = fixtures.as_array().unwrap().iter().find(|case| case["name"] == "private_environment").unwrap();
        let diagnostic = ownership_diagnostic("ai.corsia.service", fixture["details"].as_str().unwrap());
        assert!(!diagnostic.contains("s3cr3t"));
        assert!(!diagnostic.contains("SECRET"));
        assert!(diagnostic.contains("program = /foreign/service"));
        assert!(diagnostic.contains("state = running"));
        assert!(diagnostic.contains("pid = 123"));
    }

    #[test]
    fn status_has_a_distinct_exit_for_every_smappservice_state() {
        assert_eq!(ServiceStatus::Enabled.exit_code(), STATUS_ENABLED_EXIT);
        assert_eq!(
            ServiceStatus::RequiresApproval.exit_code(),
            STATUS_REQUIRES_APPROVAL_EXIT
        );
        assert_eq!(
            ServiceStatus::NotRegistered.exit_code(),
            STATUS_NOT_REGISTERED_EXIT
        );
        assert_eq!(ServiceStatus::NotFound.exit_code(), STATUS_NOT_FOUND_EXIT);
    }

    #[test]
    fn corsia_labels_reads_launchctl_list_output() {
        assert_eq!(
            corsia_labels(
                "PID\tStatus\tLabel\n-\t0\tcom.corsia.prod\n123\t0\tai.corsia.service\n",
            ),
            vec!["com.corsia.prod", "ai.corsia.service"],
        );
    }

    #[test]
    fn register_refuses_a_foreign_corsia_job_before_service_management() {
        let (executable, _root) = bundle_executable();
        let manager = FakeManager::default();
        let launchctl = FakeLaunchctl {
            jobs: "PID\tStatus\tLabel\n-\t0\tcom.corsia.prod\n".into(),
            details: "com.corsia.prod = {\n\tpath = /opt/corsia/bin/corsia\n}\n".into(),
        };

        let exit = service_command(
            &["--service".into(), "register".into()],
            &manager,
            &launchctl,
            &executable,
        );

        assert_eq!(exit, Some(FOREIGN_SERVICE_EXIT));
        assert_eq!(manager.registrations.get(), 0);
    }

    #[test]
    fn register_allows_a_service_program_inside_this_bundle() {
        let (executable, root) = bundle_executable();
        let manager = FakeManager::default();
        let owned_program = root.path().join("CORSIA.app/Contents/Resources/bin/corsia-service");
        fs::create_dir_all(owned_program.parent().unwrap()).unwrap();
        fs::write(&owned_program, "").unwrap();
        let launchctl = FakeLaunchctl {
            jobs: "PID\tStatus\tLabel\n123\t0\tai.corsia.service\n".into(),
            details: format!(
                "ai.corsia.service = {{\n\tprogram = {}\n}}\n",
                owned_program.display()
            ),
        };

        let exit = service_command(
            &["--service".into(), "register".into()],
            &manager,
            &launchctl,
            &executable,
        );

        assert_eq!(exit, Some(0));
        assert_eq!(manager.registrations.get(), 1);
    }
}
