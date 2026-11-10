import { describe, expect, it } from 'vitest';
import {
	addressesOnAnsweredEmail, badEntriesIn, emailDraftContext, isEmailDraftPath, newerEmails, newerEmailsLine, notFullAddressLine, looksLikeEmailDraft, previewLine, recipientEntries, repliedToId, senderAddress, senderName,
} from '$lib/emailDraft';

const REPLY = [
	'From: Alex Rivera alex.rivera@example.com',
	'To: Dana Whitfield dana.whitfield@example.com',
	'Subject: Re: Saturday work day',
	'',
	'Hi Dana,',
].join('\n');

// A draft saved before the line was dropped carries it and must still open as an email draft.
const OLD_REPLY = REPLY.replace('\n\n', '\nIn reply to: "Saturday work day" from Dana Whitfield dana.whitfield@example.com\n\n');

const NEW_EMAIL = ['From: Alex', 'To: Marcus', 'Subject: Brush crew', '', 'Hi Marcus,'].join('\n');

describe('looksLikeEmailDraft', () => {
	it('accepts the header block compose_document writes', () => {
		expect(looksLikeEmailDraft(REPLY)).toBe(true);
		expect(looksLikeEmailDraft(NEW_EMAIL)).toBe(true);
	});

	it('still accepts a draft saved with the old In reply to line', () => {
		expect(looksLikeEmailDraft(OLD_REPLY)).toBe(true);
	});

	it('accepts CRLF line endings', () => {
		expect(looksLikeEmailDraft(NEW_EMAIL.replace(/\n/g, '\r\n'))).toBe(true);
	});

	it('rejects ordinary documents', () => {
		expect(looksLikeEmailDraft('# Notes\n\nTo do: Subject: later')).toBe(false);
		expect(looksLikeEmailDraft('')).toBe(false);
	});

	it('needs To and Subject and at least three header lines', () => {
		expect(looksLikeEmailDraft('From: a\nTo: b\n\nbody')).toBe(false);
		expect(looksLikeEmailDraft('From: a\nCc: b\nNote: c\n\nbody')).toBe(false);
	});

	it('rejects a block where any line is not a header', () => {
		expect(looksLikeEmailDraft('From: a\nTo: b\nSubject: c\nrandom line\n\nbody')).toBe(false);
	});
});

describe('isEmailDraftPath', () => {
	it('is true for a file directly inside an email-drafts folder', () => {
		expect(isEmailDraftPath('email-drafts/email-1.md')).toBe(true);
		expect(isEmailDraftPath('/work/thread/email-drafts/email-1.md')).toBe(true);
	});

	it('is false elsewhere, including a deeper folder or a lookalike name', () => {
		expect(isEmailDraftPath('notes/email-1.md')).toBe(false);
		expect(isEmailDraftPath('email-drafts/old/email-1.md')).toBe(false);
		expect(isEmailDraftPath('my-email-drafts/email-1.md')).toBe(false);
		expect(isEmailDraftPath('email-drafts')).toBe(false);
		expect(isEmailDraftPath(null)).toBe(false);
	});
});

describe('emailDraftContext', () => {
	it('is null with no fixture named, so no document changes', () => {
		expect(emailDraftContext(REPLY, null)).toBeNull();
	});

	it('is null for an unknown fixture and for a non-email document', () => {
		expect(emailDraftContext(REPLY, 'nope')).toBeNull();
		expect(emailDraftContext('# Notes', 'reply4')).toBeNull();
	});

	it('returns the conversation newest first for a reply', () => {
		const context = emailDraftContext(REPLY, 'reply4');
		expect(context?.conversation).toHaveLength(4);
		const dates = context!.conversation.map((m) => Date.parse(m.date));
		expect(dates).toEqual([...dates].sort((a, b) => b - a));
	});

	it('keeps the conversation of a draft whatever its header says, and its notes with their kind', () => {
		expect(emailDraftContext(NEW_EMAIL, 'new0')?.conversation).toEqual([]);
		// A bad address is named from the entries in To and Cc, not from a note.
		expect(emailDraftContext(NEW_EMAIL, 'badto')?.notes).toEqual([]);
		expect(emailDraftContext(NEW_EMAIL, 'badto')?.badRecipients).toEqual(['priya.raman']);
		expect(emailDraftContext(NEW_EMAIL, 'warn4')?.notes.some((n) => n.kind === 'quiet')).toBe(true);
		expect(emailDraftContext(NEW_EMAIL, 'warn4')?.notes.every((n) => n.kind === 'quiet')).toBe(true);
		expect(emailDraftContext(NEW_EMAIL, 'quiet4')?.notes.every((n) => n.kind === 'quiet')).toBe(true);
	});

	it('carries the attachments and comments a fixture names', () => {
		expect(emailDraftContext(REPLY, 'attach')?.attachments?.map((a) => a.type)).toEqual(['image', 'document']);
		expect(emailDraftContext(REPLY, 'comment4')?.comments?.[0].messageId).toBe('f4-1');
	});

	it('a new email has nothing to read: the draft record names no reply, so the fixture has no conversation', () => {
		expect(emailDraftContext(NEW_EMAIL, 'new0')).toMatchObject({ status: 'ready', conversation: [] });
	});

	it('reports a failed read, and the conversation once the person retries', () => {
		expect(emailDraftContext(REPLY, 'error')).toMatchObject({ status: 'error', conversation: [] });
		expect(emailDraftContext(REPLY, 'error', 1)).toMatchObject({ status: 'ready' });
		expect(emailDraftContext(REPLY, 'error', 1)?.conversation).toHaveLength(4);
	});

	it('a partial read keeps the emails it read and counts the rest', () => {
		expect(emailDraftContext(REPLY, 'partial')).toMatchObject({ status: 'partial', unreadable: 3 });
		expect(emailDraftContext(REPLY, 'partial')?.conversation).toHaveLength(2);
	});
});

describe('sender and preview text', () => {
	it('splits a sender into name and address', () => {
		expect(senderName('Dana Whitfield <dana@example.com>')).toBe('Dana Whitfield');
		expect(senderAddress('Dana Whitfield <dana@example.com>')).toBe('dana@example.com');
	});

	it('handles a bare address and a bare name', () => {
		expect(senderName('dana@example.com')).toBe('dana@example.com');
		expect(senderAddress('dana@example.com')).toBe('dana@example.com');
		expect(senderAddress('Dana')).toBe('');
	});

	it('previewLine puts the body on one line', () => {
		expect(previewLine('Hi,\n\n  one   two\nthree ')).toBe('Hi, one two three');
	});
});

describe('newer emails and the answered email', () => {
	const MARCUS = { id: 'a', sender: 'Marcus Obi <marcus.obi@example.com>', date: '2026-11-05T17:40:00Z', body: 'x' };
	const DANA = { id: 'b', sender: 'Dana Whitfield <dana.whitfield@example.com>', date: '2026-11-06T18:05:00Z', body: 'y' };

	it('says one newer email with its sender and day, and several with a count and the distinct senders', () => {
		expect(newerEmailsLine(undefined)).toBeNull();
		expect(newerEmailsLine([])).toBeNull();
		expect(newerEmailsLine([MARCUS])).toBe('Marcus Obi wrote on Nov 5, after the email you’re replying to.');
		expect(newerEmailsLine([DANA, MARCUS])).toBe('2 emails came after the one you’re replying to: Dana Whitfield, Marcus Obi.');
		expect(newerEmailsLine([DANA, MARCUS, DANA])).toBe('3 emails came after the one you’re replying to: Dana Whitfield, Marcus Obi.');
	});

	it('reads the newer emails from the conversation: the ones above the email being replied to, newest first', () => {
		const context = emailDraftContext(REPLY, 'newer2')!;
		expect(context.conversation.slice(0, 3).map((message) => message.id)).toEqual(['n-2', 'n-1', 'f4-1']);
		expect(newerEmails(context).map((message) => message.id)).toEqual(['n-2', 'n-1']);
		expect(repliedToId(context)).toBe('f4-1');
		expect(newerEmails(emailDraftContext(REPLY, 'reply4')!)).toEqual([]);
		expect(repliedToId(emailDraftContext(REPLY, 'reply4')!)).toBe('f4-1');
		expect(newerEmails(emailDraftContext(REPLY, 'newer6')!)).toHaveLength(6);
		expect(repliedToId({ conversation: [] })).toBeNull();
	});

	it('compares To and Cc with the sender and other recipients of the answered email, and with nothing for a new email', () => {
		const context = emailDraftContext(REPLY, 'newer2')!;
		// The sender of the answered email (Dana's), not of the newer emails above it.
		expect(addressesOnAnsweredEmail(context)).toEqual(['dana.whitfield@example.com']);
		expect(addressesOnAnsweredEmail({ conversation: context.conversation, repliedTo: 'f4-1', answered: ['Priya@example.com'] })).toEqual(['dana.whitfield@example.com', 'priya@example.com']);
		expect(addressesOnAnsweredEmail({ conversation: [] })).toBeNull();
	});
});

describe('a bad address', () => {
	const DRAFT = ['From: Alex', 'To: Dana Whitfield <dana@example.com>, priya.raman, dana@', 'Cc: ', 'Subject: S', '', 'To: not a header'].join('\n');

	it('reads the entries of To and Cc as typed, stopping at the first blank line', () => {
		expect(recipientEntries(DRAFT)).toEqual(['Dana Whitfield <dana@example.com>', 'priya.raman', 'dana@']);
		expect(recipientEntries('To: a@b.co\r\nCc: c@d.co , e@f.co\r\n\r\nTo: x')).toEqual(['a@b.co', 'c@d.co', 'e@f.co']);
	});

	it('names one entry as "<entry> isn’t a full email address." and several with a count', () => {
		expect(notFullAddressLine([])).toBeNull();
		expect(notFullAddressLine(['priya.raman'])).toBe('priya.raman isn’t a full email address.');
		expect(notFullAddressLine(['priya.raman', 'dana@'])).toBe('2 entries aren’t full email addresses: priya.raman, dana@');
	});

	it('names only the safeguard’s entries that are still in To or Cc', () => {
		expect(badEntriesIn(DRAFT, ['priya.raman', 'dana@'])).toEqual(['priya.raman', 'dana@']);
		expect(badEntriesIn(DRAFT.replace('priya.raman', 'priya.raman@example.com'), ['priya.raman', 'dana@'])).toEqual(['dana@']);
		expect(badEntriesIn(DRAFT, undefined)).toEqual([]);
		expect(emailDraftContext(REPLY, 'badto2')?.badRecipients).toEqual(['priya.raman', 'dana@']);
	});
});

describe('the quiet notes', () => {
	it('never carry a count of recent emails from a sender, and an added recipient or newer emails are not notes', () => {
		for (const name of ['warn4', 'loud3', 'quiet4', 'attach', 'badto']) {
			const notes = emailDraftContext(REPLY, name)!.notes.map((note) => note.text).join(' ');
			expect(notes).not.toMatch(/in the last 30 days/);
		}
		expect(emailDraftContext(REPLY, 'quiet4')!.notes.every((note) => note.kind === 'quiet')).toBe(true);
	});
});
