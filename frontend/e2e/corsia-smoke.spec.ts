import { test } from './support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, openSettings, saveEvidenceScreenshot, TEST_TOKEN } from './support/corsia';

test.describe('@surface:auth @surface:chat @surface:navigation CORSIA shell', () => {
	test('unauthenticated users see the sign-in form', async ({ page }, testInfo) => {
		await page.goto('/');

		await expect(page.getByRole('heading', { name: 'CORSIA' })).toBeVisible();
		await expect(page.getByPlaceholder('Username')).toBeVisible();
		await expect(page.getByPlaceholder('Password')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'auth-gate');
	});

	test('token login reaches the empty chat shell', async ({ page }, testInfo) => {
		await loginByUrl(page);

		// The New Chat FAB lives in the sidebar drawer and is hidden on mobile
		// while the drawer is closed (NewChatFab.svelte .sidebar-closed rule,
		// intentional per 471ed249 and its unit test coverage).
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await expect(page.getByRole('button', { name: 'New chat' }).first()).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'chat-shell-empty');
	});

	test('settings opens from the sidebar without leaving the app shell', async ({ page }, testInfo) => {
		await loginByUrl(page);
		await openSettings(page);

		// Settings sections are Providers, System, Keys and Debug (SettingsView.svelte); Measurement is a sidebar rail link.
		await expect(page.getByRole('button', { name: 'Providers' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'System' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'settings-providers');
	});
});

test.describe('@surface:api @surface:chat session API', () => {
	test('creates and lists a chat session against the isolated test backend', async ({ request }) => {
		const title = `E2E session ${Date.now()}`;
		const sessionId = await createSession(request, title);

		const response = await request.get(`/api/threads?token=${encodeURIComponent(TEST_TOKEN)}`);
		expect(response.ok()).toBeTruthy();
		const body = await response.json();
		expect(body.threads).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ id: sessionId, title }),
			]),
		);
	});
});
