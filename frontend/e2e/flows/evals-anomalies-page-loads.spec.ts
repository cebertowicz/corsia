
import { expect, test } from '@playwright/test';
import { captureConsoleErrors, loginByUrl, saveEvidenceScreenshot } from '../support/corsia';

async function pollForRenderedState(page: import('@playwright/test').Page): Promise<void> {
	const empty = page.locator('[data-testid="anomalies-empty"]');
	const issueBucket = page.locator('[data-testid="issue-type-bucket"]').first();
	const anomalyBucket = page.locator('[data-testid="anomaly-bucket"]').first();
	const error = page.locator('[data-testid="anomalies-error"]');
	await expect.poll(async () => {
		const counts = await Promise.all([
			empty.count(), issueBucket.count(), anomalyBucket.count(), error.count(),
		]);
		return counts[0] + counts[1] + counts[2] + counts[3];
	}, { timeout: 15_000 }).toBeGreaterThan(0);
}

const NAV_BUTTON_RE = /^Issues$/;
const PAGE_HEADING_RE = /Issues|Anomalies/;

test.describe('@flow:evals-anomalies @surface:evals evals anomalies page loads', () => {
	test('renders heading and an empty-state OR buckets at desktop width', async ({ page }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		await loginByUrl(page);

		await page.goto('/evals/anomalies');
		await expect(page.locator('h1')).toContainText(PAGE_HEADING_RE);
		await pollForRenderedState(page);

		await saveEvidenceScreenshot(page, testInfo, 'evals-anomalies-desktop');

		const sliceErrors = consoleErrors.filter((m) =>
			!m.includes('/api/me') && !m.includes('Failed to load resource'),
		);
		expect(sliceErrors).toEqual([]);
	});

	test('renders at mobile width 390x844 (iPhone 13 default)', async ({ page }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		await page.setViewportSize({ width: 390, height: 844 });
		await loginByUrl(page);

		await page.goto('/evals/anomalies');
		await expect(page.locator('h1')).toContainText(PAGE_HEADING_RE);
		await pollForRenderedState(page);

		const navButton = page.getByRole('button', { name: NAV_BUTTON_RE }).first();
		await expect(navButton).toBeVisible();

		const overflow = await page.evaluate(() => {
			const html = document.documentElement;
			return html.scrollWidth - html.clientWidth;
		});
		expect(overflow).toBeLessThanOrEqual(1); // tolerate 1px sub-pixel

		await saveEvidenceScreenshot(page, testInfo, 'evals-anomalies-mobile-390x844');

		const sliceErrors = consoleErrors.filter((m) =>
			!m.includes('/api/me') && !m.includes('Failed to load resource'),
		);
		expect(sliceErrors).toEqual([]);
	});

	test('evals nav exposes an Issues entry that routes correctly', async ({ page }) => {
		await loginByUrl(page);
		await page.goto('/evals');
		const navButton = page.getByRole('button', { name: NAV_BUTTON_RE }).first();
		await expect(navButton).toBeVisible();
		await navButton.click();
		await expect(page).toHaveURL(/\/evals\/anomalies/);
	});
});
