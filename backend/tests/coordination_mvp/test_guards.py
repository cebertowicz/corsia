"""S0 guard ratchets — fail any future PR that breaks a locked invariant.

These run GREEN today against the existing (PATH-A) ``events`` package and stay
green only while S1–S7 honor the locks:

1. **No connector-specific routing code** in the generic path — connector tokens
   live only in grandfathered PATH-A files or a declared ``events/connectors``
   home; new generic-router modules must be connector-agnostic
   (RFC-GENERIC-EVENT-ROUTING-BINDING; ED22/EM38; AC-0.4).
2. **Delivery taxonomy never absorbs a user-attention class** — no Enum in
   ``events`` may use ``warning``/``decision``/``report``/``user_raise``/… as a
   delivery disposition (ED5/ED18; AC-3.1/AC-1.4/AC-0.4).
3. **Parent-chain is digest-only** — no scenario/contract value wakes a
   ``parent_chain`` recipient (ED19; AC-3.5).
"""

from __future__ import annotations

import ast
import enum
import importlib
import pkgutil
from pathlib import Path

import pytest

from tests.coordination_mvp.fixtures import (
    CONNECTOR_TOKENS,
    DELIVERY_DISPOSITIONS,
    FORBIDDEN_DELIVERY_CLASSES,
    SUBSCRIBER_KINDS,
    all_scenarios,
)

# Files that already couple to a connector (the pre-MVP GitHub PATH-A). Connector
# coupling here is grandfathered; it must MIGRATE into a declared-data home as
# S4 generalizes routing — it must never spread to the generic path.
_GRANDFATHERED = frozenset({
    "github_mapper.py", "guard.py", "identity.py", "ingress.py",
    "markers.py", "reviewer_gate.py", "reviewer_route.py", "router.py",
})

# The declared-data home: connector specifics (manifests, mappers) may live here.
_CONNECTOR_HOME_DIRS = ("connectors", "manifests")

def _events_dir() -> Path:
    import events
    return Path(events.__file__).parent


def _generic_router_files() -> dict[str, Path]:
    """The generic-router modules that MUST stay connector-agnostic, at their
    ACTUAL paths — including binder.py under connections/ (an events-only scan
    silently missed it). Each path is asserted to exist so a move can't drop it
    from the guard."""
    backend = _events_dir().parent
    return {
        "events/resolve.py": backend / "events" / "resolve.py",
        "events/disposition.py": backend / "events" / "disposition.py",
        "events/envelope.py": backend / "events" / "envelope.py",
        "connections/binder.py": backend / "connections" / "binder.py",
    }


def _connector_tokens_in(path: Path) -> set[str]:
    """Connector tokens that appear in a source file (lowercased scan)."""
    src = path.read_text(encoding="utf-8").lower()
    return {tok for tok in CONNECTOR_TOKENS if tok in src}


def _is_in_connector_home(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return any(part in _CONNECTOR_HOME_DIRS for part in rel.parts)


class TestNoConnectorSpecificRouting:
    """Guard 1 — the generic routing path carries no connector branches."""

    def test_no_new_connector_coupled_module_outside_declared_home(self):
        root = _events_dir()
        violations: list[str] = []
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.name in _GRANDFATHERED:
                continue
            if _is_in_connector_home(path, root):
                continue
            toks = _connector_tokens_in(path)
            if toks:
                rel = path.relative_to(root)
                violations.append(f"{rel} -> {sorted(toks)}")
        assert not violations, (
            "Connector-specific tokens found in the generic event path. Move "
            "connector specifics into events/connectors/ (declared data) or keep "
            "the routing path connector-agnostic. Violations:\n  "
            + "\n  ".join(violations)
        )

    def test_named_generic_router_modules_are_connector_agnostic(self):
        files = _generic_router_files()
        # binder.py lives outside events/ — it must be in the scanned set so it
        # can never silently escape the guard again.
        assert any(label.endswith("binder.py") for label in files)
        offenders: list[str] = []
        for label, path in files.items():
            assert path.exists(), (
                f"generic-router module {label} not found at {path} — the guard "
                "cannot scan it; update _generic_router_files()")
            toks = _connector_tokens_in(path)
            if toks:
                offenders.append(f"{label} -> {sorted(toks)}")
        assert not offenders, (
            "A generic-router module names a connector — resolve / disposition / "
            "envelope / binder must be data-driven:\n  " + "\n  ".join(offenders)
        )

    def test_grandfather_list_matches_reality(self):
        # Keeps the ratchet honest: if a grandfathered file loses ALL connector
        # coupling, drop it from the list so new coupling there is caught too.
        root = _events_dir()
        stale = [
            name for name in _GRANDFATHERED
            if (root / name).exists() and not _connector_tokens_in(root / name)
        ]
        assert not stale, (
            "These files no longer reference any connector — remove them from "
            f"_GRANDFATHERED so the guard catches new coupling: {sorted(stale)}"
        )


class TestDeliveryTaxonomyNoAttentionClass:
    """Guard 2 — Event Delivery dispositions never include a user-attention class."""

    def _event_enums(self) -> list[type[enum.Enum]]:
        import events
        found: list[type[enum.Enum]] = []
        errors: list[str] = []
        for mod in pkgutil.walk_packages(events.__path__, prefix="events."):
            try:
                module = importlib.import_module(mod.name)
            except Exception as exc:  # noqa: BLE001 — collected and failed loudly below
                errors.append(f"{mod.name}: {exc!r}")
                continue
            for obj in vars(module).values():
                if (isinstance(obj, type) and issubclass(obj, enum.Enum)
                        and obj.__module__ == module.__name__):
                    found.append(obj)
        # Fail loud: a swallowed import error could hide a forbidden enum.
        assert not errors, (
            "events.* modules failed to import — the taxonomy guard cannot "
            "trust its enum scan:\n  " + "\n  ".join(errors))
        return found

    def test_no_enum_member_is_a_user_attention_class(self):
        offenders: list[str] = []
        for en in self._event_enums():
            for member in en:
                value = str(member.value).lower()
                name = member.name.lower()
                if value in FORBIDDEN_DELIVERY_CLASSES or name in FORBIDDEN_DELIVERY_CLASSES:
                    offenders.append(f"{en.__module__}.{en.__name__}.{member.name}={member.value!r}")
        assert not offenders, (
            "User-attention concepts (warning/decision/report/user_raise/…) must "
            "be artifact/attention workflows, not Event Delivery dispositions:\n  "
            + "\n  ".join(offenders)
        )

    def test_taxonomy_and_forbidden_sets_are_disjoint(self):
        overlap = DELIVERY_DISPOSITIONS & FORBIDDEN_DELIVERY_CLASSES
        assert not overlap, f"contract self-contradiction: {overlap}"

    def test_existing_route_outcome_has_no_attention_class(self):
        from events.router import RouteOutcome
        for member in RouteOutcome:
            assert str(member.value).lower() not in FORBIDDEN_DELIVERY_CLASSES


class TestParentChainDigestOnly:
    """Guard 3 — a parent_chain recipient is never woken."""

    def test_parent_chain_scenarios_are_digest(self):
        for scn in all_scenarios():
            if scn.subscriber_kind == "parent_chain":
                assert scn.disposition == "subscriber_digest", (
                    f"{scn.name}: parent_chain must be subscriber_digest, "
                    f"got {scn.disposition}")

    def test_no_scenario_wakes_a_parent_chain(self):
        wake_dispositions = {"owner_wake", "subscriber_wake"}
        offenders = [
            scn.name for scn in all_scenarios()
            if scn.subscriber_kind == "parent_chain" and scn.disposition in wake_dispositions
        ]
        assert not offenders, f"parent_chain woken by: {offenders}"

    def test_parent_chain_is_a_declared_subscriber_kind(self):
        assert "parent_chain" in SUBSCRIBER_KINDS


def test_fixtures_module_has_no_runtime_side_effects():
    """S0 introduces no behavior: importing fixtures must not touch the DB/net."""
    src = (Path(__file__).parent / "fixtures.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"get_db", "register_artifact", "deliver_to_thread", "emit_product_event"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & banned), f"fixtures call runtime fns: {called & banned}"
