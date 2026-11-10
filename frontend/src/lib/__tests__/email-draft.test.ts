import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	currentRecipients, headerLayout, newerEmails, newerEmailsLine, notFullAddressLine, previewLine, refusedEntries, repliedToId, senderAddress, senderName,
	type EmailMessage, type ServerHeader,
} from '$lib/emailDraft';
import { fetchEmailDraftContext } from '$lib/api/emailDrafts';

const message = (id: string, sender: string, date: string): EmailMessage => ({ id, sender, date, body: `${id} body` });
const MARCUS = message('n-1', 'Marcus Obi <marcus.obi@example.com>', '2026-11-05T17:40:00Z');
const DANA = message('n-2', 'Dana Whitfield <dana.whitfield@example.com>', '2026-11-06T18:05:00Z');
const ANSWERED = message('r-1', 'Dana Whitfield <dana.whitfield@example.com>', '2026-11-04T09:00:00Z');
const OLDER = message('o-1', 'Alex Rivera <alex@example.com>', '2026-11-01T09:00:00Z');

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

describe('newer emails', () => {
	it('says one newer email with its sender and day, and several with a count and the distinct senders', () => {
		expect(newerEmailsLine(undefined)).toBeNull();
		expect(newerEmailsLine([])).toBeNull();
		expect(newerEmailsLine([MARCUS])).toBe('Marcus Obi wrote on Nov 5, after the email you’re replying to.');
		expect(newerEmailsLine([DANA, MARCUS])).toBe('2 emails came after the one you’re replying to: Dana Whitfield, Marcus Obi.');
		expect(newerEmailsLine([DANA, MARCUS, DANA])).toBe('3 emails came after the one you’re replying to: Dana Whitfield, Marcus Obi.');
	});

	it('are the emails above the one being replied to in the newest-first conversation', () => {
		const context = { conversation: [DANA, MARCUS, ANSWERED, OLDER], repliedTo: 'r-1' };
		expect(newerEmails(context).map((email) => email.id)).toEqual(['n-2', 'n-1']);
		expect(repliedToId(context)).toBe('r-1');
		expect(newerEmails({ conversation: [ANSWERED, OLDER], repliedTo: 'r-1' })).toEqual([]);
	});

	it('are none, and nothing is marked replied to, when the answered email is not in the conversation or there is none', () => {
		expect(newerEmails({ conversation: [DANA, OLDER], repliedTo: 'r-1' })).toEqual([]);
		expect(repliedToId({ conversation: [DANA, OLDER], repliedTo: 'r-1' })).toBeNull();
		expect(newerEmails({ conversation: [DANA, OLDER] })).toEqual([]);
		expect(repliedToId({ conversation: [] })).toBeNull();
	});
});

describe('the header layout', () => {
	const DRAFT = ['From: Alex', 'To: dana@example.com, priya.raman', 'Cc: ', 'C', 'Subject: S', '', 'To: not a header'];

	it('lays out every line up to the first blank line, each with the field its label names and its range', () => {
		const { text, lines } = headerLayout(DRAFT);
		expect(text).toBe(DRAFT.slice(0, 5).join('\n'));
		expect(lines.map((line) => line.key)).toEqual(['from', 'to', 'cc', '', 'subject']);
		expect(lines[1]).toEqual({ key: 'to', from: 11, to: 44, valueFrom: 14 });
		// A line being typed is still a header line, with no label.
		expect(lines[3]).toEqual({ key: '', from: 50, to: 51, valueFrom: 50 });
	});

	it('uses the server’s recipients only while the header reads as the server read it', () => {
		const header: ServerHeader = { text: 'To: priya.raman', recipients: [{ field: 'to', text: 'priya.raman', from: 4, to: 15, full: false }] };
		expect(currentRecipients(header, 'To: priya.raman')).toBe(header.recipients);
		expect(currentRecipients(header, 'To: priya.raman@')).toBeNull();
		expect(currentRecipients(null, 'To: priya.raman')).toBeNull();
	});
});

describe('a bad address', () => {
	it('names one entry as "<entry> isn’t a full email address." and several with a count', () => {
		expect(notFullAddressLine([])).toBeNull();
		expect(notFullAddressLine(['priya.raman'])).toBe('priya.raman isn’t a full email address.');
		expect(notFullAddressLine(['priya.raman', 'dana@'])).toBe('2 entries aren’t full email addresses: priya.raman, dana@.');
	});

	it('follows the server’s reading; after an edit it hasn’t read, keeps the line only while a new reading will come', () => {
		const reading = [{ field: 'to' as const, text: 'priya.raman', full: false, from: 4, to: 15 }, { field: 'to' as const, text: 'sam@example.com', full: true, from: 17, to: 32 }];
		expect(refusedEntries([], reading, false)).toEqual(['priya.raman']);
		// Live editing: the next reading is on its way, so the line doesn't flicker.
		expect(refusedEntries(['priya.raman'], null, true)).toEqual(['priya.raman']);
		// A plain save brings no new reading, so a fixed address isn't left named.
		expect(refusedEntries(['priya.raman'], null, false)).toEqual([]);
	});
});

describe('reading a draft’s context', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('asks the draft’s route by artifact id and maps the server’s answer to the view’s names', async () => {
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({
			status: 'ready', reason: null, replied_to: 'r-1', answered: ['dana.whitfield@example.com'],
			conversation: [DANA, ANSWERED],
			attachments: [{ name: 'map.png', path: '/w/map.png', size: 10, type: 'image', unavailable: false }],
			notes: ['Same subject as an email from Oct 2.'],
		}), { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);
		const context = await fetchEmailDraftContext('art/1');
		expect((fetchMock.mock.calls[0] as unknown[])[0]).toBe('/api/email-drafts/art%2F1/context');
		expect(context).toEqual({
			status: 'ready', reason: undefined, repliedTo: 'r-1', answered: ['dana.whitfield@example.com'],
			conversation: [DANA, ANSWERED],
			attachments: [{ name: 'map.png', path: '/w/map.png', size: 10, type: 'image', unavailable: false }],
			notes: ['Same subject as an email from Oct 2.'],
		});
	});

	it('passes a named failure through', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
			status: 'error', reason: 'account', replied_to: 'r-1', answered: null, conversation: [], attachments: [], notes: [],
		}), { status: 200 })));
		expect(await fetchEmailDraftContext('a')).toMatchObject({ status: 'error', reason: 'account', conversation: [] });
	});

	it('names an ended sign-in and a draft that isn’t found, and turns any other failure into a failed read the view can retry', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })));
		expect(await fetchEmailDraftContext('a')).toEqual({ status: 'error', reason: 'signin', conversation: [], notes: [], attachments: [] });
		vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404 })));
		expect(await fetchEmailDraftContext('a')).toMatchObject({ status: 'error', reason: 'missing' });
		vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 500 })));
		expect(await fetchEmailDraftContext('a')).toMatchObject({ status: 'error', reason: 'unreadable' });
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline'); }));
		expect(await fetchEmailDraftContext('a')).toMatchObject({ status: 'error', reason: 'unreadable' });
	});
});
