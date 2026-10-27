"""Join a private session keyring in a child before executing bubblewrap."""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path

# Linux syscall numbers for the supported 64-bit architectures. Unknown ABIs
# fail closed instead of risking a call to an unrelated syscall.
_KEYCTL_SYSCALL = {'x86_64': 250, 'aarch64': 219}
_KEYCTL_JOIN_SESSION_KEYRING = 1


def launcher_argv(argv: list[str]) -> list[str]:
    return [sys.executable, '-I', str(Path(__file__).resolve()), *argv]


def main(argv: list[str]) -> int:
    try:
        if sys.platform != 'linux' or not argv:
            raise RuntimeError('Linux keyring launcher requires a command on Linux')
        number = _KEYCTL_SYSCALL.get(platform.machine())
        if number is None:
            raise RuntimeError(f'unsupported Linux architecture: {platform.machine()}')
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        result = libc.syscall(ctypes.c_long(number), ctypes.c_long(_KEYCTL_JOIN_SESSION_KEYRING),
                              ctypes.c_void_p(None), ctypes.c_long(0), ctypes.c_long(0), ctypes.c_long(0))
        if result == -1:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        os.execv(argv[0], argv)
    except (OSError, RuntimeError) as exc:
        print(f'Linux sandbox keyring launcher: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
