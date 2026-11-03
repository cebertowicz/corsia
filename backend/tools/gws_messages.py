"""What agents read from ``google_workspace``: its tool text and its fixed results."""

from __future__ import annotations

DESCRIPTION = (
    'Run one Google Workspace CLI (gws) command on a connected Google account: '
    'Docs, Sheets, Drive and Calendar. Pass `account` (its email address; '
    'needed when more than one is connected), the words after `gws` as `args` '
    '(for example `["docs","documents","create"]`), URL parameters as `params` '
    'and the request body as `body`. '
    'Run `["schema","drive.files.list"]` (the method\'s name, dotted) to see its fields. '
    "`upload` and `output` take files in your workspace. Returns gws's JSON; "
    'a list returns one page, so ask for the next. Keep results small: '
    'pass `fields` and `pageSize` or a cell range, '
    'and read a long Doc by exporting it as `text/plain` to `output`. '
    'Read just before you change anything, and change only what was asked. Docs: '
    'read with `suggestionsViewMode: SUGGESTIONS_INLINE`; '
    "to delete or replace anything, send that read's `revisionId` as `writeControl.requiredRevisionId`. "
    "If Google says it's stale, someone edited: read again and tell the person what changed. "
    'Where others are editing, use `writeMode: SUGGEST`. Calendar: '
    'use `patch` and send the whole `attendees` list; '
    'guests are emailed only with `sendUpdates: all`. '
    "Your edits appear as the person's own. Email is not available here: "
    'use the email tools. Deleting a Drive file moves it to the trash. '
    'A deleted event is saved first in `google-deletions/<account>/`; '
    'to bring it back, recreate it from there (ask before re-inviting its guests). '
    "CORSIA signs in for you; you never handle the account's sign-in."
)

MAIL = 'Not available here: email goes through the email tools.'
NO_PREVIEW = "This command can't be checked before it runs, so it isn't available here."
PATHS = 'upload and output must be files in your workspace.'
TRASH = "Emptying the trash isn't available here; Google empties it after 30 days."
CANT_UNDO = "This delete can't be undone, so it isn't available here."
DOCS_GUARD = (
    'Read the document just before, then send its revisionId as '
    'writeControl.requiredRevisionId to delete or replace anything.'
)
ACCOUNT = 'Name the account: one of {addresses}.'

# Results of a delete made recoverable (tools.gws_deletions) and of running gws itself.
TRASHED = "Moved to the trash, not deleted: Google keeps it for 30 days and the person can restore it."
SAVED_FIRST = "Done. What was deleted was saved first in {where}."
NOT_SAVED = "Not deleted: it couldn't be saved first. {detail}"
NOT_FOUND = "Google Workspace isn't available right now: CORSIA couldn't find its gws program."
BAD_ARGS = 'Error: args must be the words after gws, for example ["drive","files","list"].'
FAILED = "Error: Couldn't run the Google Workspace command. Try again later."
