# Gmail reply guardrail specification

`assets/gws-reply-guard.py` is the only path for replying to an existing Gmail thread. It rejects unsafe sends before any `gws gmail +reply --send` call.

| Guard | Rule | Resolution |
| --- | --- | --- |
| G1 | Target message must be readable. | Choose a readable target. |
| G2 | Reply recipient comes only from original `Reply-To` then `From`; no-reply addresses stop the send. | Choose a valid target. |
| G3 | Reply body cannot be empty. | Update the draft document. |
| G4 | A later self-authored message in the thread stops a duplicate send. | Review the prior reply. |
| G5 | Newer messages warn; a newer message from the original sender stops a stale reply. | Re-target with `--pivot-to` or acknowledge after reading with `--confirm-stale-ok`. |
| G6 | A newer same-subject message in another thread stops a likely duplicate-channel reply. | Re-target or acknowledge after reading. |
| G8 | Every execution previews with `--dry-run`; only an explicit `--send` performs the send. | Confirm the current draft text. |

Exit codes: `0` preview or send completed; `1` tool/input error; `2` unacknowledged warning; `3` blocking guard. `--force` is a recorded escape hatch for an explicit user-directed exceptional case; it is never the normal flow.

The Assistant first writes recipients, subject, and body to an editable CORSIA document. On the user's explicit send word it re-reads that document, writes the current text to a temporary file, then invokes:

```bash
python3 .corsia/skills/assistant-onboarding/assets/gws-reply-guard.py --message-id "<message-id>" --body-file "<current-document-text-file>" --send
```

The guard never accepts a recipient argument, OAuth material, or an instruction to bypass a failed guard with direct Gmail send.
