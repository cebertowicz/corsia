"""The owner identity a scan reads out of a workspace profile.

The profile is the automatic source for the owner-identity sub-check: its first
labeled ``Name:`` phrase (validated as a plain person-name phrase, never
repaired) plus email and phone literals from a section headed with the word
``contact``. ``OwnerProfileTokens`` is the result and the unit of coverage --
it reports which kinds resolved (``person``, ``contact``) and why any kind did
not, so a source that yielded only contact literals can never be read as a
completed owner-name check.

How each kind is MATCHED against a file is deliberately not here: that is the
MATCHING RULE block in ``private_scan_check``, one home for the whole scan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


_PROFILE_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
# Bold and bullet are accepted on the LABEL, here and nowhere else: the value is
# validated as written (see _PROFILE_NAME_PHRASE_RE), so nothing has to be
# repaired after the fact.
_PROFILE_NAME_LABEL_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Name:(?:\*\*)?\s*(.+?)\s*$", re.I)
_PROFILE_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")

# A person-name phrase is VALIDATED, never repaired. Accepted: one to five words
# of Unicode letters, each allowed an internal hyphen, apostrophe, or period,
# with at most one trailing sentence terminator -- so the eval fixture's
# `Name: Eval User.` yields the phrase `Eval User`. Anything else -- prose after
# a comma or a dash, a parenthetical, a list, digits, backticks, emphasis
# markers, an email -- is not a name: the person kind does not resolve, and the
# profile says why instead of inventing a token that can never match.
_NAME_WORD = r"[^\W\d_]+(?:[-'.][^\W\d_]+)*"
_PROFILE_NAME_PHRASE_RE = re.compile(rf"^{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,4}}[.!?]?$")
_PROFILE_NAME_TERMINATOR_RE = re.compile(r"[.!?]$")

# Contact literals: emails, and phones written with any separators. A ' - '
# sequence is a clause boundary -- two labeled fields on one line, or a date
# range -- so the line is split on it before the phone pattern runs and two
# digit runs can never merge into one token. A candidate must then carry at
# least _PROFILE_PHONE_MIN_DIGITS digits, which is what keeps an ISO date (8)
# out of the token set.
_PROFILE_PHONE_CLAUSE_RE = re.compile(r"\s+[-\u2013\u2014]\s+")
_PROFILE_PHONE_RE = re.compile(r"\+?\d[\d.()\s-]*\d")
_PROFILE_PHONE_MIN_DIGITS = 9


@dataclass(frozen=True)
class PhoneToken:
    """One phone literal, and the digits a file is actually searched for.

    The literal is what a report shows. The match runs on digits, with any
    separator allowed between them and a leading country code ignored, so
    `+1 555-010-2345`, `555-010-2345`, `(555) 010-2345` and `5550102345` are
    recognized as the same number and not as four different strings.
    """

    literal: str
    digits: str
    match_digits: str

    @property
    def reported_forms(self) -> list[str]:
        """The literal, plus its digit-only form when the two differ."""
        return [self.literal] if self.digits == self.literal else [self.literal, self.digits]


@dataclass(frozen=True)
class OwnerProfileTokens:
    """What a profile resolved, per kind, and why any kind did not.

    The kinds are the unit of coverage the gate reports. A run that resolved
    only contact literals did not check for the owner's name, and has to say so.
    """

    person_names: list[str] = field(default_factory=list)
    email_tokens: list[str] = field(default_factory=list)
    phone_tokens: list[PhoneToken] = field(default_factory=list)
    kind_reasons: dict[str, str] = field(default_factory=dict)
    read_error: str | None = None

    @property
    def kinds(self) -> list[str]:
        resolved = []
        if self.person_names:
            resolved.append("person")
        if self.email_tokens or self.phone_tokens:
            resolved.append("contact")
        return resolved

    @property
    def skip_reason(self) -> str | None:
        """Why no owner identity whatsoever could be scanned, if that is the case."""
        if self.kinds:
            return None
        return "; ".join(self.kind_reasons.values()) or None


def _phone_token(literal: str) -> PhoneToken | None:
    """Build a phone token, or None when the candidate is not phone-shaped."""
    trimmed = literal.strip().strip("-. \u2013\u2014")
    digits = re.sub(r"\D", "", trimmed)
    if len(digits) < _PROFILE_PHONE_MIN_DIGITS:
        return None
    # A leading NANP country code is not part of the number a file repeats.
    match_digits = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    return PhoneToken(literal=trimmed, digits=digits, match_digits=match_digits)


def _phone_tokens(line: str) -> list[PhoneToken]:
    """Phone tokens on this line, after splitting ' - ' clause boundaries."""
    tokens: list[PhoneToken] = []
    for clause in _PROFILE_PHONE_CLAUSE_RE.split(line):
        for match in _PROFILE_PHONE_RE.finditer(clause):
            token = _phone_token(match.group())
            if token is not None:
                tokens.append(token)
    return tokens


def read_owner_profile(path: Path) -> OwnerProfileTokens:
    """Read the owner's tokens from a profile, validating rather than repairing.

    The accepted shape is the whole contract, and it is the only shape this
    module checks for:

    * a labeled ``Name:`` field in any Markdown section, the label itself
      optionally bulleted or bold, whose value is a plain person-name phrase:
      one to five words of Unicode letters with internal hyphen, apostrophe, or
      period, and at most one trailing sentence terminator. The FIRST labeled
      ``Name:`` is the owner and later fields never replace it, because a later
      section may list other people.
    * email and phone literals below a heading whose text contains the word
      ``contact``, at any Markdown heading level.

    A ``Name:`` value outside that shape yields no person token and a stated
    reason; it is never trimmed, unwrapped, or otherwise repaired into one.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return OwnerProfileTokens(
            read_error=f"owner profile unreadable: {path} ({exc.strerror or type(exc).__name__})"
        )

    person_names: list[str] = []
    email_tokens: list[str] = []
    phone_tokens: list[PhoneToken] = []
    saw_name_label = False
    in_contact_section = False
    for line in lines:
        heading = _PROFILE_HEADING_RE.match(line)
        if heading:
            in_contact_section = bool(re.search(r"\bcontact\b", heading.group(1), re.I))
            continue
        label = _PROFILE_NAME_LABEL_RE.match(line)
        if label and not saw_name_label:
            saw_name_label = True
            value = label.group(1).strip()
            if _PROFILE_NAME_PHRASE_RE.match(value):
                person_names.append(_PROFILE_NAME_TERMINATOR_RE.sub("", value))
        if in_contact_section:
            email_tokens.extend(_PROFILE_EMAIL_RE.findall(line))
            phone_tokens.extend(_phone_tokens(line))

    email_tokens = list(dict.fromkeys(email_tokens))
    unique_phones = list({token.literal: token for token in phone_tokens}.values())

    kind_reasons: dict[str, str] = {}
    if not person_names:
        kind_reasons["person"] = (
            f"profile has no labeled Name field: {path}"
            if not saw_name_label
            else f"profile Name value is not a plain person-name phrase, so no name token: {path}"
        )
    if not email_tokens and not unique_phones:
        kind_reasons["contact"] = (
            f"profile has no email or phone literal under a heading containing 'contact': {path}"
        )

    return OwnerProfileTokens(
        person_names=person_names,
        email_tokens=email_tokens,
        phone_tokens=unique_phones,
        kind_reasons=kind_reasons,
    )
