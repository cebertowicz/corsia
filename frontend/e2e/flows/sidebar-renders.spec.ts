
import { mkdir } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
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

	test('renders Stuck in every requested row mode', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name === 'chromium-mobile', 'One explicit desktop/mobile proof run owns the captures.');
		const title = `Stuck ${Date.now()}`;
		const threadId = await createSession(request, title);
		const proofDir = process.env.CORSIA_STUCK_PROOF_DIR;
		if (!proofDir) throw new Error('CORSIA_STUCK_PROOF_DIR is required for Stuck-row evidence');
		await mkdir(proofDir, { recursive: true });
		await page.route('**/api/plans/operator/thread-states?ids=*', async (route) => {
			const ids = new URL(route.request().url()).searchParams.get('ids')?.split(',') ?? [];
			await route.fulfill({ json: { states: Object.fromEntries(ids.flatMap((id) => id === threadId ? [[id, {
				state: 'stuck', label_key: null, paused: false, attention_items: [],
				stuck_overdue_waits: 3, next_overdue_at: null, has_update: true, opened: false,
			}]] : [])) } });
		});
		await loginByUrl(page, { waitForComposer: false });
		const row = page.locator('.thread-item-wrapper').filter({ hasText: title });
		const mark = row.getByRole('button', { name: 'Stuck · 3 waits past the deadline' });
		await expect(mark).toHaveText('3 Stuck');
		await expect(mark).toHaveAttribute('title', 'Stuck · 3 waits past the deadline');
		await expect(row.locator('.update-dot')).toHaveCount(0);

		for (const [name, width, height, colorScheme] of [
			['desktop-light', 1280, 800, 'light'], ['desktop-dark', 1280, 800, 'dark'],
			['mobile-light', 393, 852, 'light'], ['mobile-dark', 393, 852, 'dark'],
		] as const) {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme });
			await expect(mark).toBeVisible();
			await page.screenshot({ path: `${proofDir}/${name}.png` });
		}
		await mark.focus();
		await expect(mark).toBeFocused();
		await page.screenshot({ path: `${proofDir}/keyboard-focus-title.png` });
	});

	test('empty session list shows "No conversations yet"', async ({ page, request }, testInfo) => {
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
			await expect(emptyState).toContainText(/no conversations/i);
		}

		await saveEvidenceScreenshot(page, testInfo, 'sidebar-empty-state');
	});
});
