import fs from 'node:fs';
import path from 'node:path';
import { type Locator, type Page } from '@playwright/test';
import { expect, test } from '../support/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { waitForMainSlideToSettle } from '../support/sidebar';
import { openComments } from '../support/commentDrawer';
import { putSheetAway } from '../support/docImageComments';

/**
 * A reply half typed on a document comment is still there, with its caret, after the person goes to another conversation and
 * comes back: the comment surface is rebuilt on return, and the reply is kept outside it until it is posted or cleared. A reply
 * whose post fails says why under its field and keeps its text. Taken at 1440 (the margin beside the document) and at 393 (the
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

type Seeded = { title: string; other: string; id: string };

/** A document with one comment on QUOTE in its own conversation, a second conversation, and the document open in the first. */
async function seed(page: Page): Promise<Seeded> {
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
		params, data: { delivery_policy: 'immediate', body: 'Should we say where to park?', anchor_text: QUOTE, from_off: from, to_off: from + QUOTE.length },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const id: string = (await created.json()).comment.id;
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'reply-kept-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'reply-kept-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	return { title, other, id };
}

const docText = (page: Page) => page.locator('.cm-content', { hasText: 'Trail day' });
const mark = (page: Page) => page.locator('.cm-comment-anchor', { hasText: 'Volunteers meet' }).first();
const sheet = (page: Page) => page.locator('[data-adaptive-drawer] .drawer-panel');
/** The thread itself, in the margin at 1440 and alone in the phone's sheet at 393. */
const card = (page: Page, id: string) => page.locator(`[data-thread-id="${id}"]`).first();
const field = (page: Page, id: string) => card(page, id).getByPlaceholder('Reply…');
const phone = (page: Page) => (page.viewportSize()?.width ?? 0) <= 768;
const painted = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));

/**
 * Opens a conversation from the navigation, as a person does at this width. On a phone the thread's sheet is modal over the
 * header's sidebar control, so a person puts it away first.
 */
async function goTo(page: Page, title: string) {
	if (phone(page)) {
		if (await sheet(page).count()) await putSheetAway(page);
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

/** The comment's thread opened and its reply field showing: the margin card pressed at 1440, the highlight tapped at 393. */
async function openThread(page: Page, id: string): Promise<Locator> {
	await expect(docText(page)).toBeVisible({ timeout: 15_000 });
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
		});

		test('a reply whose post fails says why under its field and keeps its text', async ({ page }) => {
			const { title, id } = await seed(page);
			await page.route('**/api/artifacts/*/comments*', (route) => {
				const request = route.request();
				if (request.method() === 'POST' && (request.postDataJSON() as { parent_id?: string } | null)?.parent_id === id) return route.abort('failed');
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
			await expect(page.locator('.toast').first(), 'the person is told it did not post').toBeVisible({ timeout: 30_000 });
			await painted(page);
			await frame(page, 'reply-failed');
			await expect(reply, 'the text stays in the field').toHaveValue(DRAFT);
			const reason = card(page, id).getByRole('alert');
			await expect(reason, 'the reason shows under the field').toBeVisible();
			expect(await reason.evaluate((line) => {
				const probe = document.createElement('span');
				probe.style.cssText = 'color: var(--red-text); font-size: var(--text-caption)';
				line.parentElement!.append(probe);
				const want = getComputedStyle(probe), got = getComputedStyle(line);
				const looks = [got.color === want.color, got.fontSize === want.fontSize];
				probe.remove();
				return looks;
			}), 'the reason has the card’s error look: caption size, error colour').toEqual([true, true]);
			const after = await spaceBelow(reason);
			expect(Math.abs(after.gap - before.gap), 'the space under the reason is the space that was under the field').toBeLessThanOrEqual(1);
			if (phone(page)) {
				expect(after.inside, 'the reason ends inside the sheet, above its bottom padding').toBeGreaterThanOrEqual(after.pad - 0.5);
			}
		});
	});
}
