import { expect, test } from '@playwright/test';
import { createComment, openLiveDocument, seedLiveDocument } from '../support/liveDocument';

for (const width of [1440, 393]) {
	test(`@surface:live-document the selection Comment button stays inside the document column and clickable at ${width}`, async ({ page, request }) => {
		await page.setViewportSize({ width, height: width > 500 ? 960 : 852 });
		const stamp = `clamp-${Date.now().toString(36)}`;
		const words = Array.from({ length: 60 }, (_, index) => `word${index}`).join(' ');
		const body = `# Clamp ${stamp}\n\n${words}\n`;
		const document = await seedLiveDocument(request, stamp, body);
		// An existing comment opens the comments pane beside the document.
		await createComment(request, document, 'word50', `Existing ${stamp}`);
		await openLiveDocument(page, document, `# Clamp ${stamp}`);
		const editor = page.locator('.cm-content');
		await editor.click();
		await page.keyboard.press('End');
		await page.keyboard.type(' saved');
		await page.keyboard.press('ControlOrMeta+S');
		const saved = page.getByText('✓ Saved', { exact: true });
		const commentsPill = page.getByRole('button', { name: /^1 open thread\b/ });
		await expect(saved).toBeVisible();
		const [savedBox, commentsPillBox] = await Promise.all([saved.boundingBox(), commentsPill.boundingBox()]);
		expect(savedBox!.x + savedBox!.width, 'save status ends before the comment pill starts').toBeLessThanOrEqual(commentsPillBox!.x);
		// A selection that ends just inside the right edge of a full-width visual line puts the
		// button's natural position beyond the column. Backing off the wrap point keeps the end on
		// that line, since the wrap point itself reports the next line's start.
		const firstLine = page.locator('.cm-line').filter({ hasText: 'word0 word1' });
		// A comment marker owns the rail; begin in the line's reserved text inset, not its box's left edge.
		const textInset = await firstLine.evaluate((line) => Number.parseFloat(getComputedStyle(line.parentElement!).paddingLeft));
		await firstLine.click({ position: { x: textInset + 1, y: 2 } });
		await page.keyboard.press('Home');
		await page.keyboard.press('Shift+End');
		for (let step = 0; step < 3; step += 1) await page.keyboard.press('Shift+ArrowLeft');
		const button = page.locator('.sel-comment');
		await expect(button).toBeVisible();
		// The button follows the selection's end; wait for the visual line to be fully selected.
		await expect.poll(() => page.evaluate(() => document.getSelection()?.toString().length ?? 0)).toBeGreaterThan(30);
		const column = (await page.locator('.doc-main').boundingBox())!;
		await expect.poll(async () => (await button.boundingBox())!.x, { message: 'the button follows the end of the selected line' }).toBeGreaterThan(column.x + column.width * 0.5);
		const box = (await button.boundingBox())!;
		expect(box.x, 'button starts inside the column').toBeGreaterThanOrEqual(column.x);
		expect(box.x + box.width, 'button ends inside the column').toBeLessThanOrEqual(column.x + column.width);
		const hit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('.sel-comment') !== null, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
		expect(hit, 'the button is the element under its own centre').toBe(true);
		const comments = page.getByRole('region', { name: 'Document comments', exact: true });
		await page.getByRole('button', { name: /^1 open thread\b/ }).click();
		await expect(comments).toBeVisible();
		await expect(button).toHaveCount(0);
		await comments.getByRole('button', { name: 'Close comments', exact: true }).click();
		await expect(comments).toHaveCount(0);
		// Closing restores the still-valid selection on both mobile and desktop.
		await expect(button).toBeVisible();
		await button.click();
		if (width === 393) await expect(page.getByRole('heading', { name: 'New comment', exact: true })).toBeVisible();
		await expect(page.getByPlaceholder('Comment…')).toBeVisible();
	});
}
