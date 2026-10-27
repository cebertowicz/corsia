"""Claude Code, Codex, and oMLX reviewer backend connectors."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from infra import config
from subagents.managed_launch import codex_project_discovery_args
from tools.private_runtime_files import REVIEWER_OUTPUT_DIR, private_mkstemp
from reviewer.failures import (
    CCMaxTurnsError,
    CCRateLimitError,
    CCUnavailableError,
    cc_max_turns as _cc_max_turns,
    classify_cc_error as _classify_cc_error,
    envelope_signals as _envelope_signals,
    is_usage_or_quota_limit as _is_usage_or_quota_limit,
    parse_envelope,
    raise_cli_failure,
)
from reviewer.reviewer_exact_source import (
    claude_config_state_scope,
    claude_exact_source_controls,
    codex_exact_source_controls,
    discard_config_state,
    write_confined,
)

log = logging.getLogger(__name__)


def _resolve_cc_binary() -> str:
    """Resolve the Claude Code CLI binary, mirroring the cc_runtime path.

    ``CLAUDE_BIN`` (CI/operator override) -> the configured ``claude-code``
    connection's ``config.binary`` (the runtime's source of truth, e.g.
    ``~/.local/bin/claude``) -> bare ``"claude"`` on PATH. The reviewer runs in
    the backend process, whose PATH may not include the user's ``~/.local/bin``;
    defaulting to a bare ``"claude"`` made the reviewer die with
    ``FileNotFoundError: 'claude'`` whenever the configured reviewer model
    routed to the CC backend. The connection-config lookup removes that PATH
    dependency. The lookup must never break the reviewer, so it falls back
    loudly-logged rather than raising.
    """
    env_bin = os.environ.get("CLAUDE_BIN")
    if env_bin and env_bin.strip():
        return env_bin.strip()
    try:
        from connections.models import get_connection
        conn = get_connection("claude-code")
        binary = ((conn or {}).get("config") or {}).get("binary")
        if binary and str(binary).strip():
            return str(binary).strip()
    except Exception as exc:  # config lookup must never break the reviewer
        log.warning("reviewer: claude-code connection lookup failed: %s", exc)
    return "claude"


def _run_write_confined(
    cmd: list[str], prompt: str, working_dir: str, env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run the Claude CLI where it may write only its own scopes.

    The model reads untrusted PR text: it may write only its scratch directory
    and its own run's state folder under the account config. The run directory
    is fresh, so that folder is named for this run alone.
    """
    scratch = os.path.join(working_dir, "tmp")
    os.makedirs(scratch, exist_ok=True)
    env["TMPDIR"] = scratch
    with tempfile.TemporaryDirectory(prefix="corsia-cc-", dir=working_dir) as run_dir:
        config_state = claude_config_state_scope(env, run_dir)
        try:
            return subprocess.run(
                write_confined(cmd, [working_dir], config_state),
                input=prompt,
                capture_output=True,
                text=True,
                cwd=run_dir,
                timeout=1800,  # 30 min per locked decision #16
                env=env,
            )
        finally:
            discard_config_state(config_state)


def invoke_cc_subagent(
    prompt: str, working_dir: str, *, model: str, source_git_dir: str,
    base_sha: str, head_sha: str, env: dict[str, str],
) -> str:
    """Invoke ``claude -p`` and return the model's result text.

    The model is explicit, the prompt travels over stdin, and the CLI JSON
    envelope must report success before its result is accepted. ``env`` is the
    admitted account's launch environment from ``reviewer_account``.
    """
    claude_bin = _resolve_cc_binary()
    source_args, source_env = claude_exact_source_controls(
        source_git_dir, base_sha, head_sha,
    )
    cmd = [
        claude_bin, "-p",
        *source_args,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--model", model,
        "--max-turns", _cc_max_turns(),
        # `high`, not `max`: max extended-thinking per agentic iteration is the
        # dominant reviewer token cost (rate-limit pressure). A diff review does
        # not need max effort; high preserves cross-referencing depth at a much
        # lower token cost.
        "--effort", "high",
        "--output-format", "json",
    ]

    env = dict(env)
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
    env.update(source_env)
    result = _run_write_confined(cmd, prompt, working_dir, env)

    output = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        raise_cli_failure(result.returncode, output, stderr)

    envelope = parse_envelope(output)
    if envelope is None:
        raise RuntimeError(
            f"claude -p emitted non-JSON despite --output-format json: {output[:200]}"
        )

    result_text = envelope.get("result")
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        signals = _envelope_signals(envelope) or f"subtype={envelope.get('subtype')}"
        raise _classify_cc_error(
            f"{result_text} {stderr} subtype={envelope.get('subtype')}",
            f"CC error envelope: {str(result_text)[:200]} | {signals} | {output[:200]}",
            envelope,
        )

    usage = envelope.get("usage") or {}
    log.info(
        "CC review call ok: model=%s tokens_in=%s tokens_out=%s cost_usd=%s",
        model, usage.get("input_tokens"), usage.get("output_tokens"),
        envelope.get("total_cost_usd"),
    )

    if not isinstance(result_text, str) or not result_text.strip():
        raise RuntimeError("CC returned an empty result string")
    return result_text


def invoke_codex_reviewer(
    prompt: str, working_dir: str, *, model: str, source_git_dir: str,
    env: dict[str, str],
) -> str:
    """Invoke ``codex exec`` and return the final reviewer text.

    The prompt travels via stdin, never argv, so promotion-scale diffs do not
    hit ARG_MAX. ``env`` is the admitted account's launch environment from
    ``reviewer_account``.
    """
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    output_path: str | None = None
    try:
        descriptor, output_path = private_mkstemp(REVIEWER_OUTPUT_DIR, prefix="codex-last-", suffix=".txt")
        os.close(descriptor)

        cmd = [
            codex_bin, "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral", "--ignore-user-config",
            "--ignore-rules",
            *codex_exact_source_controls(working_dir, source_git_dir),
            *codex_project_discovery_args(),
            "--output-last-message", output_path,
            "--model", model,
            "--", "-",
        ]
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=1800,
            env=env,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        event_error = _codex_error_from_jsonl(stdout)
        if event_error is not None:
            raise event_error

        if result.returncode != 0:
            raise _classify_codex_error(
                f"{stdout} {stderr}",
                f"codex exec failed (rc={result.returncode}): "
                f"{(stderr or stdout)[:200]}",
            )

        output = Path(output_path).read_text().strip()
        if not output:
            raise RuntimeError("codex exec returned an empty reviewer result")
        return output
    finally:
        if output_path:
            try:
                Path(output_path).unlink()
            except FileNotFoundError:
                pass


def _codex_error_from_jsonl(stdout: str) -> Exception | None:
    """Return a loud reviewer exception for Codex JSONL failure events."""
    if not stdout.strip():
        return None

    # JSONL frames on LF alone, while str.splitlines() also breaks on U+2028, U+2029 and
    # U+0085, which JSON may leave raw inside a string; strip() absorbs a CRLF stream's CR.
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            return RuntimeError(
                f"codex exec emitted non-JSON despite --json: {line[:200]}"
            )
        if not isinstance(event, dict):
            return RuntimeError(
                f"codex exec emitted non-object JSON event: {line[:200]}"
            )

        event_type = event.get("type")
        if event_type not in ("turn.failed", "error"):
            continue

        raw_error = event.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        account = event.get("account")
        rate_limits = account.get("rateLimits") if isinstance(account, dict) else {}
        if not isinstance(rate_limits, dict):
            rate_limits = {}

        code = str(error.get("code") or "")
        message = str(error.get("message") or event.get("message") or "")
        reached = str(rate_limits.get("rateLimitReachedType") or "")
        detail = message or code or event_type
        return _classify_codex_error(
            f"{event_type} {code} {message} {reached}",
            f"codex exec emitted {event_type}: {detail[:200]}",
        )

    return None


def _classify_codex_error(error_text: str, detail: str) -> Exception:
    """Map raw Codex CLI error text to reviewer backend exceptions."""
    low = str(error_text).lower()
    if _is_usage_or_quota_limit(low):
        return CCUnavailableError(detail)
    if "rate_limit" in low or "rate limit" in low or "429" in low:
        return CCRateLimitError(detail)
    if (
        "temporarily unavailable" in low
        or "selected model is at capacity" in low
        or "overloaded" in low
        or "529" in low
        or "model not found" in low
        or "model_not_found" in low
        or "model not available" in low
        or "unknown model" in low
        or "invalid model" in low
        or "login required" in low
        or "not logged in" in low
        or "auth_failed" in low
        or "unauthorized" in low
        or "invalid_api_key" in low
        or "authentication" in low
        or "provider_unavailable" in low
        or "exec_failed" in low
        or "spawn_failed" in low
    ):
        return CCUnavailableError(detail)
    return RuntimeError(detail)


def invoke_local_model(prompt: str) -> str:
    """Invoke the local oMLX 27B model for review."""
    import httpx

    omlx_base = os.environ.get("OMLX_BASE_URL", "http://localhost:10000")
    response = httpx.post(
        f"{omlx_base}/v1/chat/completions",
        json={
            "model": "qwen3.6-27B-Dense",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=600,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def is_27b_loaded() -> bool:
    """Return whether qwen3.6-27B Dense is loaded in oMLX."""
    omlx_base = os.environ.get("OMLX_BASE_URL", "http://localhost:10000")
    try:
        import httpx
        resp = httpx.get(f"{omlx_base}/v1/models/status", timeout=10)
        if resp.status_code != 200:
            log.warning("oMLX /v1/models/status returned %d", resp.status_code)
            return False
        models = resp.json()
        for m in models if isinstance(models, list) else [models]:
            name = m.get("id", "") if isinstance(m, dict) else str(m)
            if "27B" in name and "Dense" in name:
                return True
        return False
    except (httpx.HTTPError, httpx.TimeoutException, OSError, ValueError) as exc:
        log.warning("Failed to check oMLX model status: %s", exc)
        return False


def write_calibration_data(
    pr_number: int,
    cc_output: str | None,
    local_output: str | None,
    cc_label: str = "CC reviewer",
) -> None:
    """Write parallel review outputs to operational runtime state."""
    cal_dir = config.STATE_ROOT / "reviewer" / "calibration" / str(pr_number)
    cal_dir.mkdir(parents=True, exist_ok=True)

    if cc_output:
        (cal_dir / "cc.md").write_text(
            f"# {cc_label} Review -- PR #{pr_number}\n\n"
            f"```json\n{cc_output}\n```\n",
            encoding="utf-8",
        )

    if local_output:
        (cal_dir / "qwen-27b.md").write_text(
            f"# qwen3.6-27B Dense Review -- PR #{pr_number}\n\n"
            f"```json\n{local_output}\n```\n",
            encoding="utf-8",
        )

    if cc_output and local_output:
        diff_text = (
            f"# Calibration Diff -- PR #{pr_number}\n\n"
            f"## {cc_label}\n```json\n{cc_output}\n```\n\n"
            f"## qwen3.6-27B Dense\n```json\n{local_output}\n```\n"
        )
        (cal_dir / "diff.md").write_text(diff_text, encoding="utf-8")
