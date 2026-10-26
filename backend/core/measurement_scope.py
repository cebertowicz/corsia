"""Canonical Measurement view filters and completed-week windows, without HTTP."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TREND_RANGES = ("12w", "26w", "all")
GRAINS = ("days", "weeks")
# Periods drawn per grain, and periods in each side of the one comparison rule.
_RANGE_PERIODS = {"days": 14, "weeks": 12}
_COMPARE_PERIODS = {"days": 7, "weeks": 4}
# Stored UTC instant text at a grain's precision: whole hours, or minutes for half-hour zones.
_INSTANT_FORMATS = {13: "%Y-%m-%d %H", 16: "%Y-%m-%d %H:%M"}


def _now() -> datetime:
    """The request clock; one place for a test to pin the page's today."""
    return datetime.now(timezone.utc)


class ScopeError(ValueError):
    """A rejected view filter with its existing HTTP adapter status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def is_workspace_scope(project: str, agent: str | None) -> bool:
    """True only at All work. A fact that belongs to the whole workspace (the
    subscription bill, account usage, what was paid) is served here and nowhere
    narrower, so a project or agent view never shows it as its own."""
    return project == "all" and agent is None


@dataclass(frozen=True)
class MeasurementScope:
    """The only Measurement scope: an optional active agent inside a project."""

    project: str
    agent: str | None

    def params(self, user_id: str) -> dict[str, str | None]:
        return {"user_id": user_id, "scope_project": self.project, "scope_agent": self.agent}

    def response(self) -> dict[str, str | None]:
        return {"project": self.project, "agent": self.agent}

    @property
    def is_workspace(self) -> bool:
        return is_workspace_scope(self.project, self.agent)


def validate_scope(db, user_id: str, project: str, agent: str | None) -> MeasurementScope:
    """Validate existing project/active-agent filters, without widening visibility."""
    if project != "all" and not project:
        raise ScopeError(400, "project is required")
    if agent == "none":
        agent = None
    if agent == "":
        raise ScopeError(400, "agent must be an id or none")
    if project != "all":
        # The project catalog is workspace-wide and already listed to every user, so a
        # project with no threads yet is a real, empty scope; its threads stay per-user.
        row = db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()
        if not row:
            raise ScopeError(404, "project not found")
    if agent:
        row = db.execute(
            "SELECT project_id FROM threads WHERE id=? AND user_id=? "
            "AND COALESCE(agent_enabled,0)=1 AND COALESCE(agent_status,'active')='active'",
            (agent, user_id),
        ).fetchone()
        if not row:
            raise ScopeError(404, "agent not found")
        if project != "all" and row["project_id"] != project:
            raise ScopeError(400, "agent is not in project")
    return MeasurementScope(project=project, agent=agent)


def scope_cte() -> str:
    """The existing same-user root/descendant closure, shared by all consumers.

    Project/all filters start at top-level threads. A selected active agent may
    itself have a parent. Orphan subtrees are unreachable; no owner history is
    recovered by scanning a deleted parent's descendants.
    """
    return """
        WITH RECURSIVE scope_roots(id) AS (
          SELECT t.id FROM threads t
          WHERE t.user_id=:user_id
            AND (:scope_project='all' OR t.project_id=:scope_project)
            AND (:scope_agent IS NULL AND t.parent_thread_id IS NULL
                 OR t.id=:scope_agent
                 AND COALESCE(t.agent_enabled,0)=1
                 AND COALESCE(t.agent_status,'active')='active')
        ), scope_threads(id, root_id) AS (
          SELECT id,id FROM scope_roots
          UNION ALL
          SELECT child.id,parent.root_id FROM scope_threads parent
            CROSS JOIN threads child ON child.parent_thread_id=parent.id
            WHERE child.user_id=:user_id
        ), top_plans(id) AS (
          SELECT p.id FROM plans p JOIN scope_roots r ON r.id=p.owner_id
          WHERE p.owner_kind='thread'
        )
    """


def parse_trend_range(value: str) -> str:
    if value not in _TREND_RANGES:
        raise ScopeError(400, "range must be 12w, 26w, or all")
    return value


def completed_week_bounds(value: str, earliest: date | None = None, *,
                          now: datetime | None = None) -> tuple[date, date]:
    """Inclusive Monday bounds; the partial current UTC week never enters."""
    trend_range = parse_trend_range(value)
    today = (now or _now()).date()
    current_monday = today - timedelta(days=today.weekday())
    end = current_monday - timedelta(days=7)
    if trend_range == "all":
        return (earliest - timedelta(days=earliest.weekday()) if earliest else end), end
    weeks = 12 if trend_range == "12w" else 26
    return end - timedelta(days=7 * (weeks - 1)), end


def usage_bounds(start: date | str, end: date | str) -> tuple[str, str]:
    """Bare-date bounds include either space/T timestamp separator correctly."""
    start_date = date.fromisoformat(str(start)[:10])
    end_date = date.fromisoformat(str(end)[:10])
    return start_date.isoformat(), (end_date + timedelta(days=7)).isoformat()


def completed_week_window(value: str, earliest: date | None = None, *,
                          now: datetime | None = None) -> tuple[str, str]:
    return usage_bounds(*completed_week_bounds(value, earliest, now=now))


def query_params(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize the one composite scope and add sargable usage bounds."""
    project = params.get("scope_project")
    agent = params.get("scope_agent", "")
    if not isinstance(project, str) or not project or not (agent is None or (isinstance(agent, str) and agent)):
        raise ValueError("Measurement scope is composite: pass scope_project and scope_agent")
    grain = params.get("grain")
    if grain is not None:
        # Whole UTC dates that cover every period, in-progress one included, unless the
        # caller narrowed the read; the period bucket then drops what lies outside the
        # exact local range.
        to_day = date.fromisoformat(grain.current_end[:10]) + timedelta(days=1)
        return {"from_ts": grain.starts[0][:10], "to_ts": to_day.isoformat(), **params, **grain.binds()}
    if "from_ts" in params:
        return params
    from_ts, to_ts = usage_bounds(params["from"], params["to"])
    return {**params, "from_ts": from_ts, "to_ts": to_ts}


# --- The page grain: one set of local periods for every Measurement read --------------------
#
# Days are local calendar days and weeks are local Monday-to-Sunday weeks, in the viewer's own
# zone, so a day or week starts at local midnight. Every period is a half-open pair of UTC
# instants, so a day that crosses a DST change is 23 or 25 hours long and nothing is shifted.
# The wire key for a period is ``period``; it holds the period's first local date.


@dataclass(frozen=True)
class Window:
    """Inclusive first and last period key of one comparison side."""

    first: str
    last: str
    periods: tuple[str, ...]


@dataclass(frozen=True)
class Grain:
    """One grain, in one viewer zone, at one instant: the periods every read buckets by."""

    kind: str
    zone: str
    current: str
    periods: tuple[str, ...]
    starts: tuple[str, ...]
    end: str
    current_end: str
    precision: int
    lead: int = 0

    @property
    def served(self) -> tuple[str, ...]:
        """The completed periods the page draws: ``periods`` without the lead-in read only for averages."""
        return self.periods[self.lead:]

    @property
    def average_periods(self) -> int:
        """Periods in one moving average: the length of a comparison window, 7 days or 4 weeks."""
        return _COMPARE_PERIODS[self.kind]

    @property
    def latest(self) -> Window:
        """The last comparison window of completed periods: 7 days, or 4 weeks."""
        return self._window(0)

    @property
    def previous(self) -> Window:
        """The window of the same length right before ``latest``."""
        return self._window(1)

    def unaveraged(self) -> Grain:
        """The same calendar at the same instant without the averaging lead-in: only the drawn periods."""
        return replace(self, periods=self.served, starts=self.starts[self.lead:], lead=0)

    def averaged(self) -> Grain:
        """The same calendar at the same instant with the averaging lead-in: what ``unaveraged`` drops, read again.

        Equal to the grain ``resolve_grain(..., averaged=True)`` gave for any request that day, so a read keyed
        by it joins the one the page's own averaged panels made.
        """
        noon = datetime.combine(date.fromisoformat(self.current), time(12), parse_zone(self.zone))
        return resolve_grain(self.kind, self.zone, now=noon, averaged=True)

    def _window(self, back: int) -> Window:
        size = _COMPARE_PERIODS[self.kind]
        stop = len(self.periods) - back * size
        keys = self.periods[stop - size:stop]
        return Window(keys[0], keys[-1], keys)

    def period_end(self, key: str) -> str:
        """The last local date a period covers."""
        last = date.fromisoformat(key) + timedelta(days=0 if self.kind == "days" else 6)
        return last.isoformat()

    @staticmethod
    def utc_midnights(days: Iterable[date], zone: ZoneInfo) -> tuple[datetime, ...]:
        """Convert local calendar midnights into the UTC instants Measurement stores."""
        return tuple(datetime.combine(day, time.min, zone).astimezone(timezone.utc) for day in days)

    def period_instants(self, key: str) -> tuple[str, str]:
        """The exact half-open UTC interval for one display period at this grain's precision."""
        start = date.fromisoformat(key)
        stop = start + timedelta(days=1 if self.kind == "days" else 7)
        before, after = self.utc_midnights((start, stop), parse_zone(self.zone))
        return before.strftime(_INSTANT_FORMATS[self.precision]), after.strftime(_INSTANT_FORMATS[self.precision])

    def period_of(self, day: str) -> str:
        """Key of the period a local date falls in."""
        value = date.fromisoformat(day[:10])
        return (value if self.kind == "days" else value - timedelta(days=value.weekday())).isoformat()

    def binds(self) -> dict[str, str]:
        """The bound values ``bucket`` refers to: every period's first instant and its key."""
        keys = (*self.periods, self.current)
        binds = {f"gb{index}": start for index, start in enumerate(self.starts)}
        binds.update({f"gk{index}": key for index, key in enumerate(keys)})
        binds.update({"gend": self.end, "gend_partial": self.current_end})
        return binds

    def bucket(self, column: str, *, partial: bool = False) -> str:
        """SQL naming the period a stored timestamp falls in, or NULL outside the range.

        Stored timestamps come in ``T`` and space forms, so the column is normalized to the
        precision the boundaries have, then compared with them as text.
        """
        key = f"replace(substr({column},1,{self.precision}),'T',' ')"
        keys = (*self.periods, self.current)
        branches = " ".join(f"WHEN {key}>=:gb{index} THEN :gk{index}" for index in range(len(keys) - 1, -1, -1))
        end = ":gend_partial" if partial else ":gend"
        return f"(CASE WHEN {key}>={end} THEN NULL {branches} END)"

    def instants(self, window: Window) -> tuple[str, str]:
        """The window's half-open [start, end) UTC instants, as ``YYYY-MM-DD HH[:MM]`` text."""
        return self.starts[self.periods.index(window.first)], self.starts[self.periods.index(window.last) + 1]

    def window_sql(self, column: str, window: Window) -> str:
        """SQL keeping a stored timestamp column inside a window, for either separator form.

        The first two conditions are a whole-date range an index on the column can use; the
        last two are the exact local-midnight bounds.
        """
        start, end = self.instants(window)
        last_day = (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()
        key = f"replace(substr({column},1,{self.precision}),'T',' ')"
        return f"({column}>='{start[:10]}' AND {column}<'{last_day}' AND {key}>='{start}' AND {key}<'{end}')"

    def describe(self) -> dict[str, Any]:
        """What the page reads out of a response: the grain, its zone and every window it compared."""
        def span(window: Window) -> dict[str, str]:
            return {"from": window.first, "to": self.period_end(window.last)}
        return {
            "grain": self.kind, "timezone": self.zone, "current": self.current,
            "range": {"from": self.served[0], "to": self.period_end(self.periods[-1])},
            "latest": span(self.latest), "previous": span(self.previous),
        }


def parse_grain(value: str) -> str:
    if value not in GRAINS:
        raise ScopeError(400, "grain must be days or weeks")
    return value


def parse_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScopeError(422, "timezone must be an IANA timezone") from exc


def resolve_grain(kind: str, zone_name: str, *, now: datetime | None = None, averaged: bool = False) -> Grain:
    """The completed periods of one grain in the viewer's zone, ending with the one in progress.

    ``averaged`` reads one comparison window less one more periods before the drawn range, so the
    first drawn period still has a full span for its moving average.
    """
    parse_grain(kind)
    zone = parse_zone(zone_name)
    today = (now or _now()).astimezone(zone).date()
    step = 1 if kind == "days" else 7
    current = today if kind == "days" else today - timedelta(days=today.weekday())
    lead = _COMPARE_PERIODS[kind] - 1 if averaged else 0
    count = _RANGE_PERIODS[kind] + lead
    firsts = [current - timedelta(days=step * back) for back in range(count, -1, -1)]
    instants = Grain.utc_midnights((*firsts, current + timedelta(days=step)), zone)
    precision = 13 if all(instant.minute == 0 for instant in instants) else 16
    text = [instant.strftime(_INSTANT_FORMATS[precision]) for instant in instants]
    return Grain(kind=kind, zone=zone_name, current=current.isoformat(),
                 periods=tuple(day.isoformat() for day in firsts[:-1]), starts=tuple(text[:-1]),
                 end=text[-2], current_end=text[-1], precision=precision, lead=lead)
