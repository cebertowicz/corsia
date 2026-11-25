import fs from 'node:fs';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { loginByUrl } from '../support/corsia';
import { enterDocumentEdit } from '../support/documentReading';
import { syntheticPng } from '../support/syntheticImages';
import { DOC, seedImageDocument, docImage } from '../support/docImageComments';
import { openComments } from '../support/commentDrawer';

// Commenting on a document image taller than the window: the screen does not jump while the person types or posts, a scroll
// they make while a comment is being brought into view is not undone, and the Comment button stays inside the window.
const SIZES = { fits: { width: 1600, height: 1000 }, tall: { width: 1032, height: 2237 } };
const PHONE = { w: 393, h: 852 };
const DESKTOP = { w: 1440, h: 900 };
const COMMENT_BOX = 'textarea[placeholder="Comment, or @ to tag an agent"]';

async function open(page: Page, request: Parameters<typeof seedImageDocument>[1], label: string, size: { w: number; h: number }, image: { width: number; height: number }) {
	await page.setViewportSize({ width: size.w, height: size.h });
	const seeded = await seedImageDocument(page, request, label, null, DOC);
	fs.writeFileSync(seeded.imagePath, syntheticPng(image.width, image.height));
	await loginByUrl(page);
	if (size.w <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(seeded.sessionTitle).first().click();
	await expect(docImage(page)).toBeVisible();
	await expect.poll(() => docImage(page).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(image.width);
	await enterDocumentEdit(page);
}

/** Every distinct scrollTop the document takes from now on, one per painted frame, in order. */
async function watchScroll(page: Page) {
	await page.evaluate(() => {
		const scroller = (() => {
			for (let node = document.querySelector('.cm-md-image')?.parentElement ?? null; node; node = node.parentElement) {
				const overflow = getComputedStyle(node).overflowY;
				if ((overflow === 'auto' || overflow === 'scroll') && node.scrollHeight > node.clientHeight + 1) return node;
			}
			return document.scrollingElement as HTMLElement;
		})();
		const seen = [Math.round(scroller.scrollTop)];
		(window as unknown as { __seen: number[]; __scroller: HTMLElement }).__seen = seen;
		(window as unknown as { __scroller: HTMLElement }).__scroller = scroller;
		const tick = () => {
			const top = Math.round(scroller.scrollTop);
			if (top !== seen[seen.length - 1]) seen.push(top);
			requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	});
}
const seenValues = (page: Page) => page.evaluate(() => (window as unknown as { __seen: number[] }).__seen);
const markSeen = (page: Page) => page.evaluate(() => { const seen = (window as unknown as { __seen: number[] }).__seen; seen.splice(0, seen.length - 1); });

/** Selects the image by keyboard, the way a caret reaches it: from the start of the paragraph after it, up over it. */
async function selectImage(page: Page) {
	await page.getByText('Paragraph 4:').first().click({ position: { x: 2, y: 2 } });
	await page.keyboard.press('Home');
	await page.keyboard.press('Shift+ArrowUp');
	await page.keyboard.press('Shift+ArrowUp');
	await expect(page.locator('.cm-md-image-selected img')).toHaveCount(1);
	await page.waitForTimeout(600);
}

const commentButton = (page: Page) => page.getByRole('button', { name: 'Comment', exact: true }).last();
async function expectInWindow(page: Page, step: string) {
	const window = page.viewportSize()!;
	const box = await commentButton(page).boundingBox();
	expect(box, `${step}: the Comment button is drawn`).not.toBeNull();
	expect(box!.y, `${step}: its top is inside the window`).toBeGreaterThanOrEqual(0);
	expect(box!.y + box!.height, `${step}: its bottom is inside the window`).toBeLessThanOrEqual(window.height);
}

async function wheelScroll(page: Page, size: { w: number; h: number }, by: number) {
	await page.mouse.move(size.w * 0.75, size.h * 0.5);
	await page.mouse.wheel(0, by);
	await page.waitForTimeout(300);
}

async function compose(page: Page, lines: number) {
	const composer = page.locator(COMMENT_BOX);
	await expect(composer).toBeVisible();
	const text: string[] = [];
	for (let i = 1; i <= lines; i++) { text.push(`Line ${i}: the spacing here feels off compared with the other screen`); await composer.fill(text.join('\n')); await page.waitForTimeout(250); }
	return composer;
}

test.describe('commenting on an image taller than the window', () => {
	test.beforeEach(({}, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(180_000);
	});

	test('the Comment button stays inside the window beside a selected tall image, and opens the composer', async ({ page, request }) => {
		await open(page, request, 'tall-offer-image', DESKTOP, SIZES.tall);
		await selectImage(page);
		await expectInWindow(page, 'selected');
		for (const by of [300, 400, -700]) {
			await wheelScroll(page, DESKTOP, by);
			await expectInWindow(page, `after scrolling ${by}`);
		}
		await commentButton(page).click();
		await expect(page.locator(COMMENT_BOX)).toBeVisible();
	});

	test('the Comment button stays inside the window beside a tall text selection, and opens the composer', async ({ page, request }) => {
		await open(page, request, 'tall-offer-text', DESKTOP, SIZES.fits);
		// From the first paragraph to the end of the document: more rows than the window shows.
		await page.getByText('Paragraph 1:').first().click({ position: { x: 2, y: 2 } });
		await page.keyboard.press('Home');
		await page.keyboard.press('ControlOrMeta+Shift+End');
		await page.waitForTimeout(600);
		await expectInWindow(page, 'selected');
		for (const by of [300, 600, -900]) {
			await wheelScroll(page, DESKTOP, by);
			await expectInWindow(page, `after scrolling ${by}`);
		}
		await commentButton(page).click();
		await expect(page.locator(COMMENT_BOX)).toBeVisible();
	});

	test('a fitting image: the keyboard, typing and posting do not move the document again', async ({ page, request }) => {
		await open(page, request, 'tall-shrink-fits', PHONE, SIZES.fits);
		await selectImage(page);
		await commentButton(page).click();
		const composer = page.locator(COMMENT_BOX);
		await expect(composer).toBeVisible();
		await page.waitForTimeout(900);
		await watchScroll(page);
		await markSeen(page);
		// The keyboard takes 300px of height a moment after the composer opens.
		await page.setViewportSize({ width: PHONE.w, height: PHONE.h - 300 });
		await page.waitForTimeout(900);
		await compose(page, 6);
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(composer).toHaveCount(0);
		await page.waitForTimeout(1500);
		const seen = await seenValues(page);
		expect(seen.length - 1, `distinct document positions after the composer settled: ${JSON.stringify(seen)}`).toBeLessThanOrEqual(0);
	});

	test('a tall image: composer, typing and posting do not move the document', async ({ page, request }) => {
		await open(page, request, 'tall-type-tall', PHONE, SIZES.tall);
		await selectImage(page);
		await commentButton(page).click();
		const composer = page.locator(COMMENT_BOX);
		await expect(composer).toBeVisible();
		await page.waitForTimeout(900);
		await watchScroll(page);
		await markSeen(page);
		await page.setViewportSize({ width: PHONE.w, height: PHONE.h - 300 });
		await page.waitForTimeout(900);
		await compose(page, 6);
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(composer).toHaveCount(0);
		await page.waitForTimeout(1500);
		const seen = await seenValues(page);
		expect(seen.length - 1, `distinct document positions: ${JSON.stringify(seen)}`).toBeLessThanOrEqual(0);
	});

	for (const size of [PHONE, DESKTOP]) {
		test(`a scroll while a comment on a tall image is brought into view is not undone at ${size.w}`, async ({ page, request }) => {
			await open(page, request, `tall-scroll-${size.w}`, size, SIZES.tall);
			await selectImage(page);
			await expectInWindow(page, 'selected');
			await commentButton(page).click();
			await compose(page, 1);
			await page.keyboard.press('ControlOrMeta+Enter');
			await expect(page.locator(COMMENT_BOX)).toHaveCount(0);
			await page.waitForTimeout(1200);
			// Back to the top, then bring the posted comment into view from the comments list.
			await page.mouse.move(size.w * 0.75, size.h * 0.5);
			await page.mouse.wheel(0, -5000);
			await page.waitForTimeout(500);
			await openComments(page);
			await watchScroll(page);
			await page.getByText('Line 1:').first().click();
			await page.waitForTimeout(80);
			await page.mouse.wheel(0, 300);
			await page.waitForTimeout(400);
			const afterWheel = await page.evaluate(() => Math.round((window as unknown as { __scroller: HTMLElement }).__scroller.scrollTop));
			await page.waitForTimeout(1500);
			const settled = await page.evaluate(() => Math.round((window as unknown as { __scroller: HTMLElement }).__scroller.scrollTop));
			expect(settled, 'the document stays where the reader scrolled it').toBe(afterWheel);
		});
	}
});
