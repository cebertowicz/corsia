from __future__ import annotations

import socket
from types import SimpleNamespace

from infra import devices_names
from infra.devices_names import computer_name, local_hostname, macos_computer_name, remote_connect_name


def test_local_hostname_is_a_lowercase_mdns_name(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Studio.Local.")

    assert local_hostname() == "studio.local"


def test_macos_computer_name_decodes_a_cfstring_as_utf8(monkeypatch):
    name = "Stúdió Mac"
    copied_name = object()
    released = []

    def copy_name(store, encoding):
        assert (store, encoding) == (None, None)
        return copied_name

    def string_length(value):
        assert value is copied_name
        return len(name)

    def max_size(length, encoding):
        assert (length, encoding) == (len(name), 0x08000100)
        return len(name.encode("utf-8"))

    def get_string(value, buffer, size, encoding):
        assert (value, size, encoding) == (copied_name, len(name.encode("utf-8")) + 1, 0x08000100)
        buffer.value = name.encode("utf-8")
        return True

    def release(value):
        released.append(value)

    system_configuration = SimpleNamespace(SCDynamicStoreCopyComputerName=copy_name)
    core_foundation = SimpleNamespace(
        CFStringGetLength=string_length,
        CFStringGetMaximumSizeForEncoding=max_size,
        CFStringGetCString=get_string,
        CFRelease=release,
    )
    monkeypatch.setattr(devices_names.sys, "platform", "darwin")
    monkeypatch.setattr(
        devices_names,
        "_load_macos_frameworks",
        lambda: (system_configuration, core_foundation),
    )

    assert macos_computer_name() == name
    assert released == [copied_name]


def test_remote_connect_name_uses_announced_name_before_macos_name(monkeypatch):
    monkeypatch.setattr(devices_names, "macos_computer_name", lambda: "System name")
    monkeypatch.setattr(devices_names, "local_hostname", lambda: "fallback.local")

    assert remote_connect_name(SimpleNamespace(announced_name="Announced name")) == "Announced name"
    assert remote_connect_name(SimpleNamespace(announced_name=None)) == "System name"


def test_remote_connect_name_falls_back_to_local_hostname(monkeypatch):
    monkeypatch.setattr(devices_names, "macos_computer_name", lambda: None)
    monkeypatch.setattr(devices_names, "local_hostname", lambda: "fallback.local")

    assert remote_connect_name(None) == "fallback.local"


def test_computer_name_is_the_persons_own_name_not_the_mdns_host(monkeypatch):
    monkeypatch.setattr(devices_names, "macos_computer_name", lambda: "Studio Mac")
    monkeypatch.setattr(devices_names, "local_hostname", lambda: "studio.local")

    assert computer_name() == "Studio Mac"

    monkeypatch.setattr(devices_names, "macos_computer_name", lambda: None)
    assert computer_name() == "studio.local"
