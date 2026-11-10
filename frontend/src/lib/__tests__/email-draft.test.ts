import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	badEntries, isFullAddress, newerEmails, newerEmailsLine, notFullAddressLine, previewLine, recipientEntries, repliedToId, senderAddress, senderName,
	type EmailMessage,
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

describe('a bad address', () => {
	const DRAFT = ['From: Alex', 'To: dana@example.com, priya.raman, dana@', 'Cc: ', 'Subject: S', '', 'To: not a header'].join('\n');

	it('reads the entries of To and Cc as typed, stopping at the first blank line', () => {
		expect(recipientEntries(DRAFT)).toEqual(['dana@example.com', 'priya.raman', 'dana@']);
		expect(recipientEntries('To: a@b.co\r\nCc: c@d.co , e@f.co\r\n\r\nTo: x')).toEqual(['a@b.co', 'c@d.co', 'e@f.co']);
	});

	it('follows the send’s plain-address rule: a name, angle brackets, a missing part or a hidden character fail', () => {
		expect(isFullAddress('dana@example.com')).toBe(true);
		expect(isFullAddress('Dana Whitfield <dana@example.com>')).toBe(false);
		expect(isFullAddress('priya.raman')).toBe(false);
		expect(isFullAddress('dana@')).toBe(false);
		expect(isFullAddress('a@b@c.co')).toBe(false);
		expect(isFullAddress('dana​@example.com')).toBe(false);
	});

	it('names the entries the send would refuse, in the order typed', () => {
		expect(badEntries(DRAFT)).toEqual(['priya.raman', 'dana@']);
		expect(badEntries(DRAFT.replace('priya.raman', 'priya.raman@example.com'))).toEqual(['dana@']);
		expect(badEntries('To: a@b.co\nSubject: S\n\nbody')).toEqual([]);
	});

	it('names one entry as "<entry> isn’t a full email address." and several with a count', () => {
		expect(notFullAddressLine([])).toBeNull();
		expect(notFullAddressLine(['priya.raman'])).toBe('priya.raman isn’t a full email address.');
		expect(notFullAddressLine(['priya.raman', 'dana@'])).toBe('2 entries aren’t full email addresses: priya.raman, dana@');
	});
});

describe('reading a draft’s context', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('asks the draft’s route by artifact id and maps the server’s answer to the view’s names', async () => {
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({
			status: 'partial', reason: null, replied_to: 'r-1', answered: ['dana.whitfield@example.com'],
			conversation: [DANA, ANSWERED], unreadable: 1,
			attachments: [{ name: 'map.png', path: '/w/map.png', size: 10, type: 'image', unavailable: false }],
			notes: ['Same subject as an email from Oct 2.'],
		}), { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);
		const context = await fetchEmailDraftContext('art/1');
		expect((fetchMock.mock.calls[0] as unknown[])[0]).toBe('/api/email-drafts/art%2F1/context');
		expect(context).toEqual({
			status: 'partial', reason: undefined, repliedTo: 'r-1', answered: ['dana.whitfield@example.com'],
			conversation: [DANA, ANSWERED], unreadable: 1,
			attachments: [{ name: 'map.png', path: '/w/map.png', size: 10, type: 'image', unavailable: false }],
			notes: ['Same subject as an email from Oct 2.'],
		});
	});

	it('passes a named failure through', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
			status: 'error', reason: 'account', replied_to: 'r-1', answered: null, conversation: [], unreadable: 0, attachments: [], notes: [],
		}), { status: 200 })));
		expect(await fetchEmailDraftContext('a')).toMatchObject({ status: 'error', reason: 'account', conversation: [] });
	});

	it('turns a refused or failed request into the same failed read, so the view offers its retry', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404 })));
		expect(await fetchEmailDraftContext('a')).toEqual({ status: 'error', reason: 'unreadable', conversation: [], notes: [], attachments: [] });
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline'); }));
		expect(await fetchEmailDraftContext('a')).toMatchObject({ status: 'error', reason: 'unreadable' });
	});
});
