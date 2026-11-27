import fs from 'node:fs';
import path from 'node:path';
import { type Locator, type Page } from '@playwright/test';
import { expect, test } from '../support/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { waitForMainSlideToSettle } from '../support/sidebar';
import { openComments } from '../support/commentDrawer';
import { putSheetAway } from '../support/docImageComments';
import { expectFailureLook } from '../support/failureLook';

/**
 * A reply half typed on a document comment is still there, with its caret, after the person goes to another conversation and
 * comes back: the comment surface is rebuilt on return, and the reply is kept outside it until it is posted or cleared. A reply
 * whose post is refused says why and keeps its text; a transient failure waits to send again. Taken at 1440 (the margin beside the document) and at 393 (the
 * phone's thread sheet, opened by tapping the highlight).
 */

const TAG = '@flow:doc-reply-draft-kept @surface:documents';
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 393, height: 852 };
/** Frames for the review of the failure line, in both themes, when set; CI keeps its own artifacts. */
const FRAMES = process.env.CORSIA_REPLY_FRAMES;

const QUOTE = 'Volunteers meet at the north gate at eight and bring their own gloves and water.';
const DOC = ['# Trail day', '', 'The crew walks the loop on Saturday.', '', QUOTE, '', 'Lunch is at the picnic tables by the creek.', ''].join('\n');
const DRAFT = 'Half a reply about the gate and the gloves';
/** How far back from the end the caret is moved before leaving, so its place is not just the end. */
const BACK = 6;

type Seeded = { title: string; other: string; id: string; artifactId: string };

/** A comment taller than the thread's popover at 1440 and its sheet at 393, so either scrolls its own reply field out of view (`long`). */
const LONG_COMMENT = ['Should we say where to park?', ...Array.from({ length: 30 }, (_, i) => `Point ${i + 1}: the lot by the north gate fills early, so say which street to use.`)].join('\n\n');

/**
 * A document with one comment on QUOTE in its own conversation, a second conversation, and the document open in the first.
 * `long` makes the comment taller than the popover or sheet its thread opens in.
 */
async function seed(page: Page, long = false): Promise<Seeded> {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `reply-kept-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const other = `${title}-other`;
	await createSession(request, other);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const from = DOC.indexOf(QUOTE);
	const created = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params, data: { delivery_policy: 'immediate', body: long ? LONG_COMMENT : 'Should we say where to park?', anchor_text: QUOTE, from_off: from, to_off: from + QUOTE.length },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const id: string = (await created.json()).comment.id;
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'reply-kept-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'reply-kept-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	return { title, other, id, artifactId };
}

const docText = (page: Page) => page.locator('.cm-content', { hasText: 'Trail day' });
const mark = (page: Page) => page.locator('.cm-comment-anchor', { hasText: 'Volunteers meet' }).first();
const sheet = (page: Page) => page.locator('[data-adaptive-drawer] .drawer-panel');
/** The thread itself, in the margin at 1440 and alone in the phone's sheet at 393. */
const card = (page: Page, id: string) => page.locator(`[data-thread-id="${id}"]`).first();
const field = (page: Page, id: string) => card(page, id).getByPlaceholder('Reply…');
const phone = (page: Page) => (page.viewportSize()?.width ?? 0) <= 768;
/** The reply POST for thread `id` is held until `fail()`, which fails it as a dropped connection. */
async function holdReply(page: Page, id: string, refusal = false) {
	let release!: () => void, seen!: () => void, finished!: () => void;
	let clientId = '', posts = 0, restored = false;
	const held = new Promise<void>((resolve) => { release = resolve; });
	const arrived = new Promise<void>((resolve) => { seen = resolve; });
	const dropped = new Promise<void>((resolve) => { finished = resolve; });
	await page.route('**/api/artifacts/*/comments*', async (route) => {
		const request = route.request();
		const body = request.method() === 'POST' ? request.postDataJSON() as { parent_id?: string; client_id?: string } | null : null;
		if (body?.parent_id !== id) return route.continue();
		posts++;
		if (posts > 1) return restored ? route.continue() : route.abort('failed');
		clientId = body.client_id ?? '';
		seen();
		await held;
		if (refusal) await route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: 'Cannot post this reply' }) });
		else await route.abort('failed');
		finished();
	});
	return { arrived, dropped, fail: release, restore: () => { restored = true; }, clientId: () => clientId };
}

async function sentOnce(page: Page, artifactId: string, parentId: string, clientId: string) {
	expect(clientId).not.toBe('');
	await expect.poll(async () => {
		const response = await page.request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } });
		expect(response.ok()).toBeTruthy();
		const { comments } = await response.json();
		const parent = comments.find((item: { id: string }) => item.id === parentId);
		return (parent?.replies ?? []).filter((item: { body: string }) => item.body === DRAFT).map((item: { id: string }) => item.id);
	}).toEqual([clientId]);
}

async function waitingReply(page: Page, id: string, reply: Locator) {
	await expect(reply, 'the text is kept').toHaveValue(DRAFT);
	await expect(card(page, id).getByRole('status').filter({ hasText: 'Sending…' })).toHaveCount(1);
	await expect(card(page, id).getByRole('alert')).toHaveCount(0);
	await expect(failToast(page)).toHaveCount(0);
}
const failToast = (page: Page) => page.locator('.toast').filter({ hasText: /post/i });
const painted = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));

/**
 * Opens a conversation from the navigation, as a person does at this width. On a phone the thread's sheet is modal over the
 * header's sidebar control, so a person puts it away first.
 */
async function goTo(page: Page, title: string) {
	if (phone(page)) {
		if (await sheet(page).count()) await putSheetAway(page);
		const chat = page.getByRole('button', { name: 'Chat', exact: true });
		if (await chat.isVisible()) await chat.tap();
		await page.getByRole('button', { name: /^Toggle sidebar/ }).tap();
		await expect(page.locator('aside.sidebar')).toHaveClass(/\bopen\b/);
		await waitForMainSlideToSettle(page);
		await sidebarThread(page, title).tap();
		await expect(page.locator('aside.sidebar')).not.toHaveClass(/\bopen\b/);
		await waitForMainSlideToSettle(page);
	} else {
		await sidebarThread(page, title).click();
	}
}

/** On a phone returning to chat, open its document panel as the person does. */
async function showDocument(page: Page) {
	if (phone(page) && await page.getByRole('button', { name: 'Toggle panel', exact: true }).isVisible()) {
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).tap();
		await page.getByRole('button', { name: /^reply-kept-.*\.md / }).tap();
		await waitForMainSlideToSettle(page);
	}
	await expect(docText(page)).toBeVisible({ timeout: 15_000 });
}

/** The comment's thread opened and its reply field showing: the margin card pressed at 1440, the highlight tapped at 393. */
async function openThread(page: Page, id: string): Promise<Locator> {
	await showDocument(page);
	await expect(mark(page)).toBeVisible({ timeout: 15_000 });
	if (phone(page)) {
		const line = await mark(page).evaluate((el) => { const r = el.getClientRects()[0]; return { x: r.left + Math.min(24, r.width / 2), y: r.top + r.height / 2 }; });
		await page.touchscreen.tap(line.x, line.y);
		await expect(sheet(page)).toHaveCount(1);
	} else {
		await openComments(page);
		await card(page, id).locator('[data-discussion-original]').click();
	}
	const reply = field(page, id);
	await expect(reply).toBeVisible();
	return reply;
}

/**
 * What lies under a row of the card: `gap` is the space from the row's bottom to the card's bottom edge (what the card puts
 * under its last row), `inside` the room left between the row and the bottom of its scroller's visible box, and `pad` that
 * scroller's own bottom padding. Given the reply field, the row is the field's whole box in the card, not its text area.
 */
function spaceBelow(target: Locator) {
	return target.evaluate((element) => {
		let row: Element = element;
		if (element instanceof HTMLTextAreaElement) while (row.parentElement && !row.parentElement.matches('[data-thread-id], .ct-res-body')) row = row.parentElement;
		let scroller = row.parentElement;
		while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
		if (!scroller) throw new Error('no scroller above the card');
		const box = scroller.getBoundingClientRect(), bottom = row.getBoundingClientRect().bottom, top = box.top + scroller.clientTop;
		return {
			gap: row.closest('[data-comment-card]')!.getBoundingClientRect().bottom - bottom,
			inside: top + scroller.clientHeight - bottom,
			pad: parseFloat(getComputedStyle(scroller).paddingBottom),
		};
	});
}

async function frame(page: Page, name: string) {
	if (!FRAMES) return;
	fs.mkdirSync(FRAMES, { recursive: true });
	for (const scheme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: scheme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
		await painted(page);
		await page.screenshot({ path: path.join(FRAMES, `${name}-${page.viewportSize()!.width}-${scheme}.png`), animations: 'disabled' });
	}
	await page.emulateMedia({ colorScheme: 'light' });
}

for (const [label, size] of [['1440', DESKTOP], ['393', PHONE]] as const) {
	test.describe(`${TAG} ${label}`, () => {
		test.use(size === PHONE ? { viewport: PHONE, hasTouch: true, isMobile: true } : { viewport: DESKTOP });
		test.beforeEach(({}, testInfo) => {
			const project = size === PHONE ? 'chromium-mobile' : 'chromium-desktop';
			test.skip(testInfo.project.name !== project, `Taken at ${label} in ${project}`);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			test.setTimeout(150_000);
		});

		test('a reply being typed is still there, caret and all, after going to another conversation and back', async ({ page }) => {
			const { title, other, id } = await seed(page);
			let posts = 0;
			await page.route('**/api/artifacts/*/comments*', async (route) => {
				const request = route.request();
				if (request.method() === 'POST' && request.postDataJSON()?.parent_id === id) posts++;
				await route.continue();
			});
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			await reply.click();
			await reply.pressSequentially(DRAFT);
			for (let i = 0; i < BACK; i += 1) await reply.press('ArrowLeft');
			const caret = DRAFT.length - BACK;
			expect(await reply.evaluate((el: HTMLTextAreaElement) => el.selectionStart)).toBe(caret);

			await goTo(page, other);
			await expect(docText(page), 'the document is gone with its conversation').toHaveCount(0);
			await goTo(page, title);
			const back = await openThread(page, id);
			await expect(back, 'the reply is kept').toHaveValue(DRAFT);
			expect(await back.evaluate((el: HTMLTextAreaElement) => [el.selectionStart, el.selectionEnd]), 'the caret is where it was left').toEqual([caret, caret]);
			expect(posts, 'the unpressed reply was never posted').toBe(0);
		});

		test('a dropped reply stays in its field and sends on return', async ({ page }) => {
			const { title, id, artifactId } = await seed(page);
			const wire = await holdReply(page, id);
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			await reply.fill(DRAFT);
			await card(page, id).getByRole('button', { name: 'Send reply' }).click();
			await wire.arrived;
			wire.fail();
			await wire.dropped;
			await waitingReply(page, id, reply);
			wire.restore();
			await page.evaluate(() => window.dispatchEvent(new Event('focus')));
			await sentOnce(page, artifactId, id, wire.clientId());
			await expect(reply).toHaveValue('');
		});

		test('a dropped reply stays kept while away and sends on return', async ({ page }) => {
			const { title, other, id, artifactId } = await seed(page);
			const wire = await holdReply(page, id);
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			await reply.fill(DRAFT);
			await card(page, id).getByRole('button', { name: 'Send reply' }).click();
			await wire.arrived;
			await goTo(page, other);
			await expect(docText(page)).toHaveCount(0);
			wire.fail();
			await wire.dropped;
			await expect(failToast(page)).toHaveCount(0);
			await goTo(page, title);
			const back = await openThread(page, id);
			await waitingReply(page, id, back);
			wire.restore();
			await goTo(page, other);
			await goTo(page, title);
			await showDocument(page);
			await sentOnce(page, artifactId, id, wire.clientId());
			const returned = await openThread(page, id);
			await expect(returned).toHaveValue('');
		});

		test('a dropped reply scrolled away stays kept and sends on reconnect', async ({ page }) => {
			const { title, id, artifactId } = await seed(page, true);
			const wire = await holdReply(page, id);
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			await card(page, id).getByRole('button', { name: 'Show more' }).click();
			await reply.fill(DRAFT);
			await card(page, id).getByRole('button', { name: 'Send reply' }).click();
			await wire.arrived;
			const over = await reply.evaluate((element) => {
				let scroller = element.parentElement;
				while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
				if (!scroller) throw new Error('no scroller above the reply field');
				const box = scroller.getBoundingClientRect(), top = Math.max(box.top, 0), bottom = Math.min(box.bottom, window.innerHeight);
				return { x: box.left + box.width / 2, y: (top + bottom) / 2 };
			});
			await page.mouse.move(over.x, over.y);
			const outside = () => reply.evaluate((element) => {
				const box = element.getBoundingClientRect();
				let scroller = element.parentElement;
				while (scroller && !/(auto|scroll)/.test(`${getComputedStyle(scroller).overflowX} ${getComputedStyle(scroller).overflowY}`)) scroller = scroller.parentElement;
				const clip = scroller?.getBoundingClientRect();
				return Math.min(box.bottom, window.innerHeight, clip?.bottom ?? Infinity) <= Math.max(box.top, 0, clip?.top ?? 0);
			}).catch(() => true);
			for (let i = 0; i < 20 && !(await outside()); i += 1) { await page.mouse.wheel(0, -600); await painted(page); }
			expect(await outside(), 'the reply field is out of view before the post fails').toBe(true);
			wire.fail();
			await wire.dropped;
			await waitingReply(page, id, reply);
			wire.restore();
			await page.evaluate(() => window.dispatchEvent(new Event('focus')));
			await sentOnce(page, artifactId, id, wire.clientId());
			await expect(reply).toHaveValue('');
		});

		test('a reply whose post is refused says why under its field, and only there, and keeps its text', async ({ page }) => {
			const { title, id } = await seed(page);
			await page.route('**/api/artifacts/*/comments*', (route) => {
				const request = route.request();
				if (request.method() === 'POST' && (request.postDataJSON() as { parent_id?: string } | null)?.parent_id === id) return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: 'Cannot post this reply' }) });
				return route.continue();
			});
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			await reply.click();
			await reply.pressSequentially(DRAFT);
			await painted(page);
			const before = await spaceBelow(reply);
			await card(page, id).getByRole('button', { name: 'Send reply' }).click();
			const reason = card(page, id).getByRole('alert');
			await expect(reason, 'the reason shows under the field').toBeVisible({ timeout: 30_000 });
			await painted(page);
			await frame(page, 'reply-failed');
			await expect(page.locator('.toast').filter({ hasText: /post/i }), 'the field is on screen, so no toast tells it a second time').toHaveCount(0);
			await expect(reply, 'the text stays in the field').toHaveValue(DRAFT);
			expect(await reason.evaluate((line) => {
				const probe = document.createElement('span');
				probe.style.cssText = 'color: var(--red-text); font-size: var(--text-footnote)';
				line.parentElement!.append(probe);
				const want = getComputedStyle(probe), got = getComputedStyle(line);
				const looks = [got.color === want.color, got.fontSize === want.fontSize];
				probe.remove();
				return looks;
			}), 'the reason has the card’s error look: footnote size, error colour').toEqual([true, true]);
			const after = await spaceBelow(reason);
			expect(Math.abs(after.gap - before.gap), 'the space under the reason is the space that was under the field').toBeLessThanOrEqual(1);
			if (phone(page)) {
				expect(after.inside, 'the reason ends inside the sheet, above its bottom padding').toBeGreaterThanOrEqual(after.pad - 0.5);
			}
		});

		test('a reply whose post fails after going to another conversation says why in a toast, and under its field on return', async ({ page }, testInfo) => {
			const { title, other, id } = await seed(page);
			const wire = await holdReply(page, id, true);
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			await reply.click();
			await reply.pressSequentially(DRAFT);
			await card(page, id).getByRole('button', { name: 'Send reply' }).click();
			await wire.arrived;
			await goTo(page, other);
			await expect(docText(page), 'the field is gone with its conversation').toHaveCount(0);
			wire.fail();
			await expect(failToast(page), 'no field is on screen, so a toast says why').toBeVisible({ timeout: 30_000 });
			await frame(page, 'reply-away-toast');
			await failToast(page).getByRole('button', { name: 'Dismiss' }).click();
			await expect(failToast(page)).toHaveCount(0);
			await goTo(page, title);
			const back = await openThread(page, id);
			await expect(back, 'the text is kept').toHaveValue(DRAFT);
			await expect(card(page, id).getByRole('alert'), 'the field says why on return').toBeVisible();
			await expectFailureLook(page, testInfo, card(page, id).getByRole('alert'), 'reply field');
			await frame(page, 'reply-returned-reason');
		});

		test('a reply whose field is scrolled out of view when its post fails says why in a toast', async ({ page }) => {
			// The thread opens in a popover at 1440 (the chat beside the document leaves no room for the margin) and in a sheet at
			// 393. Either one scrolls as a whole, so with the long comment shown in full, reading back up to its start takes the field
			// out of view.
			const { title, id } = await seed(page, true);
			const wire = await holdReply(page, id, true);
			await loginByUrl(page, { waitForComposer: false });
			await goTo(page, title);
			const reply = await openThread(page, id);
			// The person wrote the comment, so it is read and a long one folds; showing it all is what makes the thread scroll.
			await card(page, id).getByRole('button', { name: 'Show more' }).click();
			await reply.click();
			await reply.pressSequentially(DRAFT);
			await card(page, id).getByRole('button', { name: 'Send reply' }).click();
			await wire.arrived;
			// The wheel goes over the middle of what shows of the field's own scroller (the popover or sheet), so it scrolls that.
			const over = await reply.evaluate((element) => {
				let scroller = element.parentElement;
				while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
				if (!scroller) throw new Error('no scroller above the reply field');
				const box = scroller.getBoundingClientRect(), top = Math.max(box.top, 0), bottom = Math.min(box.bottom, window.innerHeight);
				return { x: box.left + box.width / 2, y: (top + bottom) / 2 };
			});
			await page.mouse.move(over.x, over.y);
			// Out of view: no part of the field inside both the window and the part of its own scroller that shows.
			const outside = () => reply.evaluate((element) => {
				const box = element.getBoundingClientRect();
				let scroller = element.parentElement;
				while (scroller && !/(auto|scroll)/.test(`${getComputedStyle(scroller).overflowX} ${getComputedStyle(scroller).overflowY}`)) scroller = scroller.parentElement;
				const clip = scroller?.getBoundingClientRect();
				const top = Math.max(box.top, 0, clip?.top ?? 0), bottom = Math.min(box.bottom, window.innerHeight, clip?.bottom ?? Infinity);
				return bottom <= top;
			}).catch(() => true);
			for (let i = 0; i < 20 && !(await outside()); i += 1) { await page.mouse.wheel(0, -600); await painted(page); }
			expect(await outside(), 'the reply field is out of view before the post fails').toBe(true);
			wire.fail();
			await expect(failToast(page), 'the field is not on screen, so a toast says why').toBeVisible({ timeout: 30_000 });
			await frame(page, 'reply-scrolled-out-toast');
			await expect(reply, 'the text is kept').toHaveValue(DRAFT);
		});
	});
}
