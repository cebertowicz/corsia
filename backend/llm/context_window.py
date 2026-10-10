"""Single source for a model's live context window (I7).

Discovery writers populate ``models.context_window``. Operators may override a
Native window in ``model_profiles.context_window`` or cap a Claude Code/Codex
window there; every runtime consumer resolves through this module. When nothing
is saved for a row, the effective window is the provider's own capped by
``DEFAULT_CHECKPOINT_WINDOW`` — a resolver rule, never a stored value. A window
is a runtime fact: it is read freshly on every resolution, never cached for
process lifetime, and a provider overflow rejection re-enters discovery here
instead of becoming a user-facing sentence.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Mapping, NamedTuple

from loguru import logger

# The window CORSIA checkpoints against when nothing is saved for the row. It
# is a resolver rule only: nothing writes it to ``model_profiles``, and a saved
# window replaces it exactly as it replaces the provider's own.
DEFAULT_CHECKPOINT_WINDOW = 262_144

# A selector or provider model id that names a bigger window owns that choice:
# ``claude-opus-5-5[1m]`` is not silently cut back to the default. The suffix
# must read as a window (a size and a k/m unit), so an unrelated bracketed name
# cannot opt out of the default by accident.
_EXTENDED_SUFFIX = re.compile(r"\[\d+(?:[.,]\d+)?[km]\]$", re.IGNORECASE)


class ModelContextWindowUnknown(ValueError):
    """Raised when the DB has no per-model context-window value."""


def effective_usage_window(selected: object, reported: object) -> int | None:
    """Keep the resolved model limit, capped by any physical runtime evidence.

    Runtime telemetry is not authority to enlarge a selected catalog limit.
    Missing telemetry does not erase an already-resolved model denominator.
    """
    windows = [value for value in (selected, reported)
               if isinstance(value, int) and not isinstance(value, bool) and value > 0]
    return min(windows) if windows else None


def validate_profile_context_window(updates: Mapping[str, Any]) -> None:
    """Validate an optional model-profile window update before persistence."""
    if "context_window" not in updates:
        return
    window = updates.get("context_window")
    if window is None:
        return
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("context_window must be a positive integer")


def _catalog_row(model: str, *, db=None) -> dict | None:
    """Fetch the freshest catalog row for a model id or transport string."""
    close = db is None
    if db is None:
        from infra.database import get_db

        db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM models WHERE id = ? OR litellm_model = ? "
            "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
            (model, model, model),
        ).fetchone()
        if row is None and "@" in model:
            provider, _, rest = model.partition("@")
            if provider == "omlx":
                _, _, provider_model_id = rest.partition("/")
                if provider_model_id:
                    row = db.execute(
                        "SELECT * FROM models "
                        "WHERE provider = ? AND provider_model_id = ? AND available = 1",
                        (provider, provider_model_id),
                    ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            db.close()


def _profile_context_window(model_id: str, *, db=None) -> int | None:
    """Read a profile window strictly, except from a pre-profile database."""
    close = db is None
    try:
        if db is None:
            from infra.database import get_db

            db = get_db()
        try:
            prof = db.execute(
                "SELECT context_window FROM model_profiles WHERE model_id = ?",
                (model_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if str(exc) == "no such table: model_profiles":
                return None
            raise
        return _profile_row_window(prof, model_id)
    finally:
        if close and db is not None:
            db.close()


def _profile_row_window(prof: Mapping[str, Any] | None, model_id: str) -> int | None:
    """Validate the window stored on one loaded ``model_profiles`` row."""
    stored = prof["context_window"] if prof else None
    if stored is None:
        return None
    if isinstance(stored, bool) or not isinstance(stored, int):
        raise ValueError(
            f"invalid persisted context_window for {model_id}: {stored!r}"
        )
    return stored if stored > 0 else None


def _optional_profile_context_window(model_id: str, *, db=None) -> int | None:
    """Read an optional native override, logging and ignoring failures."""
    return _ignoring_failure(model_id, lambda: _profile_context_window(model_id, db=db))


def _ignoring_failure(model_id: str, read: Callable[[], int | None]) -> int | None:
    """Run a saved-window read, logging any failure and reading it as no override."""
    try:
        return read()
    except Exception as exc:
        logger.warning(
            "model_profiles.context_window lookup failed for {}: {}",
            model_id,
            exc,
        )
        return None


def saved_window_override(profile_row: Mapping[str, Any] | None, model_id: str) -> int | None:
    """Return one valid saved profile window, never a discovered value."""
    try:
        return _profile_row_window(profile_row, model_id)
    except ValueError as exc:
        logger.warning("model_profiles.context_window for {}: {}", model_id, exc)
        return None


def saved_overrides_by_catalog_id(
    rows: list[Mapping[str, Any]], *, db
) -> dict[str, int | None]:
    """Return every saved profile window keyed by its catalog row id."""
    ids = [str(row["id"]) for row in rows if row["id"]]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    profiles = {
        str(profile["model_id"]): profile
        for profile in db.execute(
            f"SELECT model_id, context_window FROM model_profiles WHERE model_id IN ({marks})",
            ids,
        ).fetchall()
    }
    return {model_id: saved_window_override(profiles.get(model_id), model_id) for model_id in ids}

def provider_window_facts(
    provider: str, *, db
) -> tuple[list[Mapping[str, Any]], list[WindowFacts], dict[str, int | None]]:
    """Load one provider's catalog rows and canonical checkpoint facts."""
    rows = db.execute("SELECT id, provider_model_id, harness, context_window FROM models WHERE provider = ?", (provider,)).fetchall()
    facts = resolve_window_facts_for_rows(rows, db=db)
    try:
        overrides = saved_overrides_by_catalog_id(rows, db=db)
    except sqlite3.Error as exc:
        logger.warning("model_profiles batch lookup failed for {}: {}", provider, exc)
        overrides = {}
    return rows, facts, overrides


def known_discovery_context_identities(models) -> dict:
    """Project prior discovery identities that already carry a window fact."""
    return {key.partition('/')[2]: value.get('resolved_identity')
            for key, value in models.items() if value.get('context_window')}


def _stored_window(row: Mapping[str, Any]) -> int | None:
    """Read a catalog row's discovered window, the one normalizer for it.

    Only resolving an execution target reads it; every harness then takes both
    windows from that one resolved target (#1868).
    """
    stored = row.get("context_window")
    try:
        window = int(stored) if stored and not isinstance(stored, bool) else 0
    except (TypeError, ValueError):
        return None
    return window if window > 0 else None


class WindowFacts(NamedTuple):
    """The windows a surface needs to describe its own denominator.

    ``effective`` is what CORSIA counts against; ``discovered`` is the
    provider's own capacity. They differ exactly when an operator cap or the
    default is in force, and a surface that shows only ``effective`` cannot
    tell a user whether the limit they see is the model's or CORSIA's.
    ``source`` names which one produced ``effective`` — ``saved``, the CORSIA
    ``default``, or the ``provider`` — so no surface recomputes that rule.
    """

    effective: int | None
    discovered: int | None
    source: str = "provider"


def resolve_window_facts(row: Mapping[str, Any], *, db=None) -> WindowFacts:
    """Resolve both the counted-against and the discovered window for one row.

    Native rows honor the operator's ``model_profiles`` override outright.
    Claude Code and Codex rows may use that value only as a cap: the resolved
    result is no larger than the discovered window, so profiles cannot
    manufacture a larger runtime fact. When the CLI reports no window at all,
    the person's saved window is the only fact and is used as saved. An external harness keeps its own
    conversation, so its real occupancy is bounded by ``discovered`` and NOT by
    a CORSIA cap — which is why both numbers have to travel together. With
    nothing saved, every harness takes ``DEFAULT_CHECKPOINT_WINDOW`` as its
    ceiling instead, except a selector that names a bigger window by name.
    """
    data = dict(row)
    harness = str(data.get("harness") or "native")
    model_id = str(data.get("id") or "")
    override = _saved_override(
        harness, model_id, lambda: _profile_context_window(model_id, db=db)
    )
    return _facts_with_override(data, harness, override)


def resolve_unsaved_window_facts(row: Mapping[str, Any]) -> WindowFacts:
    """The facts that apply with nothing saved: what clearing a saved window lands on.

    Same rule as ``resolve_window_facts`` with the saved window ignored, so an
    editor can show it without recomputing the default or extended-suffix rule.
    """
    data = dict(row)
    return _facts_with_override(data, str(data.get("harness") or "native"), None)


def _saved_override(
    harness: str, model_id: str, read: Callable[[], int | None]
) -> int | None:
    """The one per-harness rule for a saved window, shared by both resolvers.

    Claude Code and Codex read it strictly, so a broken profile cannot lift
    their cap; native logs and ignores any failure; other harnesses have none.
    """
    if not model_id:
        return None
    if harness in {"claude-code", "codex"}:
        return read()
    if harness == "native":
        return _ignoring_failure(model_id, read)
    return None


def resolve_window_facts_for_rows(
    rows: list[Mapping[str, Any]], *, db
) -> list[WindowFacts]:
    """``resolve_window_facts`` for many rows with one ``model_profiles`` read.

    A failed read is replayed to each row through ``_saved_override``, so every
    row gets exactly the facts the single-row resolver would give it.
    """
    datas = [dict(row) for row in rows]
    ids = [str(d.get("id") or "") for d in datas if d.get("id")]
    profiles: dict[str, Mapping[str, Any]] = {}
    load_error: Exception | None = None
    if ids:
        marks = ",".join("?" for _ in ids)
        try:
            found = db.execute(
                "SELECT model_id, context_window FROM model_profiles "
                f"WHERE model_id IN ({marks})",
                ids,
            ).fetchall()
            profiles = {str(p["model_id"]): p for p in found}
        except sqlite3.OperationalError as exc:
            if str(exc) != "no such table: model_profiles":
                load_error = exc

    def read_for(model_id: str) -> Callable[[], int | None]:
        def read() -> int | None:
            if load_error is not None:
                raise load_error
            return _profile_row_window(profiles.get(model_id), model_id)
        return read

    facts = []
    for data in datas:
        harness = str(data.get("harness") or "native")
        model_id = str(data.get("id") or "")
        override = _saved_override(harness, model_id, read_for(model_id))
        facts.append(_facts_with_override(data, harness, override))
    return facts


def _names_extended_window(data: Mapping[str, Any]) -> bool:
    """Whether the row's selector or provider model id names a bigger window."""
    return any(
        _EXTENDED_SUFFIX.search(str(data.get(key) or ""))
        for key in ("id", "provider_model_id", "litellm_model")
    )


def _facts_with_override(
    data: Mapping[str, Any], harness: str, override: int | None
) -> WindowFacts:
    # A malformed stored window is no evidence, but a native operator override
    # stands on its own and is still honored.
    discovered = _stored_window(data)
    if harness == "native" and override:
        return WindowFacts(override, discovered, "saved")
    if discovered is None:
        if harness in {"claude-code", "codex"} and override:
            return WindowFacts(override, None, "saved")
        return WindowFacts(None, None, "provider")
    if harness in {"claude-code", "codex"} and override:
        return WindowFacts(min(discovered, override), discovered, "saved")
    if _names_extended_window(data):
        return WindowFacts(discovered, discovered, "provider")
    if discovered > DEFAULT_CHECKPOINT_WINDOW:
        return WindowFacts(DEFAULT_CHECKPOINT_WINDOW, discovered, "default")
    return WindowFacts(discovered, discovered, "provider")


def resolve_window_for_row(row: Mapping[str, Any], *, db=None) -> int | None:
    """Resolve the live window CORSIA counts against for one catalog row."""
    return resolve_window_facts(row, db=db).effective


def get_context_window_or_none(model: str, *, db=None) -> int | None:
    """Resolve the live context window, reporting absence as None."""
    row = None
    try:
        row = _catalog_row(model, db=db)
    except Exception as exc:
        logger.warning("model catalog lookup failed for {}: {}", model, exc)
    if row is not None:
        return resolve_window_for_row(row, db=db)
    # No catalog row: an optional override keyed by the raw id may still pin it.
    return _optional_profile_context_window(model, db=db)


def get_context_window(model: str) -> int:
    """Resolve an exact context window from the canonical catalog.

    Native model profiles remain explicit operator overrides. Harness profiles
    may only cap their discovered row value. Raises when no evidence exists —
    do NOT add an ``or 128000`` fallback; fix the discovery path instead.
    """
    window = get_context_window_or_none(model)
    if window:
        return window
    raise ModelContextWindowUnknown(
        f"No context_window populated for model {model!r}. Fix the discovery "
        f"path — do NOT add an `or 128000` fallback."
    )
