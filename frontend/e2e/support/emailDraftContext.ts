import type { Page } from '@playwright/test';

// What `GET /api/email-drafts/{artifact_id}/context` (backend/api/email_drafts.py) returns, for route mocks: a conversation the
// draft replies into, newest first, with example.com addresses only. The draft text itself is real, saved through the files API.

export type ServerEmail = { id: string; sender: string; date: string; body: string };
export type ServerAttachment = { name: string; path: string; size: number; type: 'image' | 'document'; unavailable: boolean };
export type ServerContext = {
	status: 'ready' | 'error';
	reason: 'account' | 'unreadable' | null;
	replied_to: string | null;
	answered: string[] | null;
	conversation: ServerEmail[];
	attachments: ServerAttachment[];
	notes: string[];
};

export const DANA = 'Dana Whitfield <dana.whitfield@example.com>';
const MARCUS = 'Marcus Obi <marcus.obi@example.com>';
const PRIYA = 'Priya Raman <priya.raman@example.com>';
const JORDAN = 'Jordan Lee <jordan.lee@example.com>';
const ALEX = 'Alex Rivera <alex.rivera@example.com>';

const email = (id: string, sender: string, date: string, body: string): ServerEmail => ({ id, sender, date, body });

/** Dana's email of Nov 4 is the one the draft answers; the three below it came before. */
export const ANSWERED_ID = 'f4-1';
export const FOUR: ServerEmail[] = [
	email(ANSWERED_ID, DANA, '2026-11-04T17:12:00Z', 'Hi Alex,\n\nI counted eight sign-ups for Saturday so far, plus the three from the scout troop. We will have the two chippers booked from the county, but loppers are short: the shed only has four pairs.\n\nThe church lot may be closed that morning for a bake sale, so I would rather meet at the north trailhead lot. Can you confirm you are fine with that?\n\nDana'),
	email('f4-2', MARCUS, '2026-11-03T09:40:00Z', 'Dana, I can lead the brush crew again. I will need the 8:30 start so we clear the drainage cut before the rain on Sunday.\n\nDoes anyone know if the gate code changed? It did not work for me last month.\n\nMarcus'),
	email('f4-3', ALEX, '2026-11-02T16:05:00Z', 'Hi all,\n\nWork day is Saturday, Nov 14, 9:00 to 1:00. Please reply with your name and whether you are bringing your own gloves. We will cover water and lunch.\n\nAlex'),
	email('f4-4', DANA, '2026-11-02T11:20:00Z', 'Alex, the county confirmed the permit for the drainage cut. Can you send the volunteer note by tonight so people can plan the weekend?\n\nDana'),
];

// Emails that came after Dana's, newest first: they sit above it in the conversation.
const MARCUS_LATER = email('n-1', MARCUS, '2026-11-05T17:40:00Z', 'Dana, a quick update: I walked the drainage cut this afternoon and the gate code did change. The new one is on the sign at the lot.\n\nMarcus');
const DANA_LATER = email('n-2', DANA, '2026-11-06T18:05:00Z', 'Alex, the county can lend a third chipper if someone with a trailer can pick it up Friday evening. Can anyone help?\n\nDana');
export const NEWER: Record<1 | 2 | 6, ServerEmail[]> = {
	1: [MARCUS_LATER],
	2: [DANA_LATER, MARCUS_LATER],
	6: [
		email('n-7', JORDAN, '2026-11-09T15:30:00Z', 'The scout troop is bringing its own gloves, so you can take them off the list.\n\nJordan'),
		email('n-6', PRIYA, '2026-11-08T21:10:00Z', 'The first-aid kit is restocked. I will bring it and a second cooler.\n\nPriya'),
		email('n-5', MARCUS, '2026-11-08T09:25:00Z', 'The forecast moved the rain to Sunday afternoon, so Saturday morning is fine for the drainage cut.\n\nMarcus'),
		email('n-4', DANA, '2026-11-07T19:00:00Z', 'The county confirmed the chipper pickup for Friday at 5:30.\n\nDana'),
		DANA_LATER,
		MARCUS_LATER,
	],
};

/** The quiet notes the draft tools write (tools/mail_guard.py same-subject note, tools/google_email.py font note). */
export const SAME_SUBJECT = 'Dana Whitfield wrote about the same subject in another conversation.';
export const FONT = 'Couldn’t read your usual font, so this email may go out in your mail’s default font.';

/** A reply to Dana's email: the answered email went to Alex from Dana, so only those two addresses are on it. */
export function reply(over: Partial<ServerContext> = {}): ServerContext {
	return {
		status: 'ready', reason: null, replied_to: ANSWERED_ID, answered: ['dana.whitfield@example.com', 'alex.rivera@example.com'],
		conversation: FOUR, attachments: [], notes: [], ...over,
	};
}

/** A new email: no conversation to read. */
export function fresh(over: Partial<ServerContext> = {}): ServerContext {
	return { status: 'ready', reason: null, replied_to: null, answered: null, conversation: [], attachments: [], notes: [], ...over };
}

/** The read failed: the draft's account can't be used, or the account answered but the thread couldn't be read. */
export function failed(reason: 'account' | 'unreadable', over: Partial<ServerContext> = {}): ServerContext {
	return { ...reply({ status: 'error', reason, answered: null, conversation: [] }), ...over };
}

/**
 * Answers the draft view's context read for every draft the page opens. `answer` gets the artifact id and how many reads that
 * draft had before this one, so a test can fail the first read and answer the retry; `'hold'` never answers (the loading state).
 */
export async function mockEmailDraftContext(page: Page, answer: (artifactId: string, reads: number) => ServerContext | 'hold') {
	const reads = new Map<string, number>();
	await page.route((url) => /^\/api\/email-drafts\/[^/]+\/context$/.test(url.pathname), async (route) => {
		const artifactId = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
		const count = reads.get(artifactId) ?? 0;
		reads.set(artifactId, count + 1);
		const body = answer(artifactId, count);
		if (body === 'hold') return;
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
	});
	return { reads: (artifactId: string) => reads.get(artifactId) ?? 0 };
}

/**
 * The header as the server reads it (connections/mail/email_document.py `read_header`), for a document load the test marks as
 * an email draft. Which entries the send refuses is named by the test, so no address rule lives here.
 */
export function serverHeader(doc: string, refused: readonly string[] = []) {
	const text = doc.split('\n\n')[0];
	const recipients: Array<{ field: 'to' | 'cc'; text: string; from: number; to: number; full: boolean }> = [];
	let at = 0;
	for (const line of text.split('\n')) {
		const label = /^(to|cc)\s*:/i.exec(line);
		if (label) {
			let start = at + label[0].length;
			for (const part of line.slice(label[0].length).split(',')) {
				const entry = part.trim(), from = start + part.length - part.trimStart().length;
				if (entry) recipients.push({ field: label[1].toLowerCase() as 'to' | 'cc', text: entry, from, to: from + entry.length, full: !refused.includes(entry) });
				start += part.length + 1;
			}
		}
		at += line.length + 1;
	}
	return { text, recipients };
}

/**
 * Makes the document at `docPath` load as an email draft, as a draft record does on the real route (`read_document`
 * `email_draft`): its load carries the record's id and the server's reading of the header. Live frames carry no reading here,
 * so after an edit the marks keep moving with the text, as they do between two server readings.
 */
export async function markEmailDraft(page: Page, docPath: string, refused: readonly string[] = []) {
	await page.route((url) => url.pathname === '/api/files/content' && url.searchParams.get('path') === docPath, async (route) => {
		if (route.request().method() !== 'GET') return route.fallback();
		const response = await route.fetch();
		const body = await response.json();
		await route.fulfill({ response, json: { ...body, email_draft: { id: 'draft-frames', header: serverHeader(body.text ?? '', refused) } } });
	});
}
