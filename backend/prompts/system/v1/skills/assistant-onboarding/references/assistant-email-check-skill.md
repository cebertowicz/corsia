---
name: assistant-email-check
description: Create and operate this Assistant's bounded email-check automation. Use on the first turn, every direct conductor kickoff, or whenever its schedule is reported missing. Not for conductor-owned schedules, confirmation relays, or new polling loops.
corsia:
  handler:
    kind: instruction
---

# Assistant email check

Once your `assistant-google-setup` skill has finished on your first turn (the conductor's kickoff says `Run your Google setup now, then create or reuse your scheduled email check and confirm its schedule id to the user.`), on every later such kickoff, on a cadence wake with no saved schedule id, and whenever the schedule is reported missing, call `automation_list` before scheduling. Treat the missing id as a reported-missing schedule. Filter its schedules to active entries with payload `{ "kind": "assistant.scheduled_email_check" }`.

- If exactly one matches, save its id; do not create another.
- If none match, create your own `automation_schedule` with `kind: "cadence"`, `spec: "3600"`, `max_runs: 1000`, and payload `{ "kind": "assistant.scheduled_email_check" }`. Save the returned schedule id.
- If more than one matches, report their ids as a configuration error to the user with `post_update`; do not create or cancel a schedule.
- If listing or scheduling is refused, report the exact refusal to the user with `post_update`.

After an exactly-one or none-match result, call `post_update(text="Scheduled email check confirmed: <schedule_id>.")` to confirm the saved schedule id to the user. Do not wait for or send a confirmation through the conductor.

Save the person's answer to `How often should I check your email?` (your responsibility file asks it right after Google setup; until it is answered, use A). A: check on every weekday wake from 08:00 to 18:00 local time. B: check on the first wake at or after 08:00 and at or after 13:00 each day. C: check once, on the first wake at or after 08:00 each day, and send it as a morning digest. The schedule stays hourly; the saved answer decides which wakes check. Before every Google call or update, enforce it. Outside it, make no service call or update; the wake is still model-visible. Never claim guard wakes cost zero model turns or tokens; report guard-wake and real-check model/token cost separately. Do not add a loop or poll.
