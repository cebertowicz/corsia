# Desktop app

The macOS desktop app wraps CORSIA's local backend in a Tauri application. This page covers installation, first run, updates and where the app keeps its state.

## Install and first run

The desktop build produces a DMG containing CORSIA.app. The source install instead uses `scripts/install.sh`; see [Getting started](../../README.md#getting-started). The desktop app can also be built from a checkout as described there.

When the app finds an earlier source install, **Take over** uses its existing account, workspace and data with the app. **Start fresh** sets the earlier install aside and starts first run again. Take over is one-way; opening a newer build over an installed one is untested. The source-install instructions describe the limitations and failure recovery.

First run opens setup to create an account and connect a model source, then moves to the first chat. The person chooses the CORSIA agent's model in that chat.

## Updates

The packaged app checks its configured update manifest for a newer version available for this platform. If the manifest cannot be read or is invalid, availability is unknown. Settings offers **Update now** when an update is available.

The app downloads and verifies the signed update and stages it beside the installed app while CORSIA is serving. The full-window update screen shows progress. Before replacing the app, the updater tries to drain running work, stops the service, swaps the app and starts the new service. If draining does not complete, the update can proceed anyway.

If the swap fails, CORSIA tries to start the old version. If the new version fails its startup checks, CORSIA tries to restore the previous app. Rollback can fail; if the update has already upgraded data beyond what the previous version can open, CORSIA keeps the new version instead. The previous app and the pre-upgrade data copy can remain for recovery.

## Data and code

By default, app metadata lives in `~/Library/Application Support/CORSIA` and operational state in its `state` folder. A takeover can retain the source install's existing state location; `CORSIA_STATE_DIR` can also specify a state root.

| Part | Path |
|---|---|
| App and first-run handling | `desktop/src-tauri/src/` |
| Installer | `scripts/install.sh` |
| Setup | `frontend/src/lib/components/SetupScreen.svelte`, `backend/onboarding/` |
| Update availability and staging | `backend/infra/update_status.py`, `desktop/src-tauri/src/update_stage.rs` |
| Update restart and rollback | `scripts/corsia.d/app_mode.sh` |
| App state paths | `desktop/src-tauri/src/app_paths.rs` |

Checked against the code at `7a8c55d84`.
