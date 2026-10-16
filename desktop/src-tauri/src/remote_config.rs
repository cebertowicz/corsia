use std::{collections::BTreeMap, fs, path::Path};

use serde::{Deserialize, Serialize};

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
            trust: RemoteTrust::Pinned { spki_sha256: "YWJj".into() },
            name: "Studio".into(),
            proxy_port: 47911,
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
