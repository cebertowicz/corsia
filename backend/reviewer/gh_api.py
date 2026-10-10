"""Small ``gh api`` JSON helpers used by reviewer automation."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def run_gh(
    args: list[str], *, input_text: str | None = None, timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run one ``gh`` command; callers own all result and failure policy."""
    return subprocess.run(
        ["gh", *args], input=input_text, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def gh_api_patch_json(api_path: str, payload: dict[str, Any]) -> bool:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(payload, f)
        tmp_path = f.name
    try:
        result = run_gh(
            ["api", "-X", "PATCH", api_path, "--input", tmp_path], timeout=30,
        )
        if result.returncode != 0:
            log.error(
                "gh api PATCH %s failed (rc=%d): %s",
                api_path, result.returncode, (result.stderr or "")[:300],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("gh api PATCH %s timed out", api_path)
        return False
    except FileNotFoundError:
        log.error("gh CLI not found")
        return False
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def gh_api_post_json(api_path: str, payload: dict[str, Any]) -> bool:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(payload, f)
        tmp_path = f.name

    try:
        result = run_gh(
            ["api", "-X", "POST", api_path, "--input", tmp_path], timeout=30,
        )
        if result.returncode != 0:
            log.error(
                "gh api POST %s failed (rc=%d): %s",
                api_path, result.returncode, result.stderr[:300],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("gh api POST %s timed out", api_path)
        return False
    except FileNotFoundError:
        log.error("gh CLI not found")
        return False
    finally:
        Path(tmp_path).unlink(missing_ok=True)
