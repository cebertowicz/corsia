# Recording your own measures

For any agent that needs to record or inspect measures of its own work: communications, research, operations or software. Not for general product analytics or another agent's measures.

1. Tell the person first what they will get: facts about your work that you can confirm arrived (step 5), and a KPI's trend only where `measure_status` or the Measurement page shows it. Then choose an outcome that answers their question and the facts you can observe. A measure is a recorded fact; a KPI (key performance indicator) summarizes facts over time. Decide what identifies one occurrence, which occurrences count (every one, not only successes, so a share has its full denominator), every value each attribute can take (a value missing from an `enum` is refused), and whether the occurrence also needs to route as an event. Use existing records where they answer the question.
2. Put your agent-level declaration in your own thread folder as `measures.yaml`: `.threads/<your thread id>/measures.yaml` under your scope's folder, which for a project agent is `projects/<project>/`. For a workflow you own, its declaration goes beside that workflow's `SKILL.md` and includes `workflow` set to the workflow's name. The following is an example declaration, not a pre-existing event to emit. Replace its namespace, event, fields and values with names and facts for your own work; use a namespace you own or an unused one.

   ```yaml
   schema: corsia.measures/1
   namespace: mail
   events:
     mail.triaged:
       use: both
       attributes:
         message_ref: id
         action: {enum: [replied, archived]}
       dedupe: [message_ref]
   kpis:
     replied_share:
       title: Triaged mail answered
       unit: percent
       better: higher
       formula: share
       num: {event: mail.triaged, attr: action, values: [replied]}
       min_n: 20
   ```

   The example's `min_n` is an example, not a prescribed threshold. `dedupe` identifies the same occurrence across retries. Choose a sample threshold that fits the question. A `both` declaration routes and records; `record` only records. A routed event cannot declare a login attribute.
3. Call `measure_sync` with `path` relative to the CORSIA workspace root, not your project folder: a project agent passes `projects/<project>/.threads/<your thread id>/measures.yaml`. Read the result and fix any validation or ownership errors; repeat after each change. A declaration does not take effect until sync succeeds.
4. For an occurrence declared `both`, call `event_rules` with `operation` set to `emit`, `event_type` set to its full declared name, and `facts` set to its declared attributes. Supply `id` when retrying the same event; check the receipt or refusal. For a record-only event, a script can print a measure line shaped as `{"measure":{"event.name":"<declared event>","attributes":{<declared attributes>}}}`. Register the script with `automation_schedule`, setting `kind` to `script`, `script_path` to the file and `cadence_seconds` to the interval in seconds (at least 300); for a workflow's measures, also set `workflow` to the workflow's name, or its rows carry no workflow or version. A scheduled run submits its output; printing a line in an ordinary shell does not.
5. Call `measure_status` after recording. It shows recorded counts, drops, refusals and active declaration files for today and the last seven days. Each refusal says whether the row or only the attribute was dropped, and the fix: apply it, sync again, and record that occurrence again from your own record of what happened if its row was dropped; if only the attribute was dropped, the row stands without it. Recording starts when you first record: earlier occurrences are not counted unless you record them yourself. When asked for a KPI's trend and neither `measure_status` nor the Measurement page shows its values, say so rather than estimating it.
