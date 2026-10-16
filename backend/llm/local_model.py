"""The local model CORSIA sets up: the one oMLX's own recommender picks for this Mac.

There is no hand-kept model list. CORSIA's managed oMLX answers
``GET /admin/api/hf/recommendations`` (first shipped in oMLX 0.7.1rc1), and the
pick is the first row of its "best" section with at least CORSIA's agent minimum
of context. Under 24 GiB of memory there is no local model at all. This module
is the one owner of that rule; every other reader goes through it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: The oMLX release CORSIA reads the recommender from. The desktop bundle ships
#: exactly this release (``desktop/scripts/build-macos-bundle.sh``), and a managed
#: oMLX older than it has no recommender; a test fails if the two disagree.
OMLX_PIN = "0.7.1rc1"

GIB = 1024**3
# RAM is compared in GiB because Apple sells memory in binary units: a Mac sold
# as "24 GB" reports 24 GiB. Below it there is no local model (agent work runs
# on the cloud; the corrections classifier still runs locally).
MIN_RAM_GIB = 24
#: CORSIA's agent minimum, and oMLX's own "comfortable" bar.
MIN_CONTEXT = 32_768
MAX_CONTEXT = 262_144

# The build suffix of a model repository: an oQ quantization with its optional
# MTP head, or an ``mlx-<bits>`` build.
_BUILD_SUFFIX = re.compile(r"-(?:oQ[\d.]+e(?:-mtp)?|mlx-\w+)$", re.IGNORECASE)
_RECORD_NAME = "local-model.json"


@dataclass(frozen=True)
class LocalModel:
    """One model oMLX downloads: what the recommender picked, or the own-oMLX starter."""

    repository: str
    download_size_bytes: int
    context_window: int

    @property
    def model(self) -> str:
        """oMLX's model id: the repository without its owner."""
        return self.repository.rsplit("/", 1)[-1]

    @property
    def display_name(self) -> str:
        """The repo id without owner or build suffix: ``Jundot/Qwen3.8-27B-oQ4e-mtp`` is "Qwen3.8 27B"."""
        name = _BUILD_SUFFIX.sub("", self.model).removesuffix("-it").replace("-", " ")
        return name[:1].upper() + name[1:]

    @property
    def mtp(self) -> bool:
        """Whether this is an ``-mtp`` build, which oMLX runs with its MTP head on."""
        return self.model.lower().endswith("-mtp")

    def view(self) -> dict:
        """The model as setup surfaces show it."""
        return {
            "name": self.display_name, "repository": self.repository,
            "download_size_bytes": self.download_size_bytes,
            "download_size_gb": round(self.download_size_bytes / 1000**3, 1),
            "context_window": self.context_window,
        }


def memory_allows_local(ram_bytes: int) -> bool:
    """Whether this Mac has the memory for any local model (24 GiB or more)."""
    return ram_bytes >= MIN_RAM_GIB * GIB


def omlx_has_recommender(version: str | None) -> bool:
    """Whether an installed oMLX version serves the recommender (``OMLX_PIN`` or newer)."""
    from packaging.version import InvalidVersion, Version

    try:
        return version is not None and Version(version) >= Version(OMLX_PIN)
    except InvalidVersion:
        return False


def from_recommendation(raw: object) -> LocalModel | None:
    """One recommender row as a model, or None when it is malformed or below the context minimum.

    The window is the row's ``max_context`` capped at 256K: oMLX already priced
    the KV cache into it, so CORSIA takes no extra memory deduction.
    """
    if not isinstance(raw, dict):
        return None
    repository = raw.get("repo_id")
    size = raw.get("download_bytes")
    context = raw.get("max_context")
    if (not isinstance(repository, str) or "/" not in repository or isinstance(size, bool)
            or not isinstance(size, int) or size <= 0 or isinstance(context, bool)
            or not isinstance(context, int) or context < MIN_CONTEXT):
        return None
    return LocalModel(repository, size, min(context, MAX_CONTEXT))


def to_record(model: LocalModel) -> dict:
    """The model as a state record keeps it: the recommender row, read back with ``from_recommendation``."""
    return {"repo_id": model.repository, "download_bytes": model.download_size_bytes,
            "max_context": model.context_window}


def pick(payload: object) -> LocalModel | None:
    """The first row of the recommender's ``best`` section that reaches 32K, else None."""
    sections = payload.get("sections") if isinstance(payload, dict) else None
    rows = sections.get("best") if isinstance(sections, dict) else None
    if not isinstance(rows, list):
        return None
    return next((model for row in rows if (model := from_recommendation(row)) is not None), None)


class PickUnavailable(RuntimeError):
    """The recommender could not answer: oMLX not up yet, Hugging Face unreachable, or no recommender."""


async def ask_recommender(server_id: str) -> LocalModel | None:
    """Ask the managed oMLX once; None when nothing fits, ``PickUnavailable`` when it can't answer.

    oMLX answers 503 before its downloader is up, 504 on a Hub timeout and 502 on
    any other failure; its own Hub reads wait up to 30 seconds, so this waits a
    little longer than that.
    """
    import httpx

    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    try:
        response = await admin_request({"server_id": server_id}, "GET", "/hf/recommendations", timeout=45.0)
    except (OmlxAdminError, httpx.HTTPError) as exc:
        raise PickUnavailable(f"oMLX could not be reached ({type(exc).__name__}).") from exc
    if response.status_code != 200:
        raise PickUnavailable(f"oMLX could not check model recommendations (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise PickUnavailable("oMLX returned unreadable model recommendations.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), dict):
        raise PickUnavailable("oMLX returned unreadable model recommendations.")
    return pick(payload)


def _record_path() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _RECORD_NAME


def record_set_up(model: LocalModel) -> None:
    """Keep the model a finished setup installed, for readers in later processes."""
    target = _record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        staged.write_text(json.dumps(to_record(model), sort_keys=True), encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _starter_installed(starter: LocalModel) -> bool:
    """Whether CORSIA's own oMLX has the pre-record starter in its configured model directory."""
    from llm.local_setup_ownership import own_omlx_settings_file

    try:
        raw = json.loads(own_omlx_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    model = raw.get("model") if isinstance(raw, dict) else None
    model_dir = model.get("model_dir") if isinstance(model, dict) else None
    return isinstance(model_dir, str) and bool(model_dir) and (Path(model_dir) / starter.repository).is_dir()


def _backfill_starter() -> LocalModel | None:
    """Write the one missing legacy record when CORSIA's own old oMLX still has its starter."""
    from llm.local_profiles import OWN_OMLX_STARTER
    from llm.local_setup_ownership import existing_install_is_foreign

    if existing_install_is_foreign() or not _starter_installed(OWN_OMLX_STARTER):
        return None
    record_set_up(OWN_OMLX_STARTER)
    return OWN_OMLX_STARTER


def set_up_model() -> LocalModel | None:
    """The model the last finished setup installed, backfilling the one pre-record starter when present."""
    try:
        raw = json.loads(_record_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _backfill_starter()
    except (OSError, ValueError) as error:
        log.warning("local model record is unreadable; treating it as absent: %s", error)
        return None
    model = from_recommendation(raw)
    if model is None:
        log.warning("local model record is malformed; treating it as absent")
    return model
