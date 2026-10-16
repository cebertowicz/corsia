from __future__ import annotations

import socket

import pytest

from infra import dns_sd


class _CFunction:
    def __init__(self, handler):
        self.handler = handler
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.handler(*args)


class _DNSLibrary:
    def __init__(self, *, register_results, process_results=()):
        self.register_results = list(register_results)
        self.process_results = list(process_results)
        self.register_calls = []
        self.deallocated = []
        self.callback = None
        self.DNSServiceRegister = _CFunction(self._register)
        self.DNSServiceRefSockFD = _CFunction(self._socket_fd)
        self.DNSServiceProcessResult = _CFunction(self._process)
        self.DNSServiceRefDeallocate = _CFunction(self._deallocate)

    def _register(self, reference, *args):
        self.register_calls.append(args)
        self.callback = args[-2]
        result = self.register_results.pop(0)
        if result == dns_sd._KDNS_SERVICE_ERR_NO_ERROR:
            reference._obj.value = len(self.register_calls)
        return result

    @staticmethod
    def _socket_fd(reference):
        return 100 + reference.value

    def _process(self, _reference):
        result = self.process_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def _deallocate(self, reference):
        self.deallocated.append(reference.value)


class _Loop:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_reader(self, fd, callback, *args):
        self.added.append((fd, callback, args))

    def remove_reader(self, fd):
        self.removed.append(fd)


def _install_dns_seams(monkeypatch, library):
    loop = _Loop()
    monkeypatch.setattr(dns_sd.ctypes, "CDLL", lambda _path: library)
    monkeypatch.setattr(dns_sd.ctypes, "CFUNCTYPE", lambda *_signature: lambda callback: callback)
    monkeypatch.setattr(dns_sd.asyncio, "get_running_loop", lambda: loop)
    return loop


def test_dns_sd_registers_the_listener_port_with_no_auto_rename(monkeypatch):
    library = _DNSLibrary(register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR])
    loop = _install_dns_seams(monkeypatch, library)
    announcement = dns_sd.DNSServiceAnnouncement(47902)

    assert announcement.start() is True

    flags, interface_index, name, service_type, domain, host, port, txt_len, txt, _callback, context = (
        library.register_calls[0]
    )
    assert flags == dns_sd._KDNS_SERVICE_FLAGS_NO_AUTO_RENAME
    assert (interface_index, name, service_type, domain, host, port, txt_len, txt, context) == (
        0,
        None,
        b"_corsia._tcp",
        None,
        None,
        socket.htons(47902),
        0,
        None,
        None,
    )
    assert loop.added[0][0] == 101


def test_dns_sd_conflict_stays_unannounced_without_retry(monkeypatch):
    conflict = -65548
    library = _DNSLibrary(register_results=[conflict])
    loop = _install_dns_seams(monkeypatch, library)
    announcement = dns_sd.DNSServiceAnnouncement(47901)

    assert announcement.start() is False
    assert announcement.announced_name is None
    assert announcement.error == f"DNS-SD registration failed ({conflict})"
    assert len(library.register_calls) == 1
    assert loop.added == []


def test_dns_sd_callback_failure_clears_the_announced_name(monkeypatch):
    conflict = -65548
    library = _DNSLibrary(register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR])
    _install_dns_seams(monkeypatch, library)
    announcement = dns_sd.DNSServiceAnnouncement(47901)

    assert announcement.start() is True
    announcement.announced_name = "Previous name"
    library.callback(None, 0, conflict, None, None, None, None)

    assert announcement.announced_name is None
    assert announcement.error == f"DNS-SD registration failed ({conflict})"


@pytest.mark.parametrize(
    "first_process_result",
    [
        dns_sd._KDNS_SERVICE_ERR_SERVICE_NOT_RUNNING,
        dns_sd._KDNS_SERVICE_ERR_DEFUNCT_CONNECTION,
    ],
)
def test_dns_sd_daemon_loss_deallocates_and_reregisters_once(monkeypatch, first_process_result):
    library = _DNSLibrary(
        register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR, dns_sd._KDNS_SERVICE_ERR_NO_ERROR],
        process_results=[first_process_result, dns_sd._KDNS_SERVICE_ERR_SERVICE_NOT_RUNNING],
    )
    loop = _install_dns_seams(monkeypatch, library)
    announcement = dns_sd.DNSServiceAnnouncement(47901)

    assert announcement.start() is True
    announcement.announced_name = "Previous name"
    announcement._process(library)

    assert library.deallocated == [1]
    assert len(library.register_calls) == 2
    assert loop.removed == [101]
    assert [entry[0] for entry in loop.added] == [101, 102]
    assert announcement.error is None
    assert announcement.announced_name is None

    announcement._process(library)

    assert library.deallocated == [1, 2]
    assert len(library.register_calls) == 2
    assert announcement.error == "DNS-SD process failed (-65563)"
    assert announcement.announced_name is None


def test_dns_sd_process_propagates_unexpected_eof(monkeypatch):
    library = _DNSLibrary(
        register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR],
        process_results=[EOFError()],
    )
    _install_dns_seams(monkeypatch, library)
    announcement = dns_sd.DNSServiceAnnouncement(47901)

    assert announcement.start() is True
    with pytest.raises(EOFError):
        announcement._process(library)


def test_dns_sd_stop_clears_the_last_announced_name(monkeypatch):
    library = _DNSLibrary(register_results=[dns_sd._KDNS_SERVICE_ERR_NO_ERROR])
    _install_dns_seams(monkeypatch, library)
    announcement = dns_sd.DNSServiceAnnouncement(47901)

    assert announcement.start() is True
    announcement.announced_name = "Previous name"
    announcement.stop(library)

    assert announcement.announced_name is None
