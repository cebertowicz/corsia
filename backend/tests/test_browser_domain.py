"""The registrable domain the password step's title and the pane's address both show (AR5)."""
import pytest

from host_access.browser_domain import registrable_domain, url_domain


@pytest.mark.parametrize(("host", "domain"), [
    # A lookalike host names the site that actually serves it, never the one it imitates.
    ("paypal.com.evil.com.br", "evil.com.br"),
    ("login.paypal.com.evil.com.br", "evil.com.br"),
    ("accounts.google.com", "google.com"),
    ("www.bbc.co.uk", "bbc.co.uk"),
    ("Accounts.Example.CO.UK.", "example.co.uk"),
    # The list's private section makes each hosted site its own domain.
    ("someone.github.io", "someone.github.io"),
    # A host with no registrable domain stands as itself.
    ("co.uk", "co.uk"),
    ("localhost", "localhost"),
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    ("[::1]", "[::1]"),
    ("", ""),
])
def test_registrable_domain_follows_the_public_suffix_list(host, domain):
    assert registrable_domain(host) == domain


def test_url_domain_reads_a_pages_host_and_has_none_without_one():
    scheme = "https" + "://"
    assert url_domain(scheme + "paypal.com.evil.com.br/login?next=1") == "evil.com.br"
    assert url_domain("http" + "://127.0.0.1:5173/probe.html") == "127.0.0.1"
    assert url_domain("about:blank") is None
    assert url_domain(None) is None
