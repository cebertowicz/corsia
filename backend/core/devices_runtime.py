"""One runtime owner for the Devices switch's listener and pairing state."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from core.device_pairing import DevicePairing
from infra.config import STATE_ROOT, _read_prod_state_root
from infra.devices_listener import DevicesListener, lan_addresses
from infra.devices_names import computer_name, local_hostname, remote_connect_name
from infra.dns_sd import DNSServiceAnnouncement
from infra.state.ws import schedule_devices_updated


NameResolver = Callable[[DNSServiceAnnouncement | None], str]
_DEVICES_DISABLED_ERROR = "Devices listener disabled"


def _matches_bootstrapped_state_root() -> bool:
    """Whether this process owns the state root selected by the installed bootstrap."""
    bootstrapped_root = _read_prod_state_root()
    return bootstrapped_root is not None and STATE_ROOT == bootstrapped_root


@dataclass(slots=True)
class DevicesRuntime:
    pairing: DevicePairing
    listener: DevicesListener | None = None
    _name_resolver: NameResolver = field(init=False, repr=False)

    def __init__(self, *, name_resolver: NameResolver | None = None) -> None:
        self.pairing = DevicePairing(on_update=schedule_devices_updated)
        self.listener = None
        self._name_resolver = name_resolver or remote_connect_name

    @property
    def enabled(self) -> bool:
        return self.pairing.enabled

    async def enable(self, app) -> bool:
        if os.environ.get("CORSIA_DEVICES_DISABLE") == "1":
            await self.disable()
            self.listener = DevicesListener(app, error=_DEVICES_DISABLED_ERROR, reason="disabled")
            return False
        if self.listener is not None and self.listener.task is not None:
            return True
        self.pairing.enable()
        self.listener = DevicesListener(app, on_announcement_change=schedule_devices_updated)
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
            and _matches_bootstrapped_state_root()
        ):
            await self.enable(app)

    def remote_connect_name(self) -> str:
        announcement = self.listener.announcement if self.listener is not None else None
        return self._name_resolver(announcement)

    def snapshot(self, *, include_code: bool = False) -> dict:
        listening = self.listener.listening if self.listener is not None else {"error": None}
        announcement = self.listener.announcement if self.listener is not None else None
        listener_active = self.listener is not None and self.listener.port is not None
        return {
            "enabled": self.enabled,
            "listening": listening,
            "announced_name": announcement.announced_name if announcement else None,
            # The host a joiner can type (studio.local), never the display name: that has spaces and does not resolve.
            "local_hostname": local_hostname(),
            # What the page shows while nothing is announced: the person's own name for this computer.
            "computer_name": computer_name(),
            "lan_addresses": lan_addresses(include_link_local=listener_active),
            "code": self.pairing.status(include_code=include_code),
        }


runtime = DevicesRuntime()
