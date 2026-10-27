import ctypes
import errno
import sys

import pytest

from tools import linux_keyring_launcher as launcher


def test_launcher_joins_anonymous_keyring_before_exec(monkeypatch):
    calls = []
    class Syscall:
        restype = None
        def __call__(self, *args):
            calls.append(('syscall', args))
            return 123
    class Libc:
        syscall = Syscall()
    monkeypatch.setattr(launcher.sys, 'platform', 'linux')
    monkeypatch.setattr(launcher.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(launcher.ctypes, 'CDLL', lambda *args, **kwargs: Libc())
    monkeypatch.setattr(launcher.os, 'execv', lambda path, argv: calls.append(('exec', path, argv)))
    assert launcher.main(['/usr/bin/bwrap', '--version']) == 0
    args = calls[0][1]
    assert [arg.value for arg in args] == [250, 1, None, 0, 0, 0]
    assert calls[1] == ('exec', '/usr/bin/bwrap', ['/usr/bin/bwrap', '--version'])


def test_join_failure_does_not_execute(monkeypatch, capsys):
    class Syscall:
        restype = None
        def __call__(self, *args):
            ctypes.set_errno(errno.EPERM)
            return -1
    class Libc:
        syscall = Syscall()
    monkeypatch.setattr(launcher.sys, 'platform', 'linux')
    monkeypatch.setattr(launcher.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(launcher.ctypes, 'CDLL', lambda *args, **kwargs: Libc())
    monkeypatch.setattr(launcher.os, 'execv', lambda *args: pytest.fail('must not exec'))
    assert launcher.main(['/usr/bin/bwrap']) == 1
    assert 'Operation not permitted' in capsys.readouterr().err


def test_unknown_abi_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, 'platform', 'linux')
    monkeypatch.setattr(launcher.platform, 'machine', lambda: 'unknown')
    monkeypatch.setattr(launcher.os, 'execv', lambda *args: pytest.fail('must not exec'))
    assert launcher.main(['/usr/bin/bwrap']) == 1
    assert 'unsupported Linux architecture' in capsys.readouterr().err


def test_argv_uses_isolated_interpreter_and_script():
    argv = launcher.launcher_argv(['/usr/bin/bwrap', '--version'])
    assert argv[:2] == [sys.executable, '-I']
    assert argv[2] == launcher.__file__
    assert argv[3:] == ['/usr/bin/bwrap', '--version']
