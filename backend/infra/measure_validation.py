"""Sync-time checks for a ``measures.yaml`` declaration file (Agent KPIs design §4.2, §5)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from core.agent_kpi_formula import unfit
from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS

_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
# Namespaces an agent never declares: every registered connector, ``worker`` (CORSIA produces it
# only) and the generic names. Static because infra must not import events; a test fails when a
# registered connector is missing here.
_RESERVED = {
    "corsia", "test", "script", "agent", "otel", "worker",
    "document", "github", "gmail", "library", "mailhost", "measurement", "microsoft", "plan",
}
# The one definition of what the server stamps on every declared event: a file can't declare these,
# the registry adds them to each event's specs, and admission removes them from an emitter's attributes.
STAMPED_ATTRIBUTES = ("run_id", "workflow_ref", "workflow_version")
_KINDS = {"id", "login", "count", "num"}
# How far back an emitter may date an event, in days: the default, and the bounds a file may set.
KEEP_DAYS_DEFAULT, KEEP_DAYS_MAX = 400, 800

Item = tuple[str, str, dict[str, Any]]
_BOOL = "tag:yaml.org,2002:bool"


class _Loader(yaml.SafeLoader):
    """YAML 1.2 booleans: only true and false, so ``on:`` and ``alert: off`` stay plain strings."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != _BOOL]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(_BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def keep_days(spec: dict[str, Any]) -> int:
    """An event's ``keep_days``, or ValueError when it is not a whole number of days from 1 to 800.

    Sync refuses a bad value with its line; the registry uses this same check to skip a bad stored spec.
    """
    value = spec.get("keep_days", KEEP_DAYS_DEFAULT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= KEEP_DAYS_MAX:
        raise ValueError(f"keep_days must be a whole number of days from 1 to {KEEP_DAYS_MAX}, not {value!r}")
    return value


def load(text: str) -> Any:
    """Parse a declaration file; raises ``yaml.YAMLError`` on invalid YAML."""
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


class Lines:
    """Maps a key path in the YAML text to its line, so a refusal can name it."""

    def __init__(self, text: str) -> None:
        try:
            self.root: yaml.Node | None = yaml.compose(text)
        except yaml.YAMLError:
            self.root = None

    def line(self, *path: Any) -> int:
        node, line = self.root, 1
        for segment in path:
            if isinstance(node, yaml.MappingNode):
                found = next(((k, v) for k, v in node.value if getattr(k, "value", None) == segment), None)
                if found is None:
                    break
                node, line = found[1], found[0].start_mark.line + 1
            elif isinstance(node, yaml.SequenceNode) and isinstance(segment, int) and segment < len(node.value):
                node = node.value[segment]
                line = node.start_mark.line + 1
            else:
                break
        return line


@dataclass
class _Context:
    events: dict[str, dict[str, Any]]
    workflow: str | None
    lines: Lines
    quality: int = 0


def fail(lines: Lines, path: tuple[Any, ...], message: str) -> ValueError:
    return ValueError(f"measures.yaml line {lines.line(*path)}: {message}")


def validate(data: dict[str, Any], lines: Lines) -> tuple[str, list[Item]]:
    """Return the file's namespace and its event, KPI and view declarations, or raise the refusal."""
    if data.get("schema") != "corsia.measures/1":
        raise fail(lines, ("schema",), 'schema must be "corsia.measures/1"; use the required schema.')
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace) or namespace in _RESERVED:
        raise fail(lines, ("namespace",), "namespace must be a non-reserved lowercase name.")
    workflow = data.get("workflow")
    if workflow is not None and not isinstance(workflow, str):
        raise fail(lines, ("workflow",), "workflow must name an owned workflow.")
    items = _event_items(data, namespace, lines)
    events = {name: spec for kind, name, spec in items if kind == "event"}
    for name, spec in events.items():
        if "since" in spec:
            _check_since(name, spec, events, lines)
    _check_version_attr(data, events, lines)
    context = _Context(events, workflow, lines)
    for kind, key in (("kpi", "kpis"), ("view", "views")):
        items.extend(_declarations(data, kind, key, context))
    return namespace, items


def _event_items(data: dict[str, Any], namespace: str, lines: Lines) -> list[Item]:
    events = data.get("events", {})
    if not isinstance(events, dict) or not events:
        raise fail(lines, ("events",), "events must declare at least one event.")
    if len(events) > 64:
        raise fail(lines, ("events",), "events allows at most 64 events.")
    items: list[Item] = []
    for name, spec in events.items():
        path = ("events", name)
        if not isinstance(name, str) or not name.startswith(namespace + "."):
            raise fail(lines, path, f"event {name} is outside namespace {namespace}; declare only {namespace}.* events.")
        if not isinstance(spec, dict) or set(spec) - {"attributes", "dedupe", "corroborate", "keep_days", "use", "since"}:
            raise fail(lines, path, f"event {name} has an unknown field; use attributes, dedupe, corroborate, keep_days, use or since.")
        _check_event(name, spec, lines)
        items.append(("event", name, spec))
    return items


def _check_event(name: str, spec: dict[str, Any], lines: Lines) -> None:
    attrs, dedupe = spec.get("attributes"), spec.get("dedupe")
    path = ("events", name)
    if not isinstance(attrs, dict) or not isinstance(dedupe, list) or not dedupe:
        raise fail(lines, path, f"event {name} needs attributes and dedupe.")
    if len(attrs) > 16:
        raise fail(lines, path + ("attributes",), f"event {name} attributes allows at most 16 attributes.")
    for key, kind in attrs.items():
        valid = isinstance(kind, str) and kind in _KINDS
        valid = valid or (isinstance(kind, dict) and set(kind) == {"enum"} and isinstance(kind["enum"], list) and 0 < len(kind["enum"]) <= 20 and all(isinstance(x, str) for x in kind["enum"]))
        if key in STAMPED_ATTRIBUTES:
            raise fail(lines, path + ("attributes", key), f"attribute {key} is reserved; the server stamps it. Remove it from attributes.")
        if not valid:
            raise fail(lines, path + ("attributes", key), f'attribute kind "{kind}" is not allowed; use id, login, count, num or enum.')
    if any(key != "run_id" and key not in attrs for key in dedupe):
        raise fail(lines, path + ("dedupe",), f"event {name} dedupe names an undeclared attribute.")
    try:
        keep_days(spec)
    except ValueError as exc:
        raise fail(lines, path + ("keep_days",), f"event {name} {exc}; the default is {KEEP_DAYS_DEFAULT}.") from None
    if spec.get("use", "record") not in {"record", "route", "both"}:
        raise fail(lines, path + ("use",), f"event {name} use must be record, route or both.")
    # A routed type's facts reach other agents' wakes, so no person's login; a record stays in the
    # owner's store. num may ride along: rules can't match on it (enforced at rule create).
    if spec.get("use") in ("route", "both"):
        for key, kind in attrs.items():
            if kind == "login":
                raise fail(lines, path + ("attributes", key), f'event {name} is use: {spec["use"]}, so attribute {key} cannot be "login"; use id, or declare use: record.')


def _attributes(event: Any, events: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(event, str):
        return None
    if event in events:
        return events[event]["attributes"]
    if event in ATTRIBUTE_ALLOWLISTS:
        return ATTRIBUTE_ALLOWLISTS[event]
    return None


def _check_since(name: str, spec: dict[str, Any], events: dict[str, dict[str, Any]], lines: Lines) -> None:
    path = ("events", name, "since")
    since = spec["since"]
    if not isinstance(since, dict) or set(since) - {"event", "on", "as"}:
        raise fail(lines, path, f"event {name} since must contain event, on and as only.")
    start = _attributes(since.get("event"), events)
    if start is None:
        raise fail(lines, path + ("event",), f"event {name} since.event must name a declared or platform event.")
    on = since.get("on")
    pairs = {on: on} if isinstance(on, str) else on
    if not isinstance(pairs, dict) or not pairs or any(
        not isinstance(end, str) or not isinstance(begin, str) or end not in spec["attributes"] or begin not in start
        for end, begin in pairs.items()
    ):
        raise fail(lines, path + ("on",), f"event {name} since.on must name attributes declared on both events.")
    target = since.get("as")
    if not isinstance(target, str) or spec["attributes"].get(target) not in ("num", "count"):
        raise fail(lines, path + ("as",), f"event {name} since.as must name a num or count attribute of the end event.")


def _check_where(name: str, operand: dict[str, Any], path: tuple[Any, ...], context: _Context) -> None:
    where = operand.get("where")
    if where is None:
        return
    if not isinstance(where, dict) or len(where) > 2:
        raise fail(context.lines, path + ("where",), f"kpi {name} where needs at most two conditions.")
    for event in _event_names(operand):
        attrs = _attributes(event, context.events) or {}
        for key, value in where.items():
            kind = attrs.get(key, "id" if event in context.events and key in ("run_id", "workflow_ref") else None)
            kind = getattr(kind, "kind", kind)
            is_enum = kind == "enum" or isinstance(kind, dict) and "enum" in kind
            if kind != "id" and not is_enum:
                raise fail(context.lines, path + ("where", key), f"kpi {name} where needs an id or enum attribute on every event.")
            if is_enum and value in ("present", "absent"):
                raise fail(context.lines, path + ("where", key), f"kpi {name} where present and absent are allowed only on id attributes.")


def _check_operand_keys(name: str, spec: dict[str, Any], context: _Context) -> None:
    coverage = spec.get("coverage")
    operands: list[tuple[tuple[str, ...], Any]] = [((key,), spec.get(key)) for key in ("num", "den")]
    if isinstance(coverage, dict):
        operands += [(("coverage", key), coverage.get(key)) for key in ("of", "labelled")]
    for position, operand in operands:
        if not isinstance(operand, dict):
            continue
        path = ("kpis", name) + position
        _check_where(name, operand, path, context)
        for index, clause in enumerate(operand.get("match", [])):
            if isinstance(clause, dict):
                _check_where(name, clause, path + ("match", index), context)
        for field in ("latest", "first"):
            if field not in operand:
                continue
            path = ("kpis", name) + position + (field,)
            keys = operand[field]
            if "latest" in operand and "first" in operand:
                raise fail(context.lines, path, f"kpi {name} operand must use only one of latest or first.")
            names = _event_names(operand)
            raw_events = operand.get("event")
            if (not isinstance(keys, list) or not 1 <= len(keys) <= 2 or not names
                    or isinstance(raw_events, list) and len(names) != len(raw_events)):
                raise fail(context.lines, path, f"kpi {name} {field} needs one or two id or enum attributes and an event.")
            for event in names:
                attrs = _attributes(event, context.events)
                for key in keys:
                    kind = attrs.get(key) if attrs is not None and isinstance(key, str) else None
                    if event in context.events and key in ("run_id", "workflow_ref"):
                        kind = "id"
                    kind = getattr(kind, "kind", kind)
                    if kind != "id" and kind != "enum" and not (isinstance(kind, dict) and "enum" in kind):
                        raise fail(context.lines, path, f"kpi {name} {field} needs id or enum attributes declared on every event.")


def _check_version_attr(data: dict[str, Any], events: dict[str, dict[str, Any]], lines: Lines) -> None:
    version_attr = data.get("version_attr")
    if version_attr is None:
        return
    if not data.get("workflow"):
        raise fail(lines, ("version_attr",), "version_attr is allowed only in a file that names a workflow you own. Add workflow: <name>, or remove version_attr.")
    if not isinstance(version_attr, str) or not any(spec["attributes"].get(version_attr) == "id" for spec in events.values()):
        raise fail(lines, ("version_attr",), f"version_attr {version_attr} must name an id attribute declared on an event in this file. Declare it as an id, or remove version_attr.")


def _declarations(data: dict[str, Any], kind: str, key: str, context: _Context) -> list[Item]:
    values = data.get(key, {} if kind == "kpi" else [])
    if kind == "kpi" and not isinstance(values, dict) or kind == "view" and not isinstance(values, list):
        raise fail(context.lines, (key,), f"{key} is not a valid declaration list.")
    pairs = values.items() if isinstance(values, dict) else [(item.get("id") if isinstance(item, dict) else None, item) for item in values]
    items: list[Item] = []
    for index, (name, spec) in enumerate(pairs):
        if not name or not isinstance(spec, dict):
            raise fail(context.lines, (key, name if kind == "kpi" else index), f"{key} contains an invalid declaration.")
        if kind == "kpi":
            _check_operand_keys(str(name), spec, context)
            _check_kpi(str(name), spec, context)
        items.append((kind, str(name), spec))
    return items


def _event_names(operand: Any) -> list[str]:
    event = operand.get("event") if isinstance(operand, dict) else None
    if isinstance(event, str):
        return [event]
    return [name for name in event if isinstance(name, str)] if isinstance(event, list) else []


def _operand_events(spec: dict[str, Any]) -> list[str]:
    """The events the KPI reads: its operands, plus any event a ``match`` anchors on."""
    names: list[str] = []
    for operand in (spec.get("num"), spec.get("den")):
        names += _event_names(operand)
        clauses = operand.get("match") if isinstance(operand, dict) else None
        for clause in clauses if isinstance(clauses, list) else []:
            if isinstance(clause, dict) and clause.get("anchor") is True:
                names += _event_names(clause)
    return names


def _check_kpi(name: str, spec: dict[str, Any], context: _Context) -> None:
    """The ``can_decide`` sync test (§4.2): each check is a field check, with its §5 refusal."""
    path = ("kpis", name)
    if problem := unfit(spec):
        raise fail(context.lines, path, f"kpi {name} {problem}.")
    deciding, decides_as = spec.get("can_decide", False), spec.get("decides_as")
    if not isinstance(deciding, bool):
        raise fail(context.lines, path + ("can_decide",), f"kpi {name} can_decide must be true or false.")
    if decides_as is not None and decides_as not in ("quality", "spend"):
        raise fail(context.lines, path + ("decides_as",), f'kpi {name} decides_as "{decides_as}" is not one of quality, spend. Use quality or spend.')
    if decides_as is not None and not deciding:
        raise fail(context.lines, path + ("decides_as",), f"kpi {name} decides_as needs can_decide: true. Add can_decide: true, or remove decides_as.")
    if not deciding:
        return
    where = path + ("can_decide",)
    missing = [field for field in ("target", "review") if field not in spec]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        raise fail(context.lines, where, f"kpi {name} can_decide: true needs target and review; {' and '.join(missing)} {verb} missing. Add {', '.join(f'{m}: <value>' for m in missing)}, or set can_decide: false.")
    if not _version_capable(spec, context):
        raise fail(context.lines, where, f"kpi {name} can_decide: true needs a KPI that can carry a workflow version (no operand anchors on a workflow event). Add match … anchor: true on a workflow event, or set can_decide: false.")
    if not _has_independent_basis(spec, context):
        raise fail(context.lines, where, f"kpi {name} can_decide: true needs a numerator that a CORSIA emitter writes, that declares corroborate, or a KPI that declares coverage (so another agent's audit can label it). Add coverage: {{of: …, by: …}}, add corroborate to the event, or set can_decide: false.")
    if decides_as in (None, "quality"):
        context.quality += 1
        if context.quality > 2:
            ordinal = "the third" if context.quality == 3 else f"number {context.quality}"
            raise fail(context.lines, where, f"kpi {name} can_decide: true with decides_as quality is {ordinal} in workflow {context.workflow}; a workflow may have two. Set can_decide: false on one, or set decides_as: spend if it measures cost.")


def deciding_refusal(name: str, spec: dict[str, Any], events: dict[str, dict[str, Any]], workflow: str | None,
                     version_attr: str | None = None) -> str | None:
    """Run the sync-time ``can_decide`` test (§4.2), version rule and ``version_attr`` check again when a
    verdict is read; the refusal text, or None when the KPI still passes. The two-quality limit spans a
    workflow's KPIs, so its caller counts it."""
    lines = Lines("")
    try:
        _check_version_attr({"workflow": workflow, "version_attr": version_attr}, events, lines)
        _check_kpi(name, spec, _Context(events, workflow, lines))
    except ValueError as exc:
        return str(exc).split(": ", 1)[-1]
    return None


def _version_capable(spec: dict[str, Any], context: _Context) -> bool:
    """The Version rule: an operand declared in a file that names a workflow its owner owns."""
    if not context.workflow:
        return False
    builtin = any(isinstance(spec.get(key), dict) and "builtin" in spec[key] for key in ("num", "den"))
    return builtin or any(event in context.events for event in _operand_events(spec))


def _has_independent_basis(spec: dict[str, Any], context: _Context) -> bool:
    """Check 3: an emitter CORSIA writes, a corroborate declaration, or a coverage line."""
    if spec.get("coverage"):
        return True
    num = spec.get("num")
    if isinstance(num, dict) and "builtin" in num:
        return True
    names = _event_names(num)
    return bool(names) and all(
        name in ATTRIBUTE_ALLOWLISTS or bool(context.events.get(name, {}).get("corroborate")) for name in names
    )
