"""Host RAM accounting for local provider preload checks."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


SAFE_RESERVE_BYTES = 16 * 1024 * 1024 * 1024

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
    GIB = 1024 ** 3
    models_used_bytes = sum(p["used_gb"] for p in breakdown.values()) * GIB
    other_used_bytes = max(0, used - models_used_bytes)

    import socket
    return {
        "device_name": socket.gethostname().split(".")[0],
        "total_gb": round(total / GIB, 2),
        "free_gb": round(free / GIB, 2),
        "models_used_gb": round(models_used_bytes / GIB, 2),
        "other_used_gb": round(other_used_bytes / GIB, 2),
        "safe_free_gb": round(max(0, free - SAFE_RESERVE_BYTES) / GIB, 2),
        "safe_reserve_gb": round(SAFE_RESERVE_BYTES / (1024 ** 3), 2),
        "by_provider": breakdown,
    }


async def check_can_load(estimated_gb: float, target_provider: str) -> dict[str, Any]:
    """Decide whether loading a model of ``estimated_gb`` is safe."""
    if target_provider not in LOCAL_PROVIDER_TYPES:
        return {"ok": True, "free_gb": None, "warning": None, "remote": True}

    res = await get_host_resources()
    free = res["free_gb"]
    after_load = free - estimated_gb
    safe_free = res["safe_free_gb"]
    safe_after_load = safe_free - estimated_gb
    ok = safe_after_load >= 0

    warning = None
    if not ok:
        warning = (
            f"Loading would leave {after_load:.1f} GB free "
            f"(below {res['safe_reserve_gb']:.0f} GB safe reserve)."
            if after_load >= 0 else
            f"Loading would exceed available RAM by {-after_load:.1f} GB. "
            f"OS will swap or kill processes."
        )

    return {
        "ok": ok,
        "free_gb": free,
        "after_load_gb": round(after_load, 2),
        "safe_free_gb": safe_free,
        "warning": warning,
        "remote": False,
    }
