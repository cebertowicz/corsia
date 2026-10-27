"""Parse and vet one ``gh api`` call so that it can only read.

``parse_gh_args`` turns the argv a poller or script hands the broker into a
``ReadRequest``, or raises ``GithubReadRefused`` naming why not. Nothing is
passed through: the broker rebuilds the argv from the vetted request, so what
runs is exactly what was checked. The policy, in one place:

* REST only, methods ``GET`` and ``HEAD``; GraphQL is refused by name;
* an API path, never a URL: ``gh api`` itself accepts any URL, and would send
  the person's token to its host;
* repository paths and GitHub's numeric repository pagination paths, plus
  a short list of non-repository paths;
* only headers that shape a read (media type, conditional reads, API version);
* no request body and no flag the broker has not listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

#: Paths outside ``/repos/...`` that the pollers read, and nothing more.
NON_REPO_PATHS = frozenset({"notifications", "user", "rate_limit"})
READ_METHODS = frozenset({"GET", "HEAD"})
#: Header names a read may set: a custom media type (a diff comes back as text),
#: conditional reads, and the API version. ``Authorization``, ``Host`` and
#: method-override headers are never among them.
ALLOWED_HEADERS = frozenset({"accept", "if-none-match", "if-modified-since", "x-github-api-version"})
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
REPOSITORY_ID = re.compile(r"^[0-9]+$")
_METHOD_FLAGS = frozenset({"-X", "--method"})
_HEADER_FLAGS = frozenset({"-H", "--header"})
_INCLUDE_FLAGS = frozenset({"-i", "--include"})
_VALUE_FLAGS = _METHOD_FLAGS | _HEADER_FLAGS
_CONTROL = re.compile(r"[\x00-\x20\x7f\\#]")


class GithubReadRefused(RuntimeError):
    """The broker will not make this call; ``reason`` names why."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """One vetted read."""

    method: str
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    include: bool = True

    def argv(self) -> list[str]:
        """The canonical ``gh`` argv, the path last."""
        args = ["api", "--method", self.method]
        args += ["-i"] if self.include else []
        for name, value in self.headers:
            args += ["-H", f"{name}: {value}"]
        return [*args, self.path]


def _vet_path(path: str) -> str:
    """The path without its leading slash, or a refusal."""
    if unquote(path.partition("?")[0]).strip("/").lower() == "graphql":
        raise GithubReadRefused("graphql_not_allowed", "the broker serves REST reads only")
    if "://" in path or path.startswith("//"):
        raise GithubReadRefused("full_url", "the broker takes an API path, never a URL")
    if not path or _CONTROL.search(path):
        raise GithubReadRefused("path_invalid")
    path = path.removeprefix("/")
    base = path.partition("?")[0]
    decoded = unquote(base)
    if re.search(r"%(2f|5c|2e|00)", base, re.IGNORECASE) or any(
        part in ("", ".", "..") for part in decoded.split("/")
    ):
        raise GithubReadRefused("path_invalid")
    parts = base.split("/")
    if parts[0].lower() == "repos":
        if len(parts) < 3 or not all(_REPO_PART.match(part) for part in parts[1:3]):
            raise GithubReadRefused("path_invalid", "a repository path names an owner and a repository")
    elif parts[0].lower() == "repositories":
        if len(parts) < 2 or not REPOSITORY_ID.match(parts[1]):
            raise GithubReadRefused("path_invalid", "a repositories path names a numeric id")
    elif base not in NON_REPO_PATHS:
        raise GithubReadRefused("path_not_allowed", parts[0])
    return path


def _vet_header(raw: str) -> tuple[str, str]:
    name, sep, value = raw.partition(":")
    name, value = name.strip(), value.strip()
    if not sep or name.lower() not in ALLOWED_HEADERS or "\r" in value or "\n" in value:
        raise GithubReadRefused("header_not_allowed", name.lower())
    return name, value


def _flag_value(args: list[str], index: int) -> tuple[str, str, int]:
    """``(flag, value, next index)`` for ``--flag value`` and ``--flag=value``."""
    flag = args[index]
    if flag.startswith("--") and "=" in flag:
        flag, _, value = flag.partition("=")
        return flag, value, index + 1
    if index + 1 >= len(args):
        raise GithubReadRefused("malformed_args", f"{flag} has no value")
    return flag, args[index + 1], index + 2


def _scan(args: list[str]) -> tuple[str | None, list[str], list[tuple[str, str]], bool]:
    method: str | None = None
    paths: list[str] = []
    headers: list[tuple[str, str]] = []
    include = False
    i = 1
    while i < len(args):
        token = args[i]
        bare = token.partition("=")[0] if token.startswith("--") else token
        if bare in _INCLUDE_FLAGS:
            include, i = True, i + 1
        elif bare in _VALUE_FLAGS:
            flag, value, i = _flag_value(args, i)
            if flag in _METHOD_FLAGS:
                if method is not None:
                    raise GithubReadRefused("malformed_args", "method given twice")
                method = value.upper()
            else:
                headers.append(_vet_header(value))
        elif token.startswith("-"):
            raise GithubReadRefused("flag_not_allowed", bare)
        else:
            paths.append(token)
            i += 1
    return method, paths, headers, include


def parse_gh_args(args: list[str]) -> ReadRequest:
    """Vet ``["api", ...]`` into a ``ReadRequest``."""
    if not args or args[0] != "api":
        raise GithubReadRefused("command_not_allowed", args[0] if args else "")
    method, paths, headers, include = _scan(args)
    if len(paths) != 1:
        raise GithubReadRefused("malformed_args", "exactly one API path is required")
    path = _vet_path(paths[0])
    if (method or "GET") not in READ_METHODS:
        raise GithubReadRefused("method_not_allowed", method or "")
    return ReadRequest(method or "GET", path, tuple(headers), include)
