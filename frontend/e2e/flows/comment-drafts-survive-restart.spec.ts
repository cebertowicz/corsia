import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, seedLiveDocument, type LiveDocument } from '../support/liveDocument';
import { beginComment, submitButton } from '../support/liveCommentProof';
import { openComments } from '../support/commentDrawer';
import { at, commentButton, composer, drag, openThread, restoreImageTab, seedImage } from '../support/imageRegionComments';
import { expectFailureLook } from '../support/failureLook';

// An unsent comment or reply is kept on this device: it is still there after a reload, and after the app is quit and opened
// again (a new page over the same storage stands in for that, and for an update restart), until it posts or is cancelled.
// Taken at 1440 (chromium-desktop) and at 393 (chromium-mobile, by touch). The rules behind it (tags, the one client id, sign
// out, the cap, two windows) are proven by the store's unit tests.

const SIZES = { 'chromium-desktop': { width: 1440, height: 900 }, 'chromium-mobile': { width: 393, height: 852 } } as const;
type Project = keyof typeof SIZES;
const phone = (page: Page) => page.viewportSize()!.width <= 768;

const QUOTE = 'Volunteers meet at the north gate';
const DOC = `# Trail day\n\nThe crew walks the loop on Saturday.\n\n${QUOTE} at eight and bring gloves.\n\nLunch is by the creek.\n`;
const PREFIX = 'corsia:commentDraft:v1:';

/** The kept drafts' bodies on this device, as the page wrote them. */
const stored = (page: Page) => page.evaluate((prefix) => Object.keys(localStorage).filter((name) => name.startsWith(prefix))
	.map((name) => (JSON.parse(localStorage.getItem(name)!) as { body: string }).body), PREFIX);

/** A draft longer than one draft may be on this device (200,000 UTF-16 units), in words a person might paste. */
const TOO_LONG = 'The crew walks the loop on Saturday and meets at the north gate. '.repeat(3_100);
const NOT_KEPT = 'Too long to keep on this device. Post it or copy it before you close CORSIA.';
/** Frames of the line under the box, in both themes, when set; taken before the line is asserted, so a build without it gives its frame too. */
const FRAMES = process.env.CORSIA_DRAFT_FRAMES;
async function frame(page: Page, name: string) {
	if (!FRAMES) return;
	fs.mkdirSync(FRAMES, { recursive: true });
	for (const scheme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: scheme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
		await page.screenshot({ path: path.join(FRAMES, `${name}-${page.viewportSize()!.width}-${scheme}.png`), animations: 'disabled' });
	}
	await page.emulateMedia({ colorScheme: 'light' });
}

type WayOut = 'reload' | 'relaunch';
/**
 * The page is left and the app opened again. `reload` reloads at once, with no wait for the draft to be written. `relaunch`
 * closes the page, once the draft is on the device, and opens a new one over the same storage, with nothing of the old page.
 */
async function leave(page: Page, way: WayOut, open: (page: Page) => Promise<void>): Promise<Page> {
	if (way === 'reload') {
		await page.reload();
		await open(page);
		return page;
	}
	await expect.poll(() => stored(page), { message: 'the draft is written to the device' }).not.toEqual([]);
	const next = await page.context().newPage();
	await next.setViewportSize(page.viewportSize()!);
	await page.close();
	await open(next);
	return next;
}

/** The highlight on QUOTE, tapped on a phone; the comment's card pressed in the margin at 1440. */
async function openReply(page: Page, id: string): Promise<Locator> {
	const mark = page.locator('.cm-comment-anchor', { hasText: 'Volunteers meet' }).first();
	await expect(mark).toBeVisible({ timeout: 15_000 });
	if (phone(page)) {
		const line = await mark.evaluate((el) => { const r = el.getClientRects()[0]; return { x: r.left + Math.min(24, r.width / 2), y: r.top + r.height / 2 }; });
		await page.touchscreen.tap(line.x, line.y);
	} else {
		await openComments(page);
		await page.locator(`[data-thread-id="${id}"]`).first().locator('[data-discussion-original]').click();
	}
	const field = page.locator(`[data-thread-id="${id}"]`).first().getByPlaceholder('Reply…');
	await expect(field).toBeVisible();
	return field;
}

/** An image's comment box from (0.2, 0.25) to (0.45, 0.5): a drag with the mouse, or Comment then a tap on a phone. */
async function drawBox(page: Page) {
	if (phone(page)) {
		await commentButton(page).tap();
		const spot = await at(page, 0.2, 0.25);
		await page.touchscreen.tap(spot.x, spot.y);
	} else {
		await drag(page, await at(page, 0.2, 0.25), await at(page, 0.45, 0.5));
	}
	await expect(composer(page)).toBeVisible();
}

async function seedComment(page: Page, document: LiveDocument): Promise<string> {
	const from = document.body.indexOf(QUOTE);
	const created = await page.request.post(`/api/artifacts/${document.artifactId}/comments`, {
		params: { token: TEST_TOKEN }, data: { body: 'Should we say where to park?', anchor_text: QUOTE, from_off: from, to_off: from + QUOTE.length, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).comment.id as string;
}

test.describe('@flow:document-comments an unsent comment outlives the page', () => {
	test.beforeEach(async ({ page }, testInfo) => {
		test.skip(!(testInfo.project.name in SIZES), 'Taken at 1440 and 393 only');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(150_000);
		await page.setViewportSize(SIZES[testInfo.project.name as Project]);
	});

	for (const way of ['reload', 'relaunch'] as const) {
		test(`document: a new comment being typed is there again after a ${way}`, async ({ page, request }) => {
			const document = await seedLiveDocument(request, `drafts-${way}-doc`, DOC);
			await openLiveDocument(page, document);
			await beginComment(page, QUOTE, 'Say which gate has parking');
			await composer(page).press('Escape');
			await expect(composer(page)).toHaveCount(0);
			page = await leave(page, way, (next) => openLiveDocument(next, document).then(() => undefined));
			if (!phone(page)) await beginComment(page, QUOTE);
			await expect(composer(page), 'the text typed before is there').toHaveValue('Say which gate has parking');
			await expect(page.locator('.composer .quote-wrap')).toContainText(QUOTE);
			await expect(page.locator('.composer').getByRole('button', { name: 'Sending…' })).toHaveCount(0);
			const response = await request.get(`/api/artifacts/${document.artifactId}/comments`, { params: { token: TEST_TOKEN } });
			expect((await response.json()).comments).toHaveLength(0);
		});

		test(`image: a new comment being typed is there again after a ${way}`, async ({ page, request }) => {
			const seeded = await seedImage(page, request, `drafts-${way}-image`);
			await openThread(page, seeded.sessionTitle, page.viewportSize()!.width);
			await drawBox(page);
			await composer(page).fill('The header is too tall');
			const region = await page.evaluate((prefix) => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith(prefix))!)!).region, PREFIX);
			expect(region).toBeTruthy();
			await composer(page).press('Escape');
			await expect(composer(page)).toHaveCount(0);
			page = await leave(page, way, async (next) => {
				if (next !== page) await restoreImageTab(next, seeded);
				await openThread(next, seeded.sessionTitle, next.viewportSize()!.width);
			});
			if (!phone(page)) await drawBox(page);
			await expect(composer(page), 'the text typed before is there').toHaveValue('The header is too tall');
			const restoredRegion = await page.evaluate((prefix) => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith(prefix))!)!).region, PREFIX);
			expect(restoredRegion).toEqual(region);
			await expect(page.locator('.composer').getByRole('button', { name: 'Sending…' })).toHaveCount(0);
			const response = await request.get(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN } });
			expect((await response.json()).comments).toHaveLength(0);
		});

		test(`document: a reply being typed is there again after a ${way}`, async ({ page, request }) => {
			const document = await seedLiveDocument(request, `drafts-${way}-reply`, DOC);
			const id = await seedComment(page, document);
			await openLiveDocument(page, document);
			const field = await openReply(page, id);
			await field.click();
			await field.pressSequentially('Park on Elm Street');
			page = await leave(page, way, (next) => openLiveDocument(next, document).then(() => undefined));
			await expect(await openReply(page, id), 'the reply typed before is there').toHaveValue('Park on Elm Street');
		});
	}

	for (const surface of ['document', 'image'] as const) {
		test(`${surface}: a draft too long to keep on this device says so under its box, and nothing is stored for it`, async ({ page, request }, testInfo) => {
			if (surface === 'document') {
				const document = await seedLiveDocument(request, 'drafts-too-long-doc', DOC);
				await openLiveDocument(page, document);
				await beginComment(page, QUOTE);
			} else {
				const seeded = await seedImage(page, request, 'drafts-too-long-image');
				await openThread(page, seeded.sessionTitle, page.viewportSize()!.width);
				await drawBox(page);
			}
			await composer(page).fill(TOO_LONG);
			const line = page.locator('[role="alert"]:not(.toast)').filter({ hasText: NOT_KEPT });
			await line.waitFor({ timeout: 3_000 }).catch(() => {});
			await frame(page, `not-kept-${surface}`);
			await expect(line, 'the box says the draft is not kept on this device').toBeVisible();
			await expect(page.locator('.toast').filter({ hasText: NOT_KEPT }), 'its box is on screen, so no toast says it too').toHaveCount(0);
			await expectFailureLook(page, testInfo, line, `${surface} not-kept line`);
			expect(await stored(page), 'nothing is stored for it').toEqual([]);
			await expect(composer(page), 'the text stays in the box').toHaveValue(TOO_LONG);
		});
	}

	test('document: a reply that fails to post on a device with no room left says both in one line under its field', async ({ page, request }, testInfo) => {
		const document = await seedLiveDocument(request, 'drafts-full-reply', DOC);
		const id = await seedComment(page, document);
		await openLiveDocument(page, document);
		// Another person's drafts fill this device's share for drafts (2,000,000 UTF-16 units) to within a few units; read at sign-in.
		await page.evaluate((prefix) => {
			let left = 2_000_000 - 40;
			for (let index = 0; left > 0; index += 1) {
				const name = `${prefix}another-person:doc-${index}`, empty = JSON.stringify({ body: '', tags: [] });
				const body = 'x'.repeat(Math.max(0, Math.min(190_000, left - name.length - empty.length)));
				localStorage.setItem(name, JSON.stringify({ body, tags: [] }));
				left -= name.length + empty.length + body.length;
			}
		}, PREFIX);
		await page.reload();
		await openLiveDocument(page, document);
		await page.route('**/api/artifacts/*/comments*', (route) => {
			const sent = route.request();
			if (sent.method() === 'POST' && (sent.postDataJSON() as { parent_id?: string } | null)?.parent_id === id) return route.abort('failed');
			return route.continue();
		});
		const field = await openReply(page, id);
		await field.click();
		await field.pressSequentially('Park on Elm Street');
		await page.locator(`[data-thread-id="${id}"]`).first().getByRole('button', { name: 'Send reply' }).click();
		const full = 'Couldn’t post the comment. Try again. This device has no room left to keep this draft. Post it or copy it before you close CORSIA.';
		const line = page.locator(`[data-thread-id="${id}"]`).first().locator('[role="alert"]');
		await line.filter({ hasText: full }).waitFor({ timeout: 30_000 }).catch(() => {});
		await frame(page, 'not-kept-reply-failed');
		await expect(line, 'one line says why it did not post and that the device does not keep it').toHaveText(full);
		const fits = await line.evaluate((el) => { const box = el.getBoundingClientRect(); return box.left >= 0 && box.right <= window.innerWidth && el.scrollWidth <= el.clientWidth; });
		expect(fits, 'the line wraps inside the screen').toBe(true);
		await expectFailureLook(page, testInfo, line, 'reply not-kept line');
		await expect(field, 'the text stays in the field').toHaveValue('Park on Elm Street');
	});

	test('document: a comment that posted does not come back after a relaunch', async ({ page, request }) => {
		const document = await seedLiveDocument(request, 'drafts-posted-doc', DOC);
		await openLiveDocument(page, document);
		await beginComment(page, QUOTE, 'Bring a spare pair of gloves');
		const posted = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().includes(`/api/artifacts/${document.artifactId}/comments`) && response.ok());
		await submitButton(page).click();
		await posted;
		await expect(composer(page)).toHaveCount(0);
		await expect.poll(() => stored(page), { message: 'nothing is kept once it posted' }).toEqual([]);
		const next = await page.context().newPage();
		await next.setViewportSize(page.viewportSize()!);
		await page.close();
		await openLiveDocument(next, document);
		await beginComment(next, QUOTE);
		await expect(composer(next), 'the box starts empty').toHaveValue('');
	});
});
