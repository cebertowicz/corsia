
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);

test.describe('@flow:project-sdlc @surface:navigation project lifecycle — create + settings + archive', () => {
	test('create (with accent) enters scope + persists; settings rename persists; archive leaves the active list', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'create/settings/archive logic is viewport-independent; verified on desktop');

		const stamp = Date.now();
		const name = `e2e-proj-${stamp}`;
		const accent = '#16a34a';

		await loginByUrl(page);

		await page.getByRole('button', { name: /Jump to/ }).click();
		await expect(page.locator('.palette')).toBeVisible();
		await page.locator('.palette .res').filter({ hasText: 'New project' }).click();
		const modal = page.getByRole('dialog');
		await expect(modal).toBeVisible();

		await expect(modal.getByRole('button', { name: 'Create project' })).toBeDisabled();
		await modal.getByRole('textbox', { name: 'Name' }).fill(name);
		await modal.getByRole('button', { name: `Accent ${accent}` }).click();
		await modal.getByRole('button', { name: 'Create project' }).click();

		await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });
		let created: { id: string; accent_color: string | null } | undefined;
		await expect.poll(async () => {
			const list = await (await request.get(`/api/projects?token=${tok}&status=all`)).json();
			created = (list.projects || []).find((p: { name: string }) => p.name === name);
			return created?.accent_color ?? null;
		}, { message: 'created project persisted with its chosen accent', timeout: 10_000 }).toBe(accent);
		const pid = created!.id;

		await saveEvidenceScreenshot(page, testInfo, 'project-sdlc-created');

		await page.getByRole('button', { name: 'Project settings' }).click();
		const nameField = page.getByRole('textbox', { name: 'Name' });
		await expect(nameField).toHaveValue(name, { timeout: 10_000 });
		const newName = `${name}-renamed`;
		await nameField.fill(newName);
		await nameField.blur();
		await expect.poll(async () => {
			return (await (await request.get(`/api/projects/${pid}?token=${tok}`)).json()).name;
		}, { message: 'rename persisted to the backend', timeout: 10_000 }).toBe(newName);

		await page.getByRole('button', { name: 'Archive' }).click();
		await expect.poll(async () => {
			const active = await (await request.get(`/api/projects?token=${tok}&status=active`)).json();
			return (active.projects || []).some((p: { id: string }) => p.id === pid);
		}, { message: 'archived project left the active list', timeout: 10_000 }).toBe(false);
		const all = await (await request.get(`/api/projects?token=${tok}&status=all`)).json();
		expect((all.projects || []).find((p: { id: string }) => p.id === pid)?.status).toBe('archived');
	});
});
