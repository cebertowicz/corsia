# Measuring and reacting to your own work

For an agent that needs to count, time or track something its work does, or be woken or act when something happens. Not for product analytics or another agent's measures.

## 1. Pick the path

| They ask | Path |
|---|---|
| Count or track X | Record (3). `measure_status` gives counts for today and the last 7 days, and with `kpis: true` a KPI's value for this week and the last complete week. There is no monthly history |
| Tell me when X happens | A rule that wakes you (4), once 2 shows something produces X. Ask what it waits for: a sender address, or a label their mail already carries |
| ...and how often | `use: both`: the rule plus a KPI |
| Time between A and B; where each thing stands | `since`; `latest` (5) |
| Run this when X happens | A rule with `action: run_script` |
| Wake me once when this task's wait ends | A plan watch (skill `plans-and-workers`), not a rule |
| Tell me if a number goes over N | Not built. Offer a scheduled check of yours that prints an event when it crosses N, with a rule that wakes you |
| Show me a chart | Not built. No page shows a declared KPI; read its value with `measure_status` |

OpenTelemetry intake is not built either. Use only fields this reference shows: sync also accepts a field it doesn't know (an `alert`, say), and nothing acts on it.

## 2. Does something produce the event?

A rule fires only on events that arrive; `event_rules` `types` lists types even if nothing feeds them. Today:
- **Email** from a connected mailbox your chat may use: `email.received` on the connector `types` names. New mail only. Mail screened as needing a person now wakes its owner agent; other mail is a digest line. A rule can match `from`, `to`, `label`, `thread_id`, `message_id`, `author_origin`, not the subject or body. On mail `author_origin` is always `outside`, so it narrows nothing; match `from`, `to` or `label`.
- **GitHub** pull requests, issues, checks, labels, comments; **CORSIA** plan blocks, review requests, document comments, worker results.
- **Yours**: an event you declare and `emit`, or print from a scheduled script as `{"event": {"type": "<declared name>", "facts": {...}}}`. With a shell, `corsia event emit` does the same.

Nothing feeds a calendar, chat or other service. A scheduled script can poll one and print the event.

## 3. Record

1. Choose what identifies one occurrence, count every occurrence (a share needs its full denominator), and list every value each attribute can take (a value missing from an `enum` is refused).
2. Put `measures.yaml` in `.threads/<your thread id>/` under your scope's folder (a workflow's goes beside its `SKILL.md` and names `workflow`). Declare a namespace nobody holds and no connector uses.

   ```yaml
   schema: corsia.measures/1
   namespace: backup
   events:
     backup.checked:
       use: both
       attributes:
         run_ref: id
         result: {enum: [ok, failed]}
       dedupe: [run_ref]
   kpis:
     failed_share:
       title: Nightly backups that failed
       unit: percent
       better: lower
       formula: share
       num: {event: backup.checked, attr: result, values: [failed]}
       min_n: 7
   ```
   Use your own names and `min_n`. `use` is `record`, `route` or `both`; a routed event can't declare `login`. `formula` is `count`, `ratio` (needs `den`), `share` (`num.attr`, `num.values`), `percentile` (`num.attr`, `num.p`) or `rate_per_run` (workflows only); there is no mean, so use `percentile`, `p: 50`.
3. `measure_sync` with `path` relative to the workspace root. Fix what it refuses and sync again; nothing takes effect before it succeeds.
4. Record each occurrence: `event_rules` `emit` with the full name (`backup.checked`) and `facts`, or print the event line from a script registered with `automation_schedule` (`kind: script`, `cadence_seconds` at least 300; a `record` event prints `{"measure": {"event.name": "...", "attributes": {...}}}`).
5. `measure_status`. Each refusal says whether the row or only the attribute was dropped, and the fix. A KPI that reads "not tracked" gives its reason. Recording starts when you first record; earlier occurrences are not counted.

## 4. Wake or act

A rule delivers to you, or with `to: registered_owner` to the agent that registered the event type; that agent is woken only if it wrote the rule or is a temporary worker started by its writer, otherwise it gets a digest line. You cannot wake another agent. A declared event is named by its short name: `connector: backup`, `event_type: checked`.

```
event_rules create  connector: backup  event_type: checked
  match: [{field: result, op: eq, value: failed}]
  action: deliver  mode: wake  note: "Backup failed. Check it and tell the owner."
```

- Tests are `eq`, `not_eq`, `in`, `prefix` (case-sensitive) and `present`, at most 4, on declared id, count, enum or login fields; not a `num` attribute or a `record` event.
- `mode` defaults to `wake`; `mode: digest` makes the match a digest line instead. `action: run_script` runs a script registered without a cadence, giving it the event on stdin.
- Prove it: `event_rules test` with sample `facts`; `explain` after a real event, or with an `event_type` for its recent refusals. A rule acts only while its writer is available: it holds while the writer is archived, and turns off without a message once the writer is deleted or leaves the rule's project. Write a rule from the agent that should keep receiving it; `list` (with when each rule last acted) and `explain` show why one went quiet.

## 5. Duration and latest

Declare the end event with `since: {event: <start>, on: <shared id attribute>, as: <a num attribute>}`. Intake stamps whole minutes from the newest earlier start, or leaves it empty. Read the median with a `percentile` KPI on `as`. For each thing's current state, give a KPI operand `latest: [<id attribute>]`: it keeps the newest row per value.
