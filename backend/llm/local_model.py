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
from dataclasses import dataclass, field
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
    #: A name the profile gives the model; without one the name comes from the repository id.
    name: str | None = field(default=None, compare=False)
    #: oMLX recommender facts, retained for Settings display without changing model identity.
    level: float | None = field(default=None, compare=False)
    fp16: bool | None = field(default=None, compare=False)
    relative_speed: float | None = field(default=None, compare=False)

    @property
    def model(self) -> str:
        """oMLX's model id: the repository without its owner."""
        return self.repository.rsplit("/", 1)[-1]

    @property
    def display_name(self) -> str:
        """The profile's name when it gives one, else the repo id without owner or build suffix: ``Jundot/Qwen3.8-27B-oQ4e-mtp`` is "Qwen3.8 27B"."""
        if self.name:
            return self.name
        name = _BUILD_SUFFIX.sub("", self.model).removesuffix("-it").replace("-", " ")
        return name[:1].upper() + name[1:]

    @property
    def mtp(self) -> bool:
        """Whether this is an ``-mtp`` build, which oMLX runs with its MTP head on."""
        return self.model.lower().endswith("-mtp")

    @property
    def quantization_bits(self) -> float | None:
        """The recommender's structured quantization precision for display."""
        return 16.0 if self.fp16 else self.level

    def view(self) -> dict:
        """The model as setup surfaces show it."""
        result = {
            "name": self.display_name, "repository": self.repository, "model": self.model,
            "download_size_bytes": self.download_size_bytes,
            "download_size_gb": round(self.download_size_bytes / 1000**3, 1),
            "context_window": self.context_window,
        }
        if self.quantization_bits is not None:
            result["quantization_bits"] = self.quantization_bits
        if self.relative_speed is not None:
            result["relative_speed"] = self.relative_speed
        return result


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
    name = raw.get("display_name")
    level = raw.get("level")
    fp16 = raw.get("fp16")
    relative_speed = raw.get("relative_speed")
    return LocalModel(
        repository, size, min(context, MAX_CONTEXT), name if isinstance(name, str) and name.strip() else None,
        float(level) if isinstance(level, (int, float)) and not isinstance(level, bool) else None,
        fp16 if isinstance(fp16, bool) else None,
        float(relative_speed) if isinstance(relative_speed, (int, float)) and not isinstance(relative_speed, bool) else None,
    )


def to_record(model: LocalModel) -> dict:
    """The model as a state record keeps it: the recommender row, read back with ``from_recommendation``."""
    record = {"repo_id": model.repository, "download_bytes": model.download_size_bytes,
              "max_context": model.context_window}
    if model.name:
        record["display_name"] = model.name
    if model.level is not None:
        record["level"] = model.level
    if model.fp16 is not None:
        record["fp16"] = model.fp16
    if model.relative_speed is not None:
        record["relative_speed"] = model.relative_speed
    return record


class PickUnavailable(RuntimeError):
    """The recommender could not answer: oMLX not up yet, Hugging Face unreachable, or no recommender."""


async def ask_recommendations(server_id: str) -> dict[str, list[LocalModel]]:
    """Return every usable named recommender section from one oMLX request.

    oMLX answers 503 before its downloader is up, 504 on a Hub timeout and 502 on
    any other failure; its own Hub reads wait up to 30 seconds, so this waits a
    little longer than that.
    """
    import httpx

    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    try:
        response = await admin_request({"server_id": server_id}, "GET", "/hf/recommendations", timeout=45.0)
    except (OmlxAdminError, httpx.HTTPError) as exc:
        log.exception("oMLX recommender request failed")
        raise PickUnavailable("Couldn't get model recommendations from oMLX. Check that oMLX is running, then try again.") from exc
    if response.status_code != 200:
        log.warning("oMLX recommender returned status %s", response.status_code)
        raise PickUnavailable("Couldn't get model recommendations from oMLX. Check that oMLX is running, then try again.")
    try:
        payload = response.json()
    except ValueError as exc:
        log.warning("oMLX recommender returned unreadable data")
        raise PickUnavailable("Couldn't get model recommendations from oMLX. Try again.") from exc
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections, dict):
        log.warning("oMLX recommender returned an invalid payload")
        raise PickUnavailable("Couldn't get model recommendations from oMLX. Try again.")
    return {
        name: [model for row in rows if (model := from_recommendation(row)) is not None]
        for name, rows in sections.items() if isinstance(name, str) and isinstance(rows, list)
    }


async def ask_recommender(server_id: str) -> LocalModel | None:
    """Pick setup's model from the shared Settings recommender reader."""
    sections = await ask_recommendations(server_id)
    return next((model for model in sections.get("best", [])), None)


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
    """Whether CORSIA's private model directory holds the completed starter."""
    from llm.local_setup_managed import managed_omlx_paths
    from llm.local_setup_disk import download_complete

    model_dir = managed_omlx_paths().model_dir
    return download_complete(model_dir / starter.repository, starter.download_size_bytes)


def backfill_legacy_record() -> LocalModel | None:
    """Once at startup, record the starter a finished pre-record setup installed.

    Writes only when there is no record or recovery record (a setup may be mid-
    download) and CORSIA's private model directory holds the whole starter.
    Blocking: run it off the event loop.
    """
    from llm import local_setup_operation
    from llm.local_profiles import STARTER_MODEL

    if _record_path().exists() or local_setup_operation.record_path().exists():
        return None
    if not _starter_installed(STARTER_MODEL):
        return None
    try:
        record_set_up(STARTER_MODEL)
    except OSError as error:
        log.warning("could not record the earlier setup's starter model: %s", error)
        return None
    log.info("recorded the starter model a finished earlier setup installed")
    return STARTER_MODEL


def set_up_model() -> LocalModel | None:
    """The model the last finished setup installed, else None."""
    try:
        raw = json.loads(_record_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log.warning("local model record is unreadable; treating it as absent: %s", error)
        return None
    model = from_recommendation(raw)
    if model is None:
        log.warning("local model record is malformed; treating it as absent")
    return model
