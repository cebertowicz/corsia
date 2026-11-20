import { expect, test } from './support/test';
import { commentedMessage, QUOTE } from './support/chatCommentedMessage';
import { createSession, sidebarThread } from './support/corsia';
import { openCommentThread, settleHighlight } from './support/chatComments';

for (const width of [900, 1440]) test(`switching conversations reopens a chat reply at ${width}`, async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const other = `Other chat ${crypto.randomUUID()}`;
	await createSession(request, other);
	const { row } = await commentedMessage(page, request, width);
	await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	const thread = await openCommentThread(row, page, QUOTE);
	await thread.getByPlaceholder('Reply…').fill('A reply kept through a switch');
	await sidebarThread(page, other).click();
	await expect(row).toHaveCount(0);
	await page.locator('aside.sidebar').getByText(new RegExp(`^Margin comments ${width} `)).click();
	await expect(row).toBeVisible();
	try {
		const reopened = await openCommentThread(row, page, QUOTE);
		await expect(reopened.getByPlaceholder('Reply…')).toHaveValue('A reply kept through a switch');
	} finally {
		const dir = process.env.CORSIA_CHAT_LEAVE_FRAMES;
		if (dir) await page.screenshot({ path: `${dir}/switch-${width}.png`, animations: 'disabled' });
	}
});

for (const name of ['text', 'empty'] as const) test(`${name} releases the picked margin card, keeping its draft`, async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, 1920);
	await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	await row.evaluate(el => el.scrollIntoView({ block: 'center' }));
	const mark = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE });
	await settleHighlight(page, mark);
	const card = page.locator('.chat-margin-card').filter({ hasText: 'A note on the phrase' });
	await expect(card).toBeVisible();
	{
		const target = page.locator('.messages');
		await mark.click();
		await expect(card).toHaveClass(/selected/);
		await card.getByPlaceholder('Reply…').fill('A kept margin reply');
		const dir = process.env.CORSIA_CHAT_LEAVE_FRAMES;
		if (dir && name === 'text') for (const theme of ['light', 'dark']) {
			await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
			await page.screenshot({ path: `${dir}/picked-${theme}.png`, animations: 'disabled' });
		}
		if (name === 'text') {
			const box = await mark.boundingBox();
			if (!box) throw new Error('highlight missing');
			await page.mouse.click(box.x - 8, box.y + box.height / 2);
		} else {
			const box = await target.boundingBox();
			if (!box) throw new Error('transcript missing');
			await page.mouse.click(box.x + 8, box.y + 100);
		}
		if (dir && name === 'text') for (const theme of ['light', 'dark']) {
			await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
			await page.screenshot({ path: `${dir}/after-click-${theme}.png`, animations: 'disabled' });
		}
		await expect(card).not.toHaveClass(/selected/);
		await mark.click();
		await expect(card.getByPlaceholder('Reply…')).toHaveValue('A kept margin reply');
	}
});

for (const width of [1440, 393]) test(`outside interaction keeps the ${width} thread behavior`, async ({ page, request }, info) => {
	test.skip(info.project.name !== (width === 393 ? 'chromium-mobile' : 'chromium-desktop'));
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, width);
	await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	const thread = await openCommentThread(row, page, QUOTE, { tap: width === 393 });
	await thread.getByPlaceholder('Reply…').fill('A kept reply');
	if (width === 393) {
		const scroll = page.locator('.messages');
		await scroll.evaluate(async el => {
			let previous = el.scrollTop, stable = 0;
			while (stable < 12) {
				await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
				const next = el.scrollTop;
				stable = next === previous ? stable + 1 : 0;
				previous = next;
			}
		});
		const before = await scroll.evaluate(el => el.scrollTop);
		const backdrop = page.locator('[data-adaptive-drawer].drawer-backdrop');
		const box = await backdrop.boundingBox();
		if (!box) throw new Error('sheet backdrop missing');
		const cdp = await page.context().newCDPSession(page);
		const x = box.x + box.width / 2, y = box.y + Math.min(box.height / 2, 100);
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y + 80 }] });
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
		console.log('BACKDROP_SWIPE', JSON.stringify({ before, after: await scroll.evaluate(el => el.scrollTop) }));
		if (await thread.isVisible()) await backdrop.tap({ position: { x: box.width / 2, y: Math.min(box.height / 2, 100) } });
	} else {
		await page.keyboard.press('Escape');
	}
	await expect(thread).toHaveCount(0);
	const reopened = await openCommentThread(row, page, QUOTE, { tap: width === 393 });
	await expect(reopened.getByPlaceholder('Reply…')).toHaveValue('A kept reply');
	const dir = process.env.CORSIA_CHAT_LEAVE_FRAMES;
	if (dir) await page.screenshot({ path: `${dir}/surface-${width}.png`, animations: 'disabled' });
});
