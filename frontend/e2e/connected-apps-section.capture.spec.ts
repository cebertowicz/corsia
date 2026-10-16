import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';
import { loginByUrl, openSettings } from './support/corsia';

const outputDir = '/Users/jarvis/Corsia/projects/corsia/shared/integrations/2026-10-16-apps-section.assets';
const captureEnabled = process.env.CORSIA_CAPTURE_CONNECTED_APPS === '1';

test.describe('Connected apps Settings section capture', () => {
	test.skip(!captureEnabled, 'Capture-only proof; enable explicitly when producing the evidence pack.');

	test('captures the Settings section states', async ({ page }) => {
		mkdirSync(outputDir, { recursive: true });
		let state: 'empty' | 'apps' = 'empty';
		await page.route('**/api/mcp/servers**', async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			const servers = state === 'empty' ? [] : [
				{ id: 'notes', name: 'Notes', state: 'working', tool_count: 2, reach: { mode: 'all', project_ids: [], outside_projects: false } },
				{ id: 'linear', name: 'Linear', state: 'awaiting_approval', reach: { mode: 'all', project_ids: [], outside_projects: false } },
			];
			await route.fulfill({ json: { servers, sign_in_here: false } });
		});

		for (const theme of ['light', 'dark'] as const) {
			for (const viewport of [1440, 393]) {
				state = 'empty';
				await page.setViewportSize({ width: viewport, height: 980 });
				await page.emulateMedia({ colorScheme: theme });
				await loginByUrl(page, { waitForComposer: false });
				await openSettings(page);
				const appsButton = viewport > 768
					? page.getByRole('button', { name: 'Connected apps' })
					: page.getByRole('button', { name: 'Apps' });
				await appsButton.click();
				await expect(page.getByRole('heading', { name: 'Connected apps' })).toBeVisible();
				await expect(page.getByText('No connected apps yet.')).toBeVisible();
				await page.screenshot({ path: join(outputDir, `connected-apps-empty-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				state = 'apps';
				await page.reload();
				await openSettings(page);
				await (viewport > 768
					? page.getByRole('button', { name: 'Connected apps' })
					: page.getByRole('button', { name: 'Apps' })).click();
				await expect(page.getByText('Connected · 2 actions')).toBeVisible();
				await expect(page.getByText('Waiting for your approval')).toBeVisible();
				await page.screenshot({ path: join(outputDir, `connected-apps-after-approve-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				await (viewport > 768
					? page.getByRole('button', { name: 'System' })
					: page.getByRole('button', { name: 'System' })).click();
				await expect(page.getByRole('heading', { name: 'Connected apps' })).toHaveCount(0);
				await page.screenshot({ path: join(outputDir, `settings-system-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });
			}
		}
	});
});
