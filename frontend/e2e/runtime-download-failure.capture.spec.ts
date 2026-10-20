import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';
import { openAppByUrl } from './support/corsia';

const outputDir = process.env.CORSIA_CAPTURE_DIR ?? 'test-results/runtime-download-failure';
const captureEnabled = process.env.CORSIA_CAPTURE_RUNTIME_DOWNLOAD_FAILURE === '1';
const allProjects = { mode: 'all', project_ids: [], outside_projects: false };

const failedApp = (failureClass: 'cannot_start' | 'runtime_download_failed') => ({
	id: 'files',
	name: 'Files',
	state: 'failed',
	failure_class: failureClass,
	tool_count: 0,
	reach: allProjects,
});

test.describe('Runtime download failure capture', () => {
	test.skip(!captureEnabled, 'Capture-only proof; enable explicitly when producing the evidence pack.');

	test('captures the distinct managed runtime download failure states', async ({ page }) => {
		let failureClass: 'cannot_start' | 'runtime_download_failed' = 'runtime_download_failed';
		await page.route('**/api/mcp/servers**', async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			return route.fulfill({ json: { servers: [failedApp(failureClass)], accounts: [], sign_in_here: false } });
		});
		await page.route('**/api/projects**', async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			return route.fulfill({ json: { projects: [] } });
		});

		await page.setViewportSize({ width: 1280, height: 900 });
		await openAppByUrl(page);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		await page.locator('button[title="Settings"]').first().click();
		await page.locator('.settings-view .desktop-nav').getByRole('button', { name: 'Connected apps', exact: true }).click();
		await expect(page.getByText("Couldn't connect: download failed")).toBeVisible();

		mkdirSync(outputDir, { recursive: true });
		await page.screenshot({ path: join(outputDir, 'runtime-download-failed-closed-1280.png'), animations: 'disabled' });

		await page.getByRole('button', { name: /^Files\b/ }).click();
		const recovery = page.getByText("CORSIA couldn't download what this app needs to run on this computer. Check that you're connected to the internet, then press Reconnect. If it keeps happening, your network may be blocking the download.");
		await expect(recovery).toBeVisible();
		await expect(page.getByRole('button', { name: 'Reconnect', exact: true })).toBeVisible();
		await page.screenshot({ path: join(outputDir, 'runtime-download-failed-open-1280.png'), animations: 'disabled' });

		failureClass = 'cannot_start';
		await page.reload();
		await page.locator('button[title="Settings"]').first().click();
		await page.locator('.settings-view .desktop-nav').getByRole('button', { name: 'Connected apps', exact: true }).click();
		await expect(page.getByText("Couldn't connect: app didn't start")).toBeVisible();
	});
});
