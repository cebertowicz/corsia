"""Cloud-provider keys in the key store.

A provider's key is the entry ``provider-<name>``. Add-provider and the
provider routes write it through here, and a reader resolves it with
``key_store.resolve_provider`` or :func:`provider_keys`. Nothing reads it from
the settings table or the environment.
"""

from __future__ import annotations

import sqlite3

from infra import key_store

#: The prefix lives in ``key_store`` so the name rules there can reserve it.
PREFIX = key_store.PROVIDER_PREFIX


def entry_name(provider: str) -> str:
    """The key store entry holding ``provider``'s key."""
    if provider not in key_store.PROVIDER_NAMES:
        raise key_store.KeyStoreError("No key has that name.")
    return f"{PREFIX}{provider}"


def provider_keys(*, db: sqlite3.Connection | None = None) -> dict[str, str]:
    """Every provider that has a key, by provider name."""
    stored = key_store.resolve_names((entry_name(name) for name in sorted(key_store.PROVIDER_NAMES)), db=db)
    return {
        name: stored[entry_name(name)]
        for name in sorted(key_store.PROVIDER_NAMES)
        if entry_name(name) in stored
    }


def set_provider_key(name: str, value: object, *, db: sqlite3.Connection | None = None) -> None:
    """Store a provider's key, replacing any earlier one."""
    key_store.set_managed_value(entry_name(name), value, db=db)


def remove_provider_key(name: str, *, db: sqlite3.Connection | None = None) -> None:
    """Remove a provider's key, entry and value. Removing an absent one is fine."""
    key_store.drop_managed_entry(entry_name(name), db=db)


def provider_key_rows(keys: dict[str, str], labels: dict[str, str], hidden: frozenset[str]) -> list[dict]:
    """Status rows for Settings › Keys: never a value.

    ``labels`` names the cloud providers to show; a name in ``hidden`` is a
    legacy provider that appears only while it still holds a key.
    """
    from infra.ignored_environment import ignored_variable

    return [
        {
            "name": entry_name(provider),
            "provider": provider,
            "label": label,
            "is_set": provider in keys,
            "unsupported": provider in hidden,
            "ignored_env_var": ignored_variable(entry_name(provider)),
        }
        for provider, label in labels.items()
        if provider in keys or provider not in hidden
    ]
