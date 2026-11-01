# Measuring work

CORSIA counts what its agents finished, how much of the person's time it took and what the models used. This page covers how those numbers are produced and what they refuse to claim. For how to read the Measurement page in the app, see [Measurement](../measurement.md).

## What it is

- **Series:** one named measurement over time, such as outcomes, first-try acceptance or fresh tokens per outcome. Each is defined once, server-side, in a registry (`backend/api/measurement_registry.py`).
- **Family:** the group of series that read the same source, such as outcomes, active time, decisions, corrections, tokens or limits.
- **Scope:** the slice measured: all work, one project, or one agent. A project and an agent can combine.
- **Outcome:** an eligible accepted plan task. Measurement excludes user-assigned tasks, correction nodes and tasks owned by spawned worker plans. See [Plans](plans.md).

## Why it exists

A platform that claims to save a person's time should show whether it does, and should not rely on the agents' own summaries. Measuring from the records the platform already keeps, plan acceptance, decision answers, presence, the token ledger, means the numbers do not need an agent to report them. The same numbers let a project compare one way of working with another; see [Coordination models](coordination-models.md), whose declared KPIs name registry series.

## How it works

```mermaid
flowchart TB
    R[Records<br/>plans, decisions,<br/>presence, tokens] --> P[Prepared tables<br/>per request]
    P --> F[Family queries]
    F --> M[Registry rules<br/>min n, proxy, break]
    M --> A["/api/measurement<br/>read-only"]
    A --> U[Measurement page]
```

### Sources are existing records

Series read from records the system already writes for other reasons (`backend/api/measurement_queries.py`):

- **Outcomes and first-try acceptance:** eligible accepted plan tasks, aggregated by the period in which they settled. **Rejections:** recorded review send-backs, grouped by when they happened.
- **Active time:** the person's recorded presence in the app on that scope.
- **Decisions and corrections:** raised decisions, how they were answered, and recorded corrections.
- **Tokens:** the retained usage ledger, with cache re-reads counted separately and excluded from "fresh" tokens.
- **Limits:** recorded usage-limit hits.
- **Size:** the newest size rating for an outcome, or its author's declared size when no rating exists. Retracted, malformed, stale, conflicting and unknown latest ratings are counted but not used; outcomes can also be unsized. See [size rating import](../size-rating-import.md).

The API is read-only. It serves series, an agents table, a models table and a lessons report (`backend/api/measurement.py`).

### Rules the registry applies

Each series carries rules that the API enforces, so no chart has to remember them:

- **Minimum sample.** A ratio is withheld, served with no value, until it has enough samples: 20 outcomes for most ratios, 10 answered decisions for the shares of how decisions were answered. The raw value is kept alongside so the cut-off is visible.
- **Not measured is not zero.** A period before a series' source existed is served as not measured. A chart is never given a zero to draw a baseline through.
- **Proxies are labeled.** A series that estimates something, such as active hours or asks per outcome, carries a proxy flag.
- **Comparability is marked.** Each series has a method version. The API also uses a stored source-start mark or derives one from records, includes definition-change marks, and marks periods as comparable or not (`backend/api/measurement_breaks.py`) instead of letting a chart draw one line across them.
- **Partial periods are marked.** A period still in progress is served with a partial flag, for fresh tokens and, on the Days view, for outcomes.
- **Token counts are lower bounds when records lack counts.** The series includes how many calls had no token count. A period where no call had one has no figure; it is never served as zero tokens.

### Cost

Cost is computed at read time. The token ledger is priced at each provider's published API prices (`backend/llm/api_equivalent.py`, `backend/api/measurement_cost.py`). That is an API-equivalent figure, not what was charged. It is a subtotal of the usage records that could be priced, reported with the count of those that could not and why: no maintained list price for the recorded model, no published rate for cached tokens, an unproven context tier, unreported or inconsistent token counts, or an unproven counting format. Actual spend is recorded separately and its completeness is reported with the figure. A series for dollars over time is declared in the registry as unavailable.

### Presence

Time is estimated from the person's presence in the app, recorded as visits, merged and closed on events such as navigating away, hiding the page or going idle (`backend/infra/presence.py`). Measurement uses focused spans when recorded, counts older visits without focus data in full, and shares overlapping time among active threads (`backend/api/measurement_presence.py`). A visit with no thread belongs to the project or all work, never to an agent.

### Your own measures and events

An agent can declare facts about its own work in `measures.yaml`, record them, react to events with rules, and read a KPI back (`backend/infra/measure_registry.py`, `backend/core/agent_kpi_formula.py`). Measurement's API returns an agent's KPIs (`/api/measurement/agent-kpis`, `backend/api/measurement_agent_kpis.py`); the Measurement page doesn't show them yet.

- **Events** come from connected mail, GitHub, CORSIA itself and agents' scripts, not from calendar or chat (`backend/events/intake.py`).
- **Who is woken.** The agent that owns what an event is about is woken when the event needs action, and gets a digest line otherwise (`backend/events/disposition.py`). A rule can wake only the agent that wrote it or one that agent started, and matches declared fields only, so it can't read mail text (`backend/events/rules.py`).
- **KPI weeks** run on UTC; an agent reads the current and the last complete week (`backend/tools/measures.py`).
- **Review lines.** A KPI with a `review` line and `alert: wake` or `alert: digest` is checked nightly. Once a week has settled (its end plus the KPI's settle days), a value worse than the line wakes the agent, or adds a line to its digest, once for that week; the project's report lists the move for the conductor (`backend/jobs/kpi_thresholds.py`).

## What can go wrong

- **Little data looks like no data.** A new install or a scope with few outcomes can show empty ratio charts because of the minimum-sample rule. Count series need not have the same threshold.
- **Only eligible plan work counts as outcomes.** Work done outside a plan task is not an outcome. Other records, such as token usage, can still be measured.
- **Proxies are estimates.** Active hours, asks per outcome and size-based hours are approximations and are marked so. Do not read a small difference as a result.
- **Your own measures are the agent's word.** They are labeled Self-reported unless something vouches for them, and recording starts when the agent first records. A rule on the wrong event type or field fires on nothing and raises no error. The Measurement page doesn't show them yet, and a review line wakes the agent, not you.
- **First-try rate depends on recorded work.** A send-back, certain failed or changed-requirement attempts, or a failed predecessor makes an outcome not first-try. It cannot see a rejection that happened in conversation and was never recorded on the plan.
- **API-equivalent is not the bill.** Subscriptions and plan limits change what a person actually pays.
- **Method changes break trends.** After a break, compare only within one side.
- **No model-quality claim.** The series describe how work flowed through the platform. They are not a benchmark of any model.

## Where it lives

| Part | Path |
|---|---|
| Metric registry and rules | `backend/api/measurement_registry.py` |
| Read-only API | `backend/api/measurement.py` |
| Source queries | `backend/api/measurement_queries.py`, `backend/api/measurement_prepared.py` |
| Scope handling | `backend/api/measurement_scope.py` |
| Presence time | `backend/api/measurement_presence.py`, `backend/infra/presence.py` |
| Cost | `backend/api/measurement_cost.py`, `backend/llm/api_equivalent.py` |
| Size series | `backend/api/measurement_size.py` |
| Comparability breaks | `backend/api/measurement_breaks.py` |
| In-app page and reading guide | [Measurement](../measurement.md) |
| Declared measures, events and KPIs | `backend/infra/measure_registry.py`, `backend/events/intake.py`, `backend/events/rules.py`, `backend/core/agent_kpi_formula.py` |
