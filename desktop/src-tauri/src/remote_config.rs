use std::{collections::BTreeMap, fs, path::Path};

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use serde::{Deserialize, Serialize};

use crate::trust::Trust;

const CONFIG_FILE: &str = "config.json";
const CONFIG_TEMP_FILE: &str = "config.json.remote.tmp";

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Locator {
    DnsSd { instance: String },
    Address { host: String, port: u16 },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum RemoteTrust {
    Pinned { spki_sha256: String },
    System,
}

impl From<&Trust> for RemoteTrust {
    fn from(value: &Trust) -> Self {
        match value {
            Trust::Pinned { spki_sha256 } => Self::Pinned {
                spki_sha256: BASE64.encode(spki_sha256),
            },
            Trust::System => Self::System,
        }
    }
}

impl TryFrom<&RemoteTrust> for Trust {
    type Error = String;

    fn try_from(value: &RemoteTrust) -> Result<Self, Self::Error> {
        match value {
            RemoteTrust::System => Ok(Self::System),
            RemoteTrust::Pinned { spki_sha256 } => {
                let decoded = BASE64
                    .decode(spki_sha256)
                    .map_err(|_| "saved remote trust is not valid base64".to_owned())?;
                let bytes: [u8; 32] = decoded
                    .try_into()
                    .map_err(|_| "saved remote trust must contain exactly 32 bytes".to_owned())?;
                Ok(Self::Pinned { spki_sha256: bytes })
            }
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub struct RemoteRecord {
    pub locator: Locator,
    pub trust: RemoteTrust,
    pub name: String,
    pub proxy_port: u16,
}

#[derive(Default, Deserialize, Serialize)]
struct AppConfig {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    remote: Option<RemoteRecord>,
    #[serde(flatten)]
    other: BTreeMap<String, serde_json::Value>,
}

fn config_path(app_dir: &Path) -> std::path::PathBuf {
    app_dir.join(CONFIG_FILE)
}

fn read_config(app_dir: &Path) -> Result<AppConfig, String> {
    let path = config_path(app_dir);
    if !path.exists() {
        return Ok(AppConfig::default());
    }
    let contents = fs::read(&path).map_err(|error| format!("could not read {}: {error}", path.display()))?;
    serde_json::from_slice(&contents).map_err(|error| format!("could not parse {}: {error}", path.display()))
}

fn write_config(app_dir: &Path, config: &AppConfig) -> Result<(), String> {
    fs::create_dir_all(app_dir)
        .map_err(|error| format!("could not create {}: {error}", app_dir.display()))?;
    let bytes = serde_json::to_vec(config).map_err(|error| format!("could not encode remote configuration: {error}"))?;
    let target = config_path(app_dir);
    let temporary = app_dir.join(CONFIG_TEMP_FILE);
    fs::write(&temporary, bytes)
        .map_err(|error| format!("could not write {}: {error}", temporary.display()))?;
    fs::rename(&temporary, &target)
        .map_err(|error| format!("could not replace {}: {error}", target.display()))
}

pub fn load_remote(app_dir: &Path) -> Result<Option<RemoteRecord>, String> {
    Ok(read_config(app_dir)?.remote)
}

pub fn save_remote(app_dir: &Path, remote: RemoteRecord) -> Result<(), String> {
    let mut config = read_config(app_dir)?;
    config.remote = Some(remote);
    write_config(app_dir, &config)
}

/// Removes only the remote record. The caller owns proxy shutdown, webview-data
/// removal and restarting the local service, in the ordering required by §4.
pub fn forget_remote(app_dir: &Path) -> Result<(), String> {
    let mut config = read_config(app_dir)?;
    config.remote = None;
    write_config(app_dir, &config)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record() -> RemoteRecord {
        RemoteRecord {
            locator: Locator::DnsSd { instance: "Studio".into() },
            trust: RemoteTrust::Pinned { spki_sha256: BASE64.encode([7_u8; 32]) },
            name: "Studio".into(),
            proxy_port: 47911,
        }
    }

    #[test]
    fn trust_round_trips_as_standard_base64() {
        let trust = Trust::Pinned { spki_sha256: [7_u8; 32] };
        let saved = RemoteTrust::from(&trust);
        assert_eq!(Trust::try_from(&saved), Ok(trust));
    }

    #[test]
    fn corrupt_pinned_trust_is_rejected_without_system_fallback() {
        for encoded in [BASE64.encode([7_u8; 31]), BASE64.encode([7_u8; 33]), "not base64!".into()] {
            assert!(Trust::try_from(&RemoteTrust::Pinned { spki_sha256: encoded }).is_err());
        }
    }

    #[test]
    fn remote_round_trips_and_forget_preserves_other_config() {
        let temporary = tempfile::tempdir().unwrap();
        fs::write(temporary.path().join(CONFIG_FILE), r#"{"other":"kept"}"#).unwrap();

        save_remote(temporary.path(), record()).unwrap();
        assert_eq!(load_remote(temporary.path()).unwrap(), Some(record()));
        forget_remote(temporary.path()).unwrap();

        let stored: serde_json::Value = serde_json::from_slice(&fs::read(temporary.path().join(CONFIG_FILE)).unwrap()).unwrap();
        assert_eq!(stored, serde_json::json!({"other": "kept"}));
    }

    #[test]
    fn write_uses_rename_and_leaves_no_temporary_file() {
        let temporary = tempfile::tempdir().unwrap();
        save_remote(temporary.path(), record()).unwrap();

        assert!(temporary.path().join(CONFIG_FILE).exists());
        assert!(!temporary.path().join(CONFIG_TEMP_FILE).exists());
    }
}
