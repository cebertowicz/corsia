"""Per-provider account facts behind the shared ``vendor_accounts`` rows.

Every fact that differs between providers' accounts lives here: the provider's
own commands and home variable, the fixed causes it reports, the identity
facts it may store and compare, and its label. Code elsewhere reads a profile
instead of naming a provider. ``VENDORS`` is the one set that may launch
inference and its profiles are built in. GitHub's profile is built in too: it
holds the agents' own GitHub account, never a thread binding or a launch.
``register`` adds only another provider that holds accounts.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from connections import github_agent_home, github_person
from connections.vendor_accounts import VENDORS, AccountError


def _no_paid_capacity(usage: dict, verdict: dict) -> bool:
    return False


@dataclass(frozen=True)
class AccountProfile:
    """One vendor's account rules; the rows themselves stay in ``vendor_accounts``."""

    provider: str
    label: str
    # The variable that points the provider's CLI at one account's home.
    home_variable: str
    # Where this provider's added accounts' private homes are created.
    home_root: Callable[[], Path]
    # The vendor's own login, status and logout arguments.
    commands: Mapping[str, tuple[str, ...]]
    missing_cause: str
    in_progress_cause: str
    # Facts shown to the user; comparison keys are stored too, but only compared.
    identity_facts: tuple[str, ...]
    comparison_keys: tuple[str, ...]
    # Stored fact -> key in the vendor's JSON status report, where it reports one.
    status_report_keys: Mapping[str, str]
    clean_env: Callable[[], dict[str, str]]

    # A provider-supplied validator for the hint stored at add time, where it takes one.
    validate_hint: Callable[[dict], dict] | None
    # Whether this provider's workspace default may be chosen at all.
    workspace_default: bool
    sign_out: Callable[[dict, Path, Path], Awaitable[dict]]
    # Whether the vendor's stored facts say paid capacity still covers a plan-exhaustion
    # verdict, given the account's usage and that verdict. Only ``overage_policy`` asks, with
    # the spend switch on. The default is no such capacity: Claude settles extra usage when it
    # reads the plan (``claude_usage.usage_verdict``), so a stored Claude limit is already final.
    paid_capacity: Callable[[dict, dict], bool] = _no_paid_capacity
    # A provider whose status command is not the shared JSON ``loggedIn`` report reads its own:
    # (exit code, stdout) -> (signed in, or None when unknown; identity facts).
    parse_status: Callable[[int, bytes], tuple[bool | None, dict[str, str]]] | None = None
    # The prompt a sign-in prints for the person to act on, read from its output line by line.
    signin_prompt: Callable[[str], dict[str, str] | None] | None = None
    # Why a completed sign-in in this home may not stand: (home, status facts) -> a fixed cause or None.
    signin_refusal: Callable[[str, dict[str, str]], str | None] | None = None
    # At most one non-removed account row for this provider.
    single_account: bool = False
    # Run the vendor's logout in the home before a sign-in, so a stale login cannot stay beside the new one.
    logout_before_login: bool = False
    # A held login is also done once the vendor's status reads signed in, even if the login has not exited:
    # status, not the login process, is what decides signed in.
    status_ends_login: bool = False

    @property
    def stored_facts(self) -> tuple[str, ...]:
        """Every vendor-reported fact an account row may keep."""
        return self.identity_facts + tuple(key for key in self.comparison_keys if key not in self.identity_facts)

    def facts_from_status(self, report: dict) -> dict[str, str]:
        """The string facts a signed-in status report names; anything else is dropped."""
        return {fact: report[key] for fact, key in self.status_report_keys.items() if isinstance(report.get(key), str)}


def home_root() -> Path:
    """Where added accounts' private homes are created."""
    from infra.config import RUNTIME_DIR

    return RUNTIME_DIR / "vendor-accounts"


async def _vendor_sign_out(account: dict, source: Path, runtime: Path) -> dict:
    """Run the vendor's own logout for this account's home."""
    from connections.account_cleanup import vendor_logout

    return await vendor_logout(account)


def _claude_env() -> dict[str, str]:
    from subagents import cc_env

    return cc_env.clean_claude_env()


def _codex_env() -> dict[str, str]:
    from subagents import codex_env

    return codex_env.clean_codex_env()


def _codex_paid_capacity(usage: dict, verdict: dict) -> bool:
    from connections.codex_usage_read import paid_capacity

    return paid_capacity(usage, verdict)


_SHOWN_FACTS = ("email", "org_name", "org_id", "plan")
# Keep Codex sign-in in the account's own auth.json, never the OS keyring.
_FILE_STORE = ("-c", 'cli_auth_credentials_store="file"')
_PROFILES: dict[str, AccountProfile] = {
    "claude-code": AccountProfile(
        provider="claude-code", label="Claude Code", home_variable="CLAUDE_CONFIG_DIR",
        home_root=home_root,
        commands={"login": ("auth", "login"), "status": ("auth", "status"), "logout": ("auth", "logout")},
        missing_cause="claude_executable_missing", in_progress_cause="claude_signin_in_progress",
        identity_facts=_SHOWN_FACTS, comparison_keys=("email", "org_id"),
        status_report_keys={"email": "email", "org_name": "orgName", "org_id": "orgId", "plan": "subscriptionType"},
        clean_env=_claude_env,
        validate_hint=None, workspace_default=False, sign_out=_vendor_sign_out,
        status_ends_login=True),
    "codex": AccountProfile(
        provider="codex", label="Codex", home_variable="CODEX_HOME",
        home_root=home_root,
        commands={"login": (*_FILE_STORE, "login"), "status": (*_FILE_STORE, "login", "status"),
                  "logout": (*_FILE_STORE, "logout")},
        missing_cause="codex_executable_missing", in_progress_cause="codex_signin_in_progress",
        identity_facts=_SHOWN_FACTS, comparison_keys=("account_fp",),
        # Codex identity comes from its auth file, not a status report.
        status_report_keys={},
        clean_env=_codex_env,
        validate_hint=None, workspace_default=False, sign_out=_vendor_sign_out,
        paid_capacity=_codex_paid_capacity),
    # The agents' own GitHub account. ``--insecure-storage`` keeps the token in the home's
    # hosts.yml, never the person's keychain: only there do the read-only shell grant and the
    # credential fingerprint cover it. ``workflow`` lets agents push workflow changes.
    "github": AccountProfile(
        provider="github", label="the agents' GitHub account", home_variable="GH_CONFIG_DIR",
        home_root=github_agent_home.home_root,
        commands={"login": ("auth", "login", "--hostname", "github.com", "--git-protocol", "https",
                            "--web", "--insecure-storage", "--scopes", "workflow"),
                  "status": ("auth", "status", "--hostname", "github.com", "--active", "--json", "hosts"),
                  "logout": ("auth", "logout", "--hostname", "github.com")},
        missing_cause="github_executable_missing", in_progress_cause="github_signin_in_progress",
        identity_facts=("login",), comparison_keys=("login",), status_report_keys={},
        clean_env=github_agent_home.clean_env,
        validate_hint=None, workspace_default=False, sign_out=_vendor_sign_out,
        parse_status=github_agent_home.parse_status, signin_prompt=github_agent_home.signin_prompt,
        signin_refusal=github_person.signin_refusal, single_account=True, logout_before_login=True),
}
assert tuple(_PROFILES)[:len(VENDORS)] == VENDORS


def register(profile: AccountProfile) -> None:
    """Add one non-inference provider's profile; a built-in vendor or a duplicate is refused."""
    if profile.provider in _PROFILES:
        raise AccountError("account provider is already registered")
    _PROFILES[profile.provider] = profile


def account_providers() -> tuple[str, ...]:
    """Every provider that may hold account rows, in registration order."""
    return tuple(_PROFILES)


def profile_for(provider: str) -> AccountProfile:
    """The registered profile for a provider."""
    profile = _PROFILES.get(provider)
    if profile is None:
        raise AccountError("account provider is not registered")
    return profile


async def sign_out_account(account: dict) -> dict:
    """Run a vendor's sign-out hook for one account, after its state is committed.

    The one caller of every profile's hook. An account with no home (a removed
    tombstone kept from before every account had one) has nothing to sign out.
    """
    if account["source_home"] is None:
        return {"status": "skipped", "cause": "no_home"}
    return await profile_for(account["provider"]).sign_out(
        account, Path(account["source_home"]), Path(account["runtime_home"]))


def account_label(account: dict, *, rows: list[dict] | None = None) -> dict:
    """Return the person-facing label from this account's own evidence."""
    from connections.vendor_accounts import DEFAULT_ACCOUNT_NAME, list_accounts

    if rows is None:
        rows = list_accounts(provider=account["provider"], include_removed=True)
    provider_rows = [row for row in rows if row["provider"] == account["provider"]]
    if not any(row["id"] == account["id"] for row in provider_rows):
        raise AccountError("account is not present in its supplied label rows")

    identity = account["identity"]
    if (account["provider"] in {"claude-code", "codex"} and account["state"] == "signed_out"
            and identity.get("upgrade_reason") == "sign_in_to_keep_using_account"):
        hint = identity.get("hint")
        if isinstance(hint, dict):
            identity = hint
    email = identity.get("email")
    email = email if isinstance(email, str) and email else None
    custom_name = None if account["is_default"] and account["name"] == DEFAULT_ACCOUNT_NAME else account["name"]

    if email is None:
        if custom_name:
            return {"name": custom_name, "email": None, "text": custom_name, "custom": custom_name, "identified": True}
        plan = identity.get("plan")
        plan = plan if isinstance(plan, str) and plan else None
        vendor = {"claude-code": "Claude", "codex": "ChatGPT"}.get(account["provider"], account["provider"])
        name = f"{vendor} {plan}" if plan else vendor
        return {"name": name, "email": None, "text": name, "custom": None, "identified": plan is not None}

    detail_key = "org_name" if account["provider"] == "claude-code" else "workspace"
    detail = identity.get(detail_key)
    detail = detail if isinstance(detail, str) and detail else None
    if custom_name:
        name = f"{custom_name} · {detail}" if detail else custom_name
        text = f"{name} · {email}"
        return {"name": name, "email": email, "text": text, "custom": custom_name, "identified": True}
    if detail:
        if account["provider"] == "claude-code" and detail == f"{email}'s Organization":
            return {"name": email, "email": email, "text": email, "custom": None, "identified": True}
        return {"name": detail, "email": email, "text": f"{detail} · {email}", "custom": None, "identified": True}
    return {"name": email, "email": email, "text": email, "custom": None, "identified": True}
