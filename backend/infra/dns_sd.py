"""System DNS-SD announcement for the Devices listener.

The listener owns this object; no route or settings path calls DNS-SD directly.
"""

from __future__ import annotations

import asyncio
import ctypes
import socket
from dataclasses import dataclass


_SERVICE_TYPE = b"_corsia._tcp"
_KDNS_SERVICE_FLAGS_NO_AUTO_RENAME = 0x0008
_KDNS_SERVICE_ERR_NO_ERROR = 0
_KDNS_SERVICE_ERR_SERVICE_NOT_RUNNING = -65563
_KDNS_SERVICE_ERR_DEFUNCT_CONNECTION = -65569


def _load_dns_sd_library():
    """Use Darwin's process-exported DNS-SD symbols across system releases."""
    return ctypes.CDLL(None)


@dataclass(slots=True)
class DNSServiceAnnouncement:
    """Own one names-only registration and its event-loop reader."""

    port: int
    announced_name: str | None = None
    error: str | None = None
    _ref: ctypes.c_void_p | None = None
    _fd: int | None = None
    _callback: object | None = None
    _restarted: bool = False

    def start(self) -> bool:
        try:
            library = _load_dns_sd_library()
        except OSError as exc:
            self.announced_name = None
            self.error = type(exc).__name__
            return False
        callback_type = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
        )

        def registered(_ref, _flags, error_code, name, _regtype, _domain, _context):
            if error_code == _KDNS_SERVICE_ERR_NO_ERROR:
                self.announced_name = name.decode("utf-8") if name else None
                self.error = None
            else:
                self.announced_name = None
                self.error = f"DNS-SD registration failed ({error_code})"

        self._callback = callback_type(registered)
        reference = ctypes.c_void_p()
        register = library.DNSServiceRegister
        register.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_uint16, ctypes.c_uint16, ctypes.c_void_p, callback_type, ctypes.c_void_p,
        ]
        register.restype = ctypes.c_int
        error = register(
            ctypes.byref(reference), _KDNS_SERVICE_FLAGS_NO_AUTO_RENAME, 0,
            None, _SERVICE_TYPE, None, None, socket.htons(self.port), 0, None,
            self._callback, None,
        )
        if error != _KDNS_SERVICE_ERR_NO_ERROR:
            self.announced_name = None
            self.error = f"DNS-SD registration failed ({error})"
            return False
        socket_fd = library.DNSServiceRefSockFD
        socket_fd.argtypes = [ctypes.c_void_p]
        socket_fd.restype = ctypes.c_int
        self._ref = reference
        self._fd = socket_fd(reference)
        asyncio.get_running_loop().add_reader(self._fd, self._process, library)
        return True

    def _process(self, library) -> None:
        assert self._ref is not None
        process = library.DNSServiceProcessResult
        process.argtypes = [ctypes.c_void_p]
        process.restype = ctypes.c_int
        result = process(self._ref)
        if result == _KDNS_SERVICE_ERR_NO_ERROR:
            return
        self.announced_name = None
        self.stop(library)
        if result in {
            _KDNS_SERVICE_ERR_SERVICE_NOT_RUNNING,
            _KDNS_SERVICE_ERR_DEFUNCT_CONNECTION,
        } and not self._restarted:
            self._restarted = True
            self.start()
            return
        self.error = f"DNS-SD process failed ({result})"

    def stop(self, library=None) -> None:
        self.announced_name = None
        if self._ref is None:
            return
        if self._fd is not None:
            asyncio.get_running_loop().remove_reader(self._fd)
        library = library or _load_dns_sd_library()
        deallocate = library.DNSServiceRefDeallocate
        deallocate.argtypes = [ctypes.c_void_p]
        deallocate(self._ref)
        self._ref = None
        self._fd = None
        self._callback = None
