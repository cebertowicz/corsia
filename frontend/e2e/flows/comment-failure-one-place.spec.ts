import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { beginComment } from '../support/liveCommentProof';
import { at, commentButton, composer, drag, openThread, seedImage } from '../support/imageRegionComments';

// A failed post says why in one place, decided as it fails: in its box when the box is on screen then (the retry legs
// in comment-retry-once and doc-reply-draft-kept prove that case), otherwise in a toast, with the reason kept so the
// box shows it when it opens again, and the toast goes once it does. These legs close the box while the post is still out.
// Taken at 1440 (chromium-desktop) and at 393 (chromium-mobile, by touch).

const REASON = 'Couldn’t post the comment. Your draft is kept. Try again.';
const toast = (page: Page) => page.locator('.toast').filter({ hasText: REASON });
/** The box's own line: a toast is a status, never this. */
const reason = (page: Page) => page.locator('[role="alert"]:not(.toast)').filter({ hasText: REASON });
/** Frames for the review of where the reason shows, in both themes, when set (as doc-reply-draft-kept takes them). */
const FRAMES = process.env.CORSIA_REPLY_FRAMES;
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

/**
 * The toast stays up, as a pointer resting on it keeps it, however long the way back to the box takes (an ordinary toast
 * otherwise goes after 3 s, which a phone's way back can outlast). So only the box coming back can take it away.
 */
async function holdToast(toastLocator: Locator) {
	await toastLocator.dispatchEvent('mouseenter');
}

/** The first comment POST is held until `fail()`, which then fails it as a dropped connection; later ones go through. */
async function holdFirstPost(page: Page, artifactId: string) {
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	let posts = 0, seen!: () => void;
	const arrived = new Promise<void>((resolve) => { seen = resolve; });
	await page.route(`**/api/artifacts/${artifactId}/comments*`, async (route) => {
		if (route.request().method() !== 'POST' || posts++ > 0) return route.continue();
		seen();
		await held;
		return route.abort('failed');
	});
	return { arrived, fail: release };
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

test.describe('@flow:document-comments a post that fails after its box closed says why in a toast, then only in its box', () => {
	test.beforeEach(async ({ page }, testInfo) => {
		test.skip(!(testInfo.project.name in SIZES), 'Taken at 1440 and 393 only');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(150_000);
		await page.setViewportSize(SIZES[testInfo.project.name as Project]);
	});

	test('document: closed while posting, a toast; the composer opened again says why, and the toast goes', async ({ page, request }) => {
		const document = await seedLiveDocument(request, 'one-place-doc', DOC);
		await openLiveDocument(page, document);
		const wire = await holdFirstPost(page, document.artifactId);
		await beginComment(page, QUOTE, 'Name the fallback route');
		await composer(page).press('ControlOrMeta+Enter');
		await wire.arrived;
		await composer(page).press('Escape');
		await expect(composer(page), 'the box is closed while the post is out').toHaveCount(0);
		wire.fail();
		await expect(toast(page), 'with no box on screen, the toast says why').toBeVisible({ timeout: 30_000 });
		await holdToast(toast(page));
		await frame(page, 'document-closed-toast');
		await beginComment(page, QUOTE);
		await expect(composer(page), 'the text is kept').toHaveValue('Name the fallback route');
		await expect(reason(page), 'the box says why once it is open again').toBeVisible();
		await expect(toast(page), 'and the toast goes, so the reason shows in one place').toHaveCount(0);
		await frame(page, 'document-reopened-reason');
	});

	test('image: closed while posting, a toast; the box drawn again says why, and the toast goes', async ({ page, request }) => {
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
		await expect(toast(page), 'with no box on screen, the toast says why').toBeVisible({ timeout: 30_000 });
		await holdToast(toast(page));
		await frame(page, 'image-closed-toast');
		await drawBox(page, [0.5, 0.55], [0.7, 0.75]);
		await expect(composer(page), 'the text is kept').toHaveValue('The header is too tall');
		await expect(reason(page), 'the box says why once it is open again').toBeVisible();
		await expect(toast(page), 'and the toast goes, so the reason shows in one place').toHaveCount(0);
		await frame(page, 'image-reopened-reason');
	});
});
