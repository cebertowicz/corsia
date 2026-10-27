"""A bounded, privacy-preserving view of host CPU usage."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import psutil

from tools.base import Tool

_SAMPLE_SECONDS = 1.0


def _sample(limit: int) -> dict[str, Any]:
    processes = []
    unreadable = 0
    for process in psutil.process_iter():
        try:
            process.cpu_percent(interval=None)
            processes.append(process)
        except (psutil.Error, OSError):
            unreadable += 1
    time.sleep(_SAMPLE_SECONDS)
    rows = []
    for process in processes:
        try:
            rows.append({
                "pid": process.pid,
                "name": os.path.basename(process.name()),
                "cpu_percent": process.cpu_percent(interval=None),
                "memory_rss_bytes": process.memory_info().rss,
                "start_time": process.create_time(),
            })
        except (psutil.Error, OSError):
            unreadable += 1
    rows.sort(key=lambda row: row["cpu_percent"], reverse=True)
    return {
        "processes": rows[:limit],
        "unreadable_processes": unreadable,
        "load_averages": psutil.getloadavg(),
        "core_count": psutil.cpu_count(),
        "sample_seconds": _SAMPLE_SECONDS,
    }


class HostCpuTopTool(Tool):
    name = "host_cpu_top"
    description = (
        "Identify host processes using CPU without a host permit. Samples for one "
        "second; returns up to 50 processes and counts unreadable processes. CPU "
        "percent is per core, memory is resident bytes, and start time is Unix seconds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
        "additionalProperties": False,
    }

    async def execute(self, limit: int = 20, **kwargs: Any) -> str:
        return json.dumps(await asyncio.to_thread(_sample, max(1, min(limit, 50))))
