"""Browser-side request timing: how long each /api request waited in the browser versus ran on the server.

The page reads each /api request's resource timing entry, aggregates it per day, engine, method and
route template, and posts small batches here. Rows are aggregate-only (counts, sums, a maximum and a
histogram). A route is stored only when it exactly equals a route template registered on the app, so a
client path with an id in it can never become a key. A measure the browser did not fill for a request is
not recorded for it, so its row count can be lower than ``total`` and unknown is never stored as zero.

This capture is diagnostic. Delete it (the table, both endpoints, the page's observer and the
``route`` Server-Timing entry) once the browser-wait versus server-time reading has been taken and the
decision it informs is made.

Measures, all in milliseconds:

- ``total``: start of the fetch until the last response byte.
- ``wait``: start of the fetch until the request was sent, minus connection setup. Queueing behind the
  browser's per-host connection limit and other stalls land here.
- ``connect``: name lookup, connect and TLS for a request that opened a connection.
- ``server``: the server's own duration, from its Server-Timing ``app`` entry.
- ``network``: request sent until last byte, minus the server's duration: transfer and travel.

Alongside the rows a batch may carry ``issues``: counts of what went wrong in the page's own capture
(see ``ISSUE_KINDS``), so a missing engine or a rejected POST is visible on the server instead of
looking like no use.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Final

from loguru import logger

from infra.product_events import analytics_disabled
from infra.request_timing import BUCKET_UPPERS, CLIENT_TIMING_RETENTION_DAYS, window_start

MEASURES = ("total", "wait", "connect", "server", "network")
ENGINES = ("chromium", "webkit", "gecko", "other")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER")
BUCKET_COUNT = len(BUCKET_UPPERS)
#: Rows per POST. The page's hide-time flush must fit the browser's 64 KiB limit on bodies sent with
#: keepalive; the largest row measured is 270 bytes (a 77-character route, the longest registered, and
#: five-digit counts), so 100 rows is under 28 KiB. The page chunks to this and the server enforces it.
BATCH_ROWS = 100
#: The longest registered /api route template is 77 characters; the room above that only lets the
#: request parse, since a route is stored only if it equals a registered template.
ROUTE_MAX_LENGTH = 128
#: A request's UTC day is the page's own clock. One day either side covers a flush that crosses UTC
#: midnight; a device clock more than a day off is wrong and its rows are dropped.
DAY_SLACK = timedelta(days=1)
#: The key the page uses when the response named no route (an engine that hides Server-Timing). It is
#: the only stored route that is not a registered template, so such an engine still shows its waits.
UNKNOWN_ROUTE: Final = "/<unknown>"
#: What the page's capture can report about itself. ``post_failed:<status>`` takes any HTTP status.
ISSUE_KINDS = ("observer_install_failed", "server_dropped", "post_failed:network")
_ISSUE_STATUS = re.compile(r"post_failed:[1-5]\d\d")
#: Distinct issue keys in one POST: the three fixed kinds plus a handful of statuses.
MAX_ISSUE_KEYS = 8
#: A page that counts more of one issue than this is already plainly broken; the exact count is noise.
ISSUE_COUNT_CAP = 1_000
#: A window is at most this many days (the table's retention, see request_timing).
MAX_WINDOW_DAYS = CLIENT_TIMING_RETENTION_DAYS

Row = tuple[date, str, str, str, str, int, float, float, list[int]]
Item = tuple[str, str, str, str, str, int, float, float, list[int]]

_posts_by_engine: Counter[str] = Counter()


def registered_route_templates(app) -> frozenset[str]:
    """The /api route templates registered on the app, as the request timing middleware names them.

    Routes are fixed once the app is built, so the set is computed once and kept on the app.
    """
    cached = getattr(app.state, "client_timing_route_templates", None)
    if cached is not None:
        return cached
    templates = {path for path in _paths(app.routes) if path.startswith("/api/")}
    app.state.client_timing_route_templates = frozenset(templates)
    return app.state.client_timing_route_templates


def _paths(routes) -> set[str]:
    found: set[str] = set()
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:  # a router included with a prefix
            found.update(context.path for context in contexts() if isinstance(getattr(context, "path", None), str))
        elif isinstance(getattr(route, "path", None), str):
            found.add(route.path)
    return found


def validate_row(item: Item, today: date, templates: frozenset[str]) -> Row | None:
    """Return one storable row, or None when it is outside what the page can truthfully send."""
    day, engine, method, route, measure, count, sum_ms, max_ms, buckets = item
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return None
    if abs(parsed - today) > DAY_SLACK or (route != UNKNOWN_ROUTE and route not in templates):
        return None
    if sum(buckets) != count:
        return None
    return (parsed, engine, method, route, measure, count, sum_ms, max_ms, buckets)


def clean_issues(issues: dict[str, int]) -> dict[str, int]:
    """Keep the known issue keys, with each count capped."""
    return {
        key: min(max(count, 0), ISSUE_COUNT_CAP)
        for key, count in list(issues.items())[:MAX_ISSUE_KEYS]
        if key in ISSUE_KINDS or _ISSUE_STATUS.fullmatch(key)
    }


def write_rows(conn, rows: list[Row]) -> None:
    """Merge rows into the daily table in one DuckDB transaction; raise on failure."""
    if not rows:
        return
    cur = conn.cursor()
    try:
        cur.sql("BEGIN")
        values = ", ".join("(?, ?, ?, ?, ?, ?, ?, ?, ?)" for _ in rows)
        params = [value for row in rows for value in row]
        cur.execute(
            "INSERT INTO client_request_timing_daily (day, engine, method, route, measure, count, sum_ms, "
            "max_ms, buckets) VALUES " + values + " "
            "ON CONFLICT (day, engine, method, route, measure) DO UPDATE SET "
            "count = client_request_timing_daily.count + excluded.count, "
            "sum_ms = client_request_timing_daily.sum_ms + excluded.sum_ms, "
            "max_ms = GREATEST(client_request_timing_daily.max_ms, excluded.max_ms), "
            "buckets = list_transform(list_zip(client_request_timing_daily.buckets, excluded.buckets), "
            "bucket -> bucket[1] + bucket[2])",
            params,
        )
        cur.sql("COMMIT")
    finally:
        cur.close()  # an uncommitted transaction rolls back on close


def ingest(engine: str, batch: list[Item], issues: dict[str, int], templates: frozenset[str]) -> tuple[int, int]:
    """Validate and store one client batch; return (kept, dropped) rows.

    ``dropped`` is what the server rejected or failed to write; with analytics disabled both are 0.

    One WARNING per batch that carries issues or dropped rows is the server-side signal that a page's
    capture is failing. Analytics is non-load-bearing: a failed write drops the rows and says so.
    """
    _posts_by_engine[engine] += 1
    today = datetime.now(UTC).date()
    if analytics_disabled():
        return 0, 0  # rows never meant to be stored are not dropped, and a disabled install has nothing to warn about
    rows = [row for item in batch if (row := validate_row(item, today, templates)) is not None]
    kept = len(rows)
    if rows:
        try:
            from infra.analytics_db import get_db

            write_rows(get_db(), rows)
        except Exception as error:  # analytics is intentionally non-load-bearing
            logger.warning("client request timing write failed; dropped {} rows: {}", kept, error)
            kept = 0
    dropped = len(batch) - kept
    kinds = clean_issues(issues)
    if kinds or dropped:
        logger.warning(
            "client request timing: engine={} sent={} kept={} dropped={} issues={} posts_from_engine={}",
            engine, len(batch), kept, dropped, kinds, _posts_by_engine[engine],
        )
    return kept, dropped


def query_client_timing(conn, days: int) -> list[tuple]:
    """Return the client timing rows for the inclusive UTC day window."""
    return conn.execute(
        "SELECT day, engine, method, route, measure, count, sum_ms, max_ms, buckets "
        "FROM client_request_timing_daily WHERE day >= ?", (window_start(days),),
    ).fetchall()


def _reset_for_tests() -> None:
    _posts_by_engine.clear()
