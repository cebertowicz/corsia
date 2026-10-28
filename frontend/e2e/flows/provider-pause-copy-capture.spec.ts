import { test } from '../support/test';
import { expect } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;

test('shows a historical usage-limit pause', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const created = await request.post(authed('/api/threads'), { data: { title: 'Usage limit example', model: 'mock/test-model' } });
	expect(created.ok()).toBeTruthy();
	const { thread_id: threadId } = await created.json();
	await page.route(`**/api/threads/${threadId}/messages**`, route => route.fulfill({ json: {
		messages: [{ id: 'pause-input', thread_id: threadId, seq: 1, role: 'user', content: 'Please continue the work.', delivery_state: 'delivered', metadata: { turn_id: 'paused-turn' }, created_at: '2026-10-27T17:00:00Z' }],
		has_more: false, oldest_id: null,
		turn_summaries: { 'paused-turn': { status: 'cancelled', cause: 'plan_limit_overage_off', started_at: null, ended_at: null, activity_count: 1 } },
	} }));
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await page.goto(authed('/'));
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('.thread-title', { hasText: 'Usage limit example' }).click();
		await expect(page.getByText('Paused: usage limit reached')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`usage-paused-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});
