"""Check 3: private names and paths in publicly-shipped files.

"Publicly-shipped" means every git-tracked file in the repository (this is
a public repo; `git ls-files` is the actual shipped set), minus a small
binary-extension blocklist that would never contain a text leak. Two
sub-checks run over that set: a generic macOS home-directory path
(``/Users/...``, any name -- not the maintainer's specifically, so this
script's own source never has to carry the thing it is checking for), and
the owner's own identity. The owner identity comes from, in priority order,
``--owner-name``, ``--owner-identity-file`` (an explicit file kept outside
this public repository), ``--owner-profile`` (the workspace's ``USER.md``),
or the `git config user.name` / ``user.email`` of the checkout. The profile
contributes its FIRST labeled ``Name:`` phrase, only when that value validates
as a plain person-name phrase (it is never repaired), plus email and phone
literals from a heading containing ``contact``. Each source reports the token
kinds it resolved, so coverage is never inferred from a source having been
supplied. Never hardcoded. A commit identity is only a fallback: where the
committer is a bot, an agent, or the product itself, it is not the owner's
identity.

The home-dir-path sub-check exempts individual *path segments* that match a
short, deliberate placeholder vocabulary (see ``_PLACEHOLDER_SEGMENT_RE``),
not files that merely look like tests. Excluding whole test paths was tried
first and was wrong: most of this repo's real leaked home-directory paths
turned out to be in test fixtures, so that carve-out exempted the largest
population of actual leaks instead of catching them.

Both of those sub-checks are required by default: if the owner identity
cannot be resolved (none of ``--owner-name``, ``--owner-identity-file``, a
usable ``--owner-profile``, and git config yields a token), that is recorded
as a skip reason AND treated as
a failure, not a silent pass -- an inconclusive leak scan must say so, not
report clean, and the text report names the skip. Pass
``require_owner_identity=False`` (``--allow-missing-owner-identity`` on the
CLI) for a checkout with no owner identity configured -- a plain clone, CI,
or a contributor self-checking their own change. That accepts the skip; it
never reports the run as complete, because the report's verdict line then
names the sub-check that did not run. Who must run the scan, and where, is
policy its caller sets, not something this module infers from the checkout.

A third sub-check, internal agent/lane/thread names, needs a maintained
registry of that internal project taxonomy. That registry is deliberately
NOT part of this public repository -- committing it here would recreate the
exact leak this check exists to catch, just in a different file. It is
skipped with a clear notice unless ``--internal-names-file`` points at one
(one literal per line), which is expected to live in the maintainers'
workspace, outside the repository. Unlike owner identity, this sub-check is
optional by design: the registry has no default location within reach of a
plain clone.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .home_path import HOME_PATH_RE
from .owner_identity import OwnerProfileTokens, PhoneToken

# A segment is exempt from the home-dir-path sub-check when it is one of a
# small, deliberate set of synthetic placeholders -- not when its *file* is a
# test path. A carve-out keyed on the file (the earlier `_is_test_path`) skips
# the largest population of real leaks, because most real leaks in this repo
# are in test fixtures: a realistic-looking `/Users/...` literal copied from
# an actual local run into a fixture is exactly as public as anything else
# `git ls-files` reports. Two things are exempt: a segment made entirely of
# non-alphanumeric characters (an illustrative ellipsis, or a regex/pattern
# fragment quoting `HOME_PATH_RE`'s own shape -- neither can be a real
# username), and a short, deliberate word list of generic placeholders with
# no connection to any real identity. This list must never include a token
# that is, or could be mistaken for, an actual maintainer's name or nickname:
# that would recreate the exact blind spot this rule replaces `_is_test_path`
# to close. A fixture that wants a personal-looking placeholder should use
# one of these words, not invent a new one -- new placeholder vocabulary is a
# deliberate addition to this list, reviewed here, not an automatic pass.
_PLACEHOLDER_SEGMENT_RE = re.compile(
    r"^(?:[^a-zA-Z0-9]+|test|user|username|example|alice|you|me|home|runner"
    r"|x|someone|secret)$",
    re.IGNORECASE,
)

# This module's own pattern literally contains the `/Users/` substring it
# exists to detect -- an inherent, permanent self-match, not a leak. Without
# this exemption, this check could never pass on a clean tree, because it
# would always flag its own necessary internals.
_SELF_REFERENCE_FILES = frozenset({"scripts/check_docs_drift/home_path.py"})

# The narrow test/fixture population where a lowercase synthetic id seed may
# stand in for a real name (see _person_name_hits). Keyed on the path because
# that is what makes it a fixture; everywhere else a name is a name.
_FIXTURE_PATH_RE = re.compile(r"(?:^|/)(?:tests?|fixtures)/|(?:^|/)test_[^/]+\.py$|_test\.py$")
# A lowercase ``some_id = "value"`` seed: identifier group 1, quote group 2,
# seeded value group 3.
_FIXTURE_SEED_RE = re.compile(r"(?<![A-Za-z0-9_])([a-z0-9_]*_id)\s*=\s*([\"'])([^\"']*)\2")


def _is_fixture_path(rel: str) -> bool:
    """Whether a repo-relative path is test/fixture code."""
    return bool(_FIXTURE_PATH_RE.search(rel))


# Extensions that are never text, so scanning them for name/path leaks would
# just read binary garbage. Everything else `git ls-files` reports is scanned.
_BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".pdf", ".zip", ".gz", ".tar", ".whl",
        ".db", ".sqlite", ".sqlite3",
        ".pyc", ".so", ".dylib", ".dll", ".exe", ".bin",
        ".lock",
    }
)


@dataclass
class PrivateScanResult:
    home_dir_hits: dict[str, list[str]] = field(default_factory=dict)
    owner_hits: dict[str, list[str]] = field(default_factory=dict)
    internal_name_hits: dict[str, list[str]] = field(default_factory=dict)
    internal_names_skipped_reason: str | None = None
    owner_identity_skipped_reason: str | None = None
    owner_identity_required: bool = True
    # Which token kinds the owner-identity sub-check actually scanned, e.g.
    # ["person", "contact", "registry"]. Coverage is reported per kind so a
    # source that resolved only one kind cannot claim the others.
    owner_identity_kinds: list[str] = field(default_factory=list)
    owner_identity_kind_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        if self.owner_identity_skipped_reason and self.owner_identity_required:
            return False
        return not (self.home_dir_hits or self.owner_hits or self.internal_name_hits)


def _home_dir_hits(text: str) -> list[str]:
    hits = {
        match.group(0)
        for match in HOME_PATH_RE.finditer(text)
        if not _PLACEHOLDER_SEGMENT_RE.match(match.group(1))
    }
    return sorted(hits)


def _public_files(repo_root: Path) -> list[Path]:
    """Every git-tracked text file in the repo (the whole repo is public)."""
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    files = []
    for rel in proc.stdout.splitlines():
        if not rel:
            continue
        path = repo_root / rel
        if Path(rel).suffix.lower() in _BINARY_EXTENSIONS or not path.is_file():
            continue
        files.append(path)
    return files


def _git_config_value(repo_root: Path, key: str) -> str:
    proc = subprocess.run(
        ["git", "config", "--get", key],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def git_user_name(repo_root: Path) -> str:
    """The configured `git config user.name` for this checkout, or ''."""
    return _git_config_value(repo_root, "user.name")


def git_identity(repo_root: Path) -> list[str]:
    """The git identity configured for this checkout, as literal tokens."""
    tokens = [_git_config_value(repo_root, key) for key in ("user.name", "user.email")]
    return [t for t in tokens if t]


def read_token_file(path: Path) -> list[str]:
    """One literal token per non-blank line of a file kept outside the repo."""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# Separators a leaked number may be written with, between the digits it must
# keep in order.
_PHONE_SEPARATORS = r"[\s.()\u2013-]*"


# --------------------------------------------------------------------------
# MATCHING RULE -- the one home for how each token kind is matched. A token's
# kind is decided by the builder that produced it, never by the file it is
# looking for a leak in:
#
#   person tokens  -- the profile's validated ``Name:`` phrase and
#                     ``--owner-name``. Case-insensitive literal, with the
#                     phrase's words joined by any run of whitespace, '_', or
#                     '-', and bounded on both sides by anything that is not
#                     alphanumeric. '_' is allowed as a boundary on purpose:
#                     ``ask_alice``, ``alice_id`` and ``alice_example`` are
#                     leaks, not word boundaries (_person_name_hits).
#   email tokens   -- profile email literals. Case-insensitive literal
#                     (_literal_hits with case_insensitive=True).
#   phone tokens   -- profile phone literals, matched on their digits with any
#                     separator allowed between them and a leading country code
#                     ignored, so one number in any punctuation is one hit. The
#                     report shows the literal and its digit-only form when
#                     they differ (_phone_hits).
#   registry tokens -- ``--owner-identity-file`` and ``--internal-names-file``
#                     lines, and the git-config fallback. EXACT literal, as
#                     written: these are arbitrary maintainer-chosen strings
#                     whose case the maintainer chose (_literal_hits).
#
# A source that resolves only some kinds reports only those kinds; coverage is
# never inferred from the fact that a source was supplied.
# --------------------------------------------------------------------------


def _load_internal_names(path: Path | None, result: PrivateScanResult) -> list[str]:
    if path is None:
        result.internal_names_skipped_reason = (
            "no --internal-names-file supplied; the internal agent/lane/"
            "thread name registry is maintained outside this public repository"
        )
        return []
    return read_token_file(path)


def _literal_hits(text: str, tokens: list[str], *, case_insensitive: bool = False) -> list[str]:
    """Email and registry tokens: literals, exact unless stated otherwise."""
    haystack = text.casefold() if case_insensitive else text
    needle_of = str.casefold if case_insensitive else (lambda token: token)
    return [token for token in tokens if token and needle_of(token) in haystack]


def _fixture_seeded_value_spans(text: str, line_start: int, line_end: int) -> list[tuple[int, int]]:
    """Spans on one line that the person-token seed exemption covers.

    The exemption covers a match whose start falls inside the quoted value of a
    lowercase ``*_id = "..."`` seed on that line, in a test/fixture path -- so
    the whole quoted value is exempt, including a longer seed that happens to
    contain the name. It does not extend to the rest of the line: an unrelated
    ``*_id = "`` earlier on the line leaves a real name later on it reportable.
    """
    spans: list[tuple[int, int]] = []
    for seed in _FIXTURE_SEED_RE.finditer(text[line_start:line_end]):
        value = seed.group(3)
        if value == value.lower():
            spans.append((line_start + seed.start(3), line_start + seed.end(3)))
    return spans


def _person_pattern(phrase: str) -> re.Pattern[str]:
    """Boundary-bounded phrase pattern; '_' counts as a boundary, alphanumerics do not."""
    words = [re.escape(word) for word in phrase.split()]
    body = r"[\s_-]*".join(words)
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.I)


def _person_name_hits(text: str, phrases: list[str], *, in_fixture_path: bool) -> list[str]:
    """Person tokens: case-insensitive phrase, bounded by non-alphanumerics."""
    hits: list[str] = []
    for phrase in phrases:
        if not phrase:
            continue
        for match in _person_pattern(phrase).finditer(text):
            if not in_fixture_path:
                hits.append(phrase)
                break
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.start())
            line_end = len(text) if line_end == -1 else line_end
            seeded = any(
                start <= match.start() < end
                for start, end in _fixture_seeded_value_spans(text, line_start, line_end)
            )
            if not seeded:
                hits.append(phrase)
                break
    return hits


def _phone_hits(text: str, patterns: list[tuple[PhoneToken, re.Pattern[str]]]) -> list[str]:
    """Phone tokens: one number, whatever separators or country code a file uses."""
    hits: list[str] = []
    for token, pattern in patterns:
        if pattern.search(text):
            hits.extend(token.reported_forms)
    return hits


def _phone_patterns(tokens: list[PhoneToken]) -> list[tuple[PhoneToken, re.Pattern[str]]]:
    compiled: list[tuple[PhoneToken, re.Pattern[str]]] = []
    for token in tokens:
        body = _PHONE_SEPARATORS.join(re.escape(digit) for digit in token.match_digits)
        compiled.append((token, re.compile(rf"(?<!\d){body}(?!\d)")))
    return compiled


def check_private_names(
    repo_root: Path,
    internal_names_file: Path | None,
    owner_tokens: list[str] | None = None,
    *,
    owner_profile: OwnerProfileTokens | None = None,
    owner_identity_skip_reason: str | None = None,
    require_owner_identity: bool = True,
) -> PrivateScanResult:
    """Scan every publicly-shipped (git-tracked) file for private names and paths.

    Each token kind is matched its own way -- see MATCHING RULE above -- and the
    result reports exactly which kinds ran, so a partial source can never be
    read as a complete owner-identity scan.
    """
    result = PrivateScanResult(owner_identity_required=require_owner_identity)
    owner_tokens = git_identity(repo_root) if owner_tokens is None else owner_tokens
    profile = owner_profile or OwnerProfileTokens()
    phone_patterns = _phone_patterns(profile.phone_tokens)

    kinds = list(profile.kinds)
    if owner_tokens:
        kinds.append("registry")
    result.owner_identity_kinds = kinds
    result.owner_identity_kind_reasons = dict(profile.kind_reasons)
    if not owner_tokens and not kinds:
        result.owner_identity_skipped_reason = owner_identity_skip_reason or (
            "no --owner-name, --owner-identity-file, or usable --owner-profile resolved, "
            "and no checkout git identity is available"
        )
    internal_names = _load_internal_names(internal_names_file, result)

    for path in _public_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel not in _SELF_REFERENCE_FILES:
            home_hits = _home_dir_hits(text)
            if home_hits:
                result.home_dir_hits[rel] = home_hits
        owner_hits = _person_name_hits(text, profile.person_names, in_fixture_path=_is_fixture_path(rel))
        owner_hits += _literal_hits(text, profile.email_tokens, case_insensitive=True)
        owner_hits += _phone_hits(text, phone_patterns)
        owner_hits += _literal_hits(text, owner_tokens)
        if owner_hits:
            result.owner_hits[rel] = owner_hits
        if internal_names:
            internal_hits = _literal_hits(text, internal_names)
            if internal_hits:
                result.internal_name_hits[rel] = internal_hits
    return result
