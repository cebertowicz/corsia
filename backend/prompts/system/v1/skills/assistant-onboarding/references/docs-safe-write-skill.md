---
name: docs-safe-write
description: Use before this Assistant writes to a Google Doc. Read its current revision and send every change with writeControl.requiredRevisionId; on a refused revision, re-read and re-apply safely or ask. Not for blind overwrites or writes based on a stale read.
corsia:
  handler:
    kind: instruction
---

# Docs safe write

1. Source `.corsia/skills/assistant-onboarding/assets/gws-env.sh` before every `gws` call.
2. Read the target document and retain its `revisionId`:

```bash
gws docs documents get --params '{"documentId":"<document-id>"}' --format json
```

3. Build the smallest intended `requests` list from that exact content. Write with the revision you read:

```bash
gws docs documents batchUpdate --params '{"documentId":"<document-id>"}' --json '{"requests":[<requests>],"writeControl":{"requiredRevisionId":"<revision-id>"}}' --format json
```

4. If Google refuses the write because the revision changed, do not retry the old payload. Re-read the document, re-apply the intended change to the latest content only when it remains unambiguous, and retry with that fresh revision. If the change cannot be re-applied safely, show the conflict and ask the user.

Never omit `writeControl.requiredRevisionId`, overwrite a document based on a stale read, or claim a refused write succeeded.
