import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page, type Route, type TestInfo } from '@playwright/test';
import { lastUserMessage, loginByUrl, openSettings, saveEvidenceScreenshot } from './support/corsia';

const viewports = [
	{ name: '1440', width: 1440, height: 900 },
	{ name: '393', width: 393, height: 852 },
] as const;
const themes = ['light', 'dark'] as const;
const captureDir = process.env.CORSIA_CAPTURE_DIR;
const captureBuild = process.env.CORSIA_CAPTURE_BUILD;

async function capture(page: Page, testInfo: TestInfo, name: string): Promise<void> {
	if (captureBuild) await expect(page.locator('.version-overlay')).toContainText(`f ${captureBuild}`);
	if (!captureDir) return saveEvidenceScreenshot(page, testInfo, name, { fullPage: false });
	mkdirSync(captureDir, { recursive: true });
	await page.screenshot({ path: join(captureDir, `${name}.png`), fullPage: false, animations: 'disabled' });
}

for (const viewport of viewports) {
	for (const theme of themes) {
		test(`Connect an app captures the reviewed states at ${viewport.name} ${theme}`, async ({ page }, testInfo) => {
			let pendingConnect: Route | null = null;
			const messages: Record<string, unknown>[] = [];
			page.on('websocket', (socket) => socket.on('framesent', ({ payload }) => {
				const frame = JSON.parse(String(payload));
				if (frame.type === 'message') messages.push(frame);
			}));
			await page.emulateMedia({ colorScheme: theme });
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await page.route('**/api/mcp/servers**', async (route) => {
				if (route.request().method() === 'POST') {
					pendingConnect = route;
					return;
				}
				await route.fulfill({ json: { servers: [] } });
			});
			await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects: [{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }] } }));
			await loginByUrl(page, { waitForComposer: false });
			await openSettings(page);
			await (viewport.name === '1440'
				? page.getByRole('button', { name: 'Connected apps' })
				: page.getByRole('button', { name: 'Apps' })).click();
			await page.getByRole('button', { name: 'Connect an app', exact: true }).click();
			const dialog = page.getByRole('dialog');
			await expect(dialog.getByRole('heading', { name: 'What' })).toBeVisible();
			// Modal.svelte's open fly transition runs for 380ms; capture the settled surface.
			await page.waitForTimeout(400);
			await capture(page, testInfo, `what-${viewport.name}-${theme}`, { fullPage: false });

			await dialog.getByLabel('Google (email and calendar)').click();
			await expect(dialog.getByText('Opens a chat with CORSIA to sign in.')).toBeVisible();
			await capture(page, testInfo, `google-choice-${viewport.name}-${theme}`, { fullPage: false });
			await dialog.getByLabel('An app that runs on this Mac').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog.getByRole('heading', { name: 'Set it up' })).toBeVisible();
			await capture(page, testInfo, `mac-setup-${viewport.name}-${theme}`, { fullPage: false });

			if (viewport.name === '393') {
				await dialog.getByLabel('Name').fill('An exceptionally long connected app name that must remain readable');
				await dialog.getByLabel('What it runs').fill('npx -y @modelcontextprotocol/an-exceptionally-long-server-command-with-many-arguments --project garden-plan --verbose --safe-mode');
				await capture(page, testInfo, `long-mac-setup-${viewport.name}-${theme}`, { fullPage: false });
				await dialog.getByLabel('Name').fill('Files');
				await dialog.getByLabel('What it runs').fill('npx -y @example/app');
			}

			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByLabel('An app at a web address').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog.getByLabel('Web address')).toBeVisible();
			await capture(page, testInfo, `web-setup-${viewport.name}-${theme}`, { fullPage: false });
			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByLabel('An app that runs on this Mac').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await dialog.getByLabel('Name').fill('Files');
			await dialog.getByLabel('What it runs').fill('npx -y @example/app');
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog.getByRole('heading', { name: 'Where it works' })).toBeVisible();
			await capture(page, testInfo, `all-projects-${viewport.name}-${theme}`, { fullPage: false });

			await dialog.getByLabel('Select individually').click();
			await expect(dialog.getByText('Garden plan')).toBeVisible();
			await capture(page, testInfo, `select-individually-${viewport.name}-${theme}`, { fullPage: false });
			await dialog.getByLabel('All projects').click();
			await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
			await expect(dialog.getByRole('button', { name: 'Connect', exact: true })).toBeDisabled();
			await expect.poll(() => pendingConnect).not.toBeNull();
			await capture(page, testInfo, `mac-checking-${viewport.name}-${theme}`, { fullPage: false });
			await pendingConnect!.fulfill({ status: 400, json: { detail: 'The app did not answer' } });
			await expect(dialog.getByText('The app did not answer')).toBeVisible();
			await capture(page, testInfo, `mac-no-answer-${viewport.name}-${theme}`, { fullPage: false });

			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByLabel('Google (email and calendar)').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog).toBeHidden();
			await expect(lastUserMessage(page)).toContainText('Connect a Google account for all my projects');
			await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeEmpty();
			await expect.poll(() => messages.filter((frame) => frame.content === 'Connect a Google account for all my projects').length).toBe(1);
			await capture(page, testInfo, `google-after-continue-${viewport.name}-${theme}`, { fullPage: false });
		});
	}
}
