"""Where the person's CLI API token lives, and how it is written there.

The token has its own folder at a fixed place under the home folder. The
shell floor closes that folder to every agent route, leases included, so the
location is defined here once, depends on nothing but the home folder it is
given, and never on the environment. The agent CLIs (``scripts/corsia_api.py``)
and ``corsia token create --write-delegate-file`` import it as well.

``CORSIA_API_TOKEN_FILE`` still points the CLIs at another file. That file is
the person's own choice and lies outside the floor.

Standard library only: the CLIs load this module without the backend's
dependencies.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

FOLDER_PARTS = (".corsia", "credentials")
FILE_NAME = "api-token"
# Where earlier versions kept the token: the home folder's ``.corsia``, and the
# checkout root, the shell wrapper's old default.
_LEGACY_NAME = ".api-token"
_CHECKOUT = Path(__file__).resolve().parents[2]


def api_token_folder(home: Path) -> Path:
    """The token folder under ``home``: a credential root of the shell floor."""
    return home.joinpath(*FOLDER_PARTS)


def api_token_file(home: Path) -> Path:
    """The token file the CLIs read when no override is set."""
    return api_token_folder(home) / FILE_NAME


def write_secret_file(path: Path, value: str) -> None:
    """Write ``value`` to ``path`` atomically and readable only by its owner.

    A missing folder is created at mode 700. The temporary file is created new
    at mode 600, never through an existing path or link, so the secret is never
    briefly readable by anyone else, and it replaces ``path`` atomically, so a
    reader sees the old credential or the new one and never a half-written file.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(value)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def move_legacy_token(home: Path, override: Path | None, checkout: Path = _CHECKOUT) -> None:
    """Move a token left at an earlier location into the token folder, once.

    Afterwards no earlier copy remains. When the folder already holds a token,
    an earlier copy is deleted rather than merged. Each step leaves either the
    earlier copy or the new file in place, so an interrupted move finishes on
    the next run. A file the person named in ``CORSIA_API_TOKEN_FILE`` is theirs
    and stays where it is. Nothing here prints or logs the token.
    """
    target = api_token_file(home)
    for legacy in (home / ".corsia" / _LEGACY_NAME, checkout / _LEGACY_NAME):
        # lexists reads a folder this process cannot reach as empty: a shell
        # that cannot see the earlier copy cannot move it either.
        if not os.path.lexists(legacy) or (override is not None and _same_file(legacy, override)):
            continue
        if os.path.lexists(target):
            legacy.unlink()
            continue
        if legacy.is_symlink():
            _copy_then_remove(legacy, target)
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(legacy, 0o600)
        try:
            os.rename(legacy, target)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            _copy_then_remove(legacy, target)


def _copy_then_remove(legacy: Path, target: Path) -> None:
    """Write the token through the hardened writer, then remove the earlier copy."""
    write_secret_file(target, legacy.read_text())
    legacy.unlink()


def _same_file(first: Path, second: Path) -> bool:
    return os.path.abspath(first) == os.path.abspath(second)
