"""Host RAM accounting for local provider preload checks."""

from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Any, Literal

logger = logging.getLogger(__name__)


GIB = 1024 ** 3
SAFE_RESERVE_BYTES = 16 * GIB

# RAM preload checks apply only to managed engines — the ones CORSIA actually
# loads models into. External providers are user-run (observe-only), so their
# memory is not CORSIA's to account for.
LOCAL_PROVIDER_TYPES = {"omlx"}

# Peak process RSS while adapting one image tracks the pixels its header
# declares, not the bytes on disk. Measured against this codebase one fixture
# per process (Pillow 12.2.0, pillow-heif 1.7.0, ru_maxrss delta): 4.0 bytes per
# declared pixel for LZW TIFF, and 14.2 to 16.4 for HEIF, whose decoder hands
# Pillow a read-only frame the encoder is then obliged to copy. This is the
# measured worst case rounded up, so the derived allowance is not optimistic.
IMAGE_DECODE_BYTES_PER_PIXEL = 17

# The share of total RAM that concurrent image decodes may commit. The rest of
# the machine runs an OS, a browser, and possibly local model weights, none of
# which this path may evict.
IMAGE_DECODE_RAM_FRACTION = 0.125

# Below this a host would refuse ordinary photographs, which serves the user
# worse than the memory risk does: the smallest machine still opens a 24 MP
# frame, comfortably above a 12 MP phone photo.
MIN_IMAGE_DECODE_PIXELS = 24_000_000


def host_total_ram_bytes() -> int:
    """Return this machine's total physical RAM in bytes.

    ``psutil`` is imported here rather than at module scope because
    ``core.image_resize`` reaches this module from the import path the service
    installer runs before any dependency exists. Importing this module must cost
    nothing; only calling it may need the package.
    """
    import psutil

    return int(psutil.virtual_memory().total)


def host_available_ram_bytes() -> int:
    """Return the RAM a new process could take now, in bytes.

    macOS "available": free, inactive and purgeable pages. Read fresh on every
    call, since it moves as other programs run.
    """
    import psutil

    return int(psutil.virtual_memory().available)


def memory_reading() -> str:
    """This machine's memory as the fit verdict reads it, for the log line beside a refusal.

    The page breakdown shows which pages ``available`` left out, so a refusal
    on a machine that looked free can be explained from the log alone.
    """
    import psutil

    vm = psutil.virtual_memory()
    pages = ", ".join(f"{name} {getattr(vm, name) / GIB:.1f}"
                      for name in ("free", "inactive", "active", "wired") if hasattr(vm, name))
    return (f"available {vm.available / GIB:.1f} of {vm.total / GIB:.1f} GiB, "
            f"reserve {load_reserve_bytes(vm.total) / GIB:.1f} GiB ({pages})")


def load_reserve_bytes(total_bytes: int) -> int:
    """The memory a model load must leave free: an eighth of RAM, at most ``SAFE_RESERVE_BYTES``.

    The one headroom rule for whether a model fits now, read by setup's pick
    and the preload check alike. A flat 16 GiB would leave a 24 GB Mac, the
    smallest that runs a local model, room for none.
    """
    return min(SAFE_RESERVE_BYTES, total_bytes // 8)


def pin_room_bytes() -> int:
    """The memory pinned models may take together: total RAM less the same load reserve.

    Fixed for a given Mac, so the pin limit never shifts with what else is running.
    """
    total = host_total_ram_bytes()
    return max(0, total - load_reserve_bytes(total))


def _room_bytes(available_bytes: int, total_bytes: int) -> int:
    return max(0, available_bytes - load_reserve_bytes(total_bytes))


def model_room_bytes() -> int:
    """The memory a model may take now: what is available less the load reserve."""
    return _room_bytes(host_available_ram_bytes(), host_total_ram_bytes())


@lru_cache(maxsize=1)
def _resolved_total_ram_bytes() -> int | None:
    """Read total RAM once, or return None when this machine will not say.

    Total RAM does not change while the process runs, so it is read once and
    held. ``None`` distinguishes a machine that could not be measured from one
    that is merely small, so the caller can say which it is handling.
    """
    try:
        return host_total_ram_bytes()
    except Exception as exc:
        logger.warning(
            "could not read this machine's total RAM (%s); limiting image "
            "decodes to the smallest supported machine's allowance of %d "
            "megapixels",
            exc,
            MIN_IMAGE_DECODE_PIXELS // 1_000_000,
        )
        return None


def image_decode_pixel_budget(concurrent_decodes: int) -> int:
    """Return the largest declared frame this host may decode, in pixels.

    The one owner of that number. Resolved on first use rather than at import,
    so the modules the service installer loads before any dependency exists stay
    importable, and so a machine that cannot be measured degrades to the floor
    instead of failing an import. An unmeasurable host is treated as the
    smallest supported one: that still opens an ordinary photograph, and
    guessing larger would commit memory this process cannot show it has.
    """
    total = _resolved_total_ram_bytes()
    if total is None:
        return MIN_IMAGE_DECODE_PIXELS
    return max_image_decode_pixels(total, concurrent_decodes)


def log_image_decode_budget(concurrent_decodes: int) -> None:
    """Record at startup why this machine refuses the frames it refuses.

    Reads the same accessor the decline check reads, so the number an operator
    sees at startup is the number that will actually refuse an image. Logged
    through loguru so it lands in the startup log an operator reads, beside the
    other host ceiling this process sets for itself.
    """
    from loguru import logger as startup_logger

    total = _resolved_total_ram_bytes()
    startup_logger.info(
        "image decode budget: {} allows {:.0f} megapixels per image across {} "
        "concurrent decodes",
        f"{total / (1024**3):.0f} GB total RAM" if total else "unmeasured RAM",
        image_decode_pixel_budget(concurrent_decodes) / 1_000_000,
        concurrent_decodes,
    )


def max_image_decode_pixels(total_ram_bytes: int, concurrent_decodes: int) -> int:
    """Return the largest declared frame this host may decode, in pixels.

    Pure in its inputs, so any machine's allowance can be asked for and tested
    without a real host. A fixed share of total RAM is divided by the cost of a
    decode and by how many may run at once, then held above the floor that keeps
    ordinary photographs working on a small machine. Monotonic in RAM: a larger
    machine never allows less than a smaller one.
    """
    slots = max(1, concurrent_decodes)
    allowance = total_ram_bytes * IMAGE_DECODE_RAM_FRACTION
    per_decode = allowance / (IMAGE_DECODE_BYTES_PER_PIXEL * slots)
    return max(MIN_IMAGE_DECODE_PIXELS, int(per_decode))


def _is_local_network_server(server: dict) -> bool:
    """Does this network_servers row reach the corsia host?"""
    from llm.network_endpoints import get_endpoints
    sid = server.get("id")
    if not sid:
        return False
    for ep in get_endpoints(sid):
        if ep.get("host") in ("127.0.0.1", "localhost"):
            return True
    return False


async def _gather_local_loaded() -> dict[str, list[dict[str, Any]]]:
    """Return loaded managed-engine models grouped by provider name.

    Only managed engines (oMLX) host models on this machine; External providers
    are user-run elsewhere and excluded from host RAM accounting."""
    from llm.managed_engine import get_managed_engine, is_managed
    from llm.network_discovery import get_servers

    out: dict[str, list[dict[str, Any]]] = {}

    for name in LOCAL_PROVIDER_TYPES:
        if not is_managed(name):
            continue
        engine = get_managed_engine(name)
        models: list[dict[str, Any]] = []
        try:
            for server in get_servers(provider=name, status="online"):
                if not _is_local_network_server(server):
                    continue
                cfg = {"server_id": server["id"], "provider": name}
                loaded = await engine.list_loaded(cfg)
                for m in (loaded or []):
                    memory_bytes = m.get("size_bytes", 0) or 0
                    models.append({
                        "model_id": m.get("identifier") or m.get("model_id") or "",
                        "memory_gb": float(memory_bytes) / (1024 ** 3),
                    })
        except Exception as e:
            logger.warning("host_resources: %s gather failed: %s", name, e)
        out[name] = models

    return out


async def get_host_resources() -> dict[str, Any]:
    """Snapshot this machine's RAM by provider in GiB."""
    import psutil

    vm = psutil.virtual_memory()
    total = vm.total
    free = vm.available
    used = total - free

    by_provider = await _gather_local_loaded()
    breakdown = {
        name: {
            "models": models,
            "used_gb": round(sum(m["memory_gb"] for m in models), 2),
        }
        for name, models in by_provider.items()
    }
    models_used_bytes = sum(p["used_gb"] for p in breakdown.values()) * GIB
    other_used_bytes = max(0, used - models_used_bytes)
    reserve = load_reserve_bytes(total)

    import socket
    return {
        "device_name": socket.gethostname().split(".")[0],
        "total_gb": round(total / GIB, 2),
        "free_gb": round(free / GIB, 2),
        "models_used_gb": round(models_used_bytes / GIB, 2),
        "other_used_gb": round(other_used_bytes / GIB, 2),
        "safe_free_gb": round(_room_bytes(free, total) / GIB, 2),
        "safe_reserve_gb": round(reserve / GIB, 2),
        "by_provider": breakdown,
    }


def fits_now(needed_gb: float, safe_free_gb: float) -> bool:
    """The one "fits now" rule: a model fits when what it needs is within the room a model may take now."""
    return safe_free_gb - needed_gb >= 0


def load_verdict(
    size_bytes: int | None, other_loaded_sizes: list[int | None], room_bytes: int,
) -> Literal["fits_now", "no_room", "no_memory"]:
    """The one verdict on loading a model now, from the memory a model may take (``room_bytes``).

    An unjudgeable fit may load only on an empty seat, where no model can be evicted.
    ``no_room`` includes an unjudgeable fit beside another model, without a promise that
    unloading it will help. ``no_memory`` means even unloading all others is insufficient.
    """
    if size_bytes is None or None in other_loaded_sizes:
        return "no_room" if other_loaded_sizes else "fits_now"
    if fits_now(size_bytes / GIB, room_bytes / GIB):
        return "fits_now"
    if not other_loaded_sizes:
        return "no_memory"
    freed = room_bytes + sum(s for s in other_loaded_sizes if s)
    return "no_room" if fits_now(size_bytes / GIB, freed / GIB) else "no_memory"


def pin_fits(size_bytes: int, other_pinned_bytes: int, room_bytes: int) -> bool:
    """The one "pins fit the room" rule: a model may be pinned when the pins stay within the room for pins."""
    return other_pinned_bytes + size_bytes <= room_bytes


def _gb_text(value_bytes: float, *, up: bool = False) -> int:
    """Whole GB for a sentence: rounded half up, or up when a shortfall must not read short."""
    gb = value_bytes / GIB
    return math.ceil(gb) if up else math.floor(gb + 0.5)


def pins_full_line(used_bytes: int, room_bytes: int, *, loaded: bool = False) -> str:
    """The one sentence for a model that can't be pinned: the pins already use the room for pins.

    ``loaded`` adds what that means for a model that is running now.
    """
    line = (f"Models kept loaded use {_gb_text(used_bytes)} of {_gb_text(room_bytes)} GB. "
            "Turn one off to keep this one loaded.")
    return f"{line} It’s loaded, but won’t load at the next start." if loaded else line


#: Without a size CORSIA can't judge the room for pins, so turning the switch on only loads the model.
NO_SIZE_LINE = "CORSIA can’t keep this model loaded: oMLX didn’t report its size. Turning it on loads it until the next start."


def not_loaded_line(size_bytes: int, safe_free_bytes: int) -> str | None:
    """The sentence for a pinned model that didn't load because it doesn't fit now; None when it fits."""
    if fits_now(size_bytes / GIB, safe_free_bytes / GIB):
        return None
    return (f"Not loaded: needs {_gb_text(size_bytes, up=True)} GB, "
            f"{_gb_text(safe_free_bytes)} GB free for a model now")


def annotate_keep_loaded(models: list[dict], failed: dict[str, str] | None = None) -> None:
    """Give each row of CORSIA's own oMLX what the switch and the line under it read, on every read.

    ``pin_fits`` says whether turning the model on may pin it. ``keep_line`` is the row's one
    sentence about keeping it loaded, and this is its only writer: a failed keep attempt
    (``failed``, by model id, from ``local_keep_loaded.failures``), else the no-size line for
    an unpinned model with no size (it can't be pinned), else pins full for an unpinned model
    the pins can't take, else a pinned model that didn't load because it doesn't fit now.
    """
    room, free = pin_room_bytes(), model_room_bytes()
    size_of = {m["id"]: int(m.get("size_bytes") or 0) for m in models}
    used = sum(size_of[m["id"]] for m in models if m.get("pinned"))
    for m in models:
        size = size_of[m["id"]]
        if size > 0:
            m["pin_fits"] = pin_fits(size, used - (size if m.get("pinned") else 0), room)
        line = (failed or {}).get(m["id"])
        if line is None and size <= 0 and not m.get("pinned"):
            line = NO_SIZE_LINE
        elif line is None and size > 0:
            if not m.get("pinned") and not m["pin_fits"]:
                line = pins_full_line(used, room, loaded=bool(m.get("loaded")))
            elif m.get("pinned") and not m.get("loaded") and not m.get("starting"):
                line = not_loaded_line(size, free)
        if line:
            m["keep_line"] = line


async def check_can_load(estimated_gb: float, target_provider: str) -> dict[str, Any]:
    """Decide whether loading a model of ``estimated_gb`` is safe."""
    if target_provider not in LOCAL_PROVIDER_TYPES:
        return {"ok": True, "free_gb": None, "warning": None, "remote": True}

    res = await get_host_resources()
    free = res["free_gb"]
    after_load = free - estimated_gb
    safe_free = res["safe_free_gb"]
    ok = fits_now(estimated_gb, model_room_bytes() / GIB)

    warning = None
    if not ok:
        warning = (
            f"Loading would leave {after_load:.1f} GB of memory free "
            f"(below the {res['safe_reserve_gb']:.0f} GB safe reserve)."
            if after_load >= 0 else
            f"Loading would need {-after_load:.1f} GB more memory than is free."
        )

    return {
        "ok": ok,
        "free_gb": free,
        "after_load_gb": round(after_load, 2),
        "safe_free_gb": safe_free,
        "safe_reserve_gb": res["safe_reserve_gb"],
        "warning": warning,
        "remote": False,
    }
