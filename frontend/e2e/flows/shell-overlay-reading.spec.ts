import { expect, test } from '../support/test';
import { seedDocument } from '../contact-sheet/document-comments';

test.use({ seedPerTest: true });

test('keeps the first whole line while the panel opens and closes', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1100, height: 900 });
	await seedDocument(page, request, true);
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	const scroller = page.locator('.cm-scroller');
	await page.waitForTimeout(1800);
	await scroller.hover();
	await page.mouse.wheel(0, 1085);
	await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(500);
	await page.waitForTimeout(600);
	const line = page.locator('.cm-line').filter({ hasText: 'Review section 2' }).first();
	await expect(line).toBeVisible();
	const before = (await line.boundingBox())!.y;
	await toggle.click();
	await page.waitForTimeout(1800);
	expect(Math.abs((await line.boundingBox())!.y - before)).toBeLessThan(2);
	await toggle.click();
	await page.waitForTimeout(1800);
	expect(Math.abs((await line.boundingBox())!.y - before)).toBeLessThan(2);
});

test('keeps the same reply and focus when a margin list becomes a sheet', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	const drawer = page.locator('.canvas-comment-drawer.margin:not(.leaving)');
	if (!await drawer.count()) await page.getByRole('button', { name: '13 threads' }).click();
	const card = drawer.getByRole('region', { name: /^Comment by / }).first();
	if (!await card.locator('.ct-input').count()) await card.locator('.ct-head').click({ position: { x: 14, y: 10 } });
	const input = card.locator('.ct-input');
	await input.fill('A half-written reply');
	const id = await input.evaluate((el) => el.closest('[data-comment-id]')?.getAttribute('data-comment-id'));
	expect(id).toBeTruthy();
	await toggle.click();
	const reply = page.locator('.canvas-comment-drawer.sheet').locator(`[data-comment-id="${id}"] .ct-input`);
	await expect(reply).toHaveValue('A half-written reply');
	await expect(reply).toBeFocused();
});
