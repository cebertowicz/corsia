"""The Chrome process group owned by one managed browser session."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import psutil

from host_access import browser_launch, browser_ports

LISTENER_UNVERIFIED = "managed browser listener ownership could not be verified"
ENDPOINT_MISMATCH = "managed browser endpoint did not match its reserved listener"
CHROME_EXITED = "managed Chrome exited during startup"
CHROME_NOT_STARTED = "managed Chrome did not start its reserved listener"

# agent-browser 0.31.2 native Chrome defaults, captured from a normal launch.
# Keep the normal flags here: managed sessions attach rather than launching again.
CHROME_FLAGS = (
    "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
    "--disable-backgrounding-occluded-windows", "--disable-component-update", "--disable-default-apps",
    "--disable-hang-monitor", "--disable-popup-blocking", "--disable-prompt-on-repost", "--disable-sync",
    "--disable-features=Translate", "--enable-features=NetworkService,NetworkServiceInProcess",
    "--metrics-recording-only", "--password-store=basic", "--use-mock-keychain",
)


def chrome_argv(executable: str, port: int, profile: Path, *, headless: bool, ua: str | None) -> list[str]:
    """The single launch argument source for saved and temporary profiles."""
    # Chrome deletes session cookies at startup unless it is restoring the last session, so without it
    # a sign-in that set only session cookies is gone after any close of the saved profile.
    flags = [*CHROME_FLAGS, "--restore-last-session", f"--remote-debugging-port={port}",
             "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={profile}", *browser_launch.stealth_args(ua, headless=headless).splitlines()]
    if headless:
        flags.append("--headless=new")
    return [executable, *flags]


def require_listener(pid: int, port: int) -> None:
    """Verify the listener on the owned child, never by inspecting unrelated processes."""
    connections = psutil.Process(pid).net_connections(kind="tcp")
    if not any(item.status == psutil.CONN_LISTEN and item.laddr.port == port
               and item.laddr.ip in {"127.0.0.1", "::1"} for item in connections):
        raise RuntimeError(LISTENER_UNVERIFIED)


def browser_endpoint(port: int) -> str:
    with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3) as response:
        endpoint = json.load(response).get("webSocketDebuggerUrl")
    if not isinstance(endpoint, str) or not endpoint.startswith(f"ws://127.0.0.1:{port}/"):
        raise RuntimeError(ENDPOINT_MISMATCH)
    return endpoint


@dataclass
class ChromeProcess:
    """Own the child handle, group, reserved stream socket and temporary profile."""

    process: asyncio.subprocess.Process
    cdp_port: int
    stream_port: int
    stream_reservation: socket.socket | None
    temporary_profile: Path | None
    endpoint: str = ""
    closed: bool = False

    @classmethod
    async def launch(cls, executable: str, *, profile: str | None, headless: bool, ua: str | None) -> ChromeProcess:
        cdp, stream = browser_ports.reserve_pair()
        temporary = None
        child = None
        owned = None
        try:
            if profile is None:
                temporary = Path(tempfile.mkdtemp(prefix="session-", dir=browser_launch.profile_root()))
            target = Path(profile) if profile else temporary
            assert target is not None
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            cdp_port, stream_port = cdp.getsockname()[1], stream.getsockname()[1]
            argv = chrome_argv(executable, cdp_port, target, headless=headless, ua=ua)
            cdp.close()
            # Shield the spawn so cancellation cannot lose a newly created child handle.
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True))
            try:
                child = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                child = await spawning
                raise
            owned = cls(child, cdp_port, stream_port, stream, temporary)
            for _ in range(100):
                if child.returncode is not None:
                    raise RuntimeError(CHROME_EXITED)
                try:
                    await asyncio.to_thread(require_listener, child.pid, cdp_port)
                    owned.endpoint = await asyncio.to_thread(browser_endpoint, cdp_port)
                    return owned
                except (OSError, ValueError, RuntimeError, psutil.Error):
                    await asyncio.sleep(0.1)
            raise RuntimeError(CHROME_NOT_STARTED)
        except BaseException:
            cdp.close()
            stream.close()
            if owned is not None:
                await asyncio.shield(owned.close())
            elif child is not None:
                await asyncio.shield(cls(child, 0, 0, None, temporary).close())
            elif temporary is not None:
                shutil.rmtree(temporary)
            raise

    def release_stream(self) -> None:
        if self.stream_reservation is not None:
            self.stream_reservation.close()
            self.stream_reservation = None

    async def close(self) -> None:
        """Flush the saved profile, then terminate every remaining group member."""
        if self.closed:
            return
        self.release_stream()
        if self.endpoint and self.process.returncode is None:
            from host_access.browser_cdp import CdpClient
            with contextlib.suppress(Exception):
                async with CdpClient(self.endpoint) as cdp:
                    await asyncio.wait_for(cdp.call("Browser.close"), 2)
            # Browser.close acknowledges before Chrome finishes flushing the profile.
            # Give that exit time to complete before signalling the remaining group.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), 5)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        # The leader may have exited before a helper. The private group remains ours.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        for _ in range(50):
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                if self.temporary_profile is not None:
                    shutil.rmtree(self.temporary_profile)
                    self.temporary_profile = None
                self.closed = True
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("managed Chrome process group has not exited")
