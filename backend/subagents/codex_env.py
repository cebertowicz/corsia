"""Clean environment for spawned ``codex`` processes.

``codex exec`` must always use the user's ChatGPT subscription auth (auth.json),
never API billing. Two classes of env vars break that and must be stripped
before every Codex spawn:

- ``OPENAI_*`` — any OpenAI billing var (including OPENAI_API_KEY). Removing
  them forces Codex to fall back to its saved ChatGPT-subscription auth. A
  spawned ``codex`` that sees OPENAI_API_KEY would silently bill the API key
  instead of the subscription — exactly the silent-API-billing failure mode
  the postmortem documented.
- ``CODEX_*_KEY`` — any Codex-specific API key pattern (CODEX_API_KEY, and
  any future CODEX_<scope>_KEY variants).

This is the single source of truth for the Codex spawn env; do not pass raw
``os.environ`` to a ``codex`` process.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

_STRIP_PREFIXES = ("OPENAI_",)
# Keys starting with CODEX_ and ending with _KEY (e.g. CODEX_API_KEY,
# CODEX_WORKSPACE_KEY, etc.). CODEX_API_KEY matches this pattern, so no
# separate exact-match set is needed.
_CODEX_KEY_PREFIX = "CODEX_"
_CODEX_KEY_SUFFIX = "_KEY"
_MODEL_CACHE_FILENAME = "models_cache.json"
_GOVERNED_MODEL_CATALOG_FILENAME = "corsia-model-catalog.json"
# Model metadata that forces Code Mode regardless of ``features.code_mode``.
# Under it the provider emits only ``exec``/``wait`` at the response level and
# routes every CORSIA tool as an inner sandbox RPC whose callId never appears
# as a top-level ``function_call``/``custom_tool_call`` in the rollout. The
# durable receipt cursor can only pair usage with those top-level call IDs, so
# the RPC waits for a receipt that can never arrive, holding the session lock
# with no deadline, and the tool body is never reached. Neutralize it here,
# where the catalog is already governed, so ``features.code_mode=false`` in
# ``managed_launch`` actually takes effect.
_FORCED_CODE_MODE_TOOL_MODE = "code_mode_only"
_HOME_INSTRUCTION_FILENAMES = ("AGENTS.override.md", "AGENTS.md")


def clean_codex_env() -> dict[str, str]:
    """Return ``os.environ`` minus OPENAI/* billing vars and CODEX_*_KEY vars.

    The result is safe to hand to ``create_subprocess_exec(env=...)`` for any
    ``codex exec`` spawn: it forces the ChatGPT-subscription auth path and
    makes silent API-billing fallback impossible.
    """
    result: dict[str, str] = {}
    for k, v in os.environ.items():
        if any(k.startswith(p) for p in _STRIP_PREFIXES):
            continue
        if k.startswith(_CODEX_KEY_PREFIX) and k.endswith(_CODEX_KEY_SUFFIX):
            continue
        result[k] = v
    return result


def _subscription_auth_file(source_home: Path) -> Path:
    """Return validated file-backed ChatGPT auth without exposing its content."""
    auth_file = source_home / "auth.json"
    try:
        payload = json.loads(auth_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Codex app-server requires file-backed ChatGPT subscription auth; "
            f"no usable auth.json was found in {source_home}. Run 'codex login' "
            "and choose ChatGPT subscription authentication, then retry."
        ) from exc
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    api_key = payload.get("OPENAI_API_KEY") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("auth_mode") != "chatgpt"
        or (isinstance(api_key, str) and bool(api_key.strip()))
        or not isinstance(tokens, dict)
        or not any(
            isinstance(value, str) and bool(value.strip())
            for value in tokens.values()
        )
    ):
        raise RuntimeError(
            "Codex app-server requires file-backed ChatGPT subscription auth; "
            f"{auth_file} is not a usable ChatGPT subscription credential. "
            "Run 'codex login' and choose ChatGPT subscription authentication, "
            "then retry."
        )
    return auth_file.resolve(strict=True)


def _default_app_server_home() -> Path:
    from infra.config import RUNTIME_DIR

    return RUNTIME_DIR / "codex-app-server-home"


def governed_codex_model_catalog_path(
    isolated_home: Path | None = None, *, confined: bool = False,
) -> Path:
    """Return CORSIA's invocation-local Codex model catalog path."""
    destination = Path(
        isolated_home or _default_app_server_home()
    ).expanduser().resolve()
    filename = "corsia-confined-model-catalog.json" if confined else _GOVERNED_MODEL_CATALOG_FILENAME
    return destination / filename


def _write_governed_model_catalog(*, source: Path, destination: Path,
                                  catalog_source: Path | None = None, cache_receipt=None,
                                  confined: bool = False) -> Path:
    """Copy discovered model truth while disabling provider-native agents.

    Also clears model metadata that would force Code Mode, so every managed
    tool arrives as a top-level provider call the durable receipt cursor can
    pair usage with.
    """
    candidates = [
        path
        for path in {
            source / _MODEL_CACHE_FILENAME,
            _default_app_server_home() / _MODEL_CACHE_FILENAME,
            destination / _MODEL_CACHE_FILENAME,
        }
        if path.is_file()
    ]
    if catalog_source is not None:
        from connections.model_cache_stage import validate_cache

        if not isinstance(cache_receipt, dict) or not validate_cache(cache_receipt, catalog_source):
            raise RuntimeError('account_model_cache_changed')
        candidates = [catalog_source]
    candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    if not candidates:
        raise RuntimeError(
            "Codex managed execution requires a discovered model catalog so "
            "provider-native tools can be disabled. Refresh Codex models and "
            "retry."
        )
    candidate = candidates[0]
    try:
        catalog = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Codex discovered model catalog is unreadable: {candidate}"
        ) from exc
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list) or not models:
        raise RuntimeError(
            f"Codex discovered model catalog has no models: {candidate}"
        )
    for model in catalog["models"]:
        if isinstance(model, dict):
            model["multi_agent_version"] = None
            if confined:
                # spec_plan.rs adds clock and user-input tools from metadata,
                # independently of feature flags. No native tool may be added.
                model["experimental_supported_tools"] = []
                model["shell_type"] = "disabled"
                model["apply_patch_tool_type"] = None
                model["tool_mode"] = "direct"
                model["supports_search_tool"] = False
                model["use_responses_lite"] = False
            if model.get("tool_mode") == _FORCED_CODE_MODE_TOOL_MODE:
                model["tool_mode"] = None

    governed = governed_codex_model_catalog_path(destination, confined=confined)
    temporary = destination / f".{governed.name}-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(catalog, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, governed)
    finally:
        temporary.unlink(missing_ok=True)
    return governed


def default_codex_home() -> Path:
    """The user's own Codex home: ``CODEX_HOME`` when set, else ``~/.codex``."""
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()


def clean_codex_app_server_env(
    *,
    source_home: Path | None = None,
    isolated_home: Path | None = None,
    govern_model_catalog: bool = False,
    projection_receipt=None, catalog_source: Path | None = None, cache_receipt=None,
    confined: bool = False,
) -> dict[str, str]:
    """Build an auth-only, config-isolated environment for app-server.

    Legacy ``codex exec`` callers continue to use :func:`clean_codex_env`.
    The managed app-server gets its own persistent home for thread/SQLite state
    and an atomic link to the user's subscription credential only.
    """
    source = Path(source_home).expanduser().resolve() if source_home else default_codex_home()
    destination = Path(
        isolated_home or _default_app_server_home()
    ).expanduser().resolve()
    if source == destination:
        raise RuntimeError(
            "Codex app-server isolated home must differ from the user Codex home"
        )
    if projection_receipt is not None:
        from connections.account_projection import validate_projection

        validate_projection(projection_receipt, home=destination)
    source_auth = _subscription_auth_file(source)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    if govern_model_catalog:
        if projection_receipt is not None and catalog_source is None:
            raise RuntimeError('account_model_not_discovered')
        _write_governed_model_catalog(
            source=source,
            destination=destination,
            confined=confined,
            **({'catalog_source': catalog_source, 'cache_receipt': cache_receipt}
               if catalog_source is not None else {}),
        )

    # A stable runtime home must not turn a stale/poisoned config into a second
    # source of tool truth. Replace even a symlink with CORSIA's empty config.
    controlled_config = destination / "config.toml"
    temporary_config = destination / f".config-{uuid.uuid4().hex}.tmp"
    try:
        temporary_config.write_text("")
        temporary_config.chmod(0o600)
        os.replace(temporary_config, controlled_config)
    finally:
        temporary_config.unlink(missing_ok=True)
    # Codex delivers home-level AGENTS files as user instructions. CORSIA
    # never writes them here, so any present are foreign and must not load.
    for instructions in _HOME_INSTRUCTION_FILENAMES:
        (destination / instructions).unlink(missing_ok=True)

    if projection_receipt is None:
        projected_auth = destination / "auth.json"
        temporary_link = destination / f".auth-{uuid.uuid4().hex}.tmp"
        try:
            temporary_link.symlink_to(source_auth)
            os.replace(temporary_link, projected_auth)
        finally:
            temporary_link.unlink(missing_ok=True)

    env = clean_codex_env()
    env["CODEX_HOME"] = str(destination)
    env["CODEX_SQLITE_HOME"] = str(destination)
    return env


def build_session_env(session) -> dict[str, str]:
    """The launch environment for an app-server session, with its isolated home when it has one."""
    if session.codex_home:
        return dict(
            clean_codex_app_server_env(
                source_home=Path(session.source_codex_home) if session.source_codex_home else None,
                isolated_home=Path(session.codex_home),
                govern_model_catalog=not session.control_only,
                confined=getattr(session, "confined", False),
                **({'projection_receipt': session.vendor_account_receipt,
                    'catalog_source': getattr(session, 'vendor_catalog_source', None),
                    'cache_receipt': getattr(session, 'vendor_cache_receipt', None)}
                   if getattr(session, 'vendor_account_custom', False) else {}),
            )
        )
    return dict(
        clean_codex_app_server_env(
            govern_model_catalog=not session.control_only,
            confined=getattr(session, "confined", False),
        )
    )
