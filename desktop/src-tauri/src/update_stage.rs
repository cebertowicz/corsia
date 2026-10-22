use std::{
    env,
    future::Future,
    fs::{self, File},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

use base64::Engine;
use flate2::read::GzDecoder;
use futures_util::StreamExt;
use minisign_verify::{PublicKey, Signature};
use semver::Version;
use tauri_plugin_updater::UpdaterExt;

use crate::app_paths;

pub const UPDATE_STAGED: i32 = 0;
pub const FETCH_FAILED: i32 = 41;
pub const BUILD_FAILED: i32 = 42;
pub const USAGE_ERROR: i32 = 64;
// Another stage or swap holds the update lock (EX_TEMPFAIL).
pub const UPDATE_BUSY: i32 = 75;
// The current notarized archive is about 672 MiB. This leaves 50% headroom for
// normal release growth while bounding a malicious or malformed response.
pub const MAX_ARCHIVE_BYTES: u64 = 1_024 * 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const STALL_TIMEOUT: Duration = Duration::from_secs(20);
const ARCHIVE_CONNECT_TIMEOUT: Duration = Duration::from_secs(20);
const STAGING_HEADROOM_BYTES: u64 = 1024 * 1024 * 1024;

pub fn run(mut context: tauri::Context<tauri::Wry>, operation_id: String) -> i32 {
    // The lock comes before anything else, so a second update never touches
    // the first one's staged copy.
    let state_dir = match app_paths::state_dir() {
        Ok(state_dir) => state_dir,
        Err(reason) => {
            eprintln!("update staging failed: {reason}");
            return BUILD_FAILED;
        }
    };
    let _lock = match app_paths::try_lock_update(&state_dir) {
        Ok(Some(lock)) => lock,
        Ok(None) => {
            eprintln!("update staging refused: another update is in progress");
            return UPDATE_BUSY;
        }
        Err(reason) => {
            eprintln!("update staging failed: {reason}");
            return BUILD_FAILED;
        }
    };
    context.config_mut().app.windows.clear();
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    let app = match tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(move |app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            let handle = app.handle().clone();
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let result = stage(&handle, &operation_id).await;
                let _ = sender.send(result);
                app_handle.exit(0);
            });
            Ok(())
        })
        .build(context)
    {
        Ok(app) => app,
        Err(error) => {
            eprintln!("update staging could not initialize the updater: {error}");
            return FETCH_FAILED;
        }
    };
    let _ = app.run_return(|_, _| {});
    match receiver.recv() {
        Ok(Ok(path)) => {
            println!("{}", path.display());
            UPDATE_STAGED
        }
        Ok(Err(StageError::Fetch(reason))) => {
            eprintln!("update fetch failed: {reason}");
            FETCH_FAILED
        }
        Ok(Err(StageError::Build(reason))) => {
            eprintln!("update staging failed: {reason}");
            BUILD_FAILED
        }
        Err(_) => FETCH_FAILED,
    }
}

#[derive(Debug)]
enum StageError {
    Fetch(String),
    Build(String),
}

async fn stage<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    _operation_id: &str,
) -> Result<PathBuf, StageError> {
    let current_bundle = running_bundle().map_err(StageError::Build)?;
    let staged_bundle = staged_path(&current_bundle);
    let staging_root = staged_bundle.with_extension("work");
    clear_previous_stage(&staged_bundle).map_err(StageError::Build)?;
    clear_previous_stage(&staging_root).map_err(StageError::Build)?;
    // The swap keeps the app it replaces here until the new version is
    // proven; an earlier update's copy is cleared so this one can swap.
    clear_previous_stage(&previous_path(&current_bundle)).map_err(StageError::Build)?;
    fs::create_dir(&staging_root).map_err(|error| StageError::Build(error.to_string()))?;

    let result = stage_in_work(app, &current_bundle, &staged_bundle, &staging_root).await;
    cleanup_after_failure(result, &staged_bundle, &staging_root)
}

async fn stage_in_work<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    current_bundle: &Path,
    staged_bundle: &Path,
    staging_root: &Path,
) -> Result<PathBuf, StageError> {
    let updater = app
        .updater_builder()
        .timeout(REQUEST_TIMEOUT)
        // Return all candidates so an equal/older signed release is a loud refusal.
        .version_comparator(|_, _| true)
        .build()
        .map_err(|error| StageError::Fetch(error.to_string()))?;
    let update = updater
        .check()
        .await
        .map_err(|error| StageError::Fetch(error.to_string()))?
        .ok_or_else(|| StageError::Fetch("release manifest contained no candidate".into()))?;
    let archive = staging_root.join("CORSIA.app.tar.gz");
    download_newer_archive(&update.current_version, &update.version, || {
        download_archive(&update.download_url, &archive)
    })
    .await?;
    verify_archive(&archive, &update.signature).map_err(StageError::Fetch)?;
    ensure_same_device(
        current_bundle.parent().unwrap_or(Path::new("/")),
        staging_root,
    )
    .map_err(StageError::Build)?;
    stage_extracted_bundle(
        current_bundle,
        &archive,
        staging_root,
        staged_bundle,
        expand_archive,
        verify_bundle_identity,
        |extracted| {
            ensure_staged_bundle_version(&update.current_version, &update.version, extracted)
                .map_err(StageError::Fetch)
        },
    )
}

fn cleanup_after_failure<T>(
    result: Result<T, StageError>,
    staged_bundle: &Path,
    staging_root: &Path,
) -> Result<T, StageError> {
    if result.is_err() {
        let _ = fs::remove_dir_all(staging_root);
        let _ = fs::remove_dir_all(staged_bundle);
    }
    result
}

fn stage_extracted_bundle<E, V, C>(
    current_bundle: &Path,
    archive: &Path,
    staging_root: &Path,
    staged_bundle: &Path,
    expand: E,
    verify_identity: V,
    verify_version: C,
) -> Result<PathBuf, StageError>
where
    E: FnOnce(&Path, &Path) -> Result<(), String>,
    V: FnOnce(&Path, &Path) -> Result<(), String>,
    C: FnOnce(&Path) -> Result<(), StageError>,
{
    expand(archive, staging_root).map_err(StageError::Build)?;
    fs::remove_file(archive).map_err(|error| StageError::Build(error.to_string()))?;
    let extracted_bundle = staging_root.join("CORSIA.app");
    if !extracted_bundle.is_dir() {
        return Err(StageError::Build("update archive did not contain CORSIA.app".into()));
    }
    verify_version(&extracted_bundle)?;
    verify_identity(current_bundle, &extracted_bundle).map_err(StageError::Build)?;
    fs::rename(&extracted_bundle, staged_bundle)
        .map_err(|error| StageError::Build(error.to_string()))?;
    fs::remove_dir(staging_root).map_err(|error| StageError::Build(error.to_string()))?;
    Ok(staged_bundle.to_path_buf())
}

async fn download_newer_archive<F, Fut>(
    current_version: &str,
    candidate_version: &str,
    download: F,
) -> Result<(), StageError>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = Result<(), StageError>>,
{
    let is_newer = candidate_is_newer(current_version, candidate_version).map_err(|error| {
        match error {
            StageError::Fetch(reason) => {
                StageError::Fetch(format!("refusing replay or downgrade: {reason}"))
            }
            StageError::Build(reason) => StageError::Build(reason),
        }
    })?;
    if !is_newer {
        return Err(StageError::Fetch(format!(
            "refusing replay or downgrade: candidate {candidate_version} is not newer than installed {current_version}"
        )));
    }
    download().await
}

async fn download_archive(url: &reqwest::Url, path: &Path) -> Result<(), StageError> {
    download_archive_with_limits(
        url,
        path,
        MAX_ARCHIVE_BYTES,
        ARCHIVE_CONNECT_TIMEOUT,
        STALL_TIMEOUT,
        available_disk_space,
    )
    .await
}

async fn download_archive_with_limits<F>(
    url: &reqwest::Url,
    path: &Path,
    max_bytes: u64,
    connect_timeout: Duration,
    stall_timeout: Duration,
    available_space: F,
) -> Result<(), StageError>
where
    F: FnOnce(&Path) -> Result<u64, String>,
{
    let client = reqwest::Client::builder()
        .connect_timeout(connect_timeout)
        .build()
        .map_err(|error| StageError::Fetch(error.to_string()))?;
    let response = tokio::time::timeout(stall_timeout, client.get(url.clone()).send())
        .await
        .map_err(|_| StageError::Fetch(format!("no response within {stall_timeout:?}")))?
        .map_err(|error| StageError::Fetch(error.to_string()))?;
    if !response.status().is_success() {
        return Err(StageError::Fetch(format!(
            "archive request returned {}",
            response.status()
        )));
    }
    if response
        .content_length()
        .is_some_and(|size| size > max_bytes)
    {
        return Err(StageError::Fetch(
            "archive Content-Length exceeds size cap".into(),
        ));
    }
    let staging_parent = path
        .parent()
        .ok_or_else(|| StageError::Fetch("archive path has no staging parent".into()))?;
    let content_length = response.content_length();
    ensure_staging_capacity(
        content_length.unwrap_or(max_bytes),
        available_space(staging_parent).map_err(StageError::Fetch)?,
    )
    .map_err(StageError::Fetch)?;
    let mut file = File::create(path).map_err(|error| StageError::Fetch(error.to_string()))?;
    let mut bytes = 0_u64;
    let mut stream = response.bytes_stream();
    loop {
        let next = tokio::time::timeout(stall_timeout, stream.next())
            .await
            .map_err(|_| {
                let _ = fs::remove_file(path);
                StageError::Fetch(format!("archive download stalled after {bytes} bytes"))
            })?;
        let Some(chunk) = next else { break };
        let chunk = chunk.map_err(|error| {
            let _ = fs::remove_file(path);
            StageError::Fetch(error.to_string())
        })?;
        bytes = bytes
            .checked_add(chunk.len() as u64)
            .ok_or_else(|| StageError::Fetch("archive size overflow".into()))?;
        if bytes > max_bytes {
            let _ = fs::remove_file(path);
            return Err(StageError::Fetch("archive exceeded size cap".into()));
        }
        file.write_all(&chunk)
            .map_err(|error| StageError::Fetch(error.to_string()))?;
        if content_length.is_some_and(|expected| bytes == expected) {
            break;
        }
    }
    file.sync_all()
        .map_err(|error| StageError::Fetch(error.to_string()))
}

fn candidate_is_newer(current: &str, candidate: &str) -> Result<bool, StageError> {
    let current = Version::parse(current)
        .map_err(|error| StageError::Fetch(format!("invalid installed version: {error}")))?;
    let candidate = Version::parse(candidate)
        .map_err(|error| StageError::Fetch(format!("invalid candidate version: {error}")))?;
    Ok(candidate > current)
}

fn ensure_staged_bundle_version(
    installed_version: &str,
    manifest_version: &str,
    extracted_bundle: &Path,
) -> Result<(), String> {
    let installed = Version::parse(installed_version)
        .map_err(|error| format!("invalid installed version: {error}"))?;
    let manifest = Version::parse(manifest_version)
        .map_err(|error| format!("invalid candidate version: {error}"))?;
    let bundle = bundle_short_version(extracted_bundle)?;
    if bundle != manifest {
        return Err(format!(
            "refusing update: signed bundle version {bundle} does not match manifest {manifest}"
        ));
    }
    if bundle <= installed {
        return Err(format!(
            "refusing replay or downgrade: signed bundle version {bundle} is not newer than installed {installed}"
        ));
    }
    Ok(())
}

fn bundle_short_version(bundle: &Path) -> Result<Version, String> {
    let plist_path = bundle.join("Contents/Info.plist");
    let plist = plist::Value::from_file(&plist_path).map_err(|error| error.to_string())?;
    let version = plist
        .as_dictionary()
        .and_then(|dictionary| dictionary.get("CFBundleShortVersionString"))
        .and_then(plist::Value::as_string)
        .ok_or_else(|| "bundle Info.plist omitted CFBundleShortVersionString".to_string())?;
    Version::parse(version).map_err(|error| format!("invalid bundle version: {error}"))
}

fn ensure_staging_capacity(archive_bytes: u64, available_bytes: u64) -> Result<(), String> {
    let required_bytes = archive_bytes
        .checked_mul(4)
        .and_then(|bytes| bytes.checked_add(STAGING_HEADROOM_BYTES))
        .ok_or_else(|| "archive size exceeds staging capacity calculation".to_string())?;
    if available_bytes < required_bytes {
        return Err(format!(
            "not enough free disk space: need {required_bytes}, have {available_bytes}"
        ));
    }
    Ok(())
}

fn available_disk_space(path: &Path) -> Result<u64, String> {
    use std::{ffi::CString, mem::MaybeUninit, os::unix::ffi::OsStrExt};

    let path = CString::new(path.as_os_str().as_bytes()).map_err(|error| error.to_string())?;
    let mut stat = MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    let stat = unsafe { stat.assume_init() };
    (stat.f_bavail as u64)
        .checked_mul(stat.f_frsize as u64)
        .ok_or_else(|| "free disk space value overflowed".to_string())
}

fn verify_archive(archive: &Path, signature: &str) -> Result<(), String> {
    let key = updater_public_key()?;
    let signature = decode_updater_signature(signature)?;
    verify_archive_with_key(archive, &key, &signature)
}

fn decode_updater_signature(signature: &str) -> Result<Signature, String> {
    let signature_text = base64::engine::general_purpose::STANDARD
        .decode(signature)
        .map_err(|error| error.to_string())?;
    Signature::decode(std::str::from_utf8(&signature_text).map_err(|error| error.to_string())?)
        .map_err(|error| error.to_string())
}

fn verify_archive_with_key(
    archive: &Path,
    key: &PublicKey,
    signature: &Signature,
) -> Result<(), String> {
    let mut verifier = key
        .verify_stream(signature)
        .map_err(|error| error.to_string())?;
    let mut file = File::open(archive).map_err(|error| error.to_string())?;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer).map_err(|error| error.to_string())?;
        if read == 0 {
            break;
        }
        verifier.update(&buffer[..read]);
    }
    verifier.finalize().map_err(|error| error.to_string())
}

fn updater_public_key() -> Result<PublicKey, String> {
    let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json"))
        .map_err(|error| error.to_string())?;
    let encoded = config["plugins"]["updater"]["pubkey"]
        .as_str()
        .ok_or_else(|| "updater public key missing from bundle config".to_string())?;
    let text = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .map_err(|error| error.to_string())?;
    PublicKey::decode(std::str::from_utf8(&text).map_err(|error| error.to_string())?)
        .map_err(|error| error.to_string())
}

pub(crate) fn running_bundle() -> Result<PathBuf, String> {
    let executable = env::current_exe().map_err(|error| error.to_string())?;
    executable
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .filter(|path| path.extension().is_some_and(|extension| extension == "app"))
        .map(Path::to_path_buf)
        .ok_or_else(|| "update staging must run from a CORSIA.app bundle".into())
}

pub(crate) fn staged_path(current_bundle: &Path) -> PathBuf {
    current_bundle.with_file_name("CORSIA.app.staged")
}

pub(crate) fn previous_path(current_bundle: &Path) -> PathBuf {
    current_bundle.with_file_name("CORSIA.app.previous")
}

fn clear_previous_stage(staged: &Path) -> Result<(), String> {
    if staged.exists() {
        fs::remove_dir_all(staged).map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn ensure_same_device(installed_parent: &Path, staging_root: &Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;

    let installed = fs::metadata(installed_parent).map_err(|error| error.to_string())?;
    let staging = fs::metadata(staging_root).map_err(|error| error.to_string())?;
    if installed.dev() != staging.dev() {
        return Err("staging directory is not on the installed app device".into());
    }
    Ok(())
}

fn expand_archive(archive: &Path, parent: &Path) -> Result<(), String> {
    let file = File::open(archive).map_err(|error| error.to_string())?;
    let mut tar = tar::Archive::new(GzDecoder::new(file));
    tar.unpack(parent).map_err(|error| error.to_string())
}

#[derive(Debug, Eq, PartialEq)]
struct SigningIdentity {
    identifier: String,
    team_id: String,
    requirement: String,
}

pub(crate) fn verify_bundle_identity(current: &Path, staged: &Path) -> Result<(), String> {
    // The installed app's seal is not re-verified: a file written into it after install (a
    // __pycache__) breaks the seal, and refusing to update is then the one thing that keeps
    // the broken copy. Its identity still has to match; the staged app is verified in full.
    let current_identity = code_identity(current, false)?;
    let staged_identity = code_identity(staged, true)?;
    ensure_matching_identity(&current_identity, &staged_identity)
}

fn ensure_matching_identity(
    current: &SigningIdentity,
    staged: &SigningIdentity,
) -> Result<(), String> {
    if current.identifier != staged.identifier {
        return Err("staged app signing identifier differs from running app".into());
    }
    if current.team_id != staged.team_id {
        return Err("staged app signing Team ID differs from running app".into());
    }
    if current.requirement != staged.requirement {
        return Err("staged app designated requirement differs from running app".into());
    }
    Ok(())
}

fn code_identity(bundle: &Path, verify_seal: bool) -> Result<SigningIdentity, String> {
    if verify_seal {
        let verify = Command::new("/usr/bin/codesign")
            .args(["--verify", "--strict"])
            .arg(bundle)
            .output()
            .map_err(|error| error.to_string())?;
        if !verify.status.success() {
            return Err(String::from_utf8_lossy(&verify.stderr).into_owned());
        }
    }
    let output = Command::new("/usr/bin/codesign")
        .args(["--display", "--verbose=4", "--requirements", "-"])
        .arg(bundle)
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    let identity = parse_code_identity(
        &String::from_utf8_lossy(&output.stdout),
        &String::from_utf8_lossy(&output.stderr),
    )?;
    ensure_developer_id_signature(&identity)?;
    Ok(identity)
}

fn parse_code_identity(stdout: &str, stderr: &str) -> Result<SigningIdentity, String> {
    let stderr_field = |name: &str| {
        stderr
            .lines()
            .find_map(|line| line.strip_prefix(name).map(str::to_owned))
            .ok_or_else(|| format!("codesign output omitted {name}"))
    };
    let identifier = stderr_field("Identifier=")?;
    let team_id = stderr_field("TeamIdentifier=")?;
    let requirement = stdout
        .lines()
        .map(str::trim_start)
        .map(|line| line.strip_prefix("# ").unwrap_or(line))
        .find_map(|line| line.strip_prefix("designated => ").map(str::to_owned))
        .ok_or_else(|| "codesign output omitted designated requirement".to_string())?;
    Ok(SigningIdentity {
        identifier,
        team_id,
        requirement,
    })
}

fn ensure_developer_id_signature(identity: &SigningIdentity) -> Result<(), String> {
    if identity.team_id == "not set" {
        return Err("bundle is not Developer ID signed".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stage_limits_and_exit_codes_are_stable() {
        assert_eq!(MAX_ARCHIVE_BYTES, 1_024 * 1024 * 1024);
        assert_eq!(ARCHIVE_CONNECT_TIMEOUT, Duration::from_secs(20));
        assert_eq!(
            (UPDATE_STAGED, FETCH_FAILED, BUILD_FAILED, USAGE_ERROR, UPDATE_BUSY),
            (0, 41, 42, 64, 75)
        );
    }

    const TEST_SIGNER_PUBLIC_KEY: &str =
        "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3";
    const TEST_SIGNED_ARCHIVE: &str = concat!(
        "untrusted comment: signature from minisign secret key\n",
        "RUQf6LRCGA9i559r3g7V1qNyJDApGip8MfqcadIgT9CuhV3EMhHoN1mGTkUidF/",
        "z7SrlQgXdy8ofjb7bNJJylDOocrCo8KLzZwo=\n",
        "trusted comment: timestamp:1556193335\tfile:test\n",
        "y/rUw2y8/hOUYjZU71eHp/Wo1KZ40fGy2VJEDl34XMJM+TX48Ss/17u3IvIfbVR1FkZZSNCisQbuQY+bHwhEBg==",
    );

    fn test_signature() -> Signature {
        Signature::decode(TEST_SIGNED_ARCHIVE).unwrap()
    }

    #[test]
    fn signed_archive_requires_the_pinned_signer_and_exact_contents() {
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("archive");
        fs::write(&archive, b"test").unwrap();
        let signer = PublicKey::from_base64(TEST_SIGNER_PUBLIC_KEY).unwrap();
        let signature = test_signature();

        assert!(verify_archive_with_key(&archive, &signer, &signature).is_ok());
        fs::write(&archive, b"Test").unwrap();
        assert!(verify_archive_with_key(&archive, &signer, &signature).is_err());

        fs::write(&archive, b"test").unwrap();
        let other_signer = updater_public_key().unwrap();
        assert!(verify_archive_with_key(&archive, &other_signer, &signature).is_err());
    }

    #[test]
    fn updater_signature_encoding_must_be_valid_base64_and_minisign() {
        assert!(decode_updater_signature("not base64!").is_err());
        let encoded = base64::engine::general_purpose::STANDARD.encode(TEST_SIGNED_ARCHIVE);
        assert!(decode_updater_signature(&encoded).is_ok());
    }

    fn write_bundle_version(bundle: &Path, version: &str) {
        let contents = bundle.join("Contents");
        fs::create_dir_all(&contents).unwrap();
        fs::write(
            contents.join("Info.plist"),
            format!(
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>CFBundleShortVersionString</key><string>{version}</string></dict></plist>"
            ),
        )
        .unwrap();
    }

    #[test]
    fn staged_bundle_version_must_match_the_manifest_and_exceed_installed() {
        let temp = tempfile::tempdir().unwrap();
        let bundle = temp.path().join("CORSIA.app");

        write_bundle_version(&bundle, "1.2.4");
        assert!(ensure_staged_bundle_version("1.2.3", "1.2.4", &bundle).is_ok());

        write_bundle_version(&bundle, "1.2.2");
        assert!(matches!(
            ensure_staged_bundle_version("1.2.3", "1.2.4", &bundle),
            Err(reason) if reason.contains("does not match")
        ));

        write_bundle_version(&bundle, "1.2.3");
        assert!(matches!(
            ensure_staged_bundle_version("1.2.3", "1.2.4", &bundle),
            Err(reason) if reason.contains("does not match")
        ));
        assert!(matches!(
            ensure_staged_bundle_version("1.2.3", "1.2.3", &bundle),
            Err(reason) if reason.contains("not newer")
        ));

        write_bundle_version(&bundle, "1.2.4");
        assert!(matches!(
            ensure_staged_bundle_version("1.2.3", "1.2.5", &bundle),
            Err(reason) if reason.contains("does not match")
        ));
        write_bundle_version(&bundle, "not-a-version");
        assert!(matches!(
            ensure_staged_bundle_version("1.2.3", "1.2.4", &bundle),
            Err(reason) if reason.contains("invalid bundle version")
        ));
    }

    #[test]
    fn stale_or_invalid_manifest_is_refused_before_archive_download() {
        for candidate in ["1.2.3", "1.2.2", "not-a-version"] {
            let result = tauri::async_runtime::block_on(download_newer_archive(
                "1.2.3",
                candidate,
                || async { Err(StageError::Build("download was called".into())) },
            ));
            assert!(matches!(result, Err(StageError::Fetch(_))), "{candidate}");
        }
    }

    fn developer_identity() -> SigningIdentity {
        parse_code_identity(
            "designated => identifier \"com.cebertowicz.corsia\" and anchor apple generic and certificate leaf[subject.OU] = \"TEAMID\"\n",
            "Identifier=com.cebertowicz.corsia\nTeamIdentifier=TEAMID\n",
        )
        .unwrap()
    }

    #[test]
    fn code_identity_parses_codesign_streams_and_rejects_ad_hoc_signatures() {
        let ad_hoc = parse_code_identity(
            "# designated => cdhash H\"e16c...\"\n",
            "Identifier=corsia_desktop-...\nTeamIdentifier=not set\n",
        )
        .unwrap();
        assert_eq!(ad_hoc.identifier, "corsia_desktop-...");
        assert_eq!(ad_hoc.team_id, "not set");
        assert_eq!(ad_hoc.requirement, "cdhash H\"e16c...\"");
        assert!(matches!(ensure_developer_id_signature(&ad_hoc), Err(reason) if reason.contains("not Developer ID signed")));

        assert_eq!(
            developer_identity(),
            SigningIdentity {
                identifier: "com.cebertowicz.corsia".into(),
                team_id: "TEAMID".into(),
                requirement: "identifier \"com.cebertowicz.corsia\" and anchor apple generic and certificate leaf[subject.OU] = \"TEAMID\"".into(),
            }
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ad_hoc_codesign_bundle_exposes_all_identity_fields_before_rejection() {
        let temp = tempfile::tempdir().unwrap();
        let bundle = temp.path().join("CORSIA.app");
        let macos = bundle.join("Contents/MacOS");
        fs::create_dir_all(&macos).unwrap();
        fs::write(
            bundle.join("Contents/Info.plist"),
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>CORSIA</string></dict></plist>",
        )
        .unwrap();
        fs::copy(env::current_exe().unwrap(), macos.join("CORSIA")).unwrap();

        let signed = Command::new("/usr/bin/codesign")
            .args(["--force", "--sign", "-"])
            .arg(&bundle)
            .output()
            .unwrap();
        assert!(signed.status.success(), "{:?}", signed.stderr);
        let displayed = Command::new("/usr/bin/codesign")
            .args(["--display", "--verbose=4", "--requirements", "-"])
            .arg(&bundle)
            .output()
            .unwrap();
        assert!(displayed.status.success(), "{:?}", displayed.stderr);

        let identity = parse_code_identity(
            &String::from_utf8_lossy(&displayed.stdout),
            &String::from_utf8_lossy(&displayed.stderr),
        )
        .unwrap();
        assert!(!identity.identifier.is_empty());
        assert!(!identity.team_id.is_empty());
        assert!(!identity.requirement.is_empty());
        assert!(ensure_developer_id_signature(&identity).is_err());
    }

    #[test]
    fn code_identity_rejects_each_identity_mismatch() {
        let current = developer_identity();
        let mut staged = developer_identity();
        staged.identifier = "other.identifier".into();
        assert!(matches!(ensure_matching_identity(&current, &staged), Err(reason) if reason.contains("identifier")));

        let mut staged = developer_identity();
        staged.team_id = "OTHERTEAM".into();
        assert!(matches!(ensure_matching_identity(&current, &staged), Err(reason) if reason.contains("Team ID")));

        let mut staged = developer_identity();
        staged.requirement = "different requirement".into();
        assert!(matches!(ensure_matching_identity(&current, &staged), Err(reason) if reason.contains("requirement")));
    }

    #[test]
    fn staging_compares_existing_real_directories_without_a_probe() {
        let temp = tempfile::tempdir().unwrap();
        let installed_parent = temp.path().join("Applications");
        let staging_root = temp.path().join("CORSIA.app.work");
        fs::create_dir(&installed_parent).unwrap();
        fs::create_dir(&staging_root).unwrap();

        assert!(ensure_same_device(&installed_parent, &staging_root).is_ok());
        assert!(!staging_root.with_extension("device-probe").exists());
    }

    #[test]
    fn expand_or_identity_failures_remove_work_and_staged_paths() {
        let temp = tempfile::tempdir().unwrap();
        let current = temp.path().join("CORSIA.app");
        fs::create_dir(&current).unwrap();

        for failure in ["expand", "identity"] {
            let staging_root = temp.path().join(format!("{failure}.work"));
            let staged = temp.path().join(format!("{failure}.staged"));
            let archive = staging_root.join("archive");
            fs::create_dir(&staging_root).unwrap();
            fs::write(&archive, b"archive").unwrap();

            let result = if failure == "expand" {
                stage_extracted_bundle(
                    &current,
                    &archive,
                    &staging_root,
                    &staged,
                    |_, _| Err("forced expand failure".into()),
                    |_, _| Ok(()),
                    |_| Ok(()),
                )
            } else {
                stage_extracted_bundle(
                    &current,
                    &archive,
                    &staging_root,
                    &staged,
                    |_, parent| {
                        fs::create_dir(parent.join("CORSIA.app"))
                            .map_err(|error| error.to_string())
                    },
                    |_, extracted| {
                        assert!(extracted.is_dir());
                        assert!(!staged.exists());
                        Err("forced identity failure".into())
                    },
                    |_| Ok(()),
                )
            };
            let result = cleanup_after_failure(result, &staged, &staging_root);

            assert!(result.is_err());
            assert!(!staging_root.exists());
            assert!(!staged.exists());
        }
    }

    #[test]
    fn extracted_version_refusal_removes_work_and_staged_paths() {
        let temp = tempfile::tempdir().unwrap();
        let current = temp.path().join("CORSIA.app");
        let staging_root = temp.path().join("CORSIA.app.work");
        let staged = temp.path().join("CORSIA.app.staged");
        let archive = staging_root.join("archive");
        fs::create_dir(&current).unwrap();
        fs::create_dir(&staging_root).unwrap();
        fs::write(&archive, b"archive").unwrap();

        let result = stage_extracted_bundle(
            &current,
            &archive,
            &staging_root,
            &staged,
            |_, parent| {
                write_bundle_version(&parent.join("CORSIA.app"), "1.2.2");
                Ok(())
            },
            |_, _| Ok(()),
            |bundle| {
                ensure_staged_bundle_version("1.2.3", "1.2.4", bundle)
                    .map_err(StageError::Fetch)
            },
        );
        let result = cleanup_after_failure(result, &staged, &staging_root);

        assert!(matches!(result, Err(StageError::Fetch(reason)) if reason.contains("does not match")));
        assert!(!staging_root.exists());
        assert!(!staged.exists());
    }

    #[test]
    fn download_cap_uses_received_bytes_not_content_length() {
        use std::{io::Read, net::TcpListener, thread};

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nABCD")
                .unwrap();
        });
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("archive");
        let result = tauri::async_runtime::block_on(download_archive_with_limits(
            &format!("http://{address}/archive").parse().unwrap(),
            &archive,
            3,
            Duration::from_secs(2),
            Duration::from_secs(1),
            |_| Ok(u64::MAX),
        ));
        assert!(matches!(result, Err(StageError::Fetch(reason)) if reason.contains("size cap")));
        assert!(!archive.exists());
    }

    #[test]
    fn response_header_stall_refuses_without_creating_archive() {
        use std::{io::Read, net::TcpListener, thread};

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 1024];
            let _ = stream.read(&mut request);
            // Stay silent until the client gives up and hangs up.
            let _ = stream.read(&mut request);
        });
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("archive");
        let result = tauri::async_runtime::block_on(download_archive_with_limits(
            &format!("http://{address}/archive").parse().unwrap(),
            &archive,
            3,
            Duration::from_secs(1),
            Duration::from_millis(25),
            |_| Ok(u64::MAX),
        ));

        assert!(
            matches!(&result, Err(StageError::Fetch(reason)) if reason.contains("no response")),
            "{result:?}"
        );
        assert!(!archive.exists());
    }

    #[test]
    fn insufficient_staging_space_refuses_before_creating_archive() {
        use std::{io::Read, net::TcpListener, thread};

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nABCD")
                .unwrap();
        });
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("CORSIA.app.work/archive");
        fs::create_dir(archive.parent().unwrap()).unwrap();
        let result = tauri::async_runtime::block_on(download_archive_with_limits(
            &format!("http://{address}/archive").parse().unwrap(),
            &archive,
            16,
            Duration::from_secs(1),
            Duration::from_secs(1),
            |_| Ok(0),
        ));
        let staged = archive.parent().unwrap().with_extension("staged");
        let result = cleanup_after_failure(result, &staged, archive.parent().unwrap());

        assert!(matches!(result, Err(StageError::Fetch(reason)) if reason.contains("not enough free disk space")));
        assert!(!archive.exists());
        assert!(!archive.parent().unwrap().exists());
        assert!(!staged.exists());
    }

    #[test]
    fn slow_progressing_download_outlives_old_total_timeout() {
        use std::{
            io::Read,
            net::{Shutdown, TcpListener},
            thread,
        };

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n1\r\nA\r\n")
                .unwrap();
            stream.flush().unwrap();
            thread::sleep(Duration::from_millis(200));
            stream.write_all(b"1\r\nB\r\n").unwrap();
            stream.flush().unwrap();
            thread::sleep(Duration::from_millis(200));
            stream.write_all(b"1\r\nC\r\n0\r\n\r\n").unwrap();
            stream.flush().unwrap();
            stream.shutdown(Shutdown::Write).unwrap();
        });
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("archive");
        let result = tauri::async_runtime::block_on(download_archive_with_limits(
            &format!("http://{address}/archive").parse().unwrap(),
            &archive,
            3,
            Duration::from_millis(20),
            Duration::from_secs(1),
            |_| Ok(u64::MAX),
        ));

        assert!(result.is_ok(), "{result:?}");
        assert_eq!(fs::read(&archive).unwrap(), b"ABC");
    }

    #[test]
    fn download_stall_removes_partial_archive() {
        use std::{io::Read, net::TcpListener, thread};

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\n\r\nA")
                .unwrap();
            // Stay silent until the client gives up and hangs up.
            let _ = stream.read(&mut request);
        });
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("archive");
        let result = tauri::async_runtime::block_on(download_archive_with_limits(
            &format!("http://{address}/archive").parse().unwrap(),
            &archive,
            3,
            Duration::from_secs(2),
            Duration::from_secs(1),
            |_| Ok(u64::MAX),
        ));
        assert!(
            matches!(&result, Err(StageError::Fetch(reason)) if reason.contains("stalled")),
            "{result:?}"
        );
        assert!(!archive.exists());
    }
}
