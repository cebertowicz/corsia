"""The records a verdict is made of: definitions, run histories, the frozen plan, arms, the verdict itself.

Split out of ``core.workflow_verdict_rules`` (which imports and re-exposes them) to keep that file inside the
size standard; the rules read these, nothing here decides.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Mapping

from core.workflow_verdicts import DEFAULT_CONFIDENCE, POLICY_VERSION, MeasureCall, PlannedMeasure, Run, parse_ts

PLAN_TEXT_LIMIT = 2_000  # infra.outcome_event_attributes: measurement.verdict.planned plan


@dataclass(frozen=True)
class Definition:
    kind: str
    name: str
    version: int
    spec: Mapping[str, object]
    synced_at: str
    removed: bool
    file_path: str
    version_attr: str | None = None


@dataclass(frozen=True)
class WorkflowHistory:
    ref: str
    owner: str | None
    runs: tuple[Run, ...]
    definitions: tuple[Definition, ...] = ()
    # Event-derived verdict runs use this rule in verdict slice 5.
    version_attr: str | None = None
    verdict_confidence: float = DEFAULT_CONFIDENCE
    verdict_confidence_reason: str | None = None
    excluded: Mapping[str, int] = field(default_factory=dict)  # never-judged runs, counted: unversioned, local_edit

    def _in_start_order(self) -> list[Run]:
        return sorted(self.runs, key=lambda item: (parse_ts(item.started_at), item.run_id))

    def versions(self) -> list[str]:
        """Judged versions by first use (v1, v2, ...)."""
        first: dict[str, str] = {}
        for run in self._in_start_order():
            first.setdefault(run.version, run.started_at)
        return list(first)

    def first_use(self, version: str) -> str:
        return min((run.started_at for run in self.runs if run.version == version), key=parse_ts)

    def successor(self, version: str) -> tuple[str, str] | None:
        """The version that replaced ``version`` and when: the first other version to run after its last run,
        whether newer or rolled back to; None while it is still the one in use."""
        ordered = self._in_start_order()
        last = max(i for i, run in enumerate(ordered) if run.version == version)
        after = next((run for run in ordered[last + 1:] if run.version != version), None)
        return (after.version, after.started_at) if after is not None else None

    def predecessors(self, version: str) -> list[str]:
        """The other versions that ran before ``version``'s first use, latest run first: the one it replaced,
        then the one before that. The reverse of ``successor``: after v1, v2, back to v1, then v3, v3 replaced
        v1, so v1 comes before v2."""
        ordered = self._in_start_order()
        first = next(i for i, run in enumerate(ordered) if run.version == version)
        return list(dict.fromkeys(run.version for run in reversed(ordered[:first])))


@dataclass(frozen=True)
class FrozenPlan:
    subject: str
    version: str
    baseline: str | None
    skipped: tuple[str, ...]
    measures: tuple[PlannedMeasure, ...]
    minimum: int
    refusals: tuple[tuple[str, str], ...]
    seed: int
    policy_version: str = POLICY_VERSION
    confidence: float = DEFAULT_CONFIDENCE
    confidence_reason: str | None = None

    def encoded(self) -> str:
        """The plan as the ``measurement.verdict.planned`` text; refusals keep only the KPI names."""
        data = {**asdict(self), "measures": [list(asdict(m).values()) for m in self.measures],
                "refusals": [name for name, _why in self.refusals]}
        text = json.dumps(data, sort_keys=True, separators=(",", ":"))
        if len(text) > PLAN_TEXT_LIMIT:
            raise ValueError(f"frozen plan for {self.subject} is {len(text)} characters; the record holds {PLAN_TEXT_LIMIT}")
        return text

    @classmethod
    def decoded(cls, text: str) -> FrozenPlan:
        data = json.loads(text)
        return cls(data["subject"], data["version"], data["baseline"], tuple(data["skipped"]),
                   tuple(PlannedMeasure(*item) for item in data["measures"]), int(data["minimum"]),
                   tuple((name, "") for name in data["refusals"]), int(data["seed"]), data["policy_version"],
                   float(data.get("confidence", DEFAULT_CONFIDENCE)), data.get("confidence_reason"))


@dataclass(frozen=True)
class Verdict:
    word: str  # keep, revert (stored); changed_early, collecting, not_enough_data (computed on read)
    reason: str
    look: int
    calls: tuple[MeasureCall, ...] = ()
    inputs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    subject: str
    owner: str | None
    plan: FrozenPlan
    verdict: Verdict
    plan_is_new: bool


@dataclass(frozen=True)
class TwoArms:
    """A neutral comparison input: the current/control arm and the changed arm."""

    control: tuple[Run, ...]
    changed: tuple[Run, ...]
    # Mental model, "Measured zero versus too few to tell": an unclear look 2 continues once, unless another
    # lesson waits for the same section. No reader of waiting lessons exists yet, so callers pass True.
    continue_if_unclear: bool = True
    # A fixed baseline (a workflow's previous version) or halves that grow together (a lesson's with and without).
    matched: bool = False


@dataclass(frozen=True)
class Snapshot:
    now: str
    workflows: tuple[WorkflowHistory, ...]
    plans: Mapping[str, FrozenPlan] = field(default_factory=dict)
    reached: frozenset[str] = frozenset()
    subjects: frozenset[str] | None = None  # None: all versions; set: this bounded open batch
