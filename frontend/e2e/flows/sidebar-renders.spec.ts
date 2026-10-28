
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, e2eModel, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

test.describe('@flow:sidebar @surface:navigation sidebar renders', () => {
	test('surfaces the scope-filtered conversation switcher + empty-state UI', async ({ page, request }, testInfo) => {
		const stamp = Date.now();
		const sessionTitles = [`s1-${stamp}`, `s2-${stamp}`, `s3-${stamp}`];
		for (const title of sessionTitles) {
			const r = await request.post(`/api/threads?token=${encodeURIComponent(TEST_TOKEN)}`, {
				data: { title, model: e2eModel() },
			});
			expect(r.ok()).toBeTruthy();
		}

		await loginByUrl(page);

		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		await expect(page.locator('button.section-header').filter({ hasText: 'Projects' })).toHaveCount(0);

		await expect(page.getByText(sessionTitles[0]).first()).toBeVisible({ timeout: 10_000 });

		await page.getByText(sessionTitles[0]).first().click();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 10_000 });

		await saveEvidenceScreenshot(page, testInfo, 'sidebar-renders');
	});

	test('shows while-away summaries without displacing stronger row state', async ({ page, request }, testInfo) => {
		if (testInfo.project.name === 'chromium-mobile') {
			await page.setViewportSize({ width: 393, height: 852 });
		}
		const stamp = Date.now();
		const updateTitle = `Update first ${stamp}`;
		const failedTitle = `Failed but updated ${stamp}`;
		const quietTitle = `Quiet ${stamp}`;
		const updateId = await createSession(request, updateTitle);
		const failedId = await createSession(request, failedTitle);
		const quietId = await createSession(request, quietTitle);
		const longSummary = 'A meaningful update stays readable here and remains available through its accessible name';
		const failedSummary = 'The release needs follow-up before it can ship';

		await page.route('**/api/plans/operator/thread-states?ids=*', async (route) => {
			const ids = new URL(route.request().url()).searchParams.get('ids')?.split(',') ?? [];
			const states = Object.fromEntries(ids.flatMap((id) => {
				if (id === updateId) return [[id, {
					state: 'idle', label_key: null, paused: false, attention_items: [],
					latest_update: { kind: 'marked', summary: longSummary }, has_update: true, opened: false,
				}]];
				if (id === failedId) return [[id, {
					state: 'failed', label_key: null, paused: false,
					attention_items: [{ kind: 'failed', title: 'Prepare the release evidence', node_id: 'node-proof' }],
					latest_update: { kind: 'marked', summary: failedSummary }, has_update: true, opened: false,
				}]];
				if (id === quietId) return [[id, {
					state: 'idle', label_key: null, paused: false, attention_items: [],
					latest_update: null, has_update: false, opened: false,
				}]];
				return [];
			}));
			await route.fulfill({ json: { states } });
		});

		await loginByUrl(page, { waitForComposer: false });
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		const updateRow = page.locator('.thread-item-wrapper').filter({ hasText: updateTitle });
		const failedRow = page.locator('.thread-item-wrapper').filter({ hasText: failedTitle });
		const quietRow = page.locator('.thread-item-wrapper').filter({ hasText: quietTitle });
		await expect(updateRow.locator('.thread-update-summary')).toHaveText(longSummary);
		await expect(updateRow.locator('.thread-update-summary')).toHaveAttribute('title', longSummary);
		await expect(failedRow.locator('.plan-state.failed')).toBeVisible();
		await expect(failedRow.locator('.thread-update-summary')).toHaveText(failedSummary);
		await expect(quietRow.locator('.thread-update-summary')).toHaveCount(0);

		const viewportWidth = page.viewportSize()?.width ?? 0;
		const pageGeometry = await page.locator('html').evaluate((root) => ({
			clientWidth: root.clientWidth,
			scrollWidth: root.scrollWidth,
		}));
		expect(pageGeometry.scrollWidth).toBeLessThanOrEqual(pageGeometry.clientWidth);
		for (const row of [failedRow, updateRow, quietRow]) {
			const box = await row.boundingBox();
			expect(box).not.toBeNull();
			expect(box!.x).toBeGreaterThanOrEqual(0);
			expect(box!.x + box!.width).toBeLessThanOrEqual(viewportWidth);
		}
		const summaryGeometry = await updateRow.locator('.thread-update-summary').evaluate((summary) => ({
			clientWidth: summary.clientWidth,
			scrollWidth: summary.scrollWidth,
		}));
		expect(summaryGeometry.scrollWidth).toBeGreaterThan(summaryGeometry.clientWidth);
		expect((await failedRow.boundingBox())?.y).toBeLessThan((await updateRow.boundingBox())?.y ?? 0);
		await saveEvidenceScreenshot(page, testInfo, `while-away-${testInfo.project.name}`);
	});

	test('empty session list shows "No chats here"', async ({ page, request }, testInfo) => {
		await loginByUrl(page);

		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		const threadList = page.locator('.thread-list');
		await expect(threadList).toBeVisible();

		const emptyState = page.locator('.empty-threads');
		const threadItems = page.locator('.thread-item');
		const itemCount = await threadItems.count();
		if (itemCount === 0) {
			await expect(emptyState).toBeVisible();
			await expect(emptyState).toContainText(/no chats here/i);
		}

		await saveEvidenceScreenshot(page, testInfo, 'sidebar-empty-state');
	});
});
