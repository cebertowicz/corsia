import { test, expect } from '../support/test';
import { openAppPastLanding } from '../support/corsia';

const source = { thread_id: 'child', thread_label: 'Reviewer', project_id: null, project_label: null, parent_reviewed: true };
const detail = {
	id: 'decision-any', plan_id: 'plan-1', node_id: 'node-decision', source_node_id: 'node-source',
	thread_id: 'child', title: 'Choose the extras', context: 'Choose what to add.', tradeoff: 'You can add more later.',
	pick: 'any', recommendation: ['A', 'B'], rationale: 'These two are ready.', other_enabled: true,
	options: [{ id: 'A', label: 'First extra', detail: 'First detail' }, { id: 'B', label: 'Second extra', detail: 'Second detail' }, { id: 'C', label: 'Third extra', detail: 'Third detail' }],
	order_key: 0, revision: 3, raised_at: '2026-11-24T10:00:00Z', source,
};
const item = { key: 'decision:any', external_id: detail.id, renderer: 'decision', kind: 'decision', title: detail.title, raised_at: detail.raised_at, source, open: null, rank: null, detail };
const heldDetail = { id: 'held-1', thread_id: 'child', title: 'Send the reply?', text: 'Here is the reply waiting to send.', recipient: 'the recipient', provider: 'Email', error: null, raised_at: detail.raised_at, source };
const heldItem = { key: 'message:held-1', external_id: heldDetail.id, renderer: 'held-reply', kind: 'message', title: heldDetail.title, raised_at: heldDetail.raised_at, source, open: null, rank: null, detail: heldDetail };

for (const width of [1440, 393]) {
	test(`pick-any decision stays usable in Activity at ${width}`, async ({ page }, testInfo) => {
		test.setTimeout(180_000);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		let pending = true;
		let sent: Record<string, unknown> | null = null;
		let disconnected = false;
		let dropConnection = () => {};
		await page.routeWebSocket((url) => url.pathname === '/ws', (socket) => {
			if (disconnected) { socket.close(); return; }
			dropConnection = () => socket.close();
			socket.connectToServer();
		});
		await page.route('**/api/attention/queue**', async (route) => {
			if (!route.request().url().includes('view=activity')) return route.fallback();
			const items = pending ? [item] : [];
			await route.fulfill({ json: { count: items.length, items, ranker: null, notifications: { count: 0, items: [] } } });
		});
		await page.route('**/api/plans/decisions/decision-any/answer', async (route) => {
			sent = route.request().postDataJSON();
			pending = false;
			await route.fulfill({ status: 200, json: {} });
		});
		await openAppPastLanding(page);
		if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
		if (width === 393) await page.locator('[data-activity-row]').filter({ hasText: detail.title }).click();
		const group = page.getByRole('group', { name: 'Choose any' });
		const boxes = group.getByRole('checkbox');
		await expect(boxes).toHaveCount(3);
		for (let i = 0; i < 3; i += 1) {
			await expect(boxes.nth(i)).not.toBeChecked();
			if (width === 393) expect((await boxes.nth(i).locator('xpath=ancestor::label').boundingBox())!.height).toBeGreaterThanOrEqual(44);
		}
		expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
		await boxes.nth(0).check();
		await boxes.nth(2).focus();
		await page.keyboard.press('Space');
		await expect(boxes.nth(2)).toBeChecked();
		await group.getByRole('textbox', { name: 'Note' }).fill('search later');
		disconnected = true;
		dropConnection();
		await expect(boxes.nth(0)).toBeDisabled({ timeout: 15_000 });
		await expect(page.getByText('Answering is paused until this reconnects.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath('offline.png'), animations: 'disabled' });
		disconnected = false;
		await expect(boxes.nth(0)).toBeEnabled({ timeout: 30_000 });
		await expect(group.getByRole('textbox', { name: 'Note' })).toHaveValue('search later');
		await expect(boxes.nth(0)).toBeChecked();
		await expect(boxes.nth(2)).toBeChecked();
		await boxes.nth(0).uncheck();
		await boxes.nth(2).uncheck();
		const answer = page.getByRole('button', { name: 'Answer', exact: true });
		await expect(answer).toBeEnabled();
		await answer.click();
		await expect.poll(() => sent).not.toBeNull();
		expect(sent).toMatchObject({ option_ids: [], other_text: 'search later' });
		expect(sent).not.toHaveProperty('option_id');
	});
}

test('focus follows a sent held message to the next task', async ({ page }) => {
	let pending = true;
	await page.route('**/api/attention/queue**', (route) => route.request().url().includes('view=activity')
		? route.fulfill({ json: { count: pending ? 2 : 1, items: pending ? [heldItem, item] : [item], ranker: null, notifications: { count: 0, items: [] } } })
		: route.fallback());
	await page.route('**/api/held-replies/held-1/approve', (route) => {
		pending = false;
		return route.fulfill({ json: { state: 'sent' } });
	});
	await openAppPastLanding(page);
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	const send = page.getByRole('button', { name: 'Send', exact: true });
	await send.focus();
	await page.keyboard.press('Enter');
	await expect(page.locator('[data-activity-row]').filter({ hasText: detail.title })).toBeFocused();
});

for (const width of [1440, 393]) {
	test(`held message pauses and recovers on connection loss at ${width}`, async ({ page }, testInfo) => {
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		let disconnected = false;
		let dropConnection = () => {};
		await page.routeWebSocket((url) => url.pathname === '/ws', (socket) => {
			if (disconnected) { socket.close(); return; }
			dropConnection = () => socket.close();
			socket.connectToServer();
		});
		await page.route('**/api/attention/queue**', (route) => route.request().url().includes('view=activity')
			? route.fulfill({ json: { count: 1, items: [heldItem], ranker: null, notifications: { count: 0, items: [] } } })
			: route.fallback());
		await openAppPastLanding(page);
		if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
		if (width === 393) await page.locator('[data-activity-row]').filter({ hasText: heldDetail.title }).click();
		await expect(page.getByText(heldDetail.text)).toBeVisible();
		disconnected = true;
		dropConnection();
		await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled({ timeout: 15_000 });
		await expect(page.getByRole('button', { name: 'Don’t send' })).toBeDisabled();
		await expect(page.getByText('Sending and declining are paused until this reconnects.')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCSS('opacity', '0.5');
		await expect(page.getByRole('button', { name: 'Don’t send' })).toHaveCSS('opacity', '0.5');
		await page.screenshot({ path: testInfo.outputPath('held-offline.png'), animations: 'disabled' });
		disconnected = false;
		await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled({ timeout: 30_000 });
		await expect(page.getByText(heldDetail.text)).toBeVisible();
	});
}
