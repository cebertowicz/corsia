// Render route for the email draft view: stills on the real document canvas, with the draft's context read through a route mock of
// `GET /api/email-drafts/{artifact_id}/context` (data shaped as backend/api/email_drafts.py returns it). Skipped unless EMAIL_FRAMES_OUT is set.
// Run from the repo root:
//   CORSIA_E2E_MOCK=1 CORSIA_E2E_BACKEND_PORT=<free> CORSIA_E2E_FRONTEND_PORT=<free> CORSIA_E2E_PYTHON=<python 3.11> \
//   CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1 EMAIL_FRAMES_OUT=<dir> [EMAIL_FRAMES_ONLY=<scenario>[,<scenario>...]] \
//   [EMAIL_FRAMES_VARIANTS=1440-light,393-dark] bash scripts/e2e-local --grep "email-draft-view frames" --project=chromium-desktop
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { createHash } from 'node:crypto';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Browser, type Page, type TestInfo } from '@playwright/test';
import { createSession, lastAssistantMessage, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { seedMockResponses } from '../support/mock';
import { FONT, FOUR, NEWER, SAME_SUBJECT, failed, fresh, markEmailDraft, mockEmailDraftContext, reply, type ServerAttachment, type ServerContext } from '../support/emailDraftContext';

const OUT = process.env.EMAIL_FRAMES_OUT ?? '';
const ONLY = process.env.EMAIL_FRAMES_ONLY?.split(',');
/** Comma list such as `1440-light,393-dark` to capture only those width-theme pairs. */
const VARIANTS = process.env.EMAIL_FRAMES_VARIANTS?.split(',');

// The draft as the draft tools write it (connections/mail/email_document.py `compose_document`): plain addresses in To and Cc.
const FROM = 'From: Alex Rivera <alex.rivera@example.com>';
const SUBJECT = 'Subject: Re: Saturday work day: tools and parking';
const BODY = [
	'Hi Dana,', '',
	'Thanks for the count. Twelve of us can come Saturday, so the two extra pairs of loppers from the shed will be plenty. I will bring the sign-in sheet and a cooler of water.', '',
	'Could you confirm the meeting spot is still the north trailhead lot, not the church lot?', '',
	'Thanks,', 'Alex', '',
	'-- ', 'Alex Rivera', 'Trail Volunteer Coordinator',
].join('\n');
const head = (...lines: string[]) => [FROM, ...lines, SUBJECT, '', BODY, ''].join('\n');
const DOCS = {
	reply: head('To: dana.whitfield@example.com'),
	ccadded: head('To: dana.whitfield@example.com', 'Cc: priya.raman@example.com'),
	bothadded: head('To: dana.whitfield@example.com, priya.raman@example.com', 'Cc: jordan.lee@example.com'),
	shortadded: head('To: dana.whitfield@example.com, a@b.co, marcus.obi@example.com'),
	badto: head('To: dana.whitfield@example.com, priya.raman'),
	badto2: head('To: dana.whitfield@example.com, priya.raman, dana@'),
	fresh: [FROM, 'To: marcus.obi@example.com', 'Subject: Brush crew start time for Saturday', '',
		'Hi Marcus,', '', 'The 8:30 start works. Could you lead the brush crew again and walk the drainage cut with the county contact before the rest of us arrive?', '',
		'Thanks,', 'Alex', '', '-- ', 'Alex Rivera', 'Trail Volunteer Coordinator', ''].join('\n'),
};

/** The entries the send refuses in each draft, as the server's reading of its header names them. */
const REFUSED: Partial<Record<keyof typeof DOCS, string[]>> = { badto: ['priya.raman'], badto2: ['priya.raman', 'dana@'] };

// The chat beside a draft says what the draft says: who it goes to is read from the draft's own To and Cc, so the two never disagree.
const REPLY_SUMMARY = 'I drafted a reply to Dana Whitfield and saved it as a document. It confirms twelve volunteers, says you will bring the sign-in sheet and a cooler of water, and asks her to confirm the north trailhead lot instead of the church lot.';
const REPLY_CHAT = (recipients: string) => ({
	user: 'Dana asked about parking and loppers for Saturday’s work day. Can you draft a reply?',
	assistant: [REPLY_SUMMARY, '', 'Check before you send:', '', `- ${recipients}`, '', 'Open the draft to read the earlier emails and edit the reply.'].join('\n'),
	tail: 'Open the draft to read the earlier emails',
});
const CHATS: Record<keyof typeof DOCS, { user: string; assistant: string; tail: string }> = {
	reply: REPLY_CHAT('The reply goes to Dana only.'),
	ccadded: REPLY_CHAT('The reply goes to Dana, with Priya in Cc. Priya wasn’t on Dana’s email, so remove her if she shouldn’t see it.'),
	bothadded: REPLY_CHAT('The reply goes to Dana and Priya, with Jordan Lee in Cc. Priya and Jordan weren’t on Dana’s email, so remove them if they shouldn’t see it.'),
	shortadded: REPLY_CHAT('The reply goes to Dana, a@b.co and Marcus. a@b.co and Marcus weren’t on Dana’s email, so remove them if they shouldn’t see it.'),
	badto: REPLY_CHAT('The reply goes to Dana, and the To line also has priya.raman, which isn’t a full email address. Fix it before you send.'),
	badto2: REPLY_CHAT('The reply goes to Dana, and the To line also has priya.raman and dana@, which aren’t full email addresses. Fix them before you send.'),
	fresh: {
		user: 'Can you ask Marcus whether the 8:30 start works for the brush crew?',
		assistant: 'I drafted a new email to Marcus Obi and saved it as a document. It asks him to lead the brush crew again and to walk the drainage cut with the county contact before the rest of you arrive.\n\nOpen the draft to edit it before you send.',
		tail: 'Open the draft to edit it',
	},
};

const VIEWPORTS = [
	{ name: '1440', viewport: { width: 1440, height: 1000 }, phone: false },
	{ name: '393', viewport: { width: 393, height: 852 }, phone: true },
] as const;
const THEMES = ['light', 'dark'] as const;
const CHAT_TITLE = 'Reply to Dana about Saturday';
const FRAME_MODEL_NAME = 'Sonnet 5.5';

/** What the scenario's context read answers: the same body every time, or one chosen by how many reads came before. */
type Answer = ServerContext | 'hold' | ((files: string, reads: number) => ServerContext | 'hold');
type Scenario = {
	name: string; doc: keyof typeof DOCS; context: Answer;
	act?: (page: Page, docPath: string, request: APIRequestContext) => Promise<void>;
	/** Only 1440 light and 393 dark. */ thin?: boolean;
	/** The pointer stays where the act left it (a hover frame); otherwise it is moved off the page before the shot. */ keepPointer?: boolean;
};

const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1440) < 500;
const scroll = (to: 'top' | 'end') => async (page: Page) => {
	await page.locator('.email-scroll').evaluate((el, to) => { el.scrollTop = to === 'top' ? 0 : el.scrollHeight; }, to);
	await page.waitForTimeout(300);
};
const openRow = (index: number) => async (page: Page) => {
	const row = page.locator('.email-toggle').nth(index);
	await row.click();
	await expect(row).toHaveAttribute('aria-expanded', 'true');
	await page.waitForTimeout(400);
};
const toList = async (page: Page) => {
	await page.locator('.email-conversation').evaluate((el) => el.scrollIntoView({ block: 'start' }));
	await page.waitForTimeout(300);
};
const toChips = async (page: Page) => {
	await page.locator('.email-attachments').evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await page.waitForTimeout(300);
};
const chipFor = (page: Page, name: string) => page.locator('.email-attachments .attachment').filter({ hasText: name });

/** The conversation reads the server's emails: collapsed, newest first, and the answered email says so. */
const expectConversation = async (page: Page, count: number, repliedAt: number) => {
	const list = page.locator('.email-conversation');
	await expect(list.locator('h3')).toHaveText(`In this conversation · ${count} emails`);
	await expect(list.locator('.email-row')).toHaveCount(count);
	await expect(list.locator('.email-row').nth(repliedAt).locator('.email-second').first()).toHaveText('Replying to this email');
	await expect(list.locator('.email-row.open')).toHaveCount(0);
};
/** The amber line above From names the newer emails, and the list holds them above the email being replied to (no badge, no link). */
const expectNewer = (line: string, newer: number) => async (page: Page) => {
	await expect(page.locator('.email-loud li')).toHaveText([line]);
	await expectConversation(page, newer + FOUR.length, newer);
	await expect(page.locator('.email-conversation a')).toHaveCount(0);
};
/** A bad address is amber where it sits in To or Cc, with no "!", and one amber line names it as typed. */
/** Every character of a flagged entry (added or not a full address) is drawn in amber, a bare address's URL colour included. */
const expectAmber = async (page: Page) => {
	const off = await page.locator('.cm-content').evaluate((content) => {
		const probe = content.appendChild(document.createElement('span'));
		probe.style.color = 'var(--amber-text)';
		const amber = getComputedStyle(probe).color;
		probe.remove();
		const wrong: string[] = [];
		for (const entry of content.querySelectorAll('.cm-email-added, .cm-email-added-tail, .cm-email-bad')) {
			const walker = document.createTreeWalker(entry, NodeFilter.SHOW_TEXT);
			for (let text = walker.nextNode(); text; text = walker.nextNode()) {
				if (text.parentElement!.closest('.cm-email-added-mark')) continue;
				if (getComputedStyle(text.parentElement!).color !== amber) wrong.push(text.textContent ?? '');
			}
		}
		return wrong;
	});
	expect(off, 'flagged entry text that is not amber').toEqual([]);
	await expect(page.locator('.cm-email-added, .cm-email-bad').first()).toBeVisible();
};
const expectBad = (entries: string[], line: string) => async (page: Page) => {
	await expect(page.locator('.cm-email-bad')).toHaveText(entries);
	await expectAmber(page);
	await expect(page.locator('.cm-email-added-mark')).toHaveCount(0);
	await expect(page.locator('.email-loud li')).toHaveText([line]);
};
/**
 * The "!" hint opens just below the flagged recipient: its box starts at or below the bottom of that recipient's text and never
 * overlaps it, and it stays inside the editor. Writes what it measured next to the frame.
 */
const expectHintBelow = (name: string, flagged: string) => async (page: Page) => {
	const mark = page.locator('.cm-email-added-mark').first();
	await expect(mark).toBeVisible();
	await expectAmber(page);
	if (isPhone(page)) await mark.click(); else await mark.hover();
	const tip = page.locator('.cm-email-added-tip').first();
	await expect(tip).toBeVisible();
	await expect(tip).toHaveText('Not on the email you’re replying to.');
	const facts = await mark.evaluate((node, flagged) => {
		const tipBox = node.querySelector('.cm-email-added-tip')!.getBoundingClientRect();
		const line = node.closest('.cm-line')!;
		const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
		const rects: DOMRect[] = [];
		for (let text = walker.nextNode(); text; text = walker.nextNode()) {
			const at = text.textContent?.indexOf(flagged) ?? -1;
			if (at < 0) continue;
			const range = document.createRange();
			range.setStart(text, at);
			range.setEnd(text, at + flagged.length);
			rects.push(...range.getClientRects());
		}
		const scroller = node.closest('.cm-scroller')!.getBoundingClientRect();
		const overlaps = rects.some((r) => r.left < tipBox.right && r.right > tipBox.left && r.top < tipBox.bottom && r.bottom > tipBox.top);
		const recipientBottom = Math.max(...rects.map((r) => r.bottom));
		return { tip: tipBox.toJSON(), recipient: rects.map((r) => r.toJSON()), recipientBottom, overlaps, editorLeft: scroller.left, editorRight: scroller.right };
	}, flagged);
	fs.writeFileSync(path.join(OUT, `after-${name}-${isPhone(page) ? '393' : '1440'}.hint.json`), JSON.stringify(facts, null, 1));
	expect(facts.recipient.length).toBeGreaterThan(0);
	expect(facts.overlaps).toBe(false);
	expect(facts.tip.top).toBeGreaterThanOrEqual(facts.recipientBottom - 0.5);
	expect(facts.tip.left).toBeGreaterThanOrEqual(facts.editorLeft - 0.5);
	expect(facts.tip.right).toBeLessThanOrEqual(facts.editorRight + 0.5);
	await page.waitForTimeout(300);
};

const WITH_NOTES = { notes: [SAME_SUBJECT, FONT] };
/** The draft's attachments as the route returns them: absolute paths under the chat's folder, which the seeding wrote. */
const attachments = (files: string, items: Array<[name: string, size: number, unavailable?: boolean]>): ServerAttachment[] =>
	items.map(([name, size, unavailable]) => ({ name, path: path.join(files, name), size, type: /\.png$/.test(name) ? 'image' : 'document', unavailable: !!unavailable }));
const LONG_PDF = 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf';
const LONG_PNG = 'drainage_cut_and_bridge_approach_crew_assignments_saturday_november_14_updated_after_safety_review.png';

const SCENARIOS: Scenario[] = [
	// Journey 1: the reply opens with its conversation below it.
	{ name: '2-rest', doc: 'reply', context: reply(), act: async (page) => { await expectConversation(page, 4, 0); } },
	{ name: '2b-rest-scrolled', doc: 'reply', context: reply(), act: scroll('end') },
	{ name: '3-expanded', doc: 'reply', context: reply(), act: async (page) => { await openRow(1)(page); await scroll('end')(page); } },
	// Journey 2: what the person should check before sending.
	{ name: '4-warning', doc: 'ccadded', context: reply({ conversation: [...NEWER[1], ...FOUR], ...WITH_NOTES }), act: expectAmber },
	{ name: '4b-quiet-open', doc: 'reply', context: reply(WITH_NOTES), act: async (page) => { await page.locator('.cm-email-notes-toggle').click(); await page.waitForTimeout(300); } },
	{ name: '4c-loud-many', doc: 'bothadded', context: reply({ conversation: [...NEWER[2], ...FOUR] }) },
	{ name: '4d-quiet-only', doc: 'reply', context: reply(WITH_NOTES), act: async (page) => { await expect(page.locator('.email-loud li')).toHaveCount(0); } },
	{ name: '4e-bad-address', doc: 'badto', context: reply(), act: expectBad(['priya.raman'], 'priya.raman isn’t a full email address.') },
	{ name: '4e2-two-bad-addresses', doc: 'badto2', context: reply(), act: expectBad(['priya.raman', 'dana@'], '2 entries aren’t full email addresses: priya.raman, dana@') },
	{ name: '4f-mid-edit', doc: 'reply', context: reply(), act: async (page, docPath, request) => {
		// Caret at the end of the To line, a second address half typed. The autosave must write the header line to the file.
		const toLine = page.locator('.cm-line.cm-email-head').filter({ hasText: 'To:' });
		const box = (await toLine.boundingBox())!;
		await page.mouse.click(box.x + 40, box.y + box.height - 4);
		await page.keyboard.press('End');
		await page.keyboard.type(', priya.raman@exam', { delay: 30 });
		const saved = async () => {
			const response = await request.get('/api/files/content', { params: { token: TEST_TOKEN, path: docPath } });
			return ((await response.json()).text ?? '') as string;
		};
		await expect.poll(saved, { timeout: 15_000 }).toContain('To: dana.whitfield@example.com, priya.raman@exam');
		await page.waitForTimeout(300);
	} },
	{ name: '4g-added-hint', doc: 'ccadded', context: reply(), keepPointer: true, act: expectHintBelow('4g-added-hint', 'priya.raman@example.com') },
	{ name: '4g2-added-hint-short', doc: 'shortadded', context: reply(), keepPointer: true, act: expectHintBelow('4g2-added-hint-short', 'a@b.co') },
	{ name: '4h-added-cc', doc: 'ccadded', context: reply(), act: expectAmber },
	{ name: '4i-two-newer', doc: 'reply', context: reply({ conversation: [...NEWER[2], ...FOUR] }), act: async (page) => {
		await expectNewer('2 emails came after the one you’re replying to: Dana Whitfield, Marcus Obi.', 2)(page);
		await toList(page);
	} },
	{ name: '4i2-one-newer', doc: 'reply', context: reply({ conversation: [...NEWER[1], ...FOUR] }), act: async (page) => {
		await expectNewer('Marcus Obi wrote on Nov 5, after the email you’re replying to.', 1)(page);
		await toList(page);
	} },
	// At the top, so the six-sender line and its phone-width wrap show.
	{ name: '4i3-six-newer', doc: 'reply', context: reply({ conversation: [...NEWER[6], ...FOUR] }), act: async (page) => {
		await expectNewer('6 emails came after the one you’re replying to: Jordan Lee, Priya Raman, Marcus Obi, Dana Whitfield.', 6)(page);
		await scroll('top')(page);
	} },
	// Journey 3: the read fails or is slow, and the person retries where a retry can help.
	{ name: '8-read-error', doc: 'reply', context: failed('unreadable'), act: async (page) => {
		await expect(page.getByRole('alert').filter({ hasText: 'Couldn’t load the earlier emails' })).toBeVisible();
		await expect(page.locator('.email-conversation').getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
	} },
	{ name: '8b-account-signed-out', doc: 'reply', context: failed('account'), act: async (page) => {
		await expect(page.getByRole('alert').filter({ hasText: 'This draft’s email account is signed out or removed. Reconnect it in Settings › Connected apps.' })).toBeVisible();
		await expect(page.locator('.email-retry')).toHaveCount(0);
	} },
	{ name: '10-loading', doc: 'reply', context: 'hold', act: async (page) => { await expect(page.locator('.email-conversation [role="status"]')).toHaveText('Loading the earlier emails…'); } },
	{ name: '12-retry-works', doc: 'reply', context: (_files, reads) => reads === 0 ? failed('unreadable') : reply(), act: async (page) => {
		await page.locator('.email-conversation').getByRole('button', { name: 'Retry', exact: true }).click();
		await expectConversation(page, 4, 0);
		await expect(page.locator('.email-conversation [role="alert"]')).toHaveCount(0);
	} },
	// A new email, and the attachment chips.
	{ name: '5-new-email', doc: 'fresh', context: fresh(), thin: true, act: async (page) => { await expect(page.locator('.email-conversation')).toHaveCount(0); } },
	{ name: '5c-attachments', doc: 'reply', thin: true, context: (files) => reply({ attachments: attachments(files, [['north-trailhead-map.png', 412_000], ['volunteer-waiver.pdf', 1_840_000]]) }), act: toChips },
	{ name: '5d-attachment-image-opened', doc: 'reply', thin: true, context: (files) => reply({ attachments: attachments(files, [['north-trailhead-map.png', 412_000], ['volunteer-waiver.pdf', 1_840_000]]) }), act: async (page) => {
		await chipFor(page, 'north-trailhead-map.png').getByRole('button').first().click();
		await expect(page.locator('[role="tab"]').filter({ hasText: 'north-trailhead-map.png' })).toBeVisible({ timeout: 10_000 });
		await page.waitForTimeout(1500);
	} },
	{ name: '14-attachment-unavailable', doc: 'reply', thin: true, context: (files) => reply({ attachments: attachments(files, [['north-trailhead-map.png', 412_000], ['volunteer-waiver.pdf', 1_840_000, true]]) }), act: async (page) => {
		await expect(chipFor(page, 'volunteer-waiver.pdf')).toContainText('Unavailable');
		await toChips(page);
	} },
	{ name: '17-attachments-long', doc: 'reply', thin: true, context: (files) => reply({ attachments: attachments(files, [[LONG_PDF, 3_120_000], [LONG_PNG, 412_000], ['waiver.pdf', 1_840_000]]) }), act: async (page) => {
		await expect(page.locator('.email-attachments .attachment')).toHaveCount(3);
		await toChips(page);
	} },
];

/** Seeds the scenario's chat and draft, opens it beside a real chat turn, and returns the page with the draft showing. */
async function seedAndOpen(request: APIRequestContext, browser: Browser, testInfo: TestInfo, scenario: Scenario, workspace: string, stamp: number, vp: typeof VIEWPORTS[number], theme: typeof THEMES[number]) {
	const variant = `${scenario.name}-${vp.name}-${theme}-${stamp}`;
	const threadId = await createSession(request, CHAT_TITLE);
	// The draft tools name a draft email-<12 hex>.md in the draft folder (`connections/mail/email_document.py` EMAIL_DRAFT_FOLDER);
	// the draft record, not the folder, makes it an email draft, which `markEmailDraft` stands in for below.
	const name = `email-${createHash('md5').update(variant).digest('hex').slice(0, 12)}.md`;
	const docPath = path.join(workspace, 'email-drafts', name);
	const files = path.join(workspace, `work-day-files-${createHash('md5').update(variant).digest('hex').slice(0, 8)}`);
	fs.mkdirSync(files, { recursive: true });
	fs.writeFileSync(path.join(files, 'north-trailhead-map.png'), placeholderPng(640, 400));
	for (const pdf of ['volunteer-waiver.pdf', 'waiver.pdf', LONG_PDF]) fs.writeFileSync(path.join(files, pdf), BLANK_PDF);
	fs.writeFileSync(path.join(files, LONG_PNG), placeholderPng(640, 400));
	const seeded = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: DOCS[scenario.doc], thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id as string;
	const context = await browser.newContext({
		baseURL: testInfo.project.use.baseURL, viewport: vp.viewport, colorScheme: theme, reducedMotion: 'reduce',
		deviceScaleFactor: vp.phone ? 2 : 1, isMobile: vp.phone, hasTouch: vp.phone,
	});
	const page = await context.newPage();
	const answer = scenario.context;
	const reads = await mockEmailDraftContext(page, (id, count) => id !== artifactId ? fresh() : typeof answer === 'function' ? answer(files, count) : answer);
	await markEmailDraft(page, docPath, REFUSED[scenario.doc] ?? []);
	// The mock backend's one model row is named `Mock Test Model`; the frame shows the catalog name a person sees instead (fixture rename only).
	await page.route((url) => url.pathname === '/api/models', async (route) => {
		const response = await route.fetch();
		const body = await response.json();
		const rows = (body.models ?? []).map((row: Record<string, unknown>) => row.litellm_model === 'mock/test-model' ? { ...row, name: FRAME_MODEL_NAME, display_name: FRAME_MODEL_NAME } : row);
		await route.fulfill({ response, json: { ...body, models: rows } });
	});
	await page.route((url) => /^\/api\/threads\/[^/]+\/messages/.test(url.pathname), async (route) => {
		const response = await route.fetch();
		await route.fulfill({ response, body: (await response.text()).replaceAll('Mock Test Model', FRAME_MODEL_NAME) });
	});
	// The canvas tab is restored only once `email-frames-open` is set, after the chat turn below has run.
	await page.addInitScript(({ threadId, docPath, name, artifactId, theme }) => {
		localStorage.setItem('corsia_theme', theme);
		if (localStorage.getItem('email-frames-open') !== '1') return;
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [threadId]: {
			tabs: [{ instanceId: 'email-draft', artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: 'email-draft', visible: true, activeSurface: 'artifact',
		} }, splitWidth: 640 }));
	}, { threadId, docPath, name, artifactId, theme });
	const openThread = async () => {
		const item = page.locator('.thread-item').filter({ hasText: CHAT_TITLE });
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
	await loginByUrl(page);
	await openThread();
	// A real turn through the mock model, so the chat beside the draft holds a user message and a final reply.
	const chat = CHATS[scenario.doc];
	await seedMockResponses(request, [{ content: chat.assistant }]);
	await sendChatMessage(page, chat.user);
	await expect(lastAssistantMessage(page)).toContainText(chat.tail);
	await page.evaluate(() => localStorage.setItem('email-frames-open', '1'));
	await page.reload();
	await openThread();
	await expect(page.locator('.cm-content')).toContainText('Alex Rivera');
	await expect.poll(() => reads.reads(artifactId), { message: 'the view read the draft context' }).toBeGreaterThan(0);
	return { page, context, docPath, threadId };
}

test.describe('@surface:email-draft-view frames', () => {
	test.skip(!OUT, 'set EMAIL_FRAMES_OUT to capture');

	// Focus (the default) hides a finished reply behind "Worked"; the frames read the raw transcript, and restore Focus after.
	test.beforeEach(async ({ request }) => { await setFocusMode(request, false); });
	test.afterEach(async ({ request }) => { await setFocusMode(request, true); });

	test('capture stills', async ({ request, browser }, testInfo) => {
		test.setTimeout(Number(process.env.EMAIL_FRAMES_TIMEOUT_MS ?? 3_600_000));
		fs.mkdirSync(OUT, { recursive: true });
		const tree = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
		expect(tree.ok()).toBeTruthy();
		const workspace = (await tree.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
		const stamp = Date.now();
		for (const scenario of SCENARIOS) {
			if (ONLY && !ONLY.includes(scenario.name)) continue;
			for (const vp of VIEWPORTS) for (const theme of THEMES) {
				if (scenario.thin && !((vp.name === '1440' && theme === 'light') || (vp.name === '393' && theme === 'dark'))) continue;
				if (VARIANTS && !VARIANTS.includes(`${vp.name}-${theme}`)) continue;
				const { page, context, docPath, threadId } = await seedAndOpen(request, browser, testInfo, scenario, workspace, stamp, vp, theme);
				await page.waitForTimeout(600);
				if (scenario.act) await scenario.act(page, docPath, request);
				await page.waitForTimeout(400);
				if (!scenario.keepPointer) await page.mouse.move(2, 2);
				// The build stamp is a dev overlay: the frame leaves it out, and the head it names goes into the frame's .state.json.
				const build = await page.locator('.version-overlay').first().textContent({ timeout: 3000 }).catch(() => null);
				await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
				const flat = build?.replace(/\s+/g, ' ').trim() ?? null;
				fs.writeFileSync(path.join(OUT, `after-${scenario.name}-${vp.name}-${theme}.state.json`), JSON.stringify({ head: flat?.match(/^b ([0-9a-f]{7}[†?]?) /)?.[1] ?? null, build: flat }));
				await page.screenshot({ path: path.join(OUT, `after-${scenario.name}-${vp.name}-${theme}.png`), animations: 'disabled' });
				await context.close();
				// Each frame starts from a fresh state: its own chat is the only one in the sidebar.
				const gone = await request.delete(`/api/threads/${threadId}`, { params: { token: TEST_TOKEN } });
				expect(gone.ok(), await gone.text()).toBeTruthy();
			}
		}
	});
});

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

// A one-page blank PDF: the chips only show its name and size, so its content carries no text that could disagree with a name.
const BLANK_PDF = Buffer.from('%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n'
	+ '3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n');
