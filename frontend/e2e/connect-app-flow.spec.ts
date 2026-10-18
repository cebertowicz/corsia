import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page, type Route, type TestInfo } from '@playwright/test';
import { createSession, lastUserMessage, loginByUrl, openSettings, openSettingsSection, saveEvidenceScreenshot, TEST_SESSION_TOKEN, userMessages } from './support/corsia';
import { readMockCallCount, seedMockResponses } from './support/mock';

const viewports = [
	{ name: '1440', width: 1440, height: 900 },
	{ name: '393', width: 393, height: 852 },
] as const;
const themes = ['light', 'dark'] as const;
const captureDir = process.env.CORSIA_CAPTURE_DIR;
const captureBuild = process.env.CORSIA_CAPTURE_BUILD;

async function capture(page: Page, testInfo: TestInfo, name: string): Promise<void> {
	if (captureBuild) {
		const versionOverlay = page.locator('.version-overlay');
		await expect(versionOverlay).toContainText(`f ${captureBuild}`);
		await expect(versionOverlay).not.toContainText('⇢');
		await expect(versionOverlay).not.toContainText('†');
	}
	if (!captureDir) return saveEvidenceScreenshot(page, testInfo, name, { fullPage: false });
	mkdirSync(captureDir, { recursive: true });
	await page.screenshot({ path: join(captureDir, `${name}.png`), fullPage: false, animations: 'disabled' });
}

for (const viewport of viewports) {
	for (const theme of themes) {
		test(`Connect an app captures the reviewed states at ${viewport.name} ${theme}`, async ({ page }, testInfo) => {
			let pendingConnect: Route | null = null;
			const messages: Record<string, unknown>[] = [];
			const corsiaThreadId = await createSession(page.request, `CORSIA Google connect ${viewport.name} ${theme}`);
			// Designation is a person decision. The disposable harness provisions a
			// browser session precisely for that protected route; its API token is
			// deliberately insufficient.
			const designated = await page.request.put(`/api/corsia-agent?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`, {
				data: { thread_id: corsiaThreadId },
			});
			expect(designated.ok(), await designated.text()).toBeTruthy();
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
			// The real designation above makes this the actual CORSIA thread.
			await page.route('**/api/system/update-status**', (route) => route.fulfill({ json: {
				branch: 'dev', current_sha: 'fixture', remote_sha: 'fixture', commits_behind: 0, available: false, checked_at: 1, error: null,
			} }));
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

			await dialog.getByLabel('Google (email, calendar and files)').click();
			await expect(dialog.getByText('Opens a chat with CORSIA to sign in.')).toBeVisible();
			await capture(page, testInfo, `google-choice-${viewport.name}-${theme}`, { fullPage: false });
			await dialog.getByLabel('An app that runs on this computer').click();
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
			await dialog.getByLabel('An app that runs on this computer').click();
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
			await pendingConnect!.fulfill({ json: {
				id: 'files', name: 'Files', state: 'failed', failure_class: 'timed_out', revision: 1,
				reach: { mode: 'all', project_ids: [], outside_projects: true },
			} });
			await expect(dialog.getByText("Couldn't connect: no response")).toBeVisible();
			await expect(dialog.getByText("The app didn't respond within 10 seconds. Go back and compare What it runs with the app's setup instructions, then try again. It may also need something that isn't installed on your computer; its setup instructions say what.")).toBeVisible();
			await capture(page, testInfo, `mac-no-answer-${viewport.name}-${theme}`, { fullPage: false });

			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await expect(dialog.getByLabel('What it runs')).toBeFocused();
			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			// Result states keep their full recovery/action surfaces in the reviewed frames.
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			pendingConnect = null;
			await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
			await expect.poll(() => pendingConnect).not.toBeNull();
			await pendingConnect!.fulfill({ json: {
				id: 'files-needs-token', name: 'Files', state: 'needs_token', revision: 1,
				reach: { mode: 'all', project_ids: [], outside_projects: true },
			} });
			await expect(dialog.getByRole('heading', { name: 'Files: Needs a key' })).toBeVisible();
			await expect(dialog.getByText('Files needs a key before agents can use it: a code Files gives you so other apps can use your account (Files may call it a token or an API key). Create one in your Files settings.')).toBeVisible();
			await expect(dialog.getByRole('button', { name: 'Set key', exact: true })).toBeVisible();
			await capture(page, testInfo, `needs-token-${viewport.name}-${theme}`);
			await dialog.getByRole('button', { name: 'Set key', exact: true }).click();
			await openSettingsSection(page, 'Connected apps');
			await page.getByRole('button', { name: 'Connect an app', exact: true }).click();
			const signInDialog = page.getByRole('dialog');
			await signInDialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await signInDialog.getByLabel('Name').fill('Files');
			await signInDialog.getByLabel('What it runs').fill('npx -y @example/app');
			await signInDialog.getByRole('button', { name: 'Continue', exact: true }).click();
			pendingConnect = null;
			await signInDialog.getByRole('button', { name: 'Connect', exact: true }).click();
			await expect.poll(() => pendingConnect).not.toBeNull();
			await pendingConnect!.fulfill({ json: {
				id: 'files-needs-sign-in', name: 'Files', state: 'needs_sign_in', revision: 1,
				reach: { mode: 'all', project_ids: [], outside_projects: true },
			} });
			await expect(signInDialog.getByRole('heading', { name: 'Files: Needs you to sign in' })).toBeVisible();
			await expect(signInDialog.getByText("Signing in lets the agents that can use an app act as you in it. You sign in on the app's own page in your browser, so CORSIA never sees your password.")).toBeVisible();
			await expect(signInDialog.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
			await capture(page, testInfo, `needs-sign-in-${viewport.name}-${theme}`);
			await signInDialog.getByRole('button', { name: 'Sign in', exact: true }).click();
			await expect(signInDialog).toBeHidden();
			await expect(page.getByRole('button', { name: 'Connect an app', exact: true })).toBeVisible();
			await page.getByRole('button', { name: 'Connect an app', exact: true }).click();
			const googleDialog = page.getByRole('dialog');

			await seedMockResponses(page.request, [
				{
					tool_calls: [{
						id: `google-post-update-${viewport.name}-${theme}`,
						name: 'post_update',
						arguments: JSON.stringify({ text: "Let's connect a Google account for all your projects. Which account should agents use?" }),
					}],
					finish_reason: 'tool_calls',
					match_thread: corsiaThreadId,
				},
				{ content: 'I asked which Google account to use.', finish_reason: 'stop', match_thread: corsiaThreadId },
			]);
			await googleDialog.getByLabel('Google (email, calendar and files)').click();
			await googleDialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(googleDialog).toBeHidden();
			await expect(lastUserMessage(page)).toContainText('Connect a Google account for all my projects');
			await expect(userMessages(page)).toHaveCount(1);
			await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeEmpty();
			await expect.poll(() => messages.filter((frame) => frame.content === 'Connect a Google account for all my projects').length).toBe(1);
			const googleReply = page.locator('main .message.assistant').filter({ hasText: "Let's connect a Google account for all your projects. Which account should agents use?" });
			await expect(googleReply).toHaveCount(1);
			await expect(googleReply).toBeVisible();
			await expect.poll(() => readMockCallCount(page.request), { timeout: 20_000 }).toBe(2);
			await capture(page, testInfo, `google-reply-${viewport.name}-${theme}`, { fullPage: false });
		});
	}
}
