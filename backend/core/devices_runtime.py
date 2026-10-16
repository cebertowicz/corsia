"""One runtime owner for the Devices switch's listener and pairing state."""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.device_pairing import DevicePairing
from infra.devices_listener import DevicesListener, lan_addresses, local_hostname


@dataclass(slots=True)
class DevicesRuntime:
    pairing: DevicePairing
    listener: DevicesListener | None = None

    def __init__(self) -> None:
        self.pairing = DevicePairing()
        self.listener = None

    @property
    def enabled(self) -> bool:
        return self.pairing.enabled

    async def enable(self, app) -> bool:
        self.pairing.enable()
        self.listener = DevicesListener(app)
        if not await self.listener.start():
            self.pairing.disable()
            return False
        return True

    async def disable(self) -> None:
        self.pairing.disable()
        if self.listener is not None:
            await self.listener.stop()
            self.listener = None

    async def restore(self, app, enabled: bool) -> None:
        """Restore only in an installed runtime, never dev/test state."""
        if (
            enabled
            and os.environ.get("CORSIA_DEVICES_DISABLE") != "1"
            and os.environ.get("CORSIA_TEST_MODE") != "1"
            and "CORSIA_STATE_DIR" not in os.environ
        ):
            await self.enable(app)

    def snapshot(self, *, include_code: bool = False) -> dict:
        listening = self.listener.listening if self.listener is not None else {"error": None}
        announcement = self.listener.announcement if self.listener is not None else None
        listener_active = self.listener is not None and self.listener.port is not None
        return {
            "enabled": self.enabled,
            "listening": listening,
            "announced_name": announcement.announced_name if announcement else None,
            "local_hostname": local_hostname(),
            "lan_addresses": lan_addresses(include_link_local=listener_active),
            "code": self.pairing.status(include_code=include_code),
        }


runtime = DevicesRuntime()
