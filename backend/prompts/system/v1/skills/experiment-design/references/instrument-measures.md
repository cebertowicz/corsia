# Measuring and reacting to your own work

For any agent that needs to count something its work does, be woken or act when something happens, or time and track it: communications, research, operations or software. Not for product analytics or another agent's measures.

## 1. Pick the path

Tell the person first what they will get, in these terms, then take the path.

| They ask | Path | Today |
|---|---|---|
| "Count or track how often I do X" | Record (section 3) | Works |
| "Tell me when X happens" | A rule that wakes you (section 4), once section 2 shows something produces X | Works where something produces X |
| "...and how often" | Declare X `use: both`, add the rule and a KPI | Works |
| "How long between A and B" | `since` on B (section 5) | Works |
| "Where does each thing stand now" | A KPI operand with `latest` (section 5) | Works |
| "Run this when X happens" | A rule with `action: run_script` | Works |
| "Tell me if the number goes over N" | Not built. Offer: your own scheduled check prints an event when it crosses N, and a rule wakes you | Partly |
| "Show me a chart" | Not built. You can read a KPI's value with `measure_status` and `kpis: true`; the Measurement page shows no declared KPI | No |

## 2. Does something produce the event?

A rule only fires on events that arrive. `event_rules` `types` lists every connector's type names and fields, whether or not anything feeds them, so it cannot tell you. What arrives today:
- **Email** from a connected mailbox: `email.received`, on the connector for that mailbox (`gmail`, `microsoft` or `mailhost`; `types` names it). New mail only, from when the mailbox connected, and only for mailboxes your chat may use. Mail screened as needing a person now wakes the file's owner; other mail is a digest line. A rule of yours can match `from`, `to`, `label`, `thread_id`, `message_id` and `author_origin`, not the subject or body.
- **GitHub**: pull requests, issues, checks, labels and comments.
- **CORSIA itself**: plan blocks and review requests, document comments, worker results, measurement digests, library changes.
- **Yours**: an event you declare and `emit`, or print from a scheduled script as `{"event": {"type": "<declared name>", "facts": {...}}}`. A program on this machine can use `corsia event emit`.

Nothing feeds a calendar, chat or any other service. If the happening lives there, say so; a scheduled script can poll it and print the event.

## 3. Record

1. Choose what identifies one occurrence, which occurrences count (every one, so a share has its full denominator), and every value each attribute can take (a value missing from an `enum` is refused).
2. Put `measures.yaml` in `.threads/<your thread id>/` under your scope's folder (a workflow's goes beside its `SKILL.md` and names `workflow`). Declare your own namespace, one nobody holds and no connector uses.

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
   An example, not a pre-existing event: use your own names, and a `min_n` that fits the question. `use: record` stores only; `route` only routes; `both` does each. A routed event cannot declare a `login` attribute. `formula` is `count`, `ratio`, `share`, `percentile` or `rate_per_run` (workflows only); there is no mean, so for an average use the median (`percentile`, `p: 50`).
3. `measure_sync` with `path` relative to the workspace root. Fix what it refuses and sync again. Nothing takes effect until it succeeds.
4. Record each occurrence: `event_rules` `emit` with the full declared name (`backup.checked`) and `facts`, or print `{"event": {"type": "backup.checked", "facts": {...}}}` from a script registered with `automation_schedule` (`kind: script`, interval at least 300 seconds); a `record` event is a script's `{"measure": {"event.name": "...", "attributes": {...}}}`. A run's lines are capped at 200.
5. `measure_status` (add `kpis: true` for each KPI's current and last-week value). Each refusal says whether the row or only the attribute was dropped, and the fix. Recording starts when you first record; earlier occurrences are not counted.

## 4. Wake or act

A rule delivers to you, or with `to: registered_owner` to the file's owner (woken only if that is you or your temporary worker, otherwise a digest line); a conductor can also set up a digest for another agent. You cannot wake another agent. Declared events are named by the short name in a rule: `connector: backup`, `event_type: checked`.

```
event_rules create  connector: backup  event_type: checked
  match: [{field: result, op: eq, value: failed}]
  action: deliver  mode: wake  note: "Backup failed. Check it and tell the owner."
```

- Tests are `eq`, `not_eq`, `in`, `prefix` and `present`, at most 4, on declared id, count, enum or login fields. Not on a `num` attribute, and not on a `record` event.
- Without `mode: wake` the match is only a digest line.
- To run something instead, `action: run_script` with a script registered without a cadence; it gets the event on stdin.
- Prove it: `event_rules test` with a sample `facts` shows which rules match; after a real event, `explain` shows why it did or didn't reach you, or, given an `event_type`, its recent refusals.

## 5. Duration and latest

A time between two events: declare the end event with `since: {event: <start>, on: <shared id attribute>, as: <a num attribute>}`. Intake stamps whole minutes from the newest earlier start, or leaves it empty; a later start never fills it in. Read it with a `percentile` KPI: `formula: percentile`, `num: {event: <end event>, attr: <the as attribute>, p: 50}` for the median. For the current state of each thing, give a KPI operand `latest: [<id attribute>]`: it keeps the newest row per value.

## 6. Not built yet

Say so plainly rather than promise: a chart or any page showing a declared KPI; an alert when a KPI crosses a line; OpenTelemetry intake; a feed from a calendar, chat or other service; a wake for another agent from your rule. Everything in this reference is what the current build does.
