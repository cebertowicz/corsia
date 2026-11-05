# Configuration reference

This page lists names fixed at environment reads in code, including constants passed to a local function one call away. Dynamic names are omitted. Generate it with `python scripts/docs/generate_config.py`; add `--check` to compare the page.

Defaults show fallbacks at read sites. `unset` means no fallback is given; `computed` means the fallback is an expression. Multiple values come from different read sites.

## CORSIA settings

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `BACKEND_LOG` | `empty`, `'the backend log'`, `computed` | Sets the backend log path in the launcher. | `scripts/corsia`, `scripts/corsia.d/app_mode.sh`, `scripts/corsia.d/runtime.sh` |
| `BACKEND_PORT` | `empty`, `computed`, `unset` | Sets the backend port. | `backend/api/setup.py`, `backend/api/system_admin.py`, `backend/automation/update_schedule.py`, `backend/cli_sources.py`, `backend/connections/google/oauth.py`, `backend/subagents/harness_tool_bridge.py`, `scripts/corsia`, `scripts/corsia.d/app_mode.sh` |
| `CORSIA_ANALYTICS_DISABLE` | `'0'` | Disables product analytics when set to 1. | `backend/infra/product_events.py` |
| `CORSIA_ANALYTICS_PROJECT` | `'corsia'` | Sets the project identifier for product events. | `backend/infra/product_events.py` |
| `CORSIA_API_BASE` | `unset` | Sets the CORSIA API base URL for CLI clients. | `backend/cli_sources.py`, `backend/scripts/corsia_api.py` |
| `CORSIA_API_TOKEN` | `empty`, `unset` | Supplies an API token to CLI clients. | `backend/scripts/corsia_api.py`, `scripts/corsia.d/delegate.sh` |
| `CORSIA_API_TOKEN_FILE` | `empty`, `unset` | Selects the file containing the CLI API token. | `backend/scripts/corsia_api.py`, `scripts/corsia.d/delegate.sh` |
| `CORSIA_BACKUP_FREE_FLOOR_BYTES` | `computed` | Sets the free-space floor for migration backups. | `backend/infra/database_migrations.py` |
| `CORSIA_CLIENT_METADATA_URL` | `empty` | Sets the OAuth client metadata document URL. | `backend/infra/config.py` |
| `CORSIA_CORRECTIONS_ASSET_BASE_URL` | `unset` | Sets the download URL for correction assets. | `backend/core/corrections_asset_download.py` |
| `CORSIA_CORRECTIONS_ASSET_DIR` | `unset` | Selects the corrections asset directory. | `backend/core/corrections_asset_files.py` |
| `CORSIA_DB_LOOP_WRITE_STRICT` | `unset` | Makes database writes on the event loop raise an error. | `backend/infra/db_loop_guard.py` |
| `CORSIA_DB_POOL_WORKERS` | `empty` | Sets the asynchronous database worker count. | `backend/infra/db_async.py` |
| `CORSIA_DEVICES_DISABLE` | `unset` | Disables the devices listener when set to 1. | `backend/core/devices_runtime.py` |
| `CORSIA_DIR` | `empty` | Specifies the launcher checkout, which must match the script checkout. | `scripts/corsia` |
| `CORSIA_DRAIN_MAX_WAIT` | `empty`, `'60'` | Sets how long shutdown waits for active work to reach a safe point, in seconds. | `backend/infra/drain.py`, `scripts/corsia.d/runtime.sh` |
| `CORSIA_ENV_FILE` | `empty` | Selects the launcher environment file. | `scripts/corsia` |
| `CORSIA_GH_AGENT_CONFIG_DIR` | `unset` | Selects the agents' GitHub account directory; an empty value turns the account off. | `backend/connections/github_agent_home.py` |
| `CORSIA_GITHUB_HEAD_CHECKS_ENABLE` | `unset` | Enables GitHub pull request head check events when set to 1. | `backend/events/connectors/github_head_checks.py` |
| `CORSIA_GITHUB_NOTIFICATIONS_DISABLE` | `unset` | Disables GitHub notifications polling when set to 1. | `backend/events/connectors/github_notifications.py` |
| `CORSIA_GITHUB_REPO_ACTIVITY_DISABLE` | `unset` | Disables GitHub repository activity polling when set to 1. | `backend/events/connectors/github_repo_activity.py` |
| `CORSIA_GIT_LOCK_STALE_SECONDS` | `'5'` | Sets how many seconds old a Git lock must be before it may be removed. | `scripts/corsia.d/git_lock_recovery.sh` |
| `CORSIA_HEALTH_WAIT_SECONDS` | `'60'`, `unset` | Sets the startup health wait in seconds. | `scripts/corsia.d/runtime.sh` |
| `CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS` | `'15'` | Sets the number of launchd bootstrap attempts. | `scripts/corsia.d/service.sh` |
| `CORSIA_MAIL_POLLER_DISABLE` | `unset` | Disables mail polling when set to 1. | `backend/events/connectors/mail_poller.py` |
| `CORSIA_MODEL_CATALOG_URL` | `'https://models.dev/api.json'` | Sets the URL for a mirror of the public model catalog. | `backend/llm/public_model_catalog.py` |
| `CORSIA_OTLP_PORT` | `empty` | Sets the local port that receives measures from external tools. | `backend/infra/otlp_listener.py` |
| `CORSIA_PRE_MIGRATION_BACKUPS_KEEP` | `'2'` | Sets how many pre-migration backups to keep. | `backend/infra/database_migrations.py` |
| `CORSIA_PROVIDER_STREAM_CAPTURE_DIR` | `unset` | Sets the directory for captured provider streams. | `backend/subagents/stream_capture.py` |
| `CORSIA_PROVIDER_STREAM_CAPTURE_THREADS` | `unset` | Selects chats whose provider streams are captured. | `backend/subagents/stream_capture.py` |
| `CORSIA_PR_QUIET_WATCH_DISABLE` | `unset` | Disables the GitHub pull request quiet watch when set to 1. | `backend/events/connectors/github_pr_quiet_watch.py` |
| `CORSIA_PYTHON` | `empty` | Selects the Python executable used by the launcher. | `scripts/corsia` |
| `CORSIA_RESTART_JITTER_SECONDS` | `'30'` | Sets the random delay applied to restart timing, in seconds. | `backend/automation/continuation/restart_jitter.py` |
| `CORSIA_SERVICE_LABEL` | `'com.corsia.prod'`, `unset` | Sets the service label. | `scripts/corsia.d/service.sh`, `scripts/corsia.d/systemd.sh` |
| `CORSIA_SERVICE_MANAGER` | `empty` | Selects the service manager. | `scripts/corsia.d/runtime.sh` |
| `CORSIA_SERVICE_PATH` | `unset` | Overrides the executable search path used for the service. | `backend/cli_location.py` |
| `CORSIA_SILENT_CHILD_SECONDS` | `'1200'` | Sets how many seconds a delegated worker can go quiet before its parent is notified. | `backend/plans/delegation_child_lifecycle.py` |
| `CORSIA_STARTUP_CEILING_SECONDS` | `'900'` | Sets the maximum startup wait in seconds. | `scripts/corsia.d/runtime.sh` |
| `CORSIA_STARTUP_SECONDS_PER_GIB` | `'60'` | Sets the startup allowance per GiB in seconds. | `scripts/corsia.d/runtime.sh` |
| `CORSIA_STATE_DIR` | `empty`, `computed`, `unset` | Selects the runtime state directory. | `backend/cli.py`, `backend/eval/host_access_action_review.py`, `backend/infra/config.py`, `backend/registry/pr_history_reconciliation.py`, `scripts/corsia`, `scripts/corsia.d/app_mode.sh`, `scripts/corsia.d/runtime.sh`, `scripts/corsia.d/service.sh` |
| `CORSIA_T1_DEFAULT_BUDGET_SECONDS` | `'3600'` | Sets how many seconds an active task can run before its agent is prompted to check it. | `backend/automation/continuation/sources/watchdog.py` |
| `CORSIA_THREAD_ID` | `unset` | Identifies the current chat for CLI calls. | `backend/cli_events.py`, `backend/scripts/delegate_cli.py`, `backend/scripts/doc_cli.py` |
| `CORSIA_VERDICT_OWED_SECONDS` | `'1800'` | Sets how many seconds a returned worker's result can await review before its parent is reminded. | `backend/plans/delegation_owed_verdict.py` |
| `CORSIA_WORKSPACE` | `empty`, `unset` | Selects the workspace directory. | `backend/infra/config.py`, `backend/registry/pr_history_reconciliation.py`, `scripts/corsia`, `scripts/corsia.d/app_mode.sh`, `scripts/corsia.d/service.sh` |
| `HERMES_HOME` | `empty` | Selects the Hermes Agent directory for import. | `backend/tools/mcp_import_agents.py` |
| `OPENCLAW_CONFIG_PATH` | `empty` | Selects the OpenClaw configuration file for import. | `backend/tools/mcp_import_agents.py` |
| `OPENCLAW_HOME` | `empty` | Selects the OpenClaw home directory for import. | `backend/tools/mcp_import_agents.py` |
| `OPENCLAW_STATE_DIR` | `empty` | Selects the OpenClaw state directory for import. | `backend/tools/mcp_import_agents.py` |

## Models, browser and review

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `AGENT_BROWSER_ARGS` | `empty` | Passes arguments to the browser process. | `backend/host_access/browser_launch.py`, `backend/host_access/browser_session_commands.py` |
| `AGENT_BROWSER_AUTO_CONNECT` | `empty` | Controls automatic connection to a browser. | `backend/host_access/browser_cli.py` |
| `AGENT_BROWSER_CDP` | `unset` | Selects a browser debugging endpoint. | `backend/host_access/browser_cli.py` |
| `AGENT_BROWSER_CONFIG` | `unset` | Selects browser configuration. | `backend/host_access/browser_cli.py` |
| `AGENT_BROWSER_ENGINE` | `computed` | Selects the browser engine. | `backend/host_access/browser_cli.py` |
| `AGENT_BROWSER_EXECUTABLE_PATH` | `unset` | Selects the browser executable. | `backend/host_access/browser_cli.py` |
| `AGENT_BROWSER_PROVIDER` | `computed` | Selects the browser provider. | `backend/host_access/browser_cli.py` |
| `CLAUDE_BIN` | `unset` | Selects the Claude executable. | `backend/core/fork_render.py`, `backend/reviewer/reviewer_backends.py` |
| `CODEX_BIN` | `'codex'` | Selects the Codex executable. | `backend/reviewer/reviewer_backends.py` |
| `CODEX_HOME` | `computed`, `unset` | Selects the Codex home directory. | `backend/subagents/codex_env.py`, `backend/tools/mcp_import.py` |
| `CORSIA_AGENT_BROWSER_VERSION_RANGE` | `unset` | Sets the accepted agent-browser version range. | `backend/host_access/browser_cli.py` |
| `CORSIA_REVIEWER_FALLBACK_MODEL` | `unset` | Selects the fallback review model. | `backend/reviewer/reviewer_runner.py` |
| `CORSIA_REVIEWER_MAX_TURNS` | `empty` | Sets the maximum reviewer turns. | `backend/reviewer/failures.py` |
| `CORSIA_REVIEWER_MODEL` | `unset` | Selects the primary review model. | `backend/reviewer/reviewer_runner.py` |
| `LITELLM_LOCAL_MODEL_COST_MAP` | `unset` | Controls the local LiteLLM model cost map. | `backend/main.py` |
| `OMLX_BASE_URL` | `'http://localhost:10000'` | Sets the oMLX endpoint for the reviewer. | `backend/reviewer/reviewer_backends.py` |

## Evaluation

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `CORSIA_EVAL_ENDPOINT` | `computed` | Sets the evaluation endpoint. | `backend/eval/cli.py`, `backend/eval/solvers/corsia_turn.py`, `backend/eval/solvers/yaml_scenario.py` |
| `CORSIA_EVAL_JUDGE_MODEL` | `unset` | Selects the evaluation judge model. | `backend/eval/cli.py`, `backend/eval/runner.py`, `backend/eval/tasks.py` |
| `CORSIA_EVAL_LOG_DIR` | `computed` | Selects the evaluation log directory. | `backend/eval/runner.py` |
| `CORSIA_EVAL_MODE` | `empty` | Enables evaluation measurements when set to 1. | `backend/eval/otel.py` |
| `CORSIA_EVAL_SUT_MODEL` | `'none/none'` | Selects the model under evaluation. | `backend/eval/runner.py` |
| `CORSIA_EVAL_TIMEOUT` | `'180'` | Sets the evaluation timeout. | `backend/eval/solvers/corsia_turn.py`, `backend/eval/solvers/yaml_scenario.py` |
| `CORSIA_EVAL_TOKEN` | `unset` | Supplies the API token used for evaluation. | `backend/eval/credentials.py` |
| `CORSIA_EVAL_TOOL_ALLOWLIST` | `unset` | Selects which tools are available during evaluation. | `backend/eval/tool_allowlist.py` |
| `CORSIA_WORKFLOW_JUDGE_MODEL` | `unset` | Selects the workflow quality judge model. | `backend/eval/scorers/registry/workflow_quality.py` |

## Development and testing

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `CI` | `unset` | Identifies a CI environment for workspace checks. | `backend/infra/config.py` |
| `CORSIA_ALLOW_HOME_WORKSPACE_IN_CI` | `unset` | Allows a home workspace in CI or tests. | `backend/infra/config.py` |
| `CORSIA_TEST_MODE` | `unset` | Enables test-specific behavior when set to 1. | `backend/app_lifecycle.py`, `backend/app_startup_runtime.py`, `backend/connections/github_agent_home.py`, `backend/core/devices_runtime.py`, `backend/eval/harbor_tools.py`, `backend/eval/host_access_action_review.py`, `backend/eval/tool_allowlist.py`, `backend/events/connectors/github_head_checks.py`, `backend/events/connectors/github_notifications.py`, `backend/events/connectors/github_pr_quiet_watch.py`, `backend/events/connectors/github_repo_activity.py`, `backend/events/connectors/mail_poller.py`, `backend/infra/config.py`, `backend/infra/startup_prerequisites.py`, `backend/infra/update_status.py`, `backend/tools/private_runtime_files.py` |
| `GITHUB_ACTIONS` | `unset` | Identifies GitHub Actions for workspace checks. | `backend/infra/config.py` |

## Standard variables CORSIA reads

| Variable | Default | What it does | Read in |
|---|---|---|---|
| `APPDATA` | `empty` | Provides the Windows application data directory. | `backend/tools/mcp_import.py` |
| `CUSTOM_TIKTOKEN_CACHE_DIR` | `unset` | Selects a custom tokenization cache directory. | `backend/core/token_estimation.py` |
| `GH_CONFIG_DIR` | `unset` | Selects the GitHub CLI configuration directory. | `backend/connections/github_agent_home.py` |
| `HOME` | `empty`, `'/'` | Provides the home directory. | `backend/infra/git_state_cache.py`, `backend/reviewer/reviewer_read.py` |
| `LANG` | `unset` | Provides locale information for region detection. | `backend/infra/os_region.py` |
| `LC_ALL` | `unset` | Provides locale information for region detection. | `backend/infra/os_region.py` |
| `LOCALAPPDATA` | `empty` | Provides the Windows local application data directory. | `backend/tools/mcp_import_agents.py` |
| `PATH` | `empty`, `'/usr/bin:/bin'`, `unset` | Provides executable search paths. | `backend/cli_github.py`, `backend/reviewer/reviewer_read.py`, `backend/tools/mcp_local_transport.py`, `backend/tools/shell_sandbox.py` |
| `SHELL` | `unset` | Selects the user's shell. | `backend/cli_location.py` |
| `TIKTOKEN_CACHE_DIR` | `empty` | Selects the tokenization cache directory. | `backend/core/token_estimation.py` |
| `TZ` | `unset` | Provides the timezone for provider usage limits. | `backend/subagents/provider_usage_limits.py` |
| `XDG_CONFIG_HOME` | `unset` | Selects the XDG configuration directory. | `backend/automation/script_sandbox.py`, `backend/infra/git_state_cache.py` |


## Internal and test variables

These variables are used internally or for tests.

- `CORSIA_AGENT_RESTART_ALLOWED`
- `CORSIA_BROWSER_INSTANCE_KEY`
- `CORSIA_CC_TOOL_TOKEN`
- `CORSIA_CC_TOOL_URL`
- `CORSIA_FORK_ID`
- `CORSIA_FORK_TOOL_TOKEN`
- `CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE`
- `CORSIA_GITHUB_REPO_ACTIVITY_TEST_ENABLE`
- `CORSIA_GIT_LOCK_RECOVERY_FAILURE_PREFIX`
- `CORSIA_HARBOR_BRIDGE_SOCKET`
- `CORSIA_INSTALL_FLOW`
- `CORSIA_INSTALL_LINUX`
- `CORSIA_MAIL_POLLER_TEST_ENABLE`
- `CORSIA_RESTART_BACKEND_PORT`
- `CORSIA_RESTART_CALLER_TOKEN`
- `CORSIA_RESTART_OPERATION_ID`
- `CORSIA_SCRIPT_GITHUB_SOCKET`
- `CORSIA_SERVICE_GENERATION`
- `CORSIA_SOURCES_SERVICE_TOKEN`
- `CORSIA_TOKEN`
- `SPIKE_CC_MODEL`
