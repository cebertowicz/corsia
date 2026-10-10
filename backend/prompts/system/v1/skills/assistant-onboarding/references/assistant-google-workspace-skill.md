---
name: assistant-google-workspace
description: Use for this Assistant's Gmail, Calendar, Drive, and Docs work after Google Workspace is connected. Prepare the environment before every gws call; make document drafts for replies and send only on the user's explicit word. Not for reading credentials, showing client configuration, or silently sending, sharing, accepting, or overwriting.
corsia:
  handler:
    kind: instruction
---

# Assistant Google Workspace

Before every `gws` command, source the shipped helper. It sets the TLS, writable config, writable temp, and file-keyring environment without printing client values:

```bash
source .corsia/skills/assistant-onboarding/assets/gws-env.sh
```

When the headed browser has downloaded the user's Desktop OAuth client JSON, transfer it without opening, printing, or pasting it:

```bash
source .corsia/skills/assistant-onboarding/assets/gws-env.sh --client-json "<downloaded-client-json-path>"
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/documents
```

The helper moves that JSON into its private writable configuration directory with mode 600 and exports its values only in the current shell. Never read its contents, include it in an instruction/document/plan, print shell variables, or use command tracing. The browser can complete Cloud Console creation where Google permits it; if its download cannot be captured automatically, the only extra user action is clicking **Download** for the Desktop client JSON. The user then completes the one Google sign-in and Allow action in their own browser.

## Read operations

Use a small read to validate each service and to gather work:

```bash
gws gmail users messages list --params '{"userId":"me","maxResults":10}' --format json
gws calendar events list --params '{"calendarId":"primary","maxResults":10,"singleEvents":true,"orderBy":"startTime"}' --format json
gws drive files list --params '{"pageSize":10,"q":"trashed=false"}' --format json
gws docs documents get --params '{"documentId":"<document-id>"}' --format json
```

If a service call fails, name that service and one corrective action, then re-check only that service. Never hide a partial connection behind a success claim.

## Replies and drafts

Write every reply as an editable CORSIA document containing recipients, subject, and body. A draft is not a plan decision and is not approval. On the user's explicit `send` or `send it` instruction, re-read that document and write its **current** body to a temporary file. For an existing thread, invoke the shipped reply guard exactly:

```bash
python3 .corsia/skills/assistant-onboarding/assets/gws-reply-guard.py --message-id "<message-id>" --body-file "<current-document-text-file>" --send
```

Never hand-assemble a reply recipient, MIME headers, quoting, or thread id. The guard derives the recipient from the original message, previews before sending, stops on duplicate/stale/parallel-thread risk, and sends only with `--send`. Surface a guard refusal with its next action; do not bypass it with direct Gmail send. A genuinely new message still requires the user's explicit send instruction and a separately safe send path.
