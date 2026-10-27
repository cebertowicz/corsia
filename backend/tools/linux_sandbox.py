"""Linux protected mounts, rendered from the existing host-path inventory."""
from __future__ import annotations

import errno
import hashlib
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from tools.linux_keyring_launcher import launcher_argv

HOME_RW = (
    '.cache/uv', '.cache/pip', '.cache/npm', '.cache/node', '.cache/go-build',
    '.cache/pnpm', '.cache/yarn', '.npm', '.cargo', '.rustup', 'go',
    '.local/share/uv', '.local/share/pnpm', '.local/share/fnm',
)
logger = logging.getLogger(__name__)
HOME_CACHES = tuple(path for path in HOME_RW if path.startswith('.cache/') or path == '.npm')
HOME_RO = ('.gitconfig', '.config/git', '.local/bin')
PERSISTENCE = ('.config/systemd/user', '.config/autostart', '.bashrc',
               '.profile', '.bash_profile', '.zshrc', '.zprofile')
SYSTEM_ROOTS = ('/usr', '/etc', '/opt', '/sys')
NAMESPACES = ('--die-with-parent', '--new-session', '--unshare-user',
              '--disable-userns', '--unshare-pid', '--unshare-ipc', '--unshare-uts')


def thread_tmp_root(state_root: Path, thread_id: str) -> Path:
    if not thread_id:
        raise ValueError('Linux sandbox requires a thread identity')
    return state_root / 'sandbox-tmp' / hashlib.sha256(thread_id.encode()).hexdigest()


def cleanup_thread_tmp(state_root: Path, thread_id: str) -> None:
    class RetryRemoval(Exception):
        pass

    root = thread_tmp_root(state_root, thread_id)

    def restore_access(_function, path, error):
        if not isinstance(error[1], PermissionError):
            raise error[1]
        # A failed unlink or rmdir can be caused by the parent directory.
        for target in (Path(path), Path(path).parent):
            if not target.is_relative_to(root) or target.is_symlink() or not target.is_dir():
                continue
            mode = os.stat(target, follow_symlinks=False).st_mode
            if mode & 0o700 != 0o700:
                os.chmod(target, mode | 0o700, follow_symlinks=False)
                raise RetryRemoval
        raise error[1]

    while True:
        try:
            shutil.rmtree(root, onerror=restore_access)
            return
        except RetryRemoval:
            continue
        except FileNotFoundError:
            return


def _prepare_home_caches(home: Path, protected_cache: Path | None = None) -> None:
    """Create cache paths without traversing symlinks; the protected cache is required."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    paths = [(home / relative, False) for relative in HOME_CACHES]
    if protected_cache is not None:
        paths.append((protected_cache, True))
    for path, required in paths:
        base = home if path.is_relative_to(home) else Path('/')
        current = None
        try:
            current = os.open(base, flags)
            for part in path.relative_to(base).parts:
                created = False
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
                if created:
                    os.fchmod(current, 0o700)
        except OSError as exc:
            if required:
                raise ValueError('Cannot protect the CORSIA runtime cache: check that its path is accessible and contains no symlinks.') from exc
            if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            logger.warning('Skipping unavailable cache path %s: %s', path, exc)
        finally:
            if current is not None:
                os.close(current)


def _canonical(paths: Sequence[Path]) -> list[Path]:
    return list(dict.fromkeys(path.expanduser().resolve() for path in paths))


def protected_mounts(rw: Sequence[Path], ro: Sequence[Path], protected: Sequence[Path]) -> dict[Path, bool]:
    """Plan grants, absent-leaf boundaries and pins without creating host paths.

    For a missing protected leaf, freeze its nearest existing ancestor and
    restore existing children. New entries at that ancestor cannot be created.
    Pins and grants share one parent-first sequence, so a later ancestor mount
    cannot hide an earlier writable child. Read-only wins an exact grant tie.
    """
    original = {path: False for path in rw if path.exists()}
    original.update({path: True for path in ro if path.exists()})
    grants = dict(original)

    def mode(path, source):
        parents = [root for root in source if path.is_relative_to(root)]
        return source[max(parents, key=lambda root: len(root.parts))] if parents else True

    for path in protected:
        if path.exists() or not any(path.is_relative_to(root) for root in rw):
            continue
        ancestor = path.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ValueError(f'protected path has a non-directory ancestor: {path}')
        grants[ancestor] = True
        for child in ancestor.iterdir():
            grants.setdefault(child, mode(child, original))
    for root, readonly in original.items():
        if readonly:
            grants[root] = True
    for path in [*protected, *grants]:
        roots = [root for root in rw if path.is_relative_to(root)]
        if not roots:
            continue
        root = min(roots, key=lambda value: len(value.parts))
        directory = path.parent
        while directory != root and directory.is_relative_to(root):
            if directory.is_dir() and directory not in grants:
                grants[directory] = mode(directory, grants)
            directory = directory.parent
    return grants


class _Mounts:
    def __init__(self, executable: str):
        self.argv = [executable, *NAMESPACES]

    def bind(self, source: Path, target: Path | None = None, *, ro: bool = False):
        self.argv.extend(('--ro-bind' if ro else '--bind', str(source), str(target or source)))

    def tmpfs(self, path: Path, mode: str = '0000'):
        self.argv.extend(('--perms', mode, '--tmpfs', str(path)))


def _mask(mounts: _Mounts, path: Path, file_mask: Path):
    if not path.exists():
        return  # Its nearest existing ancestor prevents creation.
    if path.is_dir():
        mounts.tmpfs(path)
    else:
        mounts.bind(file_mask, path, ro=True)


def _system(mounts: _Mounts, home: Path, grants: Sequence[Path]):
    bound = [Path(path) for path in SYSTEM_ROOTS]
    reserved = [*bound, home, Path('/dev'), Path('/proc'), Path('/tmp'), Path('/run'),
                Path('/var/run'), *grants]
    for entry in Path('/').iterdir():
        if entry.name in ('bin', 'lib', 'lib64', 'sbin'):
            if entry.is_symlink():
                mounts.argv.extend(('--symlink', os.readlink(entry), str(entry)))
            elif entry.exists():
                mounts.bind(entry, ro=True)
        elif not any(path == entry or path.is_relative_to(entry) for path in reserved):
            mounts.tmpfs(entry)
    for path in bound:
        if path.exists():
            mounts.bind(path, ro=True)
    mounts.tmpfs(home, '0755')


def _ssh(mounts: _Mounts, home: Path):
    ssh = home / '.ssh'
    if not ssh.is_dir():
        return
    mounts.tmpfs(ssh, '0755')
    for name in ('config', 'known_hosts', 'known_hosts.old', 'allowed_signers', 'config.d'):
        path = ssh / name
        if path.exists():
            mounts.bind(path.resolve(), path, ro=name not in ('known_hosts', 'known_hosts.old'))


def _run(mounts: _Mounts):
    # DNS is the sole exception to hidden host runtime/IPC paths.
    mounts.tmpfs(Path('/run'), '0111')
    mounts.tmpfs(Path('/run/systemd'), '0111')
    resolve = Path('/run/systemd/resolve')
    if resolve.is_dir():
        mounts.bind(resolve, ro=True)
    var_run = Path('/var/run')
    if not var_run.is_symlink():
        mounts.tmpfs(var_run)
    else:
        mounts.argv.extend(('--symlink', '/run', '/var/run'))


def _backing(state: Path, thread_id: str) -> tuple[Path, Path]:
    tmp = thread_tmp_root(state, thread_id)
    tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(tmp, 0o700)
    masks = state / 'sandbox-masks'
    masks.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_mask = masks / 'file'
    try:
        descriptor = os.open(file_mask, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o000)
    except FileExistsError:
        if file_mask.is_symlink() or not file_mask.is_file():
            raise ValueError('invalid sandbox placeholder')
    else:
        os.close(descriptor)
    os.chmod(file_mask, 0o000)
    return tmp, file_mask


def build_protected_argv(
    command: str, cwd: Path, *, path_policy: Any,
    rw_roots: Sequence[Path], ro_roots: Sequence[Path],
    readonly_overlays: Sequence[Path], env: Mapping[str, str],
    thread_id: str, executable: str,
) -> list[str]:
    """Render only; the caller owns startup verification and process launch."""
    from tools.managed_runtime import application_cache_root

    home, state = path_policy.home, path_policy.state_root
    cache = application_cache_root(home)
    _prepare_home_caches(home, cache)
    rw = _canonical([*rw_roots, *(home / path for path in HOME_RW
                                   if (home / path).exists() and not any(
                                       part.is_symlink() for part in (home / path, *(home / path).parents)
                                       if part != home and part.is_relative_to(home)))])
    ro = _canonical([*ro_roots, *(home / path for path in HOME_RO)])
    if Path('/') in [*rw, *ro]:
        raise ValueError('Linux protected shell requires specific directories instead of a root filesystem grant')
    runtime_roots = (Path('/dev'), Path('/proc'))
    rw = [path for path in rw if path != Path('/tmp') and not any(path.is_relative_to(root) for root in runtime_roots)]
    ro = [path for path in ro if path != Path('/tmp') and not any(path.is_relative_to(root) for root in runtime_roots)]
    tmp, file_mask = _backing(state, thread_id)
    hidden = _canonical([state, *path_policy.private_roots, *path_policy.credential_roots,
                         *(home / path for path in PERSISTENCE)])
    overlays = _canonical([cache, *readonly_overlays, *path_policy.write_roots])
    omitted = [entry for entry in (home.iterdir() if home.is_dir() else ())
               if not any(path == entry or path.is_relative_to(entry) for path in [*rw, *ro])]
    protected = [*overlays, *hidden, *omitted,
                 home / '.ssh', *path_policy.control_files]
    grants = protected_mounts(rw, ro, protected)
    mounts = _Mounts(executable)
    _system(mounts, home, list(grants))
    runtime = {Path('/dev'): '--dev', Path('/proc'): '--proc'}
    paths = {*grants, *runtime, Path('/tmp')}
    for path in sorted(paths, key=lambda value: (len(value.parts), str(value))):
        if path in runtime:
            mounts.argv.extend((runtime[path], str(path)))
        elif path == Path('/tmp'):
            mounts.bind(tmp, path)
        elif not any(path.is_relative_to(root) for root in runtime):
            mounts.bind(path, ro=grants[path])
    for entry in omitted:
        _mask(mounts, entry, file_mask)
    for path in sorted(overlays, key=lambda value: len(value.parts)):
        if path.exists():
            mounts.bind(path, ro=True)
    _ssh(mounts, home)
    sock = env.get('SSH_AUTH_SOCK')
    if sock and Path(sock).is_socket():
        mounts.bind(Path(sock))
    for path in sorted(hidden, key=lambda value: len(value.parts)):
        if not any(path != parent and path.is_relative_to(parent) for parent in hidden):
            _mask(mounts, path, file_mask)
    for path in path_policy.control_files:
        _mask(mounts, path, file_mask)
    _run(mounts)
    mounts.argv.append('--clearenv')
    for key, value in sorted(env.items()):
        mounts.argv.extend(('--setenv', key, value))
    mounts.argv.extend(('--chdir', str(cwd), '--', '/bin/bash', '--noprofile', '--norc', '-c', command))
    return launcher_argv(mounts.argv)
