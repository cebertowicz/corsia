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

fn is_inside_bundle(program: &Path, bundle: &Path) -> bool {
    program
        .canonicalize()
        .ok()
        .zip(bundle.canonicalize().ok())
        .is_some_and(|(program, bundle)| program.starts_with(bundle))
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

/// Parse the program path emitted by `launchctl print gui/<uid>/<label>`.
fn service_program(output: &str) -> Option<PathBuf> {
    output.lines().find_map(|line| {
        let value = line
            .trim()
            .strip_prefix("path = ")
            .or_else(|| line.trim().strip_prefix("program = "))?;
        Some(PathBuf::from(value.trim_matches('"')))
    })
}

fn foreign_corsia_job(
    reader: &impl LaunchctlReader,
    bundle: &Path,
) -> Result<Option<String>, String> {
    for label in corsia_labels(&reader.loaded_jobs()?) {
        let program = service_program(&reader.job_details(&label)?);
        if !program
            .as_deref()
            .is_some_and(|program| is_inside_bundle(program, bundle))
        {
            return Ok(Some(label));
        }
    }
    Ok(None)
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
        "unregister" => match manager.unregister() {
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
pub fn service_status() -> Result<ServiceStatus, String> {
    NativeServiceManager.status()
}

#[cfg(target_os = "macos")]
pub fn register_from_app() -> Result<(), String> {
    let executable = env::current_exe()
        .map_err(|error| format!("could not resolve CORSIA executable: {error}"))?;
    refuse_foreign_corsia_job(&SystemLaunchctl, &executable)?;
    NativeServiceManager.register()
}

#[cfg(target_os = "macos")]
pub fn open_login_items() {
    NativeServiceManager.open_login_items();
}

#[cfg(not(target_os = "macos"))]
pub fn service_status() -> Result<ServiceStatus, String> {
    Err("CORSIA service management is only available on macOS".into())
}

#[cfg(not(target_os = "macos"))]
pub fn register_from_app() -> Result<(), String> {
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
        registrations: Cell<u8>,
    }

    impl ServiceManager for FakeManager {
        fn status(&self) -> Result<ServiceStatus, String> {
            self.status.get().ok_or_else(|| "missing status".into())
        }
        fn register(&self) -> Result<(), String> {
            self.registrations.set(self.registrations.get() + 1);
            Ok(())
        }
        fn unregister(&self) -> Result<(), String> {
            Ok(())
        }
        fn open_login_items(&self) {}
    }

    struct FakeLaunchctl {
        jobs: String,
        details: String,
    }

    impl LaunchctlReader for FakeLaunchctl {
        fn loaded_jobs(&self) -> Result<String, String> {
            Ok(self.jobs.clone())
        }

        fn job_details(&self, _label: &str) -> Result<String, String> {
            Ok(self.details.clone())
        }
    }

    fn bundle_executable() -> (PathBuf, tempfile::TempDir) {
        let root = tempfile::tempdir().unwrap();
        let executable = root.path().join("CORSIA.app/Contents/MacOS/CORSIA");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, "").unwrap();
        (executable, root)
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
