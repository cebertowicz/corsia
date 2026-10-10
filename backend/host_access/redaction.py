"""Credential-safe normalization for host-action and process descriptors."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from host_access.shell_view import ShellCommandRedaction, descriptor_heredocs
from host_access.shell_view import heredoc_view, shell_words


REDACTED = "[REDACTED]"
_REDACTED_SENTINEL = "CORSIA_REDACTED_VALUE"
_COMMAND_BOUNDARIES = frozenset({";", ";;", "&", "&&", "|", "||", "|&"})
_REDIRECTION_OPERATORS = frozenset(
    {"<", "<<", "<<<", ">", ">>", ">|", "<>", "<&", ">&", "&>"}
)
_SHELL_SEPARATORS = _COMMAND_BOUNDARIES | _REDIRECTION_OPERATORS
_SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
)
_NON_VALUE_CREDENTIAL_OPTIONS = frozenset({"password-stdin", "with-token"})
_USERINFO_LONG_OPTIONS = frozenset({"proxy-user", "user", "userinfo"})
_LITERAL_OUTPUT_COMMANDS = frozenset({"echo", "printf"})
_SECURITY_PASSWORD_SUBCOMMANDS = frozenset(
    {
        "add-generic-password",
        "add-internet-password",
        "set-keychain-password",
    }
)
_SAFE_PRINTF_CREDENTIAL_FORMATS = frozenset(
    {"%b", "%b\\n", "%s", "%s\\n"}
)
_EXAMPLE_CREDENTIAL_VALUES = frozenset(
    {"dummy", "example", "placeholder", "sample"}
)
_AUTHORIZATION_ARGUMENT_RE = re.compile(
    r"^(?:auth|authorization)(?:\s*[:=]\s*|\s+|$)(.*)$",
    re.IGNORECASE,
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"^(?P<label>authorization\s*:\s*)"
    r"(?:(?P<scheme>basic|bearer)\s+)?(?P<credential>.*)$",
    re.IGNORECASE,
)
_COOKIE_HEADER_RE = re.compile(
    r"^(?P<label>(?:cookie|set-cookie)\s*:\s*)(?P<credential>.*)$",
    re.IGNORECASE,
)
_AUTH_SCHEME_ARGUMENT_RE = re.compile(
    r"^(?:basic|bearer)(?:\s+|=|:|$)(.*)$",
    re.IGNORECASE,
)
_SENSITIVE_LONG_OPTIONS = frozenset({"cookie", "header"})
_SENSITIVE_EXECUTABLE_RE = re.compile(
    r"(?:^|[-_])(?:(?:auth|credential|key|secret)(?:pass|password)?"
    r"|(?:ssh|auth|key)?pass(?:word)?)$",
    re.IGNORECASE,
)
_CREDENTIAL_CONTEXT_RE = re.compile(
    r"^(?:auth(?:enticate|entication)?|login|credentials?|set-credentials?)$",
    re.IGNORECASE,
)
_INDIRECT_VALUE_RE = re.compile(
    r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})"
)
_SIMPLE_PARAMETER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KEYCHAIN_SUBSTITUTION_RE = re.compile(
    r"\$\(\s*(?:/usr/bin/)?security\s+"
    r"find-(?:generic|internet)-password\b"
    r"[^();&|`<>\r\n]*\)",
)


class CommandRedactionError(ValueError):
    """A command cannot be represented safely for independent review."""


def _is_sensitive_key(value: str) -> bool:
    """Classify credential values without treating token metadata as secrets."""
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(
        normalized == suffix or normalized.endswith(suffix)
        for suffix in _SENSITIVE_KEY_SUFFIXES
    )


def _contains_sensitive_key_text(value: str) -> bool:
    return any(
        _is_sensitive_key(match.group())
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_-]*", value)
    )


def _is_indirect(value: str) -> bool:
    normalized = value.strip()
    if normalized in {"$(security", "$(/usr/bin/security"}:
        # The raw command validator admits this prefix only when the complete
        # substitution is one bounded macOS Keychain read.
        return True
    return _INDIRECT_VALUE_RE.fullmatch(normalized) is not None


def _secret_replacement(value: str) -> tuple[str, int, int]:
    if value == REDACTED:
        return value, 0, 0
    return REDACTED, 1, 0 if _is_indirect(value) else 1


def _userinfo_replacement(value: str) -> tuple[str, int, int]:
    identity, separator, credential = value.partition(":")
    if not separator:
        return _secret_replacement(value)
    replacement, redactions, literals = _secret_replacement(credential)
    return f"{identity}:{replacement}", redactions, literals


def _redact_authorization_header(value: str) -> tuple[str, int, int]:
    cookie = _COOKIE_HEADER_RE.fullmatch(value.strip())
    if cookie is not None:
        credential = cookie.group("credential").strip()
        if not credential:
            return value, 0, 0
        replacement, redactions, literals = _secret_replacement(credential)
        return (
            cookie.group("label") + replacement,
            redactions,
            literals,
        )

    match = _AUTHORIZATION_HEADER_RE.fullmatch(value.strip())
    if match is None:
        header, separator, credential = value.partition(":")
        if not separator or not _is_sensitive_key(header):
            return value, 0, 0
        replacement, redactions, literals = _secret_replacement(
            credential.strip()
        )
        return f"{header}: {replacement}", redactions, literals

    credential = match.group("credential").strip()
    if not credential:
        return value, 0, 0
    replacement, redactions, literals = _secret_replacement(credential)
    scheme = match.group("scheme")
    rendered = match.group("label")
    if scheme:
        rendered += f"{scheme} "
    return rendered + replacement, redactions, literals


def _redact_url(value: str) -> tuple[str, int, int]:
    if "://" not in value:
        return value, 0, 0
    parseable_value = value.replace(REDACTED, _REDACTED_SENTINEL)
    try:
        parsed = urlsplit(parseable_value)
    except ValueError as exc:
        raise CommandRedactionError("URL identity is invalid") from exc
    if not parsed.scheme or not parsed.netloc:
        raise CommandRedactionError("URL identity is invalid")

    redactions = 0
    literal_credentials = 0
    netloc = parsed.netloc
    userinfo, separator, host = netloc.rpartition("@")
    if separator:
        userinfo = userinfo.replace(_REDACTED_SENTINEL, REDACTED)
        userinfo, count, literals = _userinfo_replacement(userinfo)
        netloc = f"{userinfo}@{host}"
        redactions += count
        literal_credentials += literals

    query_pairs: list[tuple[str, str]] = []
    try:
        parsed_query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise CommandRedactionError("URL query is invalid") from exc
    for key, query_value in parsed_query:
        query_value = query_value.replace(_REDACTED_SENTINEL, REDACTED)
        if _is_sensitive_key(key):
            query_value, count, literals = _secret_replacement(query_value)
            redactions += count
            literal_credentials += literals
        query_pairs.append((key, query_value))
    query = urlencode(query_pairs, doseq=True, safe="[]$(){}:@")

    fragment = parsed.fragment
    if "=" in fragment:
        fragment_pairs: list[tuple[str, str]] = []
        try:
            parsed_fragment = parse_qsl(fragment, keep_blank_values=True)
        except ValueError as exc:
            raise CommandRedactionError("URL fragment is invalid") from exc
        for key, fragment_value in parsed_fragment:
            fragment_value = fragment_value.replace(
                _REDACTED_SENTINEL,
                REDACTED,
            )
            if _is_sensitive_key(key):
                fragment_value, count, literals = _secret_replacement(
                    fragment_value
                )
                redactions += count
                literal_credentials += literals
            fragment_pairs.append((key, fragment_value))
        fragment = urlencode(
            fragment_pairs,
            doseq=True,
            safe="[]$(){}:@",
        )

    rendered = urlunsplit(
        (parsed.scheme, netloc, parsed.path, query, fragment)
    ).replace(_REDACTED_SENTINEL, REDACTED)
    return (
        rendered,
        redactions,
        literal_credentials,
    )


def _redact_structured_json(value: str) -> tuple[str, int, int]:
    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return value, 0, 0
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        if _contains_sensitive_key_text(value):
            raise CommandRedactionError(
                "structured credential input is invalid"
            )
        return value, 0, 0

    redactions = 0
    literal_credentials = 0

    def visit(item: Any) -> Any:
        nonlocal redactions, literal_credentials
        if isinstance(item, dict):
            rendered: dict[Any, Any] = {}
            for key, child in item.items():
                if isinstance(key, str) and _is_sensitive_key(key):
                    if child == REDACTED:
                        rendered[key] = child
                        continue
                    rendered[key] = REDACTED
                    redactions += 1
                    literal_credentials += (
                        0
                        if isinstance(child, str) and _is_indirect(child)
                        else 1
                    )
                else:
                    rendered[key] = visit(child)
            return rendered
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    rendered = visit(parsed)
    if not redactions:
        return value, 0, 0
    return (
        json.dumps(
            rendered,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        redactions,
        literal_credentials,
    )


def _redact_intrinsic(value: str) -> tuple[str, int, int]:
    redacted_url, count, literals = _redact_url(value)
    if count:
        return redacted_url, count, literals

    structured, count, literals = _redact_structured_json(value)
    if count:
        return structured, count, literals

    authorization, count, literals = _redact_authorization_header(value)
    if count:
        return authorization, count, literals

    key, separator, assigned = value.partition("=")
    if separator and _is_sensitive_key(key.lstrip("-")):
        replacement, count, literals = _secret_replacement(assigned)
        return f"{key}={replacement}", count, literals
    if separator and "://" in assigned:
        assigned, count, literals = _redact_url(assigned)
        return f"{key}={assigned}", count, literals
    return value, 0, 0


def _is_sensitive_label(value: str) -> bool:
    normalized = value.strip().strip(":=").lstrip("-")
    return bool(normalized and _is_sensitive_key(normalized))


def _is_example_credential_assignment(value: str) -> bool:
    key, separator, assigned = value.partition("=")
    return bool(
        separator
        and _is_sensitive_key(key.lstrip("-"))
        and assigned.casefold() in _EXAMPLE_CREDENTIAL_VALUES
    )


def _password_style_executable(value: str | None) -> bool:
    if not value:
        return False
    name = Path(value).name.casefold()
    return (
        name
        in {
            "mariadb",
            "mariadb-admin",
            "mariadb-dump",
            "mysql",
            "mysqladmin",
            "mysqldump",
        }
        or _SENSITIVE_EXECUTABLE_RE.search(name) is not None
    )


def _validate_credential_expansions(
    command: str,
    *,
    has_credential_value: bool,
) -> None:
    """Fail closed when shlex cannot safely preserve credential provenance."""
    if not has_credential_value:
        return
    if "$'" in command or '$"' in command or "`" in command:
        raise CommandRedactionError(
            "credential expansion cannot be safely represented"
        )

    without_simple_parameters = _SIMPLE_PARAMETER_RE.sub(
        "",
        command,
    )
    if "${" in without_simple_parameters:
        raise CommandRedactionError(
            "credential parameter expansion cannot be safely represented"
        )

    without_keychain = _KEYCHAIN_SUBSTITUTION_RE.sub("", command)
    if "$(" in without_keychain:
        raise CommandRedactionError(
            "credential command substitution cannot be safely represented"
        )


def _credential_stdin_source_indices(
    tokens: Sequence[str],
) -> frozenset[int]:
    """Locate shell tokens that directly supply credential-only stdin."""
    sensitive: set[int] = set()
    hard_boundaries = frozenset({";", ";;", "&", "&&", "||"})
    for consumer_index, raw in enumerate(tokens):
        option, _separator, _value = str(raw).partition("=")
        if option.removeprefix("--").casefold() not in (
            _NON_VALUE_CREDENTIAL_OPTIONS
        ):
            continue

        command_end = len(tokens)
        for index in range(consumer_index + 1, len(tokens)):
            if str(tokens[index]) in hard_boundaries:
                command_end = index
                break
        for index in range(consumer_index + 1, command_end - 1):
            if str(tokens[index]) == "<<<":
                sensitive.add(index + 1)

        pipe_index: int | None = None
        for index in range(consumer_index - 1, -1, -1):
            token = str(tokens[index])
            if token in {"|", "|&"}:
                pipe_index = index
                break
            if token in hard_boundaries:
                break
        if pipe_index is None:
            continue

        segment_start = 0
        for index in range(pipe_index - 1, -1, -1):
            if str(tokens[index]) in _COMMAND_BOUNDARIES:
                segment_start = index + 1
                break
        if segment_start >= pipe_index:
            continue
        executable = Path(str(tokens[segment_start])).name.casefold()
        if executable not in _LITERAL_OUTPUT_COMMANDS:
            continue
        argument_indices = [
            index
            for index in range(segment_start + 1, pipe_index)
            if str(tokens[index]) not in _SHELL_SEPARATORS
        ]
        if (
            executable == "printf"
            and argument_indices
            and str(tokens[argument_indices[0]])
            in _SAFE_PRINTF_CREDENTIAL_FORMATS
        ):
            argument_indices = argument_indices[1:]
        sensitive.update(argument_indices)
    return frozenset(sensitive)


def _security_password_value_indices(
    tokens: Sequence[str],
) -> frozenset[int]:
    """Locate literal values supplied to security(1) password-write flags."""
    sensitive: set[int] = set()
    segment_start = 0
    for index in range(len(tokens) + 1):
        if (
            index < len(tokens)
            and str(tokens[index]) not in _COMMAND_BOUNDARIES
        ):
            continue
        segment = [str(token) for token in tokens[segment_start:index]]
        if (
            len(segment) >= 2
            and Path(segment[0]).name.casefold() == "security"
            and segment[1].casefold() in _SECURITY_PASSWORD_SUBCOMMANDS
        ):
            for offset, token in enumerate(segment[:-1]):
                if token == "-w":
                    sensitive.add(segment_start + offset + 1)
        segment_start = index + 1
    return frozenset(sensitive)


def _redact_shell_tokens(tokens: Sequence[str]) -> ShellCommandRedaction:
    redacted: list[str] = []
    redactions = 0
    literal_credentials = 0
    password_style_command = False
    credential_context = False
    literal_output_command = False
    redirection_target = False
    credential_input_indices = (
        _credential_stdin_source_indices(tokens)
        | _security_password_value_indices(tokens)
    )
    index = 0

    def append_sensitive(value: str, *, userinfo: bool = False) -> None:
        nonlocal redactions, literal_credentials
        if userinfo:
            rendered, count, literals = _userinfo_replacement(value)
        else:
            rendered, count, literals = _secret_replacement(value)
        redacted.append(rendered)
        redactions += count
        literal_credentials += literals

    while index < len(tokens):
        token = str(tokens[index])
        if token in _SHELL_SEPARATORS:
            redacted.append(token)
            if token in _COMMAND_BOUNDARIES:
                password_style_command = False
                credential_context = False
                literal_output_command = False
                redirection_target = False
            else:
                redirection_target = True
            index += 1
            continue
        is_redirection_target = redirection_target
        redirection_target = False

        if index in credential_input_indices:
            append_sensitive(token)
            index += 1
            continue

        if token.casefold().strip() in {"authorization:", "authorization="}:
            redacted.append(token)
            if index + 1 < len(tokens):
                scheme = str(tokens[index + 1])
                if scheme.casefold() in {"basic", "bearer"}:
                    redacted.append(scheme)
                    index += 1
            if index + 1 < len(tokens):
                append_sensitive(str(tokens[index + 1]))
                index += 2
                continue

        if token in {"-H", "--header"} and index + 1 < len(tokens):
            candidate = str(tokens[index + 1])
            rendered, count, literals = _redact_authorization_header(
                candidate
            )
            redacted.append(token)
            redacted.append(rendered)
            redactions += count
            literal_credentials += literals
            index += 2
            continue

        if (
            token in {"-u", "--proxy-user", "--user"}
            and index + 1 < len(tokens)
        ):
            candidate = str(tokens[index + 1])
            redacted.append(token)
            if ":" in candidate or _is_indirect(candidate):
                append_sensitive(candidate, userinfo=True)
            else:
                redacted.append(candidate)
            index += 2
            continue

        if (
            token == "-p"
            and index + 1 < len(tokens)
            and password_style_command
        ):
            redacted.append(token)
            append_sensitive(str(tokens[index + 1]))
            index += 2
            continue

        if token.startswith("-H") and token != "-H":
            rendered, count, literals = _redact_authorization_header(
                token[2:].strip()
            )
            redacted.append(f"-H{rendered}")
            redactions += count
            literal_credentials += literals
            index += 1
            continue

        if (
            token.startswith("-u")
            and token != "-u"
            and (":" in token[2:] or _is_indirect(token[2:]))
        ):
            rendered, count, literals = _userinfo_replacement(token[2:])
            redacted.append(f"-u{rendered}")
            redactions += count
            literal_credentials += literals
            index += 1
            continue

        if (
            token.startswith("-p")
            and token != "-p"
            and password_style_command
        ):
            replacement, count, literals = _secret_replacement(token[2:])
            redacted.append(f"-p{replacement}")
            redactions += count
            literal_credentials += literals
            index += 1
            continue

        if token.startswith("--"):
            option, separator, value = token.partition("=")
            option_key = option[2:].casefold()
            if not separator and option_key in _NON_VALUE_CREDENTIAL_OPTIONS:
                redacted.append(token)
                index += 1
                continue
            if separator and option_key in _USERINFO_LONG_OPTIONS:
                if ":" in value or _is_indirect(value):
                    rendered, count, literals = _userinfo_replacement(value)
                    redacted.append(f"{option}={rendered}")
                    redactions += count
                    literal_credentials += literals
                else:
                    redacted.append(token)
                index += 1
                continue
            if separator and option_key.casefold() == "header":
                rendered, count, literals = _redact_authorization_header(
                    value
                )
                redacted.append(f"{option}={rendered}")
                redactions += count
                literal_credentials += literals
                index += 1
                continue
            if separator and (
                _is_sensitive_key(option_key)
                or option_key.casefold() in _SENSITIVE_LONG_OPTIONS
            ):
                replacement, count, literals = _secret_replacement(value)
                redacted.append(f"{option}={replacement}")
                redactions += count
                literal_credentials += literals
                index += 1
                continue
            if (
                not separator
                and option_key not in _NON_VALUE_CREDENTIAL_OPTIONS
                and (
                    _is_sensitive_key(option_key)
                    or option_key.casefold() in _SENSITIVE_LONG_OPTIONS
                )
                and index + 1 < len(tokens)
                and str(tokens[index + 1]) not in _SHELL_SEPARATORS
            ):
                redacted.append(token)
                append_sensitive(str(tokens[index + 1]))
                index += 2
                continue

        if (
            _is_sensitive_label(token)
            and "=" not in token
            and index + 1 < len(tokens)
            and str(tokens[index + 1]) not in _SHELL_SEPARATORS
            and (
                credential_context
                or password_style_command
            )
        ):
            redacted.append(token)
            append_sensitive(str(tokens[index + 1]))
            index += 2
            continue

        if (
            literal_output_command
            and _is_example_credential_assignment(token)
        ):
            rendered, count, literals = token, 0, 0
        else:
            rendered, count, literals = _redact_intrinsic(token)
        redacted.append(rendered)
        redactions += count
        literal_credentials += literals
        if _CREDENTIAL_CONTEXT_RE.fullmatch(token.strip()):
            credential_context = True
        if (
            not is_redirection_target
            and _password_style_executable(token)
        ):
            password_style_command = True
        if (
            not is_redirection_target
            and Path(token).name.casefold() in _LITERAL_OUTPUT_COMMANDS
        ):
            literal_output_command = True
        index += 1

    return ShellCommandRedaction(
        tokens=tuple(redacted),
        redactions=redactions,
        literal_credentials=literal_credentials,
    )


def redact_shell_command(command: str) -> ShellCommandRedaction:
    """Return one canonical, credential-safe reviewer representation."""
    if (
        not isinstance(command, str)
        or not command.strip()
        or "\x00" in command
        or REDACTED in command
    ):
        raise CommandRedactionError("command text is unsafe")
    try:
        command.encode("utf-8")
        tokens = shell_words(command)
    except (UnicodeError, ValueError) as exc:
        raise CommandRedactionError("command text is unparseable") from exc
    if not tokens:
        raise CommandRedactionError("command text is empty")
    redacted = _redact_shell_tokens(tokens)
    _validate_credential_expansions(
        command,
        has_credential_value=bool(redacted.redactions),
    )
    return _with_verbatim_heredocs(command, tokens, redacted)


def _redacted_words(text: str) -> ShellCommandRedaction | None:
    """Redact the shell words of ``text``, or ``None`` if it cannot be."""
    try:
        return _redact_shell_tokens(shell_words(text))
    except (CommandRedactionError, ValueError):
        return None


def _with_verbatim_heredocs(
    command: str,
    tokens: Sequence[str],
    redacted: ShellCommandRedaction,
) -> ShellCommandRedaction:
    """Show here-document bodies as written instead of shell-tokenized.

    ``redacted`` remains the only credential classifier. The verbatim view is
    used only when every shown part re-tokenizes to a redaction fixed point
    that retains no raw token the classifier replaced; otherwise the token
    view stands.
    """
    view = heredoc_view(command, tokens, redacted.tokens, REDACTED)
    if view is None:
        return redacted
    shell_text, heredocs, hidden = view
    bodies = [item.body for item in heredocs]
    shell = _redacted_words(shell_text)
    shown = [_redacted_words(body) for body in bodies]
    clean = [item for item in shown if item is not None and not item.redactions]
    if shell is None or len(clean) != len(shown):
        return redacted
    words = [word for item in clean for word in item.tokens]
    texts = [*shell.tokens, *words, *bodies]
    if any(value in text for value in hidden for text in texts):
        return redacted
    return ShellCommandRedaction(
        tokens=shell.tokens,
        redactions=sum(
            text.count(REDACTED) for text in [*shell.tokens, *bodies]
        ),
        literal_credentials=redacted.literal_credentials,
        heredocs=heredocs,
    )


def validate_shell_command_descriptor(value: object) -> None:
    """Reject any non-canonical or still-sensitive Bash audit descriptor."""
    if not isinstance(value, Mapping) or set(value) - {"heredocs"} != {
        "tokens",
        "redactions",
        "literal_credentials",
    }:
        raise CommandRedactionError("command descriptor shape is invalid")
    tokens = value.get("tokens")
    redactions = value.get("redactions")
    literal_credentials = value.get("literal_credentials")
    if (
        not isinstance(tokens, list)
        or not tokens
        or len(tokens) > 1024
        or any(
            not isinstance(token, str)
            or "\x00" in token
            or len(token) > 16_000
            for token in tokens
        )
        or isinstance(redactions, bool)
        or not isinstance(redactions, int)
        or redactions < 0
        or isinstance(literal_credentials, bool)
        or not isinstance(literal_credentials, int)
        or literal_credentials != 0
    ):
        raise CommandRedactionError("command descriptor values are invalid")
    try:
        bodies = [item.body for item in descriptor_heredocs(value)]
    except ValueError as exc:
        raise CommandRedactionError(
            "command descriptor here-documents are invalid"
        ) from exc
    observed_markers = sum(text.count(REDACTED) for text in [*tokens, *bodies])
    if observed_markers != redactions:
        raise CommandRedactionError("command descriptor redactions are invalid")
    for body in bodies:
        shown = _redacted_words(body)
        if shown is None or shown.redactions:
            raise CommandRedactionError(
                "command descriptor here-document is not redacted"
            )
    normalized = _redact_shell_tokens(tokens)
    if (
        normalized.tokens != tuple(tokens)
        or normalized.redactions != 0
        or normalized.literal_credentials != 0
    ):
        raise CommandRedactionError("command descriptor contains raw credentials")


def validate_action_descriptor(
    tool_name: str,
    descriptor: object,
) -> None:
    """Validate the trusted descriptor boundary before review or persistence."""
    if not isinstance(descriptor, Mapping):
        raise CommandRedactionError("action descriptor binding is invalid")
    version = descriptor.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != 1
        or descriptor.get("tool") != tool_name
    ):
        raise CommandRedactionError("action descriptor binding is invalid")
    if tool_name == "bash":
        if set(descriptor) != {
            "version",
            "tool",
            "command",
            "working_directory",
            "timeout_seconds",
            "background",
        }:
            raise CommandRedactionError(
                "action descriptor contains unsupported fields"
            )
        validate_shell_command_descriptor(descriptor.get("command"))
        working_directory = descriptor.get("working_directory")
        if working_directory is not None:
            if (
                not isinstance(working_directory, str)
                or not working_directory
                or len(working_directory) > 4096
                or "\x00" in working_directory
                or REDACTED in working_directory
            ):
                raise CommandRedactionError(
                    "action working directory is invalid"
                )
            try:
                _rendered, count, literals = _redact_intrinsic(
                    working_directory
                )
            except CommandRedactionError as exc:
                raise CommandRedactionError(
                    "action working directory is invalid"
                ) from exc
            if count or literals:
                raise CommandRedactionError(
                    "action working directory contains raw credentials"
                )
        timeout_seconds = descriptor.get("timeout_seconds")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 600
        ):
            raise CommandRedactionError("action timeout is invalid")
        if not isinstance(descriptor.get("background"), bool):
            raise CommandRedactionError("action background mode is invalid")


def redact_process_arguments(
    arguments: Sequence[str],
    *,
    limit: int = 24,
) -> tuple[str, ...]:
    """Return bounded process arguments with common credential forms removed."""

    def continues_sensitive_value(value: str) -> bool:
        authorization = _AUTHORIZATION_ARGUMENT_RE.fullmatch(value.strip())
        if authorization is not None:
            credential = authorization.group(1).strip().casefold()
            return not credential or credential in {"basic", "bearer"}
        scheme = _AUTH_SCHEME_ARGUMENT_RE.fullmatch(value.strip())
        return scheme is not None and not scheme.group(1).strip()

    redacted: list[str] = []
    is_security_password_command = bool(
        arguments
        and Path(str(arguments[0])).name.casefold() == "security"
        and any(
            str(value).casefold() in _SECURITY_PASSWORD_SUBCOMMANDS
            for value in arguments[1:3]
        )
    )
    hide_next = False
    for raw in arguments[:limit]:
        value = str(raw)[:512]
        if hide_next:
            redacted.append(REDACTED)
            hide_next = continues_sensitive_value(value)
            continue

        authorization = _AUTHORIZATION_ARGUMENT_RE.match(value)
        if authorization is not None:
            redacted.append(f"Authorization: {REDACTED}")
            hide_next = continues_sensitive_value(value)
            continue

        scheme = _AUTH_SCHEME_ARGUMENT_RE.match(value)
        if scheme is not None:
            redacted.append(f"Credential {REDACTED}")
            hide_next = continues_sensitive_value(value)
            continue

        if is_security_password_command and value == "-w":
            redacted.append(value)
            hide_next = True
            continue

        if value.strip().strip(":=").casefold() in {
            "oauth_token",
            "passwd",
            "password",
        }:
            redacted.append(value)
            hide_next = True
            continue

        short_flag = next(
            (
                flag
                for flag in ("-H", "-p", "-u")
                if value.startswith(flag)
            ),
            None,
        )
        if short_flag is not None:
            attached = value[len(short_flag):].strip()
            if attached:
                redacted.append(f"{short_flag}{REDACTED}")
                hide_next = continues_sensitive_value(attached)
            else:
                redacted.append(short_flag)
                hide_next = True
            continue

        if value.startswith("-"):
            flag, separator, _attached = value.partition("=")
            key = flag.lstrip("-")
            if (
                _is_sensitive_key(key)
                or key.casefold()
                in {"cookie", "header", "proxy-user", "user"}
            ):
                redacted.append(
                    f"{flag}={REDACTED}" if separator else flag
                )
                hide_next = not separator
                continue

        if "=" in value and "://" not in value:
            key, _separator, _secret = value.partition("=")
            if _is_sensitive_key(key):
                redacted.append(f"{key}={REDACTED}")
                continue
        try:
            rendered, _count, _literals = _redact_intrinsic(value)
        except CommandRedactionError:
            rendered = REDACTED
        redacted.append(rendered)
    if len(arguments) > limit:
        redacted.append(f"[{len(arguments) - limit} more arguments omitted]")
    return tuple(redacted)
