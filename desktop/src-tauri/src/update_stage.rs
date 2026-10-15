use std::{
    env,
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

pub const UPDATE_STAGED: i32 = 0;
pub const FETCH_FAILED: i32 = 41;
pub const BUILD_FAILED: i32 = 42;
pub const USAGE_ERROR: i32 = 64;
// The current notarized archive is about 672 MiB. This leaves 50% headroom for
// normal release growth while bounding a malicious or malformed response.
pub const MAX_ARCHIVE_BYTES: u64 = 1_024 * 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const STALL_TIMEOUT: Duration = Duration::from_secs(20);

pub fn update_stage_argument() -> Result<Option<String>, String> {
    parse_update_stage_args(&env::args_os().skip(1).collect::<Vec<_>>())
}

fn parse_update_stage_args(args: &[std::ffi::OsString]) -> Result<Option<String>, String> {
    if args.is_empty() {
        return Ok(None);
    }
    if args.len() == 2 && args[0] == "--update-stage" && !args[1].is_empty() {
        return Ok(Some(args[1].to_string_lossy().into_owned()));
    }
    if args.iter().any(|arg| arg == "--update-stage") {
        return Err(
            "--update-stage requires exactly one operation ID and no other mode flags".into(),
        );
    }
    Ok(None)
}

pub fn run(mut context: tauri::Context<tauri::Wry>, operation_id: String) -> i32 {
    context.config_mut().app.windows.clear();
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    let app = match tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(move |app| {
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
    app.run(|_, _| {});
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
    fs::create_dir(&staging_root).map_err(|error| StageError::Build(error.to_string()))?;

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
    let current = Version::parse(&update.current_version)
        .map_err(|error| StageError::Fetch(format!("invalid installed version: {error}")))?;
    let candidate = Version::parse(&update.version)
        .map_err(|error| StageError::Fetch(format!("invalid candidate version: {error}")))?;
    if candidate <= current {
        return Err(StageError::Fetch(format!(
            "refusing replay or downgrade: candidate {candidate} is not newer than installed {current}"
        )));
    }

    let archive = staging_root.join("CORSIA.app.tar.gz");
    if let Err(error) = download_archive(&update.download_url, &archive).await {
        let _ = fs::remove_file(&archive);
        let _ = fs::remove_dir_all(&staging_root);
        return Err(error);
    }
    if let Err(error) = verify_archive(&archive, &update.signature) {
        let _ = fs::remove_file(&archive);
        return Err(StageError::Fetch(error));
    }
    ensure_same_device(
        current_bundle.parent().unwrap_or(Path::new("/")),
        &staging_root,
    )
    .map_err(StageError::Build)?;
    expand_archive(&archive, &staging_root).map_err(StageError::Build)?;
    let _ = fs::remove_file(&archive);
    let extracted_bundle = staging_root.join("CORSIA.app");
    if !extracted_bundle.is_dir() {
        return Err(StageError::Build(
            "update archive did not contain CORSIA.app".into(),
        ));
    }
    fs::rename(&extracted_bundle, &staged_bundle)
        .map_err(|error| StageError::Build(error.to_string()))?;
    let _ = fs::remove_dir(&staging_root);
    verify_bundle_identity(&current_bundle, &staged_bundle).map_err(StageError::Build)?;
    Ok(staged_bundle)
}

async fn download_archive(url: &reqwest::Url, path: &Path) -> Result<(), StageError> {
    download_archive_with_limits(url, path, MAX_ARCHIVE_BYTES, REQUEST_TIMEOUT, STALL_TIMEOUT).await
}

async fn download_archive_with_limits(
    url: &reqwest::Url,
    path: &Path,
    max_bytes: u64,
    request_timeout: Duration,
    stall_timeout: Duration,
) -> Result<(), StageError> {
    let response = reqwest::Client::builder()
        .timeout(request_timeout)
        .build()
        .map_err(|error| StageError::Fetch(error.to_string()))?
        .get(url.clone())
        .send()
        .await
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
    let mut file = File::create(path).map_err(|error| StageError::Fetch(error.to_string()))?;
    let mut bytes = 0_u64;
    let mut stream = response.bytes_stream();
    loop {
        let next = tokio::time::timeout(stall_timeout, stream.next())
            .await
            .map_err(|_| {
                let _ = fs::remove_file(path);
                StageError::Fetch("archive download stalled".into())
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
    }
    file.sync_all()
        .map_err(|error| StageError::Fetch(error.to_string()))
}

fn verify_archive(archive: &Path, signature: &str) -> Result<(), String> {
    let key = updater_public_key()?;
    let signature_text = base64::engine::general_purpose::STANDARD
        .decode(signature)
        .map_err(|error| error.to_string())?;
    let signature =
        Signature::decode(std::str::from_utf8(&signature_text).map_err(|error| error.to_string())?)
            .map_err(|error| error.to_string())?;
    let mut verifier = key
        .verify_stream(&signature)
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

fn running_bundle() -> Result<PathBuf, String> {
    let executable = env::current_exe().map_err(|error| error.to_string())?;
    executable
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .filter(|path| path.extension().is_some_and(|extension| extension == "app"))
        .map(Path::to_path_buf)
        .ok_or_else(|| "update staging must run from a CORSIA.app bundle".into())
}

fn staged_path(current_bundle: &Path) -> PathBuf {
    current_bundle.with_file_name("CORSIA.app.staged")
}

fn clear_previous_stage(staged: &Path) -> Result<(), String> {
    if staged.exists() {
        fs::remove_dir_all(staged).map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn ensure_same_device(installed_parent: &Path, staged: &Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    let installed = fs::metadata(installed_parent).map_err(|error| error.to_string())?;
    let probe = staged.with_extension("device-probe");
    fs::create_dir(&probe).map_err(|error| error.to_string())?;
    let staged_device = fs::metadata(&probe)
        .map_err(|error| error.to_string())?
        .dev();
    fs::remove_dir(&probe).map_err(|error| error.to_string())?;
    if installed.dev() != staged_device {
        return Err("staging directory is not on the installed app device".into());
    }
    Ok(())
}

fn expand_archive(archive: &Path, parent: &Path) -> Result<(), String> {
    let file = File::open(archive).map_err(|error| error.to_string())?;
    let mut tar = tar::Archive::new(GzDecoder::new(file));
    tar.unpack(parent).map_err(|error| error.to_string())
}

fn verify_bundle_identity(current: &Path, staged: &Path) -> Result<(), String> {
    let current_identity = code_identity(current)?;
    let staged_identity = code_identity(staged)?;
    if current_identity != staged_identity {
        return Err("staged app signing identity differs from running app".into());
    }
    Ok(())
}

fn code_identity(bundle: &Path) -> Result<(String, String, String), String> {
    let verify = Command::new("/usr/bin/codesign")
        .args(["--verify", "--strict"])
        .arg(bundle)
        .output()
        .map_err(|error| error.to_string())?;
    if !verify.status.success() {
        return Err(String::from_utf8_lossy(&verify.stderr).into_owned());
    }
    let output = Command::new("/usr/bin/codesign")
        .args(["--display", "--verbose=4", "--requirements", "-"])
        .arg(bundle)
        .output()
        .map_err(|error| error.to_string())?;
    let text = String::from_utf8_lossy(&output.stderr);
    let field = |name: &str| {
        text.lines()
            .find_map(|line| line.strip_prefix(name).map(str::to_owned))
            .ok_or_else(|| format!("codesign output omitted {name}"))
    };
    Ok((
        field("Identifier=")?,
        field("TeamIdentifier=")?,
        field("designated => ")?,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn update_stage_requires_exactly_one_operation_id() {
        use std::ffi::OsString;

        assert_eq!(
            parse_update_stage_args(&[OsString::from("--update-stage"), OsString::from("op-1")]),
            Ok(Some("op-1".into()))
        );
        assert_eq!(
            parse_update_stage_args(&[OsString::from("--service")]),
            Ok(None)
        );
        assert!(parse_update_stage_args(&[OsString::from("--update-stage")]).is_err());
        assert!(parse_update_stage_args(&[
            OsString::from("--update-stage"),
            OsString::from("op-1"),
            OsString::from("--service"),
        ])
        .is_err());
        assert_eq!(MAX_ARCHIVE_BYTES, 1_024 * 1024 * 1024);
        assert_eq!(
            (UPDATE_STAGED, FETCH_FAILED, BUILD_FAILED, USAGE_ERROR),
            (0, 41, 42, 64)
        );
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
        ));
        assert!(matches!(result, Err(StageError::Fetch(reason)) if reason.contains("size cap")));
        assert!(!archive.exists());
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
            thread::sleep(Duration::from_millis(150));
        });
        let temp = tempfile::tempdir().unwrap();
        let archive = temp.path().join("archive");
        let result = tauri::async_runtime::block_on(download_archive_with_limits(
            &format!("http://{address}/archive").parse().unwrap(),
            &archive,
            3,
            Duration::from_secs(2),
            Duration::from_millis(25),
        ));
        assert!(matches!(result, Err(StageError::Fetch(reason)) if reason.contains("stalled")));
        assert!(!archive.exists());
    }
}
