import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { beginComment } from '../support/liveCommentProof';
import { at, commentButton, composer, drag, openThread, seedImage } from '../support/imageRegionComments';
import { bar, commentOnRegion, openDocument, openImage, overlay, seedImageDocument } from '../support/docImageComments';
import { openComments, replyField } from '../support/commentDrawer';

// A retryable failure stays silent when its box is closed or covered; its next try stores it once.
async function onePost(request: APIRequestContext, artifactId: string, text: string) {
	await expect.poll(async () => {
		const response = await request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } });
		const { comments } = await response.json();
		return JSON.stringify(comments).split(text).length - 1;
	}).toBe(1);
}

/**
 * The first comment POST (to any artifact when `artifactId` is `*`) that `match` takes is held until `fail()`, which then
 * fails it as a dropped connection; later ones go through.
 */
async function holdFirstPost(page: Page, artifactId: string, match: (body: { parent_id?: string } | null) => boolean = () => true) {
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	let posts = 0, seen!: () => void, failed!: () => void;
	const arrived = new Promise<void>((resolve) => { seen = resolve; });
	const dropped = new Promise<void>((resolve) => { failed = resolve; });
	await page.route(`**/api/artifacts/${artifactId}/comments*`, async (route) => {
		const request = route.request();
		if (request.method() !== 'POST' || !match(request.postDataJSON()) || posts++ > 0) return route.continue();
		seen();
		await held;
		await route.abort('failed');
		failed();
	});
	return { arrived, dropped, fail: release };
}

const DOC = '# One place\n\nThe router decides where an inbound event goes.\n\nA second paragraph to keep the margin honest.\n';
const QUOTE = 'The router decides';
const SIZES = { 'chromium-desktop': { width: 1440, height: 900 }, 'chromium-mobile': { width: 393, height: 852 } } as const;
type Project = keyof typeof SIZES;
const phone = (page: Page) => page.viewportSize()!.width <= 768;

/** Opens an image's comment box at (fx, fy) to (gx, gy): a drag with the mouse, or Comment then a tap on a phone. */
async function drawBox(page: Page, from: [number, number], to: [number, number]) {
	if (phone(page)) {
		await commentButton(page).tap();
		const spot = await at(page, ...from);
		await page.touchscreen.tap(spot.x, spot.y);
	} else {
		await drag(page, await at(page, ...from), await at(page, ...to));
	}
	await expect(composer(page)).toBeVisible();
}

test.describe('@flow:document-comments a retryable post that fails after its box closes stays silent', () => {
	test.beforeEach(async ({ page }, testInfo) => {
		test.skip(!(testInfo.project.name in SIZES), 'Taken at 1440 and 393 only');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(150_000);
		await page.setViewportSize(SIZES[testInfo.project.name as Project]);
	});

	test('document: closed while posting, then reopened while retrying', async ({ page, request }) => {
		const document = await seedLiveDocument(request, 'one-place-doc', DOC);
		await openLiveDocument(page, document);
		const wire = await holdFirstPost(page, document.artifactId);
		await beginComment(page, QUOTE, 'Name the fallback route');
		await composer(page).press('ControlOrMeta+Enter');
		await wire.arrived;
		await composer(page).press('Escape');
		await expect(composer(page), 'the box is closed while the post is out').toHaveCount(0);
		wire.fail();
		await wire.dropped;
		await expect(page.locator('.toast').filter({ hasText: /post/i })).toHaveCount(0);
		await beginComment(page, QUOTE);
		await expect(composer(page), 'the text is kept').toHaveValue('Name the fallback route');
		await expect(page.locator('.composer').getByRole('button', { name: 'Sending…' })).toBeVisible();
		await expect(page.locator('.composer [role="alert"]')).toHaveCount(0);
		await page.evaluate(() => window.dispatchEvent(new Event('focus')));
		await onePost(request, document.artifactId, 'Name the fallback route');
	});

	test('image: closed while posting, then reopened while retrying', async ({ page, request }) => {
		const seeded = await seedImage(page, request, 'one-place-image');
		const wire = await holdFirstPost(page, seeded.artifactId);
		await openThread(page, seeded.sessionTitle, page.viewportSize()!.width);
		await drawBox(page, [0.2, 0.25], [0.45, 0.5]);
		await composer(page).fill('The header is too tall');
		await composer(page).press('ControlOrMeta+Enter');
		await wire.arrived;
		await composer(page).press('Escape');
		await expect(composer(page), 'the box is closed while the post is out').toHaveCount(0);
		wire.fail();
		await wire.dropped;
		await expect(page.locator('.toast').filter({ hasText: /post/i })).toHaveCount(0);
		await drawBox(page, [0.5, 0.55], [0.7, 0.75]);
		await expect(composer(page), 'the text is kept').toHaveValue('The header is too tall');
		await expect(page.locator('.composer').getByRole('button', { name: 'Sending…' })).toBeVisible();
		await expect(page.locator('.composer [role="alert"]')).toHaveCount(0);
		await page.evaluate(() => window.dispatchEvent(new Event('focus')));
		await onePost(request, seeded.artifactId, 'The header is too tall');
	});

	test('reply in the comments drawer: retry stays silent while the image viewer covers it', async ({ page, request }) => {
		const width = page.viewportSize()!.width;
		const seeded = await seedImageDocument(page, request, `one-place-drawer-${width}`, null);
		await openDocument(page, seeded, width);
		// Two comments on parts of the document's image, made as a person makes them; Back sends what the image holds.
		await openImage(page);
		await commentOnRegion(page, 'The header crowds the logo', { x: 0.1, y: 0.1 });
		await page.keyboard.press('Escape');
		await commentOnRegion(page, 'The footer is too faint', { x: 0.6, y: 0.6 });
		await page.keyboard.press('Escape');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);

		await openComments(page);
		const list = page.locator('[data-doc-comments-drawer]');
		const replying = list.locator('.ct').filter({ hasText: 'The footer is too faint' });
		const reply = await replyField(replying);
		await reply.fill('Lighten it a step');
		const wire = await holdFirstPost(page, '*', (body) => !!body?.parent_id);
		await replying.getByRole('button', { name: 'Send reply' }).click();
		await wire.arrived;

		// While the reply is out, the other card opens the viewer at its tag, over the drawer: the drawer stays open beneath it,
		// out of sight, so the reply's field is still on the page and laid out where it was, but nobody can see it.
		await list.locator('.ct').filter({ hasText: 'The header crowds the logo' }).locator('.ct-original').click();
		await expect(overlay(page)).toBeVisible();
		await expect(list).toBeHidden();
		await expect(reply, 'the field is still mounted under the viewer').toBeAttached();
		await expect(reply).toBeHidden();

		wire.fail();
		await wire.dropped;
		await expect(page.locator('.toast').filter({ hasText: /post/i })).toHaveCount(0);
		await expect(reply, 'the reply is kept').toHaveValue('Lighten it a step');
		if (width < 600) {
			// The selected image comment opens in a sheet over Back; close it as a person would.
			const sheet = page.locator('[data-adaptive-drawer]').filter({ has: page.locator('[data-sheet-thread]').filter({ hasText: 'The header crowds the logo' }) });
			await expect(sheet).toBeVisible();
			await sheet.getByRole('button', { name: 'Close', exact: true }).click();
			await expect(sheet).toBeHidden();
		}
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(replying.getByText('Sending…')).toBeVisible();
		await expect(replying.locator('[role="alert"]')).toHaveCount(0);
		await page.evaluate(() => window.dispatchEvent(new Event('focus')));
		await onePost(request, seeded.artifactId, 'Lighten it a step');
	});
});
