import { expect, test, type Page } from '../support/test';
import { seedDocument } from '../contact-sheet/document-comments';

test.use({ seedPerTest: true });

/** Where writing focus sits, and which comment surface form is up. */
const focusState = (page: Page, commentId: string | null = null) => page.evaluate((id) => {
	const el = document.activeElement;
	const area = el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement ? el : null;
	const card = el instanceof Element ? el.closest('[data-comment-id]') : null;
	return {
		focus: el ? `${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).split(' ')[0]}` : ''}` : null,
		of: card?.getAttribute('data-comment-id') ?? null,
		inReplyOf: !!(area && area.classList.contains('ct-input') && (id === null || !!area.closest(`[data-comment-id="${id}"]`))),
		value: area?.value ?? null,
		caret: area?.selectionStart ?? null,
		sheet: !!document.querySelector('.canvas-comment-drawer.sheet'),
		margin: !!document.querySelector('.canvas-comment-drawer.margin'),
	};
}, commentId);

/** Opens the document's comments, focuses one thread's reply and leaves a half-typed draft in it. */
async function typeAReply(page: Page): Promise<{ id: string; caret: number }> {
	const list = page.locator('.canvas-comment-drawer').locator('[data-comment-id]').first();
	await list.locator('.ct-head').click();
	const input = list.locator('.ct-input');
	await expect(input).toBeVisible();
	await input.click();
	await input.pressSequentially('An unfinished reply');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	const caret = await input.evaluate((el) => el.selectionStart);
	const id = await list.getAttribute('data-comment-id');
	expect(id).toBeTruthy();
	return { id: id!, caret };
}

/** Focus a reply, open the Workspace panel, close it: the same reply has the draft and the caret again. */
async function keepsReplyFocusAcrossThePanel(page: Page, request: Parameters<typeof seedDocument>[1], width: number, collapseSidebar: boolean) {
	await page.setViewportSize({ width, height: 900 });
	await seedDocument(page, request, true);
	if (collapseSidebar) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1200);
	if (!await page.locator('.canvas-comment-drawer').count()) await page.getByRole('button', { name: '13 threads' }).click();
	const { id, caret } = await typeAReply(page);
	await toggle.click();
	await page.waitForTimeout(1800);
	await toggle.click();
	await page.waitForTimeout(1800);
	const after = await focusState(page, id);
	expect(after.inReplyOf, `the reply ${id} should have focus again, focus was ${after.focus} of ${after.of}`).toBe(true);
	expect(after.value).toBe('An unfinished reply');
	expect(after.caret).toBe(caret);
}

for (const [width, collapseSidebar] of [[1360, false], [1360, true], [1100, false], [1100, true]] as const) {
	test(`keeps a half-typed reply focused across the panel at ${width} sidebar ${collapseSidebar ? 'collapsed' : 'open'}`, async ({ page, request }) => {
		test.setTimeout(120_000);
		await keepsReplyFocusAcrossThePanel(page, request, width, collapseSidebar);
	});
}

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
