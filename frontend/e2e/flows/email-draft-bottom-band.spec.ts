import path from 'node:path';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, replacePreviousFixtureThread, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { FOUR, NEWER, markEmailDraft, mockEmailDraftContext, reply } from '../support/emailDraftContext';
import { isolatedDb } from '../support/heldComments';

// An email draft scrolls as one column (the draft, then the earlier emails), not inside its editor. The bottom notices (a
// delivery note and the jump control) float over that column, so with a notice showing, the column scrolled to its end must
// still bring its last line out from under it, with room left for the other notice stacked on top, and nothing may move when
// the notice leaves. On a phone, a line the caret moves onto while the delivery note shows stops above the note, in a document and
// in an email draft alike, and above the jump control too when it stands over the note.
const TAG = '@surface:email-draft-bottom-band';
const BODY = [
	'From: Alex Rivera <alex.rivera@example.com>', 'To: dana.whitfield@example.com', 'Subject: Re: Saturday work day: tools and parking', '',
	'Hi Dana,', '',
	'Thanks for the count. Twelve of us can come Saturday, so the two extra pairs of loppers from the shed will be plenty.', '',
	...Array.from({ length: 8 }, (_, i) => [`Item ${i + 1}: I will bring the sign-in sheet, a cooler of water and the spare gloves, and check the gate code at the north trailhead lot before everyone arrives.`, '']).flat(),
	'Thanks,', 'Alex', '',
].join('\n');

const WIDTHS = [
	{ width: 1440, height: 900, mobile: false },
	{ width: 900, height: 900, mobile: false },
	{ width: 393, height: 852, mobile: true },
];

/** The reader's comment on a line near the draft's end with an agent's unread reply, so the jump control points down to it. */
async function unreadBelow(request: Parameters<typeof createSession>[0], artifactId: string, thread: string) {
	const quote = 'Item 8: I will bring the sign-in sheet';
	const from = BODY.indexOf(quote);
	const made = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: 'Can we confirm the gate code?', anchor_text: quote, from_off: from, to_off: from + quote.length, delivery_policy: 'immediate' },
	});
	expect(made.ok(), await made.text()).toBeTruthy();
	isolatedDb([
		"connection.execute(\"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Reviewer', 'agent', ?, 'The code is 4417.', strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 second'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 second'))\", (sys.argv[2], sys.argv[3], sys.argv[4]))",
	], artifactId, (await made.json()).comment.id as string, thread);
}

async function openDraft(page: Page, request: Parameters<typeof createSession>[0], label: string, email = true, unread = false) {
	const title = `Email band ${label} ${Date.now().toString(36)}`;
	const thread = await createSession(request, title);
	await replacePreviousFixtureThread(request, thread);
	const params = { token: TEST_TOKEN };
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots as Array<{ kind: string; path: string }>;
	const workspace = roots.find(root => root.kind === 'workspace')?.path;
	if (!workspace) throw new Error('Disposable workspace was not returned by the mock backend');
	const name = `email-${thread.replace(/[^0-9a-f]/g, '').padEnd(12, '0').slice(0, 12)}.md`;
	const docPath = path.join(workspace, 'email-drafts', name);
	const saved = await request.put('/api/files/content', { params, data: { path: docPath, text: BODY, thread_id: thread, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	if (unread) await unreadBelow(request, artifactId, thread);
	if (email) {
		await mockEmailDraftContext(page, () => reply({ conversation: [...NEWER[6], ...FOUR] }));
		await markEmailDraft(page, docPath);
	}
	await page.addInitScript(({ thread, artifactId, docPath, name }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [thread]: { tabs: [{ instanceId: 'email-band', artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'email-band', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
	}, { thread, artifactId, docPath, name });
	await loginByUrl(page, { waitForComposer: false });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	await expect(page.locator('.cm-content')).toContainText('Alex Rivera');
	if (email) await expect(page.locator('.email-conversation')).toBeVisible();
	return { artifactId };
}

const column = (page: Page) => page.locator('.doc-body.email-scroll');

async function scrollToEnd(page: Page) {
	await column(page).evaluate((node) => { node.scrollTop = node.scrollHeight; });
	await page.waitForTimeout(150);
}

/**
 * Where the column's last line ends and where the topmost bottom notice starts, in the window, and how much higher that top
 * would sit with the jump control stacked over the note (its touch target plus the band's gap).
 */
function measure(page: Page) {
	return column(page).evaluate((node) => {
		const box = node.getBoundingClientRect();
		const pad = parseFloat(getComputedStyle(node).paddingBottom) || 0;
		const band = document.querySelector<HTMLElement>('[data-doc-bottom-band]');
		const notices = [...document.querySelectorAll('[data-doc-bottom-band] [data-doc-jump], [data-doc-bottom-band] .delivery-confirmation')]
			.map((el) => el.getBoundingClientRect()).filter((rect) => rect.height > 0);
		const bandStyle = band ? getComputedStyle(band) : null;
		return {
			atEnd: node.scrollTop + node.clientHeight >= node.scrollHeight - 1,
			scrolls: node.scrollHeight > node.clientHeight,
			lastLine: box.top + node.scrollHeight - pad - node.scrollTop,
			noticeTop: Math.min(...notices.map((rect) => rect.top)),
			notices: notices.length,
			secondNotice: (parseFloat(bandStyle?.getPropertyValue('--touch-target') ?? '') || 44) + (parseFloat(bandStyle?.rowGap ?? '') || 0),
		};
	});
}

for (const { width, height, mobile } of WIDTHS) {
	test(`${TAG} ${width}px: the last line of an email draft scrolls clear of a delivery note and a jump control over it, and stays put when the note leaves`, async ({ page, request }, info) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || mobile !== info.project.name.endsWith('mobile'), 'Each device project checks its own width.');
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height });
		const { artifactId } = await openDraft(page, request, `${width}`);
		// The delivery note lasts three seconds, so it is shown and measured inside that time.
		await page.evaluate(async (id) => {
			const { deliveryConfirmation } = await import('/src/lib/stores/deliveryConfirmation.ts');
			deliveryConfirmation.show(id, 2);
		}, artifactId);
		await expect(page.locator('[data-doc-bottom-band] .delivery-confirmation')).toBeVisible();
		await scrollToEnd(page);
		const at = await measure(page);

		expect(at.notices, 'the delivery note shows').toBe(1);
		expect(at.scrolls && at.atEnd, 'the column scrolls and is at its end').toBe(true);
		expect(at.lastLine, 'the last line ends above the note').toBeLessThanOrEqual(at.noticeTop);
		expect(at.lastLine, 'and above a jump control stacked over the note').toBeLessThanOrEqual(at.noticeTop - at.secondNotice);

		await page.evaluate(async () => {
			const { deliveryConfirmation } = await import('/src/lib/stores/deliveryConfirmation.ts');
			deliveryConfirmation.dismiss();
		});
		await expect(page.locator('[data-doc-bottom-band]')).toHaveCount(0);
		await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
		const after = await measure(page);
		expect(after.lastLine, 'the text does not move when the note leaves').toBeCloseTo(at.lastLine, 0);
	});
}

/** The bottom of the row holding the caret, in the window: a wrapped paragraph is one line element over several rows. */
function caretLineBottom(page: Page) {
	return page.evaluate(() => {
		const selection = getSelection();
		if (!selection?.rangeCount) return null;
		const rect = selection.getRangeAt(0).getBoundingClientRect();
		if (rect.height > 0) return rect.bottom;
		// An empty line has no text to measure a caret in; it is a single row.
		const node = selection.focusNode;
		const line = (node instanceof Element ? node : node?.parentElement)?.closest('.cm-line');
		return line ? line.getBoundingClientRect().bottom : null;
	});
}

// The stack is checked in a document: in an email draft the jump control does not yet stay up once the column scrolls (it reads
// the editor's own box, which grows to its text), and the float there is the same measure.
for (const [email, stacked] of [[false, false], [false, true], [true, false]] as const) {
	const over = stacked ? 'the jump control stacked over the delivery note' : 'the delivery note';
	test(`${TAG} 393px: in ${email ? 'an email draft' : 'a document'}, a line the caret moves onto stops above ${over} while it shows`, async ({ page, request }, info) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'), 'A phone-width check.');
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 393, height: 852 });
		const { artifactId } = await openDraft(page, request, `caret-${email ? 'email' : 'doc'}${stacked ? '-stacked' : ''}`, email, stacked);
		const jump = page.locator('[data-doc-bottom-band] [data-doc-jump]');
		if (stacked) await expect(jump, 'the jump control points at the unread comment below').toBeVisible();
		const scroller = page.locator(email ? '.doc-body.email-scroll' : '.doc .cm-scroller');
		const foot = await scroller.evaluate((node) => node.getBoundingClientRect().bottom);
		// With no notice showing, walk the caret down to the foot of the view.
		await page.locator('.cm-line').first().click();
		for (let step = 0; step < 120; step++) {
			const bottom = await caretLineBottom(page);
			if (bottom !== null && bottom > foot - 100) break;
			await page.keyboard.press('ArrowDown');
		}
		const before = await scroller.evaluate((node) => node.scrollTop);
		// The delivery note lasts three seconds, so the caret moves and is measured inside that time.
		await page.evaluate(async (id) => {
			const { deliveryConfirmation } = await import('/src/lib/stores/deliveryConfirmation.ts');
			deliveryConfirmation.show(id, 2);
		}, artifactId);
		const note = page.locator('[data-doc-bottom-band] .delivery-confirmation');
		await expect(note).toBeVisible();
		// Keys go once the note has been placed and measured (the float follows the band a frame after it changes size).
		await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
		for (let step = 0; step < 3; step++) await page.keyboard.press('ArrowDown');
		await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
		const lineBottom = await caretLineBottom(page);
		const noticeTop = await page.locator('[data-doc-bottom-band]').evaluate((band) =>
			Math.min(...[...band.querySelectorAll('[data-doc-jump], .delivery-confirmation')].map((el) => el.getBoundingClientRect().top)));

		await expect(note, 'the note still shows').toBeVisible();
		if (stacked) await expect(jump, 'the jump control still stands over the note').toBeVisible();
		expect(await scroller.evaluate((node) => node.scrollTop), 'the view scrolled to follow the caret').toBeGreaterThan(before);
		expect(lineBottom, `the caret line ends above ${over}`).not.toBeNull();
		expect(lineBottom ?? Infinity, `the caret line ends above ${over}`).toBeLessThanOrEqual(noticeTop);
	});
}
