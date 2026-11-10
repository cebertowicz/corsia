// Render route for the email draft view prototype: stills, comparison frames and motion recordings on the real document canvas.
// Skipped unless EMAIL_FRAMES_OUT is set. Run from the repo root:
//   CORSIA_E2E_MOCK=1 CORSIA_E2E_BACKEND_PORT=<free> CORSIA_E2E_FRONTEND_PORT=<free> CORSIA_E2E_PYTHON=<python 3.11> \
//   CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1 EMAIL_FRAMES_PHASE=before|after|compare|motion EMAIL_FRAMES_OUT=<dir> \
//   [EMAIL_FRAMES_ONLY=<scenario>[,<scenario>...]] [EMAIL_FRAMES_VARIANTS=1440-light,393-dark] bash scripts/e2e-local --grep "email-draft-view frames" --project=chromium-desktop
// Conversation data comes from the localStorage key `corsia:emailDraftFixture` (see src/lib/emailDraftFixtures.ts).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { createHash } from 'node:crypto';
import { test } from '../support/test';
import { chromium, expect, type APIRequestContext, type Browser, type BrowserContext, type Page, type TestInfo, type WebSocketRoute } from '@playwright/test';
import { createSession, lastAssistantMessage, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { seedMockResponses } from '../support/mock';

const PHASE = process.env.EMAIL_FRAMES_PHASE ?? 'after';
const OUT = process.env.EMAIL_FRAMES_OUT ?? '';
const ONLY = process.env.EMAIL_FRAMES_ONLY?.split(',');
/** Comma list such as `1440-light,393-dark` to capture only those width-theme pairs. */
const VARIANTS = process.env.EMAIL_FRAMES_VARIANTS?.split(',');

const FROM = 'From: Alex Rivera <alex.rivera@example.com>';
const REPLY_BODY = [
	'Hi Dana,',
	'',
	'Thanks for the count. Twelve of us can come Saturday, so the two extra pairs of loppers from the shed will be plenty. I will bring the sign-in sheet and a cooler of water.',
	'',
	'Could you confirm the meeting spot is still the north trailhead lot, not the church lot?',
	'',
	'Thanks,',
	'Alex',
].join('\n');
const SIGNATURE = ['-- ', 'Alex Rivera', 'Trail Volunteer Coordinator'].join('\n');
const REPLIED_TO_NOTE = [
	'Note: The message being replied to:',
	'Note: Dana Whitfield <dana.whitfield@example.com> wrote on Nov 4, 2026, 5:12 PM UTC (read).',
	'Note: Hi Alex,',
	'Note: ',
	'Note: I counted eight sign-ups for Saturday so far, plus the three from the scout troop. We will have the two chippers booked from the county, but loppers are short: the shed only has four pairs.',
	'Note: ',
	'Note: The church lot may be closed that morning for a bake sale, so I would rather meet at the north trailhead lot. Can you confirm you are fine with that?',
	'Note: ',
	'Note: Dana',
	'Note: The earlier message from Dana Whitfield <dana.whitfield@example.com> will be quoted below the reply.',
].join('\n');
const SUBJECT = 'Re: Saturday work day: tools and parking';
// The draft header as the backend writes it now: the reply target lives in the draft record, not in a text line.
const REPLY_HEAD = [FROM, 'To: Dana Whitfield <dana.whitfield@example.com>', `Subject: ${SUBJECT}`];
// The header the pre-change code wrote, for the before phase.
const OLD_REPLY_HEAD = [...REPLY_HEAD, 'In reply to: "Saturday work day: tools and parking" from Dana Whitfield <dana.whitfield@example.com>'];
const LONG_HEAD = [FROM, 'To: Dana Whitfield <dana.whitfield@example.com>, Priya Raman <priya.raman@example.com>, Marcus Obi <marcus.obi@example.com>, Jordan Ellis <jordan.ellis@example.com>', 'Cc: Scout Troop 214 <troop214@example.org>', `Subject: ${SUBJECT}`];
// Priya was not on the email being answered: she sits in Cc here, and in To (with Jordan in Cc) below.
const CC_ADDED_HEAD = [FROM, 'To: Dana Whitfield <dana.whitfield@example.com>', 'Cc: Priya Raman <priya.raman@example.com>', `Subject: ${SUBJECT}`];
const BOTH_ADDED_HEAD = [FROM, 'To: Dana Whitfield <dana.whitfield@example.com>, Priya Raman <priya.raman@example.com>', 'Cc: Jordan Lee <jordan.lee@example.com>', `Subject: ${SUBJECT}`];
const BAD_TO_HEAD = [FROM, 'To: Dana Whitfield <dana.whitfield@example.com>, priya.raman', `Subject: ${SUBJECT}`];
const BAD_TO2_HEAD = [FROM, 'To: Dana Whitfield <dana.whitfield@example.com>, priya.raman, dana@', `Subject: ${SUBJECT}`];

const PLAIN_DOC = [
	'# Volunteer work day: Nov 14',
	'',
	'The work day runs from 9:00 to 1:00 at the north trailhead lot. We cover water and lunch; bring your own gloves.',
	'',
	'## Crews',
	'',
	'- **Drainage cut:** Marcus leads. Needs the two county chippers and the permit printout.',
	'- **Brush:** Priya leads. First-aid kit and a second cooler of water.',
	'- **Bridge approach:** Jordan leads, with the scout troop and a short safety talk at the start.',
	'',
	'## Before Saturday',
	'',
	'Everyone who runs a lopper, a saw or a chipper signs the waiver ahead of time. Scout leaders sign for each minor on their own forms, so the line at the sign-in table stays short.',
].join('\n');

const DOCS = {
	before: [...OLD_REPLY_HEAD, REPLIED_TO_NOTE].join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	reply: REPLY_HEAD.join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	ccadded: CC_ADDED_HEAD.join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	bothadded: BOTH_ADDED_HEAD.join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	longhead: LONG_HEAD.join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	badto: BAD_TO_HEAD.join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	badto2: BAD_TO2_HEAD.join('\n') + `\n\n${REPLY_BODY}\n\n${SIGNATURE}\n`,
	fresh: [FROM, 'To: Marcus Obi <marcus.obi@example.com>', 'Subject: Brush crew start time for Saturday'].join('\n')
		+ '\n\nHi Marcus,\n\nThe 8:30 start works. Could you lead the brush crew again and walk the drainage cut with the county contact before the rest of us arrive?\n\nThanks,\nAlex\n\n' + SIGNATURE + '\n',
	plain: PLAIN_DOC + '\n',
};

// The chat beside a draft says what the draft says: who it goes to is read from the doc's own To and Cc, so the two never disagree.
const REPLY_SUMMARY = 'I drafted a reply to Dana Whitfield and saved it as a document. It confirms twelve volunteers, says you will bring the sign-in sheet and a cooler of water, and asks her to confirm the north trailhead lot instead of the church lot.';
const REPLY_CHAT = (recipients: string) => ({
	user: 'Dana asked about parking and loppers for Saturday’s work day. Can you draft a reply?',
	assistant: [REPLY_SUMMARY, '', 'Check before you send:', '', `- ${recipients}`, '', 'Open the draft to read the earlier emails and edit the reply.'].join('\n'),
	tail: 'Open the draft to read the earlier emails',
});
const CHATS: Record<keyof typeof DOCS, { user: string; assistant: string; tail: string }> = {
	before: REPLY_CHAT('The reply goes to Dana only. Priya was on the earlier emails, so add her if she should see it.'),
	reply: REPLY_CHAT('The reply goes to Dana only. Priya wasn’t on Dana’s email, so add her if she should see it.'),
	ccadded: REPLY_CHAT('The reply goes to Dana, with Priya in Cc. Priya wasn’t on Dana’s email, so remove her if she shouldn’t see it.'),
	bothadded: REPLY_CHAT('The reply goes to Dana and Priya, with Jordan Lee in Cc. Priya and Jordan weren’t on Dana’s email, so remove them if they shouldn’t see it.'),
	longhead: REPLY_CHAT('The reply goes to Dana, Priya, Marcus and Jordan Ellis, with Scout Troop 214 in Cc. Only Dana was on her email.'),
	badto: REPLY_CHAT('The reply goes to Dana, and the To line also has priya.raman, which isn’t a full email address. Fix it before you send.'),
	badto2: REPLY_CHAT('The reply goes to Dana, and the To line also has priya.raman and dana@, which aren’t full email addresses. Fix them before you send.'),
	fresh: {
		user: 'Can you ask Marcus whether the 8:30 start works for the brush crew?',
		assistant: 'I drafted a new email to Marcus Obi and saved it as a document. It asks him to lead the brush crew again and to walk the drainage cut with the county contact before the rest of you arrive.\n\nOpen the draft to edit it before you send.',
		tail: 'Open the draft to edit it',
	},
	plain: {
		user: 'Can you write up the plan for the Nov 14 volunteer work day?',
		assistant: 'I wrote up the plan for the Nov 14 work day: the hours, the three crews and what to sign before Saturday.\n\nOpen the document to read it.',
		tail: 'Open the document to read it',
	},
};

const VIEWPORTS = [
	{ name: '1440', viewport: { width: 1440, height: 1000 }, phone: false },
	{ name: '393', viewport: { width: 393, height: 852 }, phone: true },
];
const THEMES = ['light', 'dark'] as const;

/** What an act may need beyond the page: the API, the saved draft's path and the folder holding the attachment files. */
type ActContext = { request: APIRequestContext; docPath: string; files: string; context: BrowserContext; review: ReviewMock };
/** The Needs-you queue and the review answer, mocked for a draft with an open review item: what the page asked, and whether the item is still open. */
// `mode` is how the mocked answer replies: 'ok', never ('pending'), 'stale' (the server's ask_stale refusal: the send request ended), 'changed' (the same refusal, but the send request was replaced by another request with new holds), or 503 for the first `failuresLeft` answers.
// The requests are live from the moment the draft opens: `GET /api/attention/{id}/review` reports them (`requests`, newest first, each with its hold id, asker and reason, and `purpose: 'send'` on the send request) until an answer lands, as the server would. An answer must carry holds that are all still open, else it is refused as `ask_stale`. PROTOTYPE: the real field is a dependency on Structured Plans.
type Hold = { id: string; asker: { id: string; name: string }; reason: string; purpose?: 'send' };
type ReviewMock = { holds: Hold[]; reads: number; readCalls: number; clears: Array<Record<string, unknown>>; mode: 'ok' | 'pending' | 'stale' | 'changed'; failuresLeft: number; replaceSend: () => void; readFails: boolean; nudge: () => void; addSend: () => void };
const DRAFTER = { id: 'email', name: 'Email drafter' };
const CREW = { id: 'crew', name: 'Trail crew' };
const SEND_REASON = 'Review the reply to Dana Whitfield';
const CORRECTED_REASON = 'Review the corrected reply to Dana Whitfield';
const ORDINARY_REASON = 'Check the Saturday parking numbers';
type Act = (page: Page, ctx: ActContext) => Promise<void>;
/** An act that needs only the page, so acts can call it with one argument. */
type PageAct = (page: Page) => Promise<void>;
type Scenario = {
	name: string; doc: keyof typeof DOCS; fixture: string | null; act?: Act;
	/** Only 1440 light and 393 dark. */ thin?: boolean;
	/** The pointer stays where the act left it (a hover frame); otherwise it is moved off the page before the shot. */ keepPointer?: boolean;
	/** Also a 1440 shot of the canvas pane by itself. */ alone?: boolean;
	/** Open the document with the canvas closed (the chat view). */ chatOnly?: boolean;
	/** Mock the Needs-you queue so it holds one review item for this draft, and the answer that settles it. */ review?: boolean;
	/** How the mocked review answer replies; see ReviewMock. */ reviewMode?: ReviewMock['mode']; reviewFailures?: number;
	/** An ordinary review request (not the send request) is also open on the draft. */ ordinaryRequest?: boolean;
	/** The read of the draft's requests fails until the act turns it back on: the bar does not know the requests when the person presses. */ reviewReadFails?: boolean;
	/** Launch the full Chromium (headless shell has no PDF viewer), for a frame that shows a PDF in the canvas. */ fullChromium?: boolean;
	/** Restore the canvas with the draft and, active, the PDF opened by path with no attachment chip (any other opener); its bytes are mocked as valid or broken. */ pdfByPath?: 'valid' | 'broken';
};

/** Keep the header in the frame when the chips fit under it (desktop); otherwise centre the chips. */
const revealChipsKeepingHeader: PageAct = async (page) => {
	await page.locator('.email-scroll').evaluate((el) => { el.scrollTop = 0; });
	const fits = await page.locator('.email-attachments').evaluate((el) => el.getBoundingClientRect().bottom < window.innerHeight - 24);
	if (!fits) await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
};

/** The canvas's review bar shows the server's current request. Its answer buttons show for an ordinary request and never for the send request: Send and Don’t send answer that one. */
const ANSWERS = ['Approve', 'Approve with changes', 'Reject'];
const expectCanvasAsk = async (page: Page, ask: string, options: { who?: string; answers?: boolean } = {}) => {
	const bar = page.locator('.review-bar.ask');
	await expect(bar).toBeVisible({ timeout: 10_000 });
	await expect(bar).toContainText(ask);
	await expect(bar).toContainText(options.who ?? 'Email drafter');
	for (const name of ANSWERS) await expect(page.getByRole('button', { name, exact: true })).toHaveCount(options.answers ? 1 : 0);
};
/** The chat dock's item carries the same request title as the canvas's request row. */
const expectDockTitle = async (page: Page, title: string) => {
	await expect(dockBar(page)).toBeVisible({ timeout: 15_000 });
	await expect(dockBar(page)).toContainText(title);
};
const expectNoCanvasAsk = async (page: Page) => { await expect(page.locator('.review-bar.ask')).toHaveCount(0, { timeout: 8000 }); };

const scrollEnd: PageAct = async (page) => {
	await page.locator('.email-scroll').evaluate((el) => { el.scrollTop = el.scrollHeight; });
};
const scrollToSection: PageAct = async (page) => {
	await page.locator('.email-scroll').evaluate((el) => {
		const section = el.querySelector('.email-conversation') as HTMLElement;
		el.scrollTop += section.getBoundingClientRect().top - el.getBoundingClientRect().top - 12;
	});
};
const openRow = (index: number): PageAct => async (page) => {
	const row = page.locator('.email-toggle').nth(index);
	await row.click();
	await expect(row).toHaveAttribute('aria-expanded', 'true');
	await page.waitForTimeout(400);
};

const chipFor = (page: Page, name: string) => page.locator('.email-attachments .attachment').filter({ hasText: name });
/**
 * Round 13: what every attachment chip really measures, written next to the frame. A chip is one line high at every width, a long
 * name is cut in the middle (the start gives way, the extension stays), and the full name is in the chip's title and accessible name.
 */
const chipFacts = (page: Page, file: string) => page.evaluate(() => {
	const rows = Array.from(document.querySelectorAll<HTMLElement>('.email-attachments .attachment'));
	const ul = document.querySelector<HTMLElement>('.email-attachments')!.getBoundingClientRect();
	return {
		pointerCoarse: matchMedia('(pointer: coarse)').matches,
		viewportWidth: innerWidth,
		listWidth: Math.round(ul.width * 10) / 10,
		chips: rows.map((li) => {
			const chip = li.querySelector<HTMLElement>('.tile.chip')!;
			const title = chip.querySelector<HTMLElement>('.title')!;
			const start = chip.querySelector<HTMLElement>('.name-start')!;
			const end = chip.querySelector<HTMLElement>('.name-end');
			const lineHeight = parseFloat(getComputedStyle(title).lineHeight);
			const chipBox = chip.getBoundingClientRect(), startBox = start.getBoundingClientRect(), liBox = li.getBoundingClientRect();
			const size = li.querySelector<HTMLElement>('.chip-aside')!.getBoundingClientRect();
			const removeEl = li.querySelector<HTMLElement>('.attachment-remove');
			const remove = removeEl?.getBoundingClientRect() ?? null;
			const coarse = matchMedia('(pointer: coarse)').matches;
			const hitSize = removeEl ? parseFloat(getComputedStyle(removeEl, '::after').width) : 0;
			return {
				// Round 13c: one pill. The size and the remove mark are inside the pill that carries the fill; the chip itself has none.
				pill: {
					fill: getComputedStyle(li).backgroundColor, chipFill: getComputedStyle(chip).backgroundColor, radius: getComputedStyle(li).borderTopLeftRadius,
					sizeInside: size.left >= liBox.left - 0.5 && size.right <= liBox.right + 0.5,
					removeInside: remove ? remove.left >= liBox.left - 0.5 && remove.right <= liBox.right + 0.5 : null,
					chipFillClear: getComputedStyle(chip).backgroundColor === 'rgba(0, 0, 0, 0)',
					// On touch the remove press target is 44 px around the 24 px mark, and starts after the size text ends.
					removeHit: removeEl && coarse ? { width: hitSize, clearOfSize: remove!.left + remove!.width / 2 - hitSize / 2 >= size.right } : null,
				},
				fullName: chip.getAttribute('title'),
				ariaLabel: chip.getAttribute('aria-label'),
				shownStart: start.textContent, shownEnd: end?.textContent ?? '',
				// The start is cut when it ends in the "…" the page wrote (`middleEllipsis`), or the stylesheet still clips it.
				startCutByEllipsis: (start.textContent ?? '').endsWith('…') || start.scrollWidth > start.clientWidth + 1,
				// Empty room between the end of the start's text (the "…") and the first letter of the kept end; 0 is one run.
				gapAfterEllipsis: end ? (() => { const range = document.createRange(); range.selectNodeContents(start); return Math.round((end.getBoundingClientRect().left - range.getBoundingClientRect().right) * 100) / 100; })() : null,
				// The end is cut when its box is narrower than its text, or when it sticks out of the title (which clips it).
				endScrollFits: end ? end.scrollWidth <= end.clientWidth : true,
				endCut: end ? (() => { const range = document.createRange(); range.selectNodeContents(end); return range.getBoundingClientRect().width > end.getBoundingClientRect().width + 0.5 || end.getBoundingClientRect().right > title.getBoundingClientRect().right + 0.5 || getComputedStyle(end).textOverflow === 'ellipsis'; })() : false,
				// Where the "…" is drawn: the right edge of the visible start, as a distance from the chip's left edge.
				ellipsisAtX: Math.round(startBox.right - chipBox.left),
				chipWidth: Math.round(chipBox.width * 10) / 10, chipHeight: Math.round(chipBox.height * 10) / 10,
				rowHeight: Math.round(liBox.height * 10) / 10,
				titleLines: Math.round(title.getBoundingClientRect().height / lineHeight),
				sizeOnSameLine: Math.abs((size.top + size.bottom) / 2 - (chipBox.top + chipBox.bottom) / 2) < 2,
				removeOnSameLine: remove ? Math.abs((remove.top + remove.bottom) / 2 - (chipBox.top + chipBox.bottom) / 2) < 2 : null,
				insideList: liBox.right <= ul.right + 0.5,
			};
		}),
	};
}).then((facts) => {
	const heights = new Set(facts.chips.map((c) => c.chipHeight));
	const summary = { ...facts, allChipHeightsEqual: heights.size === 1, noWrap: facts.chips.every((c) => c.titleLines === 1), endsIntact: facts.chips.every((c) => !c.endCut && c.endScrollFits), noGap: facts.chips.every((c) => c.gapAfterEllipsis === null || Math.abs(c.gapAfterEllipsis) <= 0.5), allInsideList: facts.chips.every((c) => c.insideList), onePill: facts.chips.every((c) => c.pill.sizeInside && c.pill.removeInside !== false && c.pill.chipFillClear && c.pill.fill !== 'rgba(0, 0, 0, 0)' && (!c.pill.removeHit || (c.pill.removeHit.width >= 44 && c.pill.removeHit.clearOfSize))) };
	fs.writeFileSync(path.join(OUT, `${PHASE}-${file}-${isPhone(page) ? '393' : '1440'}.chips.json`), JSON.stringify(summary, null, 1));
	return summary;
});
/** The saved text of the draft file, read back from the server. */
const savedText = async (ctx: ActContext) => {
	const response = await ctx.request.get('/api/files/content', { params: { token: TEST_TOKEN, path: ctx.docPath } });
	expect(response.ok(), await response.text()).toBeTruthy();
	return ((await response.json()).text ?? '') as string;
};
const clickBar = (name: string): PageAct => async (page) => {
	await page.locator('.email-send-bar').getByRole('button', { name, exact: true }).click();
	await page.waitForTimeout(350);
};
/** Round 10b: while the email is Sending, Undo is in the bar and no toast sits over the header: From, To, Cc and Subject are all on screen and none is under the toast container. */
const expectUndoInBarAndHeaderClear: PageAct = async (page) => {
	await expect(page.locator('.email-send-bar').getByRole('button', { name: 'Undo', exact: true })).toBeVisible();
	await expect(page.locator('.toast-container')).toHaveCount(0);
	const heads = await page.locator('.cm-line.cm-email-head').evaluateAll((lines) => lines.map((line) => {
		const rect = line.getBoundingClientRect();
		return { text: (line.textContent ?? '').slice(0, 12), top: Math.round(rect.top), bottom: Math.round(rect.bottom), right: Math.round(rect.right), covered: document.elementFromPoint(Math.min(rect.left + 8, innerWidth - 1), Math.min(rect.top + rect.height / 2, innerHeight - 1))?.closest('.toast-container') !== null }; }));
	expect(heads.length).toBeGreaterThanOrEqual(2);
	for (const head of heads) { expect(head.covered, head.text).toBe(false); expect(head.top, head.text).toBeGreaterThanOrEqual(0); expect(head.bottom, head.text).toBeLessThanOrEqual(await page.evaluate(() => innerHeight)); expect(head.right, head.text).toBeLessThanOrEqual(await page.evaluate(() => innerWidth)); }
};
/** Round 11: To and Cc wrap at every width, never cut with an ellipsis, in any state (editable, Sending, read-only after Sent). A long To line takes more than one text line, and nothing in it is clipped. */
const expectAddressLinesWrap: PageAct = async (page) => {
	const lines = await page.locator('.cm-line.cm-email-head-address').evaluateAll((els) => els.map((el) => {
		const style = getComputedStyle(el as HTMLElement);
		const lineHeight = Number.parseFloat(style.lineHeight) || (el as HTMLElement).getBoundingClientRect().height;
		return { text: (el.textContent ?? '').slice(0, 4), whiteSpace: style.whiteSpace, overflow: style.overflowX, textOverflow: style.textOverflow, clipped: el.scrollWidth > el.clientWidth + 1, rows: Math.round((el as HTMLElement).getBoundingClientRect().height / lineHeight) };
	}));
	expect(lines.length).toBeGreaterThanOrEqual(2);
	for (const line of lines) { expect(line.clipped, line.text).toBe(false); expect(line.textOverflow, line.text).not.toBe('ellipsis'); expect(line.whiteSpace, line.text).toMatch(/^(pre-wrap|break-spaces)$/); }
	const to = lines.find((line) => line.text.startsWith('To:'));
	if (isPhone(page)) expect(to?.rows, 'a four-recipient To line wraps at a phone width').toBeGreaterThan(2);
};
const NOT_ANSWERED = 'Sent. The review request wasn’t answered.';
/** Round 12: Send pressed with the requests unreadable; one Retry that still fails, one that works. Neither posts an answer. */
const retriedReadWorks: Act = async (page, ctx) => {
	await clickBar('Send')(page);
	await expect(page.locator('.email-send-bar .bar-text')).toHaveText(NOT_ANSWERED, { timeout: 12_000 });
	const failed = page.locator('.review-bar.unreadable');
	const retry = failed.getByRole('button', { name: 'Retry', exact: true });
	await expect(retry).toBeVisible();
	let reads = ctx.review.readCalls;
	await retry.click(); // the read still fails
	await expect.poll(() => ctx.review.readCalls, { timeout: 8000 }).toBe(reads + 1);
	await expect(failed).toContainText('Couldn’t load the review request.');
	await expect(retry).toBeEnabled();
	await expect(page.locator('.email-send-bar .bar-text')).toHaveText(NOT_ANSWERED);
	expect(ctx.review.clears).toHaveLength(0);
	ctx.review.readFails = false;
	reads = ctx.review.readCalls;
	await retry.click();
	await expectCanvasAsk(page, SEND_REASON, { answers: true });
	expect(ctx.review.readCalls).toBe(reads + 1); // one read, nothing else
	await expect(page.locator('.review-bar.unreadable')).toHaveCount(0);
	await expect(page.locator('.email-send-bar .bar-text')).toHaveText(`${NOT_ANSWERED} Answer it in the review bar.`); // the request and its answers are there now
	expect(ctx.review.clears).toHaveLength(0); // neither press posted an answer
};
/** Round 12: the collapsed request title in the canvas's review bar keeps who it is for (at least the recipient's first name shows, whole if it fits), and a cut is marked with an ellipsis. Measured, not eyeballed: written beside the frame. */
const checkCollapsedAsk = async (page: Page, name: string, recipient: string) => {
	const facts = await page.locator('.review-bar.ask .rb-ask').evaluate((el, who) => {
		const box = el as HTMLElement;
		const style = getComputedStyle(box);
		const rect = box.getBoundingClientRect();
		const node = box.firstChild as Text;
		const text = node.textContent ?? '';
		const rightOf = (from: number, to: number) => { const range = document.createRange(); range.setStart(node, from); range.setEnd(node, to); return range.getBoundingClientRect().right; };
		const at = text.indexOf(who);
		const firstEnd = at + who.split(' ')[0].length;
		return { text, display: style.display, textOverflow: style.textOverflow, whiteSpace: style.whiteSpace, cut: box.scrollWidth > box.clientWidth + 1, askWidth: Math.round(rect.width), askRight: Math.round(rect.right), recipientRight: Math.round(rightOf(at, at + who.length)), firstNameRight: Math.round(rightOf(at, firstEnd)), recipientFullyVisible: rightOf(at, at + who.length) <= rect.right + 0.5, firstNameVisible: rightOf(at, firstEnd) <= rect.right + 0.5 };
	}, recipient);
	fs.writeFileSync(path.join(OUT, `${PHASE}-${name}-${isPhone(page) ? '393-dark' : '1440-light'}.askline.json`), JSON.stringify(facts));
	expect(facts.text).toContain(recipient);
	expect(facts.firstNameVisible, 'the collapsed title shows who the request is for').toBe(true);
	if (facts.cut) { expect(facts.display).toBe('block'); expect(facts.textOverflow).toBe('ellipsis'); expect(facts.whiteSpace).toBe('nowrap'); }
	else expect(facts.recipientFullyVisible).toBe(true);
};
/** Opens the Files list: the pane beside the canvas on a desktop, the Files button on a phone. */
const showFiles: PageAct = async (page) => {
	const expand = page.getByRole('button', { name: /Expand Files/ });
	if (await expand.count()) { await expand.first().click(); await page.waitForTimeout(400); return; }
	const phone = page.getByRole('button', { name: /^Files \(\d+\)/ });
	if (await phone.count()) { await phone.first().click(); await page.waitForTimeout(500); }
};

/** What a person could still edit on the canvas, written beside the frame: a still can't show a control that is missing. */
const writeEditControls = async (page: Page, name: string) => {
	const facts = await page.evaluate(() => {
		const content = document.querySelector('.cm-content');
		return {
			ccButtons: document.querySelectorAll('.cm-email-cc-add').length,
			chips: document.querySelectorAll('.email-attachments .attachment').length,
			removeMarks: document.querySelectorAll('.attachment-remove').length,
			editorAriaReadonly: content?.getAttribute('aria-readonly') ?? null,
			editorContenteditable: content?.getAttribute('contenteditable') ?? null,
			emptyCcHints: document.querySelectorAll('.cm-email-address-hint').length,
			barButtons: Array.from(document.querySelectorAll('.email-send-bar button')).map((b) => (b.textContent ?? '').trim()),
			barText: (document.querySelector('.email-send-bar .bar-text')?.textContent ?? '').trim(),
		};
	});
	fs.writeFileSync(path.join(OUT, `${PHASE}-${name}-${isPhone(page) ? '393' : '1440'}.edit-controls.json`), JSON.stringify(facts));
	return facts;
};

/** The canvas toolbar's controls as a person reads them, written beside the frame. */
const writeToolbar = async (page: Page, name: string) => {
	const facts = await page.evaluate(() => {
		const row = document.querySelector('.document-status-row');
		const rect = (el: Element | null) => { const r = el?.getBoundingClientRect(); return r ? { width: Math.round(r.width), height: Math.round(r.height) } : null; };
		return {
			controls: Array.from(row?.querySelectorAll('button, [role="switch"], .delivery-toggle') ?? []).map((el) => (el.getAttribute('aria-label') ?? el.textContent ?? '').trim()).filter(Boolean),
			deliveryText: (document.querySelector('.delivery-toggle')?.textContent ?? '').trim(),
			deliveryTitle: document.querySelector('.delivery-toggle')?.getAttribute('title') ?? null,
			requestReview: { text: (document.querySelector('.review-request')?.textContent ?? '').trim(), box: rect(document.querySelector('.review-request')), iconShown: !!document.querySelector('.review-request svg') && getComputedStyle(document.querySelector('.review-request svg')!).display !== 'none' },
			toolbarOverflowsX: row ? row.scrollWidth > row.clientWidth + 1 : null,
		};
	});
	fs.writeFileSync(path.join(OUT, `${PHASE}-${name}-${isPhone(page) ? '393' : '1440'}.toolbar.json`), JSON.stringify(facts));
};

/** Opens the dock's review item from its collapsed bar. */
const openReviewItem: PageAct = async (page) => {
	const bar = page.locator('[data-attention-dock] .dock-bar');
	if (await bar.count()) await bar.click();
	await expect(page.locator('.external-review')).toBeVisible({ timeout: 15_000 });
	await page.waitForTimeout(500);
};

/** The dock bar, wherever it can be seen: beside the draft on a desktop; a phone shows the draft or the chat, so the dock waits behind Chat. */
const dockBar = (page: Page) => page.locator('[data-attention-dock] .dock-bar');
const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1440) < 500;
/** On a phone, goes from the draft back to the chat, where the Needs-you list is; a desktop already shows both. */
const toChat = async (page: Page) => {
	if (!isPhone(page)) return;
	await page.getByRole('button', { name: 'Chat', exact: true }).click();
	await page.waitForTimeout(900);
};
const expectDock = async (page: Page) => {
	await toChat(page);
	await expect(dockBar(page)).toBeVisible({ timeout: 15_000 });
};

const chatAndDock = async (page: Page, name: string, title: string) => {
	if (isPhone(page)) await page.screenshot({ path: path.join(OUT, `${PHASE}-${name}-draft-393-dark.png`), animations: 'disabled' });
	await toChat(page);
	await expectDockTitle(page, title);
	await page.waitForTimeout(500);
};

/** The amber line above From names the newer emails, and the list holds them above the email being replied to (no badge, no link). */
const expectNewer = async (page: Page, line: string, count: number) => {
	await expect(page.locator('.email-loud li')).toHaveText([line]);
	const list = page.locator('.email-conversation');
	await expect(list.locator('h3')).toContainText('In this conversation');
	await expect(list.locator('h3')).toContainText(`${count + 4} emails`);
	// Collapsed, newest first, and the email being replied to follows the newer ones.
	const rows = list.locator('.email-row');
	await expect(rows.nth(count).locator('.email-second').first()).toHaveText('Replying to this email');
	await expect(list.locator('.email-row.open')).toHaveCount(0);
	await expect(list.locator('a')).toHaveCount(0);
	await page.locator('.email-conversation').evaluate((el) => el.scrollIntoView({ block: 'start' }));
	await page.waitForTimeout(300);
};
/** A bad address is amber where it sits in To or Cc, with no "!", and one amber line names it as typed. */
const expectBad = async (page: Page, entries: string[], line: string) => {
	await expect(page.locator('.cm-email-added')).toHaveText(entries);
	await expect(page.locator('.cm-email-added-mark')).toHaveCount(0);
	await expect(page.locator('.email-loud li')).toHaveText([line]);
	await page.waitForTimeout(300);
};

const AFTER: Scenario[] = [
	{ name: '2-rest', doc: 'reply', fixture: 'reply4', alone: true },
	{ name: '2b-rest-scrolled', doc: 'reply', fixture: 'reply4', act: scrollEnd },
	{ name: '3-expanded', doc: 'reply', fixture: 'reply4', alone: true, act: async (page) => { await openRow(1)(page); await scrollEnd(page); } },
	{ name: '4-warning', doc: 'ccadded', fixture: 'warn4' },
	{ name: '4b-quiet-open', doc: 'reply', fixture: 'warn4', thin: true, act: async (page) => { await page.locator('.cm-email-notes-toggle').click(); await page.waitForTimeout(300); } },
	{ name: '4c-loud-many', doc: 'bothadded', fixture: 'loud3', thin: true },
	{ name: '4g-added-hint', doc: 'ccadded', fixture: 'reply4', thin: true, keepPointer: true, act: async (page) => {
		// A desktop shows the text on hover; a phone has no hover, so a tap opens it.
		const mark = page.locator('.cm-email-added-mark');
		await expect(mark).toHaveCount(1);
		if (isPhone(page)) await mark.click(); else await mark.hover();
		const tip = page.locator('.cm-email-added-tip');
		await expect(tip).toBeVisible();
		await expect(tip).toHaveText('Not on the email you’re replying to.');
		const box = (await tip.boundingBox())!;
		const width = page.viewportSize()!.width;
		expect(box.x).toBeGreaterThanOrEqual(0);
		expect(box.x + box.width).toBeLessThanOrEqual(width);
		await page.waitForTimeout(300);
	} },
	{ name: '4h-added-cc', doc: 'ccadded', fixture: 'reply4', thin: true },
	{ name: '4i-two-newer', doc: 'reply', fixture: 'newer2', thin: true, act: async (page) => { await expectNewer(page, '2 emails came after the one you’re replying to: Dana Whitfield, Marcus Obi.', 2); } },
	{ name: '4i2-one-newer', doc: 'reply', fixture: 'newer1', thin: true, act: async (page) => { await expectNewer(page, 'Marcus Obi wrote on Nov 5, after the email you’re replying to.', 1); } },
	{ name: '4i3-six-newer', doc: 'reply', fixture: 'newer6', thin: true, act: async (page) => { await expectNewer(page, '6 emails came after the one you’re replying to: Jordan Lee, Priya Raman, Marcus Obi, Dana Whitfield.', 6); } },
	{ name: '4d-quiet-only', doc: 'reply', fixture: 'quiet4', thin: true },
	{ name: '4e-bad-address', doc: 'badto', fixture: 'badto', thin: true, act: async (page) => { await expectBad(page, ['priya.raman'], 'priya.raman isn’t a full email address.'); } },
	{ name: '4e2-two-bad-addresses', doc: 'badto2', fixture: 'badto2', thin: true, act: async (page) => { await expectBad(page, ['priya.raman', 'dana@'], '2 entries aren’t full email addresses: priya.raman, dana@'); } },
	{ name: '4f-mid-edit', doc: 'reply', fixture: 'reply4', thin: true, act: async (page, ctx) => {
		// Caret at the end of the To line, a second address half typed. The autosave must write the header line to the file.
		const toLine = page.locator('.cm-line.cm-email-head').filter({ hasText: 'To:' });
		await toLine.click();
		// A long address wraps on a phone while the line is being edited, and End stops at the end of a visual line: tap the end of the line's last visual line.
		await page.waitForTimeout(300);
		// The Cc button sits at the right end of the To line, so click inside the last visual line and press End.
		const box = (await toLine.boundingBox())!;
		await page.mouse.click(box.x + 40, box.y + box.height - 4);
		await page.keyboard.press('End');
		await page.keyboard.type(', priya.raman@exam', { delay: 30 });
		await expect.poll(async () => (await savedText(ctx)).includes('priya.raman@exam'), { timeout: 15_000 }).toBe(true);
		const saved = await savedText(ctx);
		expect(saved).toContain('To: Dana Whitfield <dana.whitfield@example.com>, priya.raman@exam');
		fs.writeFileSync(path.join(OUT, `${PHASE}-4f-mid-edit-saved-header.txt`), saved.split('\n\n')[0] + '\n');
		await page.waitForTimeout(300);
	} },
	{ name: '5-new-email', doc: 'fresh', fixture: 'new0' },
	{ name: '5b-comment', doc: 'reply', fixture: 'comment4', thin: true, act: async (page) => { await openRow(0)(page); await scrollToSection(page); } },
	{ name: '5c-attachments', doc: 'reply', fixture: 'attach', thin: true, act: async (page) => { await expect(chipFor(page, 'volunteer-waiver.pdf')).toBeVisible(); await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' })); } },
	{ name: '5d-attachment-image-opened', doc: 'reply', fixture: 'attach', thin: true, act: async (page) => {
		await chipFor(page, 'north-trailhead-map.png').getByRole('button').first().click();
		await page.waitForTimeout(1500);
	} },
	// Round 7: the canvas's PDF view, in the browser's own PDF viewer (full Chromium: the headless shell has none). The bytes are mocked; see `mockPdfRoutes`.
	{ name: '5e-attachment-pdf-opened', doc: 'reply', fixture: 'attach', thin: true, fullChromium: true, act: async (page) => {
		await mockPdfRoutes(page, 'valid');
		await chipFor(page, 'volunteer-waiver.pdf').getByRole('button').first().click();
		await expect(page.locator('iframe[title="volunteer-waiver.pdf"]')).toBeVisible({ timeout: 15_000 });
		await page.waitForTimeout(2500);
	} },
	// Opened from the draft's attachment chip, with the draft's tab still open: Back to draft selects that tab and closes this one.
	{ name: '5e2-attachment-pdf-failed', doc: 'reply', fixture: 'attach', thin: true, act: async (page) => {
		await mockPdfRoutes(page, 'broken');
		await chipFor(page, 'volunteer-waiver.pdf').getByRole('button').first().click();
		const failure = page.getByRole('alert').filter({ hasText: 'Couldn’t show this PDF.' });
		await expect(failure).toBeVisible({ timeout: 15_000 });
		await expect(failure).toContainText('Go back to the draft, where you can remove the attachment.');
		await expect(failure.getByRole('button', { name: 'Back to draft', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0);
		await page.waitForTimeout(400);
	} },
	// Back to draft selects the draft tab the file was opened from and closes the PDF tab; the draft is where its chips are.
	{ name: '5e4-pdf-back-to-draft', doc: 'reply', fixture: 'attach', thin: true, act: async (page) => {
		await mockPdfRoutes(page, 'broken');
		await chipFor(page, 'volunteer-waiver.pdf').getByRole('button').first().click();
		const failure = page.getByRole('alert').filter({ hasText: 'Couldn’t show this PDF.' });
		await expect(failure).toBeVisible({ timeout: 15_000 });
		await failure.getByRole('button', { name: 'Back to draft', exact: true }).click();
		await expect(page.locator('.email-attachments')).toBeVisible({ timeout: 10_000 });
		await expect(page.getByRole('alert').filter({ hasText: 'Couldn’t show this PDF.' })).toHaveCount(0);
		await expect(page.locator('[role="tab"]').filter({ hasText: 'volunteer-waiver.pdf' })).toHaveCount(0);
		await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await page.waitForTimeout(400);
	} },
	// Opened any other way (here: the canvas restored with the PDF active, no chip involved): the same failure says Close and does not mention a draft.
	{ name: '5e3-pdf-failed-generic', doc: 'reply', fixture: 'attach', thin: true, pdfByPath: 'broken', act: async (page) => {
		const failure = page.getByRole('alert').filter({ hasText: 'Couldn’t show this PDF.' });
		await expect(failure).toBeVisible({ timeout: 15_000 });
		await expect(failure).not.toContainText('draft');
		await expect(failure.getByRole('button', { name: 'Close', exact: true })).toBeVisible();
		await expect(failure.getByRole('button', { name: 'Back to draft' })).toHaveCount(0);
		await page.waitForTimeout(400);
	} },
	{ name: '5f-files-row', doc: 'reply', fixture: 'reply4', thin: true, act: showFiles },
	{ name: '5g-send-bar', doc: 'reply', fixture: 'reply4', thin: true },
	{ name: '5h-sending-undo', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => { await clickBar('Send')(page); await expectUndoInBarAndHeaderClear(page); } },
	// Round 10b: a long header (four To recipients and a Cc) while Sending: every recipient stays readable and Undo is in the bar.
	{ name: '19d3-sending-long-header', doc: 'longhead', fixture: 'reply4', thin: true, act: async (page) => {
		await expect(page.locator('.cm-line.cm-email-head').filter({ hasText: 'Cc:' })).toHaveCount(1);
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		await expectUndoInBarAndHeaderClear(page);
		await expectAddressLinesWrap(page);
	} },
	// Round 11: the same long header editable (before Send) and read-only (after Sent): To and Cc wrap in every state.
	{ name: '19d4-long-header-draft', doc: 'longhead', fixture: 'reply4', thin: true, act: async (page) => {
		await expect(page.locator('.cm-line.cm-email-head').filter({ hasText: 'Cc:' })).toHaveCount(1);
		await expectAddressLinesWrap(page);
	} },
	{ name: '19d5-long-header-sent', doc: 'longhead', fixture: 'reply4', thin: true, act: async (page) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent', { timeout: 12_000 });
		await expectAddressLinesWrap(page);
		expect((await writeEditControls(page, '19d5-long-header-sent')).ccButtons).toBe(0);
	} },
	{ name: '5i-dont-send-ask', doc: 'reply', fixture: 'reply4', thin: true, act: clickBar('Don’t send') },
	{ name: '5j-declined', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => {
		await clickBar('Don’t send')(page); await clickBar('Don’t send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Not sent');
		expect((await writeEditControls(page, '5j-declined')).ccButtons).toBe(0);
	} },
	// Round 7: the canvas toolbar on an email: comments, the delivery setting and Request review, worded for an email. The words are also written beside the frame.
	{ name: '22a-toolbar-draft', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => {
		await expect(page.locator('.delivery-toggle')).toContainText('Send comments right away');
		await expect(page.getByRole('button', { name: 'Request review' })).toBeVisible();
		await writeToolbar(page, '22a-toolbar-draft');
	} },
	{ name: '22b-toolbar-sent', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent', { timeout: 12_000 });
		await writeToolbar(page, '22b-toolbar-sent');
	} },
	// Round 4: states that were not drawn in round 3.
	{ name: '13-send-failed', doc: 'reply', fixture: 'sendFails', thin: true, act: async (page) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar .bar-text.failed')).toBeVisible({ timeout: 12_000 });
		await expect(page.locator('.email-send-bar').getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
		await expect(page.locator('.email-send-bar').getByRole('button', { name: 'Try again' })).toHaveCount(0);
		await page.waitForTimeout(300);
	} },
	{ name: '14-attachment-unavailable', doc: 'reply', fixture: 'attachMissing', thin: true, act: async (page) => {
		await expect(chipFor(page, 'volunteer-waiver.pdf')).toBeVisible();
		await expect(page.locator('.email-send-bar')).toContainText('volunteer-waiver.pdf is unavailable');
		await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
	} },
	{ name: '15-send-read-only', doc: 'reply', fixture: 'readOnly', thin: true, act: async (page) => {
		await expect(page.locator('.email-send-bar')).toContainText('Read-only');
		await revealChipsKeepingHeader(page);
		const facts = await writeEditControls(page, '15-send-read-only');
		expect(facts.ccButtons + facts.removeMarks).toBe(0);
	} },
	// The fixture holds the document connection down: `context.setOffline` did not reach the bar's `connected` signal in 40 s.
	{ name: '16-send-offline', doc: 'reply', fixture: 'offline', thin: true, act: async (page) => {
		await expect(page.locator('.email-send-bar')).toContainText('Offline');
		await page.waitForTimeout(300);
	} },
	{ name: '17-attachments-long', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await expect(page.locator('.email-attachments .attachment')).toHaveCount(3);
		await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await page.waitForTimeout(300);
		const facts = await chipFacts(page, '17-attachments-long');
		expect(facts.noWrap).toBe(true);
		expect(facts.allChipHeightsEqual).toBe(true);
		expect(facts.allInsideList).toBe(true);
		expect(facts.endsIntact).toBe(true);
		expect(facts.noGap).toBe(true);
	} },
	{ name: '18-attachments-many', doc: 'reply', fixture: 'attachMany', thin: true, act: async (page) => {
		await expect(page.locator('.email-attachments .attachment')).toHaveCount(9);
		await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
	} },
	// Round 5, E7: the review item the Needs-you list holds for a draft an agent asked the person to review.
	{ name: '19a-review-entry', doc: 'reply', fixture: 'review4', thin: true, chatOnly: true, review: true, act: async (page) => {
		await expect(page.locator('[data-attention-dock] .dock-bar')).toBeVisible({ timeout: 15_000 });
	} },
	{ name: '19b-review-expanded', doc: 'reply', fixture: 'review4', thin: true, chatOnly: true, review: true, act: async (page) => {
		await openReviewItem(page);
		// The label as the page holds it, spaces and all; the capital letters in the frame are the stylesheet's.
		const eyebrow = await page.locator('.external-review .eyebrow').textContent();
		expect(eyebrow).toBe('Review · Email drafter (agent)');
		await expect(page.locator('.external-review').getByRole('button', { name: 'Open draft', exact: true })).toBeVisible();
		fs.writeFileSync(path.join(OUT, `${PHASE}-19b-review-expanded-${isPhone(page) ? '393' : '1440'}.eyebrow.json`), JSON.stringify({ eyebrow }));
	} },
	// "Open draft" hands the draft to the same Open call the Documents list uses (`ChatPanel.svelte`, `openArtifactAndShowThread`): the draft opens, the Documents list is never shown.
	{ name: '19c-review-opens-draft', doc: 'reply', fixture: 'review4', thin: true, chatOnly: true, review: true, act: async (page) => {
		await openReviewItem(page);
		await page.locator('.external-review').getByRole('button', { name: 'Open draft' }).click();
		await expect(page.locator('.cm-content')).toContainText('Alex Rivera', { timeout: 20_000 });
		await expect(page.getByPlaceholder(/Search docs/)).toHaveCount(0);
		await page.waitForTimeout(1200);
	} },
	// The draft is open with its Needs-you item: beside it on a desktop; on a phone the item sits in the chat, one tap away.
	{ name: '19d-review-beside-draft', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		// The ask is live from opening, before any Send: the canvas bar shows the request and no answer button.
		await expectCanvasAsk(page, SEND_REASON);
		if (isPhone(page)) await page.screenshot({ path: path.join(OUT, `${PHASE}-19d-review-beside-draft-draft-393-dark.png`), animations: 'disabled' });
		await expectDock(page);
		expect(ctx.review.holds.length).toBeGreaterThan(0);
		await expectDockTitle(page, SEND_REASON);
	} },
	// During the five seconds: the email is not sent yet, the ask is unchanged, and Undo can still end it.
	{ name: '19d2-review-ask-while-sending', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		await expectCanvasAsk(page, SEND_REASON);
		expect(ctx.review.clears).toHaveLength(0);
		await expectUndoInBarAndHeaderClear(page);
	} },
	{ name: '19e-review-revised', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		// The agent revises the draft: the same file gets new text. The review item is keyed by the draft, so after the queue is read again it is still one item.
		const revised = DOCS.reply.replace('Twelve of us can come Saturday', 'Fourteen of us can come Saturday');
		// The server refuses a write while the editor holds the file live, so the tab leaves it first; the agent's write lands once the live session ends.
		const url = page.url();
		await page.goto('about:blank');
		await expect.poll(async () => {
			const response = await ctx.request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: ctx.docPath, text: revised } });
			return response.ok();
		}, { timeout: 20_000, intervals: [500, 1000, 2000] }).toBe(true);
		const readsBefore = ctx.review.reads;
		await page.goto(url);
		if (isPhone(page)) {
			await page.waitForTimeout(800);
			if (!(await page.locator('.thread-item').first().isVisible())) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-item').first().click();
		await expect(page.locator('.cm-content')).toContainText('Fourteen of us can come Saturday', { timeout: 20_000 });
		await expect.poll(() => ctx.review.reads, { timeout: 10_000 }).toBeGreaterThan(readsBefore);
		if (isPhone(page)) await page.screenshot({ path: path.join(OUT, `${PHASE}-19e-review-revised-draft-393-dark.png`), animations: 'disabled' });
		await toChat(page);
		await expect(dockBar(page)).toHaveCount(1, { timeout: 15_000 });
		await page.waitForTimeout(500);
	} },
	{ name: '19f-review-sent', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent', { timeout: 12_000 });
		await expect.poll(() => ctx.review.clears.length, { timeout: 5000 }).toBe(1);
		expect(ctx.review.clears[0]).toMatchObject({ outcome: 'answer', verdict: 'approved', hold_ids: ['hold-1'] });
		await expectNoCanvasAsk(page); // the answered ask is gone from the server, so the canvas bar goes
		expect((await writeEditControls(page, '19f-review-sent')).ccButtons).toBe(0);
		if (isPhone(page)) await page.screenshot({ path: path.join(OUT, `${PHASE}-19f-review-sent-draft-393-dark.png`), animations: 'disabled' });
		await toChat(page);
		await expect(dockBar(page)).toHaveCount(0, { timeout: 10_000 });
		await page.waitForTimeout(600);
	} },
	{ name: '19g-review-not-sent', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Don’t send')(page);
		await clickBar('Don’t send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Not sent');
		await expect.poll(() => ctx.review.clears.length, { timeout: 5000 }).toBe(1);
		expect(ctx.review.clears[0]).toMatchObject({ outcome: 'answer', verdict: 'changes_requested', note: 'This email was marked not to send.', hold_ids: ['hold-1'] });
		await expectNoCanvasAsk(page); // the answered ask is gone from the server, so the canvas bar goes
		expect((await writeEditControls(page, '19g-review-not-sent')).ccButtons).toBe(0);
		if (isPhone(page)) await page.screenshot({ path: path.join(OUT, `${PHASE}-19g-review-not-sent-draft-393-dark.png`), animations: 'disabled' });
		await toChat(page);
		await expect(dockBar(page)).toHaveCount(0, { timeout: 10_000 });
		await page.waitForTimeout(600);
	} },
	// Round 7: the review answer is a second step after the email's own result. Each state keeps the email Sent.
	{ name: '19i-review-answer-pending', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewMode: 'pending', act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent. Updating the review…', { timeout: 12_000 });
		await expectCanvasAsk(page, SEND_REASON);
		expect((await writeEditControls(page, '19i-review-answer-pending')).barButtons).toEqual([]);
	} },
	{ name: '19j-review-answer-failed', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewFailures: 1, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent. Couldn’t mark the review as answered.', { timeout: 12_000 });
		await expectCanvasAsk(page, SEND_REASON);
		expect((await writeEditControls(page, '19j-review-answer-failed')).barButtons).toEqual(['Mark as answered']);
	} },
	// Mark as answered retries only the answer: the email stays Sent, and the bar returns to plain Sent when it lands.
	{ name: '19j2-review-answer-retried', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewFailures: 1, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Couldn’t mark the review as answered', { timeout: 12_000 });
		await clickBar('Mark as answered')(page);
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent');
		expect(ctx.review.clears).toHaveLength(2);
		expect(ctx.review.clears[1]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-1'] });
		await expectNoCanvasAsk(page);
		expect((await writeEditControls(page, '19j2-review-answer-retried')).barButtons).toEqual([]);
	} },
	{ name: '19k-review-answer-ended', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewMode: 'stale', act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent. The review request has ended.', { timeout: 12_000 });
		await expectNoCanvasAsk(page);
		expect((await writeEditControls(page, '19k-review-answer-ended')).barButtons).toEqual([]);
		expect(ctx.review.holds).toHaveLength(0);
	} },
	// Round 9: a replacement never inherits the answer. The answer carries the holds known at the press; the replacement waits in the canvas's review bar with its own answers, and the dock and the canvas show the same request.
	{ name: '19k2-review-request-changed', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewMode: 'changed', act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent. The earlier review request ended. A new request is waiting in the review bar.', { timeout: 12_000 });
		await expectCanvasAsk(page, CORRECTED_REASON, { answers: true });
		expect((await writeEditControls(page, '19k2-review-request-changed')).barButtons).toEqual([]);
		expect(ctx.review.clears).toHaveLength(1);
		expect(ctx.review.clears[0]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-1'] });
		await chatAndDock(page, '19k2-review-request-changed', CORRECTED_REASON);
	} },
	// The replacement lands during the five seconds: the email is still Sending, the canvas already shows the new request with its own answers.
	{ name: '19k3-review-replaced-during-wait', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		ctx.review.replaceSend();
		await expectCanvasAsk(page, CORRECTED_REASON, { answers: true });
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		expect(ctx.review.clears).toHaveLength(0);
		// Round 10b: Undo is in the bar's own Sending row; no toast sits over the header or the request row.
		await expectUndoInBarAndHeaderClear(page);
	} },
	{ name: '19k4-review-replaced-after-wait', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		ctx.review.replaceSend();
		await expect(page.locator('.email-send-bar')).toContainText('Sent. The earlier review request ended. A new request is waiting in the review bar.', { timeout: 12_000 });
		await expectCanvasAsk(page, CORRECTED_REASON, { answers: true });
		expect((await writeEditControls(page, '19k4-review-replaced-after-wait')).barButtons).toEqual([]);
		// The answer carried the hold the person answered, once; the replacement was not answered with it.
		expect(ctx.review.clears).toHaveLength(1);
		expect(ctx.review.clears[0]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-1'] });
		expect(ctx.review.holds.map((h) => h.id)).toEqual(['hold-2']);
		await chatAndDock(page, '19k4-review-replaced-after-wait', CORRECTED_REASON);
	} },
	// An ordinary review request beside the send request: each is answered in its own home.
	{ name: '19m-review-ordinary-beside-send', doc: 'reply', fixture: 'review4', thin: true, review: true, ordinaryRequest: true, act: async (page, ctx) => {
		await expectCanvasAsk(page, ORDINARY_REASON, { who: 'Trail crew', answers: true });
		await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
		expect(ctx.review.clears).toHaveLength(0);
		await chatAndDock(page, '19m-review-ordinary-beside-send', ORDINARY_REASON);
	} },
	{ name: '19m2-review-send-answered', doc: 'reply', fixture: 'review4', thin: true, review: true, ordinaryRequest: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent', { timeout: 12_000 });
		await expect.poll(() => ctx.review.clears.length, { timeout: 5000 }).toBe(1);
		expect(ctx.review.clears[0]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-1'] });
		await expectCanvasAsk(page, ORDINARY_REASON, { who: 'Trail crew', answers: true });
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent');
		await chatAndDock(page, '19m2-review-send-answered', ORDINARY_REASON);
	} },
	{ name: '19m3-review-ordinary-answered', doc: 'reply', fixture: 'review4', thin: true, review: true, ordinaryRequest: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent', { timeout: 12_000 });
		await expectCanvasAsk(page, ORDINARY_REASON, { who: 'Trail crew', answers: true });
		await page.getByRole('button', { name: 'Approve', exact: true }).click();
		await expect.poll(() => ctx.review.clears.length, { timeout: 8000 }).toBe(2);
		expect(ctx.review.clears[1]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-ord'] });
		await expect(page.locator('.review-bar.receipt')).toBeVisible({ timeout: 8000 });
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent');
		if (isPhone(page)) await page.screenshot({ path: path.join(OUT, `${PHASE}-19m3-review-ordinary-answered-draft-393-dark.png`), animations: 'disabled' });
		await toChat(page);
		await expect(dockBar(page)).toHaveCount(0, { timeout: 10_000 });
		await page.waitForTimeout(500);
	} },
	// Round 10: the bar did not know the draft's requests when the person pressed Send (the read failed). It answers nothing, the email still ends Sent,
	// the bar says the request wasn't answered, and the canvas's review bar shows the send request with its answers only once the email has ended.
	{ name: '19n-review-read-not-known', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewReadFails: true, act: async (page, ctx) => {
		await expectNoCanvasAsk(page); // the canvas could not read it either
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		ctx.review.readFails = false; // the read works again, but a read after the press is not a snapshot of what was open at the press
		ctx.review.nudge();
		await expectCanvasAsk(page, SEND_REASON); // still Sending: the request shows, its answers are Send and Don’t send
		await expect(page.locator('.email-send-bar')).toContainText('Sending…');
		await expect(page.locator('.email-send-bar')).toContainText('Sent. The review request wasn’t answered. Answer it in the review bar.', { timeout: 12_000 });
		expect(ctx.review.clears).toHaveLength(0); // no answer was posted: the holds were not known at the press
		await expect(page.locator('.email-send-bar button')).toHaveCount(0);
		await expectCanvasAsk(page, SEND_REASON, { answers: true }); // the email has ended: nothing else can answer it
		expect((await writeEditControls(page, '19n-review-read-not-known')).barButtons).toEqual([]);
		expect(ctx.review.clears).toHaveLength(0);
	} },
	{ name: '19n2-review-read-not-known-answered', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewReadFails: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		ctx.review.readFails = false;
		ctx.review.nudge();
		await expect(page.locator('.email-send-bar')).toContainText('Sent. The review request wasn’t answered. Answer it in the review bar.', { timeout: 12_000 });
		await expectCanvasAsk(page, SEND_REASON, { answers: true });
		await page.getByRole('button', { name: 'Approve', exact: true }).click();
		await expect.poll(() => ctx.review.clears.length, { timeout: 8000 }).toBe(1);
		expect(ctx.review.clears[0]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-1'] });
		await expect(page.locator('.review-bar.receipt')).toBeVisible({ timeout: 8000 });
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent'); // the request is gone, so the sentence goes; the email stays Sent
		expect(ctx.review.holds).toHaveLength(0);
		await page.waitForTimeout(300);
	} },
	// Round 11, reworded in round 12: the read of the draft's requests never comes back. The email still ends Sent; the send bar says the request wasn't answered and does not point at a review bar that shows nothing to answer.
	// The canvas's review bar says the request couldn't be loaded and offers Retry, which only re-reads. Nothing is answered, and nothing is sent again.
	{ name: '19p-review-read-still-failed', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewReadFails: true, act: async (page, ctx) => {
		await expectNoCanvasAsk(page);
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText(NOT_ANSWERED, { timeout: 12_000 });
		const failed = page.locator('.review-bar.unreadable');
		await expect(failed).toBeVisible();
		await expect(failed).toContainText('Couldn’t load the review request.');
		await expect(failed.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
		await expect(failed.getByRole('button', { name: 'Try again' })).toHaveCount(0);
		await expect(page.locator('.email-send-bar')).not.toContainText('Answer it in the review bar');
		for (const name of ANSWERS) await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
		await expect(page.locator('.email-send-bar button')).toHaveCount(0);
		expect(ctx.review.clears).toHaveLength(0);
		fs.writeFileSync(path.join(OUT, `${PHASE}-19p-review-read-still-failed-${isPhone(page) ? '393-dark' : '1440-light'}.sendbar.json`), JSON.stringify({ sendBarText: await page.locator('.email-send-bar .bar-text').innerText(), pointsAtReviewBar: false, buttonLabel: await failed.getByRole('button').innerText() }));
		await page.waitForTimeout(300);
	} },
	// Retry while the read still fails changes nothing but reads once more; then the read works and the request shows with its own answers, and only then does the send bar point at the review bar. No answer is posted by either press.
	{ name: '19p2-review-read-retried', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewReadFails: true, act: async (page, ctx) => {
		await retriedReadWorks(page, ctx);
		expect((await writeEditControls(page, '19p2-review-read-retried')).barButtons).toEqual([]);
		await checkCollapsedAsk(page, '19p2-review-read-retried', 'Dana Whitfield');
		await page.waitForTimeout(300);
	} },
	// Round 12: the same request opened with its own tap (the existing chevron row): the full title and the answers.
	{ name: '19p3-review-read-retried-expanded', doc: 'reply', fixture: 'review4', thin: true, review: true, reviewReadFails: true, act: async (page, ctx) => {
		await retriedReadWorks(page, ctx);
		await checkCollapsedAsk(page, '19p3-review-read-retried-expanded-collapsed', 'Dana Whitfield');
		const line = page.locator('.review-bar.ask .rb-line');
		await expect(line).toHaveAttribute('aria-expanded', 'false');
		await line.click();
		await expect(line).toHaveAttribute('aria-expanded', 'true');
		await expect(page.locator('.review-bar.ask .rb-ask-full')).toHaveText(SEND_REASON);
		await expect(page.locator('.review-bar.ask .rb-ask-full')).toBeVisible();
		for (const name of ANSWERS) await expect(page.getByRole('button', { name, exact: true })).toHaveCount(1);
		expect(ctx.review.clears).toHaveLength(0); // opening it answers nothing
		await page.waitForTimeout(400);
	} },
	// A send-purpose request arriving after the email is Sent: nothing is left to answer it but the canvas's review bar.
	{ name: '19o-send-request-after-sent', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent', { timeout: 12_000 });
		await expect.poll(() => ctx.review.clears.length, { timeout: 5000 }).toBe(1);
		await expectNoCanvasAsk(page);
		ctx.review.addSend();
		await expectCanvasAsk(page, CORRECTED_REASON, { answers: true });
		expect(ctx.review.clears).toHaveLength(1);
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent');
		expect((await writeEditControls(page, '19o-send-request-after-sent')).barButtons).toEqual([]);
	} },
	{ name: '19o2-send-request-after-sent-answered', doc: 'reply', fixture: 'review4', thin: true, review: true, act: async (page, ctx) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent', { timeout: 12_000 });
		await expect.poll(() => ctx.review.clears.length, { timeout: 5000 }).toBe(1);
		ctx.review.addSend();
		await expectCanvasAsk(page, CORRECTED_REASON, { answers: true });
		await page.getByRole('button', { name: 'Approve', exact: true }).click();
		await expect.poll(() => ctx.review.clears.length, { timeout: 8000 }).toBe(2);
		expect(ctx.review.clears[1]).toMatchObject({ verdict: 'approved', hold_ids: ['hold-3'] });
		await expect(page.locator('.review-bar.receipt')).toBeVisible({ timeout: 8000 });
		await expect(page.locator('.email-send-bar .bar-text')).toHaveText('Sent');
		expect(ctx.review.holds).toHaveLength(0);
		await page.waitForTimeout(300);
	} },
	// Round 6: a sent email that carries files: the chips stay, their remove marks and the Cc control are gone.
	{ name: '19h-sent-with-files', doc: 'reply', fixture: 'attach', thin: true, act: async (page) => {
		await clickBar('Send')(page);
		await expect(page.locator('.email-send-bar')).toContainText('Sent', { timeout: 12_000 });
		await revealChipsKeepingHeader(page);
		const facts = await writeEditControls(page, '19h-sent-with-files');
		expect(facts.chips).toBe(2);
		expect(facts.ccButtons + facts.removeMarks).toBe(0);
		await page.waitForTimeout(300);
	} },
	// Round 5: the Cc button at the end of the To line.
	{ name: '20a-cc-before', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => {
		await expect(page.locator('.cm-email-cc-add')).toBeVisible();
	} },
	{ name: '20b-cc-pressed', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => {
		await page.locator('.cm-email-cc-add').click();
		await expect(page.locator('.cm-line.cm-email-head').filter({ hasText: 'Cc:' })).toHaveCount(1);
		// The Cc line is the open field: outlined, with its hint, the Cc button gone, and the caret in it (a headless still shows no caret, so the focus is read from the page).
		await expect(page.locator('.cm-line.cm-email-head-empty .cm-email-address-hint')).toBeVisible();
		await expect(page.locator('.cm-email-cc-add')).toHaveCount(0);
		await expect(page.locator('.cm-content')).toBeFocused();
		const caret = await page.evaluate(() => {
			const sel = getSelection();
			const line = sel?.anchorNode ? (sel.anchorNode.nodeType === 1 ? sel.anchorNode as Element : sel.anchorNode.parentElement)?.closest('.cm-line') : null;
			return { caretLine: line?.textContent ?? null, collapsed: sel?.isCollapsed ?? null };
		});
		expect(caret.caretLine).toContain('Cc:');
		fs.writeFileSync(path.join(OUT, `${PHASE}-20b-cc-pressed-${isPhone(page) ? '393' : '1440'}.facts.json`), JSON.stringify({ editorFocused: true, ...caret, ccButtons: 0, hint: 'name@example.com' }));
		await page.waitForTimeout(400);
	} },
	{ name: '20c-cc-filled', doc: 'reply', fixture: 'reply4', thin: true, act: async (page, ctx) => {
		await page.locator('.cm-email-cc-add').click();
		await page.keyboard.type('priya.raman@example.com', { delay: 25 });
		await expect(page.locator('.cm-email-cc-add')).toHaveCount(0);
		await expect.poll(async () => (await savedText(ctx)).includes('Cc: priya.raman@example.com'), { timeout: 15_000 }).toBe(true);
		await page.waitForTimeout(400);
	} },
	// Round 5: a long attachment name. A desktop pointer gets the native title; a touch screen taps to open the file and the tab names it in full.
	{ name: '21-long-chip-name', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		const phone = isPhone(page);
		const chip = page.locator('.email-attachments .attachment').first();
		const fullName = 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf';
		// The draft from the top, so the From/To/Subject block is in the frame; the chip is brought up only when it is below the fold.
		await page.locator('.email-scroll').evaluate((el) => { el.scrollTop = 0; });
		const visible = () => chip.evaluate((el) => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; });
		if (!(await visible())) await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'end' }));
		await page.waitForTimeout(300);
		const titled = await chip.locator('[title]').evaluateAll((els) => els.map((el) => el.getAttribute('title')));
		expect(titled).toContain(fullName);
		await expect(chip.locator('.tile.chip')).toHaveAttribute('aria-label', `${fullName}, document, 3.0 MB`);
		const headerVisible = await page.evaluate(() => { const l = Array.from(document.querySelectorAll('.cm-line.cm-email-head')).find((el) => /^To:/.test(el.textContent ?? '')); if (!l) return false; const r = l.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; });
		const facts = await chipFacts(page, '21-long-chip-name');
		// One line high at every width, touch included; the start gives way with an ellipsis and the extension stays.
		expect(facts.noWrap).toBe(true);
		expect(facts.allChipHeightsEqual).toBe(true);
		expect(facts.chips[0].startCutByEllipsis).toBe(true);
		expect(facts.chips[0].shownEnd).toBe('final-v7.pdf');
		expect(facts.endsIntact).toBe(true);
		expect(facts.noGap).toBe(true);
		expect(facts.onePill).toBe(true);
		expect(facts.chips[0].sizeOnSameLine).toBe(true);
		expect(facts.chips[0].removeOnSameLine).toBe(true);
		fs.writeFileSync(path.join(OUT, `${PHASE}-21-long-chip-name-${phone ? '393' : '1440'}.titles.json`), JSON.stringify({ titled, headerVisible, chipVisible: await visible() }));
		if (!phone) { await chip.getByRole('button').first().hover(); await page.waitForTimeout(1500); }
	} },
	// Round 13: one name with no separator and no extension. It is cut near the end with the last characters kept, on one line.
	{ name: '21c-unbroken-chip-name', doc: 'reply', fixture: 'attachUnbroken', thin: true, act: async (page) => {
		await expect(page.locator('.email-attachments .attachment')).toHaveCount(2);
		await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await page.waitForTimeout(300);
		const facts = await chipFacts(page, '21c-unbroken-chip-name');
		expect(facts.noWrap).toBe(true);
		expect(facts.allChipHeightsEqual).toBe(true);
		expect(facts.endsIntact).toBe(true);
		expect(facts.noGap).toBe(true);
		expect(facts.chips[0].startCutByEllipsis).toBe(true);
		expect(facts.chips[0].shownEnd).toBe('approvedbycrew'.slice(-8));
		expect(facts.chips[0].fullName).toBe('northtrailheadlotandchurchlotparkingcomparisonwithshuttlerouteandcountyvanlimitsfinalversionsevenapprovedbycrew');
		expect(facts.onePill).toBe(true);
	} },
	// Round 13b: the long chip opened. The draft never repeats the name; the opened tab, the PDF frame and (on a phone) the app bar carry it in full.
	{ name: '21b-long-chip-opened', doc: 'reply', fixture: 'attachLong', thin: true, fullChromium: true, act: async (page) => {
		const phone = isPhone(page);
		const fullName = 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf';
		await mockPdfRoutes(page, 'valid');
		await revealChipsKeepingHeader(page);
		// The chip text is shortened in the middle, so the chip is found by its title (the full name), not by its visible text.
		await page.locator(`.email-attachments .attachment [title="${fullName}"]`).first().click();
		await expect(page.locator(`iframe[title="${fullName}"]`)).toBeVisible({ timeout: 15_000 });
		await page.waitForTimeout(2500);
		fs.writeFileSync(path.join(OUT, `${PHASE}-21b-long-chip-opened-${phone ? '393' : '1440'}.titles.json`), JSON.stringify({
			iframeTitle: await page.locator('iframe').first().getAttribute('title'),
			tabTitles: await page.locator('[role="tab"]').evaluateAll((els) => els.map((el) => ({ text: (el.textContent ?? '').trim(), title: el.getAttribute('title') }))),
			appBarTitle: phone ? await page.locator('.h-scope').first().innerText() : null,
		}));
	} },
	// Round 13b: the chip's states with two long names. A pointer's hover has no touch counterpart (the phone frame is the rest state, and the file says so); keyboard focus and a press do.
	{ name: '21d-chip-hover', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await revealChipsKeepingHeader(page);
		const chip = page.locator('.email-attachments .attachment').first().locator('.tile.chip');
		// The pill carries the fill, so the hover tint is read from the pill.
		const bg = () => chip.locator('xpath=..').evaluate((el) => getComputedStyle(el).backgroundColor);
		const before = await bg();
		await chip.hover();
		await page.waitForTimeout(400);
		const after = await bg();
		const hoverCapable = await page.evaluate(() => matchMedia('(hover: hover)').matches);
		expect(after !== before).toBe(hoverCapable);
		fs.writeFileSync(path.join(OUT, `${PHASE}-21d-chip-hover-${isPhone(page) ? '393' : '1440'}.state.json`), JSON.stringify({ hoverCapable, before, after }));
		const facts = await chipFacts(page, '21d-chip-hover');
		expect(facts.noWrap && facts.noGap && facts.endsIntact).toBe(true);
	} },
	{ name: '21d2-remove-hover', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await revealChipsKeepingHeader(page);
		const remove = page.locator('.email-attachments .attachment-remove').first();
		const bg = () => remove.evaluate((el) => getComputedStyle(el).backgroundColor);
		const before = await bg();
		await remove.hover();
		await page.waitForTimeout(400);
		const after = await bg();
		const hoverCapable = await page.evaluate(() => matchMedia('(hover: hover)').matches);
		expect(after !== before).toBe(hoverCapable);
		fs.writeFileSync(path.join(OUT, `${PHASE}-21d2-remove-hover-${isPhone(page) ? '393' : '1440'}.state.json`), JSON.stringify({ hoverCapable, before, after }));
	} },
	{ name: '21e-chip-focus', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await revealChipsKeepingHeader(page);
		const pill = page.locator('.email-attachments .attachment').first();
		const chip = pill.locator('.tile.chip');
		await page.keyboard.press('Shift');
		await chip.focus();
		expect(await chip.evaluate((el) => el.matches(':focus-visible'))).toBe(true);
		// The ring is drawn around the whole pill (icon, name, size and mark), not around the chip alone.
		const outline = await pill.evaluate((el) => { const c = getComputedStyle(el); return { style: c.outlineStyle, width: c.outlineWidth, color: c.outlineColor }; });
		expect(outline.style).not.toBe('none');
		expect(await chip.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('none');
		fs.writeFileSync(path.join(OUT, `${PHASE}-21e-chip-focus-${isPhone(page) ? '393' : '1440'}.state.json`), JSON.stringify({ outline }));
		await page.waitForTimeout(300);
		const facts = await chipFacts(page, '21e-chip-focus');
		expect(facts.noWrap && facts.noGap && facts.endsIntact).toBe(true);
	} },
	{ name: '21e2-remove-focus', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await revealChipsKeepingHeader(page);
		const remove = page.locator('.email-attachments .attachment-remove').first();
		await page.keyboard.press('Shift');
		await remove.focus();
		expect(await remove.evaluate((el) => el.matches(':focus-visible'))).toBe(true);
		const outline = await remove.evaluate((el) => { const c = getComputedStyle(el); return { style: c.outlineStyle, width: c.outlineWidth, color: c.outlineColor }; });
		expect(outline.style).not.toBe('none');
		fs.writeFileSync(path.join(OUT, `${PHASE}-21e2-remove-focus-${isPhone(page) ? '393' : '1440'}.state.json`), JSON.stringify({ outline }));
		await page.waitForTimeout(300);
	} },
	// The chip has no style of its own for a press beyond hover and focus (`AttachmentRow.svelte`); the frame holds the pointer down on the chip and the file records :active.
	{ name: '21f-chip-press', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await revealChipsKeepingHeader(page);
		const chip = page.locator('.email-attachments .attachment').first().locator('.tile.chip');
		const box = (await chip.boundingBox())!;
		await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
		await page.mouse.down();
		await page.waitForTimeout(300);
		const active = await chip.evaluate((el) => el.matches(':active'));
		expect(active).toBe(true);
		fs.writeFileSync(path.join(OUT, `${PHASE}-21f-chip-press-${isPhone(page) ? '393' : '1440'}.state.json`), JSON.stringify({ active, background: await chip.locator('xpath=..').evaluate((el) => getComputedStyle(el).backgroundColor) }));
		const facts = await chipFacts(page, '21f-chip-press');
		expect(facts.noWrap && facts.noGap && facts.endsIntact).toBe(true);
	} },
	// Remove the first long chip: the other two stay, each on one line.
	{ name: '21g-chip-removed', doc: 'reply', fixture: 'attachLong', thin: true, act: async (page) => {
		await revealChipsKeepingHeader(page);
		const first = page.getByRole('button', { name: /^Remove north-trailhead-lot/ });
		if (isPhone(page)) { const b = (await first.boundingBox())!; await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2); } else await first.click();
		await expect(page.locator('.email-attachments .attachment')).toHaveCount(2, { timeout: 5000 });
		await page.waitForTimeout(800);
		const facts = await chipFacts(page, '21g-chip-removed');
		expect(facts.chips[0].fullName).toContain('drainage_cut');
		expect(facts.noWrap && facts.noGap && facts.endsIntact && facts.allChipHeightsEqual).toBe(true);
	} },
	{ name: '6-long', doc: 'reply', fixture: 'long12', act: scrollToSection },
	{ name: '7-one-email', doc: 'reply', fixture: 'one1', act: scrollEnd },
	{ name: '8-read-error', doc: 'reply', fixture: 'error', alone: true, act: scrollEnd },
	{ name: '8b-error-retried', doc: 'reply', fixture: 'error', thin: true, act: async (page) => {
		await page.getByRole('button', { name: 'Try again' }).click();
		await expect(page.locator('.email-toggle')).toHaveCount(4);
		await page.waitForTimeout(300);
		await scrollEnd(page);
	} },
	{ name: '9-long-email', doc: 'reply', fixture: 'longmail', alone: true, act: async (page) => { await openRow(0)(page); await scrollToSection(page); } },
	{ name: '9b-long-email-middle', doc: 'reply', fixture: 'longmail', act: async (page) => {
		await openRow(0)(page);
		await page.locator('.email-text').first().evaluate((el) => el.scrollIntoView({ block: 'center' }));
	} },
	{ name: '10-loading', doc: 'reply', fixture: 'loading', thin: true, act: scrollEnd },
	{ name: '11-partial', doc: 'reply', fixture: 'partial', thin: true, act: scrollEnd },
	{ name: '12-focus-keyboard', doc: 'reply', fixture: 'reply4', thin: true, act: async (page) => {
		const row = page.locator('.email-toggle').nth(1);
		await page.keyboard.press('Shift');
		await row.focus();
		expect(await row.evaluate((el) => el.matches(':focus-visible'))).toBe(true);
		// Enter and Space each open and close the focused row.
		for (const key of ['Enter', 'Enter', 'Space', 'Space']) {
			const before = await row.getAttribute('aria-expanded');
			await page.keyboard.press(key);
			await expect(row).toHaveAttribute('aria-expanded', before === 'true' ? 'false' : 'true');
			await page.waitForTimeout(300);
		}
		await scrollEnd(page);
	} },
];

/** A flat-colour PNG with a lighter band, built with node's zlib; it stands in for the attached map. */
function placeholderPng(width: number, height: number): Buffer {
	const crcTable = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
	const crc = (buf: Buffer) => { let c = 0xffffffff; for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
	const chunk = (type: string, data: Buffer) => {
		const body = Buffer.concat([Buffer.from(type), data]);
		const length = Buffer.alloc(4); length.writeUInt32BE(data.length);
		const sum = Buffer.alloc(4); sum.writeUInt32BE(crc(body));
		return Buffer.concat([length, body, sum]);
	};
	const header = Buffer.alloc(13); header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
	const rows: Buffer[] = [];
	for (let y = 0; y < height; y++) {
		const row = Buffer.alloc(1 + width * 3);
		for (let x = 0; x < width; x++) {
			const band = Math.abs(y - (height / 2 + 60 * Math.sin(x / 70))) < 14;
			row.set(band ? [214, 196, 150] : [74, 112, 82], 1 + x * 3);
		}
		rows.push(row);
	}
	return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(Buffer.concat(rows))), chunk('IEND', Buffer.alloc(0))]);
}

// A PDF the way real ones are written: a binary marker comment, and a compressed stream with NUL bytes in it.
const PLACEHOLDER_PDF = Buffer.concat([
	Buffer.from('%PDF-1.4\n%'), Buffer.from([0xe2, 0xe3, 0xcf, 0xd3]),
	Buffer.from('\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n'
		+ '3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj\n4 0 obj << /Length 24 /Filter /FlateDecode >> stream\n'),
	Buffer.from([0x78, 0x9c, 0x4b, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01]),
	Buffer.from('\nendstream endobj\ntrailer << /Root 1 0 R /Size 5 >>\n%%EOF\n'),
]);

// A small valid PDF: the canvas's PDF view checks the bytes start with %PDF- before it shows them.
const validPdf = (heading: string, line: string) => {
	const objects = [
		'<< /Type /Catalog /Pages 2 0 R >>',
		'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
		'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
		'',
		'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
	];
	const stream = `BT /F1 22 Tf 72 700 Td (${heading}) Tj 0 -32 Td /F1 12 Tf (${line}) Tj ET`;
	objects[3] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
	let out = '%PDF-1.4\n';
	const offsets: number[] = [];
	objects.forEach((body, i) => { offsets.push(out.length); out += `${i + 1} 0 obj\n${body}\nendobj\n`; });
	const xref = out.length;
	out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n \n`).join('')}`;
	out += `trailer << /Root 1 0 R /Size ${objects.length + 1} >>\nstartxref\n${xref}\n%%EOF\n`;
	return Buffer.from(out, 'latin1');
};
// Each file's page says what its name says (a page titled for another file would read as the wrong file).
const VALID_PDF = validPdf('Volunteer waiver', 'North Trailhead work day, Saturday November 14.');
const PDF_PAGES: Record<string, Buffer> = {
	'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf':
		validPdf('North trailhead lot and church lot parking', 'Shuttle route and county van limits compared, final version 7.'),
};

/** The backend's PDF answers, mocked (prototype: the real route does not serve PDF bytes yet). A text open of a .pdf answers the typed 415; the raw read answers the bytes, or something that is not a PDF. */
const mockPdfRoutes = async (page: Page, bytes: 'valid' | 'broken') => {
	await page.route((url) => url.pathname === '/api/files/content' && (url.searchParams.get('path') ?? '').endsWith('.pdf'), async (route) => {
		const raw = new URL(route.request().url()).searchParams.get('raw') === 'true';
		if (!raw) return route.fulfill({ status: 415, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'pdf_file', message: 'This file is a PDF.' } }) });
		if (bytes === 'broken') return route.fulfill({ status: 200, contentType: 'application/pdf', body: 'The server sent text, not a PDF.' });
		const file = decodeURIComponent((new URL(route.request().url()).searchParams.get('path') ?? '').split('/').pop() ?? '');
		return route.fulfill({ status: 200, contentType: 'application/pdf', body: PDF_PAGES[file] ?? VALID_PDF });
	});
};

const REALISTIC_CHAT_TITLE = 'Reply to Dana about Saturday';
const FRAME_MODEL_NAME = 'Sonnet 5.5';

const COMPARE: Scenario[] = [
	{ name: 'cmp-chat', doc: 'reply', fixture: null, chatOnly: true },
	{ name: 'cmp-doc', doc: 'plain', fixture: null },
];

async function seedAndOpen(
	request: APIRequestContext, browser: Browser, testInfo: TestInfo, scenario: Scenario,
	workspace: string, stamp: number, vp: typeof VIEWPORTS[number], theme: typeof THEMES[number],
	contextOptions: Record<string, unknown> = {},
	/** A still for a person to judge: a chat titled as a person would title it and the product's own model name, not the harness's. */
	realistic = false,
) {
	const variant = `${vp.name}-${theme}-${contextOptions.recordVideo ? 'rec' : 'still'}`;
	const title = realistic ? REALISTIC_CHAT_TITLE : `email-view-${scenario.name}-${variant}-${stamp}`;
	const threadId = await createSession(request, title);
	// The draft tools write email drafts into an `email-drafts` folder; the envelope icon reads that folder.
	// The draft tools name a draft email-<12 hex>.md (`backend/tools/reviewed_drafts.py`); a hash of the run keeps every frame's file apart.
	const name = `email-${createHash('md5').update(`${scenario.name}-${variant}-${stamp}`).digest('hex').slice(0, 12)}.md`, docPath = path.join(workspace, scenario.doc === 'plain' ? '' : 'email-drafts', name);
	// The attachment files the `attach` fixture names: a generated placeholder PNG and a one-page PDF.
	const files = path.join(workspace, 'email-drafts', `files-${scenario.name}-${variant}-${stamp}`);
	if (scenario.fixture === 'attach' || scenario.fixture === 'attachLong' || scenario.fixture === 'attachUnbroken') {
		fs.mkdirSync(files, { recursive: true });
		if (scenario.fixture === 'attachLong') fs.writeFileSync(path.join(files, 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf'), PLACEHOLDER_PDF);
		fs.writeFileSync(path.join(files, 'north-trailhead-map.png'), placeholderPng(640, 400));
		fs.writeFileSync(path.join(files, 'volunteer-waiver.pdf'), PLACEHOLDER_PDF);
	}
	const seeded = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: DOCS[scenario.doc], thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id;
	await expectThreadStored(request, threadId, `${scenario.name} ${variant} after seeding`);
	const context = await browser.newContext({
		baseURL: testInfo.project.use.baseURL, viewport: vp.viewport, colorScheme: theme,
		deviceScaleFactor: vp.phone ? 2 : 1, isMobile: vp.phone, hasTouch: vp.phone, ...contextOptions,
	});
	const page = await context.newPage();
	if (realistic) {
		// The mock backend's one model row is named `Mock Test Model`; the page reads the catalog, so the frame shows the catalog name a person sees
		// (the same fixture rename as setup-redesign-evidence.spec.ts `presentFirstRun`; product code and the mock seed are untouched).
		await page.route((url) => url.pathname === '/api/models', async (route) => {
			const response = await route.fetch();
			const body = await response.json();
			const rows = (body.models ?? []).map((row: Record<string, unknown>) => row.litellm_model === 'mock/test-model' ? { ...row, name: FRAME_MODEL_NAME, display_name: FRAME_MODEL_NAME } : row);
			await route.fulfill({ response, json: { ...body, models: rows } });
		});
		// The chat's reply carries the same harness name in its stored metadata, which its footer shows.
		await page.route((url) => /^\/api\/threads\/[^/]+\/messages/.test(url.pathname), async (route) => {
			const response = await route.fetch();
			await route.fulfill({ response, body: (await response.text()).replaceAll('Mock Test Model', FRAME_MODEL_NAME) });
		});
	}
	const initialHolds: Hold[] = scenario.review ? [
		...(scenario.ordinaryRequest ? [{ id: 'hold-ord', asker: CREW, reason: ORDINARY_REASON }] : []),
		{ id: 'hold-1', asker: DRAFTER, reason: SEND_REASON, purpose: 'send' as const },
	] : [];
	const review: ReviewMock = { holds: initialHolds, reads: 0, readCalls: 0, clears: [], mode: scenario.reviewMode ?? 'ok', failuresLeft: scenario.reviewFailures ?? 0, replaceSend: () => {}, readFails: scenario.reviewReadFails ?? false, nudge: () => {}, addSend: () => {} };
	if (scenario.review) {
		// The app's live socket, passed through. An answered ask changes the plan, so the server nudges `plans_changed` and the canvas review bar reads again; the mock sends that nudge itself (as in person-step-focus-chip-render.spec.ts).
		let socket: WebSocketRoute | null = null;
		await page.routeWebSocket((url) => url.pathname === '/ws', (client) => {
			const server = client.connectToServer();
			socket = client;
			client.onMessage((payload) => server.send(payload));
			server.onMessage((payload) => client.send(payload));
		});
		const nudgePlansChanged = () => socket?.send(JSON.stringify({ type: 'plans_changed', contract_version: 2, plans: [{ plan_id: `plan-${threadId}`, revision: review.clears.length + review.holds.length + 1, owner: { kind: 'thread', id: threadId }, scope: { workspace_id: null, project_id: null } }] }));
		review.nudge = nudgePlansChanged;
		// A new send request arrives (its producer marked it `purpose: 'send'`), for example after the first one was answered and the email is Sent.
		review.addSend = () => { review.holds = [{ id: 'hold-3', asker: DRAFTER, reason: CORRECTED_REASON, purpose: 'send' as const }, ...review.holds]; nudgePlansChanged(); };
		// The send request is replaced by a request with new holds that its producer did not mark as the send request: it waits in the canvas's review bar.
		review.replaceSend = () => { review.holds = [{ id: 'hold-2', asker: DRAFTER, reason: CORRECTED_REASON }, ...review.holds.filter((h) => h.purpose !== 'send')]; nudgePlansChanged(); };
		// The draft's one Needs-you item carries the same request the canvas leads with (the newest ordinary request, else the send request), and `purpose: 'send'` while the send request is open.
		const lead = () => review.holds.find((h) => h.purpose !== 'send') ?? review.holds[0];
		await page.route(/\/api\/attention\/queue(\?|$)/, async (route) => {
			review.reads++;
			const top = lead();
			const items = top ? [{ external_id: `review-${artifactId}`, renderer: 'external-review', detail: {
				review_key: `draft-${artifactId}`, title: top.reason, requester: { kind: 'thread', label: top.asker.name, lane: top.asker.id },
				pending_count: review.holds.length, total_count: review.holds.length, node_id: 'node-email-review', purpose: review.holds.some((h) => h.purpose === 'send') ? 'send' : undefined,
				destination: { pane: 'documents', id: artifactId, path: docPath, title: name },
				source: { thread_id: threadId, thread_label: 'Saturday work day', project_id: null, project_label: null, parent_reviewed: true },
			} }] : [];
			await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ current: items[0] ?? null, count: items.length, items }) });
		});
		// What `fetchReviewBar` reads: the open requests, newest first, with `purpose: 'send'` on the send request. Mock only.
		await page.route(/\/api\/attention\/[^/]+\/review$/, async (route) => {
			review.readCalls++;
			if (review.readFails) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'unavailable' }) });
			const open = review.holds;
			const top = open[0];
			const read = {
				ask_raised: open.length > 0, requests: open.map((h) => ({ hold_id: h.id, asker: h.asker, reason: h.reason, purpose: h.purpose })), hold_ids: open.map((h) => h.id),
				ask_reason: top?.reason ?? null, ask_raised_at: null, asker: top?.asker ?? null, other_askers: open.slice(1).map((h) => h.asker.name), other_asks: open.slice(1).map((h) => ({ name: h.asker.name, reason: h.reason })),
				other_request_count: Math.max(0, open.length - 1), round_feedback_count: 0, round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 },
			};
			await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(read) });
		});
		// The answer is what the product's `answerReview` posts: outcome `answer` with a verdict, the note and the holds. Mock only: no plan or backend runs.
		await page.route(/\/api\/attention\/[^/]+\/clear$/, async (route) => {
			const body = route.request().postDataJSON() as Record<string, unknown>;
			review.clears.push({ outcome: body.outcome, verdict: body.verdict, note: body.note, hold_ids: body.hold_ids });
			if (review.mode === 'pending') return;
			const stale = () => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'ask_stale', message: 'This review request was replaced.' } }) });
			if (review.mode === 'stale') { review.holds = review.holds.filter((h) => h.purpose !== 'send'); nudgePlansChanged(); return stale(); } // the send request ended: its item leaves the queue unless another request keeps it
			if (review.mode === 'changed') { review.mode = 'ok'; review.replaceSend(); return stale(); } // replaced by a request with new holds
			const ids = (body.hold_ids as string[]) ?? [];
			if (!ids.length || !ids.every((id) => review.holds.some((h) => h.id === id))) return stale(); // a hold that is no longer open
			if (review.failuresLeft > 0) { review.failuresLeft--; return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'unavailable' }) }); }
			review.holds = review.holds.filter((h) => !ids.includes(h.id));
			await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ review: { verdict: body.verdict, note: body.note, feedback_counts: { comments: 0, suggestions: 0, edits: 0 } } }) });
			nudgePlansChanged();
		});
	}
	// The canvas tab is restored only once `email-frames-open` is set, after the chat turn below has run.
	if (scenario.pdfByPath) await mockPdfRoutes(page, scenario.pdfByPath);
	await page.addInitScript(({ threadId, docPath, name, artifactId, theme, fixture, files, pdfByPath }) => {
		localStorage.setItem('corsia_theme', theme);
		if (fixture) localStorage.setItem('corsia:emailDraftFixture', fixture);
		localStorage.setItem('corsia:emailDraftFiles', files);
		if (localStorage.getItem('email-frames-open') !== '1') return;
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [threadId]: {
			tabs: [
				{ instanceId: 'email-draft', artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' },
				...(pdfByPath ? [{ instanceId: 'pdf-by-path', artifactId: null, path: `${files.replace(/\/$/, '')}/volunteer-waiver.pdf`, title: 'volunteer-waiver.pdf', loadedRevision: null, latestRevision: null, status: 'loading' }] : []),
			],
			activeTabId: pdfByPath ? 'pdf-by-path' : 'email-draft', visible: true, activeSurface: 'artifact',
		} }, splitWidth: 640 }));
	}, { threadId, docPath, name, artifactId, theme, fixture: scenario.fixture, files, pdfByPath: scenario.pdfByPath ?? null });
	const openThread = async () => {
		const item = page.locator('.thread-item').filter({ hasText: title });
		// On a phone the list sits in a drawer that animates on load and can be visible but covered: wait for it to settle, then open it only if still covered.
		const reachable = () => item.evaluate((el) => { const r = el.getBoundingClientRect(); const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); return !!top && el.contains(top); }).catch(() => false);
		if (vp.phone) {
			await page.waitForTimeout(800);
			if (!(await reachable())) {
				await page.getByRole('button', { name: 'Toggle sidebar' }).click();
				await expect.poll(reachable, { timeout: 5000 }).toBe(true);
			}
		}
		await item.click();
	};
	try {
		await loginByUrl(page);
	} catch (error) {
		// Name the state the page was opened against, so a failed open says whether the scenario's data was still there.
		await expectThreadStored(request, threadId, `${scenario.name} ${variant} when the page open failed`);
		throw error;
	}
	await openThread();
	// A real turn through the mock model, so the chat beside the draft holds a user message and a final reply.
	const chat = CHATS[scenario.doc];
	await seedMockResponses(request, [{ content: chat.assistant }]);
	await sendChatMessage(page, chat.user);
	await expect(lastAssistantMessage(page)).toContainText(chat.tail);
	if (!scenario.chatOnly) {
		await page.evaluate(() => localStorage.setItem('email-frames-open', '1'));
		await page.reload();
		await openThread();
		if (!scenario.pdfByPath) await expect(page.locator('.cm-content')).toContainText(scenario.doc === 'plain' ? 'Volunteer work day' : 'Alex Rivera');
	}
	return { page, context, docPath, files, review, artifactId, threadId };
}

/** The scenario's session thread is readable from the backend the page talks to; a failure names which step lost it. */
async function expectThreadStored(request: APIRequestContext, threadId: string, when: string) {
	const response = await request.get(`/api/threads/${threadId}/messages`, { params: { token: TEST_TOKEN } });
	expect(response.ok(), `${when}: thread ${threadId} read returned ${response.status()}`).toBeTruthy();
}

async function workspaceRoot(request: APIRequestContext) {
	const tree = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	expect(tree.ok()).toBeTruthy();
	return (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
}

test.describe('@surface:email-draft-view frames', () => {
	test.skip(!OUT, 'set EMAIL_FRAMES_OUT to capture');

	// Focus (the default) hides a finished reply behind "Worked"; the frames read the raw transcript, and restore Focus after.
	test.beforeEach(async ({ request }) => { await setFocusMode(request, false); });
	test.afterEach(async ({ request }) => { await setFocusMode(request, true); });

	test(`capture stills ${PHASE}`, async ({ request, browser }, testInfo) => {
		test.skip(PHASE === 'motion' || PHASE === 'interact' || PHASE === 'r5');
		test.setTimeout(Number(process.env.EMAIL_FRAMES_TIMEOUT_MS ?? 900_000));
		fs.mkdirSync(OUT, { recursive: true });
		const workspace = await workspaceRoot(request);
		const stamp = Date.now();
		const scenarios = PHASE === 'before' ? [{ name: '1-before', doc: 'before', fixture: null } as Scenario]
			: PHASE === 'compare' ? COMPARE : AFTER;
		let fullBrowser: Browser | null = null;
		for (const scenario of scenarios) {
			if (ONLY && !ONLY.includes(scenario.name)) continue;
			for (const vp of VIEWPORTS) for (const theme of THEMES) {
				if (scenario.thin && !((vp.name === '1440' && theme === 'light') || (vp.name === '393' && theme === 'dark'))) continue;
				if (VARIANTS && !VARIANTS.includes(`${vp.name}-${theme}`)) continue;
				if (scenario.fullChromium) fullBrowser ??= await chromium.launch({ channel: 'chromium' });
				const { page, context, docPath, files, review, threadId } = await seedAndOpen(request, scenario.fullChromium ? fullBrowser! : browser, testInfo, scenario, workspace, stamp, vp, theme, { reducedMotion: 'reduce' }, true);
				await page.waitForTimeout(600);
				// Where the first body line sits (CSS px from the top of the page, scrolled to the top): the first text line after the header's blank line.
				const firstBodyLineY = await page.evaluate(() => {
					const lines = Array.from(document.querySelectorAll('.cm-line')) as HTMLElement[];
					const blank = lines.findIndex((line) => (line.textContent ?? '').trim() === '');
					const body = lines.slice(blank + 1).find((line) => (line.textContent ?? '').trim() !== '');
					return blank < 0 || !body ? null : Math.round(body.getBoundingClientRect().top * 10) / 10;
				});
				fs.writeFileSync(path.join(OUT, `${PHASE}-${scenario.name}-${vp.name}-${theme}.metrics.json`), JSON.stringify({ firstBodyLineY, viewport: vp.viewport }));
				if (scenario.act) await scenario.act(page, { request, docPath, files, context, review });
				if (scenario.review) fs.writeFileSync(path.join(OUT, `${PHASE}-${scenario.name}-${vp.name}-${theme}.facts.json`), JSON.stringify({ queueReads: review.reads, itemOpen: review.holds.length > 0, clears: review.clears }));
				await page.waitForTimeout(400);
				if (!scenario.keepPointer) await page.mouse.move(2, 2);
				// The build stamp is a dev overlay: the frame leaves it out, and the head it names goes into the frame's .state.json.
				const build = await page.locator('.version-overlay').first().textContent({ timeout: 3000 }).catch(() => null);
				await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
				const statePath = path.join(OUT, `${PHASE}-${scenario.name}-${vp.name === '1440' ? '1440' : '393'}.state.json`);
				const earlier = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : {};
				fs.writeFileSync(statePath, JSON.stringify({ ...earlier, head: build?.replace(/\s+/g, ' ').trim().match(/^b ([0-9a-f]{7}[†?]?) /)?.[1] ?? null, build: build?.replace(/\s+/g, ' ').trim() ?? null }));
				await page.screenshot({ path: path.join(OUT, `${PHASE}-${scenario.name}-${vp.name}-${theme}.png`), animations: 'disabled' });
				if (scenario.alone && !vp.phone) {
					await page.locator('.doc-main').first().locator('xpath=..').screenshot({ path: path.join(OUT, `${PHASE}-${scenario.name}-alone-${vp.name}-${theme}.png`), animations: 'disabled' });
				}
				await context.close();
				// Each frame starts from a fresh state: its own chat is the only one in the sidebar.
				const gone = await request.delete(`/api/threads/${threadId}?token=${TEST_TOKEN}`);
				expect(gone.ok(), await gone.text()).toBeTruthy();
			}
		}
		await fullBrowser?.close();
	});

	// Recordings: opening and closing a row, and press feedback, each with and without reduced motion.
	// The page also samples the opened body's height on every animation frame, so the slide has numbers beside the video.
	test('capture motion', async ({ request, browser }, testInfo) => {
		test.skip(PHASE !== 'motion');
		test.setTimeout(Number(process.env.EMAIL_FRAMES_TIMEOUT_MS ?? 900_000));
		fs.mkdirSync(OUT, { recursive: true });
		const workspace = await workspaceRoot(request);
		const stamp = Date.now();
		const takes: Array<{ name: string; vp: typeof VIEWPORTS[number]; theme: typeof THEMES[number]; reduced: boolean; kind: 'open-close' | 'press' }> = [];
		for (const reduced of [false, true]) {
			takes.push({ name: 'open-close', vp: VIEWPORTS[0], theme: 'light', reduced, kind: 'open-close' });
			takes.push({ name: 'press', vp: VIEWPORTS[0], theme: 'light', reduced, kind: 'press' });
			takes.push({ name: 'open-close', vp: VIEWPORTS[1], theme: 'dark', reduced, kind: 'open-close' });
		}
		const report: unknown[] = [];
		for (const take of takes) {
			const label = `motion-${take.kind}-${take.vp.name}-${take.theme}-${take.reduced ? 'reduced' : 'normal'}`;
			if (ONLY && !ONLY.includes(label)) continue;
			const videoDir = fs.mkdtempSync(path.join(os.tmpdir(), 'email-motion-'));
			const scenario: Scenario = { name: label, doc: 'reply', fixture: 'reply4' };
			const { page, context } = await seedAndOpen(request, browser, testInfo, scenario, workspace, stamp, take.vp, take.theme, {
				reducedMotion: take.reduced ? 'reduce' : 'no-preference',
				recordVideo: { dir: videoDir, size: take.vp.viewport },
			});
			await page.waitForTimeout(800);
			await scrollToSection(page);
			await page.waitForTimeout(500);
			const row = page.locator('.email-toggle').nth(1);
			const loadBefore = os.loadavg();
			// A rAF sampler of the opened body's height and the row's background, started before the first press.
			await page.evaluate(() => {
				const w = window as unknown as { __probe: Array<{ t: number; h: number | null; bg: string }>; __probeOn: boolean };
				w.__probe = []; w.__probeOn = true;
				const start = performance.now();
				const tick = () => {
					const body = document.querySelector('.email-row:nth-child(2) .email-body') as HTMLElement | null;
					const toggle = document.querySelector('.email-row:nth-child(2) .email-toggle') as HTMLElement;
					w.__probe.push({ t: Math.round(performance.now() - start), h: body ? Math.round(body.getBoundingClientRect().height * 10) / 10 : null, bg: getComputedStyle(toggle).backgroundColor });
					if (w.__probeOn) requestAnimationFrame(tick);
				};
				requestAnimationFrame(tick);
			});
			await page.waitForTimeout(500);
			const box = (await row.boundingBox())!;
			if (take.kind === 'open-close') {
				if (take.vp.phone) await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
				else await row.click();
				await expect(row).toHaveAttribute('aria-expanded', 'true');
				await page.waitForTimeout(900);
				if (take.vp.phone) await page.touchscreen.tap(box.x + box.width / 2, box.y + 14);
				else await row.click();
				await expect(row).toHaveAttribute('aria-expanded', 'false');
				await page.waitForTimeout(900);
			} else {
				// Press and hold, slide off the row (no click), then press and release on it (opens).
				await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
				await page.waitForTimeout(500);
				await page.mouse.down();
				await page.waitForTimeout(700);
				await page.mouse.move(box.x + box.width / 2, box.y - 80, { steps: 6 });
				await page.mouse.up();
				await page.waitForTimeout(700);
				await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 6 });
				await page.mouse.down();
				await page.waitForTimeout(500);
				await page.mouse.up();
				await expect(row).toHaveAttribute('aria-expanded', 'true');
				await page.waitForTimeout(900);
			}
			const loadAfter = os.loadavg();
			const probe = await page.evaluate(() => { const w = window as unknown as { __probe: unknown[]; __probeOn: boolean }; w.__probeOn = false; return w.__probe; });
			const video = page.video();
			await context.close();
			const saved = path.join(OUT, `${label}.webm`);
			await video!.saveAs(saved);
			fs.writeFileSync(path.join(OUT, `${label}.probe.json`), JSON.stringify({ label, loadBefore, loadAfter, probe }));
			report.push({ label, loadBefore, loadAfter, samples: (probe as unknown[]).length });
		}
		fs.writeFileSync(path.join(OUT, `motion-report-${stamp}.json`), JSON.stringify(report, null, 2));
	});

	// Round 4 recordings: Send then Undo, Send then Sent, a comment on an earlier email, and the read error at phone width.
	// Each take samples, on every animation frame, the toast's box, the canvas scroll position and the send bar's text.
	test('capture interactions', async ({ request, browser }, testInfo) => {
		test.skip(PHASE !== 'interact');
		test.setTimeout(Number(process.env.EMAIL_FRAMES_TIMEOUT_MS ?? 900_000));
		fs.mkdirSync(OUT, { recursive: true });
		const workspace = await workspaceRoot(request);
		const stamp = Date.now();
		type Kind = 'send-undo' | 'send-sent' | 'e4-comment' | 'read-error';
		const takes: Array<{ kind: Kind; vp: typeof VIEWPORTS[number]; theme: typeof THEMES[number]; fixture: string }> = [
			{ kind: 'send-undo', vp: VIEWPORTS[0], theme: 'light', fixture: 'reply4' },
			{ kind: 'send-undo', vp: VIEWPORTS[1], theme: 'dark', fixture: 'reply4' },
			{ kind: 'send-sent', vp: VIEWPORTS[0], theme: 'light', fixture: 'reply4' },
			{ kind: 'send-sent', vp: VIEWPORTS[1], theme: 'dark', fixture: 'reply4' },
			{ kind: 'e4-comment', vp: VIEWPORTS[0], theme: 'light', fixture: 'reply4' },
			{ kind: 'e4-comment', vp: VIEWPORTS[1], theme: 'dark', fixture: 'reply4' },
			{ kind: 'read-error', vp: VIEWPORTS[1], theme: 'dark', fixture: 'error' },
		];
		// Puts the caret after the last character of a line, however many rows it wraps to (End stops at the end of the row clicked).
		const clickLineEnd = async (line: ReturnType<Page['locator']>) => {
			const box = (await line.boundingBox())!;
			await line.page().mouse.click(box.x + box.width - 2, box.y + box.height - 6);
		};
		const scrollTo = (page: Page, where: 'top' | 'end' | number) => page.locator('.email-scroll').evaluate((el, target) => {
			el.scrollTop = target === 'top' ? 0 : target === 'end' ? el.scrollHeight : target;
		}, where);
		// Scrolls the canvas in small steps, so the recording holds in-between frames and the toast is sampled while it moves.
		const scrollSteps = async (page: Page, to: 'top' | 'end') => {
			for (let i = 1; i <= 8; i++) {
				await page.locator('.email-scroll').evaluate((el, [step, target]) => {
					const end = target === 'end' ? el.scrollHeight : 0;
					const from = (el as HTMLElement & { __from?: number }).__from ?? el.scrollTop;
					(el as HTMLElement & { __from?: number }).__from = from;
					el.scrollTop = from + (end - from) * (step as number) / 8;
					if (step === 8) delete (el as HTMLElement & { __from?: number }).__from;
				}, [i, to] as [number, string]);
				await page.waitForTimeout(70);
			}
		};
		const report: unknown[] = [];
		for (const take of takes) {
			const label = `interact-${take.kind}-${take.vp.name}-${take.theme}`;
			if (ONLY && !ONLY.includes(label)) continue;
			const videoDir = fs.mkdtempSync(path.join(os.tmpdir(), 'email-interact-'));
			const scenario: Scenario = { name: label, doc: 'reply', fixture: take.fixture };
			const { page, context } = await seedAndOpen(request, browser, testInfo, scenario, workspace, stamp, take.vp, take.theme, {
				reducedMotion: 'no-preference',
				recordVideo: { dir: videoDir, size: take.vp.viewport },
			});
			await page.waitForTimeout(1000);
			const loadBefore = os.loadavg();
			await page.evaluate(() => {
				const w = window as unknown as { __probe: unknown[]; __probeOn: boolean; __t0: number };
				w.__probe = []; w.__probeOn = true; w.__t0 = performance.now();
				const tick = () => {
					const toast = document.querySelector('.toast-container .toast-motion') as HTMLElement | null;
					const rect = toast?.getBoundingClientRect();
					const scroller = document.querySelector('.email-scroll') as HTMLElement | null;
					const bar = document.querySelector('.email-send-bar .bar-text') as HTMLElement | null;
					w.__probe.push({
						t: Math.round(performance.now() - w.__t0),
						toast: rect ? { top: Math.round(rect.top), left: Math.round(rect.left), w: Math.round(rect.width), h: Math.round(rect.height), opacity: Number(getComputedStyle(toast!).opacity), inView: rect.top >= 0 && rect.bottom <= innerHeight && rect.left >= 0 && rect.right <= innerWidth } : null,
						scrollTop: scroller ? Math.round(scroller.scrollTop) : null,
						bar: bar?.textContent ?? null,
					});
					if (w.__probeOn) requestAnimationFrame(tick);
				};
				requestAnimationFrame(tick);
			});
			await page.waitForTimeout(600);
			const tap = async (locator: ReturnType<Page['locator']>) => {
				if (take.vp.phone) { const b = (await locator.boundingBox())!; await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2); } else await locator.click();
			};
			const marks: Record<string, number> = {};
			const mark = async (name: string) => { marks[name] = await page.evaluate(() => Math.round(performance.now() - (window as unknown as { __t0: number }).__t0)); };
			const facts: Record<string, unknown> = {};
			if (take.kind === 'send-undo' || take.kind === 'send-sent') {
				const typed = ' (written before Send)';
				if (take.kind === 'send-undo') {
					const line = page.locator('.cm-line').filter({ hasText: 'Could you confirm the meeting spot' }).first();
					await clickLineEnd(line);
					await page.keyboard.type(typed, { delay: 25 });
					await page.waitForTimeout(500);
				}
				await mark('before-send');
				await tap(page.locator('.email-send-bar').getByRole('button', { name: 'Send', exact: true }));
				await mark('send-tapped');
				await expect(page.locator('.email-send-bar')).toContainText('Sending');
				await page.waitForTimeout(900);
				await scrollSteps(page, 'end');
				await mark('scrolled-to-conversation');
				await page.waitForTimeout(500);
				if (take.kind === 'send-undo') {
					await tap(page.locator('.email-send-bar').getByRole('button', { name: 'Undo', exact: true }));
					await mark('undo-tapped');
					await expect(page.locator('.email-send-bar').getByRole('button', { name: 'Send', exact: true })).toBeVisible();
					await page.waitForTimeout(500);
					await scrollSteps(page, 'top');
					await expect(page.locator('.cm-content')).toContainText(typed.trim());
					const again = page.locator('.cm-line').filter({ hasText: 'Could you confirm the meeting spot' }).first();
					await clickLineEnd(again);
					await page.keyboard.type(' Typing again.', { delay: 25 });
					await mark('typed-after-undo');
					await page.waitForTimeout(1200);
					facts.textKept = (await page.locator('.cm-content').innerText()).includes(typed.trim());
					facts.editableAfterUndo = (await page.locator('.cm-content').innerText()).includes('Typing again.');
					facts.sendButtonBack = true;
				} else {
					await expect(page.locator('.email-send-bar')).toContainText('Sent', { timeout: 12_000 });
					await mark('sent-shown');
					await page.waitForTimeout(1500);
					await scrollSteps(page, 'top');
					await page.waitForTimeout(1000);
					facts.barText = await page.locator('.email-send-bar .bar-text').innerText();
				}
			} else if (take.kind === 'e4-comment') {
				await scrollToSection(page);
				await page.waitForTimeout(400);
				const row = page.locator('.email-toggle').nth(1);
				await tap(row);
				await expect(row).toHaveAttribute('aria-expanded', 'true');
				await page.waitForTimeout(700);
				await page.locator('.email-row').nth(1).locator('.email-text').evaluate((el) => el.scrollIntoView({ block: 'center' }));
				await page.waitForTimeout(500);
				const quote = 'Does anyone know if the gate code changed?';
				await page.evaluate((text) => {
					const root = document.querySelectorAll('.email-row')[1].querySelector('.email-text') as HTMLElement;
					const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
					for (let n = walker.nextNode(); n; n = walker.nextNode()) {
						const at = (n.textContent ?? '').indexOf(text);
						if (at >= 0) { const r = document.createRange(); r.setStart(n, at); r.setEnd(n, at + text.length); const sel = getSelection()!; sel.removeAllRanges(); sel.addRange(r); return; }
					}
					throw new Error('quote not found');
				}, quote);
				await mark('text-selected');
				await expect(page.locator('.sel-comment')).toBeVisible();
				await page.waitForTimeout(900);
				await tap(page.locator('.sel-comment'));
				await mark('comment-tapped');
				const field = page.getByRole('textbox', { name: 'Comment' });
				await expect(field).toBeVisible();
				await page.waitForTimeout(500);
				await field.pressSequentially('Dana, can you check the gate code with the county before Saturday?', { delay: 25 });
				await mark('typed');
				await page.waitForTimeout(700);
				facts.composerHeading = await page.locator('[role="dialog"][aria-label^="Comment on"], .drawer [aria-label^="Comment on"]').first().getAttribute('aria-label').catch(() => null);
				await tap(page.locator('.composer .submit'));
				await mark('posted');
				await expect(page.locator('.email-comment')).toBeVisible();
				await page.locator('.email-comment').evaluate((el) => el.scrollIntoView({ block: 'center' }));
				await page.waitForTimeout(1500);
				facts.commentFor = await page.locator('.email-comment-for').innerText();
				facts.commentQuote = await page.locator('.email-comment .quote').innerText();
				facts.commentText = await page.locator('.email-comment-text').innerText();
				facts.emailStillReadOnly = await page.locator('.email-row').nth(1).locator('.email-text').getAttribute('contenteditable') === null;
			} else {
				// read-error: entry frame at the top, scroll to the error, scroll back.
				await expect(page.locator('.email-status.failed')).toBeAttached();
				await mark('entry');
				facts.entryHeaderVisible = await page.evaluate(() => {
					const head = Array.from(document.querySelectorAll('.cm-email-head')) as HTMLElement[];
					return head.map((el) => ({ text: (el.textContent ?? '').slice(0, 40), visible: el.getBoundingClientRect().top >= 0 && el.getBoundingClientRect().bottom <= innerHeight }));
				});
				await page.waitForTimeout(1500);
				await scrollSteps(page, 'end');
				await mark('at-error');
				facts.errorVisible = await page.locator('.email-status.failed').evaluate((el) => el.getBoundingClientRect().bottom <= innerHeight && el.getBoundingClientRect().top >= 0);
				await page.waitForTimeout(1500);
				await scrollSteps(page, 'top');
				await mark('scrolled-back');
				facts.backHeaderVisible = await page.evaluate(() => {
					const head = Array.from(document.querySelectorAll('.cm-email-head')) as HTMLElement[];
					return head.map((el) => ({ text: (el.textContent ?? '').slice(0, 40), visible: el.getBoundingClientRect().top >= 0 && el.getBoundingClientRect().bottom <= innerHeight }));
				});
				await page.waitForTimeout(1500);
			}
			const loadAfter = os.loadavg();
			const probe = await page.evaluate(() => { const w = window as unknown as { __probe: unknown[]; __probeOn: boolean }; w.__probeOn = false; return w.__probe; });
			const video = page.video();
			await context.close();
			await video!.saveAs(path.join(OUT, `${label}.webm`));
			fs.writeFileSync(path.join(OUT, `${label}.probe.json`), JSON.stringify({ label, loadBefore, loadAfter, marks, facts, probe }));
			report.push({ label, marks, facts, loadBefore, loadAfter, samples: (probe as unknown[]).length });
		}
		fs.writeFileSync(path.join(OUT, `interact-report-${stamp}.json`), JSON.stringify(report, null, 2));
	});

	// Round 5 recordings: the quiet notes, a removed attachment, recovery from an unavailable file, Don't send then Keep draft or confirm,
	// and every way the comment overlay closes. Each runs with and without reduced motion; a per-frame probe samples what moves.
	test('capture round 5 takes', async ({ request, browser }, testInfo) => {
		test.skip(PHASE !== 'r5');
		test.setTimeout(Number(process.env.EMAIL_FRAMES_TIMEOUT_MS ?? 1_800_000));
		fs.mkdirSync(OUT, { recursive: true });
		const workspace = await workspaceRoot(request);
		const stamp = Date.now();
		type Kind = 'notes' | 'chip-remove' | 'unavailable' | 'dont-send' | 'comment-close';
		const fixtures: Record<Kind, string> = { notes: 'warn4', 'chip-remove': 'attach', unavailable: 'attachMissing', 'dont-send': 'reply4', 'comment-close': 'reply4' };
		const takes: Array<{ kind: Kind; vp: typeof VIEWPORTS[number]; theme: typeof THEMES[number]; reduced: boolean }> = [];
		for (const kind of Object.keys(fixtures) as Kind[]) for (const reduced of [false, true]) {
			takes.push({ kind, vp: VIEWPORTS[0], theme: 'light', reduced });
			takes.push({ kind, vp: VIEWPORTS[1], theme: 'dark', reduced });
		}
		const report: unknown[] = [];
		for (const take of takes) {
			const label = `r5-${take.kind}-${take.vp.name}-${take.theme}-${take.reduced ? 'reduced' : 'normal'}`;
			if (ONLY && !ONLY.includes(label)) continue;
			const videoDir = fs.mkdtempSync(path.join(os.tmpdir(), 'email-r5-'));
			const scenario: Scenario = { name: label, doc: 'reply', fixture: fixtures[take.kind] };
			const { page, context } = await seedAndOpen(request, browser, testInfo, scenario, workspace, stamp, take.vp, take.theme, {
				reducedMotion: take.reduced ? 'reduce' : 'no-preference',
				recordVideo: { dir: videoDir, size: take.vp.viewport },
			});
			await page.waitForTimeout(1000);
			const loadBefore = os.loadavg();
			await page.evaluate(() => {
				const w = window as unknown as { __probe: unknown[]; __probeOn: boolean; __t0: number };
				w.__probe = []; w.__probeOn = true; w.__t0 = performance.now();
				const tick = () => {
					const list = document.querySelector('.cm-email-notes-list') as HTMLElement | null;
					const toggle = document.querySelector('.cm-email-notes-toggle');
					const send = Array.from(document.querySelectorAll('.email-send-bar button')).find((b) => b.textContent?.trim() === 'Send');
					const composer = document.querySelector('.composer');
					w.__probe.push({
						t: Math.round(performance.now() - w.__t0),
						notes: list ? { h: list.hidden ? 0 : Math.round(list.getBoundingClientRect().height * 10) / 10, hidden: list.hidden, expanded: toggle?.getAttribute('aria-expanded') } : null,
						chips: Array.from(document.querySelectorAll('.email-attachments .attachment')).map((el) => ({ name: (el.querySelector('.attachment-remove')?.getAttribute('aria-label') ?? '').replace('Remove ', ''), opacity: Number(getComputedStyle(el).opacity), w: Math.round(el.getBoundingClientRect().width) })),
						bar: document.querySelector('.email-send-bar .bar-text')?.textContent ?? null,
						sendDisabled: send ? send.getAttribute('aria-disabled') ?? String((send as HTMLButtonElement).disabled) : null,
						composer: composer ? Math.round(composer.getBoundingClientRect().height) : null,
						overlays: { popover: document.querySelectorAll('[data-popover]').length, drawer: document.querySelectorAll('.drawer-panel').length, scrim: document.querySelectorAll('.drawer-backdrop').length },
					});
					if (w.__probeOn) requestAnimationFrame(tick);
				};
				requestAnimationFrame(tick);
			});
			await page.waitForTimeout(600);
			const tap = async (locator: ReturnType<Page['locator']>) => {
				if (take.vp.phone) { const b = (await locator.boundingBox())!; await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2); } else await locator.click();
			};
			const marks: Record<string, number> = {};
			const mark = async (name: string) => { marks[name] = await page.evaluate(() => Math.round(performance.now() - (window as unknown as { __t0: number }).__t0)); };
			const facts: Record<string, unknown> = {};
			if (take.kind === 'notes') {
				const toggle = page.locator('.cm-email-notes-toggle');
				await toggle.scrollIntoViewIfNeeded();
				await mark('open-tapped'); await tap(toggle);
				await expect(toggle).toHaveAttribute('aria-expanded', 'true');
				await page.waitForTimeout(1000);
				await mark('close-tapped'); await tap(toggle);
				await expect(toggle).toHaveAttribute('aria-expanded', 'false');
				await page.waitForTimeout(1000);
			} else if (take.kind === 'chip-remove') {
				await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
				await page.waitForTimeout(500);
				await mark('remove-tapped');
				await tap(page.getByRole('button', { name: 'Remove north-trailhead-map.png' }));
				await expect(page.locator('.email-attachments .attachment')).toHaveCount(1, { timeout: 5000 });
				await page.waitForTimeout(1200);
				facts.remaining = await page.locator('.email-attachments .attachment').allInnerTexts();
			} else if (take.kind === 'unavailable') {
				await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
				await expect(page.locator('.email-send-bar')).toContainText('volunteer-waiver.pdf is unavailable');
				await page.waitForTimeout(800);
				facts.barBefore = await page.locator('.email-send-bar .bar-text').innerText();
				await mark('remove-tapped');
				await tap(page.getByRole('button', { name: 'Remove volunteer-waiver.pdf' }));
				await expect(page.locator('.email-send-bar .bar-text')).toHaveCount(0, { timeout: 5000 });
				await mark('send-enabled');
				await page.waitForTimeout(1000);
				facts.sendAriaDisabledAfter = await page.locator('.email-send-bar').getByRole('button', { name: 'Send', exact: true }).getAttribute('aria-disabled');
				await mark('send-tapped');
				await tap(page.locator('.email-send-bar').getByRole('button', { name: 'Send', exact: true }));
				await expect(page.locator('.email-send-bar')).toContainText('Sending');
				await page.waitForTimeout(1200);
				await mark('undo-tapped');
				await tap(page.locator('.email-send-bar').getByRole('button', { name: 'Undo', exact: true }));
				await page.waitForTimeout(1000);
			} else if (take.kind === 'dont-send') {
				const barButton = (name: string) => page.locator('.email-send-bar').getByRole('button', { name, exact: true });
				await mark('dont-send-1'); await tap(barButton('Don’t send'));
				await expect(page.locator('.email-send-bar')).toContainText('Don’t send this email?');
				await page.waitForTimeout(1200);
				await mark('keep-draft'); await tap(barButton('Keep draft'));
				await expect(barButton('Send')).toBeVisible();
				await page.waitForTimeout(1200);
				await mark('dont-send-2'); await tap(barButton('Don’t send'));
				await page.waitForTimeout(1000);
				await mark('confirm'); await tap(barButton('Don’t send'));
				await expect(page.locator('.email-send-bar')).toContainText('Not sent');
				await page.waitForTimeout(1500);
			} else {
				// Every close path the code has. The popover (a desktop width) closes on an outside press, Escape and Cancel; the phone sheet
				// on Done, a tap on its scrim, Escape, the browser's back, a drag down and Cancel. Each path opens a fresh composer on a selection.
				const quote = 'Does anyone know if the gate code changed?';
				const openComposer = async () => {
					await scrollToSection(page);
					await page.waitForTimeout(300);
					const row = page.locator('.email-toggle').nth(1);
					if ((await row.getAttribute('aria-expanded')) !== 'true') { await tap(row); await expect(row).toHaveAttribute('aria-expanded', 'true'); await page.waitForTimeout(700); }
					await page.locator('.email-row').nth(1).locator('.email-text').evaluate((el) => el.scrollIntoView({ block: 'center' }));
					await page.waitForTimeout(400);
					await page.evaluate((text) => {
						const root = document.querySelectorAll('.email-row')[1].querySelector('.email-text') as HTMLElement;
						const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
						for (let n = walker.nextNode(); n; n = walker.nextNode()) {
							const at = (n.textContent ?? '').indexOf(text);
							if (at >= 0) { const r = document.createRange(); r.setStart(n, at); r.setEnd(n, at + text.length); const sel = getSelection()!; sel.removeAllRanges(); sel.addRange(r); return; }
						}
						throw new Error('quote not found');
					}, quote);
					await expect(page.locator('.sel-comment')).toBeVisible();
					await page.waitForTimeout(500);
					await tap(page.locator('.sel-comment'));
					await expect(page.getByRole('textbox', { name: 'Comment' })).toBeVisible();
					await page.waitForTimeout(700);
				};
				const state = () => page.evaluate((text) => ({
					composerOpen: !!document.querySelector('[aria-label="Comment"]'),
					overlays: document.querySelectorAll('[data-popover], .drawer-panel').length,
					posted: document.querySelectorAll('.email-comment').length,
					selectionCleared: (getSelection()?.toString() ?? '') === '' || !(getSelection()?.toString() ?? '').includes(text),
				}), quote);
				const paths: Array<[string, () => Promise<void>]> = take.vp.phone ? [
					['done', async () => { await tap(page.locator('.drawer-done')); }],
					['scrim-tap', async () => { await page.touchscreen.tap(take.vp.viewport.width / 2, 40); }],
					['escape', async () => { await page.keyboard.press('Escape'); }],
					['back', async () => { await page.goBack(); }],
					['drag-down', async () => {
						const panel = (await page.locator('.drawer-panel').boundingBox())!;
						const x = panel.x + panel.width / 2, y = panel.y + 14;
						await page.mouse.move(x, y); await page.mouse.down();
						await page.mouse.move(x, y + 120, { steps: 10 }); await page.mouse.move(x, y + 360, { steps: 10 }); await page.mouse.up();
					}],
					['cancel', async () => { await tap(page.getByRole('button', { name: 'Cancel' })); }],
				] : [
					['outside-press', async () => { await page.mouse.click(take.vp.viewport.width - 40, take.vp.viewport.height - 40); }],
					['escape', async () => { await page.keyboard.press('Escape'); }],
					['cancel', async () => { await page.getByRole('button', { name: 'Cancel' }).click(); }],
				];
				const results: Record<string, unknown> = {};
				for (const [name, close] of paths) {
					await openComposer();
					await mark(`${name}-open`);
					await close();
					await mark(`${name}-closed`);
					await page.waitForTimeout(1100);
					results[name] = await state();
				}
				facts.paths = results;
			}
			const loadAfter = os.loadavg();
			const probe = await page.evaluate(() => { const w = window as unknown as { __probe: unknown[]; __probeOn: boolean }; w.__probeOn = false; return w.__probe; });
			const video = page.video();
			await context.close();
			await video!.saveAs(path.join(OUT, `${label}.webm`));
			fs.writeFileSync(path.join(OUT, `${label}.probe.json`), JSON.stringify({ label, loadBefore, loadAfter, marks, facts, probe }));
			report.push({ label, marks, facts, loadBefore, loadAfter, samples: (probe as unknown[]).length });
		}
		fs.writeFileSync(path.join(OUT, `r5-report-${stamp}.json`), JSON.stringify(report, null, 2));
	});
});
