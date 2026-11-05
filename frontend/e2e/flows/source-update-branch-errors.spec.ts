import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

for (const [code, diagnostic, headline, body] of [
	['checkout_no_upstream', 'has no upstream', "The branch my-feature isn't set to track another branch.", "Ask the CORSIA agent to set the branch it should track, then select Retry. CORSIA kept running and your code wasn't changed."],
	['checkout_diverged', 'and its upstream have diverged', 'The branch my-feature and the branch it tracks have both changed.', "Ask the CORSIA agent to combine their changes, then select Retry. CORSIA kept running and your code wasn't changed."],
] as const) {
	test(`@source-branch-error ${code} appears in Settings`, async ({ page }) => {
		await page.route('**/api/system/update-status**', route => route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				available: false, current_version: '1.0',
				operation: { operation_id: 'source-update-test', kind: 'update', status: 'failed', failure_code: code,
					log_tail: `Redeploy refused: branch my-feature ${diagnostic} — checkout left untouched`, outcome_seen_at: '2026-01-01T00:00:00Z' },
			}),
		}));
		await loginByUrl(page);
		await openSettings(page);
		await openSettingsSection(page, 'System');
		const row = page.locator('.last-update');
		await expect(row.getByText(headline)).toBeVisible();
		await expect(row.getByText(body)).toBeVisible();
		await expect(row.getByRole('button', { name: 'Retry' })).toBeVisible();
		await row.getByText('Details', { exact: true }).click();
		await expect(row.getByText(new RegExp(diagnostic))).toBeVisible();
	});
}
