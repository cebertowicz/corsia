import { expect, type Locator, type Page } from '@playwright/test';
import { test } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { cell, frame, LAYOUTS, onlyIn, openTableDocument, settled } from '../support/tableCells';

/**
 * A table cell holding only an image (a status badge) takes a comment: a press beside the badge offers Comment, the comment is
 * labelled by the badge's alt text, row and column, the whole cell is marked and named for the badge, and the mark is still there
 * after a reload. A press on the badge itself still opens it in the lightbox.
 */

const BADGE = `<svg xmlns="http://www.w3.org/2000/svg" width="72" height="20"><rect width="72" height="20" rx="10" fill="#2f6fde"/><text x="36" y="14" font-family="sans-serif" font-size="11" fill="#fff" text-anchor="middle">Design</text></svg>`;
const DOC = [
	'# Status', '',
	'Intro paragraph above the table.', '',
	'| Item | Status of the work |', '| --- | --- |',
	'| Alpha | ![Design](badges/design.svg) |',
	'| Bravo | Done |', '',
	'Paragraph below the table.', '',
].join('\n');
const LABEL = '“Design” · Alpha, Status of the work';

async function seedBadge(page: Page) {
	const params = { token: TEST_TOKEN };
	const roots: Array<{ kind: string; path: string }> = (await (await page.request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const put = await page.request.put('/api/files/content', { params, data: { path: `${workspace}/badges/design.svg`, text: BADGE, create: true } });
	expect(put.ok(), await put.text()).toBeTruthy();
}

const badgeCell = (page: Page) => cell(page, 1, 1);
const image = (target: Locator) => target.locator('.cm-md-cell-image');

/** The image is drawn and loaded, so the cell's box is the one a person sees. */
async function badgeDrawn(page: Page) {
	await expect(image(badgeCell(page)).locator('img')).toBeVisible({ timeout: 20_000 });
	await expect.poll(() => image(badgeCell(page)).locator('img').evaluate((img) => (img as HTMLImageElement).naturalWidth), { message: 'the badge loaded' }).toBeGreaterThan(0);
	await settled(page);
}

/** A point inside the badge's cell and outside its image, with what the browser finds there. */
async function besideBadge(page: Page) {
	const target = badgeCell(page);
	await target.scrollIntoViewIfNeeded();
	return target.evaluate((td) => {
		const c = td.getBoundingClientRect(), i = td.querySelector('.cm-md-cell-image')!.getBoundingClientRect();
		const right = c.right - i.right, left = i.left - c.left;
		const x = right >= left ? i.right + right / 2 : c.left + left / 2, y = c.top + c.height / 2;
		const hit = document.elementFromPoint(x, y);
		return { x, y, cell: { x: c.left, y: c.top, right: c.right, bottom: c.bottom }, image: { x: i.left, y: i.top, right: i.right, bottom: i.bottom }, inCell: !!hit && hit.closest('td, th') === td, onImage: !!hit?.closest('.cm-md-cell-image'), hit: hit?.outerHTML.slice(0, 120) };
	});
}

/** The middle of the badge, with what the browser finds there. */
async function onBadge(page: Page) {
	return image(badgeCell(page)).evaluate((wrap) => {
		const r = wrap.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
		const hit = document.elementFromPoint(x, y);
		return { x, y, onImage: !!hit && wrap.contains(hit), hit: hit?.outerHTML.slice(0, 120) };
	});
}

for (const layout of LAYOUTS.filter((l) => l.width === 1440 || l.width === 393)) {
	const at = layout.width, phone = layout.project === 'chromium-mobile';
	test.describe(`@flow:doc-table-cell-image-comment @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('a press beside a badge in an image-only cell comments on it, and a press on the badge opens it', async ({ page }) => {
			test.setTimeout(150_000);
			await seedBadge(page);
			const seeded = await openTableDocument(page, layout, { text: DOC, quoted: 'Intro paragraph' });
			await badgeDrawn(page);
			await frame(page, `image-cell-00-table-${at}`);

			const beside = await besideBadge(page);
			expect(beside.inCell && !beside.onImage, `the press lands in the badge's cell and off its image (${JSON.stringify(beside)})`).toBe(true);
			if (phone) await page.touchscreen.tap(beside.x, beside.y);
			else await page.mouse.click(beside.x, beside.y);
			const offer = page.locator('.sel-comment');
			await expect(offer, 'Comment is offered beside the badge').toBeVisible();
			await settled(page);
			await frame(page, `image-cell-01-press-beside-${at}`);
			const [shown, badge] = await Promise.all([offer.boundingBox(), badgeCell(page).locator('img').first().boundingBox({ timeout: 5_000 })]);
			const covers = !!shown && !!badge && shown.x < badge.x + badge.width && shown.x + shown.width > badge.x && shown.y < badge.y + badge.height && shown.y + shown.height > badge.y;
			expect(covers, `the offer leaves the badge in view (${JSON.stringify({ shown, badge })})`).toBe(false);
			await offer.click();
			const composer = page.locator('.composer').filter({ has: page.getByPlaceholder('Comment, or @ to tag an agent') });
			await expect(composer).toBeVisible();
			await expect(composer.locator('.quote-wrap'), 'the composer is labelled by the badge, row and column').toContainText(LABEL);
			await frame(page, `image-cell-02-composer-${at}`);
			await composer.getByPlaceholder('Comment, or @ to tag an agent').fill('Is design still the status?');
			await composer.locator('button.submit').click();
			await expect(composer).toBeHidden({ timeout: 15_000 });

			// A single card shows no quote (only a stack of cards labels each); the label is the composer's, checked above.
			await expect(page.getByText('Is design still the status?').first(), 'the comment is posted').toBeVisible();
			await frame(page, `image-cell-03-posted-${at}`);
			// On a phone the tap opened the cell for editing too; the cell is put away as a reader does, by tapping the text below.
			const drawn = page.locator('[data-adaptive-drawer].drawer-backdrop');
			if (await drawn.isVisible()) await drawn.click({ position: { x: 8, y: 8 } });
			if (await page.locator('.cm-md-cell-editor').count()) {
				const below = page.locator('.cm-line', { hasText: 'Paragraph below the table.' });
				if (phone) await below.tap(); else await below.click();
			}
			await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
			const marked = badgeCell(page);
			await expect(marked, 'the whole cell is marked').toHaveClass(/cm-comment-anchor/);
			await expect(marked, 'the marked cell is named for its badge').toHaveAttribute('aria-label', 'Commented cell: Design');
			await frame(page, `image-cell-04-marked-${at}`);

			await page.reload();
			// The document is chosen again as `openTableDocument` chose it: from the thread list, behind the sidebar on a phone.
			if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText(seeded.title, { exact: true }).first().click();
			await expect(page.locator('.cm-md-table table').first()).toBeVisible({ timeout: 20_000 });
			await badgeDrawn(page);
			await expect(badgeCell(page), 'the mark survives a reload').toHaveClass(/cm-comment-anchor/);
			await expect(badgeCell(page)).toHaveAttribute('aria-label', 'Commented cell: Design');
			await frame(page, `image-cell-05-reloaded-${at}`);

			const drawer = page.locator('[data-adaptive-drawer].drawer-backdrop');
			if (await drawer.isVisible()) await drawer.click({ position: { x: 8, y: 8 } });
			const on = await onBadge(page);
			expect(on.onImage, `the press lands on the badge (${JSON.stringify(on)})`).toBe(true);
			if (phone) await page.touchscreen.tap(on.x, on.y);
			else await page.mouse.click(on.x, on.y);
			await expect(page.locator('.image-lightbox'), 'the badge opens in the lightbox').toBeVisible({ timeout: 10_000 });
			await frame(page, `image-cell-06-lightbox-${at}`);
		});
	});
}
