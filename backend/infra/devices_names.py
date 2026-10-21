"""The names a host answers to: its mDNS host, the person's computer name, and the one every remote-connect surface shows."""

from __future__ import annotations

import ctypes
import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.dns_sd import DNSServiceAnnouncement


def local_hostname() -> str:
    """Return the host's mDNS name without exposing interface state."""
    hostname = socket.gethostname().rstrip(".").lower()
    return hostname if hostname.endswith(".local") else f"{hostname}.local"


def _load_macos_frameworks():
    return (
        ctypes.CDLL("/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration"),
        ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"),
    )


def macos_computer_name() -> str | None:
    """Return macOS's user-configured computer name when it is available."""
    if sys.platform != "darwin":
        return None
    try:
        system_configuration, core_foundation = _load_macos_frameworks()
        copy_name = system_configuration.SCDynamicStoreCopyComputerName
        copy_name.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        copy_name.restype = ctypes.c_void_p
        name = copy_name(None, None)
        if not name:
            return None
        string_length = core_foundation.CFStringGetLength
        string_length.argtypes = [ctypes.c_void_p]
        string_length.restype = ctypes.c_long
        max_size = core_foundation.CFStringGetMaximumSizeForEncoding
        max_size.argtypes = [ctypes.c_long, ctypes.c_uint32]
        max_size.restype = ctypes.c_long
        get_string = core_foundation.CFStringGetCString
        get_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        get_string.restype = ctypes.c_bool
        release = core_foundation.CFRelease
        release.argtypes = [ctypes.c_void_p]
        release.restype = None
        try:
            buffer = ctypes.create_string_buffer(max_size(string_length(name), 0x08000100) + 1)
            if get_string(name, buffer, len(buffer), 0x08000100):
                return buffer.value.decode("utf-8")
        finally:
            release(name)
    except (AttributeError, OSError, UnicodeDecodeError, ValueError):
        return None
    return None


def computer_name() -> str:
    """The person's name for this computer, else its mDNS host."""
    return macos_computer_name() or local_hostname()


def remote_connect_name(announcement: DNSServiceAnnouncement | None) -> str:
    """Resolve the one name presented by every remote-connect surface."""
    if announcement is not None and announcement.announced_name:
        return announcement.announced_name
    return computer_name()
