"""The hosts CORSIA knows: servers from each host's own help pages, never looked up elsewhere.

A known host's settings come from this table only, so an agent can't point a known address
at another server. Each host lists where the person makes its app password and the click
path there. Values are from the evidence file for the design; items it marks Not verified
(AOL's server and calendar, iCloud's CalDAV username form) are noted where they sit.
"""

from __future__ import annotations

from dataclasses import dataclass

from connections.mailhost.endpoints import Endpoints, Server, domain_of


@dataclass(frozen=True)
class KnownHost:
    name: str
    domains: tuple[str, ...]
    imap: Server
    smtp: Server
    caldav: str | None
    app_password_url: str
    click_path: str
    enable_imap_step: str | None = None
    #: What the host calls its mail product, for "Turn on IMAP in <product> settings".
    product: str = ""
    #: iCloud's IMAP username is the bare name; the full address also works, so both are tried.
    bare_name_alternate: bool = False
    #: False when ``app_password_url`` is only the host's homepage: the row then names the page in plain text, not as a link.
    shows_account_link: bool = True

    def endpoints(self, address: str) -> Endpoints:
        alternates = (address.split("@", 1)[0],) if self.bare_name_alternate else ()
        return Endpoints(
            name=self.name, known=True, username=address, imap=self.imap, smtp=self.smtp, caldav=self.caldav,
            app_password_url=self.app_password_url, click_path=self.click_path,
            enable_imap_step=self.enable_imap_step, username_alternates=alternates,
        )


def _zoho(tld: str, domains: tuple[str, ...], imap_host: str, smtp_host: str, calendar: str) -> KnownHost:
    return KnownHost(
        name="Zoho", domains=domains, imap=Server(imap_host, 993), smtp=Server(smtp_host, 465),
        caldav=calendar, app_password_url=f"https://accounts.zoho.{tld}/",
        click_path="Zoho Accounts › Security › App passwords › Generate New Password",
        enable_imap_step="Turn on IMAP in Zoho Mail: Settings › Mail Accounts › your address › IMAP Access.",
        product="Zoho Mail",
    )


def _gmx(domains: tuple[str, ...], tld: str) -> KnownHost:
    return KnownHost(
        name="GMX", domains=domains, imap=Server(f"imap.gmx.{tld}", 993), smtp=Server(f"mail.gmx.{tld}", 465),
        caldav=f"https://caldav.gmx.{tld}/", app_password_url="https://www.gmx.com/",
        click_path="GMX Settings › Security › Application-specific passwords",
        enable_imap_step="Turn on POP3 and IMAP in GMX: Settings › POP3 & IMAP › Allow access.",
        product="GMX Mail",
        shows_account_link=False,  # www.gmx.com is the homepage; the account-security page is behind sign-in and was not verified
    )


KNOWN_HOSTS: tuple[KnownHost, ...] = (
    KnownHost(
        "iCloud", ("icloud.com", "me.com", "mac.com"), Server("imap.mail.me.com", 993), Server("smtp.mail.me.com", 587),
        "https://caldav.icloud.com/", "https://account.apple.com/",
        "Sign-In and Security › App-Specific Passwords › Generate an app-specific password "
        "(two-factor sign-in is required)", product="iCloud Mail", bare_name_alternate=True,
    ),
    KnownHost(
        "Yahoo", ("yahoo.com", "ymail.com", "rocketmail.com", "yahoo.co.uk", "yahoo.ca", "yahoo.com.au", "yahoo.fr",
                  "yahoo.de", "yahoo.es", "yahoo.it", "yahoo.in", "yahoo.com.br", "yahoo.com.mx"),
        Server("imap.mail.yahoo.com", 993), Server("smtp.mail.yahoo.com", 465),
        "https://caldav.calendar.yahoo.com/", "https://login.yahoo.com/account/security",
        "Account Security › External connections › Create app password › Generate password",
        product="Yahoo Mail",
    ),
    KnownHost(
        "Fastmail", ("fastmail.com", "fastmail.fm", "messagingengine.com"),
        Server("imap.fastmail.com", 993), Server("smtp.fastmail.com", 465),
        "https://caldav.fastmail.com/", "https://app.fastmail.com/settings/security/integrations",
        "Settings › Privacy & Security › Connected apps & API tokens › Manage app passwords and access › "
        "New app password (choose Mail, Contacts & Calendars)", product="Fastmail",
    ),
    # AOL: its own help page names export.imap.aol.com, the Thunderbird database imap.aol.com (Not verified),
    # and its CalDAV is reported dead; the check reports the calendar as not offered when it is.
    KnownHost(
        "AOL", ("aol.com", "aol.co.uk", "aol.de", "aol.fr", "aim.com"),
        Server("imap.aol.com", 993), Server("smtp.aol.com", 465),
        "https://caldav.aol.com/", "https://login.aol.com/account/security",
        "Account Security › Generate app password", product="AOL Mail",
    ),
    _zoho("com", ("zoho.com", "zohomail.com"), "imap.zoho.com", "smtp.zoho.com", "https://calendar.zoho.com/"),
    _zoho("eu", ("zoho.eu", "zohomail.eu"), "imap.zoho.eu", "smtp.zoho.eu", "https://calendar.zoho.eu/"),
    _zoho("in", ("zoho.in", "zohomail.in"), "imap.zoho.in", "smtp.zoho.in", "https://calendar.zoho.in/"),
    _zoho("com.au", ("zoho.com.au", "zohomail.com.au"), "imap.zoho.com.au", "smtp.zoho.com.au",
          "https://calendar.zoho.com.au/"),
    _zoho("jp", ("zoho.jp", "zohomail.jp"), "imap.zoho.jp", "smtp.zoho.jp", "https://calendar.zoho.jp/"),
    _gmx(("gmx.com", "gmx.us"), "com"),
    _gmx(("gmx.net", "gmx.de", "gmx.at", "gmx.ch"), "net"),
)
_BY_DOMAIN = {domain: host for host in KNOWN_HOSTS for domain in host.domains}


def known_host(address: str) -> KnownHost | None:
    """The known host an address's domain belongs to, or None."""
    return _BY_DOMAIN.get(domain_of(address))
